from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
import types
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from astropy.io import fits
from sqlalchemy import (
    BigInteger,
    MetaData,
    Table,
    and_,
    create_engine,
    delete,
    event,
    func,
    inspect as sa_inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from grandgousier import config as app_config
from grandgousier import settings as app_settings
from grandgousier.archive.observation_selection import ObservationScienceAction
from grandgousier.archive.stage1_adapters import (
    FilesystemAssetProvider,
    FilesystemProductStore,
)
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    LocalAsset,
    ObservationRecord,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_execution import (
    STAGE1_EXECUTION_SCHEMA,
    STAGE1_PROVENANCE_SCHEMA,
    _fingerprint,
    _input_fingerprint_payload,
    execute_stage1_stream,
)
from grandgousier.archive.stage1_handoff import (
    Stage1HandoffItem,
    _persist_selected_evidence,
    handoff_stage1_execution,
    load_validated_stage1_products,
)
from grandgousier.archive.stage1_reclaim import _validate_product
from grandgousier.cli import build_parser
from grandgousier.db.models import (
    Base,
    CatalogSource,
    Field,
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    FrameLocation,
    FrameStatus,
    FrameType,
    InstrumentUnit,
    Observation,
    ProcessingEvent,
    ProcessingEventLevel,
    make_frame_location_uid,
    make_instrument_unit_uid,
)
from grandgousier.identity.observation import make_observation_uid
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.frame_uid import make_frame_uid
from grandgousier.resources import instrument_profile_path


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    # Limit the temporary SQLite schema to the handoff contracts under test; do not
    # rewrite the unrelated PostgreSQL-only column-list SET NULL model constraint.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[
            InstrumentUnit.__table__,
            Observation.__table__,
            Frame.__table__,
            FrameLocation.__table__,
            Field.__table__,
            FieldSeries.__table__,
            FieldSeriesFrame.__table__,
            CatalogSource.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


_POSTGRES_IT_DATABASE_NAME = "grandgousier_it_series_776_20261025_a7c9e2"
_POSTGRES_IT_DATABASE_OID = 23088045
_POSTGRES_IT_ROLE = "grandgousier"
_POSTGRES_IT_HOST = "127.0.0.1"
_POSTGRES_IT_PORT = 5432
_POSTGRES_IT_PREFIX = "grandgousier_it_"
_POSTGRES_IT_SERVER_VERSION_NUM = 180004
_PINNED_DURABLE_CASE_ID = "validation_e_d2_master_20261027_01"
_PINNED_DURABLE_ROOT = Path(
    r"C:\Users\nikita\AppData\Local\Temp\opencode\grandgousier_validation_e_case_validation_e_d2_master_20261027_01"
)
_PINNED_DURABLE_MANIFEST_ENV = "GRANDGOUSIER_VALIDATION_E_MANIFEST_SHA256"
_UNIT_ROW_COLUMNS = tuple(column.name for column in InstrumentUnit.__table__.columns)
_UNIT_WITNESS_COLUMNS = _UNIT_ROW_COLUMNS[:-2]
_UNIT_IDENTITY_FIELDS = (
    "unit_uid", "instrument", "site", "tube", "camera", "filter_name", "observing_mode"
)


def _durable_master_unit_profile_metadata() -> dict[str, Any]:
    profile = InstrumentProfile.from_yaml(instrument_profile_path("master.yaml"))
    return {
        name: getattr(profile, value)
        for name, value in {
            "profile_name": "name",
            "profile_version": "version",
            "pixel_scale_arcsec": "pixel_scale_arcsec",
            "saturation_level_adu": "saturation_level_adu",
            "nominal_gain_e_per_adu": "nominal_gain_e_per_adu",
            "nominal_read_noise_e": "nominal_read_noise_e",
            "config_json": "raw_config",
        }.items()
    } | {"config_json": _jsonb_persisted_value(profile.raw_config or {}), "active": True}


def _jsonb_persisted_value(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _instrument_unit_snapshot(row: Any) -> dict[str, Any]:
    return {name: row[name] if hasattr(row, "__getitem__") else getattr(row, name) for name in _UNIT_ROW_COLUMNS}


def _instrument_unit_witness(row: Any) -> dict[str, Any]:
    witness = {
        name: row[name] if hasattr(row, "__getitem__") else getattr(row, name)
        for name in _UNIT_WITNESS_COLUMNS
    }
    witness["config_json"] = _jsonb_persisted_value(witness["config_json"])
    return witness


def _expected_instrument_unit_witness(case: Any, instrument_unit_id: int) -> dict[str, Any]:
    identity = (
        case.instrument_unit_uid, case.record.instrument, case.record.site, case.record.tube,
        case.record.camera, case.record.filter_name, case.record.object_name,
    )
    return {
        "instrument_unit_id": instrument_unit_id,
        **dict(zip(_UNIT_IDENTITY_FIELDS, identity, strict=True)),
        **_durable_master_unit_profile_metadata(),
    }


def _instrument_unit_witness_matches_case(case: Any, witness: Any) -> bool:
    if not isinstance(witness, dict) or not isinstance(witness.get("instrument_unit_id"), int):
        return False
    return witness == _expected_instrument_unit_witness(case, witness["instrument_unit_id"])


def _assert_unit_commit_witness(
    case: Any, pending: dict[int, tuple[Any, dict[str, Any]]]
) -> None:
    preexisting = case.preflight_unit_snapshot
    entry = pending.get(id(case))
    valid = (
        preexisting is not None
        and case.unit_ownership == "PREEXISTING"
        and set(preexisting) == set(_UNIT_ROW_COLUMNS)
        and _instrument_unit_witness(preexisting)
        == _expected_instrument_unit_witness(case, preexisting["instrument_unit_id"])
    ) or (
        preexisting is None
        and case.unit_insert_witness is None
        and case.unit_ownership == "UNKNOWN"
        and entry is not None
        and entry[0] is case
        and _instrument_unit_witness_matches_case(case, entry[1])
    ) or (
        preexisting is None
        and case.unit_ownership == "NEW"
        and entry is None
        and _instrument_unit_witness_matches_case(case, case.unit_insert_witness)
    )
    if not valid:
        raise AssertionError("InstrumentUnit commit lacks an exact preflight or INSERT witness")


def _unit_identity(row: Any) -> tuple[str, ...]:
    return tuple(str(row[name]) for name in _UNIT_IDENTITY_FIELDS)


def _postgres_integration_raw_url(pytestconfig) -> str | None:
    config_path = pytestconfig.getoption("--grandgousier-test-config")
    if config_path is None:
        return os.environ.get("GRANDGOUSIER_TEST_DATABASE_URL")

    try:
        payload = app_config.read_config_file(config_path)
        explicit_url = app_config.application_settings_mapping(payload).get("database_url")
        if not isinstance(explicit_url, str) or not explicit_url:
            raise ValueError
        settings = app_settings.get_settings(config_file=config_path)
        if settings.database_url != explicit_url:
            raise ValueError
    except Exception:
        raise AssertionError(
            "Explicit PostgreSQL integration config is missing, invalid, or does not "
            "contain a matching database URL; config details and credentials are redacted"
        ) from None

    print(
        "PostgreSQL integration config loaded from --grandgousier-test-config; "
        "URL and credentials are redacted"
    )
    return explicit_url


def _guard_postgres_integration_connection(
    connection, *, read_only: bool = False
) -> tuple[str, str, str]:
    try:
        identity = connection.execute(
            text(
                """
                SELECT
                    current_database() AS database_name,
                    (SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid,
                    current_user AS role_name,
                    current_setting('server_version_num')::integer AS server_version_num,
                    inet_server_addr() AS server_address,
                    inet_server_port() AS server_port
                """
            )
        ).mappings().one()
        default_read_only = str(
            connection.execute(text("SHOW default_transaction_read_only")).scalar_one()
        ).casefold()
        transaction_read_only = str(
            connection.execute(text("SHOW transaction_read_only")).scalar_one()
        ).casefold()

        server_address_value = identity["server_address"]
        normalized_server_address = ipaddress.ip_interface(str(server_address_value)).ip
        server_is_loopback = normalized_server_address.is_loopback
        server_version_num = int(identity["server_version_num"])
        identity_summary = (
            f"database={identity['database_name']!r}, "
            f"oid={identity['database_oid']!r}, "
            f"role={identity['role_name']!r}, "
            f"server_version_num={server_version_num!r}, "
            f"server_address={str(server_address_value)!r}, "
            f"server_port={identity['server_port']!r}"
        )
        identity_matches_target = (
            identity["database_name"] == _POSTGRES_IT_DATABASE_NAME
            and identity["database_oid"] == _POSTGRES_IT_DATABASE_OID
            and identity["role_name"] == _POSTGRES_IT_ROLE
            and server_version_num == _POSTGRES_IT_SERVER_VERSION_NUM
            and server_is_loopback
            and identity["server_port"] == _POSTGRES_IT_PORT
            and str(identity["database_name"]).startswith(_POSTGRES_IT_PREFIX)
        )
    except Exception:
        raise AssertionError(
            "PostgreSQL integration identity/read-only probe failed; server details are suppressed"
        ) from None

    expected_transaction_read_only = "on" if read_only else "off"
    if (
        not identity_matches_target
        or default_read_only != "off"
        or transaction_read_only != expected_transaction_read_only
    ):
        raise AssertionError(
            "PostgreSQL integration identity/read-only guard mismatch; observed safe fields: "
            f"{identity_summary}, "
            f"default_transaction_read_only={default_read_only!r}, "
            f"transaction_read_only={transaction_read_only!r}; URL and credentials are redacted"
        )

    return identity_summary, default_read_only, transaction_read_only


def _postgres_integration_session(
    pytestconfig, *, read_only: bool = False
) -> Iterator[Session]:
    raw_url = _postgres_integration_raw_url(pytestconfig)
    if raw_url is None:
        pytest.skip(
            "GRANDGOUSIER_TEST_DATABASE_URL is not set; no database connection attempted"
        )

    try:
        database_url = make_url(raw_url)
        is_postgresql = database_url.get_backend_name() == "postgresql"
        url_matches_target = (
            is_postgresql
            and database_url.host == _POSTGRES_IT_HOST
            and database_url.port == _POSTGRES_IT_PORT
            and database_url.database == _POSTGRES_IT_DATABASE_NAME
            and database_url.username == _POSTGRES_IT_ROLE
            and _POSTGRES_IT_DATABASE_NAME.startswith(_POSTGRES_IT_PREFIX)
            and not database_url.query
        )
    except Exception:
        raise AssertionError(
            "PostgreSQL integration URL is malformed or unsupported; "
            "URL and credentials are redacted"
        ) from None

    if not url_matches_target:
        raise AssertionError(
            "PostgreSQL integration URL does not match the explicitly authorized "
            "loopback test target; URL and credentials are redacted"
        )

    try:
        engine = create_engine(database_url, echo=False)
    except Exception:
        raise AssertionError(
            "PostgreSQL integration engine setup failed; connection details are redacted"
        ) from None

    connection = None
    outer_transaction = None
    session = None
    try:
        try:
            connection = engine.connect()
        except Exception:
            raise AssertionError(
                "PostgreSQL integration connection failed; URL and credentials are redacted"
            ) from None

        try:
            outer_transaction = connection.begin()
            if read_only:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        except Exception:
            raise AssertionError(
                "PostgreSQL integration transaction setup failed; connection details are redacted"
            ) from None

        identity_summary, default_read_only, transaction_read_only = (
            _guard_postgres_integration_connection(connection, read_only=read_only)
        )
        print(
            "PostgreSQL integration guard passed: "
            f"{identity_summary}, "
            f"default_transaction_read_only={default_read_only!r}, "
            f"transaction_read_only={transaction_read_only!r}"
        )

        try:
            session = Session(
                bind=connection,
                join_transaction_mode="create_savepoint",
            )
        except Exception:
            raise AssertionError(
                "PostgreSQL integration session setup failed; connection details are redacted"
            ) from None
        yield session
    finally:
        cleanup_failed = False
        def rollback_outer_transaction() -> None:
            if outer_transaction is None:
                return
            was_active = outer_transaction.is_active
            outer_transaction.rollback()
            if not was_active:
                raise RuntimeError("outer PostgreSQL transaction was not active at teardown")

        for cleanup, label in (
            (session.close if session is not None else None, "session close"),
            (rollback_outer_transaction if outer_transaction is not None else None, "outer rollback"),
            (connection.close if connection is not None else None, "connection close"),
            (engine.dispose, "engine dispose"),
        ):
            if cleanup is None:
                continue
            try:
                cleanup()
            except Exception:
                cleanup_failed = True
            else:
                if label == "outer rollback":
                    print("PostgreSQL integration outer transaction rollback completed")
        if cleanup_failed:
            raise AssertionError(
                "PostgreSQL integration cleanup failed; connection details are suppressed"
            ) from None


@pytest.fixture()
def postgres_integration_session(pytestconfig) -> Iterator[Session]:
    yield from _postgres_integration_session(pytestconfig)


def test_postgres_integration_fixture_smoke(
    postgres_integration_session: Session,
) -> None:
    assert postgres_integration_session.scalar(text("SELECT 1")) == 1


def test_postgres_integration_connection_guard_only(
    postgres_integration_session: Session,
) -> None:
    """Exercise the guarded connection fixture without issuing application queries."""
    pass


class _FakePytestConfig:
    def __init__(self, config_path=None):
        self.config_path = config_path
        self.option_names = []

    def getoption(self, name):
        self.option_names.append(name)
        assert name == "--grandgousier-test-config"
        return self.config_path


def _write_postgres_test_config(tmp_path: Path, database_url: str) -> Path:
    path = tmp_path / "integration-test.yaml"
    path.write_text(f"database:\n  url: {database_url!r}\n", encoding="utf-8")
    return path


def _install_fake_postgres_fixture(
    monkeypatch,
    *,
    identity_overrides=None,
    show_overrides=None,
    connection_error: Exception | None = None,
):
    events = []
    identity = {
        "database_name": _POSTGRES_IT_DATABASE_NAME,
        "database_oid": _POSTGRES_IT_DATABASE_OID,
        "role_name": _POSTGRES_IT_ROLE,
        "server_version_num": 180004,
        "server_address": "127.0.0.1",
        "server_port": _POSTGRES_IT_PORT,
    }
    identity.update(identity_overrides or {})
    show_values = {
        "default_transaction_read_only": "off",
        "transaction_read_only": "off",
    }
    show_values.update(show_overrides or {})

    class FakeResult:
        def __init__(self, *, row=None, value=None):
            self.row = row
            self.value = value

        def mappings(self):
            return self

        def one(self):
            return self.row

        def scalar_one(self):
            return self.value

    class FakeTransaction:
        def __init__(self):
            self.is_active = True

        def rollback(self):
            events.append("transaction.rollback")
            self.is_active = False

    class FakeConnection:
        def __init__(self):
            self.transaction = FakeTransaction()

        def begin(self):
            events.append("transaction.begin")
            return self.transaction

        def execute(self, statement):
            query = str(statement).strip().casefold()
            if query.startswith("select"):
                events.append("probe.identity")
                return FakeResult(row=identity)
            if query == "show default_transaction_read_only":
                events.append("probe.default_transaction_read_only")
                return FakeResult(value=show_values["default_transaction_read_only"])
            if query == "show transaction_read_only":
                events.append("probe.transaction_read_only")
                return FakeResult(value=show_values["transaction_read_only"])
            raise AssertionError("unexpected fake PostgreSQL query")

        def close(self):
            events.append("connection.close")

    connection = FakeConnection()

    class FakeEngine:
        def connect(self):
            events.append("connection.open")
            if connection_error is not None:
                raise connection_error
            return connection

        def dispose(self):
            events.append("engine.dispose")

    engine = FakeEngine()

    class FakeSession:
        def __init__(self, *, bind, join_transaction_mode):
            assert bind is connection
            assert join_transaction_mode == "create_savepoint"
            events.append("session.create")

        def close(self):
            events.append("session.close")

    def fake_create_engine(_database_url, *, echo):
        assert echo is False
        events.append("engine.create")
        return engine

    monkeypatch.setattr(sys.modules[__name__], "create_engine", fake_create_engine)
    monkeypatch.setattr(sys.modules[__name__], "Session", FakeSession)
    return events, connection


def test_postgres_test_config_option_precedence_and_source(monkeypatch, tmp_path: Path) -> None:
    explicit_url = (
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-config-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}"
    )
    environment_url = "postgresql://grandgousier:fake-env-secret@127.0.0.1:5432/other"
    path = _write_postgres_test_config(tmp_path, explicit_url)
    monkeypatch.setenv("GRANDGOUSIER_TEST_DATABASE_URL", environment_url)
    monkeypatch.setenv("GRANDGOUSIER_DATABASE_URL", environment_url)

    real_get_settings = app_settings.get_settings
    settings_calls = []

    def tracked_get_settings(*, config_file=None):
        settings_calls.append(config_file)
        return real_get_settings(config_file=config_file)

    monkeypatch.setattr(app_settings, "get_settings", tracked_get_settings)
    selected = _postgres_integration_raw_url(_FakePytestConfig(path))
    assert selected == explicit_url
    assert settings_calls == [path]

    monkeypatch.setattr(
        app_config,
        "read_config_file",
        lambda *_args, **_kwargs: pytest.fail("config file read without the option"),
    )
    monkeypatch.setattr(
        app_settings,
        "get_settings",
        lambda **_kwargs: pytest.fail("settings loaded without the option"),
    )
    assert _postgres_integration_raw_url(_FakePytestConfig()) == environment_url


@pytest.mark.parametrize("failure", ["missing", "invalid", "absent-url", "mismatch"])
def test_explicit_config_failures_stop_before_engine_creation(
    monkeypatch, tmp_path: Path, failure: str, capsys
) -> None:
    explicit_url = (
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-config-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}"
    )
    monkeypatch.setenv("GRANDGOUSIER_TEST_DATABASE_URL", explicit_url)
    path = tmp_path / "explicit-test-config.yaml"
    if failure == "missing":
        path = tmp_path / "missing-test-config.yaml"
    elif failure == "invalid":
        path.write_text("database: [", encoding="utf-8")
    elif failure == "absent-url":
        path.write_text("log_level: INFO\n", encoding="utf-8")
    else:
        path = _write_postgres_test_config(tmp_path, explicit_url)
        monkeypatch.setattr(
            app_settings,
            "get_settings",
            lambda **_kwargs: SimpleNamespace(database_url="postgresql://mismatch"),
        )

    engine_calls = []

    def forbidden_engine(*_args, **_kwargs):
        engine_calls.append(True)
        raise AssertionError("engine must not be created")

    monkeypatch.setattr(sys.modules[__name__], "create_engine", forbidden_engine)
    generator = _postgres_integration_session(_FakePytestConfig(path))
    with pytest.raises(AssertionError, match="Explicit PostgreSQL integration config") as exc:
        next(generator)

    assert exc.value.__cause__ is None
    assert "fake-config-secret" not in str(exc.value)
    assert engine_calls == []
    assert "fake-config-secret" not in capsys.readouterr().out


def test_postgres_integration_url_rejection_precedes_engine_creation(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    wrong_url = (
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-url-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/wrong_test_database"
    )
    path = _write_postgres_test_config(tmp_path, wrong_url)
    engine_calls = []

    def forbidden_engine(*_args, **_kwargs):
        engine_calls.append(True)
        raise AssertionError("engine must not be created")

    monkeypatch.setattr(sys.modules[__name__], "create_engine", forbidden_engine)
    generator = _postgres_integration_session(_FakePytestConfig(path))
    with pytest.raises(AssertionError, match="does not match the explicitly authorized") as exc:
        next(generator)

    assert exc.value.__cause__ is None
    assert "fake-url-secret" not in str(exc.value)
    assert engine_calls == []
    assert "fake-url-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("identity_overrides", "show_overrides"),
    [
        ({"database_name": "wrong_database"}, {}),
        ({}, {"default_transaction_read_only": "on"}),
        ({}, {"transaction_read_only": "on"}),
    ],
    ids=["identity", "default-read-only", "transaction-read-only"],
)
def test_postgres_identity_and_readonly_mismatch_precedes_yield(
    monkeypatch, identity_overrides, show_overrides, capsys
) -> None:
    monkeypatch.setenv(
        "GRANDGOUSIER_TEST_DATABASE_URL",
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-guard-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}",
    )
    events, _connection = _install_fake_postgres_fixture(
        monkeypatch,
        identity_overrides=identity_overrides,
        show_overrides=show_overrides,
    )
    generator = _postgres_integration_session(_FakePytestConfig())
    with pytest.raises(AssertionError, match="identity/read-only guard mismatch") as exc:
        next(generator)

    assert "fake-guard-secret" not in str(exc.value)
    assert "session.create" not in events
    assert events[-3:] == ["transaction.rollback", "connection.close", "engine.dispose"]
    assert "fake-guard-secret" not in capsys.readouterr().out


def test_postgres_fixture_redacts_connection_errors_and_rolls_back_in_order(
    monkeypatch, capsys
) -> None:
    test_url = (
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-driver-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}"
    )
    monkeypatch.setenv("GRANDGOUSIER_TEST_DATABASE_URL", test_url)
    events, _connection = _install_fake_postgres_fixture(monkeypatch)
    generator = _postgres_integration_session(_FakePytestConfig())
    session = next(generator)
    assert session is not None
    generator.close()
    assert events[-4:] == [
        "session.close",
        "transaction.rollback",
        "connection.close",
        "engine.dispose",
    ]
    output = capsys.readouterr().out
    assert "fake-driver-secret" not in output
    assert f"database={_POSTGRES_IT_DATABASE_NAME!r}" in output
    assert "default_transaction_read_only='off'" in output
    assert "transaction_read_only='off'" in output
    assert "outer transaction rollback completed" in output

    failure_events, _connection = _install_fake_postgres_fixture(
        monkeypatch,
        connection_error=RuntimeError(test_url),
    )
    failed_generator = _postgres_integration_session(_FakePytestConfig())
    with pytest.raises(AssertionError, match="connection failed") as exc:
        next(failed_generator)
    assert test_url not in str(exc.value)
    assert "fake-driver-secret" not in str(exc.value)
    assert failure_events[-1] == "engine.dispose"
    assert "fake-driver-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "server_address",
    [
        pytest.param("127.0.0.1", id="ipv4-host-string"),
        pytest.param("127.0.0.1/32", id="ipv4-cidr-string"),
        pytest.param(ipaddress.IPv4Address("127.0.0.1"), id="ipv4-address-object"),
        pytest.param("::1", id="ipv6-host-string"),
        pytest.param("::1/128", id="ipv6-cidr-string"),
        pytest.param(ipaddress.IPv6Address("::1"), id="ipv6-address-object"),
    ],
)
def test_postgres_guard_accepts_loopback_ip_and_cidr_forms(
    monkeypatch, server_address
) -> None:
    monkeypatch.setenv(
        "GRANDGOUSIER_TEST_DATABASE_URL",
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-guard-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}",
    )
    events, _connection = _install_fake_postgres_fixture(
        monkeypatch,
        identity_overrides={"server_address": server_address},
    )
    generator = _postgres_integration_session(_FakePytestConfig())
    session = next(generator)
    assert session is not None
    generator.close()
    assert "probe.identity" in events
    assert events[-4:] == [
        "session.close",
        "transaction.rollback",
        "connection.close",
        "engine.dispose",
    ]


def test_postgres_guard_rejects_non_loopback_address(monkeypatch) -> None:
    monkeypatch.setenv(
        "GRANDGOUSIER_TEST_DATABASE_URL",
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-guard-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/{_POSTGRES_IT_DATABASE_NAME}",
    )
    events, _connection = _install_fake_postgres_fixture(
        monkeypatch,
        identity_overrides={"server_address": "192.0.2.1"},
    )
    generator = _postgres_integration_session(_FakePytestConfig())
    with pytest.raises(AssertionError, match="identity/read-only guard mismatch"):
        next(generator)

    assert "session.create" not in events
    assert events[-3:] == ["transaction.rollback", "connection.close", "engine.dispose"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_stage1_fingerprint(provenance: dict, action: str) -> str:
    observation = provenance["observation"]
    inputs = []
    for entry in provenance["inputs"]:
        ref_data = entry["ref"]
        ref = AssetRef(
            source_id=ref_data["source_id"],
            role=ref_data["role"],
            backend=ref_data["backend"],
            locator=ref_data["locator"],
            checksum_sha256=ref_data.get("checksum_sha256"),
            metadata=ref_data.get("metadata", {}),
        )
        assert ref_data["ref_uid"] == ref.ref_uid
        inputs.append(SimpleNamespace(ref=ref, checksum_sha256=entry["checksum_sha256"]))
    item = SimpleNamespace(
        action=action,
        observation=SimpleNamespace(
            source_id=observation["source_id"],
            native_observation_id=observation["native_observation_id"],
            source_observation_uid=observation["source_observation_uid"],
            observation_uid=observation["observation_uid"],
            identity_scheme=observation["identity_scheme"],
        ),
    )
    payload = _input_fingerprint_payload(
        item,
        inputs,
        recipe=provenance["recipe"]["name"],
        parameters=provenance["recipe"]["parameters"],
    )
    return _fingerprint(payload)


def _write_execution_report(tmp_path: Path, *, native: str = "204_15814") -> tuple[Path, Path]:
    product = tmp_path / "srcobs-test_processed.fits"
    product.write_bytes(b"stage1-product")
    checksum = _sha(product)
    obs_uid = make_observation_uid("master_uniqnum_v1", native)
    provenance = product.with_name(product.name + ".provenance.json")
    provenance_payload = {
        "schema": STAGE1_PROVENANCE_SCHEMA,
        "fingerprint": None,
        "observation": {
            "source_id": "master-smoke",
            "native_observation_id": native,
            "source_observation_uid": "srcobs-test",
            "identity_scheme": "master_uniqnum_v1",
            "p4_native_identity": native,
            "observation_uid": obs_uid,
        },
        "recipe": {"name": "FIXTURE_RECIPE", "parameters": {}},
        "inputs": [
            {
                "ref": AssetRef(
                    source_id="master-smoke",
                    role="science_raw",
                    backend="fixture",
                    locator=f"fixture://raw/{native}",
                ).to_dict(),
                "checksum_sha256": "b" * 64,
                "size_bytes": 1,
            }
        ],
        "product": {
            "store_id": "stage1-test",
            "backend": "filesystem",
            "locator": f"MASTER/2018/09/07/{product.name}",
            "checksum_sha256": checksum,
            "size_bytes": product.stat().st_size,
        },
    }
    fingerprint = _fixture_stage1_fingerprint(provenance_payload, "REDUCE_RAW")
    provenance_payload["fingerprint"] = fingerprint
    report = tmp_path / "execution.json"
    row = {
        "native_observation_id": native,
        "source_observation_uid": "srcobs-test",
        "observation_uid": obs_uid,
        "action": "REDUCE_RAW",
        "status": "SKIPPED_EXISTING",
        "product": {
            "store_id": "stage1-test",
            "backend": "filesystem",
            "locator": f"MASTER/2018/09/07/{product.name}",
        },
        "product_path": str(product),
        "product_checksum_sha256": checksum,
        "provenance_path": str(provenance),
        "fingerprint": fingerprint,
    }
    provenance.write_text(json.dumps(provenance_payload), encoding="utf-8")
    report.write_text(
        json.dumps(
            {
                "schema": STAGE1_EXECUTION_SCHEMA,
                "status": "READY",
                "source_id": "master-smoke",
                "items": [row],
            }
        ),
        encoding="utf-8",
    )
    return report, product


def _write_physical_master_execution_report(
    tmp_path: Path,
    *,
    native: str = "204_15814",
    raw_key: str = "rawdata/2018/09/07/375506.fit.gz",
    date_obs: datetime = datetime(2018, 9, 7, 17, 4, 22, tzinfo=UTC),
    camera: str = "204",
) -> tuple[Path, Path]:
    report, product = _write_execution_report(tmp_path, native=native)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    provenance["observation"].update(
        {
            "identity_scheme": "master_uniqnum_v1",
            "p4_native_identity": native,
            "instrument": "MASTER",
            "camera": camera,
            "observed_at": date_obs.isoformat(),
        }
    )
    provenance["inputs"] = [
        {
            "ref": AssetRef(
                source_id="master-smoke",
                role="science_raw",
                backend="master-physical",
                locator=f"/master/imdata/{raw_key}",
                metadata={"raw_relative_physical_key": raw_key},
            ).to_dict(),
            "checksum_sha256": "b" * 64,
            "size_bytes": 1,
        },
        *[
            {
                "ref": AssetRef(
                    source_id="master-smoke",
                    role=role,
                    backend="fixture",
                    locator=f"fixture://{role}/{native}",
                ).to_dict(),
                "checksum_sha256": checksum,
                "size_bytes": 1,
            }
            for role, checksum in (
                ("processing_header", "c" * 64),
                ("dark", "d" * 64),
                ("flat", "e" * 64),
            )
        ],
    ]
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    fingerprint = _fixture_stage1_fingerprint(
        provenance, report_payload["items"][0]["action"]
    )
    provenance["fingerprint"] = fingerprint
    report_payload["items"][0]["fingerprint"] = fingerprint
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    return report, product


def _observation(
    session: Session,
    native: str,
    *,
    instrument: str = "MASTER",
    camera: str = "204",
    date_obs: datetime = datetime(2018, 9, 7, 17, 4, 22, tzinfo=timezone.utc),
) -> Observation:
    scheme = "master_uniqnum_v1"
    row = Observation(
        observation_uid=make_observation_uid(scheme, native),
        instrument=instrument,
        identity_scheme=scheme,
        native_identity=native,
        camera_identity=camera,
        observed_at=date_obs,
        identity_components={"UNIQNUM": native},
    )
    session.add(row)
    session.flush()
    return row


def _frame(
    session: Session,
    observation: Observation,
    *,
    uid: str = "existing-frame",
    instrument: str = "MASTER",
    camera: str | None = None,
    date_obs: datetime | None = None,
    raw_relative_physical_key: str | None = None,
) -> Frame:
    header = {"UNIQNUM": observation.native_identity}
    if raw_relative_physical_key is not None:
        header["raw_relative_physical_key"] = raw_relative_physical_key
    frame = Frame(
        frame_uid=uid,
        observation_id=observation.observation_id,
        raw_path=f"C:/historical/{uid}.fits",
        instrument=instrument,
        camera=camera if camera is not None else observation.camera_identity,
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=date_obs or observation.observed_at,
        exptime=50.0,
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json=header,
    )
    session.add(frame)
    session.flush()
    return frame


def _make_series_test_frames(
    session: Session,
    *,
    prefix: str,
    count: int,
    start: datetime = datetime(2018, 9, 7, 17, 0, tzinfo=UTC),
    start_mjd: float = 58370.0,
) -> list[Frame]:
    frames = []
    for index in range(count):
        observed_at = start + timedelta(minutes=index)
        native = f"204_{prefix}_{index:03d}"
        observation = _observation(
            session,
            native,
            date_obs=observed_at,
        )
        frame = _frame(
            session,
            observation,
            uid=f"{prefix}-frame-{index:03d}",
            date_obs=observed_at,
        )
        frame.ra_center_deg = 327.52
        frame.dec_center_deg = 72.18
        frame.mjd = start_mjd + (index / 1440.0)
        session.flush()
        frames.append(frame)
    return frames


def _run_archive_pipeline_spy(monkeypatch, db_session: Session, *, status: str) -> list[int]:
    from grandgousier import cli
    from grandgousier.pipeline import runner

    class SessionContext:
        def __enter__(self):
            return db_session

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli, "make_session_factory", lambda *_args: lambda: SessionContext())
    monkeypatch.setattr(cli, "_build_pipeline_command_snapshot", dict)
    monkeypatch.setattr(cli, "_build_pipeline_environment_snapshot", lambda *_args: {})
    calls = []

    def pipeline_spy(_session, config):
        calls.append(int(config.series_id))
        raise RuntimeError("Stage-2 spy; pipeline execution intentionally disabled")

    monkeypatch.setattr(runner, "run_field_pipeline", pipeline_spy)
    args = cli.build_parser().parse_args(
        [
            "run-archive-pipeline",
            "--catalog-name",
            "test-catalog",
            "--status",
            status,
            "--format",
            "json",
        ]
    )
    assert cli.cmd_run_archive_pipeline(args) == (1 if calls else 0)
    return calls


def _assign(
    session: Session,
    frame: Frame,
    *,
    field: Field | None = None,
    series_uid: str = "handoff-series",
) -> FieldSeries:
    if field is None:
        field = Field(
            field_uid="handoff-field",
            field_name="handoff",
            ra_center_deg=327.5,
            dec_center_deg=72.1,
            radius_arcmin=30.0,
            metadata_json={},
        )
        session.add(field)
        session.flush()
    series = FieldSeries(
        field_id=field.field_id,
        series_uid=series_uid,
        filter_name="V",
        n_frames=1,
        grouping_method="test",
        metadata_json={},
    )
    session.add(series)
    session.flush()
    session.add(FieldSeriesFrame(series_id=series.series_id, frame_id=frame.frame_id, order_index=1))
    session.flush()
    return series


def _install_fake_auto_process(
    monkeypatch,
    product: Path,
    *,
    native: str,
    frame_uid: str,
    process_call,
    index_call=None,
    date_obs: datetime | None = datetime(2018, 9, 7, 17, 4, 22, tzinfo=timezone.utc),
    camera: str | None = "204",
    instrument: str = "MASTER",
):
    obs_uid = make_observation_uid("master_uniqnum_v1", native)
    detected = SimpleNamespace(
        path=str(product),
        instrument=instrument,
        camera=camera,
        frame_uid=frame_uid,
        observation_uid=obs_uid,
        identity_scheme="master_uniqnum_v1",
        native_identity=native,
        date_obs=date_obs,
        observation_identity_status="READY",
        observation_identity_detail=None,
    )

    def scan_archive_paths(paths, **_kwargs):
        assert [Path(value) for value in paths] == [product]
        return [detected], [], []

    fake = types.ModuleType("grandgousier.archive.auto_process")
    fake.scan_archive_paths = scan_archive_paths
    fake.process_archive_path = process_call
    fake.index_detected_archive_files = index_call or (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity indexer was not configured for this test")
        )
    )
    monkeypatch.setitem(sys.modules, "grandgousier.archive.auto_process", fake)


def _install_successful_physical_handoff(
    monkeypatch,
    product: Path,
    *,
    native: str,
    date_obs: datetime,
    camera: str,
    frame_uid: str,
):
    calls = []

    def fake_process(session, **kwargs):
        calls.append(kwargs)
        observation = _observation(session, native, camera=camera, date_obs=date_obs)
        frame = _frame(
            session,
            observation,
            uid=frame_uid,
            camera=camera,
            date_obs=date_obs,
        )
        frame.raw_path = str(product)
        session.flush()
        series_id = _assign(session, frame, series_uid=f"series-{frame_uid}").series_id
        return SimpleNamespace(
            to_dict=lambda: {
                "execute": True,
                "status": "DONE",
                "warnings": [],
                "discovered_files": 1,
                "detected_files": 1,
                "failed_files": 0,
                "index_results": [{"discovered": 1, "failed": 0}],
                "indexed_frame_ids": [frame.frame_id],
                "observation_selection_results": [
                    {
                        "mode": "EXECUTE_PERSISTED_FRAMES",
                        "rows": [{"action": "SELECTED", "selected_frame_id": frame.frame_id}],
                    }
                ],
                "created_series_ids": [series_id],
                "processed_series_ids": [series_id],
                "pipeline_results": [
                    {"series_id": series_id, "status": "DONE", "science_status": "OK"}
                ],
            }
        )

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid=frame_uid,
        process_call=fake_process,
        date_obs=date_obs,
        camera=camera,
    )
    return calls


def _install_identity_only_indexer(
    monkeypatch,
    product: Path,
    *,
    native: str,
    date_obs: datetime,
    camera: str,
    frame_uid: str,
):
    index_calls = []
    stage2_calls = []

    def forbidden_process(*_args, **_kwargs):
        stage2_calls.append(True)
        raise AssertionError("persistence-only handoff must not invoke Stage 2")

    def fake_index(session, detected, **kwargs):
        detected_items = list(detected)
        index_calls.append(([item.path for item in detected_items], kwargs))
        frame_ids = []
        for item in detected_items:
            observation = _observation(
                session,
                item.native_identity,
                camera=item.camera,
                date_obs=item.date_obs,
            )
            frame = _frame(
                session,
                observation,
                uid=frame_uid,
                camera=item.camera,
                date_obs=item.date_obs,
            )
            frame.raw_path = item.path
            session.flush()
            frame_ids.append(frame.frame_id)
        session.commit()
        return SimpleNamespace(
            index_results=(
                {
                    "profile_name": "MASTER",
                    "discovered": len(detected_items),
                    "inserted": len(detected_items),
                    "skipped_existing": 0,
                    "failed": 0,
                },
            ),
            indexed_paths=tuple(item.path for item in detected_items),
            indexed_frame_ids=tuple(frame_ids),
            failed_files=0,
        )

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid=frame_uid,
        process_call=forbidden_process,
        index_call=fake_index,
        date_obs=date_obs,
        camera=camera,
    )
    return index_calls, stage2_calls


def test_execution_report_validation_rejects_changed_product(tmp_path: Path) -> None:
    report, product = _write_execution_report(tmp_path)
    product.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_validated_stage1_products(report)


@pytest.mark.parametrize("value", [None, "invalid", "b" * 64])
def test_fingerprint_missing_invalid_or_mismatched_blocks_before_mutation(
    db_session: Session, tmp_path: Path, value
) -> None:
    report, product = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0]["fingerprint"] = value
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0
    assert product.is_file()


def test_physical_raw_key_and_locator_tampering_with_stale_uid_and_fingerprint_blocks(
    tmp_path: Path,
) -> None:
    report, product = _write_physical_master_execution_report(tmp_path)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    old_ref_uid = provenance["inputs"][0]["ref"]["ref_uid"]
    old_fingerprint = provenance["fingerprint"]
    tampered_key = "rawdata/2018/09/08/375507.fit.gz"
    provenance["inputs"][0]["ref"]["locator"] = f"/master/imdata/{tampered_key}"
    provenance["inputs"][0]["ref"]["metadata"]["raw_relative_physical_key"] = tampered_key
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")

    assert provenance["inputs"][0]["ref"]["ref_uid"] == old_ref_uid
    assert provenance["fingerprint"] == old_fingerprint
    with pytest.raises(ValueError, match="ref_uid"):
        load_validated_stage1_products(report)


def test_removed_physical_raw_input_does_not_downgrade_to_generic_provenance(
    tmp_path: Path,
) -> None:
    report, product = _write_physical_master_execution_report(tmp_path)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    provenance["inputs"] = [
        entry for entry in provenance["inputs"] if entry["ref"]["role"] != "science_raw"
    ]
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(ValueError, match="fingerprint"):
        load_validated_stage1_products(report)


def test_physical_marker_on_non_science_input_fails_closed(tmp_path: Path) -> None:
    report, product = _write_physical_master_execution_report(tmp_path)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    raw_input = provenance["inputs"][0]
    old_ref = raw_input["ref"]
    raw_input["ref"] = AssetRef(
        source_id=old_ref["source_id"],
        role="raw_auxiliary",
        backend=old_ref["backend"],
        locator=old_ref["locator"],
        metadata=old_ref["metadata"],
    ).to_dict()
    payload = json.loads(report.read_text(encoding="utf-8"))
    fingerprint = _fixture_stage1_fingerprint(
        provenance, payload["items"][0]["action"]
    )
    provenance["fingerprint"] = fingerprint
    payload["items"][0]["fingerprint"] = fingerprint
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one science_raw ref"):
        load_validated_stage1_products(report)


@pytest.mark.parametrize("field", ["native_observation_id", "source_observation_uid", "observation_uid"])
def test_incomplete_observation_evidence_blocks_before_mutation(
    db_session: Session, tmp_path: Path, field: str
) -> None:
    report, _ = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0][field] = None
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0


def test_conflicting_existing_fingerprint_blocks_before_stage2(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_CONFLICT"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    frame = _frame(db_session, obs, uid="new-product-frame-uid")
    frame.raw_path = str(product)
    frame.header_json = {"stage1_fingerprint": "b" * 64}
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("conflict must be found before Stage 2")

    _install_fake_auto_process(monkeypatch, product, native=native,
                               frame_uid="new-product-frame-uid", process_call=forbidden)
    with pytest.raises(ValueError, match="fingerprint conflict"):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    db_session.refresh(frame)
    assert frame.header_json["stage1_fingerprint"] == "b" * 64


def test_missing_second_frame_cannot_partially_attach_evidence(
    db_session: Session, tmp_path: Path
) -> None:
    report, product = _write_execution_report(tmp_path)
    valid = load_validated_stage1_products(report)[0]
    obs = _observation(db_session, valid.native_observation_id)
    frame = _frame(db_session, obs)
    frame.raw_path = str(product)
    db_session.commit()
    first = Stage1HandoffItem(valid, "SELECTED", "SELECTED_FOR_STAGE2", frame.frame_id, (), "")
    second = Stage1HandoffItem(
        replace(valid, product_path=tmp_path / "not-indexed.fits"),
        "SELECTED", "SELECTED_FOR_STAGE2", None, (), "",
    )
    with pytest.raises(ValueError, match="no unique exact Frame location"):
        _persist_selected_evidence(db_session, [first, second], [frame.frame_id])
    db_session.rollback()
    db_session.refresh(frame)
    assert "stage1_fingerprint" not in frame.header_json
    assert next(loc for loc in frame.locations if loc.locator == str(product)).content_sha256 is None


def test_existing_frame_source_key_is_validated_before_evidence_update(
    db_session: Session, tmp_path: Path
) -> None:
    native = "204_EXISTING_SOURCE_KEY"
    product_date = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    expected_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product_path = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=expected_key, date_obs=product_date
    )
    product = load_validated_stage1_products(report)[0]
    observation = _observation(db_session, native, date_obs=product_date)
    existing_key = "rawdata/2018/09/07/375507.fit.gz"
    frame = _frame(
        db_session,
        observation,
        uid="existing-source-key-frame",
        date_obs=product_date,
        raw_relative_physical_key=existing_key,
    )
    frame.raw_path = str(product_path)
    db_session.commit()
    item = Stage1HandoffItem(
        product=product,
        p4_action="SELECTED",
        status="SELECTED_FOR_STAGE2",
        selected_frame_id=frame.frame_id,
        existing_series_ids=(),
        reason="test persisted-value validation",
    )

    with pytest.raises(ValueError, match="source key conflict"):
        _persist_selected_evidence(db_session, [item], [frame.frame_id])

    db_session.rollback()
    db_session.refresh(frame)
    assert frame.header_json["raw_relative_physical_key"] == existing_key
    assert "stage1_fingerprint" not in frame.header_json


def test_already_represented_handoff_does_not_invoke_stage2_or_insert_frame(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_15814"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    existing = _frame(db_session, obs)
    series = _assign(db_session, existing)
    before_frames = db_session.scalar(select(func.count(Frame.frame_id)))
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("Stage 2 must not run for an already represented physical observation")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-product-frame-uid",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "READY"
    assert result.selected_items == 0
    assert result.retained_items == 1
    assert result.stage2_result is None
    assert result.items[0].p4_action == "ALREADY_REPRESENTED"
    assert result.items[0].existing_series_ids == (series.series_id,)
    assert called["process"] == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == before_frames


def test_master_date_collision_handoff_blocks_before_stage2_or_frame_writes(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_DATE_COLLISION"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    _frame(db_session, obs)
    db_session.commit()
    before_frames = db_session.scalar(select(func.count(Frame.frame_id)))
    calls = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        calls["process"] += 1
        raise AssertionError("date-colliding MASTER product must not reach Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-date-product",
        process_call=forbidden_process,
        date_obs=datetime(2018, 9, 8, 17, 4, 22, tzinfo=timezone.utc),
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True
    )

    assert result.status == "BLOCKED"
    assert result.blocked_items == 1
    assert result.items[0].p4_action == (
        "PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM"
    )
    assert calls["process"] == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == before_frames


def test_new_physical_master_handoff_keeps_target_optional_and_persists_source_key(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PHYSICAL_NEW"
    date_obs = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    source_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=source_key, date_obs=date_obs
    )
    process_calls = _install_successful_physical_handoff(
        monkeypatch,
        product,
        native=native,
        date_obs=date_obs,
        camera="204",
        frame_uid="new-physical-frame",
    )

    # No target is supplied: the existing target-optional P4/handoff path remains valid.
    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True, min_frames_per_series=1
    )

    assert result.status == "READY"
    assert result.selected_items == 1
    assert result.stage2_result is not None
    assert len(process_calls) == 1
    assert process_calls[0]["exact_paths"] == [product]
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "new-physical-frame"))
    assert frame is not None
    assert frame.header_json["raw_relative_physical_key"] == source_key
    assert "raw_data_id" not in frame.header_json
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 1
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 1


def test_duplicate_physical_master_handoff_reuses_exact_state_idempotently(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PHYSICAL_DUPLICATE"
    date_obs = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    source_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=source_key, date_obs=date_obs
    )
    process_calls = _install_successful_physical_handoff(
        monkeypatch,
        product,
        native=native,
        date_obs=date_obs,
        camera="204",
        frame_uid="duplicate-seed-frame",
    )

    first = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True, min_frames_per_series=1
    )
    assert first.status == "READY"
    assert first.selected_items == 1
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "duplicate-seed-frame"))
    assert frame is not None
    assert frame.header_json["raw_relative_physical_key"] == source_key
    before = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(frame.header_json),
    )

    # Explicitly replay the same execution report after the successful mocked index path.
    second = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True, min_frames_per_series=1
    )

    assert second.status == "READY"
    assert second.retained_items == 1
    assert second.selected_items == 0
    assert second.stage2_result is None
    assert second.items[0].p4_action == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert len(process_calls) == 1
    after = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(frame.header_json),
    )
    assert after == before
    assert after[3]["raw_relative_physical_key"] == source_key


def test_persist_only_new_master_creates_identity_and_evidence_without_stage2(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PERSIST_ONLY_NEW"
    date_obs = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    source_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=source_key, date_obs=date_obs
    )
    index_calls, stage2_calls = _install_identity_only_indexer(
        monkeypatch,
        product,
        native=native,
        date_obs=date_obs,
        camera="204",
        frame_uid="persist-only-new-frame",
    )

    # Target is intentionally omitted; the fake indexer writes only to this in-memory fixture.
    result = handoff_stage1_execution(
        db_session, execution_report_path=report, persist_only=True
    )

    assert result.status == "READY"
    assert result.persistence_only is True
    assert result.stage2_result is None
    payload = result.to_dict()
    assert payload["stage2_status"] == "NOT_INVOKED"
    assert payload["summary"]["stage2_invoked"] is False
    assert payload["summary"]["persisted_identity_only"] == 1
    assert result.items[0].status == "PERSISTED_IDENTITY_ONLY"
    assert len(index_calls) == 1
    assert index_calls[0][0] == [str(product)]
    assert stage2_calls == []

    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "persist-only-new-frame"))
    assert frame is not None
    assert frame.header_json["raw_relative_physical_key"] == source_key
    assert frame.header_json["stage1_fingerprint"] == load_validated_stage1_products(report)[0].fingerprint
    assert frame.header_json["source_observation_uid"] == "srcobs-test"
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 1
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 1
    assert db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))) == 0


def test_persist_only_replay_reuses_unassigned_physical_frame_idempotently(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PERSIST_ONLY_DUPLICATE"
    date_obs = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    source_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=source_key, date_obs=date_obs
    )
    index_calls, stage2_calls = _install_identity_only_indexer(
        monkeypatch,
        product,
        native=native,
        date_obs=date_obs,
        camera="204",
        frame_uid="persist-only-duplicate-frame",
    )

    first = handoff_stage1_execution(db_session, execution_report_path=report, persist_only=True)
    frame = db_session.scalar(
        select(Frame).where(Frame.frame_uid == "persist-only-duplicate-frame")
    )
    assert first.persisted_identity_only_items == 1
    assert frame is not None
    before = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(frame.header_json),
    )

    second = handoff_stage1_execution(db_session, execution_report_path=report, persist_only=True)

    assert second.status == "READY"
    assert second.items[0].p4_action == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert second.items[0].status == "ALREADY_REPRESENTED"
    assert second.persistence_result["status"] == "NOOP_ALREADY_REPRESENTED"
    assert len(index_calls) == 1
    assert stage2_calls == []
    after = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(frame.header_json),
    )
    assert after == before


def test_persist_only_master_collision_blocks_without_index_or_mutation(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PERSIST_ONLY_COLLISION"
    old_date = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    new_date = datetime(2018, 9, 8, 17, 4, 22, 902000, tzinfo=UTC)
    old_key = "rawdata/2018/09/07/375506.fit.gz"
    new_key = "rawdata/2018/09/08/375507.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=new_key, date_obs=new_date
    )
    observation = _observation(db_session, native, camera="204", date_obs=old_date)
    frame = _frame(
        db_session,
        observation,
        uid="persist-only-collision-existing",
        date_obs=old_date,
        raw_relative_physical_key=old_key,
    )
    db_session.commit()
    index_calls, stage2_calls = _install_identity_only_indexer(
        monkeypatch,
        product,
        native=native,
        date_obs=new_date,
        camera="204",
        frame_uid="persist-only-collision-incoming",
    )
    before = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        dict(frame.header_json),
    )

    result = handoff_stage1_execution(db_session, execution_report_path=report, persist_only=True)

    assert result.status == "BLOCKED"
    assert result.items[0].p4_action == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )
    assert index_calls == []
    assert stage2_calls == []
    after = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        dict(frame.header_json),
    )
    assert after == before


@pytest.mark.parametrize(
    ("persisted_observation_instrument", "persisted_frame_instrument"),
    [("RoboPhot", "MASTER"), ("MASTER", "RoboPhot")],
)
def test_persist_only_physical_instrument_mismatch_blocks_without_mutation(
    db_session: Session,
    tmp_path: Path,
    monkeypatch,
    persisted_observation_instrument: str,
    persisted_frame_instrument: str,
) -> None:
    native = "204_PERSIST_ONLY_INSTRUMENT_CONFLICT"
    date_obs = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    source_key = "rawdata/2018/09/07/375506.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=source_key, date_obs=date_obs
    )
    observation = _observation(
        db_session,
        native,
        instrument=persisted_observation_instrument,
        camera="204",
        date_obs=date_obs,
    )
    frame = _frame(
        db_session,
        observation,
        uid="persist-only-instrument-conflict-frame",
        instrument=persisted_frame_instrument,
        camera="204",
        date_obs=date_obs,
        raw_relative_physical_key=source_key,
    )
    db_session.commit()
    index_calls, stage2_calls = _install_identity_only_indexer(
        monkeypatch,
        product,
        native=native,
        date_obs=date_obs,
        camera="204",
        frame_uid="persist-only-instrument-conflict-incoming",
    )
    before = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        observation.instrument,
        frame.instrument,
        dict(frame.header_json),
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, persist_only=True
    )

    assert result.status == "BLOCKED"
    assert result.items[0].p4_action == ObservationScienceAction.IDENTITY_CONFLICT.value
    assert index_calls == []
    assert stage2_calls == []
    after = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        observation.instrument,
        frame.instrument,
        dict(frame.header_json),
    )
    assert after == before


def test_physical_master_collision_date_and_source_key_blocks_without_mutation(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_PHYSICAL_COLLISION"
    old_date = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    new_date = datetime(2018, 9, 8, 17, 4, 22, 902000, tzinfo=UTC)
    old_key = "rawdata/2018/09/07/375506.fit.gz"
    new_key = "rawdata/2018/09/08/375507.fit.gz"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, raw_key=new_key, date_obs=new_date
    )
    observation = _observation(db_session, native, camera="204", date_obs=old_date)
    existing = _frame(
        db_session,
        observation,
        uid="physical-collision-existing",
        camera="204",
        date_obs=old_date,
        raw_relative_physical_key=old_key,
    )
    _assign(db_session, existing)
    db_session.commit()
    before = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(existing.header_json),
    )
    process_calls = {"count": 0}

    def forbidden_process(*_args, **_kwargs):
        process_calls["count"] += 1
        raise AssertionError("physical MASTER collision must not invoke nested processing")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="colliding-input-frame",
        process_call=forbidden_process,
        date_obs=new_date,
    )
    # The fake FITS context must agree with the provenance context, so P4 sees the
    # persisted-observation collision rather than a provenance/FITS mismatch.
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "BLOCKED"
    assert result.blocked_items == 1
    assert result.items[0].p4_action == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )
    assert process_calls["count"] == 0
    after = (
        db_session.scalar(select(func.count(Observation.observation_id))),
        db_session.scalar(select(func.count(Frame.frame_id))),
        db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))),
        dict(existing.header_json),
    )
    assert after == before


@pytest.mark.parametrize(
    ("instrument", "camera", "detected_date", "message"),
    [
        ("RoboPhot", "204", datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC), "instrument mismatch"),
        ("MASTER", "203", datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC), "camera mismatch"),
        ("MASTER", "204", datetime(2018, 9, 8, 17, 4, 22, 902000, tzinfo=UTC), "DATE-OBS mismatch"),
    ],
)
def test_physical_provenance_context_must_match_detected_product(
    db_session: Session, tmp_path: Path, monkeypatch, instrument, camera, detected_date, message
) -> None:
    native = "204_PROVENANCE_CONTEXT"
    expected_date = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, date_obs=expected_date
    )
    calls = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        calls["process"] += 1
        raise AssertionError("provenance/FITS context mismatch must stop before P4 processing")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="provenance-context-frame",
        process_call=forbidden_process,
        date_obs=detected_date,
        camera=camera,
        instrument=instrument,
    )

    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.errors
    assert message in result.errors[0]
    assert calls["process"] == 0
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0


@pytest.mark.parametrize(
    ("provenance_date", "detected_date"),
    [
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 22, tzinfo=UTC),
            datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC),
            id="master-whole-second",
        ),
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC),
            datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC),
            id="exact-timestamp",
        ),
    ],
)
def test_master_date_obs_precision_passes_p4_preflight(
    db_session: Session,
    tmp_path: Path,
    monkeypatch,
    provenance_date: datetime,
    detected_date: datetime,
) -> None:
    native = "204_MASTER_DATE_PRECISION"
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, date_obs=provenance_date
    )
    process_calls = []

    def forbidden_process(*_args, **_kwargs):
        process_calls.append(True)
        raise AssertionError("P4 preflight must not invoke Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="master-date-precision-frame",
        process_call=forbidden_process,
        date_obs=detected_date,
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=False
    )

    assert result.status == "READY"
    assert result.errors == ()
    assert result.selected_items == 1
    assert result.items[0].p4_action == ObservationScienceAction.SELECTED.value
    assert result.items[0].status == "SELECTED_FOR_STAGE2"
    assert process_calls == []


def test_master_date_obs_adjacent_seconds_do_not_match_with_subsecond_delta(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_MASTER_DATE_ADJACENT_SECONDS"
    provenance_date = datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=UTC)
    detected_date = datetime(2018, 9, 7, 17, 4, 23, 1000, tzinfo=UTC)
    report, product = _write_physical_master_execution_report(
        tmp_path, native=native, date_obs=provenance_date
    )
    process_calls = []

    def forbidden_process(*_args, **_kwargs):
        process_calls.append(True)
        raise AssertionError("DATE-OBS mismatch must stop before Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="master-adjacent-seconds-frame",
        process_call=forbidden_process,
        date_obs=detected_date,
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True
    )

    assert result.status == "FAILED"
    assert len(result.errors) == 1
    assert "DATE-OBS mismatch" in result.errors[0]
    assert f"provenance={provenance_date.isoformat()}" in result.errors[0]
    assert f"FITS={detected_date}" in result.errors[0]
    assert process_calls == []
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0


def test_non_master_date_obs_still_requires_exact_timestamp(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_NON_MASTER_DATE_PRECISION"
    provenance_date = datetime(2018, 9, 7, 17, 4, 22, tzinfo=UTC)
    detected_date = datetime(2018, 9, 7, 17, 4, 22, 902000, tzinfo=UTC)
    report, product = _write_execution_report(tmp_path, native=native)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    provenance["observation"].update(
        {
            "instrument": "RoboPhot",
            "camera": "204",
            "observed_at": provenance_date.isoformat(),
        }
    )
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")
    process_calls = []

    def forbidden_process(*_args, **_kwargs):
        process_calls.append(True)
        raise AssertionError("DATE-OBS mismatch must stop before Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="non-master-date-precision-frame",
        process_call=forbidden_process,
        date_obs=detected_date,
        instrument="RoboPhot",
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True
    )

    assert result.status == "FAILED"
    assert len(result.errors) == 1
    assert "DATE-OBS mismatch" in result.errors[0]
    assert f"provenance={provenance_date.isoformat()}" in result.errors[0]
    assert f"FITS={detected_date}" in result.errors[0]
    assert process_calls == []
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0


def test_master_date_obs_missing_on_both_sides_does_not_create_date_mismatch(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_MASTER_DATE_MISSING"
    report, product = _write_execution_report(tmp_path, native=native)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    provenance["observation"].update({"instrument": "MASTER", "camera": "204"})
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")
    process_calls = []

    def forbidden_process(*_args, **_kwargs):
        process_calls.append(True)
        raise AssertionError("P4 preflight must not invoke Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="master-date-missing-frame",
        process_call=forbidden_process,
        date_obs=None,
    )

    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=False
    )

    assert result.status == "BLOCKED"
    assert result.errors == ()
    assert len(result.items) == 1
    assert result.items[0].status == "BLOCKED"
    assert process_calls == []


@pytest.mark.parametrize("raw_key", ["C:/rawdata/2018/09/07/375506.fit.gz", "rawdata/2018/02/30/375506.fit.gz"])
def test_physical_master_provenance_rejects_noncanonical_source_key(
    tmp_path: Path, raw_key: str
) -> None:
    report, _product = _write_physical_master_execution_report(tmp_path, raw_key=raw_key)

    with pytest.raises(ValueError, match="raw source key is invalid"):
        load_validated_stage1_products(report)


def test_physical_master_provenance_rejects_source_key_locator_mismatch(tmp_path: Path) -> None:
    report, product = _write_physical_master_execution_report(tmp_path)
    sidecar = product.with_name(product.name + ".provenance.json")
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    old_ref = provenance["inputs"][0]["ref"]
    provenance["inputs"][0]["ref"] = AssetRef(
        source_id=old_ref["source_id"],
        role=old_ref["role"],
        backend=old_ref["backend"],
        locator="/master/imdata/rawdata/2018/09/07/375507.fit.gz",
        metadata=old_ref["metadata"],
    ).to_dict()
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    fingerprint = _fixture_stage1_fingerprint(
        provenance, report_payload["items"][0]["action"]
    )
    provenance["fingerprint"] = fingerprint
    report_payload["items"][0]["fingerprint"] = fingerprint
    sidecar.write_text(json.dumps(provenance), encoding="utf-8")
    report.write_text(json.dumps(report_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="raw key/locator mismatch"):
        load_validated_stage1_products(report)



def test_historical_multiple_representatives_are_safe_handoff_noop(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_DOUBLE_HANDOFF"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    first = _frame(db_session, obs, uid="historical-a")
    second = _frame(db_session, obs, uid="historical-b")
    first_series = _assign(db_session, first, series_uid="handoff-series-a")
    field = db_session.get(Field, first_series.field_id)
    assert field is not None
    second_series = _assign(db_session, second, field=field, series_uid="handoff-series-b")
    before_frames = db_session.scalar(select(func.count(Frame.frame_id)))
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("historical multiple representatives must remain a handoff no-op")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-product-frame-uid",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "READY"
    assert result.selected_items == 0
    assert result.retained_items == 1
    assert result.blocked_items == 0
    assert result.stage2_result is None
    assert result.items[0].status == "ALREADY_REPRESENTED"
    assert result.items[0].p4_action == "EXISTING_MULTIPLE_REPRESENTATIVES"
    assert result.items[0].selected_frame_id is None
    assert result.items[0].existing_series_ids == tuple(sorted((first_series.series_id, second_series.series_id)))
    assert called["process"] == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == before_frames

@pytest.mark.parametrize("threshold", [None, 20])
def test_new_observation_invokes_process_archive_path_with_exact_product_only(
    db_session: Session, tmp_path: Path, monkeypatch, threshold
) -> None:
    native = "204_NEW"
    report, product = _write_execution_report(tmp_path, native=native)
    captured = {}
    unrelated = _frame(db_session, _observation(db_session, "204_UNRELATED"), uid="unrelated")
    field = db_session.get(Field, _assign(db_session, unrelated).field_id)
    db_session.commit()

    class FakeProcessResult:
        def __init__(self, frame_id):
            self.frame_id = frame_id

        def to_dict(self):
            return {
                "status": "DONE",
                "execute": True,
                "discovered_files": 1,
                "detected_files": 1,
                "failed_files": 0,
                "warnings": [],
                "indexed_frame_ids": [self.frame_id],
                "index_results": [{"discovered": 1, "failed": 0}],
                "observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": [
                    {"action": "SELECTED", "selected_frame_id": self.frame_id},
                ]}],
                "created_series_ids": [series_id],
                "processed_series_ids": [series_id],
                "pipeline_results": [{"series_id": series_id, "status": "DONE", "science_status": "OK"}],
            }

    series_id = None

    def fake_process(_session, **kwargs):
        nonlocal series_id
        captured.update(kwargs)
        obs = _observation(_session, native)
        frame = _frame(_session, obs, uid="new-observation-product")
        frame.raw_path = str(product)
        _session.flush()
        series_id = _assign(_session, frame, field=field, series_uid="selected-series").series_id
        return FakeProcessResult(frame.frame_id)

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-observation-product",
        process_call=fake_process,
    )
    kwargs = {} if threshold is None else {"ensemble_min_ref_stars_per_frame": threshold}
    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True,
        catalog_name="existing-catalog", instrument_profile="MASTER", **kwargs,
    )

    assert result.status == "READY"
    assert result.selected_items == 1
    assert result.retained_items == 0
    assert result.stage2_result["status"] == "DONE"
    assert result.stage2_result["processed_series_ids"] == [series_id]
    assert series_id != 1
    assert captured["exact_paths"] == [product]
    assert captured["recursive"] is False
    assert captured["catalog_name"] == "existing-catalog"
    assert captured["execute"] is True
    assert captured["ensemble_min_ref_stars_per_frame"] == (30 if threshold is None else 20)
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "new-observation-product"))
    fingerprint = load_validated_stage1_products(report)[0].fingerprint
    assert frame.header_json["stage1_fingerprint"] == fingerprint
    location = next(loc for loc in frame.locations if loc.locator == str(product))
    assert location.content_sha256 == _sha(product)
    reclaim = _validate_product(frame, location, tmp_path)
    assert reclaim["fingerprint_status"] == "VALID"
    assert reclaim["reason_code"] is None

    # A replay of the same physical observation does not attach evidence to
    # another Frame and cannot alter the already committed fingerprint.
    second = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert second.status == "READY"
    assert second.retained_items == 1
    assert frame.header_json["stage1_fingerprint"] == fingerprint


def test_ambiguous_new_product_blocks_before_stage2(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_AMBIG"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    _frame(db_session, obs, uid="old-unassigned")
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("blocked handoff must not invoke Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-unassigned",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "BLOCKED"
    assert result.blocked_items == 1
    assert result.items[0].p4_action == "AMBIGUOUS_MULTIPLE_PRODUCTS"
    assert result.stage2_result is None
    assert called["process"] == 0


def test_stage1_handoff_cli_contract() -> None:
    args = build_parser().parse_args(
        [
            "stage1-handoff",
            "--config",
            "grandgousier.yaml",
            "--execution-report",
            "execution.json",
            "--catalog-name",
            "catalog",
        ]
    )
    assert args.command == "stage1-handoff"
    assert args.config_file == "grandgousier.yaml"
    assert args.execution_report == "execution.json"
    assert args.execute is False
    assert args.persist_only is False
    assert args.catalog_name == "catalog"
    assert args.min_frames_per_series == 10
    assert args.ensemble_min_ref_stars_per_frame == 30
    explicit = build_parser().parse_args([
        "stage1-handoff", "--execution-report", "execution.json",
        "--ensemble-min-ref-stars-per-frame", "20",
    ])
    assert explicit.ensemble_min_ref_stars_per_frame == 20
    persistent = build_parser().parse_args(
        ["stage1-handoff", "--execution-report", "execution.json", "--persist-only"]
    )
    assert persistent.persist_only is True
    assert persistent.execute is False
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "stage1-handoff", "--execution-report", "execution.json",
                "--execute", "--persist-only",
            ]
        )


def test_stage1_handoff_cli_forwards_explicit_threshold_without_processing(monkeypatch) -> None:
    from unittest.mock import MagicMock

    import grandgousier.archive.stage1_handoff as handoff_module
    import grandgousier.cli as cli_module

    captured = {}
    session_factory = MagicMock()
    monkeypatch.setattr(cli_module, "make_session_factory", lambda _url: session_factory)

    def fake_handoff(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="READY", to_json=lambda **_kwargs: '{"status":"READY"}',
                               to_dict=lambda: {"status": "READY"},
                               selected_items=0, retained_items=0, blocked_items=0,
                               stage2_result=None)

    monkeypatch.setattr(handoff_module, "handoff_stage1_execution", fake_handoff)
    monkeypatch.setattr(handoff_module, "write_stage1_handoff_json", lambda _report, path: path)
    args = build_parser().parse_args([
        "stage1-handoff", "--execution-report", "execution.json",
        "--ensemble-min-ref-stars-per-frame", "20", "--persist-only", "--format", "json",
    ])
    assert args.func(args) == 0
    assert captured["ensemble_min_ref_stars_per_frame"] == 20
    assert captured["execute"] is False
    assert captured["persist_only"] is True


def test_execution_report_validation_rejects_product_locator_mismatch(tmp_path: Path) -> None:
    report, _product = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0]["product"]["locator"] = "different/product.fits"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="product locator mismatch"):
        load_validated_stage1_products(report)


def test_nested_stage2_exception_is_returned_as_failed_report(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_FAIL"
    report, product = _write_execution_report(tmp_path, native=native)

    def failing_process(*_args, **_kwargs):
        raise RuntimeError("synthetic nested failure")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-observation-product",
        process_call=failing_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "FAILED"
    assert result.selected_items == 1
    assert result.stage2_result is None
    assert result.errors == ("Stage-2 handoff failed: RuntimeError: synthetic nested failure",)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "DONE_WITH_WARNINGS", "warnings": ["science incomplete"]},
        {"status": "FAILED"},
        {"pipeline_results": [], "processed_series_ids": []},
        {"pipeline_results": [{"series_id": 42, "status": "DONE_NO_SCIENCE_FRAMES", "science_status": "BAD_FRAME_QC"}]},
        {"pipeline_results": [{"series_id": 42, "status": "DONE", "science_status": "WARNING_NO_MEASUREMENTS"}]},
        {"index_results": [{"discovered": 1, "failed": 1}]},
        {"indexed_frame_ids": []},
        {"observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": []}]},
        {"created_series_ids": [], "processed_series_ids": [], "pipeline_results": []},
        {"pipeline_results": [{"series_id": 43, "status": "DONE", "science_status": "OK"}]},
        {"_remove_membership": True},
        {"_add_unselected_member": True},
    ],
)
def test_incomplete_nested_stage2_never_attaches_evidence(
    db_session: Session, tmp_path: Path, monkeypatch, change
) -> None:
    native = "204_PARTIAL"
    report, product = _write_execution_report(tmp_path, native=native)

    def fake_process(session, **_kwargs):
        obs = _observation(session, native)
        frame = _frame(session, obs, uid="partial-frame")
        frame.raw_path = str(product)
        series = _assign(session, frame)
        payload = {
            "execute": True, "status": "DONE", "warnings": [],
            "discovered_files": 1, "detected_files": 1, "failed_files": 0,
            "index_results": [{"discovered": 1, "failed": 0}],
            "indexed_frame_ids": [frame.frame_id],
            "observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": [
                {"action": "SELECTED", "selected_frame_id": frame.frame_id},
            ]}],
            "created_series_ids": [series.series_id], "processed_series_ids": [series.series_id],
            "pipeline_results": [{"series_id": series.series_id, "status": "DONE", "science_status": "OK"}],
        }
        if change.get("_remove_membership"):
            membership = session.scalar(select(FieldSeriesFrame).where(
                FieldSeriesFrame.series_id == series.series_id,
                FieldSeriesFrame.frame_id == frame.frame_id,
            ))
            session.delete(membership)
            session.flush()
        if change.get("_add_unselected_member"):
            extra = _frame(session, _observation(session, "204_EXTRA"), uid="extra-frame")
            session.add(FieldSeriesFrame(series_id=series.series_id, frame_id=extra.frame_id,
                                         order_index=2))
            session.flush()
        payload.update({key: ([{**row, "series_id": series.series_id} for row in value]
                             if key == "pipeline_results" and value and value[0]["series_id"] == 42
                             else value) for key, value in change.items() if not key.startswith("_")})
        return SimpleNamespace(to_dict=lambda: payload)

    _install_fake_auto_process(monkeypatch, product, native=native,
                               frame_uid="partial-frame", process_call=fake_process)
    outcome = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert outcome.status == "FAILED"
    assert outcome.stage2_result is None
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "partial-frame"))
    # Nested work may already have committed independently; the handoff never stamps it.
    if frame is not None:
        assert "stage1_fingerprint" not in (frame.header_json or {})
        assert next(loc for loc in frame.locations if loc.locator == str(product)).content_sha256 is None


class _RecordProvider:
    def __init__(self, records):
        self.records = records

    def search(self, _request):
        yield from self.records


def _make_twenty_master_reduction_records(tmp_path: Path):
    source_id = "master-20-frame-smoke"
    assets_root = tmp_path / "stage1-inputs"
    (assets_root / "raw").mkdir(parents=True)
    (assets_root / "processing").mkdir()
    (assets_root / "calibration").mkdir()
    dark_locator = "calibration/dark.fit"
    flat_locator = "calibration/flat.fit"
    fits.PrimaryHDU(
        data=np.full((16, 16), 10, dtype=np.uint16),
        header=fits.Header(
            {
                "IMAGETYP": "SDARK",
                "ID_CAM": 204,
                "XBINNING": 1,
                "YBINNING": 1,
                "FILTER": "V",
                "EXPTIME": 50.0,
                "CCD-TEMP": -10.0,
            }
        ),
    ).writeto(assets_root / dark_locator)
    fits.PrimaryHDU(
        data=np.ones((16, 16), dtype=np.float32),
        header=fits.Header(
            {
                "IMAGETYP": "SFLAT",
                "ID_CAM": 204,
                "XBINNING": 1,
                "YBINNING": 1,
                "FILTER": "V",
                "EXPTIME": 5.0,
            }
        ),
    ).writeto(assets_root / flat_locator)

    master_header = {
        "ID_CAM": 204,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "V",
        "EXPTIME": 50.0,
        "IMAGEW": 16,
        "IMAGEH": 16,
        "CTYPE1": "RA---TAN-SIP",
        "CTYPE2": "DEC--TAN-SIP",
        "CRVAL1": 327.52,
        "CRVAL2": 72.18,
        "CRPIX1": 8.5,
        "CRPIX2": 8.5,
        "CD1_1": -0.0005,
        "CD1_2": 0.0,
        "CD2_1": 0.0,
        "CD2_2": 0.0005,
        "DARK": dark_locator,
        "FLAT": flat_locator,
    }
    records: list[ObservationRecord] = []
    for index in range(20):
        raw_id = 100000 + index
        native = f"204_{15814 + index}"
        date_obs = datetime(2018, 9, 7, 17, index, tzinfo=timezone.utc)
        raw_locator = f"raw/{raw_id}.fit"
        processing_locator = f"processing/{raw_id}_header.fits"
        raw_header = {
            "IMAGETYP": "LIGHT",
            "ID_CAM": 204,
            "XBINNING": 1,
            "YBINNING": 1,
            "FILTER": "V",
            "EXPTIME": 50.0,
            "CCD-TEMP": -9.887,
            "UNIQNUM": native,
            "DATE-OBS": date_obs.isoformat().replace("+00:00", "Z"),
            "OBJNAME": "MASTER SYNTHETIC FIELD",
        }
        fits.PrimaryHDU(
            data=np.full((16, 16), 100 + index, dtype=np.uint16),
            header=fits.Header(raw_header),
        ).writeto(assets_root / raw_locator)
        fits.PrimaryHDU(header=fits.Header(master_header)).writeto(
            assets_root / processing_locator
        )
        refs = tuple(
            AssetRef(source_id, role, "filesystem", locator)
            for role, locator in (
                ("science_raw", raw_locator),
                ("processing_header", processing_locator),
                ("dark", dark_locator),
                ("flat", flat_locator),
            )
        )
        records.append(
            ObservationRecord(
                source_id=source_id,
                instrument="MASTER",
                frame_type="science",
                input_state="raw",
                identity_status="READY",
                native_observation_id=native,
                source_observation_uid=make_source_observation_uid(source_id, native),
                identity_scheme="master_uniqnum_v1",
                p4_native_identity=native,
                observation_uid=make_observation_uid("master_uniqnum_v1", native),
                site="KAO",
                tube="EAST",
                camera="204",
                filter_name="V",
                object_name="MASTER SYNTHETIC FIELD",
                observed_at=date_obs.isoformat(),
                exptime=50.0,
                ra_deg=327.52,
                dec_deg=72.18,
                ccd_temp=-9.887,
                binning_x=1,
                binning_y=1,
                required_asset_roles=tuple(ref.role for ref in refs),
                assets=refs,
                metadata={"raw_id": str(raw_id)},
            )
        )
    return source_id, assets_root, records


def test_twenty_stage1_reductions_handoff_to_one_series_with_pipeline_stub(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    source_id, assets_root, records = _make_twenty_master_reduction_records(tmp_path)
    source = ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="filesystem",
    )
    request = ArchiveRequest(source_id=source_id)
    store = FilesystemProductStore(
        root=tmp_path / "products", store_id="master-20-smoke", disk_reserve_bytes=0
    )
    asset_provider = FilesystemAssetProvider(source_id=source_id, root=assets_root)
    from grandgousier.archive import stage1_planner

    original_iter_batches = stage1_planner.iter_stage1_plan_batches
    batch_lengths: list[int] = []

    def record_batch_lengths(**kwargs):
        for batch in original_iter_batches(**kwargs):
            batch_lengths.append(len(batch.items))
            yield batch

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", record_batch_lengths)
    execution_report = tmp_path / "stage1-execution.json"
    execution = execute_stage1_stream(
        request=request,
        source=source,
        metadata_provider=_RecordProvider(records),
        asset_provider=asset_provider,
        product_store=store,
        batch_size=7,
        plan_json_path=tmp_path / "stage1-plan.json",
        report_json_path=execution_report,
    )
    assert execution.status == "READY"
    assert execution.planned_items == 20
    assert execution.counts == {"REDUCED": 20}
    assert batch_lengths == [7, 7, 6]
    execution_payload = json.loads(execution_report.read_text(encoding="utf-8"))
    assert execution_payload["status"] == "READY"
    assert len(execution_payload["items"]) == 20
    assert {item["status"] for item in execution_payload["items"]} == {"REDUCED"}
    first_product = Path(execution_payload["items"][0]["product_path"])
    with fits.open(first_product, memmap=False) as hdul:
        np.testing.assert_allclose(hdul[0].data, np.full((16, 16), 90.0))
        assert hdul[0].header["UNIQNUM"] == "204_15814"
        assert hdul[0].header["GGSTAGE"] == "1B"

    db_session.add(
        CatalogSource(
            catalog_name="synthetic-handoff",
            source_id="synthetic-control",
            source_uid="synthetic-handoff-control",
            ra_deg=327.52,
            dec_deg=72.18,
            ref_mags={},
            colors={},
            source_flags={},
            raw_json={},
        )
    )
    db_session.commit()

    from grandgousier.archive import auto_process

    pipeline_calls = []

    def fake_pipeline(_session, config):
        pipeline_calls.append(config)
        return SimpleNamespace(
            pipeline_run_id=None,
            status="DONE",
            calibration_names={"science": "synthetic-science"},
            outputs={},
            quality_policy={},
            steps=[],
        )

    monkeypatch.setattr(auto_process, "run_field_pipeline", fake_pipeline)
    monkeypatch.setattr(
        auto_process,
        "build_light_curve_summary",
        lambda *_args, **_kwargs: SimpleNamespace(
            total_points=1, accepted_points=1, rejected_points=0, n_objects=1
        ),
    )

    # These test-only pipeline/summary stubs validate composition, not Stage-2 science.
    handoff = handoff_stage1_execution(
        db_session,
        execution_report_path=execution_report,
        execute=True,
        catalog_name="synthetic-handoff",
        min_frames_per_series=20,
        ensemble_min_ref_stars_per_frame=20,
        out_root="",
        log_dir="",
    )

    assert handoff.status == "READY", (handoff.errors, handoff.stage2_result)
    assert handoff.selected_items == 20
    assert handoff.blocked_items == 0
    assert handoff.stage2_result is not None
    stage2 = handoff.stage2_result
    assert stage2["status"] == "DONE"
    assert stage2["discovered_files"] == 20
    assert len(stage2["indexed_frame_ids"]) == 20
    assert len(stage2["created_series_ids"]) == 1
    assert stage2["processed_series_ids"] == stage2["created_series_ids"]
    assert len(pipeline_calls) == 1
    assert len(pipeline_calls[0].frame_ids) == 20
    memberships = db_session.scalars(
        select(FieldSeriesFrame).where(
            FieldSeriesFrame.series_id == stage2["created_series_ids"][0]
        )
    ).all()
    assert len(memberships) == 20
    assert len({membership.frame_id for membership in memberships}) == 20
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 20


def test_persist_only_master_handoff_uses_production_index_and_persists_identity(
    postgres_integration_session: Session, tmp_path: Path, monkeypatch
) -> None:
    db_session = postgres_integration_session
    source_id, assets_root, records = _make_twenty_master_reduction_records(tmp_path)
    source = ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="filesystem",
    )
    execution_report = tmp_path / "stage1-persist-only-execution.json"
    execution = execute_stage1_stream(
        request=ArchiveRequest(source_id=source_id),
        source=source,
        metadata_provider=_RecordProvider(records[:1]),
        asset_provider=FilesystemAssetProvider(source_id=source_id, root=assets_root),
        product_store=FilesystemProductStore(
            root=tmp_path / "persist-only-products",
            store_id="master-persist-only-smoke",
            disk_reserve_bytes=0,
        ),
        batch_size=1,
        plan_json_path=tmp_path / "stage1-persist-only-plan.json",
        report_json_path=execution_report,
    )

    assert execution.status == "READY"
    assert execution.planned_items == 1
    assert execution.counts == {"REDUCED": 1}
    product = load_validated_stage1_products(execution_report)[0]
    native_identity = "204_15814"
    assert product.native_observation_id == native_identity
    with fits.open(product.product_path, memmap=False) as hdul:
        assert hdul[0].header["UNIQNUM"] == native_identity
        assert hdul[0].header["GGSTAGE"] == "1B"

    from grandgousier.archive import auto_process

    stage2_calls = []

    def record_stage2_call(*args, **kwargs):
        stage2_calls.append((args, kwargs))
        raise AssertionError("persistence-only handoff must not invoke Stage 2")

    monkeypatch.setattr(auto_process, "process_archive_path", record_stage2_call)
    result = handoff_stage1_execution(
        db_session, execution_report_path=execution_report, persist_only=True
    )

    assert result.status == "READY"
    assert result.persistence_only is True
    assert result.errors == ()
    assert result.stage2_result is None
    payload = result.to_dict()
    assert payload["stage2_status"] == "NOT_INVOKED"
    assert payload["summary"]["stage2_invoked"] is False
    assert payload["summary"]["persisted_identity_only"] == 1
    assert result.persistence_result["status"] == "PERSISTED_IDENTITY_ONLY"
    assert result.persistence_result["index_results"] == [
        {
            "profile_name": "MASTER",
            "discovered": 1,
            "inserted": 1,
            "skipped_existing": 0,
            "failed": 0,
        }
    ]
    assert stage2_calls == []

    assert len(result.items) == 1
    item = result.items[0]
    assert item.p4_action == ObservationScienceAction.SELECTED.value
    assert item.status == "PERSISTED_IDENTITY_ONLY"
    assert item.selected_frame_id is not None
    assert result.persistence_result["indexed_frame_ids"] == [item.selected_frame_id]
    frame = db_session.get(Frame, item.selected_frame_id)
    assert frame is not None
    assert item.selected_frame_id == frame.frame_id
    assert frame.instrument == "MASTER"
    assert frame.header_json["UNIQNUM"] == native_identity
    assert frame.observation_id is not None
    observation = db_session.get(Observation, frame.observation_id)
    assert observation is not None
    assert frame.observation.observation_id == observation.observation_id
    assert observation.observation_uid == product.observation_uid
    assert observation.identity_scheme == product.identity_scheme
    assert observation.native_identity == native_identity

    assert frame.header_json["stage1_fingerprint"] == product.fingerprint
    assert frame.header_json["source_observation_uid"] == product.source_observation_uid
    locations = db_session.scalars(
        select(FrameLocation).where(FrameLocation.frame_id == frame.frame_id)
    ).all()
    assert len(locations) == 1
    assert locations[0].locator == str(product.product_path)
    assert locations[0].content_sha256 == product.product_checksum_sha256
    assert locations[0].is_primary is True
    assert db_session.scalar(select(func.count(Observation.observation_id))) == 1
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 1
    assert db_session.scalar(select(func.count(FieldSeriesFrame.frame_id))) == 0


def test_three_persist_only_handoff_cycles_keep_one_series_open(
    postgres_integration_session: Session, tmp_path: Path, monkeypatch
) -> None:
    db_session = postgres_integration_session
    from grandgousier.archive import auto_process
    from grandgousier.archive.fields_series import build_field_series

    source_id, assets_root, records = _make_twenty_master_reduction_records(tmp_path)
    source = ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="filesystem",
    )
    product_store = FilesystemProductStore(
        root=tmp_path / "incremental-products",
        store_id="master-incremental-smoke",
        disk_reserve_bytes=0,
    )
    stage2_calls = []

    def forbidden_stage2(*args, **kwargs):
        stage2_calls.append((args, kwargs))
        raise AssertionError("persistence-only handoff must not invoke Stage 2")

    monkeypatch.setattr(auto_process, "process_archive_path", forbidden_stage2)

    batch_sizes = [7, 7, 6]
    handoff_states: list[tuple[str, tuple[str, ...], tuple[int, ...]]] = []
    builder_states: list[list[tuple[int, str, int, str]]] = []
    offset = 0

    for cycle_index, batch_size in enumerate(batch_sizes, start=1):
        batch_records = records[offset : offset + batch_size]
        offset += batch_size
        execution_report = tmp_path / f"stage1-cycle-{cycle_index}-execution.json"
        execution = execute_stage1_stream(
            request=ArchiveRequest(source_id=source_id),
            source=source,
            metadata_provider=_RecordProvider(batch_records),
            asset_provider=FilesystemAssetProvider(source_id=source_id, root=assets_root),
            product_store=product_store,
            batch_size=batch_size,
            plan_json_path=tmp_path / f"stage1-cycle-{cycle_index}-plan.json",
            report_json_path=execution_report,
        )
        assert execution.status == "READY"
        assert execution.planned_items == batch_size
        assert execution.counts == {"REDUCED": batch_size}

        handoff = handoff_stage1_execution(
            db_session,
            execution_report_path=execution_report,
            persist_only=True,
        )
        successful_items = [
            item for item in handoff.items if item.status == "PERSISTED_IDENTITY_ONLY"
        ]
        selected_frame_ids = tuple(
            int(item.selected_frame_id)
            for item in successful_items
            if item.selected_frame_id is not None
        )
        handoff_states.append(
            (
                handoff.status,
                tuple(item.status for item in handoff.items),
                selected_frame_ids,
            )
        )
        assert handoff.status == "READY", handoff.errors
        assert handoff.persistence_only is True
        assert handoff.stage2_result is None
        assert len(successful_items) == batch_size
        assert len(selected_frame_ids) == batch_size
        assert all(item.selected_frame_id is not None for item in successful_items)

        # Feed only successful persisted Frame IDs returned by this handoff cycle.
        build_field_series(
            db_session,
            frame_ids=list(selected_frame_ids),
            max_series_gap_minutes=60.0,
            min_frames_per_series=1,
            finalize_tail=False,
        )
        current_series = db_session.scalars(
            select(FieldSeries).order_by(FieldSeries.series_id)
        ).all()
        builder_states.append(
            [
                (
                    int(series.series_id),
                    str(series.status),
                    int(series.n_frames),
                    str(series.series_uid),
                )
                for series in current_series
            ]
        )

    assert offset == 20
    assert [len(state[2]) for state in handoff_states] == batch_sizes
    assert [state[0] for state in handoff_states] == ["READY", "READY", "READY"]
    assert [len(state) for state in builder_states] == [1, 1, 1]
    series_ids = [state[0][0] for state in builder_states]
    assert len(set(series_ids)) == 1
    assert [state[0][1] for state in builder_states] == ["OPEN", "OPEN", "OPEN"]
    assert [state[0][2] for state in builder_states] == [7, 14, 20]
    assert len({state[0][3] for state in builder_states}) == 1

    series_rows = db_session.scalars(select(FieldSeries)).all()
    assert len(series_rows) == 1
    assert series_rows[0].status == "OPEN"
    memberships = db_session.scalars(
        select(FieldSeriesFrame).where(FieldSeriesFrame.series_id == series_rows[0].series_id)
    ).all()
    assert len(memberships) == 20
    assert len({membership.frame_id for membership in memberships}) == 20
    assert {membership.frame_id for membership in memberships} == {
        frame_id for _status, _item_statuses, frame_ids in handoff_states for frame_id in frame_ids
    }
    assert db_session.scalar(select(func.count(FieldSeries.series_id))) == 1
    assert stage2_calls == []

    from grandgousier.archive.fields_series import finalize_open_series

    pipeline_calls = []
    from grandgousier.pipeline import runner

    monkeypatch.setattr(
        runner,
        "run_field_pipeline",
        lambda _session, config: pipeline_calls.append(int(config.series_id)),
    )
    original_uid = series_rows[0].series_uid
    finalized = finalize_open_series(db_session, int(series_rows[0].series_id))
    assert finalized["finalized"] is True
    assert finalized["eligible"] is True
    assert finalized["status"] == "READY"
    assert db_session.get(FieldSeries, series_rows[0].series_id).series_uid == original_uid
    assert pipeline_calls == []

    assert _run_archive_pipeline_spy(monkeypatch, db_session, status="READY") == [
        int(series_rows[0].series_id)
    ]


def test_observed_gap_closes_eligible_tail_and_queues_only_ready_series(
    postgres_integration_session: Session, monkeypatch
) -> None:
    db_session = postgres_integration_session
    from grandgousier.archive.fields_series import build_field_series

    first_start = datetime(2018, 9, 7, 17, 0, tzinfo=UTC)
    prior_frames = _make_series_test_frames(
        db_session,
        prefix="gap-prior",
        count=2,
        start=first_start,
    )
    prior_result = build_field_series(
        db_session,
        frame_ids=[int(frame.frame_id) for frame in prior_frames],
        min_frames_per_series=2,
        finalize_tail=False,
    )
    prior_id = int(prior_result.rows[0]["series_id"])
    assert prior_result.rows[0]["status"] == "OPEN"

    later_start = first_start + timedelta(hours=2)
    later_frames = _make_series_test_frames(
        db_session,
        prefix="gap-later",
        count=1,
        start=later_start,
        start_mjd=58370.0 + (120.0 / 1440.0),
    )
    boundary_result = build_field_series(
        db_session,
        frame_ids=[int(frame.frame_id) for frame in later_frames],
        min_frames_per_series=2,
        finalize_tail=False,
    )
    later_membership = db_session.scalar(
        select(FieldSeriesFrame).where(FieldSeriesFrame.frame_id == later_frames[0].frame_id)
    )
    assert later_membership is not None
    later_id = int(later_membership.series_id)
    assert later_id != prior_id
    assert db_session.get(FieldSeries, prior_id).status == "READY"
    assert db_session.get(FieldSeries, later_id).status == "OPEN"
    rows_by_id = {int(row["series_id"]): row for row in boundary_result.rows}
    assert rows_by_id[prior_id]["status"] == "READY"
    assert rows_by_id[later_id]["status"] == "OPEN"
    assert boundary_result.open_series_ids == [later_id]

    assert _run_archive_pipeline_spy(monkeypatch, db_session, status="READY") == [prior_id]


def test_streaming_minimum_flush_and_one_shot_default_preserve_readiness(
    postgres_integration_session: Session,
) -> None:
    db_session = postgres_integration_session
    from grandgousier.archive.fields_series import build_field_series, finalize_open_series

    start = datetime(2018, 9, 7, 17, 0, tzinfo=UTC)
    first = _make_series_test_frames(
        db_session,
        prefix="minimum-stream",
        count=1,
        start=start,
    )[0]

    one_shot_short = build_field_series(
        db_session,
        frame_ids=[int(first.frame_id)],
        min_frames_per_series=2,
    )
    assert one_shot_short.rows == []
    assert db_session.scalar(select(func.count(FieldSeries.series_id))) == 0

    streamed = build_field_series(
        db_session,
        frame_ids=[int(first.frame_id)],
        min_frames_per_series=2,
        finalize_tail=False,
    )
    assert len(streamed.rows) == 1
    series_id = int(streamed.rows[0]["series_id"])
    original_uid = str(streamed.rows[0]["series_uid"])
    assert streamed.rows[0]["status"] == "OPEN"
    assert streamed.rows[0]["n_frames"] == 1

    below_minimum = finalize_open_series(db_session, series_id)
    assert below_minimum["finalized"] is False
    assert below_minimum["eligible"] is False
    assert below_minimum["reason"] == "BELOW_MINIMUM"
    assert db_session.get(FieldSeries, series_id).status == "OPEN"

    second = _make_series_test_frames(
        db_session,
        prefix="minimum-stream-append",
        count=1,
        start=start + timedelta(minutes=1),
        start_mjd=58370.0 + (1.0 / 1440.0),
    )[0]
    appended = build_field_series(
        db_session,
        frame_ids=[int(second.frame_id)],
        min_frames_per_series=2,
        finalize_tail=False,
    )
    assert len(appended.rows) == 1
    assert int(appended.rows[0]["series_id"]) == series_id
    assert appended.rows[0]["series_uid"] == original_uid
    assert appended.rows[0]["status"] == "OPEN"
    assert appended.rows[0]["n_frames"] == 2

    finalized = finalize_open_series(db_session, series_id)
    assert finalized["status"] == "READY"
    assert finalized["finalized"] is True
    assert db_session.get(FieldSeries, series_id).series_uid == original_uid
    streamed_members = db_session.scalars(
        select(FieldSeriesFrame).where(FieldSeriesFrame.series_id == series_id)
    ).all()
    assert len(streamed_members) == 2
    assert len({int(member.frame_id) for member in streamed_members}) == 2

    # A separate one-shot construction uses different database-local Frame IDs,
    # so its UID is not compared with the streamed UID; its membership and READY
    # eligibility still follow the unchanged one-shot default path.
    one_shot_frames = _make_series_test_frames(
        db_session,
        prefix="minimum-one-shot",
        count=2,
        start=start + timedelta(hours=3),
        start_mjd=58370.0 + (180.0 / 1440.0),
    )
    one_shot = build_field_series(
        db_session,
        frame_ids=[int(frame.frame_id) for frame in one_shot_frames],
        min_frames_per_series=2,
    )
    assert len(one_shot.rows) == 1
    assert one_shot.rows[0]["status"] == "READY"
    assert one_shot.rows[0]["n_frames"] == 2
    one_shot_id = int(one_shot.rows[0]["series_id"])
    assert db_session.get(FieldSeries, one_shot_id).status == "READY"
    one_shot_members = db_session.scalars(
        select(FieldSeriesFrame).where(FieldSeriesFrame.series_id == one_shot_id)
    ).all()
    assert len(one_shot_members) == 2


def test_include_assigned_exact_legacy_replay_reuses_series_uid_and_membership(
    postgres_integration_session: Session,
) -> None:
    db_session = postgres_integration_session
    from grandgousier.archive.fields_series import build_field_series

    frames = _make_series_test_frames(
        db_session,
        prefix="legacy-replay",
        count=2,
    )
    field = Field(
        field_uid="legacy-replay-field",
        field_name="legacy-replay",
        ra_center_deg=327.52,
        dec_center_deg=72.18,
        radius_arcmin=30.0,
        status="READY",
        metadata_json={},
    )
    db_session.add(field)
    db_session.flush()
    legacy_uid = "series_legacy_uid_preserved"
    series = FieldSeries(
        field_id=field.field_id,
        series_uid=legacy_uid,
        instrument_unit_id=None,
        filter_name="V",
        n_frames=2,
        grouping_method="center_time_v1",
        status="READY",
        metadata_json={"legacy_uid": True},
    )
    db_session.add(series)
    db_session.flush()
    db_session.add_all(
        [
            FieldSeriesFrame(
                series_id=series.series_id,
                frame_id=frame.frame_id,
                order_index=index,
            )
            for index, frame in enumerate(frames, start=1)
        ]
    )
    db_session.flush()

    replay = build_field_series(
        db_session,
        frame_ids=[int(frame.frame_id) for frame in frames],
        only_unassigned=False,
        min_frames_per_series=1,
    )

    assert db_session.scalar(select(func.count(FieldSeries.series_id))) == 1
    assert replay.rows[0]["series_id"] == series.series_id
    assert replay.rows[0]["series_uid"] == legacy_uid
    assert db_session.get(FieldSeries, series.series_id).series_uid == legacy_uid
    members = db_session.scalars(
        select(FieldSeriesFrame).where(FieldSeriesFrame.series_id == series.series_id)
    ).all()
    assert len(members) == 2
    assert len({int(member.frame_id) for member in members}) == 2


@dataclass(slots=True)
class _DurablePersistOnlyCase:
    name: str
    source_id: str
    native_identity: str
    source_observation_uid: str
    observation_uid: str
    camera: str
    date_obs: datetime
    object_name: str
    raw_id: int
    raw_key: str
    raw_remote_path: str
    header_remote_path: str
    dark_remote_path: str
    flat_remote_path: str
    product_root: Path
    product_path: Path
    product_store_id: str
    instrument_unit_uid: str
    frame_location_uid: str
    record: ObservationRecord
    captured_observations: dict[int, tuple[str, str, str]] = field(default_factory=dict)
    captured_frames: dict[int, tuple[str, int | None, int | None]] = field(default_factory=dict)
    captured_locations: dict[int, tuple[int, str, str]] = field(default_factory=dict)
    captured_units: dict[int, tuple[str, ...]] = field(default_factory=dict)
    captured_unit_rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    captured_events: dict[int, tuple[str, str, str | None, int | None]] = field(
        default_factory=dict
    )
    expected_frame_uid: str | None = None
    preflight_unit_snapshot: dict[str, Any] | None = None
    preflight_unit_frame_refs: list[dict[str, Any]] = field(default_factory=list)
    preflight_unit_fk_counts: dict[tuple[str, ...], int] = field(default_factory=dict)
    unit_insert_witness: dict[str, Any] | None = None
    unit_ownership: str = "UNKNOWN"


class _SyntheticStage1AssetProvider:
    def __init__(self, paths_by_role: dict[str, Path]) -> None:
        self.paths_by_role = paths_by_role

    def materialize(self, ref: AssetRef, destination: str | Path | None = None) -> LocalAsset:
        del destination
        return LocalAsset(ref=ref, path=self.paths_by_role[ref.role])


class _DurablePersistOnlyPostgresHarness:
    """Guard each NullPool backend independently; sessions have no enclosing transaction."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.cases: list[_DurablePersistOnlyCase] = []
        self.backend_pids: list[int] = []
        self.safety_stop = False
        self.cleanup_started = False
        self.cleanup_verified = False
        self._used_cameras: set[str] = set()

    def new_case(
        self,
        tmp_path: Path,
        name: str,
        index: int,
        *,
        pinned_identity: bool = False,
    ) -> _DurablePersistOnlyCase:
        if pinned_identity:
            if name != _PINNED_DURABLE_CASE_ID:
                raise ValueError("Pinned durable case ID mismatch")
            token = _PINNED_DURABLE_CASE_ID
            camera = "204"
            raw_id = 2026102701
        else:
            token = uuid4().hex
            camera = str(100000 + (int(token[:8], 16) % 900000))
            while camera in self._used_cameras:
                token = uuid4().hex
                camera = str(100000 + (int(token[:8], 16) % 900000))
            self._used_cameras.add(camera)
            raw_id = 1_000_000_000 + (int(token[8:20], 16) % 8_999_999_999)
        native = f"204_{raw_id}"
        source_id = f"synthetic-persist-only-{name}-{token}"
        source_uid = make_source_observation_uid(source_id, native)
        observation_uid = make_observation_uid("master_uniqnum_v1", native)
        date_obs = datetime(2018, 9, 7, 17, 4, 22, tzinfo=UTC) + timedelta(seconds=index)
        object_name = (
            f"SYNTHETIC PERSIST ONLY {name}"
            if pinned_identity
            else f"SYNTHETIC PERSIST ONLY {name} {token[:8]}"
        )
        raw_key = f"rawdata/2018/09/07/{raw_id}.fit.gz"
        raw_remote_path = f"/master/imdata/{raw_key}"
        header_remote_path = f"/master/imdata/headers/{raw_id}_header.fits"
        dark_remote_path = f"/master/imdata/calibration/{raw_id}_dark.fit"
        flat_remote_path = f"/master/imdata/calibration/{raw_id}_flat.fit"
        product_root = tmp_path / f"{name}-{token}" / "products"
        product_store_id = f"synthetic-{name}-{token}"
        product_locator = Path(source_id) / "MASTER" / date_obs.strftime("%Y/%m/%d")
        product_locator = product_locator / f"{source_uid}_processed.fits"
        product_path = product_root.joinpath(*product_locator.parts)
        instrument_unit_uid = make_instrument_unit_uid(
            "MASTER", "SYNTHETIC", "EAST", camera, "V", object_name
        )
        assets = (
            AssetRef(
                source_id=source_id,
                role="science_raw",
                backend="master-physical",
                locator=raw_remote_path,
                metadata={"raw_relative_physical_key": raw_key},
            ),
            AssetRef(
                source_id=source_id,
                role="processing_header",
                backend="master-physical",
                locator=header_remote_path,
            ),
            AssetRef(
                source_id=source_id,
                role="dark",
                backend="master-physical",
                locator=dark_remote_path,
            ),
            AssetRef(
                source_id=source_id,
                role="flat",
                backend="master-physical",
                locator=flat_remote_path,
            ),
        )
        record = ObservationRecord(
            source_id=source_id,
            instrument="MASTER",
            frame_type="science",
            input_state="raw",
            identity_status="READY",
            native_observation_id=native,
            source_observation_uid=source_uid,
            identity_scheme="master_uniqnum_v1",
            p4_native_identity=native,
            observation_uid=observation_uid,
            site="SYNTHETIC",
            tube="EAST",
            camera=camera,
            filter_name="V",
            object_name=object_name,
            observed_at=date_obs.isoformat(),
            exptime=50.0,
            ra_deg=327.52,
            dec_deg=72.18,
            ccd_temp=-9.0,
            binning_x=1,
            binning_y=1,
            required_asset_roles=("science_raw", "processing_header", "dark", "flat"),
            assets=assets,
            metadata={"raw_id": str(raw_id), "raw_relative_physical_key": raw_key},
        )
        return _DurablePersistOnlyCase(
            name=name,
            source_id=source_id,
            native_identity=native,
            source_observation_uid=source_uid,
            observation_uid=observation_uid,
            camera=camera,
            date_obs=date_obs,
            object_name=object_name,
            raw_id=raw_id,
            raw_key=raw_key,
            raw_remote_path=raw_remote_path,
            header_remote_path=header_remote_path,
            dark_remote_path=dark_remote_path,
            flat_remote_path=flat_remote_path,
            product_root=product_root,
            product_path=product_path,
            product_store_id=product_store_id,
            instrument_unit_uid=instrument_unit_uid,
            frame_location_uid=make_frame_location_uid(str(product_path)),
            record=record,
        )

    def _open_guarded_connection(self, *, read_only: bool = False):
        try:
            connection = self.engine.connect()
        except Exception:
            self.safety_stop = True
            raise AssertionError("Durable PostgreSQL connection failed; details redacted") from None
        try:
            connection.begin()
            if read_only:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            _guard_postgres_integration_connection(connection, read_only=read_only)
            backend_pid = int(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            if not read_only:
                connection.rollback()
        except Exception:
            self.safety_stop = True
            try:
                connection.close()
            except Exception:
                pass
            raise AssertionError("Durable PostgreSQL guard/probe failed; no session created") from None
        self.backend_pids.append(backend_pid)
        return connection, backend_pid

    @contextmanager
    def connection(self, *, read_only: bool = False) -> Iterator[tuple[Any, int]]:
        connection, backend_pid = self._open_guarded_connection(read_only=read_only)
        failure = False
        try:
            yield connection, backend_pid
        finally:
            try:
                if connection.in_transaction():
                    connection.rollback()
            except Exception:
                failure = True
                self.safety_stop = True
            try:
                connection.close()
            except Exception:
                failure = True
                self.safety_stop = True
            if failure:
                raise AssertionError("Durable PostgreSQL rollback/close failed; state uncertain") from None

    @contextmanager
    def session(
        self,
        *,
        read_only: bool = False,
        unit_commit_gate_case: _DurablePersistOnlyCase | None = None,
    ) -> Iterator[tuple[Session, int]]:
        with self.connection(read_only=read_only) as (connection, backend_pid):
            try:
                session = Session(bind=connection, expire_on_commit=False)
            except Exception:
                self.safety_stop = True
                raise AssertionError("Durable PostgreSQL Session setup failed; details redacted") from None
            unit_connection = session.connection()
            pending: dict[int, tuple[_DurablePersistOnlyCase, dict[str, Any]]] = {}
            before_commit_listener = None

            def unit_after_insert(_mapper, insert_connection, target) -> None:
                case = next(
                    (value for value in self.cases if value.instrument_unit_uid == target.unit_uid),
                    None,
                )
                if insert_connection is not unit_connection or case is None:
                    return
                if case.preflight_unit_snapshot is not None:
                    raise AssertionError("Preexisting InstrumentUnit was unexpectedly inserted")
                witness = _instrument_unit_witness(target)
                expected = _expected_instrument_unit_witness(case, target.instrument_unit_id)
                if witness != expected or id(case) in pending:
                    raise AssertionError("InstrumentUnit INSERT witness did not match the exact case")
                pending[id(case)] = (case, witness)

            def session_after_commit(_session) -> None:
                for case, witness in pending.values():
                    if case.unit_insert_witness not in (None, witness):
                        raise AssertionError("InstrumentUnit INSERT/commit witnesses conflict")
                    case.unit_insert_witness = witness
                    case.unit_ownership = "NEW"
                pending.clear()

            def session_after_rollback(_session) -> None:
                pending.clear()

            try:
                event.listen(InstrumentUnit, "after_insert", unit_after_insert)
                event.listen(session, "after_commit", session_after_commit)
                event.listen(session, "after_rollback", session_after_rollback)
                if unit_commit_gate_case is not None:
                    if not any(case is unit_commit_gate_case for case in self.cases):
                        raise AssertionError("Unit commit gate case was not preflighted by this harness")

                    def session_before_commit(_session) -> None:
                        if _session is not session:
                            raise AssertionError("Unit commit gate ran on an unexpected Session")
                        if _session.in_nested_transaction():
                            return
                        if unit_commit_gate_case.preflight_unit_snapshot is None:
                            _session.flush()
                        _assert_unit_commit_witness(unit_commit_gate_case, pending)

                    before_commit_listener = session_before_commit
                    event.listen(session, "before_commit", before_commit_listener)
                yield session, backend_pid
            finally:
                listeners = [
                    (session, "after_rollback", session_after_rollback),
                    (session, "after_commit", session_after_commit),
                    (InstrumentUnit, "after_insert", unit_after_insert),
                ]
                if before_commit_listener is not None:
                    listeners.append((session, "before_commit", before_commit_listener))
                for target, event_name, listener in listeners:
                    try:
                        event.remove(target, event_name, listener)
                    except Exception:
                        self.safety_stop = True
                try:
                    session.close()
                except Exception:
                    self.safety_stop = True
                    raise AssertionError("Durable PostgreSQL Session teardown failed; state uncertain") from None

    def preflight_absent(self, case: _DurablePersistOnlyCase) -> int:
        return self.preflight_case(case, allow_preexisting_unit=False)

    def preflight_case(
        self, case: _DurablePersistOnlyCase, *, allow_preexisting_unit: bool
    ) -> int:
        if case.expected_frame_uid is None:
            raise AssertionError("Exact product frame_uid must be pinned before preflight")
        try:
            with self.session(read_only=True) as (session, backend_pid):
                snapshot = self.snapshot(session, case)
                if any(
                    snapshot[name]
                    for name in ("observations", "frames", "locations", "events", "memberships")
                ):
                    raise AssertionError("Synthetic identity/path/Frame preflight collision")
                if len(snapshot["units"]) > 1:
                    raise AssertionError("InstrumentUnit UID/tuple preflight was ambiguous")
                if snapshot["units"]:
                    unit = snapshot["units"][0]
                    expected = dict(
                        zip(
                            _UNIT_IDENTITY_FIELDS,
                            (case.instrument_unit_uid, case.record.instrument, case.record.site,
                             case.record.tube, case.record.camera, case.record.filter_name,
                             case.record.object_name),
                            strict=True,
                        )
                    ) | _durable_master_unit_profile_metadata()
                    if not allow_preexisting_unit or any(
                        unit[name] != value for name, value in expected.items()
                    ):
                        raise AssertionError("Preexisting InstrumentUnit identity/profile/config mismatch")
                    case.preflight_unit_snapshot = _instrument_unit_snapshot(unit)
                    case.preflight_unit_frame_refs = list(snapshot["frames_by_unit"])
                    case.captured_units[int(unit["instrument_unit_id"])] = _unit_identity(unit)
                    case.unit_ownership = "PREEXISTING"
                else:
                    case.preflight_unit_snapshot = None
                    case.preflight_unit_frame_refs = []
                    case.unit_ownership = "UNKNOWN"
                self._assert_foreign_key_dependencies_are_exact(
                    session, cases=[case], preflight=True
                )
        except AssertionError:
            raise
        except Exception:
            raise AssertionError("Exact read-only durable preflight failed; no writes attempted") from None
        self.cases.append(case)
        return backend_pid

    def snapshot(self, session: Session, case: _DurablePersistOnlyCase) -> dict[str, list[dict[str, Any]]]:
        observations = [
            {
                "observation_id": int(row.observation_id),
                "observation_uid": row.observation_uid,
                "identity_scheme": row.identity_scheme,
                "native_identity": row.native_identity,
            }
            for row in session.scalars(
                select(Observation).where(
                    or_(
                        Observation.observation_uid == case.observation_uid,
                        (Observation.identity_scheme == "master_uniqnum_v1")
                        & (Observation.native_identity == case.native_identity),
                    )
                )
            )
        ]
        frames = [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT frame_id, frame_uid, observation_id, instrument_unit_id,
                           instrument, camera, filter_name, observing_mode,
                           image_type::text AS image_type, date_obs,
                           ra_center_deg, dec_center_deg, header_json, quality_flags
                    FROM public.frames
                    WHERE frame_uid = :frame_uid
                       OR (instrument = 'MASTER' AND header_json->>'UNIQNUM' = :native_identity)
                    ORDER BY frame_id
                    """
                ),
                {"native_identity": case.native_identity, "frame_uid": case.expected_frame_uid},
            ).mappings()
        ]
        locations = [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT frame_location_id, frame_id, locator, locator_uid,
                           location_kind, is_primary, content_sha256
                    FROM public.frame_locations
                    WHERE locator_uid = :locator_uid OR locator = :product_path
                    ORDER BY frame_location_id
                    """
                ),
                {
                    "locator_uid": case.frame_location_uid,
                    "product_path": str(case.product_path),
                },
            ).mappings()
        ]
        units = [
            _instrument_unit_snapshot(row)
            for row in session.scalars(
                select(InstrumentUnit).where(
                    or_(
                        InstrumentUnit.unit_uid == case.instrument_unit_uid,
                        and_(
                            InstrumentUnit.instrument == case.record.instrument,
                            InstrumentUnit.site.is_not_distinct_from(case.record.site),
                            InstrumentUnit.tube.is_not_distinct_from(case.record.tube),
                            InstrumentUnit.camera.is_not_distinct_from(case.record.camera),
                            InstrumentUnit.filter_name.is_not_distinct_from(case.record.filter_name),
                            InstrumentUnit.observing_mode.is_not_distinct_from(case.record.object_name),
                        ),
                    )
                ).order_by(InstrumentUnit.instrument_unit_id)
            )
        ]
        discovered_frame_ids = {int(row["frame_id"]) for row in frames} | {
            int(row["frame_id"]) for row in locations
        }
        events = [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT event_id, frame_id, stage, level::text AS level,
                           details->>'path' AS path, details
                    FROM public.processing_events
                    WHERE details->>'path' = :product_path
                       OR frame_id = ANY(CAST(:frame_ids AS bigint[]))
                    ORDER BY event_id
                    """
                ),
                {
                    "product_path": str(case.product_path),
                    "frame_ids": sorted(discovered_frame_ids),
                },
            ).mappings()
        ]
        frames_by_observation: list[dict[str, Any]] = []
        observation_ids = [row["observation_id"] for row in observations]
        if observation_ids:
            frames_by_observation = [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT frame_id, observation_id, frame_uid FROM public.frames "
                        "WHERE observation_id = ANY(:observation_ids) ORDER BY frame_id"
                    ),
                    {"observation_ids": observation_ids},
                ).mappings()
            ]
        frames_by_unit: list[dict[str, Any]] = []
        unit_ids = [row["instrument_unit_id"] for row in units]
        if unit_ids:
            frames_by_unit = [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT frame_id, instrument_unit_id, frame_uid FROM public.frames "
                        "WHERE instrument_unit_id = ANY(:unit_ids) ORDER BY frame_id"
                    ),
                    {"unit_ids": unit_ids},
                ).mappings()
            ]
        memberships: list[dict[str, Any]] = []
        if discovered_frame_ids:
            memberships = [
                dict(row)
                for row in session.execute(
                    text(
                        "SELECT series_id, frame_id FROM public.field_series_frames "
                        "WHERE frame_id = ANY(:frame_ids) ORDER BY frame_id, series_id"
                    ),
                    {"frame_ids": sorted(discovered_frame_ids)},
                ).mappings()
            ]
        return {
            "observations": observations,
            "frames": frames,
            "locations": locations,
            "units": units,
            "events": events,
            "frames_by_observation": frames_by_observation,
            "frames_by_unit": frames_by_unit,
            "memberships": memberships,
        }

    def capture(self, case: _DurablePersistOnlyCase, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        case.captured_observations.update(
            {
                int(row["observation_id"]): (
                    str(row["observation_uid"]),
                    str(row["identity_scheme"]),
                    str(row["native_identity"]),
                )
                for row in snapshot["observations"]
            }
        )
        case.captured_frames.update(
            {
                int(row["frame_id"]): (
                    str(row["frame_uid"]),
                    int(row["observation_id"]) if row["observation_id"] is not None else None,
                    int(row["instrument_unit_id"])
                    if row["instrument_unit_id"] is not None
                    else None,
                )
                for row in snapshot["frames"]
            }
        )
        case.captured_locations.update(
            {
                int(row["frame_location_id"]): (
                    int(row["frame_id"]),
                    str(row["locator"]),
                    str(row["locator_uid"]),
                )
                for row in snapshot["locations"]
            }
        )
        case.captured_units.update(
            {
                int(row["instrument_unit_id"]): _unit_identity(row)
                for row in snapshot["units"]
            }
        )
        if hasattr(case, "captured_unit_rows"):
            case.captured_unit_rows.update(
                {
                    int(row["instrument_unit_id"]): _instrument_unit_snapshot(row)
                    for row in snapshot["units"]
                }
            )
            if case.preflight_unit_snapshot is not None:
                case.unit_ownership = "PREEXISTING"
            elif snapshot["units"]:
                actual_witness = _instrument_unit_witness(snapshot["units"][0])
                case.unit_ownership = (
                    "NEW"
                    if case.unit_insert_witness == actual_witness
                    else "UNKNOWN"
                )
        case.captured_events.update(
            {
                int(row["event_id"]): (
                    str(row["stage"]),
                    str(row["level"]),
                    str(row["path"]) if row["path"] is not None else None,
                    int(row["frame_id"]) if row["frame_id"] is not None else None,
                )
                for row in snapshot["events"]
            }
        )

    def _assert_snapshot_matches_allowlist(
        self, case: _DurablePersistOnlyCase, snapshot: dict[str, list[dict[str, Any]]]
    ) -> None:
        if any(
            len(snapshot[table]) > 1
            for table in ("observations", "frames", "locations", "units")
        ):
            raise AssertionError(f"Ambiguous synthetic rows for {case.name}; no rows deleted")
        if any(
            row["observation_uid"] != case.observation_uid
            or row["identity_scheme"] != "master_uniqnum_v1"
            or row["native_identity"] != case.native_identity
            for row in snapshot["observations"]
        ):
            raise AssertionError(f"Observation identity mismatch for {case.name}; no rows deleted")
        if any(
            row["locator"] != str(case.product_path)
            or row["locator_uid"] != case.frame_location_uid
            for row in snapshot["locations"]
        ):
            raise AssertionError(f"Product-location mismatch for {case.name}; no rows deleted")
        if any(
            row["unit_uid"] != case.instrument_unit_uid
            or row["instrument"] != case.record.instrument
            or row["site"] != case.record.site
            or row["tube"] != case.record.tube
            or row["camera"] != case.record.camera
            or row["filter_name"] != case.record.filter_name
            or row["observing_mode"] != case.record.object_name
            for row in snapshot["units"]
        ):
            raise AssertionError(f"InstrumentUnit mismatch for {case.name}; no rows deleted")
        frame_ids = {int(row["frame_id"]) for row in snapshot["frames"]}
        if case.expected_frame_uid is not None and any(
            row["frame_uid"] != case.expected_frame_uid for row in snapshot["frames"]
        ):
            raise AssertionError(f"Frame UID mismatch for {case.name}; no rows deleted")
        if any(row["frame_id"] not in frame_ids for row in snapshot["locations"]):
            raise AssertionError(f"Unlinked synthetic identity for {case.name}; no rows deleted")
        observations = {
            int(row["observation_id"]): (
                str(row["observation_uid"]),
                str(row["identity_scheme"]),
                str(row["native_identity"]),
            )
            for row in snapshot["observations"]
        }
        frames = {
            int(row["frame_id"]): (
                str(row["frame_uid"]),
                int(row["observation_id"]) if row["observation_id"] is not None else None,
                int(row["instrument_unit_id"])
                if row["instrument_unit_id"] is not None
                else None,
            )
            for row in snapshot["frames"]
        }
        locations = {
            int(row["frame_location_id"]): (
                int(row["frame_id"]),
                str(row["locator"]),
                str(row["locator_uid"]),
            )
            for row in snapshot["locations"]
        }
        units = {
            int(row["instrument_unit_id"]): _unit_identity(row)
            for row in snapshot["units"]
        }
        unit_rows = {
            int(row["instrument_unit_id"]): _instrument_unit_snapshot(row)
            for row in snapshot["units"]
            if all(column in row for column in _UNIT_ROW_COLUMNS)
        }
        preexisting_unit = getattr(case, "preflight_unit_snapshot", None)
        if preexisting_unit is not None and unit_rows != {
            int(preexisting_unit["instrument_unit_id"]): preexisting_unit
        }:
            raise AssertionError(f"Preexisting InstrumentUnit changed for {case.name}; no rows deleted")
        if hasattr(case, "unit_ownership") and unit_rows and preexisting_unit is None:
            if (
                case.unit_ownership != "NEW"
                or case.unit_insert_witness is None
                or any(
                    _instrument_unit_witness(row) != case.unit_insert_witness
                    for row in snapshot["units"]
                )
            ):
                raise AssertionError(f"InstrumentUnit ownership unknown for {case.name}; no rows deleted")
        events = {
            int(row["event_id"]): (
                str(row["stage"]),
                str(row["level"]),
                str(row["path"]) if row["path"] is not None else None,
                int(row["frame_id"]) if row["frame_id"] is not None else None,
            )
            for row in snapshot["events"]
        }
        if (
            observations != case.captured_observations
            or frames != case.captured_frames
            or locations != case.captured_locations
            or units != case.captured_units
            or events != case.captured_events
            or {int(row["frame_id"]) for row in snapshot["frames_by_observation"]}
            != set(case.captured_frames)
            or {int(row["frame_id"]) for row in snapshot["frames_by_unit"]}
            != {
                int(row["frame_id"])
                for row in getattr(case, "preflight_unit_frame_refs", [])
            }
            | set(case.captured_frames)
            or (
                hasattr(case, "captured_unit_rows")
                and case.captured_unit_rows != unit_rows
            )
            or snapshot["memberships"]
        ):
            raise AssertionError(f"Unexpected synthetic row/dependency for {case.name}; no rows deleted")
        for row in snapshot["events"]:
            captured_event = case.captured_events.get(int(row["event_id"]))
            path = (row["details"] or {}).get("path")
            frame_id = int(row["frame_id"]) if row["frame_id"] is not None else None
            if (
                captured_event
                != (
                    str(row["stage"]),
                    str(row["level"]),
                    str(path) if path is not None else None,
                    frame_id,
                )
                or not (
                    path == str(case.product_path)
                    or frame_id in case.captured_frames
                )
            ):
                raise AssertionError(f"Cleanup event identity mismatch for {case.name}; no rows deleted")

    def _assert_foreign_key_dependencies_are_exact(
        self, session: Session, *, cases=None, preflight: bool = False
    ) -> int:
        cases = self.cases if cases is None else cases
        frame_ids = sorted({key for case in cases for key in case.captured_frames})
        frame_uids = sorted(
            {value[0] for case in cases for value in case.captured_frames.values()}
        )
        observation_ids = sorted(
            {key for case in cases for key in case.captured_observations}
        )
        observation_uids = sorted(
            {value[0] for case in cases for value in case.captured_observations.values()}
        )
        unit_ids = sorted({key for case in cases for key in case.captured_units})
        unit_uids = sorted(
            {value[0] for case in cases for value in case.captured_units.values()}
        )
        fk_rows = session.execute(
            text(
                """
                SELECT child_ns.nspname AS child_schema, child.relname AS child_table,
                       c.conname AS constraint_name,
                       array_agg(child_att.attname ORDER BY key_pair.ordinality) AS child_columns,
                       parent.relname AS parent_table,
                       array_agg(parent_att.attname ORDER BY key_pair.ordinality) AS parent_columns
                FROM pg_constraint c
                JOIN pg_class child ON child.oid = c.conrelid
                JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
                JOIN pg_class parent ON parent.oid = c.confrelid
                JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY
                     AS key_pair(child_attnum, parent_attnum, ordinality) ON TRUE
                JOIN pg_attribute child_att
                  ON child_att.attrelid = child.oid AND child_att.attnum = key_pair.child_attnum
                JOIN pg_attribute parent_att
                  ON parent_att.attrelid = parent.oid AND parent_att.attnum = key_pair.parent_attnum
                WHERE c.contype = 'f' AND parent_ns.nspname = 'public'
                  AND parent.relname IN ('frames', 'observations', 'instrument_units')
                GROUP BY child_ns.nspname, child.relname, c.conname, parent.relname
                ORDER BY child_ns.nspname, child.relname, c.conname
                """
            )
        ).mappings().all()
        connection = session.connection()
        metadata = MetaData()
        checked = 0
        values_by_parent = {
            ("frames", "frame_id"): frame_ids,
            ("frames", "frame_uid"): frame_uids,
            ("observations", "observation_id"): observation_ids,
            ("observations", "observation_uid"): observation_uids,
            ("instrument_units", "instrument_unit_id"): unit_ids,
            ("instrument_units", "unit_uid"): unit_uids,
        }
        for fk in fk_rows:
            table_name = str(fk["child_table"])
            schema_name = str(fk["child_schema"])
            child_table = Table(
                table_name,
                metadata,
                schema=schema_name,
                autoload_with=connection,
            )
            child_columns = list(fk["child_columns"])
            parent_columns = list(fk["parent_columns"])
            for child_column, parent_column in zip(child_columns, parent_columns, strict=True):
                values = values_by_parent.get((str(fk["parent_table"]), str(parent_column)), [])
                if not values:
                    continue
                count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(child_table)
                        .where(child_table.c[str(child_column)].in_(values))
                    )
                )
                dependency_key = (
                    schema_name,
                    table_name,
                    str(fk["constraint_name"]),
                    str(child_column),
                    str(parent_column),
                )
                if preflight:
                    for case in cases:
                        case.preflight_unit_fk_counts[dependency_key] = count
                    checked += 1
                    continue
                expected = (
                    sum(
                        case.preflight_unit_fk_counts.get(dependency_key, 0)
                        for case in cases
                    )
                    if fk["parent_table"] == "instrument_units"
                    else 0
                )
                if (
                    schema_name == "public"
                    and table_name == "frame_locations"
                    and fk["parent_table"] == "frames"
                    and parent_column == "frame_id"
                    and child_column == "frame_id"
                ):
                    expected += sum(len(case.captured_locations) for case in cases)
                elif (
                    schema_name == "public"
                    and table_name == "frames"
                    and fk["parent_table"] == "observations"
                    and parent_column in {"observation_id", "observation_uid"}
                ):
                    expected += sum(len(case.captured_frames) for case in cases)
                elif (
                    schema_name == "public"
                    and table_name == "frames"
                    and fk["parent_table"] == "instrument_units"
                    and parent_column in {"instrument_unit_id", "unit_uid"}
                ):
                    expected += sum(len(case.captured_frames) for case in cases)
                if count != expected:
                    raise AssertionError(
                        "Scoped cleanup FK preflight found an unexpected direct reference; "
                        "no rows were deleted"
                    )
                checked += 1
        return checked

    def cleanup(self) -> None:
        if self.cleanup_started:
            raise AssertionError("Scoped cleanup was already attempted; no retry is permitted")
        self.cleanup_started = True
        if self.safety_stop:
            raise AssertionError(
                "Scoped cleanup not attempted after a PostgreSQL guard/setup/teardown/rollback failure"
            )
        if not self.cases:
            self.cleanup_verified = True
            return
        with self.session() as (session, cleanup_pid):
            snapshots = [(case, self.snapshot(session, case)) for case in self.cases]
            for case, snapshot in snapshots:
                self._assert_snapshot_matches_allowlist(case, snapshot)
                for row in snapshot["frames_by_observation"]:
                    if int(row["frame_id"]) not in case.captured_frames:
                        raise AssertionError(
                            f"Observation has an unexpected Frame reference for {case.name}; no rows were deleted"
                        )
                for row in snapshot["frames_by_unit"]:
                    if int(row["frame_id"]) not in (
                        {
                            int(ref["frame_id"])
                            for ref in case.preflight_unit_frame_refs
                        }
                        | set(case.captured_frames)
                    ):
                        raise AssertionError(
                            f"InstrumentUnit has an unexpected Frame reference for {case.name}; no rows were deleted"
                        )
            fk_count = self._assert_foreign_key_dependencies_are_exact(session)

            for case in self.cases:
                for event_id, (stage, level, path, frame_id) in case.captured_events.items():
                    result = session.execute(
                        text(
                            """
                            DELETE FROM public.processing_events
                            WHERE event_id = :event_id
                              AND frame_id IS NOT DISTINCT FROM CAST(:frame_id AS bigint)
                              AND stage = :stage AND level::text = :level
                              AND details->>'path' IS NOT DISTINCT FROM CAST(:product_path AS text)
                            """
                        ),
                        {
                            "event_id": event_id,
                            "frame_id": frame_id,
                            "stage": stage,
                            "level": level,
                            "product_path": path,
                        },
                    )
                    if result.rowcount != 1:
                        raise AssertionError(
                            f"Exact captured processing event was not deleted once for {case.name}"
                        )
            for case in self.cases:
                for location_id, (frame_id, locator, locator_uid) in case.captured_locations.items():
                    result = session.execute(
                        text(
                            """
                            DELETE FROM public.frame_locations
                            WHERE frame_location_id = :location_id AND frame_id = :frame_id
                              AND locator = :locator AND locator_uid = :locator_uid
                            """
                        ),
                        {
                            "location_id": location_id,
                            "frame_id": frame_id,
                            "locator": locator,
                            "locator_uid": locator_uid,
                        },
                    )
                    if result.rowcount != 1:
                        raise AssertionError(
                            f"Exact captured FrameLocation was not deleted once for {case.name}"
                        )
            for case in self.cases:
                for frame_id, (frame_uid, observation_id, unit_id) in case.captured_frames.items():
                    result = session.execute(
                        text(
                            """
                            DELETE FROM public.frames
                            WHERE frame_id = :frame_id AND frame_uid = :frame_uid
                              AND observation_id = :observation_id
                              AND instrument_unit_id = :unit_id
                              AND instrument = 'MASTER'
                              AND header_json->>'UNIQNUM' = :native_identity
                            """
                        ),
                        {
                            "frame_id": frame_id,
                            "frame_uid": frame_uid,
                            "observation_id": observation_id,
                            "unit_id": unit_id,
                            "native_identity": case.native_identity,
                        },
                    )
                    if result.rowcount != 1:
                        raise AssertionError(
                            f"Exact captured Frame was not deleted once for {case.name}"
                        )
            for case in self.cases:
                for observation_id, (observation_uid, scheme, native) in case.captured_observations.items():
                    result = session.execute(
                        text(
                            """
                            DELETE FROM public.observations AS o
                            WHERE o.observation_id = :observation_id
                              AND o.observation_uid = :observation_uid
                              AND o.identity_scheme = :identity_scheme
                              AND o.native_identity = :native_identity
                              AND NOT EXISTS (
                                  SELECT 1 FROM public.frames AS fr
                                  WHERE fr.observation_id = o.observation_id
                              )
                            """
                        ),
                        {
                            "observation_id": observation_id,
                            "observation_uid": observation_uid,
                            "identity_scheme": scheme,
                            "native_identity": native,
                        },
                    )
                    if result.rowcount != 1:
                        raise AssertionError(
                            f"Exact captured Observation was not deleted once for {case.name}"
                        )
            for case in self.cases:
                if case.preflight_unit_snapshot is not None:
                    if case.unit_ownership != "PREEXISTING":
                        raise AssertionError(
                            f"Preexisting InstrumentUnit ownership changed for {case.name}; no delete"
                        )
                    continue
                for unit_id, unit_row in case.captured_unit_rows.items():
                    if (
                        case.unit_ownership != "NEW"
                        or case.unit_insert_witness is None
                        or _instrument_unit_witness(unit_row) != case.unit_insert_witness
                    ):
                        raise AssertionError(
                            f"InstrumentUnit ownership is unknown for {case.name}; no delete"
                        )
                    expected_fk_counts = dict(case.preflight_unit_fk_counts)
                    self._assert_foreign_key_dependencies_are_exact(
                        session, cases=[case], preflight=True
                    )
                    current_fk_counts = dict(case.preflight_unit_fk_counts)
                    case.preflight_unit_fk_counts = expected_fk_counts
                    if any(current_fk_counts.values()):
                        raise AssertionError(
                            f"New InstrumentUnit still has direct FK dependencies for {case.name}"
                        )
                    unit_table = InstrumentUnit.__table__
                    exact_fields = [unit_table.c[name] == value for name, value in unit_row.items()]
                    result = session.execute(
                        delete(InstrumentUnit).where(
                            and_(
                                *exact_fields,
                                ~select(Frame.frame_id)
                                .where(Frame.instrument_unit_id == unit_id)
                                .exists(),
                            )
                        )
                    )
                    if result.rowcount != 1:
                        raise AssertionError(
                            f"Exact captured new InstrumentUnit was not deleted once for {case.name}"
                        )
            session.commit()

        with self.session(read_only=True) as (session, verification_pid):
            if verification_pid == cleanup_pid:
                raise AssertionError("Cleanup verification did not use a fresh PostgreSQL backend")
            for case in self.cases:
                post_snapshot = self.snapshot(session, case)
                if any(
                    post_snapshot[name]
                    for name in ("observations", "frames", "locations", "events", "memberships")
                ):
                    raise AssertionError(
                        f"Fresh-backend scoped cleanup verification found rows for {case.name}"
                    )
                direct_rows = session.execute(
                    text(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM public.processing_events
                             WHERE frame_id = ANY(CAST(:frame_ids AS bigint[]))) AS events,
                            (SELECT COUNT(*) FROM public.field_series_frames
                             WHERE frame_id = ANY(CAST(:frame_ids AS bigint[]))) AS memberships
                        """
                    ),
                    {"frame_ids": sorted(case.captured_frames)},
                ).mappings().one()
                if any(int(value) != 0 for value in direct_rows.values()):
                    raise AssertionError(
                        f"Fresh-backend scoped cleanup verification found rows for {case.name}"
                    )
                unit_rows = {
                    int(row["instrument_unit_id"]): _instrument_unit_snapshot(row)
                    for row in post_snapshot["units"]
                }
                if case.preflight_unit_snapshot is not None:
                    expected_units = {
                        int(case.preflight_unit_snapshot["instrument_unit_id"]):
                        case.preflight_unit_snapshot
                    }
                    if unit_rows != expected_units:
                        raise AssertionError(
                            f"Fresh-backend preexisting InstrumentUnit changed for {case.name}"
                        )
                    if post_snapshot["frames_by_unit"] != case.preflight_unit_frame_refs:
                        raise AssertionError(
                            f"Fresh-backend preexisting Unit Frame dependencies changed for {case.name}"
                        )
                    expected_fk_counts = dict(case.preflight_unit_fk_counts)
                elif unit_rows or post_snapshot["frames_by_unit"]:
                    raise AssertionError(
                        f"Fresh-backend owned InstrumentUnit remains for {case.name}"
                    )
                else:
                    expected_fk_counts = {}
                self._assert_foreign_key_dependencies_are_exact(
                    session, cases=[case], preflight=True
                )
                actual_fk_counts = dict(case.preflight_unit_fk_counts)
                case.preflight_unit_fk_counts = expected_fk_counts
                if (
                    actual_fk_counts != expected_fk_counts
                    if case.preflight_unit_snapshot is not None
                    else any(actual_fk_counts.values())
                ):
                    raise AssertionError(
                        f"Fresh-backend InstrumentUnit dependency snapshot changed for {case.name}"
                    )
        self.cleanup_verified = True
        print(
            "PASS DURABLE_SYNTHETIC_CLEANUP "
            f"cases={len(self.cases)} captured_error_events="
            f"{sum(len(case.captured_events) for case in self.cases)} "
            f"foreign_keys_preflighted={fk_count} cleanup_backend_pid={cleanup_pid} "
            f"verification_backend_pid={verification_pid} owned_rows_remaining=0 "
            f"preexisting_units_preserved={sum(case.preflight_unit_snapshot is not None for case in self.cases)}"
        )


@pytest.fixture()
def durable_persist_only_postgres_harness(pytestconfig, tmp_path: Path):
    config_path = pytestconfig.getoption("--grandgousier-test-config")
    if not config_path:
        pytest.skip(
            "Durable PostgreSQL tests require --grandgousier-test-config; "
            "no database access attempted"
        )
    raw_url = _postgres_integration_raw_url(pytestconfig)
    if raw_url is None:
        raise AssertionError("Explicit durable PostgreSQL test config returned no database URL")
    try:
        database_url = make_url(raw_url)
        url_matches_target = (
            database_url.get_backend_name() == "postgresql"
            and database_url.host == _POSTGRES_IT_HOST
            and database_url.port == _POSTGRES_IT_PORT
            and database_url.database == _POSTGRES_IT_DATABASE_NAME
            and database_url.username == _POSTGRES_IT_ROLE
            and not database_url.query
        )
    except Exception:
        raise AssertionError(
            "Durable PostgreSQL test URL is malformed or unsupported; URL is redacted"
        ) from None
    if not url_matches_target:
        raise AssertionError(
            "Durable PostgreSQL test URL failed the explicit target pre-pin; no connection attempted"
        )
    try:
        engine = create_engine(database_url, echo=False, poolclass=NullPool)
    except Exception:
        raise AssertionError(
            "Durable PostgreSQL engine setup failed; connection details are redacted"
        ) from None
    harness = _DurablePersistOnlyPostgresHarness(engine)
    try:
        yield harness
    finally:
        try:
            if harness.safety_stop:
                raise AssertionError(
                    "Durable PostgreSQL fixture STOP: guard/setup/teardown/rollback failed; "
                    "cleanup was not retried"
                )
            harness.cleanup()
        finally:
            engine.dispose()


def test_durable_postgres_harness_skips_before_config_path_or_engine_access(
    monkeypatch, tmp_path: Path,
) -> None:
    pytestconfig = _FakePytestConfig()
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("access")
        raise AssertionError(
            "durable PostgreSQL setup must be skipped before config or engine access"
        )

    monkeypatch.setattr(app_config, "read_config_file", forbidden)
    monkeypatch.setattr(app_settings, "get_settings", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "create_engine", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "_postgres_integration_raw_url", forbidden)
    harness_fixture = durable_persist_only_postgres_harness.__wrapped__
    with pytest.raises(pytest.skip.Exception, match="--grandgousier-test-config"):
        next(harness_fixture(pytestconfig, tmp_path))
    assert pytestconfig.option_names == ["--grandgousier-test-config"]
    assert calls == []


def test_durable_postgres_harness_rejects_wrong_target_before_engine(
    monkeypatch, tmp_path: Path,
) -> None:
    wrong_url = (
        f"postgresql://{_POSTGRES_IT_ROLE}:fake-target-secret@{_POSTGRES_IT_HOST}:"
        f"{_POSTGRES_IT_PORT}/wrong_test_database"
    )
    config_path = _write_postgres_test_config(tmp_path, wrong_url)
    pytestconfig = _FakePytestConfig(config_path)
    engine_calls = []

    def forbidden_engine(*_args, **_kwargs):
        engine_calls.append(True)
        raise AssertionError("durable target pre-pin must precede engine creation")

    monkeypatch.setattr(sys.modules[__name__], "create_engine", forbidden_engine)
    harness_fixture = durable_persist_only_postgres_harness.__wrapped__
    with pytest.raises(AssertionError, match="explicit target pre-pin") as exc:
        next(harness_fixture(pytestconfig, tmp_path))
    assert exc.value.__cause__ is None
    assert engine_calls == []
    assert pytestconfig.option_names == [
        "--grandgousier-test-config",
        "--grandgousier-test-config",
    ]


def _make_durable_synthetic_stage1_product(
    case: _DurablePersistOnlyCase, tmp_path: Path
) -> tuple[Path, Any]:
    assets_root = tmp_path / f"{case.name}-assets-{case.source_id.rsplit('-', 1)[-1]}"
    assets_root.mkdir(parents=True, exist_ok=False)
    raw_path = assets_root / "synthetic-raw.fit"
    header_path = assets_root / "synthetic-processing-header.fits"
    dark_path = assets_root / "synthetic-dark.fit"
    flat_path = assets_root / "synthetic-flat.fit"
    camera_number = int(case.camera)
    date_text = case.date_obs.isoformat().replace("+00:00", "Z")
    object_header = {
        "ID_CAM": camera_number,
        "XBINNING": 1,
        "YBINNING": 1,
        "FILTER": "V",
        "EXPTIME": 50.0,
    }
    raw_header = fits.Header(
        {
            **object_header,
            "IMAGETYP": "LIGHT",
            "UNIQNUM": case.native_identity,
            "DATE-OBS": date_text,
            "OBJECT": case.object_name,
            "CCD-TEMP": -9.0,
        }
    )
    dark_header = fits.Header(
        {
            **object_header,
            "IMAGETYP": "SDARK",
            "EXPTIME": 50.0,
            "CCD-TEMP": -10.0,
        }
    )
    flat_header = fits.Header(
        {
            **object_header,
            "IMAGETYP": "SFLAT",
            "EXPTIME": 5.0,
        }
    )
    fits.PrimaryHDU(data=np.full((16, 16), 100, dtype=np.uint16), header=raw_header).writeto(
        raw_path
    )
    fits.PrimaryHDU(data=np.full((16, 16), 10, dtype=np.uint16), header=dark_header).writeto(
        dark_path
    )
    fits.PrimaryHDU(data=np.ones((16, 16), dtype=np.float32), header=flat_header).writeto(
        flat_path
    )
    processing_header = fits.Header(
        {
            "ID_CAM": camera_number,
            "XBINNING": 1,
            "YBINNING": 1,
            "FILTER": "V",
            "EXPTIME": 50.0,
            "IMAGEW": 16,
            "IMAGEH": 16,
            "CTYPE1": "RA---TAN",
            "CTYPE2": "DEC--TAN",
            "CRVAL1": 327.52,
            "CRVAL2": 72.18,
            "CRPIX1": 8.5,
            "CRPIX2": 8.5,
            "CD1_1": -0.0005,
            "CD1_2": 0.0,
            "CD2_1": 0.0,
            "CD2_2": 0.0005,
            "DARK": case.dark_remote_path,
            "FLAT": case.flat_remote_path,
            "OBSERVAT": "SYNTHETIC",
            "AXIS": "EAST",
            "OBJECT": case.object_name,
        }
    )
    fits.PrimaryHDU(header=processing_header).writeto(header_path)

    local_paths = {
        "science_raw": raw_path,
        "processing_header": header_path,
        "dark": dark_path,
        "flat": flat_path,
    }
    source = ArchiveSourceDescriptor(
        source_id=case.source_id,
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="master-physical",
    )
    product_store = FilesystemProductStore(
        root=case.product_root,
        store_id=case.product_store_id,
        disk_reserve_bytes=0,
    )
    execution_report = tmp_path / f"{case.name}-{case.source_id.rsplit('-', 1)[-1]}-execution.json"
    execution = execute_stage1_stream(
        request=ArchiveRequest(source_id=case.source_id, limit=1),
        source=source,
        metadata_provider=_RecordProvider([case.record]),
        asset_provider=_SyntheticStage1AssetProvider(local_paths),
        product_store=product_store,
        batch_size=1,
        plan_json_path=execution_report.with_name(execution_report.stem + "-plan.json"),
        report_json_path=execution_report,
    )
    assert execution.status == "READY"
    assert execution.planned_items == 1
    assert execution.counts == {"REDUCED": 1}
    product = load_validated_stage1_products(execution_report)[0]
    assert product.product_path == case.product_path
    assert product.native_observation_id == case.native_identity
    assert product.raw_relative_physical_key == case.raw_key
    product_stat = product.product_path.stat()
    case.expected_frame_uid = make_frame_uid(
        product.product_path, product_stat.st_size, product_stat.st_mtime_ns
    )
    with fits.open(product.product_path, memmap=False) as hdul:
        assert len(hdul) >= 2
        header = hdul[0].header
        assert header["UNIQNUM"] == case.native_identity
        assert header["GGSTAGE"] == "1B"
        assert header["GGMSMETA"] is True
        assert header["GGRAW"] == case.raw_remote_path
        assert header["DATE-OBS"] == date_text
        assert header["ID_CAM"] == camera_number
        assert header["IMAGETYP"] == "LIGHT"
        assert header["CRVAL1"] == pytest.approx(327.52)
        assert header["CRVAL2"] == pytest.approx(72.18)
        assert hdul[0].data is not None
        assert hdul[0].data.shape == (16, 16)
    return execution_report, product


@dataclass(frozen=True, slots=True)
class _PinnedDurableStage1Artifact:
    case: _DurablePersistOnlyCase
    product: Any
    execution_report_path: Path
    frame_uid: str
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, Any]


def _pinned_manifest_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def _pinned_file_record(path: Path, *, product: bool = False) -> dict[str, Any]:
    stat = path.stat()
    uid = {"make_frame_uid": make_frame_uid(path, stat.st_size, stat.st_mtime_ns)} if product else {}
    return {"original_path": str(path), "path_posix": path.as_posix(), "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": _sha(path), **uid}


def _pinned_case_payload(case: _DurablePersistOnlyCase) -> dict[str, Any]:
    return {
        "case_id": _PINNED_DURABLE_CASE_ID, "source_id": case.source_id,
        "raw_id": case.raw_id, "raw_key": case.raw_key, "identity_scheme": "master_uniqnum_v1",
        "uniqnum": case.native_identity, "source_observation_uid": case.source_observation_uid,
        "observation_uid": case.observation_uid,
        "physical_metadata": {name: getattr(case.record, name) for name in (
            "instrument", "site", "tube", "camera", "filter_name", "object_name",
            "observed_at", "exptime", "ra_deg", "dec_deg")},
        "expected_locator": str(case.product_path),
        "expected_locator_posix": case.product_path.as_posix(),
        "frame_location_uid": case.frame_location_uid,
        "instrument_unit_uid": case.instrument_unit_uid,
        "dimensions": [16, 16],
        "image_type": FrameType.SCIENCE.name,
    }


def _pinned_identities(case: _DurablePersistOnlyCase, product) -> dict[str, Any]:
    identity = product.to_dict()
    return {
        "product": identity,
        "report": {"schema": STAGE1_EXECUTION_SCHEMA, "source_id": case.source_id,
                   "status": "READY", "item": identity},
        "provenance": {"schema": STAGE1_PROVENANCE_SCHEMA, "source_id": case.source_id,
                       "fingerprint": product.fingerprint, "identity": identity},
    }


def _trusted_pinned_manifest_sha256(value: str | None = None) -> str:
    digest = os.environ.get(_PINNED_DURABLE_MANIFEST_ENV) if value is None else value
    if not isinstance(digest, str) or len(digest) != 64 or set(digest) - set("0123456789abcdef"):
        raise ValueError("A trusted lowercase 64-character manifest SHA-256 is required")
    return digest


def _pinned_path_resolves_under_root(path: Path, root: Path, root_resolved: Path) -> bool:
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir()
            or not path.is_absolute() or path.is_symlink()):
        return False
    try:
        if root.resolve(strict=True) != root_resolved:
            return False
        relative = path.relative_to(root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            return False
        if not path.resolve(strict=True).is_relative_to(root_resolved):
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    current = Path(root.anchor)
    for part in root.parts[1:] + relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _prepare_pinned_durable_stage1_artifact(
    *, case_id: str, artifact_root: Path
) -> _PinnedDurableStage1Artifact:
    if (case_id, Path(artifact_root)) != (_PINNED_DURABLE_CASE_ID, _PINNED_DURABLE_ROOT):
        raise ValueError("Pinned durable artifact identity/root mismatch")
    if artifact_root.exists() or artifact_root.is_symlink():
        raise FileExistsError("Pinned durable artifact root already exists; no overwrite permitted")
    artifact_root.mkdir(exist_ok=False)
    case = _DurablePersistOnlyPostgresHarness(None).new_case(
        artifact_root, case_id, 1, pinned_identity=True
    )
    if (case.native_identity, case.raw_id) != ("204_2026102701", 2026102701):
        raise AssertionError("Pinned physical identity does not match the accepted case")
    report_path, product = _make_durable_synthetic_stage1_product(case, artifact_root)
    manifest_path = artifact_root / f"{case_id}.manifest.json"
    payload = {
        "schema": "grandgousier.validation-e-pinned-stage1.v1",
        "artifact_root": str(_PINNED_DURABLE_ROOT.resolve(strict=True)),
        "created_at_utc": datetime.now(UTC).isoformat(),
        "case": _pinned_case_payload(case),
        "identities": _pinned_identities(case, product),
        "files": {name: _pinned_file_record(path, product=name == "product") for name, path in (
            ("product", product.product_path), ("execution_report", report_path),
            ("provenance", product.provenance_path))},
    }
    manifest_bytes = _pinned_manifest_bytes(payload)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    with manifest_path.open("xb") as stream:
        stream.write(manifest_bytes)
    with (artifact_root / f"{case_id}.manifest.sha256").open(
        "x", encoding="ascii", newline="\n"
    ) as stream:
        stream.write(manifest_sha256 + "\n")
    return _load_and_verify_pinned_case_manifest(artifact_root, manifest_sha256)


def _load_and_verify_pinned_case_manifest(
    artifact_root: Path, trusted_manifest_sha256: str | None = None
) -> _PinnedDurableStage1Artifact:
    trusted = _trusted_pinned_manifest_sha256(trusted_manifest_sha256)
    root = Path(artifact_root)
    if (root != _PINNED_DURABLE_ROOT or not root.is_absolute()
            or root.is_symlink() or not root.is_dir()):
        raise ValueError("Pinned durable artifact root is missing or unsafe")
    root_resolved = root.resolve(strict=True)
    expected_root_resolved = _PINNED_DURABLE_ROOT.resolve(strict=True)
    if root_resolved != expected_root_resolved:
        raise ValueError("Pinned durable artifact root resolved outside the fixed root")
    manifest_path = root / f"{_PINNED_DURABLE_CASE_ID}.manifest.json"
    if (not manifest_path.is_file()
            or not _pinned_path_resolves_under_root(manifest_path, root, expected_root_resolved)):
        raise ValueError("Pinned manifest path is missing or unsafe")
    manifest_bytes = manifest_path.read_bytes()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if digest != trusted:
        raise ValueError("Pinned manifest digest is not the trusted cross-phase digest")
    payload = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or "artifact_root" not in payload:
        raise ValueError("Pinned manifest artifact root is missing")
    manifest_root = payload["artifact_root"]
    if not isinstance(manifest_root, str) or manifest_root != str(root_resolved):
        raise ValueError("Pinned manifest artifact root does not match the fixed root")
    if payload.get("schema") != "grandgousier.validation-e-pinned-stage1.v1":
        raise ValueError("Pinned manifest schema mismatch")
    if not isinstance(payload.get("created_at_utc"), str) or not payload["created_at_utc"]:
        raise ValueError("Pinned manifest creation timestamp is missing or invalid")
    case = _DurablePersistOnlyPostgresHarness(None).new_case(
        artifact_root, _PINNED_DURABLE_CASE_ID, 1, pinned_identity=True)
    report_path = root / f"{case.name}-{case.source_id.rsplit('-', 1)[-1]}-execution.json"
    provenance_path = case.product_path.with_name(case.product_path.name + ".provenance.json")
    for name, path in (("product", case.product_path), ("provenance", provenance_path),
                       ("execution report", report_path)):
        if not _pinned_path_resolves_under_root(path, root, expected_root_resolved):
            raise ValueError(f"Pinned {name} path is missing or unsafe")
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_items = report_payload.get("items") if isinstance(report_payload, dict) else None
    if not isinstance(report_items, list):
        raise TypeError("Pinned execution report item list is missing or invalid")
    for row in report_items:
        if not isinstance(row, dict):
            raise TypeError("Pinned execution report item is invalid")
        for field_name, expected_path in (("product_path", case.product_path),
                                          ("provenance_path", provenance_path)):
            value = row.get(field_name)
            if not isinstance(value, str):
                raise TypeError(f"Pinned execution report {field_name} is missing or invalid")
            reported_path = Path(value)
            if (reported_path != expected_path
                    or not _pinned_path_resolves_under_root(
                        reported_path, root, expected_root_resolved)):
                raise ValueError(f"Pinned execution report {field_name} is unsafe")
    products = load_validated_stage1_products(report_path)
    if len(products) != 1:
        raise ValueError("Pinned report must contain exactly one validated product")
    product = products[0]
    actual = (product.product_path, product.provenance_path, product.native_observation_id,
              product.source_observation_uid, product.observation_uid, product.identity_scheme,
              product.instrument, product.camera, product.date_obs, product.raw_relative_physical_key)
    expected = (case.product_path, case.product_path.with_name(case.product_path.name + ".provenance.json"),
                case.native_identity, case.source_observation_uid, case.observation_uid,
                "master_uniqnum_v1", "MASTER", case.camera, case.date_obs, case.raw_key)
    if actual != expected:
        raise ValueError("Pinned report/provenance does not match the exact case identity")
    with fits.open(product.product_path, memmap=False) as hdul:
        dimensions = list(hdul[0].data.shape) if hdul[0].data is not None else None
    if dimensions != [16, 16]:
        raise ValueError("Pinned product dimensions mismatch")
    frame_uid = payload["files"]["product"]["make_frame_uid"]
    case.expected_frame_uid = frame_uid
    artifact = _PinnedDurableStage1Artifact(
        case, product, report_path, frame_uid, manifest_path, digest, payload)
    _verify_pinned_artifact_inputs(artifact, trusted_manifest_sha256=trusted)
    return artifact


def _verify_pinned_artifact_inputs(
    artifact: _PinnedDurableStage1Artifact, *, trusted_manifest_sha256: str | None = None
) -> None:
    trusted = _trusted_pinned_manifest_sha256(trusted_manifest_sha256)
    root = _PINNED_DURABLE_ROOT
    manifest_path = root / f"{_PINNED_DURABLE_CASE_ID}.manifest.json"
    root_resolved = root.resolve(strict=True)
    if (artifact.manifest_path != manifest_path or not manifest_path.is_file()
            or not _pinned_path_resolves_under_root(manifest_path, root, root_resolved)):
        raise ValueError("Pinned manifest path is missing or unsafe")
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    sidecar = root / f"{_PINNED_DURABLE_CASE_ID}.manifest.sha256"
    if (digest != artifact.manifest_sha256 or digest != trusted
            or _pinned_manifest_bytes(artifact.manifest) != raw
            or sidecar.is_symlink() or not sidecar.is_file()
            or sidecar.read_text(encoding="ascii") != digest + "\n"):
        raise ValueError("Pinned manifest changed or lacks the trusted cross-phase digest")
    if ("artifact_root" not in artifact.manifest
            or not isinstance(artifact.manifest["artifact_root"], str)
            or artifact.manifest["artifact_root"] != str(root_resolved)):
        raise ValueError("Pinned manifest artifact root changed or is invalid")
    case = artifact.case
    provenance_path = case.product_path.with_name(case.product_path.name + ".provenance.json")
    report_path = root / f"{case.name}-{case.source_id.rsplit('-', 1)[-1]}-execution.json"
    if artifact.execution_report_path != report_path:
        raise ValueError("Pinned execution report path changed")
    if (artifact.manifest.get("case") != _pinned_case_payload(case)
            or artifact.manifest.get("identities") != _pinned_identities(case, artifact.product)
            or (artifact.product.product_path, artifact.product.provenance_path)
            != (case.product_path, provenance_path)):
        raise ValueError("Pinned manifest identities changed")
    paths = {"product": case.product_path, "execution_report": report_path,
             "provenance": provenance_path}
    files = artifact.manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(paths):
        raise ValueError("Pinned manifest file inventory changed")
    for name, path in paths.items():
        entry = files[name]
        if (not isinstance(entry, dict) or not path.is_file()
                or not _pinned_path_resolves_under_root(path, root, root_resolved)):
            raise ValueError(f"Pinned {name} path is missing or unsafe")
        current = _pinned_file_record(path, product=name == "product")
        if entry != current:
            raise ValueError(f"Pinned {name} physical file changed")
        if name == "product" and artifact.frame_uid != current["make_frame_uid"]:
            raise ValueError("Pinned production make_frame_uid changed")


def _safe_persist_only_frame_ids(report) -> list[int]:
    if report.status != "READY":
        return []
    return [
        int(item.selected_frame_id)
        for item in report.items
        if item.status == "PERSISTED_IDENTITY_ONLY" and item.selected_frame_id is not None
    ]


def _assert_complete_durable_handoff(
    case: _DurablePersistOnlyCase,
    product,
    snapshot: dict[str, list[dict[str, Any]]],
) -> int:
    assert len(snapshot["observations"]) == 1
    assert len(snapshot["frames"]) == 1
    assert len(snapshot["locations"]) == 1
    assert len(snapshot["units"]) == 1
    assert snapshot["events"] == []
    assert snapshot["memberships"] == []
    observation = snapshot["observations"][0]
    frame = snapshot["frames"][0]
    location = snapshot["locations"][0]
    unit = snapshot["units"][0]
    if getattr(case, "expected_frame_uid", None) is not None:
        assert frame["frame_uid"] == case.expected_frame_uid
    assert observation["identity_scheme"] == "master_uniqnum_v1"
    assert observation["native_identity"] == case.native_identity
    assert observation["observation_uid"] == case.observation_uid
    assert frame["observation_id"] == observation["observation_id"]
    assert frame["instrument_unit_id"] == unit["instrument_unit_id"]
    assert frame["instrument"] == "MASTER"
    assert frame["camera"] == case.camera
    assert frame["filter_name"] == "V"
    assert frame["image_type"] == FrameType.SCIENCE.name
    assert frame["date_obs"] is not None
    assert frame["ra_center_deg"] == pytest.approx(327.52)
    assert frame["dec_center_deg"] == pytest.approx(72.18)
    assert frame["header_json"]["UNIQNUM"] == case.native_identity
    assert frame["header_json"]["GGRAW"] == case.raw_remote_path
    assert frame["header_json"]["stage1_fingerprint"] == product.fingerprint
    assert frame["header_json"]["source_observation_uid"] == case.source_observation_uid
    assert frame["header_json"]["raw_relative_physical_key"] == case.raw_key
    assert location["frame_id"] == frame["frame_id"]
    assert location["locator"] == str(case.product_path)
    assert location["locator_uid"] == case.frame_location_uid
    assert location["is_primary"] is True
    assert location["content_sha256"] == product.product_checksum_sha256
    assert unit["unit_uid"] == case.instrument_unit_uid
    assert unit["instrument"] == "MASTER"
    assert unit["site"] == "SYNTHETIC"
    assert unit["tube"] == "EAST"
    assert unit["camera"] == case.camera
    assert unit["filter_name"] == "V"
    assert unit["observing_mode"] == case.object_name
    return int(frame["frame_id"])


def _run_pinned_durable_persist_only_success(
    harness: _DurablePersistOnlyPostgresHarness,
    artifact: _PinnedDurableStage1Artifact,
    monkeypatch,
) -> None:
    from grandgousier.archive import auto_process

    monkeypatch.setattr(
        auto_process,
        "process_archive_path",
        lambda *_args, **_kwargs: pytest.fail("Pinned persist-only handoff invoked Stage 2"),
    )
    case = artifact.case
    _verify_pinned_artifact_inputs(artifact)
    harness.preflight_case(case, allow_preexisting_unit=True)
    with harness.session(unit_commit_gate_case=case) as (session, write_pid):
        _verify_pinned_artifact_inputs(artifact)
        try:
            report = handoff_stage1_execution(
                session,
                execution_report_path=artifact.execution_report_path,
                persist_only=True,
            )
        finally:
            try:
                _verify_pinned_artifact_inputs(artifact)
            except Exception:
                harness.safety_stop = True
                raise
    with harness.session(read_only=True) as (session, read_pid):
        snapshot = harness.snapshot(session, case)
    harness.capture(case, snapshot)
    assert read_pid != write_pid
    assert (report.status, report.persistence_only, report.stage2_result) == ("READY", True, None)
    assert (report.items[0].p4_action, report.items[0].status) == (
        ObservationScienceAction.SELECTED.value,
        "PERSISTED_IDENTITY_ONLY",
    )
    frame_id = _assert_complete_durable_handoff(case, artifact.product, snapshot)
    assert snapshot["frames"][0]["frame_uid"] == artifact.frame_uid
    expected = artifact.manifest["case"]
    assert (
        snapshot["locations"][0]["locator"], snapshot["locations"][0]["locator_uid"],
        snapshot["units"][0]["unit_uid"], snapshot["frames"][0]["image_type"],
    ) == (
        expected["expected_locator"], expected["frame_location_uid"],
        expected["instrument_unit_uid"], expected["image_type"],
    )
    assert report.persistence_result["indexed_frame_ids"] == [frame_id]
    assert _safe_persist_only_frame_ids(report) == [frame_id]
    assert case.unit_ownership in {"NEW", "PREEXISTING"}


def test_durable_postgres_persist_only_success_only(
    durable_persist_only_postgres_harness: _DurablePersistOnlyPostgresHarness,
    monkeypatch,
) -> None:
    artifact = _load_and_verify_pinned_case_manifest(_PINNED_DURABLE_ROOT)
    _run_pinned_durable_persist_only_success(
        durable_persist_only_postgres_harness, artifact, monkeypatch
    )


def test_durable_unit_commit_gate_requires_exact_witness() -> None:
    case = _DurablePersistOnlyPostgresHarness(None).new_case(Path("C:/e-x-f"), "local-gate", 1)
    raw_config = InstrumentProfile.from_yaml(instrument_profile_path("master.yaml")).raw_config
    assert raw_config is not None
    assert None in raw_config["filter_aliases"]
    witness = _expected_instrument_unit_witness(case, 41)
    assert "null" not in raw_config["filter_aliases"]
    assert witness["config_json"]["filter_aliases"]["null"] == raw_config["filter_aliases"][None]
    insert_row = SimpleNamespace(**(witness | {"config_json": raw_config}))
    insert_witness = _instrument_unit_witness(insert_row)
    assert insert_witness == witness
    other = SimpleNamespace()
    with pytest.raises(AssertionError):
        _assert_unit_commit_witness(case, {})
    _assert_unit_commit_witness(case, {id(case): (case, insert_witness)})
    wrong_witness = dict(insert_witness)
    wrong_config = _jsonb_persisted_value(insert_witness["config_json"])
    wrong_config["filter_aliases"]["null"] = "unexpected"
    wrong_witness["config_json"] = wrong_config
    with pytest.raises(AssertionError):
        _assert_unit_commit_witness(case, {id(case): (case, wrong_witness)})
    case.unit_insert_witness, case.unit_ownership = insert_witness, "NEW"
    _assert_unit_commit_witness(case, {})
    case.unit_insert_witness = wrong_witness
    with pytest.raises(AssertionError):
        _assert_unit_commit_witness(case, {})
    case.unit_insert_witness, case.unit_ownership = None, "UNKNOWN"
    with pytest.raises(AssertionError):
        _assert_unit_commit_witness(case, {id(other): (other, insert_witness)})
    case.preflight_unit_snapshot = dict.fromkeys(_UNIT_ROW_COLUMNS) | insert_witness
    case.unit_ownership = "PREEXISTING"
    _assert_unit_commit_witness(case, {})


def test_production_make_frame_uid_exact_pin() -> None:
    assert make_frame_uid(
        Path("C:/pinned-stage1/product.fits"), 123456, 1700000000000000000
    ) == "4b6d3f9ee67ac78ac53b02cf664e223c"


def test_durable_postgres_persist_only_partial_failure_matrix(
    durable_persist_only_postgres_harness: _DurablePersistOnlyPostgresHarness,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A-H exercise actual per-file and evidence commits across fresh PG backends."""
    from grandgousier.archive import auto_process, stage1_handoff as handoff_module
    from grandgousier.io import archive_indexer

    harness = durable_persist_only_postgres_harness
    stage2_calls = []

    def forbidden_stage2(*_args, **_kwargs):
        stage2_calls.append(True)
        raise AssertionError("Persist-only crash tests must not invoke Stage 2")

    monkeypatch.setattr(auto_process, "process_archive_path", forbidden_stage2)

    # A: normal production-path persist-only baseline.
    case_a = harness.new_case(tmp_path, "A-success", 1)
    report_a_path, product_a = _make_durable_synthetic_stage1_product(case_a, tmp_path)
    harness.preflight_absent(case_a)
    with harness.session() as (session, write_pid_a):
        report_a = handoff_stage1_execution(
            session, execution_report_path=report_a_path, persist_only=True
        )
    with harness.session() as (session, read_pid_a):
        snapshot_a = harness.snapshot(session, case_a)
    harness.capture(case_a, snapshot_a)
    assert report_a.status == "READY"
    assert report_a.persistence_only is True
    assert report_a.stage2_result is None
    assert report_a.persistence_result["status"] == "PERSISTED_IDENTITY_ONLY"
    assert report_a.items[0].p4_action == ObservationScienceAction.SELECTED.value
    assert report_a.items[0].status == "PERSISTED_IDENTITY_ONLY"
    assert report_a.items[0].selected_frame_id is not None
    assert read_pid_a != write_pid_a
    frame_id_a = _assert_complete_durable_handoff(case_a, product_a, snapshot_a)
    assert report_a.persistence_result["indexed_frame_ids"] == [frame_id_a]
    assert _safe_persist_only_frame_ids(report_a) == [frame_id_a]
    print(
        "E3_A_SUCCESS "
        f"status={report_a.status} observation=1 frame=1 location=1 unit=1 evidence=FULL "
        f"handoff_item=PERSISTED_IDENTITY_ONLY frame_id={frame_id_a} "
        f"write_backend_pid={write_pid_a} restart_backend_pid={read_pid_a}"
    )

    # B: F0 raises before real Observation linking/persistence.
    case_b = harness.new_case(tmp_path, "B-F0", 2)
    report_b_path, _product_b = _make_durable_synthetic_stage1_product(case_b, tmp_path)
    harness.preflight_absent(case_b)
    real_ensure = archive_indexer.ensure_observation_for_frame

    def fail_before_observation(*_args, **_kwargs):
        raise RuntimeError("F0 synthetic pre-Observation fault")

    with monkeypatch.context() as scoped:
        scoped.setattr(archive_indexer, "ensure_observation_for_frame", fail_before_observation)
        with harness.session() as (session, write_pid_b):
            report_b = handoff_stage1_execution(
                session, execution_report_path=report_b_path, persist_only=True
            )
    with harness.session() as (session, read_pid_b):
        snapshot_b = harness.snapshot(session, case_b)
    assert read_pid_b != write_pid_b
    assert len(snapshot_b["observations"]) == 0
    assert len(snapshot_b["frames"]) == 0
    assert len(snapshot_b["locations"]) == 0
    assert len(snapshot_b["units"]) == 0
    assert len(snapshot_b["events"]) == 1
    event_b = snapshot_b["events"][0]
    assert event_b["frame_id"] is None
    assert event_b["stage"] == "index_archive"
    assert event_b["level"] == "ERROR"
    assert event_b["details"]["path"] == str(case_b.product_path)
    assert report_b.status == "FAILED"
    assert report_b.items[0].status == "PERSISTENCE_FAILED"
    assert report_b.persistence_result["status"] == "FAILED"
    assert report_b.persistence_result["indexed_frame_ids"] == []
    harness.capture(case_b, snapshot_b)
    print(
        "E3_B_F0 "
        f"status={report_b.status} observation=0 frame=0 location=0 unit=0 "
        "index_error_events=1 frame_id=NULL"
    )

    # C: F1 calls the real Observation linker, then fails before Frame insertion.
    case_c = harness.new_case(tmp_path, "C-F1", 3)
    report_c_path, _product_c = _make_durable_synthetic_stage1_product(case_c, tmp_path)
    harness.preflight_absent(case_c)
    flushed_observation_ids: list[int] = []

    def link_then_fail_before_frame(session, frame):
        result = real_ensure(session, frame)
        assert result.observation_id is not None
        flushed_observation_ids.append(int(result.observation_id))
        raise RuntimeError("F1 synthetic post-Observation-flush fault")

    with monkeypatch.context() as scoped:
        scoped.setattr(archive_indexer, "ensure_observation_for_frame", link_then_fail_before_frame)
        with harness.session() as (session, write_pid_c):
            report_c = handoff_stage1_execution(
                session, execution_report_path=report_c_path, persist_only=True
            )
    with harness.session() as (session, read_pid_c):
        snapshot_c = harness.snapshot(session, case_c)
    assert read_pid_c != write_pid_c
    assert flushed_observation_ids
    assert len(snapshot_c["observations"]) == 0
    assert len(snapshot_c["frames"]) == 0
    assert len(snapshot_c["locations"]) == 0
    assert len(snapshot_c["units"]) == 0
    assert len(snapshot_c["events"]) == 1
    event_c = snapshot_c["events"][0]
    assert event_c["frame_id"] is None
    assert event_c["stage"] == "index_archive"
    assert event_c["level"] == "ERROR"
    assert event_c["details"]["path"] == str(case_c.product_path)
    assert report_c.status == "FAILED"
    assert report_c.items[0].status == "PERSISTENCE_FAILED"
    assert report_c.persistence_result["indexed_frame_ids"] == []
    harness.capture(case_c, snapshot_c)
    print(
        "E3_C_F1 "
        f"real_observation_flush_ids={len(flushed_observation_ids)} durable_observations=0 "
        "frames=0 locations=0 units=0 index_error_events=1"
    )

    # D: F2 fault exactly at evidence entry, after the real indexer's per-file commit.
    case_d = harness.new_case(tmp_path, "D-F2", 4)
    report_d_path, product_d = _make_durable_synthetic_stage1_product(case_d, tmp_path)
    harness.preflight_absent(case_d)
    real_evidence = handoff_module._persist_selected_evidence
    evidence_entry_calls: list[int] = []

    def fail_at_evidence_entry(_session, _items, indexed_ids):
        evidence_entry_calls.extend(int(value) for value in indexed_ids)
        raise RuntimeError("F2 synthetic crash between index and evidence commits")

    with monkeypatch.context() as scoped:
        scoped.setattr(handoff_module, "_persist_selected_evidence", fail_at_evidence_entry)
        with harness.session() as (session, write_pid_d):
            report_d = handoff_stage1_execution(
                session, execution_report_path=report_d_path, persist_only=True
            )
    with harness.session() as (session, restart_pid_d):
        snapshot_d = harness.snapshot(session, case_d)
    assert restart_pid_d != write_pid_d
    assert len(evidence_entry_calls) == 1
    assert report_d.status == "FAILED"
    assert report_d.items[0].status == "PERSISTENCE_FAILED"
    assert report_d.items[0].selected_frame_id is None
    assert report_d.persistence_result["status"] == "FAILED"
    assert report_d.persistence_result["indexed_frame_ids"] == evidence_entry_calls
    assert len(snapshot_d["observations"]) == 1
    assert len(snapshot_d["frames"]) == 1
    assert len(snapshot_d["locations"]) == 1
    assert len(snapshot_d["units"]) == 1
    assert snapshot_d["events"] == []
    assert snapshot_d["memberships"] == []
    f2_frame = snapshot_d["frames"][0]
    f2_location = snapshot_d["locations"][0]
    assert f2_frame["header_json"]["UNIQNUM"] == case_d.native_identity
    assert f2_frame["header_json"]["GGRAW"] == case_d.raw_remote_path
    assert "stage1_fingerprint" not in f2_frame["header_json"]
    assert "source_observation_uid" not in f2_frame["header_json"]
    assert "raw_relative_physical_key" not in f2_frame["header_json"]
    assert f2_location["content_sha256"] is None
    assert f2_location["locator"] == str(case_d.product_path)
    assert f2_location["is_primary"] is True
    frame_id_d = int(f2_frame["frame_id"])
    assert evidence_entry_calls == [frame_id_d]
    harness.capture(case_d, snapshot_d)
    f2_e3_state = {
        "observation_count": len(snapshot_d["observations"]),
        "frame_count": len(snapshot_d["frames"]),
        "location_count": len(snapshot_d["locations"]),
        "instrument_unit_count": len(snapshot_d["units"]),
        "frame_location_sha256": f2_location["content_sha256"],
        "stage1_fingerprint_present": "stage1_fingerprint" in f2_frame["header_json"],
        "raw_relative_physical_key_present": "raw_relative_physical_key"
        in f2_frame["header_json"],
        "ggraw": f2_frame["header_json"]["GGRAW"],
        "membership_count": len(snapshot_d["memberships"]),
        "path_event_count": len(snapshot_d["events"]),
        "report_status": report_d.status,
        "item_status": report_d.items[0].status,
        "item_selected_frame_id": report_d.items[0].selected_frame_id,
        "diagnostic_indexed_frame_ids": list(report_d.persistence_result["indexed_frame_ids"]),
        "write_backend_pid": write_pid_d,
        "restart_backend_pid": restart_pid_d,
    }
    print("E3_D_F2=" + json.dumps(f2_e3_state, sort_keys=True))

    # E: report-level feed accepts only READY/PERSISTED_IDENTITY_ONLY items.
    safe_a_ids = _safe_persist_only_frame_ids(report_a)
    safe_d_ids = _safe_persist_only_frame_ids(report_d)
    diagnostic_d_ids = [int(value) for value in report_d.persistence_result["indexed_frame_ids"]]
    assert safe_a_ids == [frame_id_a]
    assert safe_d_ids == []
    assert diagnostic_d_ids == [frame_id_d]
    assert report_d.items[0].selected_frame_id is None
    print(
        "E3_E_REPORT_FEED "
        f"success_report_frame_ids={safe_a_ids} failed_report_safe_frame_ids={safe_d_ids} "
        f"failed_report_diagnostic_indexed_frame_ids={diagnostic_d_ids} "
        "production_coordinator_consumer_exercised=NO"
    )

    # F: the existing DB-discovery builder is tested strictly in dry-run mode.
    from grandgousier.archive.fields_series import build_field_series

    with harness.session() as (session, builder_pid):
        builder_result = build_field_series(
            session,
            frame_ids=None,
            instrument_unit_id=int(snapshot_d["units"][0]["instrument_unit_id"]),
            dry_run=True,
        )
        builder_row = next(
            (
                row
                for row in builder_result.observation_selection_rows
                if row.get("frame_id") == frame_id_d
            ),
            None,
        )
        builder_selected = bool(
            builder_row is not None
            and builder_row.get("action") == ObservationScienceAction.SELECTED.value
            and builder_row.get("selected_frame_id") == frame_id_d
        )
        membership_count_after_dry_run = int(
            session.scalar(
                text(
                    "SELECT COUNT(*) FROM public.field_series_frames "
                    "WHERE frame_id = :frame_id"
                ),
                {"frame_id": frame_id_d},
            )
        )
    assert builder_result.dry_run is True
    assert membership_count_after_dry_run == 0
    assert len(builder_result.observation_selection_rows) == 1
    print(
        "E3_F_BUILDER "
        f"db_discovered_candidate_count={builder_result.selected_frames} "
        f"p4_selection_action={(builder_row or {}).get('action')} "
        f"partial_frame_selected={builder_selected} dry_run=True "
        f"series_membership_writes=0 backend_pid={builder_pid}"
    )

    # G: production P4 plus replay of the same report, after restart inspection.
    with harness.session() as (session, replay_pid):
        replay_d = handoff_stage1_execution(
            session, execution_report_path=report_d_path, persist_only=True
        )
    with harness.session() as (session, replay_read_pid):
        snapshot_d_replay = harness.snapshot(session, case_d)
    assert replay_read_pid != replay_pid
    assert len(snapshot_d_replay["observations"]) == 1
    assert len(snapshot_d_replay["frames"]) == 1
    assert len(snapshot_d_replay["locations"]) == 1
    assert len(snapshot_d_replay["units"]) == 1
    assert snapshot_d_replay["events"] == []
    assert snapshot_d_replay["memberships"] == []
    replay_frame_id = int(snapshot_d_replay["frames"][0]["frame_id"])
    assert replay_frame_id == frame_id_d
    harness.capture(case_d, snapshot_d_replay)
    replay_item = replay_d.items[0] if replay_d.items else None
    replay_p4_action = replay_item.p4_action if replay_item is not None else "NO_ITEM"
    replay_evidence_complete = (
        len(snapshot_d_replay["locations"]) == 1
        and snapshot_d_replay["locations"][0]["content_sha256"]
        == product_d.product_checksum_sha256
        and snapshot_d_replay["frames"][0]["header_json"].get("stage1_fingerprint")
        == product_d.fingerprint
        and snapshot_d_replay["frames"][0]["header_json"].get("source_observation_uid")
        == case_d.source_observation_uid
        and snapshot_d_replay["frames"][0]["header_json"].get("raw_relative_physical_key")
        == case_d.raw_key
    )
    replay_recovered = bool(
        replay_d.status == "READY"
        and replay_item is not None
        and replay_item.status == "PERSISTED_IDENTITY_ONLY"
        and replay_evidence_complete
    )
    duplicate_observation = len(snapshot_d_replay["observations"]) != 1
    duplicate_frame = len(snapshot_d_replay["frames"]) != 1
    print(
        "E3_G_P4_REPLAY "
        f"report_status={replay_d.status} p4_action={replay_p4_action} "
        f"item_status={replay_item.status if replay_item else 'NO_ITEM'} "
        f"evidence_complete_after_replay={replay_evidence_complete} "
        f"replay_recovers={replay_recovered} duplicate_observation={duplicate_observation} "
        f"duplicate_frame={duplicate_frame} observation_count={len(snapshot_d_replay['observations'])} "
        f"frame_count={len(snapshot_d_replay['frames'])} "
        f"replay_backend_pid={replay_pid} read_backend_pid={replay_read_pid}"
    )

    # H: real evidence commit succeeds; injected return loss is not literal power loss.
    case_h = harness.new_case(tmp_path, "H-F3", 5)
    report_h_path, product_h = _make_durable_synthetic_stage1_product(case_h, tmp_path)
    harness.preflight_absent(case_h)
    real_evidence_returns: list[dict[str, int]] = []

    def commit_evidence_then_lose_return(session, items, indexed_ids):
        evidence_result = real_evidence(session, items, indexed_ids)
        real_evidence_returns.append(evidence_result)
        raise RuntimeError("F3 simulated post-commit handoff return loss")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            handoff_module,
            "_persist_selected_evidence",
            commit_evidence_then_lose_return,
        )
        with harness.session() as (session, write_pid_h):
            report_h = handoff_stage1_execution(
                session, execution_report_path=report_h_path, persist_only=True
            )
    with harness.session() as (session, restart_pid_h):
        snapshot_h = harness.snapshot(session, case_h)
    assert restart_pid_h != write_pid_h
    assert len(real_evidence_returns) == 1
    assert report_h.status == "FAILED"
    assert report_h.items[0].status == "PERSISTENCE_FAILED"
    frame_id_h = _assert_complete_durable_handoff(case_h, product_h, snapshot_h)
    assert report_h.persistence_result["indexed_frame_ids"] == [frame_id_h]
    harness.capture(case_h, snapshot_h)
    print(
        "E3_H_F3_POST_COMMIT_RETURN_LOSS "
        f"caller_status={report_h.status} evidence_commit_succeeded=True durable_evidence=FULL "
        f"frame_id={frame_id_h} write_backend_pid={write_pid_h} "
        f"restart_backend_pid={restart_pid_h} literal_power_loss=NO"
    )

    state_classification = (
        "UNSAFE_BUILDER_LEAK"
        if builder_selected
        else "UNSAFE_DUPLICATE"
        if duplicate_observation or duplicate_frame
        else "UNSAFE_SKIP"
        if replay_p4_action == ObservationScienceAction.ALREADY_REPRESENTED.value
        and not replay_recovered
        else "SAFE_RECOVERABLE"
        if replay_recovered
        else "SAFE_BLOCKED"
        if replay_d.status in {"BLOCKED", "FAILED"} and not builder_selected
        else "UNKNOWN"
    )
    assert stage2_calls == []
    verdicts = {
        "OBSERVATION_COMMIT_BEFORE_EVIDENCE": "YES [E1/E3]",
        "FRAME_COMMIT_BEFORE_EVIDENCE": "YES [E1/E3]",
        "F2_PARTIAL_DURABLE_STATE_EXISTS": "YES [E3]",
        "PARTIAL_HANDOFF_RETURNS_SUCCESS_FRAME_ID": "NO [E3]",
        "PARTIAL_HANDOFF_EXCLUDED_FROM_RESULT_DRIVEN_BUILDER": "YES [E3 report-level only]",
        "PARTIAL_FRAME_DB_DISCOVERABLE_BY_BUILDER": f"{'YES' if builder_selected else 'NO'} [E3]",
        "P4_AFTER_PARTIAL_HANDOFF": f"{replay_p4_action} [E3]",
        "REPLAY_CREATES_DUPLICATE_OBSERVATION": f"{'YES' if duplicate_observation else 'NO'} [E3]",
        "REPLAY_CREATES_DUPLICATE_FRAME": f"{'YES' if duplicate_frame else 'NO'} [E3]",
        "REPLAY_RECOVERS_PARTIAL_HANDOFF": f"{'YES' if replay_recovered else 'NO'} [E3]",
        "PARTIAL_STATE_CLASSIFICATION": state_classification,
        "PERSIST_ONLY_PARTIAL_FAILURE_SEMANTICS": "KNOWN [E3]",
        "NEW_DB_SCHEMA_REQUIRED": "NO",
        "ACTUAL_STAGE2_EXECUTION_PERFORMED": "NO",
        "PRODUCTION_COORDINATOR_CONSUMER_EXERCISED": "NO",
        "READY_FOR_OBSERVING_NIGHT_ORCHESTRATION_PATCH": "NO",
        "READY_FOR_STAGE2_EXECUTE": "NO",
        "READY_FOR_FULL_MASTER_ARCHIVE_EXECUTE": "NO",
    }
    print("E3_REQUIRED_VERDICTS=" + json.dumps(verdicts, sort_keys=True))
    assert harness.cleanup_verified is False


def test_durable_handoff_capture_precedes_semantic_assertion_failure(monkeypatch) -> None:
    expected = {21: ("synthetic-observation-uid", "master_uniqnum_v1", "204_123456789")}
    case = SimpleNamespace(**{
        f"captured_{name}": {} for name in ("observations", "frames", "locations", "units", "events")
    })
    snapshot = {
        "observations": [dict(zip(
            ("observation_id", "observation_uid", "identity_scheme", "native_identity"),
            (21, *expected[21]), strict=True,
        ))],
        **dict.fromkeys(("frames", "locations", "units", "events"), []),
    }

    def fail_after_capture(case, _product, _snapshot):
        assert case.captured_observations == expected
        raise AssertionError("intentional semantic assertion failure")

    monkeypatch.setattr(sys.modules[__name__], "_assert_complete_durable_handoff", fail_after_capture)
    _DurablePersistOnlyPostgresHarness.capture(None, case, snapshot)
    with pytest.raises(AssertionError, match="intentional semantic assertion failure"):
        _assert_complete_durable_handoff(case, None, snapshot)
