from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from grandgousier.lightcurves.product import (
    export_science_light_curve_csv,
    load_science_light_curve_rows,
)


@dataclass
class TargetResolution:
    resolved: bool
    object_id: int | None = None
    source_id: str | None = None
    catalog_source_id: int | None = None
    catalog_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None
    sep_arcsec: float | None = None
    n_measurements: int = 0
    method: str = "unresolved"
    reason: str | None = None
    nearest_object_id: int | None = None
    nearest_source_id: str | None = None
    nearest_catalog_source_id: int | None = None
    nearest_ra_deg: float | None = None
    nearest_dec_deg: float | None = None
    nearest_sep_arcsec: float | None = None
    nearest_n_measurements: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "object_id": self.object_id,
            "source_id": self.source_id,
            "catalog_source_id": self.catalog_source_id,
            "catalog_name": self.catalog_name,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "target_ra_deg": self.target_ra_deg,
            "target_dec_deg": self.target_dec_deg,
            "sep_arcsec": self.sep_arcsec,
            "n_measurements": self.n_measurements,
            "method": self.method,
            "reason": self.reason,
            "nearest_object_id": self.nearest_object_id,
            "nearest_source_id": self.nearest_source_id,
            "nearest_catalog_source_id": self.nearest_catalog_source_id,
            "nearest_ra_deg": self.nearest_ra_deg,
            "nearest_dec_deg": self.nearest_dec_deg,
            "nearest_sep_arcsec": self.nearest_sep_arcsec,
            "nearest_n_measurements": self.nearest_n_measurements,
        }


@dataclass
class TargetLightCurveProduct:
    target: TargetResolution
    rows_total: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    mag_median: float | None = None
    mag_mean: float | None = None
    mag_rms: float | None = None
    mag_mad: float | None = None
    mag_err_median: float | None = None
    mag_min: float | None = None
    mag_max: float | None = None
    peak_to_peak: float | None = None
    mjd_min: float | None = None
    mjd_max: float | None = None
    csv_path: str | None = None
    json_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "rows_total": self.rows_total,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "mag_median": self.mag_median,
            "mag_mean": self.mag_mean,
            "mag_rms": self.mag_rms,
            "mag_mad": self.mag_mad,
            "mag_err_median": self.mag_err_median,
            "mag_min": self.mag_min,
            "mag_max": self.mag_max,
            "peak_to_peak": self.peak_to_peak,
            "mjd_min": self.mjd_min,
            "mjd_max": self.mjd_max,
            "csv_path": self.csv_path,
            "json_path": self.json_path,
            "warnings": list(self.warnings),
        }


def resolve_target_object(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    frame_ids: list[int] | None = None,
    target_source_id: str | None = None,
    target_object_id: int | None = None,
    max_sep_arcsec: float | None = 120.0,
) -> TargetResolution:
    """Resolve the science target object for a field-series run.

    Explicit ``target_object_id`` wins.  Explicit ``target_source_id`` is matched
    through measurement provenance.  Without explicit IDs the function assumes a
    pointed-series target and selects the measured catalog object closest to the
    mean frame centre.  This keeps target light-curve export independent of the
    variable-candidate RMS threshold.
    """

    cv_id = session.execute(
        text("SELECT calibration_version_id FROM calibration_versions WHERE name = :name"),
        {"name": calibration_name},
    ).scalar()
    if cv_id is None:
        return TargetResolution(resolved=False, method="calibration_name", reason=f"calibration not found: {calibration_name}")

    if target_object_id is not None:
        row = _load_object_measurement_row(
            session,
            calibration_version_id=int(cv_id),
            object_id=int(target_object_id),
            catalog_name=catalog_name,
            frame_ids=frame_ids,
        )
        if row is None:
            return TargetResolution(
                resolved=False,
                object_id=int(target_object_id),
                catalog_name=catalog_name,
                method="target_object_id",
                reason="target object has no measurements in selected calibration/frame set",
            )
        return _resolution_from_row(row, method="target_object_id")

    if target_source_id:
        row = _load_source_measurement_row(
            session,
            calibration_version_id=int(cv_id),
            source_id=str(target_source_id),
            catalog_name=catalog_name,
            frame_ids=frame_ids,
        )
        if row is None:
            return TargetResolution(
                resolved=False,
                source_id=str(target_source_id),
                catalog_name=catalog_name,
                method="target_source_id",
                reason="target source_id has no measurements in selected calibration/frame set",
            )
        return _resolution_from_row(row, method="target_source_id")

    target_center = _mean_frame_center(session, frame_ids=frame_ids)
    if target_center is None:
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name,
            method="nearest_to_frame_center",
            reason="selected frames do not have usable RA/Dec centres",
        )
    target_ra, target_dec = target_center
    row = _load_nearest_measurement_row(
        session,
        calibration_version_id=int(cv_id),
        target_ra_deg=target_ra,
        target_dec_deg=target_dec,
        catalog_name=catalog_name,
        frame_ids=frame_ids,
    )
    if row is None:
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name,
            target_ra_deg=target_ra,
            target_dec_deg=target_dec,
            method="nearest_to_frame_center",
            reason="no measured catalog object is available for target resolution",
        )
    res = _resolution_from_row(row, method="nearest_to_frame_center")
    res.target_ra_deg = target_ra
    res.target_dec_deg = target_dec
    if max_sep_arcsec is not None and res.sep_arcsec is not None and float(res.sep_arcsec) > float(max_sep_arcsec):
        return TargetResolution(
            resolved=False,
            catalog_name=catalog_name or res.catalog_name,
            target_ra_deg=target_ra,
            target_dec_deg=target_dec,
            method="nearest_to_frame_center",
            reason=f"nearest object is farther than max_sep_arcsec={max_sep_arcsec:g}",
            nearest_object_id=res.object_id,
            nearest_source_id=res.source_id,
            nearest_catalog_source_id=res.catalog_source_id,
            nearest_ra_deg=res.ra_deg,
            nearest_dec_deg=res.dec_deg,
            nearest_sep_arcsec=res.sep_arcsec,
            nearest_n_measurements=res.n_measurements,
        )
    return res


def export_target_light_curve_product(
    session: Session,
    *,
    target: TargetResolution,
    calibration_name: str,
    catalog_name: str | None = None,
    series_id: int | None = None,
    frame_ids: list[int] | None = None,
    reject_flags: set[str] | None = None,
    out_csv: str | Path | None = None,
    out_json: str | Path | None = None,
) -> TargetLightCurveProduct:
    product = TargetLightCurveProduct(target=target)
    if not target.resolved or target.object_id is None:
        product.warnings.append(target.reason or "target object was not resolved")
        if out_json:
            product.json_path = str(out_json)
            _write_json(product.to_dict(), out_json)
        return product

    # ``process-archive-path`` passes both the series id and an explicit list of
    # frames that came from the requested filesystem path.  The light-curve scope
    # resolver intentionally rejects combining field-series selectors with
    # explicit frame_ids, because the combination is ambiguous for public CLI
    # calls.  For target export the explicit path frame selection is the stricter
    # and safer scope, so prefer it and do not also pass series_id downstream.
    scope_series_id = None if frame_ids else series_id
    _scope, rows = load_science_light_curve_rows(
        session,
        calibration_names=[calibration_name],
        catalog_name=catalog_name,
        object_id=int(target.object_id),
        series_id=scope_series_id,
        frame_ids=frame_ids,
        reject_flags=reject_flags,
        exclude_rejected=False,
    )
    product.rows_total = len(rows)
    product.rows_accepted = sum(1 for r in rows if not r.get("is_rejected"))
    product.rows_rejected = sum(1 for r in rows if r.get("is_rejected"))
    mags = [float(r["mag_global"]) for r in rows if _is_finite(r.get("mag_global"))]
    errs = [float(r["mag_global_err"]) for r in rows if _is_finite(r.get("mag_global_err"))]
    mjds = [float(r["mjd"]) for r in rows if _is_finite(r.get("mjd"))]
    product.mag_median = _median(mags)
    product.mag_mean = _mean(mags)
    product.mag_rms = _sample_rms(mags)
    product.mag_mad = _mad(mags)
    product.mag_err_median = _median(errs)
    product.mag_min = min(mags) if mags else None
    product.mag_max = max(mags) if mags else None
    product.peak_to_peak = (max(mags) - min(mags)) if mags else None
    product.mjd_min = min(mjds) if mjds else None
    product.mjd_max = max(mjds) if mjds else None

    if out_csv:
        export_science_light_curve_csv(rows, out_csv)
        product.csv_path = str(out_csv)
    if out_json:
        product.json_path = str(out_json)
        _write_json(product.to_dict(), out_json)
    return product


def _load_object_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    object_id: int,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id", "m.object_id = :object_id"]
    params: dict[str, Any] = {"cv_id": calibration_version_id, "object_id": object_id}
    return _load_measurement_identity_row(session, where=where, params=params, catalog_name=catalog_name, frame_ids=frame_ids)


def _load_source_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    source_id: str,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id", "m.flags ->> 'source_id' = :source_id"]
    params: dict[str, Any] = {"cv_id": calibration_version_id, "source_id": str(source_id)}
    return _load_measurement_identity_row(session, where=where, params=params, catalog_name=catalog_name, frame_ids=frame_ids)


def _load_nearest_measurement_row(
    session: Session,
    *,
    calibration_version_id: int,
    target_ra_deg: float,
    target_dec_deg: float,
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    where = ["m.calibration_version_id = :cv_id"]
    params: dict[str, Any] = {
        "cv_id": calibration_version_id,
        "target_ra": float(target_ra_deg),
        "target_dec": float(target_dec_deg),
    }
    if catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = catalog_name
    if frame_ids:
        where.append("m.frame_id IN :frame_ids")
        params["frame_ids"] = list(frame_ids)
    sql = f"""
        SELECT
            m.object_id,
            o.ra_deg,
            o.dec_deg,
            m.flags ->> 'catalog_name' AS catalog_name,
            m.flags ->> 'source_id' AS source_id,
            MIN(NULLIF(m.flags ->> 'catalog_source_id', '')::bigint) AS catalog_source_id,
            COUNT(DISTINCT m.frame_id) AS n_measurements,
            SQRT(
                POWER((o.ra_deg - :target_ra) * COS(RADIANS(:target_dec)), 2)
                + POWER(o.dec_deg - :target_dec, 2)
            ) * 3600.0 AS sep_arcsec
        FROM measurements m
        JOIN objects o ON o.object_id = m.object_id
        WHERE {' AND '.join(where)}
        GROUP BY m.object_id, o.ra_deg, o.dec_deg, m.flags ->> 'catalog_name', m.flags ->> 'source_id'
        ORDER BY sep_arcsec ASC, n_measurements DESC, m.object_id
        LIMIT 1
    """
    stmt = text(sql)
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    row = session.execute(stmt, params).mappings().first()
    return dict(row) if row is not None else None


def _load_measurement_identity_row(
    session: Session,
    *,
    where: list[str],
    params: dict[str, Any],
    catalog_name: str | None,
    frame_ids: list[int] | None,
) -> dict[str, Any] | None:
    if catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = catalog_name
    if frame_ids:
        where.append("m.frame_id IN :frame_ids")
        params["frame_ids"] = list(frame_ids)
    sql = f"""
        SELECT
            m.object_id,
            o.ra_deg,
            o.dec_deg,
            m.flags ->> 'catalog_name' AS catalog_name,
            m.flags ->> 'source_id' AS source_id,
            MIN(NULLIF(m.flags ->> 'catalog_source_id', '')::bigint) AS catalog_source_id,
            COUNT(DISTINCT m.frame_id) AS n_measurements,
            0.0::double precision AS sep_arcsec
        FROM measurements m
        JOIN objects o ON o.object_id = m.object_id
        WHERE {' AND '.join(where)}
        GROUP BY m.object_id, o.ra_deg, o.dec_deg, m.flags ->> 'catalog_name', m.flags ->> 'source_id'
        ORDER BY n_measurements DESC, m.object_id
        LIMIT 1
    """
    stmt = text(sql)
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    row = session.execute(stmt, params).mappings().first()
    return dict(row) if row is not None else None


def _mean_frame_center(session: Session, *, frame_ids: list[int] | None) -> tuple[float, float] | None:
    where = ["ra_center_deg IS NOT NULL", "dec_center_deg IS NOT NULL"]
    params: dict[str, Any] = {}
    if frame_ids:
        where.append("frame_id IN :frame_ids")
        params["frame_ids"] = list(frame_ids)
    stmt = text(
        f"""
        SELECT AVG(ra_center_deg)::double precision AS ra_deg,
               AVG(dec_center_deg)::double precision AS dec_deg
        FROM frames
        WHERE {' AND '.join(where)}
        """
    )
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    row = session.execute(stmt, params).mappings().first()
    if row is None or row["ra_deg"] is None or row["dec_deg"] is None:
        return None
    return float(row["ra_deg"]), float(row["dec_deg"])


def _resolution_from_row(row: dict[str, Any], *, method: str) -> TargetResolution:
    return TargetResolution(
        resolved=True,
        object_id=int(row["object_id"]) if row.get("object_id") is not None else None,
        source_id=str(row["source_id"]) if row.get("source_id") is not None else None,
        catalog_source_id=int(row["catalog_source_id"]) if row.get("catalog_source_id") is not None else None,
        catalog_name=str(row["catalog_name"]) if row.get("catalog_name") is not None else None,
        ra_deg=float(row["ra_deg"]) if row.get("ra_deg") is not None else None,
        dec_deg=float(row["dec_deg"]) if row.get("dec_deg") is not None else None,
        sep_arcsec=float(row["sep_arcsec"]) if row.get("sep_arcsec") is not None else None,
        n_measurements=int(row["n_measurements"] or 0),
        method=method,
    )


def _write_json(data: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _mean(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return sum(vals) / len(vals) if vals else None


def _median(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return median(vals) if vals else None


def _sample_rms(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def _mad(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = median(vals)
    return median(abs(v - med) for v in vals)
