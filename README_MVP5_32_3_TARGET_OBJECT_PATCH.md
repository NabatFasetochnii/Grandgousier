# Grandgousier 0.5.32.3 — target object handling

This patch makes the field target an explicit pipeline product instead of relying
on the RMS-outlier candidate list.

## Why

BK Lyn is the science target of the processed RoboPhot series.  It has a real
light curve and clean object QC, but it may not exceed the generic field
`rms_excess >= 2` candidate threshold.  A pointed target must therefore be
reported independently and must not be used as an ensemble reference star.

## Changes

- `process-archive-path` adds target controls:
  - `--target-source-id`
  - `--target-object-id`
  - `--target-max-sep-arcsec`
  - `--no-target-light-curve`
- The pipeline resolves a field target after raw/aperture-ZP measurements exist:
  - explicit internal `object_id`, if provided;
  - explicit external/catalog `source_id`, if provided;
  - otherwise, nearest measured catalog object to the mean frame centre.
- The resolved target is excluded from ensemble reference-object selection.
- Ensemble measurement flags now record:
  - `ensemble_object_excluded_from_reference`
  - `ensemble_reference_exclusion_reason`
- Object QC marks the target with `FIELD_TARGET` and records target metadata in
  `object_qc.details.target`.
- Field candidate triage always includes the resolved target as a target-origin
  candidate, even if it is not an RMS-excess outlier.
- Candidate CSV gains:
  - `candidate_origin`
  - `is_target`
  - `target_reason`
- Field QC summary includes target-candidate counts.
- The pipeline exports:
  - `<prefix>_target_light_curve.csv`
  - `<prefix>_target_light_curve.json`
- The manifest and `quality_policy` include the resolved target identifiers and
  target light-curve paths.

## Expected BK Lyn behavior

BK Lyn should appear in the field candidate triage with `candidate_origin=target`
and `is_target=True`, and its target light curve should be exported even if the
object is not selected by the generic RMS-excess threshold.

No database migration is required.
