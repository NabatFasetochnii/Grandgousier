import argparse
import ast
import contextlib
import csv
import io
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]


class _StopCall(RuntimeError):
    pass


class _Args:
    execute = False
    plan_only = False
    database_url = None
    spatial_ensemble_max_residual_mad = 0.05
    spatial_ensemble_max_residual_mad_degradation = 0.0
    series_ids = [78]
    ensemble_min_ref_stars_per_frame = 20

    def __getattr__(self, _name):
        return None


class _SessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def _extract_function(path: Path, name: str):
    module = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_process_archive_cli_forwards_selective_series_and_ensemble_threshold():
    source_path = ROOT / "grandgousier" / "cli.py"
    fn = _extract_function(source_path, "cmd_process_archive_path")
    isolated = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(isolated)

    captured = {}
    fake_auto = types.ModuleType("grandgousier.archive.auto_process")
    fake_auto.process_archive_group_columns = lambda: []
    fake_auto.process_archive_summary_columns = lambda: []
    fake_auto.process_archive_summary_rows = lambda result: []

    def fake_process_archive_path(session, **kwargs):
        captured.update(kwargs)
        raise _StopCall

    fake_auto.process_archive_path = fake_process_archive_path
    old = sys.modules.get("grandgousier.archive.auto_process")
    sys.modules["grandgousier.archive.auto_process"] = fake_auto
    namespace = {
        "argparse": argparse,
        "sys": sys,
        "make_session_factory": lambda _url: (lambda: _SessionContext()),
        "_parse_extensions_arg": lambda value: value,
    }
    try:
        exec(compile(isolated, str(source_path), "exec"), namespace)
        with pytest.raises(_StopCall):
            namespace["cmd_process_archive_path"](_Args())
    finally:
        if old is None:
            sys.modules.pop("grandgousier.archive.auto_process", None)
        else:
            sys.modules["grandgousier.archive.auto_process"] = old

    assert captured["series_ids"] == [78]
    assert captured["ensemble_min_ref_stars_per_frame"] == 20


def test_process_archive_parser_exposes_selective_rerun_without_changing_default_threshold():
    source = (ROOT / "grandgousier" / "cli.py").read_text(encoding="utf-8")
    assert '"--series-id", dest="series_ids", type=int, action="append"' in source
    assert '"--ensemble-min-ref-stars-per-frame", type=int, default=30' in source


def test_selective_mode_skips_series_builder_and_validates_path_membership():
    source = (ROOT / "grandgousier" / "archive" / "auto_process.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "process_archive_path")
    guarded_builds = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not):
            if isinstance(node.test.operand, ast.Name) and node.test.operand.id == "requested_series_ids":
                if any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "build_field_series"
                    for child in ast.walk(node)
                ):
                    guarded_builds.append(node)
    assert guarded_builds, "build_field_series must be skipped in selective --series-id mode"
    assert 'result.status = "FAILED_PRECONDITION_SERIES_SELECTION"' in source


def _load_runner_precondition_symbols():
    source_path = ROOT / "grandgousier" / "pipeline" / "runner.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "FAILED_PRECONDITION_ENSEMBLE_STATUS" for t in node.targets):
                wanted.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "EnsemblePreconditionError":
            wanted.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
            "_ensemble_precondition_diagnostics",
            "_raise_if_ensemble_unusable",
        }:
            wanted.append(node)
    module = ast.Module(body=wanted, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"Any": Any}
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


def test_ensemble_precondition_rejects_22_references_when_pipeline_requires_30():
    ns = _load_runner_precondition_symbols()
    result = SimpleNamespace(
        reference_candidates=22,
        usable_reference_objects=22,
        selected_frames=29,
        frames_with_offsets=0,
        calibrated=0,
        target_calibration_version_id=971,
    )
    with pytest.raises(ns["EnsemblePreconditionError"]) as exc_info:
        ns["_raise_if_ensemble_unusable"](result, min_ref_stars_per_frame=30)
    exc = exc_info.value
    assert exc.status == "FAILED_PRECONDITION_ENSEMBLE"
    assert exc.diagnostics["base_ensemble_usable_reference_objects"] == 22
    assert exc.diagnostics["base_ensemble_required_references_per_frame"] == 30
    assert exc.diagnostics["base_ensemble_frames_with_offsets"] == 0


def test_ensemble_precondition_accepts_real_gpxtf17_dryrun_shape_at_20():
    ns = _load_runner_precondition_symbols()
    result = SimpleNamespace(
        reference_candidates=22,
        usable_reference_objects=22,
        selected_frames=29,
        frames_with_offsets=29,
        calibrated=57882,
        target_calibration_version_id=972,
    )
    ns["_raise_if_ensemble_unusable"](result, min_ref_stars_per_frame=20)


def test_photometric_frame_qc_result_retains_base_ensemble_telemetry_fields():
    source = (ROOT / "grandgousier" / "calibration" / "photometric_frame_qc.py").read_text(encoding="utf-8")
    for field_name in (
        "base_ensemble_reference_candidates",
        "base_ensemble_usable_reference_objects",
        "base_ensemble_required_references_per_frame",
        "base_ensemble_selected_frames",
        "base_ensemble_frames_with_offsets",
        "base_ensemble_calibrated",
        "base_ensemble_target_calibration_version_id",
    ):
        assert field_name in source


def test_cli_json_rows_serialize_datetime():
    source_path = ROOT / "grandgousier" / "cli.py"
    fn = _extract_function(source_path, "_print_rows")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"json": json, "csv": csv, "sys": sys, "_print_table": lambda *_args: None}
    exec(compile(module, str(source_path), "exec"), namespace)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        namespace["_print_rows"](
            [{"created_at": datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)}],
            ["created_at"],
            "json",
        )
    parsed = json.loads(output.getvalue())
    assert parsed[0]["created_at"] == "2026-09-04 12:00:00+00:00"


def test_failed_precondition_pipeline_status_maps_to_failed_series_state():
    from grandgousier.archive.fields_series import _series_status_from_pipeline_status

    assert _series_status_from_pipeline_status("FAILED_PRECONDITION_ENSEMBLE") == "FAILED"
    assert _series_status_from_pipeline_status("FAILED_PRECONDITION_SERIES_SELECTION") == "FAILED"


def test_runner_rejects_empty_explicit_frame_scope_before_database_operations():
    from grandgousier.pipeline.runner import FieldPipelineConfig, run_field_pipeline

    class NoDatabaseOperations:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected database operation: {name}")

    config = FieldPipelineConfig(
        catalog_name="catalog",
        calibration_prefix="test",
        series_id=7,
        frame_ids=[],
    )
    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        run_field_pipeline(NoDatabaseOperations(), config)


def test_runner_rejects_open_series_before_pipeline_side_effects(monkeypatch):
    from grandgousier.pipeline import runner

    session = _FakeSession()
    monkeypatch.setattr(
        runner,
        "load_series_selection",
        lambda *_args: {
            "status": "OPEN",
            "frame_ids": [10],
            "field_id": 1,
            "instrument_unit_id": None,
        },
    )
    before = session.pipeline_run_count
    calls = _install_admission_spies(monkeypatch, session)

    config = runner.FieldPipelineConfig(
        catalog_name="catalog",
        calibration_prefix="test",
        series_id=7,
    )
    with pytest.raises(ValueError, match="field_series is OPEN.*series_id=7"):
        runner.run_field_pipeline(session, config)
    assert session.execute_calls == []
    assert session.pipeline_run_count == before
    assert calls == {
        "capture": 0,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }


@pytest.mark.parametrize(
    ("series_frame_ids", "explicit_frame_ids", "message"),
    [
        ([], None, "field_series has no selected frames"),
        ([10], [99], "has no frames in series_id=7"),
    ],
)
def test_runner_rejects_empty_resolved_series_scope_before_database_operations(
    monkeypatch, series_frame_ids, explicit_frame_ids, message
):
    from grandgousier.pipeline import runner

    monkeypatch.setattr(
        runner,
        "load_series_selection",
        lambda *_args: {
            "frame_ids": series_frame_ids,
            "field_id": 1,
            "instrument_unit_id": None,
        },
    )

    class NoDatabaseOperations:
        def __getattr__(self, name):
            raise AssertionError(f"unexpected database operation: {name}")

    config = runner.FieldPipelineConfig(
        catalog_name="catalog",
        calibration_prefix="test",
        series_id=7,
        frame_ids=explicit_frame_ids,
    )
    with pytest.raises(ValueError, match=message):
        runner.run_field_pipeline(NoDatabaseOperations(), config)


@pytest.fixture
def runner_fake_session():
    return _FakeSession()


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeNoAutoflush:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        self.session.no_autoflush_entries += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self):
        self.series_memberships = {}
        self.execute_calls = []
        self.pipeline_run_count = 4
        self.no_autoflush_entries = 0
        self.new = set()
        self.dirty = set()
        self.deleted = set()
        self.no_autoflush = _FakeNoAutoflush(self)

    def execute(self, statement, parameters=None):
        self.execute_calls.append((str(statement), parameters or {}))
        frame_ids = (parameters or {}).get("frame_ids", ())
        for frame_id in frame_ids:
            for series_id, status in self.series_memberships.get(int(frame_id), ()):
                if str(status).upper() == "OPEN":
                    return _FakeResult(series_id)
        return _FakeResult(None)


class _UnexpectedAdmissionSideEffect(RuntimeError):
    pass


class _AdmissionPassed(RuntimeError):
    pass


def _pipeline_config(**scope):
    from grandgousier.pipeline.runner import FieldPipelineConfig

    return FieldPipelineConfig(
        catalog_name="not-a-field-scope",
        calibration_prefix="admission-test",
        no_file_log=False,
        **scope,
    )


def _seed_admission_series(
    session,
    *,
    series_id,
    status,
    frame_ids=(),
):
    for frame_id in frame_ids:
        session.series_memberships.setdefault(int(frame_id), []).append(
            (int(series_id), str(status))
        )


def _install_admission_spies(monkeypatch, session, *, stop_at_artifact=False):
    from grandgousier.pipeline import runner

    calls = {
        "capture": 0,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }

    def capture(*_args, **_kwargs):
        calls["capture"] += 1
        if stop_at_artifact:
            raise _AdmissionPassed
        return {}

    def insert(*_args, **_kwargs):
        calls["insert"] += 1
        session.pipeline_run_count += 1
        raise _UnexpectedAdmissionSideEffect("pipeline run insertion reached")

    def series_started(*_args, **_kwargs):
        calls["series_started"] += 1
        raise _UnexpectedAdmissionSideEffect("series start bookkeeping reached")

    def series_result(*_args, **_kwargs):
        calls["series_result"] += 1
        raise _UnexpectedAdmissionSideEffect("series result bookkeeping reached")

    def logger(*_args, **_kwargs):
        calls["logger"] += 1
        raise _UnexpectedAdmissionSideEffect("file logger creation reached")

    def stage(*_args, **_kwargs):
        calls["stages"] += 1
        raise _UnexpectedAdmissionSideEffect("Stage-2 function reached")

    monkeypatch.setattr(runner, "capture_tree_state", capture)
    monkeypatch.setattr(runner, "_insert_pipeline_run", insert)
    monkeypatch.setattr(runner, "mark_series_pipeline_started", series_started)
    monkeypatch.setattr(runner, "mark_series_pipeline_result", series_result)
    monkeypatch.setattr(runner.PipelineFileLogger, "create", logger)
    for name in (
        "check_frames_wcs",
        "run_frame_pre_qc",
        "match_catalog_to_frames",
        "detect_sources_in_frames",
        "run_forced_aperture_photometry",
    ):
        monkeypatch.setattr(runner, name, stage)
    return calls


def _assert_rejected_before_side_effects(
    monkeypatch, session, config, expected_message
):
    from grandgousier.pipeline import runner

    calls = _install_admission_spies(monkeypatch, session)
    before = session.pipeline_run_count
    with pytest.raises(ValueError, match=expected_message):
        runner.run_field_pipeline(session, config)
    assert session.pipeline_run_count == before
    assert calls == {
        "capture": 0,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }


def _assert_admission_passes_to_artifact_boundary(monkeypatch, session, config):
    from grandgousier.pipeline import runner

    calls = _install_admission_spies(monkeypatch, session, stop_at_artifact=True)
    with pytest.raises(_AdmissionPassed):
        runner.run_field_pipeline(session, config)
    assert calls == {
        "capture": 1,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }


def test_runner_rejects_explicit_frame_ids_in_open_series_before_side_effects(
    monkeypatch, runner_fake_session
):
    session = runner_fake_session
    _seed_admission_series(
        session,
        series_id=10,
        status="OPEN",
        frame_ids=[100, 101],
    )
    before_count = session.pipeline_run_count
    calls = _install_admission_spies(monkeypatch, session)

    with pytest.raises(ValueError, match="requested frame_ids.*OPEN.*series_id=10"):
        from grandgousier.pipeline import runner

        runner.run_field_pipeline(session, _pipeline_config(frame_ids=[101, 100]))

    assert session.no_autoflush_entries == 1
    assert session.pipeline_run_count == before_count
    query, parameters = session.execute_calls[0]
    assert query.lstrip().upper().startswith("SELECT")
    assert parameters["frame_ids"] == [100, 101]
    assert calls == {
        "capture": 0,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }


def test_runner_rejects_mixed_open_and_finalized_frame_memberships_atomically(
    monkeypatch, runner_fake_session
):
    session = runner_fake_session
    _seed_admission_series(
        session,
        series_id=10,
        status="OPEN",
        frame_ids=[100],
    )
    _seed_admission_series(
        session,
        series_id=11,
        status="READY",
        frame_ids=[101],
    )

    _assert_rejected_before_side_effects(
        monkeypatch,
        session,
        _pipeline_config(frame_ids=[101, 100]),
        "requested frame_ids.*OPEN.*series_id=10",
    )
    assert session.execute_calls[0][1]["frame_ids"] == [100, 101]


def test_runner_allows_finalized_and_unassigned_explicit_frames(
    monkeypatch, runner_fake_session
):
    session = runner_fake_session
    _seed_admission_series(
        session,
        series_id=11,
        status="READY",
        frame_ids=[101],
    )

    _assert_admission_passes_to_artifact_boundary(
        monkeypatch, session, _pipeline_config(frame_ids=[101, 102])
    )


def test_runner_allows_explicit_frames_without_series_membership(
    monkeypatch, runner_fake_session
):
    session = runner_fake_session
    _assert_admission_passes_to_artifact_boundary(
        monkeypatch, session, _pipeline_config(frame_ids=[102])
    )
    assert session.execute_calls[0][1]["frame_ids"] == [102]


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"field_id": 1},
        {"instrument_unit_id": 7},
        {"field_id": 1, "instrument_unit_id": 7},
    ],
)
def test_runner_rejects_unscoped_broad_invocation_with_any_field_unit_filters(
    monkeypatch, runner_fake_session, scope
):
    session = runner_fake_session
    _assert_rejected_before_side_effects(
        monkeypatch,
        session,
        _pipeline_config(**scope),
        "requires either series_id or explicit frame_ids",
    )
    assert session.execute_calls == []


def test_cli_without_series_cannot_bypass_runner_admission_guard(monkeypatch, capsys):
    from grandgousier import cli

    class SessionContext:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return False

    session = _FakeSession()
    calls = _install_admission_spies(monkeypatch, session)
    monkeypatch.setattr(cli, "make_session_factory", lambda *_args: lambda: SessionContext())
    monkeypatch.setattr(cli, "_build_pipeline_command_snapshot", dict)
    monkeypatch.setattr(cli, "_build_pipeline_environment_snapshot", lambda *_args: {})
    args = cli.build_parser().parse_args(
        [
            "run-field-pipeline",
            "--catalog-name",
            "catalog",
            "--calibration-prefix",
            "test",
            "--instrument-unit-id",
            "7",
        ]
    )

    assert args.series_id is None
    assert cli.cmd_run_field_pipeline(args) == 1
    assert "requires either series_id or explicit frame_ids" in capsys.readouterr().err
    assert calls == {
        "capture": 0,
        "insert": 0,
        "series_started": 0,
        "series_result": 0,
        "logger": 0,
        "stages": 0,
    }
