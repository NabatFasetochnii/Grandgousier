# Grandgousier 0.5.32.9 — candidate export QC

This patch tightens accepted-candidate light-curve export semantics.

## Changes

- Candidate light-curve summary rows now include:
  - `candidate_export_status`
  - `accepted_fraction`
- A candidate that passed field candidate triage but has no usable light-curve points is no longer counted as a successful accepted export.
- Such candidates are marked as:
  - `REJECTED_NO_ACCEPTED_POINTS`, if all light-curve points are rejected
  - `REJECTED_NO_POINTS`, if no points are available
  - `EXPORT_FAILED`, if per-object export failed
- Diagnostic CSV/JSON products are still written when possible, so rejected exports can be inspected.
- Manifest export counters now distinguish:
  - `triage_accepted_candidates`
  - `accepted_candidates`
  - `exported_light_curves`
  - `diagnostic_light_curves`
  - `rejected_no_accepted_points`
  - `rejected_no_points`

## Database

No migration is required.
