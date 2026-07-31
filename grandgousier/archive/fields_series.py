from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session


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
    dry_run: bool = False
    frame_selection_diagnostics: dict[str, Any] = field(default_factory=dict)
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
            "dry_run": self.dry_run,
            "frame_selection_diagnostics": self.frame_selection_diagnostics,
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
    dry_run: bool = False,
) -> BuildFieldSeriesResult:
    """Assign frames to persistent sky fields and observing series.

    A field is a sky area. A series is a contiguous observing block of one field
    with one instrument_unit/filter. This function is deliberately conservative:
    it uses WCS/image-center coordinates and time gaps, not object-name alone.
    """

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
    existing_fields = _load_fields(session)
    field_cache = {int(f["field_id"]): dict(f) for f in existing_fields}

    # First pass: assign each frame to a field, creating in-memory/DB fields as needed.
    frames_by_field: dict[int, list[dict[str, Any]]] = {}
    synthetic_field_id = -1
    for frame in frames:
        if frame.get("already_assigned"):
            result.skipped_already_assigned += 1
            continue
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
            prev = None
            for frame in bucket:
                if prev is not None and _gap_minutes(prev, frame) > max_series_gap_minutes:
                    _create_series_group(
                        session,
                        result=result,
                        field_id=field_id,
                        frames=current,
                        grouping_method=grouping_method,
                        min_frames=min_frames_per_series,
                        dry_run=dry_run,
                    )
                    current = []
                current.append(frame)
                prev = frame
            _create_series_group(
                session,
                result=result,
                field_id=field_id,
                frames=current,
                grouping_method=grouping_method,
                min_frames=min_frames_per_series,
                dry_run=dry_run,
            )

    if not dry_run:
        _refresh_field_summaries(session)
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
                fr.raw_path,
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
    if status == "FAILED":
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
            COUNT(*)::int AS n_frames,
            COUNT(*) FILTER (WHERE fr.ra_center_deg IS NOT NULL AND fr.dec_center_deg IS NOT NULL)::int AS n_with_center,
            COUNT(*) FILTER (WHERE fr.ra_center_deg IS NULL OR fr.dec_center_deg IS NULL)::int AS n_without_center
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
    dry_run: bool,
) -> None:
    if len(frames) < int(min_frames):
        return
    frames = sorted(frames, key=_frame_sort_key)
    first = frames[0]
    last = frames[-1]
    ra_vals = [float(f["ra_center_deg"]) for f in frames if f.get("ra_center_deg") is not None]
    dec_vals = [float(f["dec_center_deg"]) for f in frames if f.get("dec_center_deg") is not None]
    mjds = [float(f["mjd"]) for f in frames if f.get("mjd") is not None]
    times = [f.get("date_obs") for f in frames if f.get("date_obs") is not None]
    object_names = [_clean_name(f.get("object_name")) for f in frames if _clean_name(f.get("object_name"))]
    primary_object = object_names[0] if object_names else None
    metadata = {
        "primary_object_name": primary_object,
        "first_frame_id": first.get("frame_id"),
        "last_frame_id": last.get("frame_id"),
        "frame_ids_sample": [int(f["frame_id"]) for f in frames[:10]],
    }
    series_uid = _make_series_uid(
        field_id=field_id,
        instrument_unit_id=first.get("instrument_unit_id"),
        filter_name=first.get("filter_name"),
        mjd_start=min(mjds) if mjds else None,
        mjd_end=max(mjds) if mjds else None,
        frame_ids=[int(f["frame_id"]) for f in frames],
    )
    if dry_run:
        result.created_series += 1
        result.linked_frames += len(frames)
        result.rows.append(
            {
                "field_id": field_id,
                "series_id": None,
                "series_uid": series_uid,
                "instrument_unit_id": first.get("instrument_unit_id"),
                "filter_name": first.get("filter_name"),
                "n_frames": len(frames),
                "mjd_start": min(mjds) if mjds else None,
                "mjd_end": max(mjds) if mjds else None,
                "frame_id_first": first.get("frame_id"),
                "frame_id_last": last.get("frame_id"),
                "dry_run": True,
            }
        )
        return

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
                 :grouping_method, 'READY', CAST(:metadata_json AS JSONB), now(), now())
            ON CONFLICT (series_uid) DO UPDATE
            SET n_frames = EXCLUDED.n_frames,
                time_start = EXCLUDED.time_start,
                time_end = EXCLUDED.time_end,
                mjd_start = EXCLUDED.mjd_start,
                mjd_end = EXCLUDED.mjd_end,
                ra_center_deg = EXCLUDED.ra_center_deg,
                dec_center_deg = EXCLUDED.dec_center_deg,
                metadata_json = EXCLUDED.metadata_json,
                updated_at = now()
            RETURNING series_id
            """
        ),
        {
            "field_id": field_id,
            "series_uid": series_uid,
            "instrument_unit_id": first.get("instrument_unit_id"),
            "filter_name": first.get("filter_name"),
            "time_start": min(times) if times else None,
            "time_end": max(times) if times else None,
            "mjd_start": min(mjds) if mjds else None,
            "mjd_end": max(mjds) if mjds else None,
            "ra_center_deg": _mean_circular_ra(ra_vals) if ra_vals else None,
            "dec_center_deg": sum(dec_vals) / len(dec_vals) if dec_vals else None,
            "n_frames": len(frames),
            "grouping_method": grouping_method,
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
    ).scalar_one()
    session.commit()
    values = [
        {"series_id": int(series_id), "frame_id": int(f["frame_id"]), "order_index": i + 1}
        for i, f in enumerate(frames)
    ]
    for value in values:
        session.execute(
            text(
                """
                INSERT INTO field_series_frames (series_id, frame_id, order_index)
                VALUES (:series_id, :frame_id, :order_index)
                ON CONFLICT (frame_id) DO UPDATE
                SET series_id = EXCLUDED.series_id,
                    order_index = EXCLUDED.order_index
                """
            ),
            value,
        )
    session.execute(
        text("UPDATE field_series SET n_frames = (SELECT COUNT(*) FROM field_series_frames WHERE series_id = :series_id), updated_at = now() WHERE series_id = :series_id"),
        {"series_id": int(series_id)},
    )
    result.created_series += 1
    result.linked_frames += len(frames)
    result.rows.append(
        {
            "field_id": field_id,
            "series_id": int(series_id),
            "series_uid": series_uid,
            "instrument_unit_id": first.get("instrument_unit_id"),
            "filter_name": first.get("filter_name"),
            "n_frames": len(frames),
            "mjd_start": min(mjds) if mjds else None,
            "mjd_end": max(mjds) if mjds else None,
            "time_start": min(times) if times else None,
            "time_end": max(times) if times else None,
            "frame_id_first": first.get("frame_id"),
            "frame_id_last": last.get("frame_id"),
            "dry_run": False,
        }
    )


def _refresh_field_summaries(session: Session) -> None:
    rows = session.execute(
        text(
            """
            SELECT f.field_id,
                   AVG(s.ra_center_deg) AS decoy_ra,
                   AVG(s.dec_center_deg) AS avg_dec,
                   COUNT(DISTINCT s.series_id)::int AS n_series
            FROM fields f
            JOIN field_series s ON s.field_id = f.field_id
            GROUP BY f.field_id
            """
        )
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
    a = prev.get("date_obs")
    b = cur.get("date_obs")
    if isinstance(a, datetime) and isinstance(b, datetime):
        return abs((b - a).total_seconds()) / 60.0
    return 0.0


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
) -> str:
    key = "|".join(
        [
            str(field_id),
            str(instrument_unit_id),
            str(filter_name),
            f"{mjd_start:.8f}" if mjd_start is not None else "-",
            f"{mjd_end:.8f}" if mjd_end is not None else "-",
            ",".join(map(str, frame_ids[:3] + frame_ids[-3:])),
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
