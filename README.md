# Grandgousier

Версия: **0.5.32.6**

Grandgousier - Python-пакет для индексирования FITS-архивов, forced-aperture фотометрии, локальной и ensemble-калибровки, reference/color calibration, QC объектов, проверки event coverage и отбора кандидатов в переменные объекты.

Текущий статус: MVP5.32.0. Кодовая база уже ушла дальше старой инженерной карты: кроме forced photometry и self-calibration реализованы field/series grouping, source-detection branch, event registry, reference photometry layer, PS1/reference-band color calibration, pipeline run logging и сохранение aperture-stability QC для derived calibrations. Фактическая инженерная карта вынесена в `docs/CURRENT_ARCHITECTURE.md`.

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

В режиме `--execute` команда выполняет стандартный цикл:

```text
scan headers -> auto-detect profile -> index current-path frames -> optional Gaia auto-catalog -> build field_series from current-path frames only -> run-field-pipeline -> light-curve summary
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

Reference/color calibration добавляется после импорта внешней photometry. Пример для Pan-STARRS DR2 i-band и цвета r-i:

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



## Изменения версии 0.5.18

Добавлена итоговая triage-колонка `event_observability_status` в `event-coverage-summary`. Теперь отчет различает геометрическое покрытие события и практическую пригодность наблюдения: хорошее покрытие с хорошей ожидаемой точностью, хорошее покрытие при низком SNR, хорошее покрытие у проблемного объекта, частичное покрытие и отсутствие match/точек/окна.

Миграция БД не требуется.

## Очистка проекта в версии 0.5.17

Из поставляемого архива удалены временные и локальные файлы:

```text
.venv/
.idea/
__pycache__/
*.pyc
grandgousier.egg-info/
устаревший корневой cli.py
```

Все README-файлы объединены в текущий `README.md`. Ниже сохранен архив предыдущих README-разделов.

## Архив предыдущих README-разделов

### Предыдущий README.md

# Grandgousier MVP1

Grandgousier — модульный движок архивной фотометрии для телескопов типа MASTER/RoboPhot: несколько труб, камер и фильтров, неоднородные серии, неполные калибровочные данные и PostgreSQL как основная операционная БД.

MVP1 реализует базовый слой:

1. YAML-профили инструментов;
2. чтение FITS-заголовков без загрузки полного изображения;
3. индексирование архива raw-кадров;
4. нормализацию метаданных MASTER/RoboPhot;
5. запись `frames` и `instrument_units` в PostgreSQL;
6. статусы обработки и статусы ссылок на FLAT/DARK/SEXCAT;
7. каркас следующих этапов пайплайна.

## Установка через venv

PowerShell из корня проекта:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Если PowerShell запрещает `Activate.ps1`, активация не нужна: запускай команды через `.\.venv\Scripts\python.exe`.

## PostgreSQL URL

Для текущей сессии PowerShell:

```powershell
$env:GRANDGOUSIER_DATABASE_URL="postgresql+psycopg://grandgousier:glodga@localhost:5432/grandgousier"
```

Или подключить готовый dev-скрипт:

```powershell
. .\scripts\dev_env.ps1
```

Файл `.env.example` содержит пример. Реальный `.env` лучше не коммитить.

## Создать таблицы

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli init-db
```

## Полная dev-переустановка

Без очистки БД:

```powershell
.\scripts\dev_reinstall.ps1
```

С полной очисткой и пересозданием всех таблиц:

```powershell
.\scripts\dev_reinstall.ps1 -ResetDb
```

Команда `reset-db` разрушает все таблицы Grandgousier в выбранной БД:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli reset-db --yes
```

## Проиндексировать архив FITS

MASTER:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli index-archive `
  --root "C:\path\to\master\fits" `
  --instrument "configs\master.yaml" `
  --pattern "*.fits" `
  --recursive
```

RoboPhot:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli index-archive `
  --root "C:\path\to\robophot\fits" `
  --instrument "configs\robophot.yaml" `
  --pattern "*.fits" `
  --recursive
```

Сухой прогон без записи в БД:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli index-archive --root "C:\path\to\fits" --instrument "configs\master.yaml" --dry-run
```

## Проверить БД

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli db-summary
```

SQL-проверка:

```sql
SELECT frame_id, frame_uid, instrument, site, tube, camera, filter_name, observing_mode, image_type, processing_status
FROM frames
ORDER BY frame_id
LIMIT 20;

SELECT instrument_unit_id, unit_uid, instrument, site, tube, camera, filter_name, observing_mode
FROM instrument_units
ORDER BY instrument_unit_id;
```

## Что считается готовым в MVP1

MVP1 считается рабочим, если:

- архив обходится рекурсивно;
- FITS-заголовки читаются без загрузки всего изображения;
- для каждого кадра создается нормализованная запись;
- кадры получают стабильный `frame_uid`;
- повторный запуск не дублирует уже найденные кадры;
- для каждой комбинации `instrument/site/tube/camera/filter/observing_mode` создается запись в `instrument_units`;
- `JD/MJD` взаимно восстанавливаются, если один из них отсутствует;
- `flat_status`, `dark_status`, `sexcat_status` выставляются по наличию ссылок в header;
- таблица `frames` пригодна для выбора задач следующего этапа.

## Следующий этап MVP2

MVP2 должен добавить:

- WCS-check / astrometry status;
- загрузку локального каталога поля;
- forced aperture photometry;
- запись `objects` и `measurements`;
- первичные QC-графики.

### README_MVP3_PATCH.md

# Grandgousier MVP3.0 catalog patch

Adds local catalog import and frame-footprint projection.

## Files changed

- `grandgousier/db/models.py`
- `grandgousier/cli.py`
- `grandgousier/catalogs/import_catalog.py`
- `grandgousier/catalogs/frame_match.py`
- `grandgousier/catalogs/__init__.py`
- `scripts/migrate_mvp3_catalogs.sql`

## Database

If using a development reset:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli reset-db --yes
.\.venv\Scripts\python.exe -m grandgousier.cli init-db
```

If keeping the current `frames` table:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp3_catalogs.sql
```

Then reinstall:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Commands

Import a local CSV catalog:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-catalog `
  --file C:\path\to\catalog.csv `
  --catalog-name gaia_test `
  --ra-col ra `
  --dec-col dec `
  --source-id-col source_id `
  --mag-col G=phot_g_mean_mag `
  --color-col BP_RP=bp_rp
```

Dry run:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-catalog --file catalog.csv --catalog-name test --dry-run
```

Summary:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli catalog-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-catalog-sources --catalog-name gaia_test --limit 10
```

Project catalog sources into WCS frames:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-catalog-to-frames `
  --catalog-name gaia_test `
  --instrument-unit-id 1 `
  --margin-pix 2
```

Test projection without writing:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-catalog-to-frames --catalog-name gaia_test --limit-frames 1 --limit-sources 100 --dry-run
```

## Limits

This is not Gaia/LSDB download yet. It imports a local catalog table and projects sources into frames using existing header WCS. This is enough to validate the object-catalog layer before forced photometry.

### README_MVP4_PATCH.md

# Grandgousier MVP4 patch: forced aperture photometry

Patch contents:

```text
grandgousier/cli.py
grandgousier/photometry/forced_aperture.py
grandgousier/photometry/__init__.py
patch.diff
README_MVP4_PATCH.md
```

No database migration is required: the existing `objects`, `measurements`, and `calibration_versions` tables are used.

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Run test photometry on the imported test catalog

Assuming `catalog_sources` and `frame_catalog_links` are already populated:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli run-forced-photometry `
  --catalog-name test_master `
  --instrument-unit-id 1 `
  --aperture-radius-pix 3 `
  --annulus-inner-pix 8 `
  --annulus-outer-pix 12
```

Dry run:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli run-forced-photometry `
  --catalog-name test_master `
  --instrument-unit-id 1 `
  --limit-frames 2 `
  --dry-run
```

## Inspect results

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli measurement-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-measurements --limit 20
.\.venv\Scripts\python.exe -m grandgousier.cli db-summary
```

## Notes

This is an MVP aperture backend, not final survey photometry.

It uses:

- x/y from `frame_catalog_links`;
- circular aperture sum;
- local annulus median background;
- instrumental magnitude `-2.5 log10(flux / exptime) + zero_point`;
- `objects` are created from `catalog_sources` using stable `object_uid` values;
- `measurements` are upserted using `calibration_version_id`.

It does not yet include centroid refinement, aperture correction, PSF fitting, gain/read-noise error model, ensemble zero point, or self-calibration.

### README_MVP5_10_CANDIDATE_TRIAGE_PATCH.md

# Grandgousier MVP5.10 — candidate triage and rejection reasons

This patch adds:

```text
candidate-triage-summary
```

No database migration is required.

The command is intended for the situation where `variable-candidate-summary` returns
`No rows` after strict filters. It keeps rejected high-RMS candidates in the output and
adds explicit rejection reasons.

## Files

```text
grandgousier/quality/candidate_triage.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Strict triage, same filters as clean candidate search

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli candidate-triage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 50
```

## Export full triage CSV

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli candidate-triage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --out exports\candidate_triage_ensemble_aperture.csv `
  --limit 50
```

## Relaxed triage examples

Include crowded/blended objects, but keep other filters:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli candidate-triage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --include-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 50
```

Allow weak detection support:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli candidate-triage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --allow-weak-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 50
```

## Output columns

Important columns:

```text
candidate_status
rejection_reasons
rms_excess
mag_rms
mag_mad
nearest_neighbor_arcsec
detection_fraction
det_sep_median_arcsec
mag_ref_delta
object_qc_flags
```

Typical rejection reasons:

```text
CROWDED_OR_BLEND
EDGE
SATURATED_LIKE
CATALOG_MAG_RESIDUAL
WEAK_OR_NO_DETECTION_SUPPORT
TOO_FAINT
REF_MAG_DELTA_TOO_LARGE
NO_OBJECT_QC
```

### README_MVP5_11_APERTURE_SWEEP_PATCH.md

# Grandgousier MVP5.11 — aperture-radius stability sweep

This patch adds:

```text
aperture-radius-sweep
```

It runs and compares one object across several aperture radii. The goal is to test
whether a candidate light curve is robust or caused by aperture/seeing/blend systematics.

No database migration is required.

## Files

```text
grandgousier/quality/aperture_sweep.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Run for the current blend-suspect candidate

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli aperture-radius-sweep `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --radii 2,3,4,5 `
  --annulus-inner-pix 8 `
  --annulus-outer-pix 12 `
  --mag-key G `
  --min-ref-stars-per-frame 30 `
  --max-mag-err 0.08 `
  --clip-sigma 3 `
  --n-iterations 3 `
  --min-ref-measurements 10 `
  --ensemble-min-ref-stars-per-frame 30 `
  --max-ref-rms 0.08 `
  --max-ref-mad 0.05 `
  --out exports\aperture_sweep_2304896849394561408.csv
```

This will create/use these calibration versions:

```text
raw_aperture_r2_ann8_12_v1
preliminary_aperture_r2_gaia_g_zp_v1
ensemble_aperture_r2_gaia_g_v1

raw_aperture_r3_ann8_12_v1
preliminary_aperture_r3_gaia_g_zp_v1
ensemble_aperture_r3_gaia_g_v1

raw_aperture_r4_ann8_12_v1
preliminary_aperture_r4_gaia_g_zp_v1
ensemble_aperture_r4_gaia_g_v1

raw_aperture_r5_ann8_12_v1
preliminary_aperture_r5_gaia_g_zp_v1
ensemble_aperture_r5_gaia_g_v1
```

## Output columns

Important columns:

```text
radius_pix
n_points
mag_median
mag_rms
mag_mad
mag_err_median
corr_with_ref
median_delta_vs_ref
rms_delta_vs_ref
```

The first radius in `--radii` is the reference curve for `corr_with_ref`.

## Interpretation

A robust real variability candidate should roughly preserve light-curve shape across
reasonable apertures:

```text
corr_with_ref high
mag_rms and mag_mad similar across radii
rms_delta_vs_ref small
```

An aperture/seeing/blend artifact usually shows stronger dependence on aperture radius:

```text
mag_rms changes strongly with radius
corr_with_ref decreases
median_delta/rms_delta changes systematically
```

## Summarize existing versions only

After the first run, or if calibration versions already exist:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli aperture-radius-sweep `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --radii 2,3,4,5 `
  --skip-photometry `
  --skip-calibration
```

### README_MVP5_12_DYNAMIC_APERTURE_QC_PATCH.md

# Grandgousier MVP5.12 — dynamic separation and aperture-stability QC

This patch adds two related improvements.

1. `compute-object-qc` can now use a dynamic separation criterion based on frame seeing/FWHM, aperture radius and pixel scale instead of only fixed descriptive angular thresholds.
2. `compute-aperture-stability-qc` stores aperture-radius sweep results in `object_qc.details["aperture_stability"]` and adds flags for aperture instability and likely blends.

No database migration is required. New information is stored in `object_qc.flags` and `object_qc.details` JSONB.

## Files

```text
grandgousier/quality/object_quality.py
grandgousier/quality/aperture_stability.py
grandgousier/quality/candidate_triage.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## 1. Dynamic separation QC

For MASTER-like seeing-limited frames, fixed values such as `crowded_arcsec=10` are useful but crude. The dynamic threshold is computed per object from the actual measurement geometry:

```text
blend = max(seeing_factor * FWHM,
            aperture_factor * aperture_radius_pix * pixel_scale,
            min_dynamic_blend_arcsec)

crowded = dynamic_crowding_factor * blend
```

Recommended run for the current MASTER test series:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 10 `
  --blended-arcsec 5 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08 `
  --min-detection-fraction 0.5 `
  --max-ref-mag-delta 2.0 `
  --dynamic-separation `
  --dynamic-seeing-factor 1.5 `
  --dynamic-aperture-factor 1.0 `
  --dynamic-crowding-factor 1.5 `
  --fwhm-header-unit arcsec
```

New flags may be added:

```text
DYNAMIC_BLEND_RISK
DYNAMIC_CROWDED
POSSIBLE_BLEND
```

The dynamic metrics are stored in:

```text
object_qc.details["dynamic_separation"]
```

If the FWHM header value is in pixels rather than arcseconds, use:

```powershell
--fwhm-header-unit pix
```

If unsure, try both and compare `dynamic_crowded` / `dynamic_blend_risk` counts.

## 2. Aperture stability QC

After running `aperture-radius-sweep`, store its result in object QC:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-aperture-stability-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --radii 2,3,4,5 `
  --min-stable-corr 0.7 `
  --max-stable-rms-delta 0.03 `
  --max-median-shift 0.2 `
  --possible-blend-neighbor-arcsec 10
```

For the current object the expected result should flag it as aperture-unstable and likely blend-contaminated because the sweep gave low correlations and large median shifts.

New flags:

```text
APERTURE_UNSTABLE
APERTURE_MEDIAN_SHIFT
POSSIBLE_BLEND
```

Stored metrics:

```text
object_qc.details["aperture_stability"]
```

Important metrics:

```text
min_corr_with_ref
max_rms_delta_vs_ref
max_abs_median_delta_vs_ref
rms_range
mad_range
```

## 3. Candidate queries after this patch

Strict clean candidate search can now reject aperture-unstable objects too:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli variable-candidate-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --exclude-aperture-unstable `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 30
```

Triage now also rejects and explains aperture-instability by default:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli candidate-triage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 50
```

To inspect aperture-unstable candidates without rejecting them:

```powershell
--include-aperture-unstable
```

### README_MVP5_13_BLEND_DIAGNOSTICS_PATCH.md

# Grandgousier MVP5.13 — separation-index and blend diagnostics report

This patch adds a continuous dynamic-separation diagnostic layer and a field-level report command.

New command:

```text
blend-diagnostic-summary
```

No database migration is required.

## Files

```text
grandgousier/quality/blend_diagnostics.py
grandgousier/quality/object_quality.py
grandgousier/quality/candidate_triage.py
grandgousier/cli.py
```

## What changed

`compute-object-qc --dynamic-separation` now also stores in
`object_qc.details["dynamic_separation"]`:

```text
nearest_neighbor_arcsec
separation_index = nearest_neighbor_arcsec / dynamic_blend_arcsec
crowding_index = nearest_neighbor_arcsec / dynamic_crowded_arcsec
separation_class
```

Interpretation:

```text
separation_index < 1.0   → inside dynamic blend radius
1.0–1.5                  → marginal dynamic separation
> 1.5                    → separated by dynamic threshold
```

`candidate-triage-summary` now displays the dynamic separation columns.

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Recompute object QC

Run this so the new `separation_index` and `crowding_index` are written to JSONB:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 10 `
  --blended-arcsec 5 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08 `
  --min-detection-fraction 0.5 `
  --max-ref-mag-delta 2.0 `
  --dynamic-separation `
  --dynamic-aperture-factor 1.0 `
  --dynamic-crowding-factor 1.5 `
  --fwhm-header-unit arcsec
```

If `SEXFWHM` is in pixels for a given instrument config, use:

```powershell
--fwhm-header-unit pix
```

## Blend diagnostics for likely problem objects

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli blend-diagnostic-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --only-possible-blends `
  --sort risk `
  --limit 50
```

## Export full blend report

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli blend-diagnostic-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --only-flagged `
  --sort risk `
  --out exports\blend_diagnostics_ensemble_aperture.csv `
  --limit 50
```

## Focus on marginally separated objects

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli blend-diagnostic-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --max-separation-index 1.5 `
  --sort separation_index `
  --limit 50
```

## Important output columns

```text
nearest_neighbor_arcsec
dynamic_blend_arcsec
dynamic_crowded_arcsec
separation_index
crowding_index
separation_class
fwhm_arcsec_used
aperture_radius_pix_used
pixel_scale_arcsec_used
possible_blend
aperture_unstable
aperture_median_shift
object_qc_flags
```

### README_MVP5_14_PRESERVE_APERTURE_QC_PATCH.md

# Grandgousier MVP5.14 — preserve aperture-stability QC layer

This patch fixes the command-order issue:

```text
compute-object-qc
```

must not erase aperture-stability diagnostics previously written by:

```text
compute-aperture-stability-qc
```

No database migration is required.

## Files

```text
grandgousier/quality/object_quality.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## What changed

`compute-object-qc` now preserves this existing layer when recomputing object QC:

```text
object_qc.details["aperture_stability"]
```

and preserves these flags if they were already set:

```text
APERTURE_UNSTABLE
APERTURE_MEDIAN_SHIFT
POSSIBLE_BLEND
```

The command output now also includes:

```text
aperture_unstable
aperture_median_shift
preserved_aperture_stability
```

## Recommended check

Run the commands in either order. First write aperture stability:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-aperture-stability-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --radii 2,3,4,5 `
  --min-stable-corr 0.7 `
  --max-stable-rms-delta 0.03 `
  --max-median-shift 0.2 `
  --possible-blend-neighbor-arcsec 10
```

Then recompute ordinary object QC:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 10 `
  --blended-arcsec 5 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08 `
  --min-detection-fraction 0.5 `
  --max-ref-mag-delta 2.0 `
  --dynamic-separation `
  --dynamic-aperture-factor 1.0 `
  --dynamic-crowding-factor 1.5 `
  --fwhm-header-unit arcsec
```

Expected output should include something like:

```text
aperture_unstable=1
aperture_median_shift=1
preserved_aperture_stability=1
```

Then `candidate-triage-summary` and `blend-diagnostic-summary` should still show aperture-stability metrics without requiring another `compute-aperture-stability-qc` run.

### README_MVP5_15_FIELD_QC_REPORT_PATCH.md

# Grandgousier MVP5.15 — field QC report

This patch adds one summary command:

```text
field-qc-report
```

It collects the main products of a processed field/series into one report:

- frame summary;
- frame_qc summary;
- measurement/object counts for one calibration;
- RMS--magnitude bin summary;
- object_qc flag counts;
- candidate triage counts and top rejected/accepted candidates;
- blend/dynamic-separation diagnostics;
- JSON export and optional CSV table export.

No database migration is required.

## Files

```text
grandgousier/quality/field_report.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Basic report

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1
```

Для отчета строго по одной persistent series используйте `--series-id` или `--field-series-name`. Это ограничивает `frames`, `frame_qc`, `measurements`, RMS/scatter, candidate triage и blend diagnostics только кадрами из `field_series_frames`:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --field-series-name master_test_20230919_s1_v30 `
  --out reports\master_test_20230919_s1_v30_ps1_i_color_v1\field_qc_report.json
```

## JSON report

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --out exports\field_qc_report_ensemble_aperture.json
```

## JSON + CSV tables

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --out exports\field_qc_report_ensemble_aperture.json `
  --tables-prefix exports\field_qc_ensemble_aperture
```

This creates:

```text
exports\field_qc_ensemble_aperture_rms_bins.csv
exports\field_qc_ensemble_aperture_candidate_triage.csv
exports\field_qc_ensemble_aperture_blend_diagnostics.csv
```

## Uses the same strict candidate filters by default

Defaults match the current clean candidate policy:

```text
min_measurements = 10
min_excess = 2.0
exclude_crowded = true
exclude_edge = true
exclude_saturated = true
exclude_catalog_outliers = true
reject_aperture_unstable = true
require_detection_support = true
max_mag_median = 22.5
max_ref_mag_delta = 2.0
```

These can be relaxed, for example:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli field-qc-report `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --include-crowded `
  --include-aperture-unstable
```

## Console output

The table reports metrics such as:

```text
frames.n_frames
frame_qc.astrom_rms_arcsec_median
measurements.n_measurements
objects.mag_rms_median
object_qc.n_object_qc
candidate_triage.raw_candidates
candidate_triage.accepted
candidate_triage.rejected
candidate_triage.rejection_counts
blend.possible_blend
blend.dynamic_crowded
blend.aperture_unstable
warnings
```

The full JSON includes top accepted/rejected candidates and top blend-risk objects.

### README_MVP5_16_EVENT_REGISTRY_PATCH.md

# Grandgousier MVP5.16 — generic event registry and coverage index

This patch adds a generic event layer for predicted, externally alerted, and internally discovered events. It is not a transit/nova analysis model; it is infrastructure for answering whether the final light-curve database covers an event.

## New tables

Run the migration once:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_16_event_registry.sql
```

Tables:

```text
event_registry
event_object_links
event_frame_coverage
```

## New commands

```text
import-events
show-events
match-events-to-objects
show-event-object-links
event-coverage-summary
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Generic CSV format

Supported columns. You may leave irrelevant columns empty.

```text
event_uid,event_type,event_subtype,source_catalog,source_event_id,
external_catalog,external_id,target_name,
ra_deg,dec_deg,position_error_arcsec,object_id,
t_start_bjd_tdb,t_peak_bjd_tdb,t_end_bjd_tdb,t0_bjd_tdb,period_days,duration_hours,
expected_sign,expected_depth_ppt,expected_amplitude_mag,priority,status,event_model_json
```

Examples:

Transit-like event:

```csv
event_uid,event_type,event_subtype,source_catalog,source_event_id,external_catalog,external_id,target_name,t0_bjd_tdb,period_days,duration_hours,expected_sign,expected_depth_ppt,priority
manual:TIC42026841501:20230919,transit,toi,manual,TIC420268415.01,gaia_dr3_master,2304896849394561408,TIC 420268415.01,2460207.45,,1.2,dimming,6.6,1
```

Nova-like event:

```csv
event_uid,event_type,event_subtype,source_catalog,source_event_id,target_name,ra_deg,dec_deg,position_error_arcsec,t_start_bjd_tdb,t_peak_bjd_tdb,t_end_bjd_tdb,expected_sign,expected_amplitude_mag,priority
manual:nova:test,nova,classical_nova,manual,nova-test,Nova candidate,310.1234,87.0123,2.0,2460207.1,2460207.5,2460208.0,brightening,5.0,2
```

## Import events

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-events `
  --file examples\events_test.csv
```

With defaults:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli import-events `
  --file examples\events_test.csv `
  --default-event-type transit `
  --default-source-catalog manual
```

## Show events

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-events --limit 20
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-events --event-type transit --limit 20
```

## Match events to objects

For Gaia-source events already expressed as `external_catalog=gaia_dr3_master`, `external_id=<source_id>`:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-events-to-objects `
  --event-type transit `
  --catalog-name gaia_dr3_master `
  --coordinate-radius-arcsec 5
```

Coordinate matching only:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-events-to-objects `
  --event-type nova `
  --coordinate-radius-arcsec 10
```

Show links:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-event-object-links --limit 50
```

## Event coverage summary

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli event-coverage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --event-type transit `
  --out exports\event_coverage_transit.csv
```

Write per-frame coverage rows too:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli event-coverage-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --event-type transit `
  --write-frame-coverage `
  --out exports\event_coverage_transit.csv
```

## Output interpretation

Important columns:

```text
n_points
n_before
n_in_window
n_after
coverage_status
median_mag_err
expected_depth_ppt
depth_per_point_snr_est
depth_total_snr_est
object_qc_flags
```

Coverage statuses:

```text
COVERED_WITH_BASELINE     points before, inside, and after event window
PARTIAL_EVENT_ONLY        event window has points, but baseline is incomplete
PARTIAL_BASELINE_ONLY     nearby baseline exists, but no points inside event
OUTSIDE_WINDOW            object has data but not near event window
NO_POINTS                 event linked to object, but no selected measurements
NO_MATCHED_OBJECT          event is not linked to an object
NO_EVENT_WINDOW            event has no usable time window
```

Time note: the first implementation compares event BJD_TDB windows to `frame.mjd + 2400000.5` as an approximate JD-like frame time. This is sufficient for coarse event-coverage indexing. A later time-cache patch should store per-measurement BJD_TDB for precise transit timing.

### README_MVP5_1_PATCH.md

# Grandgousier MVP5.1: detection-catalog QC

This patch adds cross-match between image detections and projected catalog sources, plus frame-level QC statistics.

## Files

```text
grandgousier/cli.py
grandgousier/db/models.py
grandgousier/quality/__init__.py
grandgousier/quality/detection_catalog_qc.py
scripts/migrate_mvp5_1_detection_catalog_qc.sql
patch.diff
```

## Database migration

Run once:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_1_detection_catalog_qc.sql
```

No database reset is required.

## Reinstall package

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Main command

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli match-detections-to-catalog `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --radius-arcsec 2 `
  --mag-key G
```

Useful options:

```powershell
--min-snr 10
--limit-frames 2
--dry-run
--replace-existing
```

## Checks

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli source-catalog-match-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-source-catalog-matches --catalog-name gaia_dr3_master --limit 20
.\.venv\Scripts\python.exe -m grandgousier.cli frame-qc-summary --catalog-name gaia_dr3_master --instrument-unit-id 1
.\.venv\Scripts\python.exe -m grandgousier.cli db-summary
```

## New tables

```text
detection_catalog_matches
frame_qc
```

`detection_catalog_matches` stores per-detection nearest-catalog matches.

`frame_qc` stores per-frame statistics:

```text
n_detections
n_catalog_sources
n_matched
astrom_rms_arcsec
sep_median_arcsec
sep_p95_arcsec
dx_median_pix
dy_median_pix
zp_median
zp_mad
```

The zero point is estimated as:

```text
zp = catalog_mag - mag_inst
mag_inst = -2.5 log10(flux)
```

This is still a rough diagnostic zero point, not final calibrated photometry.

### README_MVP5_2_PATCH.md

# Grandgousier MVP5.2 patch — preliminary frame-QC calibration and light-curve export

This patch adds a diagnostic preliminary calibration stage based on per-frame zero points already stored in `frame_qc` by MVP5.1.

It does not change the database schema. No SQL migration is required.

## Changed/new files

```text
grandgousier/cli.py
grandgousier/calibration/preliminary.py
README_MVP5_2_PATCH.md
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Preliminary calibration

The command copies raw forced-aperture measurements into a new `CalibrationVersion` and fills:

```text
mag_cal = mag_inst + frame_qc.zp_median
local_zp_correction = frame_qc.zp_median
```

This is a diagnostic Gaia-G zero-point calibration. It is not final color-corrected photometry.

Dry run:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli calibrate-measurements `
  --source-calibration-name raw_aperture_r3_ann8_12_v1 `
  --target-calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-zp-n 30 `
  --dry-run
```

Real run:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli calibrate-measurements `
  --source-calibration-name raw_aperture_r3_ann8_12_v1 `
  --target-calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-zp-n 30
```

By default, the command calibrates only source measurements with `flags.catalog_name == --catalog-name`. To use a different source measurement catalog namespace, pass:

```powershell
--source-catalog-name <catalog_name>
```

## Checks

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli measurement-summary
.\.venv\Scripts\python.exe -m grandgousier.cli object-summary --calibration-name preliminary_gaia_g_zp_v1 --catalog-name gaia_dr3_master --limit 20
```

Show one light curve by Gaia source id:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-lightcurve `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128
```

Export to CSV:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --out exports\lc_2304900388447536128.csv
```

`show-measurements` now includes `mag_cal` and `local_zp_correction` and can filter by calibration name:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-measurements `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --limit 20
```

## Expected effect for the current MASTER test DB

After the Gaia forced-photometry run, raw measurements are approximately:

```text
raw_aperture_r3_ann8_12_v1: 2340 Gaia measurements (+ older test measurements if kept)
```

After `calibrate-measurements`, a second measurement set should appear:

```text
preliminary_gaia_g_zp_v1: about 2340 calibrated Gaia measurements
```

If older test measurements remain in the same raw calibration version, they are skipped by catalog-name filtering unless `--source-catalog-name` is changed.

### README_MVP5_3_1_PATCH.md

# Grandgousier MVP5.3.1 — improved light-curve plot export

This patch extends `export-lightcurve-plot`.

## Changes

- Better X-axis choices:
  - `mjd`
  - `mjd_zero` (default; plots `MJD - floor(min(MJD))`)
  - `mjd_frac`
  - `bjd_tdb`
  - `bjd_tdb_zero`
  - `bjd_tdb_frac`
  - `frame_id`
  - `measurement_id`
- Optional on-the-fly BJD_TDB conversion using Astropy.
- Built-in KAO observatory location for BJD_TDB plotting.
- Optional custom observatory coordinates.
- Optional stats box: `N`, mean, median, RMS, MAD.
- More informative default plot title.
- `export-lightcurve` CSV now includes `calibration_name`, `instrument`, `site`, `ra_deg`, `dec_deg`.

No database migration is required.

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Astropy is already a core dependency. Matplotlib is needed for plotting:

```powershell
.\.venv\Scripts\python.exe -m pip install matplotlib
```

## Examples

Default plot, now using `mjd_zero`:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --show-stats `
  --out exports\lc_2304900388447536128.png
```

Plot by BJD_TDB, zeroed by integer day:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x bjd_tdb_zero `
  --show-stats `
  --out exports\lc_2304900388447536128_bjd.png
```

Plot by BJD_TDB fractional part:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x bjd_tdb_frac `
  --show-stats `
  --out exports\lc_2304900388447536128_bjd_frac.png
```

Custom observatory for BJD_TDB:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x bjd_tdb_zero `
  --observatory-lat-deg 57.0363889 `
  --observatory-lon-deg 59.5416667 `
  --observatory-height-m 290 `
  --out exports\lc_custom_bjd.png
```

## Note on MJD vs BJD_TDB

For quick technical checks, `mjd_zero` is sufficient. For final astrophysical light curves, period searches, eclipse timings, or combining data from different observatories, prefer `bjd_tdb`/`bjd_tdb_zero`. The conversion requires object coordinates and observatory location.

### README_MVP5_3_2_PATCH.md

# Grandgousier MVP5.3.2: исправление оси времени на графиках

Исправляет две ошибки в `export-lightcurve-plot`:

1. `--x mjd_zero` и `--x bjd_tdb_zero` подписывали ось как `MJD - 0` / `BJD_TDB - 0`, потому что offset вычислялся уже после вычитания offset.
2. `--x bjd_tdb*` фактически строил BMJD_TDB (`.mjd`), хотя по названию ожидался BJD_TDB (`.jd`).

Теперь:

- `mjd_zero`: ось `MJD - <floor(min(MJD))>`.
- `bjd_tdb_zero`: ось `BJD_TDB - <floor(min(BJD_TDB))>` и используются JD-значения.
- Добавлены отдельные режимы `bmjd_tdb`, `bmjd_tdb_zero`, `bmjd_tdb_frac` для MJD-шкалы в TDB.

## Установка

```powershell
Copy-Item .\grandgousier\visualization\lightcurve_plot.py C:\PycharmProjects\Grandgousier\grandgousier\visualization\lightcurve_plot.py -Force
Copy-Item .\grandgousier\cli.py C:\PycharmProjects\Grandgousier\grandgousier\cli.py -Force
cd C:\PycharmProjects\Grandgousier
.\.venv\Scripts\python.exe -m pip install -e .
```

## Проверка

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x mjd_zero `
  --show-stats `
  --out exports\lc_mjd_zero_fixed.png
```

Ось должна быть `MJD - 60206`, а не `MJD - 0`.

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x bjd_tdb_zero `
  --show-stats `
  --out exports\lc_bjd_tdb_zero_fixed.png
```

Ось должна быть вида `BJD_TDB - 2460207`, а не `BJD_TDB - 0`.

Для барицентрической MJD-шкалы можно использовать:

```powershell
--x bmjd_tdb_zero
```

### README_MVP5_3_PATCH.md

# Grandgousier MVP5.3: export-lightcurve-plot

Adds a diagnostic plot export command for light curves stored in `measurements`.

No database migration is required.

## Files changed

- `grandgousier/cli.py`
- `grandgousier/visualization/__init__.py`
- `grandgousier/visualization/lightcurve_plot.py`
- `requirements-photometry.txt`

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install matplotlib
```

or, if you use optional photometry requirements:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-photometry.txt
```

## Example: calibrated magnitude light curve

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --out exports\lc_2304900388447536128.png
```

Default axes:

- x: `mjd`
- y: `mag_cal`
- magnitude axis inverted: yes
- error bars: enabled when `mag_err` is present

## Alternative plots

Raw instrumental magnitude:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name raw_aperture_r3_ann8_12_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --y mag_inst `
  --out exports\lc_2304900388447536128_raw.png
```

Flux curve:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --y flux `
  --no-invert-y `
  --out exports\lc_2304900388447536128_flux.png
```

Plot against frame id:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name preliminary_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x frame_id `
  --out exports\lc_2304900388447536128_by_frame.png
```

## Notes

This is for quick visual testing. It does not replace final plotting/QC reports.
The command imports matplotlib only when called, so normal indexing/database commands do not depend on plotting libraries.

### README_MVP5_4_PATCH.md

# Grandgousier MVP5.4: ensemble differential calibration

This patch adds a first internal ensemble calibration stage for already measured light curves.

It does not change the database schema.

## Files

- `grandgousier/cli.py`
- `grandgousier/calibration/ensemble.py`

## Install

Copy the files into the project root and reinstall editable package:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Recommended workflow

The intended source calibration is the preliminary frame-QC Gaia zero-point calibration:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli ensemble-calibrate `
  --source-calibration-name preliminary_gaia_g_zp_v1 `
  --target-calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-ref-measurements 10 `
  --min-ref-stars-per-frame 30 `
  --max-ref-rms 0.08 `
  --max-ref-mad 0.05 `
  --n-iterations 3 `
  --dry-run
```

Real run:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli ensemble-calibrate `
  --source-calibration-name preliminary_gaia_g_zp_v1 `
  --target-calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-ref-measurements 10 `
  --min-ref-stars-per-frame 30 `
  --max-ref-rms 0.08 `
  --max-ref-mad 0.05 `
  --n-iterations 3
```

## Summaries

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli ensemble-frame-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1

.\.venv\Scripts\python.exe -m grandgousier.cli measurement-summary

.\.venv\Scripts\python.exe -m grandgousier.cli object-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --limit 20
```

## Plot a light curve after ensemble calibration

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-lightcurve-plot `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304900388447536128 `
  --x bjd_tdb_zero `
  --show-stats `
  --out exports\lc_2304900388447536128_ensemble.png
```

## Model

For each reference object, the code estimates a robust median magnitude. For each frame, it estimates a median residual over reference stars:

```text
frame_offset_j = median_i(m_ij - median(m_i))
```

Then it writes a new calibrated measurement set:

```text
mag_cal_ensemble = input_mag - frame_offset_j
```

The input magnitude is `mag_cal` by default, with fallback to `mag_inst`. Use `--use-mag-inst` to prefer instrumental magnitudes instead.

This is an internal differential calibration stage. It is not a final standard-system transform and it does not include a color term.

### README_MVP5_5_QC_PATCH.md

# Grandgousier MVP5.5 — light-curve quality diagnostics

This patch adds quality-control commands for the already computed light curves.
It does not change the database schema.

## Files

```text
grandgousier/cli.py
grandgousier/quality/__init__.py
grandgousier/quality/lightcurve_quality.py
grandgousier/visualization/rms_mag_plot.py
patch.diff
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Matplotlib is required for plot export:

```powershell
.\.venv\Scripts\python.exe -m pip install matplotlib
```

## RMS--magnitude summary

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli rms-mag-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --bin-width 0.5
```

## RMS--magnitude plot

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-rms-mag-plot `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --out exports\rms_mag_ensemble.png
```

Optional y-limit:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-rms-mag-plot `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --ylim-max 0.15 `
  --out exports\rms_mag_ensemble_limited.png
```

## Variable-candidate triage

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli variable-candidate-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --limit 30
```

This ranks objects by `mag_rms / local_median_rms` in their magnitude bin.
It is only a triage metric, not a final variability classifier.

## Compare calibrations

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compare-calibrations `
  --left preliminary_gaia_g_zp_v1 `
  --right ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --limit 20
```

The command prints median RMS values and per-object RMS ratios. Values below 1.0 mean the right calibration has smaller RMS.

## Notes

The diagnostics use stored `measurements` and do not recompute photometry. For MVP5.5, the statistics are based on `mag_cal` values of one calibration version. The ensemble calibration still has no color term or spatial correction; a good RMS--magnitude diagram here only means the internal series calibration improved.

### README_MVP5_6_1_PATCH.md

# MVP5.6.1: show-object-qc flag filtering fix

Fixes `show-object-qc --flag ...` returning no rows when matching flagged rows occur after the first page.

## Problem

The old implementation applied SQL `LIMIT/OFFSET` before Python-side JSON flag filtering. If the first `--limit` rows had no `CROWDED` flag, this returned `No rows.` even when `object-qc-summary` reported `CROWDED > 0`.

## Fix

`show-object-qc` now:

1. Loads rows matching ordinary SQL filters.
2. Applies `--flag` / `--exclude-flag` to `ObjectQc.flags`.
3. Applies `--offset` / `--limit` after flag filtering.
4. Normalizes CLI flag names to uppercase.

## Apply

Copy `grandgousier/cli.py` from this patch over the project file, then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Test

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-object-qc `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --flag CROWDED `
  --limit 20
```

### README_MVP5_6_OBJECT_QC_PATCH.md

# Grandgousier MVP5.6 — object QC flags

This patch adds object-level quality control used to filter false variable candidates caused by crowding, edge proximity, saturation-like measurements, and noisy/faint objects.

## Files

- `grandgousier/db/models.py`
- `grandgousier/cli.py`
- `grandgousier/quality/object_quality.py`
- `grandgousier/quality/__init__.py`
- `scripts/migrate_mvp5_6_object_qc.sql`

## Migration

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_6_object_qc.sql
```

## Reinstall

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Compute object QC

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 5 `
  --blended-arcsec 3 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08
```

## Inspect QC

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli object-qc-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1

.\.venv\Scripts\python.exe -m grandgousier.cli show-object-qc `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --flag CROWDED `
  --limit 20
```

## Filter variable candidates

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli variable-candidate-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --max-mag-median 22.5 `
  --limit 30
```

## Filter RMS plot

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-rms-mag-plot `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --ylim-max 0.15 `
  --out exports\rms_mag_ensemble_clean.png
```

## Notes

`ObjectQc` is not a variability classifier. It is a triage layer. It removes common false positives from the candidate list before more expensive analysis.

Important flags:

- `CROWDED`: nearest catalog neighbor is closer than `--crowded-arcsec`.
- `BLENDED_NEIGHBOR`: nearest neighbor is closer than `--blended-arcsec`.
- `EDGE`: at least one measurement is too close to frame edge.
- `SATURATED_LIKE`: forced photometry flagged saturation-like pixels.
- `HIGH_RMS`: object RMS is above the configured absolute threshold.
- `FAINT_NOISY`: median magnitude error is above the configured threshold.

### README_MVP5_7_DETECTION_SUPPORT_PATCH.md

# Grandgousier MVP5.7 — detection support and catalog-residual QC

This patch implements the first four MVP5.7 items:

1. `object_qc` now stores detection-support metrics in `details["detection_support"]`:
   - `n_detection_matches`
   - `detection_fraction`
   - `det_sep_median_arcsec`
   - `det_snr_median`
   - `det_snr_max`

2. `object_qc` now stores catalog/photometry consistency in `details["photometric_consistency"]`:
   - `ref_mag`
   - `mag_ref_delta = mag_median - ref_mag`
   - `abs_mag_ref_delta`

3. New flags:
   - `NO_DETECTION_SUPPORT`
   - `WEAK_DETECTION_SUPPORT`
   - `LARGE_CATALOG_MAG_RESIDUAL`
   - `POSSIBLE_PROPER_MOTION_MISMATCH`

4. `variable-candidate-summary` now supports:
   - `--require-detection-support`
   - `--max-ref-mag-delta`
   - `--exclude-catalog-outliers`

No database migration is required. The new values are stored inside `object_qc.details` JSONB and `object_qc.flags`.

## Apply

Copy the patched files:

```text
grandgousier/quality/object_quality.py
grandgousier/cli.py
```

Then reinstall:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Recompute object QC

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 5 `
  --blended-arcsec 3 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08 `
  --min-detection-fraction 0.5 `
  --max-ref-mag-delta 2.0
```

## Inspect QC

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli object-qc-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli show-object-qc `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --flag LARGE_CATALOG_MAG_RESIDUAL `
  --limit 30
```

## Filter candidate variables

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli variable-candidate-summary `
  --calibration-name ensemble_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 30
```

### README_MVP5_8_APERTURE_ZP_PATCH.md

# Grandgousier MVP5.8 — forced-aperture zero point calibration

This patch adds `aperture-zp-calibrate`.

It fixes the calibration mismatch where a zero point derived from `source_detections`
was applied to forced-aperture `measurements`. The new command derives frame zero
points directly from the same forced-aperture `mag_inst` values.

No database migration is required.

## Files

```text
grandgousier/calibration/aperture_zp.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Dry-run

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli aperture-zp-calibrate `
  --source-calibration-name raw_aperture_r3_ann8_12_v1 `
  --target-calibration-name preliminary_aperture_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --mag-key G `
  --min-ref-stars-per-frame 30 `
  --max-mag-err 0.08 `
  --clip-sigma 3 `
  --n-iterations 3 `
  --dry-run
```

## Real run

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli aperture-zp-calibrate `
  --source-calibration-name raw_aperture_r3_ann8_12_v1 `
  --target-calibration-name preliminary_aperture_gaia_g_zp_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --mag-key G `
  --min-ref-stars-per-frame 30 `
  --max-mag-err 0.08 `
  --clip-sigma 3 `
  --n-iterations 3
```

## Then ensemble-calibrate again from the aperture-ZP version

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli ensemble-calibrate `
  --source-calibration-name preliminary_aperture_gaia_g_zp_v1 `
  --target-calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-ref-measurements 10 `
  --min-ref-stars-per-frame 30 `
  --max-ref-rms 0.08 `
  --max-ref-mad 0.05 `
  --n-iterations 3
```

## Recompute object QC on the corrected calibration

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli compute-object-qc `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --crowded-arcsec 5 `
  --blended-arcsec 3 `
  --edge-distance-pix 10 `
  --high-rms 0.10 `
  --high-mag-err 0.08 `
  --min-detection-fraction 0.5 `
  --max-ref-mag-delta 2.0
```

## Candidate query

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli variable-candidate-summary `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --min-measurements 10 `
  --min-excess 2.0 `
  --exclude-crowded `
  --exclude-edge `
  --exclude-saturated `
  --exclude-catalog-outliers `
  --require-detection-support `
  --max-ref-mag-delta 2.0 `
  --max-mag-median 22.5 `
  --limit 30
```

Expected outcome: `LARGE_CATALOG_MAG_RESIDUAL` should no longer flag all objects. Some residuals remain expected because this is still Gaia G applied to MASTER I without a color term.

### README_MVP5_9_OBJECT_CUTOUT_PATCH.md

# Grandgousier MVP5.9 — object frame finder and FITS cutouts

This patch adds two commands:

```text
find-object-frames
export-object-cutout
```

No database migration is required.

## Files

```text
grandgousier/visualization/object_cutout.py
grandgousier/cli.py
```

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## List frames for a candidate

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli find-object-frames `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --sort time
```

Useful alternatives:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli find-object-frames `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --sort brightest `
  --limit 5
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli find-object-frames `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --instrument-unit-id 1 `
  --sort faintest `
  --limit 5
```

## Export one cutout

For the candidate light curve, frame 17 is among the brightest points and frame 7 is among the faintest points.

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-object-cutout `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --frame-id 17 `
  --size-pix 80 `
  --out exports\cutout_2304896849394561408_frame17_bright.png
```

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli export-object-cutout `
  --calibration-name ensemble_aperture_gaia_g_v1 `
  --catalog-name gaia_dr3_master `
  --source-id 2304896849394561408 `
  --frame-id 7 `
  --size-pix 80 `
  --out exports\cutout_2304896849394561408_frame7_faint.png
```

The PNG overlay contains:

- forced-aperture/catalog position;
- aperture circle;
- all source detections inside the cutout;
- matched source detection when available.

If too many detections clutter the image:

```powershell
--no-detections
```

If the image stretch is poor, adjust:

```powershell
--vmin-percentile 5 --vmax-percentile 99
```

### README_MVP5_PATCH.md

# Grandgousier MVP5: source detection + Gaia frame catalog query

This patch adds two independent branches:

1. image-driven source detection stored in `source_detections`;
2. Gaia DR3 cone query for selected frame footprints, writing to `catalog_sources` and optionally projecting into `frame_catalog_links`.

## Files

```text
grandgousier/db/models.py
grandgousier/cli.py
grandgousier/photometry/source_detection.py
grandgousier/catalogs/gaia_query.py
requirements-photometry.txt
scripts/migrate_mvp5_detection_catalog.sql
patch.diff
```

## Database migration

Run once on the existing DB:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_detection_catalog.sql
```

Then reinstall editable package:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## Source detection

Dry run on two MASTER frames:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli detect-sources `
  --instrument-unit-id 1 `
  --limit-frames 2 `
  --threshold-sigma 5 `
  --dry-run
```

Real run, replacing old detections for selected frames:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli detect-sources `
  --instrument-unit-id 1 `
  --threshold-sigma 5 `
  --max-sources-per-frame 2000 `
  --replace-existing
```

Inspect:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli source-detection-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-source-detections --instrument-unit-id 1 --limit 20
.\.venv\Scripts\python.exe -m grandgousier.cli db-summary
```

The detector is deliberately minimal: robust median/MAD background, thresholded local maxima, simple non-max suppression, positive center-of-mass centroid, rough aperture flux. It does not replace photutils/SEP; it is an MVP branch for transient/detection plumbing.

## Gaia DR3 query for frame footprint

Install optional query dependency if needed:

```powershell
.\.venv\Scripts\python.exe -m pip install astroquery
```

Query one MASTER frame and only bright Gaia sources:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli query-gaia-for-frames `
  --catalog-name gaia_dr3_master_test `
  --frame-id 1 `
  --mag-limit 18 `
  --max-rows-per-frame 20000 `
  --match
```

Or query all frames in one instrument unit:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli query-gaia-for-frames `
  --catalog-name gaia_dr3_master `
  --instrument-unit-id 1 `
  --mag-limit 18 `
  --match
```

Inspect:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli catalog-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-catalog-sources --catalog-name gaia_dr3_master --limit 20
```

## Notes

`query-gaia-for-frames` needs internet access and astroquery. If network access is unreliable, use the existing `import-catalog` command with a local CSV/FITS/ECSV catalog instead.

### README_PATCH.md

# Grandgousier MVP2.1 patch

Files in this archive replace the corresponding files in the project:

- `grandgousier/cli.py`
- `grandgousier/astrometry/wcs_check.py`

Changes:

- Adds `show-frames` command.
- Adds `frame-summary` command.
- Suppresses Astropy `FITSFixedWarning` during WCS header parsing.

No database migration is required.

Install after copying:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Quick checks:

```powershell
.\.venv\Scripts\python.exe -m grandgousier.cli frame-summary
.\.venv\Scripts\python.exe -m grandgousier.cli show-frames --limit 10
.\.venv\Scripts\python.exe -m grandgousier.cli show-frames --instrument MASTER --limit 5 --format csv
```



---

## MVP5.30.5 — explicit derived-calibration provenance and inherited aperture QC

Patch `0.5.30.5` fixes the remaining aperture-stability propagation gap for derived calibrations.

Changes:

- `calibration_versions` now has explicit `source_calibration_version_id` provenance.
- New migration: `scripts/migrate_mvp5_30_5_calibration_provenance.sql`.
- Derived calibrations produced by preliminary, aperture-ZP, ensemble and PS1/color calibration now write both:
  - `calibration_versions.source_calibration_version_id`;
  - legacy JSON keys `parameters.source_calibration_name` and `parameters.source_calibration_version_id`.
- `compute-object-qc` resolves the source calibration by FK first, then by legacy JSON fallback.
- `compute-object-qc` inherits `aperture_stability`, `APERTURE_UNSTABLE` and `APERTURE_MEDIAN_SHIFT` from the source calibration when the target calibration does not have its own aperture-stability layer.
- New maintenance command:

```bash
grandgousier transfer-aperture-stability-qc \
  --target-calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 \
  --source-calibration-name master_test_20230919_s1_v30_aperture_zp_i_v1 \
  --catalog-name gaia_dr3_master
```

Without `--source-calibration-name`, the command uses target calibration provenance. By default it updates only existing target `object_qc` rows. Add `--create-missing` only when a minimal inherited aperture-stability row is acceptable.

`field-qc-report` now includes inherited source aperture-stability counts in `object_qc.flag_counts`, so `APERTURE_UNSTABLE` / `APERTURE_MEDIAN_SHIFT` remain visible for PS1/color-derived calibration reports after the source calibration has already been diagnosed.

---

## MVP5.30.7 — smarter field-series resolver for field-qc-report

Patch `0.5.30.7` fixes the `field-qc-report --field-series-name` case where the persistent `field_series` row was created under an older pipeline prefix, but the user passes a newer derived calibration prefix.

The resolver now derives aliases from the target calibration and its source-calibration chain and can fall back to frame overlap between `measurements.calibration_version_id` and `field_series_frames`. This makes names like `master_test_20230919_s1_v30` resolve to the existing series whose stored `last_calibration_prefix` is `master_test_20230919_s1_v27`, when the target calibration is `master_test_20230919_s1_v30_ps1_i_color_v1`.

If no series can be resolved, the error now lists available `field_series` rows instead of returning a bare `ValueError`.

Example:

```powershell
python -m grandgousier.cli field-qc-report `
  --field-series-name master_test_20230919_s1_v30 `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --out reports\check_05307_series_filtered_by_name.json
```

Expected result for the current test database: `frame_selection_mode=field_series`, `series_id=1`, `selected_frame_ids_count=18`, `n_frames=18`.


---

## MVP5.30.8 — pipeline idempotency audit

Patch `0.5.30.9` adds a read-only idempotency audit for repeated `run-field-pipeline` executions.

New command:

```powershell
python -m grandgousier.cli pipeline-idempotency-report `
  --field-series-name master_test_20230919_s1_v30 `
  --calibration-name master_test_20230919_s1_v30_ps1_i_color_v1 `
  --catalog-name gaia_dr3_master `
  --out reports\check_05308_idempotency.json
```

The audit checks logical duplicate identities in:

- `measurements`: `object_id, frame_id, calibration_version_id`;
- `object_qc`: `object_id, catalog_name, instrument_unit_id, calibration_version_id, algorithm`;
- `source_detections`: `frame_id, algorithm, rounded x/y position`;
- `detection_catalog_matches`: `frame_id, catalog_source_id, catalog_name, algorithm`;
- `frame_qc`: `frame_id, catalog_name, algorithm`;
- `event_detection_links`: `event_id, source_detection_id` when the event-detection layer exists.

The command returns exit code `0` for OK/WARNING and `2` when real duplicate violations are found. `MISSING` checks are used for optional tables such as `event_detection_links` when the corresponding migration has not been applied.

This patch does not change existing science tables and does not require a DB migration. It is a diagnostic guard before and after repeated pipeline runs.


## Patch 0.5.30.9

Fixes `run-field-pipeline` failure introduced by provenance support: aperture/preliminary/ensemble calibration modules now import `set_source_calibration`, and regression coverage checks that the symbol is available before pipeline runtime. No database migration is required.

---

## MVP5.31.0 — science light-curve product foundation

Patch `0.5.31.0` starts the main science-product layer: database-backed light curves with explicit series/calibration provenance, QC flags and an inter-series offset slot.

New migration:

```powershell
psql -U grandgousier -d grandgousier -h localhost -f scripts\migrate_mvp5_31_0_lightcurve_product.sql
```

The migration adds `interseries_calibration_offsets`. The table is intentionally empty until the later inter-series solver is implemented. When no offset exists, exported science light curves use the best selected calibrated magnitude as `mag_global` and set `interseries_offset_mag` to null / `NO_INTERSERIES_OFFSET`.

New commands:

```powershell
python -m grandgousier.cli list-light-curve-objects `
  --catalog-name gaia_dr3_master `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --min-points 10 `
  --format table
```

```powershell
python -m grandgousier.cli light-curve-summary `
  --catalog-name gaia_dr3_master `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --out reports\check_05310_light_curve_summary.json
```

```powershell
python -m grandgousier.cli export-light-curve `
  --catalog-name gaia_dr3_master `
  --source-id 2304906332682275456 `
  --calibration-name master_test_20230919_s1_v31_ensemble_g_v1 `
  --field-series-name master_test_20230919_s1_v31 `
  --out exports\light_curves\gaia_2304906332682275456_v31.csv
```

Science CSV columns include `object_id`, `catalog_name`, `source_id`, `frame_id`, `series_id`, `measurement_id`, `calibration_name`, `calibration_version_id`, `mjd`, `jd`, `date_obs`, instrument/filter fields, `mag_cal`, `mag_global`, `mag_global_err`, inter-series offset fields, JSON QC flags, `is_rejected` and `reject_reasons`.

Default science rejection flags include high RMS, faint/noisy objects, crowding/blend flags, edge flags, weak/no detection support, catalog outliers, aperture instability and saturated-like objects. The export keeps rejected rows by default and marks them explicitly; add `--exclude-rejected` to write only accepted science points.

The existing legacy commands `show-lightcurve` and `export-lightcurve` are unchanged. The new hyphenated `export-light-curve` is the science-product exporter.



## MVP 5.31.4 — safer auto-catalog execution

Patch-level safety update for `process-archive-path` auto catalogs. Automatic Gaia fetch now defaults to a single field-union cone per input path instead of per-frame queries. The command refuses to run the heavy photometry pipeline when an auto catalog fails or inserts zero sources, unless `--allow-empty-catalog` is explicitly supplied for debugging. Added best-effort astroquery timeout/radius controls and manifest fields for query mode, queried cones, read rows and oversized-cone skips. No database migration is required.

## MVP 5.31.2 — process-archive-path test hotfix

Patch-level fix for the process-archive-path regression test double: the fake SQLAlchemy query object now implements `one_or_none()`, matching the production path used when creating instrument units during explicit path indexing. No database migration is required.

### MVP5.31.5 — auto-catalog footprint fallback

`process-archive-path` auto-catalog mode can now build an approximate Gaia query
cone from indexed frame center coordinates and instrument pixel scale when the
raw FITS header lacks a complete celestial WCS. This is intended for RoboPhot
raw-frame bootstrapping.


## MVP5.31.6 — RoboPhot auto-catalog footprint fallback fix

Fixes `process-archive-path` auto-catalog bootstrapping for RoboPhot frames whose Gaia footprint cannot be evaluated through full Astropy WCS.  The Gaia cone builder now falls back to `CRVAL1/CRVAL2` plus FITS `CD*` matrix scale, and selected frames eager-load `instrument_unit` so the indexed-center fallback can use instrument pixel scale.  This prevents `failed_frames=all` / `queried_cones=0` on binned solved RoboPhot FITS headers.

## MVP5.31.9 — large-catalog batching and faster detection matching

The field pipeline now writes large PostgreSQL INSERT/UPSERT operations in safe
chunks instead of building one giant SQL statement.  This fixes the RoboPhot
auto-catalog case where 13k Gaia sources exceeded psycopg/PostgreSQL bind
parameter limits during forced photometry.  The same batching path is applied to
raw and derived measurements, frame-catalog links, source detections,
detection-catalog matches, and object QC rows.

Detection-to-catalog matching also uses a conservative pixel-grid prefilter
before the final sky-coordinate separation test, avoiding the expensive full
all-pairs loop for large Gaia catalogs.



## MVP5.32.0 — staged optimized photometry, pre-QC and working catalog

Patch `0.5.32.0` changes the one-command RoboPhot/MASTER path workflow from a
fully exhaustive diagnostic run to a staged production-safe photometry run.  The
previous default could repeatedly run full forced-aperture photometry during
aperture-stability QC and could measure every Gaia source in a deep `G<20.5`
auto catalog.  In crowded/low-latitude fields this produced several full
`~5e5` measurement passes and multi-hour executions.

Key changes:

- `process-archive-path` now runs a cheap pre-photometry frame QC stage before
  forced photometry.  It records `n_local_maxima_before_cap`,
  `DETECTION_CAP_REACHED`, bright-pixel/background-structure proxies and BAD/WARNING
  flags in `frames.quality_flags["frame_pre_photometry_qc_v1"]`.
- The source detector also records the raw candidate count before its save cap,
  so `source_detections=2000` is no longer ambiguous by itself.
- The auto Gaia identity catalog can still be deep, but the working set used for
  `frame_catalog_links` and forced photometry is separately limited.
  `process-archive-path` defaults to `--working-catalog-mag-limit 19.0`,
  `--max-working-sources-per-frame 2000`, and `--catalog-edge-margin-pix 20`.
- Existing frame-catalog links for the same catalog/frame are replaced when the
  working set is rebuilt, so old oversized links do not silently keep feeding
  photometry.
- Calibration version names include a working-set tag, for example
  `_work_g19_max2000_edge20_`, preventing accidental reuse of bloated older raw
  measurement layers.
- Raw forced photometry can reuse already complete raw measurements and can be
  explicitly recomputed with `--force-recompute-photometry`.
- `process-archive-path` disables the detection branch and aperture-stability
  sweep by default.  Use `--enable-detection-branch` and
  `--enable-aperture-stability-qc` when those diagnostics are intentionally
  needed.
- Pipeline step records now include `elapsed_sec` and are copied into the
  process manifest, making long-running stages visible.

No database migration is required.  The new pre-QC diagnostics are stored in the
existing `frames.quality_flags` JSONB column.

## MVP5.32.1 — pre-QC exclusion guard

Patch `0.5.32.1` fixes the `--exclude-bad-pre-qc-frames` control flow.

When pre-photometry QC rejects every selected frame, the pipeline now stops
before catalog matching and forced photometry.  The previous behavior converted
an empty explicit frame list to `None`, which lower-level stages interpret as
"all frames for this instrument unit".  That could make a path-scoped run resume
on unrelated frames from the same series/instrument unit.

The new behavior records a `stop_no_usable_frames` step and returns
`DONE_NO_SCIENCE_FRAMES` for the series with `science_status=BAD_FRAME_QC` in the
process manifest.  The command finishes as `DONE_WITH_WARNINGS` rather than
starting catalog matching on the wrong frame set.

No database migration is required.
