"""
src/cleaning/cleaner.py
───────────────────────
Transforms raw Adzuna response dicts into flat, typed records.

Responsibilities:
    - Extract nested fields (company, location, category)
    - Parse and normalise job titles
    - Parse salary fields (present in ~35% of SA listings)
    - Extract province and city from location area array
    - Detect remote roles
    - Parse posted date to date string
    - Drop fields not needed downstream

Input:  raw dict from AdzunaClient.pull_all()
Output: flat dict matching the pipeline data schema

Usage:
    from src.cleaning.cleaner import clean_job
    cleaned = [clean_job(raw) for raw in raw_jobs]
"""

import re
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Location normalisation ─────────────────────────────────────────────────────

# Map Adzuna area strings → our standard city labels for the dashboard
CITY_MAP: dict[str, str] = {
    "Cape Town":        "Cape Town",
    "City of Cape Town":"Cape Town",
    "Cape Town City":   "Cape Town",
    "Johannesburg":     "Johannesburg",
    "Joburg":           "Johannesburg",
    "Sandton":          "Johannesburg",
    "Midrand":          "Johannesburg",
    "Randburg":         "Johannesburg",
    "Rosebank":         "Johannesburg",
    "Pretoria":         "Pretoria",
    "Tshwane":          "Pretoria",
    "Centurion":        "Pretoria",
    "Durban":           "Durban",
    "eThekwini":        "Durban",
    "Port Elizabeth":   "Gqeberha",
    "Gqeberha":         "Gqeberha",
    "East London":      "East London",
    "Stellenbosch":     "Stellenbosch",
    "Paarl":            "Cape Winelands",
}

PROVINCE_MAP: dict[str, str] = {
    "Gauteng":          "Gauteng",
    "Western Cape":     "Western Cape",
    "KwaZulu-Natal":    "KwaZulu-Natal",
    "Eastern Cape":     "Eastern Cape",
    "Free State":       "Free State",
    "Limpopo":          "Limpopo",
    "Mpumalanga":       "Mpumalanga",
    "North West":       "North West",
    "Northern Cape":    "Northern Cape",
}

REMOTE_KEYWORDS = re.compile(
    r"\b(remote|work[\s-]from[\s-]home|wfh|fully[\s-]remote|hybrid)\b",
    re.IGNORECASE,
)

# ── Title normalisation ────────────────────────────────────────────────────────

# Parenthetical suffixes to strip from titles
# e.g. "Data Scientist (WhatsApp and Genesys Cloud Capabilities)" → "Data Scientist"
TITLE_PAREN_STRIP = re.compile(r"\s*[\(\[].*?[\)\]]", re.IGNORECASE)

# Pipe / dash suffixes like "Senior Dev | Cape Town" or "Software Engineer - Remote"
TITLE_SUFFIX_STRIP = re.compile(
    r"\s*[\|–—-]\s*(cape town|johannesburg|pretoria|durban|remote|south africa|gauteng|za)\s*$",
    re.IGNORECASE,
)

# Trailing punctuation/separators left after the above strips
# Catches orphaned " -", " /", " |", " –" at the end of a title
TITLE_TRAILING_PUNCT = re.compile(r"[\s\-/|–—]+$")

# Collapse multiple spaces
MULTI_SPACE = re.compile(r"\s{2,}")


# ── Extraction helpers ─────────────────────────────────────────────────────────

def _extract_company(raw_company: Any) -> str:
    """Extract display name from Adzuna's nested company dict."""
    if isinstance(raw_company, dict):
        return raw_company.get("display_name", "Unknown").strip()
    if isinstance(raw_company, str):
        return raw_company.strip()
    return "Unknown"


def _extract_location(raw_location: Any) -> tuple[str, str, str]:
    """
    Extract (display_name, province, city) from Adzuna's nested location dict.

    Adzuna area array structure (when present):
        ['South Africa', '<Province>', '<District/Metro>', '<City>']
    Province is always index 1, city is the last element.
    """
    if not isinstance(raw_location, dict):
        return "Unknown", "Unknown", "Unknown"

    display = raw_location.get("display_name", "Unknown")
    area: list[str] = raw_location.get("area", [])

    # Province: index 1 (index 0 is always 'South Africa')
    raw_province = area[1] if len(area) > 1 else ""
    province = PROVINCE_MAP.get(raw_province, raw_province or "Unknown")

    # City: last element of area array
    raw_city = area[-1] if area else ""
    city = CITY_MAP.get(raw_city, raw_city or "Unknown")

    return display, province, city


def _parse_salary(raw: dict) -> tuple[float | None, float | None, float | None]:
    """
    Parse salary_min / salary_max from the raw Adzuna dict.

    Adzuna returns these as top-level numeric fields (annual, ZAR) when available.
    Returns (salary_min, salary_max, salary_mid) — all None if not present.
    """
    s_min = raw.get("salary_min")
    s_max = raw.get("salary_max")

    # Adzuna sometimes returns 0 rather than null — treat 0 as missing
    if s_min is not None and float(s_min) <= 0:
        s_min = None
    if s_max is not None and float(s_max) <= 0:
        s_max = None

    s_min = float(s_min) if s_min is not None else None
    s_max = float(s_max) if s_max is not None else None

    # Sanity check: reject implausibly low values (Adzuna occasionally
    # returns monthly figures or data errors)
    if s_min is not None and s_min < 24_000:   # < R2k/month annualised
        logger.debug("Rejecting implausible salary_min: %.0f", s_min)
        s_min = None
    if s_max is not None and s_max < 24_000:
        logger.debug("Rejecting implausible salary_max: %.0f", s_max)
        s_max = None

    # Midpoint — only compute if both bounds are present and sensible
    s_mid: float | None = None
    if s_min is not None and s_max is not None:
        s_mid = round((s_min + s_max) / 2, 0)

    return s_min, s_max, s_mid


def _clean_title(raw_title: str) -> str:
    """Normalise a job title string."""
    title = raw_title.strip()
    title = TITLE_PAREN_STRIP.sub("", title)
    title = TITLE_SUFFIX_STRIP.sub("", title)
    title = MULTI_SPACE.sub(" ", title)
    # BUG FIX: strip any trailing separators/punctuation left after the above
    # e.g. "Data Scientist / Data Analyst -" → "Data Scientist / Data Analyst"
    title = TITLE_TRAILING_PUNCT.sub("", title)
    return title.strip()


def _detect_remote(title: str, description: str) -> bool:
    """Return True if the job appears to be remote or hybrid."""
    return bool(
        REMOTE_KEYWORDS.search(title) or REMOTE_KEYWORDS.search(description[:500])
    )


def _parse_date(date_str: str | None) -> str | None:
    """Convert ISO datetime string to YYYY-MM-DD date string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return None


# ── Main transform ─────────────────────────────────────────────────────────────

def clean_job(raw: dict) -> dict:
    """
    Transform a single raw Adzuna job dict into a flat, typed pipeline record.

    All downstream modules (enrichment, storage) consume this schema.
    Fields that cannot be determined are set to None — never omitted —
    so the schema remains consistent across all records.
    """
    title_raw   = str(raw.get("title", "")).strip()
    title_clean = _clean_title(title_raw)
    description = str(raw.get("description", ""))

    company                          = _extract_company(raw.get("company"))
    location_display, province, city = _extract_location(raw.get("location"))
    salary_min, salary_max, salary_mid = _parse_salary(raw)

    return {
        # ── Identity
        "job_id":           str(raw.get("id", "")),
        "source":           "adzuna",

        # ── Title
        "title_raw":        title_raw,
        "title_clean":      title_clean,

        # ── Company
        "company":          company,
        "company_type":     None,    # filled by enrichment/company_classifier.py

        # ── Location
        "location_display": location_display,
        "province":         province,
        "city":             city,
        "is_remote":        _detect_remote(title_raw, description),

        # ── Salary (annual ZAR — None if not provided by advertiser)
        "salary_min":       salary_min,
        "salary_max":       salary_max,
        "salary_mid":       salary_mid,
        # BUG FIX: Adzuna returns salary_is_predicted as string "0"/"1" in some
        # responses. bool("0") is True (non-empty string). Cast to int first.
        "salary_is_predicted": bool(int(raw.get("salary_is_predicted", 0))),

        # ── Classification (filled by enrichment modules)
        "category":         raw.get("_category"),   # from AdzunaClient tagging
        "seniority":        None,
        "skills":           None,
        "is_graduate":      None,

        # ── Description (kept for skills extraction, not written to Sheets)
        "description":      description,

        # ── Dates
        "date_posted":      _parse_date(raw.get("created")),
        "date_pulled":      _parse_date(raw.get("_pulled_at")),

        # ── Source metadata
        "redirect_url":     raw.get("redirect_url", ""),
        "search_query":     raw.get("_query", ""),
    }


def clean_jobs(raw_jobs: list[dict]) -> list[dict]:
    """
    Clean a list of raw Adzuna dicts.
    Logs a warning and skips any record that raises an unexpected error.
    """
    cleaned = []
    for raw in raw_jobs:
        try:
            cleaned.append(clean_job(raw))
        except Exception as exc:
            logger.warning("Skipping job id=%s — clean_job raised: %s", raw.get("id"), exc)
    logger.info("Cleaned %d / %d records successfully", len(cleaned), len(raw_jobs))
    return cleaned


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import logging
    from src.ingestion.adzuna_client import AdzunaClient

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("\n── Pulling 2 pages of 'data scientist' to test cleaner ──")
    client = AdzunaClient()
    raw_jobs = list(client.search("data scientist", "Data", max_pages=2))
    print(f"Raw jobs fetched: {len(raw_jobs)}")

    cleaned = clean_jobs(raw_jobs)
    print(f"Cleaned jobs:     {len(cleaned)}")

    if cleaned:
        print("\nSample cleaned record:")
        sample = cleaned[0]
        for k, v in sample.items():
            if k == "description":
                print(f"  description: {str(v)[:100]}...")
            else:
                print(f"  {k}: {v}")

        print("\nSalary coverage:")
        with_salary = sum(1 for j in cleaned if j["salary_mid"] is not None)
        print(f"  {with_salary}/{len(cleaned)} jobs have salary data ({100*with_salary//len(cleaned) if cleaned else 0}%)")

        print("\nCity breakdown:")
        from collections import Counter
        cities = Counter(j["city"] for j in cleaned)
        for city, count in cities.most_common():
            print(f"  {city}: {count}")
