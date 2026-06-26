# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

There are no automated tests. Verify changes manually via the dashboard at `http://localhost:3000` and the FastAPI docs at `http://localhost:8000/docs`.

**Vite dev proxy:** The frontend calls bare `/api/*` paths. Vite proxies them to `http://localhost:8000`. Never hardcode `localhost:8000` in frontend code — the proxy handles it.

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
```

**Live-mode extra dependencies** (not in `requirements.txt` — install separately when going live):
```powershell
pip install pyodbc "sqlalchemy[mssql]"
```

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
| POST | `/api/bols/{id}/reassign-invoice` | Move invoice to a different trip/BOL/manifest; body: `{ target, action: preview\|merge\|replace }` |
| PATCH | `/api/bols/{id}/notes` | Auto-save notes field (called by frontend with 500ms debounce) |
| POST | `/api/admin/pull` | Pull Technique manifests from AWP-SQL-PROD (disabled in mock mode) |
| POST | `/api/admin/poll-email` | Poll O365 IMAP for unread ALG invoice emails → extract CSVs → process (live mode only) |
| POST | `/api/admin/reset-invoices` | Dev: clear invoice fields on all records + delete invoice-only stubs |
| POST | `/api/invoices/upload` | Upload ALG invoice CSV (Z-number format) → match + update record; response includes `conflict` key if trip already had an invoice |
| GET | `/api/invoices/{z}/file` | Serve original Z-number CSV from `INVOICE_FOLDER` (or `test_data/` in mock mode) |
| POST | `/api/invoices/poll-folder` | Scan `INVOICE_FOLDER` path for unprocessed CSVs → process each |
| GET | `/api/export/prophecy-sid` | Download Prophecy SID import CSV for approved manifests (live mode only) |
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
| 2 | **Destination → ZIP field in VisualMail**: Current code parses `Locations.AccountNumber` (e.g. `SCF606` → `606`) for tariff zone lookup. Marge (June 22) said the ZIP is "not in the Manifest table" and pointed to a field called `DestinationID`. Unclear if this is the same field via the Pallet→Locations join or a different one. Do NOT change `get_pallet_data_for_manifests()` SQL until confirmed. Email sent to Marge June 22. | Marge | ❓ Open |
| 3 | **ALG invoice format**: Tanya can send CSV (format confirmed from Z556229.CSV). `POST /api/invoices/upload` accepts it. Ask Phil to switch Tanya to CSV delivery. | Phil / Tanya | ✅ CSV format confirmed |
| 8 | **Prophecy BOL sync**: After Katie imports SID file + creates loads, how do we query Prophecy DB for the resulting BOL numbers? Need connection string + table schema. | Megha | ❓ Open — needed for EOD BOL sync |
| 10 | **VisualMail SELECT permission on AWD-SQL-WH4**: `get_pallet_data_for_manifests()` and `get_manifest_weights()` fail with Msg 229 (SELECT permission denied on VisualMail). Affects pallet destination ZIP lookup and SID export in live mode. Does NOT block mock mode. | Megha | ❓ Open — ask Megha to grant SELECT on VisualMail to service account |

**Resolved June 22 meeting:**
- **Q1 FSC unit**: Percentage of base freight. DB stores `fsc_amount=0.365` → 36.5% surcharge (decimal fraction, NOT 36.5). `get_fsc_rate()` returns `fsc_amount` directly. Applied as `access_prog = base_tariff × (1 + fsc_pct)`.
- **Q2/Q7 Destination → ZIP**: Tariff lookup is **per-pallet**. Confirmed approach: `SCF`/`NDC` prefix + 3-digit zone (e.g. `SCF606` → `606`). **Exact VisualMail field uncertain** — current code uses `Locations.AccountNumber` via Pallet→Locations join, but Marge (June 22) indicated the field may be `DestinationID` in the Manifest table. See Open Question #2. See `get_pallet_data_for_manifests()`.
- **Q4 Diesel source**: EIA weekly on-highway diesel (`EMD_EPD2D_PTE_NUS_DPG`). Requires `EIA_API_KEY` in `.env`. `get_current_diesel_price()` in `data_layer.py`.
- **Q5 Z-number flow**: Katie creates load in Prophecy manually (Import → Consolidate → Re-rate → Create Load). Load number = BOL number. Our SID export feeds this import step.
- **Q6 AWP-SQL-PROD**: Access confirmed live.

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

**EIA API** (`EIA_API_KEY` in `.env`): fetches the current weekly diesel price to select the right FSC band. The FSC matrix itself comes from the xlsx above — EIA is only needed for today's price. Without the key, FSC is skipped and `access_prog` stores base tariff only.

### Live SQL queries — `data_layer.py` (AWP-SQL-PROD)

All queries connect to AWP-SQL-PROD. TECH, SegGroup, and SQLAPPS3 are linked servers accessible from there. Requires `pip install pyodbc "sqlalchemy[mssql]"`. ⚠️ SELECT permission on VisualMail currently denied on AWD-SQL-WH4 — see Open Question #10.

| Function | Status | Returns |
|---|---|---|
| `get_technique_data(days_back)` | ✅ Implemented | trip, manifest, pallets, VM pieces — **no weight** |
| `get_manifest_weights(manifests)` | ✅ Implemented | weight, pieces, pallets per manifest (separate query) |
| `get_pallet_data_for_manifests(manifests)` | ✅ Implemented | pallet rows for SID export; `Dest ID` = `Locations.AccountNumber` (e.g. `SCF606`) — **field uncertain, see Open Question #2** |
| `get_tariff_rate(zip3, weight)` | ✅ Implemented | `access_prog` = base rate × (1 + FSC) from PostgreSQL |
| `get_prophecy_data(bol_number)` | ⬜ Stub | prop_reship, prophecy weight/pallets/pcs — needs Megha |
| `get_alg_invoice(invoice_number)` | ⬜ Stub | Z-number, amount, alg weight/pal/pcs — workaround: manual CSV upload |

**Weight split**: `get_technique_data()` does NOT return weight. The morning pull (`POST /api/admin/pull`) always calls both Query A (`get_technique_data`) + Query B (`get_manifest_weights`) and merges by manifest number.

**ALG quantity fields** (`alg_weight`, `alg_pallets`, `alg_pcs` on `BOLRecord`): null until a CSV is uploaded via `POST /api/invoices/upload`. `weight_diff`, `pallet_diff`, `pcs_diff` are computed at upload time and stored; they are not recalculated on re-pull.

**Invoice matching — `_process_invoice_csv()` in `main.py` (~line 620):** shared by manual upload and email polling.
1. **Z-number**: CSV Z-number → `invoice_number` field on `BOLRecord`
2. **Job Name**: CSV "Job Name" field → trip DespatchID suffix (`str(int(trip.split('T_')[-1]))`, e.g. `TEC_T_0397246` → `"397246"`)
3. **BOL number**: CSV "BOL No" → `bol_number` field (for non-comingle loads only)
4. **No match**: create stub record (invoice-only, `technique_trip` is null)

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

## Database schema highlights

- UUID surrogate PKs everywhere
- `bol_number` nullable Integer
- `Numeric(12,2)` for weights (up to 416,000 lbs)
- `Numeric(10,2)` for dollar amounts
- `Numeric(8,6)` for cost_pct ratio
- `accounting_exported_at` nullable DateTime — set when "Send to Accounting" runs; exposed in Log tab
- `approval_history` table for full audit trail
- `users` table stubbed for future auth

## File layout rationale

```
backend/main.py          — All routes in one file (Module 1 only; split by module when Module 2 ships)
backend/data_layer.py    — The integration boundary; implement get_prophecy_data + get_alg_invoice to finish live mode
backend/mock_data.py     — 12 records at real scale; safe to delete when DB is live
backend/email_parser.py  — O365 IMAP4_SSL polling (outlook.office365.com:993); marks emails read even
                           with no CSV attachment (prevents re-scan loop on next poll)
backend/email_service.py — SMTP STARTTLS export; returns False (soft-fail, no exception) when credentials
                           missing — POST /api/export still returns HTTP 200
backend/csv_export.py    — Three exports: accounting CSV (18 cols), Prophecy SID (13 cols with underscore
                           names — any column name difference breaks Prophecy import),
                           generate_mock_sid_rows() for mock-mode SID
backend/database.py      — SQLAlchemy engine; pool_pre_ping=True is required for RDS idle-timeout reconnect
backend/test_data/       — Sample ALG invoice CSVs for testing the upload flow in mock mode
                           Z555226_test.csv → matches trip TEC_T_0109888 (BOL No 109888)
                           Z555227_test.csv → matches trip TEC_T_0109889 (BOL No 109889)
test_invoices_0622/      — 26 real Z-number CSVs from Tanya's June 22 email (e.g. Z557707.CSV).
                           Use for live-mode invoice upload testing. NOT committed — real production
                           data; add to .gitignore if not already excluded.
frontend/src/App.jsx     — Owns all state + fetch/mutation handlers; passes data+callbacks down as props
frontend/src/components/
  SummaryBar.jsx         — Pending/approved/flagged counts strip
  BOLTable.jsx           — Pending + flagged records table (wraps BOLRow)
  BOLRow.jsx             — Single record row; Approve and Flag buttons
  ApprovedSection.jsx    — Approved records table + SID export + Send to Accounting flow
  FlagModal.jsx          — Modal overlay for entering a flag reason
  LogSection.jsx         — Historical log viewer (separate tab)
```

## Known bugs

**`days_back` default is 10**, but invoices lag 11–18 days from despatch — matching fails unless `?days_back=20` is passed explicitly. The default needs to be raised in two places: `get_technique_data(days_back=10)` in `data_layer.py` and the `pull_technique_data()` route default in `main.py`. Until changed, pass `?days_back=20` on the `POST /api/admin/pull` call.

**Duplicate `unapprove_bol` function in `main.py`**: The function is defined twice (identical bodies), at the same FastAPI route `POST /api/bols/{record_id}/unapprove`. Python silently replaces the first definition with the second; FastAPI registers two route entries that both resolve to the second definition. Behavior is currently correct by coincidence — remove the duplicate (lines ~253–286 are the second copy).

**Mock state** (`_mock_state` in `main.py`): in-memory dict initialized from `MOCK_BOLS` at startup. Mutations (approvals, flags, invoice uploads) survive the process lifetime but reset on every backend restart. Restart the backend to reset all records to their initial pending state during development.

**Mock mode now supports the full daily workflow end-to-end:**
1. Records 11 and 12 start without invoice data — upload the test CSVs to fill them in
2. SID export (`GET /api/export/prophecy-sid`) generates synthetic pallet rows from approved records
3. Email export logs to console instead of sending (SMTP not configured)

## Future modules (do not implement yet)

- **Module 2**: Sheet 2 / Mary Group workflow (same pattern, different recipient)
- **Commingle billing**: `CM_` manifests already appear in Module 1 data
- **ALG email parsing**: Need sample email from Katie; stub is `get_alg_invoice()` in `data_layer.py`
- **Prophecy BOL creation**: Long-term goal to create BOLs here instead of in Prophecy
- **Scheduled pulls**: 7/8/9am cron jobs calling `data_layer.py` functions
- **Auth**: `users` table ready; add `fastapi-users` or JWT without touching existing routes
- **AWS RDS**: Change `DATABASE_URL` in `.env` — no code changes needed
