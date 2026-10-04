# TruthLayer: key decisions and why

## 1. Detect and explain conflicts. Never silently fix them.
Every value from every file is stored exactly as received (`records.raw_json`), next to its
normalized form and the file and row it came from. A canonical value is always backed by
evidence rows. When sources disagree, we show the authoritative value **provisionally**,
mark it `CONFLICT`, and open an issue. A human answers the question; we don't hide it.

## 2. Authority is per field and configured. It is not decided by majority vote.
HR decides role and facility, licensing decides license dates, payroll decides hours paid.
Majority vote fails in practice. Example: Daniel Kim is at Riverdale in HR but at Bayside in
both payroll and the schedule. HR still wins provisionally, and the issue says that 2 of 3
sources disagree. That is often the clue that HR is the stale system.

## 3. Entity resolution is the hard part, so it is layered and cautious.
Payroll and the schedule carry **no employee ID**, only names written differently
("REYES, SOFIA", "Marc Bell"). Matching works in this order:
exact key (license number) → exact name → nickname table (Marc ↔ Marcus) → initials and
swapped names → fuzzy similarity, with role and facility as tie-breakers.
- ≥ 0.92: linked automatically, with the method and score recorded on the record.
- 0.75–0.92: linked **provisionally**. The record is shown as evidence but **excluded from
  reconciliation** until a human confirms it ("Sofia Rayes" in payroll).
- Below that, records with the same name are grouped into an **unverified** entity.
  Someone paid and scheduled but missing from HR (Carmen Diaz) becomes a visible person
  with an open critical issue, not a dropped row.

## 4. Human decisions are keyed to the question, not the row.
A decision is stored against a fingerprint of the issue, which includes the conflicting values.
Processing is a full rebuild from the raw data, so decisions are re-applied every time.
If the data changes (HR is corrected, or a third value appears), the old decision no longer
matches. The issue then clears or reopens, and the audit log records which. Confirmed
matches become match overrides, so they hold across new payroll files.

## 5. Generic engine, client-specific YAML.
The engine has no healthcare words in it. Sources, field synonyms, vocabularies, authority,
thresholds and rules all live in `clients/harborview.yaml`. Rules are generic types
(`expiration`, `staleness`, `activity_after_expiry`, `aggregate_compare`, `presence`,
`duplicate_entity`, ...). We proved this by onboarding a retail client
(`clients/northwind_retail.yaml`: CRM, billing, shipping) with **zero code changes**.
It finds the same kinds of problems: a duplicate customer, a conflicting email, billed vs
shipped value, and a buyer who isn't in the CRM.

## 6. Built to survive files we haven't seen.
- Columns are matched by name, synonym and fuzzy similarity, in any order. Title rows above
  the header, blank rows, `;` or tab delimiters, cp1252 encoding and XLSX are all handled.
  Extra columns are kept raw and reported.
- Values that can't be parsed (bad dates, unknown facility codes) are flagged. We never guess them.
- The schedule PDF goes through a connector that reads the table grid, or falls back to text
  lines when there are no ruled lines. It reads the shift legend from the footer ("7a-7p is
  12 hours"). A shift not in the legend has its hours computed from its times and is flagged.
- Tests mangle the sample files in all of these ways and require the same findings.

## 7. Business views read only from the source of truth.
- **State staffing report:** paid hours by week, facility and role, using the **canonical
  role**, not the payroll job code, and compared with scheduled hours. Every hour tied to an
  open question (duplicate pay, role conflict, paid ≠ scheduled, unverified person) is counted
  as *flagged*, with the reason. "52% report-ready" is a number you can act on: fix those
  hours, export the rest. The goal is to replace weeks of reconciliation with a reviewable list.
- **Credentials:** authoritative expiry, what each system claims, verification age, and
  whether someone worked after expiry (the schedule shows Aisha working the day after her
  license lapsed).
- **Referral readiness:** for each facility, day and shift, is a licensed nurse on duty
  whose record has no critical problem? That is the first question when a hospital calls,
  answered in seconds.
- **Vendor paperwork** wasn't in the data we were given. The `expiration` and `staleness` rule
  types apply to any document with a date. Adding a vendor source is a YAML block.

## 8. Metrics that mean something.
We avoid a single made-up "data health" score. The UI shows three numbers, each with its
definition: records linked to a verified entity, canonical fields where sources agree,
and entities with no critical issue.

## 9. Stack chosen for a live demo.
Python, FastAPI and SQLite: one `pip install`, one command, no Docker or Node build. The
schema is plain SQL and moves to Postgres with a connection change. The UI is a single static
page that calls the same REST API any downstream app would use.

## 10. Where AI fits (deliberately not in the core).
Every flag comes from deterministic, explainable code, so the same input always gives the same
answer. That matters for compliance data. AI fits in two places later, both bounded:
1. Suggesting matches in the 0.75–0.92 band. It would only suggest; a human still confirms.
2. A natural-language front end over the existing API. The database stays the authority.

## 11. Any company, any industry: workspaces + guided setup
- **One workspace per company.** Each has its own folder, database, setup and problem log. Nothing is shared, so
  a mistake in one client can't leak into another. (In production this becomes a tenant id in Postgres.)
- **The setup is inferred from the data, then confirmed by a person.** Column types come from values (dates,
  amounts, emails, IDs like `RN-551203`, names written "LAST, FIRST", company names), not only from headers.
  Links between files are found by measuring how many values actually overlap. Coded values are matched across
  systems automatically (`BYS` ↔ "Harborview Bayside", `RN` ↔ "Registered Nurse"). The person sees every
  guess in plain words and can change it before saving.
- **No tables created from uploaded files.** Column names come from whoever exported the file, so building SQL
  tables from them is fragile and unsafe. Storage keeps a fixed schema. Each client's data shape is stored as rows
  (`sources`, `source_fields`), and read-only **views** (`file_billing`, `profiles`) give a table per file for
  querying. Every identifier is checked against a strict pattern before it goes into SQL.
- **PDFs work in the guided setup too.** A PDF with a table is read like a CSV. A printed schedule grid
  (people down the side, days across the top, shift codes in the cells) is recognised and turned into one row per
  person per day, with hours worked out from the shifts and the legend.
- **Industry packs are optional.** Healthcare-only features (schedule PDF reader, staffing report, shift coverage)
  switch on for Harborview. Every company gets the general views: files, to-do, profiles, things expiring,
  totals that should match.
- **Proof:** `tests/test_setup.py` gives the setup raw CSVs from three industries (healthcare, retail,
  manufacturing), accepts every suggestion, and checks the planted problems are found. No config is written by hand.

## 12. Learning from decisions (rules people approve)
After someone marks an issue "Not a problem" or "Known, leave it", TruthLayer offers to turn that into a rule:
"same kind of issue" (rule + field), optionally narrowed by an attribute of the entity ("when position is
Dispatcher"). The offer shows how many other issues it would close. Rules live in `auto_rules`, are re-applied on
every rebuild, and mark matching issues `auto` with the rule named on the issue. Guardrails: never applied to
critical issues, every auto-resolution is logged, one click undoes it for a single issue (a `keep_open` decision),
and any rule can be switched off in How it works. It is pattern matching on decisions people made, not AI.

## What we'd do next
Incremental ingest instead of a full rebuild (fine at this scale), role-based review
permissions, scheduled re-verification against licensing APIs, and push alerts for expiring
credentials.
