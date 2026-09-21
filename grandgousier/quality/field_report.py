from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from grandgousier.calibration.provenance import resolve_source_calibration
from grandgousier.db.models import (
    CalibrationVersion,
    Frame,
    FrameQc,
    InstrumentUnit,
    Measurement,
    ObjectQc,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view
from grandgousier.quality.blend_diagnostics import (
    blend_diagnostic_columns,
    build_blend_diagnostic_rows,
    summarize_blend_diagnostics,
)
from grandgousier.quality.candidate_triage import (
    build_candidate_triage,
    candidate_triage_columns,
)
from grandgousier.quality.lightcurve_quality import (
    CalibrationAnalysisSnapshot,
    build_rms_mag_summary,
    load_object_scatter_rows,
)
from grandgousier.quality.object_quality import _has_aperture_stability, _preserve_aperture_stability_flags


@dataclass
class FieldQcReport:
    report: dict[str, Any]
    summary_rows: list[dict[str, Any]]
    rms_bins: list[dict[str, Any]]
    candidate_rows: list[dict[str, Any]]
    blend_rows: list[dict[str, Any]]


def build_field_qc_report(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_qc_algorithm: str = "detection_catalog_qc_v1",
    object_qc_algorithm: str = "object_qc_v1",
    min_measurements: int = 10,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    min_rms: float | None = None,
    min_excess: float = 2.0,
    exclude_flagged: bool = False,
    exclude_crowded: bool = True,
    exclude_edge: bool = True,
    exclude_saturated: bool = True,
    exclude_catalog_outliers: bool = True,
    reject_aperture_unstable: bool = True,
    require_detection_support: bool = True,
    max_mag_median: float | None = 22.5,
    max_ref_mag_delta: float | None = 2.0,
    top_limit: int = 20,
    target_object_ids: list[int] | None = None,
    enable_intrinsic_variability_candidates: bool = True,
    min_intrinsic_rms: float | None = 0.03,
    min_rms_over_mag_err: float | None = 4.0,
    min_mad_over_mag_err: float | None = 2.5,
    intrinsic_mag_err_floor: float | None = 0.01,
    min_robust_peak_to_peak: float | None = 0.08,
    enable_low_amplitude_intrinsic_candidates: bool = True,
    low_amplitude_min_measurements: int = 30,
    low_amplitude_min_intrinsic_rms: float | None = 0.03,
    low_amplitude_min_rms_over_mag_err: float | None = 2.3,
    low_amplitude_min_mad_over_mag_err: float | None = 2.0,
    low_amplitude_min_robust_peak_to_peak: float | None = 0.08,
    borderline_threshold_ratio: float = 0.9,
    diagnostic_max_failed_criteria: int = 2,
    analysis_snapshot: CalibrationAnalysisSnapshot | None = None,
) -> FieldQcReport:
    """Build a compact field/series QC report from existing database products."""

    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"CalibrationVersion not found: {calibration_name}")

    instrument_unit = None
    if instrument_unit_id is not None:
        instrument_unit = session.scalar(select(InstrumentUnit).where(InstrumentUnit.instrument_unit_id == instrument_unit_id))

    source_cv = resolve_source_calibration(session, cv)

    calibration_chain_names = _calibration_chain_names(session, cv)
    series_selection = _resolve_field_series_filter(
        session,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids,
        calibration_version_id=cv.calibration_version_id,
        calibration_names=calibration_chain_names,
    )
    selected_frame_ids = frame_ids
    if series_selection is not None:
        selected_frame_ids = series_selection["frame_ids"]
        if instrument_unit_id is None and series_selection.get("instrument_unit_id") is not None:
            instrument_unit_id = int(series_selection["instrument_unit_id"])
            instrument_unit = session.scalar(select(InstrumentUnit).where(InstrumentUnit.instrument_unit_id == instrument_unit_id))

    frames = _frame_summary(session, instrument_unit_id=instrument_unit_id, frame_ids=selected_frame_ids)
    frame_qc = _frame_qc_summary(
        session,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        algorithm=frame_qc_algorithm,
    )
    measurements = _measurement_summary(
        session,
        calibration_version_id=cv.calibration_version_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        analysis_snapshot=analysis_snapshot,
    )

    analysis_snapshot_cache_hit = bool(
        analysis_snapshot is not None
        and analysis_snapshot.timings_sec.get("_runner_cache_hit", 1.0)
    )
    if analysis_snapshot is not None:
        if not analysis_snapshot.matches(
            calibration_name=calibration_name,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            frame_ids=selected_frame_ids,
            min_measurements=min_measurements,
            fwhm_header_unit=analysis_snapshot.fwhm_header_unit,
        ):
            raise ValueError("analysis_snapshot scope does not match build_field_qc_report arguments")
        object_rows = analysis_snapshot.object_rows
    else:
        object_rows = load_object_scatter_rows(
            session,
            calibration_name=calibration_name,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            frame_ids=selected_frame_ids,
            min_measurements=min_measurements,
        )
    rms_bins = build_rms_mag_summary(
        object_rows,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
    )

    object_qc = _object_qc_summary(
        session,
        calibration_version_id=cv.calibration_version_id,
        source_calibration_version_id=(source_cv.calibration_version_id if source_cv is not None else None),
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        algorithm=object_qc_algorithm,
    )

    triage = build_candidate_triage(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        min_measurements=min_measurements,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
        min_rms=min_rms,
        min_excess=min_excess,
        exclude_flagged=exclude_flagged,
        exclude_crowded=exclude_crowded,
        exclude_edge=exclude_edge,
        exclude_saturated=exclude_saturated,
        exclude_catalog_outliers=exclude_catalog_outliers,
        reject_aperture_unstable=reject_aperture_unstable,
        require_detection_support=require_detection_support,
        max_mag_median=max_mag_median,
        max_ref_mag_delta=max_ref_mag_delta,
        qc_algorithm=object_qc_algorithm,
        limit=None,
        target_object_ids=target_object_ids,
        enable_intrinsic_variability_candidates=enable_intrinsic_variability_candidates,
        min_intrinsic_rms=min_intrinsic_rms,
        min_rms_over_mag_err=min_rms_over_mag_err,
        min_mad_over_mag_err=min_mad_over_mag_err,
        intrinsic_mag_err_floor=intrinsic_mag_err_floor,
        min_robust_peak_to_peak=min_robust_peak_to_peak,
        enable_low_amplitude_intrinsic_candidates=enable_low_amplitude_intrinsic_candidates,
        low_amplitude_min_measurements=low_amplitude_min_measurements,
        low_amplitude_min_intrinsic_rms=low_amplitude_min_intrinsic_rms,
        low_amplitude_min_rms_over_mag_err=low_amplitude_min_rms_over_mag_err,
        low_amplitude_min_mad_over_mag_err=low_amplitude_min_mad_over_mag_err,
        low_amplitude_min_robust_peak_to_peak=low_amplitude_min_robust_peak_to_peak,
        borderline_threshold_ratio=borderline_threshold_ratio,
        diagnostic_max_failed_criteria=diagnostic_max_failed_criteria,
        object_rows=object_rows,
    )
    candidate_rows = triage.rows

    blend_rows = build_blend_diagnostic_rows(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        algorithm=object_qc_algorithm,
        only_flagged=False,
        sort="risk",
        limit=None,
    )
    blend_summary = summarize_blend_diagnostics(blend_rows)

    warnings = _build_warnings(
        frames=frames,
        measurements=measurements,
        object_qc=object_qc,
        triage_summary=triage.summary,
    )

    report = {
        "metadata": {
            "calibration_name": calibration_name,
            "calibration_version_id": cv.calibration_version_id,
            "source_calibration_version_id": source_cv.calibration_version_id if source_cv is not None else None,
            "source_calibration_name": source_cv.name if source_cv is not None else None,
            "catalog_name": catalog_name,
            "instrument_unit_id": instrument_unit_id,
            "instrument": getattr(instrument_unit, "instrument", None),
            "site": getattr(instrument_unit, "site", None),
            "tube": getattr(instrument_unit, "tube", None),
            "camera": getattr(instrument_unit, "camera", None),
            "filter_name": getattr(instrument_unit, "filter_name", None),
            "frame_qc_algorithm": frame_qc_algorithm,
            "object_qc_algorithm": object_qc_algorithm,
            "analysis_snapshot_algorithm": (
                analysis_snapshot.algorithm if analysis_snapshot is not None else None
            ),
            "analysis_snapshot_cache_hit": analysis_snapshot_cache_hit,
            "analysis_snapshot_measurements": (
                len(analysis_snapshot.measurement_rows)
                if analysis_snapshot is not None
                else None
            ),
            "frame_selection_mode": _frame_selection_mode(series_selection=series_selection, frame_ids=selected_frame_ids),
            "selected_frame_ids_count": len(selected_frame_ids) if selected_frame_ids else None,
            "field_series": _field_series_metadata(series_selection),
            "quality_policy": {
                "require_detection_support_effective": bool(require_detection_support),
                "target_object_ids": list(target_object_ids or []),
                "enable_intrinsic_variability_candidates": enable_intrinsic_variability_candidates,
                "min_intrinsic_rms": min_intrinsic_rms,
                "min_rms_over_mag_err": min_rms_over_mag_err,
                "min_mad_over_mag_err": min_mad_over_mag_err,
                "intrinsic_mag_err_floor": intrinsic_mag_err_floor,
                "min_robust_peak_to_peak": min_robust_peak_to_peak,
                "enable_low_amplitude_intrinsic_candidates": enable_low_amplitude_intrinsic_candidates,
                "low_amplitude_min_measurements": low_amplitude_min_measurements,
                "low_amplitude_min_intrinsic_rms": low_amplitude_min_intrinsic_rms,
                "low_amplitude_min_rms_over_mag_err": low_amplitude_min_rms_over_mag_err,
                "low_amplitude_min_mad_over_mag_err": low_amplitude_min_mad_over_mag_err,
                "low_amplitude_min_robust_peak_to_peak": low_amplitude_min_robust_peak_to_peak,
                "borderline_threshold_ratio": borderline_threshold_ratio,
                "diagnostic_max_failed_criteria": diagnostic_max_failed_criteria,
            },
        },
        "frames": frames,
        "frame_qc": frame_qc,
        "measurements": measurements,
        "objects": {
            "objects_used_for_scatter": len(object_rows),
            "rms_mag_bins": len(rms_bins),
            "mag_rms_median": _median([r.mag_rms for r in object_rows if r.mag_rms is not None]),
            "mag_mad_median": _median([r.mag_mad for r in object_rows if r.mag_mad is not None]),
        },
        "object_qc": object_qc,
        "candidate_triage": triage.summary,
        "blend_diagnostics": blend_summary,
        "top_candidates": {
            "accepted": [r for r in candidate_rows if r.get("candidate_status") == "ACCEPTED"][:top_limit],
            "borderline": [r for r in candidate_rows if r.get("candidate_status") == "BORDERLINE"][:top_limit],
            "diagnostic": [r for r in candidate_rows if r.get("candidate_status") == "DIAGNOSTIC"][:top_limit],
            "rejected": [r for r in candidate_rows if r.get("candidate_status") == "REJECTED"][:top_limit],
        },
        "top_blend_risk": blend_rows[:top_limit],
        "warnings": warnings,
    }
    summary_rows = _summary_rows(report)
    return FieldQcReport(
        report=report,
        summary_rows=summary_rows,
        rms_bins=rms_bins,
        candidate_rows=candidate_rows,
        blend_rows=blend_rows,
    )



def _resolve_field_series_filter(
    session: Session,
    *,
    series_id: int | None,
    field_series_name: str | None,
    frame_ids: list[int] | None,
    calibration_version_id: int | None = None,
    calibration_names: list[str] | None = None,
) -> dict[str, Any] | None:
    """Resolve an optional field_series selector into metadata and frame IDs.

    ``field-qc-report`` historically reported all frames compatible with the
    selected instrument unit.  For series-level reports this is too broad: frame
    counts, frame_qc and scatter diagnostics can silently include older runs.
    The selector below keeps the public report builder independent from the
    archive pipeline while still accepting common user-facing identifiers.
    """

    if series_id is None and not field_series_name:
        return None
    if frame_ids:
        raise ValueError("field_series selectors cannot be combined with explicit frame_ids")
    if series_id is not None and field_series_name:
        resolved_by_id = _load_field_series_selection(session, int(series_id))
        resolved_by_name = _find_field_series_selection(
            session,
            field_series_name,
            calibration_version_id=calibration_version_id,
            calibration_names=calibration_names,
        )
        if int(resolved_by_id["series_id"]) != int(resolved_by_name["series_id"]):
            raise ValueError(
                "field_series selectors disagree: "
                f"series_id={series_id} resolves to {resolved_by_id['series_id']}, "
                f"field_series_name={field_series_name!r} resolves to {resolved_by_name['series_id']}"
            )
        return resolved_by_id
    if series_id is not None:
        return _load_field_series_selection(session, int(series_id))
    return _find_field_series_selection(
        session,
        str(field_series_name),
        calibration_version_id=calibration_version_id,
        calibration_names=calibration_names,
    )


def _load_field_series_selection(session: Session, series_id: int) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT
                s.series_id,
                s.series_uid,
                s.field_id,
                f.field_name,
                s.instrument_unit_id,
                s.filter_name,
                s.time_start,
                s.time_end,
                s.mjd_start,
                s.mjd_end,
                s.n_frames,
                s.grouping_method,
                s.status,
                s.metadata_json
            FROM field_series s
            JOIN fields f ON f.field_id = s.field_id
            WHERE s.series_id = :series_id
            """
        ),
        {"series_id": int(series_id)},
    ).mappings().first()
    if row is None:
        raise ValueError(f"field_series row not found: series_id={series_id}")
    return _with_series_frame_ids(session, dict(row))


def _find_field_series_selection(
    session: Session,
    field_series_name: str,
    *,
    calibration_version_id: int | None = None,
    calibration_names: list[str] | None = None,
) -> dict[str, Any]:
    name = str(field_series_name).strip()
    if not name:
        raise ValueError("field_series_name cannot be empty")
    if name.isdigit():
        return _load_field_series_selection(session, int(name))

    aliases = _field_series_name_aliases(name=name, calibration_names=calibration_names)
    rows = _query_field_series_by_aliases(session, aliases)
    if not rows and calibration_version_id is not None and _name_matches_calibration_names(
        name,
        calibration_names=calibration_names,
    ):
        rows = _query_field_series_by_calibration_overlap(
            session,
            calibration_version_id=int(calibration_version_id),
        )
    if not rows:
        raise ValueError(_field_series_not_found_message(session, field_series_name))

    best_rank_rows = []
    # The SQL ORDER BY encodes match priority.  Ambiguity is only an error for
    # rows with the same highest-priority match.  This allows a series_uid match
    # to win over a same-named field containing multiple series.
    def _rank(row: dict[str, Any]) -> int:
        if row.get("series_uid") == name:
            return 0
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, dict) and metadata.get("series_name") == name:
            return 1
        if row.get("last_calibration_prefix") == name:
            return 2
        if row.get("field_name") == name:
            return 3
        if isinstance(metadata, dict) and metadata.get("primary_object_name") == name:
            return 4
        if row.get("match_reason") == "calibration_overlap":
            return 6
        return 5

    first_rank = _rank(dict(rows[0]))
    for row in rows:
        row_dict = dict(row)
        if _rank(row_dict) == first_rank:
            best_rank_rows.append(row_dict)
    if len(best_rank_rows) > 1:
        ids = ", ".join(str(r.get("series_id")) for r in best_rank_rows[:10])
        raise ValueError(
            f"field_series_name={field_series_name!r} is ambiguous; matching series_id values: {ids}"
        )
    return _with_series_frame_ids(session, best_rank_rows[0])


def _query_field_series_by_aliases(session: Session, aliases: list[str]) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            WITH latest_run AS (
                SELECT DISTINCT ON (series_id)
                    series_id,
                    calibration_prefix
                FROM pipeline_runs
                WHERE series_id IS NOT NULL
                ORDER BY series_id, pipeline_run_id DESC
            )
            SELECT
                s.series_id,
                s.series_uid,
                s.field_id,
                f.field_name,
                s.instrument_unit_id,
                s.filter_name,
                s.time_start,
                s.time_end,
                s.mjd_start,
                s.mjd_end,
                s.n_frames,
                s.grouping_method,
                s.status,
                s.metadata_json,
                lr.calibration_prefix AS last_calibration_prefix,
                'alias' AS match_reason
            FROM field_series s
            JOIN fields f ON f.field_id = s.field_id
            LEFT JOIN latest_run lr ON lr.series_id = s.series_id
            WHERE
                s.series_uid = ANY(:aliases)
                OR f.field_name = ANY(:aliases)
                OR s.metadata_json ->> 'series_name' = ANY(:aliases)
                OR s.metadata_json ->> 'primary_object_name' = ANY(:aliases)
                OR s.metadata_json ->> 'calibration_prefix' = ANY(:aliases)
                OR lr.calibration_prefix = ANY(:aliases)
            ORDER BY
                CASE
                    WHEN s.series_uid = :name THEN 0
                    WHEN s.metadata_json ->> 'series_name' = :name THEN 1
                    WHEN lr.calibration_prefix = :name THEN 2
                    WHEN f.field_name = :name THEN 3
                    WHEN s.metadata_json ->> 'primary_object_name' = :name THEN 4
                    ELSE 5
                END,
                s.series_id
            """
        ),
        {"name": aliases[0], "aliases": aliases},
    ).mappings().all()
    return [dict(row) for row in rows]


def _query_field_series_by_calibration_overlap(
    session: Session,
    *,
    calibration_version_id: int,
) -> list[dict[str, Any]]:
    """Find series whose frames contain measurements from a calibration version.

    This is the important fallback for derived calibrations: a user may type the
    current calibration prefix (for example ``..._v30``), while the stored
    field_series row still carries the earlier pipeline prefix (for example
    ``..._v27``).  Frame overlap is the least ambiguous link between the target
    calibration and the actual observing series.
    """

    rows = session.execute(
        text(
            """
            WITH latest_run AS (
                SELECT DISTINCT ON (series_id)
                    series_id,
                    calibration_prefix
                FROM pipeline_runs
                WHERE series_id IS NOT NULL
                ORDER BY series_id, pipeline_run_id DESC
            ), overlap AS (
                SELECT
                    sf.series_id,
                    COUNT(DISTINCT m.frame_id) AS overlap_frames
                FROM field_series_frames sf
                JOIN measurements m ON m.frame_id = sf.frame_id
                WHERE m.calibration_version_id = :calibration_version_id
                GROUP BY sf.series_id
            )
            SELECT
                s.series_id,
                s.series_uid,
                s.field_id,
                f.field_name,
                s.instrument_unit_id,
                s.filter_name,
                s.time_start,
                s.time_end,
                s.mjd_start,
                s.mjd_end,
                s.n_frames,
                s.grouping_method,
                s.status,
                s.metadata_json,
                lr.calibration_prefix AS last_calibration_prefix,
                overlap.overlap_frames,
                'calibration_overlap' AS match_reason
            FROM overlap
            JOIN field_series s ON s.series_id = overlap.series_id
            JOIN fields f ON f.field_id = s.field_id
            LEFT JOIN latest_run lr ON lr.series_id = s.series_id
            ORDER BY overlap.overlap_frames DESC, s.series_id
            """
        ),
        {"calibration_version_id": int(calibration_version_id)},
    ).mappings().all()
    result = [dict(row) for row in rows]
    if len(result) <= 1:
        return result
    top_overlap = result[0].get("overlap_frames")
    return [row for row in result if row.get("overlap_frames") == top_overlap]


def _field_series_name_aliases(*, name: str, calibration_names: list[str] | None) -> list[str]:
    aliases: list[str] = [name]
    calibration_aliases: list[str] = []
    for calibration_name in calibration_names or []:
        calibration_aliases.extend(_calibration_name_aliases(calibration_name))
    calibration_aliases = _unique_nonempty(calibration_aliases)
    if _name_matches_calibration_aliases(name, calibration_aliases):
        aliases.extend(calibration_aliases)
    return _unique_nonempty(aliases)


def _calibration_chain_names(session: Session, cv: CalibrationVersion) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    current: CalibrationVersion | None = cv
    while current is not None and current.calibration_version_id not in seen:
        seen.add(int(current.calibration_version_id))
        names.append(current.name)
        current = resolve_source_calibration(session, current)
    return names


def _calibration_name_aliases(calibration_name: str | None) -> list[str]:
    if not calibration_name:
        return []
    name = str(calibration_name).strip()
    aliases = [name]
    markers = (
        "_ps1_",
        "_color_",
        "_ensemble_",
        "_aperture_",
        "_raw_aperture_",
        "_apstab_",
        "_preliminary_",
    )
    for marker in markers:
        idx = name.find(marker)
        if idx > 0:
            aliases.append(name[:idx])
    return _unique_nonempty(aliases)


def _name_matches_calibration_names(name: str, *, calibration_names: list[str] | None) -> bool:
    aliases: list[str] = []
    for calibration_name in calibration_names or []:
        aliases.extend(_calibration_name_aliases(calibration_name))
    return _name_matches_calibration_aliases(name, _unique_nonempty(aliases))


def _name_matches_calibration_aliases(name: str, aliases: list[str]) -> bool:
    needle = name.strip()
    if not needle:
        return False
    for alias in aliases:
        if needle == alias:
            return True
        if alias.startswith(f"{needle}_"):
            return True
        if needle.startswith(f"{alias}_"):
            return True
    return False


def _unique_nonempty(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text_value = str(value).strip() if value is not None else ""
        if text_value and text_value not in seen:
            seen.add(text_value)
            out.append(text_value)
    return out


def _field_series_not_found_message(session: Session, field_series_name: str) -> str:
    suggestions = _field_series_suggestions(session)
    if not suggestions:
        return f"field_series row not found: field_series_name={field_series_name!r}"
    lines = [
        f"field_series row not found: field_series_name={field_series_name!r}.",
        "Available field_series values:",
    ]
    for row in suggestions:
        parts = [
            f"series_id={row.get('series_id')}",
            f"series_uid={row.get('series_uid')}",
            f"field_name={row.get('field_name')}",
            f"n_frames={row.get('n_frames')}",
        ]
        if row.get("last_calibration_prefix"):
            parts.append(f"last_calibration_prefix={row.get('last_calibration_prefix')}")
        metadata = row.get("metadata_json") or {}
        if isinstance(metadata, dict) and metadata.get("primary_object_name"):
            parts.append(f"primary_object_name={metadata.get('primary_object_name')}")
        lines.append("  - " + ", ".join(parts))
    return "\n".join(lines)


def _field_series_suggestions(session: Session, *, limit: int = 10) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            WITH latest_run AS (
                SELECT DISTINCT ON (series_id)
                    series_id,
                    calibration_prefix
                FROM pipeline_runs
                WHERE series_id IS NOT NULL
                ORDER BY series_id, pipeline_run_id DESC
            )
            SELECT
                s.series_id,
                s.series_uid,
                f.field_name,
                s.n_frames,
                s.status,
                s.metadata_json,
                lr.calibration_prefix AS last_calibration_prefix
            FROM field_series s
            JOIN fields f ON f.field_id = s.field_id
            LEFT JOIN latest_run lr ON lr.series_id = s.series_id
            ORDER BY s.updated_at DESC, s.series_id DESC
            LIMIT :limit
            """
        ),
        {"limit": int(limit)},
    ).mappings().all()
    return [dict(row) for row in rows]


def _with_series_frame_ids(session: Session, row: dict[str, Any]) -> dict[str, Any]:
    frame_rows = session.execute(
        text(
            """
            SELECT sf.frame_id
            FROM field_series_frames sf
            JOIN frames fr ON fr.frame_id = sf.frame_id
            WHERE sf.series_id = :series_id
            ORDER BY sf.order_index, fr.mjd, fr.date_obs, fr.frame_id
            """
        ),
        {"series_id": int(row["series_id"])},
    ).all()
    frame_ids = [int(r[0]) for r in frame_rows]
    if not frame_ids:
        raise ValueError(f"field_series has no frames: series_id={row['series_id']}")
    row["frame_ids"] = frame_ids
    return row


def _field_series_metadata(series_selection: dict[str, Any] | None) -> dict[str, Any] | None:
    if series_selection is None:
        return None
    metadata = dict(series_selection.get("metadata_json") or {})
    return {
        "series_id": int(series_selection["series_id"]),
        "series_uid": series_selection.get("series_uid"),
        "field_id": int(series_selection["field_id"]) if series_selection.get("field_id") is not None else None,
        "field_name": series_selection.get("field_name"),
        "instrument_unit_id": int(series_selection["instrument_unit_id"]) if series_selection.get("instrument_unit_id") is not None else None,
        "filter_name": series_selection.get("filter_name"),
        "time_start": series_selection.get("time_start"),
        "time_end": series_selection.get("time_end"),
        "mjd_start": series_selection.get("mjd_start"),
        "mjd_end": series_selection.get("mjd_end"),
        "n_frames": int(series_selection.get("n_frames") or 0),
        "selected_frame_ids_count": len(series_selection.get("frame_ids") or []),
        "grouping_method": series_selection.get("grouping_method"),
        "status": series_selection.get("status"),
        "last_calibration_prefix": series_selection.get("last_calibration_prefix"),
        "metadata": metadata,
    }


def _frame_selection_mode(*, series_selection: dict[str, Any] | None, frame_ids: list[int] | None) -> str:
    if series_selection is not None:
        return "field_series"
    if frame_ids:
        return "explicit_frame_ids"
    return "calibration_scope"

def field_qc_summary_columns() -> list[str]:
    return ["section", "metric", "value"]


def export_field_qc_json(report: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8")


def export_field_qc_tables(
    *,
    prefix: str | Path,
    rms_bins: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    blend_rows: list[dict[str, Any]],
) -> dict[str, str]:
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    outputs = {
        "rms_bins": str(prefix.parent / f"{prefix.name}_rms_bins.csv"),
        "candidate_triage": str(prefix.parent / f"{prefix.name}_candidate_triage.csv"),
        "blend_diagnostics": str(prefix.parent / f"{prefix.name}_blend_diagnostics.csv"),
    }
    _write_csv(rms_bins, outputs["rms_bins"])
    _write_csv(candidate_rows, outputs["candidate_triage"], columns=candidate_triage_columns())
    _write_csv(blend_rows, outputs["blend_diagnostics"], columns=blend_diagnostic_columns())
    return outputs


def _frame_summary(session: Session, *, instrument_unit_id: int | None, frame_ids: list[int] | None = None) -> dict[str, Any]:
    stmt = select(Frame)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    frames = list(session.scalars(stmt).all())
    wcs_counts = Counter(str(f.wcs_status) for f in frames)
    flat_counts = Counter(str(f.flat_status) for f in frames)
    dark_counts = Counter(str(getattr(f, "dark_status", None)) for f in frames)
    sexcat_counts = Counter(str(getattr(f, "sexcat_status", None)) for f in frames)
    mjds = [float(f.mjd) for f in frames if f.mjd is not None and _is_finite(f.mjd)]
    exptimes = [float(f.exptime) for f in frames if f.exptime is not None and _is_finite(f.exptime)]
    return {
        "n_frames": len(frames),
        "mjd_min": min(mjds) if mjds else None,
        "mjd_max": max(mjds) if mjds else None,
        "exptime_median": _median(exptimes),
        "wcs_status_counts": dict(sorted(wcs_counts.items())),
        "flat_status_counts": dict(sorted(flat_counts.items())),
        "dark_status_counts": dict(sorted(dark_counts.items())),
        "sexcat_status_counts": dict(sorted(sexcat_counts.items())),
    }


def _frame_qc_summary(
    session: Session,
    *,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None = None,
    algorithm: str,
) -> dict[str, Any]:
    stmt = select(FrameQc, Frame).join(Frame, Frame.frame_id == FrameQc.frame_id)
    if catalog_name:
        stmt = stmt.where(FrameQc.catalog_name == catalog_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if algorithm:
        stmt = stmt.where(FrameQc.algorithm == algorithm)
    rows = list(session.execute(stmt).all())
    qcs = [qc for qc, _frame in rows]
    return {
        "n_frame_qc": len(qcs),
        "n_matched_median": _median([qc.n_matched for qc in qcs]),
        "astrom_rms_arcsec_median": _median([qc.astrom_rms_arcsec for qc in qcs]),
        "sep_median_arcsec_median": _median([qc.sep_median_arcsec for qc in qcs]),
        "sep_p95_arcsec_median": _median([qc.sep_p95_arcsec for qc in qcs]),
        "zp_median_median": _median([qc.zp_median for qc in qcs]),
        "zp_mad_median": _median([qc.zp_mad for qc in qcs]),
    }


def _measurement_summary(
    session: Session,
    *,
    calibration_version_id: int,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None = None,
    analysis_snapshot: CalibrationAnalysisSnapshot | None = None,
) -> dict[str, Any]:
    if analysis_snapshot is not None:
        measurements = list(analysis_snapshot.measurement_rows)
        object_ids = {int(row.object_id) for row in measurements}
        mag_cals = [
            float(row.mag_cal)
            for row in measurements
            if row.mag_cal is not None and _is_finite(row.mag_cal)
        ]
        mag_errs = [
            float(row.mag_err)
            for row in measurements
            if row.mag_err is not None and _is_finite(row.mag_err)
        ]
        return {
            "n_measurements": len(measurements),
            "n_objects": len(object_ids),
            "mag_cal_min": min(mag_cals) if mag_cals else None,
            "mag_cal_max": max(mag_cals) if mag_cals else None,
            "mag_err_median": _median(mag_errs),
            "materialization_source": analysis_snapshot.materialization_source,
        }

    view = load_calibration_photometry_view(
        session,
        calibration_version_id=int(calibration_version_id),
        frame_ids=frame_ids,
        prefer_science=True,
    )
    rows = list(view.rows)
    if instrument_unit_id is not None and rows:
        candidate_frame_ids = sorted({int(row.frame_id) for row in rows})
        allowed = set(
            int(value)
            for value in session.scalars(
                select(Frame.frame_id).where(
                    Frame.frame_id.in_(candidate_frame_ids),
                    Frame.instrument_unit_id == int(instrument_unit_id),
                )
            ).all()
        )
        rows = [row for row in rows if int(row.frame_id) in allowed]
    measurements = [
        row
        for row in rows
        if not catalog_name or (row.flags or {}).get("catalog_name") == catalog_name
    ]
    object_ids = {int(row.object_id) for row in measurements}
    mag_cals = [
        float(row.mag_cal)
        for row in measurements
        if row.mag_cal is not None and _is_finite(row.mag_cal)
    ]
    mag_errs = [
        float(row.mag_err)
        for row in measurements
        if row.mag_err is not None and _is_finite(row.mag_err)
    ]
    return {
        "n_measurements": len(measurements),
        "n_objects": len(object_ids),
        "n_mag_cal": len(mag_cals),
        "mag_cal_min": min(mag_cals) if mag_cals else None,
        "mag_cal_max": max(mag_cals) if mag_cals else None,
        "mag_err_median": _median(mag_errs),
        "materialization_source": view.source,
    }

def _object_qc_summary(
    session: Session,
    *,
    calibration_version_id: int,
    source_calibration_version_id: int | None = None,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None = None,
    algorithm: str,
) -> dict[str, Any]:
    stmt = select(ObjectQc).where(ObjectQc.calibration_version_id == calibration_version_id)
    if catalog_name:
        stmt = stmt.where(ObjectQc.catalog_name == catalog_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(ObjectQc.instrument_unit_id == instrument_unit_id)
    if algorithm:
        stmt = stmt.where(ObjectQc.algorithm == algorithm)
    qcs = list(session.scalars(stmt).all())

    source_qc_by_object: dict[int, ObjectQc] = {}
    if source_calibration_version_id is not None and int(source_calibration_version_id) != int(calibration_version_id):
        source_stmt = select(ObjectQc).where(ObjectQc.calibration_version_id == int(source_calibration_version_id))
        if catalog_name:
            source_stmt = source_stmt.where(ObjectQc.catalog_name == catalog_name)
        if instrument_unit_id is not None:
            source_stmt = source_stmt.where(ObjectQc.instrument_unit_id == instrument_unit_id)
        if algorithm:
            source_stmt = source_stmt.where(ObjectQc.algorithm == algorithm)
        for source_qc in session.scalars(source_stmt).all():
            if _has_aperture_stability(source_qc):
                source_qc_by_object[int(source_qc.object_id)] = source_qc

    qcs_by_object = {int(qc.object_id): qc for qc in qcs}
    count_object_ids = set(qcs_by_object) | set(source_qc_by_object)
    counts: Counter[str] = Counter()
    inherited_aperture = 0
    source_only_aperture = 0
    for object_id in count_object_ids:
        qc = qcs_by_object.get(object_id)
        source_qc = source_qc_by_object.get(object_id)
        flags = dict((qc.flags or {}) if qc is not None else {})
        if source_qc is not None and (qc is None or not _has_aperture_stability(qc)):
            if _preserve_aperture_stability_flags(source_qc, flags):
                inherited_aperture += 1
                if qc is None:
                    source_only_aperture += 1
        for key, value in flags.items():
            if value:
                counts[str(key)] += 1
    return {
        "n_object_qc": len(qcs),
        "n_source_aperture_stability_qc": len(source_qc_by_object),
        "n_inherited_aperture_stability_qc": inherited_aperture,
        "n_source_only_aperture_stability_qc": source_only_aperture,
        "flag_counts": dict(sorted(counts.items())),
        "nearest_neighbor_arcsec_median": _median([qc.nearest_neighbor_arcsec for qc in qcs]),
        "edge_distance_pix_min_median": _median([qc.edge_distance_pix_min for qc in qcs]),
        "mag_rms_median": _median([qc.mag_rms for qc in qcs]),
        "mag_mad_median": _median([qc.mag_mad for qc in qcs]),
    }


def _summary_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = report.get("metadata", {})
    field_series = metadata.get("field_series") or {}
    quality_policy = metadata.get("quality_policy") or {}
    triage = report.get("candidate_triage", {})
    frames = report.get("frames", {})
    frame_qc = report.get("frame_qc", {})
    meas = report.get("measurements", {})
    objects = report.get("objects", {})
    object_qc = report.get("object_qc", {})
    blends = report.get("blend_diagnostics", {})
    warnings = report.get("warnings", [])
    rows = [
        {"section": "selection", "metric": "frame_selection_mode", "value": metadata.get("frame_selection_mode")},
        {"section": "selection", "metric": "series_id", "value": field_series.get("series_id")},
        {"section": "selection", "metric": "field_name", "value": field_series.get("field_name")},
        {"section": "selection", "metric": "selected_frame_ids_count", "value": metadata.get("selected_frame_ids_count")},
        {"section": "quality_policy", "metric": "require_detection_support_effective", "value": quality_policy.get("require_detection_support_effective")},
        {"section": "frames", "metric": "n_frames", "value": frames.get("n_frames")},
        {"section": "frames", "metric": "mjd_range", "value": _range_text(frames.get("mjd_min"), frames.get("mjd_max"))},
        {"section": "frame_qc", "metric": "n_frame_qc", "value": frame_qc.get("n_frame_qc")},
        {"section": "frame_qc", "metric": "astrom_rms_arcsec_median", "value": _round(frame_qc.get("astrom_rms_arcsec_median"), 4)},
        {"section": "measurements", "metric": "n_measurements", "value": meas.get("n_measurements")},
        {"section": "measurements", "metric": "n_objects", "value": meas.get("n_objects")},
        {"section": "objects", "metric": "objects_used_for_scatter", "value": objects.get("objects_used_for_scatter")},
        {"section": "objects", "metric": "mag_rms_median", "value": _round(objects.get("mag_rms_median"), 5)},
        {"section": "object_qc", "metric": "n_object_qc", "value": object_qc.get("n_object_qc")},
        {"section": "object_qc", "metric": "n_source_aperture_stability_qc", "value": object_qc.get("n_source_aperture_stability_qc")},
        {"section": "object_qc", "metric": "n_inherited_aperture_stability_qc", "value": object_qc.get("n_inherited_aperture_stability_qc")},
        {"section": "candidate_triage", "metric": "raw_candidates", "value": triage.get("raw_candidates")},
        {"section": "candidate_triage", "metric": "rms_excess_candidates", "value": triage.get("rms_excess_candidates")},
        {"section": "candidate_triage", "metric": "intrinsic_variability_candidates", "value": triage.get("intrinsic_variability_candidates")},
        {"section": "candidate_triage", "metric": "low_amplitude_intrinsic_candidates", "value": triage.get("low_amplitude_intrinsic_candidates")},
        {"section": "candidate_triage", "metric": "intrinsic_only_candidates", "value": triage.get("intrinsic_only_candidates")},
        {"section": "candidate_triage", "metric": "low_amplitude_only_candidates", "value": triage.get("low_amplitude_only_candidates")},
        {"section": "candidate_triage", "metric": "target_candidates", "value": triage.get("target_candidates")},
        {"section": "candidate_triage", "metric": "forced_target_candidates", "value": triage.get("forced_target_candidates")},
        {"section": "candidate_triage", "metric": "accepted", "value": triage.get("accepted")},
        {"section": "candidate_triage", "metric": "borderline", "value": triage.get("borderline")},
        {"section": "candidate_triage", "metric": "borderline_low_amplitude", "value": triage.get("borderline_low_amplitude")},
        {"section": "candidate_triage", "metric": "near_threshold", "value": triage.get("near_threshold")},
        {"section": "candidate_triage", "metric": "near_threshold_low_amplitude", "value": triage.get("near_threshold_low_amplitude")},
        {"section": "candidate_triage", "metric": "rejected", "value": triage.get("rejected")},
        {"section": "candidate_triage", "metric": "rejection_counts", "value": _counts_text(triage.get("rejection_counts", {}))},
        {"section": "blend", "metric": "possible_blend", "value": blends.get("possible_blend")},
        {"section": "blend", "metric": "dynamic_crowded", "value": blends.get("dynamic_crowded")},
        {"section": "blend", "metric": "dynamic_blend_risk", "value": blends.get("dynamic_blend_risk")},
        {"section": "blend", "metric": "aperture_unstable", "value": blends.get("aperture_unstable")},
        {"section": "warnings", "metric": "count", "value": len(warnings)},
    ]
    for idx, warning in enumerate(warnings, start=1):
        rows.append({"section": "warnings", "metric": f"warning_{idx}", "value": warning})
    return rows


def _build_warnings(*, frames: dict, measurements: dict, object_qc: dict, triage_summary: dict) -> list[str]:
    warnings: list[str] = []
    if frames.get("n_frames", 0) == 0:
        warnings.append("No frames matched the selected instrument_unit_id.")
    if measurements.get("n_measurements", 0) == 0:
        warnings.append("No measurements matched the selected calibration/catalog/instrument filters.")
    if object_qc.get("n_object_qc", 0) == 0:
        warnings.append("No object_qc rows matched the selected filters; run compute-object-qc first.")
    if triage_summary.get("raw_candidates", 0) > 0 and triage_summary.get("accepted", 0) == 0:
        warnings.append("High-RMS raw candidates exist, but all are rejected by QC filters.")
    return warnings


def _write_csv(rows: list[dict[str, Any]], path: str | Path, columns: list[str] | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        keys: list[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        columns = keys
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_json_safe(row))


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _median(values) -> float | None:
    vals = []
    for value in values:
        if value is None:
            continue
        try:
            x = float(value)
        except Exception:
            continue
        if math.isfinite(x):
            vals.append(x)
    vals.sort()
    if not vals:
        return None
    n = len(vals)
    if n % 2:
        return float(vals[n // 2])
    return float((vals[n // 2 - 1] + vals[n // 2]) / 2.0)


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _round(value, ndigits: int):
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return value


def _range_text(a, b) -> str | None:
    if a is None or b is None:
        return None
    return f"{_round(a, 6)}..{_round(b, 6)}"


def _counts_text(counts: dict[str, Any]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
