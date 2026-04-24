"""
Volet 2 — Web search for off-board CCQ painter job postings.

v4: Expanded EXCLUDED_SITES to filter out aggregators beyond just Indeed.
    Added: talent.com, ziprecruiter.com, chantieremploi.com, neuvoo, jooble, etc.
    These ramened du bruit multi-trades that the prompt now filters anyway,
    but blocking them at search time saves Claude tokens and cleans results.
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

# Sites to exclude from Serper queries:
# 1. Big job boards we already cover via Gmail
# 2. Multi-trade aggregators that return too much noise
# 3. Social platforms that don't work for scraping
EXCLUDED_SITES = [
    # Major job boards covered by Gmail alerts
    "indeed.com", "indeed.ca",
    "jobillico.com", "jobboom.com",
    "glassdoor.com", "glassdoor.ca",

    # Multi-trade aggregators (too much non-painter noise)
    "talent.com", "ca.talent.com",
    "ziprecruiter.com",
    "neuvoo.ca", "neuvoo.com",
    "jooble.org",
    "chantieremploi.com",
    "monster.com", "monster.ca",
    "simplyhired.com", "simplyhired.ca",
    "workopolis.com",
    "careerbuilder.com",

    # Social / media that don't yield good results
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "tiktok.com",

    # Ad/marketing sites that clutter results
    "pinterest.com",
]

# Surgical queries aimed at painter employer career pages
CCQ_QUERIES = [
    '"carte CCQ" peintre Québec',
    '"cartes CCQ" peintre emploi',
    'peintre "décret construction" emploi Québec',
    '"selon la convention CCQ" peintre',
    '"compétence CCQ" peintre emploi',
    '"peintre en bâtiment" "R-20"',
    '"peintre compagnon" emploi Québec chantier',
    '"apprenti peintre" CCQ emploi',
]


@dataclass
class PageContent:
    message_id: str
    sender: str
    subject: str
    received_date: str
    body_text: str
    body_html: str


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


BAD_URL_PATTERNS = [
    r"/search[?/]", r"/jobs\?", r"/recherche[?/]", r"/emplois\?",
    r"/category[?/]", r"/listing[?/]", r"/q-", r"\.pdf$",
    r"/tag/", r"/categorie/",
]


def _looks_like_listing_url(url: str) -> bool:
    low = url.lower()
    return any(re.search(p, low) for p in BAD_URL_PATTERNS)


_PAINTING_KEYWORDS = ["peintre", "peinture", "painter", "painting"]

_CCQ_KEYWORDS = [
    "ccq", "carte ccq", "cartes ccq", "cartes requises",
    "décret", "r-20", "r20",
    "convention", "convention collective",
    "construction", "chantier",
    "compagnon", "apprenti",
    "industriel", "commercial", "institutionnel",
    "en bâtiment",
]


def _prescreen_page(text: str, html: str) -> tuple[bool, str]:
    content = (text or html or "").lower()
    if len(content) < 500:
        return False, f"too short ({len(content)} chars)"
    if not any(kw in content for kw in _PAINTING_KEYWORDS):
        return False, "no painting keyword"
    if not any(kw in content for kw in _CCQ_KEYWORDS):
        return False, "no CCQ/construction signal"
    return True, "passed"


def _fetch_page_text(client: httpx.Client, url: str) -> tuple[str, str]:
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


def find_ccq_job_pages(
    max_results_per_query: int = 10,
    skip_urls: Optional[set[str]] = None,
) -> list[PageContent]:
    if not settings.serper_api_key:
        logger.warning("SERPER_API_KEY not set, skipping Volet 2.")
        return []

    skip_urls = skip_urls or set()
    seen_urls: set[str] = set()
    pages: list[PageContent] = []

    with httpx.Client() as client:
        all_candidates: list[tuple[str, str, str]] = []
        cache_hit_count = 0

        for query in CCQ_QUERIES:
            full_query = _build_query(query)
            logger.info(f"Serper search: {query}")

            try:
                results = _serper_search(client, full_query, num=max_results_per_query)
            except Exception as e:
                logger.error(f"Serper query failed: {e}")
                continue

            logger.info(f"  Got {len(results)} results.")
            for r in results:
                url = r.get("link")
                title = r.get("title", "")
                if not url or url in seen_urls:
                    continue
                if url in skip_urls:
                    cache_hit_count += 1
                    continue
                if _looks_like_listing_url(url):
                    continue
                seen_urls.add(url)
                all_candidates.append((url, title, query))

        logger.info(
            f"Candidates: {len(all_candidates)} new URLs, "
            f"{cache_hit_count} cache hits (already processed)"
        )

        MAX_PAGES_PER_RUN = 25
        prescreen_pass = 0
        prescreen_fail = 0

        for i, (url, title, query) in enumerate(all_candidates[:MAX_PAGES_PER_RUN]):
            logger.info(f"Fetching {i+1}/{min(len(all_candidates), MAX_PAGES_PER_RUN)}: {url[:80]}")
            text, html = _fetch_page_text(client, url)

            if not text and not html:
                continue

            passed, reason = _prescreen_page(text, html)
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
            f"Prescreen: {prescreen_pass} passed, {prescreen_fail} skipped "
            f"(saved ~{prescreen_fail} Claude calls)"
        )

    logger.info(f"Volet 2 returning {len(pages)} pages for Claude extraction.")
    return pages
