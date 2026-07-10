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
python -m backend.seed_rates [--tariff PATH] [--fsc PATH]
```
Omit flags to use the default source paths under `c:\nikhilm\billing-freight-automation\`.

There are no automated tests and no linter configured for the frontend. Verify changes manually via the dashboard at `http://localhost:3000` and the FastAPI docs at `http://localhost:8000/docs`.

**Other frontend scripts:**
```powershell
cd frontend && npm run build     # production build (not currently deployed anywhere — no CI/CD)
cd frontend && npm run preview   # serve the production build locally
```

**Vite dev proxy:** The frontend calls bare `/api/*` paths. Vite proxies them to `http://localhost:8000`. Never hardcode `localhost:8000` in frontend code — the proxy handles it.

**Launching the app for verification:** use the `run` skill (`.claude/skills/run`) — it starts both servers, waits for health checks, and screenshots the dashboard.

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
EIA_API_KEY=                    # eia.gov/developer (free) — for weekly diesel FSC lookup
SMTP_USER=user@sg360.com
SMTP_PASSWORD=
ALG_SENDER_EMAIL=               # Tanya's email address — filters IMAP search to her messages only
IMAP_MAILBOX=INBOX              # folder to poll (default INBOX)
INVOICE_FOLDER=\\sg360-wbapp-prd\Logistics\AgentsInvoices\Invoices to Process
TECH_PRD1_SERVER=SG360-TECH-PRD1  # direct ShipperPlus host (default); used by get_prophecy_data()
TECH_PRD1_USER=                 # blank = Windows auth to SG360-TECH-PRD1
TECH_PRD1_PASSWORD=
INVOICE_S3_BUCKET=              # S3 bucket for invoice PDFs (Lambda only); empty = use INVOICE_FOLDER
```

**AWS Lambda only:** if `AWS_SECRET_NAME` env var is set, `config.py` loads all settings from AWS Secrets Manager instead of `.env`. This overrides everything above — the Lambda function uses this to pull credentials from a single Secrets Manager secret rather than individual env vars.

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
| POST | `/api/bols/{id}/ignore` | Mark record as ignored — stays in log, excluded from exports, reversible |
| POST | `/api/bols/{id}/unignore` | Remove ignored flag |
| POST | `/api/bols/mark-accounting-sent` | Set `accounting_exported_at = now()` on a list of record IDs; removes them from Approved view |
| POST | `/api/bols/{id}/reassign-invoice` | Move invoice to a different trip/BOL/manifest; body: `{ target, action: preview\|merge\|replace }` |
| PATCH | `/api/bols/{id}/notes` | Auto-save notes field (called by frontend with 500ms debounce) |
| POST | `/api/admin/pull` | Pull Technique manifests from AWP-SQL-PROD (disabled in mock mode) |
| POST | `/api/admin/refetch-bols` | Re-query Technique for specific manifests; updates `bol_number` after Prophecy import (live mode only) |
| POST | `/api/admin/poll-email` | Poll O365 IMAP for unread ALG invoice emails → extract CSVs → process (live mode only) |
| POST | `/api/admin/reset-invoices` | Dev: clear invoice fields on all records + delete invoice-only stubs |
| POST | `/api/admin/recompute-diffs` | Backfill weight_diff/pallet_diff/pcs_diff on existing records via `_compute_diffs()` (live mode only); pure DB recompute, no live Technique/Prophecy query |
| POST | `/api/admin/recompute-access-prog` | Backfill Calculated Cost on existing matched records — re-locates and re-parses each record's original invoice CSV (`_find_invoice_file(..., require_csv=True)`) since ALG's per-zone rate isn't stored anywhere else, then re-runs `_apply_access_prog_calc()` with a fresh live pallet-data query (live mode only); records whose original file can't be found are reported as `skipped_no_file`, not guessed at |
| POST | `/api/invoices/upload` | Upload ALG invoice CSV(s) — pass a whole sender folder (`invoice_folder_name`, parsed the same way as `poll-folder`) or fall back to manual `invoice_sender`/`invoice_date`/`invoice_time` fields; response includes `conflict` key if trip already had an invoice |
| GET | `/api/invoices/{z}/file` | Serve the invoice file for a Z-number; checks S3 (`INVOICE_S3_BUCKET`) first with a presigned-URL redirect, then falls back to `INVOICE_FOLDER` (live) or `test_data/` (mock), searching root + one level of dated sender subfolders; prefers PDF over CSV |
| POST | `/api/invoices/poll-folder` | Scan `INVOICE_FOLDER` (root + one level of dated sender subfolders) for unprocessed CSVs → process each, parsing sender/date from the subfolder name; files stay in place, dedup via DB `invoice_number` |
| GET | `/api/export/invoice-pdfs` | Download a merged PDF of all invoice PDFs for the given `?invoice_numbers=Z1,Z2,...`; fetches each from S3 (`INVOICE_S3_BUCKET`) first, falls back to `INVOICE_FOLDER`; skips any not found rather than failing the whole batch; returns 404 if none found. Triggered by "Download Invoice PDFs" in EmailComposeModal |
| GET | `/api/export/prophecy-sid` | Download Prophecy SID import CSV for approved manifests (live mode only); also stamps `sid_exported_at` on each included record |
| POST | `/api/bols/{id}/export-prophecy-sid` | Per-record SID export — pushes one pending Type A record to Prophecy without waiting for a batch approval; same CSV logic as the bulk route, scoped to one manifest |
| POST | `/api/bols/{id}/refresh-bol` | Refresh one record's manifest-side data: re-pulls weight/pallets/pieces from VisualMail (`get_manifest_weights()`) and checks Prophecy for a BOL (`get_technique_data()` filtered to one manifest), without a full Technique pull — ~10s live (hits AWP-SQL-PROD), near-instant if the record already has a BOL. Does not touch invoice-side fields (access_prog/cost_pct/amount/alg_*) |
| POST | `/api/bols/{id}/retry-match` | On-demand retry for one unmatched (`match_strategy=invoice_only`) invoice stub — checks a wide 21-day Technique window immediately instead of waiting for the next Pull Manifests click |
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
| 11 | **`tariff_rates` coverage gaps**: Confirmed (2026-07-01, against a real invoice) that at least 3 destination zones (253, 231, 235) are entirely absent from the source rate card, and zone 282 has two conflicting rates for two different facilities (disambiguated by a `Drop Ship Site Key` column in the source spreadsheet, which ALG's own invoices reference via their `SiteKey` column — not currently used in our lookup). `access_prog` now falls back to that same invoice's own billed rate for a gap zone before guessing a nearest zone, which covers the common case, but the rate card itself should still be completed. | Marge / Phil | ❓ Open — less urgent now that ALG's own invoiced rate is a working interim fallback |

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

Both source files are on disk at `c:\nikhilm\billing-freight-automation\`. Run `python -m backend.seed_rates` once against a live DB to load them. Requires `pip install openpyxl>=3.1.0`.

| Table | Source file | Notes |
|---|---|---|
| `tariff_rates` | `SG360_Romeoville Letters-Flats Tariff_Inbounds Included_Effective 04-01-2026.csv` | SCF facility rows only; `Ignore=Y` rows stored but excluded from lookups |
| `fuel_surcharge_rates` | `SG360_ALG Worldwide Logistics FSC Matrix_06.01.2026.xlsx` (sheet "Direct FSC", rows 10–144) | 135 diesel price bands; `fsc_amount` = decimal fraction (0.365 = 36.5% surcharge) |

**EP ZIP lookup chain:** Tariff CSV `EP ZIP = "3-d 606"` → `seed_rates.py` strips `"3-d "` → stores `ep_zip3 = "606"` in PostgreSQL → live mode: VisualMail `Locations.AccountNumber = "SCF606"` → code parses `dest_id[3:] = "606"` → matches `ep_zip3`. Mock mode: `access_prog` is hardcoded; no lookup occurs.

**EIA API** (`EIA_API_KEY` in `.env`): fetches the current weekly diesel price. As of 2026-07-01 this is a **fallback only** — `access_prog` prefers the FSC rate parsed directly off the matched ALG invoice (see below), since the real invoice is always already in hand by the time `access_prog` is computed. EIA is used only when a CSV's Fuel Surcharge footer row can't be parsed.

**`access_prog` calculation (rewritten 2026-07-06, issue #40 — supersedes the 2026-07-01/#21 version):** Computed in `_apply_access_prog_calc()`, shared by `_process_invoice_csv()` (every invoice upload, including Wolf/311 stub creation) and the `POST /api/admin/recompute-access-prog` backfill. Weight/pallets/pieces must always be **ours**, never ALG's — but the tariff/zone rate structure is legitimately **ALG's own pricing**, so using it is correct, not a violation of independence:
1. Get our own per-pallet `(zone, weight)`: `get_pallet_data_for_manifests()` for Corp/Technique trips, `get_prophecy_pallet_data()` for Wolf/311 (Prophecy-matched) loads. **If this comes back empty (manifest/BOL not found, not yet synced), `access_prog` is left `null`** — no independent data means no independent estimate; there is no fallback to ALG's weight anymore.
2. Per pallet (zone, our weight), resolve the rate in priority order: (a) ALG's own invoiced rate for that zone (`alg_rate_by_zip3`, parsed from the same invoice's CSV — primary source, since the rate schedule is ALG's); (b) if the invoice didn't bill that exact zone, our internal `tariff_rates` card as a fallback — sets `tariff_zone_approximate = true` when this happens.
3. FSC comes from the invoice's own "Fuel Surcharge" footer row (`alg_fsc_pct`/`alg_fsc_cost` columns), not EIA, per above.
`access_prog`/`base_tariff`/`fsc_pct` are recomputed fresh from our own data on every invoice upload for a trip (not accumulated per-invoice like `amount` — our own weight doesn't change just because a second Z-invoice arrived). Historical records whose original invoice CSV can no longer be located (`INVOICE_FOLDER` file since moved/deleted) can't be backfilled with this formula and are left as-is — `POST /api/admin/recompute-access-prog` reports these as `skipped_no_file` rather than guessing.

### Live SQL queries — `data_layer.py` (AWP-SQL-PROD)

All queries connect to AWP-SQL-PROD. TECH, SegGroup, and SQLAPPS3 are linked servers accessible from there. Requires `pip install pyodbc "sqlalchemy[mssql]"`.

| Function | Status | Returns |
|---|---|---|
| `get_technique_data(days_back)` | ✅ Implemented | trip, manifest, pallets, VM pieces — **no weight** |
| `get_manifest_weights(manifests)` | ✅ Implemented | weight, pieces, pallets per manifest (separate query) |
| `get_pallet_data_for_manifests(manifests)` | ✅ Implemented | per-pallet rows for SID export **and** for `access_prog` (below); `Dest ID` = `Locations.AccountNumber` (e.g. `SCF606`) — confirmed correct, see Open Questions |
| `get_prophecy_pallet_data(bol_number)` | ✅ Implemented | per-order-header rows from ShipperPlus `order_headers` (`destination_id`/`destination_zip`, `weight`) — the Wolf/311 equivalent of `get_pallet_data_for_manifests()`, used for `access_prog` on Prophecy-matched loads |
| `get_tariff_rate(zip3, weight)` | ✅ Implemented | one pallet's `access_prog` = base rate × (1 + FSC); also returns `is_exact_zone_match` |
| `get_prophecy_data(bol_number)` | ✅ Implemented | prophecy weight/pallets/pcs from ShipperPlus via SG360-TECH-PRD1 (primary) or SQLAPPS3 (fallback); pallet count formula needs Megha confirmation |
| `get_alg_invoice(invoice_number)` | ⬜ Stub | Z-number, amount, alg weight/pal/pcs — workaround: manual CSV upload |

**Weight split**: `get_technique_data()` does NOT return weight. The morning pull (`POST /api/admin/pull`) always calls both Query A (`get_technique_data`) + Query B (`get_manifest_weights`) and merges by manifest number.

**ALG quantity fields** (`alg_weight`, `alg_pallets`, `alg_pcs` on `BOLRecord`): null until a CSV is uploaded via `POST /api/invoices/upload`. `weight_diff`, `pallet_diff`, `pcs_diff` are computed at upload time and stored; they are not recalculated on re-pull.

**Invoice matching — `_process_invoice_csv()` in `main.py` (~line 1005):** shared by manual upload and email polling. Tried in this order — exact matches always before the Prophecy-BOL guess (fixed 2026-07-01, issue #31: a real trip whose numeric suffix coincidentally starts with "14" was being misclassified as a Wolf/311 load before this reorder):
1. **Z-number**: CSV Z-number → `invoice_number` field on `BOLRecord`
2. **Job Name as trip suffix**: CSV "Job Name" field → trip DespatchID suffix (`str(int(trip.split('T_')[-1]))`, e.g. `TEC_T_0397246` → `"397246"`)
3. **Job Name as Prophecy BOL** (Wolf/311 — no Technique trip for this load): only checked once steps 1-2 rule out a real trip match. `BOL No` is a Post Office permit number, never used for matching.
4. **Pallets + pieces** (last resort, non-comingle only, exact count match against a single unmatched record — logs a warning to verify manually)
5. **No match**: create stub record (invoice-only, `technique_trip` is null)

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
Cost %:        stored as ratio 0.9881 = 98.81% (amount / access_prog)
```

## Key variance metric

**Cost %** = `amount / access_prog` (actual ALG invoice ÷ expected Access program rate).

Color thresholds:
- Green: within 3% of 100% (0.97–1.03)
- Orange: 3–6% off (0.94–0.97 or 1.03–1.06)
- Red: >6% off (<0.94 or >1.06)

Quantity differences (weight_diff, pallet_diff, pcs_diff) are secondary — shown with sign but no hard threshold.

**`tariff_zone_approximate`** / **`weight_source_fallback`** (Boolean, on `BOLRecord`): when either is true, Cost % for that record carries a caveat — a rate had to be approximated, or our own pallet data wasn't available and the calc fell back to ALG's self-reported weight. Surfaced in the dashboard as a `~EST` badge next to Calculated Cost.

## Database schema highlights

- UUID surrogate PKs everywhere
- `bol_number` nullable Integer
- `Numeric(12,2)` for weights (up to 416,000 lbs)
- `Numeric(10,2)` for dollar amounts
- `Numeric(8,6)` for cost_pct / fsc_pct ratios
- `base_tariff` / `fsc_pct` (Numeric): rate breakdown tooltip; `access_prog = base_tariff × (1 + fsc_pct)`
- `alg_fsc_pct` / `alg_fsc_cost` (Numeric): ALG's own reported FSC rate/cost for the matched invoice, parsed from the CSV's "Fuel Surcharge" row — this is what feeds `fsc_pct` now, not EIA
- `tariff_zone_approximate` / `weight_source_fallback` (Boolean, default False): flag when `access_prog` had to approximate a rate or fall back to ALG's own weight — see "Key variance metric"
- `is_third_party` / `is_ignored` (Boolean): both exclude from SID + accounting exports; reversible
- `needs_sid_export` (Boolean): True = Type A record (no BOL yet); False = Type B (BOL already in Prophecy)
- `match_strategy` (String): how the invoice was matched — `"trip"`, `"bol"`, or null for stubs
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
documentation/           — Five .md spec files (Design & Workflow, Requirements & SQL Mapping,
                           SECURITY.md, SG360_BOL_Project_Context.md, etc.). Reference for business
                           rules and SQL source queries; not runtime code. Developmental Documentation.md
                           is the running dev changelog — one entry per closed GitHub issue, appended
                           by the `commit` skill. Read it for recent history that isn't yet folded
                           into this file.
Dockerfile               — Lambda container image build (see "AWS Lambda deployment" below); not yet deployed
terraform/bootstrap/     — One-time remote-state backend (S3 + DynamoDB), local state; see "AWS Lambda deployment"
frontend/src/main.jsx    — Vite entry point; mounts <App /> only, no router
frontend/src/App.jsx     — Owns all state + fetch/mutation handlers; passes data+callbacks down as props
frontend/src/components/
  SummaryBar.jsx              — Pending/approved/flagged counts strip
  BOLTable.jsx                — Pending + flagged records table (wraps BOLRow)
  BOLRow.jsx                  — Single record row; Approve, Flag, Third-party, Ignore buttons
  ApprovedSection.jsx         — Approved records table + SID export + Send to Accounting flow
  ThirdPartySection.jsx       — Third-party records (customer pays direct); excluded from SID export
  FlagModal.jsx               — Modal overlay for entering a flag reason
  ReassignInvoiceModal.jsx    — Modal for moving an invoice to a different trip (preview/merge/replace)
  BulkActionToolbar.jsx       — Floating bar shown when rows are multi-selected; bulk approve/flag/third-party/ignore
  EmailComposeModal.jsx       — Builds an HTML table for selected records and calls mark-accounting-sent on send
  LogSection.jsx              — Historical log viewer (separate tab)
```

## Frontend patterns

No Context, no reducer, no router, no `useMemo`/`useCallback` anywhere in `frontend/src` — just `useState`/`useEffect`/`useRef`, all lifted to `App.jsx` (~1160 lines) and passed down as props. Conventions to match when extending it:

- **Inline styles only** — no CSS modules, Tailwind, or styled-components. Style objects are built ad hoc per component (e.g. `TD`/`TD_R` constants in `BOLRow.jsx` for shared cell styles). Colors are hardcoded hex per-component (`#2D6A4F` green, `#dc2626` red, etc.) — there's no shared theme/constants file, so matching an existing color means grepping for its hex value in the relevant component.
- **Modals** (`FlagModal.jsx`, `ReassignInvoiceModal.jsx`) are conditionally rendered inline in `App.jsx`'s JSX based on a target-id state variable (e.g. `flagTarget`, `reassignTargetId`) — not a portal, not router-based.
- **Debounced auto-save**: the notes field in `BOLRow.jsx` uses local `useState` + `useRef` to hand-roll a 500ms `setTimeout` debounce, then calls back up to `App.jsx`'s `onNotesUpdate` (which hits `PATCH /api/bols/{id}/notes`). Reuse this pattern for any other field needing autosave.
- **Bulk-select**: `selectedIds` is a `Set` in `App.jsx` state, with `toggleSelect`/`toggleSelectAll`/`clearSelection` helpers; `BulkActionToolbar.jsx` reads from it.
- **Module 2 refactor seam**: `App.jsx` has an in-code comment marking the intended split when Module 2 ships — extract fetch helpers to `src/api/bolsApi.js` and move this state/logic to `src/pages/BolReconciliation.jsx`. Don't do this preemptively; it's noted for when a second module actually needs the shared shell.

## Known bugs

**`POST /api/invoices/upload`'s folder-based sender auto-detection is not working in live testing as of 2026-07-02**, despite passing all unit-level checks (`_parse_invoice_folder_name` verified correct; the picker itself was rewritten from `webkitdirectory` to the File System Access API). Treat as broken until confirmed fixed — see `documentation/Developmental Documentation.md` Reference section for the full investigation trail.

**`days_back` is 20** in `pull_technique_data()` — re-pull refreshes technique-side fields (weight/pallets/pcs, BOL status) but preserves invoice matches, approvals, flags, and notes on existing records. Safe to re-run any time.

**Mock state** (`_mock_state` in `main.py`): in-memory dict initialized from `MOCK_BOLS` at startup. Mutations (approvals, flags, invoice uploads) survive the process lifetime but reset on every backend restart. Restart the backend to reset all records to their initial pending state during development.

**Mock mode now supports the full daily workflow end-to-end:**
1. Records 11 and 12 start without invoice data — upload the test CSVs to fill them in
2. SID export (`GET /api/export/prophecy-sid`) generates synthetic pallet rows from approved records
3. Email export logs to console instead of sending (SMTP not configured)

## AWS Lambda deployment (Stage 1 — in progress, not yet live)

Container-image Lambda deployment is being built out; nothing here is deployed yet.

- `Dockerfile` (repo root): builds `public.ecr.aws/lambda/python:3.13`, installs `backend/requirements.txt`, copies `backend/` (including `test_data/`, needed at runtime for mock mode), entrypoint `backend.main.handler`.
- `backend/main.py` ends with `handler = Mangum(app)` — wraps the existing FastAPI `app` for Lambda's event format. No route code changes needed for this; it's purely an adapter tacked onto the same app used by uvicorn locally.
- `backend/requirements.txt` was switched from `>=` floors to pinned `==` versions for reproducible container builds, and gained `mangum==0.21.0`.
- `terraform/bootstrap/` provisions only the remote-state backend (S3 bucket with versioning + encryption, DynamoDB lock table) using **local** state — deliberately, since it creates the very backend a future `terraform/` (main infra: Lambda, API Gateway, etc.) project would use. That main project doesn't exist yet.
- `.env.example` is the Stage 1 reference `.env` — everything below its "OUT OF SCOPE for Stage 1" divider is live-mode-only and left blank/default.
- Target architecture is serverless (Lambda + API Gateway + Aurora + CloudFront); this Dockerfile/bootstrap work is the first slice. Two known blockers for going live-mode in Lambda, not yet resolved: VPN connectivity from Lambda to the on-prem AWP-SQL-PROD/ShipperPlus SQL Servers, and UNC file share access (`INVOICE_FOLDER`) from Lambda.

## Future modules (do not implement yet)

- **Module 2**: Sheet 2 / Mary Group workflow (same pattern, different recipient)
- **Commingle billing**: `CM_` manifests already appear in Module 1 data
- **ALG email parsing**: Need sample email from Katie; stub is `get_alg_invoice()` in `data_layer.py`
- **Prophecy BOL creation**: Long-term goal to create BOLs here instead of in Prophecy
- **Scheduled pulls**: 7/8/9am cron jobs calling `data_layer.py` functions
- **Auth**: `users` table ready; add `fastapi-users` or JWT without touching existing routes
- **AWS RDS**: Change `DATABASE_URL` in `.env` — no code changes needed
