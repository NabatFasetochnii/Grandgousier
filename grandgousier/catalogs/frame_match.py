from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.catalog_link_io import PostgresFrameCatalogLinkCopyStage, postgres_copy_capable
from grandgousier.db.models import CatalogSource, Frame, FrameCatalogLink


COPY_STAGE_BUFFER_ROWS = 8192


@dataclass
class MatchCatalogResult:
    selected_frames: int = 0
    checked_frames: int = 0
    selected_sources: int = 0
    linked: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_missing_file: int = 0
    failed_frames: int = 0
    deleted_existing: int = 0
    mag_limited_sources: int = 0
    limited_links: int = 0
    dry_run: bool = False

    # 0.5.40.6 performance/provenance telemetry.
    write_strategy: str = "SQLALCHEMY_BATCHED_UPSERT"
    projected_source_points: int = 0
    vectorized_frames: int = 0
    scalar_fallback_frames: int = 0
    header_cache_frames: int = 0
    fits_header_frames: int = 0
    staging_rows: int = 0
    staging_copy_batches: int = 0
    source_load_sec: float = 0.0
    header_wcs_sec: float = 0.0
    wcs_projection_sec: float = 0.0
    row_build_sec: float = 0.0
    delete_existing_sec: float = 0.0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    write_finalize_sec: float = 0.0


def match_catalog_to_frames(
    session: Session,
    *,
    catalog_name: str,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    margin_pix: float = 0.0,
    limit_frames: int | None = None,
    limit_sources: int | None = None,
    mag_key: str = "G",
    working_mag_limit: float | None = None,
    include_catalog_source_ids: list[int] | None = None,
    eligible_catalog_source_ids: list[int] | None = None,
    priority_catalog_source_ids: list[int] | None = None,
    max_sources_per_frame: int | None = None,
    edge_margin_pix: float = 0.0,
    replace_existing: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> MatchCatalogResult:
    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

    result = MatchCatalogResult(dry_run=dry_run, insert_batch_size=int(insert_batch_size))
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.selected_frames = len(frames)

    source_started = time.perf_counter()
    sources_stmt = (
        select(
            CatalogSource.catalog_source_id,
            CatalogSource.ra_deg,
            CatalogSource.dec_deg,
            CatalogSource.ref_mags,
        )
        .where(CatalogSource.catalog_name == catalog_name)
        .order_by(CatalogSource.catalog_source_id)
    )
    if limit_sources is not None:
        sources_stmt = sources_stmt.limit(limit_sources)
    sources = [dict(row._mapping) for row in session.execute(sources_stmt).all()]
    result.selected_sources = len(sources)

    # Compute the working magnitude once per source. The previous implementation
    # decoded the JSONB magnitude repeatedly during filtering, sorting and row
    # construction.
    for source in sources:
        source["_match_mag"] = _source_mag(source, mag_key)
    included_source_ids = {
        int(source_id) for source_id in (include_catalog_source_ids or [])
    }
    eligible_source_ids = (
        None
        if eligible_catalog_source_ids is None
        else {int(source_id) for source_id in eligible_catalog_source_ids}
    )
    priority_source_ids = {
        int(source_id) for source_id in (priority_catalog_source_ids or [])
    }
    if working_mag_limit is not None:
        limit_value = float(working_mag_limit)
        sources = [
            source
            for source in sources
            if (
                source["_match_mag"] is not None
                and source["_match_mag"] <= limit_value
            )
            or int(source["catalog_source_id"]) in included_source_ids
        ]
    result.mag_limited_sources = len(sources)
    if eligible_source_ids is not None:
        eligible_source_ids.update(included_source_ids)
        sources = [
            source
            for source in sources
            if int(source["catalog_source_id"]) in eligible_source_ids
        ]
    sources.sort(
        key=lambda source: (
            source["_match_mag"] is None,
            source["_match_mag"] if source["_match_mag"] is not None else 99.0,
            int(source["catalog_source_id"]),
        )
    )
    result.source_load_sec = float(time.perf_counter() - source_started)

    if not sources:
        return result

    source_ids = [int(source["catalog_source_id"]) for source in sources]
    source_mags = [source["_match_mag"] for source in sources]
    source_ra = np.asarray([float(source["ra_deg"]) for source in sources], dtype=float)
    source_dec = np.asarray([float(source["dec_deg"]) for source in sources], dtype=float)

    copy_stage: PostgresFrameCatalogLinkCopyStage | None = None
    if dry_run:
        result.write_strategy = "DRY_RUN"
    elif postgres_copy_capable(session):
        copy_stage = PostgresFrameCatalogLinkCopyStage.create(session)
        result.write_strategy = "POSTGRESQL_COPY_STAGING"
    else:
        result.write_strategy = "SQLALCHEMY_BATCHED_UPSERT"

    successful_frame_ids: list[int] = []
    fallback_rows: list[dict[str, Any]] = []
    copy_buffer: list[dict[str, Any]] = []

    try:
        for frame in tqdm(frames, desc="Matching catalog to frames", unit="frame"):
            result.checked_frames += 1
            path = Path(frame.raw_path)
            if not path.exists():
                result.skipped_missing_file += 1
                continue
            try:
                header_started = time.perf_counter()
                wcs, naxis1, naxis2, header_source = _frame_wcs(frame, path)
                result.header_wcs_sec += float(time.perf_counter() - header_started)
                if header_source == "DB_HEADER_JSON":
                    result.header_cache_frames += 1
                else:
                    result.fits_header_frames += 1
                if naxis1 <= 0 or naxis2 <= 0 or not wcs.has_celestial:
                    result.failed_frames += 1
                    continue

                projection_started = time.perf_counter()
                x_values, y_values, vectorized = _project_sources(
                    wcs,
                    source_ra=source_ra,
                    source_dec=source_dec,
                )
                result.wcs_projection_sec += float(time.perf_counter() - projection_started)
                result.projected_source_points += len(source_ids)
                if vectorized:
                    result.vectorized_frames += 1
                else:
                    result.scalar_fallback_frames += 1

                row_started = time.perf_counter()
                valid = np.isfinite(x_values) & np.isfinite(y_values)
                if edge_margin_pix and edge_margin_pix > 0:
                    edge = float(edge_margin_pix)
                    valid &= (
                        (x_values >= edge)
                        & (x_values <= (naxis1 - 1 - edge))
                        & (y_values >= edge)
                        & (y_values <= (naxis2 - 1 - edge))
                    )
                else:
                    margin = float(margin_pix)
                    valid &= (
                        (x_values >= -margin)
                        & (x_values <= (naxis1 - 1 + margin))
                        & (y_values >= -margin)
                        & (y_values <= (naxis2 - 1 + margin))
                    )

                selected_indices = np.flatnonzero(valid)
                if max_sources_per_frame is not None and len(selected_indices) > int(max_sources_per_frame):
                    result.limited_links += len(selected_indices) - int(max_sources_per_frame)
                    # Preserve the exact legacy brightness/source-id order apart
                    # from placing explicit target priorities first at the cap.
                    selected_order = sorted(
                        selected_indices.tolist(),
                        key=lambda index: (
                            0 if source_ids[index] in priority_source_ids else 1,
                            index,
                        ),
                    )
                    selected_indices = np.asarray(
                        selected_order[: int(max_sources_per_frame)], dtype=int
                    )

                common_details = {
                    "naxis1": naxis1,
                    "naxis2": naxis2,
                    "mag_key": mag_key,
                    "working_mag_limit": working_mag_limit,
                    "max_sources_per_frame": max_sources_per_frame,
                    "edge_margin_pix": edge_margin_pix,
                }
                rows = [
                    {
                        "frame_id": frame.frame_id,
                        "catalog_source_id": source_ids[index],
                        "x": float(x_values[index]),
                        "y": float(y_values[index]),
                        "in_footprint": True,
                        "margin_pix": float(margin_pix),
                        "match_status": "IN_FOOTPRINT",
                        "details": {**common_details, "ref_mag": source_mags[index]},
                    }
                    for index in selected_indices.tolist()
                ]
                result.row_build_sec += float(time.perf_counter() - row_started)

                successful_frame_ids.append(int(frame.frame_id))
                result.linked += len(rows)
            except Exception:
                result.failed_frames += 1
                continue

            # Keep database write errors outside the per-frame WCS compatibility
            # guard: a COPY/SQL failure must abort the stage, not be mislabeled as
            # one bad astronomical frame. Buffer several frames per COPY call to
            # amortize protocol/cursor overhead without retaining the whole match.
            if rows and not dry_run:
                if copy_stage is not None:
                    copy_buffer.extend(rows)
                    if len(copy_buffer) >= COPY_STAGE_BUFFER_ROWS:
                        copy_stage.append_rows(copy_buffer)
                        copy_buffer.clear()
                else:
                    fallback_rows.extend(rows)

        if not dry_run:
            if copy_stage is not None and copy_buffer:
                copy_stage.append_rows(copy_buffer)
                copy_buffer.clear()

            if replace_existing and successful_frame_ids:
                delete_started = time.perf_counter()
                del_stmt = delete(FrameCatalogLink).where(
                    FrameCatalogLink.frame_id.in_(successful_frame_ids),
                    FrameCatalogLink.catalog_source_id.in_(
                        select(CatalogSource.catalog_source_id).where(
                            CatalogSource.catalog_name == catalog_name
                        )
                    ),
                )
                result.deleted_existing += session.execute(del_stmt).rowcount or 0
                result.delete_existing_sec = float(time.perf_counter() - delete_started)

            if copy_stage is not None:
                result.staging_rows = int(copy_stage.rows_staged)
                result.staging_copy_batches = int(copy_stage.copy_batches)
                result.staging_copy_sec = float(copy_stage.staging_copy_sec)
                if copy_stage.rows_staged:
                    copy_stage.merge(upsert=not replace_existing)
                    result.insert_batches = 1
                result.insert_select_sec = float(copy_stage.insert_select_sec)
            elif fallback_rows:
                result.insert_batches = _upsert_frame_catalog_links(
                    session,
                    fallback_rows,
                    insert_batch_size=insert_batch_size,
                )

            finalize_started = time.perf_counter()
            if copy_stage is not None:
                copy_stage.close()
            session.commit()
            result.write_finalize_sec = float(time.perf_counter() - finalize_started)
    finally:
        if copy_stage is not None and not copy_stage.closed:
            try:
                copy_stage.close()
            except Exception:
                # Preserve the original processing/database exception. A temp
                # table is transaction-local and will also disappear on rollback.
                pass

    return result


def _frame_wcs(frame: Frame, path: Path) -> tuple[WCS, int, int, str]:
    """Build frame WCS from indexed header metadata, with FITS fallback.

    ``frames.header_json`` is captured from the same FITS header during archive
    indexing and normally contains all WCS cards. Avoiding 60–150 repeated FITS
    opens matters on network/rotating archives. The fallback preserves support
    for older rows whose cached header is incomplete or predates a WCS solve.
    """

    cached = frame.header_json if isinstance(frame.header_json, dict) else {}
    if cached:
        try:
            header = fits.Header()
            for key, value in cached.items():
                try:
                    header[str(key)] = value
                except Exception:
                    continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                wcs = WCS(header)
            naxis1 = int(cached.get("NAXIS1", 0) or 0)
            naxis2 = int(cached.get("NAXIS2", 0) or 0)
            if naxis1 > 0 and naxis2 > 0 and wcs.has_celestial:
                return wcs, naxis1, naxis2, "DB_HEADER_JSON"
        except Exception:
            pass

    with fits.open(path, memmap=False) as hdul:
        header = hdul[0].header.copy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        wcs = WCS(header)
    return (
        wcs,
        int(header.get("NAXIS1", 0) or 0),
        int(header.get("NAXIS2", 0) or 0),
        "FITS_HEADER",
    )


def _project_sources(
    wcs: WCS,
    *,
    source_ra: np.ndarray,
    source_dec: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """Project the complete working catalog in one WCS call when possible."""

    try:
        x_values, y_values = wcs.world_to_pixel_values(source_ra, source_dec)
        x_array = np.asarray(x_values, dtype=float)
        y_array = np.asarray(y_values, dtype=float)
        if x_array.shape != source_ra.shape or y_array.shape != source_dec.shape:
            raise ValueError("vectorized WCS projection returned an unexpected shape")
        return x_array, y_array, True
    except Exception:
        # Some unusual WCS/distortion implementations may not support vector
        # input. Keep the exact old resilience semantics as a compatibility path.
        x_array = np.full(source_ra.shape, np.nan, dtype=float)
        y_array = np.full(source_dec.shape, np.nan, dtype=float)
        for index, (ra_deg, dec_deg) in enumerate(zip(source_ra, source_dec, strict=True)):
            try:
                x, y = wcs.world_to_pixel_values(float(ra_deg), float(dec_deg))
                x_array[index] = float(x)
                y_array[index] = float(y)
            except Exception:
                continue
        return x_array, y_array, False


def _upsert_frame_catalog_links(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    if not rows:
        return 0
    table = FrameCatalogLink.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        stmt = insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_frame_catalog_links_frame_source",
            set_={
                "x": stmt.excluded.x,
                "y": stmt.excluded.y,
                "in_footprint": stmt.excluded.in_footprint,
                "margin_pix": stmt.excluded.margin_pix,
                "match_status": stmt.excluded.match_status,
                "details": stmt.excluded.details,
            },
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches


def _source_mag(source: dict, mag_key: str) -> float | None:
    ref_mags = source.get("ref_mags") or {}
    if not isinstance(ref_mags, dict):
        return None
    candidates = [mag_key, mag_key.upper(), mag_key.lower(), "G", "g", "phot_g_mean_mag"]
    for key in candidates:
        if key in ref_mags:
            try:
                value = float(ref_mags[key])
            except Exception:
                continue
            if math.isfinite(value):
                return value
    return None


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
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
