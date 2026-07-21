# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Prerequisites**: Python 3.11+, Node.js 18+, PostgreSQL 15+ (only needed when `USE_MOCK_DATA=False`).

## Commands

**Start both servers (recommended):**
```powershell
.\start.ps1
```
Kills any existing processes on ports 8000 and 3000 before launching.

**If edits don't take effect after restart** (stale `.pyc` bytecode from a zombie process):
```powershell
Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess -Force
```
Then restart normally. The `start.ps1` script kills by port but may not catch subprocesses spawned by `--reload`.

**If `/run` or `start.ps1` fails with `ModuleNotFoundError` for a package you know is installed:** bare `python` resolves to different interpreters depending on execution context on this machine — an interactive shell picks one install, `start.ps1`'s `-NoProfile` background process picks another (`Python314`) that can be missing packages. Install new backend deps into both, or point `start.ps1` at a full interpreter path.

**Or start individually:**
```powershell
# Backend (from project root)
python -m uvicorn backend.main:app --reload --port 8000

# Frontend
cd frontend && npm run dev
```

**Install dependencies:**
```powershell
pip install -r backend/requirements.txt
cd frontend && npm install
```

**Seed tariff rates (run once, requires USE_MOCK_DATA=False and a real DB):**
```powershell
python -m backend.seed_rates [--tariff PATH] [--fsc PATH] [--alg-tariff PATH]
```
Omit flags to use the default source paths under `c:\nikhilm\billing-freight-automation\`.

There are no automated tests and no linter configured for the frontend. Verify changes manually via the dashboard at `http://localhost:3000` and the FastAPI docs at `http://localhost:8000/docs`.

**Other frontend scripts:**
```powershell
cd frontend && npm run build     # production build; deployed via `.\deploy.ps1 -Frontend` or the `/deploy` skill (S3 + CloudFront) — no CI/CD, deploys are manual
cd frontend && npm run preview   # serve the production build locally
```

**Vite dev proxy:** The frontend calls bare `/api/*` paths. Vite proxies them to `http://localhost:8000`. Never hardcode `localhost:8000` in frontend code — the proxy handles it.

**Launching the app for verification:** use the `run` skill (`.claude/skills/run`) — it starts both servers, waits for health checks, and screenshots the dashboard.

**Deploying to AWS:** use the `deploy` skill (`.claude/skills/deploy`) — wraps `deploy.ps1`, adds pre-flight checks and a human-reviewed `terraform apply` gate, and verifies the live deployment afterward. See "AWS Lambda deployment" below for the current infra.

## Security notes (see `documentation/SECURITY.md` for full detail)

- `.env` never gets committed; real credentials live only there. `.env.example`-style placeholders are fine to commit.
- All production DB access is intended to be read-only (SELECT-only service account) — the app writes only to its own PostgreSQL database.
- Don't push directly to `main` once Katie is using the app day-to-day; land changes through a branch/PR.
- No production data (real BOL/invoice exports) belongs in the repo — `test_invoices_*/` is gitignored for this reason.

## .env quick-start

Minimal `.env` for mock-data prototype (no DB or email needed):
```
USE_MOCK_DATA=True
MOCK_INVOICES=True   # skips ALG invoice lookup during morning pull
```

Additional keys required for live mode:
```
USE_MOCK_DATA=False
DATABASE_URL=postgresql://sg360_user:localpass@localhost:5432/sg360_bol
SQLSERVER_USER=                 # blank = Windows auth to AWP-SQL-PROD
SQLSERVER_PASSWORD=
SQLSERVER_ODBC_DRIVER=          # blank = "ODBC Driver 18 for SQL Server" (matches the Lambda container).
                                 # Set to "ODBC Driver 17 for SQL Server" on a dev machine that only has
                                 # Driver 17 installed — Driver 18 was silently failing every live SQL
                                 # query locally (IM002 driver-not-found) until this was made configurable.
EIA_API_KEY=                    # eia.gov/developer (free) — for weekly diesel FSC lookup
SMTP_USER=user@sg360.com
SMTP_PASSWORD=
ALG_SENDER_EMAIL=               # Tanya's email address — filters IMAP search to her messages only
IMAP_MAILBOX=INBOX              # folder to poll (default INBOX)
INVOICE_FOLDER=\\sg360-wbapp-prd\Logistics\AgentsInvoices\Invoices to Process
INVOICE_S3_BUCKET=              # S3 bucket for invoice PDFs (Lambda only); empty = use INVOICE_FOLDER
```

**AWS Lambda only:** if `AWS_SECRET_NAME` env var is set, `config.py` loads all settings from AWS Secrets Manager instead of `.env`. This overrides everything above — the Lambda function uses this to pull credentials from a single Secrets Manager secret rather than individual env vars.

**`RDS_MASTER_SECRET_ARN` (built, not currently wired up):** `config.py` has support for rebuilding `DATABASE_URL` fresh at every cold start from Aurora's own AWS-managed, auto-rotated master-user secret (via `DB_HOST`/`DB_PORT`/`DB_NAME`), added 2026-07-16 after a stale, manually-synced DB password in `sg360-bol-live-credentials` caused an outage. **Not currently active in the live Lambda** — enabling it requires an IAM policy change (`iam:PutRolePolicy` on `sg360-bol-lambda-exec`) the deploying user didn't have permission for at the time; the Lambda's `environment.variables` in `terraform/main/lambda.tf` intentionally omits these 4 env vars for now, so this code path is a no-op and `DATABASE_URL` still comes from `AWS_SECRET_NAME`'s secret (manually resynced 2026-07-16, next AWS auto-rotation ~2026-07-23). See the comment in `lambda.tf` for how to finish enabling this once that permission is granted.

All live-mode dependencies (`pyodbc`, `sqlalchemy[mssql]`, `boto3`, `mangum`) are already in `requirements.txt` — `pip install -r backend/requirements.txt` installs everything.

## API Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check — reports DB status and mock mode |
| GET | `/api/bols` | Pending + flagged records (Katie's morning view) |
| GET | `/api/bols/approved` | Approved records for today (or `?export_date=YYYY-MM-DD`) |
| POST | `/api/bols/{id}/approve` | Approve a record; idempotent |
| POST | `/api/bols/{id}/unapprove` | Revert an approved record back to pending |
| POST | `/api/bols/{id}/flag` | Flag a record with a reason |
| POST | `/api/bols/{id}/unflag` | Remove flag from a record |
| POST | `/api/bols/{id}/mark-third-party` | Mark as third-party (customer pays direct); excludes from SID export |
| POST | `/api/bols/{id}/unmark-third-party` | Revert third-party record back to pending queue |
| POST | `/api/bols/{id}/mark-do-not-pay` | Mark an unmatched invoice-only record as do-not-pay — approves it into its sender's Approved batch, renders "DO NOT PAY" instead of an amount; reversible |
| POST | `/api/bols/{id}/unmark-do-not-pay` | Undo do-not-pay — reverts to pending review, same as unapprove |
| POST | `/api/bols/mark-accounting-sent` | Set `accounting_exported_at = now()` on a list of record IDs; removes them from Approved view |
| POST | `/api/bols/{id}/reassign-invoice` | Move invoice to a different trip/BOL/manifest; body: `{ target, action: preview\|merge\|replace }` |
| GET | `/api/bols/{id}/trip-manifests` | Manual-verification view for an `is_ambiguous_trip` row: every manifest sharing this record's `technique_trip` (all statuses, not just pending), each scored against whichever sibling actually holds the invoice via the same relative-quantity-difference formula as `_closest_technique_match`. Powers `CompareManifestsModal.jsx` — human decides, nothing here auto-resolves. DB/mock only, no live Technique query |
| PATCH | `/api/bols/{id}/notes` | Auto-save notes field; not currently called by any UI (input removed from `BOLRow.jsx` 2026-07-14 pending a notes redesign) |
| POST | `/api/admin/pull` | Pull Technique manifests from AWP-SQL-PROD (disabled in mock mode). Also re-matches any stuck `invoice_only` stubs against already-committed DB records (cheap, local — no live query). Calls `_finish_resolving_stub()` — previously this silently dropped `invoice_email_sender`/`invoice_sent_at` and never computed `access_prog`/`cost_pct`, unlike the main upload-time match. **Note (2026-07-20):** this route used to also run a bulk live wide-fallback Technique search (21→90→40 days, over several iterations 2026-07-17 through 2026-07-20) over every remaining stub. Stacked onto this route's own main-pull live query, it reliably exceeded API Gateway's hard 30s integration timeout whenever any stub existed — confirmed live post-deploy, and no day-count narrowing reliably fixed it (a cold Lambda/Aurora start alone can eat most of the 30s budget before either query even runs). That live wide-fallback search moved to `_process_invoice_csv()` instead — see `_wide_fallback_technique_search()` and step 4b under "Invoice matching" below — where it runs once per newly-unmatched invoice, isolated in its own request/budget, rather than as a bulk sweep bolted onto this route |
| POST | `/api/admin/refetch-bols` | Re-query Technique for specific manifests; updates `bol_number` after Prophecy import (live mode only) |
| POST | `/api/admin/poll-email` | Poll O365 IMAP for unread ALG invoice emails → extract CSVs → process (live mode only) |
| POST | `/api/admin/reset-invoices?confirm=true` | Dev: clear ALL ALG-invoice-derived fields (including on already-approved records — resets status to pending unconditionally) + delete invoice-only stubs. Never touches Technique-side fields, `is_third_party`, `sid_exported_at`, or the static rate-card tables. Requires `confirm=true`. Wrapped by the `/cleanout` skill for repeatable testing |
| POST | `/api/admin/wipe-test-data` | Dev: deletes ALL `bol_records` (pending/approved/flagged/logged) + cascaded `approval_history`, for a clean invoice-by-invoice re-test. Does NOT touch `tariff_rates`/`fuel_surcharge_rates`/`users`. Requires `?confirm=true` |
| POST | `/api/admin/recompute-diffs` | Backfill weight_diff/pallet_diff/pcs_diff on existing records via `_compute_diffs()` (live mode only); pure DB recompute, no live Technique/Prophecy query |
| POST | `/api/admin/recompute-access-prog` | Backfill Calculated Cost on existing matched records — re-locates and re-parses each record's original invoice CSV (`_find_invoice_file(..., require_csv=True)`) since ALG's per-zone rate isn't stored anywhere else, then re-runs `_apply_access_prog_calc()` with a fresh live pallet-data query (live mode only); records whose original file can't be found are reported as `skipped_no_file`, not guessed at |
| POST | `/api/admin/fix-duplicate-invoice-matches` | One-time backfill for a since-fixed matching bug (pre-`_closest_technique_match()`): an invoice matching several manifests on one trip suffix used to be applied to *every* one of them with the same amount/weight/pallets/pcs, instead of just the closest match. Finds all records sharing an identical `invoice_number`, re-scores each group with `_closest_technique_match()`, keeps the best-scoring member untouched, and reverts every other member to a clean unmatched state (back to pending, invoice-derived fields cleared). Live mode only |
| POST | `/api/invoices/upload` | Upload ALG invoice CSV(s) — pass a whole sender folder (`invoice_folder_name`, parsed the same way as `poll-folder`) or fall back to manual `invoice_sender`/`invoice_date`/`invoice_time` fields; response includes `conflict` key if trip already had an invoice. Any invoice not matched by the already-pulled DB data triggers one live 90-day wide-fallback Technique search per invoice before falling back to a stub (`_wide_fallback_technique_search()`, 2026-07-20 — see "Invoice matching" step 4b below) |
| GET | `/api/invoices/{z}/file` | Serve the invoice file for a Z-number; checks S3 (`INVOICE_S3_BUCKET`) first with a presigned-URL redirect, then falls back to `INVOICE_FOLDER` (live) or `test_data/` (mock), searching root + one level of dated sender subfolders; prefers PDF over CSV |
| POST | `/api/invoices/poll-folder` | Scan `INVOICE_FOLDER` (root + one level of dated sender subfolders) for unprocessed CSVs → process each, parsing sender/date from the subfolder name; files stay in place, dedup via DB `invoice_number`. Same per-invoice wide-fallback search as `/api/invoices/upload` above applies to each unmatched CSV in the scan — a poll picking up several never-pulled invoices at once pays that live-query cost once per invoice, sequentially, not just once for the whole batch |
| GET | `/api/export/invoice-pdfs` | Download a merged PDF of all invoice PDFs for the given `?invoice_numbers=Z1,Z2,...`; fetches each from S3 (`INVOICE_S3_BUCKET`) first, falls back to `INVOICE_FOLDER`; skips any not found rather than failing the whole batch; returns 404 if none found. Triggered by "Download Invoice PDFs" in EmailComposeModal |
| GET | `/api/export/prophecy-sid` | Download Prophecy SID import CSV for approved manifests (live mode only); also stamps `sid_exported_at` on each included record |
| POST | `/api/bols/{id}/export-prophecy-sid` | Per-record SID export — pushes one pending Type A record to Prophecy without waiting for a batch approval; same CSV logic as the bulk route, scoped to one manifest |
| POST | `/api/bols/{id}/refresh-bol` | Refresh one record's manifest-side data: re-pulls weight/pallets/pieces and checks Prophecy for a BOL (`get_technique_data()` filtered to one manifest), without a full Technique pull — ~10s live (hits AWP-SQL-PROD), near-instant if the record already has a BOL. Weight source depends on `bol_number`: `get_manifest_weights()` (Query B) before a BOL exists, `get_manifest_weights_from_sid()` (the SID-export query) once one does — see "Ambiguous trips" below. Does not touch invoice-side fields (access_prog/cost_pct/amount/alg_*) |
| POST | `/api/bols/{id}/retry-match` | On-demand retry for one unmatched (`match_strategy=invoice_only`) invoice stub — checks a wide 90-day Technique window immediately instead of waiting for the next Pull Manifests click (widened from 21 days 2026-07-16 — a real trip older than 21 days was failing to match with no clear reason why). Now also calls `_finish_resolving_stub()` (2026-07-17) to copy `invoice_email_sender`/`invoice_sent_at` from the stub and compute `access_prog`/`cost_pct` from the record's own invoice CSV — previously left both blank, unlike a normal upload-time match |
| POST | `/api/export` | Generate accounting CSV and email to Mary + Katie |
| GET | `/api/logs` | All records across all dates; optional `?start_date=` / `?end_date=` / `?status=` filters |
| GET | `/api/logs/export` | Download log as CSV; same date-range params as above |

---

# SG360 BOL Reconciliation — AI Context

## What this project is

Internal logistics operations platform for SG360 (commercial printing company). **Module 1** replaces a manual daily Excel process: reconciling freight billing between Visual Mail/Technique, ALG Worldwide invoices, and Access tariff rates.

The source file being replaced: `c:\nikhilm\billing-freight-automation\Technique and BOL Numbers New June 2026.xlsx` (Sheet 1).

## Key people

- **Katie** — SG360 logistics coordinator. Reviews the dashboard ~10am each morning, approves or flags records.
- **Tanya** (at ALG Worldwide) — External. Sends the ALG invoice email each morning referencing Z-number loads.
- **Mary** — SG360 accounting. Receives the approved BOL summary CSV by email after Katie approves.
- **Marge** — Wrote the original Technique SQL queries. Source of truth on what data is accessible and how.
- **Megha** — Knows Prophecy internals. Contact for Prophecy DB schema and Z-number generation.
- **Phil** — Logistics lead. Owns ALG relationship; can request CSV invoices instead of PDF.

---

## Open Questions

| # | Question | Who to ask | Status |
|---|---|---|---|
| 3 | **ALG invoice format**: Tanya can send CSV (format confirmed from Z556229.CSV). `POST /api/invoices/upload` accepts it. Ask Phil to switch Tanya to CSV delivery. | Phil / Tanya | ✅ CSV format confirmed |
| 11 | **`tariff_rates` coverage gaps**: The 2026-07-01 estimate of "3 zones missing" badly understated this — directly checking 2 real invoices against the full card (2026-07-15) found 59 of 92 destination zip3s (64%, 71.5% of shipped weight) entirely absent. Root cause: the card was only ever seeded from a partial ~201-zone CSV, not a complete national rate table. **Resolved** by importing a much more complete source (see `alg_tariff_rates` below) — the zone-282-style two-conflicting-facilities case (disambiguated by ALG's own `SiteKey` column, not currently read) is a separate, smaller remaining item. | Marge / Phil | ✅ Resolved 2026-07-15 — see `alg_tariff_rates` |

**Resolved June 22 meeting:**
- **Q1 FSC unit**: Percentage of base freight. DB stores `fsc_amount=0.365` → 36.5% surcharge (decimal fraction, NOT 36.5). `get_fsc_rate()` returns `fsc_amount` directly. Applied as `access_prog = base_tariff × (1 + fsc_pct)`.
- **Q4 Diesel source**: EIA weekly on-highway diesel (`EMD_EPD2D_PTE_NUS_DPG`). Requires `EIA_API_KEY` in `.env`. `get_current_diesel_price()` in `data_layer.py`. As of 2026-07-01, only used as a fallback — `access_prog` prefers the FSC rate parsed directly off the matched ALG invoice (see below).
- **Q5 Z-number flow**: Katie creates load in Prophecy manually (Import → Consolidate → Re-rate → Create Load). Load number = BOL number. Our SID export feeds this import step.
- **Q6 AWP-SQL-PROD**: Access confirmed live.

**Resolved 2026-07-01 (verified live against AWP-SQL-PROD):**
- **Q2/Q7 Destination → ZIP**: `Locations.AccountNumber` via the Pallet→Locations join (e.g. `SCF606` → `606`) is confirmed correct — independently re-run against AWP-SQL-PROD and returns correct per-pallet destination/weight data. Marge's alternate suggestion (`DestinationID`) is not needed. `get_pallet_data_for_manifests()`'s SQL is unchanged and confirmed correct.
- **Q10 VisualMail SELECT permission**: `get_manifest_weights()` and `get_pallet_data_for_manifests()` both succeed live today — the permission is granted (or this was never actually blocking). Both are now load-bearing for `access_prog` (see below), not just SID export.
- **Q8 Prophecy BOL sync**: Already implemented, was just undocumented. `get_technique_data()`'s existing LEFT JOIN to `SQLAPPS3.ShipperPlus_Segerdahl.dbo.shipments` returns `load_id`/`pooled_to_load_id` for every manifest — `pull_technique_data()` (and now the per-record `POST /api/bols/{id}/refresh-bol`) already pick these up automatically via the shared `_apply_bol_status()` helper. No new connection string or schema needed. Live round-trip (real SID export → Katie imports into Prophecy → BOL appears) verified by the user directly, not by an automated test — see `documentation/Developmental Documentation.md`.

**Design decisions (June 22):**
- `prop_reship` column intentionally hidden from dashboard (Prophecy uses wrong 2006 tariff; Katie was manually typing it).
- ALG invoice join key: `BOL No` field in CSV = `str(int(trip.split('T_')[-1]))` (e.g., `TEC_T_0397246` → `'397246'`).
- SID SQL source: `C:\Users\nikhilm\Downloads\Created From Create Import from VM to Prophesy by Manifest.sql`.

## Daily workflow

1. 7/8/9am: Automated pulls load data from Visual Mail (Technique) and parse ALG's invoice email
2. ~10am: Katie opens dashboard, reviews each record
3. Katie approves or flags each record
4. When done: Katie clicks "Send to Accounting" → CSV emailed to both Mary and Katie

## Tech stack

- **Backend**: Python 3.13, FastAPI, SQLAlchemy 2.0, pydantic-settings, psycopg2-binary
- **Frontend**: React 18 (Vite), functional components + hooks, inline styles, no UI library
- **Database**: PostgreSQL locally, designed for AWS RDS in production
- **Email**: smtplib STARTTLS port 587 (O365)

## Critical config flag

```python
USE_MOCK_DATA = True  # in .env or config.py
```

When `True`: all routes use `mock_data.py` (no DB or SQL needed). When `False`: routes use real PostgreSQL + `data_layer.py` functions. **Always default to `True` for prototype work.**

## Data sources — complete inventory

### Static rate tables (seeded once into PostgreSQL)

All three source files are on disk at `c:\nikhilm\billing-freight-automation\`. Run `python -m backend.seed_rates` once against a live DB to load them. Requires `pip install openpyxl>=3.1.0`.

| Table | Source file | Notes |
|---|---|---|
| `tariff_rates` | `SG360_Romeoville Letters-Flats Tariff_Inbounds Included_Effective 04-01-2026.csv` | SCF facility rows only; `Ignore=Y` rows stored but excluded from lookups. Zip3-keyed; the `EP ZIP` column is ALG's own zone **label**, not always a literal zip3 (confirmed 2026-07-15: label "140" is used for a Buffalo-area facility whose real ZIP is 142xx) — a latent source of imprecision. Kept only as a last-resort fallback below `alg_tariff_rates`. |
| `fuel_surcharge_rates` | `SG360_ALG Worldwide Logistics FSC Matrix_06.01.2026.xlsx` (sheet "Direct FSC", rows 10–144) | 135 diesel price bands; `fsc_amount` = decimal fraction (0.365 = 36.5% surcharge) |
| `alg_tariff_rates` | `ALG5_2026_tariff_rates.csv` (exported 2026-07-15 from a live query against `SQLAPPS3.ShipperPlus_Segerdahl.dbo.tariff_details WHERE tariff_id='ALG5_2026'` — no live route to that server from this dev environment, hence the one-time export instead of a `get_alg_tariff_rates()` live query function) | 527 rows, keyed by the exact destination code (`Locations.AccountNumber` format, e.g. `SCF606`/`ASF140`) — confirmed identical to what our own pallet data already carries as `Dest_ID`/`destination_id`, so lookups are an **exact match**, no zip3 slicing or nearest-zone tolerance needed. Confirmed 2026-07-15 via `VM_Locations.xlsx` (VisualMail's own `Locations` table) that every one of these 527 codes has a real ZIP, and cross-checking 2 real invoices found 0% of their zones missing here vs. 71.5% of shipped weight missing from `tariff_rates`. This is the **primary** rate source in `_apply_access_prog_calc()`'s fallback chain now; `tariff_rates` is only reached if a destination code isn't found here. |

**Dest ID lookup chain:** live mode: VisualMail `Locations.AccountNumber = "SCF606"` (Corp/Technique path) or ShipperPlus `destination_id`/`destination_zip` (Wolf/311 path) → exact match against `alg_tariff_rates.dest_id` first; only falls to zip3-keyed `tariff_rates` if not found there. Zip3 derivation (fixed 2026-07-16) now prefers a real ZIP on both paths — `Locations.ZipCode` (added to `get_pallet_data_for_manifests()`'s query) for Corp/Technique, `destination_zip` for Wolf/311 — falling back to slicing the destination code (`dest_id[3:6]`) only if the real ZIP is unavailable; the code's own digits are ALG's zone label, not always the literal zip3 (e.g. "ASF140" → real ZIP 142xx). Mock mode: `access_prog` is hardcoded; no lookup occurs.

**EIA API** (`EIA_API_KEY` in `.env`): fetches the current weekly diesel price. As of 2026-07-01 this is a **fallback only** — `access_prog` prefers the FSC rate parsed directly off the matched ALG invoice (see below), since the real invoice is always already in hand by the time `access_prog` is computed. EIA is used only when a CSV's Fuel Surcharge footer row can't be parsed.

**`access_prog` calculation (last corrected 2026-07-16, supersedes the 2026-07-15/issue #64 version, which this section previously described stale):** Computed in `_apply_access_prog_calc()`, shared by `_process_invoice_csv()` (every invoice upload, including Wolf/311 stub creation) and the `POST /api/admin/recompute-access-prog` backfill. Weight/pallets/pieces must always be **ours**, never ALG's — but the tariff/zone rate structure is legitimately **ALG's own pricing**, so using it is correct, not a violation of independence:
1. Get our own per-pallet `(zone, weight, exact Dest_ID)`: `get_pallet_data_for_manifests()` for Corp/Technique trips, `get_prophecy_pallet_data()` for Wolf/311 (Prophecy-matched) loads. Both a zip3 (for the ALG-invoice-rate and `tariff_rates` lookups below) and the exact `Dest_ID`/`destination_id` string (for the `alg_tariff_rates` exact-match lookup) are carried per pallet. **If our own data comes back empty (manifest/BOL not found, not yet synced), `access_prog` is left `null`** — no independent data means no independent estimate; there is no fallback to ALG's weight.
2. Per pallet, resolve the rate in priority order: (a) ALG's own invoiced rate for that exact zone on **this invoice** — read directly from the CSV's `Rate` column (confirmed populated and accurate on 126 real historical invoices; do **not** derive it as `Billed$/GrossWt`, which silently bakes in ALG's per-shipment minimum-freight charge as if it were a flat rate — e.g. a $70 minimum on a 216 lb parcel implies a fake ~$32/cwt "rate"); exact zip3 match first, else nearest invoice zone within `_ALG_ZONE_TOLERANCE` (±5). (b) If this invoice didn't bill that zone, an **exact match** against `alg_tariff_rates` (see above) on the pallet's own `Dest_ID` — no zip3 involved, no ambiguity. (c) If not found there either, the older zip3-keyed `tariff_rates` card as a last resort — sets `tariff_zone_approximate = true` when reached. **Any of the three paths applies the zone's own minimum freight charge** as a floor on the computed cost — sourced from `alg_tariff_rates.mc1` on the exact `Dest_ID` for paths (a) and (b) (fixed 2026-07-16: path (a) previously sourced its minimum from `tariff_rates.minimum_freight` via `get_tariff_rate()`, which is missing ~64% of real zones — so most pallets priced via ALG's own per-zone rate got no minimum-charge protection at all, systematically under-pricing loads with several small/light shipments; confirmed on one real invoice where 18 of 126 line items hit ALG's real minimum, accounting for $556 of a $571 total shortfall), falling back to `tariff_rates.minimum_freight` only if the exact `Dest_ID` isn't in `alg_tariff_rates` either.
3. **Requires full coverage** (2026-07-15; previously 80%, `_RATE_COVERAGE_THRESHOLD`): if even one pallet's zone resolves via none of the three paths above, discard the per-zone sum entirely and price the whole load at the invoice's own blended $/cwt (`alg_blended_rate` = total freight billed ÷ total billed weight) instead, or leave `access_prog` null if no blended rate is available either. The old 80% threshold let a shipment with, say, 85% zone coverage report only the rated 85% slice's dollars as the whole shipment's cost — silently dropping the other 15% entirely rather than scaling or falling back — which was the single largest driver of the "tiny weight change → huge Cost % swing" symptom in issue #64 (whichever pallet flipped coverage across the 80% line flipped the entire pricing method). A partial per-zone match previously also produced 800–1300% readings in an unrelated earlier incident, which is what originally motivated *some* coverage threshold, just not one this low.
4. FSC comes from the invoice's own "Fuel Surcharge" footer row, but **derived from the two exact dollar figures** (`fsc_cost_val / alg_freight_total`), not the row's own `Rate` label — confirmed 2026-07-15 that ALG's CSV export rounds this label to 2 decimals (e.g. "0.41") while the true rate (confirmed against the matching PDF, and by this dollar-derived formula matching to 4 decimals on 8/8 invoices checked) is more precise (e.g. 0.4050). This is the opposite of the per-zone freight `Rate` in point 2(a), which — unlike this FSC row — is *not* derived from dollars, precisely because that column's own printed value is already exact for freight (confirmed on 126 invoices) while this one demonstrably isn't.
`access_prog`/`base_tariff`/`fsc_pct` are recomputed fresh from our own data on every invoice upload for a trip (not accumulated per-invoice like `amount` — our own weight doesn't change just because a second Z-invoice arrived). Historical records whose original invoice CSV can no longer be located (`INVOICE_FOLDER` file since moved/deleted) can't be backfilled with this formula and are left as-is — `POST /api/admin/recompute-access-prog` reports these as `skipped_no_file` rather than guessing.

### Live SQL queries — `data_layer.py` (AWP-SQL-PROD)

All queries connect to AWP-SQL-PROD. TECH, SegGroup, and SQLAPPS3 are linked servers accessible from there. Requires `pip install pyodbc "sqlalchemy[mssql]"`.

| Function | Status | Returns |
|---|---|---|
| `get_technique_data(days_back)` | ✅ Implemented | trip, manifest, pallets, VM pieces — **no weight** |
| `get_manifest_weights(manifests)` | ✅ Implemented | weight, pieces, pallets per manifest (separate query) |
| `get_manifest_weights_from_sid(manifests)` | ✅ Implemented (2026-07-16) | Same shape as `get_manifest_weights()`, aggregated from `get_pallet_data_for_manifests()` instead — used by `refresh-bol` once a record has a `bol_number`, see "Ambiguous trips" below |
| `get_pallet_data_for_manifests(manifests)` | ✅ Implemented | per-pallet rows for SID export **and** for `access_prog` (below); `Dest ID` = `Locations.AccountNumber` (e.g. `SCF606`) — confirmed correct, see Open Questions |
| `get_prophecy_pallet_data(bol_number)` | ✅ Implemented | per-order-header rows from ShipperPlus `order_headers` (`destination_id`/`destination_zip`, `weight`) — the Wolf/311 equivalent of `get_pallet_data_for_manifests()`, used for `access_prog` on Prophecy-matched loads |
| `get_tariff_rate(zip3, weight)` | ✅ Implemented | one pallet's `access_prog` = base rate × (1 + FSC); also returns `is_exact_zone_match` |
| `get_prophecy_data(bol_number)` | ✅ Implemented | prophecy weight/pallets/pcs from ShipperPlus via the SQLAPPS3 linked server on AWP-SQL-PROD (only path); pallet count formula needs Megha confirmation |
| `get_alg_invoice(invoice_number)` | ⬜ Stub | Z-number, amount, alg weight/pal/pcs — workaround: manual CSV upload |

**Weight split**: `get_technique_data()` does NOT return weight. The morning pull (`POST /api/admin/pull`) always calls both Query A (`get_technique_data`) + Query B (`get_manifest_weights`) and merges by manifest number.

**ALG quantity fields** (`alg_weight`, `alg_pallets`, `alg_pcs` on `BOLRecord`): null until a CSV is uploaded via `POST /api/invoices/upload`. `weight_diff`, `pallet_diff`, `pcs_diff` are computed at upload time and stored, and are also recomputed against the latest `technique_weight`/`pallets`/`pcs` on every `pull_technique_data()` re-pull (`_compute_diffs(row)`), so they stay current as manifest-side numbers change — not just a one-time snapshot from upload.

**Invoice matching — `_process_invoice_csv()` in `main.py` (~line 1950):** shared by manual upload and email polling. Tried in this order — exact matches always before the Prophecy-BOL guess (fixed 2026-07-01, issue #31: a real trip whose numeric suffix coincidentally starts with "14" was being misclassified as a Wolf/311 load before this reorder):
1. **Z-number**: CSV Z-number → `invoice_number` field on `BOLRecord`
2. **Job Name as trip suffix**: CSV "Job Name" field → trip DespatchID suffix (`_trip_to_suffix()`, e.g. `TEC_T_0397246` → `"397246"`). One trip can have several manifests — `_closest_technique_match()` scores every individual manifest plus one synthetic "whole trip" candidate (summed quantities across all of them) against the invoice's own billed weight/pallets/pcs; if the trip-sum candidate wins, the invoice attaches to one primary manifest (prefers one with a BOL already, else the heaviest) and every other manifest on the trip gets an explanatory note but is otherwise left alone.
   - **2b. Manifest suffix fallback (added 2026-07-15, issue #65)**: if no trip shares this suffix at all, try the same idea against the **manifest**'s own suffix instead (`_manifest_to_suffix()`, e.g. `TEC_M_0228920` → `"228920"`) — a trip and its manifest are genuinely different numbers, so some invoices' Job Name reflects the manifest rather than the trip. No trip-sum candidate here (summing unrelated manifests that only coincidentally share a suffix wouldn't mean the invoice covers all of them).
3. **Job Name as Prophecy BOL** (Wolf/311 — no Technique trip for this load): only checked once steps 1-2 rule out a real trip/manifest match. `BOL No` is a Post Office permit number, never used for matching.
4. **Pallets + pieces** (last resort, non-comingle only, exact count match against a single unmatched record — logs a warning to verify manually)
   - **4b. Wide live fallback (added 2026-07-20, `_wide_fallback_technique_search()`)**: if nothing in the already-pulled DB data matched at all (skipped for Wolf/311 and comingle invoices), check live Technique data across a 90-day window — same trip-then-manifest suffix logic as steps 2/2b, just against a much wider date range — before giving up and creating a stub. Catches a real trip whose despatch date lags outside the daily pull's normal 20-day window. This used to run as a bulk sweep over every remaining stub inside `pull_technique_data()` itself; moved to run once per newly-unmatched invoice instead (at upload/poll-folder time) because stacked onto that route's own live pull it reliably exceeded API Gateway's 30s timeout — see `pull_technique_data()`'s removal note and the `/api/admin/pull` route entry above.
5. **No match**: create stub record (invoice-only, `technique_trip` is null) — now only reached once 4b also finds nothing live.

`POST /api/bols/{id}/retry-match` reuses the same trip-suffix → manifest-suffix fallback order and `_closest_technique_match()` scoring (fixed 2026-07-15, issue #65 — previously took whichever Technique candidate the query happened to return first, with no scoring, when multiple manifests shared a suffix).

**Ambiguous trips / resolution-preference matching (added 2026-07-16, per a call with Katie):** in Technique, one trip can split into several manifests — commonly because one job on the trip should be billed third-party and the project manager either does or doesn't mark that correctly (`TranType`/`Notes`, unreliable and out of our control). When it's not marked correctly, `_closest_technique_match()`'s quantity-closeness scoring has produced real wrong attachments in production (invoice attached to a 5-pallet/15,000-piece manifest when the real one was 36 pallets/24,000 pieces). Root-cause note: comparing `get_manifest_weights()` (Query B) against `get_pallet_data_for_manifests()` (the SID-export query) confirmed both use the identical `Pallet.ID = Manifest.ManifestID` join and `Pallet.Active=1` filter — the SID query only adds extra `INNER JOIN`s that can *narrow*, never widen, its result — so this was never a wrong-query bug, only a wrong-manifest-attribution bug.
- `is_ambiguous_trip` (Boolean, on `BOLRecord`): set fresh on every `pull_technique_data()` pull — true when the manifest's `technique_trip` had more than one manifest in that pull. Un-flags automatically if the trip resolves to one manifest on a later pull.
- `_partition_candidates_by_resolution()` in `main.py`: given several trip-suffix or manifest-suffix candidates, excludes any already marked `is_third_party` (unless that would empty the pool), then splits the rest into `resolved` (already has a `bol_number` — Katie created the real Prophecy BOL via her SID-export flow) vs not. Applied in Strategy 2 and 2b above: exactly one resolved candidate → attach directly, skip quantity scoring entirely; multiple resolved → score only among the resolved ones (an unresolved manifest never outscores one Katie's already confirmed); zero resolved → unchanged quantity-closeness behavior, since the invoice can arrive before Katie's ~10am review and holding the match entirely would force every multi-manifest trip into a stub.
- Frontend: `isUnverifiedQuantity()` in `BOLRow.jsx` derives `is_ambiguous_trip && !bol_number && !is_third_party` — an ambiguous manifest Katie hasn't resolved yet. Badged `~UNVERIFIED` next to the weight cell, same visual pattern as the `~EST` badge below. Deliberately does **not** read `notes_3pl`/`TranType` or auto-set `is_third_party` — resolution only comes from actions Katie already takes herself.
- **Manual verification UI (added 2026-07-21):** the `~UNVERIFIED` badge is clickable whenever `is_ambiguous_trip` is set, opening `CompareManifestsModal.jsx` — fetches `GET /api/bols/{id}/trip-manifests` (see API Routes table) to show every manifest on the trip side by side with whichever one actually holds the invoice, each scored via `_score_technique_candidates()` (the full-ranked-list sibling of `_closest_technique_match()`, which still only returns the single best match). Assigning the invoice to a different sibling reuses the existing `reassign-invoice` route unchanged, targeted by the sibling's own `manifest` string (never a bare trip-suffix — ambiguous among siblings by definition). This is intentionally a human-decision tool, not an auto-resolver; `_score_technique_candidates()` returning the full ranked list (not just a winner) is meant to be reusable by a future automated resolver, but nothing acts on it automatically today.

**Comingle invoices** (CSV "Cust Job No" starts with `"CM"`): always create a stub with `access_prog=null` and `cost_pct=null`. These are comingle loads that have no Technique record to match against — label them "Comingle — no Technique match". Non-comingle unmatched stubs also get `access_prog=null` / `cost_pct=null`.

**Multiple Z-numbers per trip**: `amount` is **additive** across uploads; `alg_weight`/`alg_pallets`/`alg_pcs` are **not** — first upload wins for quantities. This avoids double-counting when a load is split across invoices.

**Note on BOL numbers**: BOL numbers are created by Katie in Prophecy *after* the morning data loads. The `bol_number` column is nullable. Records are identified by `technique_trip + manifest + invoice_number` before a BOL exists.

## Real data field formats

```
BOL number:    integer, e.g. 145547       (nullable until created in Prophecy)
Trip ID:       TEC_T_0109878              (nullable — blank rows belong to trip above)
Manifest:      TEC_M_0228920              (standard)
               CM_052926A                 (comingle — future Module 2)
Invoice:       Z555216                    (Z + 6 digits — generated in Prophecy, referenced on ALG invoice)
Invoice sender: "Tanya 6/10/2026 4:21PM"
Weight:        8,000–416,000 lbs          (use Numeric(12,2) — NOT Numeric(10,2))
Pieces:        100,000–700,000
Amount:        $249–$27,019
Cost %:        stored as ratio 1.0121 = 101.21% (access_prog / amount) — changed 2026-07-16, was amount / access_prog
```

## Key variance metric

**Cost %** = `access_prog / amount` (our calculated Access program rate ÷ ALG's actual invoice) — flipped 2026-07-16 (was `amount / access_prog`) so that when our calculated cost is *higher* than what ALG actually billed, the percentage reads *above* 100%, not below. `>100%` = ALG billed less than we calculated (or our calc overshot); `<100%` = ALG billed more than we calculated. Existing historical records keep their pre-flip value until reprocessed (invoice re-upload or `POST /api/admin/recompute-access-prog`) — there is no separate migration, since that same endpoint already recomputes both `access_prog` and `cost_pct` together.

Color thresholds (symmetric around 100% either way, unaffected by the flip):
- Green: within 3% of 100% (0.97–1.03)
- Orange: 3–6% off (0.94–0.97 or 1.03–1.06)
- Red: >6% off (<0.94 or >1.06)

Quantity differences (weight_diff, pallet_diff, pcs_diff) are secondary — shown with sign but no hard threshold.

**`tariff_zone_approximate`** / **`weight_source_fallback`** (Boolean, on `BOLRecord`): when either is true, Cost % for that record carries a caveat — a rate had to be approximated, or our own pallet data wasn't available and the calc fell back to ALG's self-reported weight. Surfaced in the dashboard as a `~EST` badge next to Calculated Cost. Separately, `is_ambiguous_trip` surfaces as a `~UNVERIFIED` badge on the weight cell itself — see "Ambiguous trips / resolution-preference matching" above; this flags the quantities, not the cost calc.

## Database schema highlights

- UUID surrogate PKs everywhere
- `bol_number` nullable Integer
- `Numeric(12,2)` for weights (up to 416,000 lbs)
- `Numeric(10,2)` for dollar amounts
- `Numeric(8,6)` for cost_pct / fsc_pct ratios
- `base_tariff` / `fsc_pct` (Numeric): rate breakdown tooltip; `access_prog = base_tariff × (1 + fsc_pct)`
- `alg_fsc_pct` / `alg_fsc_cost` (Numeric): ALG's own reported FSC rate/cost for the matched invoice, parsed from the CSV's "Fuel Surcharge" row — this is what feeds `fsc_pct` now, not EIA
- `tariff_zone_approximate` / `weight_source_fallback` (Boolean, default False): flag when `access_prog` had to approximate a rate or fall back to ALG's own weight — see "Key variance metric"
- `is_ambiguous_trip` (Boolean, default False): flag when this manifest's trip had more than one manifest in the most recent pull — see "Ambiguous trips / resolution-preference matching"
- `is_third_party` (Boolean): excludes from SID + accounting exports; reversible
- `is_do_not_pay` (Boolean): marks an unmatched invoice-only record do-not-pay — unlike `is_third_party`, does NOT exclude from the accounting export; it's included and rendered as "DO NOT PAY"/"DNP" instead of an amount. Setting it also sets `status=approved`. Reversible
- `needs_sid_export` (Boolean): True = Type A record (no BOL yet); False = Type B (BOL already in Prophecy). `_apply_bol_status()` (shared by the bulk pull and `POST /api/bols/{id}/refresh-bol`) only flips a record back to Type A when it never had a `bol_number` — once a record is Type B, a later Technique/ShipperPlus query returning no `load_id` is treated as a transient query/join hiccup, not proof the BOL vanished from Prophecy, so `bol_number`/`needs_sid_export` are left alone rather than flip-flopped
- `match_strategy` (String): how the invoice was matched — `"invoice_number"` (Z-number re-upload), `"job_name"` (trip or manifest suffix, or a re-scored retry-match), `"prophecy_bol"` (Wolf/311, Job Name is a Prophecy BOL), `"pallets_pieces"` (last-resort exact quantity match), or `"invoice_only"` for an unmatched stub — never null once an invoice has been processed
- `accounting_exported_at` nullable DateTime — set when "Send to Accounting" runs; exposed in Log tab
- `sid_exported_at` nullable DateTime — set when a record's SID CSV is downloaded (bulk `GET /api/export/prophecy-sid` or per-record `POST /api/bols/{id}/export-prophecy-sid`); previously existed but was never written until 2026-07-02
- `approval_history` table for full audit trail
- `users` table stubbed for future auth

## File layout rationale

```
backend/main.py          — All routes in one file (Module 1 only; split by module when Module 2 ships)
backend/config.py        — Pydantic BaseSettings; loads .env with typed defaults for all keys (DB, SMTP,
                           EIA API, IMAP, USE_MOCK_DATA). Single source of truth for config — no hardcoded
                           values elsewhere. Also patches `socket.getaddrinfo` at import time with static
                           IP overrides for Lambda VPC DNS (DNS resolver unreachable in current VPC/subnet;
                           direct TCP to all real hosts works fine). If `AWS_SECRET_NAME` env var is set,
                           Settings are loaded from AWS Secrets Manager instead of `.env`.
backend/data_layer.py    — The integration boundary; get_prophecy_data implemented; get_alg_invoice still stub (workaround: manual CSV upload)
backend/mock_data.py     — 16 records at real scale; safe to delete when DB is live
backend/email_parser.py  — O365 IMAP4_SSL polling (outlook.office365.com:993); marks emails read even
                           with no CSV attachment (prevents re-scan loop on next poll)
backend/email_service.py — SMTP STARTTLS export; returns False (soft-fail, no exception) when credentials
                           missing — POST /api/export still returns HTTP 200
backend/csv_export.py    — Three exports: accounting CSV (18 cols), Prophecy SID (13 cols with underscore
                           names — any column name difference breaks Prophecy import),
                           generate_mock_sid_rows() for mock-mode SID
backend/models.py        — All SQLAlchemy ORM models (BOLRecord, TariffRate, FuelSurchargeRate,
                           ApprovalHistory, User) + all Pydantic schemas (BOLSummary, etc.).
                           ⚠️ The FuelSurchargeRate docstring says "fsc_amount/100" — this is WRONG.
                           The actual stored value is a decimal fraction (0.365 = 36.5%); do NOT divide by 100.
backend/database.py      — SQLAlchemy engine; pool_pre_ping=True is required for RDS idle-timeout reconnect
backend/main.py lifespan — DB schema migrations are inline `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` calls
                           at startup (no Alembic). Add new columns here, not as separate migration scripts.
backend/test_data/       — Sample ALG invoice CSVs for testing the upload flow in mock mode
                           Z555226_test.csv → matches trip TEC_T_0109888 (BOL No 109888)
                           Z555227_test.csv → matches trip TEC_T_0109889 (BOL No 109889)
test_invoices_0622/      — 26 real Z-number CSVs from Tanya's June 22 email (e.g. Z557707.CSV).
                           Use for live-mode invoice upload testing. NOT committed — real production
                           data; add to .gitignore if not already excluded.
documentation/           — Six .md spec files (Design & Workflow, Requirements & SQL Mapping,
                           SECURITY.md, SG360_BOL_Project_Context.md, Agentic Automation Architecture.md,
                           etc.). Reference for business rules and SQL source queries; not runtime code.
                           Developmental Documentation.md is the running dev changelog — one entry per
                           closed GitHub issue, appended by the `/commit` command
                           (`.claude/commands/commit.md`). Read it for recent history that isn't yet
                           folded into this file.
backend/agents/          — Design-only. `Agentic Automation Architecture.md` specs an LLM-agent layer
                           (Task Registry, Proposals, human-in-the-loop review) that would live here,
                           but no implementation is committed — the directory currently holds only a
                           stale `__pycache__` from a deleted prototype (`runner.py`/`classify.py`/`llm.py`
                           .pyc files with no matching .py source). Don't assume any of it is wired up;
                           check for actual .py files before referencing this layer as if it exists.
README.md / ONBOARDING.md — README.md: local setup only (defers to this file for everything else).
                           ONBOARDING.md: non-technical project overview for anyone picking this up cold
                           (people, data sources, core features) — written 2026-07-01 and now stale on
                           deployment status (still says "no live deployment yet"; see "AWS Lambda
                           deployment" below for current state).
Dockerfile               — Lambda container image build; live (see "AWS Lambda deployment" below)
terraform/bootstrap/     — One-time remote-state backend (S3 + DynamoDB), local state; see "AWS Lambda deployment"
terraform/main/          — The actual deployed infra: Lambda, API Gateway, Aurora, CloudFront, WAF, S3 (frontend + invoices), ECR. Tracked in git; terraform.tfvars holds the live lambda_image_tag. State is still local (not migrated to the bootstrap-provisioned S3 backend yet).
deploy.ps1               — Builds/pushes the backend image and bumps terraform.tfvars (stops before `terraform apply` by design — human review gate), and fully automates the frontend build/S3 sync/CloudFront invalidation. Wrapped by the `deploy` skill.
frontend/src/main.jsx    — Vite entry point; mounts <App /> only, no router
frontend/src/App.jsx     — Owns all state + fetch/mutation handlers; passes data+callbacks down as props
frontend/src/components/
  SummaryBar.jsx              — Pending/approved/flagged counts strip
  BOLTable.jsx                — Pending + flagged records table (wraps BOLRow)
  BOLRow.jsx                  — Single record row; Approve, Flag, Third-party, Do Not Pay buttons; also exports isDoNotPayEligible(), isThirdPartyEligible(), isUnverifiedQuantity()
  ApprovedSection.jsx         — Approved records grouped into per-sender batch cards + SID export + Send to Accounting flow; do-not-pay rows render "DO NOT PAY" and an Undo action
  ThirdPartySection.jsx       — Third-party records (customer pays direct); excluded from SID export
  FlagModal.jsx               — Modal overlay for entering a flag reason
  ReassignInvoiceModal.jsx    — Modal for moving an invoice to a different trip (preview/merge/replace)
  CompareManifestsModal.jsx   — Opened from the clickable ~UNVERIFIED badge; compares every manifest on an
                                ambiguous trip against the matched invoice, scored, with an inline assign
                                action per sibling (reuses reassign-invoice, no separate mutation route)
  BulkActionToolbar.jsx       — Floating bar shown when rows are multi-selected; bulk approve/flag/third-party/do-not-pay
  EmailComposeModal.jsx       — Builds an HTML table for selected records and calls mark-accounting-sent on send
  LogSection.jsx              — Historical log viewer (separate tab)
```

## Frontend patterns

No Context, no reducer, no router, no `useMemo`/`useCallback` anywhere in `frontend/src` — just `useState`/`useEffect`/`useRef`, all lifted to `App.jsx` (~1230 lines) and passed down as props. Conventions to match when extending it:

- **Inline styles only** — no CSS modules, Tailwind, or styled-components. Style objects are built ad hoc per component (e.g. `TD`/`TD_R` constants in `BOLRow.jsx` for shared cell styles). Colors are hardcoded hex per-component (`#2D6A4F` green, `#dc2626` red, etc.) — there's no shared theme/constants file, so matching an existing color means grepping for its hex value in the relevant component.
- **Modals** (`FlagModal.jsx`, `ReassignInvoiceModal.jsx`) are conditionally rendered inline in `App.jsx`'s JSX based on a target-id state variable (e.g. `flagTarget`, `reassignTargetId`) — not a portal, not router-based.
- **Debounced auto-save** (pattern currently unused): `App.jsx`'s `onNotesUpdate` / `PATCH /api/bols/{id}/notes` still exist, but the editable input that drove them was removed from `BOLRow.jsx` on 2026-07-14 pending a notes redesign — reuse the 500ms `setTimeout` debounce approach (local `useState` + `useRef`) if re-adding a notes UI or building similar autosave elsewhere.
- **Bulk-select**: `selectedIds` is a `Set` in `App.jsx` state, with `toggleSelect`/`toggleSelectAll`/`clearSelection` helpers; `BulkActionToolbar.jsx` reads from it.
- **Module 2 refactor seam**: `App.jsx` has an in-code comment marking the intended split when Module 2 ships — extract fetch helpers to `src/api/bolsApi.js` and move this state/logic to `src/pages/BolReconciliation.jsx`. Don't do this preemptively; it's noted for when a second module actually needs the shared shell.

## Known bugs

**`days_back` is 20** in `pull_technique_data()` — re-pull refreshes technique-side fields (weight/pallets/pcs, BOL status) but preserves invoice matches, approvals, flags, and notes on existing records. Safe to re-run any time. This route's own stub-rematch pass is now DB-only (no live query) — the live wide-fallback search that used to run here moved to `_process_invoice_csv()` on 2026-07-20 (see `/api/admin/pull` route entry above) after repeatedly exceeding API Gateway's 30s timeout when combined with this route's own live pull.

**`_finish_resolving_stub()` (new 2026-07-17, `main.py`):** shared helper covering the two things `_apply_invoice_match()` does inline during a normal upload that every other stub-resolution path had been skipping — copying `invoice_email_sender`/`invoice_sent_at` from the stub, and re-locating/re-parsing the record's own invoice CSV to compute `access_prog`/`base_tariff`/`fsc_pct`/`cost_pct` (same logic `POST /api/admin/recompute-access-prog` uses). Called from `pull_technique_data()`'s DB-side re-match and wide-fallback blocks, and from `POST /api/bols/{id}/retry-match`. No-ops silently (leaves cost fields null) if `INVOICE_FOLDER` isn't configured or the original file can't be found — same resilience contract as the recompute-access-prog backfill.

**Mock state** (`_mock_state` in `main.py`): in-memory dict initialized from `MOCK_BOLS` at startup. Mutations (approvals, flags, invoice uploads) survive the process lifetime but reset on every backend restart. Restart the backend to reset all records to their initial pending state during development.

**Mock mode now supports the full daily workflow end-to-end:**
1. Records 11 and 12 start without invoice data — upload the test CSVs to fill them in
2. SID export (`GET /api/export/prophecy-sid`) generates synthetic pallet rows from approved records
3. Email export logs to console instead of sending (SMTP not configured)

## AWS Lambda deployment — live

Fully serverless, deployed and actively used for testing (first deployed 2026-07-09, redeployed several times since). **There is no EC2 instance or long-running process** — the backend is a Lambda container image, replaced wholesale on each deploy; AWS handles starting/stopping execution environments automatically.

- **Backend**: Lambda function `sg360-bol-api` (container image, pulled from ECR by digest) behind an API Gateway HTTP API (`AWS_PROXY` integration, `$default` stage). `Dockerfile` (repo root) builds `public.ecr.aws/lambda/python:3.13`, installs `backend/requirements.txt`, copies `backend/` (including `test_data/`, needed at runtime for mock mode), entrypoint `backend.main.handler`. `backend/main.py` ends with `handler = Mangum(app)` — wraps the same FastAPI `app` used by uvicorn locally, no route code changes needed.
- **Database**: Aurora Serverless v2 Postgres (`sg360-bol-aurora`), VPC-private — reachable only from the Lambda's own security group, not from a dev machine directly.
- **Frontend**: S3 static bucket (`sg360-bol-frontend`) + CloudFront, deployed via `deploy.ps1 -Frontend` (build → `aws s3 sync --delete` → CloudFront invalidation).
- **Invoice PDFs**: separate private S3 bucket (`sg360-bol-invoices`); Lambda role has `PutObject`/`GetObject` only (no delete).
- **WAF**: CloudFront-scoped WAFv2 web ACL. Currently `default_action = allow{}` (opened 2026-07-14 for testing — testers' egress IPs rotate through a NAT/VPN faster than an IP allowlist can track; the CloudFront URL isn't linked/indexed anywhere so this is obscurity, not real access control). The IP-allowlist rule is left intact in `terraform/main/waf.tf` — flip back to `block{}` before any real production rollout.
- **Secrets**: Lambda reads `AWS_SECRET_NAME=sg360-bol-live-credentials` from Secrets Manager instead of `.env` (see `backend/config.py`), including `DATABASE_URL` — manually resynced 2026-07-16 after AWS's automatic rotation of Aurora's real master password (see `manage_master_user_password = true` in `aurora.tf`) silently invalidated the previous copy, causing an outage.
- **Provisioned concurrency** (added 2026-07-20, `lambda.tf`): keeps 1 execution environment permanently warm on a `live` alias. A cold start (fresh container, Python imports, Secrets Manager fetch, first Aurora connection — measured ~13–23s) stacked on top of the live AWP-SQL-PROD query itself (~15–23s) reliably pushed `POST /api/admin/pull` past API Gateway's hard 30s timeout; this removes the cold-start cost entirely (the on-prem query's own latency is unaffected). Requires `publish = true` on `aws_lambda_function.app` — provisioned concurrency can only target a published version, never `$LATEST`; the `live` alias moves to each new version automatically on deploy.
- **`lambda_sql_access` security group's `description` is frozen — do not edit it.** AWS treats a security group `description` as immutable, so any change forces Terraform to destroy-and-recreate the whole group. That was attempted 2026-07-20 and caused an outage: provisioned concurrency (above) keeps a warm execution environment permanently attached to the group's ENI, which AWS could never detach, so the apply died on a 45-minute ENI-detach timeout mid-replacement — with the group sitting at zero egress rules for the whole window. Recovered by manually restoring the 9 rules via the AWS API and reconciling with `terraform import`. The description text still reads "...AWP-SQL-PROD and SG360-TECH-PRD1" even though that path was removed the same day (see changelog) — that's known and intentional, not a bug to fix reflexively. To ever change it safely: remove provisioned concurrency from `lambda.tf` first, apply the security-group replacement, then re-add provisioned concurrency. Full incident writeup is in the comment block at the top of `terraform/main/lambda_sql_security_group.tf`.
- **Planned (not yet live)**: `backend/config.py` supports reading `DATABASE_URL` directly from Aurora's own auto-rotated secret instead of the manually-synced copy above (via `RDS_MASTER_SECRET_ARN`/`DB_HOST`/`DB_PORT`/`DB_NAME`), which would eliminate this class of bug permanently. Not yet wired up in `terraform/main/lambda.tf`/`iam.tf` — needs an `iam:PutRolePolicy` grant the deploying user doesn't currently have. Same blocker applies to a planned CloudWatch-alarm-on-Lambda-Errors → SNS email alert (needs `sns:CreateTopic`/`cloudwatch:PutMetricAlarm`). Until one of these lands, a future auto-rotation will silently break the DB connection again — watch for it, or revisit getting the IAM grant.
- **DNS workaround**: the link-local DNS resolver is unreachable from this Lambda's VPC/subnet. `backend/config.py` monkey-patches `socket.getaddrinfo` with static IPs for Secrets Manager, Aurora, the two on-prem SQL hosts, and the S3 endpoint — this is how Lambda reaches on-prem AWP-SQL-PROD/ShipperPlus and S3 despite the broken resolver. Stopgap, not a permanent fix; re-resolve and update if AWS's underlying IPs ever shift.
- **Terraform**: `terraform/main/` defines all of the above (`lambda.tf`, `apigateway.tf`, `aurora.tf`, `frontend.tf`, `invoices_s3.tf`, `waf.tf`, `ecr.tf`, `iam.tf`). State is still local (not migrated to the S3 backend `terraform/bootstrap/` provisioned). `terraform.tfvars` is tracked in git and holds the live `lambda_image_tag`.
- **Deploying**: use the `/deploy` skill (`.claude/skills/deploy`), which wraps `deploy.ps1` — build/push/plan for the backend (stops for a human-reviewed `terraform apply`), fully automatic build/sync/invalidate for the frontend.

## Future modules (do not implement yet)

- **Module 2**: Sheet 2 / Mary Group workflow (same pattern, different recipient)
- **Commingle billing**: `CM_` manifests already appear in Module 1 data
- **ALG email parsing**: Need sample email from Katie; stub is `get_alg_invoice()` in `data_layer.py`
- **Prophecy BOL creation**: Long-term goal to create BOLs here instead of in Prophecy
- **Scheduled pulls**: 7/8/9am cron jobs calling `data_layer.py` functions
- **Auth**: `users` table ready; add `fastapi-users` or JWT without touching existing routes
- **AWS RDS**: Change `DATABASE_URL` in `.env` — no code changes needed
