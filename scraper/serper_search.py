"""
Indeed-direct scraper via Serper.dev's /scrape endpoint.

Strategy:
    1. Hit Indeed search URLs directly (peintre ccq, peintre construction, etc.)
    2. Serper fetches the HTML with their residential IPs (bypasses Indeed's 403)
    3. Parse the returned HTML to extract individual job listings
    4. Return clean RawJobListing objects ready for Claude classification

Why this is better than the /search approach:
    - We use Indeed's native job search (already optimized for jobs)
    - Results are 100% Indeed jobs, no mix of electricians/labourers
    - No Google listing pages ("25+ offres...") to filter out
    - Much higher signal-to-noise ratio
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import quote_plus
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_SCRAPE_ENDPOINT = "https://scrape.serper.dev"
INDEED_BASE = "https://emplois.ca.indeed.com"
REQUEST_DELAY_SEC = 1.5


@dataclass
class RawJobListing:
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


def _build_indeed_urls(search_terms: list[str], city: str, radius_km: int = 60) -> list[str]:
    """Build clean Indeed search URLs (no Cloudflare tokens, no vjk)."""
    urls = []
    encoded_location = quote_plus(f"{city}, QC")
    for term in search_terms:
        encoded_q = quote_plus(term)
        url = (
            f"{INDEED_BASE}/jobs"
            f"?q={encoded_q}"
            f"&l={encoded_location}"
            f"&radius={radius_km}"
            f"&sort=date"
        )
        urls.append(url)
    return urls


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_scrape(client: httpx.Client, url: str) -> dict:
    """Ask Serper to scrape a URL for us."""
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "url": url,
        "includeMarkdown": False,
    }
    resp = client.post(SERPER_SCRAPE_ENDPOINT, json=payload, headers=headers, timeout=60)
    if resp.status_code == 429:
        logger.warning("Serper scrape rate limit hit — backing off")
        raise httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _parse_indeed_html(html: str) -> list[RawJobListing]:
    """Parse Indeed's search results HTML to extract individual jobs."""
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    jobs = []

    cards = soup.select(
        "div.job_seen_beacon, div.tapItem, li.css-1ac2h1w, "
        "div[data-testid='mosaic-provider-jobcards'] li, "
        "div.cardOutline, a.tapItem"
    )

    if not cards:
        cards = soup.select("[data-jk]")

    for card in cards:
        try:
            jk = card.get("data-jk")
            if not jk:
                jk_el = card.select_one("[data-jk]")
                if jk_el:
                    jk = jk_el.get("data-jk")

            title_el = card.select_one(
                "h2.jobTitle span, h2.jobTitle a, a.jcs-JobTitle, "
                "h2 a span[title], h2 span[title]"
            )
            title = None
            if title_el:
                title = title_el.get("title") or title_el.get_text(strip=True)
            if not title:
                title_el = card.select_one("h2")
                if title_el:
                    title = title_el.get_text(strip=True)

            link_el = card.select_one("h2 a, a.jcs-JobTitle, a[data-jk]")
            href = link_el.get("href") if link_el else None
            if href and href.startswith("/"):
                href = INDEED_BASE + href
            if not href and jk:
                href = f"https://ca.indeed.com/viewjob?jk={jk}"

            employer_el = card.select_one(
                "[data-testid='company-name'], span.companyName, "
                ".companyName, [class*='companyName']"
            )
            employer = employer_el.get_text(strip=True) if employer_el else None

            loc_el = card.select_one(
                "[data-testid='text-location'], .companyLocation, "
                "div.companyLocation, [class*='locationsContainer']"
            )
            location = loc_el.get_text(strip=True) if loc_el else None

            snippet_el = card.select_one(
                ".job-snippet, div[class*='snippet'], "
                "[data-testid='jobsnippet_footer'], ul li"
            )
            snippet = snippet_el.get_text(strip=True, separator=" ") if snippet_el else None

            salary_el = card.select_one(
                "[data-testid='attribute_snippet_testid'], "
                ".salary-snippet-container, .salaryOnly, "
                "div[class*='salary']"
            )
            salary = salary_el.get_text(strip=True) if salary_el else None

            posted_el = card.select_one(
                ".date, span.date, [data-testid*='Date'], "
                "[class*='myJobsStateDate']"
            )
            posted = posted_el.get_text(strip=True) if posted_el else None

            if not title or not href:
                continue

            jobs.append(RawJobListing(
                source_name="indeed",
                external_id=jk,
                title=title,
                employer_name=employer,
                location_text=location,
                description_snippet=snippet,
                salary_text=salary,
                posted_text=posted,
                original_url=href,
            ))
        except Exception as e:
            logger.debug(f"Failed to parse a card: {e}")
            continue

    return jobs


def search_jobs(
    search_terms: list[str] = None,
    city: str = None,
    radius_km: int = 60,
) -> list[RawJobListing]:
    """
    Scrape Indeed search pages via Serper for each search term.
    Returns deduplicated list of individual job postings.
    """
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    urls = _build_indeed_urls(search_terms, city, radius_km=radius_km)

    all_jobs: dict[str, RawJobListing] = {}

    if not settings.serper_api_key:
        logger.error("SERPER_API_KEY is not set — cannot run Serper scraper.")
        return []

    with httpx.Client() as client:
        for url in urls:
            logger.info(f"Scraping Indeed: {url}")
            try:
                data = _serper_scrape(client, url)
            except Exception as e:
                logger.error(f"Serper scrape failed for {url}: {e}")
                continue

            # Serper returns the scraped content in various fields
            html = data.get("html") or data.get("content") or ""

            jobs = []
            if html:
                jobs = _parse_indeed_html(html)
                logger.info(f"  Parsed {len(jobs)} jobs from HTML (HTML size: {len(html)} chars)")
            else:
                keys = list(data.keys())
                logger.warning(
                    f"  No HTML content. Serper response keys: {keys}"
                )

            for job in jobs:
                key = job.external_id or job.original_url
                if key and key not in all_jobs:
                    all_jobs[key] = job

            time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Indeed scraper kept {len(all_jobs)} unique jobs.")
    return list(all_jobs.values())


# Backwards compatibility with run.py
def scrape_indeed(*args, **kwargs):
    return search_jobs()
