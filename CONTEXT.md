# Grandgousier: входной контекст

Текущая версия: `0.5.33.3`.

Этот файл — короткий индекс для продолжения работы в новом чате. Он не должен превращаться в полный журнал разработки и не должен перезаписываться автоматически.

## Порядок чтения

1. [`docs/context/CURRENT_STATE.md`](docs/context/CURRENT_STATE.md) — подтверждённое текущее состояние.
2. [`docs/context/NEXT_TASKS.md`](docs/context/NEXT_TASKS.md) — ближайшие задачи и критерии готовности.
3. [`docs/context/KNOWN_ISSUES.md`](docs/context/KNOWN_ISSUES.md) — известные ограничения и дефекты.
4. [`docs/context/TEST_DATASETS.md`](docs/context/TEST_DATASETS.md) — контрольные наборы и идентификаторы.
5. [`docs/context/COMMANDS.md`](docs/context/COMMANDS.md) — воспроизводимые команды.
6. [`docs/CURRENT_ARCHITECTURE.md`](docs/CURRENT_ARCHITECTURE.md) — фактическая архитектура.
7. [`docs/releases/`](docs/releases/) — изменения по версиям.
8. [`docs/decisions/`](docs/decisions/) — принятые архитектурные решения.

Машиночитаемое резюме: [`context/project_state.yaml`](context/project_state.yaml).

## Создание переносного файла для нового чата

```powershell
python scripts\build_context_bundle.py
```

С конкретным manifest последнего контрольного запуска:

```powershell
python scripts\build_context_bundle.py `
  --manifest reports\check_05332_bklyn_20260328_i_candidate_excluded_until_converged.json
```

Результат записывается в `artifacts/context/`. Сгенерированные bundle-файлы не являются первичным источником истины: править нужно документы из `docs/context/` и `context/project_state.yaml`.
