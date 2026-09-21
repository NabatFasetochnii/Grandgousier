if (-not $env:GRANDGOUSIER_DATABASE_URL) { throw "GRANDGOUSIER_DATABASE_URL must be configured before running this script" }
$env:PGCLIENTENCODING="UTF8"
Write-Host "Database environment is configured"
