# SG360 BOL Reconciliation — Full Project Context

*For planning/brainstorming. Originally written June 19, 2026; last updated: 2026-07-22.*

> This document started as pre-launch planning notes. Most of what it originally described as "blocked" or "not working yet" has since shipped. For the definitive, actively-maintained technical reference (API routes, schema, business rules, live deployment), see `CLAUDE.md` — this file keeps its original planning-notes character and points there rather than re-duplicating that detail.

---

## What This Is

SG360 (commercial printing company) has a logistics coordinator named **Katie** who manually maintains an Excel spreadsheet every morning to reconcile freight billing. The spreadsheet (`Technique and BOL Numbers New June 2026.xlsx`) pulls data from three separate systems:

1. **Visual Mail / Technique** — physical shipment data (trips, manifests, weight, pallets, pieces)
2. **ALG Worldwide invoices** — what ALG actually charged (emailed each morning by Tanya at ALG)
3. **Access tariff rates** — what SG360 *expected* to pay (rate card in an old Microsoft Access database)

Katie manually copies data from all three into the spreadsheet, calculates cost variance, then emails a CSV to **Mary** in accounting.

**This project replaces that with a web dashboard — and, as of 2026-07-09, that dashboard is deployed and live on AWS**, actively being tested against real data (see "Current State" below).

---

## Key People

| Name | Role | Relevance |
|---|---|---|
| **Katie** | SG360 logistics coordinator | Reviews dashboard ~10am, approves/flags records |
| **Tanya** | ALG Worldwide (external) | Sends ALG invoice email each morning with Z-number loads |
| **Mary** | SG360 accounting | Receives the approved BOL summary CSV by email |
| **Marge** | SG360 SQL team | Wrote original Technique queries; knows DB schema |
| **Megha** | SG360 Prophecy admin | Knows Prophecy internals and Z-number generation |
| **Phil** | Logistics lead | Owns ALG relationship; could request CSV invoices instead of PDF |
| **Nikhil** | Developer | Building this system |

---

## The Workflow Being Replaced

**Current (manual):**
1. 7–9am — Katie opens Technique, copies trip/manifest/weight/pieces into Excel
2. ~8am — Tanya emails ALG invoice PDF/CSV with Z-numbers and dollar amounts
3. ~9am — Katie enters Z-numbers + amounts into Excel, calculates variance manually
4. ~10am — Katie reviews, emails Mary a CSV of approved rows

**Target (automated), as actually built:**
1. Tanya's invoice CSVs land in a shared folder; Katie clicks "⤓ Pull Invoices" (or uploads the folder manually) — each invoice is matched against Technique automatically, discovering trip/manifest data on demand if it isn't already known (there's no separate scheduled "pull everything" step — that was tried and removed 2026-07-22 in favor of this on-demand approach)
2. ~10am — Katie opens the dashboard, reviews color-coded Cost % variance, approves or flags each record
3. Done — "Send to Accounting" emails Mary automatically; "Export to Prophecy" generates the SID file Katie imports to create BOL numbers, which then sync back automatically

---

## Data Flow (Corrected Understanding)

This was surprising — the systems are connected in a specific way:

```
mail.dat file
    → VisualMail (physical data only — no costing at all)
        → Prophecy/ShipperPlus (estimates a cost — usually wrong)
            → Katie corrects manually using ALG invoice
                → BOL number created in Prophecy
```

- **VisualMail** = authoritative source for weight, pallets, pieces
- **Prophecy** = estimates freight cost (usually wrong), where BOL numbers are created
- **ALG invoice** = what was actually charged; the app now calculates its own independent expected cost from ALG's own per-zone rates + SG360's own weight data, and compares it against what ALG billed
- **BOL numbers** are still created by Katie in Prophecy after morning review (the app generates the SID import file for this) — creating BOLs directly from this app remains a long-term goal, not yet built. Reading BOL numbers back once Katie creates them, however, **is** automated (resolved 2026-07-01 — see "Current State" below)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy 2.0, pydantic-settings; packaged as a Lambda container image in production (Mangum adapter) |
| Frontend | React 18 (Vite), functional components + hooks, inline styles only; static S3/CloudFront build in production |
| Database | PostgreSQL locally (`sg360_bol` DB); Aurora Serverless v2 in production |
| SQL Server | pyodbc → AWP-SQL-PROD, via linked servers for Prophecy/ShipperPlus data. ODBC Driver 18 by default (matches the Lambda container); override to Driver 17 locally via `SQLSERVER_ODBC_DRIVER` if only that's installed |
| Email | smtplib STARTTLS port 587 (O365) — console fallback in dev; IMAP polling exists but manual upload/poll-folder is the real daily intake path |

**Key config flag:** `USE_MOCK_DATA` in `.env`
- `True` = all routes use `mock_data.py` (no DB or SQL needed) — this is how most day-to-day development happens
- `False` = real PostgreSQL + real AWP-SQL-PROD queries; invoice fields are fully populated via CSV upload/poll-folder, not left null

---

## Current State (updated 2026-07-22)

Nearly everything originally listed here as "blocked" is now implemented and has been running against real data since before the AWS deployment went live 2026-07-09:

- **Live on AWS** — Lambda + API Gateway backend, Aurora Serverless v2 database, S3 + CloudFront frontend. See `documentation/AWS Deployment.md`.
- **Invoice matching** — Z-number, Job Name (trip or manifest suffix), or Prophecy BOL number, with an automatic wider fallback search for anything that doesn't match instantly
- **Cost calculation (`access_prog`) and Cost %** — computed from ALG's own per-zone invoiced rate (falling back to a much more complete ALG-sourced rate table, then SG360's own internal card as a last resort), against SG360's own independently-pulled weight/pallet/piece counts — fully implemented, not blocked on a destination→ZIP mapping (that's resolved; see `CLAUDE.md`'s data-source table)
- **BOL number sync** — reads `load_id` back from Prophecy/ShipperPlus automatically once Katie creates a load; resolved 2026-07-01, was previously listed here as needing Megha's input but turned out to already be queryable
- **Ambiguous-trip handling, third-party/Do Not Pay workflows, bulk actions, Prophecy SID export** — all built; see `documentation/Design and Workflow - BOL Reconciliation.md` for the current end-to-end walkthrough and `CLAUDE.md` for full route/schema detail

**What's still genuinely open:**
- **Prophecy reship cost** — `get_prophecy_data()` is implemented and used for Wolf/311 loads' own weight/pallet data, but the *reship cost estimate itself* is still intentionally hidden from the dashboard (Prophecy's estimate uses a known-wrong 2006 tariff — Katie was manually correcting it in the old process, and this app deliberately doesn't surface it)
- **Automated/scheduled invoice email polling** — the IMAP route exists but isn't the real daily mechanism; manual upload/poll-folder is
- **Creating BOLs directly in Prophecy** — still a long-term goal, not built; Katie still does this step herself using the app's SID export
- **`RDS_MASTER_SECRET_ARN`** (auto-rotated DB credential) — built but not wired up in production Terraform yet, blocked on an IAM permission grant (see `CLAUDE.md`)

---

## The Key Variance Metric

**Cost % = amount ÷ access_prog** (actual ALG charge ÷ SG360's own calculated expected cost) — confirmed current as of the 2026-07-21 reversal (was `access_prog / amount` 2026-07-16 to 2026-07-21). Over 100% means SG360's calculation came in *lower* than what ALG billed.

Stored as ratio (e.g., 0.9881 = 98.81%).

Color thresholds in the dashboard (confirmed against `CLAUDE.md` and the live frontend):
- 🟢 Green: within 3% of 100% (0.97–1.03) — normal
- 🟡 Orange: 3–6% off (0.94–0.97 or 1.03–1.06) — investigate
- 🔴 Red: >6% off (<0.94 or >1.06) — flag

This is the whole point of the tool. Everything else is supporting data.

---

## How Real Data Gets Loaded (Two SQL Queries)

Both queries run against **AWP-SQL-PROD**, using Windows authentication (blank `SQLSERVER_USER`/`SQLSERVER_PASSWORD` in `.env`) rather than a SQL-auth service account.

> A previous version of this doc listed an example SQL-auth username/password pair here. That's been removed — if that was ever a real credential rather than a placeholder, treat it as compromised (it was sitting in this file's git history) and rotate it; current access uses Windows auth, not a stored SQL credential.

### Query A — Technique Trip & Manifest Pull
Finds all ALG-destined shipments despatched in a given window (now run per-invoice on demand, not on a fixed daily schedule — see "Current State").

Hits: `TECH.Live_Orders` (linked server) + `SegGroup` (linked) + `SQLAPPS3/ShipperPlus` (linked) + `VisualMail` (local)

Returns per manifest:
- Trip ID (`TEC_T_0109878` format)
- Manifest number (`TEC_M_0228920` format)
- Destination code (e.g. `SCF606`, confirmed as `Locations.AccountNumber`)
- Prophecy pieces (from ShipperPlus order_headers) and `load_id`/`pooled_to_load_id` (BOL sync)
- Notes (trailer number etc.)
- Carrier

**Does NOT return weight** — weight doesn't exist in `TECH.Live_Orders`.

### Query B — VisualMail Weights, Pallets & Pieces
For a list of manifest numbers, returns authoritative counts from VisualMail.

Returns:
- `SUM(p.Weight)` → technique_weight
- `COUNT(p.UniqueContainerID)` → technique_pallets
- `SUM(p.NumberOfCopies)` → technique_pcs

**Why two queries?** Weight only lives in VisualMail. Query A discovers which manifests exist; Query B fetches the physical measurements.

---

## Database Schema Highlights

The schema has grown substantially since this doc was first written — see `backend/models.py` (`BOLRecord`) or `CLAUDE.md`'s "Database schema highlights" section for the complete, current column list (it now includes ALG quantity fields, ambiguous-trip/dismiss/acknowledge flags, cost-calc-detail JSON, do-not-pay/third-party flags, and more — roughly 20 columns beyond what's listed below). The columns below are still accurate as far as they go:

```
bol_records
  id                UUID PK
  bol_number        Integer, NULLABLE (created in Prophecy by Katie, not at load time)
  technique_trip    String(50), nullable
  manifest          String(50), nullable
  technique_weight  Numeric(12,2)    ← up to 416,000 lbs, needs 12 digits
  technique_pallets Integer
  technique_pcs     Integer
  invoice_number    String(20), nullable   ← Z555216 format
  invoice_email_sender String(200), nullable
  access_prog       Numeric(10,2), nullable  ← our own calculated expected cost
  amount            Numeric(10,2), nullable  ← actual ALG charge (additive across multiple invoices on one trip)
  cost_pct          Numeric(8,6), nullable   ← amount/access_prog stored as ratio (confirmed current 2026-07-21)
  weight_diff       Numeric(12,2), nullable  ← ALG minus technique
  pallet_diff       Integer, nullable
  pcs_diff          Integer, nullable
  notes             Text, nullable
  status            Enum(pending/approved/flagged)
  flag_reason       Text, nullable
  approved_at       DateTime(tz), nullable
  approved_by       String(100), nullable
  created_at        DateTime(tz)
  updated_at        DateTime(tz)

approval_history
  id, bol_id (FK), action, performed_by, reason, performed_at

tariff_rates          ← legacy zip3-keyed card, last-resort fallback only
alg_tariff_rates       ← 527 rows, exact-match on destination code — the primary rate source now (added 2026-07-15)
fuel_surcharge_rates   ← 135 rows seeded; fsc_amount is a decimal fraction (0.365 = 36.5%), not a percent
```

---

## API Routes

The route surface has grown to roughly 35 routes (invoice matching, ambiguous-trip resolution, per-record exports/refreshes, admin backfills, etc.) — see `CLAUDE.md`'s full API Routes table for the current, maintained list rather than a partial copy here. `POST /api/admin/pull` (the original "morning data pull," listed in earlier versions of this doc) was removed 2026-07-22 — manifest discovery now happens per-invoice on demand instead.

---

## File Structure

```
C:\nikhilm\excel-prophesy-BOL-automation\
├── backend\
│   ├── main.py          — All routes (single file for Module 1)
│   ├── config.py        — Settings via pydantic-settings + .env (or AWS Secrets Manager in Lambda)
│   ├── models.py        — SQLAlchemy ORM + Pydantic schemas
│   ├── database.py      — Engine + session (works against AWS RDS/Aurora unmodified)
│   ├── data_layer.py    — THE integration boundary — see below
│   ├── mock_data.py     — 16 records at real scale (safe to delete when live)
│   ├── email_service.py — smtplib STARTTLS + console fallback
│   ├── email_parser.py  — O365 IMAP polling
│   ├── csv_export.py    — accounting CSV + Prophecy SID CSV
│   └── requirements.txt
├── frontend\
│   └── src\
│       ├── App.jsx              — Owns all state
│       └── components\          — SummaryBar, BOLTable, BOLRow, ApprovedSection, ThirdPartySection,
│                                   FlagModal, ReassignInvoiceModal, CompareManifestsModal,
│                                   BulkActionToolbar, EmailComposeModal, LogSection, and more
├── terraform\           — AWS infrastructure (Lambda, API Gateway, Aurora, CloudFront, WAF, S3)
├── CLAUDE.md            — AI context file — the current, actively-maintained technical reference
├── .env                 — GITIGNORED (has SQL/SMTP/AWS credentials)
└── .gitignore
```

`data_layer.py` is the integration seam — it now has 11 functions, not the original 4. Only one remains an actual stub:
- `get_technique_data(days_back)`, `get_manifest_weights(...)`, `get_manifest_weights_from_sid(...)`, `get_pallet_data_for_manifests(...)`, `get_tariff_rate(zip3, weight)`, `get_alg_tariff_rate(...)`, `reconcile_alg_tariff_rates(...)`, `get_prophecy_data(bol_number)`, `get_prophecy_pallet_data(bol_number)`, `get_current_diesel_price()` — all implemented
- `get_alg_invoice(invoice_number)` — **still a stub**; invoice ingestion goes through CSV upload/poll-folder instead, not a live ALG API query

---

## Open Questions

Most of what this doc originally tracked as open/blocking has been resolved — see `CLAUDE.md`'s own "Open Questions" section, which is the actively-maintained version of this table. As of 2026-07-22, everything below is resolved except where noted:

| # | Original question | Resolution |
|---|---|---|
| 1 | FSC unit: % of base rate or $/cwt? | Resolved — decimal fraction of base rate (e.g. `0.365` = 36.5%), confirmed 2026-06-19 |
| 2 | Destination → ZIP mapping | Resolved — `Locations.AccountNumber` (e.g. `SCF606`) confirmed correct 2026-07-01 |
| 3 | Can ALG send CSV instead of PDF? | Resolved — CSV format confirmed and is what's used today |
| 4 | Which diesel index for FSC? | Resolved — EIA weekly on-highway diesel, but only used as a fallback; the invoice's own fuel-surcharge line is primary |
| 5 | What triggers Z-number creation in Prophecy? | Resolved — Katie creates the load manually in Prophecy; load number = BOL number |
| 6 | Is `NumberOfCopies` the right pieces column? | Resolved — confirmed correct |
| 7 | Is there a BOL number in ShipperPlus linked via `load_id`? | Resolved 2026-07-01 — yes, already queryable, just wasn't being used per-record until then |
| 8 | Which ShipperPlus column holds Prop Reship? | Resolved, but the field is intentionally hidden from the dashboard (Prophecy's own estimate uses a known-wrong tariff) |

---

## What This Tool Looks Like Today

Katie opens the dashboard. She sees three summary cards (Awaiting Invoice / Ready to Review / Approved Today) and a pending-records table like this:

| Trip | Manifest | BOL | Wgt | Pal | PCS | Invoice | Calc Cost | Amount | Cost % | Actions |
|---|---|---|---|---|---|---|---|---|---|---|
| TEC_T_01... | TEC_M_02... | — | 35,240 | 42 | 343,521 | Z555216 | $3,139 | $3,177 | 🟢 98.8% | Approve / Flag |
| TEC_T_01... | TEC_M_02... | — | 21,104 | 95 | 405,063 | Z555217 | $2,806 | $3,155 | 🔴 88.9% | Approve / Flag |

She approves what looks right, flags what needs investigation, then clicks "Send to Accounting" — done. See `documentation/Design and Workflow - BOL Reconciliation.md` for the full current walkthrough, including ambiguous-trip handling, third-party/Do Not Pay, and Prophecy export.

---

## Future Modules (Not Being Built Yet)

- **Module 2** — Sheet 2 / Mary Group workflow (same pattern, different recipient)
- **Commingle billing** — `CM_` manifests already appear in Module 1 data
- **Scheduled/automated invoice email polling** — the route exists; manual upload/poll-folder is the real mechanism today
- **Auth** — `users` table ready; add JWT without touching existing routes
- **BOL creation in Prophecy** — long-term goal to create BOLs here instead of Katie doing it manually in Prophecy
