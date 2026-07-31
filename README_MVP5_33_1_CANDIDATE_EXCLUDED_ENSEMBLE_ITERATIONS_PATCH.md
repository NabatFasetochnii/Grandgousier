# Grandgousier 0.5.33.1 — candidate-excluded ensemble provenance and iterations

This patch stabilizes the candidate-excluded ensemble mode introduced in 0.5.33.0.

## Changes

- Adds `--candidate-ensemble-max-iterations` to:
  - `field-pipeline`
  - `process-archive-path`
- Default is `2`, which preserves the 0.5.33.0 behavior: initial ensemble pass plus one candidate-excluded pass.
- Values above 2 allow iterative candidate-excluded ensemble calibration:
  - pass 1: normal ensemble and candidate triage;
  - pass 2: excludes accepted candidates from pass 1;
  - pass 3+: excludes the cumulative accepted candidates from previous passes until the limit is reached or no new exclusions appear.
- Adds explicit requested-vs-actual exclusion provenance:
  - requested excluded object IDs: all accepted candidates and target objects requested for reference exclusion;
  - actual excluded reference object IDs: the subset that would otherwise have been used as ensemble reference objects.
- Keeps 0.5.33.0 manifest aliases for compatibility, but adds unambiguous fields.

## New manifest / quality policy fields

```text
candidate_ensemble_max_iterations
candidate_ensemble_exclusion_iterations_executed
candidate_ensemble_requested_excluded_object_ids
candidate_ensemble_requested_excluded_objects
candidate_ensemble_actual_excluded_reference_object_ids
candidate_ensemble_actual_excluded_reference_objects
candidate_ensemble_exclusion_history
```

## New ensemble result fields

```text
requested_excluded_reference_object_ids
requested_excluded_reference_objects
actually_excluded_reference_object_ids
actually_excluded_reference_objects
```

The legacy `excluded_reference_object_ids` / `excluded_reference_objects` fields now refer to actual reference exclusions. The requested set is available in the new `requested_*` fields.

## Database migration

No database migration is required.
