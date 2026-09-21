#requires -Version 5.1
<#
.SYNOPSIS
    Batch processing of a heterogeneous Grandgousier observation archive (v1.5).

.DESCRIPTION
    Scans an archive tree for directories that directly contain FITS files.
    By default only raw-looking camera directories named EAST_* or WEST_* are
    queued. Obvious derived directories (Photometry, old, *_bad, *FWHM, etc.)
    are excluded.

    Jobs are grouped by the first directory below -Root (for example TIC...).
    The first successful job for an object creates an automatic Gaia DR3
    catalog. The same catalog is reused for the remaining filters/nights of
    that object.

    Without -Execute the script only builds an inventory and queue. Re-running
    with the same -BatchName resumes from PostgreSQL and skips completed jobs.
    state.csv and summary_latest.csv are regenerated as compatibility exports.

    Version 1.5.2 keeps the v1.4 native runner and moves queue/attempt state to PostgreSQL. CSV files are compatibility exports. It also launches Python through System.Diagnostics.Process with
    asynchronous stdout/stderr capture, preserves the real native exit code,
    and runs a PostgreSQL preflight before executing the queue. It tolerates optional manifest fields and recovers already-successful manifests after parser updates. It also prints
    a unique startup marker so obsolete copies can be identified immediately.

.EXAMPLE
    # 1. Safe inventory only
    .\process_all_tess_observations_v1_5.ps1

.EXAMPLE
    # 2. Process the complete queue
    .\process_all_tess_observations_v1_5.ps1 -Execute

.EXAMPLE
    # 3. Retry failed jobs, but do not recompute completed raw photometry
    .\process_all_tess_observations_v1_5.ps1 -Execute -RetryFailed

.EXAMPLE
    # Fresh rebuild after reset-derived-data. Ignore legacy state/manifests and
    # recompute completed photometry with the current pipeline.
    .\process_all_tess_observations_v1_5.ps1 -Execute -FreshStart -RetryCompleted -ForceRecomputePhotometry

.EXAMPLE
    # 4. Include non-standard FITS directories after reviewing inventory.csv
    .\process_all_tess_observations_v1_5.ps1 -Execute -IncludeAmbiguous

.EXAMPLE
    # 5. Use a target map CSV with columns ObjectName,TargetSourceId
    .\process_all_tess_observations_v1_5.ps1 -Execute `
        -TargetMapCsv "C:\Users\nikita\Desktop\tess_targets.csv" `
        -ExportTargetLightCurves
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$Root = "C:\Users\nikita\Desktop\tess",

    [Parameter()]
    [string]$ProjectRoot = "C:\PycharmProjects\Grandgousier",

    [Parameter()]
    [string]$PythonExe = "python",

    [Parameter()]
    [string]$BatchName = "tess_all_observations",

    [Parameter()]
    [string]$DatabaseUrl,

    [Parameter()]
    [switch]$Execute,

    [Parameter()]
    [switch]$IncludeAmbiguous,

    [Parameter()]
    [switch]$RetryFailed,

    [Parameter()]
    [switch]$RetryCompleted,

    [Parameter()]
    [switch]$FreshStart,

    [Parameter()]
    [switch]$ForceRecomputePhotometry,

    [Parameter()]
    [switch]$DiagnoseScienceMaterialization,

    [Parameter()]
    [switch]$DiagnoseForcedPhotometry,

    [Parameter()]
    [switch]$DiagnoseCalibrationPerformance,

    [Parameter()]
    [switch]$WithCandidatePlots,

    [Parameter()]
    [string]$TargetMapCsv,

    [Parameter()]
    [switch]$ExportTargetLightCurves,

    [Parameter()]
    [ValidateRange(1, 1000000)]
    [int]$MinFramesPerSeries = 10,

    [Parameter()]
    [ValidateRange(0, 120)]
    [int]$AutoCatalogMarginArcmin = 10,

    [Parameter()]
    [ValidateRange(1, 1000000)]
    [double]$WorkingCatalogMagLimit = 19.0,

    [Parameter()]
    [ValidateRange(0, 1000000)]
    [int]$MaxWorkingSourcesPerFrame = 2000,

    [Parameter()]
    [ValidateRange(0, 1000000)]
    [int]$StopAfter = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$BatchScriptVersion = "1.5.2"

$FitsExtensions = @(".fits", ".fit", ".fts")
$PreferredRawDirectoryRegex = "^(EAST|WEST)_[A-Za-z0-9]+$"
$CompletedStatuses = @("DONE", "DONE_WITH_WARNINGS")


function Convert-ToWindowsCommandLineArgument {
    param([AllowEmptyString()][string]$Value)

    if ($null -eq $Value) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }

    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0

    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }

        if ($character -eq '"') {
            if ($backslashes -gt 0) {
                [void]$builder.Append(('\' * ($backslashes * 2)))
            }
            [void]$builder.Append('\"')
            $backslashes = 0
            continue
        }

        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }

    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-GrandgousierProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$ConsoleLogPath,
        [ValidateRange(5, 3600)][int]$HeartbeatSeconds = 60
    )

    Remove-Item -LiteralPath $ConsoleLogPath -Force -ErrorAction SilentlyContinue

    $argumentLine = (($Arguments | ForEach-Object {
        Convert-ToWindowsCommandLineArgument -Value ([string]$_)
    }) -join ' ')

    @(
        "Executable: $Executable"
        "WorkingDirectory: $WorkingDirectory"
        "Arguments: $argumentLine"
        "StartedAt: $((Get-Date).ToString('o'))"
        ""
    ) | Set-Content -LiteralPath $ConsoleLogPath -Encoding UTF8

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = $argumentLine
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) {
            throw "Failed to start native process: $Executable"
        }

        # Start both asynchronous readers immediately to avoid pipe-buffer deadlocks.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        $startedAt = Get-Date
        $lastHeartbeat = $startedAt
        while (-not $process.HasExited) {
            Start-Sleep -Seconds 1
            $now = Get-Date
            if (($now - $lastHeartbeat).TotalSeconds -ge $HeartbeatSeconds) {
                $elapsed = [math]::Round(($now - $startedAt).TotalMinutes, 1)
                Write-BatchMessage "Python process still running: elapsed=${elapsed} min"
                $lastHeartbeat = $now
            }
        }

        $process.WaitForExit()
        $stdoutText = [string]$stdoutTask.Result
        $stderrText = [string]$stderrTask.Result
        $nativeExitCode = [int]$process.ExitCode

        Add-Content -LiteralPath $ConsoleLogPath -Value "===== STDOUT =====" -Encoding UTF8
        if ($stdoutText) {
            Add-Content -LiteralPath $ConsoleLogPath -Value $stdoutText -Encoding UTF8
        }
        Add-Content -LiteralPath $ConsoleLogPath -Value "===== STDERR =====" -Encoding UTF8
        if ($stderrText) {
            Add-Content -LiteralPath $ConsoleLogPath -Value $stderrText -Encoding UTF8
        }
        Add-Content -LiteralPath $ConsoleLogPath -Value "FinishedAt: $((Get-Date).ToString('o'))" -Encoding UTF8
        Add-Content -LiteralPath $ConsoleLogPath -Value "ExitCode: $nativeExitCode" -Encoding UTF8

        return $nativeExitCode
    }
    finally {
        $process.Dispose()
    }
}

function Test-GrandgousierDatabase {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$PreflightLogPath
    )

    $pythonCode = "from sqlalchemy import text; from grandgousier.db.session import make_engine; engine=make_engine(); connection=engine.connect(); connection.execute(text('SELECT 1')); connection.close(); engine.dispose(); print('DATABASE_OK')"
    Write-BatchMessage "Checking PostgreSQL connection before queue execution"
    $exitCode = Invoke-GrandgousierProcess `
        -Executable $Executable `
        -Arguments @("-c", $pythonCode) `
        -WorkingDirectory $WorkingDirectory `
        -ConsoleLogPath $PreflightLogPath `
        -HeartbeatSeconds 30

    if ($exitCode -ne 0) {
        throw "PostgreSQL preflight failed with exit code $exitCode. Set a valid GRANDGOUSIER_DATABASE_URL (or use -DatabaseUrl) and inspect $PreflightLogPath"
    }

    Write-BatchMessage "PostgreSQL preflight passed" "OK"
}

function Invoke-GrandgousierJsonCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    Push-Location -LiteralPath $WorkingDirectory
    try {
        $output = & $Executable @Arguments 2>&1
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $text = (($output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine).Trim()
    if ($nativeExitCode -ne 0) {
        throw ("Grandgousier control command failed with exit code {0}: {1}" -f $nativeExitCode, $text)
    }
    try {
        return ($text | ConvertFrom-Json)
    }
    catch {
        throw "Grandgousier control command returned invalid JSON: $text"
    }
}

function Write-BatchMessage {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR", "OK")][string]$Level = "INFO"
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $prefix = "[$timestamp][$Level]"
    switch ($Level) {
        "WARN"  { Write-Host "$prefix $Message" -ForegroundColor Yellow }
        "ERROR" { Write-Host "$prefix $Message" -ForegroundColor Red }
        "OK"    { Write-Host "$prefix $Message" -ForegroundColor Green }
        default { Write-Host "$prefix $Message" }
    }
}

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path)
}

function Get-RelativePathCompat {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $base = [System.IO.Path]::GetFullPath($BasePath).TrimEnd([char[]]@('\', '/'))
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside archive root: $full"
    }
    return $full.Substring($base.Length).TrimStart([char[]]@('\', '/'))
}

function Get-ShortHash {
    param([Parameter(Mandatory = $true)][string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text.ToLowerInvariant())
        $hash = $sha.ComputeHash($bytes)
        $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
        return $hex.Substring(0, 12)
    }
    finally {
        $sha.Dispose()
    }
}

function Get-FitsDirectorySignature {
    param([Parameter(Mandatory = $true)][object[]]$Files)

    # Fast metadata fingerprint for copied observation directories. FITS file
    # names already encode timestamps/channel for this archive, and adding the
    # byte length prevents same-name but non-identical files from collapsing.
    # This deliberately avoids hashing multi-GB image contents during inventory.
    $tokens = @(
        $Files |
            Sort-Object Name, Length |
            ForEach-Object {
                "{0}|{1}" -f ([string]$_.Name).ToLowerInvariant(), ([int64]$_.Length)
            }
    )
    return Get-ShortHash -Text ($tokens -join "`n")
}

function Test-StronglyExcludedPath {
    param([Parameter(Mandatory = $true)][string[]]$Components)

    foreach ($component in $Components) {
        if ($component -match "^(?i:Photometry|old|exports|reports|logs|__pycache__|\.git)$") {
            return "derived:$component"
        }
        if ($component -match "(?i)(^|[_-])bad($|[_-])") {
            return "marked_bad:$component"
        }
        if ($component -match "(?i)FWHM") {
            return "derived_fwhm:$component"
        }
        if ($component -match "^(?i:[BVRI]_?[0-9]+(?:\.[0-9]+)?.*)$") {
            return "derived_aperture:$component"
        }
        if ($component -match "^(?i:[BVRI]_?)$") {
            return "ambiguous_single_filter_dir:$component"
        }
    }
    return $null
}

function Convert-ToSafeName {
    param([Parameter(Mandatory = $true)][string]$Text)
    $safe = $Text -replace "[^A-Za-z0-9._-]+", "_"
    return $safe.Trim([char[]]@('_', '.'))
}

function Convert-ToInvariantString {
    param([Parameter(Mandatory = $true)]$Value)
    if ($Value -is [System.IFormattable]) {
        return $Value.ToString($null, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    return [string]$Value
}

function Get-LatestStateByJob {
    param([Parameter(Mandatory = $true)][string]$StateCsv)

    $map = @{}
    if (Test-Path -LiteralPath $StateCsv) {
        foreach ($row in Import-Csv -LiteralPath $StateCsv) {
            if ($row.JobId) {
                $map[$row.JobId] = $row
            }
        }
    }
    return $map
}

function Get-CatalogByObjectFromState {
    param([Parameter(Mandatory = $true)][hashtable]$LatestState)

    $map = @{}
    foreach ($row in $LatestState.Values) {
        if (
            $row.ObjectName -and
            $row.CatalogName -and
            ($CompletedStatuses -contains [string]$row.Status)
        ) {
            $map[$row.ObjectName] = $row.CatalogName
        }
    }
    return $map
}

function Get-TargetMap {
    param([string]$CsvPath)

    $map = @{}
    if (-not $CsvPath) {
        return $map
    }
    if (-not (Test-Path -LiteralPath $CsvPath -PathType Leaf)) {
        throw "Target map CSV not found: $CsvPath"
    }

    foreach ($row in Import-Csv -LiteralPath $CsvPath) {
        if (-not $row.ObjectName) {
            continue
        }
        if (-not $row.TargetSourceId) {
            continue
        }
        $map[[string]$row.ObjectName] = [string]$row.TargetSourceId
    }
    return $map
}

function Add-StateRow {
    param(
        [Parameter(Mandatory = $true)][string]$StateCsv,
        [Parameter(Mandatory = $true)][psobject]$Row
    )

    $append = Test-Path -LiteralPath $StateCsv
    $Row | Export-Csv -LiteralPath $StateCsv -NoTypeInformation -Encoding UTF8 -Append:$append
}

function Get-OptionalPropertyValue {
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}


function Read-ManifestSummary {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][int]$ExitCode
    )

    $summary = [ordered]@{
        Status = if ($ExitCode -eq 0) { "NO_MANIFEST" } else { "PROCESS_EXIT_$ExitCode" }
        CatalogName = ""
        CatalogSources = 0
        PipelineCount = 0
        ScienceStatus = ""
        TotalPoints = 0
        AcceptedPoints = 0
        Health = if ($ExitCode -eq 0) { "WARNING_NO_MANIFEST" } else { "FAILED_PROCESS" }
        Message = ""
        PipelineRunId = ""
    }

    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        return [pscustomobject]$summary
    }

    try {
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json

        $manifestStatus = Get-OptionalPropertyValue -Object $manifest -Name "status"
        if ($null -ne $manifestStatus) {
            $summary.Status = [string]$manifestStatus
        }

        $catalogName = Get-OptionalPropertyValue -Object $manifest -Name "catalog_name"
        if ($null -ne $catalogName) {
            $summary.CatalogName = [string]$catalogName
        }

        $catalogResultsValue = Get-OptionalPropertyValue -Object $manifest -Name "catalog_results"
        foreach ($catalogResult in @($catalogResultsValue)) {
            if ($null -eq $catalogResult) {
                continue
            }

            # Different process-archive-path catalog modes expose different
            # count fields. n_sources is optional, so use the best available
            # non-negative count instead of failing under StrictMode.
            foreach ($countName in @("n_sources", "inserted_or_updated", "read_rows")) {
                $countValue = Get-OptionalPropertyValue -Object $catalogResult -Name $countName
                if ($null -ne $countValue -and [string]$countValue -ne "") {
                    $summary.CatalogSources = [math]::Max(
                        [int64]$summary.CatalogSources,
                        [int64]$countValue
                    )
                }
            }
        }

        $pipelineResultsValue = Get-OptionalPropertyValue -Object $manifest -Name "pipeline_results"
        $pipelines = @($pipelineResultsValue | Where-Object { $null -ne $_ })
        $summary.PipelineCount = $pipelines.Count

        $pipelineRunIds = @()
        $scienceStatuses = @()
        $totalPoints = 0L
        $acceptedPoints = 0L
        foreach ($pipeline in $pipelines) {
            $pipelineRunId = Get-OptionalPropertyValue -Object $pipeline -Name "pipeline_run_id"
            if ($null -ne $pipelineRunId -and [string]$pipelineRunId -ne "") {
                $pipelineRunIds += [int64]$pipelineRunId
            }

            $scienceStatus = Get-OptionalPropertyValue -Object $pipeline -Name "science_status"
            if ($scienceStatus) {
                $scienceStatuses += [string]$scienceStatus
            }

            $pipelineTotalPoints = Get-OptionalPropertyValue -Object $pipeline -Name "total_points"
            if ($null -ne $pipelineTotalPoints -and [string]$pipelineTotalPoints -ne "") {
                $totalPoints += [int64]$pipelineTotalPoints
            }

            $pipelineAcceptedPoints = Get-OptionalPropertyValue -Object $pipeline -Name "accepted_points"
            if ($null -ne $pipelineAcceptedPoints -and [string]$pipelineAcceptedPoints -ne "") {
                $acceptedPoints += [int64]$pipelineAcceptedPoints
            }
        }
        $uniquePipelineRunIds = @($pipelineRunIds | Sort-Object -Unique)
        if ($uniquePipelineRunIds.Count -eq 1) {
            $summary.PipelineRunId = [string]$uniquePipelineRunIds[0]
        }
        $summary.ScienceStatus = ($scienceStatuses | Sort-Object -Unique) -join ","
        $summary.TotalPoints = $totalPoints
        $summary.AcceptedPoints = $acceptedPoints

        if ($ExitCode -ne 0) {
            $summary.Health = "FAILED_PROCESS"
        }
        elseif ($CompletedStatuses -notcontains $summary.Status) {
            $summary.Health = "FAILED_MANIFEST_STATUS"
        }
        elseif ($pipelines.Count -eq 0) {
            $summary.Health = "WARNING_NO_PIPELINE"
        }
        elseif ($totalPoints -le 0) {
            $summary.Health = "WARNING_ZERO_POINTS"
        }
        elseif (@($scienceStatuses | Where-Object { $_ -ne "OK" }).Count -gt 0) {
            $summary.Health = "WARNING_SCIENCE_STATUS"
        }
        else {
            $summary.Health = "OK"
        }

        $warningsValue = Get-OptionalPropertyValue -Object $manifest -Name "warnings"
        if ($warningsValue) {
            $summary.Message = (@($warningsValue) -join " | ")
        }
    }
    catch {
        $summary.Status = "MANIFEST_PARSE_ERROR"
        $summary.Health = "FAILED_MANIFEST_PARSE"
        $summary.Message = $_.Exception.Message
    }

    return [pscustomobject]$summary
}

# Validate runtime paths.
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "Archive root not found: $Root"
}
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Grandgousier project root not found: $ProjectRoot"
}

$RootFull = Get-FullPath -Path $Root
$ProjectFull = Get-FullPath -Path $ProjectRoot
$BatchDir = Join-Path $ProjectFull (Join-Path "batch_runs" $BatchName)
$ManifestDir = Join-Path $BatchDir "manifests"
$ConsoleLogDir = Join-Path $BatchDir "console_logs"
$InventoryCsv = Join-Path $BatchDir "inventory.csv"
$QueueCsv = Join-Path $BatchDir "queue.csv"
$StateCsv = Join-Path $BatchDir "state.csv"
$SummaryCsv = Join-Path $BatchDir "summary_latest.csv"
$DatabasePreflightLog = Join-Path $BatchDir "database_preflight.log"
$BatchConfigJson = Join-Path $BatchDir "batch_config.json"

New-Item -ItemType Directory -Path $BatchDir -Force | Out-Null
New-Item -ItemType Directory -Path $ManifestDir -Force | Out-Null
New-Item -ItemType Directory -Path $ConsoleLogDir -Force | Out-Null


# Guard against accidentally running an obsolete copied script body.
$scriptText = Get-Content -LiteralPath $PSCommandPath -Raw -ErrorAction Stop
if ($scriptText -match '&\s*\$PythonExe\s+@arguments\s+2>&1\s*\|\s*Tee-Object') {
    throw "Obsolete native runner detected in $PSCommandPath. Use process_all_tess_observations_v1_5.ps1."
}

if ($DatabaseUrl) {
    $env:GRANDGOUSIER_DATABASE_URL = $DatabaseUrl
}

$dbSource = if ($DatabaseUrl) { "-DatabaseUrl parameter" } elseif ($env:GRANDGOUSIER_DATABASE_URL) { "GRANDGOUSIER_DATABASE_URL environment" } else { "project .env or built-in default" }
Write-BatchMessage "Batch script version $BatchScriptVersion; native runner=System.Diagnostics.Process" "OK"
Write-BatchMessage "Database configuration source: $dbSource"
Write-BatchMessage "Script path: $PSCommandPath"
Write-BatchMessage "Scanning FITS files under $RootFull"

$fitsFiles = Get-ChildItem -LiteralPath $RootFull -File -Recurse -Force |
    Where-Object { $FitsExtensions -contains $_.Extension.ToLowerInvariant() }

$inventory = @()
foreach ($group in ($fitsFiles | Group-Object DirectoryName)) {
    $directoryPath = [System.IO.Path]::GetFullPath([string]$group.Name)
    $relativePath = Get-RelativePathCompat -BasePath $RootFull -Path $directoryPath
    $components = @($relativePath -split "[\\/]+" | Where-Object { $_ })
    if ($components.Count -eq 0) {
        continue
    }

    $leafName = Split-Path -Leaf $directoryPath
    $objectName = [string]$components[0]
    $observationName = if ($components.Count -ge 2) { [string]$components[1] } else { "ROOT" }
    $excludeReason = Test-StronglyExcludedPath -Components $components
    $preferred = $leafName -match $PreferredRawDirectoryRegex
    $selected = (-not $excludeReason) -and ($preferred -or $IncludeAmbiguous)
    $classification = if ($excludeReason) {
        "EXCLUDED"
    }
    elseif ($preferred) {
        "RAW_PREFERRED"
    }
    else {
        "AMBIGUOUS"
    }

    $groupFiles = @($group.Group)
    $fitsBytes = ($groupFiles | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $fitsBytes) { $fitsBytes = 0 }
    $contentSignature = Get-FitsDirectorySignature -Files $groupFiles
    $jobId = "{0}_{1}" -f (Convert-ToSafeName -Text $objectName), (Get-ShortHash -Text $directoryPath)
    $inventory += [pscustomobject]@{
        JobId = $jobId
        ObjectName = $objectName
        ObservationName = $observationName
        DirectoryName = $leafName
        Path = $directoryPath
        RelativePath = $relativePath
        FitsCount = $group.Count
        FitsBytes = [int64]$fitsBytes
        ContentSignature = $contentSignature
        DuplicateOf = ""
        Classification = $classification
        ExcludeReason = if ($excludeReason) { $excludeReason } else { "" }
        Selected = $selected
    }
}

$inventory = @($inventory | Sort-Object ObjectName, ObservationName, Path)

# Guard against copied archive directories such as 3988 and 3988_1. Without
# this check, path-based frame_uid values make byte-identical copies look like
# new frames and create duplicate field_series/science products. A duplicate is
# recognized only inside the same object/channel and only when the complete
# FITS filename+size signature agrees. The first sorted path is canonical.
$duplicateOwners = @{}
foreach ($row in $inventory) {
    if (-not $row.Selected) {
        continue
    }
    $dedupeKey = "{0}|{1}|{2}|{3}|{4}" -f (
        ([string]$row.ObjectName).ToLowerInvariant(),
        ([string]$row.DirectoryName).ToLowerInvariant(),
        [int64]$row.FitsCount,
        [int64]$row.FitsBytes,
        [string]$row.ContentSignature
    )
    if ($duplicateOwners.ContainsKey($dedupeKey)) {
        $owner = $duplicateOwners[$dedupeKey]
        $row.Selected = $false
        $row.Classification = "EXCLUDED"
        $row.DuplicateOf = [string]$owner.RelativePath
        $row.ExcludeReason = "duplicate_directory:$($owner.RelativePath)"
    }
    else {
        $duplicateOwners[$dedupeKey] = $row
    }
}

$inventory | Export-Csv -LiteralPath $InventoryCsv -NoTypeInformation -Encoding UTF8

$queue = @($inventory | Where-Object { $_.Selected -eq $true })
$queue | Export-Csv -LiteralPath $QueueCsv -NoTypeInformation -Encoding UTF8

$batchConfig = [ordered]@{
    root = $RootFull
    include_ambiguous = [bool]$IncludeAmbiguous
    min_frames_per_series = $MinFramesPerSeries
    auto_catalog_margin_arcmin = $AutoCatalogMarginArcmin
    working_catalog_mag_limit = $WorkingCatalogMagLimit
    max_working_sources_per_frame = $MaxWorkingSourcesPerFrame
    target_map_csv = if ($TargetMapCsv) { [string]$TargetMapCsv } else { $null }
    export_target_light_curves = [bool]$ExportTargetLightCurves
    with_candidate_plots = [bool]$WithCandidatePlots
    retry_failed = [bool]$RetryFailed
    retry_completed = [bool]$RetryCompleted
    fresh_start = [bool]$FreshStart
    force_recompute_photometry = [bool]$ForceRecomputePhotometry
    diagnose_science_materialization = [bool]$DiagnoseScienceMaterialization
    diagnose_forced_photometry = [bool]$DiagnoseForcedPhotometry
    diagnose_calibration_performance = [bool]$DiagnoseCalibrationPerformance
    duplicate_directory_guard = "filename_size_signature_v1"
    stop_after = $StopAfter
}
$batchConfig | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $BatchConfigJson -Encoding UTF8

$preferredCount = @($inventory | Where-Object { $_.Classification -eq "RAW_PREFERRED" }).Count
$ambiguousCount = @($inventory | Where-Object { $_.Classification -eq "AMBIGUOUS" }).Count
$excludedCount = @($inventory | Where-Object { $_.Classification -eq "EXCLUDED" }).Count
$duplicateCount = @($inventory | Where-Object { ([string]$_.ExcludeReason).StartsWith("duplicate_directory:") }).Count
$objectCount = @($queue | Select-Object -ExpandProperty ObjectName -Unique).Count
$totalFits = ($queue | Measure-Object -Property FitsCount -Sum).Sum
if ($null -eq $totalFits) { $totalFits = 0 }

Write-BatchMessage "Inventory: preferred=$preferredCount, ambiguous=$ambiguousCount, excluded=$excludedCount, duplicate_copies=$duplicateCount"
Write-BatchMessage "Queue: jobs=$($queue.Count), objects=$objectCount, FITS=$totalFits"
Write-BatchMessage "Inventory written to $InventoryCsv"
Write-BatchMessage "Queue written to $QueueCsv"

if ($ambiguousCount -gt 0 -and -not $IncludeAmbiguous) {
    Write-BatchMessage "Ambiguous FITS directories were not queued. Review inventory.csv and use -IncludeAmbiguous only if they are original frames." "WARN"
}

if (-not $Execute) {
    Write-BatchMessage "Plan-only mode complete. Run the same command with -Execute to start processing." "OK"
    return
}

Test-GrandgousierDatabase `
    -Executable $PythonExe `
    -WorkingDirectory $ProjectFull `
    -PreflightLogPath $DatabasePreflightLog

if ($queue.Count -eq 0) {
    throw "No jobs selected. Review $InventoryCsv"
}

$syncArgs = @(
    "-m", "grandgousier.cli", "batch-sync-plan",
    "--batch-name", $BatchName,
    "--root", $RootFull,
    "--inventory-csv", $InventoryCsv,
    "--queue-csv", $QueueCsv,
    "--script-name", (Split-Path -Leaf $PSCommandPath),
    "--script-version", $BatchScriptVersion,
    "--config-json-file", $BatchConfigJson
)
if ((-not $FreshStart) -and (Test-Path -LiteralPath $StateCsv -PathType Leaf)) {
    $syncArgs += @("--legacy-state-csv", $StateCsv)
}
elseif ($FreshStart) {
    Write-BatchMessage "FreshStart enabled: legacy state.csv import is disabled." "OK"
}
$batchSync = Invoke-GrandgousierJsonCommand `
    -Executable $PythonExe `
    -Arguments $syncArgs `
    -WorkingDirectory $ProjectFull
$BatchRunId = [int64]$batchSync.batch_run_id
Write-BatchMessage "DB batch state synchronized: batch_run_id=$BatchRunId, plan_revision=$($batchSync.plan_revision), imported_legacy_attempts=$($batchSync.imported_legacy_attempts)" "OK"

[void](Invoke-GrandgousierJsonCommand `
    -Executable $PythonExe `
    -Arguments @(
        "-m", "grandgousier.cli", "batch-export-state",
        "--batch-run-id", [string]$BatchRunId,
        "--state-csv", $StateCsv,
        "--summary-csv", $SummaryCsv
    ) `
    -WorkingDirectory $ProjectFull)

$latestState = Get-LatestStateByJob -StateCsv $StateCsv
$catalogByObject = Get-CatalogByObjectFromState -LatestState $latestState
$targetByObject = Get-TargetMap -CsvPath $TargetMapCsv
$blockedObjects = @{}
$processedThisRun = 0

Write-BatchMessage "Starting execution. Authoritative state: PostgreSQL batch_run_id=$BatchRunId; CSV mirror: $StateCsv"

foreach ($job in $queue) {
    if ($StopAfter -gt 0 -and $processedThisRun -ge $StopAfter) {
        Write-BatchMessage "StopAfter=$StopAfter reached; remaining jobs stay queued." "WARN"
        break
    }

    $previous = $null
    if ($latestState.ContainsKey($job.JobId)) {
        $previous = $latestState[$job.JobId]
    }

    $manifestPath = Join-Path $ManifestDir ($job.JobId + ".json")
    $consoleLogPath = Join-Path $ConsoleLogDir ($job.JobId + ".log")
    $attempt = 0

    # A prior Grandgousier run may have completed successfully while an older
    # batch parser rejected an optional manifest field. Recover that result
    # without repeating expensive photometry.
    if (
        (-not $FreshStart) -and
        $previous -and
        ($CompletedStatuses -notcontains [string]$previous.Status) -and
        (Test-Path -LiteralPath $manifestPath -PathType Leaf)
    ) {
        $recoveredSummary = Read-ManifestSummary -ManifestPath $manifestPath -ExitCode 0
        if (
            ($CompletedStatuses -contains [string]$recoveredSummary.Status) -and
            [int]$recoveredSummary.PipelineCount -gt 0 -and
            [int64]$recoveredSummary.TotalPoints -gt 0
        ) {
            if ($recoveredSummary.CatalogName) {
                $catalogByObject[$job.ObjectName] = [string]$recoveredSummary.CatalogName
            }

            $attemptInfo = Invoke-GrandgousierJsonCommand `
                -Executable $PythonExe `
                -Arguments @(
                    "-m", "grandgousier.cli", "batch-start-attempt",
                    "--batch-run-id", [string]$BatchRunId,
                    "--job-id", [string]$job.JobId,
                    "--attempt-kind", "RECOVERY",
                    "--manifest-path", $manifestPath,
                    "--console-log-path", $consoleLogPath,
                    "--metadata-json-file", $BatchConfigJson
                ) `
                -WorkingDirectory $ProjectFull
            $attempt = [int]$attemptInfo.attempt_no
            $batchJobAttemptId = [int64]$attemptInfo.batch_job_attempt_id
            $recoveredAt = Get-Date
            $recoveredMessageParts = @(
                "Recovered existing successful manifest after batch-parser update"
            )
            if ($recoveredSummary.Message) {
                $recoveredMessageParts += [string]$recoveredSummary.Message
            }
            $recoveredRow = [pscustomobject]@{
                Timestamp = $recoveredAt.ToString("o")
                Attempt = $attempt
                JobId = $job.JobId
                ObjectName = $job.ObjectName
                ObservationName = $job.ObservationName
                Path = $job.Path
                RelativePath = $job.RelativePath
                FitsCount = $job.FitsCount
                ExitCode = 0
                Status = $recoveredSummary.Status
                Health = $recoveredSummary.Health
                ScienceStatus = $recoveredSummary.ScienceStatus
                CatalogName = $recoveredSummary.CatalogName
                CatalogSources = $recoveredSummary.CatalogSources
                PipelineCount = $recoveredSummary.PipelineCount
                TotalPoints = $recoveredSummary.TotalPoints
                AcceptedPoints = $recoveredSummary.AcceptedPoints
                ElapsedSec = 0
                ManifestPath = $manifestPath
                ConsoleLogPath = $consoleLogPath
                Message = ($recoveredMessageParts -join " | ")
                PipelineRunId = $recoveredSummary.PipelineRunId
            }
            $finishRecoveryArgs = @(
                "-m", "grandgousier.cli", "batch-finish-attempt",
                "--batch-job-attempt-id", [string]$batchJobAttemptId,
                "--exit-code", "0",
                "--status", [string]$recoveredSummary.Status,
                "--health", [string]$recoveredSummary.Health,
                "--catalog-sources", [string]$recoveredSummary.CatalogSources,
                "--pipeline-count", [string]$recoveredSummary.PipelineCount,
                "--total-points", [string]$recoveredSummary.TotalPoints,
                "--accepted-points", [string]$recoveredSummary.AcceptedPoints,
                "--elapsed-sec", "0",
                "--manifest-path", $manifestPath,
                "--console-log-path", $consoleLogPath
            )
            if (-not [string]::IsNullOrWhiteSpace([string]$recoveredSummary.ScienceStatus)) {
                $finishRecoveryArgs += @("--science-status", [string]$recoveredSummary.ScienceStatus)
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$recoveredSummary.CatalogName)) {
                $finishRecoveryArgs += @("--catalog-name", [string]$recoveredSummary.CatalogName)
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$recoveredRow.Message)) {
                $finishRecoveryArgs += @("--message", [string]$recoveredRow.Message)
            }
            if ($recoveredSummary.PipelineRunId) {
                $finishRecoveryArgs += @("--pipeline-run-id", [string]$recoveredSummary.PipelineRunId)
            }
            [void](Invoke-GrandgousierJsonCommand -Executable $PythonExe -Arguments $finishRecoveryArgs -WorkingDirectory $ProjectFull)
            $latestState[$job.JobId] = $recoveredRow
            $processedThisRun++
            Write-BatchMessage "RECOVERED $($job.RelativePath): points=$($recoveredSummary.TotalPoints), accepted=$($recoveredSummary.AcceptedPoints), catalog=$($recoveredSummary.CatalogName)" "OK"
            continue
        }
    }

    if ($previous -and -not $RetryCompleted -and ($CompletedStatuses -contains [string]$previous.Status)) {
        Write-BatchMessage "SKIP completed: $($job.RelativePath) [$($previous.Status)]"
        continue
    }

    if ($previous -and -not $RetryFailed -and -not $RetryCompleted -and ($CompletedStatuses -notcontains [string]$previous.Status)) {
        Write-BatchMessage "SKIP previous failure: $($job.RelativePath) [$($previous.Status)]. Use -RetryFailed." "WARN"
        continue
    }

    if ($blockedObjects.ContainsKey($job.ObjectName)) {
        Write-BatchMessage "SKIP blocked object $($job.ObjectName): $($blockedObjects[$job.ObjectName])" "WARN"
        continue
    }

    $catalogName = ""
    if ($catalogByObject.ContainsKey($job.ObjectName)) {
        $catalogName = [string]$catalogByObject[$job.ObjectName]
    }

    $arguments = @(
        "-m", "grandgousier.cli", "process-archive-path",
        "--path", [string]$job.Path,
        "--execute",
        "--no-recursive",
        "--instrument-profile", "auto",
        "--min-frames-per-series", [string]$MinFramesPerSeries,
        "--working-catalog-mag-limit", (Convert-ToInvariantString $WorkingCatalogMagLimit),
        "--max-working-sources-per-frame", [string]$MaxWorkingSourcesPerFrame,
        "--catalog-edge-margin-pix", "20",
        "--exclude-bad-pre-qc-frames",
        "--enable-detection-branch",
        "--exclude-candidates-from-ensemble",
        "--candidate-ensemble-until-converged",
        "--candidate-ensemble-convergence-max-iterations", "10",
        "--candidate-ensemble-growth-policy", "initial_only",
        "--enable-color-calibration",
        "--auto-catalog-margin-arcmin", [string]$AutoCatalogMarginArcmin,
        "--manifest-out", $manifestPath
    )

    if ($catalogName) {
        $arguments += @("--catalog-name", $catalogName)
    }

    if ($targetByObject.ContainsKey($job.ObjectName)) {
        $arguments += @("--target-source-id", [string]$targetByObject[$job.ObjectName])
        if (-not $ExportTargetLightCurves) {
            $arguments += "--no-target-light-curve"
        }
    }
    else {
        $arguments += @("--target-max-sep-arcsec", "-1", "--no-target-light-curve")
    }

    if ($ForceRecomputePhotometry) {
        $arguments += "--force-recompute-photometry"
    }
    if ($DiagnoseScienceMaterialization) {
        $arguments += "--diagnose-science-materialization"
    }
    if ($DiagnoseForcedPhotometry) {
        $arguments += "--diagnose-forced-photometry"
    }
    if ($DiagnoseCalibrationPerformance) {
        $arguments += "--diagnose-calibration-performance"
    }
    if (-not $WithCandidatePlots) {
        $arguments += "--no-candidate-plots"
    }

    $attemptInfo = Invoke-GrandgousierJsonCommand `
        -Executable $PythonExe `
        -Arguments @(
            "-m", "grandgousier.cli", "batch-start-attempt",
            "--batch-run-id", [string]$BatchRunId,
            "--job-id", [string]$job.JobId,
            "--attempt-kind", "EXECUTION",
            "--manifest-path", $manifestPath,
            "--console-log-path", $consoleLogPath,
            "--metadata-json-file", $BatchConfigJson
        ) `
        -WorkingDirectory $ProjectFull
    $attempt = [int]$attemptInfo.attempt_no
    $batchJobAttemptId = [int64]$attemptInfo.batch_job_attempt_id

    # Recovery above is the only place where a previous successful manifest is
    # intentionally consumed. Once a fresh EXECUTION attempt has started, the
    # mutable manifest path must belong to this attempt only. Otherwise an early
    # Python failure can be misreported with status/points/pipeline_run_id from
    # the previous successful run.
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        Remove-Item -LiteralPath $manifestPath -Force
    }

    $catalogLabel = if ($catalogName) { $catalogName } else { "AUTO_CREATE" }
    Write-BatchMessage "RUN $($job.ObjectName) | $($job.RelativePath) | FITS=$($job.FitsCount) | catalog=$catalogLabel | DB attempt=$attempt"


    $exitCode = -1
    $startedAt = Get-Date
    try {
        $exitCode = Invoke-GrandgousierProcess `
            -Executable $PythonExe `
            -Arguments $arguments `
            -WorkingDirectory $ProjectFull `
            -ConsoleLogPath $consoleLogPath
    }
    catch {
        $_ | Out-String | Add-Content -LiteralPath $consoleLogPath -Encoding UTF8
        $exitCode = 9001
    }
    $finishedAt = Get-Date
    $elapsedSec = [math]::Round(($finishedAt - $startedAt).TotalSeconds, 3)

    $manifestSummary = Read-ManifestSummary -ManifestPath $manifestPath -ExitCode $exitCode
    if (
        $manifestSummary.CatalogName -and
        ($CompletedStatuses -contains [string]$manifestSummary.Status) -and
        [int]$manifestSummary.PipelineCount -gt 0
    ) {
        $catalogByObject[$job.ObjectName] = [string]$manifestSummary.CatalogName
    }

    $stateRow = [pscustomobject]@{
        Timestamp = $finishedAt.ToString("o")
        Attempt = $attempt
        JobId = $job.JobId
        ObjectName = $job.ObjectName
        ObservationName = $job.ObservationName
        Path = $job.Path
        RelativePath = $job.RelativePath
        FitsCount = $job.FitsCount
        ExitCode = $exitCode
        Status = $manifestSummary.Status
        Health = $manifestSummary.Health
        ScienceStatus = $manifestSummary.ScienceStatus
        CatalogName = $manifestSummary.CatalogName
        CatalogSources = $manifestSummary.CatalogSources
        PipelineCount = $manifestSummary.PipelineCount
        TotalPoints = $manifestSummary.TotalPoints
        AcceptedPoints = $manifestSummary.AcceptedPoints
        ElapsedSec = $elapsedSec
        ManifestPath = $manifestPath
        ConsoleLogPath = $consoleLogPath
        Message = $manifestSummary.Message
        PipelineRunId = $manifestSummary.PipelineRunId
    }
    $finishAttemptArgs = @(
        "-m", "grandgousier.cli", "batch-finish-attempt",
        "--batch-job-attempt-id", [string]$batchJobAttemptId,
        "--exit-code", [string]$exitCode,
        "--status", [string]$manifestSummary.Status,
        "--health", [string]$manifestSummary.Health,
        "--catalog-sources", [string]$manifestSummary.CatalogSources,
        "--pipeline-count", [string]$manifestSummary.PipelineCount,
        "--total-points", [string]$manifestSummary.TotalPoints,
        "--accepted-points", [string]$manifestSummary.AcceptedPoints,
        "--elapsed-sec", (Convert-ToInvariantString $elapsedSec),
        "--manifest-path", $manifestPath,
        "--console-log-path", $consoleLogPath
    )
    if (-not [string]::IsNullOrWhiteSpace([string]$manifestSummary.ScienceStatus)) {
        $finishAttemptArgs += @("--science-status", [string]$manifestSummary.ScienceStatus)
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$manifestSummary.CatalogName)) {
        $finishAttemptArgs += @("--catalog-name", [string]$manifestSummary.CatalogName)
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$manifestSummary.Message)) {
        $finishAttemptArgs += @("--message", [string]$manifestSummary.Message)
    }
    if ($manifestSummary.PipelineRunId) {
        $finishAttemptArgs += @("--pipeline-run-id", [string]$manifestSummary.PipelineRunId)
    }
    [void](Invoke-GrandgousierJsonCommand -Executable $PythonExe -Arguments $finishAttemptArgs -WorkingDirectory $ProjectFull)
    $latestState[$job.JobId] = $stateRow
    $processedThisRun++

    if ($manifestSummary.Health -eq "OK") {
        Write-BatchMessage "DONE $($job.RelativePath): points=$($manifestSummary.TotalPoints), accepted=$($manifestSummary.AcceptedPoints)" "OK"
    }
    else {
        Write-BatchMessage "RESULT $($job.RelativePath): status=$($manifestSummary.Status), health=$($manifestSummary.Health), exit=$exitCode" "WARN"
    }

    if (-not $catalogByObject.ContainsKey($job.ObjectName)) {
        $reason = "no usable catalog was created; inspect $consoleLogPath"
        $blockedObjects[$job.ObjectName] = $reason
        Write-BatchMessage "Blocking remaining jobs for $($job.ObjectName): $reason" "ERROR"
    }
}

# Rebuild compatibility CSVs from authoritative PostgreSQL state.
$dbBatchSummary = Invoke-GrandgousierJsonCommand `
    -Executable $PythonExe `
    -Arguments @(
        "-m", "grandgousier.cli", "batch-export-state",
        "--batch-run-id", [string]$BatchRunId,
        "--state-csv", $StateCsv,
        "--summary-csv", $SummaryCsv
    ) `
    -WorkingDirectory $ProjectFull

# Build one latest summary row per queued job from the DB-derived CSV mirror.
$latestState = Get-LatestStateByJob -StateCsv $StateCsv
$summaryRows = @()
foreach ($job in $queue) {
    if ($latestState.ContainsKey($job.JobId)) {
        $summaryRows += $latestState[$job.JobId]
    }
    else {
        $summaryRows += [pscustomobject]@{
            Timestamp = ""
            Attempt = 0
            JobId = $job.JobId
            ObjectName = $job.ObjectName
            ObservationName = $job.ObservationName
            Path = $job.Path
            RelativePath = $job.RelativePath
            FitsCount = $job.FitsCount
            ExitCode = ""
            Status = "PENDING"
            Health = "PENDING"
            ScienceStatus = ""
            CatalogName = ""
            CatalogSources = 0
            PipelineCount = 0
            TotalPoints = 0
            AcceptedPoints = 0
            ElapsedSec = 0
            ManifestPath = ""
            ConsoleLogPath = ""
            Message = ""
        }
    }
}
$doneCount = @($summaryRows | Where-Object { $CompletedStatuses -contains [string]$_.Status }).Count
$okCount = @($summaryRows | Where-Object { $_.Health -eq "OK" }).Count
$failedCount = @($summaryRows | Where-Object { $_.Health -like "FAILED*" }).Count
$warningCount = @($summaryRows | Where-Object { $_.Health -like "WARNING*" }).Count
$pendingCount = @($summaryRows | Where-Object { $_.Status -eq "PENDING" }).Count

Write-BatchMessage "Batch summary: completed=$doneCount, healthy=$okCount, warnings=$warningCount, failed=$failedCount, pending=$pendingCount"
Write-BatchMessage "Latest summary: $SummaryCsv"
Write-BatchMessage "Detailed state export: $StateCsv"
Write-BatchMessage "Authoritative batch state: PostgreSQL batch_run_id=$BatchRunId"
