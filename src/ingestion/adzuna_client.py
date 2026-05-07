"""
src/ingestion/adzuna_client.py
──────────────────────────────
Adzuna API client for the SA Tech Job Market pipeline.

Pulls job listings from Adzuna's South Africa endpoint across all
configured tech categories. Handles pagination, rate limiting, retries,
and 6-month rolling window filtering.

Usage (standalone test):
    python -m src.ingestion.adzuna_client
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Generator

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

load_dotenv()
logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────────

BASE_URL = "https://api.adzuna.com/v1/api/jobs/za/search/{page}"
RESULTS_PER_PAGE = 50          # Adzuna max is 50
REQUEST_DELAY_SECONDS = 1.2    # stay well under rate limits
ROLLING_WINDOW_DAYS = 180      # 6-month lookback

# Search queries grouped by our dashboard categories.
# Each entry is (category_label, search_term).
# Multiple queries per category cast a wider net — Adzuna's `what` param
# does keyword matching on title + description.
SEARCH_QUERIES: list[tuple[str, str]] = [
    # Data
    ("Data",              "data analyst"),
    ("Data",              "data scientist"),
    ("Data",              "data engineer"),
    ("Data",              "BI developer"),
    ("Data",              "analytics engineer"),
    ("Data",              "business intelligence"),
    # Software Engineering
    ("Software Engineering", "software engineer"),
    ("Software Engineering", "software developer"),
    ("Software Engineering", "backend developer"),
    ("Software Engineering", "frontend developer"),
    ("Software Engineering", "full stack developer"),
    # Cloud & DevOps
    ("Cloud & DevOps",    "cloud engineer"),
    ("Cloud & DevOps",    "devops engineer"),
    ("Cloud & DevOps",    "site reliability engineer"),
    ("Cloud & DevOps",    "platform engineer"),
    # ML & AI
    ("ML & AI",           "machine learning engineer"),
    ("ML & AI",           "AI engineer"),
    ("ML & AI",           "MLOps engineer"),
    # Quant & Actuarial
    ("Quant & Actuarial", "actuarial analyst"),
    ("Quant & Actuarial", "quantitative analyst"),
    ("Quant & Actuarial", "credit risk analyst"),
    # Cybersecurity
    ("Cybersecurity",     "cybersecurity analyst"),
    ("Cybersecurity",     "information security engineer"),
    ("Cybersecurity",     "security operations"),
]


# ── Client ─────────────────────────────────────────────────────────────────────

class AdzunaClient:
    """
    Thin wrapper around the Adzuna /jobs/za/search endpoint.

    Responsibilities:
        - Authenticate with app_id + app_key from environment
        - Paginate through all result pages for a given query
        - Filter results to the rolling 6-month window
        - Deduplicate within a single pull run (by Adzuna job id)
        - Return raw Adzuna response dicts — no transformation here

    Transformation and enrichment happen downstream in cleaner.py
    and the enrichment modules.
    """

    def __init__(self):
        self.app_id  = os.environ["ADZUNA_APP_ID"]
        self.app_key = os.environ["ADZUNA_APP_KEY"]
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._seen_ids: set[str] = set()   # dedup within a pull run

    # ── Private helpers ────────────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get_page(self, query: str, page: int) -> dict:
        """
        Fetch a single page of results for `query`.
        Retries up to 4 times on network errors with exponential backoff.
        Raises requests.HTTPError on 4xx/5xx after retries exhausted.
        """
        url = BASE_URL.format(page=page)
        params = {
            "app_id":              self.app_id,
            "app_key":             self.app_key,
            "results_per_page":    RESULTS_PER_PAGE,
            "what":                query,
            "content-type":        "application/json",
            # Only return jobs with a salary — comment this out if you want
            # all jobs (salary coverage in SA is ~35-40% of listings)
            # "salary_include_unknown": 0,
        }
        response = self.session.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_posted_date(date_str: str | None) -> datetime | None:
        """Parse Adzuna's ISO 8601 created date string to a timezone-aware datetime."""
        if not date_str:
            return None
        try:
            # Adzuna returns e.g. "2024-11-15T12:34:56Z"
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("Could not parse date: %s", date_str)
            return None

    def _within_rolling_window(self, job: dict) -> bool:
        """Return True if the job was posted within the last ROLLING_WINDOW_DAYS."""
        posted = self._parse_posted_date(job.get("created"))
        if posted is None:
            return True   # include if we can't determine — cleaner will handle
        cutoff = datetime.now(timezone.utc) - timedelta(days=ROLLING_WINDOW_DAYS)
        return posted >= cutoff

    def _is_new(self, job: dict) -> bool:
        """Return True if we haven't seen this job id in the current pull run."""
        job_id = str(job.get("id", ""))
        if job_id in self._seen_ids:
            return False
        self._seen_ids.add(job_id)
        return True

    # ── Public interface ───────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        category_label: str,
        max_pages: int = 10,
    ) -> Generator[dict, None, None]:
        """
        Yield raw Adzuna job dicts for a single search query.

        Paginates until:
            - All pages are exhausted, OR
            - max_pages is reached (guards against runaway pagination), OR
            - A page contains no results

        Each yielded dict is the raw Adzuna result with two fields injected:
            _category   — the category_label passed in
            _query      — the search term that produced this result
            _pulled_at  — ISO timestamp of this pull run

        Args:
            query:          Search term (e.g. "data scientist")
            category_label: Human-readable category (e.g. "Data")
            max_pages:      Safety cap on pages fetched per query
        """
        pulled_at = datetime.now(timezone.utc).isoformat()
        total_yielded = 0

        for page in range(1, max_pages + 1):
            logger.info("  Fetching page %d for query: '%s'", page, query)

            try:
                data = self._get_page(query, page)
            except requests.HTTPError as exc:
                logger.error("HTTP error on page %d for '%s': %s", page, query, exc)
                break

            results: list[dict] = data.get("results", [])

            if not results:
                logger.debug("  No results on page %d — stopping.", page)
                break

            page_yielded = 0
            for job in results:
                if not self._within_rolling_window(job):
                    # Adzuna returns newest first, so once we hit an old job
                    # the rest of this page and all subsequent pages are older.
                    # Stop paginating for this query.
                    logger.info(
                        "  Hit 6-month boundary on page %d — stopping pagination.",
                        page,
                    )
                    return

                if not self._is_new(job):
                    continue   # skip duplicate within this run

                # Inject pipeline metadata before yielding
                job["_category"]  = category_label
                job["_query"]     = query
                job["_pulled_at"] = pulled_at

                yield job
                page_yielded += 1
                total_yielded += 1

            logger.info("  Page %d: %d new jobs yielded", page, page_yielded)

            # Adzuna's count field tells us total available results.
            # If we've already paged through all of them, stop early.
            total_count = data.get("count", 0)
            if page * RESULTS_PER_PAGE >= total_count:
                logger.debug("  All %d results fetched — stopping.", total_count)
                break

            # Be a good API citizen — pause between pages
            time.sleep(REQUEST_DELAY_SECONDS)

        logger.info("  Query '%s' complete — %d jobs total", query, total_yielded)

    def pull_all(self, max_pages_per_query: int = 10) -> list[dict]:
        """
        Run all SEARCH_QUERIES and return a deduplicated list of raw job dicts.

        This is the main entry point called by pipeline.py.

        Args:
            max_pages_per_query: Safety cap per query (default 10 = 500 results max)

        Returns:
            List of raw Adzuna job dicts, each tagged with _category, _query,
            and _pulled_at. Deduplicated by Adzuna job id across all queries.
        """
        # Reset dedup state at the start of each full pull
        self._seen_ids = set()

        all_jobs: list[dict] = []

        logger.info(
            "Starting full pull — %d queries, max %d pages each",
            len(SEARCH_QUERIES),
            max_pages_per_query,
        )

        for i, (category_label, query) in enumerate(SEARCH_QUERIES, start=1):
            logger.info(
                "[%d/%d] Category: '%s' | Query: '%s'",
                i, len(SEARCH_QUERIES), category_label, query,
            )
            jobs = list(
                self.search(
                    query=query,
                    category_label=category_label,
                    max_pages=max_pages_per_query,
                )
            )
            all_jobs.extend(jobs)

            # Pause between queries (different from between-page pause)
            if i < len(SEARCH_QUERIES):
                time.sleep(REQUEST_DELAY_SECONDS * 2)

        logger.info(
            "Pull complete — %d unique jobs across %d queries",
            len(all_jobs),
            len(SEARCH_QUERIES),
        )
        return all_jobs


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    client = AdzunaClient()

    # Test with a single query before running the full pull
    print("\n── Single query test: 'data scientist' ──")
    test_jobs = list(client.search("data scientist", "Data", max_pages=2))

    if not test_jobs:
        print("No results returned — check your API credentials in .env")
    else:
        sample = test_jobs[0]
        print(f"\nTotal jobs returned: {len(test_jobs)}")
        print(f"\nSample job fields:")
        for key, value in sample.items():
            if key == "description":
                print(f"  description: {str(value)[:120]}...")
            else:
                print(f"  {key}: {value}")
