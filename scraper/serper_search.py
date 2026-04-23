"""
Serper.dev Google Jobs API scraper.

Uses Serper's /jobs endpoint (Google Jobs) which returns INDIVIDUAL job
postings instead of generic web pages. This fixes the "25+ offres Peintre CCQ"
listing pages problem we had with the regular /search endpoint.

Free tier: 2500 searches included at signup. Paid: $50/mo for 50k searches.
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_JOBS_ENDPOINT = "https://google.serper.dev/jobs"
REQUEST_DELAY_SEC = 1.0


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


def _source_from_url_and_via(url: str, via: Optional[str] = None) -> str:
    u = (url or "").lower()
    v = (via or "").lower()
    combined = f"{u} {v}"
    if "jobboom" in combined:
        return "jobboom"
    if "jobillico" in combined:
        return "jobillico"
    if "indeed" in combined:
        return "indeed"
    if "guichetemplois" in combined or "jobbank" in combined:
        return "guichetemplois"
    if "facebook" in combined:
        return "facebook"
    if "linkedin" in combined:
        return "linkedin"
    if "glassdoor" in combined:
        return "glassdoor"
    return "indeed"  # fallback (already seeded in DB)


def _is_listing_or_garbage_url(url: str) -> bool:
    """Filter out aggregator listing URLs that somehow slip through."""
    if not url:
        return True
    u = url.lower()
    bad_patterns = [
        "/jobs?",
        "/recherche",
        "/search?",
        "?q=",
        "/listings",
        "/emploi-offres",
        "/browse",
    ]
    return any(p in u for p in bad_patterns)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_jobs_query(client: httpx.Client, query: str, page: int = 1) -> dict:
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "gl": "ca",
        "hl": "fr",
        "location": "Montreal, Quebec, Canada",
        "page": page,
    }
    resp = client.post(SERPER_JOBS_ENDPOINT, json=payload, headers=headers, timeout=30)
    if resp.status_code == 429:
        logger.warning("Serper rate limit hit — backing off")
        raise httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _parse_job_item(item: dict) -> Optional[RawJobListing]:
    """Convert a Serper Jobs result into a RawJobListing."""
    # Pick the best URL available
    url = item.get("share_link") or item.get("link") or ""
    if not url:
        apply = item.get("apply_link")
        if isinstance(apply, list) and apply:
            first = apply[0]
            if isinstance(first, dict):
                url = first.get("link", "") or ""
            elif isinstance(first, str):
                url = first
        elif isinstance(apply, str):
            url = apply

    if not url or _is_listing_or_garbage_url(url):
        return None

    title = (item.get("title") or "").strip()
    if not title:
        return None

    company = (item.get("company_name") or item.get("company") or "").strip() or None
    location = (item.get("location") or "").strip() or None
    via = item.get("via") or None
    description = (item.get("description") or item.get("snippet") or "").strip()

    extensions = item.get("detected_extensions") or {}
    posted = extensions.get("posted_at") or item.get("posted_at")
    salary = extensions.get("salary") or item.get("salary")
    schedule = extensions.get("schedule_type")

    if schedule and schedule.lower() not in (description or "").lower():
        description = f"{schedule}. {description}".strip()

    return RawJobListing(
        source_name=_source_from_url_and_via(url, via),
        external_id=item.get("job_id") or None,
        title=title,
        employer_name=company,
        location_text=location,
        description_snippet=description[:1000] if description else None,
        salary_text=salary,
        posted_text=posted,
        original_url=url,
    )


def search_jobs(
    search_terms: list[str] = None,
    city: str = None,
    max_pages_per_query: int = 2,
) -> list[RawJobListing]:
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    # Google Jobs scopes by location via the payload, so the query stays clean
    queries = list(search_terms)

    all_jobs: dict[str, RawJobListing] = {}

    if not settings.serper_api_key:
        logger.error("SERPER_API_KEY is not set — cannot run Serper scraper.")
        return []

    with httpx.Client() as client:
        for query in queries:
            logger.info(f"Serper Jobs search: {query!r}")

            for page in range(1, max_pages_per_query + 1):
                try:
                    data = _serper_jobs_query(client, query, page=page)
                except Exception as e:
                    logger.error(f"Serper query failed for {query!r} page={page}: {e}")
                    break

                jobs = data.get("jobs") or []
                if not jobs:
                    logger.info(f"  No more jobs for {query!r} at page={page}")
                    break

                kept = 0
                for item in jobs:
                    job = _parse_job_item(item)
                    if job and job.original_url not in all_jobs:
                        all_jobs[job.original_url] = job
                        kept += 1

                logger.info(f"  Page {page}: {len(jobs)} jobs returned, {kept} kept after filtering")
                time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Serper Jobs scraper found {len(all_jobs)} unique individual listings.")
    return list(all_jobs.values())


# Backwards compatibility
def scrape_indeed(*args, **kwargs):
    """Legacy alias — now runs Serper Google Jobs."""
    return search_jobs()
