import inspect

import pytest

from grandgousier.calibration.aperture_correction import (
    ApertureCorrectionResult,
    apply_aperture_correction,
)
from grandgousier.calibration.color_calibration import (
    ColorCalibrationApplyResult,
    apply_color_calibration,
    compute_color_calibration_diagnostics,
)


def test_calibration_apply_results_expose_optional_performance_telemetry():
    aperture = ApertureCorrectionResult()
    assert aperture.performance_diagnostics_enabled is False
    assert aperture.timings_sec == {}
    assert aperture.throughput == {}

    color = ColorCalibrationApplyResult(
        source_calibration_name="source",
        target_calibration_name="target",
        series_id=None,
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="i",
        color_index="r-i",
    )
    assert color.performance_diagnostics_enabled is False
    assert color.timings_sec == {}
    assert color.throughput == {}


def test_calibration_functions_expose_diagnostic_switches_without_changing_defaults():
    aperture_parameter = inspect.signature(apply_aperture_correction).parameters["diagnose_performance"]
    color_parameter = inspect.signature(apply_color_calibration).parameters["diagnose_performance"]
    timing_sink = inspect.signature(compute_color_calibration_diagnostics).parameters["_performance_timings"]

    assert aperture_parameter.default is False
    assert color_parameter.default is False
    assert timing_sink.default is None


def test_calibration_performance_cli_switches_parse():
    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()

    pipeline = parser.parse_args(
        [
            "run-field-pipeline",
            "--catalog-name",
            "catalog",
            "--calibration-prefix",
            "test",
            "--diagnose-calibration-performance",
        ]
    )
    assert pipeline.diagnose_calibration_performance is True

    archive = parser.parse_args(
        ["process-archive-path", "--path", ".", "--diagnose-calibration-performance"]
    )
    assert archive.diagnose_calibration_performance is True

    direct_color = parser.parse_args(
        [
            "apply-color-calibration",
            "--source-calibration-name",
            "source",
            "--target-calibration-name",
            "target",
            "--reference-catalog-name",
            "reference",
            "--reference-band",
            "i",
            "--color-index",
            "r-i",
            "--diagnose-performance",
        ]
    )
    assert direct_color.diagnose_performance is True


def test_process_archive_path_accepts_and_forwards_calibration_performance_diagnostics():
    import ast
    from pathlib import Path

    path = Path(__file__).parents[1] / "grandgousier" / "archive" / "auto_process.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_archive_path"
    )
    kwonly = {arg.arg: default for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults)}
    assert "calibration_performance_diagnostics" in kwonly
    assert isinstance(kwonly["calibration_performance_diagnostics"], ast.Constant)
    assert kwonly["calibration_performance_diagnostics"].value is False
    assert "calibration_performance_diagnostics=calibration_performance_diagnostics" in source
