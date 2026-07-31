# MVP5.30.7 — smarter field-series resolver for field-qc-report

Patch goal: make `field-qc-report --field-series-name` robust when the stored `field_series` row was created under an older pipeline prefix, while the user now passes a newer derived calibration prefix.

## Problem fixed

After MVP5.30.6, `field-qc-report --series-id 1` correctly restricted the report to the 18 frames of the persistent series. But this command failed:

```powershell
python -m grandgousier.cli field-qc-report `
  --field-series-name master_test_20230919_s1_v30 `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --out reports\check_05306_series_filtered.json
```

because the database stored the actual series as:

```text
series_id = 1
field_name = TIC420268415_01
last_calibration_prefix = master_test_20230919_s1_v27
```

The user-facing prefix `master_test_20230919_s1_v30` existed in the derived calibration name, but not in the `field_series` row.

## Changes

- Version bumped to `0.5.30.7`.
- `field-qc-report --field-series-name` now accepts calibration-related names more reliably.
- Resolver now builds aliases from the target calibration and its source-calibration chain, for example:
  - `master_test_20230919_s1_v30_ps1_i_color_v1`;
  - `master_test_20230919_s1_v30`;
  - `master_test_20230919_s1_v27_ensemble_g_v1`;
  - `master_test_20230919_s1_v27`.
- If no literal series match is found, the resolver can fall back to frame overlap between `measurements.calibration_version_id` and `field_series_frames`.
- The overlap fallback is only enabled when the supplied name is plausibly related to the target/source calibration chain, so a random typo does not silently select an unrelated series.
- Missing-series errors now include a compact list of available `field_series` values with `series_id`, `series_uid`, `field_name`, `n_frames`, `last_calibration_prefix`, and `primary_object_name` when available.
- Added regression tests for:
  - resolving a new derived prefix through calibration-frame overlap;
  - resolving a new derived prefix through an older source pipeline prefix;
  - showing available series instead of a bare `ValueError`.

## Expected command

```powershell
python -m grandgousier.cli field-qc-report `
  --field-series-name master_test_20230919_s1_v30 `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --out reports\check_05307_series_filtered_by_name.json
```

Expected report metadata:

```text
frame_selection_mode = field_series
series_id = 1
selected_frame_ids_count = 18
n_frames = 18
```

`APERTURE_UNSTABLE` and `APERTURE_MEDIAN_SHIFT` should remain visible in `object_qc.flag_counts`.

## Checks

```powershell
python -m compileall grandgousier tests
python -m pytest -q
```

Expected in the patch build:

```text
19 passed, 2 skipped
```
