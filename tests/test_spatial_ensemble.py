from __future__ import annotations

import numpy as np

from grandgousier.calibration.spatial_ensemble import (
    fit_spatial_ensemble_model,
    predict_spatial_ensemble_correction,
)


def _rows(x, y, residual):
    return [
        {"x": float(xi), "y": float(yi), "residual": float(ri), "weight": 1.0}
        for xi, yi, ri in zip(x, y, residual)
    ]


def test_spatial_ensemble_recovers_zero_mean_plane_with_outlier():
    rng = np.random.default_rng(5446)
    x = rng.uniform(0.0, 4095.0, 240)
    y = rng.uniform(0.0, 4095.0, 240)
    xn = 2.0 * x / 4095.0 - 1.0
    yn = 2.0 * y / 4095.0 - 1.0
    residual = 0.018 + 0.041 * xn - 0.028 * yn + rng.normal(0.0, 0.0015, len(x))
    residual[3] += 0.35

    model = fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=2,
        min_ref_stars=30,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "plane"
    assert abs(model["coefficient_x"] - 0.041) < 0.004
    assert abs(model["coefficient_y"] + 0.028) < 0.004
    assert model["residual_mad_after"] < 0.003
    # Applied model is differential only: the nuisance frame intercept is never predicted.
    correction, _ = predict_spatial_ensemble_correction(
        model, x=2047.5, y=2047.5, bounds=(0.0, 4095.0, 0.0, 4095.0)
    )
    assert abs(correction) < 0.01


def test_spatial_ensemble_selects_quadratic_when_curvature_is_real():
    rng = np.random.default_rng(5447)
    x = rng.uniform(0.0, 4095.0, 300)
    y = rng.uniform(0.0, 4095.0, 300)
    xn = 2.0 * x / 4095.0 - 1.0
    yn = 2.0 * y / 4095.0 - 1.0
    residual = (
        0.01
        + 0.012 * xn
        - 0.006 * yn
        + 0.031 * xn * xn
        - 0.014 * xn * yn
        + 0.019 * yn * yn
        + rng.normal(0.0, 0.001, len(x))
    )
    model = fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=2,
        min_ref_stars=30,
    )
    assert model["selected_model"] == "quadratic"
    assert abs(model["coefficient_xx"] - 0.031) < 0.004
    assert abs(model["coefficient_xy"] + 0.014) < 0.004
    assert abs(model["coefficient_yy"] - 0.019) < 0.004


def test_spatial_ensemble_keeps_identity_for_noise_only_field():
    rng = np.random.default_rng(5448)
    x = rng.uniform(0.0, 4095.0, 220)
    y = rng.uniform(0.0, 4095.0, 220)
    residual = rng.normal(0.0, 0.01, len(x))
    model = fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=2,
        min_ref_stars=30,
        min_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.003,
    )
    assert model["accepted"] is True
    assert model["selected_model"] == "identity"


def test_spatial_ensemble_rejects_unsafe_detector_excursion():
    rng = np.random.default_rng(5449)
    x = rng.uniform(0.0, 4095.0, 200)
    y = rng.uniform(0.0, 4095.0, 200)
    xn = 2.0 * x / 4095.0 - 1.0
    residual = 0.4 * xn + rng.normal(0.0, 0.001, len(x))
    model = fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=1,
        min_ref_stars=30,
        max_abs_spatial_deviation=0.15,
    )
    assert model["selected_model"] == "identity"
    assert model["fallback_reason"] == "residual_mad_quality_gate"
    assert model["accepted"] is False


def test_spatial_ensemble_robust_non_degradation_gate_falls_back_to_identity(monkeypatch):
    import grandgousier.calibration.spatial_ensemble as spatial_module

    def fake_fit(x, y, residual, weight, *, order, clip_sigma, n_iterations):
        if order == 0:
            return {
                "bic": 100.0,
                "n_used": len(x),
                "coefficients": [0.0],
                "covariance": None,
                "basis_means": [],
                "pivot_x_norm": 0.0,
                "pivot_y_norm": 0.0,
                "residual_mad": 0.0100,
                "residual_rms": 0.0150,
                "order": 0,
            }
        assert order == 1
        return {
            "bic": 80.0,
            "n_used": len(x),
            "coefficients": [0.0, 0.010, -0.005],
            "covariance": None,
            "basis_means": [0.0, 0.0],
            "pivot_x_norm": 0.0,
            "pivot_y_norm": 0.0,
            "residual_mad": 0.0104,
            "residual_rms": 0.0140,
            "order": 1,
        }

    monkeypatch.setattr(spatial_module, "_robust_spatial_fit", fake_fit)
    x = np.linspace(0.0, 4095.0, 40)
    y = np.linspace(4095.0, 0.0, 40)
    residual = np.zeros(40)
    model = spatial_module.fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=1,
        min_ref_stars=30,
        min_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.003,
        max_residual_mad_degradation=0.0,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "identity"
    assert model["selection_reason"] == "robust_non_degradation_gate"
    assert model["fallback_reason"] == "robust_non_degradation_gate"
    assert model["proposed_model"] == "plane"
    assert abs(model["proposed_residual_mad_delta"] - 0.0004) < 1e-12
    assert model["proposed_residual_mad_fractional_improvement"] < 0.0
    assert abs(model["residual_mad_delta"]) < 1e-12


def test_spatial_ensemble_robust_non_degradation_gate_can_be_disabled(monkeypatch):
    import grandgousier.calibration.spatial_ensemble as spatial_module

    def fake_fit(x, y, residual, weight, *, order, clip_sigma, n_iterations):
        if order == 0:
            return {
                "bic": 100.0,
                "n_used": len(x),
                "coefficients": [0.0],
                "covariance": None,
                "basis_means": [],
                "pivot_x_norm": 0.0,
                "pivot_y_norm": 0.0,
                "residual_mad": 0.0100,
                "residual_rms": 0.0150,
                "order": 0,
            }
        return {
            "bic": 80.0,
            "n_used": len(x),
            "coefficients": [0.0, 0.010, -0.005],
            "covariance": None,
            "basis_means": [0.0, 0.0],
            "pivot_x_norm": 0.0,
            "pivot_y_norm": 0.0,
            "residual_mad": 0.0104,
            "residual_rms": 0.0140,
            "order": 1,
        }

    monkeypatch.setattr(spatial_module, "_robust_spatial_fit", fake_fit)
    x = np.linspace(0.0, 4095.0, 40)
    y = np.linspace(4095.0, 0.0, 40)
    residual = np.zeros(40)
    model = spatial_module.fit_spatial_ensemble_model(
        _rows(x, y, residual),
        bounds=(0.0, 4095.0, 0.0, 4095.0),
        max_order=1,
        min_ref_stars=30,
        min_bic_improvement=6.0,
        min_spatial_peak_to_peak=0.003,
        max_residual_mad_degradation=None,
    )

    assert model["accepted"] is True
    assert model["selected_model"] == "plane"
    assert model["fallback_reason"] is None
    assert model["proposed_model"] == "plane"
    assert model["residual_mad_delta"] > 0.0
