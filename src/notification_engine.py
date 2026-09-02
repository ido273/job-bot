"""Notification-rules engine backing the dashboard's Settings page.

One mode is active at a time (`settings.notification_mode`), switchable live
from the dashboard -- not four simultaneous send paths racing on one match
(see job-bot-spec.md's Phase 2 amendment for why). Every mode's parameters
stay persisted in `settings` regardless of which mode is active, so
switching back doesn't lose prior configuration.

- immediate: send_job_match() right away, exactly like pre-Phase-2 behavior.
- rate_limited: queue, release up to N per rolling hour.
- digest: queue, flush everything queued as one combined message every X minutes.
- count_batch: queue, flush everything queued once M have accumulated.
"""

import logging
from datetime import datetime, timezone

from . import db
from .config import Config
from .models import Job
from .notifiers import NotificationChannel
from .notifiers.base import Button
from .summarizer import summarize_requirements

logger = logging.getLogger("jobbot.notification_engine")


def _applied_button(job_id: int) -> list[Button]:
    return [Button("✅ Applied", f"applied:{job_id}")]


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
    )


def _summary_for(job: Job, config: Config) -> str:
    return summarize_requirements(
        job.description,
        config.summary_config.get("known_tools", []),
        config.summary_config.get("max_chars", 300),
    )


def _notify_all(channels: list[NotificationChannel], method_name: str, *args) -> None:
    for channel in channels:
        ok = getattr(channel, method_name)(*args)
        logger.info("channel=%s method=%s status=%s", channel.name, method_name, "ok" if ok else "failed")


def enqueue_or_send(conn, channels: list[NotificationChannel], job_id: int, job: Job, summary: str) -> None:
    """Call once per newly-matched job, right after db.save_job()."""
    mode = db.get_settings(conn).get("notification_mode", "immediate")
    if mode == "immediate":
        _notify_all(channels, "send_job_match", job, summary, _applied_button(job_id))
    else:
        db.enqueue_notification(conn, job_id)


def maybe_flush(conn, channels: list[NotificationChannel], config: Config) -> None:
    """Call once per scrape cycle, after every site has been processed."""
    settings = db.get_settings(conn)
    mode = settings.get("notification_mode", "immediate")
    if mode == "immediate":
        return

    pending = db.pending_notifications(conn)
    if not pending:
        return

    entries = [(_job_from_row(row), row["queue_id"], row["id"]) for row in pending]

    if mode == "rate_limited":
        limit = int(settings.get("rate_limit_per_hour") or 10)
        budget = max(0, limit - db.sent_count_last_hour(conn))
        to_release = entries[:budget]
        for job, _, job_id in to_release:
            _notify_all(channels, "send_job_match", job, _summary_for(job, config), _applied_button(job_id))
        db.mark_notifications_sent(conn, [qid for _, qid, _ in to_release])

    elif mode == "count_batch":
        batch_count = int(settings.get("batch_count") or 5)
        if len(entries) >= batch_count:
            for job, _, job_id in entries:
                _notify_all(channels, "send_job_match", job, _summary_for(job, config), _applied_button(job_id))
            db.mark_notifications_sent(conn, [qid for _, qid, _ in entries])

    elif mode == "digest":
        digest_minutes = float(settings.get("digest_minutes") or 60)
        last_sent = settings.get("last_digest_sent_at") or ""
        due = True
        if last_sent:
            due = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)).total_seconds() >= digest_minutes * 60
        if due:
            # Digest bundles N matches into one message -- neither Telegram's
            # nor WhatsApp's API can attach a distinct per-job button set to
            # one combined message, so digest mode has no "Applied" button;
            # only immediate/rate_limited/count_batch (one job per message) do.
            digest_entries = [(job, _summary_for(job, config)) for job, _, _ in entries]
            for channel in channels:
                ok = channel.send_digest(digest_entries)
                logger.info("channel=%s method=send_digest status=%s count=%d", channel.name, "ok" if ok else "failed", len(digest_entries))
            db.mark_notifications_sent(conn, [qid for _, qid, _ in entries])
            db.set_setting(conn, "last_digest_sent_at", datetime.now(timezone.utc).isoformat())

    else:
        logger.warning("Unknown notification_mode=%r in settings, not flushing", mode)
