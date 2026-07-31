# MVP5.30.9 — pipeline provenance import fix

Patch version: `0.5.30.9`

## Problem

`run-field-pipeline` failed during calibration creation with:

```text
NameError: name 'set_source_calibration' is not defined
```

The function was used in aperture/preliminary/ensemble calibration modules after the derived-calibration provenance patch, but the modules did not import it. Unit tests imported the modules but did not exercise the affected global symbol.

## Changes

- Added `set_source_calibration` imports to:
  - `grandgousier/calibration/aperture_zp.py`
  - `grandgousier/calibration/ensemble.py`
  - `grandgousier/calibration/preliminary.py`
- Added a regression test that checks these modules expose callable `set_source_calibration`.
- Bumped version to `0.5.30.9`.

## Database migration

No migration is required.

## Verification

```powershell
python -m compileall grandgousier tests
python -m pytest -q
```

Expected: all tests pass.

Then rerun the previously failing command:

```powershell
python -m grandgousier.cli run-field-pipeline `
  --series-id 1 `
  --catalog-name gaia_dr3_master `
  --calibration-prefix master_test_20230919_s1_v31 `
  --aperture-radius-pix 3 `
  --annulus-inner-pix 8 `
  --annulus-outer-pix 12 `
  --out-dir exports\master_test_20230919_s1_v31 `
  --log-dir logs `
  --file-log-level INFO
```

It must not fail with `NameError: set_source_calibration`.
