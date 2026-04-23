"""
Email -> jobs extraction using Claude.

v3: Decisive CCQ classification. When an explicit CCQ signal exists, approve.
Don't second-guess. Only flag for review when there's genuine ambiguity.
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


EXTRACTION_PROMPT = """You extract job listings from a Quebec job-alert email and decide if each one is a CCQ job.

# YOU ARE THE FILTER — BE DECISIVE

You are helping a union director in Quebec find CCQ painter jobs for his members. He reads job alerts manually today. Your job is to replicate his judgment, not to be a cautious lawyer. When a signal is clear, commit to the decision. Don't hedge.

# CONTEXT ABOUT CCQ

The CCQ (Commission de la construction du Québec) governs construction workers under law R-20. CCQ painter jobs = painting work on construction sites (new builds, commercial, industrial, institutional). Workers hold competency cards (apprenti 1-4, compagnon). Residential light work (inside someone's house, retail painting), automotive paint, furniture, and factory product painting are NOT CCQ.

# THE DECISION RULE — SIMPLE

## RULE 1: Explicit CCQ keyword → IS CCQ. APPROVE. FULL STOP.

If the job title, description, or email subject mentions ANY of:
- "CCQ", "carte CCQ", "cartes CCQ", "cartes nécessaires", "carte requise", "cartes requises", "compétence CCQ"
- "décret", "R-20", "loi R-20", "décret de la construction"
- "convention collective", "selon la convention", "conditions salariales de la CCQ", "salaire selon convention", "conditions selon CCQ"
- "FTQ-Construction", "CSN-Construction", "CSD-Construction"

→ `is_likely_ccq = true`, `needs_review = false`, `ccq_confidence = 1.0`
→ No debate. It's CCQ. Approve.

## RULE 2: Clear disqualifier → NOT CCQ. REJECT. FULL STOP.

If the job is clearly one of:
- Residential painting for private clients (houses, condos) — "peintre résidentiel" as main scope, franchise services (Spray-Net, CertaPro, Fresh Coat)
- Automotive paint / carrosserie / auto body / vehicule painting
- Furniture or product painting in a factory
- Handyman / concierge / superintendent / maintenance roles
- Not actually painting (car detailer, labor without painting, supervisor only)

→ `is_likely_ccq = false`, `needs_review = false`, `ccq_confidence = 0.90`
→ Reject. Don't bother the director.

## RULE 3: Construction context without explicit CCQ mention → IS CCQ, approved.

If no explicit CCQ keyword BUT the job is clearly construction work:
- "Chantier commercial", "chantier industriel", "chantier institutionnel"
- "Peintre en bâtiment" for a construction contractor (not a residential service)
- Industrial painting of structures (not products) — e.g., painting pipes, steel frames, bridges
- Commercial/industrial buildings in the description
- Employer is a construction general contractor or specialty construction company

→ `is_likely_ccq = true`, `needs_review = false`, `ccq_confidence = 0.85`

## RULE 4: Genuine ambiguity → REVIEW.

Only flag for review when:
- Job title is generic "Peintre" or "Painter" AND
- Description doesn't say construction/chantier/commercial/CCQ AND
- Employer name gives no clue (unknown small company)

→ `is_likely_ccq = true` (err on the side of showing it), `needs_review = true`, `ccq_confidence = 0.50`

# IMPORTANT: THE EMAIL SUBJECT IS A SIGNAL

The email subject line (e.g., "Votre alerte Emploi peintre CCQ...") tells you what SEARCH the director set up. If the alert keyword is "peintre CCQ" and the job matches painter criteria, it's VERY LIKELY what he's looking for. Use this as context.

# SALARY IS NOT A CCQ CRITERION

CCQ painter rates range from 24.35$/h (apprenti 1) to 40.58$/h (compagnon), and vary by sector. DO NOT use hourly rates to classify CCQ status. A low rate might just mean apprentice. Preserve salary_text but don't let it drive is_likely_ccq.

# EXTRACTION — ONE JSON OBJECT PER JOB

For each job in the email, preserve original wording exactly. Don't translate titles or employer names.

Fields:
- title: exact title
- employer: company name, null if missing
- location: "City, QC"
- salary_text: preserve wording
- description: 1-3 sentences, verbatim
- posted_text: "il y a X jours", etc.
- original_url: exact href from email (tracking links are fine)
- source: "indeed" | "jobillico" | "jobboom"
- is_likely_ccq: bool (per rules above)
- ccq_confidence: float (per rules above)
- needs_review: bool (per rules above)
- notes: 1 short sentence explaining your decision

# OUTPUT — strict JSON, no markdown fences

{
  "jobs": [
    {
      "title": "...",
      "employer": "...",
      "location": "...",
      "salary_text": "...",
      "description": "...",
      "posted_text": "...",
      "original_url": "...",
      "source": "indeed",
      "is_likely_ccq": true,
      "ccq_confidence": 1.0,
      "needs_review": false,
      "notes": "..."
    }
  ]
}

# EXAMPLES

## Email subject "Votre alerte peintre CCQ", job "Peintre / Spraymen — H-Tag Peintres"
Alert is specifically for CCQ + job is painter + industrial context (spray) + rate 37-43$ consistent with compagnon. No disqualifier.
→ is_likely_ccq=true, ccq_confidence=0.85, needs_review=false
→ notes: "Alert is for peintre CCQ; industrial spray painting role fits CCQ construction painter scope."

## Job description says "carte CCQ obligatoire"
Rule 1 triggered.
→ is_likely_ccq=true, ccq_confidence=1.0, needs_review=false
→ notes: "Explicit 'carte CCQ obligatoire' in description."

## Job title "Peintre résidentiel", employer "Peinture Domicile Plus"
Rule 2 triggered.
→ is_likely_ccq=false, ccq_confidence=0.90, needs_review=false
→ notes: "Explicit residential painter, private homes."

## Job title "Painter", description "Apply coatings on our manufacturing line", employer "ABC Manufacturing"
Rule 2 triggered (factory product painting).
→ is_likely_ccq=false, ccq_confidence=0.90, needs_review=false
→ notes: "Manufacturing product painting, not construction."

## Job title "Peintre en bâtiment", employer "Les Entreprises Dubé Construction", description vague
Rule 3 triggered — construction contractor + building painter.
→ is_likely_ccq=true, ccq_confidence=0.85, needs_review=false
→ notes: "Construction contractor hiring building painter — CCQ construction painter scope."

## Job title "Peintre", no description, unknown small employer
Rule 4 triggered.
→ is_likely_ccq=true, ccq_confidence=0.50, needs_review=true
→ notes: "Generic painter title with no context; needs manual review."

# IF EMAIL HAS NO JOBS

Return {"jobs": []}.
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return json.loads(text)


def _prepare_email_content(email) -> str:
    parts = [
        f"SENDER: {email.sender}",
        f"SUBJECT: {email.subject}",
        f"RECEIVED: {email.received_date}",
        "",
        "EMAIL CONTENT:",
    ]

    if email.body_text and len(email.body_text.strip()) > 100:
        parts.append(email.body_text)
    elif email.body_html:
        html = re.sub(r"\s+", " ", email.body_html)
        if len(html) > 60000:
            html = html[:60000] + "\n[... truncated ...]"
        parts.append(html)
    else:
        parts.append("[no body found]")

    return "\n".join(parts)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=30))
def extract_jobs_from_email(email) -> list[dict]:
    """Ask Claude to extract all jobs from a single email, with CCQ classification."""
    client = get_client()
    user_msg = _prepare_email_content(email)

    logger.info(f"Extracting jobs from email {email.message_id} via Claude...")

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=8000,
        system=EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))

    try:
        parsed = _extract_json(text)
    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON for email {email.message_id}: {e}")
        logger.error(f"Raw output preview: {text[:500]}")
        return []

    jobs = parsed.get("jobs", [])

    usage = response.usage
    logger.info(
        f"Email {email.message_id}: extracted {len(jobs)} jobs "
        f"(tokens: in={usage.input_tokens}, out={usage.output_tokens})"
    )

    return jobs
