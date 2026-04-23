"""
Email -> jobs extraction using Claude.

Takes a raw Indeed alert email (plaintext + HTML) and asks Claude to extract
all the individual job listings as structured JSON with CCQ classification.
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


EXTRACTION_PROMPT = """You receive the content of a single job-alert email from a Quebec job board (Indeed, Jobillico, Jobboom). Your task: extract every distinct job listing and classify whether it's a CCQ job.

# ABOUT CCQ (context for your classification)

The CCQ (Commission de la construction du Québec) governs construction workers in Quebec under law R-20. A "CCQ painter" is a painter working on construction sites (new builds, renovations, commercial/industrial/institutional buildings, civil engineering). They must hold a valid competency card (apprentice 1-4 or compagnon).

# EXTRACTION — one JSON object per job

For each job in the email, preserve original wording. Don't translate or paraphrase titles, employer names, or descriptions.

Fields:
- title: exact title as written
- employer: company name, null if not shown
- location: "City, QC" format
- salary_text: preserve wording exactly (e.g., "25 $ - 40 $ par heure", "À discuter")
- description: 1-3 sentences from the listing, verbatim
- posted_text: "il y a X jours", "Publiée à l'instant", etc.
- original_url: the EXACT href from the email (tracking links are fine, don't rewrite)
- source: "indeed" | "jobillico" | "jobboom" (infer from sender email)

# CCQ CLASSIFICATION — be precise

## STRONG CCQ signals (confidence >= 0.90)
Any of these = definitely CCQ, set is_likely_ccq=true:
- Explicit mentions: "CCQ", "carte CCQ", "cartes CCQ", "carte requise", "cartes requises", "compétence CCQ"
- Regulation mentions: "décret", "R-20", "selon le décret", "loi R-20"
- Convention mentions: "selon la convention", "convention collective de la construction", "conditions salariales de la CCQ", "salaire selon convention"
- Union mentions: "syndiqué", "FTQ-Construction", "CSN-Construction", "CSD-Construction"

## MEDIUM CCQ signals (confidence 0.55-0.75, set needs_review=true if no strong signal)
Likely CCQ but not 100% explicit:
- "Chantier", "chantier de construction", "chantier commercial", "chantier industriel"
- Sector indicators: "commercial", "industriel", "institutionnel" (écoles, hôpitaux), "génie civil", "voirie"
- Construction context: "nouvelle construction", "bâtiment neuf", "rénovation commerciale"
- Employer is a known construction general contractor or specialty contractor

## NEGATIVE signals (is_likely_ccq=false, confidence 0.80+ that it's NOT CCQ)
These are explicitly NOT CCQ work even if "peintre" is in the title:
- "Peintre résidentiel" / "résidentielle" WITHOUT any construction/chantier context (= residential light work, NOT governed by R-20)
- "Peintre automobile", "carrosserie", "auto body", "peinture de véhicules"
- "Peintre décorateur" for private clients
- "Peinture sur mobilier", "peinture de meubles"
- Factory/manufacturing painting on products (not buildings)
- Franchise-style residential services (CertaPro, Fresh Coat, etc.)

## AMBIGUOUS cases — set needs_review=true with confidence 0.40-0.60
- Title is just "Peintre" with no sector specified and description gives no clue
- Employer is unknown and description is too vague to judge
- "Peintre en bâtiment" without mention of construction/chantier/commercial context (could go either way)

# SALARY IS NOT A CCQ CRITERION

Do NOT use hourly rates to decide CCQ status. CCQ painter rates range from 24.35$/h (apprenti 1) to 40.58$/h (compagnon), and vary by sector (residential heavy, commercial, industrial, civil engineering). A low rate doesn't rule out CCQ — it might just be an apprentice. Preserve the salary_text field but don't let it influence is_likely_ccq.

# OUTPUT

Strict JSON only, no markdown fences, no preamble:

{
  "jobs": [
    {
      "title": "Peintre en bâtiment — chantier commercial",
      "employer": "Construction ABC",
      "location": "Montréal, QC",
      "salary_text": "selon la convention CCQ",
      "description": "Recherchons peintre avec carte CCQ valide pour projet commercial de 8 mois...",
      "posted_text": "il y a 2 jours",
      "original_url": "https://ca.indeed.com/viewjob?jk=abc123",
      "source": "indeed",
      "is_likely_ccq": true,
      "ccq_confidence": 0.95,
      "needs_review": false,
      "notes": "Explicit CCQ card requirement + commercial chantier"
    }
  ]
}

# EXAMPLES

## Example 1 — CLEAR CCQ
Description: "Carte CCQ obligatoire. Chantier commercial à Laval."
→ is_likely_ccq=true, ccq_confidence=0.98, needs_review=false

## Example 2 — NOT CCQ
Title: "Peintre résidentiel", Description: "Équipe sympa, clients particuliers, peinture intérieure."
→ is_likely_ccq=false, ccq_confidence=0.88, needs_review=false
(Residential service, no construction context)

## Example 3 — NEEDS REVIEW
Title: "Peintre en bâtiment", Description: "Candidat expérimenté recherché. Région de Montréal."
→ is_likely_ccq=true, ccq_confidence=0.55, needs_review=true
(Could be CCQ but nothing explicit)

## Example 4 — NOT CCQ despite "industriel"
Title: "Peintre industriel", Description: "Usine de meubles. Pulvérisation au pistolet sur produits finis."
→ is_likely_ccq=false, ccq_confidence=0.85, needs_review=false
(Manufacturing on products, not construction)

## Example 5 — CCQ inferred from context
Description: "Selon le décret de la construction. Travail en équipe sur divers chantiers."
→ is_likely_ccq=true, ccq_confidence=0.95, needs_review=false
(Explicit "décret de la construction")

# IF EMAIL HAS NO JOBS

Return {"jobs": []} — for example if the email is a confirmation, promotional, or system notification.
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
