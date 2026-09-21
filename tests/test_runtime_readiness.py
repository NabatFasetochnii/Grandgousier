from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from grandgousier.services.readiness import check_runtime_readiness


class _Connection:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, statement):
        return 1


class _Engine:
    def connect(self):
        return _Connection()
    def dispose(self):
        pass


def test_runtime_readiness_reports_ready(monkeypatch, tmp_path: Path):
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr("grandgousier.services.readiness.make_engine", lambda url: _Engine())
    monkeypatch.setattr(
        "grandgousier.services.readiness.migration_status",
        lambda engine: [SimpleNamespace(migration_id="m1", state="APPLIED", checksum_status="OK")],
    )
    result = check_runtime_readiness(
        database_url="db",
        processed_root=processed,
        out_root=tmp_path / "out",
        instrument_profile="MASTER",
    )
    assert result.ready is True
    assert all(check.status == "OK" for check in result.checks)


def test_runtime_readiness_blocks_pending_or_drift(monkeypatch, tmp_path: Path):
    processed = tmp_path / "processed"
    processed.mkdir()
    monkeypatch.setattr("grandgousier.services.readiness.make_engine", lambda url: _Engine())
    monkeypatch.setattr(
        "grandgousier.services.readiness.migration_status",
        lambda engine: [
            SimpleNamespace(migration_id="pending", state="PENDING", checksum_status="-"),
            SimpleNamespace(migration_id="drift", state="APPLIED", checksum_status="DRIFT"),
        ],
    )
    result = check_runtime_readiness(
        database_url="db",
        processed_root=processed,
        out_root=tmp_path / "out",
        instrument_profile="RoboPhot",
    )
    assert result.ready is False
    schema = next(check for check in result.checks if check.name == "schema_migrations")
    assert schema.status == "FAIL"
    assert "pending=pending" in schema.detail
    assert "checksum_drift=drift" in schema.detail
