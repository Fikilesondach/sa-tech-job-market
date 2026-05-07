"""
src/enrichment/company_classifier.py
──────────────────────────────────────
Classifies each company as Corporate / Startup / Consulting / Recruiting / Unknown
using a lookup of known South African companies.

Lookup is case-insensitive and uses substring matching so partial names
(e.g. "Capitec Bank" matching "Capitec") are handled correctly.

Usage:
    from src.enrichment.company_classifier import classify_company
    company_type = classify_company("Capitec Bank Limited")
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── SA Company Lookup ──────────────────────────────────────────────────────────
# Format: (substring_to_match, company_type)
# Evaluated in order — first match wins.
# Add new companies here as the dataset grows.

COMPANY_LOOKUP: list[tuple[str, str]] = [

    # ── Recruiting / Staffing (check first — avoid misclassifying)
    ("communicate recruiting",      "Recruiting"),
    ("datafin",                     "Recruiting"),
    ("set consulting",              "Recruiting"),
    ("kontak",                      "Recruiting"),
    ("sydsen",                      "Recruiting"),
    ("network recruitment",         "Recruiting"),
    ("hire resolve",                "Recruiting"),
    ("job crystal",                 "Recruiting"),
    ("e-merge",                     "Recruiting"),
    ("parvana",                     "Recruiting"),
    ("talent101",                   "Recruiting"),
    ("manpower",                    "Recruiting"),
    ("adcorp",                      "Recruiting"),
    ("kelly",                       "Recruiting"),
    ("iitpsa",                      "Recruiting"),
    ("michael page",                "Recruiting"),
    ("robert walters",              "Recruiting"),
    ("hays",                        "Recruiting"),
    ("the talent room",             "Recruiting"),
    ("best it recruitment",         "Recruiting"),

    # ── Big Consulting & Advisory
    ("deloitte",                    "Consulting"),
    ("pwc",                         "Consulting"),
    ("ernst & young",               "Consulting"),
    ("ey ",                         "Consulting"),
    ("kpmg",                        "Consulting"),
    ("accenture",                   "Consulting"),
    ("mckinsey",                    "Consulting"),
    ("bcg",                         "Consulting"),
    ("boston consulting",           "Consulting"),
    ("bain",                        "Consulting"),
    ("thoughtworks",                "Consulting"),
    ("dvt",                         "Consulting"),
    ("bbd",                         "Consulting"),
    ("ioco",                        "Consulting"),
    ("synthesis",                   "Consulting"),
    ("entelect",                    "Consulting"),
    ("electrum",                    "Consulting"),
    ("blue label",                  "Consulting"),
    ("oltio",                       "Consulting"),
    ("bcx",                         "Consulting"),
    ("dimension data",              "Consulting"),
    ("ntt",                         "Consulting"),
    ("wipro",                       "Consulting"),
    ("infosys",                     "Consulting"),
    ("tcs",                         "Consulting"),
    ("tata consultancy",            "Consulting"),

    # ── Financial Services Corporates
    ("capitec",                     "Corporate"),
    ("standard bank",               "Corporate"),
    ("fnb",                         "Corporate"),
    ("first national bank",         "Corporate"),
    ("nedbank",                     "Corporate"),
    ("absa",                        "Corporate"),
    ("old mutual",                  "Corporate"),
    ("sanlam",                      "Corporate"),
    ("santam",                      "Corporate"),
    ("momentum",                    "Corporate"),
    ("discovery",                   "Corporate"),
    ("liberty",                     "Corporate"),
    ("investec",                    "Corporate"),
    ("rand merchant",               "Corporate"),
    ("rmb",                         "Corporate"),
    ("firstrand",                   "Corporate"),
    ("african bank",                "Corporate"),
    ("bidvest",                     "Corporate"),
    ("psg",                         "Corporate"),
    ("coronation",                  "Corporate"),
    ("allan gray",                  "Corporate"),
    ("ninety one",                  "Corporate"),
    ("swiss re",                    "Corporate"),
    ("munich re",                   "Corporate"),
    ("hannover re",                 "Corporate"),
    ("guardrisk",                   "Corporate"),
    ("hollard",                     "Corporate"),
    ("king price",                  "Corporate"),

    # ── Telecoms
    ("mtn",                         "Corporate"),
    ("vodacom",                     "Corporate"),
    ("telkom",                      "Corporate"),
    ("cell c",                      "Corporate"),
    ("rain",                        "Startup"),    # rain is more startup-ish

    # ── Retail & Consumer
    ("shoprite",                    "Corporate"),
    ("pick n pay",                  "Corporate"),
    ("woolworths",                  "Corporate"),
    ("mr price",                    "Corporate"),
    ("foschini",                    "Corporate"),
    ("truworths",                   "Corporate"),
    ("massmart",                    "Corporate"),
    ("game",                        "Corporate"),
    ("makro",                       "Corporate"),
    ("checkers",                    "Corporate"),

    # ── Mining, Energy & Industrial
    ("sasol",                       "Corporate"),
    ("eskom",                       "Corporate"),
    ("transnet",                    "Corporate"),
    ("anglo american",              "Corporate"),
    ("bhp",                         "Corporate"),
    ("glencore",                    "Corporate"),
    ("sibanye",                     "Corporate"),
    ("impala",                      "Corporate"),
    ("de beers",                    "Corporate"),

    # ── Tech Corporates
    ("takealot",                    "Corporate"),
    ("naspers",                     "Corporate"),
    ("prosus",                      "Corporate"),
    ("multichoice",                 "Corporate"),
    ("dstv",                        "Corporate"),
    ("tyme bank",                   "Corporate"),
    ("tymebank",                    "Corporate"),

    # ── SA Tech Startups & Scale-ups
    ("offerzen",                    "Startup"),
    ("jumo",                        "Startup"),
    ("yoco",                        "Startup"),
    ("ozow",                        "Startup"),
    ("peach payments",              "Startup"),
    ("luno",                        "Startup"),
    ("mama money",                  "Startup"),
    ("nomanini",                    "Startup"),
    ("flutterwave",                 "Startup"),
    ("paystack",                    "Startup"),
    ("carry1st",                    "Startup"),
    ("sweep south",                 "Startup"),
    ("sweep",                       "Startup"),
    ("aerobotics",                  "Startup"),
    ("custos",                      "Startup"),
    ("prodigy finance",             "Startup"),
    ("ukheshe",                     "Startup"),
    ("bettr",                       "Startup"),
    ("wonga",                       "Startup"),
    ("lula",                        "Startup"),
    ("franc",                       "Startup"),
    ("revix",                       "Startup"),
    ("wigroup",                     "Startup"),
    ("hyperion",                    "Startup"),
    ("terrafinance",                "Startup"),
    ("wethinkcode",                 "Startup"),
    ("22seven",                     "Startup"),
    ("clickatell",                  "Startup"),
    ("inprop",                      "Startup"),
    ("iono.fm",                     "Startup"),
    ("zindi",                       "Startup"),
]

# Compile substring patterns once at module load
_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(re.escape(substring), re.IGNORECASE), company_type)
    for substring, company_type in COMPANY_LOOKUP
]


def classify_company(company_name: str) -> str:
    """
    Return company type for a given company name string.

    Args:
        company_name: Company display name from Adzuna

    Returns:
        One of: "Corporate", "Startup", "Consulting", "Recruiting", "Unknown"
    """
    if not company_name or company_name == "Unknown":
        return "Unknown"

    for pattern, company_type in _COMPILED:
        if pattern.search(company_name):
            return company_type

    return "Unknown"


def enrich_company_type(jobs: list[dict]) -> list[dict]:
    """Apply company classification to a list of cleaned job dicts. Mutates in place."""
    for job in jobs:
        job["company_type"] = classify_company(job.get("company", ""))

    # Log coverage
    type_counts: dict[str, int] = {}
    for job in jobs:
        ct = job["company_type"]
        type_counts[ct] = type_counts.get(ct, 0) + 1
    logger.info("Company types assigned: %s", type_counts)

    return jobs
