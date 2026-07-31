# MVP5.31.8 — DB-centered Gaia footprint diagnostics

Patch version: `0.5.31.8`

This patch fixes and instruments Gaia auto-catalog footprint construction used by
`process-archive-path`.

## What changed

- Gaia footprint construction now prefers the indexed DB pointing center:
  `frames.ra_center_deg` / `frames.dec_center_deg`.
- FITS WCS is no longer a hard precondition for building a Gaia cone.
- FITS header `CD*` / `CDELT*` and instrument profile pixel scale are used for
  footprint radius estimation.
- `CRVAL1/CRVAL2` and RoboPhot `ALPHA/DELTA` are only fallback centers.
- Gaia query result now records:
  - `failed_footprint_reasons`
  - `sample_failed_footprints`
- `process-archive-path` manifest and summary now include footprint failure reasons.

## Why

RoboPhot frames can have valid indexed centers in the database while FITS WCS
footprint calculation is fragile. The previous code could report
`queried_frames=0`, `queried_cones=0`, `failed_frames=N` without explaining the
actual failed precondition.

## Verification

```powershell
python -c "import grandgousier; print(grandgousier.__version__)"
python -m compileall grandgousier tests
python -m pytest -q
```

Expected version:

```text
0.5.31.8
```

Re-run the RoboPhot path:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\PycharmProjects\GPX\photometry\obs\robophot\2023-11-05 GPX-TF16E-48\i" `
  --execute `
  --auto-catalog-mag-limit 20.5 `
  --auto-catalog-timeout-sec 120 `
  --auto-catalog-max-radius-deg 1.0 `
  --manifest-out reports\check_05318_process_execute.json
```

If the footprint still fails, inspect:

```text
catalog.failed_footprint_reasons
catalog.sample_failed_footprints
```
