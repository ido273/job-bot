"""Nisha (nisha.co.il) scraper.

Plain server-rendered WordPress site, no SPA framework -- verified against
a live fetch of the dedicated DevOps positions page on 2026-09-02. No
per-job location field exists; location is mentioned in description prose
instead (matcher.py already scans location+description combined, so this
degrades gracefully). Recruitment-agency style -- no hiring company name
shown in listings, same as Dialog/Gotfriends.
"""

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.nisha")

_HYBRID_KEYWORDS = ["hybrid", "היברידי"]
_REMOTE_KEYWORDS = ["remote", "עבודה מהבית", "מהבית", "מרחוק"]

_BLOCK_MARKERS = ["access denied", "unusual traffic", "are you a robot", "verify you are human"]


def _guess_work_mode(text: str) -> str:
    lowered = text.lower()
    if any(kw.lower() in lowered for kw in _HYBRID_KEYWORDS):
        return "hybrid"
    if any(kw.lower() in lowered for kw in _REMOTE_KEYWORDS):
        return "remote"
    return "onsite"


class NishaScraper(SiteScraper):
    name = "nisha"
    display_name = "Nisha"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        base_url = self.site_config.get("base_url", "https://www.nisha.co.il")
        jobs: list[Job] = []
        search_urls = self.site_config.get("search_urls", [])
        for i, url in enumerate(search_urls):
            if i > 0:
                self.rate_limit_sleep()
            resp = self.get(session, url)
            self._check_block(resp.text)
            jobs.extend(self._parse_listing_page(resp.text, base_url))
        return jobs

    def _check_block(self, html: str) -> None:
        lowered = html.lower()
        if any(marker in lowered for marker in _BLOCK_MARKERS):
            raise BlockedError("Nisha response matched a block/CAPTCHA marker")
        if "item-job" not in html:
            raise BlockedError("No job items found in Nisha response (layout change or block)")

    def _parse_listing_page(self, html: str, base_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for container in soup.find_all("div", class_="item-job"):
            job = self._parse_container(container, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_container(self, container, base_url: str) -> Job | None:
        title_el = container.find("h3", class_="job-title")
        if title_el is None:
            return None
        link = title_el.find("a", href=True)
        if link is None:
            return None
        title = link.get_text(strip=True)
        job_url = urljoin(base_url, link["href"])

        job_content = container.find("div", class_="job-content")
        description = job_content.get_text("\n", strip=True) if job_content else ""

        work_mode = _guess_work_mode(description)

        return Job(
            url=job_url,
            title=title,
            company="",  # Nisha doesn't show the hiring company in listings
            location="",  # no structured field; mentioned in description prose instead
            work_mode=work_mode,
            source_site=self.name,
            description=description,
        )
