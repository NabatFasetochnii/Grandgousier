from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.detection_runs import SOURCE_DETECTION_RUN
from grandgousier.db.models import (
    CatalogSource,
    DetectionCatalogMatch,
    DetectionRun,
    DetectionRunFrame,
    Frame,
    SourceDetection,
)


PSF_FIELD_DIAGNOSTICS_ALGORITHM = "second_moments_spatial_v1"
_FWHM_FACTOR = 2.0 * math.sqrt(2.0 * math.log(2.0))
_SPATIAL_METRICS = ("fwhm_geom", "fwhm_major", "fwhm_minor", "ellipticity", "growth_delta_mag")


@dataclass(frozen=True, slots=True)
class PsfShapeMeasurement:
    x_input: float
    y_input: float
    x_centroid: float
    y_centroid: float
    fwhm_major: float
    fwhm_minor: float
    fwhm_geom: float
    ellipticity: float
    theta_deg: float
    orientation_cos2: float
    orientation_sin2: float
    growth_inner_flux: float
    growth_outer_flux: float
    growth_delta_mag: float | None
    background_adu: float
    weight_sum: float
    n_weight_pixels: int


@dataclass
class PsfFieldDiagnosticsResult:
    selected_frames: int = 0
    analyzed_frames: int = 0
    failed_frames: int = 0
    skipped_missing_file: int = 0
    frames_without_detections: int = 0
    frames_without_usable_psf_sources: int = 0
    detection_rows: int = 0
    selected_source_rows: int = 0
    measured_sources: int = 0
    algorithm: str = PSF_FIELD_DIAGNOSTICS_ALGORITHM
    sources_csv: str | None = None
    frames_csv: str | None = None
    summary_json: str | None = None
    field_map_png: str | None = None
    time_series_png: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def analyze_psf_field(
    session: Session,
    *,
    frame_ids: list[int] | None = None,
    instrument_unit_id: int | None = None,
    detection_algorithm: str = "numpy_localmax_v1",
    out_dir: str | Path | None = None,
    max_sources_per_frame: int = 500,
    min_snr: float = 20.0,
    cutout_radius_pix: int = 7,
    min_isolation_pix: float = 12.0,
    clip_sigma: float = 4.0,
    n_iterations: int = 3,
    render_plots: bool = True,
) -> PsfFieldDiagnosticsResult:
    """Measure per-source PSF morphology and robust spatial trends.

    This is deliberately diagnostic-only in 0.5.44.0.  No measurements,
    calibrations, frame QC flags or processing statuses are changed.
    """

    if max_sources_per_frame <= 0:
        raise ValueError("max_sources_per_frame must be positive")
    if min_snr < 0:
        raise ValueError("min_snr must be non-negative")
    if cutout_radius_pix < 3:
        raise ValueError("cutout_radius_pix must be >= 3")
    if min_isolation_pix < 0:
        raise ValueError("min_isolation_pix must be non-negative")
    if clip_sigma <= 0:
        raise ValueError("clip_sigma must be positive")
    if n_iterations < 1:
        raise ValueError("n_iterations must be >= 1")

    from astropy.io import fits

    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument_unit_id=instrument_unit_id,
    )
    result = PsfFieldDiagnosticsResult(selected_frames=len(frames))
    if not frames:
        result.warnings.append("no frames selected")
        return result

    selected_ids = [int(frame.frame_id) for frame in frames]
    detection_rows = _load_detection_rows(
        session,
        frame_ids=selected_ids,
        algorithm=detection_algorithm,
    )
    result.detection_rows = sum(len(rows) for rows in detection_rows.values())
    detection_identities = _load_detection_identities(
        session, frame_ids=selected_ids
    )

    source_records: list[dict[str, Any]] = []
    frame_records: list[dict[str, Any]] = []

    for frame in tqdm(frames, desc="PSF field diagnostics", unit="frame"):
        frame_id = int(frame.frame_id)
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        rows = detection_rows.get(frame_id, [])
        if not rows:
            result.frames_without_detections += 1
            continue

        try:
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
            if data.ndim != 2:
                raise ValueError(f"expected 2-D image, got shape={data.shape}")
        except Exception as exc:  # diagnostics must not corrupt/alter pipeline state
            result.failed_frames += 1
            result.warnings.append(f"frame_id={frame_id}: {type(exc).__name__}: {exc}")
            continue

        ny, nx = data.shape
        selected = _select_shape_sources(
            rows,
            width=nx,
            height=ny,
            min_snr=min_snr,
            max_sources=max_sources_per_frame,
            edge_margin=float(cutout_radius_pix + 2),
            min_isolation_pix=min_isolation_pix,
        )
        result.selected_source_rows += len(selected)
        local_records: list[dict[str, Any]] = []
        for det in selected:
            try:
                shape = measure_psf_shape(
                    data,
                    x=float(det["x"]),
                    y=float(det["y"]),
                    local_background=_finite_float(det.get("local_background")),
                    cutout_radius_pix=cutout_radius_pix,
                )
            except Exception:
                shape = None
            if shape is None:
                continue
            identity = detection_identities.get(int(det["source_detection_id"]), {})
            record = {
                "frame_id": frame_id,
                "date_obs": frame.date_obs.isoformat() if frame.date_obs is not None else None,
                "mjd": _finite_float(frame.mjd),
                "airmass": _finite_float(frame.airmass),
                "source_detection_id": int(det["source_detection_id"]),
                "snr": _finite_float(det.get("snr")),
                "peak_adu": _finite_float(det.get("peak_adu")),
                "catalog_source_id": identity.get("catalog_source_id"),
                "catalog_name": identity.get("catalog_name"),
                "source_id": identity.get("source_id"),
                "match_sep_arcsec": identity.get("sep_arcsec"),
                "image_width": int(nx),
                "image_height": int(ny),
                "xnorm": _normalize_coordinate(shape.x_centroid, nx),
                "ynorm": _normalize_coordinate(shape.y_centroid, ny),
                **asdict(shape),
            }
            record["rnorm"] = float(math.hypot(record["xnorm"], record["ynorm"]))
            local_records.append(record)

        if not local_records:
            result.frames_without_usable_psf_sources += 1
            continue

        result.analyzed_frames += 1
        result.measured_sources += len(local_records)
        source_records.extend(local_records)
        frame_records.append(
            _summarize_frame(
                frame=frame,
                image_shape=(ny, nx),
                records=local_records,
                clip_sigma=clip_sigma,
                n_iterations=n_iterations,
            )
        )

    if out_dir is None:
        return result

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    sources_path = output / "psf_sources.csv"
    frames_path = output / "psf_frames.csv"
    summary_path = output / "psf_summary.json"

    source_df = pd.DataFrame(source_records)
    frame_df = pd.DataFrame(frame_records)
    source_df.to_csv(sources_path, index=False)
    frame_df.to_csv(frames_path, index=False)
    result.sources_csv = str(sources_path)
    result.frames_csv = str(frames_path)

    summary_payload = _build_summary_payload(
        result=result,
        source_df=source_df,
        frame_df=frame_df,
        parameters={
            "detection_algorithm": detection_algorithm,
            "max_sources_per_frame": max_sources_per_frame,
            "min_snr": min_snr,
            "cutout_radius_pix": cutout_radius_pix,
            "min_isolation_pix": min_isolation_pix,
            "clip_sigma": clip_sigma,
            "n_iterations": n_iterations,
            "render_plots": render_plots,
        },
    )
    summary_path.write_text(json.dumps(summary_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    result.summary_json = str(summary_path)

    if render_plots and not source_df.empty and not frame_df.empty:
        try:
            field_map_path = output / "psf_field_maps.png"
            _render_field_maps(source_df, field_map_path)
            result.field_map_png = str(field_map_path)
            time_path = output / "psf_time_series.png"
            _render_time_series(frame_df, time_path)
            result.time_series_png = str(time_path)
        except Exception as exc:
            result.warnings.append(f"plotting failed: {type(exc).__name__}: {exc}")

    # Re-write after optional plotting so paths/warnings in the JSON are complete.
    summary_payload = _build_summary_payload(
        result=result,
        source_df=source_df,
        frame_df=frame_df,
        parameters=summary_payload["parameters"],
    )
    summary_path.write_text(json.dumps(summary_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def measure_psf_shape(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    local_background: float | None = None,
    cutout_radius_pix: int = 7,
) -> PsfShapeMeasurement | None:
    """Estimate an elliptical second-moment PSF shape around one detection."""

    if data.ndim != 2 or not (math.isfinite(x) and math.isfinite(y)):
        return None
    ny, nx = data.shape
    radius = int(cutout_radius_pix)
    x0 = int(math.floor(x)) - radius
    x1 = int(math.floor(x)) + radius + 1
    y0 = int(math.floor(y)) - radius
    y1 = int(math.floor(y)) + radius + 1
    if x0 < 0 or y0 < 0 or x1 > nx or y1 > ny:
        return None

    cut = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.indices(cut.shape, dtype=float)
    xx += float(x0)
    yy += float(y0)
    rr2_input = (xx - x) ** 2 + (yy - y) ** 2
    finite = np.isfinite(cut)
    aperture = finite & (rr2_input <= float(radius) ** 2)
    if np.count_nonzero(aperture) < 20:
        return None

    background = _finite_float(local_background)
    if background is None:
        annulus = finite & (rr2_input >= max(2.0, radius - 2.0) ** 2) & (rr2_input <= float(radius) ** 2)
        values = cut[annulus]
        if values.size < 10:
            values = cut[aperture]
        background = float(np.nanmedian(values))
    weights = np.where(aperture, cut - background, 0.0)
    weights = np.where(np.isfinite(weights) & (weights > 0), weights, 0.0)
    total = float(np.sum(weights))
    if not math.isfinite(total) or total <= 0:
        return None

    xcen = float(np.sum(weights * xx) / total)
    ycen = float(np.sum(weights * yy) / total)
    dx = xx - xcen
    dy = yy - ycen
    mxx = float(np.sum(weights * dx * dx) / total)
    myy = float(np.sum(weights * dy * dy) / total)
    mxy = float(np.sum(weights * dx * dy) / total)
    covariance = np.asarray([[mxx, mxy], [mxy, myy]], dtype=float)
    eigvals, eigvecs = np.linalg.eigh(covariance)
    eigvals = np.asarray(eigvals, dtype=float)
    if eigvals.size != 2 or not np.all(np.isfinite(eigvals)) or eigvals[0] <= 0 or eigvals[1] <= 0:
        return None
    sigma_minor = float(math.sqrt(eigvals[0]))
    sigma_major = float(math.sqrt(eigvals[1]))
    fwhm_minor = _FWHM_FACTOR * sigma_minor
    fwhm_major = _FWHM_FACTOR * sigma_major
    fwhm_geom = float(math.sqrt(fwhm_major * fwhm_minor))
    ellipticity = float(max(0.0, min(1.0, 1.0 - sigma_minor / sigma_major)))

    major_vec = eigvecs[:, 1]
    theta = math.degrees(math.atan2(float(major_vec[1]), float(major_vec[0]))) % 180.0
    theta_rad = math.radians(theta)

    rr2 = dx * dx + dy * dy
    inner_radius = min(2.0, max(1.0, radius / 3.0))
    outer_radius = min(5.0, max(inner_radius + 1.0, radius - 1.0))
    inner_flux = float(np.sum(weights[rr2 <= inner_radius**2]))
    outer_flux = float(np.sum(weights[rr2 <= outer_radius**2]))
    growth_delta_mag = None
    if inner_flux > 0 and outer_flux > 0:
        growth_delta_mag = float(-2.5 * math.log10(inner_flux / outer_flux))

    return PsfShapeMeasurement(
        x_input=float(x),
        y_input=float(y),
        x_centroid=xcen,
        y_centroid=ycen,
        fwhm_major=fwhm_major,
        fwhm_minor=fwhm_minor,
        fwhm_geom=fwhm_geom,
        ellipticity=ellipticity,
        theta_deg=float(theta),
        orientation_cos2=float(math.cos(2.0 * theta_rad)),
        orientation_sin2=float(math.sin(2.0 * theta_rad)),
        growth_inner_flux=inner_flux,
        growth_outer_flux=outer_flux,
        growth_delta_mag=growth_delta_mag,
        background_adu=float(background),
        weight_sum=total,
        n_weight_pixels=int(np.count_nonzero(weights > 0)),
    )


def fit_spatial_model(
    xnorm: Iterable[float],
    ynorm: Iterable[float],
    values: Iterable[float],
    *,
    model: str,
    clip_sigma: float = 4.0,
    n_iterations: int = 3,
) -> dict[str, Any]:
    """Robust constant/plane/quadratic least-squares fit in normalized detector coordinates."""

    x = np.asarray(list(xnorm), dtype=float)
    y = np.asarray(list(ynorm), dtype=float)
    q = np.asarray(list(values), dtype=float)
    finite = np.isfinite(x) & np.isfinite(y) & np.isfinite(q)
    x, y, q = x[finite], y[finite], q[finite]
    design, names = _design_matrix(x, y, model=model)
    min_points = max(len(names) + 3, len(names))
    if q.size < min_points:
        return {
            "status": "TOO_FEW_POINTS",
            "model": model,
            "n_input": int(q.size),
            "n_used": 0,
            "coefficients": {name: None for name in names},
            "residual_mad": None,
            "bic": None,
            "spatial_peak_to_peak": None,
        }

    mask = np.ones(q.size, dtype=bool)
    coeff = np.zeros(len(names), dtype=float)
    for _ in range(int(n_iterations)):
        if np.count_nonzero(mask) < min_points:
            break
        coeff, *_ = np.linalg.lstsq(design[mask], q[mask], rcond=None)
        residual = q - design @ coeff
        center = float(np.median(residual[mask]))
        mad = float(np.median(np.abs(residual[mask] - center)))
        sigma = 1.4826 * mad
        if not math.isfinite(sigma) or sigma <= 0:
            break
        new_mask = mask & (np.abs(residual - center) <= float(clip_sigma) * sigma)
        if np.array_equal(new_mask, mask):
            break
        mask = new_mask

    if np.count_nonzero(mask) < min_points:
        return {
            "status": "CLIPPED_TOO_FEW_POINTS",
            "model": model,
            "n_input": int(q.size),
            "n_used": int(np.count_nonzero(mask)),
            "coefficients": {name: None for name in names},
            "residual_mad": None,
            "bic": None,
            "spatial_peak_to_peak": None,
        }

    coeff, *_ = np.linalg.lstsq(design[mask], q[mask], rcond=None)
    residual = q - design @ coeff
    residual_used = residual[mask]
    center = float(np.median(residual_used))
    residual_mad = float(np.median(np.abs(residual_used - center)))
    rss = float(np.sum(residual_used * residual_used))
    n_used = int(np.count_nonzero(mask))
    k = int(len(names))
    bic = None
    if n_used > 0:
        rss_per = max(rss / n_used, np.finfo(float).tiny)
        bic = float(n_used * math.log(rss_per) + k * math.log(n_used))

    grid = np.linspace(-1.0, 1.0, 9)
    gx, gy = np.meshgrid(grid, grid)
    gdesign, _ = _design_matrix(gx.ravel(), gy.ravel(), model=model)
    predicted = gdesign @ coeff
    spatial_ptp = float(np.nanmax(predicted) - np.nanmin(predicted)) if predicted.size else 0.0
    return {
        "status": "OK",
        "model": model,
        "n_input": int(q.size),
        "n_used": n_used,
        "coefficients": {name: float(value) for name, value in zip(names, coeff, strict=True)},
        "residual_mad": residual_mad,
        "bic": bic,
        "spatial_peak_to_peak": spatial_ptp,
    }


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_([int(value) for value in frame_ids]))
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == int(instrument_unit_id))
    return list(session.scalars(stmt).all())


def _load_detection_rows(
    session: Session,
    *,
    frame_ids: list[int],
    algorithm: str,
) -> dict[int, list[dict[str, Any]]]:
    if not frame_ids:
        return {}
    stmt = (
        select(
            SourceDetection.source_detection_id,
            DetectionRunFrame.frame_id.label("frame_id"),
            SourceDetection.x,
            SourceDetection.y,
            SourceDetection.peak_adu,
            SourceDetection.snr,
            SourceDetection.local_background,
            SourceDetection.flags,
        )
        .select_from(SourceDetection)
        .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == SourceDetection.detection_run_frame_id)
        .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
        .where(DetectionRunFrame.frame_id.in_(frame_ids))
        .where(DetectionRun.run_kind == SOURCE_DETECTION_RUN)
        .where(DetectionRun.algorithm == algorithm)
        .order_by(DetectionRunFrame.frame_id, SourceDetection.snr.desc().nullslast())
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in session.execute(stmt).mappings().all():
        grouped.setdefault(int(row["frame_id"]), []).append(dict(row))
    return grouped


def _load_detection_identities(
    session: Session,
    *,
    frame_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Load nearest stored catalog identity for each detection when available."""

    if not frame_ids:
        return {}
    stmt = (
        select(
            DetectionCatalogMatch.source_detection_id,
            DetectionCatalogMatch.catalog_source_id,
            CatalogSource.catalog_name.label("catalog_name"),
            DetectionCatalogMatch.sep_arcsec,
            CatalogSource.source_id,
        )
        .select_from(DetectionCatalogMatch)
        .join(DetectionRunFrame, DetectionRunFrame.detection_run_frame_id == DetectionCatalogMatch.detection_run_frame_id)
        .join(CatalogSource, CatalogSource.catalog_source_id == DetectionCatalogMatch.catalog_source_id)
        .where(DetectionRunFrame.frame_id.in_(frame_ids))
        .where(DetectionCatalogMatch.match_status == "MATCHED")
        .order_by(
            DetectionCatalogMatch.source_detection_id,
            DetectionCatalogMatch.sep_arcsec.asc().nullslast(),
            DetectionCatalogMatch.detection_catalog_match_id,
        )
    )
    identities: dict[int, dict[str, Any]] = {}
    for row in session.execute(stmt).mappings().all():
        detection_id = int(row["source_detection_id"])
        if detection_id in identities:
            continue
        identities[detection_id] = {
            "catalog_source_id": int(row["catalog_source_id"]),
            "catalog_name": str(row["catalog_name"]),
            "source_id": str(row["source_id"]),
            "sep_arcsec": _finite_float(row["sep_arcsec"]),
        }
    return identities


def _select_shape_sources(
    rows: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    min_snr: float,
    max_sources: int,
    edge_margin: float,
    min_isolation_pix: float,
) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    for row in rows:
        x = _finite_float(row.get("x"))
        y = _finite_float(row.get("y"))
        snr = _finite_float(row.get("snr"))
        if x is None or y is None or snr is None or snr < min_snr:
            continue
        if x < edge_margin or y < edge_margin or x > width - 1 - edge_margin or y > height - 1 - edge_margin:
            continue
        flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
        if flags.get("saturated_or_near_saturated"):
            continue
        usable.append(row)

    if min_isolation_pix > 0 and usable:
        isolated_ids = _isolated_source_detection_ids(
            rows, min_distance=float(min_isolation_pix)
        )
        usable = [
            row for row in usable
            if int(row["source_detection_id"]) in isolated_ids
        ]
    usable.sort(key=lambda row: float(row.get("snr") or 0.0), reverse=True)
    return usable[: int(max_sources)]


def _isolated_source_detection_ids(
    rows: list[dict[str, Any]],
    *,
    min_distance: float,
) -> set[int]:
    """Return isolated detection ids using an O(N) spatial hash neighborhood scan."""

    if min_distance <= 0:
        return {int(row["source_detection_id"]) for row in rows}
    cell = float(min_distance)
    entries: list[tuple[int, float, float, int, int]] = []
    buckets: dict[tuple[int, int], list[int]] = {}
    for row in rows:
        x = _finite_float(row.get("x"))
        y = _finite_float(row.get("y"))
        if x is None or y is None:
            continue
        source_id = int(row["source_detection_id"])
        ix = int(math.floor(x / cell))
        iy = int(math.floor(y / cell))
        entry_index = len(entries)
        entries.append((source_id, x, y, ix, iy))
        buckets.setdefault((ix, iy), []).append(entry_index)

    threshold2 = cell * cell
    isolated: set[int] = set()
    for entry_index, (source_id, x, y, ix, iy) in enumerate(entries):
        has_neighbor = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for other_index in buckets.get((ix + dx, iy + dy), ()):
                    if other_index == entry_index:
                        continue
                    _, other_x, other_y, _, _ = entries[other_index]
                    if (other_x - x) ** 2 + (other_y - y) ** 2 < threshold2:
                        has_neighbor = True
                        break
                if has_neighbor:
                    break
            if has_neighbor:
                break
        if not has_neighbor:
            isolated.add(source_id)
    return isolated


def _summarize_frame(
    *,
    frame: Frame,
    image_shape: tuple[int, int],
    records: list[dict[str, Any]],
    clip_sigma: float,
    n_iterations: int,
) -> dict[str, Any]:
    ny, nx = image_shape
    df = pd.DataFrame(records)
    summary: dict[str, Any] = {
        "frame_id": int(frame.frame_id),
        "date_obs": frame.date_obs.isoformat() if frame.date_obs is not None else None,
        "mjd": _finite_float(frame.mjd),
        "airmass": _finite_float(frame.airmass),
        "image_width": int(nx),
        "image_height": int(ny),
        "n_psf_sources": int(len(df)),
    }
    for metric in _SPATIAL_METRICS:
        values = pd.to_numeric(df[metric], errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(values)
        summary[f"{metric}_median"] = _median_or_none(values[finite])
        summary[f"{metric}_mad"] = _mad_or_none(values[finite])
        summary[f"{metric}_corr_x"] = _corr_or_none(df.loc[finite, "xnorm"], values[finite])
        summary[f"{metric}_corr_y"] = _corr_or_none(df.loc[finite, "ynorm"], values[finite])
        summary[f"{metric}_corr_r"] = _corr_or_none(df.loc[finite, "rnorm"], values[finite])
        for model in ("constant", "plane", "quadratic"):
            fit = fit_spatial_model(
                df.loc[finite, "xnorm"],
                df.loc[finite, "ynorm"],
                values[finite],
                model=model,
                clip_sigma=clip_sigma,
                n_iterations=n_iterations,
            )
            prefix = f"{metric}_{model}"
            summary[f"{prefix}_status"] = fit["status"]
            summary[f"{prefix}_n_used"] = fit["n_used"]
            summary[f"{prefix}_residual_mad"] = fit["residual_mad"]
            summary[f"{prefix}_bic"] = fit["bic"]
            summary[f"{prefix}_spatial_peak_to_peak"] = fit["spatial_peak_to_peak"]
            for name, value in fit["coefficients"].items():
                summary[f"{prefix}_{name}"] = value

    cos2 = pd.to_numeric(df["orientation_cos2"], errors="coerce").to_numpy(dtype=float)
    sin2 = pd.to_numeric(df["orientation_sin2"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(cos2) & np.isfinite(sin2)
    if np.any(valid):
        c = float(np.mean(cos2[valid]))
        s = float(np.mean(sin2[valid]))
        summary["orientation_mean_deg"] = float((0.5 * math.degrees(math.atan2(s, c))) % 180.0)
        summary["orientation_coherence"] = float(math.hypot(c, s))
    else:
        summary["orientation_mean_deg"] = None
        summary["orientation_coherence"] = None
    return summary


def _design_matrix(x: np.ndarray, y: np.ndarray, *, model: str) -> tuple[np.ndarray, tuple[str, ...]]:
    if model == "constant":
        return np.column_stack([np.ones_like(x)]), ("c0",)
    if model == "plane":
        return np.column_stack([np.ones_like(x), x, y]), ("c0", "cx", "cy")
    if model == "quadratic":
        return (
            np.column_stack([np.ones_like(x), x, y, x * x, x * y, y * y]),
            ("c0", "cx", "cy", "cxx", "cxy", "cyy"),
        )
    raise ValueError("model must be one of: constant, plane, quadratic")


def _build_summary_payload(
    *,
    result: PsfFieldDiagnosticsResult,
    source_df: pd.DataFrame,
    frame_df: pd.DataFrame,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    frame_medians: dict[str, float | None] = {}
    for column in (
        "n_psf_sources",
        "fwhm_geom_median",
        "fwhm_geom_plane_cx",
        "fwhm_geom_plane_cy",
        "fwhm_geom_quadratic_spatial_peak_to_peak",
        "ellipticity_median",
        "ellipticity_plane_cx",
        "ellipticity_plane_cy",
        "growth_delta_mag_median",
        "growth_delta_mag_plane_cx",
        "growth_delta_mag_plane_cy",
        "orientation_coherence",
    ):
        if column in frame_df.columns:
            frame_medians[column] = _median_or_none(pd.to_numeric(frame_df[column], errors="coerce").to_numpy(dtype=float))
    return {
        "algorithm": PSF_FIELD_DIAGNOSTICS_ALGORITHM,
        "parameters": dict(parameters),
        "result": result.summary(),
        "frame_medians": frame_medians,
        "source_columns": list(source_df.columns),
        "frame_columns": list(frame_df.columns),
    }


def _render_field_maps(source_df: pd.DataFrame, path: Path, *, bins: int = 12) -> None:
    import matplotlib.pyplot as plt

    metrics = (
        ("fwhm_geom", "FWHM geom residual [pix]"),
        ("ellipticity", "Ellipticity residual"),
        ("growth_delta_mag", "Growth residual [mag]"),
    )
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5), constrained_layout=True)
    work = source_df.copy()
    for metric, label in metrics:
        work[f"{metric}_frame_residual"] = work[metric] - work.groupby("frame_id")[metric].transform("median")
    for ax, (metric, label) in zip(axes, metrics, strict=True):
        grid = _binned_median_grid(
            work["xnorm"].to_numpy(dtype=float),
            work["ynorm"].to_numpy(dtype=float),
            pd.to_numeric(work[f"{metric}_frame_residual"], errors="coerce").to_numpy(dtype=float),
            bins=bins,
        )
        im = ax.imshow(grid, origin="lower", extent=(-1, 1, -1, 1), aspect="equal")
        ax.set_title(label)
        ax.set_xlabel("x normalized")
        ax.set_ylabel("y normalized")
        fig.colorbar(im, ax=ax, shrink=0.8)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _render_time_series(frame_df: pd.DataFrame, path: Path) -> None:
    import matplotlib.pyplot as plt

    x = np.arange(len(frame_df), dtype=int)
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, constrained_layout=True)
    series = (
        ("fwhm_geom_plane_cy", "FWHM plane cy"),
        ("ellipticity_plane_cy", "Ellipticity plane cy"),
        ("growth_delta_mag_plane_cy", "Growth plane cy [mag]"),
    )
    for ax, (column, label) in zip(axes, series, strict=True):
        if column in frame_df.columns:
            values = pd.to_numeric(frame_df[column], errors="coerce").to_numpy(dtype=float)
            ax.plot(x, values, marker="o", linewidth=1)
        ax.set_ylabel(label)
        ax.grid(True, alpha=0.2)
    axes[-1].set_xlabel("Frame order")
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _binned_median_grid(x: np.ndarray, y: np.ndarray, values: np.ndarray, *, bins: int) -> np.ndarray:
    grid = np.full((bins, bins), np.nan, dtype=float)
    ix = np.floor((x + 1.0) * 0.5 * bins).astype(int)
    iy = np.floor((y + 1.0) * 0.5 * bins).astype(int)
    ix = np.clip(ix, 0, bins - 1)
    iy = np.clip(iy, 0, bins - 1)
    for row in range(bins):
        for col in range(bins):
            mask = (ix == col) & (iy == row) & np.isfinite(values)
            if np.any(mask):
                grid[row, col] = float(np.median(values[mask]))
    return grid


def _normalize_coordinate(value: float, size: int) -> float:
    return float(2.0 * float(value) / max(float(size - 1), 1.0) - 1.0)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _median_or_none(values: np.ndarray) -> float | None:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    return float(np.median(finite)) if finite.size else None


def _mad_or_none(values: np.ndarray) -> float | None:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    med = float(np.median(finite))
    return float(np.median(np.abs(finite - med)))


def _corr_or_none(x: Iterable[float], y: Iterable[float]) -> float | None:
    xa = np.asarray(list(x), dtype=float)
    ya = np.asarray(list(y), dtype=float)
    finite = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[finite], ya[finite]
    if xa.size < 3 or float(np.std(xa)) <= 0 or float(np.std(ya)) <= 0:
        return None
    return float(np.corrcoef(xa, ya)[0, 1])
