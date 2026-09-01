"""WhatsApp via Meta's official Cloud API (Graph API), not Twilio and not an
unofficial client. Docs: https://developers.facebook.com/docs/whatsapp/cloud-api

Free-tier note: in Cloud API test mode, the recipient number must be added
as a tester in the Meta for Developers dashboard, and business-initiated
free-form text only delivers within the 24h window after the recipient last
messaged the business number (same "user must message first" shape as a
Telegram bot). Outside that window, Meta requires an approved message
template — out of scope for Phase 1.
"""

import logging

import requests

from ..models import Job
from .base import Button, NotificationChannel

logger = logging.getLogger("jobbot.notifiers.whatsapp")

GRAPH_API_URL = "https://graph.facebook.com/{version}/{phone_number_id}/messages"

WORK_MODE_LABELS = {
    "onsite": "🏢 On-site",
    "hybrid": "🏠🏢 Hybrid",
    "remote": "🏠 Remote",
}

MAX_INTERACTIVE_BUTTONS = 3
MAX_BUTTON_TITLE_CHARS = 20


class WhatsAppChannel(NotificationChannel):
    name = "whatsapp"

    def __init__(self, access_token: str, phone_number_id: str, recipient_number: str, api_version: str = "v21.0"):
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.recipient_number = recipient_number
        self.api_version = api_version

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def _url(self) -> str:
        return GRAPH_API_URL.format(version=self.api_version, phone_number_id=self.phone_number_id)

    def _post(self, payload: dict) -> bool:
        resp = None
        try:
            resp = requests.post(self._url(), headers=self._headers(), json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            logger.exception("WhatsApp send failed (response: %s)", resp.text if resp is not None else "<no response>")
            return False

    def _send_text(self, text: str, to: str | None = None) -> bool:
        payload = {
            "messaging_product": "whatsapp",
            "to": to or self.recipient_number,
            "type": "text",
            "text": {"body": text, "preview_url": True},
        }
        return self._post(payload)

    def _send_interactive_buttons(self, text: str, buttons: list[Button]) -> bool:
        payload = {
            "messaging_product": "whatsapp",
            "to": self.recipient_number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {"id": b.payload, "title": b.text[:MAX_BUTTON_TITLE_CHARS]},
                        }
                        for b in buttons[:MAX_INTERACTIVE_BUTTONS]
                    ]
                },
            },
        }
        return self._post(payload)

    def _format_job_message(self, job: Job, summary: str) -> str:
        work_mode_label = WORK_MODE_LABELS.get(job.work_mode, job.work_mode or "Unknown")
        text = (
            f"💼 *{job.title}*\n"
            f"🏭 {job.company or 'Unknown company'}\n"
            f"📍 {job.location or 'Unknown location'} · {work_mode_label}\n"
        )
        if summary:
            text += f"\n📋 {summary}\n"
        text += f"\n🌐 Source: {job.source_site}\n🔗 {job.url}"
        return text

    def send_job_match(self, job: Job, summary: str, buttons: list[Button] | None = None) -> bool:
        text = self._format_job_message(job, summary)
        if buttons:
            return self._send_interactive_buttons(text, buttons)
        return self._send_text(text)

    def send_degraded_alert(self, site_display_name: str, reason: str) -> bool:
        text = f"⚠️ {site_display_name} scraper degraded / blocked, skipping for now.\nReason: {reason}"
        return self._send_text(text)

    def send_test_message(self) -> bool:
        return self._send_text("✅ Job bot WhatsApp connection OK.")

    def send_text_to(self, to: str, text: str) -> bool:
        """Reply to a specific number -- used for bot-command confirmations."""
        return self._send_text(text, to=to)

    def send_digest(self, entries: list[tuple[Job, str]]) -> bool:
        if not entries:
            return True
        lines = [f"📬 *{len(entries)} new job match{'es' if len(entries) != 1 else ''}*\n"]
        for job, summary in entries:
            work_mode_label = WORK_MODE_LABELS.get(job.work_mode, job.work_mode or "Unknown")
            lines.append(f"💼 *{job.title}* - {job.company or 'Unknown company'} · {work_mode_label}\n🔗 {job.url}")
        text = "\n\n".join(lines)
        if len(text) > 4000:  # WhatsApp text bodies cap at 4096 characters
            text = text[:3990].rsplit("\n", 1)[0] + "\n\n…(truncated)"
        return self._send_text(text)
