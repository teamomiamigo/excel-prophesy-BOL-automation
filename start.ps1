# SG360 BOL Reconciliation - Start both servers
# Run from the project root: .\start.ps1

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Kill ALL processes listening on port 8000 or 3000 (handles multiple zombie processes)
foreach ($port in @(8000, 3000)) {
    $pids = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).OwningProcess | Sort-Object -Unique
    foreach ($p in $pids) {
        taskkill /PID $p /F /T 2>$null | Out-Null
        Write-Host "Killed PID $p on :$port" -ForegroundColor Yellow
    }
}
# Belt-and-suspenders: kill any remaining python/node on those ports by name if still stuck
Start-Sleep -Seconds 1
$stillOn8000 = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($stillOn8000) {
    Write-Host "WARNING: Port 8000 still in use by PID(s): $stillOn8000 - attempting name-based kill" -ForegroundColor Red
    Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

Write-Host "Starting SG360 BOL backend on :8000..." -ForegroundColor Green
Start-Process -FilePath "python" `
    -ArgumentList "-m", "uvicorn", "backend.main:app", "--reload", "--port", "8000" `
    -WorkingDirectory $root `
    -NoNewWindow

Start-Sleep -Seconds 2

Write-Host "Starting SG360 BOL frontend on :3000..." -ForegroundColor Green
Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/c cd frontend && npm run dev" `
    -WorkingDirectory $root `
    -NoNewWindow

Write-Host ""
Write-Host "Dashboard: http://localhost:3000" -ForegroundColor Cyan
Write-Host "API docs:  http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
