# SG360 BOL Reconciliation Dashboard

Replaces the manual daily Excel process (`Technique and BOL Numbers New June 2026.xlsx`, Sheet 1) with a web dashboard. Logistics coordinator Katie reviews freight billing records each morning, approves or flags each one, then sends a summary to Mary in accounting.

> For architecture, data model, API routes, open questions, and dev history, see [CLAUDE.md](CLAUDE.md) — this file only covers getting the app running locally.

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

Create a `.env` file in the project root. Minimal mock-data setup (no DB or email needed):

```
USE_MOCK_DATA=True
MOCK_INVOICES=True   # skips ALG invoice lookup during morning pull
```

See CLAUDE.md's `.env` quick-start for the full list of live-mode keys (DB, SMTP, EIA API, IMAP, AWP-SQL-PROD).

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

### Or start both at once

```powershell
.\start.ps1
```

Kills any existing processes on ports 8000 and 3000 before launching both servers.

There are no automated tests. Verify changes manually via the dashboard and the FastAPI docs above.

---

## Switching from Mock Data to Real Data

1. Set `USE_MOCK_DATA=False` in `.env` and fill in the live-mode keys (see CLAUDE.md)
2. Create the PostgreSQL database:
   ```sql
   CREATE DATABASE sg360_bol;
   CREATE USER sg360_user WITH PASSWORD 'your-password';
   GRANT ALL PRIVILEGES ON DATABASE sg360_bol TO sg360_user;
   ```
3. Tables are created automatically on first startup (inline migrations in `main.py`'s lifespan hook — no Alembic)
4. Install the live-mode extra dependencies: `pip install pyodbc "sqlalchemy[mssql]"`
5. Seed the tariff/FSC rate tables once: `python -m backend.seed_rates`

`backend/data_layer.py` is the integration boundary to AWP-SQL-PROD, Prophecy/ShipperPlus, and the tariff tables — see CLAUDE.md for the current implementation status of each function.

---

## Pointing to AWS RDS

Change `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgresql://user:password@your-instance.rds.amazonaws.com:5432/sg360_bol
```

No code changes required. The connection pool is already configured for RDS (`pool_pre_ping=True` handles the idle-timeout reconnect).

---

## Email Setup (O365)

Set SMTP credentials in `.env`:

```
SMTP_USER=user@sg360.com
SMTP_PASSWORD=your-app-password
```

Use an App Password, not the regular account password. When credentials are blank, the app logs what it would have sent instead of failing — safe for prototyping. Reading ALG's invoice emails additionally requires `ALG_SENDER_EMAIL` and IMAP access (see CLAUDE.md).

---

## Key Contacts

- **Katie** — SG360 logistics coordinator (reviews dashboard daily)
- **Mary** — SG360 accounting (receives CSV export)
- **Tanya** — ALG Worldwide (sends invoice CSV each morning)
- **Marge** — Technique/VisualMail SQL source of truth
- **Megha** — Prophecy internals
- **Phil** — Logistics lead, owns the ALG relationship
