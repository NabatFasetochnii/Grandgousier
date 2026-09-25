## Current source candidate (2026-09-24) — IMPLEMENTED_NOT_REAL_VALIDATED

- HEAD base `4e2daab`, source candidate version 0.7.0.10 (uncommitted). New Stage-2 runs default to `adaptive_ellipse` per star; `circular` remains explicit. Adaptive base radius is 3 pixels, capped at 2.5×, with the existing flagged circular fallback. No Stage-2/Stage-3 recipe formula changes.
- Stage-1 handoff inherits the archive-path default. Effective estimator is persisted in `pipeline_runs.recipe_json` through `FieldPipelineConfig`; the frozen `stage3_scale_v1` still requires its other scientific settings. No real science/DB validation of this candidate; no DB writes or reruns authorized here.
- Older dated sections below are historical snapshots, not current execution instructions.

## 0.7.0.9 clean SCIENCE parity hotfix (2026-09-21)

- Full GPX clean rebuild Stage2 is now complete through 2018-09-23 in `grandgousier_070_gpx_full`; 0.7.0.8 successfully resumed only failed series 5 with the validated 20-reference ensemble threshold and processed all remaining full dates.
- The archive workflow then failed before Stage3 in `science_materialization_parity_report`: that compatibility verifier still queried removed legacy table `science_measurements`, even though the clean 0.7 schema intentionally stores final durable state in narrow `science_photometry`.
- 0.7.0.9 makes the parity gate schema-aware. On clean 0.7 it compares normalized reconstruction against `science_photometry` joined to immutable BASE `measurements` for identity/extraction geometry; the frozen wide-table path remains only as a pre-0.7 fallback.
- No schema or scientific calibration change. Do not rerun Stage2: after Windows validation, rerun the same `process-field-archive --resume --execute`; all full dates should be `SKIP_READY` and orchestration should proceed directly to Stage3 V/R.

## 0.7.0.8 GPX ensemble threshold + resumable field-archive hotfix (2026-09-21)

- Full clean rebuild database: `grandgousier_070_gpx_full`, PostgreSQL 18.4, owner `grandgousier`, clean 46-table schema, 32 baselined migrations.
- 2018-09-07 completed under `stage3_scale_v1`: 272 frames, 4 series (25+25+111+111), all pipeline runs DONE.
- First archive-wide execute stopped on 2018-09-10 only because series 5 (V, 29 frames) had 22 stable ensemble references versus the new-run threshold 30; sibling series 6/7/8 completed. Historical validated Stage-2 semantics accept threshold 20, and the existing runner regression explicitly models the GPX 22-reference shape as valid at 20.
- 0.7.0.8 restores canonical `stage3_scale_v1` threshold 20 while retaining compatibility for already-produced threshold-30 series. It also fixes `process-field-archive --resume` to rerun only existing incomplete series explicitly; ordinary archive-path execution intentionally does not rerun represented observations.
- Schema unchanged. Do not recreate `grandgousier_070_gpx_full`; after Windows validation, run plan-only, confirm 2018-09-10 is `RESUME_INCOMPLETE` with resume series 5, then execute with `--resume`.

## 0.7.0.7 durable SCIENCE publication-order hotfix (2026-09-20)

- Clean GPX_TF17_2 seed Stage2 completed successfully on 0.7.0.6 for both 25-frame V/R series (`DONE`, `science_status=OK`), producing 100,000 immutable BASE measurements.
- Read-only seed storage audit exposed 200,000 `science_photometry` rows: the expected 100,000 rows on the two current SCIENCE products plus 100,000 stale rows on two `INTERMEDIATE / SUPERSEDED_IMMUTABLE` ensemble generations created by earlier failed runs.
- Root cause: `materialize_science_photometry` ran before late candidate-light-curve export. Because pipeline steps commit independently, a later failure could leave durable narrow rows attached to a product that never became final SCIENCE.
- 0.7.0.7 moves durable SCIENCE publication to the final fallible science step, after candidate export; SCIENCE-role stamping and durable materialization now share one transaction. The durable writer refuses any target that is not `SCIENCE / COMPLETE_IMMUTABLE` and prunes stale non-SCIENCE durable rows in the same series on successful publication.
- Schema and migration ledger are unchanged. Existing invalid seed rows are repaired separately by a guarded fresh-DB cleanup; frozen production remains untouched.

## 0.7.0.6 candidate light-curve PostgreSQL transaction hotfix (2026-09-20)

- Fresh GPX_TF17_2 Stage2 reached forced photometry, aperture-ZP, ensemble calibration and object QC successfully on both seed series, but final candidate-light-curve export swallowed a PostgreSQL/SQLAlchemy database exception per candidate. PostgreSQL then left the transaction aborted and the recorder surfaced only a misleading `InFailedSqlTransaction` while finishing `pipeline_run_steps`.
- Candidate export now treats `SQLAlchemyError` as an infrastructure failure: it propagates to the pipeline runner, which rolls back the step and records the primary database exception. Non-database per-candidate export/plot failures remain diagnostic-only.
- The light-curve reader no longer uses raw PostgreSQL `ANY(:calibration_names)`/`ANY(:cv_ids)` array binds or nullable `:algorithm IS NULL` predicates on this path. Calibration names use SQLAlchemy expanding `IN`; interseries source IDs use an expanding bind and separate algorithm/no-algorithm branches.
- `resolve_light_curve_scope()` now passes an actual `CalibrationVersion` into `_calibration_chain_names()` instead of an integer ID; the old `AttributeError` was silently swallowed and source-chain aliases were therefore lost.
- Schema and migration ledger are unchanged. Fresh DB `grandgousier_070_fresh` must not be recreated. 0.7.0.6 is a runtime/read-path hotfix over the existing clean schema.

## 0.7.0.5 same-catalog object-association hotfix (2026-09-20)

- 0.7.0.4 is Windows/CPython 3.11 REAL-VALIDATED (`723 passed, 1 skipped`, release package/install smoke OK).
- Fresh GPX_TF17_2 Stage2 reached forced aperture on both 25-frame series and exposed a Phase-C identity defect: two distinct catalog sources from the same catalog namespace could be auto-associated to one internal Object when separated by <0.75 arcsec. That produces duplicate BASE keys `(object_id, frame_id, calibration_version_id)` and PostgreSQL `uq_measurement_version` failure.
- 0.7.0.5 forbids astrometric auto-merge when the candidate Object already has any link to the same `catalog_name`; exact existing catalog links remain authoritative, and cross-catalog unique astrometric association remains allowed. This preserves close pairs rather than masking them with measurement UPSERT.
- No schema migration or production/frozen DB operation is required. The fresh rebuild DB remains the active smoke target.


## 0.7.0.4 fresh pipeline-recording hotfix (2026-09-20)

- Fresh PostgreSQL bootstrap for `grandgousier_070_fresh` completed successfully on 0.7.0.3: 46/46 tables, 32/32 migrations BASELINED, pending=0, checksum drift=0, doctor READY.
- First GPX_TF17_2 seed execute indexed 50 MASTER frames and built two 25-frame series, then exposed a clean-schema/runtime contract defect before photometry: raw SQL INSERT into `pipeline_runs` omitted non-null `summary_json`; `pipeline_run_steps` would analogously omit non-null `result_json`.
- 0.7.0.4 initializes both required JSON payloads explicitly in raw SQL and rolls back failed per-series transactions before continuing to another series. No science formulas or schema columns changed.
- The old production database remains frozen reference; `grandgousier_070_fresh` is the clean rebuild target.
# Grandgousier — текущий контекст

Актуально на **2026-09-21**. Текущий source-кандидат: **0.7.0.8**, hotfix валидированного ensemble threshold и точечного `RESUME_INCOMPLETE` для full GPX rebuild. Phase A–D и 0.7.0.4 REAL-VALIDATED на Windows/CPython 3.11; последний validated source baseline — **0.7.0.4** (`723 passed, 1 skipped`). Validated seed DB `grandgousier_070_fresh` сохранена; full rebuild идёт в `grandgousier_070_gpx_full`; frozen production/reference DB остаётся **0.6.4.5.1**.

## 1. Текущее решение

Продолжать точечную forensic-очистку старой production БД признано нерациональным. Старая БД `grandgousier` теперь рассматривается как **frozen scientific/reference database** и не должна подвергаться дальнейшим destructive cleanup/refactor операциям без отдельного явного решения.

Новая ветка 0.7 должна сначала исправить саму структуру хранения, затем поднять **новую пустую БД**, прогнать на ней `GPX_TF17_2` с нуля, сравнить science acceptance со frozen baseline, проверить idempotency, и только после этого начинать archive-wide rebuild.

Ключевой порядок:

```text
0.6.4.5.1 frozen reference DB
        ↓
0.7.0-A clean schema foundation — WINDOWS REAL-VALIDATED, production DB не менялась
        ↓
0.7.0-B detection storage normalization — WINDOWS REAL-VALIDATED
        ↓
0.7.0-C physical/catalog identities + frame locations — WINDOWS REAL-VALIDATED
        ↓
0.7.0-D normalized reference photometry — IMPLEMENTED SOURCE CANDIDATE
        ↓
fresh DB bootstrap через init-db
        ↓
GPX_TF17_2 clean smoke rebuild
        ↓
old/new acceptance + idempotency comparison
        ↓
полная повторная обработка исходных наблюдений
        ↓
архивирование/удаление старой БД только после отдельного подтверждения
```

## 2. Frozen production baseline

Production snapshot снят перед clean-rebuild решением:

```text
reports/db_pre_clean_rebuild_20260919/database_structure.json
PostgreSQL 18.4, Windows x86_64
DB size: 45,726,176,959 bytes ≈ 43 GB
```

Точные основные счётчики:

```text
frames                               7,323
observations                         7,230
field_series_frames                  7,271
field_series                            93
measurements                    11,888,095
science_measurements                     0
science_photometry_cache        10,151,494
science_flag_override_sets              104
science_flag_overrides              917,220
calibration_versions                    720
calibration_product_aliases              642
calibration_iterations                  591
calibration_frame_terms              40,667
calibration_iteration_objects       337,831
object_qc                           313,619
source_detections                 6,925,992
detection_catalog_matches         5,573,857
frame_catalog_links               8,486,095
reference_photometry                630,738
catalog_sources                     514,783
```

Крупнейшие физические relations:

```text
measurements                         ≈25 GB
source_detections                    ≈5.98 GB
detection_catalog_matches            ≈4.57 GB
frame_catalog_links                  ≈3.72 GB
reference_photometry                 ≈1.20 GB
science_photometry_cache             ≈0.89 GB
object_qc                            ≈0.67 GB
catalog_sources                      ≈0.65 GB
science_flag_overrides               ≈0.19 GB
```

`measurements` остаётся главным storage target:

```text
BASE_PHOTOMETRY / BASE / COMPLETE_IMMUTABLE        10,088,095 rows / 115 CV
ENSEMBLE / INTERMEDIATE / SUPERSEDED_IMMUTABLE        900,000 rows / 18 CV
ENSEMBLE / INTERMEDIATE / COMPLETE_IMMUTABLE          600,000 rows / 12 CV
LEGACY_UNCLASSIFIED / INTERMEDIATE / MATERIALIZING     200,000 rows / 4 CV
ENSEMBLE / SCIENCE / COMPLETE_IMMUTABLE                100,000 rows / 2 CV
```

Итого historical non-BASE `measurements` = **1,800,000 rows**.

## 3. SCIENCE storage refactor — завершённый этап 0.6.3–0.6.4

До refactor production DB была примерно 72 GB, а wide `science_measurements` занимала около 31 GiB.

Доказано и real-validated:

- scalar reconstruction из immutable BASE + normalized calibration terms работает для 104 SCIENCE products / 10,151,494 rows;
- exact historical effective flags нельзя было считать автоматически восстановимыми, поэтому 0.6.4.3 добавил calibration-common + sparse residual override layer;
- field 33: все 36 materialized products / 7,211,538 V1 rows прошли all-row scalar/identity/flag verification;
- 0.6.4.4 retirement field 33 удалил эти V1 rows, acceptance остался `READY`;
- 0.6.4.5.1 исправил nullable global-scope SQL bind и позволил безопасно обработать remaining products;
- global retirement завершён: все оставшиеся materialized SCIENCE products backfill/override/verify/retire прошли exact gate;
- после logical retirement `science_measurements = 0`;
- затем выполнен guarded `TRUNCATE TABLE science_measurements` **без `RESTART IDENTITY` и без `CASCADE`**, только после `COUNT(*)=0`;
- frozen acceptance после TRUNCATE остался `READY`;
- текущие compact counts: `science_photometry_cache=10,151,494`, `science_flag_override_sets=104`, `science_flag_overrides=917,220`;
- DB уменьшилась примерно с 72 GB до 43 GB.

Schema state после этого этапа:

```text
managed migrations total = 28
applied                  = 12
baselined                 = 16
pending                    = 0
checksum drift             = 0
```

## 4. Frozen science acceptance GPX_TF17_2

Dataset: `gpx_tf17_2`, field 33.

```text
frames=7323
observations=7230
field_series_frames=7271
field33_series=20
multiple_observation_representatives=41
field33 interseries generations=12
max_generation_id=21
```

Stage 2 recipe `stage3_scale_v1`:

```text
series total=20
READY=18
eligible pairs=72
largest connected component=9
series 74/75 intentionally MISSING_PROVENANCE / too small
```

Stage 3:

```text
V generation=20, ref=88
series=[76,78,79,82,84,86,88,90,92]
edges=35, dof=27
reduced_chi2=1.3814557112577586
verification=OK
reliability=REDUNDANT_GRAPH_OK

R generation=21, ref=89
series=[77,80,81,83,85,87,89,91,93]
edges=36, dof=28
reduced_chi2=3.4106354601814393
verification=OK
reliability=REDUNDANT_GRAPH_OK
```

M5 controls:

```text
object 29786:
  source=2640
  accepted=2578
  rejected=62
  rejection reasons=22 NO_VALID_MAG + 40 SATURATED_OR_NEAR_SATURATED

object 30112:
  source=2640
  accepted=2618
  rejected=22
  rejection reasons=22 NO_VALID_MAG
```

Этот baseline нельзя менять в schema/storage work без отдельного научного обоснования.

## 5. Permanent domain rules

Stage 1 = archive/source discovery/materialization/reduction -> processed FITS.
Stage 2 = single-series photometry/calibration.
Stage 3 = inter-series calibration/global light curves.

Physical observation identity:

- MASTER: authoritative `UNIQNUM`;
- RoboPhot: `SERNUM + DATE-OBS`;
- RoboPhot FITS `DATE-OBS` **уже UTC**; никогда не добавлять `+05:00`;
- paths/URLs — mutable locators, не identity;
- P4 `Observation` identity authoritative;
- historical multiple Frame representatives не удалять и не плодить автоматически.

Stage-2/Stage-3 production-v1 formulas и selection semantics frozen до отдельного science change + real acceptance.

## 6. Что показал forensic audit оставшихся 1.8M non-BASE measurements

Прямых FK на candidate `measurement_id` из `event_frame_coverage`, `science_photometry_cache`, `science_flag_overrides` и уже пустой `science_measurements` не найдено.

Но metadata/runtime dependencies делают простой DELETE небезопасным:

- 14 candidate CV были current alias heads;
- 16 candidate CV использовались как `source_calibration_version_id` потомками;
- direct runtime readers `measurements` по calibration version всё ещё существуют;
- 12 detached candidate CV (600k rows) дополнительно проверены reconstruction verifier.

Detached CVs:

```text
824 828 845 847 851 853 856 859 863 865 869 871
```

Все 12 дали `MISMATCH`:

- `824, 828, 856, 859`: `INSUFFICIENT_NORMALIZED_STATE`, unsupported `LEGACY_UNCLASSIFIED`;
- `845, 847, 851, 853, 863, 865, 869, 871`: `NO_NORMALIZED_ITERATION` для самого materialized ENSEMBLE stage;
- row identity/count совпадают, но `mag_cal` и `local_zp_correction` отличаются; это не lossless reconstructable state.

Следовательно, **не удалять 1.8M historical rows из frozen DB** и не строить 0.7 migration вокруг их починки. Старую БД оставить reference corpus.

## 7. Проверенные факты текущего кода 0.6.4.5.1

### Fresh bootstrap

`grandgousier/db/init_db.py`:

1. определяет действительно свежую schema;
2. вызывает `Base.metadata.create_all(engine)`;
3. проверяет schema contract;
4. baseline-ит все известные migrations через `baseline_current_schema(..., reason="fresh_orm_init")`.

Для новой пустой БД intended path = **`python -m grandgousier.cli init-db`**, а не replay legacy migrations через `migrate-db`.

### Intermediate calibration storage

`PipelineConfig.materialize_intermediate_calibrations = False` по умолчанию. Derived stages получают `NORMALIZED_TERMS`; `FULL_MEASUREMENTS` включается только explicit compatibility option.

Значит исторические 1.8M non-BASE rows **не должны повторно возникнуть** при clean rebuild на текущих defaults.

### SCIENCE writer

Normal finalization пишет `science_photometry_cache`; wide `science_measurements` вызывается только при explicit `materialize_intermediate_calibrations` compatibility/debug mode.

Это хорошая основа, но `science_photometry_cache` сейчас хранит только `(calibration_version_id, base_measurement_id, mag_cal, mag_err_random)` и формально считается rebuildable cache. В clean 0.7 нужен durable final science entity с чётким контрактом.


## 7A. Реализованный 0.7.0-A source contract

Phase A реализована в source tree 0.7.0 без операций с frozen production DB:

- fresh ORM/schema contract содержит `science_photometry` и не создаёт `science_measurements`, `science_photometry_cache`, historical flag-override tables или legacy `interseries_calibration_offsets`; forensic mappings этих 0.6 relations отделены от `Base.metadata`;
- `measurements.calibration_version_id` обязателен, а low-level writers fail-closed разрешают записи только в calibration product `BASE_PHOTOMETRY / BASE / FULL_MEASUREMENTS`;
- normal Stage-2/spatial paths больше не могут материализовать derived full copies в `measurements`;
- durable `science_photometry` хранит только calibration-changing scalars: `flux`, `flux_err`, `mag_inst`, `mag_cal`, `mag_err`, `mag_err_random`, `mag_err_systematic`, `local_zp_correction`; object/frame/extraction identity наследуется через `base_measurement_id`;
- Stage 3, M5/global light curves и current light-curve reads переведены на durable SCIENCE view;
- `field_series_frames` сохраняет только uniqueness `(series_id, frame_id)`; grouping method включён в reproducible series identity; readers, которым важна membership, используют `(frame_id, series_id)`, а не предполагают глобально уникальный frame;
- `calibration_frame_terms` и `calibration_iteration_objects` больше не дублируют `calibration_version_id`; identity выводится через `calibration_iteration_id`;
- scientific-lineage FK в затронутых core entities переведены на `RESTRICT`, где `SET NULL` уничтожало бы provenance;
- `init-db` разрешён только для пустой либо уже полной clean 0.7 schema; наличие retired 0.6 compatibility tables или partial clean schema блокирует mutation; `migrate-db` также fail-closed отказывается преобразовывать 0.6 schema in-place;
- public 0.6 SCIENCE retirement/backfill materialization commands и `--materialize-intermediate-calibrations` удалены из active CLI 0.7.

0.7.0-A закрыта как Windows/CPython 3.11 REAL-VALIDATED (`704 passed, 1 skipped`). Phase B также закрыта как REAL-VALIDATED в 0.7.0.1 (`709 passed, 1 skipped`). Phase C закрыта как REAL-VALIDATED в 0.7.0.2 (`714 passed, 1 skipped`). Phase D реализована в source 0.7.0.3 и ожидает Windows gate; fresh DB bootstrap остаётся запрещён до этого gate.


## 7B. Реализованный 0.7.0.1 Phase B — Windows REAL-VALIDATED

Phase B нормализовала detection storage без изменения detection/matching science algorithms:

- `detection_runs` хранит run-level algorithm/version/config/fingerprint/pipeline provenance;
- `detection_run_frames` хранит frame scope/status/common diagnostics, включая zero-result frames;
- `source_detections` хранит только row-specific measured quantities/status/flags + run-frame identity;
- `detection_catalog_matches` выводит frame/catalog/run provenance через normalized graph, но сохраняет исторические photometric snapshots `detection_mag_inst`, `catalog_mag`, `zero_point`;
- active readers/writers/QC/events/idempotency/cleanup/CLI cut over на новый слой;
- `frame_catalog_links` намеренно не slimmed до post-A–D real PostgreSQL EXPLAIN/physical benchmark.

Windows/CPython 3.11 validation: `709 passed, 1 skipped`, `compileall` OK, release packaging/install smoke/checksums OK; `READY_WITH_WARNINGS` только из-за не переданных validated constraints/snapshot. Production DB не менялась.

## 7C. 0.7.0.2 Phase C — Windows REAL-VALIDATED

Phase C реализована source-level без операций с frozen production DB:

- `objects.object_uid` — opaque internal UUID (`INTERNAL_UUID_V1`), не hash catalog/source ID/coordinates;
- explicit `object_catalog_links` связывает внутренний Object с `catalog_sources` и хранит association method/separation/comparison epoch/evidence;
- association policy conservative: exact existing catalog link -> unique astrometric candidate within 0.75 arcsec с epoch/proper-motion propagation -> новый Object; разные source IDs одного `catalog_name` никогда не auto-merge-ятся; неоднозначность **не** auto-merge-ится;
- Gaia/external identifiers больше не являются колонками `objects` и не определяют internal identity;
- `frame_locations` отделяет mutable path/URI и file metadata от `frames`; fresh `frames` больше не содержит `raw_path/file_size_bytes/file_mtime_utc`;
- Python compatibility accessors `frame.raw_path/file_*` работают через primary location, чтобы scientific readers не дублировали locator logic;
- один Frame может иметь несколько locations; moved/copied file при совпадающем physical observation + product consistency не создаёт новый physical observation/frame;
- Stage-1 SHA-256 может записываться как `frame_locations.content_sha256`, но content fingerprint не заменяет MASTER `UNIQNUM` или RoboPhot `SERNUM+DATE-OBS` physical identity;
- fresh-only migration marker `mvp7_0_2_identity_locations` fail-closed и не конвертирует существующую 0.7.0.1/0.6 schema in-place;
- fresh schema contract: 44 ORM tables, managed migrations total 31.

Windows/CPython 3.11 gate: targeted regressions `3 passed`, полный pytest `714 passed, 1 skipped`, `compileall` OK, release check `READY_WITH_WARNINGS`; warnings только из-за не переданных validated constraints/snapshot. Production DB не менялась.

## 8. Одобренный clean-schema target 0.7

Не делать destructive in-place migration старой 43 GB БД. 0.7 — fresh-baseline release для новой archive DB.

### 0.7.0-A — clean schema foundation

До создания новой БД:

- `measurements` становится BASE-only immutable storage;
- `calibration_version_id` для BASE rows должен быть `NOT NULL`;
- normal code path не должен создавать intermediate/science full measurement copies;
- `science_photometry_cache` заменить/продвинуть до durable narrow `science_photometry` с scientifically changing scalar state;
- historical `science_measurements` в fresh schema не нужен;
- historical `science_flag_override_sets/science_flag_overrides` в fresh schema не нужны как обычная модель; это 0.6 compatibility mechanism;
- `field_series_frames`: убрать global `UNIQUE(frame_id)`, оставить membership uniqueness `(series_id, frame_id)` и explicit grouping semantics;
- `calibration_frame_terms` и `calibration_iteration_objects`: убрать redundant `calibration_version_id` или обеспечить composite integrity; preferred clean layout — identity через `calibration_iteration_id`;
- пересмотреть provenance FK: scientific lineage предпочтительно `RESTRICT/NO ACTION`, а не silent `SET NULL`;
- schema contract + models + tests должны описывать fresh 0.7 layout без старых compatibility tables.

### 0.7.0-B — detection storage

- добавить `detection_runs`;
- вынести одинаковые `algorithm + parameters` из миллионов `source_detections` rows;
- оставить в detection row только row-specific measured values/status/flags;
- сохранить научный transient/unusual-variable слой, не заменять его losslessly непроверенным artifact-only storage;
- slim `detection_catalog_matches`/`frame_catalog_links` только после проверки real readers и EXPLAIN.

### 0.7.0-C — identities and locations

- object identity должна быть catalog-independent;
- добавить explicit `object_catalog_links`;
- Gaia/external IDs не должны определять Grandgousier physical object identity;
- отделить frame/product identity от file locator;
- добавить `frame_locations` и optional content fingerprint;
- observation identity rules MASTER/RoboPhot остаются authoritative.

### 0.7.0-D — normalized reference photometry — IMPLEMENTED SOURCE CANDIDATE 0.7.0.3

- fresh schema больше не создаёт wide `reference_photometry`; frozen pre-D mapping остаётся только в `LegacyBase` для forensic reads;
- `reference_photometry_catalogs` сохраняется как registry, `photometric_bands` хранит band definitions per reference catalog, `reference_photometry_sources` — source astrometry/identity/provenance, `reference_photometry_points` — per-band magnitude/error;
- importer принимает новые band keys без ALTER TABLE; повторный импорт source полностью заменяет его point set, сохраняя прежнюю wide-row idempotency semantics;
- Gaia-specific DB columns заменены generic identity-catalog linkage, но CLI/calibration compatibility payload сохраняет старые `gaia_*`/`mag_*` поля там, где это требуется текущему public/science API;
- color calibration, diagnostics, catalog summary, object lookup и auto-process band coverage переведены на normalized readers;
- fixed-band wide SQL/ORM reads отсутствуют в active runtime;
- fresh-only marker `mvp7_0_3_reference_photometry_normalization` fail-closed запрещает in-place conversion существующей 0.7.0.2/0.6 БД;
- fresh contract: 46 ORM tables, 32 managed migrations.

Source gate в доступной среде: targeted D/schema/color suite `80 passed, 1 skipped`; broad `619 passed, 42 skipped` при исключении единственного Gaia transport test из-за отсутствующего `astropy`; `compileall` OK. Windows full pytest/release gate обязателен перед fresh DB bootstrap.

## 9. После A–D: clean rebuild gate

Только после source-level refactor и тестов:

1. определить реальный connection source без вывода password;
2. создать отдельную DB, например `grandgousier_clean_20260919` или новую датированную;
3. bootstrap через `init-db`;
4. `schema-status` + `doctor --strict-optional`;
5. убедиться, что все science/data tables пусты и schema соответствует 0.7 contract;
6. обработать только `GPX_TF17_2` из исходных/raw inputs;
7. сравнить old/new science baseline, не raw numeric IDs;
8. повторить тот же clean run и доказать idempotency/no duplicate materialization;
9. снять storage profile после одного поля;
10. только если всё READY — переходить к archive-wide rebuild.

Не сравнивать `calibration_version_id`, `object_id`, `series_id` как обязательные равные числа между old/new DB: на fresh DB surrogate IDs закономерно могут отличаться. Сравнивать logical identities, counts, fingerprints, series membership, science values и acceptance semantics.

## 10. Структурные замечания к текущей БД, которые 0.7 должен учесть

- 44 таблицы, 239 indexes, 587 constraints, 40 sequences, triggers отсутствуют.
- `measurements.flags` JSONB/TOAST остаётся одним из главных BASE storage costs; после clean smoke измерить повторяемость flags и только затем проектировать BASE flag factorization.
- `source_detections` repetition устранена в 0.7.0.1 Phase B; physical/query benefit ещё должен быть измерен на fresh PostgreSQL smoke.
- `field_series_frames` many-to-many constraint исправлен в Phase A; fresh workload/idempotency ещё должен подтвердить operational semantics.
- catalog-dependent object UID устранён в 0.7.0.2: internal UID opaque, catalog provenance вынесен в `object_catalog_links`; policy намеренно conservative при неоднозначном astrometric match.
- redundant calibration identity в `calibration_frame_terms`/`calibration_iteration_objects` устранена в Phase A.
- `frames.raw_path` как DB column устранён в 0.7.0.2; mutable locators хранятся в `frame_locations`, а instance compatibility accessor остаётся только API adapter.
- frozen 0.6 `reference_photometry` — wide fixed-band reference table; current 0.7.0.3 fresh schema использует normalized catalog/band/source/point model.
- `event_frame_coverage` одновременно держит measurement/calibration refs и copied `mag_cal/mag_err`; в clean model event evidence должен ссылаться на immutable science state либо явно versioned snapshot.
- некоторые UNIQUE включают nullable columns; на PostgreSQL 18 требуется явная desired NULL semantics (`NULLS NOT DISTINCT`, partial/generated identity или иной guard).
- `pg_stat_user_tables.n_live_tup` в старой БД сильно stale для ряда tables; exact counts и planner estimates не путать. После bulk rebuild делать `ANALYZE`/контролировать autovacuum-analyze.
- large `pipeline_run_steps.result_json`/run JSON может уходить в TOAST; большие diagnostics/log payloads лучше registry artifact, а в DB — compact searchable summary.

## 11. Ошибки предыдущей работы, которые нельзя повторять

Это обязательная часть handoff.

1. **Не угадывать имена колонок.** Были ошибочные предположения:
   - `cv.calibration_name` вместо реального `cv.name`;
   - `cv.parent_calibration_version_id` — такого столбца нет;
   - `calibration_product_aliases.calibration_version_id` вместо реального `head_calibration_version_id`.
   Перед query неизвестной schema сначала `information_schema.columns` / `pg_constraint` или source models.

2. **Не делать вывод “можно удалять” только по отсутствию FK.** Detached 600k rows выглядели orphan-like, но reconstruction verifier доказал `MISMATCH` для всех 12 CV.

3. **Не считать scalar parity равной historical diagnostic parity.** Это уже было опровергнуто CV1094 и потребовало exact flag override layer.

4. **Не предполагать наличие `rg` на Windows.** На рабочей машине `rg` не установлен. Использовать PowerShell `Get-ChildItem | Select-String` или Python.

5. **Не подключаться к administrative DB с production app credentials “по аналогии”.** Попытка создать clean DB через connection к `postgres` под user `grandgousier` закончилась authentication failure. Никаких изменений сделано не было. Следующий чат сначала должен выяснить реальный connection source/admin procedure, не менять users/pg_hba и не просить password в чат.

6. Psycopg nullable bind pitfall: `(:field_id IS NULL OR cv.field_id = :field_id)` с `None` уже давал `AmbiguousParameter`. Для global/scoped selectors использовать отдельные SQL statements.

7. SQLAlchemy bind cast pitfall: `:cv::bigint` может быть разобран неверно. Использовать `CAST(:cv AS bigint)`.

8. В данном PostgreSQL окружении ранее отсутствовала ожидаемая `jsonb_object_length(jsonb)`; считать keys через `jsonb_object_keys` при необходимости.

9. Всегда qualify columns в сложных joins: уже был `AmbiguousColumn` в reconstructability audit.

10. TEMP physical probes требуют осторожности с JSONB detoast; capped equal samples — layout comparison, не автоматическая global weighted saving estimate.

11. Не менять frozen science formulas/selection semantics ради storage cleanup.

12. Не выполнять destructive DB действия без явного согласия пользователя и отдельного preflight/count/acceptance gate.

## 12. Документационная дисциплина

Не создавать новые per-patch README, release-note Markdown, handoff Markdown или next-chat Markdown внутри проекта.

Canonical files:

```text
README.md
README_PUBLIC.md
CONTEXT.md
docs/CURRENT_ARCHITECTURE.md
docs/ROADMAP.md
docs/PROJECT_HISTORY.md
docs/user_manual_ru/
context/project_state.yaml
```

Пользовательский LaTeX manual обновлять только если меняется public CLI/workflow/config/user-visible behavior. Schema-only internal refactor сам по себе manual change не требует.

Legacy `NEXT_CHAT_PROMPT.md` в старом tree считать deprecated pointer, а не источником текущего состояния.

## 13. Что читать в начале следующего чата

Обязательно:

```text
CONTEXT.md
docs/ROADMAP.md
docs/CURRENT_ARCHITECTURE.md
context/project_state.yaml
```

Затем source-level audit минимум:

```text
grandgousier/db/models.py
grandgousier/db/schema_contract.py
grandgousier/db/init_db.py
grandgousier/db/migrations.py
grandgousier/db/session.py
grandgousier/calibration/reconstruction.py
grandgousier/photometry/calibration_view.py
grandgousier/photometry/science_cache.py
grandgousier/photometry/science_materialization.py
grandgousier/photometry/science_flag_overrides.py
grandgousier/photometry/science_retirement.py
grandgousier/pipeline/runner.py
grandgousier/archive/auto_process.py
grandgousier/archive/fields_series.py
grandgousier/photometry/forced_aperture.py
grandgousier/photometry/source_detection.py
grandgousier/db/detection_io.py
grandgousier/db/catalog_link_io.py
grandgousier/lightcurves/product.py
grandgousier/lightcurves/global_object.py
grandgousier/interseries/planning.py
grandgousier/interseries/analysis.py
grandgousier/interseries/persistence.py
grandgousier/events/registry.py
grandgousier/services/acceptance.py
grandgousier/cli.py
```

И relevant tests вокруг schema/bootstrap/pipeline/reconstruction/Stage3/acceptance.

Если доступны companion audit artifacts, использовать:

```text
Grandgousier_0.7.0_CLEAN_SCHEMA_SPEC.md
Grandgousier_0.7.0_table_audit.csv
database_structure.json
measurements_detached_reconstruction_064501.txt
measurements_reader_context_064501.txt
acceptance_gpx_tf17_2_064501_global_retired.json
```

## 14. Ближайшая конкретная задача

**Не создавать новую БД и не удалять старую.**

0.7.0-A реализован как source candidate и должен пройти Windows/Python 3.11 validation после overlay. Следующий engineering deliverable: **0.7.0-B detection storage normalization** поверх 0.7.0-A. Production/reference DB 0.6.4.5.1 остаётся frozen; fresh PostgreSQL DB создавать только после завершения A–D.
