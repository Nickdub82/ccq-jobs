"""
Serper.dev Google Search scraper — uses the REAL /search endpoint.

Serper does NOT have a /jobs endpoint (that's SerpAPI, a different service).
Instead, we craft Google queries that target individual job posting URLs on
known job boards, and we aggressively filter out listing / aggregator pages
in post-processing.

Together with the hardened Claude prompt (which also rejects listings), this
gives us a clean pipeline without paying for SerpAPI.
"""
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
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


def _source_from_url(url: str) -> str:
    u = (url or "").lower()
    if "jobboom" in u:
        return "jobboom"
    if "jobillico" in u:
        return "jobillico"
    if "indeed" in u:
        return "indeed"
    if "guichetemplois" in u or "jobbank" in u:
        return "guichetemplois"
    if "facebook" in u:
        return "facebook"
    if "linkedin" in u:
        return "linkedin"
    if "glassdoor" in u:
        return "glassdoor"
    return "indeed"  # sensible fallback


# Patterns that mark a URL as a listing/aggregator page (not an individual job)
_BAD_URL_PATTERNS = [
    "/jobs?",            # indeed.com/jobs?q=... (search page)
    "/jobs/q-",          # indeed variant
    "/recherche",        # jobboom.com/recherche...
    "/search?",
    "?q=",
    "&q=",
    "/listings",
    "/emploi-offres",    # jobillico generic listing
    "/browse",
    "/jobs-in-",         # linkedin/glassdoor browse pages
]

# URL patterns that indicate a real individual job posting
_GOOD_URL_PATTERNS = [
    "/viewjob",             # indeed individual job
    "/rc/clk",              # indeed click tracker → individual
    "/offre-emploi",        # jobboom/jobillico individual offer
    "/fr/emplois/",         # jobboom individual
    "/emploi/",             # jobillico individual
    "/jobposting/",
    "/jobsearch.jobview",   # guichet emplois individual
]


def _classify_url(url: str) -> str:
    """Return 'individual', 'listing', or 'unknown'."""
    if not url:
        return "listing"
    u = url.lower()
    if any(p in u for p in _BAD_URL_PATTERNS):
        return "listing"
    if any(p in u for p in _GOOD_URL_PATTERNS):
        return "individual"
    return "unknown"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_search(client: httpx.Client, query: str, page: int = 1) -> dict:
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "gl": "ca",
        "hl": "fr",
        "num": 10,
        "page": page,
    }
    resp = client.post(SERPER_SEARCH_ENDPOINT, json=payload, headers=headers, timeout=30)
    if resp.status_code == 429:
        logger.warning("Serper rate limit hit — backing off")
        raise httpx.HTTPStatusError("rate limit", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp.json()


def _parse_search_item(item: dict) -> Optional[RawJobListing]:
    """Convert a Serper organic result into a RawJobListing."""
    url = item.get("link") or ""
    if not url:
        return None

    # Reject listing pages upfront (before spending a Claude call on them)
    classification = _classify_url(url)
    if classification == "listing":
        return None

    title = (item.get("title") or "").strip()
    if not title:
        return None

    # Reject titles that scream "listing page"
    title_lower = title.lower()
    bad_title_patterns = [
        "25+ offres",
        "100+ offres",
        "500+ offres",
        "900+ offres",
        "1000+ offres",
        "offres d'emploi |",
        "consultez nos",
        "jobs in montreal",
        "painter jobs",
        "peintre jobs",
    ]
    if any(p in title_lower for p in bad_title_patterns):
        return None

    snippet = (item.get("snippet") or "").strip()

    # Pull richer metadata if Google returned it
    rich = item.get("richSnippet") or {}
    top = rich.get("top") or {}
    detected = top.get("detectedExtensions") or {}

    employer = detected.get("company") or None
    location = detected.get("address") or detected.get("location") or None
    posted = detected.get("postedat") or detected.get("postedtime") or None

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
    Query Serper /search with Google queries targeted at individual job pages.

    Uses `inurl:` and `site:` operators to nudge Google toward individual
    job posting URLs instead of category listings.
    """
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    queries = []
    for term in search_terms:
        # Generic query — let Google surface anything
        queries.append(f"{term} {city}")
        # Indeed individual job pages
        queries.append(f'"{term}" {city} site:ca.indeed.com inurl:viewjob')
        # Jobboom individual job pages
        queries.append(f'"{term}" {city} site:jobboom.com inurl:offre-emploi')
        # Jobillico individual job pages
        queries.append(f'"{term}" {city} site:jobillico.com inurl:emploi')

    all_jobs: dict[str, RawJobListing] = {}

    if not settings.serper_api_key:
        logger.error("SERPER_API_KEY is not set — cannot run Serper scraper.")
        return []

    with httpx.Client() as client:
        for query in queries:
            logger.info(f"Serper search: {query!r}")

            for page in range(1, max_pages_per_query + 1):
                try:
                    data = _serper_search(client, query, page=page)
                except Exception as e:
                    logger.error(f"Serper query failed for {query!r} page={page}: {e}")
                    break

                items = data.get("organic") or []
                if not items:
                    logger.info(f"  No more results for {query!r} at page={page}")
                    break

                kept = 0
                rejected = 0
                for item in items:
                    job = _parse_search_item(item)
                    if job is None:
                        rejected += 1
                        continue
                    if job.original_url not in all_jobs:
                        all_jobs[job.original_url] = job
                        kept += 1

                logger.info(
                    f"  Page {page}: {len(items)} results, {kept} kept, {rejected} rejected (listings)"
                )
                time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Serper scraper kept {len(all_jobs)} individual job listings.")
    return list(all_jobs.values())


# Backwards compatibility
def scrape_indeed(*args, **kwargs):
    return search_jobs()
