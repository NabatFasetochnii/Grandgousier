from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from grandgousier.orchestration.field_archive import Stage3Policy
from grandgousier.services.locks import (
    FieldArchiveJobLocked,
    field_archive_advisory_lock,
    field_archive_lock_key,
)
from grandgousier.services.runtime import FieldArchiveJobConfig


class _ScalarResult:
    def __init__(self, value):
        self.value = value
    def scalar(self):
        return self.value


class _Connection:
    def __init__(self, acquire=True):
        self.acquire = acquire
        self.calls = []
    def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, dict(params)))
        if "pg_try_advisory_lock" in sql:
            return _ScalarResult(self.acquire)
        if "pg_advisory_unlock" in sql:
            return _ScalarResult(True)
        raise AssertionError(sql)
    def close(self):
        self.calls.append(("CLOSE", {}))


class _Engine:
    def __init__(self, acquire=True):
        self.connection = _Connection(acquire=acquire)
    def connect(self):
        return self.connection
    def dispose(self):
        pass


def _config(tmp_path: Path):
    return FieldArchiveJobConfig(
        database_url="db",
        processed_root=str(tmp_path / "processed"),
        field_id=33,
        catalog_name="shared_catalog",
        stage3_policies=(Stage3Policy("V", 15, 76),),
        out_root=str(tmp_path / "out"),
    )


def test_field_archive_lock_key_is_stable_and_scope_sensitive(tmp_path: Path):
    config = _config(tmp_path)
    key1 = field_archive_lock_key(config)
    key2 = field_archive_lock_key(config)
    other = replace(config, field_id=34)
    assert key1 == key2
    assert -(2**63) <= key1 < 2**63
    assert field_archive_lock_key(other) != key1


def test_advisory_lock_acquire_and_release(monkeypatch, tmp_path: Path):
    engine = _Engine(acquire=True)
    monkeypatch.setattr("grandgousier.services.locks.make_engine", lambda url: engine)
    config = _config(tmp_path)
    with field_archive_advisory_lock(config) as key:
        assert key == field_archive_lock_key(config)
    sql = "\n".join(call[0] for call in engine.connection.calls)
    assert "pg_try_advisory_lock" in sql
    assert "pg_advisory_unlock" in sql


def test_advisory_lock_refuses_concurrent_scope(monkeypatch, tmp_path: Path):
    engine = _Engine(acquire=False)
    monkeypatch.setattr("grandgousier.services.locks.make_engine", lambda url: engine)
    with pytest.raises(FieldArchiveJobLocked):
        with field_archive_advisory_lock(_config(tmp_path)):
            pass
