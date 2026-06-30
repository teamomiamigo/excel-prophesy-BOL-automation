# SG360 BOL Reconciliation — Full Project Context

*For planning/brainstorming. Last updated: June 19, 2026.*

---

## What This Is

SG360 (commercial printing company) has a logistics coordinator named **Katie** who manually maintains an Excel spreadsheet every morning to reconcile freight billing. The spreadsheet (`Technique and BOL Numbers New June 2026.xlsx`) pulls data from three separate systems:

1. **Visual Mail / Technique** — physical shipment data (trips, manifests, weight, pallets, pieces)
2. **ALG Worldwide invoices** — what ALG actually charged (emailed each morning by Tanya at ALG)
3. **Access tariff rates** — what SG360 *expected* to pay (rate card in an old Microsoft Access database)

Katie manually copies data from all three into the spreadsheet, calculates cost variance, then emails a CSV to **Mary** in accounting.

**This project replaces that with a web dashboard.**

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
2. ~8am — Tanya emails ALG invoice PDF with Z-numbers and dollar amounts
3. ~9am — Katie enters Z-numbers + amounts into Excel, calculates variance manually
4. ~10am — Katie reviews, emails Mary a CSV of approved rows

**Target (automated):**
1. 7am — Automated pull runs (or Katie hits Refresh) → records appear in dashboard
2. ~10am — Katie opens dashboard, reviews color-coded cost variance, approves or flags
3. Done — "Send to Accounting" emails Mary automatically

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
- **ALG invoice** = what was actually charged; Katie verifies this against the Access tariff rate
- **BOL numbers** are created by Katie in Prophecy *after* morning review — NOT automatically

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy 2.0, pydantic-settings |
| Frontend | React 18 (Vite), functional components + hooks, inline styles only |
| Database | PostgreSQL 17 locally (`sg360_bol` DB, `sg360_user`/`localpass`) |
| SQL Server | pyodbc + ODBC Driver 17 → AWP-SQL-PROD (SQL Server) |
| Email | smtplib STARTTLS port 587 (O365) — console fallback in dev |

**Key config flag:** `USE_MOCK_DATA` in `.env`
- `True` = all routes use `mock_data.py` (no DB or SQL needed)
- `False` = real PostgreSQL + real AWP-SQL-PROD queries

**Current state:** `USE_MOCK_DATA=False`, `MOCK_INVOICES=True`
→ Real Technique data loads from AWP-SQL-PROD, invoice fields stay null

---

## What's Working Right Now

- **Backend** running on port 8000 (needs manual restart: `uvicorn backend.main:app --reload`)
- **Frontend** running on port 3001 (`npm run dev` in frontend/)
- **Real Technique data loading** — 7 deduplicated records pull from AWP-SQL-PROD each morning
- **PostgreSQL** — `sg360_bol` database, tables created, data persists
- **Tariff rates seeded** — 253 rows from the Access CSV tariff file
- **Fuel surcharge rates seeded** — 135 rows from ALG's FSC matrix
- **Approve/Flag workflow** — Katie can approve or flag records in the dashboard
- **Audit trail** — `approval_history` table logs every action
- **Export** — "Send to Accounting" generates CSV and attempts email to Mary + Katie

---

## What's NOT Working Yet (Blocked)

| Feature | Status | What's needed |
|---|---|---|
| **Invoice # (Z-number)** | ❌ Null | Need sample ALG invoice email from Katie |
| **Invoice Amount ($)** | ❌ Null | Same as above |
| **Access Prog (expected cost)** | ❌ Null | Need destination → 3-digit ZIP mapping from Katie (ENRU/ALG/LSC/CHOICE → SCF zone) |
| **Cost % (the key metric)** | ❌ N/A | Blocked until both Amount and Access Prog are populated |
| **Prophecy reship cost** | ❌ Not pulled | `load_id` is available from Query A but not yet used to fetch Prophecy estimate |
| **Prophecy weight/pallets** | ❌ Not pulled | ShipperPlus has these; not yet queried |
| **BOL numbers** | Manual | Katie creates in Prophecy; could be pullable via ShipperPlus |

---

## The Key Variance Metric

**Cost % = amount ÷ access_prog** (actual ALG charge ÷ expected tariff rate)

Stored as ratio (e.g., 0.9881 = 98.81%).

Color thresholds in the dashboard:
- 🟢 Green: within 5% (0.95–1.05) — normal
- 🟡 Yellow: 5–10% off (0.90–0.95 or 1.05–1.10) — investigate
- 🔴 Red: >10% off — flag

This is the whole point of the tool. Everything else is supporting data.

---

## How Real Data Gets Loaded (Two SQL Queries)

Both queries run against **AWP-SQL-PROD** using SQL auth (`Application` / `Welcome123`).

### Query A — Technique Trip & Manifest Pull
Finds all ALG-destined shipments despatched in the last N days.

Hits: `TECH.Live_Orders` (linked server) + `SegGroup` (linked) + `SQLAPPS3/ShipperPlus` (linked) + `VisualMail` (local)

Returns per manifest:
- Trip ID (`TEC_T_0109878` format)
- Manifest number (`TEC_M_0228920` format)
- Destination code (`ENRU`, `ALG`, `LSC`, `CHOICE`)
- Prophecy pieces (from ShipperPlus order_headers)
- Notes (trailer number etc.)
- Carrier

**Does NOT return weight** — weight doesn't exist in TECH.Live_Orders.

### Query B — VisualMail Weights, Pallets & Pieces
For a list of manifest numbers, returns authoritative counts from VisualMail.

Returns:
- `SUM(p.Weight)` → technique_weight
- `COUNT(p.UniqueContainerID)` → technique_pallets
- `SUM(p.NumberOfCopies)` → technique_pcs

**Why two queries?** Weight only lives in VisualMail. Query A discovers which manifests exist; Query B fetches the physical measurements.

---

## Database Schema Highlights

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
  prop_reship       Numeric(10,2), nullable  ← Prophecy estimate
  access_prog       Numeric(10,2), nullable  ← tariff rate calculation
  amount            Numeric(10,2), nullable  ← actual ALG charge
  cost_pct          Numeric(8,6), nullable   ← amount/access_prog stored as ratio
  prophecy_weight   Numeric(12,2), nullable
  weight_diff       Numeric(12,2), nullable  ← prophecy - technique
  prophecy_pallets  Integer, nullable
  pallet_diff       Integer, nullable
  prophecy_pcs      Integer, nullable
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

tariff_rates        ← 253 rows seeded from Access CSV
  ep_zip3           3-digit SCF zone (e.g. "060") — lookup key
  cost_per_100lb    rate
  minimum_freight   floor rate

fuel_surcharge_rates  ← 135 rows seeded
  fuel_price_min/max  band
  fsc_amount          surcharge (unit TBD — % or $/cwt)
```

---

## API Routes

| Method | Path                   | Purpose                             |
| ------ | ---------------------- | ----------------------------------- |
| GET    | /health                | Status + mock mode flag             |
| GET    | /api/bols              | All pending + flagged records       |
| GET    | /api/bols/approved     | Approved records for today          |
| POST   | /api/bols/{id}/approve | Approve a record                    |
| POST   | /api/bols/{id}/flag    | Flag with reason                    |
| POST   | /api/admin/pull        | Morning data pull from AWP-SQL-PROD |
| POST   | /api/export            | CSV export + email to Mary + Katie  |

---

## File Structure

```
C:\nikhilm\excel-prophesy-BOL-automation\
├── backend\
│   ├── main.py          — All routes (single file for Module 1)
│   ├── config.py        — Settings via pydantic-settings + .env
│   ├── models.py        — SQLAlchemy ORM + Pydantic schemas
│   ├── database.py      — Engine + session (designed for AWS RDS swap)
│   ├── data_layer.py    — THE integration boundary: 4 stub functions
│   ├── mock_data.py     — 10 records at real scale (safe to delete when live)
│   ├── email_service.py — smtplib STARTTLS + console fallback
│   ├── csv_export.py    — CSV matching Excel Sheet 1 column order
│   └── requirements.txt
├── frontend\
│   └── src\
│       ├── App.jsx              — Owns all state
│       └── components\
│           ├── SummaryBar.jsx
│           ├── BOLTable.jsx
│           ├── BOLRow.jsx
│           ├── ApprovedSection.jsx
│           └── FlagModal.jsx
├── CLAUDE.md            — AI context file
├── .env                 — GITIGNORED (has SQL credentials)
└── .gitignore
```

`data_layer.py` is the seam — implement these 4 functions to connect real data:
- `get_technique_data(days_back)` → Query A results
- `get_manifest_weights(manifest_numbers)` → Query B results
- `get_tariff_rate(zip3, weight)` → PostgreSQL lookup
- `get_alg_invoice(invoice_number)` → ALG email parse (stub)

---

## Open Questions (Must Resolve Before Go-Live)

| #   | Question                                                                           | Who           | Blocks               |
| --- | ---------------------------------------------------------------------------------- | ------------- | -------------------- |
| 1   | FSC unit: is `fsc_amount` a % of base rate or $/cwt?                               | Katie / Tanya | Accurate cost calc   |
| 2   | Destination → ZIP: what 3-digit SCF zone maps to ENRU, ALG, LSC, CHOICE?           | Katie         | Access Prog + Cost % |
| 3   | Can ALG send CSV invoices instead of PDF?                                          | Phil / Tanya  | Invoice automation   |
| 4   | Which diesel price index does SG360 use for FSC? How often does it change?         | Phil / Katie  | FSC calc             |
| 5   | What triggers Z-number creation in Prophecy — Technique import or manual step?     | Katie / Megha | Invoice matching     |
| 6   | Is `VisualMail.dbo.Pallet.NumberOfCopies` the right pieces column (matches Excel)? | Marge         | Pieces accuracy      |
| 7   | Is there a BOL number in ShipperPlus linked to the manifest via `load_id`?         | Marge         | BOL auto-fill        |
| 8   | Which ShipperPlus column holds the Prop Reship (Prophecy cost estimate)?           | Marge         | Prop Reship field    |

---

## Pending To-Do Items

1. **Clear PENDING placeholders in DB** — run: `UPDATE bol_records SET invoice_number = NULL WHERE invoice_number LIKE 'PENDING-%';`
2. **Restart backend** — uvicorn died; run `uvicorn backend.main:app --reload`
3. **Set up Bitbucket repo** — private repo under SG360 org workspace; URL created: `https://nikhilm6@bitbucket.org/SG360/sg360-bol-automation.git`; blocked on Bitbucket App Password creation
4. **Get destination→ZIP mapping from Katie** — unlocks Access Prog + Cost %
5. **Get sample ALG invoice from Katie** — to implement Z-number + amount parsing
6. **Ask Marge about pieces column + BOL number in ShipperPlus**

---

## What This Tool Will Look Like When Done

Katie opens a browser at 10am. She sees a table like this:

| Trip | Manifest | BOL | Wgt | Pal | PCS | Invoice | Prop Reship | Amount | Cost % | Actions |
|---|---|---|---|---|---|---|---|---|---|---|
| TEC_T_01... | TEC_M_02... | — | 35,240 | 42 | 343,521 | Z555216 | $3,177 | $3,139 | 🟢 98.8% | Approve / Flag |
| TEC_T_01... | TEC_M_02... | — | 21,104 | 95 | 405,063 | Z555217 | $3,155 | $2,806 | 🔴 88.9% | Approve / Flag |

She approves what looks right, flags what needs investigation, then clicks "Send to Accounting" — done.

---

## Future Modules (Not Being Built Yet)

- **Module 2** — Sheet 2 / Mary Group workflow (same pattern, different recipient)
- **Commingle billing** — `CM_` manifests already appear in Module 1 data
- **ALG email parsing** — need sample email from Katie; stub exists in `data_layer.py`
- **Scheduled pulls** — 7/8/9am cron jobs calling `data_layer.py` functions
- **Auth** — `users` table ready; add JWT without touching existing routes
- **AWS RDS** — just change `DATABASE_URL` in `.env`
- **BOL creation in Prophecy** — long-term goal to create BOLs here instead
