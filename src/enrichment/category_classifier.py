"""
src/enrichment/category_classifier.py
──────────────────────────────────────
Classifies each job into one of seven dashboard categories based on title keywords.

Priority order (first match wins — avoids dual-tagging):
    1. ML & AI          — data scientists, ML engineers, AI roles
    2. Quant & Actuarial — actuaries, quants, risk modellers
    3. Cloud & DevOps   — cloud engineers, platform, SRE, DevOps
    4. Data             — data analysts, engineers, BI, analytics
    5. Software Eng     — SWE, backend, frontend, full-stack, mobile
    6. Cybersecurity    — security analysts, pen testers, SOC
    7. Other Tech       — catch-all for remaining tech roles

Title is searched first. If no regex matches, falls back to the
_category tag injected by AdzunaClient at ingestion time.
If neither matches, returns "Other Tech".

Usage:
    from src.enrichment.category_classifier import classify_category
    category = classify_category(title, adzuna_category)
"""

import re
import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "categories.yaml"

# ── Adzuna category → dashboard category fallback map ─────────────────────────
# Adzuna's _category tag is injected per-query in adzuna_client.py.
# This maps Adzuna's raw category strings to our dashboard categories.
ADZUNA_FALLBACK: dict[str, str] = {
    "data scientist":               "ML & AI",
    "machine learning":             "ML & AI",
    "ml engineer":                  "ML & AI",
    "ai engineer":                  "ML & AI",
    "actuarial":                    "Quant & Actuarial",
    "quant":                        "Quant & Actuarial",
    "risk":                         "Quant & Actuarial",
    "cloud engineer":               "Cloud & DevOps",
    "devops":                       "Cloud & DevOps",
    "data analyst":                 "Data",
    "data engineer":                "Data",
    "business intelligence":        "Data",
    "bi developer":                 "Data",
    "software engineer":            "Software Engineering",
    "developer":                    "Software Engineering",
    "cybersecurity":                "Cybersecurity",
    "security analyst":             "Cybersecurity",
}


@lru_cache(maxsize=1)
def _load_config() -> dict[str, list[str]]:
    """Load and cache the categories config from YAML."""
    with open(CONFIG_PATH, "r") as f:
        data = yaml.safe_load(f)
    return data["categories"]


def _build_patterns() -> list[tuple[str, re.Pattern]]:
    """
    Compile one combined alternation pattern per category.
    Evaluated in the priority order defined in categories.yaml.

    Returns list of (category_label, compiled_pattern) in priority order.
    """
    config = _load_config()
    patterns = []
    for category, keywords in config.items():
        # Build one alternation: (?:keyword1|keyword2|...)
        # Word boundary on both ends for single-word keywords.
        # For multi-word phrases, substring match is fine (phrase is specific enough).
        alternation = "|".join(re.escape(kw) for kw in keywords)
        pattern = re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)
        patterns.append((category, pattern))
    return patterns


# Compile once at module import
_PATTERNS: list[tuple[str, re.Pattern]] = _build_patterns()


def classify_category(title: str, adzuna_category: str = "") -> str:
    """
    Classify a job into a dashboard category.

    Args:
        title:            Cleaned job title string
        adzuna_category:  The _category tag from AdzunaClient (optional fallback)

    Returns:
        One of the seven dashboard categories, or "Other Tech" if unclassified.
    """
    # 1. Title-first — most reliable signal
    for category, pattern in _PATTERNS:
        if pattern.search(title):
            return category

    # 2. Adzuna category fallback — coarse but useful for edge cases
    if adzuna_category:
        adzuna_lower = adzuna_category.lower().strip()
        for key, mapped_category in ADZUNA_FALLBACK.items():
            if key in adzuna_lower:
                return mapped_category

    # 3. Default
    return "Other Tech"


def enrich_category(jobs: list[dict]) -> list[dict]:
    """
    Apply category classification to a list of cleaned job dicts.
    Mutates in place. Reads title_clean and _category from each job dict.
    """
    for job in jobs:
        job["category"] = classify_category(
            job.get("title_clean", ""),
            job.get("_category", ""),
        )

    # Log category distribution for pipeline visibility
    cat_counts: dict[str, int] = {}
    for job in jobs:
        cat = job["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    logger.info("Category distribution: %s", cat_counts)

    return jobs
