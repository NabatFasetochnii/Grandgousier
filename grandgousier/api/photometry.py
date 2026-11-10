"""Stable public photometry primitives.

This module is a deliberately small facade over scientifically validated Grandgousier
implementations.  It contains no database or filesystem orchestration.  Public units
and coordinate conventions are documented on each function and are part of the API
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from grandgousier.photometry.error_model import (
    ApertureErrorBudget,
    LEGACY_APERTURE_ERROR_MODEL,
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    aperture_error_budget as _aperture_error_budget,
)


@dataclass(frozen=True, slots=True)
class CircularApertureMeasurement:
    """Result of one public circular-aperture measurement.

    ``flux_adu`` and ``flux_err_adu`` are background-subtracted ADU values.
    ``local_background_adu_per_pix`` is the robust local annulus background in
    ADU/pixel.  ``instrumental_mag`` uses the supplied exposure time and zero point.
    ``flags`` preserves the existing measurement diagnostics without interpretation.
    """

    flux_adu: float | None
    flux_err_adu: float | None
    instrumental_mag: float | None
    mag_err: float | None
    local_background_adu_per_pix: float | None
    aperture_radius_pix: float
    flags: dict[str, Any]
    error_budget: ApertureErrorBudget | None


def compute_aperture_error_budget(
    *,
    net_flux_adu: float,
    local_background_adu_per_pix: float | None,
    annulus_sigma_adu_per_pix: float | None,
    n_aperture_pixels: int,
    n_annulus_pixels: int,
    gain_e_per_adu: float | None,
    read_noise_e: float | None,
    model: str = PHYSICAL_APERTURE_ERROR_MODEL,
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    empirical_background_floor: bool = True,
    fractional_flux_floor: float = 0.0,
    scintillation_enabled: bool = True,
    scintillation_coefficient: float = 0.09,
    telescope_aperture_m: float | None = None,
    site_altitude_m: float | None = None,
    airmass: float | None = None,
    exptime_s: float | None = None,
    include_legacy_comparison: bool = True,
) -> ApertureErrorBudget:
    """Compute the validated aperture-photometry random-error budget.

    Inputs use ADU/electron/second units as named.  The function performs no I/O and
    does not access the Grandgousier database.  It delegates directly to the
    production ``physical_aperture_v1``/legacy A/B implementation, so validation and
    exception semantics are identical to the Stage-2 kernel. Set
    ``include_legacy_comparison=False`` to omit only the parallel legacy budget and
    ratio; selected and physical values are unchanged.

    Raises
    ------
    ValueError
        For unsupported error models or invalid pixel-count/non-negative parameters.
    """

    return _aperture_error_budget(
        net_flux_adu=net_flux_adu,
        local_background_adu=local_background_adu_per_pix,
        annulus_sigma_adu=annulus_sigma_adu_per_pix,
        n_aperture_pixels=n_aperture_pixels,
        n_annulus_pixels=n_annulus_pixels,
        gain_e_per_adu=gain_e_per_adu,
        read_noise_e=read_noise_e,
        model=model,
        background_estimator_variance_factor=background_estimator_variance_factor,
        empirical_background_floor=empirical_background_floor,
        fractional_flux_floor=fractional_flux_floor,
        scintillation_enabled=scintillation_enabled,
        scintillation_coefficient=scintillation_coefficient,
        telescope_aperture_m=telescope_aperture_m,
        site_altitude_m=site_altitude_m,
        airmass=airmass,
        exptime_s=exptime_s,
        include_legacy_comparison=include_legacy_comparison,
    )


def measure_circular_aperture(
    data: np.ndarray,
    *,
    x_pix: float,
    y_pix: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime_s: float | None = None,
    instrumental_zero_point_mag: float = 0.0,
    saturation_level_adu: float | None = None,
    gain_e_per_adu: float | None = None,
    read_noise_e: float | None = None,
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL,
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    empirical_background_floor: bool = True,
    fractional_flux_floor: float = 0.0,
    scintillation_enabled: bool = True,
    scintillation_coefficient: float = 0.09,
    telescope_aperture_m: float | None = None,
    site_altitude_m: float | None = None,
    airmass: float | None = None,
) -> CircularApertureMeasurement:
    """Measure one source with the validated circular Stage-2 aperture kernel.

    ``data`` is a two-dimensional image in ADU with NumPy shape ``(ny, nx)``.
    Coordinates are zero-based array coordinates: ``x_pix`` increases along columns
    and ``y_pix`` along rows.  Radii are pixels.  ``exptime_s`` is seconds.

    This function performs no database/filesystem writes.  The underlying measurement
    implementation is imported lazily because its Stage-2 module also contains the
    database-oriented batch runner; the numerical kernel itself is unchanged.
    """

    from grandgousier.photometry.forced_aperture import measure_one_source

    result = measure_one_source(
        data,
        x=x_pix,
        y=y_pix,
        aperture_radius_pix=aperture_radius_pix,
        annulus_inner_pix=annulus_inner_pix,
        annulus_outer_pix=annulus_outer_pix,
        exptime=exptime_s,
        zero_point=instrumental_zero_point_mag,
        saturation_level_adu=saturation_level_adu,
        gain_e_per_adu=gain_e_per_adu,
        read_noise_e=read_noise_e,
        random_error_model=random_error_model,
        background_estimator_variance_factor=background_estimator_variance_factor,
        empirical_background_floor=empirical_background_floor,
        fractional_flux_floor=fractional_flux_floor,
        scintillation_enabled=scintillation_enabled,
        scintillation_coefficient=scintillation_coefficient,
        telescope_aperture_m=telescope_aperture_m,
        site_altitude_m=site_altitude_m,
        airmass=airmass,
    )
    budget = ApertureErrorBudget(**result.error_budget) if result.error_budget is not None else None
    return CircularApertureMeasurement(
        flux_adu=result.flux,
        flux_err_adu=result.flux_err,
        instrumental_mag=result.mag_inst,
        mag_err=result.mag_err,
        local_background_adu_per_pix=result.local_background,
        aperture_radius_pix=result.aperture_radius_pix,
        flags=dict(result.flags),
        error_budget=budget,
    )


__all__ = [
    "ApertureErrorBudget",
    "CircularApertureMeasurement",
    "LEGACY_APERTURE_ERROR_MODEL",
    "MEDIAN_BACKGROUND_VARIANCE_FACTOR",
    "PHYSICAL_APERTURE_ERROR_MODEL",
    "compute_aperture_error_budget",
    "measure_circular_aperture",
]
