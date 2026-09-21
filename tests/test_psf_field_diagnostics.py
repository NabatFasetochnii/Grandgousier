from __future__ import annotations

import math

import numpy as np

from grandgousier.quality.psf_field import (
    _isolated_source_detection_ids,
    fit_spatial_model,
    measure_psf_shape,
)


def _elliptical_gaussian(
    *,
    size: int = 61,
    x0: float = 30.2,
    y0: float = 29.7,
    sigma_major: float = 2.0,
    sigma_minor: float = 1.2,
    theta_deg: float = 28.0,
    amplitude: float = 5000.0,
    background: float = 100.0,
) -> np.ndarray:
    yy, xx = np.indices((size, size), dtype=float)
    theta = math.radians(theta_deg)
    dx = xx - x0
    dy = yy - y0
    u = math.cos(theta) * dx + math.sin(theta) * dy
    v = -math.sin(theta) * dx + math.cos(theta) * dy
    return background + amplitude * np.exp(
        -0.5 * ((u / sigma_major) ** 2 + (v / sigma_minor) ** 2)
    )


def test_measure_psf_shape_recovers_elliptical_gaussian_moments():
    image = _elliptical_gaussian()
    shape = measure_psf_shape(
        image,
        x=30.2,
        y=29.7,
        local_background=100.0,
        cutout_radius_pix=7,
    )
    assert shape is not None
    assert abs(shape.x_centroid - 30.2) < 0.03
    assert abs(shape.y_centroid - 29.7) < 0.03
    assert abs(shape.fwhm_major - 2.354820045 * 2.0) < 0.12
    assert abs(shape.fwhm_minor - 2.354820045 * 1.2) < 0.08
    assert abs(shape.ellipticity - (1.0 - 1.2 / 2.0)) < 0.03
    angle_delta = min(
        abs(shape.theta_deg - 28.0),
        abs(shape.theta_deg - 28.0 + 180.0),
        abs(shape.theta_deg - 28.0 - 180.0),
    )
    assert angle_delta < 1.0
    assert shape.growth_delta_mag is not None
    assert shape.growth_delta_mag > 0


def test_fit_spatial_plane_recovers_known_detector_gradient_with_outlier():
    grid = np.linspace(-1.0, 1.0, 9)
    x, y = np.meshgrid(grid, grid)
    x = x.ravel()
    y = y.ravel()
    q = 3.2 + 0.35 * x - 0.55 * y
    q = q.copy()
    q[0] += 8.0

    fit = fit_spatial_model(x, y, q, model="plane", clip_sigma=3.5, n_iterations=4)
    assert fit["status"] == "OK"
    assert fit["n_used"] == q.size - 1
    assert abs(fit["coefficients"]["c0"] - 3.2) < 1e-10
    assert abs(fit["coefficients"]["cx"] - 0.35) < 1e-10
    assert abs(fit["coefficients"]["cy"] + 0.55) < 1e-10
    assert abs(fit["spatial_peak_to_peak"] - 1.8) < 1e-10


def test_fit_spatial_quadratic_recovers_curvature():
    rng = np.random.default_rng(123)
    x = rng.uniform(-1, 1, 200)
    y = rng.uniform(-1, 1, 200)
    q = 2.0 + 0.1 * x - 0.2 * y + 0.3 * x * x + 0.15 * x * y - 0.25 * y * y
    fit = fit_spatial_model(x, y, q, model="quadratic")
    assert fit["status"] == "OK"
    expected = {
        "c0": 2.0,
        "cx": 0.1,
        "cy": -0.2,
        "cxx": 0.3,
        "cxy": 0.15,
        "cyy": -0.25,
    }
    for key, value in expected.items():
        assert abs(fit["coefficients"][key] - value) < 1e-10


def test_isolation_spatial_hash_rejects_close_pair_only():
    rows = [
        {"source_detection_id": 1, "x": 10.0, "y": 10.0},
        {"source_detection_id": 2, "x": 15.0, "y": 10.0},
        {"source_detection_id": 3, "x": 40.0, "y": 40.0},
        {"source_detection_id": 4, "x": 80.0, "y": 15.0},
    ]
    isolated = _isolated_source_detection_ids(rows, min_distance=12.0)
    assert isolated == {3, 4}
