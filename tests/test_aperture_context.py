import math

import numpy as np
import pytest

from grandgousier.calibration.aperture_context import (
    analyze_aperture_frame_context,
    estimate_psf_shape,
)
from grandgousier.calibration.aperture_correction import fit_aperture_correction_model


def test_psf_shape_recovers_elliptical_gaussian_scale_and_axis():
    size = 51
    yy, xx = np.mgrid[:size, :size]
    x0 = y0 = 25.0
    theta = math.radians(32.0)
    dx = xx - x0
    dy = yy - y0
    u = dx * math.cos(theta) + dy * math.sin(theta)
    v = -dx * math.sin(theta) + dy * math.cos(theta)
    sigma_major = 2.2
    sigma_minor = 1.4
    data = 1000.0 + 15000.0 * np.exp(-0.5 * ((u / sigma_major) ** 2 + (v / sigma_minor) ** 2))

    shape = estimate_psf_shape(data, x=x0, y=y0, background=1000.0, radius_pix=8.0)

    assert shape is not None
    assert shape["psf_fwhm_major_pix"] == pytest.approx(2.354820045 * sigma_major, rel=0.03)
    assert shape["psf_fwhm_minor_pix"] == pytest.approx(2.354820045 * sigma_minor, rel=0.03)
    assert shape["psf_ellipticity"] == pytest.approx(1.0 - sigma_minor / sigma_major, abs=0.03)
    delta = abs(shape["psf_position_angle_deg"] - 32.0)
    assert min(delta, 180.0 - delta) < 2.0


def test_context_analysis_reports_coherent_detector_gradient():
    rows = []
    for index, angle in enumerate((225.0, 230.0, 220.0, 228.0, 224.0, 232.0)):
        rows.append(
            {
                "frame_id": index + 1,
                "mjd": 60000.0 + index / 1440.0,
                "selected_model": "plane",
                "accepted": True,
                "gradient_angle_deg": angle,
                "plane_candidate_gradient_angle_deg": angle,
                "gradient_psf_axis_delta_deg": 12.0 + index,
                "psf_fwhm_pix_median": 3.0 + 0.1 * index,
                "psf_ellipticity_median": 0.1 + 0.01 * index,
                "plane_candidate_spatial_peak_to_peak": 0.05 + 0.005 * index,
                "background_median": 4000.0 + 10.0 * index,
            }
        )
    rows.append(
        {
            "frame_id": 20,
            "mjd": 60000.02,
            "selected_model": "constant",
            "accepted": True,
            "psf_fwhm_pix_median": 2.8,
            "psf_ellipticity_median": 0.08,
            "plane_candidate_spatial_peak_to_peak": 0.01,
            "background_median": 3900.0,
        }
    )

    analysis = analyze_aperture_frame_context(rows)

    assert analysis["status"] == "OK"
    assert analysis["n_plane_frames"] == 6
    assert analysis["plane_gradient_resultant_length"] > 0.98
    assert analysis["plane_gradient_sign_consistency"] == 1.0
    assert analysis["n_plane_candidate_frames"] == 6
    assert analysis["plane_candidate_gradient_resultant_length"] > 0.98
    assert analysis["plane_candidate_gradient_sign_consistency"] == 1.0
    assert analysis["plane_gradient_psf_axis_delta_median_deg"] == pytest.approx(14.5)
    assert "coherent_detector_gradient_direction" in analysis["interpretation_hints"]
    assert "coherent_plane_candidate_gradient_direction" in analysis["interpretation_hints"]
    assert "gradient_aligned_with_psf_major_axis" in analysis["interpretation_hints"]
    assert analysis["causal_inference_allowed"] is False


def test_constant_selection_retains_rejected_plane_candidate_for_diagnostics():
    width = 1000
    height = 800
    rows = []
    for ix in range(7):
        for iy in range(6):
            x = 60 + ix * 130
            y = 50 + iy * 120
            xn = 2.0 * x / (width - 1) - 1.0
            yn = 2.0 * y / (height - 1) - 1.0
            # A weak plane is fit but deliberately fails the spatial-amplitude gate.
            rows.append({"x": x, "y": y, "delta": -0.2 + 0.0005 * xn - 0.0004 * yn})

    model = fit_aperture_correction_model(
        rows,
        image_width=width,
        image_height=height,
        min_calibrators=20,
        min_plane_calibrators=30,
        min_plane_bic_improvement=-100.0,
        min_spatial_peak_to_peak=0.01,
    )

    assert model["selected_model"] == "constant"
    assert model["plane_candidate_coefficients"] is not None
    assert model["plane_candidate_spatial_peak_to_peak"] is not None
    assert model["plane_candidate_spatial_peak_to_peak"] < 0.01
