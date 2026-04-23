"""
Serper.dev Google Search API scraper.

Uses Serper.dev as a managed proxy to Google Search. Returns results in JSON
just like Google Custom Search JSON API, but without the "closed to new
customers" restriction.

Free tier: 2500 searches included at signup. Paid: $50/mo for 50k searches.

Docs: https://serper.dev/api-key / https://serper.dev/playground
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_ENDPOINT = "https://google.serper.dev/search"
REQUEST_DELAY_SEC = 1.0  # polite delay between API calls


@dataclass
class RawJobListing:
    """Raw job data from Serper search. Goes to Claude for processing."""
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


def _source_from_url(url: str) -> str:
    """Figure out which source a URL belongs to."""
    u = (url or "").lower()
    if "jobboom.com" in u:
        return "jobboom"
    if "jobillico.com" in u:
        return "jobillico"
    if "indeed.com" in u:
        return "indeed"
    if "guichetemplois.gc.ca" in u or "jobbank.gc.ca" in u:
        return "guichetemplois"
    if "facebook.com" in u:
        return "facebook"
    return "indeed"  # fallback — maps to something already in DB


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_query(client: httpx.Client, query: str, page: int = 1) -> dict:
    """Make one Serper.dev Search API call."""
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "gl": "ca",       # Geographic location: Canada
        "hl": "fr",       # Interface language: French
        "num": 10,        # 10 results per page
        "page": page,     # 1-indexed pagination
    }
    resp = client.post(SERPER_ENDPOINT, json=payload, headers=headers, timeout=30)
    if resp.status_code == 429:
        logger.warning("Serper rate limit hit — backing off")
        raise httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _parse_search_item(item: dict) -> Optional[RawJobListing]:
    """Convert a Serper search result into a RawJobListing."""
    url = item.get("link") or ""
    if not url:
        return None

    title = (item.get("title") or "").strip()
    snippet = (item.get("snippet") or "").strip()

    # Serper sometimes provides rich data for Indeed/LinkedIn job listings
    rich = item.get("richSnippet") or {}
    top = rich.get("top") or {}
    detected = top.get("detectedExtensions") or {}

    # Try to pull employer / location / date from rich snippet
    employer = detected.get("company") or None
    location = detected.get("address") or detected.get("location") or None
    posted = detected.get("postedat") or detected.get("postedtime") or None

    # Fallback: parse location out of the snippet text (e.g. "... Montreal, QC")
    return RawJobListing(
        source_name=_source_from_url(url),
        external_id=None,
        title=title,
        employer_name=employer,
        location_text=location,
        description_snippet=snippet,
        salary_text=None,
        posted_text=posted,
        original_url=url,
    )


def search_jobs(
    search_terms: list[str] = None,
    city: str = None,
    max_pages_per_query: int = 2,
) -> list[RawJobListing]:
    """
    Run all configured search terms through Serper.dev.

    For each term, pulls up to `max_pages_per_query` pages (10 results each).
    Deduplicates by URL within this run.
    """
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    # Build queries that match what a person would actually type on Google
    queries = []
    for term in search_terms:
        queries.append(f"{term} {city}")
        queries.append(f"emploi {term} {city}")

    # Restrict to the job boards we care about, via Google's `site:` operator.
    # This keeps results focused on real job listings.
    site_restriction = (
        "(site:jobboom.com OR site:jobillico.com OR site:ca.indeed.com "
        "OR site:indeed.com OR site:guichetemplois.gc.ca OR site:jobbank.gc.ca)"
    )
    queries = [f"{q} {site_restriction}" for q in queries]

    all_jobs: dict[str, RawJobListing] = {}  # dedup by URL

    if not settings.serper_api_key:
        logger.error("SERPER_API_KEY is not set — cannot run Serper scraper.")
        return []

    with httpx.Client() as client:
        for query in queries:
            logger.info(f"Serper search: {query!r}")

            for page in range(1, max_pages_per_query + 1):
                try:
                    data = _serper_query(client, query, page=page)
                except Exception as e:
                    logger.error(f"Serper query failed for {query!r} page={page}: {e}")
                    break

                items = data.get("organic") or []
                if not items:
                    logger.info(f"  No more results for {query!r} at page={page}")
                    break

                for item in items:
                    job = _parse_search_item(item)
                    if job and job.original_url not in all_jobs:
                        all_jobs[job.original_url] = job

                time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Serper search found {len(all_jobs)} unique job results.")
    return list(all_jobs.values())


# Backwards compatibility: run.py still imports `scrape_indeed`
def scrape_indeed(*args, **kwargs):
    """Legacy alias — now runs Serper.dev across all configured sources."""
    return search_jobs()
