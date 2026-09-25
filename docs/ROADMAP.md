# Grandgousier — план перехода к 0.7 clean rebuild

Current gate (2026-09-24): **IMPLEMENTED_NOT_REAL_VALIDATED** source candidate 0.7.0.10
sets `adaptive_ellipse` as the new Stage-2 estimator default, with explicit circular
override and unchanged radius/fallback and `stage3_scale_v1` recipe. DB-free tests only;
real photometric acceptance, DB execution, deployment, and release remain separate gates.
The dated milestones below are retained as historical plans, not current instructions.

Актуально на **2026-09-21**. Current source candidate: **0.7.0.9 clean SCIENCE parity hotfix**. Phase A–D и 0.7.0.4 REAL-VALIDATED на Windows/CPython 3.11; 0.7.0.4 gate: `723 passed, 1 skipped`, packaging/install smoke OK. Frozen production/reference baseline: **0.6.4.5.1**, ≈43 GB. Fresh DB `grandgousier_070_fresh` bootstrap READY; seed V/R Stage2 is DONE on 0.7.0.6, and storage audit found stale durable rows on failed intermediate generations that 0.7.0.7 prevents and repairs. Full clean rebuild now uses `grandgousier_070_gpx_full`; all full Stage2 dates through 2018-09-23 are complete; the remaining blocker is a pre-Stage3 parity reader that still referenced retired `science_measurements`, fixed in 0.7.0.9.

## 0. Неподвижные условия

Не делать destructive in-place migration production DB.

Не удалять старую `grandgousier` до завершения old/new acceptance и отдельного согласия пользователя.

Не менять frozen Stage-2/Stage-3 production-v1 formulas/selection semantics в storage/schema patch.

MASTER `UNIQNUM` и RoboPhot `SERNUM+DATE-OBS(UTC)` остаются authoritative observation identity.

Нормальный pipeline не должен включать `--materialize-intermediate-calibrations` при clean rebuild.

## 1. Phase 0 — source baseline и pre-change audit

Перед первой правкой следующего чата:

1. подтвердить актуальный source tree `0.6.4.5.1`;
2. прочитать `CONTEXT.md`, `CURRENT_ARCHITECTURE.md`, `ROADMAP.md`, `project_state.yaml`;
3. прочитать actual models/schema/bootstrap и relevant readers/writers;
4. проверить tests, которые зафиксируют 0.6.4.5.1 behavior;
5. не выполнять никаких DB commands;
6. сформировать dependency matrix каждой таблицы/колонки, затрагиваемой 0.7.0-A;
7. решить, является ли 0.7 отдельной fresh-baseline schema без backward DB compatibility (предпочтительно) или нужен compatibility adapter; production DB не мигрируется.

Exit gate: точный patch scope без guessed schema и без destructive operations.

## 2. Phase A — 0.7.0-A clean schema foundation — WINDOWS REAL-VALIDATED

Это **первый обязательный implementation patch**.

### A1. BASE-only measurements

Цель:

```text
measurements = immutable BASE extraction truth only
```

Сделать:

- `calibration_version_id NOT NULL`;
- enforce/validate `product_kind=BASE_PHOTOMETRY`, `product_role=BASE`, `COMPLETE_IMMUTABLE` для calibration versions, которым принадлежат BASE rows;
- убрать/запретить code paths, которые в normal mode пишут derived full rows;
- compatibility option не должен определять clean schema;
- добавить tests, что default pipeline не создаёт non-BASE measurements.

Не делать пока BASE flags factorization: сначала clean smoke physical profile.

### A2. Durable final science

Спроектировать `science_photometry` вместо rebuildable-name `science_photometry_cache`.

До реализации определить exact durable scalar contract по reconstruction/API consumers. Не копировать wide V1 автоматически.

Минимум должен поддерживать:

- Stage 3 planning/analysis/persistence;
- M5/global object;
- current light curves;
- stable SQL query surface;
- object/frame identity через BASE reference.

Нужно решить migration/API strategy: rename vs new table в fresh baseline. Поскольку production DB не мигрируется, допускается clean break 0.7 при сохранении Python compatibility boundary.

### A3. Убрать fresh-schema historical compatibility baggage

Candidates to omit from 0.7 fresh schema after reader audit:

```text
science_measurements
science_flag_override_sets
science_flag_overrides
interseries_calibration_offsets
```

Если current source импортирует models/readers этих tables, сначала сделать code cutover, затем удалить из schema contract/models. Не удалять вслепую.

### A4. Series membership

Убрать `UNIQUE(frame_id)` из `field_series_frames`.

Сохранить `(series_id, frame_id)` uniqueness.

Добавить explicit reproducible grouping definition/version там, где лучше всего соответствует существующей `field_series` model.

Тест: один Frame может легально принадлежать двум alternative series definitions, при этом duplicate membership в одной series запрещён.

### A5. Normalized calibration integrity

Предпочтительно удалить redundant `calibration_version_id` из:

```text
calibration_frame_terms
calibration_iteration_objects
```

и получать через `calibration_iterations`.

Перед этим проверить все direct SQL/ORM readers и indexes. Если removal слишком широк для A, временный допустимый вариант — composite integrity FK/unique pair, но финальный target остаётся без duplicated identity.

### A6. Provenance FK policy

Проверить `SET NULL` на scientific lineage (`CalibrationVersion`, artifacts, QC etc.). Для clean immutable provenance использовать `RESTRICT/NO ACTION`, где удаление parent означало бы потерю scientific lineage. Не менять operational relations механически.

### A7. Fresh bootstrap contract

`init-db` должен создать clean 0.7 schema из ORM и baseline migration ledger.

Добавить isolated test на абсолютно пустую PostgreSQL schema/database-like environment, насколько позволяет test infrastructure. Не строить fresh bootstrap через legacy migration replay.

### A8. A-phase tests

Минимум:

- model/schema contract tests;
- `init-db` fresh baseline tests;
- default pipeline storage mode tests;
- reconstruction/calibration view tests;
- Stage3 cache/science reader tests;
- series membership tests;
- acceptance-service unit/integration tests без production DB;
- broad pytest + compileall;
- release_check по существующей политике.

Implementation и Windows/Python 3.11 release gate закрыты: `704 passed, 1 skipped`; compileall и release packaging/install smoke OK. Production DB untouched.

## 3. Phase B — detection storage normalization — WINDOWS REAL-VALIDATED

Source version: **0.7.0.1**. Production DB untouched.

### B1. `detection_runs` — IMPLEMENTED

Добавлены `detection_runs` + `detection_run_frames`. Run-level algorithm/version/config/fingerprint/pipeline provenance хранится один раз; frame-level status/diagnostics — один раз на frame/run. Zero-detection/zero-match frames сохраняются явно.

`source_detections` больше не содержит repeated `frame_id`, `algorithm`, `parameters` или frame-common detection diagnostics; остаются source-specific measured quantities/status/flags + run-frame reference.

### B2. Match/projection slimming — PARTIAL/CONSERVATIVE

Real readers/writers audited. `detection_catalog_matches` переведён на run/source/catalog graph и лишён reconstructable repeated frame/catalog/algorithm/details payload. `detection_mag_inst`, `catalog_mag`, `zero_point` сохранены как immutable historical snapshots, потому что current catalog/reference state может обновляться.

`frame_catalog_links` **не менялся**: его slimming/index changes запрещены до реального workload EXPLAIN.

### B3. Physical/query benchmark — DEFERRED GATE

После A–D на отдельной fresh GPX smoke DB сравнить rows/bytes/indexes/query plans до/после. Не экстраполировать synthetic/capped sample как global saving. Если join plans регрессируют, допускается вернуть только доказанную typed denormalization.

Source implementation gate закрыт. Windows/CPython 3.11: `709 passed, 1 skipped`, `compileall` OK, release check `READY_WITH_WARNINGS` только из-за не переданных validated constraints/snapshot; wheel/sdist/resources/install-smoke/checksums OK. Physical EXPLAIN/bytes exit gate остаётся post-A–D fresh-smoke условием до archive-wide rebuild.

## 4. Phase C — identities and locations — WINDOWS REAL-VALIDATED 0.7.0.2

### C1. Object identity — IMPLEMENTED

`objects.object_uid` теперь opaque internal UUID; catalog/source ID и координаты не входят в UID. `object_catalog_links` хранит explicit catalog identity/evidence.

Association contract: existing catalog link -> ровно один astrometric candidate в 0.75 arcsec с epoch/proper-motion propagation -> новый Object. Candidate, уже связанный с другим source ID того же `catalog_name`, исключается из astrometric merge; разные источники одного catalog release не схлопываются. Если candidates несколько, auto-merge запрещён: создаётся новый Object. Это conservative policy против false merges; отдельный evidence-driven merge можно добавить позже.

### C2. Frame locations — IMPLEMENTED

`frame_locations` хранит mutable locator, size/mtime, availability и optional SHA-256. Fresh `frames` не содержит locator/file-metadata columns; Python compatibility properties читают primary location.

`raw_path` больше не identity. MASTER/RoboPhot observation identity не менялась; content hash — только дополнительное content/location evidence.

### C3. Replay/moved-file tests — WINDOWS REAL-VALIDATED

Preflight/indexer различают новый product и relocated COPY_LIKE product по observation identity + non-locator product metadata. Одно physical product может иметь несколько locations без создания нового Frame/Observation.

Windows/CPython 3.11: targeted regressions `3 passed`, полный pytest `714 passed, 1 skipped`, `compileall` OK; release check `READY_WITH_WARNINGS` только из-за не переданных validated constraints/snapshot.

## 5. Phase D — normalized reference photometry — IMPLEMENTED SOURCE CANDIDATE 0.7.0.3

Fresh layout:

```text
reference_photometry_catalogs
        ↓
photometric_bands + reference_photometry_sources
        ↓
reference_photometry_points(source, band, magnitude, error, quality/provenance)
```

Active runtime больше не читает wide `reference_photometry`. Import/query/color-calibration/auto-process paths переведены на normalized representation; новые полосы импортируются как rows, а не schema columns. Compatibility payload сохраняет текущие `mag_g/.../mag_g_gaia`, colors и error keys для существующей calibration logic. Wide pre-D table остаётся только в frozen `LegacyBase`, marker D fail-closed запрещает in-place conversion.

Source gate: `619 passed, 42 skipped` broad available suite, targeted D/schema/color `80 passed, 1 skipped`, `compileall` OK. Exit gate: Windows full pytest + release packaging/install smoke. После него переходить к Phase E fresh DB bootstrap.

## 6. Phase E — fresh DB bootstrap

Только после A–D.

Не использовать production app credentials для administrative connection без проверки.

Сначала определить текущий connection source (env/config) без вывода password. Если user `grandgousier` не имеет CREATE DATABASE, использовать explicit admin procedure вне приложения; не менять `pg_hba.conf` автоматически.

После создания fresh DB:

```text
init-db
schema-status
doctor --strict-optional
```

Снять fresh schema snapshot и сравнить с 0.7 contract.

Ожидание: science/data rows = 0, migration ledger clean-baselined, no compatibility baggage unless explicitly retained.

## 7. Phase F — GPX_TF17_2 clean smoke rebuild

Обработать только исходные/raw inputs GPX_TF17_2. Не reuse old DB-derived caches/materializations, если они могут скрыть bootstrap/storage defects.

Сравнить old/new по:

- physical observation identities;
- frame/series logical membership;
- Stage2 READY coverage;
- exact science values/tolerances по frozen contract;
- Stage3 graph topology/offsets/statistics;
- M5 source/accepted/rejected + reasons;
- output fingerprints где meaningful;
- storage composition and bytes.

Не требовать одинаковых surrogate IDs.

## 8. Phase G — idempotency gate

Повторить тот же dataset тем же current 0.7 workflow.

Доказать:

```text
new observations = 0
unexpected new frames = 0
unexpected duplicate BASE rows = 0
unexpected new calibration generations = 0 при identical input fingerprint
unexpected new science rows = 0
non-BASE measurements = 0
```

Проверить series membership и artifacts/pipeline run semantics.

## 9. Phase H — archive-wide rebuild

Только после F/G.

Запускать по инструментам/датам/полям контролируемыми batches с периодическими gates:

- DB size/row growth;
- duplicate observation identities;
- BASE materialization factor;
- science rows relative to BASE;
- detection bytes/row;
- failed/partial jobs;
- ANALYZE/autovacuum health;
- selected science acceptance controls.

Не ждать конца всего архива, чтобы обнаружить storage regression.

## 10. Phase I — old DB retirement

Только после полного rebuild и explicit user decision:

1. final acceptance old/new;
2. export schema snapshot + counts + checksums + critical manifests;
3. DB dump/archive strategy;
4. verify dump restore if archive required;
5. затем отдельно решить DROP old DB.

Никакого автоматического DROP в application patch.

## 11. Validation discipline / pitfalls

- неизвестные columns/table semantics сначала inspect, потом SQL;
- absence of FK != reconstructability;
- exact historical flags != scalar parity;
- PowerShell environment не имеет `rg` по умолчанию;
- nullable psycopg binds нельзя использовать в untyped `IS NULL OR` selector;
- SQLAlchemy bind casts через `CAST()`;
- qualify joined columns;
- exact counts отдельно от stale planner estimates;
- production DB read-only reference на весь 0.7 migration project.

## 12. Ближайший следующий шаг

После Windows validation 0.7.0.3 следующая работа начинается **с Phase E fresh DB bootstrap**, а не с дальнейшего schema refactor. Phase B physical/query benchmark остаётся частью fresh GPX gate до archive-wide rebuild.

Expected deliverable первой итерации:

```text
Grandgousier_0.7.0-A_clean_schema_foundation_patch.zip
```

rootless overlay, без лишних README/release-note/handoff файлов, с обновлением canonical context docs только если patch реально меняет состояние/архитектуру.
