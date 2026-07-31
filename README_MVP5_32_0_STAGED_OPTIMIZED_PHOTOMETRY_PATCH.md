# MVP5.32.0 — staged optimized photometry, pre-QC and working catalog

This patch addresses the RoboPhot large-catalog execution problem observed after
`0.5.31.9`: the Gaia footprint and batched inserts worked, but the path pipeline
still ran several full forced-photometry passes and used an overly deep working
catalog.

## What changed

1. **Pre-photometry frame QC**

   New module: `grandgousier.quality.frame_pre_qc`.

   The pipeline now reads each FITS frame before forced photometry and stores a
   compact diagnostic block in `frames.quality_flags["frame_pre_photometry_qc_v1"]`:

   - `n_local_maxima_before_cap`
   - `DETECTION_CAP_REACHED`
   - background median/MAD sigma and percentiles
   - bright-pixel fraction
   - BAD/WARNING/OK severity

   This resolves the ambiguity where `source_detections=2000` only meant that the
   detector save cap was reached, not that exactly 2000 real sources existed.

2. **Working catalog limits**

   `process-archive-path` now separates the deep Gaia identity catalog from the
   practical forced-photometry working set.  Defaults:

   ```text
   --working-catalog-mag-limit 19.0
   --max-working-sources-per-frame 2000
   --catalog-edge-margin-pix 20
   ```

   Existing `frame_catalog_links` for the same frame/catalog are replaced before
   inserting the new working links.

3. **Calibration-name isolation**

   Calibration versions include a working-set tag such as
   `_work_g19_max2000_edge20_`.  This prevents a rerun with a smaller working set
   from accidentally reusing older `G<20.5` raw measurement layers.

4. **Raw photometry reuse**

   `run_forced_aperture_photometry` can skip frames whose target raw calibration
   already has enough measurements.  Use `--force-recompute-photometry` to force a
   replacement computation.

5. **Expensive branches disabled by default in one-command processing**

   In `process-archive-path`, the detection branch and aperture-stability sweep
   are now opt-in:

   ```text
   --enable-detection-branch
   --enable-aperture-stability-qc
   ```

6. **Stage timing in manifests**

   Pipeline steps now include `elapsed_sec` and are copied into the
   `process-archive-path` manifest.

## Database migration

No migration is required.  The patch uses existing tables and JSONB fields.

## Suggested RoboPhot command

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\PycharmProjects\GPX\photometry\obs\robophot\2023-11-05 GPX-TF16E-48\i" `
  --execute `
  --auto-catalog-mag-limit 20.5 `
  --working-catalog-mag-limit 19.0 `
  --max-working-sources-per-frame 2000 `
  --catalog-edge-margin-pix 20 `
  --auto-catalog-timeout-sec 120 `
  --auto-catalog-max-radius-deg 1.0 `
  --manifest-out reports\check_05320_process_execute.json
```
