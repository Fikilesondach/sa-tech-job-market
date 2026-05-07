"""
src/enrichment/seniority_classifier.py
───────────────────────────────────────
Classifies each job into a seniority tier based on title keywords.

Tiers (in priority order, first match wins):
    Graduate  — graduate programmes, internships, entry-level
    Junior    — junior / jr roles, 0–2 years exp
    Senior    — senior, principal, staff
    Lead      — lead, head of, chapter lead
    Mid       — everything else (default for experienced roles)

Title is checked first. If no match, falls back to scanning
the first 500 chars of description for experience year indicators.

Usage:
    from src.enrichment.seniority_classifier import classify_seniority
    tier = classify_seniority(title, description)
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Patterns (evaluated in priority order) ────────────────────────────────────

_GRADUATE = re.compile(
    r"\b(graduate|intern|internship|learnership|entry[\s-]level|junior\s+graduate|"
    r"cadet|trainee|bursary)\b",
    re.IGNORECASE,
)

_JUNIOR = re.compile(
    r"\b(junior|jr\.?)\b",
    re.IGNORECASE,
)

_LEAD = re.compile(
    r"\b(lead|principal|staff\s+engineer|chapter\s+lead|head\s+of|"
    r"engineering\s+manager|practice\s+lead)\b",
    re.IGNORECASE,
)

_SENIOR = re.compile(
    r"\b(senior|sr\.?|specialist|expert)\b",
    re.IGNORECASE,
)

# Fallback: scan description for year-of-experience indicators
# "3+ years", "3-5 years", "minimum 3 years"
_EXP_YEARS = re.compile(
    r"(\d+)\s*[\+\-–]?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?(?:experience|exp)",
    re.IGNORECASE,
)


def _years_from_description(description: str) -> int | None:
    """Extract the minimum years of experience mentioned in description."""
    matches = _EXP_YEARS.findall(description[:800])
    if not matches:
        return None
    years = [int(m) for m in matches if m.isdigit()]
    return min(years) if years else None


def classify_seniority(title: str, description: str) -> str:
    """
    Classify a job into a seniority tier.

    Args:
        title:       Cleaned job title
        description: Job description (first 800 chars used for fallback)

    Returns:
        One of: "Graduate", "Junior", "Mid", "Senior", "Lead"
    """
    # Title-first classification
    if _GRADUATE.search(title):
        return "Graduate"
    if _JUNIOR.search(title):
        return "Junior"
    if _LEAD.search(title):
        return "Lead"
    if _SENIOR.search(title):
        return "Senior"

    # Fallback: infer from years of experience in description
    years = _years_from_description(description)
    if years is not None:
        if years <= 1:
            return "Junior"
        elif years <= 4:
            return "Mid"
        elif years <= 7:
            return "Senior"
        else:
            return "Lead"

    # Default for unclassified roles (likely mid-level)
    return "Mid"


def is_graduate_programme(title: str, description: str) -> bool:
    """Return True if the job is explicitly a graduate programme or internship."""
    return bool(_GRADUATE.search(title) or _GRADUATE.search(description[:300]))


def enrich_seniority(jobs: list[dict]) -> list[dict]:
    """Apply seniority classification to a list of cleaned job dicts. Mutates in place."""
    for job in jobs:
        title = job.get("title_clean", "")
        desc  = job.get("description", "")
        job["seniority"]   = classify_seniority(title, desc)
        job["is_graduate"] = is_graduate_programme(title, desc)
    logger.info("Seniority classified for %d jobs", len(jobs))
    return jobs
