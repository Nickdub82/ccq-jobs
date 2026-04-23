"""
Google Custom Search API scraper.

Uses Google's official Programmable Search Engine to query multiple job sites
at once (Indeed, Jobboom, Jobillico, Guichet-Emplois) and extract job listings.

This is 100% legal — it's Google's official API. We don't scrape any site
directly; Google does it for us and returns results.

Free tier: 100 queries/day. At 2h intervals with 4-5 search terms per run,
that's ~60 queries/day — comfortably under the limit.

Docs: https://developers.google.com/custom-search/v1/using_rest
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
REQUEST_DELAY_SEC = 1.5  # polite delay between API calls


@dataclass
class RawJobListing:
    """Raw job data from Google search. Goes to Claude for processing."""
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
    u = url.lower()
    if "jobboom.com" in u:
        return "jobboom"
    if "jobillico.com" in u:
        return "jobillico"
    if "indeed.com" in u:
        return "indeed"
    if "guichetemplois.gc.ca" in u or "jobbank.gc.ca" in u:
        return "guichetemplois"
    return "indeed"  # fallback — maps to something already in DB


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _google_query(client: httpx.Client, query: str, start: int = 1) -> dict:
    """Make one Google Custom Search API call."""
    params = {
        "key": settings.google_api_key,
        "cx": settings.google_search_engine_id,
        "q": query,
        "start": start,          # 1, 11, 21... for pagination
        "num": 10,               # max per request
        "hl": "fr",              # French interface
        "gl": "ca",              # geographic location: Canada
        "lr": "lang_fr|lang_en", # French or English
    }
    resp = client.get(GOOGLE_SEARCH_ENDPOINT, params=params, timeout=30)
    if resp.status_code == 429:
        logger.warning("Google rate limit hit — backing off")
        raise httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _parse_search_item(item: dict) -> Optional[RawJobListing]:
    """Convert a Google search result into a RawJobListing."""
    url = item.get("link", "")
    if not url:
        return None

    title = item.get("title", "").strip()
    # Google appends " - Jobboom" or similar — keep as-is, Claude will clean
    snippet = item.get("snippet", "").strip()

    # Try to pull richer metadata from pagemap if available
    pagemap = item.get("pagemap", {})

    # Try for job posting schema.org data
    job_posting = pagemap.get("jobposting", [{}])[0] if "jobposting" in pagemap else {}
    metatags = pagemap.get("metatags", [{}])[0] if "metatags" in pagemap else {}

    employer = (
        job_posting.get("hiringorganization")
        or metatags.get("og:site_name")
        or None
    )

    location = (
        job_posting.get("joblocation")
        or metatags.get("geo.placename")
        or metatags.get("og:locality")
        or None
    )

    salary = job_posting.get("basesalary") or None

    posted = (
        job_posting.get("dateposted")
        or metatags.get("article:published_time")
        or None
    )

    return RawJobListing(
        source_name=_source_from_url(url),
        external_id=None,
        title=title,
        employer_name=employer,
        location_text=location,
        description_snippet=snippet,
        salary_text=salary,
        posted_text=posted,
        original_url=url,
    )


def search_jobs(
    search_terms: list[str] = None,
    city: str = None,
    max_pages_per_query: int = 2,
) -> list[RawJobListing]:
    """
    Run all configured search terms through Google Custom Search.

    For each term, pulls up to `max_pages_per_query` pages (10 results each).
    Deduplicates by URL within this run.
    """
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    # Build queries. Pairing each trade keyword with the city gives us good results.
    queries = []
    for term in search_terms:
        queries.append(f"{term} {city}")
        queries.append(f"emploi {term} {city}")

    all_jobs: dict[str, RawJobListing] = {}  # dedup by URL

    with httpx.Client() as client:
        for query in queries:
            logger.info(f"Google search: {query!r}")

            for page in range(max_pages_per_query):
                start = page * 10 + 1
                try:
                    data = _google_query(client, query, start=start)
                except Exception as e:
                    logger.error(f"Google query failed for {query!r}: {e}")
                    break

                items = data.get("items", [])
                if not items:
                    logger.info(f"  No more results for {query!r} at start={start}")
                    break

                for item in items:
                    job = _parse_search_item(item)
                    if job and job.original_url not in all_jobs:
                        all_jobs[job.original_url] = job

                time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Google search found {len(all_jobs)} unique job results.")
    return list(all_jobs.values())


# Backwards compatibility: run.py still imports `scrape_indeed`
def scrape_indeed(*args, **kwargs):
    """Legacy alias — now runs Google Custom Search across all sources."""
    return search_jobs()
