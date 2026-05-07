"""
src/pipeline.py
───────────────
Full pipeline orchestrator for the SA Tech Job Market Analytics project.

Stages (in order):
    1. Ingest   — pull jobs from Adzuna API
    2. Clean    — normalise raw dicts into flat typed records
    3. Enrich   — classify category, seniority, skills, company type
    4. Store    — write to Google Sheets (skipped in --dry-run mode)

CLI:
    PYTHONPATH=. python -m src.pipeline                  # full run
    PYTHONPATH=. python -m src.pipeline --dry-run        # skip Sheets write
    PYTHONPATH=. python -m src.pipeline --max-pages 2    # limit API pages (dev)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# ── Logging — configure before any module imports so all logger.info() calls land
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("pipeline")


def _stage(name: str) -> None:
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("STAGE: %s", name)
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def run(dry_run: bool = False, max_pages: int = 10) -> dict:
    """
    Execute the full pipeline and return a summary dict.

    Args:
        dry_run:   If True, skips the Sheets write stage.
        max_pages: Max Adzuna result pages per query (use 2 for dev/testing).

    Returns:
        Summary dict with job counts, timing, and stage results.
    """
    pipeline_start = time.time()
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("Pipeline started at %s  |  dry_run=%s  max_pages=%d",
                run_ts, dry_run, max_pages)

    # ── Stage 1: Ingest ───────────────────────────────────────────────────────
    _stage("1 / 4 — Ingest (Adzuna API)")
    t0 = time.time()

    from src.ingestion.adzuna_client import AdzunaClient
    client = AdzunaClient()
    raw_jobs = client.pull_all(max_pages_per_query=max_pages)
    n_raw = len(raw_jobs)

    logger.info("Fetched %d raw jobs in %.1fs", n_raw, time.time() - t0)

    if n_raw == 0:
        logger.error("No jobs returned from Adzuna — aborting pipeline.")
        sys.exit(1)

    # ── Stage 2: Clean ────────────────────────────────────────────────────────
    _stage("2 / 4 — Clean")
    t0 = time.time()

    from src.cleaning.cleaner import clean_jobs
    cleaned = clean_jobs(raw_jobs)
    n_cleaned = len(cleaned)

    logger.info("Cleaned %d / %d records in %.1fs", n_cleaned, n_raw, time.time() - t0)

    # ── Stage 3: Enrich ───────────────────────────────────────────────────────
    _stage("3 / 4 — Enrich")
    t0 = time.time()

    from src.enrichment.category_classifier import enrich_category
    from src.enrichment.seniority_classifier import enrich_seniority
    from src.enrichment.skills_extractor import enrich_skills
    from src.enrichment.company_classifier import enrich_company_type

    cleaned = enrich_category(cleaned)
    logger.info("Category enrichment complete")

    cleaned = enrich_seniority(cleaned)
    logger.info("Seniority enrichment complete")

    cleaned = enrich_skills(cleaned)
    logger.info("Skills enrichment complete")

    cleaned = enrich_company_type(cleaned)
    logger.info("Company type enrichment complete")

    logger.info("Enrichment finished in %.1fs", time.time() - t0)

    # ── Quick enrichment summary ──────────────────────────────────────────────
    def _count(field: str) -> dict:
        counts: dict[str, int] = {}
        for job in cleaned:
            val = str(job.get(field, "Unknown"))
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))

    logger.info("Category breakdown:  %s", _count("category"))
    logger.info("Seniority breakdown: %s", _count("seniority"))
    logger.info("Company type breakdown: %s", _count("company_type"))

    with_salary = sum(1 for j in cleaned if j.get("salary_mid") is not None)
    logger.info("Salary coverage: %d / %d (%.0f%%)",
                with_salary, n_cleaned, 100 * with_salary / n_cleaned if n_cleaned else 0)

    # ── Stage 4: Store ────────────────────────────────────────────────────────
    _stage("4 / 4 — Store (Google Sheets)")

    if dry_run:
        logger.info("DRY RUN — Sheets write skipped.")
        sheets_result = "skipped"
    else:
        t0 = time.time()
        from src.storage.sheets_writer import SheetsWriter
        writer = SheetsWriter()
        writer.write(cleaned)
        sheets_result = "ok"
        logger.info("Sheets write complete in %.1fs", time.time() - t0)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.time() - pipeline_start
    summary = {
        "run_ts":        run_ts,
        "dry_run":       dry_run,
        "n_raw":         n_raw,
        "n_cleaned":     n_cleaned,
        "with_salary":   with_salary,
        "sheets":        sheets_result,
        "elapsed_s":     round(elapsed, 1),
    }

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("PIPELINE COMPLETE in %.1fs", elapsed)
    logger.info("Summary: %s", summary)
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SA Tech Job Market — Adzuna ingestion pipeline"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all stages but skip writing to Google Sheets",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        metavar="N",
        help="Max Adzuna result pages per query (default: 10, use 2 for dev)",
    )
    args = parser.parse_args()

    run(dry_run=args.dry_run, max_pages=args.max_pages)
