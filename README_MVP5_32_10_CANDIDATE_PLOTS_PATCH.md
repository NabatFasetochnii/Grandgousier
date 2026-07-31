# Grandgousier 0.5.32.10 — candidate quick-look plots and HTML index

This patch adds quick-look products for accepted candidate light-curve exports.

## Changes

- Per-candidate PNG plots are written for candidates whose exported light curves contain at least one accepted point.
- A standalone `candidates/index.html` quick-look page is written next to `candidates_summary.csv/json`.
- `candidates_summary.csv/json` now includes `light_curve_png`.
- `candidates_summary.json` is self-contained: its `summary` block includes the actual paths to `candidates_summary_csv`, `candidates_summary_json`, and `candidates_index_html`.
- Pipeline results expose:
  - `accepted_candidates_index_html`
  - `candidate_plot_count`
- Candidate diagnostics with no accepted points remain exported as CSV/JSON, but no PNG quick-look is generated for them.

## Database migration

No database migration is required.
