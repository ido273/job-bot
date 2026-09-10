"""Task 1 -- independent discovery. Called once per cycle by loop.py. Gives
the model web_search + fetch_page and lets it go find real, currently-open
postings on its own, using the same role/exclude/seniority/location criteria
config.yaml already defines for the scrapers (read, never duplicated).
"""

import logging

from .. import db, notification_engine
from ..models import Job
from ..notifiers import build_channels
from ..summarizer import summarize_requirements
from . import cv_reader, tool_loop, web_tools
from .parsing import extract_last_json_block

logger = logging.getLogger("jobbot.agent.discovery")

ORIGIN_TAG = "🤖 מסוכן ה-AI"

SYSTEM_PROMPT = (
    "You are a job-search agent for a candidate. Your job is to search the open web and find REAL, "
    "currently-open job postings that are genuine fits for the candidate -- not just keyword matches. "
    "Follow promising leads (search, then fetch the actual posting page) before deciding. Use the "
    "candidate's CV to judge fit, not just the job title.\n\n"
    "When you are done searching, respond with ONLY this JSON object as your final answer:\n"
    '{"candidates": [{"title": "...", "company": "...", "location": "...", '
    '"work_mode": "onsite|hybrid|remote|unspecified", "url": "...", '
    '"requirements_summary": "...", "description": "...", "relevance_score": <1-10>, '
    '"rationale_he": "<short Hebrew rationale>"}, ...]}\n'
    'Use "unspecified" for work_mode whenever the posting does not clearly state it -- never guess. '
    "Only include postings you are reasonably confident are still open and real. It is fine for "
    '"candidates" to be an empty list if you found nothing genuinely worth surfacing.'
)


def _criteria_message(matching_config: dict, cv_text: str) -> str:
    locations = matching_config.get("locations", {})
    return (
        f"Role keywords to search for: {', '.join(matching_config.get('role_keywords', []))}\n"
        f"Exclude (disqualifying): {', '.join(matching_config.get('exclude_keywords', []))}\n"
        f"Max years of experience wanted: under {matching_config.get('seniority', {}).get('min_years_to_exclude', 3)}\n"
        f"Preferred locations: {', '.join(locations.get('primary', []))} "
        f"(remote/hybrid also acceptable: {', '.join(locations.get('remote_hybrid_keywords', []))})\n\n"
        f"Candidate's CV:\n{cv_text}\n\n"
        "Search the web for matching, currently-open job postings now."
    )


def run_discovery_cycle(config, conn) -> dict:
    agent_config = config.agent
    cv_text = cv_reader.read_cv(conn)

    tools = {
        "web_search": {
            "description": "Search the open web.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
        "fetch_page": {
            "description": "Fetch a URL and return its visible text (linkedin.com URLs are skipped -- use the search snippet instead).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
        },
    }
    dispatch = {
        "web_search": lambda query: web_tools.web_search(query, config),
        "fetch_page": lambda url: web_tools.fetch_page(url, config),
    }

    # Not caught here: requests.RequestException means Ollama dropped mid-cycle
    # (loop.py's health check already confirmed it was up before calling in) --
    # let it propagate up to loop.py, which logs it and lets the next
    # iteration's health check handle recovery.
    raw, calls_used = tool_loop.run_tool_loop(
        system_prompt=SYSTEM_PROMPT,
        user_message=_criteria_message(config.matching, cv_text),
        tools=tools,
        dispatch=dispatch,
        base_url=agent_config["ollama_base_url"],
        model=agent_config["ollama_model"],
        timeout=agent_config["scoring_timeout_seconds"],
        max_tool_calls=agent_config["max_tool_calls_per_cycle"],
    )

    parsed = extract_last_json_block(raw) or {}
    candidates = parsed.get("candidates") or []
    if not isinstance(candidates, list):
        logger.warning("discovery: model's final answer had no usable 'candidates' list: %r", raw[:500])
        candidates = []

    min_score = agent_config["min_relevance_score"]
    channels = None
    inserted = 0

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title", "")).strip()
        url = str(candidate.get("url", "")).strip()
        if not title or not url:
            continue
        try:
            score = int(candidate.get("relevance_score", 0))
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            continue

        company = str(candidate.get("company", "")).strip()
        if db.job_exists_by_url(conn, url) or db.title_company_exists(conn, title, company):
            continue

        work_mode = str(candidate.get("work_mode", "unspecified")).strip().lower()
        if work_mode not in ("onsite", "hybrid", "remote", "unspecified"):
            work_mode = "unspecified"
        description = str(candidate.get("description") or candidate.get("requirements_summary") or "")

        job = Job(
            url=url,
            title=title,
            company=company,
            location=str(candidate.get("location", "")).strip(),
            work_mode=work_mode,
            source_site="ai_agent",
            description=description,
        )
        job_id = db.save_job(conn, job)
        rationale = str(candidate.get("rationale_he", ""))
        db.set_job_relevance(conn, job_id, score, rationale)

        if channels is None:
            channels = build_channels(config)
        summary = summarize_requirements(description, config.summary_config.get("known_tools", []), config.summary_config.get("max_chars", 300))
        notification_engine.enqueue_or_send(conn, channels, job_id, job, summary, origin_tag=ORIGIN_TAG)
        inserted += 1

    logger.info("discovery cycle complete: candidates=%d inserted=%d tool_calls=%d", len(candidates), inserted, calls_used)
    return {"candidates_found": len(candidates), "inserted": inserted, "tool_calls_used": calls_used}
