from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from grandgousier.photometry.error_model import (
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    aperture_error_budget,
)
from grandgousier.quality.psf_field import _select_shape_sources, measure_psf_shape

EMPIRICAL_PSF_ALGORITHM = "spatial_empirical_psf_grid_v1"


@dataclass(frozen=True, slots=True)
class EmpiricalPsfField:
    x_nodes: np.ndarray
    y_nodes: np.ndarray
    templates: np.ndarray
    reference_counts: np.ndarray
    stamp_radius_pix: int
    reference_radius_pix: float
    n_input_references: int
    n_usable_references: int

    @property
    def grid_size_x(self) -> int:
        return int(self.x_nodes.size)

    @property
    def grid_size_y(self) -> int:
        return int(self.y_nodes.size)


@dataclass(frozen=True, slots=True)
class EmpiricalPsfMeasurement:
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_err: float | None
    local_background: float | None
    aperture_radius_pix: float
    flags: dict[str, Any]
    error_budget: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _ReferenceStamp:
    x: float
    y: float
    template: np.ndarray


def build_empirical_psf_field(
    data: np.ndarray,
    detection_rows: list[dict[str, Any]],
    *,
    min_snr: float = 20.0,
    max_sources: int = 500,
    min_isolation_pix: float = 12.0,
    grid_size: int = 3,
    stamp_radius_pix: int = 8,
    reference_radius_pix: float = 5.0,
    references_per_node: int = 48,
    min_references_per_node: int = 12,
    background_annulus_inner_pix: float = 9.0,
    background_annulus_outer_pix: float = 13.0,
) -> EmpiricalPsfField | None:
    """Build a spatial empirical PSF grid directly from stellar image stamps.

    Each reference star is sub-pixel registered using the PSF-shape centroid and
    normalized to the flux inside ``reference_radius_pix``.  Grid-node templates
    are robust medians of the nearest registered reference stamps.  Consequently
    the fitted amplitude of the resulting template is already on the same scale
    as the chosen large reference aperture, rather than an extrapolated analytic
    total-flux scale.
    """

    if data.ndim != 2:
        raise ValueError("data must be a 2-D image")
    if grid_size < 2:
        raise ValueError("grid_size must be >= 2")
    if stamp_radius_pix < 3:
        raise ValueError("stamp_radius_pix must be >= 3")
    if reference_radius_pix <= 0 or reference_radius_pix >= stamp_radius_pix:
        raise ValueError("reference_radius_pix must be > 0 and < stamp_radius_pix")
    if references_per_node < 3:
        raise ValueError("references_per_node must be >= 3")
    if min_references_per_node < 3 or min_references_per_node > references_per_node:
        raise ValueError("invalid min_references_per_node")
    if background_annulus_inner_pix <= reference_radius_pix:
        raise ValueError("background annulus must start outside reference aperture")
    if background_annulus_outer_pix <= background_annulus_inner_pix:
        raise ValueError("background_annulus_outer_pix must be greater than inner")

    ny, nx = data.shape
    edge_margin = float(max(stamp_radius_pix + 2, math.ceil(background_annulus_outer_pix) + 1))
    selected = _select_shape_sources(
        detection_rows,
        width=int(nx),
        height=int(ny),
        min_snr=float(min_snr),
        max_sources=int(max_sources),
        edge_margin=edge_margin,
        min_isolation_pix=float(min_isolation_pix),
    )
    references: list[_ReferenceStamp] = []
    for row in selected:
        x = _finite_float(row.get("x"))
        y = _finite_float(row.get("y"))
        if x is None or y is None:
            continue
        shape = measure_psf_shape(
            data,
            x=x,
            y=y,
            cutout_radius_pix=max(int(stamp_radius_pix), 7),
        )
        if shape is None:
            continue
        stamp = _registered_normalized_stamp(
            data,
            x=float(shape.x_centroid),
            y=float(shape.y_centroid),
            stamp_radius_pix=int(stamp_radius_pix),
            reference_radius_pix=float(reference_radius_pix),
            background_annulus_inner_pix=float(background_annulus_inner_pix),
            background_annulus_outer_pix=float(background_annulus_outer_pix),
        )
        if stamp is None:
            continue
        references.append(_ReferenceStamp(x=float(shape.x_centroid), y=float(shape.y_centroid), template=stamp))

    if len(references) < int(min_references_per_node):
        return None

    x_nodes = np.linspace(0.0, float(nx - 1), int(grid_size), dtype=float)
    y_nodes = np.linspace(0.0, float(ny - 1), int(grid_size), dtype=float)
    templates = np.zeros((grid_size, grid_size, 2 * stamp_radius_pix + 1, 2 * stamp_radius_pix + 1), dtype=float)
    counts = np.zeros((grid_size, grid_size), dtype=int)
    coords = np.asarray([(ref.x / max(nx - 1, 1), ref.y / max(ny - 1, 1)) for ref in references], dtype=float)
    stack = np.stack([ref.template for ref in references], axis=0)

    for iy, node_y in enumerate(y_nodes):
        yn = node_y / max(ny - 1, 1)
        for ix, node_x in enumerate(x_nodes):
            xn = node_x / max(nx - 1, 1)
            d2 = (coords[:, 0] - xn) ** 2 + (coords[:, 1] - yn) ** 2
            order = np.argsort(d2)
            take = order[: min(int(references_per_node), len(order))]
            if take.size < int(min_references_per_node):
                return None
            node_template = _robust_template(stack[take], reference_radius_pix=float(reference_radius_pix))
            if node_template is None:
                return None
            templates[iy, ix] = node_template
            counts[iy, ix] = int(take.size)

    return EmpiricalPsfField(
        x_nodes=x_nodes,
        y_nodes=y_nodes,
        templates=templates,
        reference_counts=counts,
        stamp_radius_pix=int(stamp_radius_pix),
        reference_radius_pix=float(reference_radius_pix),
        n_input_references=int(len(selected)),
        n_usable_references=int(len(references)),
    )


def interpolate_empirical_template(field: EmpiricalPsfField, *, x: float, y: float) -> tuple[np.ndarray, dict[str, Any]] | None:
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    ix0, ix1, tx = _bracket(field.x_nodes, float(x))
    iy0, iy1, ty = _bracket(field.y_nodes, float(y))
    t00 = field.templates[iy0, ix0]
    t10 = field.templates[iy0, ix1]
    t01 = field.templates[iy1, ix0]
    t11 = field.templates[iy1, ix1]
    template = (1.0 - ty) * ((1.0 - tx) * t00 + tx * t10) + ty * ((1.0 - tx) * t01 + tx * t11)
    template = np.asarray(template, dtype=float)
    template[~np.isfinite(template)] = 0.0
    template = np.clip(template, 0.0, None)
    template = _renormalize_reference_aperture(template, field.reference_radius_pix)
    if template is None:
        return None
    counts = [
        int(field.reference_counts[iy0, ix0]), int(field.reference_counts[iy0, ix1]),
        int(field.reference_counts[iy1, ix0]), int(field.reference_counts[iy1, ix1]),
    ]
    return template, {
        "empirical_psf_grid_ix0": int(ix0), "empirical_psf_grid_ix1": int(ix1),
        "empirical_psf_grid_iy0": int(iy0), "empirical_psf_grid_iy1": int(iy1),
        "empirical_psf_grid_tx": float(tx), "empirical_psf_grid_ty": float(ty),
        "empirical_psf_local_reference_count_min": int(min(counts)),
        "empirical_psf_local_reference_count_max": int(max(counts)),
    }


def measure_empirical_psf_source(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    field: EmpiricalPsfField,
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
    centroid_search_radius_pix: float = 0.75,
    centroid_grid_steps: int = 3,
    residual_clip_sigma: float = 5.0,
    residual_clip_iterations: int = 2,
) -> EmpiricalPsfMeasurement:
    flags: dict[str, Any] = {"photometry_estimator": EMPIRICAL_PSF_ALGORITHM}

    def empty(reason: str) -> EmpiricalPsfMeasurement:
        flags[reason] = True
        return EmpiricalPsfMeasurement(None, None, None, None, None, 0.0, flags, None)

    local = interpolate_empirical_template(field, x=x, y=y)
    if local is None:
        return empty("empirical_psf_interpolation_failed")
    template, interpolation_flags = local
    flags.update(interpolation_flags)
    r = int(field.stamp_radius_pix)
    effective_annulus_inner = max(float(annulus_inner_pix), float(field.reference_radius_pix) + 2.0)
    effective_annulus_outer = max(float(annulus_outer_pix), effective_annulus_inner + 4.0)
    pad = int(math.ceil(max(effective_annulus_outer, r + centroid_search_radius_pix) + 1.0))
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
    if annulus_values.size < 8:
        return empty("too_few_annulus_pixels")
    background = float(np.nanmedian(annulus_values))
    annulus_sigma = float(1.4826 * np.nanmedian(np.abs(annulus_values - background)))
    if not math.isfinite(annulus_sigma) or annulus_sigma <= 0:
        annulus_sigma = float(np.nanstd(annulus_values))
    if not math.isfinite(annulus_sigma) or annulus_sigma <= 0:
        annulus_sigma = 1.0
    centered = cutout - background

    offsets = _offset_grid(float(centroid_search_radius_pix), int(centroid_grid_steps))
    best: dict[str, Any] | None = None
    for off_y in offsets:
        for off_x in offsets:
            dx = dx0 - float(off_x)
            dy = dy0 - float(off_y)
            p = _sample_template(template, dx, dy)
            mask = finite & (np.abs(dx) <= r) & (np.abs(dy) <= r) & (p > 0)
            if np.count_nonzero(mask) < 12:
                continue
            pv = p[mask]
            dv = centered[mask]
            denom = float(np.sum(pv * pv))
            if denom <= 0:
                continue
            flux = float(np.sum(pv * dv) / denom)
            residual = dv - flux * pv
            score = float(np.mean((residual / max(annulus_sigma, 1e-9)) ** 2))
            candidate = {"flux": flux, "off_x": float(off_x), "off_y": float(off_y), "score": score, "p": pv, "d": dv}
            if best is None or score < float(best["score"]):
                best = candidate
    if best is None:
        return empty("empirical_psf_fit_failed")

    p = np.asarray(best.pop("p"), dtype=float)
    d = np.asarray(best.pop("d"), dtype=float)
    keep = np.ones(p.size, dtype=bool)
    core = p >= (0.05 * float(np.max(p)))
    flux = float(best["flux"])
    residual = d - flux * p
    for _ in range(int(residual_clip_iterations)):
        used = residual[keep]
        med = float(np.median(used))
        mad = float(1.4826 * np.median(np.abs(used - med)))
        scale = max(float(annulus_sigma), mad if math.isfinite(mad) else 0.0, 1e-9)
        new_keep = core | (np.abs(residual - med) <= float(residual_clip_sigma) * scale)
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
        denom = float(np.sum(p[keep] * p[keep]))
        if denom <= 0 or np.count_nonzero(keep) < 8:
            break
        flux = float(np.sum(p[keep] * d[keep]) / denom)
        residual = d - flux * p

    denom = float(np.sum(p[keep] * p[keep]))
    if denom <= 0:
        return empty("empirical_psf_degenerate_fit")
    noise_equiv = float(1.0 / denom)
    resid_used = residual[keep]
    resid_med = float(np.median(resid_used))
    resid_mad = float(1.4826 * np.median(np.abs(resid_used - resid_med)))
    budget = aperture_error_budget(
        net_flux_adu=flux,
        local_background_adu=background,
        annulus_sigma_adu=annulus_sigma,
        n_aperture_pixels=max(1, int(math.ceil(noise_equiv))),
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
        core_mask = finite & (rr20 <= 2.5**2)
        if np.any(core_mask) and float(np.nanmax(cutout[core_mask])) >= float(saturation_level_adu):
            flags["saturated_or_near_saturated"] = True

    flags.update({
        "empirical_psf_algorithm": EMPIRICAL_PSF_ALGORITHM,
        "empirical_psf_grid_size_x": int(field.grid_size_x),
        "empirical_psf_grid_size_y": int(field.grid_size_y),
        "empirical_psf_stamp_radius_pix": int(field.stamp_radius_pix),
        "empirical_psf_reference_radius_pix": float(field.reference_radius_pix),
        "empirical_psf_frame_reference_count": int(field.n_usable_references),
        "psf_fit_centroid_dx_pix": float(best["off_x"]),
        "psf_fit_centroid_dy_pix": float(best["off_y"]),
        "psf_fit_score": float(best["score"]),
        "psf_fit_residual_mad_adu": float(resid_mad),
        "psf_fit_n_pixels": int(np.count_nonzero(keep)),
        "psf_fit_rejected_pixels": int(p.size - np.count_nonzero(keep)),
        "psf_fit_noise_equivalent_pixels": float(noise_equiv),
        "psf_fit_template_sum": float(np.sum(p[keep])),
        "psf_fit_annulus_inner_pix": float(effective_annulus_inner),
        "psf_fit_annulus_outer_pix": float(effective_annulus_outer),
    })
    error_budget = budget.to_dict()
    error_budget["psf_noise_equivalent_pixels"] = float(noise_equiv)
    error_budget["psf_error_semantics"] = "empirical_matched_profile_noise_equivalent_area_v1"
    error_budget["empirical_psf_flux_scale"] = "reference_aperture_normalized_v1"
    return EmpiricalPsfMeasurement(
        flux=flux,
        flux_err=budget.selected_flux_err_adu,
        mag_inst=mag,
        mag_err=mag_err,
        local_background=background,
        aperture_radius_pix=float(math.sqrt(noise_equiv / math.pi)),
        flags=flags,
        error_budget=error_budget,
    )


def _registered_normalized_stamp(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    stamp_radius_pix: int,
    reference_radius_pix: float,
    background_annulus_inner_pix: float,
    background_annulus_outer_pix: float,
) -> np.ndarray | None:
    ny, nx = data.shape
    pad = int(math.ceil(background_annulus_outer_pix + 2.0))
    x0 = max(0, int(math.floor(x)) - pad)
    x1 = min(nx, int(math.floor(x)) + pad + 1)
    y0 = max(0, int(math.floor(y)) - pad)
    y1 = min(ny, int(math.floor(y)) + pad + 1)
    cutout = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.indices(cutout.shape, dtype=float)
    dx = xx + x0 - float(x)
    dy = yy + y0 - float(y)
    rr2 = dx * dx + dy * dy
    finite = np.isfinite(cutout)
    ann = finite & (rr2 >= background_annulus_inner_pix**2) & (rr2 <= background_annulus_outer_pix**2)
    vals = cutout[ann]
    if vals.size < 8:
        return None
    background = float(np.nanmedian(vals))
    offsets = np.arange(-stamp_radius_pix, stamp_radius_pix + 1, dtype=float)
    gy, gx = np.meshgrid(offsets, offsets, indexing="ij")
    sampled = _bilinear_sample_image(np.asarray(data, dtype=float), x + gx, y + gy) - background
    if not np.all(np.isfinite(sampled)):
        return None
    ref_mask = gx * gx + gy * gy <= float(reference_radius_pix) ** 2
    ref_flux = float(np.sum(sampled[ref_mask]))
    if not math.isfinite(ref_flux) or ref_flux <= 0:
        return None
    return sampled / ref_flux


def _robust_template(stack: np.ndarray, *, reference_radius_pix: float) -> np.ndarray | None:
    if stack.ndim != 3 or stack.shape[0] < 3:
        return None
    median = np.nanmedian(stack, axis=0)
    absdev = np.abs(stack - median[None, :, :])
    mad = 1.4826 * np.nanmedian(absdev, axis=0)
    scale = np.where(np.isfinite(mad) & (mad > 0), mad, np.inf)
    keep = absdev <= 4.0 * scale[None, :, :]
    clipped = np.where(keep, stack, np.nan)
    template = np.nanmean(clipped, axis=0)
    template = np.where(np.isfinite(template), template, median)
    template = np.clip(template, 0.0, None)
    return _renormalize_reference_aperture(template, reference_radius_pix)


def _renormalize_reference_aperture(template: np.ndarray, reference_radius_pix: float) -> np.ndarray | None:
    h, w = template.shape
    cy = (h - 1) / 2.0
    cx = (w - 1) / 2.0
    yy, xx = np.indices(template.shape, dtype=float)
    ref = (xx - cx) ** 2 + (yy - cy) ** 2 <= float(reference_radius_pix) ** 2
    norm = float(np.sum(template[ref]))
    if not math.isfinite(norm) or norm <= 0:
        return None
    return np.asarray(template, dtype=float) / norm


def _sample_template(template: np.ndarray, dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    r_y = (template.shape[0] - 1) / 2.0
    r_x = (template.shape[1] - 1) / 2.0
    return _bilinear_sample_array(template, dx + r_x, dy + r_y, fill=0.0)


def _bilinear_sample_image(data: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return _bilinear_sample_array(data, x, y, fill=np.nan)


def _bilinear_sample_array(data: np.ndarray, x: np.ndarray, y: np.ndarray, *, fill: float) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < data.shape[1]) & (y1 < data.shape[0])
    out = np.full(x.shape, float(fill), dtype=float)
    if not np.any(valid):
        return out
    xv = x[valid]
    yv = y[valid]
    x0v, x1v = x0[valid], x1[valid]
    y0v, y1v = y0[valid], y1[valid]
    tx = xv - x0v
    ty = yv - y0v
    v00 = data[y0v, x0v]
    v10 = data[y0v, x1v]
    v01 = data[y1v, x0v]
    v11 = data[y1v, x1v]
    out[valid] = (1 - ty) * ((1 - tx) * v00 + tx * v10) + ty * ((1 - tx) * v01 + tx * v11)
    return out


def _offset_grid(radius: float, steps: int) -> np.ndarray:
    if radius <= 0 or steps <= 1:
        return np.asarray([0.0], dtype=float)
    offsets = np.linspace(-float(radius), float(radius), int(steps), dtype=float)
    if not np.any(np.isclose(offsets, 0.0)):
        offsets = np.sort(np.concatenate([offsets, np.asarray([0.0])]))
    return offsets


def _bracket(nodes: np.ndarray, value: float) -> tuple[int, int, float]:
    if value <= float(nodes[0]):
        return 0, 0, 0.0
    if value >= float(nodes[-1]):
        idx = int(nodes.size - 1)
        return idx, idx, 0.0
    hi = int(np.searchsorted(nodes, value, side="right"))
    lo = hi - 1
    span = float(nodes[hi] - nodes[lo])
    t = 0.0 if span <= 0 else (float(value) - float(nodes[lo])) / span
    return lo, hi, float(t)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
