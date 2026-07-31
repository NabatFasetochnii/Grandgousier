from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.models import Frame, SourceDetection
from grandgousier.photometry.forced_aperture import measure_one_source


@dataclass
class SourceDetectionResult:
    selected_frames: int = 0
    checked_frames: int = 0
    detected: int = 0
    inserted: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    deleted_existing: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    detection_cap_reached_frames: int = 0
    raw_candidates_total: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class DetectedSource:
    x: float
    y: float
    ra_deg: float | None
    dec_deg: float | None
    peak_adu: float
    snr: float
    flux: float | None
    flux_err: float | None
    local_background: float | None
    npix: int | None
    flags: dict


DEFAULT_ALGORITHM = "numpy_localmax_v1"


def detect_sources_in_frames(
    session: Session,
    *,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    threshold_sigma: float = 5.0,
    max_sources_per_frame: int = 2000,
    min_separation_pix: float = 5.0,
    aperture_radius_pix: float = 3.0,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    centroid_box_radius_pix: int = 3,
    replace_existing: bool = False,
    limit_frames: int | None = None,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> SourceDetectionResult:
    if threshold_sigma <= 0:
        raise ValueError("threshold_sigma must be positive")
    if max_sources_per_frame <= 0:
        raise ValueError("max_sources_per_frame must be positive")
    if min_separation_pix < 0:
        raise ValueError("min_separation_pix must be non-negative")

    result = SourceDetectionResult(dry_run=dry_run, insert_batch_size=int(insert_batch_size))
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.selected_frames = len(frames)

    parameters = {
        "threshold_sigma": threshold_sigma,
        "max_sources_per_frame": max_sources_per_frame,
        "min_separation_pix": min_separation_pix,
        "aperture_radius_pix": aperture_radius_pix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
        "centroid_box_radius_pix": centroid_box_radius_pix,
    }

    for frame in tqdm(frames, desc="Detecting sources", unit="frame"):
        result.checked_frames += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        try:
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
                header = hdul[0].header
            if data.ndim != 2:
                result.failed_frames += 1
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                wcs = WCS(header)
            if not wcs.has_celestial:
                wcs = None

            detections = detect_sources_in_image(
                data,
                wcs=wcs,
                threshold_sigma=threshold_sigma,
                max_sources=max_sources_per_frame,
                min_separation_pix=min_separation_pix,
                aperture_radius_pix=aperture_radius_pix,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                centroid_box_radius_pix=centroid_box_radius_pix,
                exptime=_safe_positive_float(frame.exptime),
                saturation_level_adu=_resolve_saturation(frame, header),
            )
            result.detected += len(detections)
            if detections:
                frame_raw_candidates = max(int(det.flags.get("n_candidates_raw_before_cap") or 0) for det in detections)
                result.raw_candidates_total += frame_raw_candidates
                if any(bool(det.flags.get("DETECTION_CAP_REACHED")) for det in detections):
                    result.detection_cap_reached_frames += 1

            if dry_run:
                continue

            if replace_existing:
                del_stmt = delete(SourceDetection).where(
                    SourceDetection.frame_id == frame.frame_id,
                    SourceDetection.algorithm == DEFAULT_ALGORITHM,
                )
                deleted = session.execute(del_stmt).rowcount or 0
                result.deleted_existing += deleted

            rows = [
                {
                    "frame_id": frame.frame_id,
                    "x": det.x,
                    "y": det.y,
                    "ra_deg": det.ra_deg,
                    "dec_deg": det.dec_deg,
                    "peak_adu": det.peak_adu,
                    "flux": det.flux,
                    "flux_err": det.flux_err,
                    "local_background": det.local_background,
                    "snr": det.snr,
                    "npix": det.npix,
                    "detection_status": "DETECTED",
                    "algorithm": DEFAULT_ALGORITHM,
                    "parameters": parameters,
                    "flags": det.flags,
                }
                for det in detections
            ]
            if rows:
                result.insert_batches += _insert_source_detections(
                    session,
                    rows,
                    insert_batch_size=insert_batch_size,
                )
                result.inserted += len(rows)
        except Exception:
            result.failed_frames += 1

    if not dry_run:
        session.commit()
    return result


def _insert_source_detections(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    if not rows:
        return 0
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        session.execute(insert(SourceDetection.__table__).values(list(chunk)))
        n_batches += 1
    return n_batches


def detect_sources_in_image(
    data: np.ndarray,
    *,
    wcs: WCS | None,
    threshold_sigma: float,
    max_sources: int,
    min_separation_pix: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    centroid_box_radius_pix: int,
    exptime: float | None,
    saturation_level_adu: float | None,
) -> list[DetectedSource]:
    finite = np.isfinite(data)
    if finite.sum() < 100:
        return []
    values = data[finite]
    background = float(np.nanmedian(values))
    mad = float(np.nanmedian(np.abs(values - background)))
    sigma = 1.4826 * mad
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = float(np.nanstd(values))
    if not math.isfinite(sigma) or sigma <= 0:
        return []

    threshold = background + threshold_sigma * sigma
    candidates = _local_maxima(data, threshold=threshold)
    n_candidates_raw = len(candidates)
    if not candidates:
        return []

    # Sort candidates by decreasing S/N.
    scored = []
    for y_int, x_int in candidates:
        peak = float(data[y_int, x_int])
        snr = (peak - background) / sigma
        if math.isfinite(snr):
            scored.append((snr, y_int, x_int, peak))
    scored.sort(reverse=True)

    chosen: list[tuple[float, float, float, float]] = []
    sep2 = min_separation_pix * min_separation_pix
    for snr, y_int, x_int, peak in scored:
        if len(chosen) >= max_sources:
            break
        if sep2 > 0 and any((x_int - x0) ** 2 + (y_int - y0) ** 2 < sep2 for _, y0, x0, _ in chosen):
            continue
        chosen.append((snr, float(y_int), float(x_int), peak))

    detections: list[DetectedSource] = []
    for snr, y0, x0, peak in chosen:
        x_cen, y_cen = _centroid_com(data, x0, y0, background, radius=centroid_box_radius_pix)
        if x_cen is None or y_cen is None:
            x_cen, y_cen = x0, y0
        ra_deg = dec_deg = None
        if wcs is not None:
            try:
                ra, dec = wcs.pixel_to_world_values(float(x_cen), float(y_cen))
                if math.isfinite(float(ra)) and math.isfinite(float(dec)):
                    ra_deg = float(ra) % 360.0
                    dec_deg = float(dec)
            except Exception:
                ra_deg = dec_deg = None
        measurement = measure_one_source(
            data,
            x=float(x_cen),
            y=float(y_cen),
            aperture_radius_pix=aperture_radius_pix,
            annulus_inner_pix=annulus_inner_pix,
            annulus_outer_pix=annulus_outer_pix,
            exptime=exptime,
            zero_point=0.0,
            saturation_level_adu=saturation_level_adu,
        )
        flags = {
            **measurement.flags,
            "background_global": background,
            "sigma_global": sigma,
            "threshold_adu": threshold,
            "centroid_method": "positive_com",
            "n_candidates_raw_before_cap": int(n_candidates_raw),
            "max_sources_per_frame": int(max_sources),
            "DETECTION_CAP_REACHED": bool(len(chosen) >= max_sources and n_candidates_raw >= max_sources),
        }
        detections.append(
            DetectedSource(
                x=float(x_cen),
                y=float(y_cen),
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                peak_adu=peak,
                snr=float(snr),
                flux=measurement.flux,
                flux_err=measurement.flux_err,
                local_background=measurement.local_background,
                npix=measurement.flags.get("n_aperture_pix"),
                flags=flags,
            )
        )
    return detections


def _local_maxima(data: np.ndarray, *, threshold: float) -> list[tuple[int, int]]:
    finite = np.isfinite(data)
    mask = finite & (data > threshold)
    if not np.any(mask):
        return []
    padded = np.pad(data, 1, mode="constant", constant_values=-np.inf)
    center = padded[1:-1, 1:-1]
    local = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neigh = padded[1 + dy : 1 + dy + data.shape[0], 1 + dx : 1 + dx + data.shape[1]]
            local &= center >= neigh
    ys, xs = np.nonzero(local)
    return list(zip(ys.tolist(), xs.tolist()))


def _centroid_com(data: np.ndarray, x: float, y: float, background: float, *, radius: int) -> tuple[float | None, float | None]:
    x_int = int(round(x))
    y_int = int(round(y))
    y0 = max(0, y_int - radius)
    y1 = min(data.shape[0], y_int + radius + 1)
    x0 = max(0, x_int - radius)
    x1 = min(data.shape[1], x_int + radius + 1)
    cut = data[y0:y1, x0:x1] - background
    cut = np.where(np.isfinite(cut) & (cut > 0), cut, 0.0)
    total = float(cut.sum())
    if total <= 0 or not math.isfinite(total):
        return None, None
    yy, xx = np.indices(cut.shape)
    x_cen = float((cut * (xx + x0)).sum() / total)
    y_cen = float((cut * (yy + y0)).sum() / total)
    return x_cen, y_cen


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument:
        stmt = stmt.where(Frame.instrument == instrument)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if only_wcs_ok:
        stmt = stmt.where(Frame.wcs_status == "HEADER_WCS_OK")
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _safe_positive_float(value) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    return x


def _resolve_saturation(frame: Frame, header) -> float | None:
    for key in ("SATURATE", "SATLEVEL", "SATURATI", "SATURATE_LEVEL"):
        try:
            value = float(header.get(key))
        except Exception:
            value = None
        if value is not None and math.isfinite(value) and value > 0:
            return value
    return None
