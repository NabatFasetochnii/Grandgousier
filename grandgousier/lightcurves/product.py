from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationVersion,
    CalibrationProductAlias,
    Field,
    FieldSeries,
    Frame,
    InterseriesCalibrationGeneration,
    InterseriesCalibrationSeries,
    ObjectQc,
    PipelineRun,
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
    "interseries_generation_id",
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
    "interseries_generation_ids",
    "interseries_status",
    "filters",
)


class LightCurveSelectionError(ValueError):
    """A fail-closed selection or M4 provenance error in a light-curve request."""


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
    current_science_calibration_version_ids: list[int] = field(default_factory=list)
    interseries_generation_ids: list[int] = field(default_factory=list)
    interseries_generation_scopes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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
            "current_science_calibration_version_ids": self.current_science_calibration_version_ids,
            "interseries_generation_ids": self.interseries_generation_ids,
            "interseries_generation_scopes": self.interseries_generation_scopes,
            "warnings": self.warnings,
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


@dataclass(frozen=True)
class LightCurveCountSummary:
    total_points: int
    accepted_points: int
    rejected_points: int
    n_objects: int


def parse_reject_flags(value: str | None, *, use_defaults: bool = True) -> set[str]:
    flags = set(DEFAULT_REJECT_FLAGS) if use_defaults else set()
    if value:
        flags.update(part.strip().upper() for part in str(value).split(",") if part.strip())
    return flags


def light_curve_reject_flags(*, require_detection_support: bool = True, extra_flags: str | None = None) -> set[str]:
    """Return science-light-curve reject flags for the requested QC policy.

    Detection-support flags remain diagnostic Object-QC evidence unless the
    caller explicitly enables their science-light-curve veto. Runner defaults
    keep them diagnostic even when detection matching and source selection run.
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

    current_science_ids = [
        int(row["calibration_version_id"])
        for row in calibration_rows
        if row.get("is_current_alias_head") and row.get("product_role") == "SCIENCE"
    ]
    science_heads_by_series: dict[int, list[int]] = {}
    for row in calibration_rows:
        if not row.get("is_current_alias_head") or row.get("product_role") != "SCIENCE":
            continue
        if row.get("series_id") is None:
            raise LightCurveSelectionError(
                "Current SCIENCE alias head "
                f"{row['calibration_version_id']} has no series_id; refusing ambiguous light-curve scope"
            )
        science_heads_by_series.setdefault(int(row["series_id"]), []).append(
            int(row["calibration_version_id"])
        )
    ambiguous_heads = {
        series_id: sorted(set(version_ids))
        for series_id, version_ids in science_heads_by_series.items()
        if len(set(version_ids)) > 1
    }
    if ambiguous_heads and not names:
        details = "; ".join(
            f"series {series_id}: {', '.join(str(value) for value in version_ids)}"
            for series_id, version_ids in sorted(ambiguous_heads.items())
        )
        raise LightCurveSelectionError(
            "Multiple current SCIENCE alias heads match one series; select one exact calibration "
            f"name instead of using a broad prefix/model selector ({details})"
        )

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
        current_science_calibration_version_ids=sorted(set(current_science_ids)),
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
    interseries_generation_ids: list[int] | None = None,
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
        interseries_generation_ids=interseries_generation_ids,
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
    interseries_generation_ids: list[int] | None = None,
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
        interseries_generation_ids=interseries_generation_ids,
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


def build_light_curve_count_summary(
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
    interseries_generation_ids: list[int] | None = None,
    reject_flags: set[str] | None = None,
) -> LightCurveCountSummary:
    """Count science points without building detailed light-curve row dictionaries.

    Calibration scope, normalized photometry loading, M4 generation selection,
    and the row inclusion checks are shared with the full summary path. Rejection
    uses the same merged flags and magnitude/error rules as
    :func:`_science_row_from_db`.
    """

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
    effective_reject_flags = (
        reject_flags if reject_flags is not None else set(DEFAULT_REJECT_FLAGS)
    )
    object_ids: set[int] = set()
    total_points = accepted_points = rejected_points = 0
    for row in _iter_raw_light_curve_rows_virtual(
        session,
        scope=scope,
        source_id=None,
        object_id=None,
        object_qc_algorithm=object_qc_algorithm,
        offset_algorithm=offset_algorithm,
        interseries_generation_ids=interseries_generation_ids,
        count_only=True,
    ):
        (
            _measurement_flags,
            _object_flags,
            _frame_flags,
            _quality_flags,
            _random_err,
            is_rejected,
            _reject_reasons,
        ) = _science_rejection_state(
            measurement_flags=row.get("measurement_flags"),
            object_qc_flags=row.get("object_qc_flags"),
            frame_quality_flags=row.get("frame_quality_flags"),
            mag_cal=row.get("mag_cal"),
            mag_err=row.get("mag_err"),
            mag_err_random=row.get("mag_err_random"),
            reject_flags=effective_reject_flags,
        )
        total_points += 1
        if is_rejected:
            rejected_points += 1
        else:
            accepted_points += 1
        object_ids.add(int(row["object_id"]))

    return LightCurveCountSummary(
        total_points=total_points,
        accepted_points=accepted_points,
        rejected_points=rejected_points,
        n_objects=len(object_ids),
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
        {
            "section": "interseries",
            "metric": "generation_ids",
            "value": ",".join(str(value) for value in scope.interseries_generation_ids),
        },
        {
            "section": "interseries",
            "metric": "warnings",
            "value": "; ".join(scope.warnings),
        },
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
    interseries_generation_ids: list[int] | None = None,
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
        interseries_generation_ids=interseries_generation_ids,
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
                "interseries_generation_ids": ",".join(
                    str(value)
                    for value in sorted(
                        {
                            int(row["interseries_generation_id"])
                            for row in group
                            if row.get("interseries_generation_id") is not None
                        }
                    )
                ),
                "interseries_status": ",".join(
                    sorted(
                        {
                            str(row["interseries_offset_status"])
                            for row in group
                            if row.get("interseries_offset_status")
                        }
                    )
                ),
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
    """Resolve exact logical names and generic selectors to calibration products.

    Exact logical names resolve through their alias head. If a name is not a
    logical alias, it remains an explicit physical-name read (including archived
    immutable generations). Prefix/model searches intentionally inspect alias
    heads only, so stale physical rows can never become a fallback candidate.
    Selector predicates retain their prior intersection behavior.
    """

    exact_ids: set[int] = set()
    logical_alias_names: set[str] = set()
    normalized_names = [str(value).strip() for value in calibration_names if str(value).strip()]
    if normalized_names:
        alias_rows = session.execute(
            select(
                CalibrationProductAlias.logical_name.label("logical_name"),
                CalibrationProductAlias.head_calibration_version_id.label("head_id"),
            ).where(CalibrationProductAlias.logical_name.in_(normalized_names))
        ).mappings().all()
        alias_by_name = {str(row["logical_name"]): row.get("head_id") for row in alias_rows}
        logical_alias_names = set(alias_by_name)
        empty_aliases = sorted(name for name, head_id in alias_by_name.items() if head_id is None)
        if empty_aliases:
            raise LightCurveSelectionError(
                "Logical calibration alias has no current head: " + ", ".join(empty_aliases)
            )
        exact_ids.update(int(alias_by_name[name]) for name in logical_alias_names)

        physical_names = [name for name in normalized_names if name not in logical_alias_names]
        if physical_names:
            physical_rows = session.execute(
                select(CalibrationVersion.calibration_version_id.label("calibration_version_id"))
                .where(CalibrationVersion.name.in_(physical_names))
            ).mappings().all()
            exact_ids.update(int(row["calibration_version_id"]) for row in physical_rows)

    generic_requested = bool(calibration_prefixes or model_name)
    generic_ids: set[int] = set()
    if generic_requested:
        generic_stmt = (
            select(
                CalibrationVersion.calibration_version_id.label("calibration_version_id")
            )
            .join(
                CalibrationProductAlias,
                CalibrationProductAlias.head_calibration_version_id
                == CalibrationVersion.calibration_version_id,
            )
        )
        if calibration_prefixes:
            generic_stmt = generic_stmt.where(
                or_(
                    *[
                        CalibrationVersion.name.like(f"{str(prefix)}%")
                        for prefix in calibration_prefixes
                    ]
                )
            )
        if model_name:
            generic_stmt = generic_stmt.where(CalibrationVersion.model_name == str(model_name))
        generic_rows = session.execute(generic_stmt).mappings().all()
        generic_ids = {int(row["calibration_version_id"]) for row in generic_rows}

    selected_ids = generic_ids if not normalized_names else exact_ids
    if normalized_names and generic_requested:
        selected_ids = exact_ids & generic_ids
    if not selected_ids:
        return []

    version_rows = session.execute(
        select(
            CalibrationVersion.calibration_version_id.label("calibration_version_id"),
            CalibrationVersion.name.label("name"),
            CalibrationVersion.model_name.label("model_name"),
            CalibrationVersion.instrument_unit_id.label("instrument_unit_id"),
            CalibrationVersion.field_id.label("field_id"),
            CalibrationVersion.series_id.label("series_id"),
            CalibrationVersion.product_role.label("product_role"),
            CalibrationVersion.created_at.label("created_at"),
        ).where(CalibrationVersion.calibration_version_id.in_(sorted(selected_ids)))
    ).mappings().all()
    alias_rows = session.execute(
        select(
            CalibrationProductAlias.head_calibration_version_id.label("head_id"),
            CalibrationProductAlias.logical_name.label("logical_name"),
        ).where(CalibrationProductAlias.head_calibration_version_id.in_(sorted(selected_ids)))
    ).mappings().all()
    alias_names_by_id: dict[int, list[str]] = {}
    for row in alias_rows:
        alias_names_by_id.setdefault(int(row["head_id"]), []).append(str(row["logical_name"]))

    rows_by_id = {
        int(row["calibration_version_id"]): dict(row)
        for row in version_rows
    }
    missing_ids = sorted(selected_ids - set(rows_by_id))
    if missing_ids:
        raise LightCurveSelectionError(
            "Calibration selector resolved missing version IDs: "
            + ", ".join(str(value) for value in missing_ids)
        )
    result = []
    for calibration_version_id, row in rows_by_id.items():
        row["is_current_alias_head"] = calibration_version_id in alias_names_by_id
        row["alias_logical_names"] = sorted(alias_names_by_id.get(calibration_version_id, []))
        result.append(row)
    result.sort(
        key=lambda row: (
            str(row.get("created_at") or ""),
            int(row["calibration_version_id"]),
        )
    )
    return result


def preflight_science_light_curve_selection(
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
    offset_algorithm: str = "interseries_graph_median_v1",
    interseries_generation_ids: list[int] | None = None,
) -> LightCurveScope:
    """Validate calibration/M4 selection before callers create output products."""

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
    contexts = _load_calibration_scope_contexts(
        session,
        calibration_version_ids=scope.calibration_version_ids,
        scope=scope,
        virtual_rows=[],
    )
    generation_records = _load_interseries_generation_records(
        session,
        scope_contexts=contexts,
        explicit_generation_ids=interseries_generation_ids,
    )
    offset_selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids=set(
            scope.current_science_calibration_version_ids
        ),
        generation_records=generation_records,
        offset_algorithm=offset_algorithm,
        explicit_generation_ids=interseries_generation_ids,
    )
    _apply_interseries_selection_to_scope(scope, offset_selection)
    return scope


def _load_raw_light_curve_rows(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
    interseries_generation_ids: list[int] | None = None,
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
        interseries_generation_ids=interseries_generation_ids,
    )


def _load_raw_light_curve_rows_virtual(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
    interseries_generation_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    rows = list(
        _iter_raw_light_curve_rows_virtual(
            session,
            scope=scope,
            source_id=source_id,
            object_id=object_id,
            object_qc_algorithm=object_qc_algorithm,
            offset_algorithm=offset_algorithm,
            interseries_generation_ids=interseries_generation_ids,
        )
    )
    rows.sort(
        key=lambda row: (
            int(row["object_id"]),
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
            int(row.get("measurement_id") or 0),
        )
    )
    return rows


def _iter_raw_light_curve_rows_virtual(
    session: Session,
    *,
    scope: LightCurveScope,
    source_id: str | None,
    object_id: int | None,
    object_qc_algorithm: str,
    offset_algorithm: str,
    interseries_generation_ids: list[int] | None = None,
    count_only: bool = False,
) -> Iterable[dict[str, Any]]:
    """Stream full rows or the minimal fields needed for point counts."""

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
    contexts = _load_calibration_scope_contexts(
        session,
        calibration_version_ids=cv_ids,
        scope=scope,
        virtual_rows=virtual_rows,
    )
    generation_records = _load_interseries_generation_records(
        session,
        scope_contexts=contexts,
        explicit_generation_ids=interseries_generation_ids,
    )
    offset_selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids=set(
            scope.current_science_calibration_version_ids
        ),
        generation_records=generation_records,
        offset_algorithm=offset_algorithm,
        explicit_generation_ids=interseries_generation_ids,
    )
    _apply_interseries_selection_to_scope(scope, offset_selection)
    if not virtual_rows:
        return

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
        return

    # A frame may belong to several reproducible grouping definitions in 0.7.
    # CalibrationVersion.series_id is the authoritative series scope for a
    # derived product; never infer it from a frame->series dictionary.
    scoped_series_ids = sorted({
        int(cv.series_id)
        for cv, _row in virtual_rows
        if cv.series_id is not None
    })
    series_info: dict[int, tuple[str | None, int]] = {}
    if scoped_series_ids and not count_only:
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

    for cv, measurement in virtual_rows:
        frame = frames.get(int(measurement.frame_id))
        if frame is None:
            continue
        flags = dict(measurement.flags or {})
        qc = qc_by_key.get(
            (
                int(measurement.object_id),
                int(cv.calibration_version_id),
                flags.get("catalog_name"),
                int(frame.instrument_unit_id) if frame.instrument_unit_id is not None else None,
            )
        )
        if count_only:
            yield {
                "object_id": int(measurement.object_id),
                "mag_cal": measurement.mag_cal,
                "mag_err": measurement.mag_err,
                "mag_err_random": measurement.mag_err_random,
                "measurement_flags": flags,
                "frame_quality_flags": dict(frame.quality_flags or {}),
                "object_qc_flags": dict(qc.flags or {}) if qc is not None else {},
            }
            continue

        series_id_value = (
            int(cv.series_id)
            if cv.series_id is not None
            else (int(scope.series_id) if scope.series_id is not None else None)
        )
        field_name = (
            series_info.get(series_id_value, (None, -1))[0]
            if series_id_value is not None
            else None
        )
        offset = (
            offset_selection["source_offsets"].get(
                (
                    int(offset_selection["generation_by_scope"][contexts[int(cv.calibration_version_id)]["scope_key"]]["interseries_generation_id"]),
                    int(series_id_value),
                    int(cv.calibration_version_id),
                )
            )
            if series_id_value is not None
            and int(cv.calibration_version_id) in contexts
            and contexts[int(cv.calibration_version_id)]["scope_key"]
            in offset_selection["generation_by_scope"]
            else None
        )
        offset_status = (
            offset["status"]
            if offset is not None
            else offset_selection["status_by_scope"].get(
                contexts.get(int(cv.calibration_version_id), {}).get("scope_key"),
                "NO_M4_SOURCE_MATCH",
            )
        )
        yield {
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
            "interseries_offset_status": offset_status,
            "interseries_algorithm": offset["algorithm"] if offset is not None else None,
            "interseries_generation_id": offset["interseries_generation_id"] if offset is not None else None,
        }


def _load_calibration_scope_contexts(
    session: Session,
    *,
    calibration_version_ids: list[int],
    scope: LightCurveScope,
    virtual_rows: list[tuple[CalibrationVersion, Any]],
) -> dict[int, dict[str, Any]]:
    if not calibration_version_ids:
        return {}
    stmt = (
        select(
            CalibrationVersion.calibration_version_id.label("calibration_version_id"),
            CalibrationVersion.product_role.label("product_role"),
            CalibrationVersion.field_id.label("calibration_field_id"),
            CalibrationVersion.series_id.label("calibration_series_id"),
            CalibrationVersion.instrument_unit_id.label("calibration_instrument_unit_id"),
            CalibrationVersion.used_catalogs.label("used_catalogs"),
            FieldSeries.field_id.label("series_field_id"),
            FieldSeries.instrument_unit_id.label("series_instrument_unit_id"),
            FieldSeries.filter_name.label("series_filter_name"),
            PipelineRun.catalog_name.label("pipeline_catalog_name"),
        )
        .outerjoin(FieldSeries, FieldSeries.series_id == CalibrationVersion.series_id)
        .outerjoin(PipelineRun, PipelineRun.pipeline_run_id == CalibrationVersion.pipeline_run_id)
        .where(CalibrationVersion.calibration_version_id.in_(calibration_version_ids))
    )
    rows = session.execute(stmt).mappings().all()
    catalog_names_by_cv: dict[int, set[str]] = {}
    for cv, measurement in virtual_rows:
        flags = measurement.flags if isinstance(measurement.flags, dict) else {}
        value = flags.get("catalog_name")
        if value not in (None, ""):
            catalog_names_by_cv.setdefault(int(cv.calibration_version_id), set()).add(str(value))

    current_head_ids = set(scope.current_science_calibration_version_ids)
    contexts: dict[int, dict[str, Any]] = {}
    returned_ids: set[int] = set()
    for row in rows:
        cv_id = int(row["calibration_version_id"])
        returned_ids.add(cv_id)
        cv_field = _int_or_none(row.get("calibration_field_id"))
        series_field = _int_or_none(row.get("series_field_id"))
        cv_series = _int_or_none(row.get("calibration_series_id"))
        series_id = cv_series if cv_series is not None else scope.series_id
        field_id = cv_field if cv_field is not None else series_field
        if (
            cv_id in current_head_ids
            and cv_field is not None
            and series_field is not None
            and cv_field != series_field
        ):
            raise LightCurveSelectionError(
                f"Current SCIENCE alias head {cv_id} has a field_id inconsistent with its series"
            )
        cv_unit = _int_or_none(row.get("calibration_instrument_unit_id"))
        series_unit = _int_or_none(row.get("series_instrument_unit_id"))
        if (
            cv_id in current_head_ids
            and cv_unit is not None
            and series_unit is not None
            and cv_unit != series_unit
        ):
            raise LightCurveSelectionError(
                f"Current SCIENCE alias head {cv_id} has an instrument scope inconsistent with its series"
            )
        instrument_unit_id = (
            cv_unit
            if cv_unit is not None
            else (series_unit if series_unit is not None else scope.instrument_unit_id)
        )
        actual_catalogs = catalog_names_by_cv.get(cv_id, set())
        if len(actual_catalogs) > 1:
            raise LightCurveSelectionError(
                f"Calibration version {cv_id} has multiple catalog namespaces in the selected rows"
            )
        used_catalogs = row.get("used_catalogs")
        used_catalog_name = (
            used_catalogs.get("catalog_name") if isinstance(used_catalogs, dict) else None
        )
        catalog_name = (
            scope.catalog_name
            or next(iter(actual_catalogs), None)
            or row.get("pipeline_catalog_name")
            or used_catalog_name
        )
        scope_key = (
            field_id,
            instrument_unit_id,
            _str_or_none(row.get("series_filter_name")),
            _str_or_none(catalog_name),
        )
        if cv_id in current_head_ids and (series_id is None or field_id is None):
            raise LightCurveSelectionError(
                f"Current SCIENCE alias head {cv_id} lacks field/series scope required for M4 matching"
            )
        contexts[cv_id] = {
            "calibration_version_id": cv_id,
            "product_role": str(row.get("product_role") or ""),
            "series_id": series_id,
            "scope_key": scope_key,
            "is_current_alias_head": cv_id in current_head_ids,
        }

    missing_current_ids = current_head_ids - returned_ids
    if missing_current_ids:
        raise LightCurveSelectionError(
            "Current SCIENCE alias heads disappeared while resolving scope: "
            + ", ".join(str(value) for value in sorted(missing_current_ids))
        )
    return contexts


def _load_interseries_generation_records(
    session: Session,
    *,
    scope_contexts: dict[int, dict[str, Any]],
    explicit_generation_ids: list[int] | None,
) -> dict[int, dict[str, Any]]:
    generation_ids = _normalize_interseries_generation_ids(explicit_generation_ids)
    field_ids = sorted(
        {
            int(context["scope_key"][0])
            for context in scope_contexts.values()
            if context["scope_key"][0] is not None
        }
    )
    if generation_ids is None and not field_ids:
        return {}

    stmt = select(
        InterseriesCalibrationGeneration.interseries_generation_id.label(
            "interseries_generation_id"
        ),
        InterseriesCalibrationGeneration.field_id.label("field_id"),
        InterseriesCalibrationGeneration.instrument_unit_id.label("instrument_unit_id"),
        InterseriesCalibrationGeneration.filter_name.label("filter_name"),
        InterseriesCalibrationGeneration.catalog_name.label("catalog_name"),
        InterseriesCalibrationGeneration.analysis_algorithm.label("analysis_algorithm"),
        InterseriesCalibrationGeneration.lifecycle_status.label("lifecycle_status"),
        InterseriesCalibrationGeneration.reliability_status.label("reliability_status"),
        InterseriesCalibrationGeneration.n_series.label("n_series"),
        InterseriesCalibrationSeries.field_id.label("membership_field_id"),
        InterseriesCalibrationSeries.series_id.label("series_id"),
        InterseriesCalibrationSeries.science_calibration_version_id.label(
            "science_calibration_version_id"
        ),
        InterseriesCalibrationSeries.offset_mag.label("offset_mag"),
        InterseriesCalibrationSeries.offset_err_mag.label("offset_err"),
    ).outerjoin(
        InterseriesCalibrationSeries,
        InterseriesCalibrationSeries.interseries_generation_id
        == InterseriesCalibrationGeneration.interseries_generation_id,
    )
    if generation_ids is not None and field_ids:
        stmt = stmt.where(
            or_(
                InterseriesCalibrationGeneration.field_id.in_(field_ids),
                InterseriesCalibrationGeneration.interseries_generation_id.in_(generation_ids),
            )
        )
    elif generation_ids is not None:
        stmt = stmt.where(
            InterseriesCalibrationGeneration.interseries_generation_id.in_(generation_ids)
        )
    else:
        stmt = stmt.where(InterseriesCalibrationGeneration.field_id.in_(field_ids))

    records: dict[int, dict[str, Any]] = {}
    for row in session.execute(stmt).mappings().all():
        generation_id = int(row["interseries_generation_id"])
        record = records.setdefault(
            generation_id,
            {
                "interseries_generation_id": generation_id,
                "field_id": int(row["field_id"]),
                "instrument_unit_id": _int_or_none(row.get("instrument_unit_id")),
                "filter_name": _str_or_none(row.get("filter_name")),
                "catalog_name": _str_or_none(row.get("catalog_name")),
                "analysis_algorithm": str(row["analysis_algorithm"]),
                "lifecycle_status": str(row["lifecycle_status"]),
                "reliability_status": str(row["reliability_status"]),
                "expected_n_series": row.get("n_series"),
                "broken_membership": False,
                "scope_key": (
                    int(row["field_id"]),
                    _int_or_none(row.get("instrument_unit_id")),
                    _str_or_none(row.get("filter_name")),
                    _str_or_none(row.get("catalog_name")),
                ),
                "sources": {},
            },
        )
        if (row.get("series_id") is None) != (row.get("science_calibration_version_id") is None):
            record["broken_membership"] = True
            continue
        if (row.get("membership_field_id") is not None
                and int(row["membership_field_id"]) != int(row["field_id"])):
            record["broken_membership"] = True
            continue
        if row.get("series_id") is not None:
            source_key = (
                int(row["series_id"]),
                int(row["science_calibration_version_id"]),
            )
            if source_key in record["sources"]:
                raise LightCurveSelectionError(
                    f"M4 generation {generation_id} has duplicate source identity {source_key}"
                )
            record["sources"][source_key] = {
                "interseries_generation_id": generation_id,
                "series_id": source_key[0],
                "calibration_version_id": source_key[1],
                "offset_mag": row.get("offset_mag"),
                "offset_err": row.get("offset_err"),
                "status": str(row["reliability_status"]),
                "algorithm": str(row["analysis_algorithm"]),
            }
    for record in records.values():
        expected_count = record["expected_n_series"]
        if expected_count is not None and len(record["sources"]) != int(expected_count):
            record["broken_membership"] = True
    return records


def _resolve_interseries_generation_selection(
    *,
    scope_contexts: dict[int, dict[str, Any]],
    current_science_calibration_version_ids: set[int],
    generation_records: dict[int, dict[str, Any]],
    offset_algorithm: str,
    explicit_generation_ids: list[int] | None,
) -> dict[str, Any]:
    algorithm = str(offset_algorithm or "").strip()
    if not algorithm:
        raise LightCurveSelectionError("offset_algorithm must be a non-empty exact algorithm name")
    requested_ids = _normalize_interseries_generation_ids(explicit_generation_ids)

    science_contexts = {
        cv_id: context
        for cv_id, context in scope_contexts.items()
        if context.get("product_role") == "SCIENCE" and context.get("series_id") is not None
    }
    contexts_by_scope: dict[tuple[Any, ...], dict[int, dict[str, Any]]] = {}
    for cv_id, context in science_contexts.items():
        contexts_by_scope.setdefault(context["scope_key"], {})[cv_id] = context
    if not contexts_by_scope:
        if requested_ids:
            raise LightCurveSelectionError(
                "Explicit interseries generation IDs cannot be matched to a selected SCIENCE scope"
            )
        return {
            "generation_by_scope": {},
            "source_offsets": {},
            "status_by_scope": {},
            "warnings": [],
        }

    explicit_by_scope: dict[tuple[Any, ...], dict[str, Any]] = {}
    if requested_ids is not None:
        for generation_id in requested_ids:
            record = generation_records.get(generation_id)
            if record is None:
                raise LightCurveSelectionError(
                    f"Explicit interseries generation {generation_id} does not exist"
                )
            key = record["scope_key"]
            if key not in contexts_by_scope:
                raise LightCurveSelectionError(
                    f"Explicit interseries generation {generation_id} is outside the selected field/instrument/filter/catalog scopes"
                )
            if record["lifecycle_status"] != "COMPLETE_IMMUTABLE":
                raise LightCurveSelectionError(
                    f"Explicit interseries generation {generation_id} is not COMPLETE_IMMUTABLE"
                )
            if record["analysis_algorithm"] != algorithm:
                raise LightCurveSelectionError(
                    f"Explicit interseries generation {generation_id} uses algorithm "
                    f"{record['analysis_algorithm']!r}, not {algorithm!r}"
                )
            if record.get("broken_membership"):
                raise LightCurveSelectionError(
                    f"Explicit M4 generation {generation_id} has orphaned or incomplete membership"
                )
            if key in explicit_by_scope:
                previous = int(explicit_by_scope[key]["interseries_generation_id"])
                raise LightCurveSelectionError(
                    f"Multiple explicit M4 generations ({previous}, {generation_id}) target one scope"
                )
            explicit_by_scope[key] = record

    generation_by_scope: dict[tuple[Any, ...], dict[str, Any]] = {}
    source_offsets: dict[tuple[int, int, int], dict[str, Any]] = {}
    status_by_scope: dict[tuple[Any, ...], str] = {}
    warnings_out: list[str] = []

    for scope_key, cv_contexts in contexts_by_scope.items():
        required_sources = {
            (int(context["series_id"]), cv_id)
            for cv_id, context in cv_contexts.items()
        }
        explicit_record = explicit_by_scope.get(scope_key)
        if explicit_record is not None:
            selected = explicit_record
            missing = sorted(required_sources - set(selected["sources"]))
            if missing:
                raise LightCurveSelectionError(
                    f"Explicit M4 generation {selected['interseries_generation_id']} does not cover "
                    f"every selected SCIENCE source in scope {scope_key}: {missing}"
                )
        else:
            in_scope = [
                record
                for record in generation_records.values()
                if record["scope_key"] == scope_key
                and record["lifecycle_status"] == "COMPLETE_IMMUTABLE"
                and record["analysis_algorithm"] == algorithm
            ]
            if any(record.get("broken_membership") for record in in_scope):
                raise LightCurveSelectionError(
                    f"M4 generation in scope {scope_key} has orphaned or incomplete membership"
                )
            eligible = [
                record
                for record in in_scope
                if required_sources.issubset(set(record["sources"]))
            ]
            if len(eligible) > 1:
                ids = sorted(int(record["interseries_generation_id"]) for record in eligible)
                raise LightCurveSelectionError(
                    f"Multiple eligible M4 generations {ids} match scope {scope_key}; "
                    "supply --interseries-generation-id explicitly"
                )
            if len(eligible) == 1:
                selected = eligible[0]
            elif in_scope:
                available_ids = sorted(
                    int(record["interseries_generation_id"]) for record in in_scope
                )
                raise LightCurveSelectionError(
                    f"No complete M4 generation among {available_ids} covers every selected "
                    f"SCIENCE source in scope {scope_key}; refusing partial output"
                )
            else:
                status_by_scope[scope_key] = "NO_M4_GENERATION"
                warnings_out.append(
                    f"NO_M4_GENERATION for field/instrument/filter/catalog scope {scope_key}; "
                    "returning Stage-2-only rows"
                )
                continue

        generation_by_scope[scope_key] = selected
        status_by_scope[scope_key] = "NO_M4_SOURCE_MATCH"
        for (series_id, calibration_version_id), source in selected["sources"].items():
            source_offsets[
                (
                    int(selected["interseries_generation_id"]),
                    int(series_id),
                    int(calibration_version_id),
                )
            ] = source

    return {
        "generation_by_scope": generation_by_scope,
        "source_offsets": source_offsets,
        "status_by_scope": status_by_scope,
        "warnings": warnings_out,
    }


def _apply_interseries_selection_to_scope(
    scope: LightCurveScope,
    selection: dict[str, Any],
) -> None:
    scope.interseries_generation_ids = sorted(
        int(record["interseries_generation_id"])
        for record in selection["generation_by_scope"].values()
    )
    scope.interseries_generation_scopes = [
        {
            "interseries_generation_id": int(record["interseries_generation_id"]),
            "field_id": record["field_id"],
            "instrument_unit_id": record["instrument_unit_id"],
            "filter_name": record["filter_name"],
            "catalog_name": record["catalog_name"],
            "analysis_algorithm": record["analysis_algorithm"],
        }
        for _key, record in sorted(
            selection["generation_by_scope"].items(),
            key=lambda item: (
                tuple("" if value is None else str(value) for value in item[0]),
                int(item[1]["interseries_generation_id"]),
            ),
        )
    ]
    scope.warnings = list(selection["warnings"])


def _normalize_interseries_generation_ids(values: list[int] | None) -> list[int] | None:
    if values is None:
        return None
    try:
        generation_ids = [int(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise LightCurveSelectionError(
            "interseries_generation_ids must contain integer IDs"
        ) from exc
    if not generation_ids:
        raise LightCurveSelectionError(
            "interseries_generation_ids must not be empty when explicitly provided"
        )
    if any(value <= 0 for value in generation_ids):
        raise LightCurveSelectionError("interseries_generation_ids must contain positive IDs")
    if len(set(generation_ids)) != len(generation_ids):
        raise LightCurveSelectionError("interseries_generation_ids must not contain duplicate IDs")
    return generation_ids


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _science_row_from_db(row: dict[str, Any], *, reject_flags: set[str]) -> dict[str, Any]:
    (
        measurement_flags,
        object_flags,
        frame_flags,
        quality_flags,
        random_err,
        is_rejected,
        reject_reasons,
    ) = _science_rejection_state(
        measurement_flags=row.get("measurement_flags"),
        object_qc_flags=row.get("object_qc_flags"),
        frame_quality_flags=row.get("frame_quality_flags"),
        mag_cal=row.get("mag_cal"),
        mag_err=row.get("mag_err"),
        mag_err_random=row.get("mag_err_random"),
        reject_flags=reject_flags,
    )
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
    mag_global = row.get("mag_cal")
    offset = row.get("interseries_offset_mag")
    if _is_finite(mag_global) and _is_finite(offset):
        mag_global = float(mag_global) + float(offset)
    mag_global_err_random = random_err
    mag_global_err_systematic = combine_mag_error(systematic_err, row.get("interseries_offset_err"))
    mag_global_err = combine_mag_error(mag_global_err_random, mag_global_err_systematic)
    if row.get("interseries_offset_mag") is None:
        interseries_status = row.get("interseries_offset_status") or "NO_INTERSERIES_OFFSET"
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
        "interseries_generation_id": row.get("interseries_generation_id"),
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


def _science_rejection_state(
    *,
    measurement_flags: Any,
    object_qc_flags: Any,
    frame_quality_flags: Any,
    mag_cal: Any,
    mag_err: Any,
    mag_err_random: Any,
    reject_flags: set[str],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    Any,
    bool,
    list[str],
]:
    measurement_flags = _dict_or_empty(measurement_flags)
    object_flags = _dict_or_empty(object_qc_flags)
    frame_flags = _dict_or_empty(frame_quality_flags)
    quality_flags = merge_quality_flags(
        measurement_flags=measurement_flags,
        object_qc_flags=object_flags,
        frame_quality_flags=frame_flags,
    )
    random_err = mag_err_random if mag_err_random is not None else mag_err
    is_rejected, reject_reasons = evaluate_rejection(
        quality_flags=quality_flags,
        mag=mag_cal,
        mag_err=random_err,
        reject_flags=reject_flags,
    )
    return (
        measurement_flags,
        object_flags,
        frame_flags,
        quality_flags,
        random_err,
        is_rejected,
        reject_reasons,
    )


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
