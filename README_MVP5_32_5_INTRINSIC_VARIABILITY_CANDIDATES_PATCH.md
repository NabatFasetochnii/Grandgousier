# MVP5 0.5.32.5 — intrinsic variability candidates

This patch extends candidate triage beyond pure RMS-excess outliers.

## Why

A scientifically interesting object can be clearly variable relative to its own
formal photometric errors, while not being a strong outlier in the field
RMS--magnitude diagram. BK Lyn is the motivating example: it should be surfaced
as a candidate even when it is not explicitly provided as the field target.

## Changes

- Adds an intrinsic-variability candidate channel.
- Keeps the existing RMS-excess channel.
- Keeps forced target candidates from 0.5.32.3/0.5.32.4.
- Candidate origin is now explicit in CSV/JSON via `candidate_origin`.
- New CSV diagnostics:
  - `rms_over_mag_err`
  - `mad_over_mag_err`
  - `effective_mag_err_median`
  - `intrinsic_variability_score`
  - `is_target`
  - `target_reason`
- Field report summary now separates:
  - `rms_excess_candidates`
  - `intrinsic_variability_candidates`
  - `intrinsic_only_candidates`
  - `target_candidates`

## Default intrinsic thresholds

- `min_intrinsic_rms = 0.03` mag
- `min_rms_over_mag_err = 2.0`
- `min_mad_over_mag_err = 1.5`
- `intrinsic_mag_err_floor = 0.01` mag

Use negative values in CLI options to disable individual numeric thresholds.

## Database migration

No database migration is required.
