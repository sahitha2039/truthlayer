# TruthLayer: The Complete Guide

Oct 4, 2026 · Sahi · Pulse Foundry hackathon

## Summary

TruthLayer is a reusable source of truth: it takes exports from a company's systems, works out which records describe the same real person or thing, shows where the systems disagree, and hands people only the decisions that need a human.

**One-liner:** Every company already has its data. The problem is that its systems disagree. TruthLayer sits on top of them, works out what's actually true, shows where the evidence for each value comes from, and hands people only the decisions that need a human.

- **Built for:** Pulse Foundry's challenge client, Harborview Care Group (2 nursing facilities, HR, payroll, licensing and a printed schedule PDF).
- **Reusable for:** any company whose systems describe the same people or things. Proven on retail, manufacturing and trucking data with no hand-written setup.
- **What it is not:** a replacement for HR or payroll, or a data-cleaning script that silently picks values. It sits on top of existing systems and never hides a disagreement.
- **Status:** working app (Python, FastAPI, SQLite, single-page UI), test suite passing, demo companies for four industries.

## The problem

Businesses don't lack data; they can't trust it, because the same person or thing is recorded differently in every system and the systems disagree.

At Harborview, the same nurse appears as "Sofia Reyes" in HR, "REYES, SOFIA" in payroll and the licensing service, and "Sofia Reyes" on a printed PDF schedule. Payroll and the schedule carry no employee ID. Facilities are written "Harborview Bayside", "Bayside" and "BYS". Nobody has one place to get a trusted answer.

| Harborview's words | What actually goes wrong |
|---|---|
| "Hospitals send us patient referrals, and by the time we get through them, the patient has often gone somewhere else." | Answering "do we have licensed staff for this patient?" means checking the schedule, HR and licenses by hand, and they don't agree. |
| "Every quarter, reporting our staffing numbers to the state takes someone weeks, and we're never fully confident in them." | Someone hand-matches payroll hours to people, roles and facilities, finding errors as they go. |
| "Licenses, certifications, and vendor paperwork expire, and we usually find out too late." | Each system holds a different expiry date and nobody watches all of them. |

All three share one root cause: no trusted, shared record of who works where, in what role, with which credentials, and how many hours.

## Why it works

It works because it treats trust as the product: every value carries its evidence, every disagreement is visible, and every human decision is remembered.

1. **Never hide a disagreement.** Every value is stored exactly as received, next to its cleaned-up form and the file and row it came from. When systems disagree, the trusted system's value is shown *provisionally* and a to-do item is opened. Nothing is silently overwritten.
2. **Authority per field, not majority vote.** HR is trusted for role and facility, the licensing service for license dates, payroll for hours. Example: Daniel Kim is at Riverdale in HR but Bayside in payroll and the schedule. HR's value is shown, and the flag says most systems disagree, which often means HR is the stale one.
3. **Careful matching.** Exact IDs first, then names, nicknames (Marc = Marcus), initials and close spellings, with role and facility as tie-breakers. At 92% similarity or above, records link automatically. Between 75% and 92%, they link provisionally and wait for a person ("Is Sofia Rayes the same employee?"). Below that, they become a visible "not in HR" person, never a dropped row.
4. **Decisions are remembered, not repeated.** A decision is saved against a fingerprint of the exact disagreement. Every rebuild re-applies it. If the underlying values change, the old decision no longer fits and the question reopens. Every decision is logged with who and when.
5. **Rules you can explain.** Every flag comes from a deterministic check, so the same input always gives the same answer, and each flag shows its evidence. That's what compliance and state reporting need.
6. **Plain language for non-technical staff.** "Systems disagree on facility", "Worked after license expired", "Is 'Jose Moralez' the same driver?". One button answers each question.
7. **The engine knows nothing about healthcare.** Everything client-specific lives in one setup file per company. A new industry is a new setup, not new code.

## How it works

Every upload runs the same six steps, and the whole result is rebuilt from the raw files each time, so it's always consistent and decisions are re-applied automatically.

1. **Ingest (****`ingest.py`****).** Each file is matched to a system by its columns, using names, synonyms and fuzzy matching, so renamed or reordered columns still load. It handles CSV, TSV, Excel, PDF tables, title rows above the header, blank rows, `;` or tab delimiters and odd encodings. Printed schedule PDFs are read as a grid (people × days), the shift legend ("7a-7p is 12 hours") is read from the footer, and each shift becomes one row with its hours. The file is stored exactly as received.
2. **Normalize (****`normalize.py`****).** Every value is converted to a comparable form: four-plus date formats to ISO, phones to digits, `RN 552310` and `RN-552310` to one license format, "Harborview Bayside" / "BYS" / "Bayside" to one facility code, "REYES, SOFIA" to first + last. Company names drop suffixes ("ACME Corp." = "Acme Corporation"). Anything that can't be parsed ("2024-02-30") is flagged, never guessed.
3. **Match (****`engine.py`****).** Records are linked to one profile per real person. The master list (HR) defines who exists. Other files link by shared identifiers (license number, email, account number, several in order) and then by name with nickname and fuzzy matching. Unsure matches wait for a person; unmatched records become visible "not on the roster" people.
4. **Reconcile.** For each field (role, facility, license expiry…), every system's value is collected. If they agree: confirmed. If only one system has it: single source. If they disagree: the trusted system's value is shown provisionally and a conflict is flagged. A past human decision overrides it.
5. **Run the checks (****`rules.py`****).** Generic rule types run over the profiles: expired / expiring, not verified recently, activity after expiry, missing from a system, possible duplicates, totals that should match between two systems (paid vs scheduled hours), unusual status values (SUSPENDED, Terminated), date order, and value ranges.
6. **Decide and record.** Everything that needs a human becomes a plain-language to-do item with its evidence and one-click answers. Decisions are stored by fingerprint, logged with who and when, and survive re-uploads. Read-only SQL views are rebuilt so every file can be queried like a table.

## How it decides what to flag

Every flag comes from an explicit, readable rule, not from AI: the same files always produce the same flags, and each flag names the rule and the records that triggered it.

Flags come from three places:

1. **A value that can't be trusted on its own.** It fails to parse ("2024-02-30"), isn't a known code ("Riverdale West"), is blank where required, or falls outside a set range (88 hours in a week when the limit is 80).
2. **Systems that disagree about the same field.** For each profile field, every system's value is compared after cleaning. Any real difference is flagged, with the trusted system's value shown provisionally. Matching problems are flagged the same way: an unsure match, someone missing from the master list, or two profiles that share a license number or phone.
3. **Checks chosen in the setup.** Each company's setup lists which checks run and with what thresholds.

| Check type | Fires when | Example |
|---|---|---|
| Expiration | A date has passed, or is within N days (default 60) | License expired 19 days ago |
| Staleness | A "last verified" date is older than N days (default 180) | Last verified 481 days ago |
| Activity after expiry | A shift, trip or pay period is dated after the expiry | Worked after license expired |
| Presence | Someone on the master list has no record in another system | Not found in the licensing service |
| Totals compare | Two systems' totals per person and period differ by more than a tolerance | Paid 44 h vs scheduled 36 h |
| Duplicates | Identical rows, or two profiles sharing an identifier | Possibly paid twice |
| Unusual status | A status column holds something other than its normal value | License status is SUSPENDED |
| Date order | An end date is before its start date | Pay period ends before it starts |

**Who picks the checks?** For Harborview, a person wrote them in the setup file. For a new company, the guided setup proposes them from the columns: a date column named like "expires" or "valid until" gets an expiration check, a "last verified" date gets a staleness check, a "status" column gets an unusual-status check, and two activity files with matching measures (hours and hours, miles and miles) get a totals check. These are pattern-matching heuristics on names and values, not machine learning, and a person approves every proposed check before it runs.

**How to describe it to judges:** "Detection is rule-based and explainable on purpose. The smart part is the setup, which reads the files and proposes the rules, plus the matching, which handles nicknames, typos and different formats. AI is the next step for the uncertain cases, not the judge of compliance."

## How it solves Harborview's problems

TruthLayer doesn't build three apps; it builds the one trusted layer all three apps would read from, and each problem already has a working view on top of it.

| Problem | What's built today (demo it) | What the app on top would do (not built) | Honest gap |
|---|---|---|---|
| **Referrals** | Hospital referrals card and shift calendar (`views.coverage`): for each facility, day and shift, is a licensed nurse on duty **and** clean? Aisha shows red because she's scheduled after her license expired. API: `/views/coverage`. | Intake asks "Bayside, nights, next 3 days: cleared RN on duty?" and answers a hospital in seconds instead of making calls. | No beds, census or patient-needs data yet; that's one more source plugged in the same way. |
| **State staffing report** | State staffing report card (`views.staffing`): paid hours by week, facility and role, using the trusted role, compared with scheduled hours. Every questionable hour is flagged with its reason. "52% ready to report". CSV export. | Formats the trusted hours into the state's submission template; quarterly reconciliation becomes a short checklist. | Real submissions (e.g. CMS's Payroll-Based Journal for nursing homes) also need resident census and the state's job codes. |
| **Expiring licenses and paperwork** | Licenses card (`views.credentials`) and checks: expired, expiring within 60 days, systems disagree, not verified in 180 days, can't be verified, and worked after expiry. Dates come from the licensing service, not HR's copy. | A daily job emails managers 60/30/7 days before expiry, and immediately when someone is scheduled past expiry. | Vendor paperwork wasn't in the data; the same expiry check works on any dated file (shown on supplier insurance and ISO certificates). |

Concrete flags from the sample data: Priya paid 44 h vs scheduled 36 h; Marcus Bell paid twice; Carmen Diaz paid and scheduled but not in HR; Grace Thompson listed as RN in payroll but LPN everywhere else; Aisha working two days after her license lapsed.

## Works for any industry

A new client in any industry is a new workspace and a reviewed setup, never new code, and this was tested on four industries with no hand-written configuration.

**Workspaces.** Each company has its own folder: its setup, database, original uploads and problem log. Nothing is shared between companies.

**Guided setup.** Drop a company's files and TruthLayer proposes a setup in plain words for a person to confirm:

1. **What each column holds,** judged from the values, not just the header: IDs (`DRV-1001`, `RN-551203`), people's names (including "LAST, FIRST"), company names, emails, phones, dates, amounts, hours, miles, categories, status columns.
2. **Which file is the master list** (one row per real thing, with a unique ID and names) and what each row is called (Employee, Customer, Supplier, Driver).
3. **How the other files connect,** by measuring overlap of actual values: "90% of license numbers match", "same name, 92% match". Several identifiers can be used in order.
4. **Which fields to compare and who to trust:** columns that mean the same thing are paired by values and header meaning (Role = Job title, Site = Facility). Coded values are aligned automatically: `BYS` = "Harborview Bayside", `RN` = "Registered Nurse".
5. **Which checks to run:** expiry warnings for expiry-like dates, staleness for "last verified" dates, missing records, duplicates, unusual statuses, and totals that should match (paid vs scheduled hours, billed vs shipped value, invoices vs purchase orders).

**PDFs.** A PDF with a table is read like a CSV. A printed schedule grid is recognised and turned into one row per person per day, with hours from the shift legend.

**Industry packs.** Features that only make sense for one industry (the healthcare staffing report and referral calendar) are optional packs. Every company gets the general views: files, to-do list, profiles, expiring items and totals.

| Company | Industry | Files | Found automatically (examples) |
|---|---|---|---|
| Harborview Care Group | Healthcare | HR, payroll, licensing (CSV), schedule (PDF) | Expired license with shifts after it, paid ≠ scheduled, facility and role conflicts, duplicate pay |
| Northwind Outfitters | Retail | CRM, billing, shipping | Billed $990 vs shipped $600, duplicate customer, conflicting email, buyer not in CRM |
| Apex Manufacturing | Manufacturing | Suppliers, ISO certificates, purchase orders, invoices | Expired insurance and ISO certificate, "ACME CORP" = "Acme Corporation", duplicate supplier, invoices ≠ POs |
| Ridgeline Freight | Trucking | HR roster, CDL checks, medical cards (Excel), trip logs, payroll | Suspended license, driving on expired CDL and medical card, fired driver still paid, miles paid ≠ driven; all 21 planted problems |

**Why not create a database table per uploaded CSV?** Column names come from whoever exported the file, so building tables from them is fragile (a renamed column breaks the table) and unsafe (SQL injection). Storage keeps a fixed schema, each company's data shape is stored as rows (`sources`, `source_fields`), and read-only SQL views (`file_payroll`, `profiles`) give a queryable table per file without the risk.

## Product tour

The app has a company picker and three pages per company, with details opening in a side panel so you never lose your place.

| Screen | What it shows | What you do there |
|---|---|---|
| **Your companies** | One card per company with urgent / open counts; "Add a company"; "Start with the Harborview setup, no data"; demo companies for healthcare, retail and manufacturing | Open, create or start a demo |
| **Guided setup** | 5 steps: files; master list, what each row is and its ID; how other files connect (% of values matching); fields to compare and who to trust; checks to run with editable thresholds | Confirm or change each guess, then Save |
| **Home → 1. Your files** | One row per system with file name, rows and upload time | Upload, Replace or Remove; drag files anywhere on the page |
| **Home → 2. Your questions, answered** | One card per question: one big number and one sentence (e.g. "1 expired", "52% ready to report", "16 of 42 shifts covered") | Click for the detail panel (license list, hours by week, shift calendar, totals) |
| **Home → 3. To-do** | One line per issue, most urgent first; red = urgent, amber = should check, grey = for your info | Expand to see why and the original records; answer with one click (pick the right value, same person / different person, I'll handle it, not a problem, undo) |
| **People (Employees, Customers, Drivers…)** | One row per profile with status "All good" or "2 things to check" | Open a profile: its to-do items, the "what each system says" table (red = disagrees, ★ = trusted system), hours or totals, history |
| **How it works** | How each file was read, which system is trusted for what, how much the data agrees, activity log, data as tables, setup file | Re-run setup, browse tables, delete or reset the company |

Sidebar settings: **Check dates as of** (re-evaluate expiries for any date) and **Your name** (stamped on every decision in the log).

## Tech stack and code map

The stack was chosen for a reliable live demo: one `pip install`, one command, no Docker, no Node build, and every piece swappable for its production equivalent.

```
  HR roster    Payroll    Licensing    Schedule PDF      (any CSV / Excel / PDF export)
       \          |            |            /
        v         v            v           v
  +-----------------------------------------------+
  | 1 Ingest -> 2 Normalize -> 3 Match ->          |
  | 4 Reconcile -> 5 Check & record                |
  +-----------------------------------------------+
                        |
                        v
        Source of truth (profiles, evidence, issues,
        decisions, learned rules, activity log)
                        |
     +---------------+--+-------------+----------------+
     v               v                v                v
  Review UI    Referral intake   State staffing   Credential alerts
               (API)             report (API)     (API)
```

Files flow down through the engine into one source of truth; people review it in the UI and other apps read it through the API.

| Layer | Today | Why | Production swap |
|---|---|---|---|
| Language | Python 3 | Best data-wrangling ecosystem; fast to write | Same |
| API server | FastAPI + Uvicorn | Typed REST API, auto docs at `/docs` | Same, behind a load balancer |
| Database | SQLite, one file per company | Zero setup; plain SQL | PostgreSQL (JSONB for raw records), tenant id per company |
| File reading | `csv`, `openpyxl` (Excel), `pdfplumber` (PDF tables and grids) | Handles the formats clients actually send | Add OCR (e.g. Tesseract or a cloud OCR service) for scanned PDFs |
| Setup files | YAML (`config.yaml` per company) | Human-readable, diff-able, reviewable | Same, stored in the database with versions |
| UI | One HTML page, vanilla JavaScript | No build step; calls the same API any app would | React or similar if the UI grows |
| Tests | Plain Python test scripts (pytest-compatible) | Planted-problem datasets prove detection | CI on every change |

| File | What it does |
|---|---|
| `truthlayer/ingest.py` | Reads files, matches columns to fields, reads PDF tables and schedule grids |
| `truthlayer/normalize.py` | Dates, phones, IDs, license numbers, codes, person and company names, nicknames |
| `truthlayer/engine.py` | Matching, reconciliation, issues, decisions, persistence, summary metrics |
| `truthlayer/rules.py` | Generic checks: expiration, staleness, activity after expiry, presence, duplicates, totals, unusual values, date order |
| `truthlayer/setup.py` | Guided setup: column profiling, master detection, link discovery, value alignment, check proposals, config builder |
| `truthlayer/views.py` | Business views: staffing, credentials, coverage (healthcare pack); expiring, totals (any company) |
| `truthlayer/workspaces.py` | Company workspaces and demo presets |
| `truthlayer/sqlviews.py` | Read-only SQL view per file + combined `profiles` view |
| `truthlayer/store.py` | Database schema and helpers |
| `truthlayer/app.py` | REST API and UI host |
| `truthlayer/cli.py` | Command-line fallback that prints every flag |
| `static/index.html` | The whole UI |
| `clients/*.yaml` | Hand-written setups (Harborview, retail example) |
| `tests/` | Engine tests and guided-setup tests across industries |

## Data model and API

The database has three layers: raw evidence that never changes, a derived source of truth rebuilt on every run, and human input that survives every rebuild.

| Layer | Table | Holds |
|---|---|---|
| Raw evidence | `files` | Every upload: system, file name, checksum, time, active or replaced, how columns were read |
| Raw evidence | `records` | Every row exactly as received (`raw_json`), its cleaned form, problems found, which profile it links to, how and how confidently |
| Derived | `entities` | One profile per real person or thing: canonical values, status per field, sources, open issue count |
| Derived | `evidence` | For each profile field: every system's raw and cleaned value and the record it came from |
| Derived | `issues` | Every flag: fingerprint, severity, category, title, explanation, recommended action, evidence, status, first and last seen |
| Human input | `decisions` | Who decided what on which fingerprint, with a note and time |
| Human input | `match_overrides` | Confirmed or rejected matches ("Sofia Rayes" in payroll = E201) |
| Human input | `events` | The activity log: ingests, issues detected or cleared, decisions, setup changes |
| Human input | `auto_rules` | Learned rules: pattern (check + field), optional condition, action, label, who created it, how many issues it has resolved, on or off |
| Metadata | `sources`, `source_fields` | Each company's data shape as rows |
| Views | `file_<system>`, `profiles` | Read-only, queryable tables built from the above |

**Fingerprints** are what make decisions durable. Each issue's fingerprint is a hash of its type, the profile and the conflicting values. The same disagreement next quarter gets the same fingerprint, so the old decision re-applies. Different values mean a new fingerprint and a fresh question.

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/workspaces` | List companies and presets; create a company (blank, demo, or Harborview setup with no data) |
| POST | `/api/w/{company}/setup/files`, `/setup/master`, `/setup/save` | Guided setup: add files, change the master list, save the reviewed setup |
| POST | `/api/w/{company}/files` | Upload more files (auto-detected) |
| GET | `/api/w/{company}/summary`, `/entities`, `/entities/{key}`, `/issues` | The source of truth and to-do list |
| POST | `/api/w/{company}/issues/{id}/resolve` | Record a decision: choose, confirm, reject, acknowledge, dismiss, reopen |
| GET | `/api/w/{company}/issues/{id}/similar` | Rule options for this issue and how many open issues each would close |
| GET/POST/DELETE | `/api/w/{company}/auto-rules` | List learned rules, create one from a decided issue, switch one off |
| GET | `/api/w/{company}/views/{name}` | staffing, credentials, coverage, expiring, totals |
| GET | `/api/w/{company}/tables`, `/tables/{name}` | Read-only SQL views |
| GET | `/api/w/{company}/export/{name}.csv` | staffing, issues, entities as CSV |
| GET | `/api/w/{company}/events` | Activity log |

Any downstream app (referral intake, state reporting, credential alerts) uses this API without touching the UI.

## Testing and proof

The tests plant known problems in messy data and fail if any one goes undetected, so "it works" is a checked claim rather than a promise.

| Test | What it proves |
|---|---|
| Every planted Harborview problem is flagged | 17+ planted problems (expired license, paid twice, facility conflict, typo'd name, impossible date…) all caught; clean people raise no false alarms |
| Mangled exports still load | Same data with renamed and reordered columns, a title row, blank rows, `;` and tab delimiters and Windows encoding gives the same findings |
| Borderless PDF and Excel | A schedule PDF with no table lines, and a license file as Excel, both load |
| Decisions survive re-uploads | A decision re-applies when the same data comes back, clears when the source is fixed, and a different disagreement opens fresh |
| Value alignment | `BYS` = Harborview Bayside, `RN` = Registered Nurse, "Certified Nursing Asst." = "Certified Nursing Assistant", and different values never merge |
| Guided setup, healthcare | Raw HR, payroll and licensing CSVs plus the schedule PDF, no config: finds the same problems as the hand-tuned setup |
| Table PDF | A license report as a PDF table reads like the CSV |
| Guided setup, retail and manufacturing | Correct master list, entity name and planted problems, including company names written three ways |

On top of the tests: the Ridgeline Freight trucking dataset (5 files, ~600 rows, 21 planted problems) was set up through the UI from raw files and all 21 were flagged. Its answer key is in `test_data/ridgeline_freight/ANSWER_KEY.md`.

## Scaling to millions of records

The design carries over unchanged; scaling changes how the work runs, not what it does.

| Today (hundreds of rows) | At scale (millions of rows) | Why |
|---|---|---|
| SQLite file per company | PostgreSQL, partitioned by company and month; raw files in object storage (S3 or similar) | Same fixed schema, so no redesign; JSONB keeps raw rows queryable |
| Full rebuild on every upload | **Incremental**: only people touched by the new file are re-matched and re-checked | A new payroll file touches thousands of people, not millions; fingerprints already show what's new, cleared or unchanged |
| Compare each record to every profile | **Blocking**: only compare within small buckets (same license number, same last name + first initial, same email domain) | A million people means trillions of pairs otherwise; blocking cuts it to millions. Probabilistic record-linkage tools like Splink do this at scale |
| Checks in a Python loop | Checks as set-based SQL or Spark/dbt jobs over whole tables | "Paid vs driven", "expired but working" and duplicates are each one query |
| Upload a CSV | **Connectors** to HR, payroll and licensing systems' APIs, or change-data capture | The source of truth stays current without anyone exporting files |
| One request does all the work | Upload → queue → worker processes → results | Big files don't block the app; workers scale out |
| To-do list sorted by severity | Ranked by **impact**, with repeated issues grouped ("400 records share this terminal spelling problem: fix once") | 1% bad data at a million records is 10,000 issues; no team reviews that unranked |
| One user | Accounts, roles (reviewer, admin), approvals for sensitive decisions | Accountability scales with the team |

Rough sizing: blocking plus incremental runs mean a weekly payroll file for 100,000 employees re-checks only those employees, which should take minutes rather than hours on Postgres or Spark (an estimate, not yet measured).

## Where AI fits

AI is deliberately not the checker: deterministic rules check 100% of records cheaply and repeatably, and AI helps only where the rules can't decide on their own.

**Why not AI on every record:** it's too slow and expensive at millions of rows, and it can give different answers to the same input. Compliance answers must be repeatable and explainable.

| Where AI helps | Volume | A person still decides? |
|---|---|---|
| **Uncertain matches** ("Is Jose Moralez the same driver?"): read the context (same truck, same route, one letter off) and suggest an answer with reasons | Usually well under 1% of records | Yes, confirms or rejects |
| **Setup suggestions**: propose which columns mean the same thing ("Payee" = "Driver Name") and which checks fit | Once per company or new file type | Yes, on the review screen |
| **Scanned PDFs**: read images of paper (OCR plus layout understanding) | Only files with no text layer | Results go through the same checks |
| **Explaining and summarizing**: "This week: 3 drivers on expired credentials, payroll overpaid 1,240 miles, mostly at Allentown" | A daily or weekly digest | It's a summary, not a decision |
| **Spotting new patterns** the rules don't cover | Periodic review | Yes, decides whether it becomes a new check |

In short: the rules do the checking, AI helps with the uncertain cases and the setup, and people make the decisions that matter.

## Learning from decisions

Built. When a reviewer settles one issue, TruthLayer can settle the similar ones the same way, after the reviewer approves the pattern once.

**Two levels of memory:**

- **One exact issue.** Every decision is stored against the issue's fingerprint (check + profile + the values that disagree). If the same disagreement comes back next quarter, the decision re-applies. If the values change, the issue reopens.
- **A pattern (learned rule).** A decision can be turned into a rule that covers every issue of the same kind, optionally only where a profile attribute matches.

**How it works in the app:**

1. A reviewer clicks *Not a problem* or *Known, leave it* on an issue, for example "Derek Grant: not found in CDL Verification Report".
2. A small panel offers: "Handle similar issues the same way?" with choices such as *all "not found in CDL Verification Report" issues* or *only when position is Dispatcher*. Each choice shows how many open issues it would close right now.
3. The reviewer picks one and clicks *Make it a rule*, or *Just this one* to skip.
4. The rule is saved and applied on every rebuild. Matching issues move to Done, marked "Auto-resolved by rule: Not a problem: not found in CDL Verification Report when position is Dispatcher", with an *Undo* link. In the trucking test data, this closed Tanya Ortiz's issue automatically after Derek Grant's was dismissed.
5. *How it works* lists every learned rule, who created it, how many issues it has resolved, and a *Switch off* button.

**How matching works:** a rule is a pattern key (check name + field) plus an optional condition (profile attribute = value). The condition choices are profile attributes with a small number of distinct values (role, facility, status), so the rule reads like a business sentence, not a single person. No AI is involved: it is an exact match on fields a person chose.

**Guardrails (built):**

- Critical issues (expired license, worked after expiry, unknown person being paid) are never auto-resolved. The app explains why instead of offering a rule, and the API refuses such a rule. Issues already marked "Not a problem" also get a *Make it a rule* button in the Done list.
- Every auto-resolution is written to the activity log, and the issue names the rule that closed it.
- *Undo* on one issue reopens it and stores a "keep open" decision so the rule can't close it again. *Switch off* stops a rule everywhere and reopens what it closed.
- A rule only affects the company it was created in.

**Next steps:** suggest a rule automatically after the same decision is made twice; require a second reviewer to approve rules for high-severity checks; make rules expire for periodic re-review. With enough decision history, a model could propose patterns too, but they would still go through the same approve-once step.

## Security, privacy and compliance

The prototype already gets the structural decisions right (isolation, audit trail, no SQL built from user input); the production work is accounts, encryption and hosting.

**Already in the build:**

- **Isolation:** each company has its own folder and database; data can't leak between clients.
- **Audit trail:** every upload, detected issue, cleared issue and decision is logged with time and reviewer name.
- **Originals preserved:** files are stored exactly as uploaded and never modified, so any value can be traced to its source row.
- **Safe SQL:** no tables are created from uploaded column names; every identifier in a SQL view is checked against a strict pattern first, and the table browser is read-only.
- **Deterministic results:** the same files always give the same flags, which makes outcomes reviewable.

**Needed for production:**

- Logins with single sign-on, roles (viewer, reviewer, admin) and per-company access.
- Encryption at rest and in transit; secrets management.
- Hosting suited to the data: HIPAA-eligible infrastructure and a business associate agreement for healthcare clients with any patient data; SOC 2 controls for enterprise clients generally.
- Data retention and deletion policies per client; masking of sensitive fields (license and ID numbers) for viewers.
- Approval workflow for high-impact decisions (e.g. clearing an expired-license flag needs a second reviewer).

## Limitations and roadmap

Knowing the limits is part of the pitch: each one below has a clear next step, and none requires redesigning the engine.

| Limitation today | Effect | Next step |
|---|---|---|
| One kind of profile per company (employees, or customers, or suppliers) | Can't yet check "this order points to a product that doesn't exist" | Several linked entity types per company |
| Scanned PDFs (images of paper) | Skipped with a clear message | OCR before the table reader |
| Guided setup's presence checks don't know roles | Dispatchers flagged for missing CDL checks | Suggest "only for roles that need it" conditions in setup |
| Guided setup guesses can be wrong on unseen files | A person must review before saving | Learn from corrections; AI suggestions for ambiguous columns |
| Full rebuild per upload | Fine for thousands of rows, slow for millions | Incremental processing and blocking (see Scaling) |
| File uploads only | Data is as fresh as the last export | API connectors and change-data capture |
| No logins | Demo only | Accounts, roles, approvals (see Security) |
| Healthcare views lack census and job-code mapping | Staffing report isn't submission-ready | Add census source and state job-code table |
| Every company has a master list | Companies with no single system of record need one chosen | Build profiles by clustering across sources when no master exists |

1. **Next 2 weeks:** OCR, role-aware checks, several entity types, approvals.
2. **Next quarter:** Postgres, incremental processing, blocking, first API connectors (HR and payroll), accounts.
3. **After:** AI-assisted matching and setup, impact-ranked to-do list, downstream apps (referral intake, state report export, credential alerts).

## Pitch: demo script and Q&A

The demo should prove three things in four minutes: it handles their real files live, it answers their three problems, and it isn't built only for healthcare.

**Before judging:** start the server fresh (`uvicorn truthlayer.app:app`), open the app, and have the Ridgeline Freight company ready in a second tab.

1. **Problem (20 s):** "Four systems, all disagreeing. Nobody can get one trusted answer."
2. **Load their files live (40 s):** choose "Start with the Harborview setup, no data", drop their 4 files. Point at the file list: each recognised, the PDF read as a schedule.
3. **The three cards (60 s):** licenses, state staffing report, hospital referrals. Open one detail panel.
4. **One person (60 s):** open someone with a conflict, show "What each system says" (red = disagrees, ★ = trusted), pick the right value, show it in the activity log.
5. **Any industry (40 s):** switch to the trucking company created from raw files. "Same engine, no new code, 21 of 21 problems found."
6. **Close (20 s):** the one-liner.

**Talking points:**

- We never silently fix data; every value shows its evidence and every disagreement is a visible question. Every flag comes from a rule you can read, not a black box.
- Trust is per field: HR for role, licensing for license dates, payroll for hours. Not majority vote.
- Uncertain matches wait for a person, and decisions are remembered across uploads.
- One trusted layer feeds all three apps through one API.
- Proven on four industries with no hand-written setup.

| Likely question | Answer |
|---|---|
| What if your matching is wrong? | Below 92% similarity nothing links automatically; it waits for a person. Confirmed matches can be undone, and every match shows how it was made. |
| Why not just use AI? | Flags come from explicit rules, not AI, on purpose: compliance answers must be repeatable and explainable. The setup reads the files and proposes the rules; AI is the next step for uncertain matches and setup, never the final call. |
| How would a new client onboard? | Create a company, drop the files, review one screen. Minutes, not a project. |
| How does this scale? | Postgres, incremental processing, blocking for matching, set-based checks, connectors instead of CSVs, impact-ranked to-do list. |
| What did you deliberately not build? | Logins, live connectors, OCR for scanned PDFs, the downstream apps themselves. All on the roadmap. |
| Why not create a table per CSV? | Unsafe and fragile; we store the shape as rows and give read-only views per file instead. |
| What's the business value? | Referral answers in seconds, the quarterly report becomes a checklist, expiries are caught before they become violations. |
