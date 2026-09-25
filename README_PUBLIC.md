# Grandgousier

For new Stage-2 runs, `adaptive_ellipse` is the default per-star forced-flux estimator in
`run-field-pipeline`, `process-archive-path`, `run-forced-photometry`, and the Stage-1 handoff
to archive processing. Use `--photometry-estimator circular` to request the historical
circular estimator explicitly. The adaptive equal-area radius has a 3-pixel base floor
and a 2.5× base-radius cap; unavailable/invalid local PSF geometry uses the existing
flagged circular fallback. The effective estimator is stored in Stage-2 recipe lineage.
This source candidate is **IMPLEMENTED_NOT_REAL_VALIDATED**; this default alone does not
make a generic run `stage3_scale_v1` compatible or establish photometric acceptance.

Grandgousier is a Python/PostgreSQL pipeline for calibrated astronomical time-series photometry.

The public distribution contains the scientific photometry/calibration pipeline, instrument profiles,
managed database migrations, the stable Python API, the resumable field-archive service layer, and path-independent physical-observation identity support.
Site-specific archive transports, credentials, private endpoints, and observatory-internal storage
layouts are intentionally outside the public distribution. Input FITS or prepared local manifests must
be supplied by the deployment environment.

## Install

```bash
python -m pip install grandgousier
```

For a source checkout:

```bash
python -m pip install -e .
```

## Stable Python API

```python
from grandgousier.api import (
    compute_aperture_error_budget,
    measure_circular_aperture,
    InterseriesObjectState,
    compute_interseries_pair_offset,
    solve_interseries_offsets,
)
```

## Service layer

`grandgousier.services` exposes typed field-archive job configuration, deployment readiness checks,
and resumable execution over the same validated orchestration used by the CLI. It does not embed
site credentials or private archive discovery logic.


## Stage 1 archive-source contract (0.6.0 foundation)

0.6.0 adds a source-neutral, metadata-first boundary for archive ingestion. `MetadataProvider` discovers canonical observations, `AssetProvider` materializes opaque source assets only during execute workflows, and Grandgousier owns derived products through a separate `ProductStore`. The initial adapters are `fits-filesystem` and `master-manifest`; `stage1-plan` writes a metadata-only JSON/CSV plan without downloading science pixels or changing the Grandgousier DB.

Physical identity remains instrument-specific: MASTER uses `UNIQNUM`; RoboPhot uses `SERNUM + DATE-OBS UTC`. Paths and legacy raw IDs are not substituted for physical observation identity.

Starting with 0.6.1.0, `stage1-execute` explicitly materializes a READY canonical plan into the configured filesystem ProductStore. MASTER raw reduction reuses the established `MASTER_DARK_FLAT_V1` implementation; already processed FITS sources are imported byte-for-byte. Products receive checksum/fingerprint provenance sidecars and identical reruns are safely skipped. The command does not write Grandgousier DB state or automatically invoke Stage 2. Starting with 0.6.1.1, MASTER execution supports partial local caches: existing files are reused and only missing selected refs may be fetched by the repository-local transport during explicit execution; plan-only remains metadata-only.

Starting with 0.6.2.0, `stage1-handoff` provides the explicit Stage-1 -> Stage-2 boundary. It consumes one Stage-1 execution report, validates the named FITS/provenance/checksums, runs the existing P4 observation guard before any new Frame insertion, and delegates only genuinely selected exact ProductStore paths to the existing Stage-2 pipeline. It never scans the whole ProductStore. Already represented physical observations are retained without creating another Frame representative; ambiguous/conflicting cases block before DB writes. Starting with 0.6.2.1, historical `EXISTING_MULTIPLE_REPRESENTATIVES` is also retained as a handoff no-op: the audit state remains visible, but no third Frame is inserted and Stage 2 is not invoked. The default handoff mode is read-only.



## Rebuildable Stage-3 science cache (0.6.4.0)

0.6.4.0 begins the cutover away from wide row-level SCIENCE duplication. The authoritative final state remains immutable BASE photometry plus normalized calibration terms. An additive `science_photometry_cache` stores only calibration/base identity plus `mag_cal` and `mag_err_random` for Stage-3 hot queries. The cache is rebuildable and is used only when complete; existing `science_measurements` remains a compatibility baseline in this release.

0.6.4.1 extends the complete-cache path to M5/global-object light curves and current alias-head light-curve reads. Values not stored in the cache are reconstructed from immutable BASE plus normalized terms. Incomplete caches and historical non-head products still use the V1 compatibility layer; no V1 data is deleted.

Starting with 0.6.4.2, normal SCIENCE finalization writes the compact rebuildable cache directly from BASE plus normalized provenance and no longer creates a new wide `science_measurements` layer. Legacy wide SCIENCE rows remain available for historical compatibility; full wide materialization is opt-in through the existing intermediate-materialization compatibility/debug mode.

0.6.4.3 adds an exact historical flag-compatibility layer for future V1 retirement. Calibration-wide differences are factored once, while only per-BASE residuals are stored row-wise. All-row backfill and verification commands must prove zero scalar, identity and flag mismatches before any destructive cleanup; this release itself does not delete V1 data.

## Exact historical effective-flag preservation (0.6.3.2)

Historical normalized reconstruction is scalar-authoritative but may not reproduce every diagnostic/provenance flag present in an older immutable SCIENCE product. Starting with 0.6.3.2, shadow-checked rematerialization compacts the exact pre-write effective flag mapping from the chosen shadow baseline. This preserves `BASE || SCIENCE` exactly while still removing keys that are byte-semantically redundant with immutable BASE flags. Existing-SCIENCE BASE identity drift blocks the rewrite before deletion.

## Shadow-safe historical science rematerialization (0.6.3.1)

`materialize-science-calibration --shadow-compare-legacy` now uses existing immutable `science_measurements` as the scalar shadow baseline when the target calibration no longer has a legacy `measurements` layer. Legacy target rows are still preferred when present. If neither source exists, the command refuses the shadow-checked write before deleting any science rows. This aligns write-time validation with `verify-science-materialization` and enables safe storage compaction of historical normalized SCIENCE generations without changing photometric reconstruction or schema.

## Science measurement storage (0.6.3.0)

The final science layer exposes effective flags as immutable BASE measurement flags overlaid by SCIENCE-specific flags. Starting with 0.6.3.0, the Python reconstruction fallback stores only keys whose value differs from BASE instead of copying the complete accumulated BASE JSONB payload into every `science_measurements` row. The PostgreSQL server fast path was already compact. This is a storage-representation change only: scalar photometry, normalized calibration terms and the public effective flag view are unchanged. Existing historical rows are not rewritten automatically.

## Self-service operations

Starting with 0.5.56.0, `grandgousier config-example` writes a packaged YAML template, `grandgousier doctor` performs a non-destructive installation/database/schema/path preflight, and `grandgousier status --field-id ...` reports read-only Stage-2 provenance readiness plus latest Stage-3 generation verification/freshness. The same general config can feed `process-field-archive`; legacy flat job configs remain supported.

Starting with 0.5.59.0, every `process-field-archive` service invocation writes a self-service run bundle under `<out_root>/_jobs/<job_id>/` with a secret-safe runtime manifest and normalized diagnosis/recovery guidance. Use `grandgousier diagnose-run <job-dir>` to inspect either a 0.5.59+ bundle or a legacy job directory without touching the database.


## Reproducible release tooling

Starting with 0.5.57.0, source checkouts include `tools/snapshot_environment.py` and `tools/release_check.py`. The first captures the exact installed dependency closure on a validated deployment into a constraints file plus machine-readable provenance; the second builds and inspects wheel/sdist, performs an isolated-target wheel import smoke (hardened in 0.5.58.1 against already-installed older versions), checks public/private package boundaries, and writes SHA-256 checksums. `Pipfile.lock` is retained only as legacy history and is not an authoritative production lock. See `release/README.md` in the source distribution.

## Database

Grandgousier 0.7 is a fresh-baseline schema epoch. Create a new empty database and use `grandgousier init-db`; the ORM creates the complete clean schema and baselines the managed migration ledger. `migrate-db` refuses databases that still contain retired 0.6 compatibility relations, so it must not be used to transform the frozen 0.6 production database in place. Use `grandgousier schema-status` to inspect the ledger.


## Physical observation identity

Starting with 0.5.52.0, `observations` separates one physical exposure from one or more file products in `frames`. MASTER identity v1 uses FITS `UNIQNUM`; RoboPhot identity v1 uses FITS `SERNUM + DATE-OBS`, with RoboPhot `DATE-OBS` defined as UTC. The migration is additive and backfill is plan-only unless `--execute` is given; legacy `frame_uid` is not rewritten. Starting with 0.5.53.0, normal archive ingestion automatically creates/reuses and links these observations while still allowing multiple Frame/file products per physical exposure. Missing or invalid stable identity is never replaced by a path-derived fallback. Starting with 0.5.54.0, new science-series membership is guarded by physical Observation: an existing representative is retained and multiple unrepresented products are blocked rather than ranked by an arbitrary file heuristic. Starting with 0.5.54.1, ordinary `process-archive-path` replay queues only series created from science inputs selected in that invocation; existing series are rerun only when explicitly requested with `--series-id`. Starting with 0.5.54.2, persisted Stage-3 M4 edges are normalized to the schema-required `left_series_id < right_series_id` orientation without changing the solved graph. Starting with 0.5.54.3, M8 resume classification is observation-aware: alternate Frame products already represented by a READY series in the target field do not force a false incomplete batch, while genuinely unrepresented observations and legacy null observation identities remain blocking. Version 0.5.54.4 is a documentation/configuration cleanup release that removes the obsolete GPX_TF17 2018-09-07 production exclusion from current examples after real plan+execute validation; runtime and scientific algorithms are unchanged.


## Scientific acceptance

Starting with 0.5.58.0, source/wheel installations include a frozen read-only scientific acceptance specification for the validated GPX_TF17_2 field. After installing the full `[photometry,gaia]` stack, run `grandgousier acceptance-check --dataset gpx_tf17_2 --config grandgousier.yaml`. The check verifies frozen database counters, Stage-2 readiness, exact Stage-3 source scopes/graph statistics, M5 control-object counts and that no database counters changed during the check.


Release note: 0.5.59.1 is a release-tooling-only hotfix that cleans local setuptools `build/` and root `*.egg-info` staging before wheel/sdist creation, preventing stale Python modules from leaking into a new-version wheel after ZIP overlay updates.

0.6.4.4 adds a fail-closed retirement command for legacy wide SCIENCE rows. Retirement requires complete compact cache coverage, a completed exact historical flag manifest, and a fresh all-row verification immediately before deletion. Retired manifests remain readable and preserve the original source row count as the post-V1 cache completeness reference. Physical PostgreSQL file reclamation remains a separate maintenance step.


0.6.4.5.1 adds an explicit global `--all-materialized` scope for the compact-cache, historical-override verification, and legacy SCIENCE retirement tools. It never implies execution; destructive deletion still requires `--execute`.


0.7.0 introduces the fresh-baseline clean schema epoch. Canonical `measurements` storage is BASE-only, final SCIENCE scalars are persisted in narrow `science_photometry`, legacy wide/cache/flag-override SCIENCE relations are excluded from fresh ORM metadata, and field-series membership is genuinely many-to-many. Historical 0.6 databases remain reference corpora and are intentionally not migrated in place.

0.7.0.1 normalizes detection storage for the same future fresh schema. Repeated source-detection and catalog-match configuration/provenance is stored once in `detection_runs`, frame-level execution state and common diagnostics live in `detection_run_frames`, and high-cardinality detection/match rows keep row-specific measurements and scientific snapshots. `frame_catalog_links` is deliberately unchanged pending a real PostgreSQL query-plan and physical-storage benchmark. This is not an in-place migration path for an existing 0.6 or 0.7.0-A database.
