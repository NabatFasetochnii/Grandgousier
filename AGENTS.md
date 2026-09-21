# Agent Instructions

## Project Boundaries

- This is a Python 3.11+ package backed by PostgreSQL; the supported development environment is Windows/PowerShell.
- `pyproject.toml` is authoritative for package metadata and dependencies. `Pipfile.lock` is legacy and is not a production lockfile.
- Public API entrypoints are under `grandgousier.api`; the CLI entrypoint is `python -m grandgousier.cli` (also installed as `grandgousier`).
- Read `CONTEXT.md`, `docs/CURRENT_ARCHITECTURE.md`, and `docs/ROADMAP.md` before changing database, pipeline, or scientific behavior. `context/project_state.yaml` is the machine-readable state summary.
- Keep project history in the existing canonical docs; do not add per-patch README, release-note, or handoff Markdown files.

## Setup And Verification

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

- Set `GRANDGOUSIER_DATABASE_URL` explicitly when running database-backed commands; never put credentials in source or reports.
- Fast checks: `\.venv\Scripts\python.exe -m compileall grandgousier tests` and `\.venv\Scripts\python.exe -m pytest`.
- Run one test with `\.venv\Scripts\python.exe -m pytest tests\test_name.py -q` or a focused node with `-k expression`.
- Ruff is configured in `pyproject.toml` with 100-character lines and Python 3.11 target; run `\.venv\Scripts\python.exe -m ruff check grandgousier tests` when touching Python code.
- Release validation requires `python -m pip install -r requirements-release.txt`, then `python tools\release_check.py --constraints <validated constraints.txt> --snapshot <environment_snapshot.json>`; it builds wheel/sdist and writes `dist/` artifacts.
- Optional `[photometry,gaia]` dependencies are needed for full science/acceptance checks; missing optional dependencies can make `doctor --strict-optional` fail.

## Database And Science Safety

- Grandgousier 0.7 is a fresh-baseline schema epoch. Use `init-db` only for a new empty clean 0.7 database; do not replay legacy migrations or migrate the frozen 0.6 production/reference database in place.
- Before database work, inspect actual model/schema columns and constraints; never guess SQL names. In particular, the calibration version name is `name`, and alias targets use `head_calibration_version_id`.
- The frozen production/reference database is read-only for this work. Do not drop, truncate, reset, delete, or perform forensic cleanup without explicit user approval plus a read-only preflight/count/acceptance gate.
- Normal 0.7 storage keeps immutable BASE extraction rows in `measurements` and final narrow science state in `science_photometry`; derived full measurement copies are not normal output.
- Do not change frozen Stage-2/Stage-3 formulas or selection semantics in storage/refactor work. Compare rebuilds by logical identities and science acceptance, not surrogate numeric IDs.
- Physical observation identity is authoritative FITS metadata: MASTER `UNIQNUM`; RoboPhot `SERNUM + DATE-OBS` where `DATE-OBS` is already UTC. Paths and URLs are mutable locators, not identity.
- Object identity is opaque and catalog-independent. Same-catalog sources must not be auto-merged merely by close astrometry; ambiguous associations create a new object.
- Detection storage is run-normalized through `detection_runs` and `detection_run_frames`; do not reintroduce repeated run configuration into high-cardinality rows. `frame_catalog_links` remains unchanged until a real PostgreSQL EXPLAIN/bytes benchmark.

## CLI And Workflow Gotchas

- `process-archive-path` is plan-only unless `--execute` is supplied and only queues series represented by frames from the supplied path; it does not implicitly replay historical series.
- `process-field-archive` defaults to resumable orchestration. For an incomplete full rebuild, use the explicit `--resume` path after plan-only confirmation; do not rerun Stage 2 blindly.
- Fresh database validation order is `init-db`, `schema-status`, then `doctor --strict-optional`; archive-wide rebuild follows only after clean smoke, acceptance, and idempotency checks.
- Avoid nullable untyped PostgreSQL binds in `IS NULL OR` selectors; use separate scoped/global statements. Use `CAST(:value AS bigint)` rather than `:value::bigint`, and qualify columns in joins.
- Exact row counts are required for baselines; PostgreSQL planner estimates such as `n_live_tup` may be stale. `rg` is not assumed available on Windows; use repository search tools or PowerShell alternatives.

## Canonical References

- Current factual state: `CONTEXT.md`
- Architecture and storage contract: `docs/CURRENT_ARCHITECTURE.md`
- Ordered engineering gates: `docs/ROADMAP.md`
- Historical record: `docs/PROJECT_HISTORY.md`
