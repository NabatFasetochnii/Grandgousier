# MVP5.31.5 — auto-catalog footprint fallback

This patch improves `process-archive-path` auto-catalog bootstrapping for frames
that have indexed pointing coordinates but no complete celestial WCS in the raw
FITS header.

Changes:

- Gaia auto-catalog cone generation now prefers the full FITS WCS when present.
- If no celestial WCS is available, it falls back to `frames.ra_center_deg`,
  `frames.dec_center_deg` and `instrument_units.pixel_scale_arcsec` plus FITS
  image dimensions.
- Catalog summary output now exposes `failed_frames`, `skipped_missing_file`,
  and `skipped_oversized_cones`.
- Added regression coverage for RoboPhot-like FITS files without celestial WCS.

No database migration is required.
