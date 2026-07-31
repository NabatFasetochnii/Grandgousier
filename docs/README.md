# Документация Grandgousier

## Для продолжения разработки

- [`../CONTEXT.md`](../CONTEXT.md) — входная точка для нового чата.
- [`context/CURRENT_STATE.md`](context/CURRENT_STATE.md) — текущее подтверждённое состояние.
- [`context/NEXT_TASKS.md`](context/NEXT_TASKS.md) — ближайшие задачи.
- [`context/KNOWN_ISSUES.md`](context/KNOWN_ISSUES.md) — известные проблемы.
- [`context/TEST_DATASETS.md`](context/TEST_DATASETS.md) — контрольные данные.
- [`context/COMMANDS.md`](context/COMMANDS.md) — рабочие команды.

## Архитектура и решения

- [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) — фактическая архитектура кода.
- [`decisions/`](decisions/) — ADR: причины решений, которые неочевидны из кода.

## История версий

Новые release notes хранятся в [`releases/`](releases/). Старые `README_MVP*.md` пока оставлены в корне для сохранения истории и ссылок; новые файлы такого формата создавать не следует.

## Правило обновления

При завершении значимого патча нужно обновить:

1. версию в `pyproject.toml` и `grandgousier/__init__.py`;
2. `docs/context/CURRENT_STATE.md`;
3. `docs/context/NEXT_TASKS.md` и `KNOWN_ISSUES.md`, если изменились;
4. `context/project_state.yaml`;
5. release note в `docs/releases/<version>.md`;
6. regression tests;
7. переносной bundle командой `python scripts/build_context_bundle.py`.
