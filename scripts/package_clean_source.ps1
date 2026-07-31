param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PyprojectPath = Join-Path $ProjectRoot "pyproject.toml"
$VersionMatch = Select-String -Path $PyprojectPath -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $VersionMatch) {
    throw "Cannot determine version from $PyprojectPath"
}
$Version = $VersionMatch.Matches[0].Groups[1].Value

if (-not $OutputPath) {
    $OutputPath = Join-Path (Split-Path $ProjectRoot -Parent) "Grandgousier_${Version}_clean_source.zip"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (Test-Path $OutputPath) {
    Remove-Item $OutputPath -Force
}

Push-Location $ProjectRoot
try {
    & tar.exe -a -c -f $OutputPath `
        --exclude=.git `
        --exclude=.venv `
        --exclude=venv `
        --exclude=.idea `
        --exclude=.vscode `
        --exclude=.pytest_cache `
        --exclude=.ruff_cache `
        --exclude=.mypy_cache `
        --exclude='__pycache__' `
        --exclude='*.pyc' `
        --exclude='*.pyo' `
        --exclude='*.egg-info' `
        --exclude=logs `
        --exclude=exports `
        --exclude=reports `
        --exclude=artifacts/context `
        --exclude='backup_*.sql' `
        .
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Clean source archive: $OutputPath"
