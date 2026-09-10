"""Task 2 -- second-pass review of a scraper-found match, wired into
src/main.py's run_cycle right after a keyword match is saved. Single-shot
prompt, no tools: the full job description is already on the Job the
scraper produced, nothing needs fetching.

Returns None whenever the agent can't be trusted for this job (Ollama
unreachable, or it came back but didn't actually answer with the expected
JSON shape) -- the caller's job is to treat None as "fall back to
pre-agent behavior for this one job", never to crash the scrape cycle.
"""

import logging
from dataclasses import dataclass

import requests

from . import ollama_client
from .parsing import extract_last_json_block

logger = logging.getLogger("jobbot.agent.scoring")

SYSTEM_PROMPT = """You are helping a job-seeking candidate filter job postings by relevance.
Score how well the given job posting fits the candidate on a scale of 1 (poor fit) to 10 (excellent fit), \
using the candidate's CV/background and their stated search criteria below. Penalize postings that clearly \
violate an exclusion keyword or exceed the maximum experience level, even if other things about it look good.

Respond with ONLY a single JSON object, nothing else:
{"relevance_score": <integer 1-10>, "rationale_he": "<one or two sentence rationale, written in Hebrew>"}
"""


@dataclass
class ScoreResult:
    relevance_score: int
    rationale_he: str


def _criteria_block(matching_config: dict) -> str:
    locations = matching_config.get("locations", {})
    return (
        f"Role keywords: {', '.join(matching_config.get('role_keywords', []))}\n"
        f"Exclude keywords: {', '.join(matching_config.get('exclude_keywords', []))}\n"
        f"Max years of experience wanted: under {matching_config.get('seniority', {}).get('min_years_to_exclude', 3)}\n"
        f"Preferred locations: {', '.join(locations.get('primary', []))} "
        f"(remote/hybrid also acceptable: {', '.join(locations.get('remote_hybrid_keywords', []))})"
    )


def score_job(job, matching_config: dict, cv_text: str, config) -> ScoreResult | None:
    agent_config = config.agent
    user_message = (
        f"CANDIDATE'S SEARCH CRITERIA:\n{_criteria_block(matching_config)}\n\n"
        f"CANDIDATE'S CV:\n{cv_text}\n\n"
        f"JOB POSTING:\n"
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
        f"Work mode: {job.work_mode}\nDescription:\n{job.description}"
    )
    try:
        raw = ollama_client.chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}],
            base_url=agent_config["ollama_base_url"],
            model=agent_config["ollama_model"],
            timeout=agent_config["scoring_timeout_seconds"],
        )
    except requests.RequestException as exc:
        logger.warning("score_job: Ollama unreachable (%s), falling back to unreviewed notification", exc)
        return None

    parsed = extract_last_json_block(raw)
    if not parsed or "relevance_score" not in parsed:
        logger.warning("score_job: could not parse a relevance_score out of model output: %r", raw[:500])
        return None

    try:
        score = int(parsed["relevance_score"])
    except (TypeError, ValueError):
        logger.warning("score_job: relevance_score not an int: %r", parsed.get("relevance_score"))
        return None

    return ScoreResult(relevance_score=max(1, min(10, score)), rationale_he=str(parsed.get("rationale_he", "")))
