"""Handles taps on a job notification's inline action buttons (Telegram
callback_query data / WhatsApp interactive button_reply id) -- both carry
the same opaque "<action>:<job_id>[:<extra>]" payload a Button was built
with (see notifiers/base.py), so one handler covers both channels.

Returns an ActionResult rather than a bare string: `replace_buttons` tells
the caller what to do with the *original* message's button set --
`None` = leave as-is, `[]` = clear them, a non-empty list = show new ones
(the "⏰ הזכר לי מאוחר יותר" submenu). Telegram can genuinely edit a sent
message's keyboard; WhatsApp can't, so its caller (the webhook handler in
dashboard/app.py) degrades a non-empty `replace_buttons` into a brand-new
message instead -- see notifiers/whatsapp.py's send_buttons().
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .notifiers.base import Button

logger = logging.getLogger("jobbot.job_actions")


@dataclass
class ActionResult:
    reply_text: str
    replace_buttons: list[Button] | None = None


def _remind_menu_buttons(job_id: int, config: Config) -> list[Button]:
    offsets = config.agent.get("reminder_offsets_minutes", [30, 60, 120])
    labels = {30: "30 דק׳", 60: "שעה", 120: "שעתיים"}
    return [Button(labels.get(m, f"{m} דק׳"), f"remind:{job_id}:{m}") for m in offsets]


def handle_button_action(payload: str, conn, config: Config) -> ActionResult:
    parts = (payload or "").split(":")
    action = parts[0] if parts else ""

    if action in ("applied", "not_relevant", "remind_menu"):
        try:
            job_id = int(parts[1])
        except (IndexError, ValueError):
            logger.warning("button_action payload=%r malformed job id", payload)
            return ActionResult("⚠️ Unrecognized action.")
        if db.get_job(conn, job_id) is None:
            logger.warning("button_action action=%s job_id=%d not found", action, job_id)
            return ActionResult("⚠️ Job not found.")

        if action == "applied":
            db.mark_applied(conn, job_id)
            logger.info("button_action action=applied job_id=%d", job_id)
            return ActionResult("✅ סומן כהוגש", replace_buttons=[])

        if action == "not_relevant":
            db.mark_not_relevant(conn, job_id)
            logger.info("button_action action=not_relevant job_id=%d", job_id)
            return ActionResult("❌ סומן כלא רלוונטי", replace_buttons=[])

        # remind_menu -- no DB write, just swap to the time-choice buttons.
        return ActionResult("בחר מתי להזכיר", replace_buttons=_remind_menu_buttons(job_id, config))

    if action == "remind":
        try:
            job_id = int(parts[1])
            minutes = int(parts[2])
        except (IndexError, ValueError):
            logger.warning("button_action payload=%r malformed remind args", payload)
            return ActionResult("⚠️ Unrecognized action.")
        if db.get_job(conn, job_id) is None:
            logger.warning("button_action action=remind job_id=%d not found", job_id)
            return ActionResult("⚠️ Job not found.")
        remind_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        db.schedule_reminder(conn, job_id, remind_at)
        logger.info("button_action action=remind job_id=%d minutes=%d", job_id, minutes)
        return ActionResult(f"⏰ אזכיר בעוד {minutes} דקות", replace_buttons=[])

    logger.warning("button_action payload=%r unknown action=%r", payload, action)
    return ActionResult("⚠️ Unrecognized action.")
