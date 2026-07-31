# Grandgousier 0.5.31.0 — science light-curve product foundation

This patch starts the main science-product layer for the archive: light curves assembled from calibrated database measurements with explicit field-series provenance, calibration provenance, QC flags and a future inter-series offset slot.

## Files changed

- `pyproject.toml`
- `grandgousier/__init__.py`
- `grandgousier/cli.py`
- `grandgousier/db/models.py`
- `grandgousier/lightcurves/__init__.py`
- `grandgousier/lightcurves/product.py`
- `scripts/migrate_all.sql`
- `scripts/migrate_mvp5_31_0_lightcurve_product.sql`
- `tests/test_lightcurve_product.py`
- `README.md`

## New DB layer

The migration adds:

- `interseries_calibration_offsets`

This table stores one future offset per calibration/series/algorithm/reference. The current patch does not compute offsets yet. If no row exists for a light-curve point, `mag_global = mag_cal`, `interseries_offset_mag = null`, and `interseries_offset_status = NO_INTERSERIES_OFFSET`.

Apply migration:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_31_0_lightcurve_product.sql
```

## New commands

List objects with light-curve coverage:

```powershell
python -m grandgousier.cli list-light-curve-objects `
  --catalog-name gaia_dr3_master `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --min-points 10 `
  --format table
```

Summarize the science light-curve layer:

```powershell
python -m grandgousier.cli light-curve-summary `
  --catalog-name gaia_dr3_master `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --out reports\check_05310_light_curve_summary.json
```

Export one science light curve:

```powershell
python -m grandgousier.cli export-light-curve `
  --catalog-name gaia_dr3_master `
  --source-id 2304906332682275456 `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --out exports\light_curves\gaia_2304906332682275456_v31.csv
```

`--calibration-name` and `--calibration-prefix` may be repeated, so the same commands can later assemble multi-series curves from several calibrated series.

## QC behavior

The export includes both raw QC JSON fields and explicit science decision fields:

- `quality_flags`
- `is_rejected`
- `reject_reasons`

Rejected rows are included by default. Use `--exclude-rejected` to write only accepted science points.

## Checks

```powershell
python -m compileall grandgousier tests
python -m pytest -q
```

Expected in this patch environment:

```text
28 passed, 2 skipped
```
