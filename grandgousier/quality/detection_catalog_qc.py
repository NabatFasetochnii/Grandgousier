from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import and_, bindparam, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload
from tqdm import tqdm

from grandgousier.catalogs.frame_match import (
    _frame_wcs,
    _project_sources,
    _source_mag_sql_expression,
)
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.detection_io import PostgresDetectionCatalogMatchCopyStage, postgres_copy_capable
from grandgousier.db.detection_runs import (
    CATALOG_MATCH_RUN,
    SOURCE_DETECTION_RUN,
    create_detection_run,
    create_detection_run_frame,
)
from grandgousier.db.models import (
    CatalogSource,
    DetectionCatalogMatch,
    DetectionRun,
    DetectionRunFrame,
    Frame,
    FrameQc,
    SourceDetection,
)

MATCH_ALGORITHM = "nearest_sky_v1"
QC_ALGORITHM = "detection_catalog_qc_v1"


@dataclass
class DetectionCatalogQcResult:
    detection_run_id: int | None = None
    selected_frames: int = 0
    checked_frames: int = 0
    selected_detections: int = 0
    selected_catalog_sources: int = 0
    matched: int = 0
    frame_qc_written: int = 0
    frame_qc_batches: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    deleted_existing_matches: int = 0
    delete_batches: int = 0
    failed_frames: int = 0
    dry_run: bool = False
    catalog_name: str = ""
    read_strategy: str = "BULK_SCALAR_2QUERY_V1"
    write_strategy: str = "SQLALCHEMY_PER_FRAME_UPSERT"
    prefetched_detection_rows: int = 0
    prefetched_catalog_rows: int = 0
    staging_rows: int = 0
    staging_copy_batches: int = 0
    frame_load_sec: float = 0.0
    detection_load_sec: float = 0.0
    catalog_load_sec: float = 0.0
    matching_sec: float = 0.0
    catalog_projection_sec: float = 0.0
    spatial_index_sources: int = 0
    spatial_candidate_checks: int = 0
    candidate_pairs: int = 0
    qc_build_sec: float = 0.0
    delete_existing_sec: float = 0.0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    frame_qc_upsert_sec: float = 0.0
    quality_flag_merge_sec: float = 0.0
    quality_flag_flush_sec: float = 0.0
    write_finalize_sec: float = 0.0


@dataclass(frozen=True, slots=True)
class DetectionMatchInput:
    source_detection_id: int
    frame_id: int
    x: float
    y: float
    ra_deg: float
    dec_deg: float
    peak_adu: float | None
    flux: float | None
    snr: float | None
    algorithm: str


@dataclass(frozen=True, slots=True)
class CatalogMatchSource:
    catalog_source_id: int
    source_id: str
    ra_deg: float
    dec_deg: float
    ref_mags: dict


@dataclass(frozen=True, slots=True)
class _SkyQueryBox:
    ra_min_deg: float
    ra_max_deg: float
    dec_min_deg: float
    dec_max_deg: float


def aggregate_detection_catalog_support(
    session: Session,
    *,
    catalog_name: str,
    detection_run_id: int,
    frame_ids: list[int] | None,
) -> dict[int, int]:
    """Count distinct selected-frame support for each matched catalog source."""

    if frame_ids is not None and not frame_ids:
        return {}
    sql = """
        SELECT dcm.catalog_source_id,
               COUNT(DISTINCT fr.frame_id) AS n_frames
        FROM detection_catalog_matches dcm
        JOIN detection_run_frames drf
          ON drf.detection_run_frame_id = dcm.detection_run_frame_id
        JOIN detection_runs dr ON dr.detection_run_id = drf.detection_run_id
        JOIN frames fr ON fr.frame_id = drf.frame_id
        WHERE dr.detection_run_id = :detection_run_id
          AND dr.run_kind = 'CATALOG_MATCH'
          AND dr.catalog_name = :catalog_name
          AND drf.status = 'DONE'
          AND dcm.match_status = 'MATCHED'
    """
    if frame_ids is not None:
        sql += " AND fr.frame_id IN :frame_ids\n"
    sql += """
        GROUP BY dcm.catalog_source_id
        ORDER BY dcm.catalog_source_id
        """
    statement = text(sql)
    parameters = {
        "detection_run_id": int(detection_run_id),
        "catalog_name": str(catalog_name),
    }
    if frame_ids is not None:
        statement = statement.bindparams(bindparam("frame_ids", expanding=True))
        parameters["frame_ids"] = sorted({int(value) for value in frame_ids})
    rows = session.execute(statement, parameters).all()
    return {int(source_id): int(n_frames) for source_id, n_frames in rows}


def match_detections_to_catalog(
    session: Session,
    *,
    catalog_name: str,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    detection_algorithm: str = "numpy_localmax_v1",
    radius_arcsec: float = 2.0,
    mag_key: str = "G",
    working_mag_limit: float | None = None,
    include_catalog_source_ids: list[int] | None = None,
    min_snr: float | None = None,
    limit_frames: int | None = None,
    replace_existing: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    pipeline_run_id: int | None = None,
) -> DetectionCatalogQcResult:
    """Cross-match stored detections with projected catalog sources.

    Run-level configuration is stored once in ``detection_runs`` and per-frame
    execution state in ``detection_run_frames``.  High-cardinality match rows keep
    only relation-specific diagnostics plus photometric snapshots required for exact
    historical QC.
    """

    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

    if radius_arcsec <= 0:
        raise ValueError("radius_arcsec must be positive")

    frame_load_started = time.perf_counter()
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    frame_load_sec = float(time.perf_counter() - frame_load_started)
    result = DetectionCatalogQcResult(
        selected_frames=len(frames),
        dry_run=dry_run,
        catalog_name=catalog_name,
        insert_batch_size=int(insert_batch_size),
        frame_load_sec=frame_load_sec,
    )
    selected_frame_ids = [int(frame.frame_id) for frame in frames]

    started = time.perf_counter()
    detections_by_frame = _select_detections_bulk(
        session,
        frame_ids=selected_frame_ids,
        algorithm=detection_algorithm,
        min_snr=min_snr,
    )
    result.detection_load_sec = float(time.perf_counter() - started)
    result.prefetched_detection_rows = int(sum(len(rows) for rows in detections_by_frame.values()))

    started = time.perf_counter()
    catalog_rows = _select_catalog_sources_bulk(
        session,
        detections_by_frame=detections_by_frame,
        catalog_name=catalog_name,
        radius_arcsec=radius_arcsec,
        mag_key=mag_key,
        working_mag_limit=working_mag_limit,
        include_catalog_source_ids=include_catalog_source_ids,
    )
    result.catalog_load_sec = float(time.perf_counter() - started)
    result.prefetched_catalog_rows = len(catalog_rows)
    result.selected_catalog_sources = len(catalog_rows)
    catalog_index = _CatalogSkyIndex(catalog_rows, radius_arcsec=radius_arcsec)
    result.spatial_index_sources = len(catalog_rows)

    match_run = None
    if not dry_run:
        match_run = create_detection_run(
            session,
            run_kind=CATALOG_MATCH_RUN,
            algorithm=MATCH_ALGORITHM,
            catalog_name=catalog_name,
            parameters={
                "detection_algorithm": detection_algorithm,
                "radius_arcsec": radius_arcsec,
                "mag_key": mag_key,
                "working_mag_limit": working_mag_limit,
                "include_catalog_source_ids": sorted(
                    {int(value) for value in (include_catalog_source_ids or [])}
                ),
                "min_snr": min_snr,
            },
            pipeline_run_id=pipeline_run_id,
        )
        result.detection_run_id = int(match_run.detection_run_id)

    copy_stage: PostgresDetectionCatalogMatchCopyStage | None = None
    copy_buffer: list[dict] = []
    copy_buffer_rows = 8192
    successful_frame_ids: list[int] = []
    qc_rows: list[dict] = []
    if dry_run:
        result.write_strategy = "DRY_RUN"
    elif postgres_copy_capable(session):
        copy_stage = PostgresDetectionCatalogMatchCopyStage.create(session)
        result.write_strategy = "POSTGRESQL_COPY_STAGING"
    else:
        result.write_strategy = "SQLALCHEMY_PER_FRAME_UPSERT"

    try:
        for frame in tqdm(frames, desc="Matching detections to catalog", unit="frame"):
            result.checked_frames += 1
            try:
                detections = detections_by_frame.get(int(frame.frame_id), [])
                result.selected_detections += len(detections)

                started = time.perf_counter()
                checks_before = catalog_index.total_query_checks
                frame_pairs = _candidate_pairs(
                    detections=detections,
                    catalog_rows=catalog_rows,
                    radius_arcsec=radius_arcsec,
                    spatial_index=catalog_index,
                )
                result.spatial_candidate_checks += (
                    catalog_index.total_query_checks - checks_before
                )
                result.candidate_pairs += len(frame_pairs)
                frame_catalog_sources = _unique_candidate_sources(frame_pairs)
                rows = _match_frame(
                    frame=frame,
                    detections=detections,
                    candidate_pairs=frame_pairs,
                    radius_arcsec=radius_arcsec,
                    mag_key=mag_key,
                    projection_timing=result,
                )
                result.matching_sec += float(time.perf_counter() - started)
                result.matched += len(rows)

                started = time.perf_counter()
                qc_row = _build_frame_qc_row(
                    frame=frame,
                    catalog_name=catalog_name,
                    detections=detections,
                    catalog_rows=frame_catalog_sources,
                    match_rows=rows,
                    mag_key=mag_key,
                    radius_arcsec=radius_arcsec,
                    detection_algorithm=detection_algorithm,
                )
                result.qc_build_sec += float(time.perf_counter() - started)
            except Exception as exc:  # noqa: BLE001 - preserve per-frame isolation.
                result.failed_frames += 1
                if match_run is not None:
                    create_detection_run_frame(
                        session,
                        detection_run_id=int(match_run.detection_run_id),
                        frame_id=int(frame.frame_id),
                        status="FAILED",
                        diagnostics={
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:1000],
                        },
                    )
                continue

            if dry_run:
                continue

            assert match_run is not None
            if replace_existing and copy_stage is None:
                started = time.perf_counter()
                deleted, _ = _delete_prior_catalog_match_run_frames(
                    session,
                    frame_ids=[int(frame.frame_id)],
                    catalog_name=catalog_name,
                    algorithm=MATCH_ALGORITHM,
                    keep_detection_run_id=int(match_run.detection_run_id),
                )
                result.delete_existing_sec += float(time.perf_counter() - started)
                result.delete_batches += 1
                result.deleted_existing_matches += deleted

            run_frame = create_detection_run_frame(
                session,
                detection_run_id=int(match_run.detection_run_id),
                frame_id=int(frame.frame_id),
                status="DONE",
                diagnostics={
                    "n_detections": int(len(detections)),
                    "n_catalog_sources": len(frame_catalog_sources),
                    "n_matched": int(len(rows)),
                },
            )
            storage_rows = [
                _match_storage_row(row, detection_run_frame_id=int(run_frame.detection_run_frame_id))
                for row in rows
            ]

            if copy_stage is not None:
                successful_frame_ids.append(int(frame.frame_id))
                if storage_rows:
                    copy_buffer.extend(storage_rows)
                    if len(copy_buffer) >= copy_buffer_rows:
                        copy_stage.append_rows(copy_buffer)
                        copy_buffer.clear()
                qc_rows.append(qc_row)
                started = time.perf_counter()
                _merge_frame_quality_flags(frame, qc_row)
                result.quality_flag_merge_sec += float(time.perf_counter() - started)
                result.frame_qc_written += 1
                continue

            if storage_rows:
                started = time.perf_counter()
                result.insert_batches += _upsert_detection_catalog_matches(
                    session,
                    storage_rows,
                    insert_batch_size=insert_batch_size,
                )
                result.insert_select_sec += float(time.perf_counter() - started)
            started = time.perf_counter()
            _upsert_frame_qc(session, qc_row)
            result.frame_qc_upsert_sec += float(time.perf_counter() - started)
            result.frame_qc_batches += 1
            started = time.perf_counter()
            _merge_frame_quality_flags(frame, qc_row)
            result.quality_flag_merge_sec += float(time.perf_counter() - started)
            result.frame_qc_written += 1

        if not dry_run and copy_stage is not None:
            if copy_buffer:
                copy_stage.append_rows(copy_buffer)
                copy_buffer.clear()

            if qc_rows:
                started = time.perf_counter()
                session.flush()
                result.quality_flag_flush_sec = float(time.perf_counter() - started)

            if replace_existing and successful_frame_ids:
                assert match_run is not None
                started = time.perf_counter()
                deleted, _ = _delete_prior_catalog_match_run_frames(
                    session,
                    frame_ids=successful_frame_ids,
                    catalog_name=catalog_name,
                    algorithm=MATCH_ALGORITHM,
                    keep_detection_run_id=int(match_run.detection_run_id),
                )
                result.delete_existing_sec += float(time.perf_counter() - started)
                result.delete_batches = 1
                result.deleted_existing_matches += deleted

            copy_stage.merge()
            result.staging_rows = int(copy_stage.rows_staged)
            result.staging_copy_batches = int(copy_stage.copy_batches)
            result.staging_copy_sec = float(copy_stage.staging_copy_sec)
            result.insert_select_sec = float(copy_stage.insert_select_sec)
            result.insert_batches = 1 if copy_stage.rows_staged else 0

            if qc_rows:
                started = time.perf_counter()
                result.frame_qc_batches = _upsert_frame_qc_rows(
                    session,
                    qc_rows,
                    insert_batch_size=insert_batch_size,
                )
                result.frame_qc_upsert_sec = float(time.perf_counter() - started)

        if not dry_run:
            started = time.perf_counter()
            if copy_stage is not None:
                copy_stage.close()
            session.commit()
            result.write_finalize_sec = float(time.perf_counter() - started)
    finally:
        if copy_stage is not None and not copy_stage.closed:
            try:
                copy_stage.close()
            except Exception:
                pass

    return result


def _delete_prior_catalog_match_run_frames(
    session: Session,
    *,
    frame_ids: list[int],
    catalog_name: str,
    algorithm: str,
    keep_detection_run_id: int,
) -> tuple[int, int]:
    if not frame_ids:
        return 0, 0
    old_run_frame_ids = (
        select(DetectionRunFrame.detection_run_frame_id)
        .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
        .where(
            DetectionRunFrame.frame_id.in_(frame_ids),
            DetectionRun.run_kind == CATALOG_MATCH_RUN,
            DetectionRun.catalog_name == catalog_name,
            DetectionRun.algorithm == algorithm,
            DetectionRun.detection_run_id != int(keep_detection_run_id),
        )
    )
    n_matches = int(
        session.scalar(
            select(func.count(DetectionCatalogMatch.detection_catalog_match_id)).where(
                DetectionCatalogMatch.detection_run_frame_id.in_(old_run_frame_ids)
            )
        )
        or 0
    )
    deleted_run_frames = int(
        session.execute(
            delete(DetectionRunFrame).where(
                DetectionRunFrame.detection_run_frame_id.in_(old_run_frame_ids)
            )
        ).rowcount
        or 0
    )
    return n_matches, deleted_run_frames


def _match_storage_row(row: dict, *, detection_run_frame_id: int) -> dict:
    return {
        "detection_run_frame_id": int(detection_run_frame_id),
        "source_detection_id": int(row["source_detection_id"]),
        "catalog_source_id": int(row["catalog_source_id"]),
        "match_status": str(row.get("match_status") or "MATCHED"),
        "sep_arcsec": row.get("sep_arcsec"),
        "dx_pix": row.get("dx_pix"),
        "dy_pix": row.get("dy_pix"),
        "detection_mag_inst": row.get("detection_mag_inst"),
        "catalog_mag": row.get("catalog_mag"),
        "zero_point": row.get("zero_point"),
    }


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).options(joinedload(Frame.instrument_unit)).order_by(Frame.frame_id)
    if frame_ids is not None:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument:
        stmt = stmt.where(Frame.instrument == instrument)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if only_wcs_ok:
        stmt = stmt.where(Frame.wcs_status == "HEADER_WCS_OK")
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _select_detections_bulk(
    session: Session,
    *,
    frame_ids: list[int],
    algorithm: str,
    min_snr: float | None,
) -> dict[int, list[DetectionMatchInput]]:
    if not frame_ids:
        return {}
    stmt = (
        select(
            SourceDetection.source_detection_id,
            DetectionRunFrame.frame_id.label("frame_id"),
            SourceDetection.x,
            SourceDetection.y,
            SourceDetection.ra_deg,
            SourceDetection.dec_deg,
            SourceDetection.peak_adu,
            SourceDetection.flux,
            SourceDetection.snr,
            DetectionRun.algorithm.label("algorithm"),
        )
        .select_from(SourceDetection)
        .join(
            DetectionRunFrame,
            DetectionRunFrame.detection_run_frame_id == SourceDetection.detection_run_frame_id,
        )
        .join(
            DetectionRun,
            DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id,
        )
        .where(
            DetectionRunFrame.frame_id.in_(frame_ids),
            DetectionRun.run_kind == SOURCE_DETECTION_RUN,
            DetectionRun.algorithm == algorithm,
            SourceDetection.detection_status == "DETECTED",
            SourceDetection.ra_deg.is_not(None),
            SourceDetection.dec_deg.is_not(None),
        )
        .order_by(
            DetectionRunFrame.frame_id,
            SourceDetection.snr.desc().nullslast(),
        )
    )
    if min_snr is not None:
        stmt = stmt.where(SourceDetection.snr >= float(min_snr))

    grouped: dict[int, list[DetectionMatchInput]] = {}
    for row in session.execute(stmt).mappings().all():
        frame_id = int(row["frame_id"])
        grouped.setdefault(frame_id, []).append(
            DetectionMatchInput(
                source_detection_id=int(row["source_detection_id"]),
                frame_id=frame_id,
                x=float(row["x"]),
                y=float(row["y"]),
                ra_deg=float(row["ra_deg"]),
                dec_deg=float(row["dec_deg"]),
                peak_adu=_safe_float(row["peak_adu"]),
                flux=_safe_float(row["flux"]),
                snr=_safe_float(row["snr"]),
                algorithm=str(row["algorithm"]),
            )
        )
    return grouped


def _select_catalog_sources_bulk(
    session: Session,
    *,
    detections_by_frame: dict[int, list[DetectionMatchInput]],
    catalog_name: str,
    radius_arcsec: float,
    mag_key: str,
    working_mag_limit: float | None,
    include_catalog_source_ids: list[int] | None = None,
) -> list[CatalogMatchSource]:
    """Load only catalog rows in selected detection footprints and match radius.

    The match radius is expanded around each selected frame's detection envelope.
    Explicitly requested target IDs are the only exception to the sky scope and
    remain subject to the exact sky-distance check before they can gain support.
    """

    include_ids = sorted({int(value) for value in (include_catalog_source_ids or [])})
    boxes = _sky_query_boxes(detections_by_frame, radius_arcsec=radius_arcsec)
    if not boxes and not include_ids:
        return []

    stmt = (
        select(
            CatalogSource.catalog_source_id,
            CatalogSource.source_id,
            CatalogSource.ra_deg,
            CatalogSource.dec_deg,
            CatalogSource.ref_mags,
        )
        .where(CatalogSource.catalog_name == catalog_name)
    )
    scope_terms = [
        and_(
            CatalogSource.ra_deg >= box.ra_min_deg,
            CatalogSource.ra_deg <= box.ra_max_deg,
            CatalogSource.dec_deg >= box.dec_min_deg,
            CatalogSource.dec_deg <= box.dec_max_deg,
        )
        for box in boxes
    ]
    source_scope = or_(*scope_terms) if scope_terms else None
    if include_ids:
        include_scope = CatalogSource.catalog_source_id.in_(include_ids)
        source_scope = or_(source_scope, include_scope) if source_scope is not None else include_scope
    if source_scope is not None:
        stmt = stmt.where(source_scope)
    if working_mag_limit is not None:
        mag_limit = _source_mag_sql_expression(mag_key) <= float(working_mag_limit)
        if include_ids:
            mag_limit = or_(mag_limit, CatalogSource.catalog_source_id.in_(include_ids))
        stmt = stmt.where(mag_limit)
    stmt = stmt.order_by(CatalogSource.catalog_source_id)

    sources: list[CatalogMatchSource] = []
    for row in session.execute(stmt).mappings().all():
        sources.append(
            CatalogMatchSource(
                catalog_source_id=int(row["catalog_source_id"]),
                source_id=str(row["source_id"]),
                ra_deg=float(row["ra_deg"]),
                dec_deg=float(row["dec_deg"]),
                ref_mags=dict(row["ref_mags"] or {}),
            )
        )
    return sources


def _sky_query_boxes(
    detections_by_frame: dict[int, list[DetectionMatchInput]],
    *,
    radius_arcsec: float,
) -> list[_SkyQueryBox]:
    boxes: list[_SkyQueryBox] = []
    for detections in detections_by_frame.values():
        positions = [
            (float(det.ra_deg), float(det.dec_deg))
            for det in detections
            if _safe_float(det.ra_deg) is not None and _safe_float(det.dec_deg) is not None
        ]
        boxes.extend(_sky_query_boxes_for_positions(positions, radius_arcsec=radius_arcsec))
    return boxes


def _sky_query_boxes_for_positions(
    positions: list[tuple[float, float]],
    *,
    radius_arcsec: float,
) -> list[_SkyQueryBox]:
    if not positions:
        return []
    radius_deg = min(180.0, float(radius_arcsec) / 3600.0)
    dec_values = [max(-90.0, min(90.0, dec)) for _, dec in positions]
    dec_min = max(-90.0, min(dec_values) - radius_deg)
    dec_max = min(90.0, max(dec_values) + radius_deg)

    ra_values = sorted(float(ra) % 360.0 for ra, _ in positions)
    if len(ra_values) == 1:
        ra_start = ra_end = ra_values[0]
        ra_span = 0.0
    else:
        gaps = [
            (ra_values[(index + 1) % len(ra_values)] + (360.0 if index == len(ra_values) - 1 else 0.0) - value,
             index)
            for index, value in enumerate(ra_values)
        ]
        largest_gap, gap_index = max(gaps)
        ra_start = ra_values[(gap_index + 1) % len(ra_values)]
        ra_end = ra_values[gap_index]
        if ra_end < ra_start:
            ra_end += 360.0
        ra_span = max(0.0, 360.0 - largest_gap)

    if dec_min <= -90.0 or dec_max >= 90.0:
        ra_margin = 180.0
    else:
        cos_floor = math.cos(math.radians(max(abs(dec_min), abs(dec_max))))
        ratio = math.sin(math.radians(radius_deg)) / max(cos_floor, 1e-12)
        ra_margin = (
            180.0
            if ratio >= 1.0
            else math.degrees(math.asin(ratio))
        )
    if ra_span + 2.0 * ra_margin >= 360.0:
        return [_SkyQueryBox(0.0, 360.0, dec_min, dec_max)]

    expanded_start = ra_start - ra_margin
    expanded_end = ra_end + ra_margin
    normalized_start = expanded_start % 360.0
    normalized_end = expanded_end % 360.0
    if expanded_start < 0.0 or expanded_end >= 360.0 or normalized_start > normalized_end:
        return [
            _SkyQueryBox(normalized_start, 360.0, dec_min, dec_max),
            _SkyQueryBox(0.0, normalized_end, dec_min, dec_max),
        ]
    return [_SkyQueryBox(normalized_start, normalized_end, dec_min, dec_max)]


class _CatalogSkyIndex:
    """Uniform hash over unit-sphere coordinates for bounded radius lookups."""

    def __init__(self, sources: list[CatalogMatchSource], *, radius_arcsec: float):
        self.radius_rad = math.radians(float(radius_arcsec) / 3600.0)
        self.chord_radius = 2.0 * math.sin(self.radius_rad / 2.0)
        self.cell_size = max(self.chord_radius, 1e-12)
        self.chord_radius_squared = self.chord_radius * self.chord_radius
        self.cells: dict[tuple[int, int, int], list[tuple[CatalogMatchSource, tuple[float, float, float]]]] = {}
        self.total_query_checks = 0
        self.last_query_checks = 0
        self.indexed_sources = 0
        for source in sources:
            vector = _unit_vector(source.ra_deg, source.dec_deg)
            if vector is None:
                continue
            self.cells.setdefault(self._cell(vector), []).append((source, vector))
            self.indexed_sources += 1

    def _cell(self, vector: tuple[float, float, float]) -> tuple[int, int, int]:
        return tuple(int(math.floor(value / self.cell_size)) for value in vector)

    def query(self, ra_deg: float, dec_deg: float) -> list[CatalogMatchSource]:
        vector = _unit_vector(ra_deg, dec_deg)
        self.last_query_checks = 0
        if vector is None:
            return []
        cx, cy, cz = self._cell(vector)
        matches: list[CatalogMatchSource] = []
        for ix in range(cx - 1, cx + 2):
            for iy in range(cy - 1, cy + 2):
                for iz in range(cz - 1, cz + 2):
                    for source, source_vector in self.cells.get((ix, iy, iz), ()):
                        self.last_query_checks += 1
                        if sum((a - b) ** 2 for a, b in zip(vector, source_vector, strict=True)) <= (
                            self.chord_radius_squared * (1.0 + 1e-12)
                        ):
                            matches.append(source)
        self.total_query_checks += self.last_query_checks
        matches.sort(key=lambda source: int(source.catalog_source_id))
        return matches


def _unit_vector(ra_deg: float, dec_deg: float) -> tuple[float, float, float] | None:
    ra = _safe_float(ra_deg)
    dec = _safe_float(dec_deg)
    if ra is None or dec is None or not -90.0 <= dec <= 90.0:
        return None
    ra_rad = math.radians(ra % 360.0)
    dec_rad = math.radians(dec)
    cos_dec = math.cos(dec_rad)
    return cos_dec * math.cos(ra_rad), cos_dec * math.sin(ra_rad), math.sin(dec_rad)


def _match_frame(
    *,
    frame: Frame,
    detections: list[DetectionMatchInput],
    candidate_pairs: list[tuple[DetectionMatchInput, CatalogMatchSource]],
    radius_arcsec: float,
    mag_key: str,
    projection_timing: DetectionCatalogQcResult | None = None,
) -> list[dict]:
    if not detections or not candidate_pairs:
        return []

    candidates: list[tuple[float, DetectionMatchInput, CatalogMatchSource]] = []
    for det, source in candidate_pairs:
        sep = _sky_sep_arcsec(det.ra_deg, det.dec_deg, source.ra_deg, source.dec_deg)
        if sep is not None and sep <= radius_arcsec:
            candidates.append((sep, det, source))
    candidates.sort(
        key=lambda item: (
            item[0],
            int(item[1].source_detection_id),
            int(item[2].catalog_source_id),
        )
    )

    used_detections: set[int] = set()
    used_sources: set[int] = set()
    accepted: list[tuple[float, DetectionMatchInput, CatalogMatchSource]] = []
    for sep, det, source in candidates:
        if det.source_detection_id in used_detections:
            continue
        if source.catalog_source_id in used_sources:
            continue
        used_detections.add(det.source_detection_id)
        used_sources.add(source.catalog_source_id)
        accepted.append((sep, det, source))

    projection_started = time.perf_counter()
    pixel_positions = _project_catalog_sources_to_frame(
        frame,
        [source for _, _, source in accepted],
    )
    if projection_timing is not None:
        projection_timing.catalog_projection_sec += float(
            time.perf_counter() - projection_started
        )

    rows: list[dict] = []
    for sep, det, source in accepted:
        projected = pixel_positions.get(int(source.catalog_source_id))
        dx = _safe_sub(det.x, projected[0]) if projected is not None else None
        dy = _safe_sub(det.y, projected[1]) if projected is not None else None
        det_mag_inst = _mag_inst(det.flux)
        cat_mag = _catalog_mag(source, mag_key)
        zp = None
        if det_mag_inst is not None and cat_mag is not None:
            zp = cat_mag - det_mag_inst
        rows.append(
            {
                "source_detection_id": det.source_detection_id,
                "catalog_source_id": source.catalog_source_id,
                "match_status": "MATCHED",
                "sep_arcsec": sep,
                "dx_pix": dx,
                "dy_pix": dy,
                "detection_mag_inst": det_mag_inst,
                "catalog_mag": cat_mag,
                "zero_point": zp,
            }
        )
    return rows


def _project_catalog_sources_to_frame(
    frame: Frame,
    sources: list[CatalogMatchSource],
) -> dict[int, tuple[float, float]]:
    if not sources:
        return {}
    raw_path = getattr(frame, "raw_path", None)
    try:
        wcs, _, _, _ = _frame_wcs(frame, Path(str(raw_path or "")))
        source_ra = np.asarray([source.ra_deg for source in sources], dtype=float)
        source_dec = np.asarray([source.dec_deg for source in sources], dtype=float)
        x_values, y_values, _ = _project_sources(
            wcs,
            source_ra=source_ra,
            source_dec=source_dec,
        )
    except Exception:
        return {}
    projected: dict[int, tuple[float, float]] = {}
    for source, x, y in zip(sources, x_values, y_values, strict=True):
        x_value = _safe_float(x)
        y_value = _safe_float(y)
        if x_value is not None and y_value is not None:
            projected[int(source.catalog_source_id)] = (x_value, y_value)
    return projected


def _candidate_pairs(
    *,
    detections: list[DetectionMatchInput],
    catalog_rows: list[CatalogMatchSource],
    radius_arcsec: float,
    spatial_index: _CatalogSkyIndex | None = None,
) -> list[tuple[DetectionMatchInput, CatalogMatchSource]]:
    """Return sky-radius candidates using a unit-sphere spatial hash."""

    index = spatial_index or _CatalogSkyIndex(catalog_rows, radius_arcsec=radius_arcsec)
    pairs: list[tuple[DetectionMatchInput, CatalogMatchSource]] = []
    for det in detections:
        if _safe_float(det.ra_deg) is None or _safe_float(det.dec_deg) is None:
            continue
        pairs.extend((det, source) for source in index.query(det.ra_deg, det.dec_deg))
    return pairs


def _unique_candidate_sources(
    pairs: list[tuple[DetectionMatchInput, CatalogMatchSource]],
) -> list[CatalogMatchSource]:
    by_id = {int(source.catalog_source_id): source for _, source in pairs}
    return [by_id[source_id] for source_id in sorted(by_id)]


def _build_frame_qc_row(
    *,
    frame: Frame,
    catalog_name: str,
    detections: list[DetectionMatchInput],
    catalog_rows: list[CatalogMatchSource],
    match_rows: list[dict],
    mag_key: str,
    radius_arcsec: float,
    detection_algorithm: str,
) -> dict:
    seps = _finite_array([row.get("sep_arcsec") for row in match_rows])
    dxs = _finite_array([row.get("dx_pix") for row in match_rows])
    dys = _finite_array([row.get("dy_pix") for row in match_rows])
    zps = _finite_array([row.get("zero_point") for row in match_rows])

    zp_median = _median(zps)
    return {
        "frame_id": frame.frame_id,
        "catalog_name": catalog_name,
        "algorithm": QC_ALGORITHM,
        "n_detections": len(detections),
        "n_catalog_sources": len(catalog_rows),
        "n_matched": len(match_rows),
        "zp_n": int(len(zps)),
        "astrom_rms_arcsec": _rms(seps),
        "dx_median_pix": _median(dxs),
        "dy_median_pix": _median(dys),
        "sep_median_arcsec": _median(seps),
        "sep_p95_arcsec": _percentile(seps, 95),
        "zp_median": zp_median,
        "zp_mad": _mad(zps, center=zp_median),
        "details": {
            "mag_key": mag_key,
            "radius_arcsec": radius_arcsec,
            "detection_algorithm": detection_algorithm,
            "match_algorithm": MATCH_ALGORITHM,
            "qc_algorithm": QC_ALGORITHM,
        },
    }


def _upsert_detection_catalog_matches(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    table = DetectionCatalogMatch.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        stmt = insert(table).values(list(chunk))
        # One catalog source may have only one accepted detection per frame and
        # matching algorithm. Earlier versions keyed the upsert by
        # (source_detection_id, catalog_source_id, algorithm); after repeated
        # source-detection reruns that allowed stale matches for old detection IDs
        # to accumulate. The frame/source/catalog key makes this step idempotent.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_detection_catalog_matches_run_frame_catalog_source",
            set_={
                "source_detection_id": stmt.excluded.source_detection_id,
                "match_status": stmt.excluded.match_status,
                "sep_arcsec": stmt.excluded.sep_arcsec,
                "dx_pix": stmt.excluded.dx_pix,
                "dy_pix": stmt.excluded.dy_pix,
                "detection_mag_inst": stmt.excluded.detection_mag_inst,
                "catalog_mag": stmt.excluded.catalog_mag,
                "zero_point": stmt.excluded.zero_point,
            },
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches


def _upsert_frame_qc_rows(
    session: Session,
    rows: list[dict],
    *,
    insert_batch_size: int | None = None,
) -> int:
    if not rows:
        return 0
    table = FrameQc.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        stmt = insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_frame_qc_frame_catalog_algorithm",
            set_={
                "n_detections": stmt.excluded.n_detections,
                "n_catalog_sources": stmt.excluded.n_catalog_sources,
                "n_matched": stmt.excluded.n_matched,
                "zp_n": stmt.excluded.zp_n,
                "astrom_rms_arcsec": stmt.excluded.astrom_rms_arcsec,
                "dx_median_pix": stmt.excluded.dx_median_pix,
                "dy_median_pix": stmt.excluded.dy_median_pix,
                "sep_median_arcsec": stmt.excluded.sep_median_arcsec,
                "sep_p95_arcsec": stmt.excluded.sep_p95_arcsec,
                "zp_median": stmt.excluded.zp_median,
                "zp_mad": stmt.excluded.zp_mad,
            },
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches


def _upsert_frame_qc(session: Session, row: dict) -> None:
    table = FrameQc.__table__
    stmt = insert(table).values(row)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_frame_qc_frame_catalog_algorithm",
        set_={
            "n_detections": stmt.excluded.n_detections,
            "n_catalog_sources": stmt.excluded.n_catalog_sources,
            "n_matched": stmt.excluded.n_matched,
            "zp_n": stmt.excluded.zp_n,
            "astrom_rms_arcsec": stmt.excluded.astrom_rms_arcsec,
            "dx_median_pix": stmt.excluded.dx_median_pix,
            "dy_median_pix": stmt.excluded.dy_median_pix,
            "sep_median_arcsec": stmt.excluded.sep_median_arcsec,
            "sep_p95_arcsec": stmt.excluded.sep_p95_arcsec,
            "zp_median": stmt.excluded.zp_median,
            "zp_mad": stmt.excluded.zp_mad,
            "details": stmt.excluded.details,
        },
    )
    session.execute(stmt)


def _merge_frame_quality_flags(frame: Frame, qc_row: dict) -> None:
    flags = dict(frame.quality_flags or {})
    qc_flags = dict(flags.get("detection_catalog_qc") or {})
    qc_flags[qc_row["catalog_name"]] = {
        "algorithm": qc_row["algorithm"],
        "n_detections": qc_row["n_detections"],
        "n_catalog_sources": qc_row["n_catalog_sources"],
        "n_matched": qc_row["n_matched"],
        "astrom_rms_arcsec": qc_row["astrom_rms_arcsec"],
        "sep_median_arcsec": qc_row["sep_median_arcsec"],
        "zp_median": qc_row["zp_median"],
        "zp_mad": qc_row["zp_mad"],
    }
    flags["detection_catalog_qc"] = qc_flags
    frame.quality_flags = flags


def _sky_sep_arcsec(ra1_deg, dec1_deg, ra2_deg, dec2_deg) -> float | None:
    try:
        ra1 = math.radians(float(ra1_deg))
        dec1 = math.radians(float(dec1_deg))
        ra2 = math.radians(float(ra2_deg))
        dec2 = math.radians(float(dec2_deg))
    except Exception:
        return None
    if not all(math.isfinite(x) for x in (ra1, dec1, ra2, dec2)):
        return None
    dra = (ra1 - ra2 + math.pi) % (2 * math.pi) - math.pi
    ddec = dec1 - dec2
    a = math.sin(ddec / 2) ** 2 + math.cos(dec1) * math.cos(dec2) * math.sin(dra / 2) ** 2
    a = min(1.0, max(0.0, a))
    return math.degrees(2 * math.asin(math.sqrt(a))) * 3600.0


def _catalog_mag(source: CatalogMatchSource, mag_key: str) -> float | None:
    mags = source.ref_mags or {}
    if mag_key in mags:
        return _safe_float(mags.get(mag_key))
    # Common aliases for Gaia.
    aliases = {
        "phot_g_mean_mag": "G",
        "phot_bp_mean_mag": "BP",
        "phot_rp_mean_mag": "RP",
    }
    alias = aliases.get(mag_key)
    if alias and alias in mags:
        return _safe_float(mags.get(alias))
    return None


def _mag_inst(flux) -> float | None:
    x = _safe_float(flux)
    if x is None or x <= 0:
        return None
    return -2.5 * math.log10(x)


def _safe_float(value) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _safe_sub(a, b) -> float | None:
    x = _safe_float(a)
    y = _safe_float(b)
    if x is None or y is None:
        return None
    return x - y


def _finite_array(values) -> np.ndarray:
    out = []
    for value in values:
        x = _safe_float(value)
        if x is not None:
            out.append(x)
    return np.asarray(out, dtype=float)


def _median(arr: np.ndarray) -> float | None:
    if arr.size == 0:
        return None
    return float(np.nanmedian(arr))


def _mad(arr: np.ndarray, *, center: float | None) -> float | None:
    if arr.size == 0 or center is None:
        return None
    return float(np.nanmedian(np.abs(arr - center)))


def _rms(arr: np.ndarray) -> float | None:
    if arr.size == 0:
        return None
    return float(math.sqrt(float(np.nanmean(arr * arr))))


def _percentile(arr: np.ndarray, q: float) -> float | None:
    if arr.size == 0:
        return None
    return float(np.nanpercentile(arr, q))
