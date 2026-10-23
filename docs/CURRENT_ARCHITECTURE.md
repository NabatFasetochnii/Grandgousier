# Grandgousier — текущая и одобренная целевая архитектура

## 2026-10-23 Target-optional resolver correction — local focused tests PASS; live/science validation OPEN
- **Current implementation; current behavior supersedes earlier target-resolution descriptions for implementation behavior only.** After paired-coordinate validation and the existing explicit-empty-frame / negative-limit checks, no object ID, catalog source ID, or paired caller coordinates returns `method="not_requested"` before calibration, frame-instrument, or header DB queries. Frame pointing and header cards—including MASTER `ALPHA/DELTA`, `CRVAL/WCS`, and `coord2000`—never identify a target. The MASTER profile's unused `ra_requested` / `dec_requested` aliases are removed; pointing aliases and the separate Gaia catalog-search-center behavior are unchanged.
- No database schema field or migration was added; this patch does add paired RA/Dec runner configuration/CLI inputs and `coordinate_provenance` in resolver output. Existing policy fields distinguish `not_requested` / `UNRESOLVED_NO_TRUSTED_TARGET_INPUT` from explicit-but-unresolved selector provenance and resolution method/reason. Explicit ID, catalog-ID, and paired-coordinate routes remain available.
- Ordinary field runs no longer automatically invoke `cleanup_target_light_curve_artifacts`; the helper was left unchanged and uncalled. Target export still requires resolution. Synthetic runner/QC tests cover empty target/reference IDs, successful non-failure status, no export, byte-preserved target-named files in `tmp_path`, and no `FIELD_TARGET` flag for empty target IDs.
- Reference provenance remains unchanged: static source evidence shows ensemble result serialization of `reference_object_ids`, persisted normalized `is_reference`, and object/frame/series/calibration identity on target LC rows. Whether live iteration rows are present and always unambiguously join to exports is **UNVERIFIED**; no new membership feature was introduced.
- **Focused local test results:** combined field-target/runner/QC/archive-path/Stage-1 command: `112 passed, 1 deselected` (deselected/excluded: real-MASTER inventory test `test_real_master_inventory_exact_gpx_v25_read_only_if_available`). Full pytest and full Ruff were not run. No DB/network/private config, real CLI/DB-backed pipeline or archive execution, product access/write, cleanup, schema/formula/materialization change, or commit. `READY_FOR_STAGE2_SCIENCE=NO`, `READY_FOR_LIVE_MASTER_DB=NO`, `READY_FOR_MASTER_PRODUCTION=NO`, `READY_FOR_DB_WRITE=NO`, `READY_FOR_CLEANUP=NO`, `READY_FOR_COMMIT=NO`.
- **Readiness and science boundary:** a field can be processed without a target; field-processing readiness is distinct from optional target-product readiness. `READY_FOR_STAGE2_SCIENCE=NO` here is due to the separately documented RoboPhot r ensemble-contamination/science STOP (object 2275 remained a reference), not merely a missing selector; no new science acceptance is claimed.
- This workflow/identity phase changed no photometry, aperture/centroid/correction, ensemble/calibration or uncertainty formulas, Stage 2 thresholds, or Stage 3 mathematics.
- MASTER `coord2000` is optional archive/search/frame metadata that varies by frame, not target identity. The current parenthesized `coord2000::text` value is not parsed by the Stage 1 parser; its final semantics/conversion are open. This is an archive/search metadata limitation, not a global field-science blocker.
- **Storage direction:** one calibrated field supports multiple downstream object selections; full Stage 2 is not rerun per target. Immutable BASE and current validated SCIENCE are durable; heavy temporary/superseded products need a bounded lifecycle. Reduced Stage 1 FITS are reclaimable only under existing policy after durable Stage 2 validation.
- **NEXT (audit only):** audit one-command bounded processing of the existing complete MASTER archive: MASTER inventory → canonical observation dedup → bounded Stage 1 batches → complete series → Stage 2 → durable science → controlled reclaim → Stage 3/final products. No processing design or implementation in this phase; earlier dated PLAN/NEXT entries below remain historical and are not the current next step.


Current source candidate (2026-09-24, **IMPLEMENTED_NOT_REAL_VALIDATED**): new Stage-2
defaults select per-star `adaptive_ellipse` with explicit `circular` override, 3-pixel
base floor, 2.5× cap, and existing flagged circular fallback. `FieldPipelineConfig`
records the effective estimator in `pipeline_runs.recipe_json`; Stage-1 handoff inherits
`process_archive_path` defaults. Frozen `stage3_scale_v1` selection/Stage-3 formulas are
unchanged; generic adaptive runs are not automatically Stage-3 recipe-ready. Historical
dated architecture below describes earlier baselines, not acceptance of this candidate.

Актуально на **2026-09-21**. Current source candidate: **0.7.0.9 clean SCIENCE parity hotfix**. Phase A–D и 0.7.0.4 user-real-validated на Windows/CPython 3.11; последний validated baseline — 0.7.0.4 (`723 passed, 1 skipped`). Frozen production/reference DB остаётся **0.6.4.5.1**, ≈43 GB. 0.7 — fresh-baseline schema epoch; frozen 0.6 DB не мигрируется in-place.


## 0. Current 0.7.0.3 clean storage/identity/reference-photometry boundary

- `measurements` — только immutable BASE extraction rows; low-level writers проверяют `BASE_PHOTOMETRY / BASE / FULL_MEASUREMENTS`.
- `science_photometry` — durable narrow final science state keyed by `(calibration_version_id, base_measurement_id)` with eight calibration-changing scalars. Publication occurs only after all earlier science/export steps succeed; writer requires `SCIENCE / COMPLETE_IMMUTABLE` and does not retain durable rows for intermediate products.
- Clean-schema science verification compares normalized reconstruction against `science_photometry` + immutable BASE geometry; it must never require the retired `science_measurements` relation.
- Retired 0.6 SCIENCE compatibility relations не входят в fresh `Base.metadata`; их mappings остаются только для frozen-reference forensic code.
- `field_series_frames` — many-to-many membership with uniqueness only on `(series_id, frame_id)`; grouping method participates in series UID.
- `calibration_frame_terms` and `calibration_iteration_objects` derive calibration identity through `calibration_iterations`.
- `init-db` creates/baselines only an empty clean 0.7 schema; `init-db`/`migrate-db` refuse a pre-0.7 compatibility schema before writes.
- Detection storage is run-normalized: `detection_runs` owns algorithm/config/fingerprint/provenance, `detection_run_frames` owns per-frame status/diagnostics, and `source_detections` keeps row-specific source measurements/flags only.
- `detection_catalog_matches` derives frame/catalog/algorithm/config identity through the run/source/catalog graph; `detection_mag_inst`, `catalog_mag`, and `zero_point` remain immutable historical QC snapshots.
- `frame_catalog_links` remains unchanged in Phase B pending a real clean-workload EXPLAIN/bytes benchmark.
- `objects.object_uid` is opaque internal identity; external catalog identities live in explicit `object_catalog_links`.
- Object association is conservative: existing catalog link first, then exactly one epoch/PM-aware candidate within 0.75 arcsec; an Object already linked to the same `catalog_name` is excluded for a different source ID, preserving close same-catalog pairs; ambiguity creates a new Object rather than a false merge.
- `frame_locations` owns mutable locators/file metadata/content fingerprint; `frames` no longer maps `raw_path/file_size_bytes/file_mtime_utc` columns.
- Multiple paths can represent the same Frame; MASTER/RoboPhot native Observation identity remains authoritative.
- Stage-2/Stage-3 production-v1 numerical rules are unchanged.

Phase A, Phase B and Phase C are Windows-real-validated. Phase D is the current source candidate. No fresh archive DB is created until Phase D passes the Windows gate. Sections below describing 0.6.4.5.1 are the frozen reference architecture used for old/new comparison.

## 1. Frozen 0.6.4.5.1 reference pipeline

```text
Stage 1: source discovery/materialization/reduction -> processed FITS
Stage 2: immutable BASE extraction + normalized single-series calibration/QC
Stage 3: immutable inter-series generations + global light curves
```

Stage 1 source boundary остаётся source-neutral: `MetadataProvider`, `AssetProvider`, `ProductStore`. P4 physical Observation identity отделена от locator.

MASTER identity = `UNIQNUM`. RoboPhot identity = `SERNUM + DATE-OBS`, причём RoboPhot `DATE-OBS` уже UTC.

## 2. Frozen 0.6.4.5.1 reference database truth layers

### Observation / frame / series

`observations` — authoritative physical-exposure identity.

`frames` в frozen 0.6.4.5.1 — image/product row, который всё ещё содержит `raw_path`. В current 0.7.0.3 source candidate locator вынесен в `frame_locations`.

`field_series` + `field_series_frames` в frozen 0.6 имеют global `UNIQUE(frame_id)`. Current 0.7 source contract уже разрешает альтернативные memberships через uniqueness `(series_id, frame_id)`.

### Object/catalog layer

`objects` в frozen 0.6 использует catalog-dependent `object_uid`. Current 0.7.0.3 source contract использует opaque internal UUID + explicit `object_catalog_links`.

`catalog_sources`, `frame_catalog_links`, `source_detections`, `detection_catalog_matches` — high-volume catalog/detection evidence. В 0.7.0.1 detection rows уже run-normalized, но слой остаётся научно значимым для transient/unusual-variable search и не является cleanup-мусором.

### Photometry/calibration layer

`measurements` сейчас содержит:

- immutable BASE extraction truth;
- 1.8M исторических `FULL_MEASUREMENTS` derived rows.

Current default pipeline уже не создаёт derived full rows: `materialize_intermediate_calibrations=False`, normal derived state = `NORMALIZED_TERMS`.

Normalized state:

```text
calibration_versions
calibration_iterations
calibration_frame_terms
calibration_iteration_objects
```

`reconstruct_calibration_measurement_states()` строит calibration state из BASE + normalized terms.

`CalibrationPhotometryView` — storage-independent reader boundary, но несколько historical/direct SQL readers всё ещё обходят её; это важно при изменении schema.

### Final SCIENCE

Wide `science_measurements` полностью logical-retired и физически truncated до 0 rows.

Current compact state:

```text
science_photometry_cache       10,151,494 rows
science_flag_override_sets            104 rows
science_flag_overrides            917,220 rows
```

Override tables нужны только для exact preservation исторических V1 diagnostic flags. Они не являются желаемой нормой для fresh 0.7 products.

`science_photometry_cache` сейчас формально rebuildable и содержит только:

```text
calibration_version_id
base_measurement_id
mag_cal
mag_err_random
```

Для clean 0.7 он должен стать/быть заменён durable narrow final-science entity с явным научным контрактом.

### Stage 3

Canonical Stage-3 storage:

```text
interseries_calibration_generations
interseries_calibration_series
interseries_calibration_edges
```

`interseries_calibration_offsets` — legacy compatibility storage и не должен определять clean 0.7 модель.

M5 global object export соединяет exact generation-linked Stage-2 science points и не создаёт ещё один full measurement layer.

## 3. Database governance

Current schema определяется SQLAlchemy ORM + managed migration ledger `schema_migrations`.

Для действительно fresh DB `grandgousier/db/init_db.py` выполняет:

```text
Base.metadata.create_all
-> assert_schema_contract
-> baseline_current_schema(reason="fresh_orm_init")
```

Следовательно:

```text
fresh install -> init-db
existing managed DB -> migrate-db
legacy pre-ledger adoption -> отдельный validated adopt path
```

Не пытаться создавать fresh 0.7 через replay всей legacy migration history.

## 4. Approved 0.7 clean-schema principles

1. Original FITS остаются вне PostgreSQL.
2. Один immutable BASE photometry row на object/frame/base product.
3. Normal pipeline не materialize-ит full intermediate measurement copies.
4. Final science state durable, narrow и SQL-queryable.
5. Repeated frame/run/calibration context хранится один раз.
6. Catalog identity отделена от physical/internal object identity.
7. Paths/URLs — locators, не identity.
8. Historical 0.6 compatibility tables не должны автоматически входить в fresh 0.7 baseline.
9. Scientific provenance parents immutable; destructive parent deletion не должна silently erase lineage.
10. JSONB — для sparse/raw/rare payload, а не для часто фильтруемых/science scalars и миллионов одинаковых contexts.

## 5. Approved 0.7 structural changes

### A. Core clean schema

`measurements` -> BASE-only; `calibration_version_id NOT NULL`; prohibit normal derived FULL_MEASUREMENTS.

Promote/replace `science_photometry_cache` -> durable `science_photometry` with only scientifically changing final state plus BASE identity reference.

Omit clean-baseline `science_measurements`; historical flag override layer should not be default fresh-schema concept.

`field_series_frames`: remove global unique frame constraint; preserve `(series_id, frame_id)` uniqueness; grouping definition/version explicit.

`calibration_frame_terms` / `calibration_iteration_objects`: calibration identity should be determined through `calibration_iteration_id` or protected by composite integrity; preferred target removes redundant child `calibration_version_id`.

### B. Detection normalization

Implemented source contract in 0.7.0.1:

- `detection_runs` stores run-level algorithm/version/config/fingerprint/pipeline provenance;
- `detection_run_frames` stores frame scope, status and frame-common diagnostics, including zero-result frames;
- `source_detections` stores only row-specific measurements/status/flags plus run-frame FK;
- `detection_catalog_matches` removes duplicated frame/catalog/algorithm/config payload while deliberately retaining historical photometric match snapshots;
- all active readers/writers/QC/event/idempotency/cleanup paths derive context through explicit joins;
- `frame_catalog_links` is not slimmed yet because its query/workload benchmark requires a fresh PostgreSQL smoke dataset.

Physical bytes/row and EXPLAIN are acceptance gates after A–D fresh GPX smoke, before archive-wide rebuild.

### C. Object identity and file locations

Implemented and Windows-real-validated contract in 0.7.0.2:

- `objects.object_uid` is an opaque internal UUID, independent of catalog namespace, source identifier and coordinate formatting;
- explicit `object_catalog_links` records catalog provenance/evidence;
- association is fail-conservative: existing link -> exactly one epoch/proper-motion-aware astrometric candidate within 0.75 arcsec -> otherwise new Object; ambiguous matches are not merged automatically;
- `frame_locations` contains path/URI, size/mtime, availability and optional content SHA-256; these values no longer define Frame/Observation identity;
- a moved/copy-like product can add a second location to the same Frame rather than create another physical observation;
- MASTER `UNIQNUM` and RoboPhot `SERNUM + DATE-OBS(UTC)` remain authoritative physical observation identity.

The policy deliberately prefers occasional duplicate internal Objects over scientifically dangerous false merges. A later explicit association/merge workflow may reconcile such duplicates with stronger evidence.

### D. Reference photometry

Implemented source contract in 0.7.0.3:

- `reference_photometry_catalogs` remains the provider/catalog registry;
- `photometric_bands` defines bands as rows per catalog, so adding a band does not require ALTER TABLE;
- `reference_photometry_sources` stores source identity, astrometry, generic identity-catalog cross-link, colors, source-level quality and one raw provider payload;
- `reference_photometry_points` stores per-source/per-band magnitude, uncertainty and optional point-level quality/provenance;
- importer replacement semantics delete/rewrite a source point set on re-import, preventing stale bands;
- active color calibration, diagnostics, lookup, summary and auto-process readers use normalized data; legacy `mag_*` payload fields are synthesized only at API/calibration boundaries;
- the wide pre-D relation exists only as `LegacyBase` forensic mapping and is forbidden in a fresh 0.7.0.3 schema.

## 6. Tables classification

### KEEP first-class

`schema_migrations`, `observations`, `fields`, `frame_qc`, `calibration_iterations`, `calibration_product_aliases`, `interseries_calibration_generations`, `interseries_calibration_series`, `interseries_calibration_edges`, `artifacts`, `artifact_publications`, batch tables, `event_registry`, `event_object_links`, `event_detection_links`, `processing_events`.

### KEEP but tighten/slim

`instrument_units`, `calibration_versions`, `calibration_frame_terms`, `calibration_iteration_objects`, `catalog_sources`, `frame_catalog_links`, `object_qc`, `field_series`, pipeline provenance/log tables, `reference_photometry_catalogs`.

### REFACTOR/REPLACE before full archive rebuild

`event_frame_coverage` remains a known later cleanup candidate. `objects`, `frames`, `reference_photometry`, `measurements`, `source_detections`, `detection_catalog_matches` and `field_series_frames` have received their clean-schema source refactors but still await fresh-DB physical validation.

### Compatibility-only / omit from fresh 0.7 if readers are cut over

`science_measurements`, `science_flag_override_sets`, `science_flag_overrides`, `interseries_calibration_offsets`.

## 7. Frozen production DB vs 0.7 DB

0.6.4.5.1 DB остаётся reference truth для comparison и historical exact representation. Не пытаться автоматически преобразовать её в 0.7.

Fresh 0.7 DB создаётся только после реализации clean schema в source code. GPX_TF17_2 должен быть повторно обработан из raw/source inputs. Old/new сравнивается по logical identity/science semantics, а не по surrogate numeric IDs.

## 8. Known architecture debts to measure, not guess

- BASE `measurements.flags` JSONB/TOAST может оставаться крупнейшим repeating payload; сначала clean smoke profile, затем factorization experiment.
- high-cardinality detection/catalog tables необходимо сохранять scientifically useful; compression/normalization — только lossless.
- nullable UNIQUE semantics должны быть явными на PostgreSQL 18.
- planner stats старой DB stale для ряда tables; exact counts ≠ `n_live_tup`.
- large pipeline/result JSON лучше хранить в artifacts с compact searchable DB summary.

## 9. Source of truth docs

```text
CONTEXT.md                    current factual state
this file                     architecture contract
ROADMAP.md                    next engineering sequence
PROJECT_HISTORY.md            historical archive
context/project_state.yaml    machine-readable state
```

Legacy `NEXT_CHAT_PROMPT.md` deprecated и не должен заменять canonical context.
# Portable science product (source implementation; live validation pending)

`grandgousier export-science-parquet --database-url URL --series-id N --out ABSOLUTE_PATH`

The export is bounded and read-only. Stage 3 values are included only when an
explicit `--interseries-generation-id N` is supplied and its immutable generation
contains a matching SCIENCE-calibration row for every requested series. Without
that option, global magnitudes/errors are null and the product is explicitly
Stage-2-only (`NOT_REQUESTED`); calibrated magnitudes are not presented as global.
exports one or more explicit series via a PostgreSQL READ ONLY transaction. PyArrow
is optional (`pip install grandgousier[parquet]`). Output contains explicitly typed
`sources.parquet`, `frames.parquet`, sparse `photometry.parquet`, `series.parquet`,
and `manifest.json`. Frames are selected from series membership independently of
photometry, so zero-measurement frames remain represented without synthetic science
rows. Object and physical-observation UIDs, not database surrogate IDs, are the
portable keys. Observing-night convention is `date(DATE-OBS_UTC - 12 hours)`.
The writer streams query results in batches, stages under a sibling temporary
directory, hashes files incrementally, refuses existing output, and promotes by
same-volume rename. Point acceptance delegates to the shared light-curve product
rejection contract rather than defining exporter-specific veto rules. Database-backed
validation and real export remain pending. FrameQc rows are aggregated in the frame
query; QC category counts remain null when persisted classifications are unavailable.
Global Stage-3 values are explicitly `NOT_EXPORTED`; they are not implied absent or
zero. This implementation is not production/live-export readiness evidence.
