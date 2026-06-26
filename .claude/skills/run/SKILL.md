---
description: Launch the SG360 BOL Reconciliation app (FastAPI backend + Vite frontend), verify both servers are healthy, screenshot the dashboard, and report any errors or maintenance issues.
---

# /run — SG360 BOL App Launch & Health Check

## What this skill does

1. Kills any stale processes on ports 8000 and 3000
2. Starts both servers via `start.ps1`
3. Waits for them to respond
4. Hits key API endpoints to verify health
5. Takes a browser screenshot of the dashboard
6. Reports errors, warnings, and anything needing attention

---

## Launch steps

### Step 1 — Kill stale processes and start servers

```powershell
# Kill any existing processes on the ports
$ports = @(8000, 3000)
foreach ($port in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 1

# Launch both servers via start.ps1 in background, capture output
Start-Process powershell -ArgumentList '-NoProfile','-Command','& { cd C:\nikhilm\excel-prophesy-BOL-automation; .\start.ps1 2>&1 | Out-File C:\Users\nikhilm\AppData\Local\Temp\sg360-run.log -Encoding utf8 }' -WindowStyle Hidden
```

### Step 2 — Wait for backend to be ready (poll up to 20s)

```powershell
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest http://localhost:8000/health -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
}
if (-not $ready) { Write-Host "ERROR: Backend did not start within 20 seconds" }
```

### Step 3 — Health check

```powershell
$health = Invoke-WebRequest http://localhost:8000/health -UseBasicParsing | ConvertFrom-Json
# Report: status, mock_mode, db_online, version
```

**Interpret health response:**
- `mock_mode: true` → using in-memory mock data (no DB needed). Records 13–14 (third-party test) will be visible.
- `mock_mode: false` → live PostgreSQL. Real data from AWP-SQL-PROD pulls. `db_online` must be `true`.
- `db_online: false` → DATABASE_URL unreachable — report as a blocker.

### Step 4 — Verify key API endpoints

```powershell
# Pending records
$bols = Invoke-WebRequest http://localhost:8000/api/bols -UseBasicParsing | ConvertFrom-Json
# Approved records
$approved = Invoke-WebRequest http://localhost:8000/api/bols/approved -UseBasicParsing | ConvertFrom-Json
```

**Things to flag:**
- `is_third_party` field missing from records → migration didn't run; backend needs restart
- 0 pending records in live mode → may be normal (after approval) or may mean pull didn't run
- Any 5xx responses → report the error body

### Step 5 — Frontend check

```powershell
$fe = Invoke-WebRequest http://localhost:3000 -UseBasicParsing
# Should be HTTP 200 with HTML body
```

### Step 6 — Screenshot (using Claude in Chrome MCP)

Use `mcp__Claude_in_Chrome__tabs_context_mcp` + `mcp__Claude_in_Chrome__navigate` to open `http://localhost:3000`, then `mcp__Claude_in_Chrome__computer` with `action: screenshot` to capture the dashboard.

**What to look for in the screenshot:**
- SummaryBar shows correct counts (Manifest Only / Invoice Only / Ready to Review / Approved Today)
- Pending table renders with records
- Orange "3rd Party" button visible on rows with no invoice amount AND no BOL number
- ThirdPartySection visible (collapsed) if any records are marked third-party
- No blank/white page (would indicate React render error — check browser console)

---

## Startup log

```powershell
Get-Content C:\Users\nikhilm\AppData\Local\Temp\sg360-run.log -ErrorAction SilentlyContinue
```

**Common errors and fixes:**

| Error | Cause | Fix |
|---|---|---|
| `Port 8000 already in use` | Zombie uvicorn process | `Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess -Force` |
| `ModuleNotFoundError` | Missing Python dep | `pip install -r backend/requirements.txt` |
| `connection refused` on DB | PostgreSQL not running or wrong URL | Check `DATABASE_URL` in `.env`; start PostgreSQL service |
| `SELECT permission denied on VisualMail` | AWD-SQL-WH4 access | Known open issue — blocks pallet ZIP lookup in live mode; mock mode unaffected |
| Vite `ENOENT` / missing node_modules | Frontend deps not installed | `cd frontend && npm install` |
| React blank page | JS runtime error | Screenshot won't show content; check browser console via `mcp__Claude_in_Chrome__read_console_messages` |

---

## Environment overview

- **Backend:** Python 3.13, FastAPI, uvicorn `--reload`, port 8000
- **Frontend:** React 18, Vite dev server, port 3000
- **Proxy:** Vite proxies `/api/*` → `http://localhost:8000` — never hardcode `localhost:8000` in frontend
- **Mock mode:** `USE_MOCK_DATA=True` in `.env` → no DB or SQL Server needed
- **Live mode:** Needs PostgreSQL (`DATABASE_URL`) + optionally AWP-SQL-PROD (Windows auth)

## Known open issues (as of June 2026)

| # | Issue | Impact |
|---|---|---|
| Q2 | VisualMail `DestinationID` field unclear — using `Locations.AccountNumber` | Tariff ZIP lookup may be wrong in live mode |
| Q8 | Prophecy BOL sync not implemented | `get_prophecy_data()` is a stub |
| Q10 | SELECT permission denied on VisualMail (AWD-SQL-WH4) | Pallet data and SID export fail in live mode |
| — | `days_back` default is 10; invoices lag 11–18 days | Pass `?days_back=20` to pull to avoid missed matches |
| — | Duplicate `unapprove_bol` function in main.py | Harmless by coincidence; second definition wins |
