import math

import numpy as np
import pytest

from grandgousier.photometry.error_model import (
    LEGACY_APERTURE_ERROR_MODEL,
    PHYSICAL_APERTURE_ERROR_MODEL,
    aperture_error_budget,
)
from grandgousier.photometry.forced_aperture import measure_one_source


def test_physical_aperture_error_budget_decomposes_variance():
    budget = aperture_error_budget(
        net_flux_adu=10_000.0,
        local_background_adu=100.0,
        annulus_sigma_adu=20.0,
        n_aperture_pixels=28,
        n_annulus_pixels=200,
        gain_e_per_adu=2.0,
        read_noise_e=5.0,
        model=PHYSICAL_APERTURE_ERROR_MODEL,
    )

    source = 10_000.0 / 2.0
    empirical = 20.0**2
    detector = 100.0 / 2.0 + (5.0 / 2.0) ** 2
    aperture_background = 28 * empirical
    background_estimate = (math.pi / 2.0) * 28**2 / 200 * empirical
    total = source + aperture_background + background_estimate

    assert budget.detector_background_variance_adu2_per_pix == pytest.approx(detector)
    assert budget.selected_background_variance_source == "empirical_annulus"
    assert budget.aperture_background_variance_adu2 == pytest.approx(aperture_background)
    assert budget.background_estimate_variance_adu2 == pytest.approx(background_estimate)
    assert budget.physical_total_variance_adu2 == pytest.approx(total)
    assert budget.selected_flux_err_adu == pytest.approx(math.sqrt(total))
    assert budget.selected_mag_err == pytest.approx(
        (2.5 / math.log(10.0)) * math.sqrt(total) / 10_000.0
    )
    assert (
        budget.source_poisson_fraction
        + budget.aperture_background_fraction
        + budget.background_estimate_fraction
    ) == pytest.approx(1.0)


def test_detector_prediction_is_used_when_annulus_scatter_is_too_small():
    budget = aperture_error_budget(
        net_flux_adu=1_000.0,
        local_background_adu=400.0,
        annulus_sigma_adu=1.0,
        n_aperture_pixels=20,
        n_annulus_pixels=100,
        gain_e_per_adu=2.0,
        read_noise_e=10.0,
    )

    assert budget.detector_background_variance_adu2_per_pix == pytest.approx(225.0)
    assert budget.selected_background_variance_adu2_per_pix == pytest.approx(225.0)
    assert budget.selected_background_variance_source == "detector_prediction"


def test_legacy_comparison_exactly_reproduces_pre_05370_formula():
    flux = 1_500.0
    annulus_sigma = 7.0
    n_ap = 30
    budget = aperture_error_budget(
        net_flux_adu=flux,
        local_background_adu=100.0,
        annulus_sigma_adu=annulus_sigma,
        n_aperture_pixels=n_ap,
        n_annulus_pixels=150,
        gain_e_per_adu=1.4,
        read_noise_e=9.0,
        model=LEGACY_APERTURE_ERROR_MODEL,
    )

    expected_variance = flux + n_ap * annulus_sigma**2
    expected_flux_err = math.sqrt(expected_variance)
    expected_mag_err = (2.5 / math.log(10.0)) * expected_flux_err / flux

    assert budget.legacy_total_variance_adu2 == pytest.approx(expected_variance)
    assert budget.selected_flux_err_adu == pytest.approx(expected_flux_err)
    assert budget.selected_mag_err == pytest.approx(expected_mag_err)
    assert budget.physical_mag_err is not None
    assert budget.physical_to_legacy_mag_err_ratio == pytest.approx(
        budget.physical_mag_err / expected_mag_err
    )


def test_legacy_comparison_off_preserves_physical_measurement_scalars():
    yy, xx = np.indices((41, 41), dtype=float)
    data = 100.0 + 1200.0 * np.exp(-((xx - 20.2) ** 2 + (yy - 19.7) ** 2) / 4.0)
    common = {
        "x": 20.2,
        "y": 19.7,
        "aperture_radius_pix": 3.5,
        "annulus_inner_pix": 7.0,
        "annulus_outer_pix": 10.0,
        "exptime": 30.0,
        "zero_point": 0.0,
        "saturation_level_adu": None,
        "gain_e_per_adu": 1.7,
        "read_noise_e": 4.0,
        "telescope_aperture_m": None,
        "site_altitude_m": None,
        "airmass": None,
        "scintillation_enabled": False,
    }

    diagnostic = measure_one_source(data, **common)
    bulk = measure_one_source(data, include_legacy_comparison=False, **common)

    assert bulk.flux == diagnostic.flux
    assert bulk.flux_err == diagnostic.flux_err
    assert bulk.mag_inst == diagnostic.mag_inst
    assert bulk.mag_err == diagnostic.mag_err
    assert bulk.error_budget is not None and diagnostic.error_budget is not None
    for key in (
        "physical_total_variance_adu2",
        "physical_flux_err_adu",
        "physical_mag_err",
        "selected_flux_err_adu",
        "selected_mag_err",
    ):
        assert bulk.error_budget[key] == diagnostic.error_budget[key]
    assert "legacy_total_variance_adu2" in diagnostic.error_budget
    assert "physical_to_legacy_mag_err_ratio" in diagnostic.error_budget
    assert "legacy_total_variance_adu2" not in bulk.error_budget
    assert "legacy_mag_err" not in bulk.error_budget
    assert "physical_to_legacy_mag_err_ratio" not in bulk.error_budget


def test_missing_detector_metadata_is_explicit_in_provenance():
    budget = aperture_error_budget(
        net_flux_adu=500.0,
        local_background_adu=0.0,
        annulus_sigma_adu=3.0,
        n_aperture_pixels=10,
        n_annulus_pixels=40,
        gain_e_per_adu=None,
        read_noise_e=None,
    )

    assert budget.gain_e_per_adu == 1.0
    assert budget.read_noise_e == 0.0
    assert budget.gain_fallback_used is True
    assert budget.read_noise_fallback_used is True
    flags = budget.to_flags()
    assert flags["random_error_model"] == PHYSICAL_APERTURE_ERROR_MODEL
    assert flags["random_error_gain_fallback_used"] is True
    assert flags["random_error_read_noise_fallback_used"] is True


def test_scintillation_is_added_only_when_required_metadata_are_available():
    unavailable = aperture_error_budget(
        net_flux_adu=20_000.0,
        local_background_adu=100.0,
        annulus_sigma_adu=10.0,
        n_aperture_pixels=28,
        n_annulus_pixels=200,
        gain_e_per_adu=1.4,
        read_noise_e=9.0,
        scintillation_enabled=True,
        telescope_aperture_m=None,
        site_altitude_m=290.0,
        airmass=1.2,
        exptime_s=120.0,
    )
    available = aperture_error_budget(
        net_flux_adu=20_000.0,
        local_background_adu=100.0,
        annulus_sigma_adu=10.0,
        n_aperture_pixels=28,
        n_annulus_pixels=200,
        gain_e_per_adu=1.4,
        read_noise_e=9.0,
        scintillation_enabled=True,
        telescope_aperture_m=0.60,
        site_altitude_m=290.0,
        airmass=1.2,
        exptime_s=120.0,
    )

    assert unavailable.scintillation_available is False
    assert unavailable.scintillation_variance_adu2 == 0.0
    assert available.scintillation_available is True
    assert available.scintillation_fractional_sigma > 0.0
    assert available.scintillation_variance_adu2 > 0.0
    assert available.physical_total_variance_adu2 > unavailable.physical_total_variance_adu2


def test_invalid_model_is_rejected():
    with pytest.raises(ValueError, match="Unsupported aperture random-error model"):
        aperture_error_budget(
            net_flux_adu=1.0,
            local_background_adu=0.0,
            annulus_sigma_adu=1.0,
            n_aperture_pixels=1,
            n_annulus_pixels=1,
            gain_e_per_adu=1.0,
            read_noise_e=0.0,
            model="unknown",
        )
