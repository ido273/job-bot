"""Common interface every notification channel implements. A channel is
added/removed purely via `notifications.channels` in config — nothing in the
scraper or matcher knows or cares which channels are active.

`buttons` carries the per-job action set (✅ הגשתי / ❌ לא רלוונטי / ⏰ הזכר לי
מאוחר יותר — see notification_engine.default_buttons); `origin_tag` is the
short "🔍 מהסקרייפר" / "🤖 מסוכן ה-AI" / "🤖 מסוכן ה-AI (בדק תוצאה מהסקרייפר)"
prefix identifying where a match came from (see src/main.py and
src/agent/discovery.py).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Job


@dataclass
class Button:
    text: str
    payload: str  # opaque action id, e.g. "applied:<job_id>" or "remind:<job_id>:<minutes>"


class NotificationChannel(ABC):
    name: str

    @abstractmethod
    def send_job_match(self, job: Job, summary: str, buttons: list[Button] | None = None, origin_tag: str = "") -> bool:
        """Send a new-match notification. Returns True on success."""
        raise NotImplementedError

    @abstractmethod
    def send_degraded_alert(self, site_display_name: str, reason: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_test_message(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_digest(self, entries: list[tuple[Job, str, str]]) -> bool:
        """Send one combined message covering several matches at once
        (Phase 2's "digest" notification mode). `entries` is
        (job, summary, origin_tag) triples in the order they were queued."""
        raise NotImplementedError
