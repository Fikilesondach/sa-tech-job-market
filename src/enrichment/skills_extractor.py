"""
src/enrichment/skills_extractor.py
───────────────────────────────────
Extracts a list of matched skills from job title + description
using keyword matching against the skills taxonomy YAML.

Returns a pipe-delimited string (e.g. "Python|SQL|AWS") rather than
a Python list so it can be written to a single Google Sheets cell.

Usage:
    from src.enrichment.skills_extractor import extract_skills
    skills_str = extract_skills(title, description)
"""

import re
import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "skills_taxonomy.yaml"


@lru_cache(maxsize=1)
def _load_taxonomy() -> dict[str, list[str]]:
    """Load and cache the skills taxonomy from YAML."""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def _build_patterns() -> list[tuple[str, re.Pattern]]:
    """
    Compile a regex pattern for each skill keyword.
    Uses word boundaries so 'R' doesn't match inside 'Spark'.
    Returns list of (skill_label, compiled_pattern).
    """
    taxonomy = _load_taxonomy()
    patterns = []
    for _group, skills in taxonomy.items():
        for skill in skills:
            # Use word boundary for short tokens, substring match for phrases
            if len(skill) <= 3:
                pattern = re.compile(rf"\b{re.escape(skill)}\b", re.IGNORECASE)
            else:
                pattern = re.compile(re.escape(skill), re.IGNORECASE)
            patterns.append((skill, pattern))
    return patterns


# Build once at module import — avoids recompiling on every call
_PATTERNS: list[tuple[str, re.Pattern]] = _build_patterns()


def extract_skills(title: str, description: str) -> str:
    """
    Match skills from title + description against the taxonomy.

    Searches the full title and the first 2000 chars of description
    (enough to cover requirements sections, avoids noise from boilerplate).

    Args:
        title:       Cleaned job title string
        description: Raw job description string

    Returns:
        Pipe-delimited string of matched skill labels, e.g. "Python|SQL|AWS"
        Empty string if no skills matched.
    """
    search_text = f"{title} {description[:2000]}"
    matched: list[str] = []

    for skill_label, pattern in _PATTERNS:
        if pattern.search(search_text):
            matched.append(skill_label)

    return "|".join(matched)


def enrich_skills(jobs: list[dict]) -> list[dict]:
    """Apply extract_skills to a list of cleaned job dicts. Mutates in place."""
    for job in jobs:
        job["skills"] = extract_skills(
            job.get("title_clean", ""),
            job.get("description", ""),
        )
    logger.info("Skills extracted for %d jobs", len(jobs))
    return jobs
