# Reproducible release workflow

`pyproject.toml` is the authoritative declaration of supported dependencies. `Pipfile.lock` is a
legacy file and is **not** a production lock for current Grandgousier releases.

A production dependency baseline is captured on a real validated deployment with:

```powershell
python tools\snapshot_environment.py `
  --out-dir "release\validated_windows_py311"
```

By default this captures the core dependency closure. Add `--extra photometry` and/or `--extra gaia` only when those optional groups are actually installed and validated on the target machine.

The command writes:

- `environment_snapshot.json` — Python/platform/package provenance;
- `constraints.txt` — exact versions in the installed Grandgousier dependency closure.

The snapshot is read-only and does not modify the Python environment.

Release artifacts are checked with:

```powershell
python -m pip install -r requirements-release.txt
python tools\release_check.py `
  --constraints "release\validated_windows_py311\constraints.txt" `
  --snapshot "release\validated_windows_py311\environment_snapshot.json"
```

The checker:

1. verifies `pyproject.toml` and `grandgousier.__version__` agree;
2. verifies the validated constraints contain all core dependencies;
3. builds wheel + sdist with `the configured `setuptools.build_meta` PEP-517 backend without build isolation`;
4. checks wheel resources and excludes private/internal paths;
5. installs the wheel into a temporary target without dependencies and imports it from there;
   starting with 0.5.58.1 this smoke uses `--ignore-installed` plus `python -I -S` and verifies the actual import/resource origin is inside that target;
6. writes `dist/SHA256SUMS.txt` and `dist/release_check.json`.

For a clean-machine acceptance test, create a new Python 3.11 venv and install using the validated
constraints file:

```powershell
py -3.11 -m venv .venv-clean
.\.venv-clean\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install `
  --constraint "release\validated_windows_py311\constraints.txt" `
  ".[photometry,gaia]"
python -m grandgousier.cli doctor --config grandgousier.yaml
```

If external package download is unavailable, create/use a wheelhouse and replace the installation
step with `--no-index --find-links <wheelhouse>`. Do not regenerate the validated constraints from a
different OS/Python and call it equivalent to the Windows production baseline.


## Full-science acceptance baseline

For the complete photometry/Gaia environment, install the validated wheel in a clean Python 3.11 venv with `[photometry,gaia]`, require `grandgousier doctor --strict-optional` to be `READY`, then capture a separate snapshot:

```powershell
python tools\snapshot_environment.py `
  --out-dir "release\validated_windows_py311_full_science" `
  --extra photometry `
  --extra gaia
```

The dependency snapshot proves the environment; the scientific result is checked separately and read-only:

```powershell
grandgousier acceptance-check `
  --dataset gpx_tf17_2 `
  --config grandgousier.yaml `
  --report-out reports\acceptance_gpx_tf17_2.json
```

Do not call a full-science constraints snapshot validated until both the clean-venv doctor/import gate and the frozen scientific acceptance pass.
