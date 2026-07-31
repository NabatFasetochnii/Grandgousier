# Известные проблемы и ограничения

## Требуют работы

1. Полная схема БД частично задаётся ORM, частично SQL migrations. `init-db` без `scripts/migrate_all.sql` недостаточен для всех MVP5-слоёв.
2. Нет компактного end-to-end fixture для candidate convergence. Реальные проверки зависят от локальной PostgreSQL и архива BK Lyn.
3. Политика минимального числа reference objects при iterative exclusion пока не оформлена отдельным стабильным контрактом.
4. Межсерийная калибровка остаётся заделом, а не завершённым production workflow.
5. Время наблюдений не имеет общего per-measurement cache `BJD_TDB`, что ограничивает точные transit/event задачи.
6. Старые `README_MVP*.md` находятся в корне. Они сохранены ради истории, но могут содержать устаревшие команды. Для текущего состояния использовать `CONTEXT.md` и `docs/context/`.

## Исправлено в 0.5.33.3

Ранее top-level `candidate_ensemble_actual_excluded_reference_*` брался из последней history-записи. В convergence mode эта запись могла быть terminal no-op с `executed=false`, поэтому итог ошибочно показывал ноль actual exclusions. Теперь используется последняя выполненная ensemble-итерация.

## Интерпретационные ограничения

- Detection support — QC-признак, а не доказательство физической переменности.
- Candidate exclusion меняет reference set и может выявлять новые candidates; это не гарантирует, что каждый новый candidate астрофизически реален.
- Цветовая калибровка улучшает привязку к внешней системе, но не обязана уменьшать временной RMS.
