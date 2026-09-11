"""Task 1 -- independent discovery. Called once per cycle by loop.py. Gives
the model web_search + fetch_page and lets it go find real, currently-open
postings on its own, using the same role/exclude/seniority/location criteria
config.yaml already defines for the scrapers (read, never duplicated).

Confirmed live (reproduced directly against the real model, not just
inferred): qwen3:8b will frequently skip fetch_page entirely and fabricate
a "candidate" straight from a web_search snippet -- often a search-results
or category/listing page itself (e.g. a Drushim /jobs/subcat/NNN/ URL, an
Indeed /q-....html search query, a Glassdoor SRCH_ results page), self-
assigning a confident relevance_score to something it never actually read.
The system prompt telling it to fetch before deciding is necessary but not
sufficient -- a smaller model doesn't reliably follow it. So this module
enforces it in code: a candidate is rejected unless its exact URL was
actually passed to fetch_page during this same cycle (see
`_run_with_fetch_tracking`), on top of a URL-pattern reject for the most
obvious listing/search/category shapes as defense in depth (a page CAN be
fetched and still be a listing page the model summarized as if singular --
the fetch-was-called check alone doesn't catch that case).
"""

import logging
import re

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
    "Use the candidate's CV to judge fit, not just the job title.\n\n"
    "HARD REQUIREMENT: you MUST call fetch_page on a URL before including it as a candidate. Never "
    "include a URL you have not fetched -- a web_search snippet alone is not enough to know what a page "
    "actually contains. A candidate whose URL was not fetched will be discarded even if you include it.\n\n"
    "HARD REQUIREMENT: a candidate URL must be ONE SPECIFIC job posting -- a single role at a single "
    "company. It must NOT be a search-results page, a category/listing page, or a job board's landing "
    "page (these often rank highly in search results and look promising from the snippet alone, but "
    "contain no single job description -- e.g. a URL with 'search', 'subcat', a query string of keywords, "
    "or a bare category name is almost always a listing, not a posting). If fetch_page returns a page "
    "listing multiple jobs rather than one specific posting, do NOT use that page as a candidate -- either "
    "find and fetch a specific job's own link from within it, or discard it and move on.\n\n"
    "When you are done searching, respond with ONLY this JSON object as your final answer:\n"
    '{"candidates": [{"title": "...", "company": "...", "location": "...", '
    '"work_mode": "onsite|hybrid|remote|unspecified", "url": "...", '
    '"requirements_summary": "...", "description": "...", "relevance_score": <1-10>, '
    '"rationale_he": "<short Hebrew rationale>"}, ...]}\n'
    'Use "unspecified" for work_mode whenever the posting does not clearly state it -- never guess. '
    "It is fine for \"candidates\" to be an empty list if you found nothing genuinely worth surfacing -- "
    "an empty list is far better than a fabricated one.\n\n"
    "rationale_he must be written in Hebrew only. Do not use Arabic, Chinese, or any non-Hebrew script "
    "characters under any circumstances."
)

# Defense in depth, not the primary gate (that's "was fetch_page actually
# called on this URL" below) -- catches the specific listing/search/category
# URL shapes confirmed live (Drushim /subcat/, Indeed /q-*.html and bare
# "-jobs" category pages, Glassdoor SRCH_ results), general enough to also
# catch the same shapes on other job sites SearxNG might surface.
_LISTING_URL_PATTERNS = [
    re.compile(r"/subcat/", re.IGNORECASE),
    re.compile(r"/search", re.IGNORECASE),
    re.compile(r"SRCH_", re.IGNORECASE),
    re.compile(r"/q-[^/]*\.html?$", re.IGNORECASE),  # e.g. Indeed's /q-<keywords>.html
    re.compile(r"-jobs/?$", re.IGNORECASE),  # e.g. Indeed's /Devops-jobs category page
    re.compile(r"/jobs/?$", re.IGNORECASE),  # a bare .../jobs board landing page
]


def _looks_like_listing_url(url: str) -> bool:
    return any(pattern.search(url) for pattern in _LISTING_URL_PATTERNS)


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


def _run_with_fetch_tracking(config, system_prompt: str, user_message: str, max_tool_calls: int, timeout: float, model: str, base_url: str):
    """Same tools as always, wrapped only to record which exact URLs were
    actually passed to fetch_page during this cycle -- the ground truth
    candidate validation below checks against."""
    fetched_urls: set[str] = set()

    def _tracked_fetch_page(url: str) -> str:
        fetched_urls.add(url.strip())
        return web_tools.fetch_page(url, config)

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
            "description": "Fetch a URL and return its visible text (linkedin.com URLs are skipped -- use the search snippet instead). "
            "You must call this on a URL before it can be used as a candidate.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch"}},
                "required": ["url"],
            },
        },
    }
    dispatch = {
        "web_search": lambda query: web_tools.web_search(query, config),
        "fetch_page": _tracked_fetch_page,
    }

    # Not caught here: requests.RequestException means Ollama dropped mid-cycle
    # (loop.py's health check already confirmed it was up before calling in) --
    # let it propagate up to loop.py, which logs it and lets the next
    # iteration's health check handle recovery.
    raw, calls_used = tool_loop.run_tool_loop(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        dispatch=dispatch,
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_tool_calls=max_tool_calls,
    )
    return raw, calls_used, fetched_urls


def run_discovery_cycle(config, conn) -> dict:
    agent_config = config.agent
    cv_text = cv_reader.read_cv(conn)

    raw, calls_used, fetched_urls = _run_with_fetch_tracking(
        config,
        system_prompt=SYSTEM_PROMPT,
        user_message=_criteria_message(config.matching, cv_text),
        max_tool_calls=agent_config["max_tool_calls_per_cycle"],
        timeout=agent_config["scoring_timeout_seconds"],
        model=agent_config["ollama_model"],
        base_url=agent_config["ollama_base_url"],
    )

    parsed = extract_last_json_block(raw) or {}
    candidates = parsed.get("candidates") or []
    if not isinstance(candidates, list):
        logger.warning("discovery: model's final answer had no usable 'candidates' list: %r", raw[:500])
        candidates = []

    min_score = agent_config["min_relevance_score"]
    channels = None
    inserted = 0
    rejected_unfetched = 0
    rejected_listing_url = 0

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title", "")).strip()
        url = str(candidate.get("url", "")).strip()
        if not title or not url:
            continue

        # Ground-truth check: the model claimed a candidate is a real,
        # specific posting -- confirm it actually fetched that exact URL
        # this cycle rather than fabricating from a search snippet.
        if url not in fetched_urls:
            logger.warning("discovery: rejecting candidate %r (url=%s) -- fetch_page was never called on this URL", title, url)
            rejected_unfetched += 1
            continue

        if _looks_like_listing_url(url):
            logger.warning("discovery: rejecting candidate %r (url=%s) -- looks like a listing/search/category page, not a specific posting", title, url)
            rejected_listing_url += 1
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
        rationale = str(candidate.get("rationale_he", ""))

        job = Job(
            url=url,
            title=title,
            company=company,
            location=str(candidate.get("location", "")).strip(),
            work_mode=work_mode,
            source_site="ai_agent",
            description=description,
            relevance_score=score,
            relevance_rationale=rationale,
        )
        job_id = db.save_job(conn, job)
        db.set_job_relevance(conn, job_id, score, rationale)

        if channels is None:
            channels = build_channels(config)
        summary = summarize_requirements(description, config.summary_config.get("known_tools", []), config.summary_config.get("max_chars", 300))
        notification_engine.enqueue_or_send(conn, channels, job_id, job, summary, origin_tag=ORIGIN_TAG)
        inserted += 1

    logger.info(
        "discovery cycle complete: candidates=%d inserted=%d rejected_unfetched=%d rejected_listing_url=%d tool_calls=%d",
        len(candidates), inserted, rejected_unfetched, rejected_listing_url, calls_used,
    )
    return {
        "candidates_found": len(candidates),
        "inserted": inserted,
        "rejected_unfetched": rejected_unfetched,
        "rejected_listing_url": rejected_listing_url,
        "tool_calls_used": calls_used,
    }
