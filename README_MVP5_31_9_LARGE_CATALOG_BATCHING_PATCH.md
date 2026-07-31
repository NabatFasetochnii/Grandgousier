# MVP5.31.9 — large-catalog batching and faster detection matching

Patch version: `0.5.31.9`

This patch makes the field pipeline safe for large Gaia auto-catalogs, including
RoboPhot fields where `process-archive-path` can import ten thousand or more
catalog sources.

## What changed

- Added shared PostgreSQL-safe bulk chunking helpers:
  - `grandgousier.db.bulk`
  - `grandgousier.db.measurement_io`
- Batched multi-row INSERT/UPSERT writes for:
  - forced-aperture `measurements`
  - aperture-ZP derived `measurements`
  - ensemble derived `measurements`
  - preliminary frame-QC calibrated `measurements`
  - `frame_catalog_links`
  - `source_detections`
  - `detection_catalog_matches`
  - `object_qc`
- Default batch size is 1000 rows and is also capped by the PostgreSQL bind
  parameter limit with safety headroom.
- Added `insert_batches` / `insert_batch_size` result diagnostics where relevant.
- Added `--bulk-insert-batch-size` to:
  - `process-archive-path`
  - `run-field-pipeline`
  - `run-forced-photometry`
  - calibration commands that copy measurement rows.
- Optimized detection-to-catalog matching with a conservative pixel-grid
  prefilter.  Final acceptance is still done by sky-coordinate separation.

## Why

The RoboPhot test run successfully created an auto Gaia catalog with 13,159
sources, but then failed during forced photometry because one huge multi-row
`INSERT INTO measurements` exceeded PostgreSQL/psycopg's bind-parameter limit:

```text
number of parameters must be between 0 and 65535
```

A single measurement row uses many bound parameters, so large catalogs must be
written in chunks.  The same issue can appear later in derived calibration and
QC tables, so this patch applies batching to the full field-pipeline write path
rather than only the immediate failing statement.

## Verification

```powershell
python -c "import grandgousier; print(grandgousier.__version__)"
python -m compileall grandgousier tests
python -m pytest -q
```

Expected version:

```text
0.5.31.9
```

Re-run the same RoboPhot command:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\PycharmProjects\GPX\photometry\obs\robophot\2023-11-05 GPX-TF16E-48\i" `
  --execute `
  --auto-catalog-mag-limit 20.5 `
  --auto-catalog-timeout-sec 120 `
  --auto-catalog-max-radius-deg 1.0 `
  --manifest-out reports\check_05319_process_execute.json
```

Expected change relative to `0.5.31.8`: no failure with PostgreSQL bind-parameter
limit during `Forced aperture photometry`.
