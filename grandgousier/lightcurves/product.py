from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationVersion,
    Field,
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    Measurement,
    InterseriesCalibrationGeneration,
    InterseriesCalibrationSeries,
    ObjectQc,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view
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
    "mag_err_random",
    "mag_err_systematic",
    "mag_err_detector",
    "mag_err_epoch_floor",
    "mag_err_effective_detector_only",
    "mag_err_effective_total",
    "mag_err_effective",
    "residual_noise_floor_applied",
    "mag_global",
    "mag_global_err",
    "mag_global_err_random",
    "mag_global_err_systematic",
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
    "mag_err_systematic_median",
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
        cv = session.get(CalibrationVersion, int(cv_id))
        if cv is not None:
            source_chain_names.extend(_calibration_chain_names(session, cv))
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
    offset_algorithm: str = "interseries_graph_median_v1",
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
    offset_algorithm: str = "interseries_graph_median_v1",
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
    offset_algorithm: str = "interseries_graph_median_v1",
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
    """Load selected calibrations without PostgreSQL array-bind ambiguity.

    Psycopg array adaptation for raw ``ANY(:names)`` binds is driver/type
    dependent (notably varchar/text arrays).  Use SQLAlchemy expanding ``IN``
    expressions instead so the ordinary light-curve path behaves identically
    on PostgreSQL and the SQLite test backend.
    """

    stmt = select(
        CalibrationVersion.calibration_version_id.label("calibration_version_id"),
        CalibrationVersion.name.label("name"),
        CalibrationVersion.model_name.label("model_name"),
        CalibrationVersion.instrument_unit_id.label("instrument_unit_id"),
    )
    if calibration_names:
        stmt = stmt.where(CalibrationVersion.name.in_([str(v) for v in calibration_names]))
    if calibration_prefixes:
        stmt = stmt.where(
            or_(
                *[CalibrationVersion.name.like(f"{str(prefix)}%") for prefix in calibration_prefixes]
            )
        )
    if model_name:
        stmt = stmt.where(CalibrationVersion.model_name == str(model_name))
    stmt = stmt.order_by(CalibrationVersion.created_at, CalibrationVersion.calibration_version_id)
    return [dict(row) for row in session.execute(stmt).mappings().all()]

def _load_raw_light_curve_rows(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
) -> list[dict[str, Any]]:
    # 0.7 has one BASE Measurement layer; every derived/science representation
    # is consumed through the storage-independent calibration view.
    return _load_raw_light_curve_rows_virtual(
        session,
        scope=scope,
        source_id=source_id,
        object_id=object_id,
        object_qc_algorithm=object_qc_algorithm,
        offset_algorithm=offset_algorithm,
    )


def _load_raw_light_curve_rows_virtual(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
) -> list[dict[str, Any]]:
    """Build the public light-curve row shape from normalized calibration state."""

    cv_ids = sorted({int(value) for value in scope.calibration_version_ids})
    cvs = {
        int(cv.calibration_version_id): cv
        for cv in session.scalars(
            select(CalibrationVersion).where(CalibrationVersion.calibration_version_id.in_(cv_ids))
        ).all()
    }
    object_filter = [int(object_id)] if object_id is not None else None
    virtual_rows: list[tuple[CalibrationVersion, Any]] = []
    for cv_id in cv_ids:
        cv = cvs.get(cv_id)
        if cv is None:
            continue
        view = load_calibration_photometry_view(
            session,
            calibration_version_id=cv_id,
            frame_ids=scope.selected_frame_ids or None,
            object_ids=object_filter,
            prefer_science=True,
            prefer_cache=True,
        )
        for row in view.rows:
            flags = row.flags if isinstance(row.flags, dict) else {}
            if scope.catalog_name and flags.get("catalog_name") != scope.catalog_name:
                continue
            if source_id is not None and str(flags.get("source_id") or "") != str(source_id):
                continue
            if object_id is not None and int(row.object_id) != int(object_id):
                continue
            virtual_rows.append((cv, row))
    if not virtual_rows:
        return []

    frame_ids = sorted({int(row.frame_id) for _cv, row in virtual_rows})
    frames = {
        int(frame.frame_id): frame
        for frame in session.scalars(select(Frame).where(Frame.frame_id.in_(frame_ids))).all()
    }
    if scope.instrument_unit_id is not None:
        virtual_rows = [
            (cv, row)
            for cv, row in virtual_rows
            if (frames.get(int(row.frame_id)) is not None)
            and frames[int(row.frame_id)].instrument_unit_id == int(scope.instrument_unit_id)
        ]
    if not virtual_rows:
        return []

    # A frame may belong to several reproducible grouping definitions in 0.7.
    # CalibrationVersion.series_id is the authoritative series scope for a
    # derived product; never infer it from a frame->series dictionary.
    scoped_series_ids = sorted({
        int(cv.series_id)
        for cv, _row in virtual_rows
        if cv.series_id is not None
    })
    series_info: dict[int, tuple[str | None, int]] = {}
    if scoped_series_ids:
        for series_id_value, field_name, field_id_value in session.execute(
            select(FieldSeries.series_id, Field.field_name, Field.field_id)
            .join(Field, Field.field_id == FieldSeries.field_id)
            .where(FieldSeries.series_id.in_(scoped_series_ids))
        ).all():
            series_info[int(series_id_value)] = (field_name, int(field_id_value))

    object_ids = sorted({int(row.object_id) for _cv, row in virtual_rows})
    qcs = session.scalars(
        select(ObjectQc).where(
            ObjectQc.object_id.in_(object_ids),
            ObjectQc.calibration_version_id.in_(cv_ids),
            ObjectQc.algorithm == str(object_qc_algorithm),
        )
    ).all()
    qc_by_key: dict[tuple[int, int, str | None, int | None], ObjectQc] = {}
    for qc in qcs:
        key = (
            int(qc.object_id),
            int(qc.calibration_version_id) if qc.calibration_version_id is not None else -1,
            qc.catalog_name,
            int(qc.instrument_unit_id) if qc.instrument_unit_id is not None else None,
        )
        qc_by_key[key] = qc

    offset_sql = """
        SELECT DISTINCT ON (ics.science_calibration_version_id, ics.series_id)
            ics.science_calibration_version_id AS calibration_version_id,
            ics.series_id,
            ics.offset_mag,
            ics.offset_err_mag AS offset_err,
            g.reliability_status AS status,
            g.analysis_algorithm AS algorithm
        FROM interseries_calibration_series ics
        JOIN interseries_calibration_generations g
          ON g.interseries_generation_id = ics.interseries_generation_id
        WHERE ics.science_calibration_version_id IN :cv_ids
          AND g.lifecycle_status = 'COMPLETE_IMMUTABLE'
    """
    offset_params: dict[str, Any] = {"cv_ids": cv_ids}
    if offset_algorithm:
        offset_sql += " AND g.analysis_algorithm = :algorithm"
        offset_params["algorithm"] = str(offset_algorithm)
    offset_sql += """
        ORDER BY ics.science_calibration_version_id, ics.series_id,
                 g.created_at DESC, g.interseries_generation_id DESC
    """
    offset_stmt = text(offset_sql).bindparams(bindparam("cv_ids", expanding=True))
    offset_rows = session.execute(offset_stmt, offset_params).mappings().all()
    offsets_by_key = {
        (int(value["calibration_version_id"]), int(value["series_id"])): value
        for value in offset_rows
    }

    result: list[dict[str, Any]] = []
    for cv, measurement in virtual_rows:
        frame = frames.get(int(measurement.frame_id))
        if frame is None:
            continue
        flags = dict(measurement.flags or {})
        series_id_value = (
            int(cv.series_id)
            if cv.series_id is not None
            else (int(scope.series_id) if scope.series_id is not None else None)
        )
        field_name = series_info.get(series_id_value, (None, -1))[0] if series_id_value is not None else None
        qc = qc_by_key.get(
            (
                int(measurement.object_id),
                int(cv.calibration_version_id),
                flags.get("catalog_name"),
                int(frame.instrument_unit_id) if frame.instrument_unit_id is not None else None,
            )
        )
        offset = (
            offsets_by_key.get((int(cv.calibration_version_id), int(series_id_value)))
            if series_id_value is not None
            else None
        )
        result.append(
            {
                "measurement_id": measurement.measurement_id or measurement.base_measurement_id,
                "object_id": int(measurement.object_id),
                "frame_id": int(measurement.frame_id),
                "series_id": series_id_value,
                "field_name": field_name,
                "calibration_name": str(cv.name),
                "calibration_version_id": int(cv.calibration_version_id),
                "model_name": cv.model_name,
                "date_obs": frame.date_obs,
                "mjd": frame.mjd,
                "jd": frame.jd,
                "instrument_unit_id": frame.instrument_unit_id,
                "instrument": frame.instrument,
                "site": frame.site,
                "tube": frame.tube,
                "camera": frame.camera,
                "filter_name": frame.filter_name,
                "x": measurement.x,
                "y": measurement.y,
                "flux": measurement.flux,
                "flux_err": measurement.flux_err,
                "mag_inst": measurement.mag_inst,
                "mag_cal": measurement.mag_cal,
                "mag_err": measurement.mag_err,
                "mag_err_random": measurement.mag_err_random,
                "mag_err_systematic": measurement.mag_err_systematic,
                "aperture_radius_pix": measurement.aperture_radius_pix,
                "local_background": measurement.local_background,
                "local_zp_correction": measurement.local_zp_correction,
                "centroid_dx_pix": measurement.centroid_dx_pix,
                "centroid_dy_pix": measurement.centroid_dy_pix,
                "measurement_flags": flags,
                "frame_quality_flags": dict(frame.quality_flags or {}),
                "object_qc_flags": dict(qc.flags or {}) if qc is not None else {},
                "interseries_offset_mag": offset["offset_mag"] if offset is not None else None,
                "interseries_offset_err": offset["offset_err"] if offset is not None else None,
                "interseries_offset_status": offset["status"] if offset is not None else None,
                "interseries_algorithm": offset["algorithm"] if offset is not None else None,
            }
        )
    result.sort(
        key=lambda row: (
            int(row["object_id"]),
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
            int(row.get("measurement_id") or 0),
        )
    )
    return result

def _science_row_from_db(row: dict[str, Any], *, reject_flags: set[str]) -> dict[str, Any]:
    measurement_flags = _dict_or_empty(row.get("measurement_flags"))
    object_flags = _dict_or_empty(row.get("object_qc_flags"))
    frame_flags = _dict_or_empty(row.get("frame_quality_flags"))
    quality_flags = merge_quality_flags(
        measurement_flags=measurement_flags,
        object_qc_flags=object_flags,
        frame_quality_flags=frame_flags,
    )
    random_err = row.get("mag_err_random") if row.get("mag_err_random") is not None else row.get("mag_err")
    systematic_err = row.get("mag_err_systematic")
    detector_err = measurement_flags.get("mag_err_detector")
    if not _is_finite(detector_err):
        detector_err = random_err
    epoch_floor_err = measurement_flags.get("mag_err_epoch_floor")
    effective_detector_only_err = measurement_flags.get("mag_err_effective_detector_only")
    if (
        not _is_finite(effective_detector_only_err)
        and _is_finite(detector_err)
        and _is_finite(epoch_floor_err)
    ):
        effective_detector_only_err = math.hypot(
            float(detector_err), float(epoch_floor_err)
        )
    effective_total_err = measurement_flags.get("mag_err_effective_total")
    if (
        not _is_finite(effective_total_err)
        and _is_finite(random_err)
        and _is_finite(epoch_floor_err)
    ):
        effective_total_err = math.hypot(float(random_err), float(epoch_floor_err))
    effective_err = effective_total_err
    if not _is_finite(effective_err):
        effective_err = measurement_flags.get("mag_err_effective")
    is_rejected, reject_reasons = evaluate_rejection(
        quality_flags=quality_flags,
        mag=row.get("mag_cal"),
        mag_err=random_err,
        reject_flags=reject_flags,
    )
    mag_global = row.get("mag_cal")
    offset = row.get("interseries_offset_mag")
    if _is_finite(mag_global) and _is_finite(offset):
        mag_global = float(mag_global) + float(offset)
    mag_global_err_random = random_err
    mag_global_err_systematic = combine_mag_error(systematic_err, row.get("interseries_offset_err"))
    mag_global_err = combine_mag_error(mag_global_err_random, mag_global_err_systematic)
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
        "mag_err": random_err,
        "mag_err_random": random_err,
        "mag_err_systematic": systematic_err,
        "mag_err_detector": detector_err,
        "mag_err_epoch_floor": epoch_floor_err,
        "mag_err_effective_detector_only": effective_detector_only_err,
        "mag_err_effective_total": effective_total_err,
        "mag_err_effective": effective_err,
        "residual_noise_floor_applied": bool(measurement_flags.get("residual_noise_floor_applied", False)),
        "mag_global": mag_global,
        "mag_global_err": mag_global_err,
        "mag_global_err_random": mag_global_err_random,
        "mag_global_err_systematic": mag_global_err_systematic,
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
