# Apply Grandgousier 0.5.33.3 patch

Base version: `0.5.33.2`.

No database migration is required.

## Option A — unified diff

From the project root:

```powershell
git apply Grandgousier_0.5.33.3.patch
```

## Option B — patch-only overlay

Extract `Grandgousier_0.5.33.3_patch.zip` over the existing 0.5.33.2 project, preserving relative paths and replacing files when prompted.

The patch includes the provenance fix, regression test, version update and the new documentation/context layout.

## Checks

```powershell
python -m compileall grandgousier tests scripts
python -m pytest -q
```

## Runtime verification

Use the command from `docs/context/COMMANDS.md`, then inspect:

```text
quality_policy.candidate_ensemble_actual_exclusions_iteration
quality_policy.candidate_ensemble_actual_excluded_reference_object_ids
quality_policy.candidate_ensemble_actual_excluded_reference_objects
```

For the previously observed BK Lyn convergence history, the expected summary is iteration 6 and 12 actual reference exclusions, not the empty terminal iteration 7 entry.
