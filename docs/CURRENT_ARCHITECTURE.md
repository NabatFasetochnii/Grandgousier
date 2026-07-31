# Grandgousier 0.5.33.3: фактическая инженерная карта

Документ фиксирует фактическую архитектуру кода после серии MVP5-патчей. Старая инженерная карта описывала целевую систему в терминах `frames`, `objects`, `measurements`, `transient_candidates`, self-calibration и PostgreSQL. Текущий код уже шире: transient-ветка реализована через `source_detections`, `detection_catalog_matches` и event/QC-слои, а обработка поля ведется через persistent `fields` / `field_series` и `pipeline_runs`.

## 1. Текущий статус

Текущая версия: `0.5.33.3`.

Рабочее состояние: MVP5-пайплайн для MASTER-like архивов. Основной поток остается catalog-forced aperture photometry. Детекции и события отделены от основной forced-фотометрии, чтобы не загрязнять таблицу `measurements` артефактами повторного детектирования.

Основные реализованные блоки:

1. индексирование FITS-кадров в `frames` и `instrument_units`;
2. проверка WCS по header и запись diagnostics в `quality_flags`;
3. импорт identity-каталога в `catalog_sources`;
4. импорт внешней reference photometry в `reference_photometry`;
5. проекция каталожных источников в кадры через `frame_catalog_links`;
6. forced aperture photometry в `measurements`;
7. aperture-derived ZP calibration;
8. ensemble differential calibration;
9. PS1/reference-band color calibration;
10. source detection как отдельная ветка;
11. detection-catalog matching и frame-level QC;
12. object-level QC, blend/crowding diagnostics, aperture stability diagnostics;
13. event registry и покрытие событий кривыми блеска/детекциями;
14. grouping кадров в `fields` и `field_series`;
15. field pipeline runner с `pipeline_runs`, `pipeline_run_steps` и structured logs;
16. compact field QC report и сравнение calibration versions;
17. science light-curve product с QC/provenance;
18. one-command `process-archive-path` для штатной обработки директории FITS;
19. multi-channel candidate selection и экспорт accepted candidate light curves;
20. candidate-excluded ensemble calibration в fixed и until-converged режимах;
21. provenance requested exclusions, actual reference exclusions и convergence history.

## 2. Фактические слои системы

```text
Raw FITS archive
  -> index-archive
  -> frames + instrument_units
  -> check-wcs
  -> import/query identity catalog
  -> match-catalog-to-frames
  -> run-forced-photometry
  -> aperture-zp-calibrate
  -> ensemble-calibrate
  -> import/reference photometry
  -> compute/apply color calibration
  -> compute object/frame/field QC
  -> candidate triage / light-curve exports

Source-detection branch
  -> detect-sources
  -> match-detections-to-catalog
  -> frame-qc-summary / source-catalog-match-summary
  -> event-detection links

Field/series orchestration
  -> build-field-series
  -> process-archive-path / run-field-pipeline / run-archive-pipeline
  -> process-archive-path restricts work to current input frame_ids
  -> optional auto_catalog_* Gaia identity catalog
  -> pipeline_runs + pipeline_run_steps + pipeline_log_events
  -> field-qc-report               # use --series-id / --field-series-name for strict series reports
```

Ключевой принцип не изменился: forced-фотометрия и детекции - разные потоки. Детекции используются как QC-поддержка, transient/event layer и sanity check, но не заменяют forced measurements.

## 3. PostgreSQL: фактические таблицы

Таблицы, создаваемые ORM-моделью `grandgousier.db.models`:

- `instrument_units` - отдельные photometric systems: instrument/site/tube/camera/filter/mode.
- `frames` - индекс raw FITS, normalized metadata, WCS/flat/dark/status flags.
- `processing_events` - frame-level сообщения ранних этапов.
- `catalog_sources` - identity-каталог, обычно Gaia-like.
- `reference_photometry_catalogs` - описание внешних фотометрических каталогов.
- `reference_photometry` - внешний photometric reference layer, например PS1/APASS.
- `frame_catalog_links` - связь catalog source с frame footprint / pixel projection.
- `source_detections` - результаты независимого source detection.
- `detection_catalog_matches` - связь detection -> catalog source.
- `frame_qc` - frame-level QC на основе detection/catalog cross-match.
- `objects` - внутренние archive object ids.
- `object_qc` - object-level QC flags и diagnostics.
- `measurements` - forced photometry для calibration version.
- `calibration_versions` - версии калибровки и provenance.
- `interseries_calibration_offsets` - будущие межсерийные смещения для science light-curve product.

Таблицы, добавленные SQL migrations и используемые через SQL/text helpers:

- `event_registry`;
- `event_object_links`;
- `event_frame_coverage`;
- `event_detection_links`;
- `pipeline_runs`;
- `pipeline_run_steps`;
- `pipeline_log_events`;
- `fields`;
- `field_series`;
- `field_series_frames`.

Практическое следствие: `init-db` создает ORM-часть схемы, но для полной MVP5-схемы на существующей или новой базе нужно применять миграции. Для этого добавлен `scripts/migrate_all.sql`.

## 4. Калибровочная цепочка

Текущий рекомендуемый порядок для одной серии:

```text
raw_aperture_*                  # forced photometry, mag_inst/mag_cal initial
  -> aperture-zp-calibrate       # per-frame ZP from catalog/reference stars
  -> ensemble-calibrate          # internal differential correction
  -> compute-color-calibration   # diagnostics only
  -> apply-color-calibration     # reference/color-derived calibration version
  -> compute-object-qc           # QC for final calibration
  -> field-qc-report             # compact report and export tables; can be restricted to one field_series
```

Цветовая калибровка не должна оцениваться только по RMS light curves. Ее задача - перевод шкалы к выбранному внешнему photometric reference band и явная запись color-term provenance. Улучшение или ухудшение scatter в короткой серии - отдельная диагностика через `compare-calibrations`.

## 5. QC-слои

Фактически разделены несколько уровней QC:

- frame-level WCS/QC: WCS RMS, match counts, ZP, background-like diagnostics;
- detection-catalog QC: support fraction по уникальным кадрам, а не по raw match count;
- object-level QC: crowding, blends, edge, saturation-like, high RMS, weak/no detection support, possible proper-motion mismatch;
- dynamic blend diagnostics: thresholds depend on FWHM, aperture radius and pixel scale;
- aperture sweep/stability: flags `APERTURE_UNSTABLE`, `APERTURE_MEDIAN_SHIFT`;
- candidate triage: явные причины отклонения кандидатов;
- field report: compact JSON/CSV summary for one calibration/series.
- science light-curve product: `list-light-curve-objects`, `light-curve-summary`, `export-light-curve` собирают точки с provenance, QC flags и полем будущего inter-series offset.
- one-command archive path processing: `process-archive-path` автоматически сканирует FITS-директорию, определяет `MASTER`/`RoboPhot`, индексирует кадры, создает `field_series` из frame_ids текущего пути и запускает стандартный field pipeline только для этих серий. Если `--catalog-name` не задан, генерируется `auto_catalog_*` и выполняется попытка Gaia DR3 query.

Патч 0.5.30.3 зафиксировал важное правило: aperture-stability flags должны сохраняться при пересчете object QC для derived calibrations. Эти флаги относятся к объекту/апертуре/серии, а не к абсолютной photometric scale.

## 6. Риски, которые остаются открытыми

1. Схема БД частично живет в ORM, частично в SQL migrations. Это допустимо для MVP, но в дальнейшем лучше перейти на единый migration manager.
2. `README.md` содержит длинную историю MVP-патчей. Она полезна как журнал, но плохо работает как краткое руководство.
3. Большая часть end-to-end сценариев пока не покрыта тестами. Нужны regression fixtures на маленькой synthetic/real серии.
4. Цветовая калибровка требует осторожной интерпретации: она улучшает физическую шкалу, но не обязана снижать временной RMS.
5. Detection support не является доказательством реальности переменности; это только дополнительный фильтр против ложных candidates.
6. Для точных transit/event задач время `mjd + 2400000.5` недостаточно. Нужен per-measurement `BJD_TDB` cache.

## 7. Ближайшая инженерная задача

Ближайший этап — стабилизация candidate-excluded ensemble и воспроизводимости:

1. проверить 0.5.33.3 на реальном BK Lyn manifest и подтвердить final actual exclusions;
2. добавить компактный end-to-end regression fixture для convergence mode;
3. формализовать безопасную остановку при деградации usable reference set;
4. держать чистый source archive без `.venv`, `.idea`, `__pycache__`, logs, reports, exports и локальных SQL backups;
5. использовать `CONTEXT.md`, `docs/context/` и `context/project_state.yaml` как текущий контекст;
6. использовать patch-only архивы для следующих изменений;
7. после стабилизации перейти к межсерийным photometric offsets.

Точные задачи и критерии проверки хранятся в `docs/context/NEXT_TASKS.md`.
