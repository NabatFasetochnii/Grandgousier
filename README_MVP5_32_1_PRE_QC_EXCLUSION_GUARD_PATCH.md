# Grandgousier 0.5.32.1 — pre-QC exclusion guard

Patch over 0.5.32.0. No database migration is required.

## Fix

When `--exclude-bad-pre-qc-frames` rejected all selected frames, the runner converted the empty explicit frame list to `None` for later stages. Low-level stages interpret `None` as "all frames for the instrument unit", so the pipeline accidentally continued on unrelated/current series frames.

0.5.32.1 keeps an empty frame selection explicit and stops the pipeline before catalog matching/photometry when no usable frames remain. The run is recorded as `DONE_NO_SCIENCE_FRAMES` with `science_status=BAD_FRAME_QC`, not as a crash.

## Expected behavior

For a fully icy series with `--exclude-bad-pre-qc-frames`:

```text
check_wcs -> DONE
pre_photometry_frame_qc -> DONE, remaining_frames=0
stop_no_usable_frames -> DONE
process status -> DONE_WITH_WARNINGS
science_status -> BAD_FRAME_QC
```

No catalog-to-frame matching or forced photometry should start after all frames are rejected.
