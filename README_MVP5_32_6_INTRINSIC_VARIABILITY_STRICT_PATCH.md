# MVP5 0.5.32.6 — strict intrinsic variability candidates

This patch tightens the intrinsic-variability candidate channel introduced in 0.5.32.5.

## Why

The first intrinsic channel correctly recovered BK Lyn without target forcing, but the default thresholds were too permissive: many field objects passed because formal `mag_err` underestimates the full systematic scatter. The channel should find objects that are internally significant and robustly variable, not single-frame outliers or mild systematics.

## Changes

- Makes the tested strict thresholds the defaults:
  - `min_rms_over_mag_err = 4.0`
  - `min_mad_over_mag_err = 2.5`
  - `min_intrinsic_rms = 0.03 mag`
  - `intrinsic_mag_err_floor = 0.01 mag`
- Adds a robust amplitude gate for intrinsic candidates:
  - `robust_peak_to_peak = P90(mag) - P10(mag)`
  - default `min_robust_peak_to_peak = 0.08 mag`
- Exposes robust amplitude diagnostics in object scatter and candidate CSV/JSON:
  - `mag_p10`
  - `mag_p90`
  - `robust_peak_to_peak`
- Adds CLI options:
  - `--min-robust-peak-to-peak`
  - `--field-report-min-robust-peak-to-peak`
- Fixes unresolved target metadata:
  - when `target_resolved=false`, `target_object_id`, `target_catalog_source_id`, and `target_sep_arcsec` are reported as `null`;
  - the nearest unresolved auto-target candidate is reported separately as `nearest_target_candidate_*`.

## Expected BK Lyn behavior

With no explicit target and strict intrinsic thresholds, BK Lyn should still appear as an accepted candidate via:

```text
candidate_origin = intrinsic_variability
is_target = false
```

## Database migration

No database migration is required.
