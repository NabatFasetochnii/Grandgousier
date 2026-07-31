# Grandgousier 0.5.31.2 — process-archive-path test hotfix

Patch-level hotfix for the one-command archive processing patch.

## What changed

- Bumped version to `0.5.31.2`.
- Fixed `tests/test_process_archive_path.py`: the fake SQLAlchemy query object now implements `one_or_none()`.
- No production behavior or database schema changes.
- No migration required.

## Why

`index_archive_paths()` creates or resolves an `InstrumentUnit` through SQLAlchemy's `one_or_none()` path. The regression test used a lightweight `FakeQuery` object that implemented `first()` but not `one_or_none()`, so the test failed even though the production SQLAlchemy query path is valid.

## Checks

```bash
python -m compileall grandgousier tests
python -m pytest -q
```
