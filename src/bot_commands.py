"""Global bot commands (Telegram + WhatsApp): scan / pause / resume.

Plain-text exact-match, case-insensitive -- these are fixed-vocabulary
global actions, not tied to a specific job, so unlike Phase 3's per-job
actions (which need buttons to avoid "which job?" ambiguity) a small
command list is fine here. No NLU, no fuzzy matching.
"""

import logging
import threading

from . import db, main

logger = logging.getLogger("jobbot.bot_commands")

COMMAND_ALIASES = {
    "scan": ["חיפוש", "scan", "search"],
    "pause": ["כיבוי", "pause", "off"],
    "resume": ["הפעלה", "resume", "on"],
}

_LOOKUP = {alias.strip().lower(): action for action, aliases in COMMAND_ALIASES.items() for alias in aliases}


def match_command(text: str) -> str | None:
    return _LOOKUP.get((text or "").strip().lower())


def handle_command(action: str, conn) -> str:
    """Executes the command and returns the confirmation text to reply with."""
    if action == "scan":
        if main.is_scan_running():
            logger.info("bot_command action=scan status=already_running")
            return "🔍 כבר מתבצעת סריקה… / A scan is already in progress."
        # Runs in the background so the reply comes back immediately instead
        # of blocking on a scan that can take a couple of minutes.
        logger.info("bot_command action=scan")
        threading.Thread(target=main.trigger_manual_scan, daemon=True).start()
        return "🔍 Scanning now…"

    if action == "pause":
        db.set_paused(conn, True)
        logger.info("bot_command action=pause")
        return "⏸️ Paused"

    if action == "resume":
        db.set_paused(conn, False)
        logger.info("bot_command action=resume")
        return "▶️ Resumed"

    raise ValueError(f"Unknown command action: {action!r}")
