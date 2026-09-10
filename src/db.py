"""SQLite storage, shared by the scraper (src/main.py) and the dashboard
(src/dashboard/app.py) -- WAL mode is enabled so both can safely read/write
the same file concurrently (scraper is a periodic writer, dashboard is a
long-running reader/writer).
"""

import sqlite3
from pathlib import Path
from typing import Any

from .models import Job

SCHEMA = """
-- Matched jobs + application tracking (what the dashboard shows/edits).
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    work_mode TEXT,
    source_site TEXT NOT NULL,
    found_at TEXT NOT NULL,
    status TEXT,
    description TEXT
);

-- Every URL a scraper has ever evaluated, matched or not, so a non-matching
-- listing (the vast majority of any general search) is never re-fetched and
-- re-scored on every cycle -- separate from `jobs` so that table stays a
-- clean "jobs I might apply to" list rather than every posting ever seen.
CREATE TABLE IF NOT EXISTS seen_urls (
    url TEXT PRIMARY KEY,
    source_site TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scraper_state (
    source_site TEXT PRIMARY KEY,
    blocked_until TEXT,
    last_checked_at TEXT,
    last_error TEXT
);

-- Site registry: dashboard-editable source of truth for which sites are in
-- rotation. `has_scraper` is a display flag only -- SCRAPER_CLASSES in
-- main.py is what actually decides whether a scraper module runs, so a
-- site added from the dashboard never fakes a working scraper.
CREATE TABLE IF NOT EXISTS sites (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    base_url TEXT,
    status TEXT NOT NULL DEFAULT 'paused', -- 'active' | 'paused'
    has_scraper INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    last_checked_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cvs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    label TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

-- Key/value settings, notably the notification-rules mode + its parameters.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Pending matches under a batching notification mode; also the send-log
-- used for rate-limit math (rows with sent_at set, within the last hour).
CREATE TABLE IF NOT EXISTS notification_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    queued_at TEXT NOT NULL,
    sent_at TEXT,
    origin_tag TEXT
);

-- Chat history for the AI agent's chat interface (src/agent/chat.py).
-- `platform` is "telegram" | "dashboard" | "whatsapp"; `thread_id` is a
-- fixed "default" per platform (single-user bot, no multi-session need) --
-- kept as a real column rather than hardcoding "default" in queries so a
-- future multi-session dashboard doesn't need a schema change.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL, -- 'user' | 'assistant'
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "notification_mode": "immediate",
    "rate_limit_per_hour": "10",
    "digest_minutes": "60",
    "batch_count": "5",
    "last_digest_sent_at": "",
    "scraper_paused": "false",
    "telegram_last_update_id": "0",
    # AI agent status/degradation state -- see src/agent/status.py and
    # src/agent/loop.py's graceful-degradation handling.
    "agent_status": "מאותחל",
    "agent_status_updated_at": "",
    "agent_last_cycle_at": "",
    "agent_ollama_degraded": "false",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns/rows introduced after a DB may already have been created
    (idempotent -- safe to run on every connect())."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column in (
        "description",
        "date_applied",
        "cv_version_used",
        "cover_letter",
        "notes",
        "relevance_score",
        "relevance_rationale",
        "remind_at",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")

    queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_queue)").fetchall()}
    if "origin_tag" not in queue_columns:
        conn.execute("ALTER TABLE notification_queue ADD COLUMN origin_tag TEXT")

    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    conn.commit()


def _seed_sites_from_config(conn: sqlite3.Connection, sites_config: dict[str, Any]) -> None:
    """One-time bootstrap of the `sites` registry from config.yaml's sites:
    block, only if the table is empty -- after that, the dashboard owns it."""
    existing = conn.execute("SELECT 1 FROM sites LIMIT 1").fetchone()
    if existing or not sites_config:
        return
    for name, cfg in sites_config.items():
        conn.execute(
            """
            INSERT INTO sites (name, display_name, base_url, status, has_scraper, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                name,
                cfg.get("display_name", name),
                cfg.get("base_url", ""),
                "active" if cfg.get("enabled") else "paused",
                1 if cfg.get("enabled") else 0,
                None,
            ),
        )
    conn.commit()


def connect(db_path: str, sites_config: dict[str, Any] | None = None) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the dashboard's per-request connection (see
    # dashboard/app.py's get_conn dependency) can be opened in FastAPI's
    # threadpool and then used from an async route's event-loop thread --
    # each request still gets its own connection, never shared concurrently.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    _seed_sites_from_config(conn, sites_config or {})
    return conn


def is_seen(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_seen(conn: sqlite3.Connection, url: str, source_site: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_urls (url, source_site, first_seen_at) VALUES (?, ?, datetime('now'))",
        (url, source_site),
    )
    conn.commit()


def save_job(conn: sqlite3.Connection, job: Job) -> int:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO jobs
            (url, title, company, location, work_mode, source_site, found_at, status, description)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.url,
            job.title,
            job.company,
            job.location,
            job.work_mode,
            job.source_site,
            job.found_at,
            job.status,
            job.description,
        ),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM jobs WHERE url = ?", (job.url,)).fetchone()
    return row[0]


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.row_factory = None
    return row


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    source_site: str | None = None,
    work_mode: str | None = None,
    sort: str = "found_at",
    order: str = "desc",
    exclude_statuses: list[str] | None = None,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    elif exclude_statuses:
        # Only applied when no explicit status filter is chosen -- e.g. a
        # job marked "not_relevant" stops showing up as a pending item by
        # default, but is still reachable by explicitly filtering for it.
        placeholders = ",".join("?" for _ in exclude_statuses)
        clauses.append(f"COALESCE(status, '') NOT IN ({placeholders})")
        params.extend(exclude_statuses)
    if source_site:
        clauses.append("source_site = ?")
        params.append(source_site)
    if work_mode:
        clauses.append("work_mode = ?")
        params.append(work_mode)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sort_col = sort if sort in {"found_at", "title", "company", "status", "source_site"} else "found_at"
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY {sort_col} {order_dir}", params).fetchall()
    conn.row_factory = None
    return rows


def update_job_tracking(
    conn: sqlite3.Connection,
    job_id: int,
    status: str | None,
    date_applied: str | None,
    cv_version_used: str | None,
    cover_letter: str | None,
    notes: str | None,
) -> None:
    conn.execute(
        """
        UPDATE jobs SET status = ?, date_applied = ?, cv_version_used = ?, cover_letter = ?, notes = ?
        WHERE id = ?
        """,
        (status, date_applied, cv_version_used, cover_letter, notes, job_id),
    )
    conn.commit()


def mark_applied(conn: sqlite3.Connection, job_id: int) -> None:
    """Used by the notification-button "Applied" action -- only touches
    status/date_applied, unlike update_job_tracking() which overwrites every
    tracking field (a chat reply has no cv_version_used/cover_letter/notes
    to send, so it must not blank out ones already set from the dashboard)."""
    conn.execute(
        "UPDATE jobs SET status = 'applied', date_applied = datetime('now') WHERE id = ?",
        (job_id,),
    )
    conn.commit()


def mark_not_relevant(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'not_relevant' WHERE id = ?", (job_id,))
    conn.commit()


def set_job_relevance(conn: sqlite3.Connection, job_id: int, score: int, rationale: str) -> None:
    """Called by the AI agent's review step (src/agent/scoring.py) and by
    discovery (src/agent/discovery.py) -- score/rationale are stored
    regardless of whether the job ends up notified, so a below-threshold
    scraper match is still visible (with its score) in the dashboard."""
    conn.execute(
        "UPDATE jobs SET relevance_score = ?, relevance_rationale = ? WHERE id = ?",
        (score, rationale, job_id),
    )
    conn.commit()


def job_exists_by_url(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone()
    return row is not None


def title_company_exists(conn: sqlite3.Connection, title: str, company: str) -> bool:
    """Secondary dedup for the AI agent's own discovery (src/agent/discovery.py)
    -- catches the same posting found under a different URL than a scraper
    already stored it under. Normalized case/whitespace-insensitive compare;
    `jobs.url` UNIQUE already handles the exact-URL case via save_job()."""
    norm_title = " ".join((title or "").split()).lower()
    norm_company = " ".join((company or "").split()).lower()
    if not norm_title:
        return False
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE lower(trim(title)) = ? AND lower(trim(COALESCE(company, ''))) = ? LIMIT 1",
        (norm_title, norm_company),
    ).fetchone()
    return row is not None


def schedule_reminder(conn: sqlite3.Connection, job_id: int, remind_at_iso: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'remind_later', remind_at = ? WHERE id = ?",
        (remind_at_iso, job_id),
    )
    conn.commit()


def due_reminders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'remind_later' AND remind_at IS NOT NULL AND remind_at <= datetime('now')"
    ).fetchall()
    conn.row_factory = None
    return rows


def clear_reminder(conn: sqlite3.Connection, job_id: int) -> None:
    """Called right after a due reminder is re-sent (src/dashboard/reminder_checker.py)
    -- back to a plain 'found' job so it's a normal pending item again rather
    than staying stuck in 'remind_later' forever."""
    conn.execute("UPDATE jobs SET status = 'found', remind_at = NULL WHERE id = ?", (job_id,))
    conn.commit()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT COALESCE(status, 'found') AS s, COUNT(*) FROM jobs GROUP BY s").fetchall()
    return dict(rows)


def source_site_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT source_site, COUNT(*) FROM jobs GROUP BY source_site").fetchall()
    return dict(rows)


def get_blocked_until(conn: sqlite3.Connection, source_site: str) -> str | None:
    """Returns an ISO-8601 UTC timestamp string, or None if not currently blocked."""
    row = conn.execute(
        "SELECT blocked_until FROM scraper_state WHERE source_site = ?", (source_site,)
    ).fetchone()
    return row[0] if row else None


def set_blocked_until(conn: sqlite3.Connection, source_site: str, blocked_until_iso: str, last_error: str) -> None:
    conn.execute(
        """
        INSERT INTO scraper_state (source_site, blocked_until, last_checked_at, last_error)
        VALUES (?, ?, datetime('now'), ?)
        ON CONFLICT(source_site) DO UPDATE SET
            blocked_until = excluded.blocked_until,
            last_checked_at = excluded.last_checked_at,
            last_error = excluded.last_error
        """,
        (source_site, blocked_until_iso, last_error),
    )
    conn.commit()


def mark_checked(conn: sqlite3.Connection, source_site: str) -> None:
    conn.execute(
        """
        INSERT INTO scraper_state (source_site, blocked_until, last_checked_at, last_error)
        VALUES (?, NULL, datetime('now'), NULL)
        ON CONFLICT(source_site) DO UPDATE SET
            blocked_until = NULL,
            last_checked_at = excluded.last_checked_at,
            last_error = NULL
        """,
        (source_site,),
    )
    conn.commit()
    conn.execute("UPDATE sites SET last_checked_at = datetime('now'), last_error = NULL WHERE name = ?", (source_site,))
    conn.commit()


def mark_site_error(conn: sqlite3.Connection, source_site: str, error: str) -> None:
    conn.execute(
        "UPDATE sites SET last_checked_at = datetime('now'), last_error = ? WHERE name = ?", (error, source_site)
    )
    conn.commit()


# --- Sites registry (dashboard-managed) -------------------------------------


def get_active_sites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sites WHERE status = 'active' ORDER BY name").fetchall()
    conn.row_factory = None
    return rows


def list_sites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sites ORDER BY name").fetchall()
    conn.row_factory = None
    return rows


def add_site(conn: sqlite3.Connection, name: str, display_name: str, base_url: str, notes: str) -> None:
    conn.execute(
        """
        INSERT INTO sites (name, display_name, base_url, status, has_scraper, notes, created_at)
        VALUES (?, ?, ?, 'paused', 0, ?, datetime('now'))
        """,
        (name, display_name, base_url, notes),
    )
    conn.commit()


def set_site_status(conn: sqlite3.Connection, name: str, status: str) -> None:
    conn.execute("UPDATE sites SET status = ? WHERE name = ?", (status, name))
    conn.commit()


def delete_site(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM sites WHERE name = ?", (name,))
    conn.commit()


def set_site_has_scraper(conn: sqlite3.Connection, name: str, has_scraper: bool) -> None:
    """Called by main.py at startup to keep the dashboard's badge honest --
    reflects whether SCRAPER_CLASSES actually has a module for this site."""
    conn.execute("UPDATE sites SET has_scraper = ? WHERE name = ?", (1 if has_scraper else 0, name))
    conn.commit()


# --- CVs ---------------------------------------------------------------------


def save_cv(conn: sqlite3.Connection, filename: str, stored_path: str, label: str) -> int:
    cur = conn.execute(
        "INSERT INTO cvs (filename, stored_path, label, uploaded_at) VALUES (?, ?, ?, datetime('now'))",
        (filename, stored_path, label),
    )
    conn.commit()
    return cur.lastrowid


def list_cvs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM cvs ORDER BY uploaded_at DESC").fetchall()
    conn.row_factory = None
    return rows


def get_cv(conn: sqlite3.Connection, cv_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cvs WHERE id = ?", (cv_id,)).fetchone()
    conn.row_factory = None
    return row


def delete_cv(conn: sqlite3.Connection, cv_id: int) -> None:
    conn.execute("DELETE FROM cvs WHERE id = ?", (cv_id,))
    conn.commit()


# --- Settings (notification rules) -------------------------------------------


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return dict(rows)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_settings(conn).get("scraper_paused", "false") == "true"


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    set_setting(conn, "scraper_paused", "true" if paused else "false")


# --- Notification queue (batching modes) -------------------------------------


def enqueue_notification(conn: sqlite3.Connection, job_id: int, origin_tag: str = "") -> None:
    conn.execute(
        "INSERT INTO notification_queue (job_id, queued_at, sent_at, origin_tag) VALUES (?, datetime('now'), NULL, ?)",
        (job_id, origin_tag),
    )
    conn.commit()


def pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT nq.id AS queue_id, nq.origin_tag AS origin_tag, j.*
        FROM notification_queue nq
        JOIN jobs j ON j.id = nq.job_id
        WHERE nq.sent_at IS NULL
        ORDER BY nq.queued_at ASC
        """
    ).fetchall()
    conn.row_factory = None
    return rows


def mark_notifications_sent(conn: sqlite3.Connection, queue_ids: list[int]) -> None:
    if not queue_ids:
        return
    placeholders = ",".join("?" for _ in queue_ids)
    conn.execute(
        f"UPDATE notification_queue SET sent_at = datetime('now') WHERE id IN ({placeholders})",
        queue_ids,
    )
    conn.commit()


def sent_count_last_hour(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM notification_queue WHERE sent_at IS NOT NULL AND sent_at >= datetime('now', '-1 hour')"
    ).fetchone()
    return row[0]


# --- Chat history (AI agent chat interface, src/agent/chat.py) ---------------


def save_chat_message(conn: sqlite3.Connection, platform: str, thread_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (platform, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        (platform, thread_id, role, content),
    )
    conn.commit()


def get_chat_history(conn: sqlite3.Connection, platform: str, thread_id: str, limit: int = 20) -> list[dict]:
    """Returns the most recent `limit` turns in chronological order (oldest
    first) -- the shape src/agent/chat.py needs to hand straight to Ollama as
    the message history."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT role, content FROM chat_messages
        WHERE platform = ? AND thread_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (platform, thread_id, limit),
    ).fetchall()
    conn.row_factory = None
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
