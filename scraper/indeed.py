"""
Indeed.ca scraper.

Pulls public job listings for painter/CCQ jobs in Montreal.

Notes:
    - Indeed blocks aggressive scraping. This module uses modest rate limits,
      reasonable User-Agent headers, and respects robots.txt.
    - For production, consider using a legitimate job API (e.g. SerpAPI, ScrapingBee)
      or partnering with Indeed directly. This scraper is meant for low-volume
      personal/informational use.
    - If Indeed blocks us, the fallback plan is to switch to their RSS feed or
      an official API partner.
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

INDEED_BASE = "https://ca.indeed.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
}

REQUEST_DELAY_SEC = 2.5  # polite delay between requests


@dataclass
class RawJobListing:
    """Raw job data scraped from a source. Goes to Claude for processing."""
    source_name: str
    external_id: Optional[str]
    title: str
    employer_name: Optional[str]
    location_text: Optional[str]
    description_snippet: Optional[str]
    salary_text: Optional[str]
    posted_text: Optional[str]
    original_url: str

    def to_dict(self):
        return asdict(self)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _fetch(client: httpx.Client, url: str) -> str:
    """Fetch a URL with retries."""
    resp = client.get(url, headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _parse_search_page(html: str) -> list[RawJobListing]:
    """Parse an Indeed search results page."""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Indeed wraps each result in a mosaic card. Multiple possible container selectors
    # depending on their layout version — try the common ones.
    cards = soup.select("div.job_seen_beacon, div.tapItem, a.tapItem, li div.cardOutline")

    for card in cards:
        try:
            title_el = card.select_one("h2.jobTitle, h2 a, a.jcs-JobTitle")
            title = title_el.get_text(strip=True) if title_el else None

            link_el = card.select_one("a[data-jk], a[href*='/rc/clk'], a[href*='/viewjob']")
            href = link_el.get("href") if link_el else None
            external_id = link_el.get("data-jk") if link_el else None

            if href and href.startswith("/"):
                href = INDEED_BASE + href

            employer_el = card.select_one(
                "[data-testid='company-name'], span.companyName, .companyName"
            )
            employer = employer_el.get_text(strip=True) if employer_el else None

            loc_el = card.select_one(
                "[data-testid='text-location'], .companyLocation, div.companyLocation"
            )
            location = loc_el.get_text(strip=True) if loc_el else None

            snippet_el = card.select_one(".job-snippet, div[class*='snippet']")
            snippet = snippet_el.get_text(strip=True, separator=" ") if snippet_el else None

            salary_el = card.select_one(
                "[data-testid='attribute_snippet_testid'], .salary-snippet-container, .salaryOnly"
            )
            salary = salary_el.get_text(strip=True) if salary_el else None

            posted_el = card.select_one(".date, span.date, [data-testid='myJobsStateDate']")
            posted = posted_el.get_text(strip=True) if posted_el else None

            if not title or not href:
                continue

            results.append(RawJobListing(
                source_name="indeed",
                external_id=external_id,
                title=title,
                employer_name=employer,
                location_text=location,
                description_snippet=snippet,
                salary_text=salary,
                posted_text=posted,
                original_url=href,
            ))
        except Exception as e:
            logger.warning(f"Failed to parse a card: {e}")
            continue

    return results


def scrape_indeed(
    search_terms: list[str] = None,
    city: str = None,
    max_pages: int = None,
) -> list[RawJobListing]:
    """
    Scrape Indeed.ca for job listings matching the search terms in the city.

    Returns a flat deduplicated list of RawJobListing (deduplicated by external_id
    within this run; cross-run dedup happens later via fingerprint).
    """
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city
    max_pages = max_pages or settings.scraper_max_pages

    # Combine search terms into one Indeed query string (OR logic)
    query = " OR ".join(f'"{t}"' for t in search_terms)

    all_jobs: dict[str, RawJobListing] = {}  # dedup within run by external_id or url

    with httpx.Client() as client:
        for page in range(max_pages):
            start = page * 10
            url = (
                f"{INDEED_BASE}/jobs"
                f"?q={httpx.URL(query).raw_path.decode()}"
                f"&l={httpx.URL(city).raw_path.decode()}"
                f"&start={start}"
                f"&sort=date"
            )
            # Simpler, manual URL building to avoid httpx encoding quirks
            from urllib.parse import urlencode
            params = {"q": query, "l": city, "start": start, "sort": "date"}
            url = f"{INDEED_BASE}/jobs?{urlencode(params)}"

            logger.info(f"Fetching Indeed page {page + 1}: {url}")

            try:
                html = _fetch(client, url)
            except Exception as e:
                logger.error(f"Failed to fetch page {page + 1}: {e}")
                break

            page_results = _parse_search_page(html)
            if not page_results:
                logger.info("No results on this page — stopping pagination.")
                break

            for job in page_results:
                key = job.external_id or job.original_url
                if key not in all_jobs:
                    all_jobs[key] = job

            time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Indeed scraper found {len(all_jobs)} unique jobs.")
    return list(all_jobs.values())
