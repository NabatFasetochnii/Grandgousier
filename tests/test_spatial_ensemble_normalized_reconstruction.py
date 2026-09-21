from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from grandgousier.calibration.reconstruction import _apply_stage
from grandgousier.calibration.spatial_ensemble import (
    _normalized_spatial_frame_term,
    predict_spatial_ensemble_correction,
)
from grandgousier.db.models import CalibrationFrameTerm, CalibrationIteration

ROOT = Path(__file__).parents[1]


def _materialized_term(frame_id: int, encoded) -> CalibrationFrameTerm:
    return CalibrationFrameTerm(
        frame_id=frame_id,
        term_kind=encoded.term_kind,
        model_name=encoded.model_name,
        coefficient_0=encoded.coefficient_0,
        coefficient_x=encoded.coefficient_x,
        coefficient_y=encoded.coefficient_y,
        scatter_mad=encoded.scatter_mad,
        scatter_rms=encoded.scatter_rms,
        n_candidates=encoded.n_candidates,
        n_used=encoded.n_used,
        n_reference=encoded.n_reference,
        accepted=encoded.accepted,
        quality_status=encoded.quality_status,
        details=encoded.details,
    )


def test_normalized_spatial_frame_term_reproduces_centered_quadratic_prediction():
    bounds = (0.0, 4095.0, 0.0, 4095.0)
    model = {
        "selected_model": "quadratic",
        "accepted": True,
        "order": 2,
        "coefficients": [0.012, 0.020, -0.010, 0.030, -0.015, 0.025],
        "basis_means": [0.08, -0.03, 0.41, 0.02, 0.38],
        "n_candidates": 120,
        "n_used": 110,
        "residual_mad_after": 0.007,
        "residual_rms_after": 0.011,
    }
    encoded = _normalized_spatial_frame_term(
        frame_id=42,
        model=model,
        bounds=bounds,
        global_quality_gate_passed=True,
    )
    term = _materialized_term(42, encoded)

    from grandgousier.calibration.reconstruction import _spatial_ensemble_prediction

    for x, y in ((0.0, 0.0), (1024.0, 3072.0), (2047.5, 2047.5), (4095.0, 4095.0)):
        expected, _ = predict_spatial_ensemble_correction(model, x=x, y=y, bounds=bounds)
        actual = _spatial_ensemble_prediction(term, x=x, y=y, bounds=bounds)
        assert abs(actual - expected) < 1e-12


def test_spatial_reconstruction_dispatch_preserves_errors_and_applies_sign():
    bounds = (0.0, 100.0, 0.0, 100.0)
    model = {
        "selected_model": "plane",
        "accepted": True,
        "order": 1,
        "coefficients": [0.5, 0.02, -0.01],
        "basis_means": [0.10, -0.20],
        "n_candidates": 60,
        "n_used": 55,
    }
    encoded = _normalized_spatial_frame_term(
        frame_id=7,
        model=model,
        bounds=bounds,
        global_quality_gate_passed=True,
    )
    term = _materialized_term(7, encoded)
    iteration = CalibrationIteration(
        iteration_kind="SPATIAL_ENSEMBLE",
        parameters={"algorithm": "robust_zero_mean_spatial_residual_v3", "detector_bounds": list(bounds)},
        metrics={},
    )
    state = {
        (3, 7): {
            "object_id": 3,
            "frame_id": 7,
            "x": 75.0,
            "y": 25.0,
            "mag_cal": 15.0,
            "mag_err": 0.012,
            "mag_err_random": 0.010,
            "mag_err_systematic": 0.006,
            "local_zp_correction": 0.04,
            "flags": {"catalog_name": "gaia_dr3_master", "source_id": "123"},
        }
    }
    expected, _ = predict_spatial_ensemble_correction(model, x=75.0, y=25.0, bounds=bounds)
    out = _apply_stage(
        state,
        product_kind="ENSEMBLE",
        iteration=iteration,
        frame_terms=[term],
        object_terms=[],
        calibration_version=None,
    )[(3, 7)]

    assert abs(out["mag_cal"] - (15.0 - expected)) < 1e-12
    assert abs(out["local_zp_correction"] - (0.04 - expected)) < 1e-12
    assert out["mag_err"] == 0.012
    assert out["mag_err_random"] == 0.010
    assert out["mag_err_systematic"] == 0.006
    assert out["flags"]["catalog_name"] == "gaia_dr3_master"
    assert out["flags"]["source_id"] == "123"
    assert out["flags"]["spatial_ensemble_model"] == "plane"


def test_spatial_apply_dual_writes_normalized_iteration_contract():
    path = ROOT / "grandgousier" / "calibration" / "spatial_ensemble.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "apply_spatial_ensemble_correction"
    )
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replace_calibration_iteration"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: kw.value for kw in calls[0].keywords if kw.arg}
    assert isinstance(keywords["iteration_kind"], ast.Constant)
    assert keywords["iteration_kind"].value == "SPATIAL_ENSEMBLE"
    assert isinstance(keywords["algorithm"], ast.Name)
    assert keywords["algorithm"].id == "SPATIAL_ENSEMBLE_ALGORITHM"
