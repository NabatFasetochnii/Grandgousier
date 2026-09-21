from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from grandgousier.photometry.adaptive_aperture import AdaptiveApertureGeometry
from grandgousier.photometry.error_model import (
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    aperture_error_budget,
)

SPATIAL_PSF_ALGORITHM = "local_elliptical_moffat_fit_v1"


@dataclass(frozen=True, slots=True)
class SpatialPsfMeasurement:
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_err: float | None
    local_background: float | None
    aperture_radius_pix: float
    flags: dict[str, Any]
    error_budget: dict[str, Any] | None = None


def measure_spatial_psf_source(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    geometry: AdaptiveApertureGeometry,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime: float | None,
    zero_point: float = 0.0,
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
    profile: str = "moffat",
    moffat_beta: float = 2.5,
    fit_radius_fwhm: float = 2.5,
    min_fit_radius_pix: float = 5.0,
    max_fit_radius_pix: float = 14.0,
    centroid_search_radius_pix: float = 0.75,
    centroid_grid_steps: int = 3,
    residual_clip_sigma: float = 5.0,
    residual_clip_iterations: int = 2,
) -> SpatialPsfMeasurement:
    """Fit a local spatially varying PSF profile at a forced catalog position.

    The shape (major/minor FWHM and angle) is supplied by the local PSF field.
    Only flux and a small centroid offset are fitted.  The profile is normalized
    to total flux, so the fitted amplitude is a total-flux estimator rather than
    an aperture sum.  The random-error model reuses the physical aperture budget
    with the matched-filter noise-equivalent pixel count, which gives a directly
    comparable Poisson/background/scintillation decomposition without pretending
    that the fit uses a literal circular aperture.
    """

    profile = str(profile or "moffat").strip().lower()
    if profile not in {"moffat", "gaussian"}:
        raise ValueError("profile must be 'moffat' or 'gaussian'")
    if moffat_beta <= 1.0:
        raise ValueError("moffat_beta must be > 1")
    if fit_radius_fwhm <= 0 or min_fit_radius_pix <= 0 or max_fit_radius_pix <= 0:
        raise ValueError("PSF fit radii must be positive")
    if max_fit_radius_pix < min_fit_radius_pix:
        raise ValueError("max_fit_radius_pix must be >= min_fit_radius_pix")
    if centroid_search_radius_pix < 0:
        raise ValueError("centroid_search_radius_pix must be non-negative")
    if centroid_grid_steps < 1:
        raise ValueError("centroid_grid_steps must be >= 1")
    if residual_clip_sigma <= 0 or residual_clip_iterations < 0:
        raise ValueError("invalid residual clipping configuration")

    flags: dict[str, Any] = {
        "photometry_estimator": SPATIAL_PSF_ALGORITHM,
        "psf_profile": profile,
    }

    def empty(reason: str) -> SpatialPsfMeasurement:
        flags[reason] = True
        return SpatialPsfMeasurement(
            flux=None,
            flux_err=None,
            mag_inst=None,
            mag_err=None,
            local_background=None,
            aperture_radius_pix=0.0,
            flags=flags,
            error_budget=None,
        )

    if data.ndim != 2 or not (math.isfinite(x) and math.isfinite(y)):
        return empty("bad_xy")
    if geometry.local_fwhm_major <= 0 or geometry.local_fwhm_minor <= 0:
        return empty("bad_local_psf")

    fit_radius = min(
        float(max_fit_radius_pix),
        max(float(min_fit_radius_pix), float(fit_radius_fwhm) * float(geometry.local_fwhm_major)),
    )
    effective_annulus_inner = max(float(annulus_inner_pix), fit_radius + 1.5)
    effective_annulus_outer = max(float(annulus_outer_pix), effective_annulus_inner + 4.0)
    pad = int(math.ceil(effective_annulus_outer + centroid_search_radius_pix + 1.0))
    ny, nx = data.shape
    x0 = max(0, int(math.floor(x)) - pad)
    x1 = min(nx, int(math.floor(x)) + pad + 1)
    y0 = max(0, int(math.floor(y)) - pad)
    y1 = min(ny, int(math.floor(y)) + pad + 1)
    if x0 >= x1 or y0 >= y1:
        return empty("outside_image")

    cutout = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.indices(cutout.shape, dtype=float)
    dx0 = xx + x0 - float(x)
    dy0 = yy + y0 - float(y)
    rr20 = dx0 * dx0 + dy0 * dy0
    finite = np.isfinite(cutout)
    annulus_mask = finite & (rr20 >= effective_annulus_inner**2) & (rr20 <= effective_annulus_outer**2)
    annulus_values = cutout[annulus_mask]
    if annulus_values.size < 5:
        return empty("too_few_annulus_pixels")
    background = float(np.nanmedian(annulus_values))
    annulus_sigma = float(1.4826 * np.nanmedian(np.abs(annulus_values - background)))
    if not math.isfinite(annulus_sigma) or annulus_sigma <= 0:
        annulus_sigma = float(np.nanstd(annulus_values))
    if not math.isfinite(annulus_sigma) or annulus_sigma <= 0:
        annulus_sigma = 1.0

    fit_window = finite & (rr20 <= (fit_radius + centroid_search_radius_pix) ** 2)
    if np.count_nonzero(fit_window) < 12:
        return empty("too_few_fit_pixels")

    if centroid_grid_steps == 1 or centroid_search_radius_pix == 0:
        offsets = np.asarray([0.0], dtype=float)
    else:
        offsets = np.linspace(
            -float(centroid_search_radius_pix),
            float(centroid_search_radius_pix),
            int(centroid_grid_steps),
        )
        if not np.any(np.isclose(offsets, 0.0)):
            offsets = np.sort(np.concatenate([offsets, np.asarray([0.0])]))

    best: dict[str, Any] | None = None
    centered = cutout - background
    # Centroid selection must see the whole stellar core.  Robust clipping during
    # the grid search can otherwise make a wrong centroid look artificially good
    # by discarding the very pixels that disagree most strongly.
    for off_y in offsets:
        for off_x in offsets:
            dx = dx0 - float(off_x)
            dy = dy0 - float(off_y)
            rr2 = dx * dx + dy * dy
            mask = fit_window & (rr2 <= fit_radius**2)
            if np.count_nonzero(mask) < 12:
                continue
            p = _profile_values(
                dx[mask],
                dy[mask],
                fwhm_major=float(geometry.local_fwhm_major),
                fwhm_minor=float(geometry.local_fwhm_minor),
                theta_deg=float(geometry.theta_deg),
                profile=profile,
                moffat_beta=float(moffat_beta),
            )
            d = centered[mask]
            valid = np.isfinite(p) & np.isfinite(d) & (p >= 0)
            if np.count_nonzero(valid) < 10:
                continue
            p = p[valid]
            d = d[valid]
            denom = float(np.sum(p * p))
            if denom <= 0:
                continue
            flux = float(np.sum(p * d) / denom)
            residual = d - flux * p
            score = float(np.mean((residual / max(float(annulus_sigma), 1e-9)) ** 2))
            candidate = {
                "flux": flux,
                "off_x": float(off_x),
                "off_y": float(off_y),
                "score": score,
                "p": p,
                "d": d,
            }
            if best is None or candidate["score"] < best["score"]:
                best = candidate

    if best is not None:
        # Once the centroid is fixed, remove isolated contaminated wing pixels while
        # protecting the PSF core.  This handles faint neighbours/cosmic rays without
        # allowing the fit to hide a centroid/profile mismatch.
        p = np.asarray(best.pop("p"), dtype=float)
        d = np.asarray(best.pop("d"), dtype=float)
        keep = np.ones(p.size, dtype=bool)
        core = p >= (0.05 * float(np.max(p)))
        flux = float(best["flux"])
        residual = d - flux * p
        for _ in range(int(residual_clip_iterations)):
            resid_used = residual[keep]
            med = float(np.median(resid_used))
            mad = float(1.4826 * np.median(np.abs(resid_used - med)))
            scale = max(float(annulus_sigma), mad if math.isfinite(mad) else 0.0, 1e-9)
            candidate_keep = core | (np.abs(residual - med) <= float(residual_clip_sigma) * scale)
            if np.array_equal(candidate_keep, keep):
                break
            keep = candidate_keep
            denom = float(np.sum(p[keep] * p[keep]))
            if denom <= 0 or np.count_nonzero(keep) < 8:
                break
            flux = float(np.sum(p[keep] * d[keep]) / denom)
            residual = d - flux * p
        denom = float(np.sum(p[keep] * p[keep]))
        resid_used = residual[keep]
        resid_med = float(np.median(resid_used))
        resid_mad = float(1.4826 * np.median(np.abs(resid_used - resid_med)))
        best.update({
            "flux": flux,
            "residual_mad": resid_mad,
            "n_fit_pixels": int(np.count_nonzero(keep)),
            "n_rejected_pixels": int(p.size - np.count_nonzero(keep)),
            "noise_equiv_pixels": float(1.0 / denom),
            "capture_fraction": float(np.sum(p[keep])),
        })

    if best is None:
        return empty("psf_fit_failed")

    flux = float(best["flux"])
    noise_equiv_pixels = max(1, int(math.ceil(float(best["noise_equiv_pixels"]))))
    budget = aperture_error_budget(
        net_flux_adu=flux,
        local_background_adu=background,
        annulus_sigma_adu=annulus_sigma,
        n_aperture_pixels=noise_equiv_pixels,
        n_annulus_pixels=max(1, int(annulus_values.size)),
        gain_e_per_adu=gain_e_per_adu,
        read_noise_e=read_noise_e,
        model=random_error_model,
        background_estimator_variance_factor=background_estimator_variance_factor,
        empirical_background_floor=empirical_background_floor,
        fractional_flux_floor=fractional_flux_floor,
        scintillation_enabled=scintillation_enabled,
        scintillation_coefficient=scintillation_coefficient,
        telescope_aperture_m=telescope_aperture_m,
        site_altitude_m=site_altitude_m,
        airmass=airmass,
        exptime_s=exptime,
    )

    mag = None
    mag_err = None
    if flux > 0:
        rate = flux / exptime if exptime and exptime > 0 else flux
        if rate > 0:
            mag = float(-2.5 * math.log10(rate) + zero_point)
            mag_err = budget.selected_mag_err
    else:
        flags["non_positive_flux"] = True

    if saturation_level_adu is not None:
        core_radius = max(1.5, 0.75 * float(geometry.local_fwhm_major))
        core = cutout[finite & (rr20 <= core_radius**2)]
        if core.size and float(np.nanmax(core)) >= float(saturation_level_adu):
            flags["saturated_or_near_saturated"] = True
            flags["psf_core_max_adu"] = float(np.nanmax(core))

    equivalent_noise_radius = float(math.sqrt(float(best["noise_equiv_pixels"]) / math.pi))
    flags.update(
        {
            "local_fwhm_major": float(geometry.local_fwhm_major),
            "local_fwhm_minor": float(geometry.local_fwhm_minor),
            "local_fwhm_geom": float(geometry.local_fwhm_geom),
            "local_ellipticity": float(geometry.local_ellipticity),
            "theta_deg": float(geometry.theta_deg),
            "psf_neighbor_count": int(geometry.neighbor_count),
            "psf_nearest_neighbor_pix": float(geometry.nearest_neighbor_pix),
            "psf_fit_profile": profile,
            "psf_fit_moffat_beta": float(moffat_beta) if profile == "moffat" else None,
            "psf_fit_radius_pix": float(fit_radius),
            "psf_fit_centroid_dx_pix": float(best["off_x"]),
            "psf_fit_centroid_dy_pix": float(best["off_y"]),
            "psf_fit_residual_mad_adu": float(best["residual_mad"]),
            "psf_fit_score": float(best["score"]),
            "psf_fit_n_pixels": int(best["n_fit_pixels"]),
            "psf_fit_rejected_pixels": int(best["n_rejected_pixels"]),
            "psf_fit_noise_equivalent_pixels": float(best["noise_equiv_pixels"]),
            "psf_fit_capture_fraction": float(best["capture_fraction"]),
            "psf_fit_annulus_inner_pix": float(effective_annulus_inner),
            "psf_fit_annulus_outer_pix": float(effective_annulus_outer),
        }
    )
    error_budget = budget.to_dict()
    error_budget["psf_noise_equivalent_pixels"] = float(best["noise_equiv_pixels"])
    error_budget["psf_error_semantics"] = "matched_profile_noise_equivalent_area_v1"
    return SpatialPsfMeasurement(
        flux=flux,
        flux_err=budget.selected_flux_err_adu,
        mag_inst=mag,
        mag_err=mag_err,
        local_background=background,
        aperture_radius_pix=equivalent_noise_radius,
        flags=flags,
        error_budget=error_budget,
    )


def _profile_values(
    dx: np.ndarray,
    dy: np.ndarray,
    *,
    fwhm_major: float,
    fwhm_minor: float,
    theta_deg: float,
    profile: str,
    moffat_beta: float,
) -> np.ndarray:
    theta = math.radians(float(theta_deg))
    c, s = math.cos(theta), math.sin(theta)
    u = c * dx + s * dy
    v = -s * dx + c * dy
    if profile == "gaussian":
        factor = 2.0 * math.sqrt(2.0 * math.log(2.0))
        sigma_major = max(float(fwhm_major) / factor, 1e-6)
        sigma_minor = max(float(fwhm_minor) / factor, 1e-6)
        r2 = (u / sigma_major) ** 2 + (v / sigma_minor) ** 2
        norm = 1.0 / (2.0 * math.pi * sigma_major * sigma_minor)
        return norm * np.exp(-0.5 * r2)

    beta = float(moffat_beta)
    scale = 2.0 * math.sqrt(2.0 ** (1.0 / beta) - 1.0)
    alpha_major = max(float(fwhm_major) / scale, 1e-6)
    alpha_minor = max(float(fwhm_minor) / scale, 1e-6)
    r2 = (u / alpha_major) ** 2 + (v / alpha_minor) ** 2
    norm = (beta - 1.0) / (math.pi * alpha_major * alpha_minor)
    return norm * np.power(1.0 + r2, -beta)
