from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.models import (
    CatalogSource,
    DetectionCatalogMatch,
    Frame,
    FrameCatalogLink,
    FrameQc,
    SourceDetection,
)

MATCH_ALGORITHM = "nearest_sky_v1"
QC_ALGORITHM = "detection_catalog_qc_v1"


@dataclass
class DetectionCatalogQcResult:
    selected_frames: int = 0
    checked_frames: int = 0
    selected_detections: int = 0
    selected_catalog_sources: int = 0
    matched: int = 0
    frame_qc_written: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    deleted_existing_matches: int = 0
    failed_frames: int = 0
    dry_run: bool = False
    catalog_name: str = ""


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
    min_snr: float | None = None,
    limit_frames: int | None = None,
    replace_existing: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> DetectionCatalogQcResult:
    """Cross-match stored detections with catalog sources already projected into frames.

    Matching is greedy one-to-one per frame. It chooses the nearest catalog source
    within ``radius_arcsec`` for every detection, sorted by increasing separation.
    The resulting matches are stored in ``detection_catalog_matches`` and per-frame
    aggregate QC is stored in ``frame_qc``.
    """

    if radius_arcsec <= 0:
        raise ValueError("radius_arcsec must be positive")

    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result = DetectionCatalogQcResult(
        selected_frames=len(frames),
        dry_run=dry_run,
        catalog_name=catalog_name,
        insert_batch_size=int(insert_batch_size),
    )

    for frame in tqdm(frames, desc="Matching detections to catalog", unit="frame"):
        result.checked_frames += 1
        try:
            detections = _select_detections(
                session,
                frame_id=frame.frame_id,
                algorithm=detection_algorithm,
                min_snr=min_snr,
            )
            catalog_rows = _select_catalog_sources_for_frame(
                session,
                frame_id=frame.frame_id,
                catalog_name=catalog_name,
            )
            result.selected_detections += len(detections)
            result.selected_catalog_sources += len(catalog_rows)

            rows = _match_frame(
                frame=frame,
                detections=detections,
                catalog_rows=catalog_rows,
                catalog_name=catalog_name,
                radius_arcsec=radius_arcsec,
                mag_key=mag_key,
            )
            result.matched += len(rows)

            qc_row = _build_frame_qc_row(
                frame=frame,
                catalog_name=catalog_name,
                detections=detections,
                catalog_rows=catalog_rows,
                match_rows=rows,
                mag_key=mag_key,
                radius_arcsec=radius_arcsec,
                detection_algorithm=detection_algorithm,
            )

            if dry_run:
                continue

            if replace_existing:
                deleted = session.execute(
                    delete(DetectionCatalogMatch).where(
                        DetectionCatalogMatch.frame_id == frame.frame_id,
                        DetectionCatalogMatch.catalog_name == catalog_name,
                        DetectionCatalogMatch.algorithm == MATCH_ALGORITHM,
                    )
                ).rowcount or 0
                result.deleted_existing_matches += deleted

            if rows:
                result.insert_batches += _upsert_detection_catalog_matches(session, rows, insert_batch_size=insert_batch_size)
            _upsert_frame_qc(session, qc_row)
            _merge_frame_quality_flags(frame, qc_row)
            result.frame_qc_written += 1
        except Exception:
            result.failed_frames += 1

    if not dry_run:
        session.commit()
    return result


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


def _select_detections(
    session: Session,
    *,
    frame_id: int,
    algorithm: str,
    min_snr: float | None,
) -> list[SourceDetection]:
    stmt = (
        select(SourceDetection)
        .where(
            SourceDetection.frame_id == frame_id,
            SourceDetection.algorithm == algorithm,
            SourceDetection.detection_status == "DETECTED",
            SourceDetection.ra_deg.is_not(None),
            SourceDetection.dec_deg.is_not(None),
        )
        .order_by(SourceDetection.snr.desc().nullslast())
    )
    if min_snr is not None:
        stmt = stmt.where(SourceDetection.snr >= float(min_snr))
    return list(session.scalars(stmt).all())


def _select_catalog_sources_for_frame(
    session: Session,
    *,
    frame_id: int,
    catalog_name: str,
) -> list[tuple[CatalogSource, FrameCatalogLink]]:
    stmt = (
        select(CatalogSource, FrameCatalogLink)
        .select_from(FrameCatalogLink)
        .join(CatalogSource, CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id)
        .where(
            FrameCatalogLink.frame_id == frame_id,
            FrameCatalogLink.in_footprint.is_(True),
            CatalogSource.catalog_name == catalog_name,
        )
        .order_by(CatalogSource.catalog_source_id)
    )
    return [(row[0], row[1]) for row in session.execute(stmt).all()]


def _match_frame(
    *,
    frame: Frame,
    detections: list[SourceDetection],
    catalog_rows: list[tuple[CatalogSource, FrameCatalogLink]],
    catalog_name: str,
    radius_arcsec: float,
    mag_key: str,
) -> list[dict]:
    if not detections or not catalog_rows:
        return []

    candidates: list[tuple[float, SourceDetection, CatalogSource, FrameCatalogLink]] = []
    for det, source, link in _candidate_pairs(frame=frame, detections=detections, catalog_rows=catalog_rows, radius_arcsec=radius_arcsec):
        if det.ra_deg is None or det.dec_deg is None:
            continue
        sep = _sky_sep_arcsec(det.ra_deg, det.dec_deg, source.ra_deg, source.dec_deg)
        if sep is not None and sep <= radius_arcsec:
            candidates.append((sep, det, source, link))
    candidates.sort(key=lambda item: item[0])

    used_detections: set[int] = set()
    used_sources: set[int] = set()
    rows: list[dict] = []
    for sep, det, source, link in candidates:
        if det.source_detection_id in used_detections:
            continue
        if source.catalog_source_id in used_sources:
            continue
        used_detections.add(det.source_detection_id)
        used_sources.add(source.catalog_source_id)

        dx = _safe_sub(det.x, link.x)
        dy = _safe_sub(det.y, link.y)
        det_mag_inst = _mag_inst(det.flux)
        cat_mag = _catalog_mag(source, mag_key)
        zp = None
        if det_mag_inst is not None and cat_mag is not None:
            zp = cat_mag - det_mag_inst
        rows.append(
            {
                "frame_id": frame.frame_id,
                "source_detection_id": det.source_detection_id,
                "catalog_source_id": source.catalog_source_id,
                "catalog_name": catalog_name,
                "algorithm": MATCH_ALGORITHM,
                "match_status": "MATCHED",
                "sep_arcsec": sep,
                "dx_pix": dx,
                "dy_pix": dy,
                "detection_mag_inst": det_mag_inst,
                "catalog_mag": cat_mag,
                "zero_point": zp,
                "details": {
                    "catalog_source_id": source.catalog_source_id,
                    "source_id": source.source_id,
                    "mag_key": mag_key,
                    "detection_algorithm": det.algorithm,
                    "catalog_x": link.x,
                    "catalog_y": link.y,
                    "detection_snr": det.snr,
                    "detection_peak_adu": det.peak_adu,
                },
            }
        )
    return rows


def _candidate_pairs(
    *,
    frame: Frame,
    detections: list[SourceDetection],
    catalog_rows: list[tuple[CatalogSource, FrameCatalogLink]],
    radius_arcsec: float,
) -> list[tuple[SourceDetection, CatalogSource, FrameCatalogLink]]:
    """Return plausible detection/catalog pairs using a pixel-space prefilter.

    The final acceptance is still performed in sky coordinates.  This grid only
    prevents the O(N_detection * N_catalog) all-pairs loop that becomes very slow
    for auto-generated Gaia catalogs.
    """

    radius_pix = _prefilter_radius_pix(frame, radius_arcsec=radius_arcsec)
    if radius_pix is None:
        return [(det, source, link) for det in detections for source, link in catalog_rows]

    cell = max(1.0, float(radius_pix))
    r2 = float(radius_pix) * float(radius_pix)
    grid: dict[tuple[int, int], list[tuple[CatalogSource, FrameCatalogLink]]] = {}
    for source, link in catalog_rows:
        x = _safe_float(link.x)
        y = _safe_float(link.y)
        if x is None or y is None:
            continue
        key = (int(math.floor(x / cell)), int(math.floor(y / cell)))
        grid.setdefault(key, []).append((source, link))

    pairs: list[tuple[SourceDetection, CatalogSource, FrameCatalogLink]] = []
    neighbor_span = max(1, int(math.ceil(radius_pix / cell)))
    for det in detections:
        dx0 = _safe_float(det.x)
        dy0 = _safe_float(det.y)
        if dx0 is None or dy0 is None:
            continue
        cx = int(math.floor(dx0 / cell))
        cy = int(math.floor(dy0 / cell))
        for ix in range(cx - neighbor_span, cx + neighbor_span + 1):
            for iy in range(cy - neighbor_span, cy + neighbor_span + 1):
                for source, link in grid.get((ix, iy), []):
                    lx = _safe_float(link.x)
                    ly = _safe_float(link.y)
                    if lx is None or ly is None:
                        continue
                    if (dx0 - lx) ** 2 + (dy0 - ly) ** 2 <= r2:
                        pairs.append((det, source, link))
    return pairs


def _prefilter_radius_pix(frame: Frame, *, radius_arcsec: float) -> float | None:
    scale = None
    try:
        unit = frame.instrument_unit
        if unit is not None and unit.pixel_scale_arcsec is not None:
            scale = float(unit.pixel_scale_arcsec)
    except Exception:
        scale = None
    if scale is None or not math.isfinite(scale) or scale <= 0:
        return None
    # Conservative factor protects binned frames and imperfect WCS scale metadata.
    return max(2.0, float(radius_arcsec) / scale * 4.0)


def _build_frame_qc_row(
    *,
    frame: Frame,
    catalog_name: str,
    detections: list[SourceDetection],
    catalog_rows: list[tuple[CatalogSource, FrameCatalogLink]],
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
            constraint="uq_detection_catalog_matches_frame_source_catalog_algorithm",
            set_={
                "source_detection_id": stmt.excluded.source_detection_id,
                "match_status": stmt.excluded.match_status,
                "sep_arcsec": stmt.excluded.sep_arcsec,
                "dx_pix": stmt.excluded.dx_pix,
                "dy_pix": stmt.excluded.dy_pix,
                "detection_mag_inst": stmt.excluded.detection_mag_inst,
                "catalog_mag": stmt.excluded.catalog_mag,
                "zero_point": stmt.excluded.zero_point,
                "details": stmt.excluded.details,
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


def _catalog_mag(source: CatalogSource, mag_key: str) -> float | None:
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
