# Grandgousier

Версия: **0.7.0.1**

Grandgousier - Python-пакет для индексирования FITS-архивов, forced-aperture фотометрии, локальной и ensemble-калибровки, reference/color calibration, QC объектов, проверки event coverage и отбора кандидатов в переменные объекты.

Текущий source-кандидат: 0.7.0.1 (Phase B detection storage normalization). Phase A (`0.7.0`) user-real-validated на Windows/CPython 3.11 (`704 passed, 1 skipped`); последний production runtime и frozen reference DB остаются на 0.6.4.5.1. 0.7.x предназначен только для новой пустой БД после завершения Phase A–D: in-place migration frozen 0.6 database запрещена. Phase B факторизует повторяющиеся detection algorithm/config/provenance в `detection_runs` и frame-level status/diagnostics в `detection_run_frames`; high-cardinality `source_detections` и `detection_catalog_matches` оставляют только row-specific state, при этом исторические photometric match snapshots сохраняются. `frame_catalog_links` намеренно не ужимается до реального PostgreSQL EXPLAIN/physical benchmark. Stage 2/Stage 3 production-v1 scientific formulas не меняются. См. `CONTEXT.md`, `docs/CURRENT_ARCHITECTURE.md` и `docs/ROADMAP.md`.

0.6.4.0 вводит additive `science_photometry_cache`: rebuildable Stage-3 cache только из `calibration_version_id`, `base_measurement_id`, `mag_cal`, `mag_err_random`. Он не является новым authoritative science layer. Полный science state по-прежнему определяется immutable BASE + normalized calibration terms; V1 `science_measurements` пока сохраняется как compatibility baseline и не удаляется этим патчем.

0.6.4.1 переводит M5/global-lightcurve и current alias-head lightcurve reads на полный `science_photometry_cache` + normalized reconstruction. Выбор cache допускается только при доказанной полноте; иначе сохраняется V1 fallback. Этот patch не меняет схему и не удаляет `science_measurements`.

0.6.4.2 прекращает штатную запись новых wide `science_measurements`: финализация SCIENCE по умолчанию пишет только reconstructable `science_photometry_cache`, а широкая V1 materialization выполняется лишь в существующем compatibility/debug режиме `--materialize-intermediate-calibrations`. Historical V1 rows не удаляются этим patch.

0.6.4.3 добавляет additive exact historical flag compatibility layer: calibration-common patch хранится один раз в `science_flag_override_sets`, а `science_flag_overrides` содержит только остаточные per-BASE различия. `backfill-science-flag-overrides` выполняет all-row factoring/proof, `verify-science-flag-overrides` независимо проверяет exact scalar/identity/flag parity перед будущим retirement V1. Сам 0.6.4.3 ничего из `science_measurements` не удаляет.

0.6.4.4 adds controlled retirement of legacy wide `science_measurements`. `retire-science-measurements` re-runs the exact all-row scalar/identity/flag proof, requires row-for-row compact cache coverage and a `COMPLETE_EXACT` historical override manifest, then deletes only that calibration's V1 rows and marks its manifest `RETIRED_EXACT`. Cache completeness after retirement is checked against the preserved manifest `source_row_count`, so a partial cache can never become authoritative merely because V1 is gone. The command does not run `VACUUM FULL`, `pg_repack` or any physical table rewrite.


## Production service/job layer

Начиная с 0.5.51.0 `process-field-archive` использует единый `grandgousier.services` layer: typed YAML/JSON/env/CLI configuration, readiness preflight, PostgreSQL advisory lock от конкурентного запуска того же field/catalog/root scope, JSONL job events и failure/summary artifacts. Научный M8/Stage 2/M0--M5 код не дублируется и не меняется. Отдельная команда `field-archive-readiness` проверяет DB/schema/resources/output root без запуска M8 и без автоматических migrations.

Начиная с 0.5.52.0 P4a вводит отдельную path-independent physical-observation identity: новая таблица `observations` и nullable `frames.observation_id` позволяют нескольким file/processing products ссылаться на одну физическую экспозицию. MASTER scheme v1 использует FITS `UNIQNUM`; RoboPhot scheme v1 использует FITS `SERNUM + DATE-OBS`, причём RoboPhot `DATE-OBS` уже является UTC и не сдвигается на локальный часовой пояс. `backfill-observation-identity` по умолчанию строит read-only plan и пишет только при явном `--execute`; `frame_id`/legacy `frame_uid` и Stage 2/M0--M5/M8 semantics не меняются.

Начиная с 0.5.53.0 P4b делает штатный FITS indexer observation-aware: новый Frame автоматически создаёт или повторно использует соответствующий `Observation`, а повторная индексация exact legacy frame проверяет и при необходимости восстанавливает отсутствующую связь. Копии/перемещения и alternate processed products сохраняются отдельными Frame rows, если их legacy `frame_uid` различается, но используют один `observation_id` при совместимых физических metadata. Missing/invalid stable identity не заменяется guessed fallback; новый Frame остаётся unlinked с warning. Hard physical conflict является ошибкой ingestion. Downstream Stage 2 всё ещё работает по Frame rows; representative-product selection остаётся отдельным P4c.

Начиная с 0.5.54.0 P4c ставит observation-level guard перед созданием новых `field_series_frames`: если Observation уже представлена в series, alternate Frame не создаёт вторую science epoch; если несколько unassigned products существуют до выбора representative, они блокируются как `AMBIGUOUS_MULTIPLE_PRODUCTS` без эвристики `MIN(frame_id)`/mtime/path/size. Историческое наличие нескольких representatives обозначается отдельно и не чинится автоматически. `process-archive-path` показывает эту policy уже в plan-only режиме; при `--manifest-out` пишет подробные JSON rows и sibling `*_observation_selection.csv`.

Начиная с 0.5.54.1 replay обычного `process-archive-path` не перезапускает старые series, которые встретились только через уже представленные/заблокированные Frames. В обычном режиме pipeline queue строится только из series, созданных из `SELECTED` Frames текущего invocation; намеренный rerun существующей series остаётся доступен через явный `--series-id`. Если guard не выбрал ни одного нового Frame, команда завершается до catalog/Stage-2 работы.

Начиная с 0.5.54.2 M4 persistence канонизирует ориентацию каждого сохраняемого inter-series edge как `left_series_id < right_series_id`. Если M1--M3 сохранил planner orientation в обратном порядке, M4 разворачивает только storage orientation и знак `delta/predicted/closure/standardized` величин; научное решение графа, uncertainties, scatter и calibrator membership не меняются.

Начиная с 0.5.54.4 M8 resume/readiness учитывает physical Observation identity: alternate Frame product не делает batch `INCOMPLETE`, если та же Observation уже имеет READY science representative в целевом field. При historical multiple representatives выбирается READY representative с максимальным science-frame coverage; настоящий непредставленный Observation и legacy `observation_id=NULL` остаются blocking. Это orchestration-only hotfix без изменения Stage-2/M0--M5 численной семантики.

## Публичный Python API

Стабильный программный интерфейс начинается с `grandgousier.api`. В 0.5.50.0 он включает физический aperture error budget, one-source circular aperture measurement и численные Stage-3 M2/M3 pair/graph primitives. Внутренние модули остаются implementation detail. Пример:

```python
from grandgousier.api import compute_aperture_error_budget
```

Подробные единицы, sign conventions и примеры приведены в `docs/user_manual_ru/chapters/11_python_api.tex`.

## Пользовательская документация

Русскоязычное пользовательское руководство ведётся как отдельный LaTeX-проект в `docs/user_manual_ru/`. Начинайте с `docs/user_manual_ru/README.md`; структура manual: быстрый старт, установка, полный workflow, продукты/QC, batch, затем главы по развиваемым межсерийной и raw-reduction стадиям.

## Навигация по актуальной документации

Для продолжения разработки и переноса работы в новый чат используйте:

- `CONTEXT.md` — короткая входная точка;
- `docs/CURRENT_ARCHITECTURE.md` — действующая архитектура и storage boundaries;
- `docs/ROADMAP.md` — ближайшие задачи, ограничения и validation gates;
- `docs/CURRENT_ARCHITECTURE.md` — действующая архитектура;
- `docs/PROJECT_HISTORY.md` — единый исторический архив;
- `context/project_state.yaml` — машиночитаемое резюме;
- `python scripts/build_context_bundle.py` — сборка переносного context bundle.

Старые patch/release/handoff документы объединены в `docs/PROJECT_HISTORY.md`; отдельные файлы больше не являются частью рабочего дерева.

## Что умеет текущая версия

Основные блоки:

1. Индексирование FITS-архива MASTER/RoboPhot в PostgreSQL.
2. Разделение photometric systems через `instrument_units`: instrument/site/tube/camera/filter/mode.
3. Проверка WCS по FITS headers.
4. Импорт identity-каталогов и Gaia-query по кадрам.
5. Импорт внешней reference photometry: PS1/APASS-like слой отдельно от identity catalog.
6. Сопоставление catalog sources с кадрами и source detections.
7. Forced-aperture photometry по catalog-frame links.
8. Aperture-derived zero point calibration.
9. Ensemble differential calibration.
10. Reference-band/color calibration с diagnostics и отдельной calibration version.
11. RMS--magnitude QC, candidate triage, object QC.
12. Dynamic blend/crowding diagnostics на основе FWHM, pixel scale и радиуса апертуры.
13. Aperture-radius sweep и aperture-stability QC.
14. Field QC report.
15. Generic event registry, event-object links, event-frame coverage и event-detection links.
16. Persistent fields/field_series и batch field pipeline runner с structured logs.
17. Pipeline idempotency audit для поиска логических дублей после повторных запусков.
18. Science light-curve product: экспорт кривых блеска из БД с QC/provenance и заделом под межсерийные поправки.
19. One-command archive processing: `process-archive-path` автоматически определяет профиль инструмента, индексирует FITS, собирает `field_series` и запускает стандартный pipeline.
20. Gaia auto-catalog footprint теперь строится по DB-центру кадра с диагностикой причин отказа, а FITS WCS/CD используются для радиуса/масштаба.
21. One-command pipeline ограничивает working catalog для forced photometry, делает pre-photometry frame QC и по умолчанию не запускает дорогой aperture-stability sweep.
22. Accepted candidates экспортируются в CSV/JSON/HTML/PNG с отдельным export QC.
23. Candidate-excluded ensemble calibration поддерживает fixed iteration mode и режим до сходимости.
24. Manifest разделяет requested candidate exclusions и actual exclusions из reference set.
25. В 0.5.33.3 top-level actual exclusions привязаны к последней реально выполненной ensemble iteration.
26. В 0.5.33.4 счётчик новых requested exclusions вычисляется из фактического списка object IDs.
27. В 0.5.34.0 reference/color calibration встроена в стандартный field pipeline как финальная `science_calibration`.
28. В 0.5.34.2 абсолютная PS1 transformation применяется поверх финальной ensemble-кривой без изменения её формы; random и systematic ошибки разделены, а активация science version защищена quality gates.
29. В 0.5.34.3 pipeline выбирает между offset-only и offset+color моделями по значимости коэффициента, BIC и robust MAD.
30. Candidate selection выполняется по финальной внутренней ensemble-калибровке, а экспорт кривых — по `science_calibration`.
31. Candidate products публикуются заменой полного staging-каталога, поэтому устаревшие CSV/JSON/PNG не сохраняются.
32. В 0.5.34.4 candidate summary и объектный JSON получают величины, scatter и ошибки из фактически экспортированной `science_calibration`, а selection diagnostics остаются из внутренней ensemble-калибровки.
33. При `--no-target-light-curve`, неразрешённой цели или отсутствии science frames pipeline удаляет старые target CSV/JSON/PNG.
34. `process-archive-path --enable-color-calibration` проверяет, автоматически загружает и импортирует недостающую Pan-STARRS DR2 photometry для поддерживаемых фильтров.
35. В 0.5.35.0 между raw forced photometry и frame-ZP добавлена per-frame small-to-large aperture correction. Constant и spatial-plane модели выбираются по BIC/MAD, при плохой статистике используется raw fallback.
36. Полная uncertainty aperture correction сохраняется как diagnostics. В `mag_err_random` проходит только position-dependent uncertainty spatial-plane относительно среднего положения aperture-калибраторов; constant/identity не увеличивают случайную ошибку.
37. В 0.5.35.1 переполняющиеся generated calibration names детерминированно сокращаются до лимита `VARCHAR(128)`; long candidate-excluded names используют читаемый тег `candx`, а SQLAlchemy session выполняет rollback перед записью исходной ошибки шага.
38. В 0.5.35.2 устранено завышение epoch-random errors: frame-common aperture intercept/model floor больше не подавляют low-amplitude variability; flags раздельно хранят total и differential aperture uncertainty.
39. В 0.5.35.3 aperture diagnostics сохраняются в итоговом `quality_policy`, а полный namespace `aperture_correction_*` переносится через reference/color calibration в science-level measurement flags.
40. В 0.5.36.0 добавлена detector/PSF context-диагностика aperture correction: FWHM, ellipticity, orientation, background, gradient coherence и exploratory correlations.
41. В 0.5.36.1 safety gate spatial plane разделён на отклонение относительно calibrator pivot и отдельный аварийный предел полной поправки. Legacy-решение 0.5 mag сохраняется как shadow A/B provenance, а новые calibration names используют `spatial_v2`/`sv2` и не перезаписывают 0.5.36.0.
42. В 0.5.36.2 пограничные low-amplitude объекты сохраняются как `BORDERLINE`, экспортируются отдельно и отслеживаются между ensemble/science branches без изменения автоматических порогов или ensemble exclusion.
43. В 0.5.36.3 полный набор `value / threshold / pass / margin / ratio` для всех пяти low-amplitude критериев переносится в science-level candidate rows и публикуется в summary, borderline CSV/JSON и объектных JSON.
44. В 0.5.37.0 `mag_err_random` строится из физического error budget: source Poisson с gain, measured/detector background, uncertainty медианного фона, optional fractional floor и scintillation. Legacy formula считается параллельно, а normalized-residual A/B validation сохраняется по финальным ensemble references.
45. В 0.5.37.1 residual floor оценивается диагностически по нижней огибающей reference stars и после удаления frame-common residual. `mag_err_detector`, `mag_err_epoch_floor` и hypothetical `mag_err_effective` сохраняются в provenance без замены `mag_err_random`; объекты с двумя близкими к порогу критериями сохраняются как `DIAGNOSTIC`.
46. В 0.5.37.2 frame-common decomposition получает формальный support gate, after-common floor не называется independent при отсутствии улучшения, а hypothetical errors разделены на detector-only и total; candidate A/B использует total random uncertainty.
47. В 0.5.37.3 explicit empty catalog завершается fail-fast, а `show-catalog-sources` исправлен для безопасной проверки identity namespace.
48. В 0.5.38.0 `candidate-evidence-report` объединяет candidate products нескольких серий, измеряет повторяемость и синхронную межфильтровую корреляцию по `source_id`.
49. Candidate-excluded ensemble по умолчанию использует growth policy `initial_only`: исходный accepted set исключается один раз, а новые candidates последующих итераций сохраняются как suppressed diagnostics без каскадного расширения reference exclusions.
50. Target provenance явно различает source ID, object ID, auto-nearest и disabled-unresolved режимы.

## Установка

Рекомендуемый способ на Windows:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Для графиков и фотометрических расширений:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-photometry.txt
```

Переменная подключения к БД:

```powershell
$env:GRANDGOUSIER_DATABASE_URL="postgresql+psycopg://USER:PASSWORD@localhost:5432/grandgousier"
```

Инициализация новой БД:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli init-db
```

Полная MVP5-схема требует SQL migrations, потому что поздние таблицы event/field/pipeline layers пока живут не в ORM-модели, а в migration scripts:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_all.sql
```

## Упрощенный one-command workflow

Для штатной обработки директории с FITS-кадрами используется верхнеуровневая команда. По умолчанию она работает в безопасном режиме планирования и ничего не пишет в БД:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night"
```

Команда сама сканирует `.fits/.fit/.fts`, читает заголовки, определяет профиль `MASTER` или `RoboPhot`, группирует файлы по instrument/site/tube/camera/filter/object и показывает план. Если `--catalog-name` не указан, имя identity-каталога создается автоматически, например `auto_catalog_robophot_20231105_GPX_TF16E_48_i`.

Реальный запуск:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night" `
  --execute `
  --manifest-out reports\process_archive_manifest.json
```

Финальная цветовая шкала для поддерживаемого фильтра включается явно:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night" `
  --execute `
  --enable-color-calibration `
  --manifest-out reports\process_archive_color_manifest.json
```

Политика reference band/color index берётся из instrument profile. При отсутствии достаточного импортированного PS1-покрытия команда сама получает Pan-STARRS DR2 CSV и импортирует его в отдельное namespace текущего identity catalog, поэтому новый автоматический Gaia-каталог не перезаписывает прежний PS1↔Gaia match. Для полностью локального запуска используйте `--no-auto-reference-photometry`; тогда цветовая стадия активируется только при уже существующем покрытии. Ошибка внешнего сервиса не уничтожает результат: `science_calibration` остаётся последней ensemble version, а причина записывается в manifest.

В режиме `--execute` команда выполняет стандартный цикл:

```text
scan headers -> auto-detect profile -> index current-path frames -> optional Gaia auto-catalog -> build field_series from current-path frames only -> internal ensemble calibration -> optional PS1 fetch/import + color calibration -> science light-curve summary
```

Если задан `--catalog-name`, используется уже существующий каталог. Если `--catalog-name` не задан, команда пытается автоматически запросить Gaia DR3 для текущих кадров. Начиная с `0.5.31.4`, штатный auto-catalog режим использует один общий Gaia-cone для всего входного поля (`--auto-catalog-query-mode frame_union`), а не отдельный запрос на каждый кадр. Это резко уменьшает риск зависаний и дублирования запросов. Если auto-catalog не создал ни одного источника, pipeline не запускается без явного `--allow-empty-catalog`. Запрос можно отключить, оставив только сгенерированное имя каталога:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\field_or_night" `
  --execute `
  --no-auto-catalog-query
```

Критическое ограничение после `0.5.31.3`: `process-archive-path` строит и обрабатывает только серии, связанные с frame_ids из переданного `--path`. Исторические серии того же `instrument_unit_id` больше не подхватываются автоматически.

Обычные параметры апертуры и группировки остаются доступными как отладочные опции, но для типового запуска их можно не указывать. Для принудительного выбора профиля:

```powershell
python -m grandgousier.cli process-archive-path `
  --path "D:\archive\robophot_field" `
  --instrument-profile RoboPhot `
  --execute
```

Если pipeline технически завершился, но не дал научных точек, manifest получит `science_status`, например `WARNING_NO_MEASUREMENTS`, а общий статус команды станет `DONE_WITH_WARNINGS`. Если зависает внешний сервис Gaia/astroquery, остановите команду `Ctrl+C` и повторите позже либо используйте уже импортированный каталог через `--catalog-name`. Для отладки доступны `--auto-catalog-timeout-sec`, `--auto-catalog-max-radius-deg`, `--auto-catalog-query-mode per_frame` и `--allow-empty-catalog`.

## Candidate-excluded ensemble calibration

Обычный фиксированный режим сохраняет заданное общее число ensemble stages, включая initial pass:

```powershell
--exclude-candidates-from-ensemble `
--candidate-ensemble-max-iterations 3
```

Режим до сходимости продолжает пересчёт, пока текущий field QC не перестанет добавлять новые accepted candidate object IDs в cumulative exclusion set:

```powershell
--exclude-candidates-from-ensemble `
--candidate-ensemble-until-converged `
--candidate-ensemble-convergence-max-iterations 10
```

Последняя запись `candidate_ensemble_exclusion_history` может быть terminal convergence check с `executed=false`. Поэтому сводные actual exclusions относятся к последней выполненной calibration; её номер записывается в `candidate_ensemble_actual_exclusions_iteration`. Подробности текущего workflow и validation gates находятся в `docs/ROADMAP.md` и пользовательском мануале.

## Типовой workflow для одной серии

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli index-archive `
  --root "C:\\path\\to\\fits" `
  --instrument configs\master.yaml `
  --pattern "*.fits" `
  --recursive
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli check-wcs --instrument-unit-id 1 --all-statuses
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli build-field-series `
  --instrument-unit-id 1 `
  --field-radius-arcmin 20 `
  --series-gap-hours 3
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli query-gaia-for-frames `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --mag-limit 18 `
  --match
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli run-forced-photometry `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --aperture-radius-pix 3 `
  --annulus-inner-pix 8 `
  --annulus-outer-pix 12
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli aperture-zp-calibrate `
  --source-calibration-name raw_aperture_r3_ann8_12_v1 `
  --target-calibration-name preliminary_aperture_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --mag-key G `
  --min-ref-stars-per-frame 30
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli ensemble-calibrate `
  --source-calibration-name preliminary_aperture_gaia_g_zp_v1 `
  --target-calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-ref-measurements 10 `
  --min-ref-stars-per-frame 30
```

В `process-archive-path` reference/color calibration выполняется автоматически при `--enable-color-calibration`. Следующие команды сохраняются для ручной диагностики и повторного импорта Pan-STARRS DR2 i-band с цветом r-i:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli create-reference-photometry-csv `
  --provider panstarrs_dr2 `
  --series-id 1 `
  --require-band i `
  --require-band r `
  --max-mag i=19 `
  --out exports\ps1_reference.csv `
  --overwrite `
  --print-import-command
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-reference-photometry `
  --file exports\ps1_reference.csv `
  --catalog-name panstarrs_dr2 `
  --catalog-version dr2 `
  --photometric-system ps1_ab `
  --source-priority 10 `
  --ra-col ra `
  --dec-col dec `
  --source-id-col catalog_source_id `
  --mag-col g=mag_g --mag-col r=mag_r --mag-col i=mag_i --mag-col z=mag_z --mag-col y=mag_y `
  --mag-err-col g=err_g --mag-err-col r=err_r --mag-err-col i=err_i --mag-err-col z=err_z --mag-err-col y=err_y `
  --color-col g_r=color_g_r --color-col r_i=color_r_i --color-col i_z=color_i_z `
  --quality-col n_detections=n_detections --quality-col quality_flag=quality_flag --quality-col obj_info_flag=obj_info_flag `
  --match-to-catalog gaia_dr3_master `
  --max-match-arcsec 1.0
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-color-calibration `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --reference-catalog-name panstarrs_dr2 `
  --reference-band i `
  --color-index r-i `
  --out-dir exports\color_diagnostics
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli apply-color-calibration `
  --source-calibration-name ensemble_aperture_gaia_g_v1 `
  --target-calibration-name ps1_i_color_v1 `
  --reference-catalog-name panstarrs_dr2 `
  --reference-band i `
  --color-index r-i
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 10 `
  --blended-arcsec 5 `
  --dynamic-separation `
  --dynamic-aperture-factor 1.0 `
  --dynamic-crowding-factor 1.5 `
  --fwhm-header-unit arcsec
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --series-id 42 `
  --out exports\field_qc_report.json `
  --tables-prefix exports\field_qc
```

## Проверка разработки

Минимальная синтаксическая проверка:

```powershell
.\.venv\Scripts\python.exe -m compileall grandgousier tests
```

Unit-тесты:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Generic event registry / event coverage

События задаются универсально: транзит, затмение, новая, вспышка, микролинзирование, transient alert.

Пример импорта:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-events --file examples\events_test.csv
```

Матч событий к объектам:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-events-to-objects `
  --event-type transit `
  --catalog-name gaia_dr3_master `
  --coordinate-radius-arcsec 5
```

Покрытие событий кривыми блеска:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli event-coverage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --event-type transit `
  --event-window-source auto `
  --pre-margin-hours 0.5 `
  --post-margin-hours 0.5 `
  --write-frame-coverage `
  --out exports\event_coverage_transit.csv
```

Поля MVP5.17--MVP5.18 в event coverage:

```text
n_pre_baseline
n_event
n_post_baseline
event_window_source
baseline_status
pre_baseline_start_bjd_tdb
post_baseline_end_bjd_tdb
pre_margin_hours
post_margin_hours
event_observability_status
event_observability_reasons
```

`event-window-source`:

```text
auto         — использовать explicit t_start/t_end, если они есть, иначе t0/duration
explicit     — использовать только t_start_bjd_tdb/t_end_bjd_tdb
t0_duration  — использовать t0 или t_peak + duration_hours, с folding по period_days при наличии периода
```

`baseline_status`:

```text
FULL_BASELINE
PRE_BASELINE_ONLY
POST_BASELINE_ONLY
NO_BASELINE
BASELINE_ONLY
NO_EVENT_POINTS
NO_EVENT_WINDOW
NO_POINTS
```

`event_observability_status` — итоговая triage-оценка пригодности события после проверки покрытия, ожидаемого SNR и QC объекта:

```text
GOOD_COVERAGE_GOOD_PRECISION
GOOD_COVERAGE_LOW_SNR
GOOD_COVERAGE_BAD_OBJECT_QC
GOOD_COVERAGE_UNKNOWN_PRECISION
GOOD_COVERAGE_UNKNOWN_SIGNAL
PARTIAL_COVERAGE
NO_EVENT_POINTS
NO_EVENT_WINDOW
NO_POINTS
NO_MATCH
```

Настройки observability-triage:

```powershell
--min-depth-total-snr 3.0
--min-event-points 1
--min-pre-baseline-points 1
--min-post-baseline-points 1
--bad-object-qc-flags EDGE,CROWDED,APERTURE_UNSTABLE
--ignore-object-qc
```

По умолчанию `event-coverage-summary` консервативно понижает статус события, если объект имеет флаги blend/crowding/edge/saturation/aperture instability/detection support problems. Это не физическая модель события, а быстрый фильтр для массового архива.

## История проекта

Исторические README/release notes/patch notes/ADR/handoff документы объединены в `docs/PROJECT_HISTORY.md`. Актуальный инженерный план хранится только в `docs/ROADMAP.md`; текущее подтверждённое состояние — в `CONTEXT.md`. Новые Markdown-файлы на каждый patch/version создавать не следует.


### 0.6.4.5.1 global materialized SCIENCE scope

After the real field-33 retirement gate, the storage-maintenance commands accept explicit `--all-materialized`. This scope is fail-closed and mutually exclusive with `--field-id` / explicit calibration IDs; it selects only immutable SCIENCE products that still have physical `science_measurements` rows. It is intended for the final global legacy-V1 retirement pass.
