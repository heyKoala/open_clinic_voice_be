# ManageOPD backend — one-command dev setup for Windows (PowerShell)
# Usage: .\scripts\setup.ps1
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$VenvDir   = ".venv"
$EnvFile   = ".env"
$EnvExample = ".env.example"
$Python    = "python"

Write-Host "==> Creating virtual environment in ${VenvDir}/" -ForegroundColor Cyan
& $Python -m venv $VenvDir

Write-Host "==> Upgrading pip" -ForegroundColor Cyan
& "${VenvDir}\Scripts\python.exe" -m pip install --quiet --upgrade pip

Write-Host "==> Installing pinned dependencies from requirements.txt" -ForegroundColor Cyan
& "${VenvDir}\Scripts\python.exe" -m pip install --quiet -r requirements.txt

if (-Not (Test-Path $EnvFile)) {
    Write-Host "==> Copying ${EnvExample} -> ${EnvFile} (edit before running the server)" -ForegroundColor Cyan
    Copy-Item -Path $EnvExample -Destination $EnvFile
} else {
    Write-Host "==> ${EnvFile} already exists — skipping copy" -ForegroundColor Yellow
}

Write-Host "==> Running Django system check" -ForegroundColor Cyan
& "${VenvDir}\Scripts\python.exe" manage.py check

Write-Host "==> Applying migrations" -ForegroundColor Cyan
& "${VenvDir}\Scripts\python.exe" manage.py migrate --run-syncdb

Write-Host "==> Running test suite" -ForegroundColor Cyan
& "${VenvDir}\Scripts\python.exe" -m pytest

Write-Host ""
Write-Host "Setup complete. Start the dev server with:" -ForegroundColor Green
Write-Host "    .\.venv\Scripts\python.exe manage.py runserver" -ForegroundColor Green
