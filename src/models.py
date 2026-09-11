from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    url: str
    title: str
    company: str
    location: str
    work_mode: str  # "onsite" | "hybrid" | "remote"
    source_site: str
    description: str = ""
    found_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str | None = None  # unused in Phase 1, reserved for Phase 2 tracking
    # Set by src/main.py (Task 2 review) or src/agent/discovery.py (Task 1) --
    # None for a scraper match that hasn't been AI-reviewed (or couldn't be).
    # Carried on Job (not just the DB row) so the notification message
    # itself can actually show it -- confirmed live that it was being
    # computed and stored but never reaching the outgoing message text.
    relevance_score: int | None = None
    relevance_rationale: str | None = None
