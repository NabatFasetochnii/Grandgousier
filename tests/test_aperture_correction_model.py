import math

import pytest

from grandgousier.calibration.aperture_correction import (
    aperture_corrected_random_error,
    aperture_correction_differential_error,
    aperture_correction_model_error,
    fit_aperture_correction_model,
    predict_aperture_correction,
)


def _row(x, y, delta):
    return {"x": float(x), "y": float(y), "delta": float(delta)}


def test_spatial_plane_is_selected_when_detector_gradient_is_supported():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 50 + ix * 125
            y = 40 + iy * 115
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            noise = 0.0005 * math.sin(ix * 1.7 + iy * 0.9)
            rows.append(_row(x, y, -0.19 + 0.025 * xn - 0.018 * yn + noise))
    rows.append(_row(500, 400, 0.45))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.005,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "plane"
    assert model["n_used"] == 56
    assert model["plane_bic_improvement"] > 6.0
    assert model["spatial_peak_to_peak"] > 0.05
    assert model["coefficients"][0] == pytest.approx(-0.19, abs=0.001)
    assert model["coefficients"][1] == pytest.approx(0.025, abs=0.001)
    assert model["coefficients"][2] == pytest.approx(-0.018, abs=0.001)


def test_constant_model_is_selected_without_spatial_signal():
    rows = []
    for ix in range(7):
        for iy in range(6):
            rows.append(_row(50 + ix * 130, 40 + iy * 120, -0.205 + 0.0008 * math.sin(ix + iy)))

    model = fit_aperture_correction_model(
        rows,
        image_width=1000,
        image_height=800,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.005,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "constant"
    assert model["coefficients"][0] == pytest.approx(-0.205, abs=0.001)
    assert model["coefficients"][1] == 0.0
    assert model["coefficients"][2] == 0.0


def test_too_few_calibrators_produces_identity_fallback():
    model = fit_aperture_correction_model(
        [_row(10, 10, -0.2), _row(20, 20, -0.21)],
        image_width=100,
        image_height=100,
        min_calibrators=5,
    )

    assert model["accepted"] is False
    assert model["selected_model"] == "identity"
    assert "need 5" in model["fallback_reason"]
    assert predict_aperture_correction(model, 50, 50) is None


def test_prediction_and_model_error_are_finite():
    rows = [_row(10 + i * 20, 20 + (i % 5) * 25, -0.18) for i in range(30)]
    model = fit_aperture_correction_model(
        rows,
        image_width=800,
        image_height=600,
        min_calibrators=20,
        min_plane_calibrators=40,
    )

    correction = predict_aperture_correction(model, 400, 300)
    uncertainty = aperture_correction_model_error(model, 400, 300)
    assert correction == pytest.approx(-0.18)
    assert uncertainty is not None
    assert uncertainty >= 0.0


def test_unsafe_plane_edge_extrapolation_falls_back_to_constant():
    width = 1000
    height = 800
    rows = []
    # Calibrators occupy only a narrow detector strip, so a large fitted gradient
    # would extrapolate beyond the configured correction range at the corners.
    for ix in range(6):
        for iy in range(6):
            x = 450 + ix * 10
            y = 350 + iy * 10
            xn = 2.0 * x / (width - 1) - 1.0
            rows.append(_row(x, y, -0.15 + 1.2 * xn + 0.0002 * math.sin(ix + iy)))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=0.0,
        min_spatial_peak_to_peak=0.0,
        max_abs_correction=0.5,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "constant"
    assert "max absolute spatial deviation" in model["selection_reason"]


def test_separated_safety_gate_promotes_plane_rejected_by_legacy_total_edge_limit():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 100 + ix * 110
            y = 80 + iy * 100
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            noise = 0.0004 * math.sin(ix * 1.2 + iy * 0.8)
            rows.append(_row(x, y, -0.44 - 0.04 * xn - 0.03 * yn + noise))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.005,
        max_abs_correction=0.5,
        max_abs_spatial_deviation=0.15,
        max_abs_total_correction=0.75,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "plane"
    assert model["legacy_selected_model"] == "constant"
    assert model["selection_changed_vs_legacy"] is True
    assert model["plane_candidate_max_abs_total_correction"] > 0.5
    assert model["plane_candidate_max_abs_total_correction"] < 0.75
    assert model["plane_candidate_max_abs_spatial_deviation"] < 0.15
    assert "legacy plane edge correction" in model["legacy_selection_reason"]


def test_total_emergency_gate_still_rejects_extreme_plane():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 80 + ix * 120
            y = 60 + iy * 105
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            rows.append(_row(x, y, -0.35 + 0.22 * xn - 0.18 * yn))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=0.0,
        min_spatial_peak_to_peak=0.0,
        max_residual_mad=None,
        max_abs_correction=1.0,
        max_abs_spatial_deviation=0.5,
        max_abs_total_correction=0.6,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "constant"
    assert "max absolute total correction" in model["selection_reason"]


def test_constant_model_error_is_diagnostic_but_not_random_error():
    rows = [
        _row(20 + i * 20, 30 + (i % 6) * 35, -0.21 + 0.002 * math.sin(i))
        for i in range(36)
    ]
    model = fit_aperture_correction_model(
        rows,
        image_width=900,
        image_height=700,
        min_calibrators=20,
        min_plane_calibrators=50,
    )

    assert model["selected_model"] == "constant"
    assert aperture_correction_model_error(model, 400, 300) > 0.0
    assert aperture_correction_differential_error(model, 400, 300) == 0.0
    assert aperture_corrected_random_error(0.012, model=model, x=400, y=300) == pytest.approx(0.012)


def test_plane_random_error_excludes_intercept_and_uses_calibrator_pivot():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 80 + ix * 115
            y = 60 + iy * 105
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            noise = 0.003 * math.sin(ix * 1.3 + iy * 0.7)
            rows.append(_row(x, y, -0.22 + 0.030 * xn - 0.020 * yn + noise))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.005,
    )
    assert model["selected_model"] == "plane"

    pivot_x = (model["pivot_xnorm"] + 1.0) * (width - 1) / 2.0
    pivot_y = (model["pivot_ynorm"] + 1.0) * (height - 1) / 2.0
    pivot_diff = aperture_correction_differential_error(model, pivot_x, pivot_y)
    edge_diff = aperture_correction_differential_error(model, width - 1, height - 1)
    total_edge = aperture_correction_model_error(model, width - 1, height - 1)

    assert pivot_diff == pytest.approx(0.0, abs=1e-15)
    assert edge_diff is not None and edge_diff > 0.0
    assert total_edge is not None and total_edge > edge_diff
    assert aperture_corrected_random_error(0.012, model=model, x=pivot_x, y=pivot_y) == pytest.approx(0.012)
    assert aperture_corrected_random_error(0.012, model=model, x=width - 1, y=height - 1) > 0.012


def test_identity_fallback_adds_no_random_error():
    model = fit_aperture_correction_model(
        [_row(10, 10, -0.2)],
        image_width=100,
        image_height=100,
        min_calibrators=5,
    )
    assert model["selected_model"] == "identity"
    assert aperture_correction_differential_error(model, 50, 50) == 0.0
    assert aperture_corrected_random_error(0.02, model=model, x=50, y=50) == pytest.approx(0.02)


def test_safety_only_policy_selects_safe_plane_even_when_evidence_policy_rejects_it():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 50 + ix * 125
            y = 40 + iy * 115
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            rows.append(_row(x, y, -0.19 + 0.025 * xn - 0.018 * yn))

    evidence = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=1.0e9,
        min_spatial_peak_to_peak=0.005,
        plane_selection_policy="evidence",
    )
    safety_only = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=1.0e9,
        min_spatial_peak_to_peak=0.005,
        plane_selection_policy="safety_only",
    )

    assert evidence["selected_model"] == "constant"
    assert safety_only["selected_model"] == "plane"
    assert safety_only["plane_selection_policy"] == "safety_only"
    assert safety_only["statistical_rejection_reasons"]
    assert "safety_only" in safety_only["selection_reason"]


def test_safety_only_policy_still_rejects_unsafe_plane():
    width = 1000
    height = 800
    rows = []
    for ix in range(8):
        for iy in range(7):
            x = 80 + ix * 120
            y = 60 + iy * 105
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            rows.append(_row(x, y, -0.35 + 0.22 * xn - 0.18 * yn))

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        plane_selection_policy="safety_only",
        max_residual_mad=None,
        max_abs_correction=1.0,
        max_abs_spatial_deviation=0.5,
        max_abs_total_correction=0.6,
    )

    assert model["selected_model"] == "constant"
    assert "max absolute total correction" in model["selection_reason"]
