"""Offline synthetic-only checks; never inspect the fixed private file or contact PostgreSQL."""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from grandgousier import cli
from grandgousier.db import migrations
from grandgousier.db.models import (
    InterseriesCalibrationEdge,
    InterseriesCalibrationGeneration,
    InterseriesCalibrationSeries,
)

# The autouse fixture blocks the unmocked ledger helper. Invoke this saved reference
# only after _fake_probe has replaced its source and driver with synthetic fakes.
_FAKE_ONLY_LEDGER_HELPER = migrations.guarded_p207_test_readonly_ledger
_FAKE_ONLY_INTEGRITY_HELPER = migrations.guarded_p207_test_readonly_integrity

FLAG = "--guard-p207-test-readonly-ledger"
PROBE_FLAG = "--guard-p207-test-readonly-probe"
INTEGRITY_FLAG = "--guard-p207-test-readonly-integrity"
PRIVATE_CONFIG = r"C:\PycharmProjects\grandgousier_p207_test_ro.private.yaml"
GUARDED_ARGS = ["--config", PRIVATE_CONFIG, "schema-status", FLAG]
PROBE_ARGS = ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG]
INTEGRITY_ARGS = ["--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG]
URL = (
    "postgresql+psycopg://grandgousier:synthetic%21@127.0.0.1:5432/"
    "grandgousier_070_gpx_full"
)


def _expected():
    return tuple(
        (spec, hashlib.sha256(spec.filename.encode()).hexdigest())
        for spec in migrations.MIGRATIONS
    )


def _rows(n: int, *, reverse: bool = False):
    rows = [
        (spec.migration_id, spec.filename, digest, "BASELINED" if i < 32 else "APPLIED")
        for i, (spec, digest) in enumerate(_expected()[:n])
    ]
    return rows[::-1] if reverse else rows


def _no_settings_or_parser(monkeypatch):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("ordinary path")
        raise AssertionError("Ordinary Settings, .env, parser or config was reached")

    monkeypatch.setattr(cli, "get_settings", forbidden)
    monkeypatch.setattr(cli, "build_parser", forbidden)
    monkeypatch.setattr(cli, "_run_main", forbidden)
    return calls


def test_guarded_tokens_match_fixed_module_constants_without_path_access():
    assert FLAG == cli._P207_GUARDED_LEDGER_FLAG
    assert PRIVATE_CONFIG == cli._P207_GUARDED_LEDGER_CONFIG
    assert PRIVATE_CONFIG == str(migrations._P207_PRIVATE_SOURCE)
    assert PROBE_FLAG == cli._P207_GUARDED_PROBE_FLAG
    assert PRIVATE_CONFIG == cli._P207_GUARDED_PROBE_CONFIG
    assert INTEGRITY_FLAG == cli._P207_GUARDED_INTEGRITY_FLAG


@pytest.fixture(autouse=True)
def no_real_private_source_or_helper(monkeypatch):
    def forbidden():
        pytest.fail("The real private source or guarded helper must not run in offline tests")

    monkeypatch.setattr(migrations, "_p207_read_private_source", forbidden)
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_ledger", forbidden)
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_integrity", forbidden)


@pytest.mark.parametrize("count,expected_exit,marker", [
    (35, 2, "STOP P207_TEST_LEDGER_PENDING ledger=35 baselined=32 applied=3 pending=2"),
    (36, 2, "STOP P207_TEST_LEDGER_PENDING ledger=36 baselined=32 applied=4 pending=1"),
    (37, 0, "PASS P207_TEST_LEDGER_ONLY ledger=37 baselined=32 applied=5 pending=0 drift=0"),
])
def test_exact_opt_in_guards_logical_prefix_without_settings(
    monkeypatch, capsys, count, expected_exit, marker,
):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    state = migrations._p207_ledger_state(_rows(count, reverse=True), _expected())
    calls = []
    monkeypatch.setattr(
        migrations, "guarded_p207_test_readonly_ledger",
        lambda: calls.append("mocked guard") or state,
    )
    assert cli.main(GUARDED_ARGS) == expected_exit
    out = capsys.readouterr()
    assert (out.out if expected_exit == 0 else out.err).strip() == marker
    assert (out.err if expected_exit == 0 else out.out) == ""
    assert calls == ["mocked guard"]
    assert ordinary_calls == []


@pytest.mark.parametrize("args", [
    ["schema-status", FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status"],
    ["--config", PRIVATE_CONFIG, "migrate-db"],
    ["--conf", PRIVATE_CONFIG, "schema-status", FLAG],
    ["--config=" + PRIVATE_CONFIG, "schema-status", FLAG],
    ["--config=" + PRIVATE_CONFIG, "schema-status"],
    ["schema-status", "--config", PRIVATE_CONFIG, FLAG],
    ["--config", PRIVATE_CONFIG, FLAG, "schema-status"],
    ["--config", PRIVATE_CONFIG, "migrate-db", FLAG],
    ["--config", "other-file.yaml", "schema-status", FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", "--g"],
    ["--config", PRIVATE_CONFIG, "schema-status", "--guar"],
    ["--g", "--config", PRIVATE_CONFIG, "schema-status"],
    ["schema-status", "--g"],
    ["schema-status", "--g=true"],
    ["schema-status", "--guard"],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG + "=true"],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG + "true"],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG, FLAG],
    ["--config", PRIVATE_CONFIG, "--config", PRIVATE_CONFIG, "schema-status", FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG, "--format", "json"],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG, "--database-url", URL],
    ["--database-url", URL, "--config", PRIVATE_CONFIG, "schema-status", FLAG],
    ["schema-status", FLAG, "--database-url", URL],
    ["--database-url", URL, "schema-status", FLAG],
    ["migrate-db", FLAG],
])
def test_malformed_guarded_arguments_are_fixed_stop_before_import_or_settings(
    monkeypatch, capsys, args,
):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    guard_calls = []
    monkeypatch.setattr(
        migrations, "guarded_p207_test_readonly_ledger",
        lambda: guard_calls.append("guarded helper") or "NEW37",
    )
    assert cli.main(args) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_GUARDED_LEDGER_INVALID_ARGS"
    assert URL not in result.err
    assert PRIVATE_CONFIG not in result.err
    assert ordinary_calls == guard_calls == []


def test_implicit_sys_argv_disguised_opt_in_stops_before_ordinary_path(
    monkeypatch, capsys,
):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    guard_calls = []
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_ledger", lambda: guard_calls.append(1))
    monkeypatch.setattr(sys, "argv", ["grandgousier", "schema-status", "--g"])
    assert cli.main() == 2
    assert capsys.readouterr().err.strip() == "STOP P207_GUARDED_LEDGER_INVALID_ARGS"
    assert ordinary_calls == guard_calls == []


@pytest.mark.parametrize("args", [
    ["schema-status"],
    ["--config", "synthetic-ordinary.yaml", "schema-status"],
    ["--database-url", URL, "schema-status"],
    ["migrate-db", "--dry-run"],
])
def test_unopted_dispatch_preserves_ordinary_path(monkeypatch, args):
    calls = []
    monkeypatch.setattr(cli, "_run_main", lambda args: calls.append(args) or 17)
    assert cli.main(args) == 17
    assert calls == [args]


@pytest.mark.parametrize("source", [
    b'{"database_url":"postgresql+psycopg://grandgousier:synthetic%21@localhost:5432/grandgousier_070_gpx_full"}',
    b'{"database_url":"postgresql+psycopg://grandgousier:synthetic%21@127.0.0.1:5432/grandgousier"}',
    b'{"database_url":"postgresql+psycopg://grandgousier:synthetic%21@127.0.0.1:5432/grandgousier_070_gpx_full?hostaddr=127.0.0.1"}',
    b'{"database_url":"postgresql+psycopg://grandgousier:synthetic%ZZ@127.0.0.1:5432/grandgousier_070_gpx_full"}',
    b'{"database_url":"postgresql+psycopg://grandgousier:synthetic%21@127.0.0.1:5432/grandgousier_070_gpx_full#x"}',
    b'{"database_url":"x","database_url":"x"}',
    b'database_url: x\ndatabase_url: y\n',
    b'{"database_url":"x","password":"another"}',
    b'{"database_url":"x","nested":{"database_url":"other"}}',
    b'{"database_url":"x","DATABASE_URL":"other"}',
    b'{"database_url":"x","hostaddr":"other"}',
    b'{"database_url":"x","PGPASSWORD":"other"}',
    b'{"database_url":"x","GRANDGOUSIER_DATABASE_URL":"other"}',
    b'{"database_url":"x","databaseUrl":"other"}',
    b'{"database_url":"x","notes":"postgresql://another-target"}',
])
def test_synthetic_invalid_config_bytes_are_rejected_without_source_access(source):
    with pytest.raises(ValueError):
        migrations._p207_password_from_source(source)


def test_benign_unrelated_yaml_and_json_keys_are_allowed():
    assert migrations._p207_password_from_source(
        ("database_url: " + URL + "\nnotes:\n  answer: 42\n").encode()
    ) == "synthetic!"
    assert migrations._p207_password_from_source(
        ('{"database_url":"' + URL + '","notes":{"answer":42}}').encode()
    ) == "synthetic!"


def test_manifest_checksums_cover_every_packaged_migration(monkeypatch):
    calls = []
    monkeypatch.setattr(migrations, "validate_manifest", lambda: None)

    def checksum(spec):
        calls.append(spec.migration_id)
        return hashlib.sha256(spec.filename.encode()).hexdigest()

    monkeypatch.setattr(migrations, "migration_checksum", checksum)
    assert migrations._p207_expected_ledger() == _expected()
    assert calls == [spec.migration_id for spec in migrations.MIGRATIONS]
    assert len(calls) == 37


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[:34],
    lambda rows: rows + [rows[-1]],
    lambda rows: rows[:1] + [rows[0]] + rows[2:],
    lambda rows: rows[:1] + [("unknown", *rows[1][1:])] + rows[2:],
    lambda rows: rows[:1] + [(rows[1][0], "wrong.sql", *rows[1][2:])] + rows[2:],
    lambda rows: rows[:1] + [(rows[1][0], rows[1][1], "0" * 64, rows[1][3])] + rows[2:],
    lambda rows: rows[:1] + [(*rows[1][:3], "PENDING")] + rows[2:],
    lambda rows: rows[:32] + [(*rows[32][:3], "BASELINED")] + rows[33:],
    lambda rows: rows[:35] + [_rows(37)[36]],
])
def test_synthetic_bad_ledger_sets_fail_closed_without_helper(mutation):
    with pytest.raises(ValueError):
        migrations._p207_ledger_state(mutation(_rows(36)), _expected())


@pytest.mark.parametrize("error", [RuntimeError(URL), SystemExit(URL), KeyboardInterrupt()])
def test_mocked_helper_exception_is_sanitized_without_retry(monkeypatch, capsys, error):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    calls = []

    def fail_once():
        calls.append("mocked helper")
        raise error

    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_ledger", fail_once)
    assert cli.main(GUARDED_ARGS) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip() == "STOP P207_GUARDED_LEDGER_UNVERIFIED"
    assert URL not in output.err
    assert PRIVATE_CONFIG not in output.err
    assert calls == ["mocked helper"]
    assert ordinary_calls == []


class _FakeProbeConnection:
    def __init__(self, row, *, backend_pid=457, fail_at=None):
        self.row = row
        self.info = SimpleNamespace(backend_pid=backend_pid)
        self.fail_at = fail_at
        self.statements = []
        self.fetches = 0
        self.rollbacks = 0
        self.closes = 0

    def execute(self, sql):
        self.statements.append(sql)
        assert sql in (
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            migrations._P207_PROBE_SQL,
        ), "Only the transaction start and the single backend probe are allowed"
        if self.fail_at == "begin" and sql.startswith("BEGIN"):
            raise RuntimeError(URL)
        if self.fail_at == "probe" and sql == migrations._P207_PROBE_SQL:
            raise RuntimeError(URL)
        return self

    def fetchone(self):
        self.fetches += 1
        if self.fail_at == "fetch":
            raise RuntimeError(URL)
        return self.row

    def rollback(self):
        self.rollbacks += 1
        if self.fail_at == "rollback":
            raise RuntimeError(URL)

    def close(self):
        self.closes += 1
        if self.fail_at == "close":
            raise RuntimeError(URL)


class _FakeLedgerConnection(_FakeProbeConnection):
    def execute(self, sql):
        if sql == migrations._P207_LEDGER_SQL:
            self.statements.append(sql)
            return self
        return super().execute(sql)

    def fetchall(self):
        self.fetches += 1
        return _rows(35)


def _probe_row():
    return (
        "grandgousier_070_gpx_full", 23069087, "grandgousier", "grandgousier",
        False, 180001, 457, "127.0.0.1", 5432, *([True] * 6),
    )


def _fake_probe(monkeypatch, row=None, *, backend_pid=457, fail_at=None, environ=None,
                connection_type=_FakeProbeConnection):
    conn = connection_type(_probe_row() if row is None else row,
                           backend_pid=backend_pid, fail_at=fail_at)
    actions = []
    monkeypatch.setattr(migrations, "os", SimpleNamespace(environ={} if environ is None else environ))
    monkeypatch.setattr(
        migrations, "_p207_read_private_source",
        lambda: actions.append("synthetic source") or ("database_url: " + URL + "\n").encode(),
    )
    monkeypatch.setattr(
        migrations, "_p207_expected_ledger",
        lambda: actions.append("manifest") or _expected(),
    )

    def connect(**kwargs):
        actions.append("fake connect")
        assert kwargs == {
            "host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": 5432,
            "dbname": "grandgousier_070_gpx_full", "user": "grandgousier",
            "password": "synthetic!", "connect_timeout": 3, "autocommit": True,
            "options": ("-c default_transaction_read_only=on -c statement_timeout=5000 "
                        "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"),
        }
        if fail_at == "connect":
            raise RuntimeError(URL)
        return conn

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    return conn, actions


def _assert_one_probe_and_cleanup(conn):
    assert conn.statements == [
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        migrations._P207_PROBE_SQL,
    ]
    assert conn.fetches == 1
    assert conn.rollbacks == conn.closes == 1


def test_probe_sql_uses_bare_host_address_without_inet_netmask():
    sql = migrations._P207_PROBE_SQL
    assert sql.count("host(inet_server_addr())") == 1
    assert "pg_backend_pid(), host(inet_server_addr()), inet_server_port(), " in sql
    assert "inet_server_addr()::text" not in sql


def test_bare_ipv4_is_accepted_by_both_helpers_with_only_the_expected_fake_queries(monkeypatch):
    probe_conn, _ = _fake_probe(monkeypatch)
    assert migrations.guarded_p207_test_readonly_probe() == ()
    _assert_one_probe_and_cleanup(probe_conn)

    ledger_conn, _ = _fake_probe(monkeypatch, connection_type=_FakeLedgerConnection)
    assert _FAKE_ONLY_LEDGER_HELPER() == "OLD35"
    assert ledger_conn.statements == [
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        migrations._P207_PROBE_SQL,
        migrations._P207_LEDGER_SQL,
    ]
    assert ledger_conn.fetches == 2
    assert ledger_conn.rollbacks == ledger_conn.closes == 1


@pytest.mark.parametrize("server_addr", [
    None, "::1", "::ffff:127.0.0.1", "192.0.2.1", "127.0.0.2", "127.0.0.1/32",
])
def test_noncanonical_server_address_refused_by_both_helpers_before_ledger(
    monkeypatch, server_addr,
):
    row = list(_probe_row())
    row[7] = server_addr
    probe_conn, _ = _fake_probe(monkeypatch, tuple(row))
    assert migrations.guarded_p207_test_readonly_probe() == ("P07",)
    _assert_one_probe_and_cleanup(probe_conn)

    ledger_conn, _ = _fake_probe(monkeypatch, tuple(row),
                                 connection_type=_FakeLedgerConnection)
    with pytest.raises(ValueError, match="Guarded backend identity or read-only safety"):
        _FAKE_ONLY_LEDGER_HELPER()
    _assert_one_probe_and_cleanup(ledger_conn)  # No ledger SELECT after a failed identity probe.


def test_probe_only_exact_cli_no_ledger_or_settings(monkeypatch, capsys):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    conn, actions = _fake_probe(monkeypatch)
    assert cli.main(PROBE_ARGS) == 0
    result = capsys.readouterr()
    assert result.out.strip() == "PROBE_ONLY P207_TEST_BACKEND_STRUCTURAL_CHECK"
    assert result.err == ""
    assert actions == ["synthetic source", "manifest", "fake connect"]
    assert ordinary_calls == []
    _assert_one_probe_and_cleanup(conn)


@pytest.mark.parametrize("args", [
    ["schema-status", PROBE_FLAG],
    ["--config", "synthetic-other.yaml", "schema-status", PROBE_FLAG],
    ["--config", PRIVATE_CONFIG, "migrate-db", PROBE_FLAG],
    ["--conf", PRIVATE_CONFIG, "schema-status", PROBE_FLAG],
    ["--config=" + PRIVATE_CONFIG, "schema-status", PROBE_FLAG],
    ["schema-status", "--config", PRIVATE_CONFIG, PROBE_FLAG],
    ["--config", PRIVATE_CONFIG, PROBE_FLAG, "schema-status"],
    ["--config", PRIVATE_CONFIG, "schema-status", "--guard-p207-test-readonly-prob"],
    ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG + "=true"],
    ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG + "extra"],
    ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG, FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG, PROBE_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG, PROBE_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG, "--database-url", URL],
    ["--database-url", URL, "--config", PRIVATE_CONFIG, "schema-status", PROBE_FLAG],
])
def test_probe_malformed_args_stop_before_source_parser_or_settings(monkeypatch, capsys, args):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    calls = []
    monkeypatch.setattr(
        migrations, "guarded_p207_test_readonly_probe",
        lambda: calls.append("probe") or (),
    )
    assert cli.main(args) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_GUARDED_PROBE_INVALID_ARGS"
    assert URL not in result.err and PRIVATE_CONFIG not in result.err
    assert ordinary_calls == calls == []


def test_probe_implicit_argv_prefix_stops_before_parser(monkeypatch, capsys):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["grandgousier", "schema-status", "--guard-p207-test-readonly-prob"])
    assert cli.main() == 2
    assert capsys.readouterr().err.strip() == "STOP P207_GUARDED_PROBE_INVALID_ARGS"
    assert ordinary_calls == []


@pytest.mark.parametrize("position,replacement,backend_pid", [
    (0, "different", 457),
    (1, 0, 457),
    (2, "different", 457),
    (3, "different", 457),
    (4, True, 457),
    (5, 170000, 457),
    (6, 457, 458),
    (7, "127.0.0.2", 457),
    (8, 5433, 457),
    *((position, False, 457) for position in range(9, 15)),
])
def test_each_shape_valid_probe_predicate_has_only_its_fixed_failure_id(
    monkeypatch, capsys, position, replacement, backend_pid,
):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    row = list(_probe_row())
    row[position] = replacement
    conn, _ = _fake_probe(monkeypatch, tuple(row), backend_pid=backend_pid)
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.err.strip() == f"STOP P207_TEST_BACKEND_PROBE_ONLY P{position:02d}"
    assert result.out == ""
    assert ordinary_calls == []
    _assert_one_probe_and_cleanup(conn)


@pytest.mark.parametrize("position,replacement", [(1, True), (4, 0), (5, True), (8, True), (12, 1)])
def test_probe_rejects_type_substitutions_matching_values(monkeypatch, position, replacement):
    row = list(_probe_row())
    row[position] = replacement
    conn, _ = _fake_probe(monkeypatch, tuple(row))
    assert migrations.guarded_p207_test_readonly_probe() == (f"P{position:02d}",)
    _assert_one_probe_and_cleanup(conn)


def test_probe_reports_combined_mismatch_and_list_structure_without_waiver(monkeypatch, capsys):
    row = list(_probe_row())
    row[0] = "different"
    row[12] = False
    conn, _ = _fake_probe(monkeypatch, row)
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.err.strip() == "STOP P207_TEST_BACKEND_PROBE_ONLY P00 P12 ROW_LIST_SLICING"
    assert result.out == ""
    _assert_one_probe_and_cleanup(conn)


def test_probe_all_fifteen_failed_positions_are_reported_as_fixed_ids(monkeypatch, capsys):
    row = ["different", 0, "different", "different", True, 170000, 0,
           "127.0.0.2", 5433, *([False] * 6)]
    conn, _ = _fake_probe(monkeypatch, row)
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    codes = " ".join(f"P{position:02d}" for position in range(15))
    assert result.err.strip() == f"STOP P207_TEST_BACKEND_PROBE_ONLY {codes} ROW_LIST_SLICING"
    assert result.out == ""
    _assert_one_probe_and_cleanup(conn)


def test_probe_backend_pid_wrong_type_is_p06_only(monkeypatch):
    conn, _ = _fake_probe(monkeypatch, backend_pid=True)
    assert migrations.guarded_p207_test_readonly_probe() == ("P06",)
    _assert_one_probe_and_cleanup(conn)


def test_probe_valid_list_is_not_promoted_when_existing_tuple_slices_fail(monkeypatch, capsys):
    row = list(_probe_row())
    conn, _ = _fake_probe(monkeypatch, row)
    assert cli.main(PROBE_ARGS) == 2
    assert capsys.readouterr().err.strip() == "STOP P207_TEST_BACKEND_PROBE_ONLY ROW_LIST_SLICING"
    assert row[:5] != _probe_row()[:5]  # List slice != tuple in the unchanged ledger guard.
    assert row[7:9] != _probe_row()[7:9]
    assert row[9:] != _probe_row()[9:]
    _assert_one_probe_and_cleanup(conn)


@pytest.mark.parametrize("row", [None, (), (1,) * 14, (1,) * 16, "not a row"])
def test_unknown_shape_stops_before_any_position_or_backend_pid_access(monkeypatch, capsys, row):
    conn, _ = _fake_probe(monkeypatch, row=())
    conn.row = row
    conn.info = None
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.err.strip() == "STOP P207_TEST_BACKEND_PROBE_ONLY ROW_SHAPE"
    assert result.out == ""
    _assert_one_probe_and_cleanup(conn)


def test_existing_aggregate_guard_semantics_remain_fail_closed_offline():
    row = _probe_row()
    conn = SimpleNamespace(info=SimpleNamespace(backend_pid=457))

    def aggregate(probe):
        return (probe[:5] != ("grandgousier_070_gpx_full", 23069087,
                              "grandgousier", "grandgousier", False)
                or type(probe[1]) is not int or type(probe[4]) is not bool
                or type(probe[5]) is not int or not 180000 <= probe[5] < 190000
                or type(probe[6]) is not int or probe[6] <= 0
                or type(conn.info.backend_pid) is not int or conn.info.backend_pid != probe[6]
                or probe[7:9] != ("127.0.0.1", 5432) or type(probe[8]) is not int
                or probe[9:] != (True, True, True, True, True, True)
                or any(type(value) is not bool for value in probe[9:]))

    assert migrations._p207_probe_failures(row, conn) == ()
    assert aggregate(row) is False
    for position in range(15):
        changed = list(row)
        changed[position] = object()
        changed = tuple(changed)
        assert aggregate(changed) is True
        assert migrations._p207_probe_failures(changed, conn) == (f"P{position:02d}",)
    assert aggregate(list(row)) is True
    assert migrations._p207_probe_failures(list(row), conn) == ("ROW_LIST_SLICING",)


@pytest.mark.parametrize("name", ["PGPASSWORD", "pgHOST", "GRANDGOUSIER_DATABASE_URL"])
def test_probe_rejects_synthetic_environment_names_before_source(monkeypatch, capsys, name):
    conn, actions = _fake_probe(monkeypatch, environ={name: "invented"})
    assert cli.main(PROBE_ARGS) == 2
    assert capsys.readouterr().err.strip() == "STOP P207_GUARDED_PROBE_UNVERIFIED"
    assert actions == []
    assert conn.statements == []
    assert conn.rollbacks == conn.closes == 0


@pytest.mark.parametrize("stage", ["source", "manifest", "connect", "begin", "probe", "fetch",
                                   "rollback", "close"])
def test_probe_errors_and_cleanup_uncertainty_are_sanitized(monkeypatch, capsys, stage):
    conn, actions = _fake_probe(monkeypatch, fail_at=stage)
    if stage == "source":
        monkeypatch.setattr(migrations, "_p207_read_private_source", lambda: (_ for _ in ()).throw(
            RuntimeError(URL)))
    elif stage == "manifest":
        monkeypatch.setattr(migrations, "_p207_expected_ledger", lambda: (_ for _ in ()).throw(
            RuntimeError(URL)))
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_GUARDED_PROBE_UNVERIFIED"
    assert URL not in result.err and PRIVATE_CONFIG not in result.err
    if stage in ("source", "manifest", "connect"):
        assert conn.rollbacks == conn.closes == 0
        assert conn.statements == []
    else:
        assert conn.rollbacks == conn.closes == 1
        assert conn.statements[:1] == ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"]
    assert actions == {
        "source": [], "manifest": ["synthetic source"],
    }.get(stage, ["synthetic source", "manifest", "fake connect"])


@pytest.mark.parametrize("error", [RuntimeError(URL), SystemExit(URL), KeyboardInterrupt()])
def test_probe_cli_catches_all_helper_exceptions_without_raw_output(monkeypatch, capsys, error):
    ordinary_calls = _no_settings_or_parser(monkeypatch)
    calls = []

    def fail_once():
        calls.append("mocked probe")
        raise error

    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_probe", fail_once)
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_GUARDED_PROBE_UNVERIFIED"
    assert calls == ["mocked probe"]
    assert ordinary_calls == []


@pytest.mark.parametrize("invalid", [("P00", "P00"), ("ROW_SHAPE", "P00"),
                                    (URL,), ["P00"], None])
def test_probe_cli_does_not_render_invalid_or_untrusted_result(monkeypatch, capsys, invalid):
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_probe", lambda: invalid)
    assert cli.main(PROBE_ARGS) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_GUARDED_PROBE_UNVERIFIED"


# All fixtures below are fabricated in memory. Never use the real fixed private source
# or the actual PostgreSQL driver; the real mirror bytes are read only in the pin test.
def _fake_integrity_catalog():
    names = migrations._P207_INTEGRITY_TABLES
    sizes = [(name, "r", False, 65536) for name in names]
    columns = [
        ("calibration_versions", "calibration_version_id", "bigint", True),
        ("calibration_versions", "field_id", "bigint", False),
        ("calibration_versions", "series_id", "bigint", False),
        ("calibration_versions", "product_role", "character varying", True),
        ("calibration_versions", "lifecycle_status", "character varying", True),
        ("field_series", "series_id", "bigint", True),
        ("field_series", "field_id", "bigint", True),
        ("interseries_calibration_generations", "interseries_generation_id", "bigint", True),
        ("interseries_calibration_generations", "field_id", "bigint", True),
        ("interseries_calibration_generations", "reference_series_id", "bigint", True),
        ("interseries_calibration_series", "interseries_series_id", "bigint", True),
        ("interseries_calibration_series", "interseries_generation_id", "bigint", True),
        ("interseries_calibration_series", "field_id", "bigint", True),
        ("interseries_calibration_series", "series_id", "bigint", False),
        ("interseries_calibration_series", "science_calibration_version_id", "bigint", True),
        ("interseries_calibration_series", "is_reference_series", "boolean", True),
        ("interseries_calibration_edges", "interseries_generation_id", "bigint", True),
        ("interseries_calibration_edges", "left_series_id", "bigint", True),
        ("interseries_calibration_edges", "right_series_id", "bigint", True),
        ("interseries_calibration_edges", "pair_status", "character varying", True),
    ]

    def fk(child, name, parent, source, target, delete, set_cols=None):
        return (child, name, "f", "public", parent, list(source), list(target),
                None if set_cols is None else list(set_cols), delete, "a", "s",
                True, False, False, True, 0, True, None, None, None, None, None)

    constraints = [
        fk("interseries_calibration_generations", "gen_field_fkey", "fields",
           ["field_id"], ["field_id"], "c"),
        fk("interseries_calibration_generations", "gen_instrument_fkey", "instrument_units",
           ["instrument_unit_id"], ["instrument_unit_id"], "n"),
        fk("interseries_calibration_generations", "fk_interseries_generations_reference_series_field",
           "field_series", ["reference_series_id", "field_id"],
           ["series_id", "field_id"], "r"),
        fk("interseries_calibration_series", "fk_interseries_series_generation_field",
           "interseries_calibration_generations", ["interseries_generation_id", "field_id"],
           ["interseries_generation_id", "field_id"], "c"),
        fk("interseries_calibration_series", "fk_interseries_series_series_field",
           "field_series", ["series_id", "field_id"], ["series_id", "field_id"],
           "n", ["series_id"]),
        fk("interseries_calibration_series", "member_cv_fkey", "calibration_versions",
           ["science_calibration_version_id"], ["calibration_version_id"], "r"),
        fk("interseries_calibration_edges", "edge_gen_fkey",
           "interseries_calibration_generations", ["interseries_generation_id"],
           ["interseries_generation_id"], "c"),
        fk("interseries_calibration_edges", "fk_interseries_edges_left_series_restrict",
           "field_series", ["left_series_id"], ["series_id"], "r"),
        fk("interseries_calibration_edges", "fk_interseries_edges_right_series_restrict",
           "field_series", ["right_series_id"], ["series_id"], "r"),
    ]

    def unique(child, name, keys):
        return (child, name, "p" if name.endswith("_pkey") else "u", None, None,
                list(keys), [], None, " ", " ", " ", True, False, False,
                True, 0, True, True, True, True, True, None)

    constraints += [
        unique("calibration_versions", "calibration_versions_pkey", ["calibration_version_id"]),
        unique("fields", "fields_pkey", ["field_id"]),
        unique("field_series", "field_series_pkey", ["series_id"]),
        unique("field_series", "uq_field_series_series_field", ["series_id", "field_id"]),
        unique("interseries_calibration_generations", "uq_interseries_generations_id_field",
               ["interseries_generation_id", "field_id"]),
        unique("interseries_calibration_series", "uq_interseries_calibration_series_generation_series",
               ["interseries_generation_id", "series_id"]),
        unique("interseries_calibration_edges", "uq_interseries_calibration_edges_generation_pair",
               ["interseries_generation_id", "left_series_id", "right_series_id"]),
        ("interseries_calibration_edges", "ck_interseries_calibration_edges_series_order",
         "c", None, None, [], [], None, " ", " ", " ", True, False, False,
         True, 0, True, None, None, None, None, "(left_series_id < right_series_id)"),
    ]
    triggers = []
    for child, name, _, _, parent, _, _, _, delete, *_ in constraints[:9]:
        triggers.extend([
            (name, child, True, "O", 5, False, False, child, "RI_FKey_check_ins"),
            (name, child, True, "O", 17, False, False, child, "RI_FKey_check_upd"),
            (name, child, True, "O", 9, False, False, parent,
             {"c": "RI_FKey_cascade_del", "r": "RI_FKey_restrict_del",
              "n": "RI_FKey_setnull_del"}[delete]),
            (name, child, True, "O", 17, False, False, parent, "RI_FKey_noaction_upd"),
        ])
    return sizes, columns, constraints, triggers


def test_integrity_old_fks_derive_independently_from_actual_model():
    old_model_fks = {}
    for model in (InterseriesCalibrationGeneration, InterseriesCalibrationSeries,
                  InterseriesCalibrationEdge):
        for constraint in model.__table__.foreign_key_constraints:
            if constraint.name in migrations._P207_INTEGRITY_NEW_NAMES:
                continue  # .7/.8 source additions are absent from OLD35.
            elements = constraint.elements
            key = (model.__tablename__, tuple(element.parent.name for element in elements))
            assert key not in old_model_fks
            parents = {element.column.table.name for element in elements}
            assert len(parents) == 1
            old_model_fks[key] = (parents.pop(), tuple(element.column.name for element in elements),
                                  constraint.ondelete)
            assert constraint.onupdate is None
            assert constraint.deferrable in (None, False)

    instrument = InterseriesCalibrationGeneration.__table__.c.instrument_unit_id
    (instrument_fk,) = instrument.foreign_keys
    assert instrument.nullable is True
    assert instrument_fk.target_fullname == "instrument_units.instrument_unit_id"
    assert instrument_fk.ondelete == "SET NULL"
    instrument_key = ("interseries_calibration_generations", ("instrument_unit_id",))
    assert old_model_fks[instrument_key] == ("instrument_units", ("instrument_unit_id",),
                                             "SET NULL")
    assert len(old_model_fks) == 9

    fake_fks = [row for row in _fake_integrity_catalog()[2] if row[2] == "f"]
    assert len(fake_fks) == 9
    for row in fake_fks:
        child, _, _, schema, parent, child_keys, parent_keys, set_columns = row[:8]
        assert schema == "public" and row[9:11] == ("a", "s")
        action = {"c": "CASCADE", "r": "RESTRICT", "n": "SET NULL"}[row[8]]
        if set_columns is not None:
            assert action == "SET NULL"
            action += " (" + ", ".join(set_columns) + ")"
        assert old_model_fks[(child, tuple(child_keys))] == (parent, tuple(parent_keys), action)
        if (child, tuple(child_keys)) == instrument_key:
            assert set_columns is None  # Default SET NULL applies to the whole child key.


def _fake_integrity_data():
    series = [(i, 1 if i <= 9 else 2) for i in range(1, 19)]
    generations = [(g, 1 if g <= 2 else 2, 1 if g <= 2 else 10)
                   for g in range(1, 5)]
    members = []
    cvs = []
    edges = []
    for generation, field, reference in generations:
        group = range(1, 10) if field == 1 else range(10, 19)
        for series_id in group:
            cv_id = len(cvs) + 1
            cvs.append((cv_id, field, series_id, "SCIENCE", "COMPLETE_IMMUTABLE"))
            members.append((len(members) + 1, generation, field, series_id,
                            cv_id, series_id == reference))
            for right in range(series_id + 1, group.stop):
                edges.append((generation, series_id, right,
                              "OK" if right % 2 else "REJECTED"))
    assert len(edges) == 144
    return [cvs, series, generations, members, edges]


class _FakeIntegrityConnection(_FakeLedgerConnection):
    def __init__(self, row, *, backend_pid=457, fail_at=None):
        super().__init__(row, backend_pid=backend_pid, fail_at=fail_at)
        self.ledger = _rows(35)
        self.catalog = [list(x) for x in _fake_integrity_catalog()]
        self.new_names = []
        self.data = _fake_integrity_data()
        self.current = None

    def execute(self, sql):
        if sql in ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                   migrations._P207_PROBE_SQL):
            self.current = ("probe", None)
            return super().execute(sql)
        self.statements.append(sql)
        for index, known in enumerate((migrations._P207_INTEGRITY_SIZES_SQL,
                                       migrations._P207_INTEGRITY_NEW_SQL,
                                       migrations._P207_INTEGRITY_COLUMNS_SQL,
                                       migrations._P207_INTEGRITY_CONSTRAINTS_SQL,
                                       migrations._P207_INTEGRITY_TRIGGERS_SQL)):
            if sql == known:
                self.current = ("catalog", index)
                return self
        if sql == migrations._P207_LEDGER_SQL:
            self.current = ("ledger", None)
            return self
        for index, (table, cap, columns) in enumerate(migrations._P207_INTEGRITY_DATA):
            if sql == f"SELECT count(*) FROM (SELECT 1 FROM public.{table} LIMIT {cap + 1}) AS bounded":
                self.current = ("count", index)
                return self
            if sql == f"SELECT {columns} FROM public.{table} LIMIT {cap + 1}":
                self.current = ("data", index)
                return self
        raise AssertionError("Unexpected SQL in read-only fake")

    def fetchone(self):
        self.fetches += 1
        if self.fail_at == "catalog_fetch" and self.current == ("count", 0):
            raise RuntimeError(URL)
        if self.current[0] == "count":
            return (len(self.data[self.current[1]]),)
        return super().fetchone() if self.current[0] == "probe" else None

    def fetchall(self):
        self.fetches += 1
        if self.fail_at == "catalog_fetch" and self.current == ("catalog", 0):
            raise RuntimeError(URL)
        kind, index = self.current
        if kind == "ledger":
            return self.ledger
        if kind == "data":
            return self.data[index]
        if kind == "catalog":
            return self.new_names if index == 1 else self.catalog[{0: 0, 2: 1, 3: 2, 4: 3}[index]]
        raise AssertionError("Unexpected fetch in read-only fake")


def _fake_integrity(monkeypatch, *, row=None, fail_at=None, environ=None):
    conn, actions = _fake_probe(monkeypatch, row=row, fail_at=fail_at,
                                environ=environ, connection_type=_FakeIntegrityConnection)
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_integrity",
                        _FAKE_ONLY_INTEGRITY_HELPER)
    monkeypatch.setattr(migrations, "_p207_integrity_source_gate",
                        lambda expected: actions.append("mirror pins") or None)
    return conn, actions


def _stop_integrity(monkeypatch, capsys, change, *, expected_statements=None):
    conn, actions = _fake_integrity(monkeypatch)
    change(conn)
    assert cli.main(INTEGRITY_ARGS) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_TEST_INTEGRITY_UNVERIFIED"
    assert URL not in result.err and PRIVATE_CONFIG not in result.err
    assert conn.rollbacks == conn.closes == 1
    if expected_statements is not None:
        assert conn.statements == expected_statements
    assert actions == ["manifest", "mirror pins", "synthetic source", "fake connect"]


def test_integrity_mirror_source_pins_offline_no_private_access():
    expected = migrations._p207_expected_ledger()
    migrations._p207_integrity_source_gate(expected)
    wrong = list(expected)
    wrong[-1] = (wrong[-1][0], "0" * 64)
    with pytest.raises(ValueError, match="migration source mismatch"):
        migrations._p207_integrity_source_gate(tuple(wrong))


def test_integrity_exact_argv_old_catalog_and_all_status_rows_offline(monkeypatch, capsys):
    ordinary = _no_settings_or_parser(monkeypatch)
    conn, actions = _fake_integrity(monkeypatch)
    assert cli.main(INTEGRITY_ARGS) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.strip() == ("PREFLIGHT_ONLY P207_TEST_INTEGRITY OLD35 NOT_WRITE_READY "
                                  "ledger=35 cv=36 series=18 generations=4 members=36 edges=144 "
                                  "role_lifecycle=DIAGNOSTIC_ONLY")
    assert ordinary == []
    assert actions == ["manifest", "mirror pins", "synthetic source", "fake connect"]
    assert conn.rollbacks == conn.closes == 1
    assert conn.statements[:3] == [
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL,
    ]
    assert conn.statements[3:8] == [
        migrations._P207_INTEGRITY_SIZES_SQL, migrations._P207_INTEGRITY_NEW_SQL,
        migrations._P207_INTEGRITY_COLUMNS_SQL, migrations._P207_INTEGRITY_CONSTRAINTS_SQL,
        migrations._P207_INTEGRITY_TRIGGERS_SQL,
    ]
    assert len(conn.statements) == 18  # BEGIN, probe, ledger, 5 catalog, 5 caps, 5 data.
    assert all(sql.startswith(("SELECT ", "BEGIN TRANSACTION "))
               for sql in conn.statements)
    assert all(" LIMIT " in sql for sql in conn.statements[2:])
    assert not any("science_photometry" in sql or "measurements" in sql
                   or "field_series_frames" in sql or "artifacts" in sql
                   or "EXPLAIN" in sql or "json" in sql.lower()
                   for sql in conn.statements)


@pytest.mark.parametrize("args", [
    ["schema-status", INTEGRITY_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", "--guard-p207-test-readonly-integrit"],
    ["--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG + "=true"],
    ["--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG + "extra"],
    ["--conf", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG],
    ["--config=" + PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG],
    ["schema-status", "--config", PRIVATE_CONFIG, INTEGRITY_FLAG],
    ["--config", PRIVATE_CONFIG, INTEGRITY_FLAG, "schema-status"],
    ["--config", PRIVATE_CONFIG, "migrate-db", INTEGRITY_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG, INTEGRITY_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG, PROBE_FLAG],
    ["--config", PRIVATE_CONFIG, "schema-status", FLAG, INTEGRITY_FLAG],
    ["--database-url", URL, "--config", PRIVATE_CONFIG, "schema-status", INTEGRITY_FLAG],
    ["--config", "synthetic.yaml", "schema-status", INTEGRITY_FLAG],
])
def test_integrity_malformed_args_stop_before_source_settings_and_driver(
    monkeypatch, capsys, args,
):
    ordinary = _no_settings_or_parser(monkeypatch)
    conn, actions = _fake_integrity(monkeypatch)
    assert cli.main(args) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "STOP P207_TEST_INTEGRITY_INVALID_ARGS"
    assert ordinary == actions == conn.statements == []


def test_integrity_implicit_argv_prefix_stops_early(monkeypatch, capsys):
    ordinary = _no_settings_or_parser(monkeypatch)
    conn, actions = _fake_integrity(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["grandgousier", "schema-status", "--guard-p207-test-readonly-integ"])
    assert cli.main() == 2
    assert capsys.readouterr().err.strip() == "STOP P207_TEST_INTEGRITY_INVALID_ARGS"
    assert ordinary == actions == conn.statements == []


@pytest.mark.parametrize("name", ["PGPASSWORD", "pgHOST", "GRANDGOUSIER_DATABASE_URL"])
def test_integrity_env_name_refused_before_source(monkeypatch, capsys, name):
    conn, actions = _fake_integrity(monkeypatch, environ={name: "fake"})
    assert cli.main(INTEGRITY_ARGS) == 2
    assert capsys.readouterr().err.strip() == "STOP P207_TEST_INTEGRITY_UNVERIFIED"
    assert actions == conn.statements == []


@pytest.mark.parametrize("defect", ["missing", "wrong_delete", "explicit_set_columns"])
def test_integrity_instrument_unit_fk_mismatch_stops_before_ri(monkeypatch, capsys, defect):
    def change(conn):
        constraints = conn.catalog[2]
        index = next(i for i, row in enumerate(constraints)
                     if row[0] == "interseries_calibration_generations"
                     and row[5] == ["instrument_unit_id"])
        row = constraints[index]
        if defect == "missing":
            constraints.pop(index)
        elif defect == "wrong_delete":
            constraints[index] = (*row[:8], "c", *row[9:])
        else:
            constraints[index] = (*row[:7], ["instrument_unit_id"], *row[8:])

    _stop_integrity(monkeypatch, capsys, change, expected_statements=[
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL,
        migrations._P207_INTEGRITY_SIZES_SQL, migrations._P207_INTEGRITY_NEW_SQL,
        migrations._P207_INTEGRITY_COLUMNS_SQL, migrations._P207_INTEGRITY_CONSTRAINTS_SQL,
    ])


@pytest.mark.parametrize("defect", ["disabled_child", "missing_parent"])
def test_integrity_instrument_unit_ri_trigger_mismatch_stops(monkeypatch, capsys, defect):
    def change(conn):
        constraints = conn.catalog[2]
        name = next(row[1] for row in constraints
                    if row[0] == "interseries_calibration_generations"
                    and row[5] == ["instrument_unit_id"])
        triggers = conn.catalog[3]
        function = "RI_FKey_check_ins" if defect == "disabled_child" else "RI_FKey_setnull_del"
        index = next(i for i, row in enumerate(triggers)
                     if row[0] == name and row[8] == function)
        if defect == "disabled_child":
            row = triggers[index]
            triggers[index] = (*row[:3], "D", *row[4:])
        else:
            triggers.pop(index)

    _stop_integrity(monkeypatch, capsys, change, expected_statements=[
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL,
        migrations._P207_INTEGRITY_SIZES_SQL, migrations._P207_INTEGRITY_NEW_SQL,
        migrations._P207_INTEGRITY_COLUMNS_SQL, migrations._P207_INTEGRITY_CONSTRAINTS_SQL,
        migrations._P207_INTEGRITY_TRIGGERS_SQL,
    ])


@pytest.mark.parametrize("name", sorted(migrations._P207_INTEGRITY_NEW_NAMES))
def test_integrity_each_new_constraint_name_blocks_before_data(monkeypatch, capsys, name):
    _stop_integrity(monkeypatch, capsys, lambda c: c.new_names.append((name,)),
                    expected_statements=["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                                         migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL,
                                         migrations._P207_INTEGRITY_SIZES_SQL,
                                         migrations._P207_INTEGRITY_NEW_SQL])


@pytest.mark.parametrize("length", [36, 37])
def test_integrity_partial_or_new_ledger_stops_before_catalog(monkeypatch, capsys, length):
    _stop_integrity(monkeypatch, capsys, lambda c: setattr(c, "ledger", _rows(length)),
                    expected_statements=["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                                         migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL])


def test_integrity_drift_stops_before_catalog(monkeypatch, capsys):
    def drift(conn):
        conn.ledger[1] = ("different", *conn.ledger[1][1:])
    _stop_integrity(monkeypatch, capsys, drift,
                    expected_statements=["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                                         migrations._P207_PROBE_SQL, migrations._P207_LEDGER_SQL])


@pytest.mark.parametrize("mutation", [
    lambda c: c.catalog[0].__setitem__(0, (c.catalog[0][0][0], "r", False, 67108865)),
    lambda c: c.catalog[1].__setitem__(0, (c.catalog[1][0][0],
                                        c.catalog[1][0][1], "integer", True)),
    lambda c: c.catalog[2].__setitem__(0, (*c.catalog[2][0][:16], False,
                                        *c.catalog[2][0][17:])),
    lambda c: c.catalog[3].__setitem__(0, (*c.catalog[3][0][:3], "D",
                                        *c.catalog[3][0][4:])),
    lambda c: c.catalog[3].pop(),
    lambda c: c.data[0].extend([(i, None, None, "BASE", "MUTABLE")
                              for i in range(37, 10002)]),
    lambda c: c.data[1].append((19, 2)),
    lambda c: c.data[2].append((5, 2, 10)),
    lambda c: c.data[3].append((37, 4, 2, 18, 36, False)),
    lambda c: c.data[4].append((4, 17, 18, "BAD")),
    lambda c: c.data[4].__setitem__(0, c.data[4][1]),
    lambda c: c.data[4].__setitem__(0, (1, 1, 10, "BAD")),
    lambda c: c.data[4].__setitem__(0, (1, 2, 1, "BAD")),
    lambda c: c.data[3].__setitem__(0, (1, 1, 1, 2, 1, True)),
    lambda c: c.data[2].__setitem__(0, (1, 1, 9)),
    lambda c: c.data[0].__setitem__(0, (1, 2, 1, "SCIENCE", "COMPLETE_IMMUTABLE")),
    lambda c: c.data[0].__setitem__(0, (1, 1, 1, "BASE", "COMPLETE_IMMUTABLE")),
    lambda c: c.data[3].__setitem__(0, (1, 1, 1, None, 1, True)),
])
def test_integrity_contract_caps_and_row_anomalies_stop(monkeypatch, capsys, mutation):
    _stop_integrity(monkeypatch, capsys, mutation)


@pytest.mark.parametrize("stage", ["source", "manifest", "pins", "connect", "begin", "probe",
                                   "fetch", "catalog_fetch", "rollback", "close"])
def test_integrity_failures_timeout_and_cleanup_are_sanitized(monkeypatch, capsys, stage):
    conn, actions = _fake_integrity(monkeypatch, fail_at=stage)
    if stage == "source":
        monkeypatch.setattr(migrations, "_p207_read_private_source",
                            lambda: (_ for _ in ()).throw(TimeoutError(URL)))
    if stage == "manifest":
        monkeypatch.setattr(migrations, "_p207_expected_ledger",
                            lambda: (_ for _ in ()).throw(RuntimeError(URL)))
    if stage == "pins":
        monkeypatch.setattr(migrations, "_p207_integrity_source_gate",
                            lambda _expected: (_ for _ in ()).throw(ValueError(URL)))
    assert cli.main(INTEGRITY_ARGS) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err.strip() == "STOP P207_TEST_INTEGRITY_UNVERIFIED"
    assert URL not in result.err and PRIVATE_CONFIG not in result.err
    assert conn.rollbacks == conn.closes == (0 if stage in ("source", "manifest", "pins", "connect") else 1)
    assert len(actions) <= 4


def test_integrity_probe_list_slice_must_not_waive_existing_aggregate_guard(monkeypatch, capsys):
    conn, _ = _fake_integrity(monkeypatch, row=list(_probe_row()))
    assert cli.main(INTEGRITY_ARGS) == 2
    assert capsys.readouterr().err.strip() == "STOP P207_TEST_INTEGRITY_UNVERIFIED"
    assert conn.statements == ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
                               migrations._P207_PROBE_SQL]
    assert conn.rollbacks == conn.closes == 1


@pytest.mark.parametrize("invalid", [(35, 36, 19, 4, 36), URL, (35, 36, 18, 4, True), None])
def test_integrity_untrusted_return_never_promoted(monkeypatch, capsys, invalid):
    monkeypatch.setattr(migrations, "guarded_p207_test_readonly_integrity", lambda: invalid)
    assert cli.main(INTEGRITY_ARGS) == 2
    assert capsys.readouterr().err.strip() == "STOP P207_TEST_INTEGRITY_UNVERIFIED"
