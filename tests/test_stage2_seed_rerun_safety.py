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
