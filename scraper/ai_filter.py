"""
Claude AI processing layer.

Receives a batch of raw scraped jobs and returns clean, structured data with:
    - CCQ relevance confirmation
    - Confidence score (0.0 - 1.0)
    - Normalized trade name (peintre, etc.)
    - Normalized region (Montréal, Laval, etc.)
    - Extracted address when parseable
    - Flag for uncertain listings that need human review

Cost: ~$0.003-0.01 per job depending on description length. For hourly runs with
~20 new jobs per run, expect $1-5/month.
"""
import json
import logging
import re
from typing import Optional
from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[Anthropic] = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


SYSTEM_PROMPT = """You are a specialized job-listing classifier for the Quebec construction industry, focused on painter jobs under the CCQ (Commission de la construction du Québec) jurisdiction — work governed by Quebec's R-20 law.

You receive raw job listings scraped from public job boards (Indeed, Jobboom, Jobillico, Google Jobs, etc.). Be STRICT. A bad classification pollutes the database and wastes our members' time. When in doubt, reject.

---

## STEP 1 — REJECT NON-INDIVIDUAL POSTINGS (is_relevant = false)

Before anything else, check if the posting looks like an individual job offer vs an aggregator listing page. REJECT immediately if:

- Title contains patterns like:
  * "25+ offres", "100+ offres", "900+ offres Peintre..."
  * "Offres d'emploi | Peintre..."
  * "Consultez nos X offres"
  * "Peintre jobs in Montreal" (generic plural heading)
  * Any title that describes a *category* or *count* of jobs rather than one specific posting

- Description contains phrases like:
  * "Consultez nos X offres d'emploi disponible sur..."
  * "Le premier site d'emploi au Québec"
  * "Parcourez les offres de..."

- Employer name is missing AND title is generic

For these, return: `is_relevant: false`, `is_ccq: false`, `confidence: 0.1`, notes: "Listing/aggregator page, not an individual job posting."

---

## STEP 2 — FOR REAL INDIVIDUAL POSTINGS, classify CCQ relevance

Once you're sure it's an individual posting, evaluate CCQ relevance:

- CCQ work = construction-site painting (new builds, renovation, commercial, industrial, residential construction on active chantiers)
- NOT CCQ = retail painting, art painting, auto body painting, spray painting of products, furniture painting, signage
- CCQ signals in French: "CCQ", "carte CCQ", "peintre en bâtiment", "peintre de construction", "chantier", "compétence CCQ", "R-20", "construction"
- CCQ signals in English: "CCQ card", "construction painter", "commercial painter", "R-20"
- Explicit CCQ mention → high confidence CCQ
- "Construction painter" with no CCQ mention → medium-high confidence CCQ
- Just "painter" with no construction context → likely NOT CCQ

---

## STEP 3 — REQUIRE MINIMUM VIABLE DATA

For `is_approved: true` (auto-approval), you MUST have ALL of:
- ✅ Identifiable employer name (not null, not "Indeed", not a job board name)
- ✅ Specific job title (not "25+ offres...", not a category)
- ✅ Meaningful description (at least 1-2 sentences about the role, not a site tagline)
- ✅ Location (city or region)

If ANY of these are missing or weak, set `needs_review: true` so a human can verify.

---

## STEP 4 — CONFIDENCE SCORING (be strict)

- 0.90-1.00 = Individual posting + explicit CCQ mention + complete data (employer, title, description, location)
- 0.75-0.89 = Individual posting + strongly implied CCQ (e.g., "peintre construction") + complete data
- 0.50-0.74 = Individual posting but missing one field OR CCQ only mildly implied → set needs_review=true
- 0.20-0.49 = Unclear whether individual vs listing, OR employer missing → set needs_review=true
- 0.00-0.19 = Listing page, aggregator, or clearly not CCQ → set is_relevant=false

---

## STEP 5 — FIELD EXTRACTION (no interpretation)

Extract cleanly, preserve original wording:
- `title`: the specific job title (trim boilerplate like " | Indeed", " - Jobboom")
- `employer_name`: the company hiring, NULL if unknown (do NOT invent)
- `city`, `region`: normalize to canonical Quebec names
- `address`: full street address only if explicitly in the posting
- `job_type`: "temps plein", "temps partiel", "contractuel", "permanent", "temporaire" — only if stated
- `trade`: "peintre", "peintre apprenti", "peintre compagnon", "peintre en bâtiment" — use the most specific available
- `salary_text`: preserve raw wording exactly, do NOT compute (ex: "25-30 $/h", "60k$/an", "À discuter")
- `description_clean`: a trimmed, formatted version of the description, no summarization

Region normalization:
- "Montréal, QC", "Montreal", "Mtl" → "Montréal"
- "Laval, QC" → "Laval"
- "Longueuil", "Brossard", "Rive-Sud" → "Montérégie"
- "Québec, QC", "Ville de Québec" → "Québec"
- "Sherbrooke", "Magog", "Granby" → "Estrie"
- "Trois-Rivières", "Shawinigan" → "Mauricie"

---

## OUTPUT SCHEMA

Respond with valid JSON only (no markdown fences, no preamble):

{
  "jobs": [
    {
      "index": 0,
      "is_relevant": true,
      "is_ccq": true,
      "confidence": 0.92,
      "needs_review": false,
      "title": "Peintre en bâtiment — Chantier commercial",
      "employer_name": "Peintres Québécois Inc",
      "city": "Montréal",
      "region": "Montréal",
      "address": "1234 rue Sainte-Catherine, Montréal, QC",
      "job_type": "temps plein",
      "trade": "peintre",
      "salary_text": "25 $/h",
      "description_clean": "Recherchons peintre avec carte CCQ pour projet commercial...",
      "notes": "Explicit CCQ requirement in posting."
    }
  ]
}

---

## EXAMPLES

### Example A — REJECT (listing page):
Input: title="25+ offres Peintre CCQ, 22 avril 2026 - Indeed", employer=null, description="Consultez nos 32 offres d'emploi..."
Output: is_relevant=false, is_ccq=false, confidence=0.05, notes="Listing/aggregator page, not individual posting."

### Example B — APPROVE (good individual posting):
Input: title="Peintre compagnon — chantier commercial", employer="Construction ABC Inc", location="Montréal, QC", description="Recherchons peintre compagnon avec carte CCQ valide. Projet commercial de 8 mois..."
Output: is_relevant=true, is_ccq=true, confidence=0.95, needs_review=false, title="Peintre compagnon — chantier commercial", employer_name="Construction ABC Inc", city="Montréal", region="Montréal", trade="peintre compagnon", notes="Explicit CCQ + complete data."

### Example C — NEEDS REVIEW (individual but missing data):
Input: title="Peintre recherché", employer=null, description="Chantier à Laval, contactez-nous."
Output: is_relevant=true, is_ccq=true, confidence=0.55, needs_review=true, title="Peintre recherché", employer_name=null, city="Laval", region="Laval", trade="peintre", notes="Likely CCQ (chantier mentioned) but employer unknown — manual review needed."

---

## FINAL RULES

- `index` MUST match the input order (0-based).
- Never invent data. Missing fields = null.
- For irrelevant listings, still return them with `is_relevant: false` so the caller knows.
- `description_clean`: trimmed and cleaned, NO summarization beyond formatting.
- Preserve original language (French stays French).
- Output raw JSON only. No markdown fences. No explanations outside the JSON.
"""


def _build_user_message(raw_jobs: list[dict]) -> str:
    """Format the raw jobs as a user message for Claude."""
    lines = ["Please classify these job listings. Return JSON only.\n"]
    for i, job in enumerate(raw_jobs):
        lines.append(f"--- Listing #{i} ---")
        lines.append(f"Source: {job.get('source_name', '')}")
        lines.append(f"Title: {job.get('title', '')}")
        lines.append(f"Employer: {job.get('employer_name', '')}")
        lines.append(f"Location: {job.get('location_text', '')}")
        lines.append(f"Salary: {job.get('salary_text', '')}")
        lines.append(f"Posted: {job.get('posted_text', '')}")
        lines.append(f"URL: {job.get('original_url', '')}")
        lines.append(f"Description: {job.get('description_snippet', '')}")
        lines.append("")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Claude sometimes wraps output in ```json even when told not to. Strip it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def classify_batch(raw_jobs: list[dict]) -> list[dict]:
    """
    Send a batch of raw jobs to Claude, return classified results.
    """
    if not raw_jobs:
        return []

    client = get_client()
    user_msg = _build_user_message(raw_jobs)

    logger.info(f"Sending {len(raw_jobs)} jobs to Claude ({settings.claude_model})...")

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    parsed = _extract_json(text)

    jobs = parsed.get("jobs", [])
    logger.info(f"Claude returned {len(jobs)} classified jobs.")

    usage = response.usage
    logger.info(
        f"Token usage: input={usage.input_tokens}, output={usage.output_tokens}"
    )

    return jobs


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Sonnet pricing: Input $3/MTok, Output $15/MTok."""
    return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0
