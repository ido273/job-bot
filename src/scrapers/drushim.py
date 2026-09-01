"""Drushim (drushim.co.il) scraper.

Drushim is a Next.js app whose search-results page embeds the full result
set as JSON in a `<script id="__NEXT_DATA__">` tag -- no per-job detail
fetch needed. Verified against live data on 2026-09-01: job descriptions
in that JSON ran up to ~2600 chars with no truncation markers observed
across a real result set, so this is the full text, not an excerpt.

Drushim runs bot-detection (PerimeterX/HUMAN-style `__uzma`/`__uzmb`/...
cookies): a cookie-less request to the search URL comes back as a page
shell with no embedded JSON at all. Confirmed live that reusing cookies
from a prior page load on the same session fixes this, so this scraper
always warms up with a homepage GET first, on the same `requests.Session`
(which persists cookies automatically) as the search request.
"""

import json
import logging
import re
from urllib.parse import urljoin

import requests

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.drushim")

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

_HYBRID_KEYWORDS = ["hybrid", "היברידי"]
_REMOTE_KEYWORDS = ["remote", "עבודה מהבית", "מהבית"]

# Kept to unambiguous phrases only -- a bare "captcha" false-positives on
# Drushim's own "protected by Google reCAPTCHA" footer disclaimer, present
# on every normal page (same lesson as AllJobs' "רובוט" false positive).
_BLOCK_MARKERS = ["access denied", "unusual traffic", "are you a robot", "verify you are human"]


def _guess_work_mode(scopes: list[str], text: str) -> str:
    combined = (" ".join(scopes) + " " + text).lower()
    if any(kw.lower() in combined for kw in _HYBRID_KEYWORDS):
        return "hybrid"
    if any(kw.lower() in combined for kw in _REMOTE_KEYWORDS):
        return "remote"
    return "onsite"


class DrushimScraper(SiteScraper):
    name = "drushim"
    display_name = "Drushim"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        base_url = self.site_config.get("base_url", "https://www.drushim.co.il")

        # Bot-detection cookies are only issued/accepted after a "normal"
        # page load -- warm the session up before hitting a search URL.
        warm_resp = self.get(session, base_url + "/")
        self._check_block(warm_resp.text)
        # A real browser never navigates from homepage to search results in
        # ~0ms; always pace this gap too, not just between search_urls[i>0].
        self.rate_limit_sleep()

        jobs: list[Job] = []
        search_urls = self.site_config.get("search_urls", [])
        for i, url in enumerate(search_urls):
            if i > 0:
                self.rate_limit_sleep()
            resp = self.get(session, url)
            self._check_block(resp.text)
            jobs.extend(self._parse_search_page(resp.text, base_url))
        return jobs

    def _check_block(self, html: str) -> None:
        lowered = html.lower()
        if any(marker in lowered for marker in _BLOCK_MARKERS):
            raise BlockedError("Drushim response matched a block/CAPTCHA marker")

    def _parse_search_page(self, html: str, base_url: str) -> list[Job]:
        match = NEXT_DATA_RE.search(html)
        if match is None:
            # Bot-detection served a shell without the embedded JSON --
            # treat as degraded rather than silently returning nothing
            # every cycle (this is also what a real layout change looks like).
            raise BlockedError("No __NEXT_DATA__ found in Drushim response (layout change or block)")

        try:
            data = json.loads(match.group(1))
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        except (KeyError, json.JSONDecodeError) as exc:
            raise BlockedError(f"Unexpected Drushim __NEXT_DATA__ shape: {exc}") from exc

        search_queries = [q for q in queries if q.get("queryKey", [None])[0] == "search-results"]
        if not search_queries:
            return []

        jobs: list[Job] = []
        for page in search_queries[0].get("state", {}).get("data", {}).get("pages", []):
            for raw in page.get("jobs", []):
                job = self._parse_job(raw, base_url)
                if job:
                    jobs.append(job)
        return jobs

    def _parse_job(self, raw: dict, base_url: str) -> Job | None:
        job_url = raw.get("jobUrl")
        title = raw.get("title")
        if not job_url or not title:
            return None

        description = raw.get("description", "").replace("<br/>", "\n").replace("<br>", "\n")
        experience = raw.get("experience")
        if experience:
            description = f"ניסיון נדרש: {experience}\n{description}"

        scopes = raw.get("scopes", [])
        location = raw.get("city") or ", ".join(raw.get("cities", []))

        return Job(
            url=urljoin(base_url, job_url),
            title=title,
            company=raw.get("companyName", ""),
            location=location,
            work_mode=_guess_work_mode(scopes, description),
            source_site=self.name,
            description=description,
        )
