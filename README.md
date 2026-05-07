# SA Tech Job Market Analytics

A fully automated pipeline that tracks the South African technology job market across six role categories — pulling live listings from the Adzuna API every week, enriching them with salary, seniority, skills, and company classification, and surfacing insights in a Looker Studio dashboard.

Built as a portfolio project demonstrating end-to-end data engineering: API ingestion, structured enrichment, Google Sheets as a lightweight warehouse, and GitHub Actions for orchestration.

---

## Live dashboard

[View on Looker Studio →](https://datastudio.google.com/reporting/6e82212d-2e5a-42a2-a239-278ff05c3c72)

Refreshes automatically every Sunday at 06:00 SAST.

---

## What it tracks

| Category | Example roles |
|---|---|
| ML & AI | Data Scientist, ML Engineer, MLOps |
| Quant & Actuarial | Actuarial Analyst, Credit Risk, Quantitative Analyst |
| Cloud & DevOps | Cloud Engineer, Platform Engineer, SRE |
| Data | Data Analyst, Data Engineer, BI Developer |
| Software Engineering | Backend, Frontend, Full Stack |
| Cybersecurity | Security Analyst, SOC Analyst, Penetration Tester |

For each job the pipeline extracts:
- **Salary** — min/max/midpoint in annual ZAR (where disclosed, ~24% of SA listings)
- **Seniority** — Graduate / Junior / Mid / Senior / Lead
- **Skills** — matched against an 80-skill taxonomy across 9 domains
- **Company type** — Corporate / Startup / Consulting / Recruiting
- **Location** — province and city, normalised to standard SA metro names

---

## Architecture

```
Adzuna API
    │
    ▼
src/ingestion/adzuna_client.py   — 24 queries × up to 10 pages, 6-month rolling window
    │
    ▼
src/cleaning/cleaner.py          — flatten, normalise titles, parse salaries, detect remote
    │
    ▼
src/enrichment/
    ├── category_classifier.py   — ML & AI / Quant / Cloud / Data / SWE / Cyber
    ├── seniority_classifier.py  — Graduate → Junior → Mid → Senior → Lead
    ├── skills_extractor.py      — 80-skill keyword taxonomy (YAML-driven)
    └── company_classifier.py    — 90 known SA companies lookup table
    │
    ▼
src/storage/sheets_writer.py     — 4-tab Google Sheets write (append + 3 aggregate rebuilds)
    │
    ▼
Google Sheets → Looker Studio dashboard
```

**Orchestration:** GitHub Actions cron (`0 4 * * 0`) — runs every Sunday, full logs visible in the Actions tab.

---

## Repository structure

```
sa-tech-job-market/
├── .github/
│   └── workflows/
│       └── weekly_pull.yml        # GitHub Actions workflow
├── config/
│   ├── categories.yaml            # 7 categories with keyword lists (priority-ordered)
│   └── skills_taxonomy.yaml       # ~80 skills across 9 domains
├── src/
│   ├── ingestion/
│   │   └── adzuna_client.py       # Adzuna API client with retry + deduplication
│   ├── cleaning/
│   │   └── cleaner.py             # Raw → flat typed pipeline records
│   ├── enrichment/
│   │   ├── category_classifier.py
│   │   ├── seniority_classifier.py
│   │   ├── skills_extractor.py
│   │   └── company_classifier.py
│   ├── storage/
│   │   └── sheets_writer.py       # Google Sheets writer (4 tabs)
│   └── pipeline.py                # Orchestrator — ingest → clean → enrich → store
├── .env.example
├── requirements.txt
└── README.md
```

---

## Google Sheets schema

Four tabs written on every pipeline run:

**`raw_jobs`** — append-only, deduplicated by `job_id`

| Field | Description |
|---|---|
| `job_id` | Adzuna unique ID |
| `title_clean` | Normalised job title |
| `company` | Employer name |
| `company_type` | Corporate / Startup / Consulting / Recruiting / Unknown |
| `city` / `province` | Normalised SA location |
| `is_remote` | Boolean — remote/hybrid detected from title + description |
| `salary_min/max/mid` | Annual ZAR, null if not disclosed |
| `category` | One of the 6 dashboard categories |
| `seniority` | Graduate / Junior / Mid / Senior / Lead |
| `skills` | Pipe-delimited matched skills e.g. `Python\|SQL\|AWS` |
| `date_posted` | YYYY-MM-DD from Adzuna |
| `date_pulled` | YYYY-MM-DD of ingestion run |

**`weekly_snapshot`** — rebuilt each run; one row per week × category

**`skills_frequency`** — rebuilt each run; one row per week × category × skill

**`company_tracker`** — rebuilt each run; one row per week × company × category

---

## Running locally

### Prerequisites

- Python 3.12+
- Adzuna API credentials — [register free at developer.adzuna.com](https://developer.adzuna.com)
- Google Cloud service account with Sheets + Drive APIs enabled

### Setup

```bash
git clone https://github.com/Fikilesondach/sa-tech-job-market.git
cd sa-tech-job-market
pip install -r requirements.txt
cp .env.example .env
# fill in ADZUNA_APP_ID, ADZUNA_APP_KEY, GOOGLE_SHEETS_ID, GOOGLE_SERVICE_ACCOUNT_PATH
```

### Run

```bash
# dry run — all stages, skip Sheets write
PYTHONPATH=. python -m src.pipeline --dry-run --max-pages 2

# full run
PYTHONPATH=. python -m src.pipeline

# limit pages for faster dev iteration
PYTHONPATH=. python -m src.pipeline --dry-run --max-pages 2
```

### Environment variables

| Variable | Description |
|---|---|
| `ADZUNA_APP_ID` | Adzuna API app ID |
| `ADZUNA_APP_KEY` | Adzuna API app key |
| `GOOGLE_SHEETS_ID` | Spreadsheet ID from the Sheets URL |
| `GOOGLE_SERVICE_ACCOUNT_PATH` | Path to service account JSON (default: `service_account.json`) |

---

## Extending the pipeline

**Add a new skill:** edit `config/skills_taxonomy.yaml` — add the skill name under the appropriate group. The extractor compiles patterns at import time so no code changes needed.

**Add a new category keyword:** edit `config/categories.yaml` — add to the relevant category list. Priority order is determined by list order in the file.

**Add a known company:** edit `src/enrichment/company_classifier.py` — append a `(substring, type)` tuple to `COMPANY_LOOKUP`. Recruiting entries should go first in the list.

**Add a new search query:** edit `src/ingestion/adzuna_client.py` — add to the `QUERIES` list as `("search term", "Category")`.

---

## Technical notes

- **Deduplication:** the Adzuna client deduplicates by job ID within a single pull run. The Sheets writer deduplicates against existing `job_id` values in `raw_jobs` on every append, so running the pipeline twice in one week won't double-count.
- **6-month window:** the client stops paginating any query the moment it encounters a job older than 180 days, preserving API quota.
- **Retry logic:** network errors retry up to 4 times with exponential backoff via `tenacity`. A 1.2s delay between pages and 2.4s between queries avoids rate limiting.
- **Aggregate tabs:** `weekly_snapshot`, `skills_frequency`, and `company_tracker` are fully rebuilt from `raw_jobs` on every run — they always reflect the complete historical dataset, not just the latest pull.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| API client | `requests`, `tenacity` |
| Data processing | `pandas`-free — pure Python dicts for pipeline speed |
| Config | `PyYAML` |
| Storage | `gspread`, `google-auth` |
| Orchestration | GitHub Actions |
| Dashboard | Looker Studio |
| Environment | `python-dotenv` |

---

## Author

**Fikile Sondach** — Senior Business Intelligence Analyst, Cape Town  
BSc Actuarial Science · IFoA CT1–CT8 exemptions · Python · SQL · Power BI  
[GitHub](https://github.com/Fikilesondach) · [CV](https://fikilesondach.github.io)
