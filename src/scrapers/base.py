"""Common interface every site module implements, so sites can be added or
removed independently (see scrapers/alljobs.py for the reference impl).
"""

import random
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests

from ..models import Job


class BlockedError(Exception):
    """Raised by a scraper when it detects a CAPTCHA/block response."""


class SiteScraper(ABC):
    name: str
    display_name: str

    def __init__(self, site_config: dict, anti_blocking_config: dict):
        self.site_config = site_config
        self.anti_blocking_config = anti_blocking_config
        self._robots_cache: dict[str, bool] = {}

    def _random_user_agent(self) -> str:
        user_agents = self.anti_blocking_config.get("user_agents") or [
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        return random.choice(user_agents)

    def _headers(self) -> dict:
        return {
            "User-Agent": self._random_user_agent(),
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _robots_allowed(self, url: str) -> bool:
        if not self.anti_blocking_config.get("respect_robots_txt", True):
            return True
        robots_url = self.site_config.get("robots_txt_url")
        if not robots_url:
            return True
        parser = self._robots_cache_parsers().get(robots_url)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            # NOT parser.read() -- that fetches with urllib's own default
            # "Python-urllib/3.x" User-Agent, which some sites' CDN/WAF
            # (confirmed live: Dialog, Gotfriends, both Cloudflare-fronted)
            # 403s outright even though the exact same path is genuinely
            # allowed for a real browser UA. Per robotparser's documented
            # behavior, a 401/403 on the robots.txt fetch itself makes it
            # conclude "disallow everything" -- a false verdict caused by
            # the *check's own* request, not the site's actual robots.txt.
            # Fetching it ourselves with the same realistic headers used
            # for real requests avoids that trap.
            try:
                resp = requests.get(robots_url, headers=self._headers(), timeout=10)
                if resp.status_code == 404:
                    parser.allow_all = True
                else:
                    resp.raise_for_status()
                    parser.parse(resp.text.splitlines())
            except requests.RequestException:
                # If robots.txt genuinely can't be fetched, fail open (don't
                # block scraping on a transient network error) but log via caller.
                return True
            self._robots_cache_parsers()[robots_url] = parser
        return parser.can_fetch(self._headers()["User-Agent"], url)

    def _robots_cache_parsers(self) -> dict:
        if not hasattr(self, "_robots_parsers"):
            self._robots_parsers = {}
        return self._robots_parsers

    def rate_limit_sleep(self) -> None:
        low = self.anti_blocking_config.get("min_request_delay_seconds", 2.0)
        high = self.anti_blocking_config.get("max_request_delay_seconds", 3.5)
        time.sleep(random.uniform(low, high))

    def get(self, session: requests.Session, url: str) -> requests.Response:
        parsed = urlparse(url)
        if not self._robots_allowed(url):
            raise BlockedError(f"robots.txt disallows fetching {url}")
        resp = session.get(url, headers=self._headers(), timeout=15)
        if resp.status_code in (403, 429, 503):
            raise BlockedError(f"HTTP {resp.status_code} from {parsed.netloc}")
        resp.raise_for_status()
        return resp

    @abstractmethod
    def fetch_new_jobs(self, session: requests.Session) -> list[Job]:
        """Fetch and parse listings, returning every job found on this pass
        (dedup against the DB happens in the caller)."""
        raise NotImplementedError
