# ADR 0002: Исключать accepted candidates из ensemble reference set итеративно

Статус: принято.

Дата: 2026-07-31.

## Проблема

Переменный объект в reference ensemble может частично переносить свой сигнал в калибровку. После первого исключения candidates могут проявиться новые объекты, поэтому одного дополнительного прохода недостаточно.

## Решение

Сохранены два режима:

- fixed mode — предсказуемое заданное число ensemble stages;
- until-converged mode — продолжение до отсутствия новых accepted candidate IDs с отдельным safety cap.

Requested exclusions и actual exclusions разделены: не каждый candidate входил в исходный reference candidate pool.

## Критерий сходимости

После текущего field QC вычисляется:

```text
new_ids = accepted_candidate_ids - cumulative_requested_exclusion_ids
```

При пустом `new_ids` новая calibration не запускается, в history записывается terminal entry с `executed=false`, а stop reason становится `converged`.

## Последствия

Manifest обязан различать terminal convergence check и последнюю реально выполненную calibration. Поэтому actual exclusions верхнего уровня берутся из последней history-записи с `executed=true`.
