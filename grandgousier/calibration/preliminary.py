from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import CalibrationVersion, Frame, FrameQc, InstrumentUnit, Measurement, Object
from grandgousier.calibration.provenance import set_source_calibration


@dataclass
class PreliminaryCalibrationResult:
    selected_measurements: int = 0
    checked_measurements: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_other_catalog: int = 0
    skipped_no_qc: int = 0
    skipped_bad_qc: int = 0
    skipped_no_mag_inst: int = 0
    skipped_nonfinite: int = 0
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    target_calibration_name: str | None = None
    dry_run: bool = False


@dataclass
class LightcurveRow:
    measurement_id: int
    frame_id: int
    object_id: int
    source_id: str | None
    catalog_name: str | None
    date_obs: object | None
    mjd: float | None
    filter_name: str | None
    x: float | None
    y: float | None
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_cal: float | None
    mag_err: float | None
    local_zp_correction: float | None
    flags: dict


def calibrate_measurements_from_frame_qc(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    catalog_name: str,
    source_catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    qc_algorithm: str = "detection_catalog_qc_v1",
    min_zp_n: int = 10,
    max_zp_mad: float | None = None,
    min_n_matched: int | None = None,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> PreliminaryCalibrationResult:
    """Create preliminary calibrated measurements using per-frame QC zero points.

    This is deliberately a diagnostic MVP calibration, not a final transformation to a
    standard system. It applies frame_qc.zp_median computed from detection-catalog
    matches to raw/forced-aperture measurements:

        mag_cal = mag_inst + frame_qc.zp_median

    The target calibration is stored as a separate CalibrationVersion and the source
    measurements are copied rather than overwritten.
    """

    result = PreliminaryCalibrationResult(dry_run=dry_run, target_calibration_name=target_calibration_name)
    if source_catalog_name is None:
        source_catalog_name = catalog_name

    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_preliminary_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            source_calibration_name=source_calibration_name,
            catalog_name=catalog_name,
            source_catalog_name=source_catalog_name,
            qc_algorithm=qc_algorithm,
            min_zp_n=min_zp_n,
            max_zp_mad=max_zp_mad,
            min_n_matched=min_n_matched,
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id

    qc_by_frame = _load_frame_qc_map(
        session,
        catalog_name=catalog_name,
        qc_algorithm=qc_algorithm,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
    )

    stmt = (
        select(Measurement, Frame)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .where(Measurement.calibration_version_id == source_cv.calibration_version_id)
        .order_by(Measurement.frame_id, Measurement.object_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    source_rows = session.execute(stmt).all()
    result.selected_measurements = len(source_rows)

    rows_to_insert = []
    for measurement, frame in tqdm(source_rows, desc="Preliminary calibration", unit="meas"):
        result.checked_measurements += 1
        if source_catalog_name and (measurement.flags or {}).get("catalog_name") != source_catalog_name:
            result.skipped_other_catalog += 1
            continue
        qc = qc_by_frame.get(frame.frame_id)
        if qc is None:
            result.skipped_no_qc += 1
            continue
        if not _qc_is_usable(qc, min_zp_n=min_zp_n, max_zp_mad=max_zp_mad, min_n_matched=min_n_matched):
            result.skipped_bad_qc += 1
            continue
        if measurement.mag_inst is None:
            result.skipped_no_mag_inst += 1
            continue

        try:
            mag_inst = float(measurement.mag_inst)
            zp = float(qc.zp_median)
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
                "calibration_stage": "preliminary_frame_qc_zp",
                "source_calibration_version_id": source_cv.calibration_version_id,
                "source_calibration_name": source_cv.name,
                "frame_qc_id": qc.frame_qc_id,
                "frame_qc_catalog_name": catalog_name,
                "frame_qc_algorithm": qc_algorithm,
                "frame_zp_median": qc.zp_median,
                "frame_zp_mad": qc.zp_mad,
                "frame_zp_n": qc.zp_n,
                "warning": "diagnostic Gaia-G zero point; not final color-corrected photometry",
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
                "local_zp_correction": qc.zp_median,
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


def build_object_summary(
    session: Session,
    *,
    calibration_name: str | None = None,
    catalog_name: str | None = None,
    source_id: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    limit: int | None = None,
) -> list[dict]:
    stmt = (
        select(Measurement, Object, Frame, CalibrationVersion, InstrumentUnit)
        .join(Object, Object.object_id == Measurement.object_id)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .join(CalibrationVersion, CalibrationVersion.calibration_version_id == Measurement.calibration_version_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .order_by(Object.object_id, Frame.mjd)
    )
    if calibration_name:
        stmt = stmt.where(CalibrationVersion.name == calibration_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)

    grouped: dict[tuple[int, int | None], list[tuple[Measurement, Object, Frame, CalibrationVersion, InstrumentUnit | None]]] = {}
    for meas, obj, frame, cv, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        if source_id and flags.get("source_id") != source_id:
            continue
        key = (obj.object_id, cv.calibration_version_id)
        grouped.setdefault(key, []).append((meas, obj, frame, cv, iu))

    rows: list[dict] = []
    for (_object_id, _cv_id), values in grouped.items():
        meas0, obj, _frame0, cv, iu = values[0]
        mag_cal_values = [float(m.mag_cal) for m, *_ in values if m.mag_cal is not None and _is_finite(m.mag_cal)]
        mag_inst_values = [float(m.mag_inst) for m, *_ in values if m.mag_inst is not None and _is_finite(m.mag_inst)]
        mjd_values = [float(f.mjd) for _m, _o, f, _cv, _iu in values if f.mjd is not None and _is_finite(f.mjd)]
        flags0 = meas0.flags or {}
        preferred_values = mag_cal_values if mag_cal_values else mag_inst_values
        rows.append(
            {
                "object_id": obj.object_id,
                "calibration_name": cv.name,
                "instrument_unit_id": getattr(iu, "instrument_unit_id", None),
                "instrument": getattr(iu, "instrument", None),
                "filter_name": getattr(iu, "filter_name", None),
                "catalog_name": flags0.get("catalog_name"),
                "source_id": flags0.get("source_id"),
                "ra_deg": obj.ra_deg,
                "dec_deg": obj.dec_deg,
                "n_measurements": len(values),
                "n_mag_inst": len(mag_inst_values),
                "n_mag_cal": len(mag_cal_values),
                "mag_mean": _mean(preferred_values),
                "mag_median": _median(preferred_values),
                "mag_rms": _sample_rms(preferred_values),
                "mag_mad": _mad(preferred_values),
                "mjd_min": min(mjd_values) if mjd_values else None,
                "mjd_max": max(mjd_values) if mjd_values else None,
            }
        )
    rows.sort(key=lambda d: (d.get("calibration_name") or "", d.get("source_id") or "", d.get("object_id") or 0))
    if limit is not None:
        rows = rows[:limit]
    return rows


def load_lightcurve_rows(
    session: Session,
    *,
    calibration_name: str | None = None,
    source_id: str | None = None,
    object_id: int | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    limit: int | None = None,
) -> list[LightcurveRow]:
    stmt = (
        select(Measurement, Frame, Object, CalibrationVersion, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .join(Object, Object.object_id == Measurement.object_id)
        .join(CalibrationVersion, CalibrationVersion.calibration_version_id == Measurement.calibration_version_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .order_by(Frame.mjd, Frame.date_obs, Measurement.measurement_id)
    )
    if calibration_name:
        stmt = stmt.where(CalibrationVersion.name == calibration_name)
    if object_id is not None:
        stmt = stmt.where(Measurement.object_id == object_id)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    rows: list[LightcurveRow] = []
    for meas, frame, obj, cv, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        if source_id and flags.get("source_id") != source_id:
            continue
        rows.append(
            LightcurveRow(
                measurement_id=meas.measurement_id,
                frame_id=frame.frame_id,
                object_id=obj.object_id,
                source_id=flags.get("source_id"),
                catalog_name=flags.get("catalog_name"),
                date_obs=frame.date_obs,
                mjd=frame.mjd,
                filter_name=getattr(iu, "filter_name", None),
                x=meas.x,
                y=meas.y,
                flux=meas.flux,
                flux_err=meas.flux_err,
                mag_inst=meas.mag_inst,
                mag_cal=meas.mag_cal,
                mag_err=meas.mag_err,
                local_zp_correction=meas.local_zp_correction,
                flags=flags,
            )
        )
        if limit is not None and len(rows) >= limit:
            break
    return rows


def export_lightcurve_csv(rows: Iterable[LightcurveRow], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "measurement_id",
        "frame_id",
        "object_id",
        "source_id",
        "catalog_name",
        "date_obs",
        "mjd",
        "filter_name",
        "x",
        "y",
        "flux",
        "flux_err",
        "mag_inst",
        "mag_cal",
        "mag_err",
        "local_zp_correction",
    ]
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(_lightcurve_row_to_dict(row))
            count += 1
    return count


def lightcurve_rows_to_dicts(rows: Iterable[LightcurveRow]) -> list[dict]:
    return [_lightcurve_row_to_dict(row) for row in rows]


def _lightcurve_row_to_dict(row: LightcurveRow) -> dict:
    return {
        "measurement_id": row.measurement_id,
        "frame_id": row.frame_id,
        "object_id": row.object_id,
        "source_id": row.source_id,
        "catalog_name": row.catalog_name,
        "date_obs": row.date_obs.isoformat() if row.date_obs is not None else None,
        "mjd": row.mjd,
        "filter_name": row.filter_name,
        "x": row.x,
        "y": row.y,
        "flux": row.flux,
        "flux_err": row.flux_err,
        "mag_inst": row.mag_inst,
        "mag_cal": row.mag_cal,
        "mag_err": row.mag_err,
        "local_zp_correction": row.local_zp_correction,
    }


def _load_frame_qc_map(
    session: Session,
    *,
    catalog_name: str,
    qc_algorithm: str,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
) -> dict[int, FrameQc]:
    stmt = (
        select(FrameQc)
        .join(Frame, Frame.frame_id == FrameQc.frame_id)
        .where(FrameQc.catalog_name == catalog_name)
        .where(FrameQc.algorithm == qc_algorithm)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    return {qc.frame_id: qc for qc in session.scalars(stmt).all()}


def _qc_is_usable(qc: FrameQc, *, min_zp_n: int, max_zp_mad: float | None, min_n_matched: int | None) -> bool:
    if qc.zp_median is None or not _is_finite(qc.zp_median):
        return False
    if qc.zp_n is None or int(qc.zp_n) < min_zp_n:
        return False
    if min_n_matched is not None and (qc.n_matched is None or int(qc.n_matched) < min_n_matched):
        return False
    if max_zp_mad is not None:
        if qc.zp_mad is None or not _is_finite(qc.zp_mad) or float(qc.zp_mad) > max_zp_mad:
            return False
    return True


def _get_or_create_preliminary_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    source_calibration_name: str,
    catalog_name: str,
    source_catalog_name: str | None,
    qc_algorithm: str,
    min_zp_n: int,
    max_zp_mad: float | None,
    min_n_matched: int | None,
) -> CalibrationVersion:
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    params = {
        "source_calibration_name": source_calibration_name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "catalog_name": catalog_name,
        "source_catalog_name": source_catalog_name,
        "qc_algorithm": qc_algorithm,
        "formula": "mag_cal = mag_inst + frame_qc.zp_median",
        "min_zp_n": min_zp_n,
        "max_zp_mad": max_zp_mad,
        "min_n_matched": min_n_matched,
        "warning": "diagnostic Gaia-G zero point; no color term",
    }
    if cv is None:
        cv = CalibrationVersion(
            name=name,
            model_name="preliminary_frame_qc_zero_point_v1",
            instrument_unit_id=instrument_unit_id,
            used_catalogs={"frame_qc_catalog": catalog_name},
            parameters=params,
            notes="MVP5.2 preliminary per-frame zero point calibration. Not final science photometry.",
        )
        set_source_calibration(cv, source_cv)
        session.add(cv)
    else:
        cv.model_name = "preliminary_frame_qc_zero_point_v1"
        cv.instrument_unit_id = instrument_unit_id
        cv.used_catalogs = {"frame_qc_catalog": catalog_name}
        cv.parameters = params
        set_source_calibration(cv, source_cv)
        cv.notes = "MVP5.2 preliminary per-frame zero point calibration. Not final science photometry."
    return cv


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _mean(values: list[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _mad(values: list[float]) -> float | None:
    if not values:
        return None
    med = median(values)
    return float(median([abs(v - med) for v in values]))


def _sample_rms(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mu = sum(values) / len(values)
    return float(math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1)))
