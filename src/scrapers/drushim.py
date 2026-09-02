"""Drushim (drushim.co.il) scraper.

Drushim is mid framework-migration: the same search URL, on different
requests (even from the same warmed-up session pattern), comes back as
EITHER a legacy Next.js SPA (`<script id="__NEXT_DATA__">` JSON blob) OR
a newer Vue/Vuetify SSR build (`data-cy="job-item0"` containers, a
`drushim_vue` cookie, `data-v-*` scoped-style hashes -- no embedded JSON,
real jobs rendered straight into HTML). Verified live 2026-09-02 across
5 fresh sessions: ~60% got the Vue variant. Both are genuine content, not
a bot challenge -- confirmed by inspecting a captured Vue-variant response
directly (real title/company/URL/location per listing). The previous
version of this scraper only understood the Next.js shape and treated
every Vue-variant response as "blocked", which silently threw away a
majority of real scan attempts. Both shapes are parsed now; only an
unrecognized third shape (or an actual block marker) raises BlockedError.

Drushim also runs bot-detection (PerimeterX/HUMAN-style `__uzma`/`__uzmb`/
... cookies) independent of the above: a cookie-less request to the
search URL can come back as a page shell with neither shape's data.
Reusing cookies from a prior page load on the same session avoids this,
so this scraper always warms up with a homepage GET first, on the same
`requests.Session` (which persists cookies automatically) as the search
request.
"""

import json
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.drushim")

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_RAW_HTML_LOG_CHARS = 2000

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
        if match is not None:
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

        vue_jobs = self._parse_vue_variant(html, base_url)
        if vue_jobs is not None:
            return vue_jobs

        # Neither known shape matched -- genuine layout change or a block
        # variant we haven't seen yet. Log a snippet so the next occurrence
        # has direct evidence instead of needing a live re-investigation.
        logger.warning("drushim unrecognized response shape, raw HTML snippet: %r", html[:_RAW_HTML_LOG_CHARS])
        raise BlockedError("No __NEXT_DATA__ or known Vue job-list markup found in Drushim response")

    def _parse_vue_variant(self, html: str, base_url: str) -> list[Job] | None:
        """Parses the newer Vue/Vuetify SSR build (see module docstring).
        Returns None (not []) when this shape isn't present at all, so the
        caller can tell "wrong shape, try something else" apart from
        "right shape, zero results"."""
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.find_all("div", attrs={"data-cy": re.compile(r"^job-item\d+$")})
        if not containers:
            return None

        jobs: list[Job] = []
        for container in containers:
            job = self._parse_vue_job(container, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_vue_job(self, container, base_url: str) -> Job | None:
        title_el = container.find("span", class_="job-url")
        link = container.find("a", href=re.compile(r"^/job/"))
        if title_el is None or link is None:
            return None
        title = title_el.get_text(strip=True)
        job_url = urljoin(base_url, link["href"])

        company = ""
        company_el = container.select_one("p.display-22 a")
        if company_el is not None:
            company = company_el.get_text(strip=True)

        details_sub = container.find("div", class_="job-details-sub")
        detail_spans = (
            [s.get_text(strip=True) for s in details_sub.find_all("span", class_="display-18", recursive=True)]
            if details_sub is not None
            else []
        )
        # Nested "|" separator spans get merged into the parent span's text
        # by get_text(); strip them off each entry rather than filtering
        # whole entries, then drop anything left empty.
        detail_spans = [s.strip(" |") for s in detail_spans]
        detail_spans = [s for s in detail_spans if s]
        # First entry is location; the rest (years-experience, employment
        # type, posted-time) get folded into the description below so the
        # matcher's keyword/seniority scan still sees them.
        location = detail_spans[0] if detail_spans else ""

        teaser_el = container.select_one("div.job-intro p.display-18")
        teaser = teaser_el.get_text(strip=True) if teaser_el is not None else ""
        description = "\n".join(filter(None, [teaser, *detail_spans[1:]]))

        return Job(
            url=job_url,
            title=title,
            company=company,
            location=location,
            work_mode=_guess_work_mode([], description),
            source_site=self.name,
            description=description,
        )

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
