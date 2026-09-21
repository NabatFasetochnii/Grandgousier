import math

import numpy as np

from grandgousier.photometry.adaptive_aperture import (
    AdaptiveApertureGeometry,
    _PsfReference,
    interpolate_adaptive_geometry,
    measure_elliptical_source,
)


def _elliptical_gaussian(shape=(81, 81), *, x0=40.0, y0=40.0, amp=1000.0, background=100.0, sigma_major=3.0, sigma_minor=1.4, theta_deg=30.0):
    yy, xx = np.indices(shape, dtype=float)
    theta = math.radians(theta_deg)
    c, s = math.cos(theta), math.sin(theta)
    dx = xx - x0
    dy = yy - y0
    u = c * dx + s * dy
    v = -s * dx + c * dy
    return background + amp * np.exp(-0.5 * ((u / sigma_major) ** 2 + (v / sigma_minor) ** 2))


def _circular_flux(data, *, x=40.0, y=40.0, radius=3.0, background=100.0):
    yy, xx = np.indices(data.shape, dtype=float)
    mask = (xx - x) ** 2 + (yy - y) ** 2 <= radius**2
    return float(np.sum(data[mask] - background))


def test_elliptical_aperture_recovers_more_flux_for_elongated_psf():
    data = _elliptical_gaussian()
    fwhm_factor = 2.0 * math.sqrt(2.0 * math.log(2.0))
    fwhm_major = fwhm_factor * 3.0
    fwhm_minor = fwhm_factor * 1.4
    equiv = math.sqrt(fwhm_major * fwhm_minor)
    q = fwhm_major / fwhm_minor
    geom = AdaptiveApertureGeometry(
        semi_major_pix=equiv * math.sqrt(q),
        semi_minor_pix=equiv / math.sqrt(q),
        equivalent_radius_pix=equiv,
        theta_deg=30.0,
        local_fwhm_major=fwhm_major,
        local_fwhm_minor=fwhm_minor,
        local_fwhm_geom=equiv,
        local_ellipticity=1.0 - fwhm_minor / fwhm_major,
        neighbor_count=24,
        nearest_neighbor_pix=2.0,
    )
    adaptive = measure_elliptical_source(
        data,
        x=40.0,
        y=40.0,
        geometry=geom,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        exptime=1.0,
    )
    circular_flux = _circular_flux(data)
    assert adaptive.flux is not None
    assert adaptive.flux > circular_flux * 1.2
    assert adaptive.n_aperture_pixels > 0
    assert adaptive.flags["photometry_estimator"] == "local_psf_ellipse_v1"


def test_interpolation_tracks_local_orientation_and_clamps_geometry():
    theta = math.radians(60.0)
    c, s = math.cos(theta), math.sin(theta)
    sigma_major = 4.0
    sigma_minor = 1.0
    mxx = sigma_major**2 * c**2 + sigma_minor**2 * s**2
    myy = sigma_major**2 * s**2 + sigma_minor**2 * c**2
    mxy = (sigma_major**2 - sigma_minor**2) * s * c
    refs = [
        _PsfReference(float(x), float(y), mxx, myy, mxy, 4.0)
        for x in (10, 30, 50, 70)
        for y in (10, 30, 50, 70)
    ]
    geom = interpolate_adaptive_geometry(
        refs,
        x=42.0,
        y=38.0,
        base_radius_pix=3.0,
        aperture_fwhm_factor=1.0,
        interpolation_neighbors=8,
        max_axis_ratio=2.5,
        max_radius_scale=1.5,
    )
    assert geom is not None
    assert geom.axis_ratio_clipped is True
    assert geom.radius_clipped is True
    assert geom.semi_major_pix / geom.semi_minor_pix <= 2.5 + 1e-12
    assert geom.equivalent_radius_pix <= 4.5 + 1e-12
    # Ellipse orientation is axial (180-degree periodic), so compare modulo 180.
    assert abs(((geom.theta_deg - 60.0 + 90.0) % 180.0) - 90.0) < 1e-6


def test_dynamic_background_annulus_moves_outside_long_axis():
    data = _elliptical_gaussian(sigma_major=4.0, sigma_minor=1.5)
    geom = AdaptiveApertureGeometry(
        semi_major_pix=9.0,
        semi_minor_pix=3.0,
        equivalent_radius_pix=math.sqrt(27.0),
        theta_deg=0.0,
        local_fwhm_major=9.0,
        local_fwhm_minor=3.0,
        local_fwhm_geom=math.sqrt(27.0),
        local_ellipticity=2 / 3,
        neighbor_count=24,
        nearest_neighbor_pix=1.0,
    )
    measured = measure_elliptical_source(
        data,
        x=40.0,
        y=40.0,
        geometry=geom,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        exptime=1.0,
    )
    assert measured.annulus_inner_pix > geom.semi_major_pix
    assert measured.annulus_outer_pix > measured.annulus_inner_pix


def test_elliptical_aperture_propagates_standard_error_budget():
    data = _elliptical_gaussian(sigma_major=2.5, sigma_minor=1.5)
    geom = AdaptiveApertureGeometry(
        semi_major_pix=5.0,
        semi_minor_pix=3.0,
        equivalent_radius_pix=math.sqrt(15.0),
        theta_deg=20.0,
        local_fwhm_major=5.8,
        local_fwhm_minor=3.5,
        local_fwhm_geom=4.5,
        local_ellipticity=0.4,
        neighbor_count=24,
        nearest_neighbor_pix=2.0,
    )
    measurement = measure_elliptical_source(
        data,
        x=40.0,
        y=40.0,
        geometry=geom,
        annulus_inner_pix=4.0,
        annulus_outer_pix=7.0,
        exptime=30.0,
        gain_e_per_adu=1.5,
        read_noise_e=7.0,
        scintillation_enabled=False,
    )
    assert measurement.flux is not None and measurement.flux > 0
    assert measurement.flux_err is not None and measurement.flux_err > 0
    assert measurement.mag_err is not None and measurement.mag_err > 0
    assert measurement.error_budget is not None
    assert measurement.error_budget["model"] == "physical_aperture_v1"
    assert measurement.error_budget["n_aperture_pixels"] == measurement.n_aperture_pixels
    assert measurement.error_budget["n_annulus_pixels"] == measurement.n_annulus_pixels
    assert measurement.annulus_inner_pix > geom.semi_major_pix
    assert math.isclose(measurement.aperture_radius_pix, geom.equivalent_radius_pix)
