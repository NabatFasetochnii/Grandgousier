# Grandgousier 0.5.32.4 — target light-curve frame-scope fix

This patch fixes a regression introduced in 0.5.32.3 target-object handling.

## Problem

`process-archive-path` runs field pipelines with both:

- `series_id`, used for provenance and field-series linkage;
- explicit `frame_ids`, used to keep processing restricted to the FITS files from the requested path.

The target light-curve export passed both selectors to `load_science_light_curve_rows()`. The public light-curve scope resolver correctly rejects this ambiguous combination with:

```text
ValueError: field_series selectors cannot be combined with explicit frame_ids
```

As a result, runs with `--target-source-id` failed after object QC, before writing target products and the final field report.

## Fix

`export_target_light_curve_product()` now prefers the explicit `frame_ids` scope whenever it is provided and does not also pass `series_id` to the light-curve scope resolver.

This preserves the safer path-scoped behavior of `process-archive-path` while still allowing series-level target export when no explicit frame list is supplied.

## Database migration

No database migration is required.
