# Grandgousier 0.5.32.2 — quality flag policy patch

Patch over 0.5.32.1.

## Purpose

This patch separates diagnostic detection-support flags from hard science-light-curve rejection policy.

`NO_DETECTION_SUPPORT` and `WEAK_DETECTION_SUPPORT` remain object-level QC diagnostics. They are used as hard rejection flags only when detection-to-catalog matching is enabled, or when the user explicitly requests it.

## Changes

- `process-archive-path` now accepts:
  - `--require-detection-support`
  - `--no-require-detection-support`
- Default policy:
  - with `--enable-detection-branch`: `require_detection_support_effective=true`
  - without detection branch: `require_detection_support_effective=false`
- Pipeline manifests now include `quality_policy` with:
  - `detection_branch_enabled`
  - `detection_matching_executed`
  - `detection_matches_count`
  - `require_detection_support_requested`
  - `require_detection_support_effective`
- Field QC report metadata/summary now records the effective detection-support policy.
- Science light-curve summaries can exclude detection-support flags from reject flags while keeping other QC rejections active.
- If detection branch is enabled and detection matching runs but produces zero matches, the science status becomes `WARNING_NO_DETECTION_MATCHES`.

## Expected behavior

- Forced-photometry-only runs no longer reject every point merely because detection support was not computed.
- Runs with `--enable-detection-branch` keep the strict detection-support veto by default.
- Explicit CLI flags can override the default policy.

## Validation

Run:

```powershell
python -m compileall grandgousier tests
python -m pytest -q
```

Recommended control checks:

```powershell
# Strict detection-supported run remains OK
python -m grandgousier.cli process-archive-path `
  --path "C:\Users\nikita\Desktop\28.03.2026\CALIBRATED\i" `
  --execute `
  --catalog-name "auto_catalog_robophot_20260402_BK_Lyn_i" `
  --working-catalog-mag-limit 19.0 `
  --max-working-sources-per-frame 2000 `
  --catalog-edge-margin-pix 20 `
  --exclude-bad-pre-qc-frames `
  --enable-detection-branch `
  --manifest-out reports\check_05322_bklyn_20260328_i_existing_catalog_detection.json

# Forced-photometry-only run should no longer collapse to accepted_points=0 solely due to detection-support flags
python -m grandgousier.cli process-archive-path `
  --path "C:\Users\nikita\Desktop\02.04.2026\CALIBRATED\i" `
  --execute `
  --catalog-name "auto_catalog_robophot_20260402_BK_Lyn_i" `
  --working-catalog-mag-limit 19.0 `
  --max-working-sources-per-frame 2000 `
  --catalog-edge-margin-pix 20 `
  --exclude-bad-pre-qc-frames `
  --manifest-out reports\check_05322_bklyn_20260402_i_existing_catalog_forced_only.json
```
