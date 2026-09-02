"""Handles taps on a job notification's inline action buttons (Telegram
callback_query data / WhatsApp interactive button_reply id) -- both carry
the same opaque "<action>:<job_id>" payload a Button was built with (see
notifiers/base.py), so one handler covers both channels. Only "applied" is
wired up so far; other action prefixes (e.g. a future "coverletter") fall
through to the unknown-action reply rather than erroring, so this handler
doesn't need to change again just because a new button gets added.
"""

import logging

from . import db

logger = logging.getLogger("jobbot.job_actions")


def handle_button_action(payload: str, conn) -> str:
    action, _, job_id_str = payload.partition(":")
    try:
        job_id = int(job_id_str)
    except ValueError:
        logger.warning("button_action payload=%r malformed job id", payload)
        return "⚠️ Unrecognized action."

    if action == "applied":
        if db.get_job(conn, job_id) is None:
            logger.warning("button_action action=applied job_id=%d not found", job_id)
            return "⚠️ Job not found."
        db.mark_applied(conn, job_id)
        logger.info("button_action action=applied job_id=%d", job_id)
        return "Marked as applied ✅"

    logger.warning("button_action payload=%r unknown action=%r", payload, action)
    return "⚠️ Unrecognized action."
