from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.models import CatalogSource, Frame, FrameCatalogLink
from grandgousier.photometry.error_model import (
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    aperture_error_budget,
)
from grandgousier.quality.psf_field import (
    _load_detection_rows,
    _select_shape_sources,
    measure_psf_shape,
)


ADAPTIVE_APERTURE_ALGORITHM = "local_psf_ellipse_v1"


@dataclass(frozen=True, slots=True)
class AdaptiveApertureGeometry:
    semi_major_pix: float
    semi_minor_pix: float
    equivalent_radius_pix: float
    theta_deg: float
    local_fwhm_major: float
    local_fwhm_minor: float
    local_fwhm_geom: float
    local_ellipticity: float
    neighbor_count: int
    nearest_neighbor_pix: float
    axis_ratio_clipped: bool = False
    radius_clipped: bool = False


@dataclass(frozen=True, slots=True)
class EllipticalApertureMeasurement:
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_err: float | None
    local_background: float | None
    aperture_radius_pix: float
    n_aperture_pixels: int
    n_annulus_pixels: int
    annulus_inner_pix: float
    annulus_outer_pix: float
    flags: dict[str, Any]
    error_budget: dict[str, Any] | None = None


@dataclass
class AdaptiveApertureDiagnosticsResult:
    selected_frames: int = 0
    analyzed_frames: int = 0
    failed_frames: int = 0
    skipped_missing_file: int = 0
    frames_without_psf_model: int = 0
    selected_links: int = 0
    measured_pairs: int = 0
    adaptive_fallbacks: int = 0
    algorithm: str = ADAPTIVE_APERTURE_ALGORITHM
    measurements_csv: str | None = None
    frames_csv: str | None = None
    objects_csv: str | None = None
    summary_json: str | None = None
    field_map_png: str | None = None
    time_series_png: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _PsfReference:
    x: float
    y: float
    mxx: float
    myy: float
    mxy: float
    fwhm_geom: float


@dataclass(frozen=True, slots=True)
class _PsfReferenceField:
    coords: np.ndarray
    mxx: np.ndarray
    myy: np.ndarray
    mxy: np.ndarray

    @property
    def size(self) -> int:
        return int(self.coords.shape[0])


def _make_reference_field(references: list[_PsfReference]) -> _PsfReferenceField:
    return _PsfReferenceField(
        coords=np.asarray([(ref.x, ref.y) for ref in references], dtype=float),
        mxx=np.asarray([ref.mxx for ref in references], dtype=float),
        myy=np.asarray([ref.myy for ref in references], dtype=float),
        mxy=np.asarray([ref.mxy for ref in references], dtype=float),
    )


def analyze_adaptive_aperture(
    session: Session,
    *,
    frame_ids: list[int],
    catalog_name: str | None = None,
    detection_algorithm: str = "numpy_localmax_v1",
    out_dir: str | Path,
    base_aperture_radius_pix: float = 3.0,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    min_psf_snr: float = 20.0,
    max_psf_sources_per_frame: int = 500,
    psf_cutout_radius_pix: int = 7,
    psf_min_isolation_pix: float = 12.0,
    interpolation_neighbors: int = 24,
    min_psf_references: int = 20,
    aperture_fwhm_factor: float = 1.0,
    max_axis_ratio: float = 3.0,
    max_radius_scale: float = 2.5,
    render_plots: bool = True,
) -> AdaptiveApertureDiagnosticsResult:
    """Compare circular and local-PSF adaptive elliptical apertures.

    Version 0.5.44.1 is diagnostic-only.  The adaptive estimator is not written to
    ``measurements`` and cannot become the science calibration implicitly.  The goal
    is to validate the geometry and quantify whether it reduces position-dependent
    variability before introducing permanent multi-estimator storage.
    """

    if not frame_ids:
        raise ValueError("frame_ids must not be empty")
    if base_aperture_radius_pix <= 0:
        raise ValueError("base_aperture_radius_pix must be positive")
    if annulus_inner_pix <= base_aperture_radius_pix:
        raise ValueError("annulus_inner_pix must be greater than base_aperture_radius_pix")
    if annulus_outer_pix <= annulus_inner_pix:
        raise ValueError("annulus_outer_pix must be greater than annulus_inner_pix")
    if interpolation_neighbors < 3:
        raise ValueError("interpolation_neighbors must be >= 3")
    if min_psf_references < 6:
        raise ValueError("min_psf_references must be >= 6")
    if aperture_fwhm_factor <= 0:
        raise ValueError("aperture_fwhm_factor must be positive")
    if max_axis_ratio < 1:
        raise ValueError("max_axis_ratio must be >= 1")
    if max_radius_scale < 1:
        raise ValueError("max_radius_scale must be >= 1")

    from astropy.io import fits
    from grandgousier.photometry.forced_aperture import measure_one_source

    frames = list(
        session.scalars(
            select(Frame).where(Frame.frame_id.in_([int(v) for v in frame_ids])).order_by(Frame.frame_id)
        ).all()
    )
    result = AdaptiveApertureDiagnosticsResult(selected_frames=len(frames))
    if not frames:
        result.warnings.append("no frames selected")
        return result

    links_by_frame = _load_frame_catalog_links(
        session,
        frame_ids=[int(frame.frame_id) for frame in frames],
        catalog_name=catalog_name,
    )
    detections_by_frame = _load_detection_rows(
        session,
        frame_ids=[int(frame.frame_id) for frame in frames],
        algorithm=detection_algorithm,
    )

    records: list[dict[str, Any]] = []
    frame_records: list[dict[str, Any]] = []

    for frame in tqdm(frames, desc="Adaptive aperture diagnostics", unit="frame"):
        frame_id = int(frame.frame_id)
        links = links_by_frame.get(frame_id, [])
        if not links:
            continue
        result.selected_links += len(links)
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        try:
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
            if data.ndim != 2:
                raise ValueError(f"expected 2-D image, got shape={data.shape}")
        except Exception as exc:
            result.failed_frames += 1
            result.warnings.append(f"frame_id={frame_id}: {type(exc).__name__}: {exc}")
            continue

        ny, nx = data.shape
        psf_refs = _build_psf_references(
            data,
            detections_by_frame.get(frame_id, []),
            min_snr=min_psf_snr,
            max_sources=max_psf_sources_per_frame,
            cutout_radius_pix=psf_cutout_radius_pix,
            min_isolation_pix=psf_min_isolation_pix,
        )
        if len(psf_refs) < min_psf_references:
            result.frames_without_psf_model += 1
            result.warnings.append(
                f"frame_id={frame_id}: only {len(psf_refs)} PSF references; adaptive estimator skipped"
            )
            continue
        psf_field = _make_reference_field(psf_refs)

        frame_local: list[dict[str, Any]] = []
        for link in links:
            x = _finite_float(link["x"])
            y = _finite_float(link["y"])
            if x is None or y is None:
                continue
            geometry = interpolate_adaptive_geometry(
                psf_field,
                x=x,
                y=y,
                base_radius_pix=base_aperture_radius_pix,
                aperture_fwhm_factor=aperture_fwhm_factor,
                interpolation_neighbors=interpolation_neighbors,
                max_axis_ratio=max_axis_ratio,
                max_radius_scale=max_radius_scale,
            )
            if geometry is None:
                result.adaptive_fallbacks += 1
                continue

            circular = measure_one_source(
                data,
                x=x,
                y=y,
                aperture_radius_pix=base_aperture_radius_pix,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                exptime=_finite_float(frame.exptime),
                zero_point=0.0,
                saturation_level_adu=None,
                scintillation_enabled=False,
            )
            adaptive = measure_elliptical_source(
                data,
                x=x,
                y=y,
                geometry=geometry,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                exptime=_finite_float(frame.exptime),
                zero_point=0.0,
            )
            delta_mag = None
            if circular.mag_inst is not None and adaptive.mag_inst is not None:
                delta_mag = float(adaptive.mag_inst - circular.mag_inst)

            row = {
                "frame_id": frame_id,
                "date_obs": frame.date_obs.isoformat() if frame.date_obs is not None else None,
                "mjd": _finite_float(frame.mjd),
                "airmass": _finite_float(frame.airmass),
                "catalog_source_id": int(link["catalog_source_id"]),
                "catalog_name": link["catalog_name"],
                "source_id": link["source_id"],
                "x": x,
                "y": y,
                "xnorm": _normalize_coordinate(x, nx),
                "ynorm": _normalize_coordinate(y, ny),
                "circular_flux": circular.flux,
                "circular_mag_inst": circular.mag_inst,
                "adaptive_flux": adaptive.flux,
                "adaptive_mag_inst": adaptive.mag_inst,
                "adaptive_minus_circular_mag": delta_mag,
                "adaptive_semi_major_pix": geometry.semi_major_pix,
                "adaptive_semi_minor_pix": geometry.semi_minor_pix,
                "adaptive_equivalent_radius_pix": geometry.equivalent_radius_pix,
                "adaptive_theta_deg": geometry.theta_deg,
                "local_fwhm_major": geometry.local_fwhm_major,
                "local_fwhm_minor": geometry.local_fwhm_minor,
                "local_fwhm_geom": geometry.local_fwhm_geom,
                "local_ellipticity": geometry.local_ellipticity,
                "psf_neighbor_count": geometry.neighbor_count,
                "psf_nearest_neighbor_pix": geometry.nearest_neighbor_pix,
                "axis_ratio_clipped": geometry.axis_ratio_clipped,
                "radius_clipped": geometry.radius_clipped,
                "adaptive_annulus_inner_pix": adaptive.annulus_inner_pix,
                "adaptive_annulus_outer_pix": adaptive.annulus_outer_pix,
                "adaptive_n_aperture_pixels": adaptive.n_aperture_pixels,
                "circular_flags": json.dumps(circular.flags, sort_keys=True),
                "adaptive_flags": json.dumps(adaptive.flags, sort_keys=True),
            }
            records.append(row)
            frame_local.append(row)
            result.measured_pairs += 1

        if frame_local:
            result.analyzed_frames += 1
            frame_records.append(_summarize_frame(frame_id, frame_local, len(psf_refs)))

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    measurements_path = output / "adaptive_aperture_measurements.csv"
    frames_path = output / "adaptive_aperture_frames.csv"
    objects_path = output / "adaptive_aperture_objects.csv"
    summary_path = output / "adaptive_aperture_summary.json"

    measurement_df = pd.DataFrame(records)
    frame_df = pd.DataFrame(frame_records)
    object_df = _build_object_diagnostics(measurement_df)
    measurement_df.to_csv(measurements_path, index=False)
    frame_df.to_csv(frames_path, index=False)
    object_df.to_csv(objects_path, index=False)
    result.measurements_csv = str(measurements_path)
    result.frames_csv = str(frames_path)
    result.objects_csv = str(objects_path)

    payload = _build_summary(result, measurement_df, frame_df, object_df, parameters={
        "catalog_name": catalog_name,
        "detection_algorithm": detection_algorithm,
        "base_aperture_radius_pix": base_aperture_radius_pix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
        "min_psf_snr": min_psf_snr,
        "max_psf_sources_per_frame": max_psf_sources_per_frame,
        "psf_cutout_radius_pix": psf_cutout_radius_pix,
        "psf_min_isolation_pix": psf_min_isolation_pix,
        "interpolation_neighbors": interpolation_neighbors,
        "min_psf_references": min_psf_references,
        "aperture_fwhm_factor": aperture_fwhm_factor,
        "max_axis_ratio": max_axis_ratio,
        "max_radius_scale": max_radius_scale,
    })
    summary_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    result.summary_json = str(summary_path)

    if render_plots and not measurement_df.empty:
        try:
            field_path = output / "adaptive_aperture_field_maps.png"
            time_path = output / "adaptive_aperture_time_series.png"
            _render_field_maps(measurement_df, object_df, field_path)
            _render_time_series(frame_df, time_path)
            result.field_map_png = str(field_path)
            result.time_series_png = str(time_path)
        except Exception as exc:
            result.warnings.append(f"plotting failed: {type(exc).__name__}: {exc}")

    payload = _build_summary(result, measurement_df, frame_df, object_df, parameters=payload["parameters"])
    summary_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def interpolate_adaptive_geometry(
    references: list[_PsfReference] | _PsfReferenceField,
    *,
    x: float,
    y: float,
    base_radius_pix: float,
    aperture_fwhm_factor: float = 1.0,
    interpolation_neighbors: int = 24,
    max_axis_ratio: float = 3.0,
    max_radius_scale: float = 2.5,
) -> AdaptiveApertureGeometry | None:
    field = references if isinstance(references, _PsfReferenceField) else _make_reference_field(references)
    if field.size < 3:
        return None
    delta = field.coords - np.asarray([x, y], dtype=float)
    dist2 = np.sum(delta * delta, axis=1)
    k = min(int(interpolation_neighbors), field.size)
    idx = np.argpartition(dist2, k - 1)[:k]
    d2 = dist2[idx]
    # A finite softening prevents a single bright diagnostic star from completely
    # dominating a target that lands almost exactly on the same coordinate.
    soft2 = 4.0
    weights = 1.0 / (d2 + soft2)
    weights /= np.sum(weights)
    mxx = float(np.sum(weights * field.mxx[idx]))
    myy = float(np.sum(weights * field.myy[idx]))
    mxy = float(np.sum(weights * field.mxy[idx]))
    cov = np.asarray([[mxx, mxy], [mxy, myy]], dtype=float)
    eigvals, eigvecs = np.linalg.eigh(cov)
    if not np.all(np.isfinite(eigvals)) or eigvals[0] <= 0 or eigvals[1] <= 0:
        return None
    sigma_minor = float(math.sqrt(eigvals[0]))
    sigma_major = float(math.sqrt(eigvals[1]))
    fwhm_factor = 2.0 * math.sqrt(2.0 * math.log(2.0))
    fwhm_minor = fwhm_factor * sigma_minor
    fwhm_major = fwhm_factor * sigma_major
    fwhm_geom = float(math.sqrt(fwhm_major * fwhm_minor))
    raw_axis_ratio = max(1.0, fwhm_major / max(fwhm_minor, 1e-12))
    axis_ratio = min(float(max_axis_ratio), raw_axis_ratio)
    axis_ratio_clipped = bool(raw_axis_ratio > max_axis_ratio)

    desired_equiv = max(float(base_radius_pix), float(aperture_fwhm_factor) * fwhm_geom)
    max_equiv = float(base_radius_pix) * float(max_radius_scale)
    equiv = min(desired_equiv, max_equiv)
    radius_clipped = bool(desired_equiv > max_equiv)
    root_q = math.sqrt(axis_ratio)
    semi_major = equiv * root_q
    semi_minor = equiv / root_q

    major_vec = eigvecs[:, 1]
    theta = math.degrees(math.atan2(float(major_vec[1]), float(major_vec[0]))) % 180.0
    ellipticity = float(max(0.0, min(1.0, 1.0 - fwhm_minor / fwhm_major)))
    return AdaptiveApertureGeometry(
        semi_major_pix=float(semi_major),
        semi_minor_pix=float(semi_minor),
        equivalent_radius_pix=float(equiv),
        theta_deg=float(theta),
        local_fwhm_major=float(fwhm_major),
        local_fwhm_minor=float(fwhm_minor),
        local_fwhm_geom=float(fwhm_geom),
        local_ellipticity=ellipticity,
        neighbor_count=int(k),
        nearest_neighbor_pix=float(math.sqrt(float(np.min(d2)))),
        axis_ratio_clipped=axis_ratio_clipped,
        radius_clipped=radius_clipped,
    )


def measure_elliptical_source(
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
) -> EllipticalApertureMeasurement:
    """Measure flux in a PSF-aligned ellipse with the standard random-error model.

    ``aperture_radius_pix`` in the returned compatibility object is the
    equal-area circular radius.  The actual semi-major/minor axes and angle are
    preserved in ``flags`` so downstream code can remain schema-compatible.
    """

    flags: dict[str, Any] = {"photometry_estimator": ADAPTIVE_APERTURE_ALGORITHM}
    if geometry.axis_ratio_clipped:
        flags["axis_ratio_clipped"] = True
    if geometry.radius_clipped:
        flags["radius_clipped"] = True

    def empty(reason: str, inner: float, outer: float) -> EllipticalApertureMeasurement:
        flags[reason] = True
        return EllipticalApertureMeasurement(
            flux=None, flux_err=None, mag_inst=None, mag_err=None, local_background=None,
            aperture_radius_pix=float(geometry.equivalent_radius_pix),
            n_aperture_pixels=0, n_annulus_pixels=0,
            annulus_inner_pix=float(inner), annulus_outer_pix=float(outer),
            flags=flags, error_budget=None,
        )

    if data.ndim != 2 or not (math.isfinite(x) and math.isfinite(y)):
        return empty("bad_xy", annulus_inner_pix, annulus_outer_pix)

    a = float(geometry.semi_major_pix)
    b = float(geometry.semi_minor_pix)
    theta = math.radians(float(geometry.theta_deg))
    c, s = math.cos(theta), math.sin(theta)
    extent_x = math.sqrt((a * c) ** 2 + (b * s) ** 2)
    extent_y = math.sqrt((a * s) ** 2 + (b * c) ** 2)
    effective_annulus_inner = max(float(annulus_inner_pix), a + 1.5)
    effective_annulus_outer = max(float(annulus_outer_pix), effective_annulus_inner + 4.0)
    pad = int(math.ceil(max(extent_x, extent_y, effective_annulus_outer) + 1.0))
    ny, nx = data.shape

    if x - extent_x < 0 or x + extent_x > nx - 1:
        flags["aperture_touches_x_edge"] = True
    if y - extent_y < 0 or y + extent_y > ny - 1:
        flags["aperture_touches_y_edge"] = True
    if x - effective_annulus_outer < 0 or x + effective_annulus_outer > nx - 1:
        flags["annulus_touches_x_edge"] = True
    if y - effective_annulus_outer < 0 or y + effective_annulus_outer > ny - 1:
        flags["annulus_touches_y_edge"] = True

    x0 = max(0, int(math.floor(x)) - pad)
    x1 = min(nx, int(math.floor(x)) + pad + 1)
    y0 = max(0, int(math.floor(y)) - pad)
    y1 = min(ny, int(math.floor(y)) + pad + 1)
    if x0 >= x1 or y0 >= y1:
        return empty("outside_image", effective_annulus_inner, effective_annulus_outer)

    cutout = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.indices(cutout.shape, dtype=float)
    dx = xx + x0 - x
    dy = yy + y0 - y
    u = c * dx + s * dy
    v = -s * dx + c * dy
    ellipse = (u / a) ** 2 + (v / b) ** 2 <= 1.0
    rr2 = dx * dx + dy * dy
    annulus = (rr2 >= effective_annulus_inner**2) & (rr2 <= effective_annulus_outer**2)
    finite = np.isfinite(cutout)
    aperture_values = cutout[ellipse & finite]
    annulus_values = cutout[annulus & finite]
    if aperture_values.size == 0:
        return empty("empty_aperture", effective_annulus_inner, effective_annulus_outer)

    if annulus_values.size < 5:
        flags["too_few_annulus_pixels"] = True
        finite_values = cutout[finite]
        background = float(np.nanmedian(finite_values)) if finite_values.size else 0.0
        annulus_std = float(np.nanstd(finite_values)) if finite_values.size else 0.0
    else:
        background = float(np.nanmedian(annulus_values))
        annulus_std = float(1.4826 * np.nanmedian(np.abs(annulus_values - background)))
        if not math.isfinite(annulus_std) or annulus_std <= 0:
            annulus_std = float(np.nanstd(annulus_values))

    flux = float(np.nansum(aperture_values) - background * aperture_values.size)
    if saturation_level_adu is not None and np.nanmax(aperture_values) >= saturation_level_adu:
        flags["saturated_or_near_saturated"] = True
        flags["aperture_max_adu"] = float(np.nanmax(aperture_values))

    budget = aperture_error_budget(
        net_flux_adu=flux,
        local_background_adu=background,
        annulus_sigma_adu=annulus_std,
        n_aperture_pixels=int(aperture_values.size),
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

    flags.update({
        "semi_major_pix": a,
        "semi_minor_pix": b,
        "theta_deg": geometry.theta_deg,
        "equivalent_radius_pix": geometry.equivalent_radius_pix,
        "local_fwhm_major": geometry.local_fwhm_major,
        "local_fwhm_minor": geometry.local_fwhm_minor,
        "local_fwhm_geom": geometry.local_fwhm_geom,
        "local_ellipticity": geometry.local_ellipticity,
        "psf_neighbor_count": geometry.neighbor_count,
        "psf_nearest_neighbor_pix": geometry.nearest_neighbor_pix,
        "adaptive_annulus_inner_pix": effective_annulus_inner,
        "adaptive_annulus_outer_pix": effective_annulus_outer,
    })
    return EllipticalApertureMeasurement(
        flux=flux,
        flux_err=budget.selected_flux_err_adu,
        mag_inst=mag,
        mag_err=mag_err,
        local_background=background,
        aperture_radius_pix=float(geometry.equivalent_radius_pix),
        n_aperture_pixels=int(aperture_values.size),
        n_annulus_pixels=int(annulus_values.size),
        annulus_inner_pix=float(effective_annulus_inner),
        annulus_outer_pix=float(effective_annulus_outer),
        flags=flags,
        error_budget=budget.to_dict(),
    )


def _build_psf_references(
    data: np.ndarray,
    detection_rows: list[dict[str, Any]],
    *,
    min_snr: float,
    max_sources: int,
    cutout_radius_pix: int,
    min_isolation_pix: float,
) -> list[_PsfReference]:
    ny, nx = data.shape
    selected = _select_shape_sources(
        detection_rows,
        width=nx,
        height=ny,
        min_snr=min_snr,
        max_sources=max_sources,
        edge_margin=float(cutout_radius_pix + 2),
        min_isolation_pix=min_isolation_pix,
    )
    refs: list[_PsfReference] = []
    fwhm_factor = 2.0 * math.sqrt(2.0 * math.log(2.0))
    for row in selected:
        shape = measure_psf_shape(
            data,
            x=float(row["x"]),
            y=float(row["y"]),
            local_background=_finite_float(row.get("local_background")),
            cutout_radius_pix=cutout_radius_pix,
        )
        if shape is None:
            continue
        sigma_major = shape.fwhm_major / fwhm_factor
        sigma_minor = shape.fwhm_minor / fwhm_factor
        theta = math.radians(shape.theta_deg)
        c, s = math.cos(theta), math.sin(theta)
        mxx = sigma_major**2 * c**2 + sigma_minor**2 * s**2
        myy = sigma_major**2 * s**2 + sigma_minor**2 * c**2
        mxy = (sigma_major**2 - sigma_minor**2) * s * c
        refs.append(_PsfReference(
            x=shape.x_centroid,
            y=shape.y_centroid,
            mxx=float(mxx),
            myy=float(myy),
            mxy=float(mxy),
            fwhm_geom=float(shape.fwhm_geom),
        ))
    return refs


def _load_frame_catalog_links(
    session: Session,
    *,
    frame_ids: list[int],
    catalog_name: str | None,
) -> dict[int, list[dict[str, Any]]]:
    stmt = (
        select(
            FrameCatalogLink.frame_id,
            FrameCatalogLink.catalog_source_id,
            FrameCatalogLink.x,
            FrameCatalogLink.y,
            CatalogSource.catalog_name,
            CatalogSource.source_id,
        )
        .join(CatalogSource, CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id)
        .where(FrameCatalogLink.frame_id.in_(frame_ids))
        .where(FrameCatalogLink.in_footprint.is_(True))
        .order_by(FrameCatalogLink.frame_id, FrameCatalogLink.frame_catalog_link_id)
    )
    if catalog_name:
        stmt = stmt.where(CatalogSource.catalog_name == catalog_name)
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in session.execute(stmt).mappings().all():
        grouped.setdefault(int(row["frame_id"]), []).append(dict(row))
    return grouped


def _summarize_frame(frame_id: int, rows: list[dict[str, Any]], n_psf_refs: int) -> dict[str, Any]:
    delta = np.asarray([_finite_float(row.get("adaptive_minus_circular_mag")) for row in rows], dtype=object)
    delta = np.asarray([v for v in delta if v is not None], dtype=float)
    y = np.asarray([float(row["ynorm"]) for row in rows if _finite_float(row.get("adaptive_minus_circular_mag")) is not None], dtype=float)
    return {
        "frame_id": int(frame_id),
        "n_psf_references": int(n_psf_refs),
        "n_measurements": int(len(rows)),
        "delta_mag_median": _median_or_none(delta),
        "delta_mag_mad": _mad_or_none(delta),
        "delta_mag_p10": _percentile_or_none(delta, 10),
        "delta_mag_p90": _percentile_or_none(delta, 90),
        "delta_mag_vs_ynorm_spearman": _spearman(y, delta) if delta.size == y.size else None,
        "adaptive_equivalent_radius_median": _median_or_none(np.asarray([row["adaptive_equivalent_radius_pix"] for row in rows], dtype=float)),
        "adaptive_axis_ratio_median": _median_or_none(np.asarray([row["adaptive_semi_major_pix"] / row["adaptive_semi_minor_pix"] for row in rows], dtype=float)),
        "local_fwhm_geom_median": _median_or_none(np.asarray([row["local_fwhm_geom"] for row in rows], dtype=float)),
        "local_ellipticity_median": _median_or_none(np.asarray([row["local_ellipticity"] for row in rows], dtype=float)),
    }


def _build_object_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "catalog_source_id", "catalog_name", "source_id", "n_measurements", "x_median", "y_median",
        "xnorm_median", "ynorm_median", "circular_rms", "adaptive_rms", "rms_ratio_adaptive_to_circular",
        "circular_mad", "adaptive_mad", "mad_ratio_adaptive_to_circular", "circular_robust_peak_to_peak",
        "adaptive_robust_peak_to_peak", "delta_mag_median", "delta_mag_mad",
    ]
    if df.empty:
        return pd.DataFrame(columns=columns)
    work = df.copy()
    for col in ("circular_mag_inst", "adaptive_mag_inst"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
        # Remove each object's constant magnitude and then each frame's common mode.
        object_median = work.groupby("catalog_source_id")[col].transform("median")
        residual = work[col] - object_median
        frame_common = residual.groupby(work["frame_id"]).transform("median")
        work[f"{col}_diff"] = residual - frame_common

    rows: list[dict[str, Any]] = []
    for catalog_source_id, group in work.groupby("catalog_source_id"):
        circ = group["circular_mag_inst_diff"].to_numpy(dtype=float)
        adap = group["adaptive_mag_inst_diff"].to_numpy(dtype=float)
        circ = circ[np.isfinite(circ)]
        adap = adap[np.isfinite(adap)]
        if min(circ.size, adap.size) < 3:
            continue
        circ_rms = float(np.sqrt(np.mean(circ * circ)))
        adap_rms = float(np.sqrt(np.mean(adap * adap)))
        circ_mad = _mad_or_none(circ)
        adap_mad = _mad_or_none(adap)
        first = group.iloc[0]
        delta = pd.to_numeric(group["adaptive_minus_circular_mag"], errors="coerce").to_numpy(dtype=float)
        delta = delta[np.isfinite(delta)]
        rows.append({
            "catalog_source_id": int(catalog_source_id),
            "catalog_name": first.get("catalog_name"),
            "source_id": first.get("source_id"),
            "n_measurements": int(min(circ.size, adap.size)),
            "x_median": float(np.nanmedian(group["x"])),
            "y_median": float(np.nanmedian(group["y"])),
            "xnorm_median": float(np.nanmedian(group["xnorm"])),
            "ynorm_median": float(np.nanmedian(group["ynorm"])),
            "circular_rms": circ_rms,
            "adaptive_rms": adap_rms,
            "rms_ratio_adaptive_to_circular": (adap_rms / circ_rms if circ_rms > 0 else None),
            "circular_mad": circ_mad,
            "adaptive_mad": adap_mad,
            "mad_ratio_adaptive_to_circular": (adap_mad / circ_mad if circ_mad and circ_mad > 0 else None),
            "circular_robust_peak_to_peak": _robust_ptp(circ),
            "adaptive_robust_peak_to_peak": _robust_ptp(adap),
            "delta_mag_median": _median_or_none(delta),
            "delta_mag_mad": _mad_or_none(delta),
        })
    return pd.DataFrame(rows, columns=columns)


def _build_summary(
    result: AdaptiveApertureDiagnosticsResult,
    measurement_df: pd.DataFrame,
    frame_df: pd.DataFrame,
    object_df: pd.DataFrame,
    *,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    summary = result.summary()
    if not object_df.empty:
        y = object_df["ynorm_median"].to_numpy(dtype=float)
        circ_rms = object_df["circular_rms"].to_numpy(dtype=float)
        adap_rms = object_df["adaptive_rms"].to_numpy(dtype=float)
        circ_mad = object_df["circular_mad"].to_numpy(dtype=float)
        adap_mad = object_df["adaptive_mad"].to_numpy(dtype=float)
        summary["object_diagnostics"] = {
            "n_objects": int(len(object_df)),
            "circular_rms_median": _median_or_none(circ_rms),
            "adaptive_rms_median": _median_or_none(adap_rms),
            "rms_ratio_median": _median_or_none(object_df["rms_ratio_adaptive_to_circular"].to_numpy(dtype=float)),
            "circular_mad_median": _median_or_none(circ_mad),
            "adaptive_mad_median": _median_or_none(adap_mad),
            "mad_ratio_median": _median_or_none(object_df["mad_ratio_adaptive_to_circular"].to_numpy(dtype=float)),
            "circular_rms_vs_ynorm_spearman": _spearman(y, circ_rms),
            "adaptive_rms_vs_ynorm_spearman": _spearman(y, adap_rms),
            "circular_mad_vs_ynorm_spearman": _spearman(y, circ_mad),
            "adaptive_mad_vs_ynorm_spearman": _spearman(y, adap_mad),
            "fraction_rms_improved": float(np.mean(adap_rms < circ_rms)),
            "fraction_mad_improved": float(np.mean(adap_mad < circ_mad)),
        }
    else:
        summary["object_diagnostics"] = {"n_objects": 0}
    if not measurement_df.empty:
        delta = pd.to_numeric(measurement_df["adaptive_minus_circular_mag"], errors="coerce").to_numpy(dtype=float)
        delta = delta[np.isfinite(delta)]
        summary["measurement_diagnostics"] = {
            "delta_mag_median": _median_or_none(delta),
            "delta_mag_mad": _mad_or_none(delta),
            "equivalent_radius_median": _median_or_none(pd.to_numeric(measurement_df["adaptive_equivalent_radius_pix"], errors="coerce").to_numpy(dtype=float)),
            "semi_major_median": _median_or_none(pd.to_numeric(measurement_df["adaptive_semi_major_pix"], errors="coerce").to_numpy(dtype=float)),
            "semi_minor_median": _median_or_none(pd.to_numeric(measurement_df["adaptive_semi_minor_pix"], errors="coerce").to_numpy(dtype=float)),
        }
    summary["parameters"] = parameters
    summary["semantics"] = {
        "science_flux_changed": False,
        "database_written": False,
        "circular_estimator": "existing forced circular aperture kernel",
        "adaptive_estimator": ADAPTIVE_APERTURE_ALGORITHM,
        "adaptive_geometry": "local inverse-distance interpolation of PSF second-moment covariance; area scales with local FWHM and ellipse follows local axis ratio/orientation",
    }
    return summary


def _render_field_maps(measurements: pd.DataFrame, objects: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(12, 9), constrained_layout=True)
    sample = measurements.dropna(subset=["xnorm", "ynorm", "adaptive_minus_circular_mag"])
    if len(sample) > 12000:
        sample = sample.sample(12000, random_state=1)
    sc = axes[0, 0].scatter(sample["xnorm"], sample["ynorm"], c=sample["adaptive_minus_circular_mag"], s=5)
    axes[0, 0].set_title("Adaptive - circular magnitude")
    axes[0, 0].set_xlabel("x normalized")
    axes[0, 0].set_ylabel("y normalized")
    fig.colorbar(sc, ax=axes[0, 0], label="mag")

    axes[0, 1].scatter(sample["ynorm"], sample["adaptive_minus_circular_mag"], s=5)
    axes[0, 1].set_title("Flux-shape correction vs detector Y")
    axes[0, 1].set_xlabel("y normalized")
    axes[0, 1].set_ylabel("adaptive - circular [mag]")

    if not objects.empty:
        sc2 = axes[1, 0].scatter(objects["xnorm_median"], objects["ynorm_median"], c=objects["rms_ratio_adaptive_to_circular"], s=10)
        axes[1, 0].set_title("Differential RMS ratio adaptive/circular")
        axes[1, 0].set_xlabel("x normalized")
        axes[1, 0].set_ylabel("y normalized")
        fig.colorbar(sc2, ax=axes[1, 0], label="RMS ratio")
        axes[1, 1].scatter(objects["ynorm_median"], objects["circular_rms"], s=8, label="circular")
        axes[1, 1].scatter(objects["ynorm_median"], objects["adaptive_rms"], s=8, label="adaptive")
        axes[1, 1].set_title("Differential RMS vs detector Y")
        axes[1, 1].set_xlabel("y normalized")
        axes[1, 1].set_ylabel("RMS [mag]")
        axes[1, 1].legend()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _render_time_series(frame_df: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt

    if frame_df.empty:
        return
    x = frame_df["frame_id"].to_numpy()
    fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
    ax.plot(x, frame_df["delta_mag_median"], marker="o", label="median adaptive-circular")
    ax.fill_between(x, frame_df["delta_mag_p10"], frame_df["delta_mag_p90"], alpha=0.2, label="p10-p90")
    ax.set_xlabel("frame_id")
    ax.set_ylabel("mag")
    ax.set_title("Adaptive aperture correction through the series")
    ax.legend()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _normalize_coordinate(value: float, size: int) -> float:
    if size <= 1:
        return 0.0
    return float((value - (size - 1) / 2.0) / ((size - 1) / 2.0))


def _finite_float(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _median_or_none(values: np.ndarray) -> float | None:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(np.median(arr)) if arr.size else None


def _mad_or_none(values: np.ndarray) -> float | None:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if not arr.size:
        return None
    med = float(np.median(arr))
    return float(np.median(np.abs(arr - med)))


def _percentile_or_none(values: np.ndarray, percentile: float) -> float | None:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(np.percentile(arr, percentile)) if arr.size else None


def _robust_ptp(values: np.ndarray) -> float | None:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if not arr.size:
        return None
    return float(np.percentile(arr, 95) - np.percentile(arr, 5))


def _spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    x = x[finite]
    y = y[finite]
    if x.size < 3:
        return None
    xr = pd.Series(x).rank(method="average").to_numpy(dtype=float)
    yr = pd.Series(y).rank(method="average").to_numpy(dtype=float)
    if np.std(xr) == 0 or np.std(yr) == 0:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])
