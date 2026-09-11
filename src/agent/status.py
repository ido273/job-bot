"""Proactive status field the dashboard polls and chat can answer from
("מה אתה עושה עכשיו?" -- see chat.py's get_agent_status tool). Plain settings
rows, not a websocket -- the dashboard already polls, and Telegram never
needs push updates for this (see chat.py / job-bot-spec's "don't spam a
message per status change").

Also home to the degraded-alert cooldown gate, shared by two independent
callers: src/main.py's per-scraper-cycle review (Task 2) and
src/agent/loop.py's own health check (Task 1's continuous loop). Both can
observe an Ollama failure independently and on their own schedule -- an
earlier version gated alerts on a shared "was it degraded a moment ago"
boolean, which let the two processes race each other: one clearing the flag
on its own success right as the other's own failure flipped it back to
true, generating a fresh alert each time even though the underlying outage
never actually ended. Gating on a plain cooldown timestamp instead makes
"at most one alert per window, no matter which of the two callers rings the
bell" true by construction, independent of any flag either side maintains.
"""

from datetime import datetime, timezone

from .. import db

DEGRADED_ALERT_COOLDOWN_MINUTES = 120


def set_status(conn, text: str) -> None:
    db.set_setting(conn, "agent_status", text)
    db.set_setting(conn, "agent_status_updated_at", datetime.now(timezone.utc).isoformat())


def mark_cycle_complete(conn) -> None:
    db.set_setting(conn, "agent_last_cycle_at", datetime.now(timezone.utc).isoformat())


def get_status(conn) -> dict:
    settings = db.get_settings(conn)
    return {
        "status": settings.get("agent_status", ""),
        "status_updated_at": settings.get("agent_status_updated_at", ""),
        "last_cycle_at": settings.get("agent_last_cycle_at", ""),
        "ollama_degraded": settings.get("agent_ollama_degraded", "false") == "true",
    }


def set_degraded(conn, degraded: bool) -> None:
    """Purely a display flag (dashboard/chat "⚠️" indicator) -- does NOT
    gate alert-sending on its own; see maybe_alert_degraded for that."""
    db.set_setting(conn, "agent_ollama_degraded", "true" if degraded else "false")


def maybe_alert_degraded(conn, channels, reason: str) -> None:
    """Sends a degraded-state alert at most once per DEGRADED_ALERT_COOLDOWN_MINUTES,
    regardless of which caller (or how many) invoke this concurrently or how
    many times the underlying failure repeats within the window. Call this
    on every observed failure (not just the first) -- the cooldown, not the
    caller, decides whether it actually sends."""
    settings = db.get_settings(conn)
    last_sent = settings.get("agent_degraded_alert_sent_at") or ""
    now = datetime.now(timezone.utc)
    if last_sent:
        elapsed_minutes = (now - datetime.fromisoformat(last_sent)).total_seconds() / 60
        if elapsed_minutes < DEGRADED_ALERT_COOLDOWN_MINUTES:
            return
    db.set_setting(conn, "agent_degraded_alert_sent_at", now.isoformat())
    for channel in channels:
        channel.send_degraded_alert("AI Agent", reason)
