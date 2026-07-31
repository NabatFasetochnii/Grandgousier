# Ближайшие задачи

## 1. Проверить 0.5.33.3 на реальном manifest

Повторить until-converged запуск BK Lyn 2026-03-28 и проверить:

```text
candidate_ensemble_converged = true
candidate_ensemble_stop_reason = converged
candidate_ensemble_actual_exclusions_iteration = 6
candidate_ensemble_actual_excluded_reference_objects = 12
candidate_ensemble_actual_excluded_reference_object_ids = history[iteration=6].actually_excluded_reference_object_ids
```

## 2. Зафиксировать минимальный end-to-end regression fixture

Нужен маленький синтетический или обезличенный набор, который покрывает:

- initial ensemble pass;
- появление нового candidate после исключения reference set;
- terminal history entry с `executed=false`;
- сходимость;
- корректное сохранение final calibration provenance.

Текущие unit tests проверяют сборку manifest, но не полную БД-цепочку.

## 3. Ограничить деградацию reference set

До массового применения convergence mode нужно явно определить политику остановки при слишком малом числе usable reference objects. Pipeline должен сохранять последнюю успешную calibration и завершаться диагностическим статусом, а не падать без пригодного результата.

## 4. Стабилизация перед следующими крупными функциями

Приоритет после candidate ensemble:

- воспроизводимый smoke test one-command pipeline;
- унификация migration management;
- межсерийные photometric offsets;
- только затем более крупные изменения схемы или PSF photometry.
