from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import CalibrationVersion, Frame, InstrumentUnit, Measurement, Object
from grandgousier.calibration.provenance import set_source_calibration


@dataclass
class EnsembleCalibrationResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    target_calibration_name: str | None = None
    selected_measurements: int = 0
    checked_measurements: int = 0
    selected_objects: int = 0
    reference_candidates: int = 0
    usable_reference_objects: int = 0
    selected_frames: int = 0
    frames_with_offsets: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_other_catalog: int = 0
    skipped_no_input_mag: int = 0
    skipped_no_frame_offset: int = 0
    skipped_nonfinite: int = 0
    dry_run: bool = False
    frame_offset_median: float | None = None
    frame_offset_mad: float | None = None
    requested_excluded_reference_objects: int = 0
    requested_excluded_reference_object_ids: list[int] = field(default_factory=list)
    actually_excluded_reference_objects: int = 0
    actually_excluded_reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_objects: int = 0
    excluded_reference_object_ids: list[int] = field(default_factory=list)


@dataclass
class _MeasRow:
    measurement: Measurement
    frame: Frame
    obj: Object
    value: float
    is_reference: bool = False


@dataclass
class _FrameOffset:
    frame_id: int
    offset: float
    n_ref: int
    scatter_mad: float | None


@dataclass
class _ObjectStats:
    object_id: int
    n: int
    median_mag: float
    rms: float | None
    mad: float | None


def ensemble_calibrate_measurements(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    min_ref_measurements: int = 5,
    min_ref_stars_per_frame: int = 20,
    max_ref_rms: float | None = 0.08,
    max_ref_mad: float | None = 0.05,
    min_mag: float | None = None,
    max_mag: float | None = None,
    n_iterations: int = 2,
    prefer_mag_cal: bool = True,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    exclude_reference_object_ids: list[int] | None = None,
) -> EnsembleCalibrationResult:
    """Create an ensemble-differential calibration from an existing measurement set.

    This is still a local-series calibration, not an absolute standard-system solution. It uses
    stable objects in the selected measurements as an ensemble, estimates a robust per-frame
    residual offset, then copies measurements into a new CalibrationVersion with:

        output_mag = input_mag - frame_ensemble_offset

    If the source calibration has mag_cal, that is used by default. Otherwise mag_inst is used.
    """

    if min_ref_stars_per_frame < 1:
        raise ValueError("min_ref_stars_per_frame must be >= 1")
    if min_measurements < 1 or min_ref_measurements < 1:
        raise ValueError("min_measurements/min_ref_measurements must be >= 1")
    if n_iterations < 1:
        raise ValueError("n_iterations must be >= 1")

    result = EnsembleCalibrationResult(dry_run=dry_run, target_calibration_name=target_calibration_name)
    requested_excluded_reference_ids = {int(v) for v in (exclude_reference_object_ids or []) if v is not None}
    result.requested_excluded_reference_object_ids = sorted(requested_excluded_reference_ids)
    result.requested_excluded_reference_objects = len(requested_excluded_reference_ids)
    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_ensemble_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            source_calibration_name=source_calibration_name,
            catalog_name=catalog_name,
            min_measurements=min_measurements,
            min_ref_measurements=min_ref_measurements,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
            max_ref_rms=max_ref_rms,
            max_ref_mad=max_ref_mad,
            min_mag=min_mag,
            max_mag=max_mag,
            n_iterations=n_iterations,
            prefer_mag_cal=prefer_mag_cal,
            exclude_reference_object_ids=sorted(requested_excluded_reference_ids),
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id

    rows = _load_source_measurements(
        session,
        source_cv=source_cv,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        prefer_mag_cal=prefer_mag_cal,
        result=result,
    )
    result.selected_measurements = len(rows)
    object_values: dict[int, list[float]] = {}
    frame_ids_seen: set[int] = set()
    for row in rows:
        object_values.setdefault(row.obj.object_id, []).append(row.value)
        frame_ids_seen.add(row.frame.frame_id)
    result.selected_objects = len(object_values)
    result.selected_frames = len(frame_ids_seen)

    object_stats = _build_object_stats(object_values)
    reference_object_ids = _select_reference_objects(
        object_stats,
        min_ref_measurements=min_ref_measurements,
        max_ref_rms=max_ref_rms,
        max_ref_mad=max_ref_mad,
        min_mag=min_mag,
        max_mag=max_mag,
    )
    before_exclusion = set(reference_object_ids)
    actually_excluded_reference_ids = before_exclusion & requested_excluded_reference_ids
    if requested_excluded_reference_ids:
        reference_object_ids = [obj_id for obj_id in reference_object_ids if int(obj_id) not in requested_excluded_reference_ids]
    result.reference_candidates = len(reference_object_ids)
    result.actually_excluded_reference_object_ids = sorted(actually_excluded_reference_ids)
    result.actually_excluded_reference_objects = len(actually_excluded_reference_ids)
    # Backwards-compatible aliases: in 0.5.33.1 these refer to objects
    # that were actually removed from the reference ensemble, not merely
    # requested as exclusions. The requested set is recorded separately above.
    result.excluded_reference_object_ids = list(result.actually_excluded_reference_object_ids)
    result.excluded_reference_objects = int(result.actually_excluded_reference_objects)
    for row in rows:
        row.is_reference = row.obj.object_id in reference_object_ids

    if not reference_object_ids:
        if not dry_run:
            session.commit()
        return result

    object_medians = {st.object_id: st.median_mag for st in object_stats.values() if st.object_id in reference_object_ids}
    frame_offsets: dict[int, _FrameOffset] = {}
    for _iteration in range(max(1, n_iterations)):
        frame_offsets = _estimate_frame_offsets(
            rows,
            object_medians=object_medians,
            reference_object_ids=reference_object_ids,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
        )
        corrected_ref_values: dict[int, list[float]] = {}
        for row in rows:
            if row.obj.object_id not in reference_object_ids:
                continue
            fo = frame_offsets.get(row.frame.frame_id)
            if fo is None:
                continue
            corrected_ref_values.setdefault(row.obj.object_id, []).append(row.value - fo.offset)
        new_stats = _build_object_stats(corrected_ref_values)
        if new_stats:
            object_medians = {obj_id: st.median_mag for obj_id, st in new_stats.items()}

    result.frames_with_offsets = len(frame_offsets)
    result.usable_reference_objects = len(object_medians)
    offsets = [fo.offset for fo in frame_offsets.values() if _is_finite(fo.offset)]
    result.frame_offset_median = _median(offsets)
    result.frame_offset_mad = _mad(offsets)

    rows_to_insert = []
    for row in tqdm(rows, desc="Ensemble calibration", unit="meas"):
        result.checked_measurements += 1
        fo = frame_offsets.get(row.frame.frame_id)
        if fo is None:
            result.skipped_no_frame_offset += 1
            continue
        if not _is_finite(row.value) or not _is_finite(fo.offset):
            result.skipped_nonfinite += 1
            continue
        output_mag = float(row.value) - float(fo.offset)
        flags = dict(row.measurement.flags or {})
        old_local_zp = row.measurement.local_zp_correction
        new_local_zp = None
        if old_local_zp is not None and _is_finite(old_local_zp):
            new_local_zp = float(old_local_zp) - float(fo.offset)
        else:
            new_local_zp = -float(fo.offset)
        flags.update(
            {
                "calibration_stage": "ensemble_differential",
                "source_calibration_version_id": source_cv.calibration_version_id,
                "source_calibration_name": source_cv.name,
                "ensemble_algorithm": "robust_frame_median_v1",
                "ensemble_frame_offset": fo.offset,
                "ensemble_frame_n_ref": fo.n_ref,
                "ensemble_frame_scatter_mad": fo.scatter_mad,
                "ensemble_object_is_reference": row.is_reference,
                "ensemble_object_requested_reference_exclusion": row.obj.object_id in requested_excluded_reference_ids,
                "ensemble_object_excluded_from_reference": row.obj.object_id in actually_excluded_reference_ids,
                "ensemble_reference_exclusion_reason": "candidate_or_target_object" if row.obj.object_id in requested_excluded_reference_ids else None,
                "ensemble_min_ref_stars_per_frame": min_ref_stars_per_frame,
                "ensemble_n_iterations": n_iterations,
                "warning": "ensemble-differential internal calibration; not final color-corrected standard photometry",
            }
        )
        result.calibrated += 1
        if dry_run or target_cv is None:
            continue
        rows_to_insert.append(
            {
                "object_id": row.measurement.object_id,
                "frame_id": row.measurement.frame_id,
                "calibration_version_id": target_cv.calibration_version_id,
                "x": row.measurement.x,
                "y": row.measurement.y,
                "flux": row.measurement.flux,
                "flux_err": row.measurement.flux_err,
                "mag_inst": row.measurement.mag_inst,
                "mag_cal": output_mag,
                "mag_err": row.measurement.mag_err,
                "aperture_radius_pix": row.measurement.aperture_radius_pix,
                "local_background": row.measurement.local_background,
                "local_zp_correction": new_local_zp,
                "centroid_dx_pix": row.measurement.centroid_dx_pix,
                "centroid_dy_pix": row.measurement.centroid_dy_pix,
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


def build_ensemble_frame_summary(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    limit: int | None = 50,
) -> list[dict]:
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")
    stmt = (
        select(Measurement, Frame, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(Measurement.calibration_version_id == cv.calibration_version_id)
        .order_by(Frame.frame_id, Measurement.measurement_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)

    grouped: dict[int, list[tuple[Measurement, Frame, InstrumentUnit | None]]] = {}
    for meas, frame, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        grouped.setdefault(frame.frame_id, []).append((meas, frame, iu))

    rows: list[dict] = []
    for frame_id, values in grouped.items():
        meas0, frame, iu = values[0]
        offsets = []
        n_ref_values = []
        scatter_values = []
        mag_values = []
        for meas, _frame, _iu in values:
            flags = meas.flags or {}
            off = flags.get("ensemble_frame_offset")
            nref = flags.get("ensemble_frame_n_ref")
            sc = flags.get("ensemble_frame_scatter_mad")
            if _is_finite(off):
                offsets.append(float(off))
            if nref is not None:
                try:
                    n_ref_values.append(int(nref))
                except Exception:
                    pass
            if _is_finite(sc):
                scatter_values.append(float(sc))
            if meas.mag_cal is not None and _is_finite(meas.mag_cal):
                mag_values.append(float(meas.mag_cal))
        rows.append(
            {
                "frame_id": frame_id,
                "instrument_unit_id": getattr(iu, "instrument_unit_id", None),
                "instrument": getattr(iu, "instrument", None),
                "filter_name": getattr(iu, "filter_name", None),
                "date_obs": frame.date_obs.isoformat() if frame.date_obs is not None else None,
                "mjd": frame.mjd,
                "n_measurements": len(values),
                "ensemble_offset": _median(offsets),
                "ensemble_n_ref": max(n_ref_values) if n_ref_values else None,
                "ensemble_scatter_mad": _median(scatter_values),
                "mag_cal_median": _median(mag_values),
                "mag_cal_mad": _mad(mag_values),
            }
        )
    rows.sort(key=lambda d: d.get("frame_id") or 0)
    if limit is not None:
        rows = rows[:limit]
    return rows


def _load_source_measurements(
    session: Session,
    *,
    source_cv: CalibrationVersion,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
    prefer_mag_cal: bool,
    result: EnsembleCalibrationResult,
) -> list[_MeasRow]:
    stmt = (
        select(Measurement, Frame, Object)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .join(Object, Object.object_id == Measurement.object_id)
        .where(Measurement.calibration_version_id == source_cv.calibration_version_id)
        .order_by(Measurement.object_id, Frame.mjd, Frame.frame_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    rows: list[_MeasRow] = []
    for meas, frame, obj in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            result.skipped_other_catalog += 1
            continue
        value = None
        if prefer_mag_cal and meas.mag_cal is not None:
            value = meas.mag_cal
        elif meas.mag_inst is not None:
            value = meas.mag_inst
        elif meas.mag_cal is not None:
            value = meas.mag_cal
        if value is None:
            result.skipped_no_input_mag += 1
            continue
        try:
            value_f = float(value)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(value_f):
            result.skipped_nonfinite += 1
            continue
        rows.append(_MeasRow(measurement=meas, frame=frame, obj=obj, value=value_f))
    # selected_measurements is reset to len(rows) by caller; checked_measurements is useful as raw scan count.
    return rows


def _build_object_stats(object_values: dict[int, list[float]]) -> dict[int, _ObjectStats]:
    out: dict[int, _ObjectStats] = {}
    for object_id, values0 in object_values.items():
        values = [float(v) for v in values0 if _is_finite(v)]
        if not values:
            continue
        out[object_id] = _ObjectStats(
            object_id=object_id,
            n=len(values),
            median_mag=float(median(values)),
            rms=_sample_rms(values),
            mad=_mad(values),
        )
    return out


def _select_reference_objects(
    object_stats: dict[int, _ObjectStats],
    *,
    min_ref_measurements: int,
    max_ref_rms: float | None,
    max_ref_mad: float | None,
    min_mag: float | None,
    max_mag: float | None,
) -> set[int]:
    out: set[int] = set()
    for object_id, st in object_stats.items():
        if st.n < min_ref_measurements:
            continue
        if min_mag is not None and st.median_mag < min_mag:
            continue
        if max_mag is not None and st.median_mag > max_mag:
            continue
        if max_ref_rms is not None:
            if st.rms is None or not _is_finite(st.rms) or st.rms > max_ref_rms:
                continue
        if max_ref_mad is not None:
            if st.mad is None or not _is_finite(st.mad) or st.mad > max_ref_mad:
                continue
        out.add(object_id)
    return out


def _estimate_frame_offsets(
    rows: list[_MeasRow],
    *,
    object_medians: dict[int, float],
    reference_object_ids: set[int],
    min_ref_stars_per_frame: int,
) -> dict[int, _FrameOffset]:
    residuals_by_frame: dict[int, list[float]] = {}
    for row in rows:
        if row.obj.object_id not in reference_object_ids:
            continue
        object_med = object_medians.get(row.obj.object_id)
        if object_med is None or not _is_finite(object_med):
            continue
        residuals_by_frame.setdefault(row.frame.frame_id, []).append(row.value - object_med)
    offsets: dict[int, _FrameOffset] = {}
    for frame_id, residuals0 in residuals_by_frame.items():
        residuals = [float(r) for r in residuals0 if _is_finite(r)]
        if len(residuals) < min_ref_stars_per_frame:
            continue
        offsets[frame_id] = _FrameOffset(
            frame_id=frame_id,
            offset=float(median(residuals)),
            n_ref=len(residuals),
            scatter_mad=_mad(residuals),
        )
    return offsets


def _get_or_create_ensemble_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    source_calibration_name: str,
    catalog_name: str | None,
    min_measurements: int,
    min_ref_measurements: int,
    min_ref_stars_per_frame: int,
    max_ref_rms: float | None,
    max_ref_mad: float | None,
    min_mag: float | None,
    max_mag: float | None,
    n_iterations: int,
    prefer_mag_cal: bool,
    exclude_reference_object_ids: list[int] | None = None,
) -> CalibrationVersion:
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    params = {
        "source_calibration_name": source_calibration_name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "catalog_name": catalog_name,
        "algorithm": "robust_frame_median_v1",
        "formula": "mag_cal = input_mag - frame_ensemble_offset",
        "input_mag": "mag_cal preferred, fallback mag_inst" if prefer_mag_cal else "mag_inst preferred, fallback mag_cal",
        "min_measurements": min_measurements,
        "min_ref_measurements": min_ref_measurements,
        "min_ref_stars_per_frame": min_ref_stars_per_frame,
        "max_ref_rms": max_ref_rms,
        "max_ref_mad": max_ref_mad,
        "min_mag": min_mag,
        "max_mag": max_mag,
        "n_iterations": n_iterations,
        "warning": "internal ensemble-differential calibration; no color term or absolute standard-system transform",
        "exclude_reference_object_ids": list(exclude_reference_object_ids or []),
    }
    if cv is None:
        cv = CalibrationVersion(
            name=name,
            model_name="ensemble_differential_v1",
            instrument_unit_id=instrument_unit_id,
            used_catalogs={"catalog_name": catalog_name} if catalog_name else {},
            parameters=params,
            notes="MVP5.4 robust ensemble differential calibration. Not final color-corrected photometry.",
        )
        set_source_calibration(cv, source_cv)
        session.add(cv)
    else:
        cv.model_name = "ensemble_differential_v1"
        cv.instrument_unit_id = instrument_unit_id
        cv.used_catalogs = {"catalog_name": catalog_name} if catalog_name else {}
        cv.parameters = params
        set_source_calibration(cv, source_cv)
        cv.notes = "MVP5.4 robust ensemble differential calibration. Not final color-corrected photometry."
    return cv


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return float(median(vals)) if vals else None


def _mad(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = median(vals)
    return float(median([abs(v - med) for v in vals]))


def _sample_rms(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return float(math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)))
