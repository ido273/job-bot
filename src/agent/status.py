"""Proactive status field the dashboard polls and chat can answer from
("מה אתה עושה עכשיו?" -- see chat.py's get_agent_status tool). Plain settings
rows, not a websocket -- the dashboard already polls, and Telegram never
needs push updates for this (see chat.py / job-bot-spec's "don't spam a
message per status change").
"""

from datetime import datetime, timezone

from .. import db


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
