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

## What we'd do next
Incremental ingest instead of a full rebuild (fine at this scale), role-based review
permissions, scheduled re-verification against licensing APIs, and push alerts for expiring
credentials.
