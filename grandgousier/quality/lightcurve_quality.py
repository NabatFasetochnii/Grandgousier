from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import CalibrationVersion, Frame, InstrumentUnit, Measurement, Object


@dataclass
class ObjectScatterRow:
    object_id: int
    calibration_name: str | None
    instrument_unit_id: int | None
    instrument: str | None
    filter_name: str | None
    catalog_name: str | None
    source_id: str | None
    ra_deg: float | None
    dec_deg: float | None
    n_measurements: int
    mag_mean: float | None
    mag_median: float | None
    mag_rms: float | None
    mag_mad: float | None
    mag_err_median: float | None
    flux_median: float | None
    ref_mag: float | None
    color: float | None
    mjd_min: float | None
    mjd_max: float | None
    mag_p10: float | None = None
    mag_p90: float | None = None
    robust_peak_to_peak: float | None = None

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "calibration_name": self.calibration_name,
            "instrument_unit_id": self.instrument_unit_id,
            "instrument": self.instrument,
            "filter_name": self.filter_name,
            "catalog_name": self.catalog_name,
            "source_id": self.source_id,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "n_measurements": self.n_measurements,
            "mag_mean": self.mag_mean,
            "mag_median": self.mag_median,
            "mag_rms": self.mag_rms,
            "mag_mad": self.mag_mad,
            "mag_err_median": self.mag_err_median,
            "mag_p10": self.mag_p10,
            "mag_p90": self.mag_p90,
            "robust_peak_to_peak": self.robust_peak_to_peak,
            "flux_median": self.flux_median,
            "ref_mag": self.ref_mag,
            "color": self.color,
            "mjd_min": self.mjd_min,
            "mjd_max": self.mjd_max,
        }


def load_object_scatter_rows(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    mag_key: str = "G",
    color_key: str = "BP_RP",
    y_key: str = "mag_cal",
) -> list[ObjectScatterRow]:
    """Return per-object scatter statistics for one calibration version.

    This intentionally works on measurements already stored in PostgreSQL rather than
    recomputing photometry. It is a quality-control layer used for RMS--magnitude
    diagrams and variable-candidate triage.
    """

    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")

    stmt = (
        select(Measurement, Object, Frame, InstrumentUnit)
        .join(Object, Object.object_id == Measurement.object_id)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(Measurement.calibration_version_id == cv.calibration_version_id)
        .order_by(Object.object_id, Frame.mjd, Frame.date_obs)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    grouped: dict[int, list[tuple[Measurement, Object, Frame, InstrumentUnit | None]]] = defaultdict(list)
    for meas, obj, frame, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        grouped[obj.object_id].append((meas, obj, frame, iu))

    rows: list[ObjectScatterRow] = []
    for object_id, values in grouped.items():
        if len(values) < int(min_measurements):
            continue
        meas0, obj, _frame0, iu0 = values[0]
        flags0 = meas0.flags or {}

        mags: list[float] = []
        mag_errs: list[float] = []
        fluxes: list[float] = []
        mjds: list[float] = []
        for meas, _obj, frame, _iu in values:
            y_value = getattr(meas, y_key, None)
            if y_value is not None and _is_finite(y_value):
                mags.append(float(y_value))
            if meas.mag_err is not None and _is_finite(meas.mag_err):
                mag_errs.append(float(meas.mag_err))
            if meas.flux is not None and _is_finite(meas.flux):
                fluxes.append(float(meas.flux))
            if frame.mjd is not None and _is_finite(frame.mjd):
                mjds.append(float(frame.mjd))

        if len(mags) < int(min_measurements):
            continue

        ref_mags = obj.ref_mags or {}
        colors = obj.colors or {}
        rows.append(
            ObjectScatterRow(
                object_id=object_id,
                calibration_name=cv.name,
                instrument_unit_id=getattr(iu0, "instrument_unit_id", None),
                instrument=getattr(iu0, "instrument", None),
                filter_name=getattr(iu0, "filter_name", None),
                catalog_name=flags0.get("catalog_name"),
                source_id=flags0.get("source_id"),
                ra_deg=obj.ra_deg,
                dec_deg=obj.dec_deg,
                n_measurements=len(mags),
                mag_mean=_mean(mags),
                mag_median=_median(mags),
                mag_rms=_sample_rms(mags),
                mag_mad=_mad(mags),
                mag_err_median=_median(mag_errs),
                mag_p10=_percentile(mags, 10.0),
                mag_p90=_percentile(mags, 90.0),
                robust_peak_to_peak=_robust_peak_to_peak(mags),
                flux_median=_median(fluxes),
                ref_mag=_to_float(ref_mags.get(mag_key)),
                color=_to_float(colors.get(color_key)),
                mjd_min=min(mjds) if mjds else None,
                mjd_max=max(mjds) if mjds else None,
            )
        )
    rows.sort(key=lambda r: (float("inf") if r.mag_median is None else r.mag_median, r.object_id))
    return rows


def build_rms_mag_summary(
    object_rows: Iterable[ObjectScatterRow],
    *,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
) -> list[dict]:
    bins: dict[int, list[ObjectScatterRow]] = defaultdict(list)
    for row in object_rows:
        if row.mag_median is None or row.mag_rms is None:
            continue
        if not _is_finite(row.mag_median) or not _is_finite(row.mag_rms):
            continue
        idx = math.floor(float(row.mag_median) / float(bin_width))
        bins[idx].append(row)

    out: list[dict] = []
    for idx in sorted(bins):
        rows = bins[idx]
        if len(rows) < int(min_objects_per_bin):
            continue
        rms_values = [float(r.mag_rms) for r in rows if r.mag_rms is not None and _is_finite(r.mag_rms)]
        mad_values = [float(r.mag_mad) for r in rows if r.mag_mad is not None and _is_finite(r.mag_mad)]
        err_values = [float(r.mag_err_median) for r in rows if r.mag_err_median is not None and _is_finite(r.mag_err_median)]
        mag_values = [float(r.mag_median) for r in rows if r.mag_median is not None and _is_finite(r.mag_median)]
        if not rms_values:
            continue
        left = idx * float(bin_width)
        right = left + float(bin_width)
        out.append(
            {
                "mag_bin_left": left,
                "mag_bin_right": right,
                "mag_bin_center": (left + right) / 2.0,
                "n_objects": len(rows),
                "mag_median": _median(mag_values),
                "rms_median": _median(rms_values),
                "rms_p10": _percentile(rms_values, 10.0),
                "rms_p90": _percentile(rms_values, 90.0),
                "mad_median": _median(mad_values),
                "mag_err_median": _median(err_values),
            }
        )
    return out


def build_variable_candidates(
    object_rows: Iterable[ObjectScatterRow],
    *,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    min_measurements: int = 5,
    min_rms: float | None = None,
    min_excess: float = 2.0,
    limit: int | None = 50,
) -> list[dict]:
    rows = [r for r in object_rows if r.n_measurements >= int(min_measurements)]
    summary = build_rms_mag_summary(rows, bin_width=bin_width, min_objects_per_bin=min_objects_per_bin)
    med_by_bin = {math.floor(float(s["mag_bin_center"]) / float(bin_width)): s.get("rms_median") for s in summary}

    out: list[dict] = []
    for row in rows:
        if row.mag_median is None or row.mag_rms is None:
            continue
        if not _is_finite(row.mag_median) or not _is_finite(row.mag_rms):
            continue
        if min_rms is not None and float(row.mag_rms) < float(min_rms):
            continue
        idx = math.floor(float(row.mag_median) / float(bin_width))
        baseline = med_by_bin.get(idx)
        if baseline is None or not _is_finite(baseline) or float(baseline) <= 0:
            continue
        excess = float(row.mag_rms) / float(baseline)
        if excess < float(min_excess):
            continue
        d = row.to_dict()
        d["baseline_rms"] = float(baseline)
        d["rms_excess"] = excess
        out.append(d)
    out.sort(key=lambda d: (-(d.get("rms_excess") or 0.0), -(d.get("mag_rms") or 0.0)))
    if limit is not None:
        out = out[: int(limit)]
    return out


def compare_calibration_scatter(
    session: Session,
    *,
    left_calibration_name: str,
    right_calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    limit: int | None = 50,
) -> tuple[list[dict], dict]:
    left = load_object_scatter_rows(
        session,
        calibration_name=left_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        min_measurements=min_measurements,
    )
    right = load_object_scatter_rows(
        session,
        calibration_name=right_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        min_measurements=min_measurements,
    )

    def key(row: ObjectScatterRow) -> tuple[str | None, str | None, int]:
        return (row.catalog_name, row.source_id, row.object_id)

    left_map = {key(row): row for row in left}
    right_map = {key(row): row for row in right}
    common_keys = sorted(set(left_map) & set(right_map), key=lambda k: (k[0] or "", k[1] or "", k[2]))

    rows: list[dict] = []
    ratios: list[float] = []
    left_rms_values: list[float] = []
    right_rms_values: list[float] = []
    for k in common_keys:
        l = left_map[k]
        r = right_map[k]
        if l.mag_rms is None or r.mag_rms is None:
            continue
        if not _is_finite(l.mag_rms) or not _is_finite(r.mag_rms):
            continue
        ratio = float(r.mag_rms) / float(l.mag_rms) if float(l.mag_rms) > 0 else None
        delta = float(r.mag_rms) - float(l.mag_rms)
        if ratio is not None and _is_finite(ratio):
            ratios.append(ratio)
        left_rms_values.append(float(l.mag_rms))
        right_rms_values.append(float(r.mag_rms))
        rows.append(
            {
                "object_id": r.object_id,
                "catalog_name": r.catalog_name,
                "source_id": r.source_id,
                "mag_median": r.mag_median,
                "n_left": l.n_measurements,
                "n_right": r.n_measurements,
                "left_rms": l.mag_rms,
                "right_rms": r.mag_rms,
                "rms_ratio": ratio,
                "rms_delta": delta,
                "left_mad": l.mag_mad,
                "right_mad": r.mag_mad,
            }
        )

    rows.sort(key=lambda d: (d.get("rms_ratio") if d.get("rms_ratio") is not None else float("inf")))
    limited_rows = rows[: int(limit)] if limit is not None else rows
    summary = {
        "left_calibration_name": left_calibration_name,
        "right_calibration_name": right_calibration_name,
        "n_left_objects": len(left),
        "n_right_objects": len(right),
        "n_common_objects": len(rows),
        "left_rms_median": _median(left_rms_values),
        "right_rms_median": _median(right_rms_values),
        "rms_ratio_median": _median(ratios),
        "improved_fraction": (sum(1 for x in ratios if x < 1.0) / len(ratios)) if ratios else None,
    }
    return limited_rows, summary


def export_object_scatter_csv(rows: Iterable[ObjectScatterRow], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "object_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "n_measurements",
        "mag_mean",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mag_err_median",
        "mag_p10",
        "mag_p90",
        "robust_peak_to_peak",
        "flux_median",
        "ref_mag",
        "color",
        "mjd_min",
        "mjd_max",
    ]
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_dict())
            n += 1
    return n


def _to_float(value) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


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


def _robust_peak_to_peak(values: list[float]) -> float | None:
    p10 = _percentile(values, 10.0)
    p90 = _percentile(values, 90.0)
    if p10 is None or p90 is None:
        return None
    return float(p90 - p10)


def _percentile(values: list[float], q: float) -> float | None:
    clean = sorted(float(v) for v in values if _is_finite(v))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    pos = (len(clean) - 1) * float(q) / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return clean[int(pos)]
    frac = pos - lo
    return clean[lo] * (1.0 - frac) + clean[hi] * frac
