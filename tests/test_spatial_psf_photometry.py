import math
import numpy as np

from grandgousier.photometry.adaptive_aperture import AdaptiveApertureGeometry
from grandgousier.photometry.spatial_psf import _profile_values, measure_spatial_psf_source


def _geometry():
    return AdaptiveApertureGeometry(
        semi_major_pix=4.0,
        semi_minor_pix=2.5,
        equivalent_radius_pix=math.sqrt(10.0),
        theta_deg=28.0,
        local_fwhm_major=4.0,
        local_fwhm_minor=2.5,
        local_fwhm_geom=math.sqrt(10.0),
        local_ellipticity=1.0 - 2.5 / 4.0,
        neighbor_count=24,
        nearest_neighbor_pix=5.0,
    )


def test_moffat_profile_is_positive_and_normalized_on_large_grid():
    yy, xx = np.indices((101, 101), dtype=float)
    dx = xx - 50.0
    dy = yy - 50.0
    p = _profile_values(
        dx,
        dy,
        fwhm_major=4.0,
        fwhm_minor=2.5,
        theta_deg=28.0,
        profile="moffat",
        moffat_beta=2.5,
    )
    assert np.all(p >= 0)
    assert 0.97 < float(np.sum(p)) < 1.03


def test_spatial_psf_recovers_elongated_moffat_flux_and_centroid():
    geom = _geometry()
    ny = nx = 61
    yy, xx = np.indices((ny, nx), dtype=float)
    x0, y0 = 30.35, 29.70
    flux_true = 25000.0
    background = 120.0
    p = _profile_values(
        xx - x0,
        yy - y0,
        fwhm_major=geom.local_fwhm_major,
        fwhm_minor=geom.local_fwhm_minor,
        theta_deg=geom.theta_deg,
        profile="moffat",
        moffat_beta=2.5,
    )
    data = background + flux_true * p
    result = measure_spatial_psf_source(
        data,
        x=30.0,
        y=30.0,
        geometry=geom,
        annulus_inner_pix=12.0,
        annulus_outer_pix=18.0,
        exptime=30.0,
        gain_e_per_adu=1.5,
        read_noise_e=7.0,
        scintillation_enabled=False,
        centroid_search_radius_pix=0.75,
        centroid_grid_steps=7,
    )
    assert result.flux is not None
    assert abs(result.flux / flux_true - 1.0) < 0.03
    assert abs(result.flags["psf_fit_centroid_dx_pix"] - 0.35) <= 0.26
    assert abs(result.flags["psf_fit_centroid_dy_pix"] + 0.30) <= 0.26
    assert result.flux_err is not None and result.flux_err > 0
    assert result.flags["psf_fit_noise_equivalent_pixels"] > 1


def test_spatial_psf_rejects_invalid_profile_configuration():
    data = np.ones((31, 31), dtype=float)
    try:
        measure_spatial_psf_source(
            data,
            x=15.0,
            y=15.0,
            geometry=_geometry(),
            annulus_inner_pix=8.0,
            annulus_outer_pix=12.0,
            exptime=30.0,
            profile="lorentz",
        )
    except ValueError as exc:
        assert "profile" in str(exc)
    else:
        raise AssertionError("expected ValueError")
