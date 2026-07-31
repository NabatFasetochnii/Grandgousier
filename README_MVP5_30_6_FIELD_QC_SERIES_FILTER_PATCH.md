# MVP5.30.6 — field-qc-report series filter patch

Patch goal: make `field-qc-report` able to build a report for one persistent `field_series` instead of silently summarizing every frame compatible with the selected calibration / instrument unit.

## Changes

- Version bumped to `0.5.30.6`.
- Added `field-qc-report --series-id`.
- Added `field-qc-report --field-series-name`.
- `field_series_name` can resolve a series by:
  - `field_series.series_uid`;
  - `fields.field_name`;
  - `field_series.metadata_json.series_name`;
  - `field_series.metadata_json.primary_object_name`;
  - `field_series.metadata_json.calibration_prefix`;
  - latest `pipeline_runs.calibration_prefix` for the series.
- The report metadata now records:
  - `frame_selection_mode`;
  - `selected_frame_ids_count`;
  - `field_series` metadata.
- Summary table now includes a `selection` section.
- Added regression tests for field-series frame selection and selector/frame-id conflict handling.

## Example

```powershell
python -m grandgousier.cli field-qc-report `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --field-series-name master_test_20230919_s1_v30 `
  --out reports\master_test_20230919_s1_v30_ps1_i_color_v1\field_qc_report.json
```

Use `--series-id` when names are ambiguous.
