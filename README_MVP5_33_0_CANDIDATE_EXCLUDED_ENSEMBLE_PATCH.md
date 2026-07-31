# Grandgousier 0.5.33.0 — candidate-excluded ensemble second pass

Adds an opt-in two-pass ensemble calibration mode for field pipelines.

## New CLI flag

```text
--exclude-candidates-from-ensemble
```

The flag is available for:

- `field-pipeline`
- `process-archive-path`

## Behaviour

1. Run the normal aperture-ZP and initial ensemble calibration.
2. Compute object QC and the first field candidate triage.
3. Collect accepted candidate `object_id` values, plus any explicitly resolved target object.
4. Run a second ensemble calibration from the same aperture-ZP input, excluding those objects from the reference set.
5. Recompute object QC, field candidate triage, target light curve and candidate exports against the second-pass ensemble calibration.

The first-pass ensemble remains in the database. The final pipeline calibration name points to the second-pass calibration when the second pass is executed.

## Manifest additions

`quality_policy` now includes:

```text
candidate_ensemble_exclusion_enabled
candidate_ensemble_exclusion_executed
candidate_ensemble_excluded_object_ids
candidate_ensemble_excluded_objects
initial_ensemble_calibration
final_ensemble_calibration
```

Pipeline outputs include:

```text
ensemble_initial_calibration
ensemble_candidate_excluded_calibration
```

No DB migration is required.
