"""Dialog (dialog.co.il) scraper.

Plain server-rendered HTML, no SPA framework, no bot-detection cookies
observed (only standard ASP.NET session + CSRF token) -- verified against
a live fetch of the dedicated DevOps category page on 2026-09-02. Company
names aren't shown in listings (Dialog anonymizes the hiring company,
recruitment-agency style, same as Drushim) -- Job.company is left empty.
"""

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.dialog")

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


class DialogScraper(SiteScraper):
    name = "dialog"
    display_name = "Dialog"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        base_url = self.site_config.get("base_url", "https://www.dialog.co.il")
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
            raise BlockedError("Dialog response matched a block/CAPTCHA marker")
        if "item_job" not in html:
            raise BlockedError("No job items found in Dialog response (layout change or block)")

    def _parse_listing_page(self, html: str, base_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for container in soup.find_all("div", class_="item_job"):
            job = self._parse_container(container, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_container(self, container, base_url: str) -> Job | None:
        title_el = container.find("h3", class_="title_job")
        if title_el is None:
            return None
        title_link = title_el.find("a", href=True)
        if title_link is None:
            return None
        title = title_link.get_text(strip=True)
        job_url = urljoin(base_url, title_link["href"])

        top_div = container.find("div", class_="top")
        description = ""
        if top_div is not None:
            desc_div = top_div.find("div", class_="desc")
            if desc_div is not None:
                description = desc_div.get_text("\n", strip=True)

        req_div = container.find("div", class_="req_list")
        if req_div is not None:
            req_text = req_div.get_text("\n", strip=True)
            description = f"{description}\n\nדרישות:\n{req_text}" if description else req_text

        skills = [img.get("alt", "").strip() for img in container.select("ul.skills_list li img") if img.get("alt")]
        if skills:
            description += f"\n\nSkills: {', '.join(skills)}"

        location = ""
        job_info = container.find("ul", class_="job_info")
        if job_info is not None:
            loc_link = job_info.find("a")
            if loc_link is not None:
                location = loc_link.get_text(strip=True)

        work_mode = _guess_work_mode(f"{location} {description}")

        return Job(
            url=job_url,
            title=title,
            company="",  # Dialog doesn't show the hiring company in listings
            location=location,
            work_mode=work_mode,
            source_site=self.name,
            description=description,
        )
