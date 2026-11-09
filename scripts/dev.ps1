# Start Caliber API + Next.js dashboard (Windows).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}
& .\.venv\Scripts\python -m pip install -e ".[dev]"
# Do not seed on startup. seed.py refuses once football data exists, and a restart
# must not be able to write demo forecasts into the research database.

$env:PYTHONPATH = "packages\core;services\api"
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m","uvicorn","app.main:app","--reload","--app-dir","services\api","--port","8000"

Set-Location apps\web
if (-not (Test-Path "node_modules")) {
  npm install
}
npm run dev
