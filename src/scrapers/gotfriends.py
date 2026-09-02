"""Gotfriends (gotfriends.co.il) scraper.

Plain server-rendered HTML, no SPA framework -- verified against a live
fetch of the dedicated DevOps category page on 2026-09-02. Like Dialog,
Gotfriends is a recruitment-agency-style board and doesn't show the
hiring company's name in listings -- Job.company is left empty.
"""

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.gotfriends")

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


class GotfriendsScraper(SiteScraper):
    name = "gotfriends"
    display_name = "Gotfriends"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        base_url = self.site_config.get("base_url", "https://www.gotfriends.co.il")
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
            raise BlockedError("Gotfriends response matched a block/CAPTCHA marker")
        if 'class="item"' not in html:
            raise BlockedError("No job items found in Gotfriends response (layout change or block)")

    def _parse_listing_page(self, html: str, base_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for container in soup.find_all("div", class_="item"):
            job = self._parse_container(container, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_container(self, container, base_url: str) -> Job | None:
        title_el = container.find("h2", class_="title")
        if title_el is None:
            return None
        link = title_el.find_parent("a", href=True) or container.find("a", class_="position", href=True)
        if link is None:
            return None
        title = title_el.get_text(strip=True)
        job_url = urljoin(base_url, link["href"])

        location = ""
        info_data = container.find("span", class_="info-data")
        if info_data is not None:
            location = info_data.get_text(strip=True)

        # Both the "תיאור המשרה" (description) and "דרישות המשרה" (requirements)
        # blocks share class="desc" -- concatenate whichever are present rather
        # than assuming exactly two, since not every listing has both.
        desc_parts = [d.get_text("\n", strip=True) for d in container.find_all("div", class_="desc")]
        description = "\n\n".join(part for part in desc_parts if part)

        work_mode = _guess_work_mode(f"{location} {description}")

        return Job(
            url=job_url,
            title=title,
            company="",  # Gotfriends doesn't show the hiring company in listings
            location=location,
            work_mode=work_mode,
            source_site=self.name,
            description=description,
        )
