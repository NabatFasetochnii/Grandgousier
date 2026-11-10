from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

MAGNITUDE_ERROR_FACTOR = 2.5 / math.log(10.0)
MEDIAN_BACKGROUND_VARIANCE_FACTOR = math.pi / 2.0

PHYSICAL_APERTURE_ERROR_MODEL = "physical_aperture_v1"
LEGACY_APERTURE_ERROR_MODEL = "legacy_aperture_v1"
SUPPORTED_APERTURE_ERROR_MODELS = {
    PHYSICAL_APERTURE_ERROR_MODEL,
    LEGACY_APERTURE_ERROR_MODEL,
}


@dataclass(frozen=True)
class ApertureErrorBudget:
    model: str
    gain_e_per_adu: float
    read_noise_e: float
    gain_fallback_used: bool
    read_noise_fallback_used: bool
    n_aperture_pixels: int
    n_annulus_pixels: int
    background_estimator_variance_factor: float
    empirical_background_floor_enabled: bool
    fractional_flux_floor: float
    scintillation_enabled: bool
    scintillation_available: bool
    scintillation_fractional_sigma: float
    scintillation_coefficient: float
    telescope_aperture_m: float | None
    site_altitude_m: float | None
    airmass: float | None
    exptime_s: float | None
    source_poisson_variance_adu2: float
    empirical_background_variance_adu2_per_pix: float
    detector_sky_variance_adu2_per_pix: float
    detector_read_variance_adu2_per_pix: float
    detector_background_variance_adu2_per_pix: float
    selected_background_variance_adu2_per_pix: float
    selected_background_variance_source: str
    aperture_background_variance_adu2: float
    background_estimate_variance_adu2: float
    fractional_floor_variance_adu2: float
    scintillation_variance_adu2: float
    physical_total_variance_adu2: float
    legacy_total_variance_adu2: float | None
    physical_flux_err_adu: float | None
    legacy_flux_err_adu: float | None
    physical_mag_err: float | None
    legacy_mag_err: float | None
    selected_flux_err_adu: float | None
    selected_mag_err: float | None
    physical_to_legacy_mag_err_ratio: float | None
    source_poisson_fraction: float | None
    aperture_background_fraction: float | None
    background_estimate_fraction: float | None
    fractional_floor_fraction: float | None
    scintillation_fraction: float | None
    _include_legacy_comparison: bool = field(default=True, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        include_legacy_comparison = values.pop("_include_legacy_comparison", True)
        if not include_legacy_comparison:
            for key in (
                "legacy_total_variance_adu2",
                "legacy_flux_err_adu",
                "legacy_mag_err",
                "physical_to_legacy_mag_err_ratio",
            ):
                values.pop(key, None)
        return values

    def to_flags(self, *, prefix: str = "random_error_") -> dict[str, Any]:
        return {f"{prefix}{key}": value for key, value in self.to_dict().items()}


def aperture_error_budget(
    *,
    net_flux_adu: float,
    local_background_adu: float | None,
    annulus_sigma_adu: float | None,
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
    """Return a physically decomposed random-error budget for aperture photometry.

    Variances are expressed in ADU². The source Poisson term is converted from
    electrons using the detector gain. The per-pixel sky/read term is predicted
    from the local background and read noise, then optionally floored by the
    robust annulus scatter so flat-field residuals and other measured pixel noise
    are not silently ignored. The uncertainty of the annulus-median background
    estimate is propagated separately using ``pi/2`` by default. Optional
    fractional flat/intrapixel and Young-type scintillation terms are represented
    as flux-proportional variances rather than being mixed into frame-common ZP.

    By default, the legacy model is evaluated in parallel for A/B provenance. It
    reproduces the pre-0.5.37.0 formula
    ``max(flux, 0) + n_ap * annulus_sigma**2``. Set
    ``include_legacy_comparison=False`` to omit those comparison values while
    retaining the selected model's output and all physical terms.
    """

    if model not in SUPPORTED_APERTURE_ERROR_MODELS:
        raise ValueError(
            f"Unsupported aperture random-error model: {model!r}; "
            f"expected one of {sorted(SUPPORTED_APERTURE_ERROR_MODELS)}"
        )
    if n_aperture_pixels < 1:
        raise ValueError("n_aperture_pixels must be >= 1")
    if n_annulus_pixels < 1:
        raise ValueError("n_annulus_pixels must be >= 1")
    if not math.isfinite(float(background_estimator_variance_factor)):
        raise ValueError("background_estimator_variance_factor must be finite")
    if float(background_estimator_variance_factor) < 0:
        raise ValueError("background_estimator_variance_factor must be non-negative")
    if not math.isfinite(float(fractional_flux_floor)) or float(fractional_flux_floor) < 0:
        raise ValueError("fractional_flux_floor must be finite and non-negative")
    if not math.isfinite(float(scintillation_coefficient)) or float(scintillation_coefficient) < 0:
        raise ValueError("scintillation_coefficient must be finite and non-negative")

    flux = _finite_or_default(net_flux_adu, 0.0)
    background = _finite_or_default(local_background_adu, 0.0)
    annulus_sigma = max(0.0, _finite_or_default(annulus_sigma_adu, 0.0))

    gain = _positive_or_none(gain_e_per_adu)
    gain_fallback_used = gain is None
    if gain is None:
        gain = 1.0

    read_noise = _nonnegative_or_none(read_noise_e)
    read_noise_fallback_used = read_noise is None
    if read_noise is None:
        read_noise = 0.0

    source_poisson_variance = max(0.0, flux) / gain
    empirical_background_variance = annulus_sigma**2
    detector_sky_variance = max(0.0, background) / gain
    detector_read_variance = (read_noise / gain) ** 2
    detector_background_variance = detector_sky_variance + detector_read_variance

    if empirical_background_floor and empirical_background_variance >= detector_background_variance:
        selected_background_variance = empirical_background_variance
        selected_background_source = "empirical_annulus"
    else:
        selected_background_variance = detector_background_variance
        selected_background_source = "detector_prediction"

    n_ap = int(n_aperture_pixels)
    n_sky = int(n_annulus_pixels)
    estimator_factor = float(background_estimator_variance_factor)
    aperture_background_variance = n_ap * selected_background_variance
    background_estimate_variance = (
        estimator_factor * (n_ap**2) * selected_background_variance / n_sky
    )
    fractional_floor = float(fractional_flux_floor)
    fractional_floor_variance = (max(0.0, flux) * fractional_floor) ** 2
    scintillation_fractional_sigma = _scintillation_fractional_sigma(
        enabled=bool(scintillation_enabled),
        coefficient=float(scintillation_coefficient),
        telescope_aperture_m=telescope_aperture_m,
        site_altitude_m=site_altitude_m,
        airmass=airmass,
        exptime_s=exptime_s,
    )
    scintillation_available = scintillation_fractional_sigma is not None
    scintillation_sigma = float(scintillation_fractional_sigma or 0.0)
    scintillation_variance = (max(0.0, flux) * scintillation_sigma) ** 2
    physical_total_variance = (
        source_poisson_variance
        + aperture_background_variance
        + background_estimate_variance
        + fractional_floor_variance
        + scintillation_variance
    )

    # A selected legacy model still needs its value to produce the requested
    # physical scalar output, even when A/B comparison fields are disabled.
    legacy_required = bool(include_legacy_comparison) or model == LEGACY_APERTURE_ERROR_MODEL
    legacy_total_variance = (
        max(0.0, flux) + n_ap * empirical_background_variance
        if legacy_required
        else None
    )

    physical_flux_err = _sqrt_or_none(physical_total_variance)
    legacy_flux_err = (
        _sqrt_or_none(legacy_total_variance)
        if legacy_total_variance is not None
        else None
    )
    physical_mag_err = _magnitude_error(physical_flux_err, flux)
    legacy_mag_err = _magnitude_error(legacy_flux_err, flux)

    if model == PHYSICAL_APERTURE_ERROR_MODEL:
        selected_flux_err = physical_flux_err
        selected_mag_err = physical_mag_err
    else:
        assert legacy_flux_err is not None
        selected_flux_err = legacy_flux_err
        selected_mag_err = legacy_mag_err

    ratio = (
        _ratio_or_none(physical_mag_err, legacy_mag_err)
        if include_legacy_comparison
        else None
    )
    source_fraction = _ratio_or_none(source_poisson_variance, physical_total_variance)
    aperture_fraction = _ratio_or_none(aperture_background_variance, physical_total_variance)
    estimate_fraction = _ratio_or_none(background_estimate_variance, physical_total_variance)
    floor_fraction = _ratio_or_none(fractional_floor_variance, physical_total_variance)
    scintillation_fraction = _ratio_or_none(scintillation_variance, physical_total_variance)

    return ApertureErrorBudget(
        model=model,
        gain_e_per_adu=float(gain),
        read_noise_e=float(read_noise),
        gain_fallback_used=bool(gain_fallback_used),
        read_noise_fallback_used=bool(read_noise_fallback_used),
        n_aperture_pixels=n_ap,
        n_annulus_pixels=n_sky,
        background_estimator_variance_factor=estimator_factor,
        empirical_background_floor_enabled=bool(empirical_background_floor),
        fractional_flux_floor=fractional_floor,
        scintillation_enabled=bool(scintillation_enabled),
        scintillation_available=bool(scintillation_available),
        scintillation_fractional_sigma=scintillation_sigma,
        scintillation_coefficient=float(scintillation_coefficient),
        telescope_aperture_m=_positive_or_none(telescope_aperture_m),
        site_altitude_m=_finite_or_none(site_altitude_m),
        airmass=_positive_or_none(airmass),
        exptime_s=_positive_or_none(exptime_s),
        source_poisson_variance_adu2=float(source_poisson_variance),
        empirical_background_variance_adu2_per_pix=float(empirical_background_variance),
        detector_sky_variance_adu2_per_pix=float(detector_sky_variance),
        detector_read_variance_adu2_per_pix=float(detector_read_variance),
        detector_background_variance_adu2_per_pix=float(detector_background_variance),
        selected_background_variance_adu2_per_pix=float(selected_background_variance),
        selected_background_variance_source=selected_background_source,
        aperture_background_variance_adu2=float(aperture_background_variance),
        background_estimate_variance_adu2=float(background_estimate_variance),
        fractional_floor_variance_adu2=float(fractional_floor_variance),
        scintillation_variance_adu2=float(scintillation_variance),
        physical_total_variance_adu2=float(physical_total_variance),
        legacy_total_variance_adu2=(
            float(legacy_total_variance)
            if legacy_total_variance is not None
            else None
        ),
        physical_flux_err_adu=physical_flux_err,
        legacy_flux_err_adu=legacy_flux_err,
        physical_mag_err=physical_mag_err,
        legacy_mag_err=legacy_mag_err,
        selected_flux_err_adu=selected_flux_err,
        selected_mag_err=selected_mag_err,
        physical_to_legacy_mag_err_ratio=ratio,
        source_poisson_fraction=source_fraction,
        aperture_background_fraction=aperture_fraction,
        background_estimate_fraction=estimate_fraction,
        fractional_floor_fraction=floor_fraction,
        scintillation_fraction=scintillation_fraction,
        _include_legacy_comparison=bool(include_legacy_comparison),
    )


def _scintillation_fractional_sigma(
    *,
    enabled: bool,
    coefficient: float,
    telescope_aperture_m: float | None,
    site_altitude_m: float | None,
    airmass: float | None,
    exptime_s: float | None,
) -> float | None:
    if not enabled:
        return None
    aperture_m = _positive_or_none(telescope_aperture_m)
    exposure = _positive_or_none(exptime_s)
    chi = _positive_or_none(airmass)
    altitude = _finite_or_none(site_altitude_m)
    if aperture_m is None or exposure is None or chi is None or altitude is None:
        return None
    aperture_cm = aperture_m * 100.0
    value = (
        float(coefficient)
        * aperture_cm ** (-2.0 / 3.0)
        * chi ** 1.75
        * math.exp(-altitude / 8000.0)
        * (2.0 * exposure) ** -0.5
    )
    return float(value) if math.isfinite(value) and value >= 0 else None


def _finite_or_none(value: float | None) -> float | None:
    parsed = _finite_or_default(value, math.nan)
    return parsed if math.isfinite(parsed) else None


def _finite_or_default(value: float | None, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if math.isfinite(parsed) else float(default)


def _positive_or_none(value: float | None) -> float | None:
    parsed = _finite_or_default(value, math.nan)
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _nonnegative_or_none(value: float | None) -> float | None:
    parsed = _finite_or_default(value, math.nan)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _sqrt_or_none(variance: float) -> float | None:
    if not math.isfinite(variance) or variance <= 0:
        return None
    return float(math.sqrt(variance))


def _magnitude_error(flux_err: float | None, flux: float) -> float | None:
    if flux_err is None or not math.isfinite(flux_err) or flux_err <= 0:
        return None
    if not math.isfinite(flux) or flux <= 0:
        return None
    return float(MAGNITUDE_ERROR_FACTOR * flux_err / flux)


def _ratio_or_none(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if not math.isfinite(float(numerator)) or not math.isfinite(float(denominator)):
        return None
    if float(denominator) == 0:
        return None
    return float(numerator) / float(denominator)
