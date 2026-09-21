from __future__ import annotations

from types import SimpleNamespace

import pytest

import grandgousier.db.init_db as init_module
import grandgousier.db.migrations as migration_module
from grandgousier.db.schema_contract import CURRENT_SCHEMA_TABLES


class _Inspector:
    def __init__(self, tables):
        self._tables = list(tables)

    def get_table_names(self):
        return list(self._tables)


def test_init_db_bootstraps_empty_clean_schema_and_baselines(monkeypatch) -> None:
    engine = object()
    events: list[object] = []
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(init_module, "inspect", lambda _engine: _Inspector([]))
    monkeypatch.setattr(init_module.Base.metadata, "create_all", lambda actual: events.append(("create", actual)))
    monkeypatch.setattr(init_module, "assert_schema_contract", lambda actual: events.append(("assert", actual)))
    monkeypatch.setattr(
        init_module,
        "baseline_current_schema",
        lambda actual, *, reason: events.append(("baseline", actual, reason)),
    )

    init_module.init_db("postgresql://unused")

    assert events == [
        ("create", engine),
        ("assert", engine),
        ("baseline", engine, "fresh_orm_init"),
    ]


def test_init_db_refuses_partial_clean_schema_before_create(monkeypatch) -> None:
    engine = object()
    existing = set(CURRENT_SCHEMA_TABLES) - {"science_photometry"}
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(init_module, "inspect", lambda _engine: _Inspector(existing))
    monkeypatch.setattr(
        init_module.Base.metadata,
        "create_all",
        lambda _engine: pytest.fail("create_all must not run for a partial clean schema"),
    )

    with pytest.raises(RuntimeError, match="partial Grandgousier 0.7 schema"):
        init_module.init_db()


def test_init_db_refuses_legacy_06_schema_before_create(monkeypatch) -> None:
    engine = object()
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(
        init_module,
        "inspect",
        lambda _engine: _Inspector(set(CURRENT_SCHEMA_TABLES) | {"science_measurements"}),
    )
    monkeypatch.setattr(
        init_module.Base.metadata,
        "create_all",
        lambda _engine: pytest.fail("create_all must not run for a 0.6 schema"),
    )

    with pytest.raises(RuntimeError, match="fresh-baseline-only"):
        init_module.init_db()


def test_init_db_validates_complete_clean_schema_without_rebaselining(monkeypatch) -> None:
    engine = object()
    events: list[str] = []
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(init_module, "inspect", lambda _engine: _Inspector(CURRENT_SCHEMA_TABLES))
    monkeypatch.setattr(init_module.Base.metadata, "create_all", lambda _engine: events.append("create"))
    monkeypatch.setattr(init_module, "assert_schema_contract", lambda _engine: events.append("assert"))
    monkeypatch.setattr(
        init_module,
        "baseline_current_schema",
        lambda *_args, **_kwargs: pytest.fail("complete schema must not be re-baselined"),
    )

    init_module.init_db()
    assert events == ["create", "assert"]


def test_migrate_db_refuses_legacy_06_schema_before_ledger_write(monkeypatch) -> None:
    engine = SimpleNamespace()
    monkeypatch.setattr(
        migration_module,
        "inspect",
        lambda _engine: _Inspector({"science_photometry_cache", "science_flag_overrides"}),
    )
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda _engine: pytest.fail("ledger write must not happen before the clean-epoch guard"),
    )

    with pytest.raises(migration_module.MigrationError, match="fresh-baseline clean-schema"):
        migration_module.apply_pending(engine)
