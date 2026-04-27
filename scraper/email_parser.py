"""
Email -> jobs extraction using Claude.

v5 changes from v4:
    - Robust JSON extraction: handles markdown fences, preambles, suffixes
    - max_tokens raised 8000 -> 16000 (Lofty/devinci emails were truncating)
    - Fallback: when JSON parsing fails completely, returns a placeholder
      job in review queue with the raw Claude output preview, so nothing
      is lost silently. The director can manually fix it from admin.
    - Stronger system instruction: "JSON ONLY, no preamble, no markdown"
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


EXTRACTION_PROMPT = """You extract job listings from a Quebec job-alert email or web page, and decide if each one is a CCQ **PAINTER** job.

# CRITICAL OUTPUT RULE — READ FIRST

Your entire response MUST be a single valid JSON object. No preamble. No explanation. No markdown fences (no ```json). Start your response with `{` and end with `}`. Nothing else.

If you have analysis to share, put it in the `notes` field of each job. Do NOT write commentary outside the JSON.

# YOU ARE THE FILTER FOR A PAINTERS' UNION

You are helping a union director for CCQ PAINTERS in Quebec. You're replicating what he does manually: skim job sources, keep only painter jobs that are under the CCQ/R-20 construction decree, and reject everything else.

Context on CCQ: it governs construction workers in Quebec under law R-20. Workers hold competency cards (apprenti 1-4, compagnon). Only certain trades are CCQ-governed: peintre, charpentier-menuisier, électricien, plombier, etc. We ONLY care about peintres.

---

# RULE 0 — TRADE MUST BE PAINTER (absolute prerequisite)

Before anything else, check the trade. If the job is NOT a painting job, reject it with high confidence.

## Painter trade signals (PASS Rule 0)
- Title or description mentions: "peintre", "peinture", "painter", "painting", "peintre en bâtiment", "peintre construction", "peintre industriel", "peintre au pistolet", "sprayman", "spraygirl", "applicateur de peinture", "applicateur de revêtement", "finition"

## Other-trade disqualifiers (FAIL Rule 0 — reject even if CCQ)
If the title or description clearly refers to a NON-painter trade, reject:
- Charpentier, charpentier-menuisier, menuisier
- Électricien, monteur-électricien
- Plombier, tuyauteur, tuyauteur de chantier
- Briqueteur, maçon, cimentier-applicateur
- Ferblantier, couvreur
- Grutier, opérateur de machinerie lourde
- Ferrailleur, monteur d'acier de structure, monteur-assembleur
- Manoeuvre (if it's a generic labourer role, not specifically painter helper)
- Journalier, ouvrier général
- Plâtrier (debatable — usually separate from peintre)
- Calorifugeur, isolant, mécanicien

For these trades: `is_likely_ccq = false`, `ccq_confidence = 0.95`, `needs_review = false`,
notes: "Non-painter trade (e.g., charpentier/électricien/manoeuvre) — outside our scope."

## Mixed trade postings
If one posting lists multiple trades (e.g., "peintre, plâtrier, plombier"), treat it as painter ONLY if painter is clearly the primary role.

---

# RULE 1 — EXPLICIT CCQ KEYWORD → IS CCQ, APPROVE

If Rule 0 passes AND the job/description/email-subject mentions:
- "CCQ", "carte CCQ", "cartes CCQ", "cartes requises", "carte nécessaire", "compétence CCQ"
- "décret", "R-20", "loi R-20", "décret de la construction"
- "convention collective", "selon la convention", "conditions salariales de la CCQ"
- "FTQ-Construction", "CSN-Construction", "CSD-Construction"

→ `is_likely_ccq = true`, `needs_review = false`, `ccq_confidence = 1.0`

---

# RULE 2 — DISQUALIFIER → NOT CCQ, REJECT

Even if Rule 0 passes (it's a painter), reject if clearly:
- Residential painting for private clients (houses, condos) — "peintre résidentiel" as main scope with no construction context, franchise services (Spray-Net, CertaPro, Fresh Coat)
- Automotive paint, carrosserie, auto body, vehicule painting
- Furniture or product painting in a factory
- Handyman / concierge / superintendent / maintenance roles that happen to include "paint touch-ups"

→ `is_likely_ccq = false`, `ccq_confidence = 0.90`, `needs_review = false`,
notes: "Painter but not CCQ scope (e.g., residential service, automotive, factory product)."

---

# RULE 3 — CONSTRUCTION CONTEXT (painter + construction signals)

Rule 0 passes, no explicit CCQ keyword, but clearly construction:
- "Chantier commercial/industriel/institutionnel"
- "Peintre en bâtiment" for a construction contractor (not a residential service)
- Industrial painting of structures (pipes, steel, bridges — not products)
- Commercial/industrial buildings in description
- Employer is a known construction contractor

→ `is_likely_ccq = true`, `ccq_confidence = 0.85`, `needs_review = false`

---

# RULE 4 — AMBIGUOUS → REVIEW

Rule 0 passes but:
- Title is generic "Peintre" with no sector info
- Description gives no clue about construction vs residential
- Employer is unknown

→ `is_likely_ccq = true` (err on side of surfacing), `needs_review = true`, `ccq_confidence = 0.50`

---

# CONTEXT CLUES FROM SENDER/SUBJECT

The email SUBJECT or source URL can hint at what search the director set up. If a Glassdoor email says "Peintre En Bâtiment" or an Indeed alert was for "peintre CCQ", that's context that what's inside is probably painter. But always verify against the actual job title — don't trust blindly.

---

# SALARY IS NOT A CCQ CRITERION

CCQ painter rates: apprenti 1 = 24.35$/h, compagnon = 40.58$/h. Low rate doesn't rule out CCQ (might be apprentice). Preserve salary_text verbatim. Don't let it drive is_likely_ccq.

---

# EXTRACTION FIELDS (per job, preserve wording)

- title (exact)
- employer (null if missing)
- location ("City, QC")
- salary_text (verbatim)
- description (1-3 sentences, verbatim — keep SHORT to save tokens)
- posted_text ("il y a X jours", etc.)
- original_url (exact href, tracking links OK)
- source ("indeed" | "glassdoor" | "jobillico" | "jobboom" | "web")
- is_likely_ccq (bool)
- ccq_confidence (float)
- needs_review (bool)
- notes (1 short sentence explaining your decision)

KEEP descriptions short (1-2 sentences max). Don't waste tokens on verbose copy.

---

# OUTPUT — JSON only, starts with `{`, ends with `}`, no fences, no preamble

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

---

# EXAMPLES

## Example 1 — NON-PAINTER CCQ → REJECT (Rule 0)
Title: "Charpentier(ère)-menuisier(ère)", Description: "Carte CCQ compagnon requise, chantier..."
→ is_likely_ccq=false, ccq_confidence=0.95, needs_review=false
→ notes: "Non-painter trade (charpentier-menuisier) — outside our scope."

## Example 2 — MANOEUVRE CCQ → REJECT (Rule 0)
Title: "Manoeuvre spécialisé (Poste CCQ)", Description: "manoeuvres/journaliers CCQ pour chantiers..."
→ is_likely_ccq=false, ccq_confidence=0.95, needs_review=false
→ notes: "Non-painter trade (manoeuvre/journalier) — outside our scope."

## Example 3 — EXPLICIT CCQ PAINTER → APPROVE
Description: "peintres avec carte CCQ apprentis et compagnons..."
→ is_likely_ccq=true, ccq_confidence=1.0, needs_review=false
→ notes: "Explicit CCQ card requirement for painters."

## Example 4 — RESIDENTIAL PAINTER → REJECT (Rule 2)
Title: "Peintre résidentiel", Description: "Peinture intérieure chez particuliers, équipe sympa"
→ is_likely_ccq=false, ccq_confidence=0.90, needs_review=false
→ notes: "Residential painter for private clients — not CCQ scope."

## Example 5 — AMBIGUOUS PAINTER → REVIEW (Rule 4)
Title: "Peintre", Description: "Candidat expérimenté, Montréal", employer: "ABC Inc"
→ is_likely_ccq=true, ccq_confidence=0.50, needs_review=true
→ notes: "Generic painter, no construction context, unknown employer."

---

# IF EMAIL/PAGE HAS NO JOBS

Return {"jobs": []}. (e.g., confirmation email, blog post, category page)
"""


def _extract_json(text: str) -> dict:
    """
    Robust JSON extraction. Handles:
    - Pure JSON: {"jobs": [...]}
    - Markdown fences: ```json\n{...}\n```
    - Preamble + JSON: "Here's the analysis...\n{...}"
    - Suffix after JSON: "{...}\n\nLet me know if..."
    - Mixed: "```json\n{...}\n```\n\nNotes: ..."

    Strategy: find the outermost {...} block by bracket counting.
    """
    text = text.strip()

    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    # Fast path: try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find first `{` and match to its closing `}` via bracket counting
    # (handles strings, escapes, nested objects)
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No `{` found in response", text, 0)

    depth = 0
    in_string = False
    escape = False
    end = -1

    for i in range(start, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\" and in_string:
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        # Truncated JSON — try to salvage by closing brackets
        raise json.JSONDecodeError(
            "Unterminated JSON object (possibly truncated by max_tokens)",
            text, start
        )

    return json.loads(text[start:end])


def _make_review_placeholder(email, raw_preview: str, error_msg: str) -> dict:
    """
    When JSON extraction fully fails, create a placeholder job that gets
    routed to the review queue. Director can manually inspect & fix from
    the admin console rather than losing the email silently.
    """
    return {
        "title": "⚠️ Extraction manuelle requise",
        "employer": email.sender[:80] if hasattr(email, "sender") else "Inconnu",
        "location": None,
        "salary_text": None,
        "description": (
            f"L'extraction automatique a échoué pour cette source. "
            f"Erreur: {error_msg}. "
            f"Aperçu de la réponse Claude: {raw_preview[:400]}"
        ),
        "posted_text": None,
        "original_url": email.message_id if hasattr(email, "message_id") else "unknown",
        "source": "web",
        "is_likely_ccq": True,
        "ccq_confidence": 0.50,
        "needs_review": True,
        "notes": f"Parser fail — needs manual extraction. Subject: {email.subject[:120] if hasattr(email, 'subject') else ''}",
    }


def _prepare_email_content(email) -> str:
    parts = [
        f"SENDER: {email.sender}",
        f"SUBJECT: {email.subject}",
        f"RECEIVED: {email.received_date}",
        "",
        "CONTENT:",
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
    """Ask Claude to extract painter jobs from a single source (email or web page)."""
    client = get_client()
    user_msg = _prepare_email_content(email)

    logger.info(f"Extracting jobs from {email.message_id[:60]} via Claude...")

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=16000,  # raised from 8000 — devinci/Lofty emails truncate at 8k
        system=EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    usage = response.usage

    try:
        parsed = _extract_json(text)
        jobs = parsed.get("jobs", [])
        logger.info(
            f"{email.message_id[:60]}: extracted {len(jobs)} jobs "
            f"(tokens: in={usage.input_tokens}, out={usage.output_tokens})"
        )
        return jobs

    except json.JSONDecodeError as e:
        # JSON extraction failed even after robust parsing.
        # Don't lose the email silently — surface it as a review-queue placeholder.
        logger.error(f"Claude returned invalid JSON for {email.message_id}: {e}")
        logger.error(f"Raw output preview: {text[:500]}")
        logger.warning(
            f"Creating review-queue placeholder for {email.message_id[:60]} "
            f"(tokens: in={usage.input_tokens}, out={usage.output_tokens})"
        )
        return [_make_review_placeholder(email, text, str(e))]
