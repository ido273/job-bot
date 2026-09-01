"""Common interface every notification channel implements. A channel is
added/removed purely via `notifications.channels` in config — nothing in the
scraper or matcher knows or cares which channels are active.

`buttons` exists now so a later phase can attach "Cover letter" / "Tailor CV"
action buttons (job ID embedded in the payload) to a match notification
without changing this interface again. No channel implements button
*actions* yet — Phase 1 only wires the parameter through.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Job


@dataclass
class Button:
    text: str
    payload: str  # opaque action id, e.g. "coverletter:<job_id>" — unused until a later phase


class NotificationChannel(ABC):
    name: str

    @abstractmethod
    def send_job_match(self, job: Job, summary: str, buttons: list[Button] | None = None) -> bool:
        """Send a new-match notification. Returns True on success."""
        raise NotImplementedError

    @abstractmethod
    def send_degraded_alert(self, site_display_name: str, reason: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_test_message(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_digest(self, entries: list[tuple[Job, str]]) -> bool:
        """Send one combined message covering several matches at once
        (Phase 2's "digest" notification mode). `entries` is (job, summary)
        pairs in the order they were queued."""
        raise NotImplementedError
