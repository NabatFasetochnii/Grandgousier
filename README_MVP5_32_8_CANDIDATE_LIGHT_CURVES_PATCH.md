# MVP5 0.5.32.8 — accepted candidate light-curve exports

Adds automatic per-candidate science-light-curve exports after field-QC candidate triage.

## Main changes

- New module `grandgousier.lightcurves.candidates`.
- Field pipeline now exports one CSV and one JSON summary per ACCEPTED candidate by default.
- Candidate products are written under `<series_out_dir>/candidates/`.
- A compact `candidates_summary.csv` and `candidates_summary.json` are produced for quick review.
- Candidate rows get a deterministic `candidate_rank_score` and rank ordering:
  target, mixed RMS/intrinsic, RMS-only, strong intrinsic, low-amplitude intrinsic.
- Pipeline outputs and process manifests now include candidate export paths and counts.
- New CLI flags:
  - `--no-candidate-light-curves`
  - `--candidate-light-curve-limit`

No database migration is required.
