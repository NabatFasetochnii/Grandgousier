from __future__ import annotations

from types import SimpleNamespace

import pytest

import grandgousier.db.init_db as init_module
import grandgousier.db.migrations as migration_module
from grandgousier.db.models import Base
from grandgousier.db.schema_contract import CURRENT_SCHEMA_TABLES
from sqlalchemy import ForeignKeyConstraint


class _Inspector:
    def __init__(self, tables):
        self._tables = list(tables)

    def get_table_names(self):
        return list(self._tables)


def test_init_db_bootstraps_empty_clean_schema_and_baselines(monkeypatch) -> None:
    assert len(migration_module.MIGRATIONS) == 37
    assert sum(isinstance(c, ForeignKeyConstraint) for c in
               Base.metadata.tables["interseries_calibration_edges"].constraints) == 5
    assert sum(isinstance(c, ForeignKeyConstraint) for c in
               Base.metadata.tables["interseries_calibration_series"].constraints) == 5
    engine = object()
    events: list[object] = []
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(init_module, "_assert_postgresql_15_or_newer", lambda _engine: None)
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
    monkeypatch.setattr(init_module, "_assert_postgresql_15_or_newer", lambda _engine: None)
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
    monkeypatch.setattr(init_module, "_assert_postgresql_15_or_newer", lambda _engine: None)
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
    monkeypatch.setattr(init_module, "_assert_postgresql_15_or_newer", lambda _engine: None)
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


def test_complete_three_edge_fk_schema_cannot_be_rebaselined_by_fake_bootstrap(monkeypatch) -> None:
    engine = object()
    events: list[str] = []
    monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
    monkeypatch.setattr(init_module, "_assert_postgresql_15_or_newer", lambda _engine: None)
    monkeypatch.setattr(init_module, "inspect", lambda _engine: _Inspector(CURRENT_SCHEMA_TABLES))
    monkeypatch.setattr(init_module.Base.metadata, "create_all", lambda _engine: events.append("create"))

    def reject_old(_engine):
        events.append("assert")
        raise RuntimeError("M4 old three-edge-FK schema is not the current contract")

    monkeypatch.setattr(init_module, "assert_schema_contract", reject_old)
    monkeypatch.setattr(init_module, "baseline_current_schema", lambda *_args, **_kwargs:
                        pytest.fail("old three-edge-FK schema must not be baselined"))
    with pytest.raises(RuntimeError, match="M4 old three-edge-FK"):
        init_module.init_db()
    assert events == ["create", "assert"]  # Fake only; do not run init-db on the TEST database.


class _VersionConnection:
    def __init__(self, version: str, events: list[str]):
        self.version = version
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.events.append("close")

    def exec_driver_sql(self, statement: str):
        assert statement == "SELECT current_setting('server_version_num')"
        self.events.append("version")
        return SimpleNamespace(scalar_one=lambda: self.version)


def test_init_and_reset_check_postgresql_version_before_any_ddl(monkeypatch) -> None:
    for operation in (init_module.init_db, init_module.reset_db):
        events: list[str] = []
        engine = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            connect=lambda: _VersionConnection("140000", events),
        )
        monkeypatch.setattr(init_module, "make_engine", lambda _url=None: engine)
        monkeypatch.setattr(init_module, "inspect", lambda _engine: pytest.fail("inspect after old version"))
        monkeypatch.setattr(init_module.Base.metadata, "create_all", lambda _engine: pytest.fail("create DDL"))
        monkeypatch.setattr(init_module.Base.metadata, "drop_all", lambda _engine: pytest.fail("drop DDL"))
        with pytest.raises(RuntimeError, match="PostgreSQL 15 or newer"):
            operation()
        assert events == ["version", "close"]


def test_version_preflight_accepts_postgresql_15_without_ddl() -> None:
    events: list[str] = []
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: _VersionConnection("150000", events),
    )
    init_module._assert_postgresql_15_or_newer(engine)
    assert events == ["version", "close"]


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
