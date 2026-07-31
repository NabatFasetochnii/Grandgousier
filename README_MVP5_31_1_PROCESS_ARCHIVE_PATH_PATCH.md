# Grandgousier 0.5.31.1 — one-command archive path processing

This patch adds a human-facing orchestration command for normal archive work.
Instead of manually calling `index-archive`, `build-field-series`, `run-field-pipeline`
and `light-curve-summary`, a user can pass a directory with FITS files and let
Grandgousier plan or execute the standard workflow.

## New command

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night" `
  --catalog-name gaia_dr3_master
```

Without `--execute` this is a safe plan-only run. It scans `.fits/.fit/.fts`, reads
headers, auto-detects `MASTER` or `RoboPhot`, and prints the planned file groups.

Real execution:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night" `
  --catalog-name gaia_dr3_master `
  --execute `
  --manifest-out reports\process_archive_manifest.json
```

Execution performs:

```text
scan headers
-> auto-detect instrument profile
-> index frames
-> build field_series
-> run-field-pipeline for new series
-> write per-series light-curve summary JSON
-> optional run manifest JSON
```

## Auto profile detection

The command supports:

```text
--instrument-profile auto
--instrument-profile MASTER
--instrument-profile RoboPhot
```

`auto` uses FITS-header signals and the existing YAML profiles:

```text
configs/master.yaml
configs/robophot.yaml
```

Profile paths can be overridden with:

```text
--master-profile
--robophot-profile
```

## Defaults

The high-level command uses the current standard pipeline defaults:

```text
aperture_radius_pix = 3
annulus_inner_pix = 8
annulus_outer_pix = 12
min_frames_per_series = 10
max_series_gap_minutes = 60
```

These remain overridable for debugging.

## New code

```text
grandgousier/archive/auto_process.py
grandgousier/io/archive_indexer.py: index_archive_paths()
grandgousier/cli.py: process-archive-path
tests/test_process_archive_path.py
```

## Version

```text
0.5.31.0 -> 0.5.31.1
```

No database migration is required.
