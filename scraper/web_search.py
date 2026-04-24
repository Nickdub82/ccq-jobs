"""
Volet 2 — Web search for off-board CCQ job postings.

v2 adds PRESCREENING: before spending Claude tokens on a page, we do a
cheap keyword check. If the page doesn't even mention painting + a CCQ
signal, we skip it. Saves ~40-60% of Claude calls on web pages that are
company landing pages, blog posts, irrelevant results, etc.

Strategy:
    1. Run surgical Serper queries (explicit CCQ keywords, exclude job boards)
    2. Filter out listing/search URLs by pattern
    3. Fetch each candidate page
    4. PRESCREEN: does the page even mention painting + CCQ/construction?
    5. If yes -> pass to Claude extractor
    6. If no -> skip, log, move on
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
REQUEST_TIMEOUT = 30

EXCLUDED_SITES = [
    "indeed.com",
    "indeed.ca",
    "jobillico.com",
    "jobboom.com",
    "glassdoor.com",
    "glassdoor.ca",
    "linkedin.com",
    "monster.com",
    "monster.ca",
]

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
    """Made to look like a RawEmail so it plugs into email_parser.extract_jobs_from_email()."""
    message_id: str
    sender: str
    subject: str
    received_date: str
    body_text: str
    body_html: str


# ============================================================
# SERPER SEARCH
# ============================================================

def _build_query(base_query: str) -> str:
    exclusions = " ".join(f"-site:{site}" for site in EXCLUDED_SITES)
    return f"{base_query} {exclusions}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20))
def _serper_search(client: httpx.Client, query: str, num: int = 10) -> list[dict]:
    headers = {
        "X-API-KEY": settings.serper_api_key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": num, "gl": "ca", "hl": "fr"}
    resp = client.post(SERPER_SEARCH_ENDPOINT, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("organic", [])


# ============================================================
# URL + CONTENT FILTERING
# ============================================================

BAD_URL_PATTERNS = [
    r"/search[?/]",
    r"/jobs\?",
    r"/recherche[?/]",
    r"/emplois\?",
    r"/category[?/]",
    r"/listing[?/]",
    r"/q-",
    r"\.pdf$",
    r"/tag/",
    r"/categorie/",
]


def _looks_like_listing_url(url: str) -> bool:
    low = url.lower()
    return any(re.search(p, low) for p in BAD_URL_PATTERNS)


# Prescreen: painting keywords (French + English)
_PAINTING_KEYWORDS = [
    "peintre", "peinture", "painter", "painting",
]

# Prescreen: CCQ/construction signals
_CCQ_KEYWORDS = [
    "ccq", "carte ccq", "cartes ccq", "cartes requises",
    "décret", "r-20", "r20",
    "convention", "convention collective",
    "construction", "chantier",
    "compagnon", "apprenti",
    "industriel", "commercial", "institutionnel",
    "en bâtiment",
]


def _prescreen_page(text: str, html: str, url: str) -> tuple[bool, str]:
    """
    Cheap keyword check to decide if a page is worth sending to Claude.
    Returns (should_pass, reason).
    """
    # Normalize: lowercase on the text (HTML fallback if text empty)
    content = (text or html or "").lower()

    if len(content) < 500:
        return False, f"too short ({len(content)} chars)"

    has_painter = any(kw in content for kw in _PAINTING_KEYWORDS)
    if not has_painter:
        return False, "no painting keyword"

    has_ccq_signal = any(kw in content for kw in _CCQ_KEYWORDS)
    if not has_ccq_signal:
        return False, "no CCQ/construction signal"

    return True, "passed"


# ============================================================
# PAGE FETCH
# ============================================================

def _fetch_page_text(client: httpx.Client, url: str) -> tuple[str, str]:
    """Fetch a page, return (plaintext, html)."""
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

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text, html
    except Exception as e:
        logger.warning(f"Failed to fetch {url[:80]}: {e}")
        return "", ""


# ============================================================
# PUBLIC API
# ============================================================

def find_ccq_job_pages(max_results_per_query: int = 10) -> list[PageContent]:
    """
    Run surgical CCQ queries, fetch pages, prescreen, return only pages
    worth sending to Claude.
    """
    if not settings.serper_api_key:
        logger.warning("SERPER_API_KEY not set, skipping web search volet.")
        return []

    seen_urls: set[str] = set()
    pages: list[PageContent] = []

    with httpx.Client() as client:
        # Step 1: Collect URLs from all queries
        all_candidates: list[tuple[str, str, str]] = []  # (url, title, query)

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
                if not url or url in seen_urls:
                    continue
                if _looks_like_listing_url(url):
                    logger.info(f"  URL-skip (listing pattern): {url[:80]}")
                    continue
                seen_urls.add(url)
                all_candidates.append((url, title, query))

        logger.info(f"Total unique candidate URLs: {len(all_candidates)}")

        # Step 2: Fetch + prescreen each page
        MAX_PAGES_PER_RUN = 25
        prescreen_pass = 0
        prescreen_fail = 0

        for i, (url, title, query) in enumerate(all_candidates[:MAX_PAGES_PER_RUN]):
            logger.info(f"Fetching {i+1}/{min(len(all_candidates), MAX_PAGES_PER_RUN)}: {url[:80]}")
            text, html = _fetch_page_text(client, url)

            if not text and not html:
                continue

            # PRESCREEN before sending to Claude
            passed, reason = _prescreen_page(text, html, url)
            if not passed:
                logger.info(f"  Prescreen-skip: {reason}")
                prescreen_fail += 1
                continue

            prescreen_pass += 1
            domain = urlparse(url).netloc

            pages.append(PageContent(
                message_id=url,
                sender=f"web-search <{domain}>",
                subject=f"[{query}] {title}",
                received_date=datetime.now(timezone.utc).isoformat(),
                body_text=text,
                body_html=html,
            ))

        logger.info(
            f"Prescreen results: {prescreen_pass} passed, {prescreen_fail} skipped "
            f"(saved ~{prescreen_fail} Claude calls)"
        )

    logger.info(f"Volet 2 fetched {len(pages)} pages ready for Claude extraction.")
    return pages
