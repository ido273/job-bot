"""JobMaster (jobmaster.co.il) scraper.

Plain server-rendered HTML, no SPA framework -- verified against a live
fetch of the `/jobs/q-DevOps/` search-results page on 2026-09-02 (10 real
job cards, real DevOps titles). robots.txt's `Disallow: /jobs/checknum.asp`
sits only under the group naming specific crawlers (Googlebot, Bingbot,
etc.) -- the generic `User-agent: *` group (what a real-browser UA falls
back to) only disallows `/api/`, so this scraper is unaffected either way:
it never fetches a job's own checknum.asp detail page, since the search
page's card already embeds a short description -- checknum.asp is stored
only as the job's canonical URL (same "search result page doubles as
enough to match on" pattern as AllJobs/Drushim), never re-fetched.
"""

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..models import Job
from .base import BlockedError, SiteScraper

logger = logging.getLogger("jobbot.scrapers.jobmaster")

_HYBRID_KEYWORDS = ["hybrid", "היברידי", "היברידית"]
_REMOTE_KEYWORDS = ["remote", "מהבית", "מרחוק", "עבודה מהבית"]

_BLOCK_MARKERS = ["access denied", "unusual traffic", "are you a robot", "verify you are human"]


def _guess_work_mode(text: str) -> str:
    lowered = text.lower()
    if any(kw.lower() in lowered for kw in _HYBRID_KEYWORDS):
        return "hybrid"
    if any(kw.lower() in lowered for kw in _REMOTE_KEYWORDS):
        return "remote"
    return "onsite"


class JobMasterScraper(SiteScraper):
    name = "jobmaster"
    display_name = "JobMaster"

    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        base_url = self.site_config.get("base_url", "https://www.jobmaster.co.il")
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
            raise BlockedError("JobMaster response matched a block/CAPTCHA marker")
        if "JobItem" not in html:
            raise BlockedError("No job cards found in JobMaster response (layout change or block)")

    def _parse_listing_page(self, html: str, base_url: str) -> list[Job]:
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[Job] = []
        for article in soup.find_all("article", class_=lambda c: c and "JobItem" in c.split()):
            job = self._parse_card(article, base_url)
            if job:
                jobs.append(job)
        return jobs

    def _parse_card(self, article, base_url: str) -> Job | None:
        link = article.select_one("a.CardHeader")
        if link is None or not link.get("href"):
            return None
        title = link.get_text(strip=True)
        job_url = urljoin(base_url, link["href"])

        company_el = article.select_one("a.CompanyNameLink span")
        company = company_el.get_text(strip=True) if company_el is not None else ""

        location = ", ".join(li.get_text(strip=True) for li in article.select("li.jobLocation"))

        desc_el = article.select_one("div.jobShortDescription")
        description = desc_el.get_text("\n", strip=True) if desc_el is not None else ""

        job_type_el = article.select_one("li.jobType")
        attrs = [a.get_text(strip=True) for a in article.select("li.jobAttributes a")]
        # Job type + attribute tags (e.g. "עבודה היברידית") aren't shown
        # elsewhere, so fold them into the matcher-visible text rather than
        # dropping them.
        extra = ", ".join(filter(None, [job_type_el.get_text(strip=True) if job_type_el else "", *attrs]))
        if extra:
            description = f"{description}\n{extra}" if description else extra

        return Job(
            url=job_url,
            title=title,
            company=company,
            location=location,
            work_mode=_guess_work_mode(f"{location} {description}"),
            source_site=self.name,
            description=description,
        )
