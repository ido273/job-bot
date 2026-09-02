import html
import logging

import requests

from ..models import Job
from .base import Button, NotificationChannel

logger = logging.getLogger("jobbot.notifiers.telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
ANSWER_CALLBACK_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"

WORK_MODE_LABELS = {
    "onsite": "🏢 On-site",
    "hybrid": "🏠🏢 Hybrid",
    "remote": "🏠 Remote",
}


class TelegramChannel(NotificationChannel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, parse_mode: str = "HTML"):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode

    def _send(self, text: str, buttons: list[Button] | None = None, chat_id: str | None = None) -> bool:
        url = API_URL.format(token=self.bot_token)
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": False,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": b.text, "callback_data": b.payload}] for b in buttons]
            }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            logger.exception("Telegram send failed")
            return False

    def _format_job_message(self, job: Job, summary: str) -> str:
        work_mode_label = WORK_MODE_LABELS.get(job.work_mode, job.work_mode or "Unknown")
        title = html.escape(job.title)
        company = html.escape(job.company or "Unknown company")
        location = html.escape(job.location or "Unknown location")
        text = (
            f"💼 <b>{title}</b>\n"
            f"🏭 {company}\n"
            f"📍 {location} · {work_mode_label}\n"
        )
        if summary:
            text += f"\n📋 {html.escape(summary)}\n"
        text += f"\n🌐 Source: {job.source_site}\n🔗 {job.url}"
        return text

    def send_job_match(self, job: Job, summary: str, buttons: list[Button] | None = None) -> bool:
        return self._send(self._format_job_message(job, summary), buttons=buttons)

    def send_degraded_alert(self, site_display_name: str, reason: str) -> bool:
        text = f"⚠️ {site_display_name} scraper degraded / blocked, skipping for now.\nReason: {reason}"
        return self._send(text)

    def send_test_message(self) -> bool:
        return self._send("✅ Job bot Telegram connection OK.")

    def send_text_to(self, chat_id: str, text: str) -> bool:
        """Reply to a specific chat -- used for bot-command confirmations,
        where the reply target is whoever sent the command, not necessarily
        the configured notification chat_id (though in this single-user bot
        they're normally the same)."""
        return self._send(text, chat_id=chat_id)

    def answer_callback_query(self, callback_query_id: str) -> bool:
        """Dismisses the button's loading spinner after a tap is handled.
        Telegram-only -- WhatsApp's interactive-button API has no equivalent."""
        url = ANSWER_CALLBACK_URL.format(token=self.bot_token)
        try:
            resp = requests.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            logger.exception("Telegram answerCallbackQuery failed")
            return False

    def send_digest(self, entries: list[tuple[Job, str]]) -> bool:
        if not entries:
            return True
        lines = [f"📬 <b>{len(entries)} new job match{'es' if len(entries) != 1 else ''}</b>\n"]
        for job, summary in entries:
            work_mode_label = WORK_MODE_LABELS.get(job.work_mode, job.work_mode or "Unknown")
            title = html.escape(job.title)
            company = html.escape(job.company or "Unknown company")
            entry = f"💼 <b>{title}</b> — {company} · {work_mode_label}\n🔗 {job.url}"
            lines.append(entry)
        text = "\n\n".join(lines)
        if len(text) > 4000:  # Telegram's hard message-length cap is 4096
            text = text[:3990].rsplit("\n", 1)[0] + "\n\n…(truncated)"
        return self._send(text)
