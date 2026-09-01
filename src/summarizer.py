"""Short requirements summary for notifications. Plain-text heuristics only
— no LLM call here (that's Phase 3's job). Pulls from the full description
already stored on the Job, so this never needs to re-scrape anything.
"""

import re

REQUIREMENTS_HEADERS = [
    "דרישות",
    "requirements",
    "qualifications",
    "what you'll need",
    "what you need",
    "must have",
]

_YEARS_RE = re.compile(
    r"\d+\+?\s*(?:שנות ניסיון|שנים ניסיון|שנות נסיון|years?|yrs)",
    re.IGNORECASE,
)


def _requirements_bullets(lines: list[str], max_bullets: int = 3) -> list[str]:
    for i, line in enumerate(lines):
        lowered = line.lower()
        if any(header in lowered for header in REQUIREMENTS_HEADERS):
            bullets = []
            for candidate in lines[i + 1 :]:
                cleaned = candidate.strip(" -•\t")
                if cleaned:
                    bullets.append(cleaned)
                if len(bullets) >= max_bullets:
                    break
            return bullets
    return []


def summarize_requirements(description: str, known_tools: list[str], max_chars: int = 300) -> str:
    if not description:
        return ""

    lines = [line.strip() for line in description.split("\n") if line.strip()]

    bullets = _requirements_bullets(lines) or lines[:2]

    years_match = _YEARS_RE.search(description)
    lowered_desc = description.lower()
    tools_found = [t for t in known_tools if t.lower() in lowered_desc]

    parts = []
    if years_match:
        parts.append(f"Experience: {years_match.group(0).strip()}")
    if tools_found:
        parts.append("Tools: " + ", ".join(tools_found[:6]))
    if bullets:
        parts.append(" | ".join(bullets))

    summary = " · ".join(parts)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary
