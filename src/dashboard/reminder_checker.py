"""Background timer for the "⏰ הזכר לי מאוחר יותר" notification button --
re-sends a due reminder's job notification at the chosen offset. Runs as a
daemon thread inside the dashboard process (same pattern as
telegram_poller.py), deliberately NOT inside the AI agent's loop
(src/agent/loop.py): this is a plain timer/resend mechanism, not an AI
decision, and must keep firing even if Ollama or the agent pod is down.
"""

import logging
import threading

from .. import db, notification_engine
from ..config import Config
from ..models import Job
from ..notifiers import build_channels
from ..summarizer import summarize_requirements

logger = logging.getLogger("jobbot.dashboard.reminder_checker")

CHECK_INTERVAL_SECONDS = 60


def _job_from_row(row) -> Job:
    return Job(
        url=row["url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        work_mode=row["work_mode"],
        source_site=row["source_site"],
        description=row["description"] or "",
        found_at=row["found_at"],
        status=row["status"],
        relevance_score=int(row["relevance_score"]) if row["relevance_score"] is not None else None,
        relevance_rationale=row["relevance_rationale"],
    )


def _check_loop(config: Config, stop_event: threading.Event) -> None:
    conn = db.connect(config.db_path, config.sites)
    logger.info("Reminder checker started (interval=%ds).", CHECK_INTERVAL_SECONDS)

    while not stop_event.is_set():
        try:
            due = db.due_reminders(conn)
            if due:
                channels = build_channels(config)
                for row in due:
                    job = _job_from_row(row)
                    buttons = notification_engine.default_buttons(row["id"])
                    summary = summarize_requirements(
                        job.description, config.summary_config.get("known_tools", []), config.summary_config.get("max_chars", 300)
                    )
                    for channel in channels:
                        ok = channel.send_job_match(job, summary, buttons, origin_tag="⏰ תזכורת")
                        logger.info("reminder job_id=%d channel=%s status=%s", row["id"], channel.name, "ok" if ok else "failed")
                    db.clear_reminder(conn, row["id"])
        except Exception:
            logger.exception("Reminder check failed")
        stop_event.wait(CHECK_INTERVAL_SECONDS)

    conn.close()
    logger.info("Reminder checker stopped.")


def start(config: Config) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(target=_check_loop, args=(config, stop_event), daemon=True)
    thread.start()
    return thread, stop_event
