# MVP5 0.5.32.7 — low-amplitude intrinsic variability candidates

This patch adds a separate candidate-triage channel for weak but coherent variables.

## Motivation

The strict intrinsic-variability channel introduced in 0.5.32.6 is useful for high-S/N variables, but it can miss lower-amplitude objects such as BK Lyn on nights where the field-level RMS envelope is high and `mag_rms / mag_err` remains below the strong intrinsic threshold.

## Changes

- Added candidate origin `low_amplitude_intrinsic`.
- Kept the strong intrinsic channel unchanged:
  - `min_intrinsic_rms = 0.03`
  - `min_rms_over_mag_err = 4.0`
  - `min_mad_over_mag_err = 2.5`
  - `min_robust_peak_to_peak = 0.08`
- Added default low-amplitude intrinsic thresholds:
  - `low_amplitude_min_measurements = 30`
  - `low_amplitude_min_intrinsic_rms = 0.03`
  - `low_amplitude_min_rms_over_mag_err = 2.3`
  - `low_amplitude_min_mad_over_mag_err = 2.0`
  - `low_amplitude_min_robust_peak_to_peak = 0.08`
- Strong intrinsic candidates are not duplicated as low-amplitude candidates.
- Field QC summaries now include:
  - `low_amplitude_intrinsic_candidates`
  - `low_amplitude_only_candidates`
- Candidate origins can now be:
  - `rms_excess`
  - `intrinsic_variability`
  - `low_amplitude_intrinsic`
  - `target`
  - comma-separated combinations of these origins.
- Added CLI controls for candidate triage, field QC reports, field pipelines, and `process-archive-path`.

## Notes

The usual object-QC rejection filters still apply after candidate generation. In detection-branch mode, low-amplitude intrinsic candidates are still rejected if detection support is weak and `require_detection_support` is effective.

No database migration is required.
