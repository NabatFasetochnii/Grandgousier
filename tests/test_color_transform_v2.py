from types import SimpleNamespace

import pytest

from grandgousier.calibration.color_calibration import (
    ColorCalibrationDiagnosticsResult,
    _color_systematic_error,
    _compare_shape_metrics,
    _fit_ensemble_color_transform,
    _select_ensemble_transform_model,
)
from grandgousier.quality.lightcurve_quality import _resolve_object_reference, _science_reference_from_flags


def test_ensemble_color_transform_recovers_offset_and_slope_with_outlier():
    offset = -0.193
    slope = 0.041
    rows = []
    for idx in range(25):
        x = -0.5 + idx / 24.0
        correction = offset + slope * x
        rows.append(
            {
                "object_id": idx + 1,
                "color_minus_color0": x,
                # current_residual = source - reference = -correction
                "current_residual": -correction,
                "n_measurements": 70,
            }
        )
    rows.append(
        {
            "object_id": 999,
            "color_minus_color0": 0.1,
            "current_residual": 1.5,
            "n_measurements": 70,
        }
    )

    fit = _fit_ensemble_color_transform(rows, clip_sigma=3.0, n_iterations=5)

    assert fit["offset"] == pytest.approx(offset, abs=1e-12)
    assert fit["color_term"] == pytest.approx(slope, abs=1e-12)
    assert fit["n_used_objects"] == 25
    assert fit["n_clipped_objects"] == 1
    assert fit["residual_rms"] == pytest.approx(0.0, abs=1e-12)
    assert fit["x_min"] == pytest.approx(-0.5)
    assert fit["x_max"] == pytest.approx(0.5)


def test_constant_per_object_transform_preserves_lightcurve_shape():
    source = {
        1: [14.9, 15.0, 14.95, 15.03, 14.92],
        2: [12.0, 12.02, 11.98, 12.01, 12.03],
    }
    output = {
        1: [value - 0.21 for value in source[1]],
        2: [value + 0.08 for value in source[2]],
    }

    result = _compare_shape_metrics(source, output)

    assert result["max_abs_rms_delta"] == pytest.approx(0.0, abs=1e-14)
    assert result["max_abs_mad_delta"] == pytest.approx(0.0, abs=1e-14)
    assert result["max_abs_peak_to_peak_delta"] == pytest.approx(0.0, abs=1e-14)


def test_systematic_error_combines_correlated_transform_terms():
    value = _color_systematic_error(
        x_color=0.4,
        color_term=0.05,
        color_error=0.03,
        offset_error=0.01,
        color_term_error=0.02,
        offset_color_cov=0.0,
        model_floor=0.04,
    )
    expected = (0.04**2 + 0.01**2 + (0.4 * 0.02) ** 2 + (0.05 * 0.03) ** 2) ** 0.5
    assert value == pytest.approx(expected)


def test_scatter_reference_prefers_reference_band_in_measurement_flags():
    measurement = SimpleNamespace(
        flags={
            "reference_catalog_name": "panstarrs_dr2",
            "reference_band": "i",
            "reference_mag": 14.72,
            "reference_color_value": 0.31,
        }
    )
    values = [(measurement, None, None, None)]

    ref_mag, color, catalog, band = _science_reference_from_flags(values)

    assert ref_mag == pytest.approx(14.72)
    assert color == pytest.approx(0.31)
    assert catalog == "panstarrs_dr2"
    assert band == "i"


def test_explicit_ps1_system_does_not_fall_back_to_gaia_when_object_reference_missing():
    ref_mag, color = _resolve_object_reference(
        science_ref_mag=None,
        science_color=None,
        science_ref_catalog="panstarrs_dr2",
        science_ref_band="i",
        legacy_ref_mag=15.4,
        legacy_color=1.2,
    )

    assert ref_mag is None
    assert color is None


def _diagnostics_for_selection(**overrides):
    values = {
        "calibration_name": "ens",
        "series_id": 1,
        "identity_catalog_name": "gaia",
        "reference_catalog_name": "ps1",
        "reference_band": "i",
        "color_index": "r_i",
        "ensemble_offset_only_offset": -0.19,
        "ensemble_offset_only_offset_err": 0.007,
        "ensemble_offset_only_residual_mad": 0.028,
        "ensemble_offset_only_residual_rms": 0.052,
        "ensemble_offset_only_residual_robust_sigma": 0.041,
        "ensemble_offset_only_bic": -320.0,
        "ensemble_transform_offset": -0.193,
        "ensemble_transform_offset_err": 0.008,
        "ensemble_transform_color_term": 0.02,
        "ensemble_transform_color_term_err": 0.032,
        "ensemble_transform_offset_color_cov": 0.0,
        "ensemble_transform_residual_mad": 0.027,
        "ensemble_transform_residual_rms": 0.051,
        "ensemble_transform_residual_robust_sigma": 0.040,
        "ensemble_color_model_bic": -318.0,
        "ensemble_color_model_bic_improvement": -2.0,
        "ensemble_color_model_mad_improvement": 0.001,
        "ensemble_color_term_significance": 0.625,
    }
    values.update(overrides)
    return ColorCalibrationDiagnosticsResult(**values)


def test_model_selection_uses_offset_only_for_insignificant_color_term():
    selected = _select_ensemble_transform_model(
        _diagnostics_for_selection(),
        min_color_term_significance=2.0,
        min_color_model_bic_improvement=6.0,
        min_color_model_mad_improvement=0.0,
    )

    assert selected["model"] == "offset_only"
    assert selected["color_term"] == 0.0
    assert "significance" in selected["reason"]


def test_model_selection_accepts_well_supported_color_term():
    selected = _select_ensemble_transform_model(
        _diagnostics_for_selection(
            ensemble_transform_color_term=0.08,
            ensemble_transform_color_term_err=0.015,
            ensemble_color_term_significance=5.33,
            ensemble_color_model_bic=-340.0,
            ensemble_color_model_bic_improvement=20.0,
            ensemble_color_model_mad_improvement=0.008,
        ),
        min_color_term_significance=2.0,
        min_color_model_bic_improvement=6.0,
        min_color_model_mad_improvement=0.0,
    )

    assert selected["model"] == "offset_plus_color"
    assert selected["color_term"] == pytest.approx(0.08)
