# TruthLayer

A reusable source of truth for businesses whose data lives in systems that disagree.
It ingests files, works out which records describe the same real thing, reconciles their
attributes according to who is authoritative for what, keeps every original value as evidence,
and puts anything it cannot settle in front of a human.

First client: **Harborview Care Group** (HR roster, payroll, licensing, printed schedule PDF).
Second client, to show it generalises: **Northwind Outfitters** (CRM, billing, shipping), with no code changes.

**Full write-up:** [GUIDE.md](GUIDE.md) covers why it works, how it decides what to flag, the tech stack, data model,
scaling, where AI fits, learned rules, security, roadmap and the pitch. Key design decisions are in [DECISIONS.md](DECISIONS.md).

## Run it

```bash
pip install -r requirements.txt
uvicorn truthlayer.app:app --port 8000        # open http://localhost:8000
```

1. **Pick a company** (or add one). Each company is its own workspace with its own files, setup, data and problem log.
2. **New company → guided setup.** Drop that company's CSV, Excel or PDF exports (PDF tables and printed schedule grids are read automatically). TruthLayer works out which file is the
   master list, how the other files link to it (by comparing actual values), which fields to compare and which checks
   to run. You review the suggestions on one screen and save.
3. **Judging day:** choose "Start with the Harborview setup, no data" and drop in the four real files.
4. **Demo companies** are one click away: Harborview (healthcare, uses the healthcare pack for the schedule PDF),
   Northwind (retail) and Apex (manufacturing). The last two are set up automatically from their CSVs.

Other ways to run it:

```bash
python -m truthlayer.cli --config clients/harborview.yaml sample_data/*     # CLI fallback, prints every flag
python tests/test_engine.py                                                 # engine: planted problems are caught
python tests/test_setup.py                                                  # guided setup works for 3 industries
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/workspaces` | list companies and demo presets / create a company (`{name}` or `{preset}`) |
| POST | `/api/w/{company}/setup/files` | add files to the guided setup; returns the suggested setup |
| POST | `/api/w/{company}/setup/save` | save the (edited) suggestions and build the source of truth |
| POST | `/api/w/{company}/files` | upload more files later (auto-detected against the company's setup) |
| GET | `/api/w/{company}/summary`, `/entities`, `/entities/{key}`, `/issues` | the source of truth and review queue |
| POST | `/api/w/{company}/issues/{id}/resolve` | record a decision |
| GET | `/api/w/{company}/views/{name}` | business views (healthcare pack: staffing, credentials, coverage; any company: expiring, totals) |
| GET | `/api/w/{company}/tables`, `/tables/{name}` | the read-only SQL views |
| GET | `/api/w/{company}/events` | audit log |

## Onboarding a new client

Usually: create the company and drop its files. The guided setup writes `workspaces/<company>/config.yaml`.
You can also write that file by hand (see `clients/harborview.yaml`) for things the setup can't infer, such as
industry packs (PDF schedule reader, staffing report, shift coverage).
