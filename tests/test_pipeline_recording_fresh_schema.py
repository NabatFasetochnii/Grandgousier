from __future__ import annotations

import json

import pytest

pytest.importorskip("astropy")

from sqlalchemy.exc import IntegrityError

from grandgousier.pipeline import runner as runner_module


class _ScalarResult:
    def __init__(self, value: int):
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CaptureSession:
    def __init__(self, values: list[int] | None = None):
        self.values = list(values or [1])
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _ScalarResult(self.values.pop(0))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FailingSession(_CaptureSession):
    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        raise IntegrityError("INSERT", params or {}, RuntimeError("boom"))


def test_pipeline_run_insert_initializes_nonnull_summary_json_for_fresh_schema():
    session = _CaptureSession([17])

    run_id = runner_module._insert_pipeline_run(
        session,
        run_uid="run-1",
        instrument_unit_id=1,
        field_id=2,
        series_id=3,
        catalog_name="cat",
        calibration_prefix="cal",
        recipe_json={"x": 1},
    )

    sql, params = session.calls[0]
    assert run_id == 17
    assert "summary_json" in sql
    assert json.loads(params["summary_json"]) == {}
    assert session.commits == 1


def test_pipeline_step_insert_initializes_nonnull_result_json_for_fresh_schema():
    session = _CaptureSession([23])

    step_id = runner_module._insert_pipeline_step(session, 17, "forced_photometry", 4)

    sql, params = session.calls[0]
    assert step_id == 23
    assert "result_json" in sql
    assert json.loads(params["result_json"]) == {}
    assert session.commits == 1


def test_pipeline_run_insert_rolls_back_on_integrity_error():
    session = _FailingSession()

    with pytest.raises(IntegrityError):
        runner_module._insert_pipeline_run(
            session,
            run_uid="run-fail",
            instrument_unit_id=None,
            field_id=None,
            series_id=None,
            catalog_name="cat",
            calibration_prefix="cal",
            recipe_json={},
        )

    assert session.rollbacks == 1
