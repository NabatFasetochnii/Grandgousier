#requires -Version 5.1
<#
Compatibility wrapper. Batch orchestration moved to PostgreSQL in v1.5.
Existing v1.4 commands are forwarded with identical parameters.
#>
[CmdletBinding()]
param(
    [Parameter()][string]$Root = "C:\Users\nikita\Desktop\tess",
    [Parameter()][string]$ProjectRoot = "C:\PycharmProjects\Grandgousier",
    [Parameter()][string]$PythonExe = "python",
    [Parameter()][string]$BatchName = "tess_all_observations",
    [Parameter()][string]$DatabaseUrl,
    [Parameter()][switch]$Execute,
    [Parameter()][switch]$IncludeAmbiguous,
    [Parameter()][switch]$RetryFailed,
    [Parameter()][switch]$RetryCompleted,
    [Parameter()][switch]$ForceRecomputePhotometry,
    [Parameter()][switch]$DiagnoseScienceMaterialization,
    [Parameter()][switch]$DiagnoseForcedPhotometry,
    [Parameter()][switch]$DiagnoseCalibrationPerformance,
    [Parameter()][switch]$WithCandidatePlots,
    [Parameter()][string]$TargetMapCsv,
    [Parameter()][switch]$ExportTargetLightCurves,
    [Parameter()][ValidateRange(1, 1000000)][int]$MinFramesPerSeries = 10,
    [Parameter()][ValidateRange(0, 120)][int]$AutoCatalogMarginArcmin = 10,
    [Parameter()][ValidateRange(1, 1000000)][double]$WorkingCatalogMagLimit = 19.0,
    [Parameter()][ValidateRange(0, 1000000)][int]$MaxWorkingSourcesPerFrame = 2000,
    [Parameter()][ValidateRange(0, 1000000)][int]$StopAfter = 0
)

$target = Join-Path $PSScriptRoot "process_all_tess_observations_v1_5.ps1"
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Batch script v1.5 not found: $target"
}
& $target @PSBoundParameters
