param(
    [switch]$ResetDb,
    [string]$DatabaseUrl = "postgresql+psycopg://grandgousier:glodga@localhost:5432/grandgousier"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$env:GRANDGOUSIER_DATABASE_URL = $DatabaseUrl
Write-Host "GRANDGOUSIER_DATABASE_URL=$env:GRANDGOUSIER_DATABASE_URL"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating .venv with Python launcher..."
    py -3.11 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .

if ($ResetDb) {
    .\.venv\Scripts\python.exe -m grandgousier.cli reset-db --yes
} else {
    .\.venv\Scripts\python.exe -m grandgousier.cli init-db
}

.\.venv\Scripts\python.exe -m grandgousier.cli db-summary
