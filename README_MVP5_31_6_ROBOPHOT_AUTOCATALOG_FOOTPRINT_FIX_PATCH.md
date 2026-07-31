# Grandgousier 0.5.31.6 — RoboPhot auto-catalog footprint fallback fix

This patch fixes auto Gaia catalog generation for RoboPhot frames where `process-archive-path` previously reported `failed_frames=86`, `queried_cones=0`, and `inserted_or_updated=0`.

## Changes

- `query_gaia_for_frames()` now eager-loads `Frame.instrument_unit`.
- `_frame_cone_for_frame()` fallback can use `CRVAL1/CRVAL2` and the FITS `CD*` matrix to compute a safe Gaia cone.
- Header CD scale is preferred over profile pixel scale for binned frames.
- Profile pixel scale fallback applies FITS binning when available.
- Added regression coverage for a RoboPhot-like binned FITS header with `CRVAL*` and `CD*`.

## Database migration

No migration is required.
