"""Dedup logic: fingerprinting jobs and normalizing employer names."""
import hashlib
import re
import unicodedata


def normalize_text(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace, remove punctuation."""
    if not s:
        return ""
    # NFKD strips accents: "Peintres Québécois" -> "Peintres Quebecois"
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_employer_name(name: str) -> str:
    """Normalize an employer name for dedup.

    Strips common suffixes like 'inc', 'ltd', 'enr', etc.
    """
    n = normalize_text(name)
    # Strip common Quebec company suffixes
    for suffix in [" inc", " ltd", " ltee", " enr", " senc", " srl", " co"]:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def make_fingerprint(employer_name: str, title: str, location: str) -> str:
    """
    Create a stable hash to detect the same job across sources.

    Dedup key = normalized(employer + title + location).
    So "Peintres Québécois Inc" + "Peintre résidentiel" + "Montréal, QC"
    matches "Peintres Quebecois" + "peintre residentiel" + "Montreal".
    """
    key = "|".join([
        normalize_employer_name(employer_name or ""),
        normalize_text(title or ""),
        normalize_text(location or ""),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
