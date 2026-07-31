---

## MVP5.30.8 — pipeline idempotency audit

Patch `0.5.30.8` adds a read-only idempotency audit for repeated `run-field-pipeline` executions.

New command:

```powershell
python -m grandgousier.cli pipeline-idempotency-report `
  --field-series-name master_test_20230919_s1_v30 `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --out reports\check_05308_idempotency.json
```

The audit checks logical duplicate identities in:

- `measurements`: `object_id, frame_id, calibration_version_id`;
- `object_qc`: `object_id, catalog_name, instrument_unit_id, calibration_version_id, algorithm`;
- `source_detections`: `frame_id, algorithm, rounded x/y position`;
- `detection_catalog_matches`: `frame_id, catalog_source_id, catalog_name, algorithm`;
- `frame_qc`: `frame_id, catalog_name, algorithm`;
- `event_detection_links`: `event_id, source_detection_id` when the event-detection layer exists.

The command returns exit code `0` for OK/WARNING and `2` when real duplicate violations are found. `MISSING` checks are used for optional tables such as `event_detection_links` when the corresponding migration has not been applied.

This patch does not change existing science tables and does not require a DB migration. It is a diagnostic guard before and after repeated pipeline runs.
