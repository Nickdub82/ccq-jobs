"""
Volet 2 — Web search for off-board CCQ job postings.

This complements the Gmail volet by discovering jobs that live on:
    - Employer career pages (Pomerleau, EBC, small contractors...)
    - Union job boards
    - Local news/community sites
    - Anywhere on the indexed web that's NOT a big job board

Strategy:
    1. Run surgical Serper queries with explicit CCQ keywords
       AND exclude the job boards we already cover via Gmail
    2. For each result URL, fetch the page
    3. Wrap the page content in a RawEmail-like object
    4. Pass to the existing email_parser for Claude extraction
       (the parser is generic -- it works for any content)

This way we reuse 100% of the CCQ classification logic.
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
REQUEST_TIMEOUT = 30

# Sites we already cover via Gmail alerts -- exclude them from web search
# to avoid duplicating work and crediting Serper quota for nothing
EXCLUDED_SITES = [
    "indeed.com",
    "indeed.ca",
    "jobillico.com",
    "jobboom.com",
    "glassdoor.com",
    "glassdoor.ca",
    "linkedin.com",  # typically needs auth, not useful
    "monster.com",
    "monster.ca",
]

# Surgical queries that target explicit CCQ signals
# Each query aims to surface jobs posted DIRECTLY by employers (not aggregators)
CCQ_QUERIES = [
    '"carte CCQ" peintre Québec',
    '"cartes CCQ" peintre emploi',
    '"décret construction" peintre emploi Québec',
    '"selon la convention CCQ" peintre',
    '"compétence CCQ" peintre emploi',
    '"peintre en bâtiment" "R-20"',
]


@dataclass
class PageContent:
    """
    A web page content made to look like a RawEmail so it plugs into
    email_parser.extract_jobs_from_email() without modification.
    """
    message_id: str      # used as page identifier (we put the URL here)
    sender: str          # we put the page domain here
    subject: str         # page title / query that found it
    received_date: str   # when we found it
    body_text: str       # extracted plain text
    body_html: str       # raw HTML


# ============================================================
# SERPER SEARCH
# ============================================================

def _build_query(base_query: str) -> str:
    """Add site exclusions to a base CCQ query."""
    exclusions = " ".join(f"-site:{site}" for site in EXCLUDED_SITES)
    return f"{base_query} {exclusions}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_search(client: httpx.Client, query: str, num: int = 10) -> list[dict]:
    """Call Serper's /search endpoint and return organic results."""
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "num": num,
        "gl": "ca",   # Canada
        "hl": "fr",   # French
    }
    resp = client.post(SERPER_SEARCH_ENDPOINT, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("organic", [])


# ============================================================
# PAGE FETCH
# ============================================================

# URL patterns that clearly are NOT individual job postings
# (listing pages, search pages, category pages)
BAD_URL_PATTERNS = [
    r"/search[?/]",
    r"/jobs\?",
    r"/recherche[?/]",
    r"/emplois\?",
    r"/category[?/]",
    r"/listing[?/]",
    r"/q-",          # Indeed-style query params even on other sites
    r"\.pdf$",       # avoid PDFs for now
    r"/tag/",
    r"/categorie/",
]


def _looks_like_listing_url(url: str) -> bool:
    """Return True if URL clearly looks like a listing/search page, not a job offer."""
    low = url.lower()
    return any(re.search(p, low) for p in BAD_URL_PATTERNS)


def _fetch_page_text(client: httpx.Client, url: str) -> tuple[str, str]:
    """
    Fetch a page and return (plaintext, html).

    Uses a real-looking User-Agent to minimize blocks on smaller sites.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8",
    }
    try:
        resp = client.get(url, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        # Quick text extraction via BeautifulSoup (the parser already imports it)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Strip script/style tags before text extraction
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text("\n", strip=True)
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text, html
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return "", ""


# ============================================================
# PUBLIC API
# ============================================================

def find_ccq_job_pages(max_results_per_query: int = 10) -> list[PageContent]:
    """
    Main entry point. Run all surgical CCQ queries, fetch the pages,
    and return them as PageContent objects ready for email_parser.

    Deduplicates URLs across queries.
    """
    if not settings.serper_api_key:
        logger.warning("SERPER_API_KEY not set, skipping web search volet.")
        return []

    from datetime import datetime, timezone

    seen_urls: set[str] = set()
    pages: list[PageContent] = []

    with httpx.Client() as client:
        # Step 1: Collect URLs from all queries
        all_candidates: list[tuple[str, str, str]] = []  # (url, title, triggering_query)

        for query in CCQ_QUERIES:
            full_query = _build_query(query)
            logger.info(f"Serper search: {query}")

            try:
                results = _serper_search(client, full_query, num=max_results_per_query)
            except Exception as e:
                logger.error(f"Serper query failed for '{query}': {e}")
                continue

            logger.info(f"  Got {len(results)} results.")
            for r in results:
                url = r.get("link")
                title = r.get("title", "")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                if _looks_like_listing_url(url):
                    logger.info(f"  Skipping listing URL: {url[:80]}")
                    continue
                seen_urls.add(url)
                all_candidates.append((url, title, query))

        logger.info(f"Total unique candidate URLs: {len(all_candidates)}")

        # Step 2: Fetch each page (with a cap to avoid runaway)
        MAX_PAGES_PER_RUN = 20
        for i, (url, title, query) in enumerate(all_candidates[:MAX_PAGES_PER_RUN]):
            logger.info(f"Fetching page {i+1}/{min(len(all_candidates), MAX_PAGES_PER_RUN)}: {url[:80]}")
            text, html = _fetch_page_text(client, url)

            if not text and not html:
                continue

            # Skip pages that are too short to contain a real job posting
            if len(text) < 200:
                logger.info(f"  Skipping (too short: {len(text)} chars)")
                continue

            domain = urlparse(url).netloc

            pages.append(PageContent(
                message_id=url,  # use URL as unique ID
                sender=f"web-search <{domain}>",
                subject=f"[{query}] {title}",
                received_date=datetime.now(timezone.utc).isoformat(),
                body_text=text,
                body_html=html,
            ))

    logger.info(f"Volet 2 fetched {len(pages)} pages ready for Claude extraction.")
    return pages
