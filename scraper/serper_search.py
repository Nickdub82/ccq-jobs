"""
Indeed-direct scraper via Serper.dev's /scrape endpoint.

Serper's scrape endpoint returns the extracted TEXT of the page (not HTML).
So we parse the plain text Indeed search results to extract individual jobs.

Strategy:
    1. Hit Indeed search URLs directly (peintre ccq, peintre construction, etc.)
    2. Serper fetches and extracts text with their residential IPs (no 403)
    3. Parse the text blocks to find individual job listings
    4. Return clean RawJobListing objects for Claude classification
"""
import time
import re
import logging
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import quote_plus
import httpx
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
    """Build clean Indeed search URLs."""
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
    """Ask Serper to scrape a URL. Returns dict with 'text' and 'metadata'."""
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


# Patterns for text parsing
_SALARY_RE = re.compile(
    r"(\d+[\s,.]?\d*\s*\$\s*(?:de l'?heure|/\s*h(?:eure)?|l'?heure|par\s*heure|par\s*an|k\$|\$)|"
    r"De \d+[\s,.]?\d*\s*\$\s*à\s*\d+[\s,.]?\d*\s*\$[^\n]*|"
    r"À partir de \d+[\s,.]?\d*\s*\$[^\n]*)",
    re.IGNORECASE,
)

_LOCATION_RE = re.compile(
    r"^[A-ZÀ-ÖÙ-Ý][^\n]*?,\s*QC(?:\s*[A-Z]\d[A-Z]\s*\d[A-Z]\d)?$",
    re.MULTILINE,
)

_POSTED_RE = re.compile(
    r"(Aujourd'?hui|Publié aujourd'?hui|Hier|Il y a \d+\s*(?:heures?|jours?|semaines?|mois)|"
    r"Embauche urgente|Répond (?:souvent|généralement) en)",
    re.IGNORECASE,
)

_JOB_TYPE_RE = re.compile(
    r"(Temps plein|Temps partiel|Permanent|Temporaire|Contractuel|Sur appel|"
    r"Full-time|Part-time|Contract|Permanent)",
    re.IGNORECASE,
)

# Signals that a line is NOT a title (boilerplate from Indeed's layout)
_NOT_TITLE_SIGNALS = [
    "téléchargez", "postuler directement", "emplois ", "afficher plus",
    "trier par", "pertinence", "date", "signaler", "enregistrer",
    "déposer votre cv", "vous devez créer", "continuer pour postuler",
    "lieu", "quart de travail", "détails du poste", "avantages",
    "description complète", "extraits de la description",
    "avez-vous besoin d'aide", "à propos d'indeed", "centre d'aide",
    "espace candidat", "espace employeur", "conseils", "métiers",
    "pays", "cookies", "politique de confidentialité",
    "© 20", "www.", "http",
]


def _looks_like_title(line: str) -> bool:
    """Heuristic: is this line a job title?"""
    low = line.lower().strip()
    if len(line) < 4 or len(line) > 150:
        return False
    if any(sig in low for sig in _NOT_TITLE_SIGNALS):
        return False
    # Titles almost always have a painter-related keyword when we're searching for painters
    return True


def _parse_indeed_text(text: str, source_url: str) -> list[RawJobListing]:
    """
    Parse Indeed's search results text block to extract jobs.

    Indeed search pages (in text form) look like:

        Peintre compagnon CCQ (carte valide obligatoire)
        Techniquipe
        Greater Montreal Area, QC
        45,13 $ de l'heure
        Temps plein

        [next job...]

    We group lines into blocks (separated by blank lines) and identify jobs
    by finding blocks that have a title + location + (salary or job type).
    """
    if not text:
        return []

    # Normalize whitespace: collapse multiple blank lines but keep block structure
    lines = [l.rstrip() for l in text.splitlines()]

    # Find the "start" of the job listings (skip header/sidebar junk)
    # Heuristic: first line containing "peintre" or "painter" case-insensitive
    start_idx = 0
    for i, l in enumerate(lines):
        if re.search(r"\bpeintre|painter\b", l, re.IGNORECASE):
            start_idx = max(0, i - 1)
            break
    lines = lines[start_idx:]

    # Split into blocks on blank lines
    blocks = []
    current = []
    for l in lines:
        if l.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(l.strip())
    if current:
        blocks.append(current)

    jobs = []
    seen_titles = set()

    for block in blocks:
        if len(block) < 2:
            continue  # too small to be a real job

        # Try to find: title, employer, location, salary, job type
        title = None
        employer = None
        location = None
        salary = None
        job_type = None
        posted = None
        description_lines = []

        for idx, line in enumerate(block):
            # First valid line is usually the title
            if title is None and _looks_like_title(line):
                title = line
                continue

            # Location line (ends with QC optionally + postal code)
            if location is None and _LOCATION_RE.match(line):
                location = line
                continue

            # Salary
            if salary is None:
                m = _SALARY_RE.search(line)
                if m:
                    salary = m.group(0)
                    continue

            # Job type
            if job_type is None:
                m = _JOB_TYPE_RE.search(line)
                if m:
                    job_type = m.group(0)
                    continue

            # Posted date
            if posted is None:
                m = _POSTED_RE.search(line)
                if m:
                    posted = m.group(0)
                    continue

            # Second non-title line (before location) is usually the employer
            if title and employer is None and location is None:
                # Skip lines that look like Indeed boilerplate
                low = line.lower()
                if not any(sig in low for sig in _NOT_TITLE_SIGNALS):
                    employer = line
                    continue

            description_lines.append(line)

        # A block is a real job only if we got at least a title and a location
        if not title or not location:
            continue

        # Dedup by title within this text
        dedup_key = (title.lower(), (employer or "").lower())
        if dedup_key in seen_titles:
            continue
        seen_titles.add(dedup_key)

        description = " ".join(description_lines) if description_lines else None

        jobs.append(RawJobListing(
            source_name="indeed",
            external_id=None,  # We can't get job_id from text — Claude will dedupe via fingerprint
            title=title,
            employer_name=employer,
            location_text=location,
            description_snippet=description,
            salary_text=salary,
            posted_text=posted,
            original_url=source_url,  # URL of the SEARCH page; individual links aren't in text
        ))

    return jobs


def search_jobs(
    search_terms: list[str] = None,
    city: str = None,
    radius_km: int = 60,
) -> list[RawJobListing]:
    """Scrape Indeed search pages via Serper, parse text results, return jobs."""
    search_terms = search_terms or settings.search_terms_list
    city = city or settings.scraper_target_city

    urls = _build_indeed_urls(search_terms, city, radius_km=radius_km)

    all_jobs: dict[str, RawJobListing] = {}  # dedup by (title, employer)

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

            text = data.get("text") or ""
            html = data.get("html") or data.get("content") or ""
            # Prefer text (that's what Serper returns); fall back to HTML if somehow present
            content = text or html

            if not content:
                keys = list(data.keys())
                logger.warning(f"  No content returned. Response keys: {keys}")
                continue

            logger.info(f"  Received {len(content)} chars from Serper")
            jobs = _parse_indeed_text(content, source_url=url)
            logger.info(f"  Parsed {len(jobs)} jobs from this page")

            for job in jobs:
                key = f"{job.title.lower()}|{(job.employer_name or '').lower()}|{(job.location_text or '').lower()}"
                if key not in all_jobs:
                    all_jobs[key] = job

            time.sleep(REQUEST_DELAY_SEC)

    logger.info(f"Indeed scraper kept {len(all_jobs)} unique jobs.")
    return list(all_jobs.values())


# Backwards compatibility with run.py
def scrape_indeed(*args, **kwargs):
    return search_jobs()
