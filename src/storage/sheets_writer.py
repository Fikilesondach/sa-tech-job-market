"""
src/storage/sheets_writer.py
─────────────────────────────
Writes enriched job records to four Google Sheets tabs.

Tab behaviour:
    raw_jobs         — append-only, deduplicated by job_id
    weekly_snapshot  — fully rebuilt each run (overwrite)
    skills_frequency — fully rebuilt each run (overwrite)
    company_tracker  — fully rebuilt each run (overwrite)

Auth: Google Service Account JSON file, path from GOOGLE_SERVICE_ACCOUNT_PATH env var.
Sheet: identified by GOOGLE_SHEETS_ID env var.

Usage:
    from src.storage.sheets_writer import SheetsWriter
    writer = SheetsWriter()
    writer.write(cleaned_jobs)
"""

import os
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ── Tab names ──────────────────────────────────────────────────────────────────
TAB_RAW          = "raw_jobs"
TAB_WEEKLY       = "weekly_snapshot"
TAB_SKILLS       = "skills_frequency"
TAB_COMPANIES    = "company_tracker"

# ── Column schemas ─────────────────────────────────────────────────────────────
RAW_COLS = [
    "job_id", "source", "date_posted", "date_pulled",
    "title_raw", "title_clean",
    "company", "company_type",
    "location_display", "province", "city", "is_remote",
    "salary_min", "salary_max", "salary_mid", "salary_is_predicted",
    "category", "seniority", "is_graduate", "skills",
    "search_query", "redirect_url",
]

WEEKLY_COLS = [
    "week_start", "category",
    "job_count", "salary_median", "salary_coverage_pct",
    "top_city", "seniority_graduate", "seniority_junior",
    "seniority_mid", "seniority_senior", "seniority_lead",
]

SKILLS_COLS = ["week_start", "category", "skill", "count"]

COMPANY_COLS = ["week_start", "company", "company_type", "category", "job_count"]


class SheetsWriter:
    """Writes pipeline output to the configured Google Sheet."""

    def __init__(self) -> None:
        sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_PATH", "service_account.json")
        sheets_id = os.getenv("GOOGLE_SHEETS_ID", "")

        if not sheets_id:
            raise ValueError("GOOGLE_SHEETS_ID environment variable is not set.")
        if not os.path.exists(sa_path):
            raise FileNotFoundError(f"Service account file not found: {sa_path}")

        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        gc = gspread.authorize(creds)
        self._sh = gc.open_by_key(sheets_id)
        logger.info("Connected to Google Sheet: %s", self._sh.title)

    # ── Public ─────────────────────────────────────────────────────────────────

    def write(self, jobs: list[dict]) -> None:
        """Write enriched jobs to all four tabs."""
        if not jobs:
            logger.warning("No jobs to write — skipping all tabs.")
            return

        self._write_raw(jobs)
        all_raw = self._read_all_raw()
        self._write_weekly_snapshot(all_raw)
        self._write_skills_frequency(all_raw)
        self._write_company_tracker(all_raw)
        logger.info("All four tabs updated successfully.")

    # ── Tab: raw_jobs ──────────────────────────────────────────────────────────

    def _write_raw(self, jobs: list[dict]) -> None:
        """Append new jobs to raw_jobs, deduplicating by job_id."""
        ws = self._get_or_create_tab(TAB_RAW, RAW_COLS)

        # Fetch existing job_ids from column A (skip header)
        existing_ids: set[str] = set()
        try:
            col_a = ws.col_values(1)
            existing_ids = set(col_a[1:])  # skip header row
        except Exception:
            pass

        new_rows = []
        for job in jobs:
            if str(job.get("job_id", "")) not in existing_ids:
                new_rows.append(self._job_to_row(job, RAW_COLS))

        if new_rows:
            ws.append_rows(new_rows, value_input_option="RAW")
            logger.info("Appended %d new rows to %s (skipped %d duplicates)",
                        len(new_rows), TAB_RAW, len(jobs) - len(new_rows))
        else:
            logger.info("No new rows to append to %s — all %d jobs already present.",
                        TAB_RAW, len(jobs))

    def _read_all_raw(self) -> list[dict]:
        """Read all records from raw_jobs back as a list of dicts."""
        ws = self._sh.worksheet(TAB_RAW)
        records = ws.get_all_records()
        logger.info("Read %d records from %s for aggregate rebuilds", len(records), TAB_RAW)
        return records

    # ── Tab: weekly_snapshot ───────────────────────────────────────────────────

    def _write_weekly_snapshot(self, all_raw: list[dict]) -> None:
        """Rebuild weekly_snapshot: job count, salary stats, seniority mix per week × category."""
        ws = self._get_or_create_tab(TAB_WEEKLY, WEEKLY_COLS)

        # Group by (week_start, category)
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for job in all_raw:
            week = _week_start(job.get("date_pulled") or job.get("date_posted") or "")
            cat  = str(job.get("category") or "Unknown")
            groups[(week, cat)].append(job)

        rows = []
        for (week, cat), g_jobs in sorted(groups.items()):
            salaries = [float(j["salary_mid"]) for j in g_jobs
                        if j.get("salary_mid") not in (None, "", "None")]
            salary_median = round(statistics.median(salaries), 0) if salaries else ""
            salary_cov    = round(100 * len(salaries) / len(g_jobs), 1) if g_jobs else 0

            cities = [j.get("city", "") for j in g_jobs if j.get("city")]
            top_city = max(set(cities), key=cities.count) if cities else ""

            seniority_counts = _count_field(g_jobs, "seniority")
            rows.append([
                week, cat,
                len(g_jobs),
                salary_median,
                salary_cov,
                top_city,
                seniority_counts.get("Graduate", 0),
                seniority_counts.get("Junior", 0),
                seniority_counts.get("Mid", 0),
                seniority_counts.get("Senior", 0),
                seniority_counts.get("Lead", 0),
            ])

        self._overwrite_tab(ws, WEEKLY_COLS, rows)
        logger.info("Rebuilt %s with %d rows", TAB_WEEKLY, len(rows))

    # ── Tab: skills_frequency ──────────────────────────────────────────────────

    def _write_skills_frequency(self, all_raw: list[dict]) -> None:
        """Rebuild skills_frequency: skill counts per week × category."""
        ws = self._get_or_create_tab(TAB_SKILLS, SKILLS_COLS)

        # Aggregate skill counts
        counts: dict[tuple, int] = defaultdict(int)
        for job in all_raw:
            week   = _week_start(job.get("date_pulled") or job.get("date_posted") or "")
            cat    = str(job.get("category") or "Unknown")
            skills = str(job.get("skills") or "")
            for skill in skills.split("|"):
                skill = skill.strip()
                if skill:
                    counts[(week, cat, skill)] += 1

        rows = [
            [week, cat, skill, count]
            for (week, cat, skill), count in sorted(counts.items())
        ]

        self._overwrite_tab(ws, SKILLS_COLS, rows)
        logger.info("Rebuilt %s with %d rows (%d unique skill × week × category combos)",
                    TAB_SKILLS, len(rows), len(rows))

    # ── Tab: company_tracker ───────────────────────────────────────────────────

    def _write_company_tracker(self, all_raw: list[dict]) -> None:
        """Rebuild company_tracker: job counts per company × week × category."""
        ws = self._get_or_create_tab(TAB_COMPANIES, COMPANY_COLS)

        counts: dict[tuple, int] = defaultdict(int)
        meta: dict[tuple, str] = {}  # (week, company, cat) → company_type
        for job in all_raw:
            week    = _week_start(job.get("date_pulled") or job.get("date_posted") or "")
            company = str(job.get("company") or "Unknown")
            cat     = str(job.get("category") or "Unknown")
            ctype   = str(job.get("company_type") or "Unknown")
            key     = (week, company, cat)
            counts[key] += 1
            meta[key]    = ctype

        rows = [
            [week, company, meta[(week, company, cat)], cat, count]
            for (week, company, cat), count in sorted(counts.items())
        ]

        self._overwrite_tab(ws, COMPANY_COLS, rows)
        logger.info("Rebuilt %s with %d rows", TAB_COMPANIES, len(rows))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_or_create_tab(self, name: str, headers: list[str]) -> gspread.Worksheet:
        """Return worksheet by name, creating it with headers if it doesn't exist."""
        try:
            ws = self._sh.worksheet(name)
            # Ensure header row is correct even if tab already existed
            if ws.row_count == 0 or ws.row_values(1) != headers:
                ws.clear()
                ws.append_row(headers, value_input_option="RAW")
            return ws
        except gspread.WorksheetNotFound:
            ws = self._sh.add_worksheet(title=name, rows=5000, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
            logger.info("Created new tab: %s", name)
            return ws

    def _overwrite_tab(self, ws: gspread.Worksheet,
                       headers: list[str], rows: list[list]) -> None:
        """Clear a tab and rewrite header + all rows in one batch call."""
        ws.clear()
        all_rows = [headers] + rows
        if all_rows:
            ws.update(all_rows, value_input_option="RAW")

    @staticmethod
    def _job_to_row(job: dict, cols: list[str]) -> list:
        """Serialise a job dict to a flat list in column order."""
        row = []
        for col in cols:
            val = job.get(col)
            if val is None:
                row.append("")
            elif isinstance(val, bool):
                row.append(str(val))
            else:
                row.append(val)
        return row


# ── Helpers ────────────────────────────────────────────────────────────────────

def _week_start(date_str: str) -> str:
    """
    Return the ISO Monday of the week containing date_str (YYYY-MM-DD).
    Falls back to current week if date_str is missing or unparseable.
    """
    try:
        if date_str:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        else:
            dt = datetime.now(timezone.utc)
        monday = dt - __import__("datetime").timedelta(days=dt.weekday())
        return monday.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        monday = datetime.now(timezone.utc)
        monday = monday - __import__("datetime").timedelta(days=monday.weekday())
        return monday.strftime("%Y-%m-%d")


def _count_field(jobs: list[dict], field: str) -> dict[str, int]:
    """Count occurrences of each value in a field across a list of job dicts."""
    counts: dict[str, int] = defaultdict(int)
    for job in jobs:
        val = str(job.get(field) or "Unknown")
        counts[val] += 1
    return dict(counts)
