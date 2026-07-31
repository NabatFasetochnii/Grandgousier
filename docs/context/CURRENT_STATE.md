# Текущее состояние Grandgousier

Обновлено: 2026-08-01.

## Версия

`0.5.33.3`

База патча: `0.5.33.2`.

## Рабочий поток

Штатный пользовательский вход — `process-archive-path --path ... --execute`. Pipeline автоматически индексирует кадры текущего пути, разрешает instrument profile и `field_series`, выполняет forced photometry, aperture-ZP calibration, ensemble calibration, QC, candidate triage и экспорт кривых блеска.

Дорогие или специализированные ветки включаются явно. Detection branch управляется `--enable-detection-branch`. Candidate-excluded ensemble calibration управляется `--exclude-candidates-from-ensemble`.

## Candidate-excluded ensemble

Поддерживаются два режима:

- fixed: `--candidate-ensemble-max-iterations N` задаёт общее число ensemble stages, включая initial pass;
- until converged: `--candidate-ensemble-until-converged` продолжает исключение новых accepted candidates до сходимости или safety cap `--candidate-ensemble-convergence-max-iterations`.

Сходимость определяется отсутствием новых accepted candidate object IDs вне cumulative requested exclusion set.

В `0.5.33.3` исправлена сводная provenance-метрика actual exclusions. Финальная проверка сходимости может создать history-запись с `executed=false`; top-level поля actual exclusions теперь берутся из последней выполненной ensemble-итерации. Поле `candidate_ensemble_actual_exclusions_iteration` показывает номер этой итерации.

## Последний подтверждённый реальный запуск

Набор: RoboPhot, BK Lyn, 2026-03-28, фильтр `i`, 70 кадров.

Manifest исходного прогона 0.5.33.2:

```text
reports/check_05332_bklyn_20260328_i_candidate_excluded_until_converged.json
```

Результат:

```text
status = DONE
science_status = OK
candidate_ensemble_iteration_mode = until_converged
candidate_ensemble_converged = true
candidate_ensemble_stop_reason = converged
candidate_ensemble_exclusion_iterations_executed = 5
final ensemble iteration = 6
requested excluded objects = 16
actual excluded reference objects in iteration 6 = 12
final accepted candidate objects = 16
exported candidate light curves = 15
final calibration = *_ensemble_g_candidate_excluded_iter6_v1
```

Патч 0.5.33.3 исправляет только сводное представление уже сохранённой history; повторная фотометрия для проверки unit-level provenance не требуется. Реальный end-to-end прогон 0.5.33.3 на BK Lyn ещё не выполнен пользователем.

## Контрольный объект

BK Lyn:

```text
Gaia DR3 source_id = 702296666944246784
catalog_source_id = 31299
object_id = 6751
```

На контрольных сериях объект устойчиво остаётся accepted candidate; iterative reference exclusion практически не меняет его параметры.

## Проверки патча

Актуальные результаты автоматических проверок записываются в release note `docs/releases/0.5.33.3.md`. Результат реального запуска следует добавить сюда после выполнения на рабочей БД.
