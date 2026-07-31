from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import CalibrationVersion, Frame, InstrumentUnit, Measurement, Object
from grandgousier.calibration.provenance import set_source_calibration


@dataclass
class ApertureZpFrameStat:
    frame_id: int
    n_candidates: int = 0
    n_used: int = 0
    zp_median: float | None = None
    zp_mad: float | None = None
    zp_std: float | None = None


@dataclass
class ApertureZpCalibrationResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_name: str | None = None
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    selected_measurements: int = 0
    checked_measurements: int = 0
    selected_frames: int = 0
    frames_with_zp: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_other_catalog: int = 0
    skipped_no_ref_mag: int = 0
    skipped_no_mag_inst: int = 0
    skipped_bad_mag_err: int = 0
    skipped_saturated: int = 0
    skipped_no_frame_zp: int = 0
    skipped_nonfinite: int = 0
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None
    dry_run: bool = False


def aperture_zp_calibrate(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    catalog_name: str,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    mag_key: str = "G",
    min_ref_stars_per_frame: int = 20,
    max_mag_err: float | None = 0.08,
    max_zp_mad: float | None = None,
    clip_sigma: float = 3.0,
    n_iterations: int = 3,
    exclude_saturated: bool = True,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> ApertureZpCalibrationResult:
    """Calibrate forced-aperture measurements with a zero point derived from the same measurements.

    This fixes the mismatch of using a source-detection zero point for forced-aperture
    photometry. For each frame:

        zp_i = catalog_mag - forced_aperture_mag_inst

    A robust median zero point is computed from the forced-aperture measurements
    themselves. The target calibration is stored as a new CalibrationVersion; source
    measurements are copied, not overwritten.
    """

    result = ApertureZpCalibrationResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        dry_run=dry_run,
        insert_batch_size=int(insert_batch_size),
    )

    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            source_calibration_name=source_calibration_name,
            catalog_name=catalog_name,
            mag_key=mag_key,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
            max_mag_err=max_mag_err,
            max_zp_mad=max_zp_mad,
            clip_sigma=clip_sigma,
            n_iterations=n_iterations,
            exclude_saturated=exclude_saturated,
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id

    stmt = (
        select(Measurement, Frame, Object, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .join(Object, Object.object_id == Measurement.object_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(Measurement.calibration_version_id == source_cv.calibration_version_id)
        .order_by(Frame.frame_id, Measurement.object_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    rows = session.execute(stmt).all()
    result.selected_measurements = len(rows)

    frame_candidates: dict[int, list[tuple[float, Measurement, Frame, Object]]] = defaultdict(list)
    source_rows: list[tuple[Measurement, Frame, Object, InstrumentUnit | None]] = []
    for measurement, frame, obj, iu in rows:
        result.checked_measurements += 1
        flags = measurement.flags or {}
        if flags.get("catalog_name") != catalog_name:
            result.skipped_other_catalog += 1
            continue
        source_rows.append((measurement, frame, obj, iu))

        if measurement.mag_inst is None:
            result.skipped_no_mag_inst += 1
            continue
        try:
            mag_inst = float(measurement.mag_inst)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(mag_inst):
            result.skipped_nonfinite += 1
            continue

        if exclude_saturated and bool(flags.get("saturated_or_near_saturated")):
            result.skipped_saturated += 1
            continue

        if max_mag_err is not None and measurement.mag_err is not None:
            try:
                if float(measurement.mag_err) > float(max_mag_err):
                    result.skipped_bad_mag_err += 1
                    continue
            except Exception:
                pass

        ref_mag = _get_ref_mag(obj, mag_key)
        if ref_mag is None:
            result.skipped_no_ref_mag += 1
            continue

        zp = float(ref_mag) - mag_inst
        if not math.isfinite(zp):
            result.skipped_nonfinite += 1
            continue
        frame_candidates[int(frame.frame_id)].append((zp, measurement, frame, obj))

    result.selected_frames = len(frame_candidates)

    frame_stats: dict[int, ApertureZpFrameStat] = {}
    for frame_id, values in sorted(frame_candidates.items()):
        zps = [v[0] for v in values]
        clipped = _sigma_clip(zps, sigma=clip_sigma, n_iterations=n_iterations)
        stat = ApertureZpFrameStat(frame_id=frame_id, n_candidates=len(zps), n_used=len(clipped))
        if len(clipped) >= int(min_ref_stars_per_frame):
            stat.zp_median = _median(clipped)
            stat.zp_mad = _mad(clipped)
            stat.zp_std = _std(clipped)
            if max_zp_mad is None or (stat.zp_mad is not None and stat.zp_mad <= float(max_zp_mad)):
                frame_stats[frame_id] = stat

    result.frames_with_zp = len(frame_stats)
    frame_zps = [s.zp_median for s in frame_stats.values() if s.zp_median is not None and math.isfinite(s.zp_median)]
    result.frame_zp_median = _median(frame_zps) if frame_zps else None
    result.frame_zp_mad = _mad(frame_zps) if frame_zps else None

    rows_to_insert = []
    for measurement, frame, obj, iu in tqdm(source_rows, desc="Aperture ZP calibration", unit="meas"):
        stat = frame_stats.get(int(frame.frame_id))
        if stat is None or stat.zp_median is None:
            result.skipped_no_frame_zp += 1
            continue
        if measurement.mag_inst is None:
            result.skipped_no_mag_inst += 1
            continue
        try:
            mag_inst = float(measurement.mag_inst)
            zp = float(stat.zp_median)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(mag_inst) or not math.isfinite(zp):
            result.skipped_nonfinite += 1
            continue

        mag_cal = mag_inst + zp
        flags = dict(measurement.flags or {})
        flags.update(
            {
                "calibration_stage": "aperture_frame_zp",
                "source_calibration_version_id": source_cv.calibration_version_id,
                "source_calibration_name": source_cv.name,
                "catalog_name": catalog_name,
                "mag_key": mag_key,
                "frame_aperture_zp": stat.zp_median,
                "frame_aperture_zp_mad": stat.zp_mad,
                "frame_aperture_zp_std": stat.zp_std,
                "frame_aperture_zp_n": stat.n_used,
                "frame_aperture_zp_candidates": stat.n_candidates,
                "warning": "diagnostic aperture zero point; color term is not applied",
            }
        )
        result.calibrated += 1
        if dry_run or target_cv is None:
            continue

        rows_to_insert.append(
            {
                "object_id": measurement.object_id,
                "frame_id": measurement.frame_id,
                "calibration_version_id": target_cv.calibration_version_id,
                "x": measurement.x,
                "y": measurement.y,
                "flux": measurement.flux,
                "flux_err": measurement.flux_err,
                "mag_inst": measurement.mag_inst,
                "mag_cal": mag_cal,
                "mag_err": measurement.mag_err,
                "aperture_radius_pix": measurement.aperture_radius_pix,
                "local_background": measurement.local_background,
                "local_zp_correction": stat.zp_median,
                "centroid_dx_pix": measurement.centroid_dx_pix,
                "centroid_dy_pix": measurement.centroid_dy_pix,
                "flags": flags,
            }
        )

    if rows_to_insert:
        result.insert_batches = upsert_measurement_rows(
            session,
            rows_to_insert,
            insert_batch_size=insert_batch_size,
        )
        result.inserted_or_updated = len(rows_to_insert)

    if not dry_run:
        session.commit()
    return result


def _get_or_create_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    source_calibration_name: str,
    catalog_name: str,
    mag_key: str,
    min_ref_stars_per_frame: int,
    max_mag_err: float | None,
    max_zp_mad: float | None,
    clip_sigma: float,
    n_iterations: int,
    exclude_saturated: bool,
) -> CalibrationVersion:
    existing = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    if existing is not None:
        set_source_calibration(existing, source_cv)
        return existing

    cv = CalibrationVersion(
        name=name,
        model_name="aperture_zp_calibration_v1",
        instrument_unit_id=instrument_unit_id,
        instrument_profile={},
        used_catalogs={"catalog_name": catalog_name, "mag_key": mag_key},
        parameters={
            "source_calibration_name": source_calibration_name,
            "source_calibration_version_id": source_cv.calibration_version_id,
            "catalog_name": catalog_name,
            "mag_key": mag_key,
            "min_ref_stars_per_frame": min_ref_stars_per_frame,
            "max_mag_err": max_mag_err,
            "max_zp_mad": max_zp_mad,
            "clip_sigma": clip_sigma,
            "n_iterations": n_iterations,
            "exclude_saturated": exclude_saturated,
            "warning": "zero point derived from forced-aperture mag_inst, not from source detections",
        },
    )
    set_source_calibration(cv, source_cv)
    session.add(cv)
    session.flush()
    return cv


def _get_ref_mag(obj: Object, mag_key: str) -> float | None:
    ref_mags = obj.ref_mags or {}
    value = ref_mags.get(mag_key)
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _sigma_clip(values: list[float], *, sigma: float, n_iterations: int) -> list[float]:
    current = [float(v) for v in values if math.isfinite(float(v))]
    if not current:
        return []
    for _ in range(max(0, int(n_iterations))):
        if len(current) < 3:
            break
        med = _median(current)
        scatter = _mad(current)
        if scatter is None or scatter <= 0:
            break
        limit = float(sigma) * 1.4826 * scatter
        new = [v for v in current if abs(v - med) <= limit]
        if len(new) == len(current):
            break
        current = new
    return current


def _median(values: list[float]) -> float | None:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return None
    n = len(vals)
    if n % 2:
        return float(vals[n // 2])
    return float((vals[n // 2 - 1] + vals[n // 2]) / 2.0)


def _mad(values: list[float]) -> float | None:
    med = _median(values)
    if med is None:
        return None
    return _median([abs(float(v) - med) for v in values])


def _std(values: list[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
