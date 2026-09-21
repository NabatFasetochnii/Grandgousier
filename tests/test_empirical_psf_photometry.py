import math
import numpy as np

from grandgousier.photometry.empirical_psf import (
    EmpiricalPsfField,
    interpolate_empirical_template,
    measure_empirical_psf_source,
)


def _asymmetric_template(radius=8, reference_radius=5.0):
    y, x = np.indices((2 * radius + 1, 2 * radius + 1), dtype=float)
    dx = x - radius
    dy = y - radius
    core = np.exp(-0.5 * ((dx / 1.55) ** 2 + (dy / 1.25) ** 2))
    coma = 0.22 * np.exp(-0.5 * (((dx - 2.2) / 2.4) ** 2 + ((dy + 0.7) / 1.5) ** 2))
    p = core + coma
    ref = dx * dx + dy * dy <= reference_radius**2
    return p / np.sum(p[ref])


def _field():
    p = _asymmetric_template()
    templates = np.stack([[p, p, p], [p, p, p], [p, p, p]], axis=0)
    return EmpiricalPsfField(
        x_nodes=np.asarray([0.0, 50.0, 100.0]),
        y_nodes=np.asarray([0.0, 50.0, 100.0]),
        templates=templates,
        reference_counts=np.full((3, 3), 32, dtype=int),
        stamp_radius_pix=8,
        reference_radius_pix=5.0,
        n_input_references=100,
        n_usable_references=96,
    )


def _sample_template_on_image(template, x0, y0, shape=(81, 81)):
    from grandgousier.photometry.empirical_psf import _sample_template
    yy, xx = np.indices(shape, dtype=float)
    return _sample_template(template, xx - x0, yy - y0)


def test_interpolated_empirical_template_keeps_reference_aperture_normalization():
    field = _field()
    template, flags = interpolate_empirical_template(field, x=37.0, y=68.0)
    r = field.stamp_radius_pix
    yy, xx = np.indices(template.shape, dtype=float)
    ref = (xx - r) ** 2 + (yy - r) ** 2 <= field.reference_radius_pix**2
    assert abs(float(np.sum(template[ref])) - 1.0) < 1e-10
    assert flags["empirical_psf_local_reference_count_min"] == 32


def test_empirical_psf_recovers_reference_aperture_flux_for_asymmetric_psf():
    field = _field()
    template, _ = interpolate_empirical_template(field, x=40.0, y=41.0)
    true_flux = 18000.0
    x0, y0 = 40.35, 40.70
    background = 125.0
    data = background + true_flux * _sample_template_on_image(template, x0, y0)
    result = measure_empirical_psf_source(
        data,
        x=40.0,
        y=41.0,
        field=field,
        annulus_inner_pix=11.0,
        annulus_outer_pix=15.0,
        exptime=30.0,
        gain_e_per_adu=1.5,
        read_noise_e=7.0,
        scintillation_enabled=False,
        centroid_search_radius_pix=0.75,
        centroid_grid_steps=7,
    )
    assert result.flux is not None
    assert abs(result.flux / true_flux - 1.0) < 0.03
    assert abs(result.flags["psf_fit_centroid_dx_pix"] - 0.35) <= 0.26
    assert abs(result.flags["psf_fit_centroid_dy_pix"] + 0.30) <= 0.26
    assert result.flags["empirical_psf_reference_radius_pix"] == 5.0
    assert result.flux_err is not None and result.flux_err > 0
