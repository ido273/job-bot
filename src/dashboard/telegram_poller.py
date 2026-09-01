"""Background long-polling listener for Telegram bot commands.

Runs as a daemon thread inside the dashboard process (the one persistent
process in this deployment -- the scraper is a periodic CronJob/loop, not a
good fit for hosting a listener). Long-polling means no public URL is
needed, unlike WhatsApp's webhook-only inbound (see app.py).
"""

import logging
import os
import threading
import time

import requests

from .. import bot_commands, db
from ..config import Config
from ..notifiers.telegram import TelegramChannel

logger = logging.getLogger("jobbot.dashboard.telegram_poller")

GET_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
POLL_TIMEOUT_SECONDS = 25


def _build_channel() -> TelegramChannel | None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return None
    return TelegramChannel(bot_token=bot_token, chat_id=chat_id)


def _poll_loop(config: Config, stop_event: threading.Event) -> None:
    channel = _build_channel()
    if channel is None:
        logger.info("TELEGRAM_BOT_TOKEN/CHAT_ID not set -- Telegram command listener not started.")
        return

    conn = db.connect(config.db_path, config.sites)
    logger.info("Telegram command listener started (long-polling).")

    while not stop_event.is_set():
        try:
            offset = int(db.get_settings(conn).get("telegram_last_update_id", "0") or 0)
            resp = requests.get(
                GET_UPDATES_URL.format(token=channel.bot_token),
                params={"offset": offset + 1, "timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": '["message"]'},
                timeout=POLL_TIMEOUT_SECONDS + 10,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException:
            logger.exception("Telegram getUpdates failed; retrying shortly")
            stop_event.wait(5)
            continue

        for update in updates:
            update_id = update.get("update_id")
            message = update.get("message") or {}
            text = message.get("text", "")
            sender_chat_id = str(message.get("chat", {}).get("id", ""))

            # Only the configured chat may issue commands -- these trigger
            # real actions (pausing the scraper), so this isn't optional.
            if sender_chat_id != channel.chat_id:
                logger.warning("Ignoring Telegram command from unrecognized chat_id=%s", sender_chat_id)
            else:
                action = bot_commands.match_command(text)
                if action:
                    reply = bot_commands.handle_command(action, conn)
                    channel.send_text_to(sender_chat_id, reply)

            if update_id is not None:
                db.set_setting(conn, "telegram_last_update_id", str(update_id))

    conn.close()
    logger.info("Telegram command listener stopped.")


def start(config: Config) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(target=_poll_loop, args=(config, stop_event), daemon=True)
    thread.start()
    return thread, stop_event
