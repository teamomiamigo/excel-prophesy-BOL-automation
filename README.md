# SG360 BOL Reconciliation Dashboard

Replaces the manual daily Excel process (`Technique and BOL Numbers New June 2026.xlsx`, Sheet 1) with a web dashboard. Logistics coordinator Katie reviews freight billing records each morning, approves or flags each one, then sends a summary to Mary in accounting.

> For architecture, data model, API routes, open questions, and dev history, see [CLAUDE.md](CLAUDE.md) — this file only covers getting the app running locally. A live version of this app is already deployed on AWS (Lambda + API Gateway + Aurora backend, S3 + CloudFront frontend) — see [documentation/AWS Deployment.md](documentation/AWS%20Deployment.md) for what's running there and the `/deploy` skill (`.claude/skills/deploy`) for how to ship changes to it. If you're using Claude Code on this repo, the `/run` skill (`.claude/skills/run`) starts both local servers, waits for health checks, and screenshots the dashboard — a faster way to verify changes than the manual steps below.

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
python -m uvicorn backend.main:app --reload --port 8000
```

Use `python -m uvicorn ...`, not bare `uvicorn` — on some machines the plain `uvicorn` console script and `python`/`start.ps1`'s background process resolve to different Python installs, and the console-script one can be missing packages installed via `pip install -r requirements.txt` against a different interpreter. If you restart the backend and your edits don't seem to take effect, it's likely a zombie `--reload` process still running on port 8000 serving stale bytecode — `start.ps1` kills processes by port but may miss subprocesses `--reload` spawned; find and kill the real owning process (e.g. `Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess -Force` on Windows) and restart.

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

## Pointing at a different Postgres instance (e.g. your own AWS RDS)

Change `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgresql://user:password@your-instance.rds.amazonaws.com:5432/sg360_bol
```

No code changes required. The connection pool is already configured for RDS (`pool_pre_ping=True` handles the idle-timeout reconnect).

This is a separate thing from the live AWS deployment mentioned above — that one runs on Aurora Serverless v2 and gets its `DATABASE_URL` from AWS Secrets Manager (`AWS_SECRET_NAME`), not from a local `.env` file. This section is for pointing your *own* local/dev backend at a real Postgres instance somewhere other than localhost.

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
