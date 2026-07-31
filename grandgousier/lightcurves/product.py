from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.quality.field_report import _calibration_chain_names, _resolve_field_series_filter

DETECTION_SUPPORT_REJECT_FLAGS: tuple[str, ...] = (
    "NO_DETECTION_SUPPORT",
    "WEAK_DETECTION_SUPPORT",
)


DEFAULT_REJECT_FLAGS: tuple[str, ...] = (
    "HIGH_RMS",
    "FAINT_NOISY",
    "CROWDED",
    "BLENDED_NEIGHBOR",
    "DYNAMIC_CROWDED",
    "DYNAMIC_BLEND_RISK",
    "POSSIBLE_BLEND",
    "APERTURE_UNSTABLE",
    "APERTURE_MEDIAN_SHIFT",
    "EDGE",
    "NO_DETECTION_SUPPORT",
    "WEAK_DETECTION_SUPPORT",
    "LARGE_CATALOG_MAG_RESIDUAL",
    "POSSIBLE_PROPER_MOTION_MISMATCH",
    "SATURATED_LIKE",
)

SCIENCE_LIGHT_CURVE_COLUMNS: tuple[str, ...] = (
    "object_id",
    "catalog_name",
    "source_id",
    "frame_id",
    "series_id",
    "field_name",
    "measurement_id",
    "calibration_name",
    "calibration_version_id",
    "model_name",
    "date_obs",
    "mjd",
    "jd",
    "filter_name",
    "instrument_unit_id",
    "instrument",
    "site",
    "tube",
    "camera",
    "x",
    "y",
    "flux",
    "flux_err",
    "mag_inst",
    "mag_cal",
    "mag_err",
    "mag_global",
    "mag_global_err",
    "interseries_offset_mag",
    "interseries_offset_err",
    "interseries_offset_status",
    "interseries_algorithm",
    "aperture_radius_pix",
    "local_background",
    "local_zp_correction",
    "centroid_dx_pix",
    "centroid_dy_pix",
    "object_qc_flags",
    "measurement_flags",
    "frame_quality_flags",
    "quality_flags",
    "is_rejected",
    "reject_reasons",
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "section",
    "metric",
    "value",
)

OBJECT_COLUMNS: tuple[str, ...] = (
    "object_id",
    "catalog_name",
    "source_id",
    "n_points",
    "n_accepted",
    "n_rejected",
    "n_series",
    "n_calibrations",
    "mjd_min",
    "mjd_max",
    "mag_global_median",
    "mag_global_rms",
    "mag_global_mad",
    "mag_err_median",
    "filters",
)


@dataclass
class LightCurveScope:
    calibration_names: list[str] = field(default_factory=list)
    calibration_version_ids: list[int] = field(default_factory=list)
    calibration_prefixes: list[str] = field(default_factory=list)
    model_name: str | None = None
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    series_id: int | None = None
    field_name: str | None = None
    frame_selection_mode: str = "calibration_scope"
    selected_frame_ids: list[int] = field(default_factory=list)
    selected_frame_ids_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_names": self.calibration_names,
            "calibration_version_ids": self.calibration_version_ids,
            "calibration_prefixes": self.calibration_prefixes,
            "model_name": self.model_name,
            "catalog_name": self.catalog_name,
            "instrument_unit_id": self.instrument_unit_id,
            "series_id": self.series_id,
            "field_name": self.field_name,
            "frame_selection_mode": self.frame_selection_mode,
            "selected_frame_ids_count": self.selected_frame_ids_count,
        }


@dataclass
class LightCurveSummary:
    scope: LightCurveScope
    total_points: int
    accepted_points: int
    rejected_points: int
    n_objects: int
    n_series: int
    n_frames: int
    n_calibrations: int
    n_with_interseries_offset: int
    n_without_interseries_offset: int
    n_reject_reasons: dict[str, int]
    n_filters: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "total_points": self.total_points,
            "accepted_points": self.accepted_points,
            "rejected_points": self.rejected_points,
            "n_objects": self.n_objects,
            "n_series": self.n_series,
            "n_frames": self.n_frames,
            "n_calibrations": self.n_calibrations,
            "n_with_interseries_offset": self.n_with_interseries_offset,
            "n_without_interseries_offset": self.n_without_interseries_offset,
            "n_reject_reasons": self.n_reject_reasons,
            "n_filters": self.n_filters,
        }


def parse_reject_flags(value: str | None, *, use_defaults: bool = True) -> set[str]:
    flags = set(DEFAULT_REJECT_FLAGS) if use_defaults else set()
    if value:
        flags.update(part.strip().upper() for part in str(value).split(",") if part.strip())
    return flags


def light_curve_reject_flags(*, require_detection_support: bool = True, extra_flags: str | None = None) -> set[str]:
    """Return science-light-curve reject flags for the requested QC policy.

    Detection support is a powerful veto only when detection-to-catalog matching
    has actually been executed for the current catalog/frame set.  In
    forced-photometry-only runs the NO/WEAK detection flags remain diagnostic
    object_qc flags, but they must not reject every light-curve point by
    default.
    """

    flags = parse_reject_flags(extra_flags, use_defaults=True)
    if not require_detection_support:
        flags.difference_update(DETECTION_SUPPORT_REJECT_FLAGS)
    return flags


def science_light_curve_columns() -> list[str]:
    return list(SCIENCE_LIGHT_CURVE_COLUMNS)


def light_curve_summary_columns() -> list[str]:
    return list(SUMMARY_COLUMNS)


def light_curve_object_columns() -> list[str]:
    return list(OBJECT_COLUMNS)


def resolve_light_curve_scope(
    session: Session,
    *,
    calibration_names: list[str] | None = None,
    calibration_prefixes: list[str] | None = None,
    model_name: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_ids: list[int] | None = None,
) -> LightCurveScope:
    """Resolve user-facing light-curve selection into calibration/frame scope."""

    names = [v.strip() for v in (calibration_names or []) if str(v).strip()]
    prefixes = [v.strip() for v in (calibration_prefixes or []) if str(v).strip()]
    if not names and not prefixes and not model_name:
        raise ValueError("Provide at least one of --calibration-name, --calibration-prefix or --model-name")

    calibration_rows = _load_calibration_rows(
        session,
        calibration_names=names,
        calibration_prefixes=prefixes,
        model_name=model_name,
    )
    if not calibration_rows:
        raise ValueError("No calibration versions matched the light-curve selection")

    calibration_version_ids = [int(row["calibration_version_id"]) for row in calibration_rows]
    resolved_names = [str(row["name"]) for row in calibration_rows]
    source_chain_names: list[str] = []
    for cv_id in calibration_version_ids:
        try:
            source_chain_names.extend(_calibration_chain_names(session, cv_id))
        except Exception:
            pass
    all_names = _unique_strs([*resolved_names, *source_chain_names])

    selected_frame_ids: list[int] = []
    selection = _resolve_field_series_filter(
        session,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids or None,
        calibration_version_id=calibration_version_ids[0] if calibration_version_ids else None,
        calibration_names=all_names,
    )
    if selection is not None:
        selected_frame_ids = list(selection.get("frame_ids") or [])
        resolved_series_id = int(selection["series_id"])
        resolved_field_name = selection.get("field_name")
        frame_selection_mode = "field_series"
    else:
        selected_frame_ids = list(frame_ids or [])
        resolved_series_id = None
        resolved_field_name = None
        frame_selection_mode = "explicit_frame_ids" if selected_frame_ids else "calibration_scope"

    return LightCurveScope(
        calibration_names=resolved_names,
        calibration_version_ids=calibration_version_ids,
        calibration_prefixes=prefixes,
        model_name=model_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        series_id=resolved_series_id,
        field_name=resolved_field_name,
        frame_selection_mode=frame_selection_mode,
        selected_frame_ids=selected_frame_ids,
        selected_frame_ids_count=len(selected_frame_ids) if selected_frame_ids else None,
    )


def load_science_light_curve_rows(
    session: Session,
    *,
    calibration_names: list[str] | None = None,
    calibration_prefixes: list[str] | None = None,
    model_name: str | None = None,
    catalog_name: str | None = None,
    source_id: str | None = None,
    object_id: int | None = None,
    instrument_unit_id: int | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_ids: list[int] | None = None,
    object_qc_algorithm: str = "object_qc_v1",
    offset_algorithm: str = "interseries_median_v1",
    reject_flags: set[str] | None = None,
    exclude_rejected: bool = False,
    limit: int | None = None,
) -> tuple[LightCurveScope, list[dict[str, Any]]]:
    scope = resolve_light_curve_scope(
        session,
        calibration_names=calibration_names,
        calibration_prefixes=calibration_prefixes,
        model_name=model_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids,
    )
    rows = _load_raw_light_curve_rows(
        session,
        scope=scope,
        source_id=source_id,
        object_id=object_id,
        object_qc_algorithm=object_qc_algorithm,
        offset_algorithm=offset_algorithm,
    )
    processed: list[dict[str, Any]] = []
    effective_reject_flags = reject_flags if reject_flags is not None else set(DEFAULT_REJECT_FLAGS)
    for row in rows:
        d = _science_row_from_db(row, reject_flags=effective_reject_flags)
        if exclude_rejected and d["is_rejected"]:
            continue
        processed.append(d)
        if limit is not None and len(processed) >= int(limit):
            break
    return scope, processed


def export_science_light_curve_csv(rows: Iterable[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = science_light_curve_columns()
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row, columns))
            count += 1
    return count


def build_light_curve_summary(
    session: Session,
    *,
    calibration_names: list[str] | None = None,
    calibration_prefixes: list[str] | None = None,
    model_name: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_ids: list[int] | None = None,
    object_qc_algorithm: str = "object_qc_v1",
    offset_algorithm: str = "interseries_median_v1",
    reject_flags: set[str] | None = None,
) -> LightCurveSummary:
    scope, rows = load_science_light_curve_rows(
        session,
        calibration_names=calibration_names,
        calibration_prefixes=calibration_prefixes,
        model_name=model_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids,
        object_qc_algorithm=object_qc_algorithm,
        offset_algorithm=offset_algorithm,
        reject_flags=reject_flags,
        exclude_rejected=False,
        limit=None,
    )
    reject_counts: dict[str, int] = {}
    filter_counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reject_reasons_list") or []:
            reject_counts[reason] = reject_counts.get(reason, 0) + 1
        filter_name = row.get("filter_name") or "UNKNOWN"
        filter_counts[filter_name] = filter_counts.get(filter_name, 0) + 1
    return LightCurveSummary(
        scope=scope,
        total_points=len(rows),
        accepted_points=sum(1 for row in rows if not row.get("is_rejected")),
        rejected_points=sum(1 for row in rows if row.get("is_rejected")),
        n_objects=len({row.get("object_id") for row in rows}),
        n_series=len({row.get("series_id") for row in rows if row.get("series_id") is not None}),
        n_frames=len({row.get("frame_id") for row in rows}),
        n_calibrations=len({row.get("calibration_version_id") for row in rows}),
        n_with_interseries_offset=sum(1 for row in rows if row.get("interseries_offset_mag") is not None),
        n_without_interseries_offset=sum(1 for row in rows if row.get("interseries_offset_mag") is None),
        n_reject_reasons=dict(sorted(reject_counts.items())),
        n_filters=dict(sorted(filter_counts.items())),
    )


def light_curve_summary_rows(summary: LightCurveSummary) -> list[dict[str, Any]]:
    scope = summary.scope
    rows = [
        {"section": "scope", "metric": "frame_selection_mode", "value": scope.frame_selection_mode},
        {"section": "scope", "metric": "series_id", "value": scope.series_id},
        {"section": "scope", "metric": "field_name", "value": scope.field_name},
        {"section": "scope", "metric": "selected_frame_ids_count", "value": scope.selected_frame_ids_count},
        {"section": "scope", "metric": "n_calibration_versions", "value": len(scope.calibration_version_ids)},
        {"section": "points", "metric": "total", "value": summary.total_points},
        {"section": "points", "metric": "accepted", "value": summary.accepted_points},
        {"section": "points", "metric": "rejected", "value": summary.rejected_points},
        {"section": "coverage", "metric": "n_objects", "value": summary.n_objects},
        {"section": "coverage", "metric": "n_series", "value": summary.n_series},
        {"section": "coverage", "metric": "n_frames", "value": summary.n_frames},
        {"section": "coverage", "metric": "n_calibrations", "value": summary.n_calibrations},
        {"section": "interseries", "metric": "with_offset", "value": summary.n_with_interseries_offset},
        {"section": "interseries", "metric": "without_offset", "value": summary.n_without_interseries_offset},
    ]
    for flag, count in summary.n_reject_reasons.items():
        rows.append({"section": "reject_reasons", "metric": flag, "value": count})
    for filter_name, count in summary.n_filters.items():
        rows.append({"section": "filters", "metric": filter_name, "value": count})
    return rows


def export_light_curve_summary_json(summary: LightCurveSummary, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def list_light_curve_objects(
    session: Session,
    *,
    calibration_names: list[str] | None = None,
    calibration_prefixes: list[str] | None = None,
    model_name: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_ids: list[int] | None = None,
    object_qc_algorithm: str = "object_qc_v1",
    offset_algorithm: str = "interseries_median_v1",
    reject_flags: set[str] | None = None,
    min_points: int = 1,
    exclude_rejected: bool = False,
    limit: int | None = None,
) -> tuple[LightCurveScope, list[dict[str, Any]]]:
    scope, rows = load_science_light_curve_rows(
        session,
        calibration_names=calibration_names,
        calibration_prefixes=calibration_prefixes,
        model_name=model_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids,
        object_qc_algorithm=object_qc_algorithm,
        offset_algorithm=offset_algorithm,
        reject_flags=reject_flags,
        exclude_rejected=exclude_rejected,
        limit=None,
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["object_id"]), []).append(row)
    out: list[dict[str, Any]] = []
    for object_id, group in grouped.items():
        if len(group) < min_points:
            continue
        mags = [float(row["mag_global"]) for row in group if _is_finite(row.get("mag_global"))]
        errs = [float(row["mag_global_err"]) for row in group if _is_finite(row.get("mag_global_err"))]
        first = group[0]
        out.append(
            {
                "object_id": object_id,
                "catalog_name": first.get("catalog_name"),
                "source_id": first.get("source_id"),
                "n_points": len(group),
                "n_accepted": sum(1 for row in group if not row.get("is_rejected")),
                "n_rejected": sum(1 for row in group if row.get("is_rejected")),
                "n_series": len({row.get("series_id") for row in group if row.get("series_id") is not None}),
                "n_calibrations": len({row.get("calibration_version_id") for row in group}),
                "mjd_min": _min_or_none(row.get("mjd") for row in group),
                "mjd_max": _max_or_none(row.get("mjd") for row in group),
                "mag_global_median": _median(mags),
                "mag_global_rms": _sample_rms(mags),
                "mag_global_mad": _mad(mags),
                "mag_err_median": _median(errs),
                "filters": ",".join(sorted({str(row.get("filter_name")) for row in group if row.get("filter_name")})),
            }
        )
    out.sort(key=lambda row: (-int(row["n_points"]), str(row.get("source_id") or ""), int(row["object_id"])))
    if limit is not None:
        out = out[: int(limit)]
    return scope, out


def _load_calibration_rows(
    session: Session,
    *,
    calibration_names: list[str],
    calibration_prefixes: list[str],
    model_name: str | None,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {}
    if calibration_names:
        where.append("cv.name = ANY(:calibration_names)")
        params["calibration_names"] = list(calibration_names)
    if calibration_prefixes:
        prefix_terms = []
        for i, prefix in enumerate(calibration_prefixes):
            key = f"prefix_{i}"
            prefix_terms.append(f"cv.name LIKE :{key}")
            params[key] = f"{prefix}%"
        where.append("(" + " OR ".join(prefix_terms) + ")")
    if model_name:
        where.append("cv.model_name = :model_name")
        params["model_name"] = model_name
    sql = """
        SELECT cv.calibration_version_id, cv.name, cv.model_name, cv.instrument_unit_id
        FROM calibration_versions cv
    """
    if where:
        sql += " WHERE " + " AND ".join(f"({part})" for part in where)
    sql += " ORDER BY cv.created_at, cv.calibration_version_id"
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


def _load_raw_light_curve_rows(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
) -> list[dict[str, Any]]:
    where = ["m.calibration_version_id = ANY(:calibration_version_ids)"]
    params: dict[str, Any] = {
        "calibration_version_ids": list(scope.calibration_version_ids),
        "object_qc_algorithm": object_qc_algorithm,
        "offset_algorithm": offset_algorithm,
    }
    if scope.catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = scope.catalog_name
    if source_id is not None:
        where.append("m.flags ->> 'source_id' = :source_id")
        params["source_id"] = str(source_id)
    if object_id is not None:
        where.append("m.object_id = :object_id")
        params["object_id"] = int(object_id)
    if scope.instrument_unit_id is not None:
        where.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(scope.instrument_unit_id)
    if scope.selected_frame_ids:
        where.append("m.frame_id = ANY(:frame_ids)")
        params["frame_ids"] = list(scope.selected_frame_ids)

    sql = f"""
        SELECT
            m.measurement_id,
            m.object_id,
            m.frame_id,
            fsf.series_id,
            fld.field_name,
            cv.name AS calibration_name,
            cv.calibration_version_id,
            cv.model_name,
            f.date_obs,
            f.mjd,
            f.jd,
            f.instrument_unit_id,
            f.instrument,
            f.site,
            f.tube,
            f.camera,
            f.filter_name,
            m.x,
            m.y,
            m.flux,
            m.flux_err,
            m.mag_inst,
            m.mag_cal,
            m.mag_err,
            m.aperture_radius_pix,
            m.local_background,
            m.local_zp_correction,
            m.centroid_dx_pix,
            m.centroid_dy_pix,
            m.flags AS measurement_flags,
            f.quality_flags AS frame_quality_flags,
            oq.flags AS object_qc_flags,
            ico.offset_mag AS interseries_offset_mag,
            ico.offset_err AS interseries_offset_err,
            ico.status AS interseries_offset_status,
            ico.algorithm AS interseries_algorithm
        FROM measurements m
        JOIN frames f ON f.frame_id = m.frame_id
        JOIN calibration_versions cv ON cv.calibration_version_id = m.calibration_version_id
        LEFT JOIN field_series_frames fsf ON fsf.frame_id = f.frame_id
        LEFT JOIN field_series fs ON fs.series_id = fsf.series_id
        LEFT JOIN fields fld ON fld.field_id = fs.field_id
        LEFT JOIN object_qc oq
            ON oq.object_id = m.object_id
           AND oq.calibration_version_id = m.calibration_version_id
           AND oq.algorithm = :object_qc_algorithm
           AND (oq.catalog_name IS NOT DISTINCT FROM (m.flags ->> 'catalog_name'))
           AND (oq.instrument_unit_id IS NOT DISTINCT FROM f.instrument_unit_id)
        LEFT JOIN interseries_calibration_offsets ico
            ON ico.calibration_version_id = m.calibration_version_id
           AND ico.series_id IS NOT DISTINCT FROM fsf.series_id
           AND ico.algorithm = :offset_algorithm
           AND ico.status IN ('ACTIVE', 'OK')
        WHERE {' AND '.join(where)}
        ORDER BY m.object_id, f.mjd NULLS LAST, f.date_obs NULLS LAST, m.frame_id, m.measurement_id
    """
    try:
        return [dict(row) for row in session.execute(text(sql), params).mappings().all()]
    except Exception as exc:
        message = str(exc).lower()
        if "interseries_calibration_offsets" in message and ("does not exist" in message or "undefinedtable" in message):
            session.rollback()
            return _load_raw_light_curve_rows_without_offsets(
                session,
                scope=scope,
                source_id=source_id,
                object_id=object_id,
                object_qc_algorithm=object_qc_algorithm,
            )
        raise


def _load_raw_light_curve_rows_without_offsets(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
) -> list[dict[str, Any]]:
    where = ["m.calibration_version_id = ANY(:calibration_version_ids)"]
    params: dict[str, Any] = {
        "calibration_version_ids": list(scope.calibration_version_ids),
        "object_qc_algorithm": object_qc_algorithm,
    }
    if scope.catalog_name:
        where.append("m.flags ->> 'catalog_name' = :catalog_name")
        params["catalog_name"] = scope.catalog_name
    if source_id is not None:
        where.append("m.flags ->> 'source_id' = :source_id")
        params["source_id"] = str(source_id)
    if object_id is not None:
        where.append("m.object_id = :object_id")
        params["object_id"] = int(object_id)
    if scope.instrument_unit_id is not None:
        where.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(scope.instrument_unit_id)
    if scope.selected_frame_ids:
        where.append("m.frame_id = ANY(:frame_ids)")
        params["frame_ids"] = list(scope.selected_frame_ids)

    sql = f"""
        SELECT
            m.measurement_id,
            m.object_id,
            m.frame_id,
            fsf.series_id,
            fld.field_name,
            cv.name AS calibration_name,
            cv.calibration_version_id,
            cv.model_name,
            f.date_obs,
            f.mjd,
            f.jd,
            f.instrument_unit_id,
            f.instrument,
            f.site,
            f.tube,
            f.camera,
            f.filter_name,
            m.x,
            m.y,
            m.flux,
            m.flux_err,
            m.mag_inst,
            m.mag_cal,
            m.mag_err,
            m.aperture_radius_pix,
            m.local_background,
            m.local_zp_correction,
            m.centroid_dx_pix,
            m.centroid_dy_pix,
            m.flags AS measurement_flags,
            f.quality_flags AS frame_quality_flags,
            oq.flags AS object_qc_flags,
            NULL::double precision AS interseries_offset_mag,
            NULL::double precision AS interseries_offset_err,
            NULL::text AS interseries_offset_status,
            NULL::text AS interseries_algorithm
        FROM measurements m
        JOIN frames f ON f.frame_id = m.frame_id
        JOIN calibration_versions cv ON cv.calibration_version_id = m.calibration_version_id
        LEFT JOIN field_series_frames fsf ON fsf.frame_id = f.frame_id
        LEFT JOIN field_series fs ON fs.series_id = fsf.series_id
        LEFT JOIN fields fld ON fld.field_id = fs.field_id
        LEFT JOIN object_qc oq
            ON oq.object_id = m.object_id
           AND oq.calibration_version_id = m.calibration_version_id
           AND oq.algorithm = :object_qc_algorithm
           AND (oq.catalog_name IS NOT DISTINCT FROM (m.flags ->> 'catalog_name'))
           AND (oq.instrument_unit_id IS NOT DISTINCT FROM f.instrument_unit_id)
        WHERE {' AND '.join(where)}
        ORDER BY m.object_id, f.mjd NULLS LAST, f.date_obs NULLS LAST, m.frame_id, m.measurement_id
    """
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


def _science_row_from_db(row: dict[str, Any], *, reject_flags: set[str]) -> dict[str, Any]:
    measurement_flags = _dict_or_empty(row.get("measurement_flags"))
    object_flags = _dict_or_empty(row.get("object_qc_flags"))
    frame_flags = _dict_or_empty(row.get("frame_quality_flags"))
    quality_flags = merge_quality_flags(
        measurement_flags=measurement_flags,
        object_qc_flags=object_flags,
        frame_quality_flags=frame_flags,
    )
    is_rejected, reject_reasons = evaluate_rejection(
        quality_flags=quality_flags,
        mag=row.get("mag_cal"),
        mag_err=row.get("mag_err"),
        reject_flags=reject_flags,
    )
    mag_global = row.get("mag_cal")
    offset = row.get("interseries_offset_mag")
    if _is_finite(mag_global) and _is_finite(offset):
        mag_global = float(mag_global) + float(offset)
    mag_global_err = combine_mag_error(row.get("mag_err"), row.get("interseries_offset_err"))
    if row.get("interseries_offset_mag") is None:
        interseries_status = "NO_INTERSERIES_OFFSET"
    else:
        interseries_status = row.get("interseries_offset_status") or "OFFSET_APPLIED"

    out = {
        "object_id": row.get("object_id"),
        "catalog_name": measurement_flags.get("catalog_name"),
        "source_id": measurement_flags.get("source_id"),
        "frame_id": row.get("frame_id"),
        "series_id": row.get("series_id"),
        "field_name": row.get("field_name"),
        "measurement_id": row.get("measurement_id"),
        "calibration_name": row.get("calibration_name"),
        "calibration_version_id": row.get("calibration_version_id"),
        "model_name": row.get("model_name"),
        "date_obs": _isoformat(row.get("date_obs")),
        "mjd": row.get("mjd"),
        "jd": row.get("jd"),
        "filter_name": row.get("filter_name"),
        "instrument_unit_id": row.get("instrument_unit_id"),
        "instrument": row.get("instrument"),
        "site": row.get("site"),
        "tube": row.get("tube"),
        "camera": row.get("camera"),
        "x": row.get("x"),
        "y": row.get("y"),
        "flux": row.get("flux"),
        "flux_err": row.get("flux_err"),
        "mag_inst": row.get("mag_inst"),
        "mag_cal": row.get("mag_cal"),
        "mag_err": row.get("mag_err"),
        "mag_global": mag_global,
        "mag_global_err": mag_global_err,
        "interseries_offset_mag": row.get("interseries_offset_mag"),
        "interseries_offset_err": row.get("interseries_offset_err"),
        "interseries_offset_status": interseries_status,
        "interseries_algorithm": row.get("interseries_algorithm"),
        "aperture_radius_pix": row.get("aperture_radius_pix"),
        "local_background": row.get("local_background"),
        "local_zp_correction": row.get("local_zp_correction"),
        "centroid_dx_pix": row.get("centroid_dx_pix"),
        "centroid_dy_pix": row.get("centroid_dy_pix"),
        "object_qc_flags": object_flags,
        "measurement_flags": measurement_flags,
        "frame_quality_flags": frame_flags,
        "quality_flags": quality_flags,
        "is_rejected": bool(is_rejected),
        "reject_reasons": ",".join(reject_reasons),
        "reject_reasons_list": reject_reasons,
    }
    return out


def merge_quality_flags(
    *,
    measurement_flags: dict[str, Any] | None,
    object_qc_flags: dict[str, Any] | None,
    frame_quality_flags: dict[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for prefix, flags in (
        ("MEAS", measurement_flags),
        ("OBJECT", object_qc_flags),
        ("FRAME", frame_quality_flags),
    ):
        for key, value in _dict_or_empty(flags).items():
            if key in {"catalog_name", "source_id"}:
                continue
            normalized = str(key).upper()
            if isinstance(value, bool):
                flag_value = value
            else:
                flag_value = value
            if normalized in merged and merged[normalized] == flag_value:
                continue
            if normalized in merged:
                merged[f"{prefix}_{normalized}"] = flag_value
            else:
                merged[normalized] = flag_value
    return merged


def evaluate_rejection(
    *,
    quality_flags: dict[str, Any],
    mag: Any,
    mag_err: Any,
    reject_flags: set[str],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not _is_finite(mag):
        reasons.append("NO_VALID_MAG")
    if mag_err is not None and not _is_finite(mag_err):
        reasons.append("BAD_MAG_ERR")
    active_flags = active_quality_flag_names(quality_flags)
    for flag in sorted(active_flags):
        if flag.upper() in reject_flags:
            reasons.append(flag.upper())
    return bool(reasons), reasons


def active_quality_flag_names(flags: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key, value in _dict_or_empty(flags).items():
        if _flag_is_active(value):
            names.add(str(key).upper())
    return names


def combine_mag_error(mag_err: Any, offset_err: Any) -> float | None:
    if _is_finite(mag_err) and _is_finite(offset_err):
        return float(math.sqrt(float(mag_err) ** 2 + float(offset_err) ** 2))
    if _is_finite(mag_err):
        return float(mag_err)
    if _is_finite(offset_err):
        return float(offset_err)
    return None


def _flag_is_active(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        try:
            return bool(value) and math.isfinite(float(value))
        except Exception:
            return bool(value)
    text = str(value).strip().lower()
    return text not in {"", "0", "false", "none", "null", "ok", "good"}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _csv_safe_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in columns:
        value = row.get(key)
        if isinstance(value, (dict, list)):
            out[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            out[key] = value
    return out


def _unique_strs(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        text_value = str(value)
        if text_value not in seen:
            seen.add(text_value)
            out.append(text_value)
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(float(v) for v in values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def _mad(values: list[float]) -> float | None:
    med = _median(values)
    if med is None:
        return None
    return _median([abs(float(v) - med) for v in values])


def _sample_rms(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mu = sum(float(v) for v in values) / len(values)
    return float(math.sqrt(sum((float(v) - mu) ** 2 for v in values) / (len(values) - 1)))


def _min_or_none(values: Iterable[Any]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return min(vals) if vals else None


def _max_or_none(values: Iterable[Any]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return max(vals) if vals else None
