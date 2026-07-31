# MVP 5.31.4 — safer auto-catalog execution

This patch improves the `process-archive-path` auto-catalog path after a RoboPhot run exposed two practical issues: Gaia/astroquery may stall before the first per-frame query finishes, and a failed auto catalog should not be followed by the full heavy pipeline.

Changes:

- Version bumped to `0.5.31.4`.
- `process-archive-path` auto catalog now defaults to `--auto-catalog-query-mode frame_union`, one cone for the whole input field.
- Added `--auto-catalog-query-mode {frame_union,per_frame}`.
- Added `--auto-catalog-max-radius-deg` to prevent accidentally huge Gaia queries from bad WCS.
- Added `--auto-catalog-timeout-sec` as a best-effort astroquery timeout.
- Added `--allow-empty-catalog` for explicit debugging; without it a failed/empty auto catalog stops before pipeline stages.
- `query-gaia-for-frames` also accepts `--query-mode`, `--max-radius-deg`, and `--gaia-timeout-sec`.
- Manifest/catalog summary includes query mode, queried cones, read rows, and skipped oversized cones.
- Added tests for the catalog precondition and Gaia cone union helper.

No database migration is required.
