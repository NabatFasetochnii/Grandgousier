# Grandgousier 0.5.33.2 — candidate-excluded ensemble until convergence

Patch-only update on top of 0.5.33.1.

## Purpose

Adds a separate candidate-excluded ensemble iteration mode that continues until the accepted candidate set converges, instead of stopping after a fixed number of candidate-excluded passes.

The previous fixed mode remains unchanged:

```powershell
--exclude-candidates-from-ensemble `
--candidate-ensemble-max-iterations 2
```

## New mode

```powershell
--exclude-candidates-from-ensemble `
--candidate-ensemble-until-converged
```

By default this uses a safety cap of 10 total ensemble iterations, counting the initial ensemble as iteration 1. The cap can be changed with:

```powershell
--candidate-ensemble-convergence-max-iterations 20
```

## New manifest fields

`quality_policy` now includes:

```text
candidate_ensemble_iteration_mode
candidate_ensemble_until_converged
candidate_ensemble_convergence_max_iterations
candidate_ensemble_iteration_limit
candidate_ensemble_converged
candidate_ensemble_stop_reason
candidate_ensemble_final_candidate_object_ids
candidate_ensemble_final_candidate_objects
candidate_ensemble_new_final_candidate_object_ids
candidate_ensemble_new_final_candidate_objects
candidate_ensemble_convergence_diagnostics
```

If the final candidate set is fully included in the cumulative exclusion set:

```text
candidate_ensemble_converged = true
candidate_ensemble_stop_reason = converged
```

If the safety cap is reached while new final candidates still appear:

```text
candidate_ensemble_converged = false
candidate_ensemble_stop_reason = max_iterations_reached_with_new_candidates
```

For fixed mode, the analogous non-converged state is:

```text
candidate_ensemble_stop_reason = fixed_iteration_limit_reached_with_new_candidates
```

## Compatibility

No database migration is required.

Existing fixed-iteration commands keep their previous behavior.
