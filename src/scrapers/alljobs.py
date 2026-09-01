"""AllJobs (alljobs.co.il) scraper.

AllJobs' listing pages are server-rendered (no JS needed to see job data),
so this uses plain requests + BeautifulSoup per the anti-blocking spec.
Verified against live markup on 2026-08-31: each result sits in
`<div id="job-box-container{JobID}">`, and the canonical per-job URL
(`/Search/UploadSingle.aspx?JobID=...`) is also the job's own description
page (confirmed by fetching it directly), not just an apply form.
"""

import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.alljobs")

CONTAINER_ID_RE = re.compile(r"^job-box-container(\d+)$")

_HYBRID_KEYWORDS = ["hybrid", "היברידי", "היברידית"]
_REMOTE_KEYWORDS = ["remote", "מהבית", "מרחוק", "עבודה מהבית"]

# Signals that AllJobs is showing a CAPTCHA/interstitial instead of results.
# Kept to unambiguous phrases only — e.g. a bare "רובוט" false-positives on
# the site's own "AI robot" assistant widget and on any "רובוטיקה" listing.
_BLOCK_MARKERS = ["captcha", "unusual traffic", "access denied", "are you a robot", "verify you are human"]


def _guess_work_mode(text: str) -> str:
    lowered = text.lower()
    if any(kw.lower() in lowered for kw in _HYBRID_KEYWORDS):
        return "hybrid"
    if any(kw.lower() in lowered for kw in _REMOTE_KEYWORDS):
        return "remote"
    return "onsite"


class AllJobsScraper(SiteScraper):
    name = "alljobs"
    display_name = "AllJobs"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        jobs: list[Job] = []
        search_urls = self.site_config.get("search_urls", [])
        base_url = self.site_config.get("base_url", "https://www.alljobs.co.il")

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
            raise BlockedError("AllJobs response matched a block/CAPTCHA marker")
        if "job-box-container" not in html:
            # Page loaded but AllJobs' layout produced zero listings — either a
            # genuinely empty search or (more likely for a previously-working
            # URL) a layout/block change. Treat as degraded rather than silently
            # returning nothing every cycle.
            raise BlockedError("No job containers found in AllJobs response (layout change or block)")

    def _parse_listing_page(self, html: str, base_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []

        containers = soup.find_all("div", id=CONTAINER_ID_RE)
        for container in containers:
            job = self._parse_container(container, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_container(self, container, base_url: str) -> Job | None:
        content_top = container.find("div", class_="job-content-top")
        if content_top is None:
            return None

        # English-language postings use a "-ltr" class variant on both the
        # title and location containers instead of the Hebrew/RTL default.
        title_div = content_top.find(
            "div", class_=lambda c: c in ("job-content-top-title", "job-content-top-title-ltr")
        )
        if title_div is None:
            return None
        if "closed-job" in title_div.get("class", []):
            return None  # listing already removed by the employer

        title_link = title_div.find("a", href=True)
        if title_link is None:
            return None
        title = title_link.get_text(strip=True)
        job_url = urljoin(base_url, title_link["href"])

        company_div = title_div.find("div", class_="T14")
        company = company_div.get_text(strip=True) if company_div else ""

        location_div = content_top.find(
            "div", class_=lambda c: c in ("job-content-top-location", "job-content-top-location-ltr")
        )
        location = ""
        if location_div:
            location = location_div.get_text(" ", strip=True)
            location = re.sub(r"^(מיקום המשרה:|Location:)\s*", "", location).strip()

        desc_div = content_top.find("div", class_="job-content-top-desc")
        # Newline separator (not space) so <br>-separated lines -- including the
        # "דרישות:" / requirements block -- survive as real lines for storage
        # and for the requirements summarizer to parse.
        description = desc_div.get_text("\n", strip=True) if desc_div else ""
        description = re.sub(r"\n{2,}", "\n", description)

        work_mode = _guess_work_mode(f"{location} {description}")

        return Job(
            url=job_url,
            title=title,
            company=company,
            location=location,
            work_mode=work_mode,
            source_site=self.name,
            description=description,
        )
