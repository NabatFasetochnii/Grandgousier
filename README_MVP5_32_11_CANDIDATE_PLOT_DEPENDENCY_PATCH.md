# Grandgousier 0.5.32.11 — candidate plot dependency and plot toggle

Small robustness patch for candidate quick-look exports.

## Changes

- Adds `matplotlib>=3.8` to project dependencies used by candidate PNG quick-look export.
- Adds `--no-candidate-plots` to `field-pipeline` and `process-archive-path`.
- Adds `export_candidate_plots` to field-pipeline/process-archive-path plumbing.
- Candidate export now checks the plot backend once per export run.
- If matplotlib is unavailable, candidate CSV/JSON/index exports still succeed and only one plot warning is emitted instead of one warning per candidate.
- Candidate export result/manifest now includes:
  - `plot_backend`
  - `candidate_plot_warnings_count`

No database migration is required.

## Usage

Default behavior renders PNG plots when matplotlib is installed:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\Users\nikita\Desktop\28.03.2026\CALIBRATED\i" `
  --execute `
  --catalog-name "auto_catalog_robophot_20260402_BK_Lyn_i" `
  --enable-detection-branch
```

Disable only PNG rendering while keeping candidate CSV/JSON/index exports:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "C:\Users\nikita\Desktop\28.03.2026\CALIBRATED\i" `
  --execute `
  --catalog-name "auto_catalog_robophot_20260402_BK_Lyn_i" `
  --enable-detection-branch `
  --no-candidate-plots
```
