# TruthLayer

A reusable source of truth for businesses whose data lives in systems that disagree.
It ingests files, works out which records describe the same real thing, reconciles their
attributes according to who is authoritative for what, keeps every original value as evidence,
and puts anything it cannot settle in front of a human.

First client: **Harborview Care Group** (HR roster, payroll, licensing, printed schedule PDF).
Second client, to show it generalises: **Northwind Outfitters** (CRM, billing, shipping), with no code changes.

## Run it

```bash
pip install -r requirements.txt
uvicorn truthlayer.app:app --port 8000        # open http://localhost:8000
```

Go to **Ingest**, drop the four files (or click *Load sample data*), then click **Process data**.

Other ways to run it:

```bash
# CLI fallback: prints every flag, no browser needed
python -m truthlayer.cli path/to/*.csv path/to/schedule.pdf

# evaluate as of a fixed date (expiry and staleness rules use this)
python -m truthlayer.cli sample_data/* --as-of 2026-10-04

# the second client, same engine
TRUTHLAYER_CONFIG=clients/northwind_retail.yaml TRUTHLAYER_SAMPLE=sample_data_retail \
  TRUTHLAYER_DB=retail.db uvicorn truthlayer.app:app --port 8001

# tests: every planted problem is caught, and mangled exports still load
python tests/test_engine.py
```

## Layout

```
truthlayer/
  ingest.py     header mapping (synonyms + fuzzy), CSV/TSV/XLSX, PDF weekly-grid connector
  normalize.py  dates, phones, IDs, license numbers, coded values, person names + nicknames
  engine.py     resolve entities → reconcile attributes → run rules → persist; human decisions
  rules.py      generic rule types (presence, expiration, staleness, activity-after-expiry,
                aggregate comparison, duplicate entity, date order)
  views.py      business views: staffing report, credentials, referral readiness
  store.py      SQLite schema (portable SQL)
  app.py        REST API + UI host
  cli.py        command-line runner
clients/
  harborview.yaml        everything specific to Harborview
  northwind_retail.yaml  a different industry, same engine
static/index.html        the UI (no build step)
sample_data/             messy Harborview test files with planted problems (see tools/make_sample_data.py)
tests/test_engine.py
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/files` | upload one or more files (auto-detects the source; `source` form field to override) |
| POST | `/api/process` | rebuild the source of truth from all active files |
| GET | `/api/summary` | counts and trust metrics |
| GET | `/api/entities`, `/api/entities/{key}` | canonical records, with evidence, records, issues and timeline |
| GET | `/api/issues?status=&severity=&category=` | review queue |
| POST | `/api/issues/{id}/resolve` | `{decision: choose\|confirm\|reject\|acknowledge\|dismiss\|reopen, value, note, reviewer}` |
| GET | `/api/views/{staffing\|credentials\|coverage}` | business views |
| GET | `/api/export/{staffing\|issues\|entities}.csv` | exports |
| GET | `/api/events` | audit log |

## Onboarding a new client

Write a YAML file like `clients/harborview.yaml`:

1. **entity**: what a "thing" is and which source is the system of record.
2. **reference**: vocabularies (every spelling → one code).
3. **sources**: fields, types and synonyms for each file, and how each one links to an entity.
4. **attributes**: which source fields feed each canonical attribute, and who is authoritative.
5. **rules**: pick from the generic rule types and set thresholds.

A new file format, such as a different PDF layout, is a new connector in `ingest.py`. Nothing else changes.
