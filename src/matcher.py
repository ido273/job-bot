"""Pluggable matching logic. Swap this module for an LLM-based scorer later
without touching scrapers, db, or the notifier — the only contract is
`is_match(job, matching_config) -> bool`.
"""

import re
from typing import Any

from .models import Job

_YEARS_PATTERN_TEMPLATES = [
    r"{n}\+?\s*(?:שנות ניסיון|שנים ניסיון|שנות נסיון)",
    r"ניסיון של\s*{n}\+?\s*שנים",
    r"{n}\+\s*years?",
    r"{n}\+\s*yrs",
    r"minimum\s*{n}\s*years?",
]


def _contains_any(text: str, keywords: list[str]) -> bool:
    lower_text = text.lower()
    return any(kw.lower() in lower_text for kw in keywords)


def _exceeds_seniority(text: str, min_years_to_exclude: int) -> bool:
    for n in range(min_years_to_exclude, 21):
        for template in _YEARS_PATTERN_TEMPLATES:
            if re.search(template.format(n=n), text, re.IGNORECASE):
                return True
    return False


def is_match(job: Job, matching_config: dict[str, Any]) -> bool:
    full_text = f"{job.title}\n{job.location}\n{job.description}"

    exclude_keywords = matching_config.get("exclude_keywords", [])
    if _contains_any(full_text, exclude_keywords):
        return False

    role_keywords = matching_config.get("role_keywords", [])
    if role_keywords and not _contains_any(full_text, role_keywords):
        return False

    min_years_to_exclude = matching_config.get("seniority", {}).get("min_years_to_exclude", 3)
    if _exceeds_seniority(full_text, min_years_to_exclude):
        return False

    locations = matching_config.get("locations", {})
    primary = locations.get("primary", [])
    remote_hybrid = locations.get("remote_hybrid_keywords", [])
    location_text = f"{job.location}\n{job.description}"
    if primary and not _contains_any(location_text, primary):
        if not (remote_hybrid and _contains_any(location_text, remote_hybrid)):
            return False

    return True
