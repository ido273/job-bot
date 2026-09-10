"""Entry point.

    python -m src.main --test-notifications              # test every active channel
    python -m src.main --test-notifications --channel whatsapp  # test just one
    python -m src.main --once            # run a single scrape cycle then exit
                                          # (this is what the k8s CronJob runs)
    python -m src.main                   # run forever, jittered ~10 min loop
                                          # (used for local/docker-compose testing)
"""

import argparse
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from . import db, notification_engine
from .agent import cv_reader
from .agent.scoring import score_job
from .config import Config, load_config
from .logging_setup import setup_logging
from .matcher import is_match
from .notifiers import NotificationChannel, build_channel, build_channels
from .scrapers.alljobs import AllJobsScraper
from .scrapers.base import BlockedError, SiteScraper
from .scrapers.dialog import DialogScraper
from .scrapers.drushim import DrushimScraper
from .scrapers.gotfriends import GotfriendsScraper
from .scrapers.jobmaster import JobMasterScraper
from .scrapers.nisha import NishaScraper
from .summarizer import summarize_requirements

logger = logging.getLogger("jobbot.main")

# Site registry: add a new scraper module here once it exists.
SCRAPER_CLASSES: dict[str, type[SiteScraper]] = {
    "alljobs": AllJobsScraper,
    "drushim": DrushimScraper,
    "dialog": DialogScraper,
    "gotfriends": GotfriendsScraper,
    "nisha": NishaScraper,
    "jobmaster": JobMasterScraper,
}

# Guards "Scan now" (dashboard button + bot "scan" command) against
# overlapping with each other or with a scheduled run already in flight.
_scan_lock = threading.Lock()


def is_scan_running() -> bool:
    return _scan_lock.locked()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_scrapers(config: Config, conn) -> dict[str, SiteScraper]:
    """Site on/off state comes from the DB `sites` registry (dashboard-editable
    in Phase 2); config.yaml's per-site block stays scraper-internal tuning
    (search_urls, robots_txt_url) for whichever sites have a real module."""
    for row in db.list_sites(conn):
        db.set_site_has_scraper(conn, row["name"], row["name"] in SCRAPER_CLASSES)

    scrapers = {}
    for row in db.get_active_sites(conn):
        name = row["name"]
        scraper_cls = SCRAPER_CLASSES.get(name)
        if scraper_cls is None:
            logger.warning("site=%s active but no scraper module registered yet", name)
            continue
        site_config = config.sites.get(name, {})
        scrapers[name] = scraper_cls(site_config, config.anti_blocking)
    return scrapers


def _notify_all(channels: list[NotificationChannel], method_name: str, *args) -> None:
    for channel in channels:
        ok = getattr(channel, method_name)(*args)
        logger.info("channel=%s method=%s status=%s", channel.name, method_name, "ok" if ok else "failed")


SCRAPER_TAG = "🔍 מהסקרייפר"
SCRAPER_REVIEWED_TAG = "🤖 מסוכן ה-AI (בדק תוצאה מהסקרייפר)"


def _alert_agent_degraded_once(conn, channels: list[NotificationChannel]) -> None:
    """Shares the `agent_ollama_degraded` settings flag with src/agent/loop.py's
    own health check -- either process flipping it back to "false" on a
    successful call means the other doesn't re-alert either, one alert for
    the whole outage rather than one per unreachable matched job."""
    if db.get_settings(conn).get("agent_ollama_degraded") == "true":
        return
    db.set_setting(conn, "agent_ollama_degraded", "true")
    _notify_all(
        channels,
        "send_degraded_alert",
        "AI Agent",
        "Ollama unreachable -- scraper matches will notify unreviewed until it recovers",
    )


def _review_scraper_match(conn, channels: list[NotificationChannel], job, job_id: int, config: Config, cv_text: str) -> str | None:
    """Returns the origin_tag to notify with, or None if the match should be
    stored but NOT notified (the agent reviewed it and scored it below
    min_relevance_score). Never blocks the scraper on Ollama being down --
    that degrades to the old pre-agent behavior (notify unreviewed, tagged
    plainly) instead of dropping or delaying the notification."""
    result = score_job(job, config.matching, cv_text, config)
    if result is None:
        logger.warning("job_id=%d AI agent unreachable, notifying unreviewed", job_id)
        _alert_agent_degraded_once(conn, channels)
        return SCRAPER_TAG

    if db.get_settings(conn).get("agent_ollama_degraded") == "true":
        db.set_setting(conn, "agent_ollama_degraded", "false")
        logger.info("AI agent scoring recovered")

    db.set_job_relevance(conn, job_id, result.relevance_score, result.rationale_he)
    if result.relevance_score >= config.agent["min_relevance_score"]:
        return SCRAPER_REVIEWED_TAG

    logger.info("job_id=%d scored %d/10 by AI agent, below threshold -- stored, not notified", job_id, result.relevance_score)
    return None


def run_cycle(config: Config, conn, scrapers: dict[str, SiteScraper], channels: list[NotificationChannel]) -> None:
    session = requests.Session()
    now = _utcnow()
    cv_text = cv_reader.read_cv(conn)

    for name, scraper in scrapers.items():
        blocked_until = db.get_blocked_until(conn, name)
        if blocked_until and datetime.fromisoformat(blocked_until) > now:
            logger.info("site=%s status=skipped reason=backed_off until=%s", name, blocked_until)
            continue

        was_blocked = blocked_until is not None
        stagger = random.uniform(
            config.polling.get("site_stagger_seconds_min", 5),
            config.polling.get("site_stagger_seconds_max", 45),
        )
        time.sleep(stagger)

        display_name = getattr(scraper, "display_name", name)
        try:
            jobs = scraper.fetch_new_jobs(session)
        except BlockedError as exc:
            logger.warning("site=%s status=blocked reason=%s", name, exc)
            backoff_cycles = config.anti_blocking.get("backoff_cycles_on_block", 6)
            interval_minutes = config.polling.get("interval_minutes", 10)
            blocked_until_dt = now + timedelta(minutes=backoff_cycles * interval_minutes)
            db.set_blocked_until(conn, name, blocked_until_dt.isoformat(), str(exc))
            if not was_blocked:
                _notify_all(channels, "send_degraded_alert", display_name, str(exc))
            continue
        except requests.RequestException as exc:
            logger.error("site=%s status=error reason=%s", name, exc)
            db.mark_site_error(conn, name, str(exc))
            continue

        db.mark_checked(conn, name)

        new_count = 0
        matched_count = 0
        for job in jobs:
            if db.is_seen(conn, job.url):
                continue
            new_count += 1
            db.mark_seen(conn, job.url, name)
            if is_match(job, config.matching):
                matched_count += 1
                job_id = db.save_job(conn, job)
                origin_tag = _review_scraper_match(conn, channels, job, job_id, config, cv_text)
                if origin_tag is None:
                    continue  # agent scored it below threshold -- stored, not notified
                summary = summarize_requirements(
                    job.description,
                    config.summary_config.get("known_tools", []),
                    config.summary_config.get("max_chars", 300),
                )
                notification_engine.enqueue_or_send(conn, channels, job_id, job, summary, origin_tag=origin_tag)

        logger.info(
            "site=%s status=ok fetched=%d new=%d matched=%d",
            name,
            len(jobs),
            new_count,
            matched_count,
        )

    notification_engine.maybe_flush(conn, channels, config)


def perform_scan(config: Config, conn) -> dict:
    """Builds scrapers/channels fresh from current config/DB state and runs
    one scan cycle. Shared by scheduled runs (CLI --once / continuous loop)
    and manual triggers (dashboard "Scan now" button, bot "scan" command) --
    callers decide whether the pause flag applies (see trigger_manual_scan)."""
    scrapers = build_scrapers(config, conn)
    if not scrapers:
        logger.error("No active sites have a registered scraper. Nothing to do.")
        return {"ok": False, "reason": "no_active_scrapers"}
    channels = build_channels(config)
    run_cycle(config, conn, scrapers, channels)
    return {"ok": True}


def trigger_manual_scan(config_path: str | None = None, env_path: str | None = None) -> dict:
    """An explicit "scan right now" request (dashboard button, bot command)
    -- always runs, even if the scraper is paused. Pausing stops the
    *automatic* schedule, not an explicit manual request. Opens its own
    config/DB connection since callers (a background thread, a webhook
    handler) don't have one of their own to reuse."""
    if not _scan_lock.acquire(blocking=False):
        logger.info("Manual scan requested but one is already running; skipping.")
        return {"ok": False, "reason": "already_running"}
    try:
        config = load_config(config_path, env_path)
        conn = db.connect(config.db_path, config.sites)
        try:
            return perform_scan(config, conn)
        finally:
            conn.close()
    finally:
        _scan_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Job search monitoring bot")
    parser.add_argument("--config", default=None, help="Path to config YAML (default: config/config.yaml)")
    parser.add_argument("--env", default=None, help="Path to .env file (default: .env)")
    parser.add_argument("--once", action="store_true", help="Run a single scrape cycle then exit")
    parser.add_argument(
        "--test-notifications", action="store_true", help="Send a test message on the active channel(s) and exit"
    )
    parser.add_argument(
        "--channel",
        default=None,
        choices=["telegram", "whatsapp"],
        help="With --test-notifications, test only this channel instead of every channel in config",
    )
    parser.add_argument(
        "--skip-startup-jitter",
        action="store_true",
        help="Skip the random startup delay (useful for --once during manual testing)",
    )
    args = parser.parse_args()

    config = load_config(args.config, args.env)
    setup_logging(config.logging_level)

    if args.test_notifications:
        test_channels = [build_channel(args.channel, config)] if args.channel else build_channels(config)
        _notify_all(test_channels, "send_test_message")
        return

    conn = db.connect(config.db_path, config.sites)

    if args.once:
        # Pause only stops *scheduled* runs (this one) -- "Scan now" always
        # goes through trigger_manual_scan() instead, which ignores it.
        if db.is_paused(conn):
            logger.info("status=paused, skipping scheduled cycle")
            return
        jitter_minutes = config.polling.get("jitter_minutes", 2.5)
        if not args.skip_startup_jitter:
            # Jitters the exact fire time within a fixed k8s CronJob schedule
            # tick, so all sites aren't hit at predictable timestamps.
            jitter_seconds = random.uniform(0, jitter_minutes * 60)
            logger.info("startup_jitter_seconds=%.1f", jitter_seconds)
            time.sleep(jitter_seconds)
        perform_scan(config, conn)
        return

    interval_minutes = config.polling.get("interval_minutes", 10)
    jitter_minutes = config.polling.get("jitter_minutes", 2.5)
    logger.info("Starting continuous loop: interval=%dm jitter=+/-%.1fm", interval_minutes, jitter_minutes)
    while True:
        # Reloaded every cycle (not just at startup) so a dashboard edit to
        # config.yaml -- role keywords, notification channels, etc -- takes
        # effect on the next tick without a restart.
        config = load_config(args.config, args.env)
        if db.is_paused(conn):
            logger.info("status=paused, skipping scheduled cycle")
        else:
            perform_scan(config, conn)
        jitter_seconds = random.uniform(-jitter_minutes * 60, jitter_minutes * 60)
        sleep_seconds = max(30.0, interval_minutes * 60 + jitter_seconds)
        logger.info("cycle complete, sleeping seconds=%.0f", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
