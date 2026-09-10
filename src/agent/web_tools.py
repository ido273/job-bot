"""web_search / fetch_page tools for the AI agent's own discovery
(discovery.py). Same anti-blocking discipline as scrapers/base.py (random
UA, robots.txt check, rate-limit sleep) reimplemented as small free
functions here rather than stretching scrapers.base.SiteScraper -- that ABC
is built around a fixed per-site `site_config` (search_urls, robots_txt_url
known up front), which a general-purpose "fetch whatever URL the model asks
for" tool doesn't have.
"""

import logging
import random
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("jobbot.agent.web_tools")

SEARCH_TIMEOUT_SECONDS = 15
FETCH_TIMEOUT_SECONDS = 15
MAX_RESULTS = 8
MAX_PAGE_TEXT_CHARS = 8000

_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def _random_user_agent(anti_blocking_config: dict) -> str:
    user_agents = anti_blocking_config.get("user_agents") or [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ]
    return random.choice(user_agents)


def _headers(anti_blocking_config: dict) -> dict:
    return {
        "User-Agent": _random_user_agent(anti_blocking_config),
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _robots_allowed(url: str, anti_blocking_config: dict) -> bool:
    if not anti_blocking_config.get("respect_robots_txt", True):
        return True
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = _robots_cache.get(robots_url)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        try:
            # Fetch it ourselves with realistic headers rather than
            # parser.read() -- see scrapers/base.py's identical note on why
            # a bare urllib UA can get a false "disallow everything" 403.
            resp = requests.get(robots_url, headers=_headers(anti_blocking_config), timeout=10)
            if resp.status_code == 404:
                parser.allow_all = True
            else:
                resp.raise_for_status()
                parser.parse(resp.text.splitlines())
        except requests.RequestException:
            return True  # fail open on a transient robots.txt fetch error
        _robots_cache[robots_url] = parser
    return parser.can_fetch(_headers(anti_blocking_config)["User-Agent"], url)


def _rate_limit_sleep(anti_blocking_config: dict) -> None:
    low = anti_blocking_config.get("min_request_delay_seconds", 2.0)
    high = anti_blocking_config.get("max_request_delay_seconds", 3.5)
    time.sleep(random.uniform(low, high))


def web_search(query: str, config) -> str:
    searxng_base_url = config.agent["searxng_base_url"]
    try:
        resp = requests.get(
            f"{searxng_base_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "language": "he"},
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])[:MAX_RESULTS]
    except (requests.RequestException, ValueError) as exc:
        logger.warning("web_search query=%r failed: %s", query, exc)
        return f"Error: web search unavailable ({exc})"

    if not results:
        return "No results found."

    lines = []
    for r in results:
        lines.append(f"- {r.get('title', '')}\n  URL: {r.get('url', '')}\n  {r.get('content', '')[:300]}")
    return "\n".join(lines)


def fetch_page(url: str, config) -> str:
    if "linkedin.com" in urlparse(url).netloc.lower():
        return "[linkedin.com skipped -- blocked to scrapers, use the search result snippet instead]"

    anti_blocking_config = config.anti_blocking
    if not _robots_allowed(url, anti_blocking_config):
        return f"Error: robots.txt disallows fetching {url}"

    _rate_limit_sleep(anti_blocking_config)
    try:
        resp = requests.get(url, headers=_headers(anti_blocking_config), timeout=FETCH_TIMEOUT_SECONDS)
        if resp.status_code in (403, 429, 503):
            return f"Error: HTTP {resp.status_code} fetching {url} (likely blocked)"
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("fetch_page url=%r failed: %s", url, exc)
        return f"Error: could not fetch {url} ({exc})"

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    if len(text) > MAX_PAGE_TEXT_CHARS:
        text = text[:MAX_PAGE_TEXT_CHARS] + "\n…(truncated)"
    return text
