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
