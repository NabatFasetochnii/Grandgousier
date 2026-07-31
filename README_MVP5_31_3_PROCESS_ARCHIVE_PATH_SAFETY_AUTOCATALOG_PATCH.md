# MVP5.31.3 — process-archive-path safety and auto-catalog patch

Patch `0.5.31.3` hardens the one-command archive workflow introduced in `0.5.31.1`.

## Changes

1. `process-archive-path` now restricts field-series construction to frame ids that came from the current `--path`. It no longer processes old unrelated `field_series` from the same `instrument_unit_id`.
2. `build-field-series` accepts an optional `frame_ids` selection.
3. `--catalog-name` is now optional for `process-archive-path`. If omitted, Grandgousier generates an `auto_catalog_*` identity catalog name from FITS group metadata.
4. When an auto catalog is used in `--execute` mode, the command tries to query Gaia DR3 for the indexed frame ids before running the pipeline.
5. Auto Gaia query can be disabled with `--no-auto-catalog-query`.
6. Manifest now includes `indexed_frame_ids`, `created_series_ids`, `processed_series_ids`, `catalog_results`, and per-pipeline `science_status`.
7. A technically successful pipeline with zero light-curve points is reported as `DONE_WITH_WARNINGS` at the `process-archive-path` level, with `science_status=WARNING_NO_MEASUREMENTS`.

## Typical usage

Plan only:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\robophot_field" `
  --manifest-out reports\process_plan.json
```

Execute with auto catalog:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\robophot_field" `
  --execute `
  --manifest-out reports\process_execute.json
```

Execute with an existing catalog:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\robophot_field" `
  --catalog-name gaia_dr3_master `
  --execute
```

## Verification

```powershell
python -c "import grandgousier; print(grandgousier.__version__)"
python -m compileall grandgousier tests
python -m pytest -q
```

Expected version: `0.5.31.3`.

No database migration is required.
