# SG360 BOL Reconciliation Dashboard

Replaces the manual daily Excel process (`Technique and BOL Numbers New June 2026.xlsx`, Sheet 1) with a web dashboard. Logistics coordinator Katie reviews freight billing records each morning, approves or flags each one, then sends a summary to Mary in accounting.

---

## Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL 15+ (only needed when `USE_MOCK_DATA=False`)

---

## Local Setup

### 1. Backend

```bash
cd backend
pip install -r requirements.txt
```

Create a `.env` file in the project root (copy from `.env` — already present, fill in SMTP credentials when ready):

```
USE_MOCK_DATA=True
DATABASE_URL=postgresql://sg360_user:localpass@localhost:5432/sg360_bol
SMTP_USER=
SMTP_PASSWORD=
EMAIL_TO_MARY=["mary@sg360.com"]
EMAIL_TO_KATIE=["katie@sg360.com"]
DEBUG=True
```

Run the backend:

```bash
uvicorn backend.main:app --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard: http://localhost:3000

---

## Switching from Mock Data to Real Data

1. Set `USE_MOCK_DATA=False` in `.env`
2. Create the PostgreSQL database:
   ```sql
   CREATE DATABASE sg360_bol;
   CREATE USER sg360_user WITH PASSWORD 'your-password';
   GRANT ALL PRIVILEGES ON DATABASE sg360_bol TO sg360_user;
   ```
3. Tables are created automatically on first startup
4. Implement the four functions in `backend/data_layer.py`:
   - `get_technique_data()` — connects to AWP-SQL-PROD / VisualMail
   - `get_prophecy_data()` — connects to Prophecy system
   - `get_tariff_rate()` — queries Access tariff DB (post-migration: PostgreSQL)
   - `get_alg_invoice()` — parses ALG invoice email from Tanya

---

## Pointing to AWS RDS

Change `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgresql://user:password@your-instance.rds.amazonaws.com:5432/sg360_bol
```

No code changes required. The connection pool is already configured for RDS
(`pool_pre_ping=True` handles the 8-hour idle timeout).

---

## Email Setup (O365)

Set SMTP credentials in `.env`:

```
SMTP_USER=katie@sg360.com
SMTP_PASSWORD=your-app-password
```

Use an App Password, not your regular account password. When credentials are blank,
the app logs what it would have sent — safe for prototyping.

---

## Data Model Notes

- **BOL numbers are nullable** — they are created by Katie in Prophecy after reviewing records. A record can exist and be approved without one.
- **Trip and Manifest may be blank** — in the source Excel, blank rows belong to the trip listed in the row above.
- **CM_ manifest prefix** = comingle shipment (future Module 2).
- **Cost %** = `amount / access_prog` — the primary variance metric. Green < 5% off, Yellow 5–10%, Red > 10%.
- **Three cost columns**: `prop_reship` (Prophecy estimate), `access_prog` (tariff/expected), `amount` (actual ALG invoice).

---

## Project Structure

```
backend/
  main.py          — All FastAPI routes
  config.py        — Settings (USE_MOCK_DATA, DB URL, email, etc.)
  models.py        — SQLAlchemy tables + Pydantic schemas
  database.py      — Engine + session factory
  data_layer.py    — Stubs for real SQL + email integration
  mock_data.py     — 10 records at real scale
  csv_export.py    — CSV generation (matches Excel column order)
  email_service.py — Email to Mary + Katie

frontend/src/
  App.jsx                      — Root: state, fetch, mutation handlers
  components/SummaryBar.jsx    — Pending / Approved / Flagged counts
  components/BOLTable.jsx      — Pending records table
  components/BOLRow.jsx        — Single row + Approve/Flag buttons
  components/ApprovedSection.jsx — Approved records + Send to Accounting
  components/FlagModal.jsx     — Flag reason input modal
```

---

## Open Questions — Must Resolve Before Going Live

These are blocking assumptions. Add new ones here whenever discovered; cross off when answered.
Full detail in `CLAUDE.md` § Critical Open Questions.

| # | Question | Who |
|---|---|---|
| 1 | **FSC unit** — is `fsc_amount` a % of base rate (e.g. 25%) or $/cwt ($0.25/100 lbs)? | Katie / Tanya |
| 2 | **Destination → ZIP zone** — how do Technique destinations (`ENRU`, `ALG`, `LSC`) map to the 3-digit SCF zone in the tariff table? | Katie |
| 3 | **ALG invoice format** — can Tanya send CSV instead of PDF? Required for `get_alg_invoice()` auto-parsing. | Phil / Tanya |
| 4 | **Diesel price source** — which fuel price index (EIA weekly, ALG published, other)? | Phil / Katie |
| 5 | **Z-number trigger** — what step in Prophecy creates the Z-number, and when does it happen relative to the morning pull? | Megha |
| 6 | **AWP-SQL-PROD access** — does the backend machine have network + SQL Server access to run `data_layer.py` live? | Nikhil / IT |

---

## Future Work (Do Not Implement Yet)

| Item | Notes |
|---|---|
| ALG email parsing | `get_alg_invoice()` stub is the integration point. Need a sample email from Katie to build the parser. |
| Scheduled 7/8/9am pulls | APScheduler job calling `data_layer.py` functions |
| Prophecy BOL number | `bol_number` column is nullable and ready. Add Prophecy pull or manual entry field when ready. |
| Access tariff integration | `get_tariff_rate()` stub ready. Exact relationship between tariff rates and `access_prog` TBD with Katie. |
| Commingle billing (Module 2) | Sheet 2 of the source Excel / Mary Group. Same approve/flag/export pattern, different recipient. |
| Authentication | `users` table stubbed. Add `fastapi-users` or JWT without touching existing routes. |
| ML / analytics | Full history in `approval_history` table. Schema designed for BI queries. |

---

## Key Contacts

- **Katie** — SG360 logistics coordinator (reviews dashboard daily)
- **Mary** — SG360 accounting (receives CSV export)
- **Tanya** — ALG Worldwide (sends invoice email each morning)
