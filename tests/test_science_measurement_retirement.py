from __future__ import annotations

from types import SimpleNamespace

import grandgousier.photometry.science_retirement as module


class _DeleteResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _Session:
    def __init__(
        self,
        *,
        v1_rows: int = 10,
        cache_rows: int = 10,
        manifest_rows: int = 10,
        manifest_residuals: int = 2,
        actual_residuals: int = 2,
        manifest_status: str = "COMPLETE_EXACT",
    ):
        self.cv = SimpleNamespace(
            calibration_version_id=9,
            name="science9",
            product_role="SCIENCE",
            lifecycle_status="COMPLETE_IMMUTABLE",
        )
        self.manifest = SimpleNamespace(
            source_row_count=manifest_rows,
            row_override_count=manifest_residuals,
            status=manifest_status,
        )
        self._scalar_values = [v1_rows, cache_rows, actual_residuals]
        self.deleted = 0
        self.flushes = 0

    def get(self, model, key):
        assert key == 9
        name = getattr(model, "__name__", "")
        if name == "CalibrationVersion":
            return self.cv
        if name == "ScienceFlagOverrideSet":
            return self.manifest
        raise AssertionError(name)

    def scalar(self, _stmt):
        if self._scalar_values:
            return self._scalar_values.pop(0)
        # post-delete V1 count
        return 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if sql.strip().startswith("DELETE FROM science_measurements"):
            self.deleted += 10
            return _DeleteResult(10)
        raise AssertionError(sql)

    def flush(self):
        self.flushes += 1


def _verified():
    return SimpleNamespace(
        status="OK",
        reason=None,
        checked_rows=10,
        scalar_mismatches=0,
        identity_mismatches=0,
        flag_mismatches=0,
    )


def test_retirement_dry_run_requires_complete_cache(monkeypatch) -> None:
    session = _Session(cache_rows=9)
    monkeypatch.setattr(module, "verify_science_flag_overrides", lambda *a, **k: _verified())
    result = module.retire_science_measurements(
        session, calibration_version_id=9, execute=False
    )
    assert result.status == "BLOCKED"
    assert result.reason == "CACHE_NOT_COMPLETE"
    assert session.deleted == 0


def test_retirement_dry_run_proves_all_contracts(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(module, "verify_science_flag_overrides", lambda *a, **k: _verified())
    result = module.retire_science_measurements(
        session, calibration_version_id=9, execute=False
    )
    assert result.status == "READY"
    assert result.verified_rows == 10
    assert result.v1_rows_before == 10
    assert result.cache_rows == 10
    assert result.deleted_rows == 0
    assert result.v1_rows_after == 10


def test_retirement_execute_deletes_v1_and_marks_manifest(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(module, "verify_science_flag_overrides", lambda *a, **k: _verified())
    result = module.retire_science_measurements(
        session, calibration_version_id=9, execute=True
    )
    assert result.status == "RETIRED"
    assert result.deleted_rows == 10
    assert result.v1_rows_after == 0
    assert session.manifest.status == "RETIRED_EXACT"
    assert session.flushes == 1


def test_retirement_requires_complete_exact_manifest(monkeypatch) -> None:
    session = _Session(manifest_status="BUILDING")
    monkeypatch.setattr(module, "verify_science_flag_overrides", lambda *a, **k: _verified())
    result = module.retire_science_measurements(
        session, calibration_version_id=9, execute=True
    )
    assert result.status == "BLOCKED"
    assert result.reason == "OVERRIDE_MANIFEST_STATUS_BUILDING"
    assert session.deleted == 0
