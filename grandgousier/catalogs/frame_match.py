from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from pathlib import Path

from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.models import CatalogSource, Frame, FrameCatalogLink


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
    max_sources_per_frame: int | None = None,
    edge_margin_pix: float = 0.0,
    replace_existing: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> MatchCatalogResult:
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
    if working_mag_limit is not None:
        sources = [src for src in sources if _source_mag(src, mag_key) is not None and _source_mag(src, mag_key) <= float(working_mag_limit)]
        result.mag_limited_sources = len(sources)
    else:
        result.mag_limited_sources = len(sources)
    sources.sort(key=lambda src: (_source_mag(src, mag_key) is None, _source_mag(src, mag_key) if _source_mag(src, mag_key) is not None else 99.0, int(src["catalog_source_id"])))

    if not sources:
        return result

    for frame in tqdm(frames, desc="Matching catalog to frames", unit="frame"):
        result.checked_frames += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        try:
            with fits.open(path, memmap=False) as hdul:
                header = hdul[0].header
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FITSFixedWarning)
                wcs = WCS(header)
            naxis1 = int(header.get("NAXIS1", 0) or 0)
            naxis2 = int(header.get("NAXIS2", 0) or 0)
            if naxis1 <= 0 or naxis2 <= 0 or not wcs.has_celestial:
                result.failed_frames += 1
                continue

            if replace_existing and not dry_run:
                del_stmt = delete(FrameCatalogLink).where(
                    FrameCatalogLink.frame_id == frame.frame_id,
                    FrameCatalogLink.catalog_source_id.in_(
                        select(CatalogSource.catalog_source_id).where(CatalogSource.catalog_name == catalog_name)
                    ),
                )
                result.deleted_existing += session.execute(del_stmt).rowcount or 0

            rows = []
            for source in sources:
                try:
                    x, y = wcs.world_to_pixel_values(source["ra_deg"], source["dec_deg"])
                    x = float(x)
                    y = float(y)
                except Exception:
                    continue
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                if edge_margin_pix and edge_margin_pix > 0:
                    in_footprint = (edge_margin_pix <= x <= (naxis1 - 1 - edge_margin_pix)) and (
                        edge_margin_pix <= y <= (naxis2 - 1 - edge_margin_pix)
                    )
                else:
                    in_footprint = (-margin_pix <= x <= (naxis1 - 1 + margin_pix)) and (
                        -margin_pix <= y <= (naxis2 - 1 + margin_pix)
                    )
                if not in_footprint:
                    continue
                rows.append(
                    {
                        "frame_id": frame.frame_id,
                        "catalog_source_id": source["catalog_source_id"],
                        "x": x,
                        "y": y,
                        "in_footprint": True,
                        "margin_pix": float(margin_pix),
                        "match_status": "IN_FOOTPRINT",
                        "details": {"naxis1": naxis1, "naxis2": naxis2, "mag_key": mag_key, "ref_mag": _source_mag(source, mag_key), "working_mag_limit": working_mag_limit, "max_sources_per_frame": max_sources_per_frame, "edge_margin_pix": edge_margin_pix},
                    }
                )

            if max_sources_per_frame is not None and len(rows) > int(max_sources_per_frame):
                rows.sort(key=lambda row: (row["details"].get("ref_mag") is None, row["details"].get("ref_mag") if row["details"].get("ref_mag") is not None else 99.0, row["catalog_source_id"]))
                result.limited_links += len(rows) - int(max_sources_per_frame)
                rows = rows[: int(max_sources_per_frame)]

            if rows and not dry_run:
                result.insert_batches += _upsert_frame_catalog_links(
                    session,
                    rows,
                    insert_batch_size=insert_batch_size,
                )
            result.linked += len(rows)
        except Exception:
            result.failed_frames += 1
    if not dry_run:
        session.commit()
    return result


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
    if frame_ids:
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
