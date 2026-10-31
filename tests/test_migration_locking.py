from __future__ import annotations

import hashlib
from inspect import getsource
from pathlib import Path
from types import SimpleNamespace

import pytest

import grandgousier.db.init_db as init_db_module
import grandgousier.db.migrations as migration_module


class _Result:
    def __init__(self, value=None, rows=None, scalar_error: BaseException | None = None) -> None:
        self.value = value
        self.rows = rows or []
        self.scalar_error = scalar_error

    def scalar_one(self):
        if self.scalar_error is not None:
            raise self.scalar_error
        if isinstance(self.value, tuple):
            return self.value[0]
        return self.value

    def one(self):
        if self.scalar_error is not None:
            raise self.scalar_error
        return self.value

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self):
        assert not self.connection._in_transaction
        self.connection.transaction_id += 1
        self.connection._in_transaction = True
        self.connection.events.append(("begin", self.connection.transaction_id))
        if self.connection.on_begin is not None:
            self.connection.on_begin(self.connection)
        return self

    def __exit__(self, exc_type, _exc_value, _traceback):
        label = "rollback" if exc_type else "commit"
        self.connection.events.append((label, self.connection.transaction_id))
        self.connection._in_transaction = False
        if self.connection.on_transaction_end is not None:
            self.connection.on_transaction_end(self.connection, label)
        return False


class _Connection:
    def __init__(
        self,
        events: list[tuple[str, object]],
        *,
        lock_result: bool = True,
        unlock_result: bool = True,
        lock_exec_error: BaseException | None = None,
        lock_scalar_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.lock_result = lock_result
        self.unlock_result = unlock_result
        self.lock_exec_error = lock_exec_error
        self.lock_scalar_error = lock_scalar_error
        self._in_transaction = False
        self.transaction_id = 0
        self.lock_held = False
        self.ledger_rows: dict[str, dict[str, object]] = {}
        self.dialect = type("Dialect", (), {"paramstyle": "pyformat"})()
        self.backend_pid = 4242
        self._pool_proxy = SimpleNamespace(driver_connection=object())
        self.invalidated = False
        self.closed = False
        self.on_begin = None
        self.on_transaction_end = None
        self.on_migration_sql = None
        self.on_try_lock = None

    @property
    def connection(self):
        assert not self.closed and not self.invalidated, "physical connection is unavailable"
        return self._pool_proxy

    def switch_backend(self, *, same_pid: bool = False) -> None:
        self._pool_proxy.driver_connection = object()
        if not same_pid:
            self.backend_pid += 1
        self.lock_held = False
        self.events.append(("backend_switch", self.backend_pid))

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.close()

    def begin(self):
        return _Transaction(self)

    def exec_driver_sql(self, sql: str):
        if not self._in_transaction:
            self._in_transaction = True
        if "pg_try_advisory_lock" in sql:
            self.events.append(("try_lock", self.transaction_id))
            self.lock_held = self.lock_result
            if self.lock_exec_error is not None:
                raise self.lock_exec_error
            lock_pid = self.backend_pid
            if self.on_try_lock is not None:
                self.on_try_lock(self)
            return _Result((self.lock_result, lock_pid), scalar_error=self.lock_scalar_error)
        if "pg_backend_pid" in sql:
            self.events.append(("pid_probe", (self.transaction_id, self.backend_pid)))
            return _Result(self.backend_pid)
        if "pg_advisory_unlock" in sql:
            self.events.append(("unlock", self.transaction_id))
            if self.unlock_result:
                self.lock_held = False
            return _Result(self.unlock_result)
        self.events.append(("migration_sql", (self.transaction_id, sql)))
        if self.on_migration_sql is not None:
            self.on_migration_sql(self)
        if "BROKEN" in sql:
            raise RuntimeError("fake migration execution failure")
        return _Result()

    def execute(self, statement):
        if getattr(statement, "is_insert", False):
            self.events.append(("ledger_insert", self.transaction_id))
            values = statement.compile().params
            self.ledger_rows[str(values["migration_id"])] = dict(values)
            return _Result()
        else:
            self.events.append(("ledger_select", self.transaction_id))
        return _Result(rows=[dict(row) for row in self.ledger_rows.values()])

    def commit(self) -> None:
        self.events.append(("commit", self.transaction_id))
        self._in_transaction = False

    def rollback(self) -> None:
        self.events.append(("rollback", self.transaction_id))
        self._in_transaction = False

    def in_transaction(self) -> bool:
        return self._in_transaction

    def invalidate(self) -> None:
        self.events.append(("invalidate", self.transaction_id))
        self._in_transaction = False
        self.lock_held = False
        self.invalidated = True

    def close(self) -> None:
        self.events.append(("close", self.transaction_id))
        self._in_transaction = False
        self.closed = True


class _Engine:
    def __init__(
        self,
        events: list[tuple[str, object]],
        *,
        lock_result: bool = True,
        unlock_result: bool = True,
        lock_exec_error: BaseException | None = None,
        lock_scalar_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.connection = _Connection(
            events,
            lock_result=lock_result,
            unlock_result=unlock_result,
            lock_exec_error=lock_exec_error,
            lock_scalar_error=lock_scalar_error,
        )
        self.checkout_count = 0
        self.checked_out = False

    def connect(self) -> _Connection:
        assert not self.checked_out, "migration work must reuse its owning connection"
        self.checkout_count += 1
        self.checked_out = True
        self.events.append(("checkout", self.checkout_count))
        return self.connection


class _Inspector:
    def __init__(self, tables: tuple[str, ...] = ("schema_migrations",)) -> None:
        self.tables = tables

    def get_table_names(self):
        return list(self.tables)


def _configure_connection_inspection(monkeypatch, events, connection, tables=("schema_migrations",)):
    def inspect(bind):
        assert bind is connection
        events.append(("inspect", bind))
        return _Inspector(tables)

    monkeypatch.setattr(migration_module, "inspect", inspect)


def _managed_specs(tmp_path: Path, *contents: str) -> tuple[migration_module.MigrationSpec, ...]:
    specs = []
    for index, content in enumerate(contents, start=1):
        filename = f"migration_{index}.sql"
        (tmp_path / filename).write_text(content, encoding="utf-8")
        specs.append(migration_module.MigrationSpec(f"test_migration_{index}", filename))
    return tuple(specs)


def test_init_db_and_reset_db_are_outside_apply_pending_lock_scope() -> None:
    for operation in (init_db_module.init_db, init_db_module.reset_db):
        source = getsource(operation)
        assert "pg_try_advisory_lock" not in source
        assert "_apply_pending_with_migration_lock" not in source


def test_public_migration_status_reads_ledger_rows_from_its_engine_connection(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    (spec,) = _managed_specs(tmp_path, "SELECT 1;")
    checksum = migration_module.migration_checksum(spec, root=tmp_path)
    row = {
        "migration_id": spec.migration_id,
        "filename": spec.filename,
        "checksum_sha256": checksum,
        "state": "APPLIED",
    }
    monkeypatch.setattr(migration_module, "MIGRATIONS", (spec,))
    monkeypatch.setattr(
        migration_module,
        "_ledger_rows_from_connection",
        lambda conn: {spec.migration_id: row} if conn is engine.connection else pytest.fail(),
    )

    result = migration_module.status(engine, root=tmp_path)

    assert len(result) == 1
    assert result[0].migration_id == spec.migration_id
    assert result[0].state == "APPLIED"
    assert result[0].checksum_status == "OK"
    assert engine.checkout_count == 1


def test_apply_pending_fails_fast_on_busy_lock_before_schema_or_ledger_work(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events, lock_result=False)

    monkeypatch.setattr(migration_module, "MIGRATIONS", ())
    monkeypatch.setattr(
        migration_module,
        "inspect",
        lambda _bind: events.append(("inspect", None)) or _Inspector(),
    )
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda _bind: events.append(("ledger_table_write", None)),
    )

    with pytest.raises(migration_module.MigrationError, match="migration lock"):
        migration_module.apply_pending(engine)

    assert [label for label, _value in events[:2]] == ["checkout", "try_lock"]
    assert not {
        "inspect",
        "ledger_table_write",
        "ledger_select",
        "ledger_insert",
        "migration_sql",
    }.intersection(label for label, _value in events)
    assert [label for label, _value in events[-2:]] == ["rollback", "close"]
    assert "invalidate" not in [label for label, _value in events]
    assert "unlock" not in [label for label, _value in events]


@pytest.mark.parametrize("failure_point", ["exec_driver_sql", "one"])
def test_uncertain_try_lock_result_discards_connection_and_preserves_error(
    monkeypatch, failure_point: str
) -> None:
    events: list[tuple[str, object]] = []
    failure = RuntimeError(f"fake {failure_point} lock-result failure")
    engine = _Engine(
        events,
        lock_exec_error=failure if failure_point == "exec_driver_sql" else None,
        lock_scalar_error=failure if failure_point == "one" else None,
    )
    monkeypatch.setattr(migration_module, "MIGRATIONS", ())

    with pytest.raises(RuntimeError, match="lock-result failure") as exc_info:
        migration_module.apply_pending(engine)

    assert exc_info.value is failure
    labels = [label for label, _value in events]
    assert labels.count("checkout") == 1
    assert labels.count("try_lock") == 1
    assert labels.index("invalidate") < labels.index("close")
    assert labels[-2:] == ["invalidate", "close"]
    assert "unlock" not in labels
    assert engine.connection.lock_held is False
    assert engine.checkout_count == 1


def test_clean_epoch_guard_runs_after_lock_before_ledger_write(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    monkeypatch.setattr(migration_module, "MIGRATIONS", ())
    _configure_connection_inspection(
        monkeypatch,
        events,
        engine.connection,
        tables=("science_photometry_cache",),
    )
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda _conn: events.append(("ledger_table_write", None)),
    )

    with pytest.raises(migration_module.MigrationError, match="fresh-baseline clean-schema"):
        migration_module.apply_pending(engine)

    labels = [label for label, _value in events]
    assert labels.index("try_lock") < labels.index("inspect")
    assert "ledger_table_write" not in labels
    assert labels[-3:] == ["unlock", "commit", "close"]


def test_apply_pending_reuses_one_connection_and_keeps_each_migration_with_its_ledger_row(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)),
    )

    applied = migration_module.apply_pending(engine, root=tmp_path)

    assert applied == [spec.migration_id for spec in specs]
    assert engine.checkout_count == 1
    assert events[0][0] == "checkout"
    assert events[1][0] == "try_lock"
    assert events[2][0] == "commit"
    assert all(value is engine.connection for label, value in events if label == "inspect")
    assert all(value is engine.connection for label, value in events if label == "ledger_table_check")

    migration_sql = [value for label, value in events if label == "migration_sql"]
    ledger_inserts = [value for label, value in events if label == "ledger_insert"]
    assert len(migration_sql) == len(ledger_inserts) == 2
    assert [tx_id for tx_id, _sql in migration_sql] == ledger_inserts
    assert len(set(ledger_inserts)) == 2
    for tx_id in ledger_inserts:
        assert ("commit", tx_id) in events
    for index, (label, _value) in enumerate(events):
        if label == "begin":
            assert events[index + 1][0] == "pid_probe"  # first SQL in every transaction
        if label == "migration_sql":
            assert events[index + 1][0] == "pid_probe"  # before that tx's ledger INSERT
    assert [label for label, _value in events[-3:]] == ["unlock", "commit", "close"]


def test_adopt_and_apply_use_the_locked_owning_connection(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    legacy, managed = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    legacy = migration_module.MigrationSpec(legacy.migration_id, legacy.filename, legacy=True)
    specs = (legacy, managed)
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(monkeypatch, events, engine.connection)

    def validate_legacy(conn):
        assert conn is engine.connection
        events.append(("legacy_validate", conn))
        return []

    monkeypatch.setattr(migration_module, "_legacy_baseline_issues", validate_legacy)
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)),
    )

    assert migration_module.apply_pending(engine, adopt_legacy=True, root=tmp_path) == [
        managed.migration_id
    ]

    labels = [label for label, _value in events]
    assert engine.checkout_count == 1
    assert labels.index("try_lock") < labels.index("legacy_validate")
    assert len([event for event in labels if event == "ledger_insert"]) == 2
    assert all(value is engine.connection for label, value in events if label == "inspect")
    assert [label for label, _value in events[-3:]] == ["unlock", "commit", "close"]


def test_dry_run_does_not_take_lock_or_write(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    monkeypatch.setattr(migration_module, "inspect", lambda _bind: _Inspector(()))

    assert migration_module.apply_pending(engine, dry_run=True, root=tmp_path) == [
        spec.migration_id for spec in specs
    ]
    assert not {
        "try_lock",
        "unlock",
        "ledger_table_write",
        "ledger_insert",
        "migration_sql",
    }.intersection(label for label, _value in events)


def test_apply_pending_rolls_back_failed_migration_before_unlock(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "BROKEN;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)),
    )

    with pytest.raises(RuntimeError, match="fake migration execution failure"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    failed_tx_id = next(value[0] for label, value in events if label == "migration_sql")
    rollback_at = events.index(("rollback", failed_tx_id))
    unlock_at = labels.index("unlock")
    assert rollback_at < unlock_at
    assert labels[-3:] == ["unlock", "commit", "close"]


def test_unlock_failure_discards_connection(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events, unlock_result=False)
    specs = _managed_specs(tmp_path, "SELECT 1;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)),
    )

    with pytest.raises(migration_module.MigrationError, match="connection was discarded"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert labels[-4:] == ["unlock", "rollback", "invalidate", "close"]


def _configure_managed_run(monkeypatch, tmp_path: Path, engine: _Engine, *sql: str) -> None:
    monkeypatch.setattr(migration_module, "MIGRATIONS", _managed_specs(tmp_path, *sql))
    _configure_connection_inspection(monkeypatch, engine.events, engine.connection)
    monkeypatch.setattr(
        migration_module,
        "ensure_ledger_table",
        lambda conn: engine.events.append(("ledger_table_check", conn)),
    )


def test_owner_loss_after_first_migration_commit_blocks_next_sql_even_with_reused_pid(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;", "SELECT 2;")

    def switch_after_first_migration(conn: _Connection, label: str) -> None:
        if label == "commit" and len(conn.ledger_rows) == 1:
            conn.on_transaction_end = None
            conn.switch_backend(same_pid=True)

    engine.connection.on_transaction_end = switch_after_first_migration
    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert labels.count("migration_sql") == labels.count("ledger_insert") == 1
    assert "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_owner_loss_after_outside_probe_before_first_ledger_sql_refuses_write(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;")

    def switch_on_ledger_begin(conn: _Connection) -> None:
        if conn.transaction_id == 2:
            conn.on_begin = None
            conn.switch_backend()

    engine.connection.on_begin = switch_on_ledger_begin
    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert "ledger_table_check" not in labels
    assert "migration_sql" not in labels and "ledger_insert" not in labels
    assert "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_owner_loss_after_migration_sql_rolls_back_before_ledger_insert(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;")
    engine.connection.on_migration_sql = lambda conn: conn.switch_backend(same_pid=True)

    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert labels.count("migration_sql") == 1
    assert "ledger_insert" not in labels and "unlock" not in labels
    migration_tx_id = next(value[0] for label, value in events if label == "migration_sql")
    assert ("rollback", migration_tx_id) in events
    assert ("commit", migration_tx_id) not in events
    assert labels[-2:] == ["invalidate", "close"]


@pytest.mark.parametrize("switch_at", ["adoption_begin", "before_insert"])
def test_owner_loss_in_private_adoption_refuses_baseline_insert(
    monkeypatch, tmp_path: Path, switch_at: str
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    legacy, managed = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    monkeypatch.setattr(
        migration_module,
        "MIGRATIONS",
        (migration_module.MigrationSpec(legacy.migration_id, legacy.filename, legacy=True), managed),
    )
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: None)

    def validate_legacy(conn: _Connection) -> list[str]:
        events.append(("legacy_validate", conn))
        if switch_at == "before_insert":
            conn.switch_backend(same_pid=True)
        return []

    def switch_at_adoption_begin(conn: _Connection) -> None:
        if conn.transaction_id == 3:
            conn.on_begin = None
            conn.switch_backend(same_pid=True)

    monkeypatch.setattr(migration_module, "_legacy_baseline_issues", validate_legacy)
    if switch_at == "adoption_begin":
        engine.connection.on_begin = switch_at_adoption_begin

    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, adopt_legacy=True, root=tmp_path)

    labels = [label for label, _value in events]
    assert ("rollback", 3) in events
    if switch_at == "adoption_begin":
        assert "legacy_validate" not in labels
    assert "ledger_insert" not in labels and "migration_sql" not in labels
    assert "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_owner_loss_after_last_probe_before_unlock_never_unlocks_replacement(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;")

    def switch_at_unlock_begin(conn: _Connection) -> None:
        if len(conn.ledger_rows) == 1 and conn.on_begin is not None:
            conn.on_begin = None
            conn.switch_backend(same_pid=True)

    engine.connection.on_begin = switch_at_unlock_begin
    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert labels.count("ledger_insert") == labels.count("migration_sql") == 1
    assert "unlock" not in labels
    unlock_tx_id = max(value for label, value in events if label == "begin")
    assert ("rollback", unlock_tx_id) in events
    assert labels[-2:] == ["invalidate", "close"]


def test_lock_acquisition_refuses_driver_switch_before_result_is_verified(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    monkeypatch.setattr(migration_module, "MIGRATIONS", ())
    engine.connection.on_try_lock = lambda conn: conn.switch_backend(same_pid=True)

    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine)

    labels = [label for label, _value in events]
    assert labels[:2] == ["checkout", "try_lock"]
    assert "migration_sql" not in labels and "ledger_insert" not in labels
    assert "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_pid_mismatch_without_driver_switch_is_refused_before_ledger_work(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;")

    def change_pid_after_lock_statement(conn: _Connection) -> None:
        conn.backend_pid += 1
        conn.on_try_lock = None

    engine.connection.on_try_lock = change_pid_after_lock_statement
    with pytest.raises(migration_module.MigrationError, match="(?i)lock|backend|owner"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert "ledger_table_check" not in labels and "ledger_insert" not in labels
    assert "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_lost_backend_during_failing_migration_preserves_primary_error(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "BROKEN;")
    engine.connection.on_migration_sql = lambda conn: conn.switch_backend()

    with pytest.raises(RuntimeError, match="fake migration execution failure"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert "ledger_insert" not in labels and "unlock" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_missing_physical_driver_refuses_lock_before_any_query(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    engine.connection._pool_proxy.driver_connection = None
    monkeypatch.setattr(migration_module, "MIGRATIONS", ())

    with pytest.raises(migration_module.MigrationError, match="physical backend"):
        migration_module.apply_pending(engine)

    assert [label for label, _value in events] == ["checkout", "close"]


def test_missing_pid_in_lock_result_discards_uncertain_backend(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    monkeypatch.setattr(migration_module, "MIGRATIONS", ())
    exec_sql = engine.connection.exec_driver_sql

    def missing_pid(sql: str):
        result = exec_sql(sql)
        return _Result((True, None)) if "pg_try_advisory_lock" in sql else result

    engine.connection.exec_driver_sql = missing_pid
    with pytest.raises(migration_module.MigrationError, match="uncertain"):
        migration_module.apply_pending(engine)

    labels = [label for label, _value in events]
    assert labels[:2] == ["checkout", "try_lock"]
    assert "unlock" not in labels and "ledger_insert" not in labels
    assert labels[-2:] == ["invalidate", "close"]


def test_invalidated_connection_refuses_probe_without_recheckout(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    _configure_managed_run(monkeypatch, tmp_path, engine, "SELECT 1;", "SELECT 2;")

    def invalidate_after_first_commit(conn: _Connection, label: str) -> None:
        if label == "commit" and len(conn.ledger_rows) == 1:
            conn.on_transaction_end = None
            conn.invalidate()

    engine.connection.on_transaction_end = invalidate_after_first_commit
    with pytest.raises(migration_module.MigrationError, match="advisory-lock backend"):
        migration_module.apply_pending(engine, root=tmp_path)

    labels = [label for label, _value in events]
    assert labels.count("migration_sql") == labels.count("ledger_insert") == 1
    assert "unlock" not in labels
    assert labels[-1] == "close"


@pytest.mark.parametrize("pinned", [False, True])
def test_pending_sql_and_ledger_use_one_captured_read_even_if_path_changes(
    monkeypatch, tmp_path: Path, pinned: bool
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    original = b"SELECT '100%';\r\n"
    (spec,) = _managed_specs(tmp_path, original.decode("utf-8"))
    path = tmp_path / spec.filename
    path.write_bytes(original)
    checksum = hashlib.sha256(original.replace(b"\r\n", b"\n")).hexdigest()
    monkeypatch.setattr(migration_module, "MIGRATIONS", (spec,))
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(
        migration_module, "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)) if not pinned else pytest.fail("DDL"),
    )
    reads = []
    read_bytes = Path.read_bytes

    def mutate_after_read(self: Path) -> bytes:
        data = read_bytes(self)
        if self == path:
            reads.append(data)
            if len(reads) == 1:
                path.write_bytes(b"SELECT 'changed';\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    expected = {spec.migration_id: checksum} if pinned else None
    assert migration_module.apply_pending(
        engine, root=tmp_path, expected_pending_checksums=expected
    ) == [spec.migration_id]
    assert reads == [original]
    assert [value[1] for label, value in events if label == "migration_sql"] == [
        "SELECT '100%%';\r\n"
    ]
    assert engine.connection.ledger_rows[spec.migration_id]["checksum_sha256"] == checksum


def test_pinned_mode_captures_all_pending_before_first_sql(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    expected = {spec.migration_id: migration_module.migration_checksum(spec, root=tmp_path)
                for spec in specs}
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    second_path = tmp_path / specs[1].filename
    engine.connection.on_migration_sql = lambda _conn: second_path.write_bytes(b"SELECT 3;")

    assert migration_module.apply_pending(
        engine, root=tmp_path, expected_pending_checksums=expected
    ) == [s.migration_id for s in specs]
    assert [value[1] for label, value in events if label == "migration_sql"] == [
        "SELECT 1;", "SELECT 2;"
    ]
    assert engine.connection.ledger_rows[specs[1].migration_id]["checksum_sha256"] == expected[
        specs[1].migration_id
    ]


@pytest.mark.parametrize("bad_mapping", ["second", "missing", "extra"])
def test_pinned_mapping_refuses_before_any_migration_sql_or_ledger_insert(
    monkeypatch, tmp_path: Path, bad_mapping: str
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    expected = {spec.migration_id: migration_module.migration_checksum(spec, root=tmp_path)
                for spec in specs}
    if bad_mapping == "second":
        expected[specs[1].migration_id] = "0" * 64
    elif bad_mapping == "missing":
        del expected[specs[1].migration_id]
    else:
        expected["unexpected"] = "0" * 64

    with pytest.raises(migration_module.MigrationError, match="(?i)pending|checksum"):
        migration_module.apply_pending(engine, root=tmp_path, expected_pending_checksums=expected)
    labels = [label for label, _value in events]
    assert "migration_sql" not in labels and "ledger_insert" not in labels
    assert "ledger_table_check" not in labels
    assert labels[-3:] == ["unlock", "commit", "close"]


@pytest.mark.parametrize("pinned", [False, True])
def test_invalid_utf8_pending_refuses_before_migration_sql(
    monkeypatch, tmp_path: Path, pinned: bool
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    (spec,) = _managed_specs(tmp_path, "SELECT 1;")
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "MIGRATIONS", (spec,))
    monkeypatch.setattr(
        migration_module, "ensure_ledger_table",
        lambda conn: events.append(("ledger_table_check", conn)) if not pinned else pytest.fail("DDL"),
    )
    data = b"SELECT '\xff';"
    (tmp_path / spec.filename).write_bytes(data)
    expected = {spec.migration_id: hashlib.sha256(data).hexdigest()} if pinned else None
    with pytest.raises((migration_module.MigrationError, UnicodeDecodeError)):
        migration_module.apply_pending(engine, root=tmp_path, expected_pending_checksums=expected)
    assert not {"migration_sql", "ledger_insert"}.intersection(
        label for label, _value in events
    )


@pytest.mark.parametrize("history", ["missing_ledger", "unknown_id", "bad_state", "gap",
                                     "bad_filename", "checksum_drift"])
def test_pinned_mode_refuses_unexpected_ledger_history_without_writes(
    monkeypatch, tmp_path: Path, history: str
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(
        monkeypatch, events, engine.connection,
        tables=() if history == "missing_ledger" else ("schema_migrations",),
    )
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    expected = {spec.migration_id: migration_module.migration_checksum(spec, root=tmp_path)
                for spec in specs}
    if history != "missing_ledger":
        key = "unknown" if history == "unknown_id" else specs[1].migration_id
        engine.connection.ledger_rows[key] = {
            "migration_id": key,
            "filename": "other.sql" if history == "bad_filename" else specs[1].filename,
            "checksum_sha256": ("0" * 64 if history == "checksum_drift"
                                else expected[specs[1].migration_id]),
            "state": "PENDING" if history == "bad_state" else "APPLIED",
        }
        if history != "unknown_id":
            del expected[specs[1].migration_id]
    with pytest.raises(migration_module.MigrationError):
        migration_module.apply_pending(engine, root=tmp_path, expected_pending_checksums=expected)
    assert not {"migration_sql", "ledger_insert"}.intersection(
        label for label, _value in events
    )


def test_pinned_mode_refuses_adoption_before_any_ledger_write(monkeypatch, tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = _managed_specs(tmp_path, "SELECT 1;")
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    with pytest.raises(migration_module.MigrationError, match="(?i)adopt"):
        migration_module.apply_pending(
            engine, adopt_legacy=True, root=tmp_path,
            expected_pending_checksums={specs[0].migration_id:
                                        migration_module.migration_checksum(specs[0], root=tmp_path)},
        )
    assert not {"ledger_insert", "migration_sql"}.intersection(
        label for label, _value in events
    )


def test_pinned_dry_run_does_not_silently_ignore_expected_checksums() -> None:
    with pytest.raises(migration_module.MigrationError, match="non-dry-run"):
        migration_module.apply_pending(object(), dry_run=True, expected_pending_checksums={})


def test_pinned_mode_accepts_only_pending_after_valid_applied_history(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    legacy, predecessor, first, second = _managed_specs(
        tmp_path, "SELECT 0;", "SELECT 1;", "SELECT 2;", "SELECT 3;"
    )
    legacy = migration_module.MigrationSpec(legacy.migration_id, legacy.filename, legacy=True)
    specs = (legacy, predecessor, first, second)
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    for spec, state in ((legacy, "BASELINED"), (predecessor, "APPLIED")):
        engine.connection.ledger_rows[spec.migration_id] = {
            "migration_id": spec.migration_id,
            "filename": spec.filename,
            "checksum_sha256": migration_module.migration_checksum(spec, root=tmp_path),
            "state": state,
        }
    expected = {spec.migration_id: migration_module.migration_checksum(spec, root=tmp_path)
                for spec in (first, second)}

    assert migration_module.apply_pending(
        engine, root=tmp_path, expected_pending_checksums=expected,
    ) == [first.migration_id, second.migration_id]
    assert [value[1] for label, value in events if label == "migration_sql"] == [
        "SELECT 2;", "SELECT 3;"
    ]
    assert [engine.connection.ledger_rows[spec.migration_id]["checksum_sha256"]
            for spec in (first, second)] == list(expected.values())


def test_pinned_two_checksum_request_refuses_partial_first_commit_without_retry(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    first, second = _managed_specs(tmp_path, "SELECT 1;", "SELECT 2;")
    monkeypatch.setattr(migration_module, "MIGRATIONS", (first, second))
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    expected = {spec.migration_id: migration_module.migration_checksum(spec, root=tmp_path)
                for spec in (first, second)}
    engine.connection.ledger_rows[first.migration_id] = {
        "migration_id": first.migration_id,
        "filename": first.filename,
        "checksum_sha256": expected[first.migration_id],
        "state": "APPLIED",
    }

    with pytest.raises(migration_module.MigrationError, match="pending migration IDs"):
        migration_module.apply_pending(
            engine, root=tmp_path, expected_pending_checksums=expected,
        )
    assert not {"migration_sql", "ledger_insert"}.intersection(
        label for label, _value in events
    )
    assert engine.checkout_count == 1


@pytest.mark.parametrize("history", ["exact", "missing", "wrong_checksum", "bad_filename",
                                     "unknown", "stale_a1"])
def test_a1_pinned_runner_requires_exact_predecessors_before_fake_sql(
    monkeypatch, history: str,
) -> None:
    # Fake connection only: no SQL is sent to PostgreSQL or read from a DB.
    events: list[tuple[str, object]] = []
    engine = _Engine(events)
    specs = migration_module.MIGRATIONS[:-1]
    monkeypatch.setattr(migration_module, "MIGRATIONS", specs)
    assert len(specs) == 36 and specs[-1].migration_id == (
        "mvp7_0_7_interseries_edge_membership_integrity"
    )
    _configure_connection_inspection(monkeypatch, events, engine.connection)
    monkeypatch.setattr(migration_module, "ensure_ledger_table", lambda _conn: pytest.fail("DDL"))
    for spec in specs[:-1]:
        engine.connection.ledger_rows[spec.migration_id] = {
            "migration_id": spec.migration_id, "filename": spec.filename,
            "checksum_sha256": migration_module.migration_checksum(spec),
            "state": "BASELINED",
        }
    for spec in specs[-4:-1]:
        engine.connection.ledger_rows[spec.migration_id]["state"] = "APPLIED"
    if history == "missing":
        del engine.connection.ledger_rows[specs[2].migration_id]
    elif history == "wrong_checksum":
        engine.connection.ledger_rows[specs[2].migration_id]["checksum_sha256"] = "0" * 64
    elif history == "bad_filename":
        engine.connection.ledger_rows[specs[2].migration_id]["filename"] = "wrong.sql"
    elif history == "unknown":
        engine.connection.ledger_rows["unknown"] = {
            "migration_id": "unknown", "filename": "wrong.sql", "checksum_sha256": "0" * 64,
            "state": "APPLIED",
        }
    elif history == "stale_a1":
        spec = specs[-1]
        engine.connection.ledger_rows[spec.migration_id] = {
            "migration_id": spec.migration_id, "filename": spec.filename,
            "checksum_sha256": migration_module.migration_checksum(spec), "state": "APPLIED",
        }
    expected = {specs[-1].migration_id: migration_module.migration_checksum(specs[-1])}
    if history == "exact":
        assert migration_module.apply_pending(
            engine, expected_pending_checksums=expected
        ) == [specs[-1].migration_id]
        assert sum(label == "migration_sql" for label, _ in events) == 1
        assert sum(label == "ledger_insert" for label, _ in events) == 1
    else:
        with pytest.raises(migration_module.MigrationError):
            migration_module.apply_pending(engine, expected_pending_checksums=expected)
        assert not {"migration_sql", "ledger_insert"}.intersection(label for label, _ in events)
