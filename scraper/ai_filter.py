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


SYSTEM_PROMPT = """You are a specialized job-listing classifier for the Quebec construction industry, focused on painter jobs that fall under the CCQ (Commission de la construction du Québec) jurisdiction — i.e. work governed by Quebec's R-20 law.

You receive raw job listings scraped from public job boards (Indeed, Jobboom, etc.). For each one, you must:

1. **Determine CCQ relevance**: Is this job plausibly a CCQ-regulated painter/construction position?
   - CCQ work = construction-site painting (new builds, renovation, commercial, industrial, residential construction)
   - NOT CCQ = retail painting, art painting, cosmetic painting, spray painting of products, automotive painting
   - CCQ signals in French: "CCQ", "carte CCQ", "peintre en bâtiment", "peintre de construction", "chantier", "compétence CCQ"
   - CCQ signals in English: "CCQ card", "construction painter", "commercial painter", "R-20"
   - If the posting mentions CCQ explicitly, it's almost certainly CCQ work.

2. **Extract structured fields** cleanly, without adding interpretation:
   - title, employer name, city, region, full address if present
   - job_type (full-time, part-time, contract, temporary, permanent — only if stated)
   - trade (peintre, painter, apprenti-peintre, etc.)
   - salary_text (preserve raw wording — do NOT compute or guess)

3. **Flag uncertain ones**: If you're not confident whether this is CCQ-relevant, set `needs_review: true` and explain briefly in `notes`.

4. **Confidence scoring**:
   - 0.9-1.0 = explicitly mentions CCQ, clearly construction-site painter
   - 0.7-0.9 = strongly implied CCQ (e.g., "construction painter" with no CCQ word)
   - 0.4-0.7 = unclear → set needs_review=true
   - 0.0-0.4 = clearly NOT CCQ → set is_ccq=false, is_relevant=false

5. **Region normalization**: Map locations to canonical Quebec regions:
   - "Montréal, QC", "Montreal" → "Montréal"
   - "Laval, QC" → "Laval"
   - "Longueuil", "Rive-Sud" → "Montérégie"
   - "Québec, QC", "Ville de Québec" → "Québec"

Respond ONLY with valid JSON matching this schema exactly:

{
  "jobs": [
    {
      "index": 0,
      "is_relevant": true,
      "is_ccq": true,
      "confidence": 0.95,
      "needs_review": false,
      "title": "Peintre en bâtiment — Chantier commercial",
      "employer_name": "Peintres Québécois Inc",
      "city": "Montréal",
      "region": "Montréal",
      "address": "1234 rue Sainte-Catherine, Montréal, QC",
      "job_type": "full-time",
      "trade": "peintre",
      "salary_text": "25 $/h",
      "description_clean": "Recherchons peintre avec carte CCQ pour projet commercial...",
      "notes": "Explicit CCQ requirement in posting."
    }
  ]
}

Rules:
- `index` must match the input order.
- Never invent data. If a field isn't in the listing, return null.
- For irrelevant jobs (not CCQ construction painter), still return them with is_relevant=false so the caller knows.
- `description_clean` is a trimmed, cleaned version of the original description — NO interpretation, NO summarization beyond formatting cleanup. Preserve original language (French stays French).
- Do not add markdown, do not wrap in ```json — just raw JSON.
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
    # Remove markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def classify_batch(raw_jobs: list[dict]) -> list[dict]:
    """
    Send a batch of raw jobs to Claude, return classified results.

    Each result dict matches the schema in SYSTEM_PROMPT.
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

    # Log token usage for cost tracking
    usage = response.usage
    logger.info(
        f"Token usage: input={usage.input_tokens}, output={usage.output_tokens}"
    )

    return jobs


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """
    Rough cost estimate for Sonnet pricing (as of 2026 — verify current rates).
    Input: $3/MTok, Output: $15/MTok (approximate — check docs.claude.com).
    """
    return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0
