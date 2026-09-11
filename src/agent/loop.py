"""Entrypoint for the job-bot-agent Deployment (k8s/agent.yaml) -- a
continuous loop, not a CronJob: a local model has no per-run cost, so there's
no reason to run discovery on a fixed schedule. Bounded per-cycle by
max_tool_calls_per_cycle (see discovery.py -> tool_loop.py), then a plain
sleep -- that tool-call budget, not a k8s activeDeadlineSeconds, is this
workload's liveness safeguard.

Graceful degradation: Ollama/SearxNG being unreachable (pod not ready yet,
connection refused, timeout, or -- confirmed live -- /api/chat erroring
under GPU/VRAM pressure while /api/tags stays up) must never crash-loop this
container. A health check runs before every cycle and actually exercises
/api/chat (not just the lightweight /api/tags), so a still-broken chat
endpoint isn't mistaken for "recovered". Alerting is a cooldown, not a
one-shot-on-transition flag (see status.maybe_alert_degraded) -- this loop
and src/main.py's own review-time check both call into that same cooldown,
so however many times either side observes a failure, at most one alert
goes out per window.
"""

import logging
import time

import requests

from .. import db
from ..config import load_config
from ..logging_setup import setup_logging
from ..notifiers import build_channels
from . import discovery, ollama_client, status

logger = logging.getLogger("jobbot.agent.loop")

HEALTH_CHECK_TIMEOUT_SECONDS = 30
DEGRADED_RETRY_SECONDS = 30


def _searxng_reachable(base_url: str) -> bool:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/search",
            params={"q": "test", "format": "json"},
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        resp.json()
        return True
    except (requests.RequestException, ValueError):
        return False


def _wait_for_infra(config, conn) -> None:
    """Blocks until both Ollama (a real /api/chat round-trip, not just
    /api/tags) and SearxNG respond. Sends a cooldown-gated alert on every
    failed check -- see status.maybe_alert_degraded -- and clears the
    display flag (silently, no "recovered" spam) once both are healthy."""
    while True:
        agent_config = config.agent
        ollama_ok = ollama_client.chat_healthcheck(
            agent_config["ollama_base_url"], agent_config["ollama_model"], timeout=HEALTH_CHECK_TIMEOUT_SECONDS
        )
        searxng_ok = _searxng_reachable(agent_config["searxng_base_url"])

        if ollama_ok and searxng_ok:
            if db.get_settings(conn).get("agent_ollama_degraded") == "true":
                status.set_degraded(conn, False)
                logger.info("Ollama/SearxNG connectivity recovered, resuming normal cycles.")
            return

        unreachable = [name for name, ok in (("Ollama", ollama_ok), ("SearxNG", searxng_ok)) if not ok]
        reason = " + ".join(unreachable)
        status.set_status(conn, f"לא ניתן להתחבר ל-{reason}, מנסה שוב...")
        logger.warning("infra unreachable=%s, retrying in %ds", reason, DEGRADED_RETRY_SECONDS)

        status.set_degraded(conn, True)
        try:
            status.maybe_alert_degraded(conn, build_channels(config), f"{reason} unreachable -- discovery paused, retrying automatically")
        except Exception:
            logger.exception("Failed to send AI agent degraded alert")

        time.sleep(DEGRADED_RETRY_SECONDS)


def main() -> None:
    config = load_config()
    setup_logging(config.logging_level)
    conn = db.connect(config.db_path, config.sites)
    logger.info(
        "AI agent discovery loop starting: cycle_sleep_minutes=%.1f max_tool_calls_per_cycle=%d model=%s",
        config.agent["cycle_sleep_minutes"],
        config.agent["max_tool_calls_per_cycle"],
        config.agent["ollama_model"],
    )

    while True:
        # Reloaded every cycle (not just at startup), same reasoning as
        # src/main.py's loop -- a dashboard edit to min_relevance_score,
        # reminder offsets, etc. takes effect on the next tick without a
        # restart.
        config = load_config()
        _wait_for_infra(config, conn)

        status.set_status(conn, "מחפש משרות נוספות")
        try:
            stats = discovery.run_discovery_cycle(config, conn)
            logger.info("cycle complete: %s", stats)
        except requests.RequestException as exc:
            # Infra dropped mid-cycle (the health check above passed but a
            # later call in the same cycle still failed -- confirmed live
            # under GPU/VRAM pressure, not just rare). Also cooldown-gated,
            # not silent -- a chat-level failure that keeps recurring here
            # while the lightweight health check above keeps passing must
            # still eventually reach you.
            logger.warning("discovery cycle aborted, infra unreachable mid-run: %s", exc)
            status.set_degraded(conn, True)
            try:
                status.maybe_alert_degraded(conn, build_channels(config), f"Ollama failing mid-cycle: {exc}")
            except Exception:
                logger.exception("Failed to send AI agent degraded alert")
        except Exception:
            logger.exception("discovery cycle failed unexpectedly")

        status.mark_cycle_complete(conn)
        sleep_minutes = config.agent["cycle_sleep_minutes"]
        status.set_status(conn, f"במנוחה, ריצה הבאה בעוד כ-{sleep_minutes:.0f} דקות")
        time.sleep(sleep_minutes * 60)


if __name__ == "__main__":
    main()
