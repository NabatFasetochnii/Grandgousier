from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, bindparam, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from grandgousier.archive.observation_selection import select_persisted_science_frames


@dataclass
class BuildFieldSeriesResult:
    selected_frames: int = 0
    skipped_no_center: int = 0
    skipped_already_assigned: int = 0
    created_fields: int = 0
    reused_fields: int = 0
    created_series: int = 0
    linked_frames: int = 0
    updated_fields: int = 0
    updated_series: int = 0
    open_series_ids: list[int] = field(default_factory=list)
    dry_run: bool = False
    frame_selection_diagnostics: dict[str, Any] = field(default_factory=dict)
    observation_selection: dict[str, Any] = field(default_factory=dict)
    observation_selection_rows: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "selected_frames": self.selected_frames,
            "skipped_no_center": self.skipped_no_center,
            "skipped_already_assigned": self.skipped_already_assigned,
            "created_fields": self.created_fields,
            "reused_fields": self.reused_fields,
            "created_series": self.created_series,
            "linked_frames": self.linked_frames,
            "updated_fields": self.updated_fields,
            "updated_series": self.updated_series,
            "open_series_ids": self.open_series_ids,
            "dry_run": self.dry_run,
            "frame_selection_diagnostics": self.frame_selection_diagnostics,
            "observation_selection": self.observation_selection,
        }


def build_field_series(
    session: Session,
    *,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    max_field_separation_arcmin: float = 30.0,
    max_series_gap_minutes: float = 60.0,
    only_unassigned: bool = True,
    only_science: bool = True,
    min_frames_per_series: int = 1,
    grouping_method: str = "center_time_v1",
    finalize_tail: bool = True,
    dry_run: bool = False,
) -> BuildFieldSeriesResult:
    """Assign frames to persistent sky fields and observing series.

    A field is a sky area. A series is a contiguous observing block of one field
    with one instrument_unit/filter. This function is deliberately conservative:
    it uses WCS/image-center coordinates and time gaps, not object-name alone.
    """

    if frame_ids is not None and not frame_ids:
        raise ValueError("explicit frame_ids selection must not be empty")
    if max_field_separation_arcmin <= 0:
        raise ValueError("max_field_separation_arcmin must be positive")
    if max_series_gap_minutes <= 0:
        raise ValueError("max_series_gap_minutes must be positive")
    if min_frames_per_series < 1:
        raise ValueError("min_frames_per_series must be >= 1")

    frames = _load_candidate_frames(
        session,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        only_unassigned=only_unassigned,
        only_science=only_science,
    )
    result = BuildFieldSeriesResult(selected_frames=len(frames), dry_run=dry_run)
    result.frame_selection_diagnostics = _load_frame_selection_diagnostics(
        session,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        only_unassigned=only_unassigned,
    )

    # Existing field-series membership always wins. Preserve the historical
    # skipped_already_assigned counter before applying the P4c guard to the
    # remaining unassigned candidates.
    result.skipped_already_assigned = sum(1 for frame in frames if frame.get("already_assigned"))
    unassigned_frames = [frame for frame in frames if not frame.get("already_assigned")]
    guard_candidates = frames if not only_unassigned else unassigned_frames
    observation_selection = select_persisted_science_frames(
        session,
        [int(frame["frame_id"]) for frame in guard_candidates],
        allow_reassign_existing_frame=not only_unassigned,
        lock_observations=not dry_run,
    )
    result.observation_selection = dict(observation_selection.summary)
    result.observation_selection_rows = list(observation_selection.rows)
    selected_ids = set(observation_selection.selected_frame_ids)
    frames = [frame for frame in guard_candidates if int(frame["frame_id"]) in selected_ids]

    existing_fields = _load_fields(session)
    field_cache = {int(f["field_id"]): dict(f) for f in existing_fields}

    # First pass: assign each frame to a field, creating in-memory/DB fields as needed.
    frames_by_field: dict[int, list[dict[str, Any]]] = {}
    synthetic_field_id = -1
    for frame in frames:
        if frame.get("ra_center_deg") is None or frame.get("dec_center_deg") is None:
            result.skipped_no_center += 1
            continue
        field_row, created = _find_or_create_field(
            session,
            frame=frame,
            field_cache=field_cache,
            max_sep_arcmin=max_field_separation_arcmin,
            synthetic_field_id=synthetic_field_id,
            dry_run=dry_run,
        )
        if created:
            result.created_fields += 1
            if dry_run:
                synthetic_field_id -= 1
        else:
            result.reused_fields += 1
        field_id = int(field_row["field_id"])
        field_cache[field_id] = field_row
        frames_by_field.setdefault(field_id, []).append(frame)

    # Second pass: within each field split by instrument/filter/time gap.
    for field_id, field_frames in sorted(frames_by_field.items(), key=lambda kv: kv[0]):
        field_frames.sort(key=_frame_sort_key)
        buckets: dict[tuple[int | None, str | None], list[dict[str, Any]]] = {}
        for frame in field_frames:
            key = (frame.get("instrument_unit_id"), frame.get("filter_name"))
            buckets.setdefault(key, []).append(frame)
        for (_iu, _filter_name), bucket in buckets.items():
            current: list[dict[str, Any]] = []
            groups: list[list[dict[str, Any]]] = []
            prev = None
            for frame in bucket:
                if prev is not None and _gap_minutes(prev, frame) > max_series_gap_minutes:
                    groups.append(current)
                    current = []
                current.append(frame)
                prev = frame
            if current:
                groups.append(current)
            for group_index, group_frames in enumerate(groups):
                is_tail = group_index == len(groups) - 1
                _create_series_group(
                    session,
                    result=result,
                    field_id=field_id,
                    frames=group_frames,
                    grouping_method=grouping_method,
                    min_frames=min_frames_per_series,
                    max_gap_minutes=max_series_gap_minutes,
                    finalize_group=(not is_tail or finalize_tail),
                    persist_subminimum_open=(is_tail and not finalize_tail),
                    dry_run=dry_run,
                )

    if not dry_run:
        _refresh_field_summaries(
            session,
            field_ids={int(row["field_id"]) for row in result.rows if row["series_id"] is not None},
        )
        session.commit()
    return result


def load_series_selection(session: Session, series_id: int) -> dict[str, Any]:
    """Return metadata and frame_ids for one field_series row."""

    row = session.execute(
        text(
            """
            SELECT s.*, f.field_name, f.field_uid
            FROM field_series s
            JOIN fields f ON f.field_id = s.field_id
            WHERE s.series_id = :series_id
            """
        ),
        {"series_id": int(series_id)},
    ).mappings().first()
    if row is None:
        raise ValueError(f"field_series row not found: series_id={series_id}")
    frame_rows = session.execute(
        text(
            """
            SELECT sf.frame_id
            FROM field_series_frames sf
            JOIN frames fr ON fr.frame_id = sf.frame_id
            JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
            WHERE sf.series_id = :series_id
            ORDER BY sf.order_index, fr.mjd, fr.date_obs, fr.frame_id
            """
        ),
        {"series_id": int(series_id)},
    ).all()
    frame_ids = [int(r[0]) for r in frame_rows]
    if not frame_ids:
        raise ValueError(f"field_series has no frames: series_id={series_id}")
    out = dict(row)
    out["frame_ids"] = frame_ids
    return _json_safe(out)


def finalize_open_series(session: Session, series_id: int) -> dict[str, Any]:
    """Finalize one OPEN series without invoking Stage 2."""

    row = session.execute(
        text(
            """
            SELECT series_id, field_id, status, n_frames, metadata_json
            FROM field_series
            WHERE series_id = :series_id
            FOR UPDATE
            """
        ),
        {"series_id": int(series_id)},
    ).mappings().first()
    if row is None:
        raise ValueError(f"field_series row not found: series_id={series_id}")
    state = dict(row)
    status = str(state["status"])
    if status != "OPEN":
        return {
            "series_id": int(series_id),
            "status": status,
            "finalized": False,
            "eligible": status == "READY",
            "reason": "NOT_OPEN",
        }

    metadata = _series_metadata(state.get("metadata_json"))
    minimum = metadata.get("min_frames_per_series")
    if minimum is None or int(minimum) < 1:
        raise ValueError(
            f"OPEN series {series_id} has no valid persisted minimum-frame threshold"
        )
    member_count = int(
        session.execute(
            text(
                "SELECT COUNT(*) FROM field_series_frames WHERE series_id = :series_id"
            ),
            {"series_id": int(series_id)},
        ).scalar_one()
    )
    if member_count < int(minimum):
        return {
            "series_id": int(series_id),
            "status": "OPEN",
            "n_frames": member_count,
            "min_frames_per_series": int(minimum),
            "finalized": False,
            "eligible": False,
            "reason": "BELOW_MINIMUM",
        }

    session.execute(
        text(
            "UPDATE field_series SET status = 'READY', updated_at = now() "
            "WHERE series_id = :series_id AND status = 'OPEN'"
        ),
        {"series_id": int(series_id)},
    )
    _refresh_field_processing_status(session, int(state["field_id"]))
    session.commit()
    return {
        "series_id": int(series_id),
        "status": "READY",
        "n_frames": member_count,
        "min_frames_per_series": int(minimum),
        "finalized": True,
        "eligible": True,
        "reason": None,
    }


def list_fields(
    session: Session,
    *,
    limit: int = 50,
    status: str | None = None,
    name_like: str | None = None,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"limit": int(limit)}
    if status:
        where.append("f.status = :status")
        params["status"] = status
    if name_like:
        where.append("COALESCE(f.field_name, '') ILIKE :name_like")
        params["name_like"] = f"%{name_like}%"
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = session.execute(
        text(
            f"""
            SELECT
                f.field_id,
                f.field_uid,
                f.field_name,
                f.primary_object_name,
                f.ra_center_deg,
                f.dec_center_deg,
                f.radius_arcmin,
                f.status,
                COUNT(DISTINCT s.series_id)::int AS n_series,
                COUNT(DISTINCT sf.frame_id)::int AS n_frames,
                MIN(s.mjd_start) AS mjd_start,
                MAX(s.mjd_end) AS mjd_end,
                MIN(s.time_start) AS time_start,
                MAX(s.time_end) AS time_end
            FROM fields f
            LEFT JOIN field_series s ON s.field_id = f.field_id
            LEFT JOIN field_series_frames sf ON sf.series_id = s.series_id
            {where_sql}
            GROUP BY f.field_id
            ORDER BY f.field_id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [_json_safe(dict(r)) for r in rows]


def list_field_series(
    session: Session,
    *,
    field_id: int | None = None,
    series_id: int | None = None,
    instrument_unit_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return field-series rows enriched with the latest pipeline-run state."""

    where = []
    params: dict[str, Any] = {"limit": int(limit)}
    if field_id is not None:
        where.append("s.field_id = :field_id")
        params["field_id"] = int(field_id)
    if series_id is not None:
        where.append("s.series_id = :series_id")
        params["series_id"] = int(series_id)
    if instrument_unit_id is not None:
        where.append("s.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if status:
        where.append("s.status = :status")
        params["status"] = status
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = session.execute(
        text(
            f"""
            WITH latest_run AS (
                SELECT DISTINCT ON (series_id)
                    pipeline_run_id,
                    series_id,
                    status AS pipeline_status,
                    calibration_prefix,
                    started_at,
                    finished_at,
                    summary_json,
                    log_file,
                    error_log_file
                FROM pipeline_runs
                WHERE series_id IS NOT NULL
                ORDER BY series_id, pipeline_run_id DESC
            ),
            latest_report_step AS (
                SELECT DISTINCT ON (pipeline_run_id)
                    pipeline_run_id,
                    result_json
                FROM pipeline_run_steps
                WHERE step_name = 'field_qc_report'
                ORDER BY pipeline_run_id, pipeline_run_step_id DESC
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
                s.ra_center_deg,
                s.dec_center_deg,
                s.n_frames,
                s.grouping_method,
                s.status,
                lr.pipeline_run_id AS last_pipeline_run_id,
                lr.pipeline_status AS last_pipeline_status,
                lr.calibration_prefix AS last_calibration_prefix,
                lr.started_at AS last_started_at,
                lr.finished_at AS last_finished_at,
                lr.summary_json #>> '{{calibration_names,raw}}' AS last_raw_calibration,
                lr.summary_json #>> '{{calibration_names,aperture_zp}}' AS last_aperture_zp_calibration,
                lr.summary_json #>> '{{calibration_names,ensemble}}' AS last_ensemble_calibration,
                lr.summary_json #>> '{{outputs,field_qc_report_json}}' AS field_qc_report_json,
                lr.log_file,
                lr.error_log_file,
                rs.result_json #>> '{{candidate_triage,raw_candidates}}' AS raw_candidates,
                rs.result_json #>> '{{candidate_triage,accepted}}' AS accepted,
                rs.result_json #>> '{{candidate_triage,rejected}}' AS rejected,
                s.updated_at
            FROM field_series s
            JOIN fields f ON f.field_id = s.field_id
            LEFT JOIN latest_run lr ON lr.series_id = s.series_id
            LEFT JOIN latest_report_step rs ON rs.pipeline_run_id = lr.pipeline_run_id
            {where_sql}
            ORDER BY s.time_start DESC NULLS LAST, s.series_id DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [_json_safe(dict(r)) for r in rows]


def list_series_for_archive_processing(
    session: Session,
    *,
    status: str = "READY",
    field_id: int | None = None,
    instrument_unit_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return series rows selected for batch archive processing."""

    rows = list_field_series(
        session,
        field_id=field_id,
        instrument_unit_id=instrument_unit_id,
        status=status,
        limit=int(limit) if limit is not None else 1000000,
    )
    rows.sort(key=lambda row: (row.get("time_start") or "", int(row.get("series_id") or 0)))
    return rows


def mark_series_pipeline_started(
    session: Session,
    *,
    series_id: int,
    pipeline_run_id: int | None,
    run_uid: str | None = None,
) -> None:
    """Mark a field_series row as RUNNING and store the latest run pointer."""

    if pipeline_run_id is None:
        return
    field_id = _update_series_status(
        session,
        series_id=int(series_id),
        status="RUNNING",
        metadata_patch={
            "last_pipeline_run_id": int(pipeline_run_id),
            "last_run_uid": run_uid,
            "last_pipeline_status": "RUNNING",
            "last_error_message": None,
        },
    )
    if field_id is not None:
        _refresh_field_processing_status(session, field_id)
    session.commit()


def mark_series_pipeline_result(
    session: Session,
    *,
    series_id: int,
    pipeline_run_id: int | None,
    pipeline_status: str,
    run_uid: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update field_series/fields after a pipeline run finishes."""

    if pipeline_run_id is None:
        return
    series_status = _series_status_from_pipeline_status(pipeline_status)
    field_id = _update_series_status(
        session,
        series_id=int(series_id),
        status=series_status,
        metadata_patch={
            "last_pipeline_run_id": int(pipeline_run_id),
            "last_run_uid": run_uid,
            "last_pipeline_status": pipeline_status,
            "last_error_message": error_message,
        },
    )
    if field_id is not None:
        _refresh_field_processing_status(session, field_id)
    session.commit()


def list_series_frames(session: Session, *, series_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT
                sf.series_id,
                sf.order_index,
                fr.frame_id,
                fl.locator AS raw_path,
                fr.object_name,
                fr.date_obs,
                fr.mjd,
                fr.instrument_unit_id,
                fr.instrument,
                fr.site,
                fr.tube,
                fr.camera,
                fr.filter_name,
                fr.ra_center_deg,
                fr.dec_center_deg,
                fr.wcs_status,
                fr.processing_status
            FROM field_series_frames sf
            JOIN frames fr ON fr.frame_id = sf.frame_id
            JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
            WHERE sf.series_id = :series_id
            ORDER BY sf.order_index, fr.mjd, fr.date_obs, fr.frame_id
            LIMIT :limit
            """
        ),
        {"series_id": int(series_id), "limit": int(limit)},
    ).mappings().all()
    return [_json_safe(dict(r)) for r in rows]



def _series_status_from_pipeline_status(pipeline_status: str | None) -> str:
    status = (pipeline_status or "UNKNOWN").upper()
    if status == "DONE":
        return "PROCESSED"
    if status.startswith("FAILED"):
        return "FAILED"
    if status in {"RUNNING", "READY", "PROCESSED"}:
        return status
    return status


def _update_series_status(
    session: Session,
    *,
    series_id: int,
    status: str,
    metadata_patch: dict[str, Any] | None = None,
) -> int | None:
    patch_json = json.dumps(_json_safe(metadata_patch or {}), ensure_ascii=False)
    row = session.execute(
        text(
            """
            UPDATE field_series
            SET status = :status,
                metadata_json = COALESCE(metadata_json, '{}'::jsonb) || CAST(:metadata_patch AS JSONB),
                updated_at = now()
            WHERE series_id = :series_id
            RETURNING field_id
            """
        ),
        {"series_id": int(series_id), "status": status, "metadata_patch": patch_json},
    ).first()
    return int(row[0]) if row is not None and row[0] is not None else None


def _refresh_field_processing_status(session: Session, field_id: int) -> None:
    rows = session.execute(
        text(
            """
            SELECT status, COUNT(*)::int AS n
            FROM field_series
            WHERE field_id = :field_id
            GROUP BY status
            """
        ),
        {"field_id": int(field_id)},
    ).mappings().all()
    counts = {str(r["status"]): int(r["n"]) for r in rows}
    total = sum(counts.values())
    if total <= 0:
        new_status = "READY"
    elif counts.get("RUNNING", 0) > 0:
        new_status = "RUNNING"
    elif counts.get("FAILED", 0) > 0:
        if counts.get("PROCESSED", 0) == 0 and counts.get("READY", 0) == 0:
            new_status = "FAILED"
        else:
            new_status = "PARTIAL_FAILED"
    elif counts.get("READY", 0) > 0:
        if counts.get("PROCESSED", 0) > 0:
            new_status = "PARTIAL_PROCESSED"
        else:
            new_status = "READY"
    elif counts.get("PROCESSED", 0) == total:
        new_status = "PROCESSED"
    else:
        new_status = "READY"
    session.execute(
        text("UPDATE fields SET status = :status, updated_at = now() WHERE field_id = :field_id"),
        {"field_id": int(field_id), "status": new_status},
    )


def _load_candidate_frames(
    session: Session,
    *,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
    only_unassigned: bool,
    only_science: bool,
) -> list[dict[str, Any]]:
    where = ["fr.ra_center_deg IS NOT NULL", "fr.dec_center_deg IS NOT NULL"]
    params: dict[str, Any] = {}
    if instrument_unit_id is not None:
        where.append("fr.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if frame_ids:
        where.append("fr.frame_id IN :frame_ids")
        params["frame_ids"] = [int(v) for v in frame_ids]
    if only_science:
        # In early Grandgousier imports many real science frames were stored as
        # image_type='unknown' because the FITS header did not provide a clean
        # IMAGETYP/FRAME value.  Treat unknown/NULL frames as science-like for
        # archive grouping, while still excluding known calibration and focus/test
        # frames.  Users can pass --include-non-science to disable even this guard.
        where.append("(fr.image_type IS NULL OR LOWER(CAST(fr.image_type AS TEXT)) IN ('science', 'unknown'))")
    if only_unassigned:
        where.append("sf.frame_id IS NULL")
    stmt = text(
        f"""
        SELECT
            fr.frame_id,
            fr.instrument_unit_id,
            fr.instrument,
            fr.site,
            fr.tube,
            fr.camera,
            fr.filter_name,
            fr.object_name,
            fr.ra_center_deg,
            fr.dec_center_deg,
            fr.date_obs,
            fr.mjd,
            fr.exptime,
            fr.wcs_status,
            CASE WHEN sf.frame_id IS NULL THEN false ELSE true END AS already_assigned
        FROM frames fr
        LEFT JOIN field_series_frames sf ON sf.frame_id = fr.frame_id
        WHERE {' AND '.join(where)}
        ORDER BY fr.mjd NULLS LAST, fr.date_obs NULLS LAST, fr.frame_id
        """
    )
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    rows = session.execute(stmt, params).mappings().all()
    return [_json_safe(dict(r)) for r in rows]


def _load_frame_selection_diagnostics(
    session: Session,
    *,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
    only_unassigned: bool,
) -> dict[str, Any]:
    """Return compact diagnostics for build-field-series frame selection.

    This is intentionally independent of the science-like filter so zero-row
    selections can be diagnosed from CLI JSON output and log messages.
    """

    where = []
    params: dict[str, Any] = {}
    if instrument_unit_id is not None:
        where.append("fr.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if frame_ids:
        where.append("fr.frame_id IN :frame_ids")
        params["frame_ids"] = [int(v) for v in frame_ids]
    if only_unassigned:
        where.append("sf.frame_id IS NULL")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    stmt = text(
        f"""
        SELECT
            COALESCE(CAST(fr.image_type AS TEXT), '<NULL>') AS image_type,
            CAST(COUNT(*) AS INTEGER) AS n_frames,
            CAST(SUM(CASE WHEN fr.ra_center_deg IS NOT NULL AND fr.dec_center_deg IS NOT NULL THEN 1 ELSE 0 END) AS INTEGER) AS n_with_center,
            CAST(SUM(CASE WHEN fr.ra_center_deg IS NULL OR fr.dec_center_deg IS NULL THEN 1 ELSE 0 END) AS INTEGER) AS n_without_center
        FROM frames fr
        LEFT JOIN field_series_frames sf ON sf.frame_id = fr.frame_id
        {where_sql}
        GROUP BY COALESCE(CAST(fr.image_type AS TEXT), '<NULL>')
        ORDER BY image_type
        """
    )
    if frame_ids:
        stmt = stmt.bindparams(bindparam("frame_ids", expanding=True))
    rows = session.execute(stmt, params).mappings().all()
    out = {str(r["image_type"]): {"n_frames": int(r["n_frames"]), "n_with_center": int(r["n_with_center"]), "n_without_center": int(r["n_without_center"])} for r in rows}
    return out


def _load_fields(session: Session) -> list[dict[str, Any]]:
    try:
        rows = session.execute(
            text(
                """
                SELECT field_id, field_uid, field_name, primary_object_name,
                       ra_center_deg, dec_center_deg, radius_arcmin, status
                FROM fields
                ORDER BY field_id
                """
            )
        ).mappings().all()
    except ProgrammingError as exc:
        session.rollback()
        raise RuntimeError(
            "fields/field_series tables are not available. Apply scripts/migrate_mvp5_26_fields_series.sql first."
        ) from exc
    return [_json_safe(dict(r)) for r in rows]


def _find_or_create_field(
    session: Session,
    *,
    frame: dict[str, Any],
    field_cache: dict[int, dict[str, Any]],
    max_sep_arcmin: float,
    synthetic_field_id: int,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    ra = float(frame["ra_center_deg"])
    dec = float(frame["dec_center_deg"])
    nearest = None
    nearest_sep = None
    for field in field_cache.values():
        sep_arcmin = _angular_sep_arcsec(ra, dec, float(field["ra_center_deg"]), float(field["dec_center_deg"])) / 60.0
        if nearest_sep is None or sep_arcmin < nearest_sep:
            nearest = field
            nearest_sep = sep_arcmin
    if nearest is not None and nearest_sep is not None and nearest_sep <= max_sep_arcmin:
        return nearest, False

    object_name = _clean_name(frame.get("object_name"))
    field_name = object_name or f"field_{ra:.5f}_{dec:.5f}"
    field_uid = _make_field_uid(field_name=field_name, ra_deg=ra, dec_deg=dec)
    metadata = {
        "created_from_frame_id": frame.get("frame_id"),
        "grouping_note": "WCS center field assignment",
    }
    if dry_run:
        row = {
            "field_id": synthetic_field_id,
            "field_uid": field_uid,
            "field_name": field_name,
            "primary_object_name": object_name,
            "ra_center_deg": ra,
            "dec_center_deg": dec,
            "radius_arcmin": max_sep_arcmin,
            "status": "READY",
            "metadata_json": metadata,
        }
        return row, True

    field_id = session.execute(
        text(
            """
            INSERT INTO fields
                (field_uid, field_name, primary_object_name, ra_center_deg, dec_center_deg,
                 radius_arcmin, status, metadata_json, created_at, updated_at)
            VALUES
                (:field_uid, :field_name, :primary_object_name, :ra_center_deg, :dec_center_deg,
                 :radius_arcmin, 'READY', CAST(:metadata_json AS JSONB), now(), now())
            ON CONFLICT (field_uid) DO UPDATE
            SET updated_at = now()
            RETURNING field_id
            """
        ),
        {
            "field_uid": field_uid,
            "field_name": field_name,
            "primary_object_name": object_name,
            "ra_center_deg": ra,
            "dec_center_deg": dec,
            "radius_arcmin": max_sep_arcmin,
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
    ).scalar_one()
    session.commit()
    row = {
        "field_id": int(field_id),
        "field_uid": field_uid,
        "field_name": field_name,
        "primary_object_name": object_name,
        "ra_center_deg": ra,
        "dec_center_deg": dec,
        "radius_arcmin": max_sep_arcmin,
        "status": "READY",
        "metadata_json": metadata,
    }
    return row, True


def _create_series_group(
    session: Session,
    *,
    result: BuildFieldSeriesResult,
    field_id: int,
    frames: list[dict[str, Any]],
    grouping_method: str,
    min_frames: int,
    max_gap_minutes: float,
    finalize_group: bool,
    persist_subminimum_open: bool,
    dry_run: bool,
) -> None:
    if not frames:
        return
    frames = sorted(frames, key=_frame_sort_key)
    input_frame_ids = {int(frame["frame_id"]) for frame in frames}
    existing_state: dict[str, Any] | None = None
    existing_members: list[dict[str, Any]] = []

    assigned_series_ids = _assigned_series_ids_for_frames(session, input_frame_ids)
    if len(assigned_series_ids) > 1:
        raise ValueError(
            "candidate group spans multiple existing field_series memberships; refusing to merge"
        )
    if assigned_series_ids:
        existing_state = _load_series_state(session, assigned_series_ids[0])
        if existing_state is None:
            raise RuntimeError("assigned field_series membership has no parent series")
        existing_members = _load_series_members(session, int(existing_state["series_id"]))
        existing_member_ids = {int(member["frame_id"]) for member in existing_members}
        if str(existing_state["status"]) != "OPEN":
            if existing_member_ids == input_frame_ids:
                _append_series_result_row(
                    result,
                    series_state=existing_state,
                    frames=existing_members,
                    dry_run=dry_run,
                )
                return
            raise ValueError(
                "candidate group would extend a finalized field_series; refusing to mutate it"
            )
        new_members = [
            frame for frame in frames if int(frame["frame_id"]) not in existing_member_ids
        ]
        if new_members:
            existing_tail = max(existing_members, key=_frame_sort_key)
            new_head = min(new_members, key=_frame_sort_key)
            if _frame_sort_key(new_head) <= _frame_sort_key(existing_tail):
                raise ValueError(
                    "new Frames cannot be inserted before or inside an OPEN series tail"
                )
            if _gap_minutes(existing_tail, new_head) > max_gap_minutes:
                raise ValueError(
                    "new Frames exceed the configured gap from the OPEN series tail"
                )
        _validate_open_series_key(
            existing_state,
            field_id=field_id,
            instrument_unit_id=frames[0].get("instrument_unit_id"),
            filter_name=frames[0].get("filter_name"),
            grouping_method=grouping_method,
        )
    else:
        compatible_open = []
        for state, members in _load_open_series_for_field(session, field_id, grouping_method):
            if (
                state.get("instrument_unit_id") != frames[0].get("instrument_unit_id")
                or state.get("filter_name") != frames[0].get("filter_name")
                or not members
            ):
                continue
            _validate_open_series_metadata(
                state,
                min_frames=min_frames,
                max_gap_minutes=max_gap_minutes,
            )
            compatible_open.append((state, members))

        matching_open = []
        for state, members in compatible_open:
            if _series_groups_are_contiguous(members, frames, max_gap_minutes):
                matching_open.append((state, members))
        if len(matching_open) > 1:
            raise ValueError(
                "candidate group is contiguous with multiple OPEN series; refusing ambiguous merge"
            )
        if matching_open:
            existing_state, existing_members = matching_open[0]
        else:
            # A later group proves the prior tail ended at the configured gap.
            # A sub-minimum tail remains OPEN/ineligible rather than bypassing
            # the existing minimum-frame acceptance rule.
            for state, members in compatible_open:
                if _gap_minutes(members[-1], frames[0]) <= max_gap_minutes:
                    continue
                if _frame_sort_key(frames[0]) <= _frame_sort_key(members[-1]):
                    continue
                _close_open_series_at_boundary(
                    session,
                    result=result,
                    state=state,
                    members=members,
                    dry_run=dry_run,
                )

    if existing_state is not None:
        metadata = _validate_open_series_metadata(
            existing_state,
            min_frames=min_frames,
            max_gap_minutes=max_gap_minutes,
        )
        effective_minimum = int(metadata["min_frames_per_series"])
        merged_by_id = {int(member["frame_id"]): member for member in existing_members}
        merged_by_id.update({int(frame["frame_id"]): frame for frame in frames})
        frames = sorted(merged_by_id.values(), key=_frame_sort_key)
    else:
        metadata = {}
        effective_minimum = int(min_frames)

    if len(frames) < effective_minimum:
        if existing_state is None and not persist_subminimum_open:
            return
        target_status = "OPEN"
    else:
        target_status = "READY" if finalize_group else "OPEN"

    first = frames[0]
    last = frames[-1]
    ra_vals = [float(frame["ra_center_deg"]) for frame in frames if frame.get("ra_center_deg") is not None]
    dec_vals = [float(frame["dec_center_deg"]) for frame in frames if frame.get("dec_center_deg") is not None]
    mjds = [float(frame["mjd"]) for frame in frames if frame.get("mjd") is not None]
    times = [
        value
        for frame in frames
        if (value := _coerce_datetime(frame.get("date_obs"))) is not None
    ]
    object_names = [_clean_name(frame.get("object_name")) for frame in frames if _clean_name(frame.get("object_name"))]
    primary_object = object_names[0] if object_names else None
    anchor_frame_id = metadata.get("uid_anchor_frame_id")
    if anchor_frame_id is None:
        anchor_frame_id = int(first["frame_id"])
    metadata.update(
        {
            "primary_object_name": primary_object,
            "first_frame_id": int(first["frame_id"]),
            "last_frame_id": int(last["frame_id"]),
            "frame_ids_sample": [int(frame["frame_id"]) for frame in frames[:10]],
            "grouping_method": grouping_method,
            "uid_anchor_frame_id": int(anchor_frame_id),
            "min_frames_per_series": effective_minimum,
            "max_series_gap_minutes": float(max_gap_minutes),
        }
    )
    series_uid = (
        str(existing_state["series_uid"])
        if existing_state is not None
        else _make_stable_series_uid(
            field_id=field_id,
            instrument_unit_id=first.get("instrument_unit_id"),
            filter_name=first.get("filter_name"),
            grouping_method=grouping_method,
            first_frame_id=int(anchor_frame_id),
        )
    )
    if dry_run:
        if existing_state is None:
            result.created_series += 1
            result.linked_frames += len(frames)
        else:
            result.updated_series += 1
            result.linked_frames += max(0, len(input_frame_ids - {int(m["frame_id"]) for m in existing_members}))
        _append_series_result_row(
            result,
            series_state={
                "series_id": existing_state.get("series_id") if existing_state else None,
                "field_id": field_id,
                "series_uid": series_uid,
                "status": target_status,
                "instrument_unit_id": first.get("instrument_unit_id"),
                "filter_name": first.get("filter_name"),
            },
            frames=frames,
            dry_run=True,
        )
        return

    if existing_state is None:
        series_id = session.execute(
            text(
                """
                INSERT INTO field_series
                    (field_id, series_uid, instrument_unit_id, filter_name, time_start, time_end,
                     mjd_start, mjd_end, ra_center_deg, dec_center_deg, n_frames,
                     grouping_method, status, metadata_json, created_at, updated_at)
                VALUES
                    (:field_id, :series_uid, :instrument_unit_id, :filter_name, :time_start, :time_end,
                     :mjd_start, :mjd_end, :ra_center_deg, :dec_center_deg, :n_frames,
                     :grouping_method, :status, CAST(:metadata_json AS JSONB), now(), now())
                RETURNING series_id
                """
            ),
            _series_values(
                field_id=field_id,
                series_uid=series_uid,
                instrument_unit_id=first.get("instrument_unit_id"),
                filter_name=first.get("filter_name"),
                time_start=min(times) if times else None,
                time_end=max(times) if times else None,
                mjd_start=min(mjds) if mjds else None,
                mjd_end=max(mjds) if mjds else None,
                ra_vals=ra_vals,
                dec_vals=dec_vals,
                n_frames=len(frames),
                grouping_method=grouping_method,
                status=target_status,
                metadata=metadata,
            ),
        ).scalar_one()
        series_id = int(series_id)
        result.created_series += 1
        new_frame_ids = input_frame_ids
    else:
        series_id = int(existing_state["series_id"])
        session.execute(
            text(
                """
                UPDATE field_series
                SET time_start = :time_start,
                    time_end = :time_end,
                    mjd_start = :mjd_start,
                    mjd_end = :mjd_end,
                    ra_center_deg = :ra_center_deg,
                    dec_center_deg = :dec_center_deg,
                    n_frames = :n_frames,
                    status = :status,
                    metadata_json = CAST(:metadata_json AS JSONB),
                    updated_at = now()
                WHERE series_id = :series_id AND status = 'OPEN'
                """
            ),
            {
                **_series_values(
                    field_id=field_id,
                    series_uid=series_uid,
                    instrument_unit_id=first.get("instrument_unit_id"),
                    filter_name=first.get("filter_name"),
                    time_start=min(times) if times else None,
                    time_end=max(times) if times else None,
                    mjd_start=min(mjds) if mjds else None,
                    mjd_end=max(mjds) if mjds else None,
                    ra_vals=ra_vals,
                    dec_vals=dec_vals,
                    n_frames=len(frames),
                    grouping_method=grouping_method,
                    status=target_status,
                    metadata=metadata,
                ),
                "series_id": series_id,
            },
        )
        result.updated_series += 1
        new_frame_ids = input_frame_ids - {int(member["frame_id"]) for member in existing_members}

    for order_index, frame in enumerate(frames, start=1):
        session.execute(
            text(
                """
                INSERT INTO field_series_frames (series_id, frame_id, order_index)
                VALUES (:series_id, :frame_id, :order_index)
                ON CONFLICT (series_id, frame_id) DO UPDATE
                SET order_index = EXCLUDED.order_index
                """
            ),
            {
                "series_id": series_id,
                "frame_id": int(frame["frame_id"]),
                "order_index": order_index,
            },
        )
    result.linked_frames += len(new_frame_ids)
    state = {
        "series_id": series_id,
        "field_id": field_id,
        "series_uid": series_uid,
        "status": target_status,
        "instrument_unit_id": first.get("instrument_unit_id"),
        "filter_name": first.get("filter_name"),
    }
    _append_series_result_row(result, series_state=state, frames=frames, dry_run=False)


def _series_values(
    *,
    field_id: int,
    series_uid: str,
    instrument_unit_id: int | None,
    filter_name: str | None,
    time_start: datetime | None,
    time_end: datetime | None,
    mjd_start: float | None,
    mjd_end: float | None,
    ra_vals: list[float],
    dec_vals: list[float],
    n_frames: int,
    grouping_method: str,
    status: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "field_id": field_id,
        "series_uid": series_uid,
        "instrument_unit_id": instrument_unit_id,
        "filter_name": filter_name,
        "time_start": time_start,
        "time_end": time_end,
        "mjd_start": mjd_start,
        "mjd_end": mjd_end,
        "ra_center_deg": _mean_circular_ra(ra_vals) if ra_vals else None,
        "dec_center_deg": sum(dec_vals) / len(dec_vals) if dec_vals else None,
        "n_frames": n_frames,
        "grouping_method": grouping_method,
        "status": status,
        "metadata_json": json.dumps(_json_safe(metadata), ensure_ascii=False),
    }


def _append_series_result_row(
    result: BuildFieldSeriesResult,
    *,
    series_state: dict[str, Any],
    frames: list[dict[str, Any]],
    dry_run: bool,
) -> None:
    if not frames:
        return
    ordered = sorted(frames, key=_frame_sort_key)
    first, last = ordered[0], ordered[-1]
    mjds = [float(frame["mjd"]) for frame in ordered if frame.get("mjd") is not None]
    times = [
        value
        for frame in ordered
        if (value := _coerce_datetime(frame.get("date_obs"))) is not None
    ]
    status = str(series_state["status"])
    series_id = series_state.get("series_id")
    if (
        series_id is not None
        and status == "OPEN"
        and int(series_id) not in result.open_series_ids
    ):
        result.open_series_ids.append(int(series_id))
    result.rows.append(
        {
            "field_id": int(series_state["field_id"]),
            "series_id": int(series_id) if series_id is not None else None,
            "series_uid": series_state["series_uid"],
            "instrument_unit_id": series_state.get("instrument_unit_id"),
            "filter_name": series_state.get("filter_name"),
            "status": status,
            "n_frames": len(ordered),
            "mjd_start": min(mjds) if mjds else None,
            "mjd_end": max(mjds) if mjds else None,
            "time_start": min(times) if times else None,
            "time_end": max(times) if times else None,
            "frame_id_first": int(first["frame_id"]),
            "frame_id_last": int(last["frame_id"]),
            "dry_run": dry_run,
        }
    )


def _assigned_series_ids_for_frames(session: Session, frame_ids: set[int]) -> list[int]:
    if not frame_ids:
        return []
    stmt = text(
        """
        SELECT DISTINCT series_id
        FROM field_series_frames
        WHERE frame_id IN :frame_ids
        ORDER BY series_id
        """
    ).bindparams(bindparam("frame_ids", expanding=True))
    return [
        int(row[0])
        for row in session.execute(stmt, {"frame_ids": sorted(frame_ids)}).all()
    ]


def _load_series_state(session: Session, series_id: int) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT series_id, field_id, series_uid, instrument_unit_id, filter_name,
                   grouping_method, status, n_frames, metadata_json
            FROM field_series
            WHERE series_id = :series_id
            """
        ),
        {"series_id": int(series_id)},
    ).mappings().first()
    return dict(row) if row is not None else None


def _load_series_members(session: Session, series_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT fr.frame_id, fr.instrument_unit_id, fr.filter_name, fr.object_name,
                   fr.ra_center_deg, fr.dec_center_deg, fr.date_obs, fr.mjd
            FROM field_series_frames sf
            JOIN frames fr ON fr.frame_id = sf.frame_id
            WHERE sf.series_id = :series_id
            ORDER BY fr.mjd NULLS LAST, fr.date_obs NULLS LAST, fr.frame_id
            """
        ),
        {"series_id": int(series_id)},
    ).mappings().all()
    return [dict(row) for row in rows]


def _load_open_series_for_field(
    session: Session, field_id: int, grouping_method: str
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    rows = session.execute(
        text(
            """
            SELECT series_id
            FROM field_series
            WHERE field_id = :field_id AND grouping_method = :grouping_method
              AND status = 'OPEN'
            ORDER BY series_id
            """
        ),
        {"field_id": int(field_id), "grouping_method": grouping_method},
    ).all()
    out = []
    for row in rows:
        state = _load_series_state(session, int(row[0]))
        if state is None:
            continue
        members = _load_series_members(session, int(row[0]))
        if members:
            out.append((state, members))
    return out


def _series_groups_are_contiguous(
    existing_members: list[dict[str, Any]],
    new_frames: list[dict[str, Any]],
    max_gap_minutes: float,
) -> bool:
    existing_ordered = sorted(existing_members, key=_frame_sort_key)
    new_ordered = sorted(new_frames, key=_frame_sort_key)
    if not existing_ordered or not new_ordered:
        return False
    if _frame_sort_key(new_ordered[0]) <= _frame_sort_key(existing_ordered[-1]):
        return False
    return _gap_minutes(existing_ordered[-1], new_ordered[0]) <= max_gap_minutes


def _validate_open_series_key(
    series_state: dict[str, Any],
    *,
    field_id: int,
    instrument_unit_id: int | None,
    filter_name: str | None,
    grouping_method: str,
) -> None:
    if (
        int(series_state["field_id"]) != int(field_id)
        or series_state.get("instrument_unit_id") != instrument_unit_id
        or series_state.get("filter_name") != filter_name
        or str(series_state.get("grouping_method")) != grouping_method
    ):
        raise ValueError("candidate Frames do not match the existing OPEN series grouping key")


def _series_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _validate_open_series_metadata(
    series_state: dict[str, Any],
    *,
    min_frames: int,
    max_gap_minutes: float,
) -> dict[str, Any]:
    metadata = _series_metadata(series_state.get("metadata_json"))
    stored_minimum = metadata.get("min_frames_per_series")
    if stored_minimum is None or int(stored_minimum) < 1:
        raise ValueError(
            f"OPEN series {series_state['series_id']} has no valid persisted minimum-frame threshold"
        )
    if int(stored_minimum) != int(min_frames):
        raise ValueError(
            "min_frames_per_series differs from the value persisted for the OPEN tail"
        )
    stored_gap = metadata.get("max_series_gap_minutes")
    if stored_gap is None:
        raise ValueError(
            f"OPEN series {series_state['series_id']} has no persisted time-gap threshold"
        )
    if float(stored_gap) != float(max_gap_minutes):
        raise ValueError(
            "max_series_gap_minutes differs from the value persisted for the OPEN tail"
        )
    return metadata


def _close_open_series_at_boundary(
    session: Session,
    *,
    result: BuildFieldSeriesResult,
    state: dict[str, Any],
    members: list[dict[str, Any]],
    dry_run: bool,
) -> None:
    metadata = _series_metadata(state.get("metadata_json"))
    minimum_value = metadata.get("min_frames_per_series")
    if minimum_value is None or int(minimum_value) < 1:
        raise ValueError(
            f"OPEN series {state['series_id']} has no valid persisted minimum-frame threshold"
        )
    minimum = int(minimum_value)
    status = "READY" if len(members) >= minimum else "OPEN"
    if status == "READY" and not dry_run:
        session.execute(
            text(
                "UPDATE field_series SET status = 'READY', updated_at = now() "
                "WHERE series_id = :series_id AND status = 'OPEN'"
            ),
            {"series_id": int(state["series_id"])},
        )
        result.updated_series += 1
        state = {**state, "status": "READY"}
    _append_series_result_row(
        result,
        series_state={**state, "status": status},
        frames=members,
        dry_run=dry_run,
    )


def _refresh_field_summaries(session: Session, field_ids: Iterable[int] | None = None) -> None:
    scoped_ids = None if field_ids is None else sorted({int(field_id) for field_id in field_ids})
    if scoped_ids == []:
        return
    scope_sql = "WHERE f.field_id IN :field_ids" if scoped_ids is not None else ""
    stmt = text(
        f"""
            SELECT f.field_id,
                   AVG(s.ra_center_deg) AS decoy_ra,
                   AVG(s.dec_center_deg) AS avg_dec,
                   COUNT(DISTINCT s.series_id)::int AS n_series
            FROM fields f
            JOIN field_series s ON s.field_id = f.field_id
            {scope_sql}
            GROUP BY f.field_id
        """
    )
    if scoped_ids is not None:
        stmt = stmt.bindparams(bindparam("field_ids", expanding=True, type_=BigInteger()))
    rows = session.execute(
        stmt, {"field_ids": scoped_ids} if scoped_ids is not None else {}
    ).mappings().all()
    for row in rows:
        series_centers = session.execute(
            text("SELECT ra_center_deg, dec_center_deg FROM field_series WHERE field_id = :field_id"),
            {"field_id": int(row["field_id"])},
        ).mappings().all()
        ras = [float(r["ra_center_deg"]) for r in series_centers if r["ra_center_deg"] is not None]
        decs = [float(r["dec_center_deg"]) for r in series_centers if r["dec_center_deg"] is not None]
        if not ras or not decs:
            continue
        session.execute(
            text(
                """
                UPDATE fields
                SET ra_center_deg = :ra_center_deg,
                    dec_center_deg = :dec_center_deg,
                    updated_at = now()
                WHERE field_id = :field_id
                """
            ),
            {
                "field_id": int(row["field_id"]),
                "ra_center_deg": _mean_circular_ra(ras),
                "dec_center_deg": sum(decs) / len(decs),
            },
        )


def _frame_sort_key(frame: dict[str, Any]) -> tuple[float, str, int]:
    mjd = frame.get("mjd")
    mjd_sort = float(mjd) if mjd is not None else float("inf")
    time_sort = str(frame.get("date_obs") or "")
    return (mjd_sort, time_sort, int(frame.get("frame_id") or 0))


def _gap_minutes(prev: dict[str, Any], cur: dict[str, Any]) -> float:
    if prev.get("mjd") is not None and cur.get("mjd") is not None:
        return abs(float(cur["mjd"]) - float(prev["mjd"])) * 1440.0
    a = _coerce_datetime(prev.get("date_obs"))
    b = _coerce_datetime(cur.get("date_obs"))
    if a is not None and b is not None:
        return abs((b - a).total_seconds()) / 60.0
    return 0.0


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _clean_name(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _make_field_uid(*, field_name: str, ra_deg: float, dec_deg: float) -> str:
    key = f"{field_name}|{ra_deg:.5f}|{dec_deg:.5f}".encode("utf-8")
    return "field_" + hashlib.sha1(key).hexdigest()[:16]


def _make_series_uid(
    *,
    field_id: int,
    instrument_unit_id: int | None,
    filter_name: str | None,
    mjd_start: float | None,
    mjd_end: float | None,
    frame_ids: list[int],
    grouping_method: str,
) -> str:
    key = "|".join(
        [
            str(field_id),
            str(instrument_unit_id),
            str(filter_name),
            str(grouping_method),
            f"{mjd_start:.8f}" if mjd_start is not None else "-",
            f"{mjd_end:.8f}" if mjd_end is not None else "-",
            ",".join(map(str, frame_ids[:3] + frame_ids[-3:])),
        ]
    ).encode("utf-8")
    return "series_" + hashlib.sha1(key).hexdigest()[:16]


def _make_stable_series_uid(
    *,
    field_id: int,
    instrument_unit_id: int | None,
    filter_name: str | None,
    grouping_method: str,
    first_frame_id: int,
) -> str:
    key = "|".join(
        [
            "series_uid_first_frame_v2",
            str(field_id),
            str(instrument_unit_id),
            str(filter_name),
            str(grouping_method),
            str(int(first_frame_id)),
        ]
    ).encode("utf-8")
    return "series_" + hashlib.sha1(key).hexdigest()[:16]


def _angular_sep_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    ra1 = math.radians(float(ra1_deg))
    dec1 = math.radians(float(dec1_deg))
    ra2 = math.radians(float(ra2_deg))
    dec2 = math.radians(float(dec2_deg))
    s = math.sin((dec2 - dec1) / 2.0) ** 2 + math.cos(dec1) * math.cos(dec2) * math.sin((ra2 - ra1) / 2.0) ** 2
    s = max(0.0, min(1.0, s))
    return math.degrees(2.0 * math.asin(math.sqrt(s))) * 3600.0


def _mean_circular_ra(ras_deg: list[float]) -> float:
    if not ras_deg:
        return 0.0
    xs = [math.cos(math.radians(ra)) for ra in ras_deg]
    ys = [math.sin(math.radians(ra)) for ra in ras_deg]
    angle = math.degrees(math.atan2(sum(ys) / len(ys), sum(xs) / len(xs)))
    return angle % 360.0


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value
