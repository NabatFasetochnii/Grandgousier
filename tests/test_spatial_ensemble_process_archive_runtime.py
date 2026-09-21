import argparse
import ast
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[1]


class _StopCall(RuntimeError):
    pass


class _Args:
    def __init__(self, spatial_max_residual_mad, spatial_max_residual_mad_degradation=0.0):
        self.spatial_ensemble_max_residual_mad = spatial_max_residual_mad
        self.spatial_ensemble_max_residual_mad_degradation = spatial_max_residual_mad_degradation
        self.execute = False
        self.plan_only = False
        self.database_url = None

    def __getattr__(self, name):
        return None


class _SessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def _load_cmd_process_archive_path(captured):
    source = (ROOT / "grandgousier" / "cli.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    fn = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_process_archive_path"
    )
    isolated = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(isolated)

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
        "make_session_factory": lambda database_url: (lambda: _SessionContext()),
        "_parse_extensions_arg": lambda value: value,
    }
    exec(compile(isolated, str(ROOT / "grandgousier" / "cli.py"), "exec"), namespace)

    def cleanup():
        if old is None:
            sys.modules.pop("grandgousier.archive.auto_process", None)
        else:
            sys.modules["grandgousier.archive.auto_process"] = old

    return namespace["cmd_process_archive_path"], cleanup


def _capture_forwarded(value):
    captured = {}
    fn, cleanup = _load_cmd_process_archive_path(captured)
    try:
        try:
            fn(_Args(value))
        except _StopCall:
            pass
    finally:
        cleanup()
    return captured


def test_process_archive_spatial_max_residual_mad_positive_is_forwarded():
    captured = _capture_forwarded(0.05)
    assert captured["spatial_ensemble_max_residual_mad"] == 0.05


def test_process_archive_spatial_max_residual_mad_negative_becomes_none():
    captured = _capture_forwarded(-1.0)
    assert captured["spatial_ensemble_max_residual_mad"] is None


def test_process_archive_spatial_max_residual_mad_degradation_zero_is_forwarded():
    captured = _capture_forwarded(0.05)
    assert captured["spatial_ensemble_max_residual_mad_degradation"] == 0.0


def test_process_archive_spatial_max_residual_mad_degradation_negative_becomes_none():
    captured = {}
    fn, cleanup = _load_cmd_process_archive_path(captured)
    try:
        try:
            fn(_Args(0.05, -1.0))
        except _StopCall:
            pass
    finally:
        cleanup()
    assert captured["spatial_ensemble_max_residual_mad_degradation"] is None
