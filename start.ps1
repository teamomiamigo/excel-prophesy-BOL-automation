# SG360 BOL Reconciliation — Start both servers
# Run from the project root: .\start.ps1

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Kill any process already listening on port 8000 or 3000
foreach ($port in @(8000, 3000)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped existing process on :$port" -ForegroundColor Yellow
    }
}
Start-Sleep -Seconds 1

Write-Host "Starting SG360 BOL backend on :8000..." -ForegroundColor Green
Start-Process -FilePath "python" `
    -ArgumentList "-m uvicorn backend.main:app --reload --port 8000" `
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
