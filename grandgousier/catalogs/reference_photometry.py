from __future__ import annotations

import hashlib
import io
import math
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from grandgousier.catalogs.import_catalog import read_catalog_table
from grandgousier.db.models import (
    CatalogSource,
    FrameCatalogLink,
    PhotometricBand,
    ReferencePhotometryCatalog,
    ReferencePhotometryPoint,
    ReferencePhotometrySource,
)
from grandgousier.identity.object import preferred_catalog_identity_for_object

_CANONICAL_MAG_FIELDS = {
    "g": "mag_g",
    "r": "mag_r",
    "i": "mag_i",
    "z": "mag_z",
    "y": "mag_y",
    "b": "mag_b",
    "v": "mag_v",
    "j": "mag_j",
    "h": "mag_h",
    "k": "mag_k",
    "bp": "mag_bp",
    "rp": "mag_rp",
    "g_gaia": "mag_g_gaia",
    "gaia_g": "mag_g_gaia",
}

_CANONICAL_BANDS = ("g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia")
_IMPORT_CHUNK_SIZE = 500


# Tonry et al. (2012), Table 6: Johnson/Cousins Vega magnitudes synthesized
# from Pan-STARRS1 stellar photometry.  The final value is
# m_JC = m_PS1 + A0 + A1*(g-r) + A2*(g-r)^2.
_TONRY2012_PS1_TO_JOHNSON = {
    "b": ("g", 0.212, 0.556, 0.034, 0.032),
    "v": ("r", 0.005, 0.462, 0.013, 0.012),
}


@dataclass
class ImportReferencePhotometryResult:
    catalog_name: str
    path: str
    read_rows: int = 0
    valid_rows: int = 0
    inserted_or_updated: int = 0
    matched_to_gaia: int = 0
    unmatched_to_gaia: int = 0
    skipped_bad_coordinates: int = 0
    skipped_duplicate_source_ids: int = 0
    dry_run: bool = False


@dataclass
class ReferencePhotometryDiagnostics:
    field_id: int | None = None
    catalog_name: str | None = None
    identity_catalog_name: str | None = None
    n_identity_sources: int = 0
    n_sources_with_any_reference: int = 0
    catalog_rows: list[dict] = field(default_factory=list)
    band_rows: list[dict] = field(default_factory=list)


@dataclass
class ReferencePhotometryCsvResult:
    provider: str
    path: str
    field_id: int | None = None
    series_id: int | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_arcmin: float | None = None
    raw_rows: int = 0
    standardized_rows: int = 0
    written_rows: int = 0
    skipped_rows: int = 0
    filtered_rows: int = 0
    filter_summary: dict = field(default_factory=dict)
    remote_filter_params: dict = field(default_factory=dict)
    remote_filters_enabled: bool = True
    tls_ca_source: str | None = None
    tls_ca_path: str | None = None
    url: str | None = None
    import_command: str | None = None


def create_reference_photometry_csv(
    session: Session,
    *,
    out_path: str | Path,
    provider: str = "panstarrs_dr2",
    field_id: int | None = None,
    series_id: int | None = None,
    ra_deg: float | None = None,
    dec_deg: float | None = None,
    radius_arcmin: float | None = None,
    overwrite: bool = False,
    limit: int | None = None,
    require_bands: Iterable[str] | None = None,
    min_mag: dict[str, float] | None = None,
    max_mag: dict[str, float] | None = None,
    max_mag_err: dict[str, float] | None = None,
    min_detections: int | None = None,
    use_remote_filters: bool = True,
) -> ReferencePhotometryCsvResult:
    """Create a local reference-photometry CSV for a field/series/cone.

    MVP5.28.1 intentionally keeps network acquisition separate from DB import.
    The output CSV has a stable Grandgousier-friendly column layout and can be
    passed to ``import-reference-photometry`` afterwards.  At this stage only
    the public Pan-STARRS DR2 mean object API is implemented.
    """

    provider_key = _normalize_provider(provider)
    if provider_key != "panstarrs_dr2":
        raise ValueError(f"Unsupported reference CSV provider: {provider!r}. Supported: panstarrs_dr2")

    cone = _resolve_reference_photometry_cone(
        session,
        field_id=field_id,
        series_id=series_id,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        radius_arcmin=radius_arcmin,
    )
    out = Path(out_path)
    if out.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {out}. Use --overwrite to replace it.")
    out.parent.mkdir(parents=True, exist_ok=True)

    df, url, remote_filter_params, tls_info = _fetch_panstarrs_dr2_mean_csv(
        cone["ra_deg"],
        cone["dec_deg"],
        cone["radius_arcmin"] / 60.0,
        require_bands=require_bands,
        min_mag=min_mag,
        max_mag=max_mag,
        max_mag_err=max_mag_err,
        min_detections=min_detections,
        use_remote_filters=use_remote_filters,
    )
    raw_rows = len(df)

    export = _standardize_panstarrs_reference_dataframe(df)
    skipped = raw_rows - len(export)
    filtered, filter_summary = _filter_reference_export_dataframe(
        export,
        require_bands=require_bands,
        min_mag=min_mag,
        max_mag=max_mag,
        max_mag_err=max_mag_err,
        min_detections=min_detections,
    )
    filtered_rows = len(export) - len(filtered)
    filter_summary["remote_filters_enabled"] = bool(use_remote_filters)
    filter_summary["remote_filter_params"] = dict(remote_filter_params)
    filter_summary["remote_query_rows"] = int(raw_rows)
    if limit is not None:
        filtered = filtered.head(limit)
        filter_summary["limit"] = int(limit)
    filtered.to_csv(out, index=False)

    import_command = _make_reference_csv_import_command(out)
    return ReferencePhotometryCsvResult(
        provider=provider_key,
        path=str(out),
        field_id=cone.get("field_id"),
        series_id=cone.get("series_id"),
        ra_deg=cone["ra_deg"],
        dec_deg=cone["dec_deg"],
        radius_arcmin=cone["radius_arcmin"],
        raw_rows=raw_rows,
        standardized_rows=len(export),
        written_rows=len(filtered),
        skipped_rows=skipped,
        filtered_rows=filtered_rows,
        filter_summary=filter_summary,
        remote_filter_params=dict(remote_filter_params),
        remote_filters_enabled=bool(use_remote_filters),
        tls_ca_source=tls_info.get("source"),
        tls_ca_path=tls_info.get("path"),
        url=url,
        import_command=import_command,
    )


def import_reference_photometry(
    session: Session,
    *,
    path: str | Path,
    catalog_name: str,
    catalog_version: str | None = None,
    photometric_system: str | None = None,
    source_priority: int = 100,
    ra_col: str = "ra",
    dec_col: str = "dec",
    source_id_col: str | None = None,
    epoch_col: str | None = None,
    mag_cols: dict[str, str] | None = None,
    mag_err_cols: dict[str, str] | None = None,
    color_cols: dict[str, str] | None = None,
    quality_cols: dict[str, str] | None = None,
    match_to_catalog: str | None = None,
    max_match_arcsec: float = 1.0,
    limit: int | None = None,
    dry_run: bool = False,
) -> ImportReferencePhotometryResult:
    """Import external reference photometry into normalized source/band/point rows.

    Re-importing a source replaces its complete point set, matching the old wide
    row semantics where omitted bands became NULL rather than retaining stale
    measurements from an earlier import.
    """

    df = _read_reference_photometry_table(path, source_id_col=source_id_col)
    result = ImportReferencePhotometryResult(
        catalog_name=catalog_name,
        path=str(path),
        read_rows=len(df),
        dry_run=dry_run,
    )
    if limit is not None:
        df = df.head(limit)

    _require_columns(df, [ra_col, dec_col])
    if source_id_col:
        _require_columns(df, [source_id_col])
    if epoch_col:
        _require_columns(df, [epoch_col])
    for mapping in [mag_cols or {}, mag_err_cols or {}, color_cols or {}, quality_cols or {}]:
        _require_columns(df, list(mapping.values()))

    normalized_mag_cols = _normalize_band_column_mapping(mag_cols or {})
    normalized_mag_err_cols = _normalize_band_column_mapping(mag_err_cols or {})

    refcat_id = None
    if not dry_run:
        refcat_id = _upsert_reference_catalog(
            session,
            catalog_name=catalog_name,
            catalog_version=catalog_version,
            photometric_system=photometric_system,
            source_priority=source_priority,
            metadata_json={"import_path": str(path)},
        )

    identity_sources = []
    if match_to_catalog:
        identity_sources = _load_match_catalog_sources(
            session,
            match_to_catalog,
            df,
            ra_col=ra_col,
            dec_col=dec_col,
        )

    rows: list[dict] = []
    for _, row in df.iterrows():
        ra = _to_float(row.get(ra_col))
        dec = _to_float(row.get(dec_col))
        if ra is None or dec is None or not (0.0 <= ra < 360.0) or not (-90.0 <= dec <= 90.0):
            result.skipped_bad_coordinates += 1
            continue

        if source_id_col:
            source_id = _to_text(row.get(source_id_col))
            if not source_id:
                source_id = _make_coordinate_source_id(catalog_name, ra, dec)
        else:
            source_id = _make_coordinate_source_id(catalog_name, ra, dec)

        colors = _extract_named_float_dict(row, color_cols or {})
        quality_flags = _extract_named_value_dict(row, quality_cols or {})
        raw_json = {str(k): _jsonable(v) for k, v in row.to_dict().items()}

        match = None
        if identity_sources:
            match = _nearest_catalog_source(
                ra,
                dec,
                identity_sources,
                max_match_arcsec=max_match_arcsec,
            )
        if match is not None:
            result.matched_to_gaia += 1
        elif match_to_catalog:
            result.unmatched_to_gaia += 1

        points: list[dict] = []
        for band, col in normalized_mag_cols.items():
            magnitude = _to_float(row.get(col))
            if magnitude is None:
                continue
            error_col = normalized_mag_err_cols.get(band)
            points.append(
                {
                    "band": band,
                    "magnitude": magnitude,
                    "magnitude_error": _to_float(row.get(error_col)) if error_col else None,
                }
            )

        rows.append(
            {
                "catalog_name": catalog_name,  # import-only deduplication key
                "catalog_source_id": source_id,
                "ref_uid": make_reference_photometry_uid(catalog_name, source_id),
                "identity_catalog_name": match["catalog_name"] if match else None,
                "identity_source_id": match["source_id"] if match else None,
                "identity_catalog_source_id": match["catalog_source_id"] if match else None,
                "distance_to_identity_arcsec": match["sep_arcsec"] if match else None,
                "ra_deg": ra,
                "dec_deg": dec,
                "epoch": _to_float(row.get(epoch_col)) if epoch_col else None,
                "colors": colors,
                "quality_flags": quality_flags,
                "raw_json": raw_json,
                "_points": points,
            }
        )

    rows = _deduplicate_reference_rows(rows, result)
    result.valid_rows = len(rows)
    if dry_run or not rows:
        return result
    assert refcat_id is not None

    band_ids = _ensure_photometric_bands(
        session,
        refcat_id=refcat_id,
        photometric_system=photometric_system,
        bands=sorted({point["band"] for row in rows for point in row["_points"]}),
    )

    source_table = ReferencePhotometrySource.__table__
    for chunk_index, chunk in enumerate(_chunks(rows, _IMPORT_CHUNK_SIZE), start=1):
        values = [
            {
                "refcat_id": refcat_id,
                "catalog_source_id": row["catalog_source_id"],
                "ref_uid": row["ref_uid"],
                "identity_catalog_name": row["identity_catalog_name"],
                "identity_source_id": row["identity_source_id"],
                "identity_catalog_source_id": row["identity_catalog_source_id"],
                "distance_to_identity_arcsec": row["distance_to_identity_arcsec"],
                "ra_deg": row["ra_deg"],
                "dec_deg": row["dec_deg"],
                "epoch": row["epoch"],
                "colors": row["colors"],
                "quality_flags": row["quality_flags"],
                "raw_json": row["raw_json"],
            }
            for row in chunk
        ]
        stmt = insert(source_table).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_reference_photometry_sources_catalog_source",
            set_={
                "ref_uid": stmt.excluded.ref_uid,
                "identity_catalog_name": stmt.excluded.identity_catalog_name,
                "identity_source_id": stmt.excluded.identity_source_id,
                "identity_catalog_source_id": stmt.excluded.identity_catalog_source_id,
                "distance_to_identity_arcsec": stmt.excluded.distance_to_identity_arcsec,
                "ra_deg": stmt.excluded.ra_deg,
                "dec_deg": stmt.excluded.dec_deg,
                "epoch": stmt.excluded.epoch,
                "colors": stmt.excluded.colors,
                "quality_flags": stmt.excluded.quality_flags,
                "raw_json": stmt.excluded.raw_json,
                "updated_at": func.now(),
            },
        )
        try:
            session.execute(stmt)
        except SQLAlchemyError as exc:
            session.rollback()
            first_id = chunk[0].get("catalog_source_id") if chunk else None
            last_id = chunk[-1].get("catalog_source_id") if chunk else None
            raise RuntimeError(
                "Reference photometry DB import failed "
                f"at batch={chunk_index}, batch_size={len(chunk)}, "
                f"first_catalog_source_id={first_id!r}, last_catalog_source_id={last_id!r}. "
                f"{_compact_sqlalchemy_error(exc)}"
            ) from None
        result.inserted_or_updated += len(chunk)

    source_id_map: dict[str, int] = {}
    external_ids = [str(row["catalog_source_id"]) for row in rows]
    for id_chunk in _chunks(external_ids, _IMPORT_CHUNK_SIZE):
        found = session.execute(
            select(
                ReferencePhotometrySource.catalog_source_id,
                ReferencePhotometrySource.refphot_source_id,
            ).where(
                ReferencePhotometrySource.refcat_id == refcat_id,
                ReferencePhotometrySource.catalog_source_id.in_(id_chunk),
            )
        ).all()
        source_id_map.update({str(external): int(internal) for external, internal in found})

    internal_source_ids = sorted(source_id_map.values())
    for source_chunk in _chunks(internal_source_ids, _IMPORT_CHUNK_SIZE):
        session.execute(
            delete(ReferencePhotometryPoint).where(
                ReferencePhotometryPoint.refphot_source_id.in_(source_chunk)
            )
        )

    point_rows: list[dict] = []
    for row in rows:
        internal_source_id = source_id_map[str(row["catalog_source_id"])]
        for point in row["_points"]:
            point_rows.append(
                {
                    "refphot_source_id": internal_source_id,
                    "band_id": band_ids[str(point["band"])],
                    "magnitude": float(point["magnitude"]),
                    "magnitude_error": point["magnitude_error"],
                    "quality_flags": {},
                    "provider_payload": {},
                }
            )
    point_table = ReferencePhotometryPoint.__table__
    for point_chunk in _chunks(point_rows, _IMPORT_CHUNK_SIZE):
        session.execute(insert(point_table).values(point_chunk))

    session.commit()
    return result


def summarize_reference_photometry_catalogs(session: Session) -> list[dict]:
    source_counts = session.execute(
        select(
            ReferencePhotometryCatalog.refcat_id,
            ReferencePhotometryCatalog.catalog_name,
            ReferencePhotometryCatalog.catalog_version,
            ReferencePhotometryCatalog.photometric_system,
            ReferencePhotometryCatalog.source_priority,
            func.count(ReferencePhotometrySource.refphot_source_id).label("n_sources"),
            func.count(ReferencePhotometrySource.identity_source_id).label("n_matched_identity"),
        )
        .outerjoin(
            ReferencePhotometrySource,
            ReferencePhotometrySource.refcat_id == ReferencePhotometryCatalog.refcat_id,
        )
        .group_by(
            ReferencePhotometryCatalog.refcat_id,
            ReferencePhotometryCatalog.catalog_name,
            ReferencePhotometryCatalog.catalog_version,
            ReferencePhotometryCatalog.photometric_system,
            ReferencePhotometryCatalog.source_priority,
        )
        .order_by(
            ReferencePhotometryCatalog.source_priority,
            ReferencePhotometryCatalog.catalog_name,
        )
    ).all()
    rows: dict[int, dict] = {}
    for row in source_counts:
        payload = dict(row._mapping)
        refcat_id = int(payload.pop("refcat_id"))
        payload["n_sources"] = int(payload.get("n_sources") or 0)
        payload["n_matched_identity"] = int(payload.get("n_matched_identity") or 0)
        payload["n_matched_gaia"] = payload["n_matched_identity"]  # CLI compatibility
        for band in _CANONICAL_BANDS:
            payload[f"n_{band}"] = 0
        rows[refcat_id] = payload

    band_counts = session.execute(
        select(
            PhotometricBand.refcat_id,
            PhotometricBand.canonical_name,
            func.count(ReferencePhotometryPoint.refphot_point_id),
        )
        .join(ReferencePhotometryPoint, ReferencePhotometryPoint.band_id == PhotometricBand.band_id)
        .group_by(PhotometricBand.refcat_id, PhotometricBand.canonical_name)
    ).all()
    for refcat_id, band, count in band_counts:
        payload = rows.get(int(refcat_id))
        if payload is not None:
            payload[f"n_{str(band)}"] = int(count or 0)
    return list(rows.values())


def find_object_reference_photometry(
    session: Session,
    *,
    object_id: int | None = None,
    identity_catalog_name: str | None = None,
    source_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if object_id is not None and (identity_catalog_name is None or source_id is None):
        identity_catalog_name, source_id = preferred_catalog_identity_for_object(session, int(object_id))
        if identity_catalog_name is None or source_id is None:
            return []

    if not identity_catalog_name or not source_id:
        raise ValueError("Use --object-id or both --identity-catalog-name and --source-id")

    sources = session.scalars(
        select(ReferencePhotometrySource)
        .join(
            ReferencePhotometryCatalog,
            ReferencePhotometryCatalog.refcat_id == ReferencePhotometrySource.refcat_id,
        )
        .where(
            ReferencePhotometrySource.identity_catalog_name == identity_catalog_name,
            ReferencePhotometrySource.identity_source_id == str(source_id),
        )
        .order_by(ReferencePhotometryCatalog.catalog_name)
        .limit(limit)
    ).all()
    payloads = _reference_source_payloads(session, list(sources))
    return [_format_refphot_row(row) for row in payloads]


def diagnose_reference_photometry(
    session: Session,
    *,
    field_id: int | None = None,
    identity_catalog_name: str | None = None,
    reference_catalog_name: str | None = None,
) -> ReferencePhotometryDiagnostics:
    identity_catalog_name = identity_catalog_name or "gaia_dr3_master"
    source_ids = _field_identity_source_ids(
        session,
        field_id=field_id,
        identity_catalog_name=identity_catalog_name,
    )
    out = ReferencePhotometryDiagnostics(
        field_id=field_id,
        catalog_name=reference_catalog_name,
        identity_catalog_name=identity_catalog_name,
        n_identity_sources=len(source_ids),
    )
    if not source_ids:
        return out

    stmt = (
        select(ReferencePhotometrySource)
        .join(
            ReferencePhotometryCatalog,
            ReferencePhotometryCatalog.refcat_id == ReferencePhotometrySource.refcat_id,
        )
        .where(
            ReferencePhotometrySource.identity_catalog_name == identity_catalog_name,
            ReferencePhotometrySource.identity_source_id.in_(source_ids),
        )
    )
    if reference_catalog_name:
        stmt = stmt.where(ReferencePhotometryCatalog.catalog_name == reference_catalog_name)
    sources = list(session.scalars(stmt).all())
    payloads = _reference_source_payloads(session, sources)
    sources_with_ref = {
        str(row["gaia_source_id"])
        for row in payloads
        if row.get("gaia_source_id") is not None
    }
    out.n_sources_with_any_reference = len(sources_with_ref)

    by_catalog: dict[str, dict] = {}
    by_band: dict[tuple[str, str], int] = {}
    for row in payloads:
        cat = str(row["catalog_name"])
        stats = by_catalog.setdefault(
            cat,
            {
                "catalog_name": cat,
                "n_reference_rows": 0,
                "n_identity_sources": 0,
                "median_distance_to_gaia_arcsec": None,
                "_sources": set(),
                "_dist": [],
            },
        )
        stats["n_reference_rows"] += 1
        if row.get("gaia_source_id"):
            stats["_sources"].add(str(row["gaia_source_id"]))
        if row.get("distance_to_gaia_arcsec") is not None:
            stats["_dist"].append(float(row["distance_to_gaia_arcsec"]))
        for band, magnitude in dict(row.get("bands") or {}).items():
            if magnitude is not None:
                by_band[(cat, str(band))] = by_band.get((cat, str(band)), 0) + 1
    for stats in by_catalog.values():
        stats["n_identity_sources"] = len(stats.pop("_sources"))
        dist = sorted(stats.pop("_dist"))
        stats["median_distance_to_gaia_arcsec"] = _median(dist)
        stats["coverage_fraction"] = (
            stats["n_identity_sources"] / len(source_ids) if source_ids else None
        )
        out.catalog_rows.append(stats)
    out.catalog_rows.sort(key=lambda row: row["catalog_name"])

    for (cat, band), count in sorted(by_band.items()):
        out.band_rows.append(
            {
                "catalog_name": cat,
                "band": band,
                "n_band_sources": count,
                "coverage_fraction": count / len(source_ids) if source_ids else None,
            }
        )
    return out


def load_reference_photometry_by_identity(
    session: Session,
    *,
    reference_catalog_name: str,
    identity_catalog_name: str,
    source_ids: Iterable[str],
) -> dict[str, dict]:
    """Return one best normalized reference payload per identity-catalog source."""

    normalized_ids = sorted({str(value) for value in source_ids if value is not None})
    if not normalized_ids:
        return {}
    sources = list(
        session.scalars(
            select(ReferencePhotometrySource)
            .join(
                ReferencePhotometryCatalog,
                ReferencePhotometryCatalog.refcat_id == ReferencePhotometrySource.refcat_id,
            )
            .where(
                ReferencePhotometryCatalog.catalog_name == reference_catalog_name,
                ReferencePhotometrySource.identity_catalog_name == identity_catalog_name,
                ReferencePhotometrySource.identity_source_id.in_(normalized_ids),
            )
        ).all()
    )
    payloads = _reference_source_payloads(session, sources)
    payloads.sort(
        key=lambda row: (
            str(row.get("gaia_source_id") or ""),
            float(row["distance_to_gaia_arcsec"])
            if row.get("distance_to_gaia_arcsec") is not None
            else float("inf"),
            int(row["refphot_source_id"]),
        )
    )
    result: dict[str, dict] = {}
    for payload in payloads:
        identity_source_id = str(payload.get("gaia_source_id") or "")
        if identity_source_id and identity_source_id not in result:
            result[identity_source_id] = payload
    return result


def _reference_source_payloads(
    session: Session,
    sources: list[ReferencePhotometrySource],
) -> list[dict]:
    if not sources:
        return []
    source_ids = [int(source.refphot_source_id) for source in sources]
    catalog_names = {
        int(refcat_id): str(name)
        for refcat_id, name in session.execute(
            select(
                ReferencePhotometryCatalog.refcat_id,
                ReferencePhotometryCatalog.catalog_name,
            ).where(
                ReferencePhotometryCatalog.refcat_id.in_(
                    sorted({int(source.refcat_id) for source in sources})
                )
            )
        ).all()
    }
    points_by_source: dict[int, list[tuple[str, float, float | None]]] = {}
    for source_id, band, magnitude, magnitude_error in session.execute(
        select(
            ReferencePhotometryPoint.refphot_source_id,
            PhotometricBand.canonical_name,
            ReferencePhotometryPoint.magnitude,
            ReferencePhotometryPoint.magnitude_error,
        )
        .join(PhotometricBand, PhotometricBand.band_id == ReferencePhotometryPoint.band_id)
        .where(ReferencePhotometryPoint.refphot_source_id.in_(source_ids))
    ).all():
        points_by_source.setdefault(int(source_id), []).append(
            (str(band), float(magnitude), _to_float(magnitude_error))
        )

    payloads: list[dict] = []
    for source in sources:
        payload = {
            "refphot_source_id": int(source.refphot_source_id),
            "catalog_name": catalog_names[int(source.refcat_id)],
            "catalog_source_id": source.catalog_source_id,
            "gaia_catalog_name": source.identity_catalog_name,
            "gaia_source_id": source.identity_source_id,
            "distance_to_gaia_arcsec": source.distance_to_identity_arcsec,
            "identity_catalog_name": source.identity_catalog_name,
            "identity_source_id": source.identity_source_id,
            "distance_to_identity_arcsec": source.distance_to_identity_arcsec,
            "ra_deg": source.ra_deg,
            "dec_deg": source.dec_deg,
            "epoch": source.epoch,
            "colors": dict(source.colors or {}),
            "quality_flags": dict(source.quality_flags or {}),
            "bands": {},
            "reference_mag_errors": {},
        }
        for band in _CANONICAL_BANDS:
            payload[f"mag_{band}"] = None
        for band, magnitude, magnitude_error in points_by_source.get(
            int(source.refphot_source_id), []
        ):
            payload["bands"][band] = magnitude
            if band in _CANONICAL_BANDS:
                payload[f"mag_{band}"] = magnitude
            if magnitude_error is not None:
                payload["reference_mag_errors"][band] = magnitude_error
        payloads.append(payload)
    return payloads


def _ensure_photometric_bands(
    session: Session,
    *,
    refcat_id: int,
    photometric_system: str | None,
    bands: Iterable[str],
) -> dict[str, int]:
    normalized = sorted({_normalize_band_key(band) for band in bands})
    if not normalized:
        return {}
    table = PhotometricBand.__table__
    for band in normalized:
        stmt = insert(table).values(
            refcat_id=int(refcat_id),
            canonical_name=band,
            provider_band_name=band,
            photometric_system=photometric_system,
            metadata_json={},
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_photometric_bands_catalog_name",
            set_={
                "photometric_system": stmt.excluded.photometric_system,
                "updated_at": func.now(),
            },
        )
        session.execute(stmt)
    rows = session.execute(
        select(PhotometricBand.canonical_name, PhotometricBand.band_id).where(
            PhotometricBand.refcat_id == int(refcat_id),
            PhotometricBand.canonical_name.in_(normalized),
        )
    ).all()
    return {str(name): int(band_id) for name, band_id in rows}


def _read_reference_photometry_table(path: str | Path, *, source_id_col: str | None) -> pd.DataFrame:
    """Read a reference-photometry table while preserving external source IDs.

    Pandas may otherwise infer large catalog identifiers as float64 and print
    them in scientific notation.  For Pan-STARRS object IDs this can lose
    precision and can also create duplicate catalog_source_id values inside one
    INSERT ... ON CONFLICT batch.
    """

    p = Path(path)
    suffix = p.suffix.lower()
    if source_id_col and suffix in {".csv", ".txt", ".tsv"}:
        sep = "\t" if suffix == ".tsv" else ","
        try:
            return pd.read_csv(p, sep=sep, dtype={source_id_col: "string"})
        except ValueError:
            # Fall back to the generic reader so the normal missing-column
            # diagnostic can explain what is wrong with the file.
            pass
    return read_catalog_table(path)


def _deduplicate_reference_rows(rows: list[dict], result: ImportReferencePhotometryResult) -> list[dict]:
    """Keep one row per (catalog_name, catalog_source_id) before upsert.

    PostgreSQL cannot update the same conflict target twice in one multi-row
    INSERT ... ON CONFLICT statement.  This also protects the import from
    duplicated rows in external catalogs and from earlier CSVs where large
    identifiers may have been rounded by dtype inference.
    """

    unique: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (str(row.get("catalog_name") or ""), str(row.get("catalog_source_id") or ""))
        if key in unique:
            result.skipped_duplicate_source_ids += 1
        else:
            order.append(key)
        unique[key] = row
    return [unique[key] for key in order]


def _compact_sqlalchemy_error(exc: SQLAlchemyError) -> str:
    """Return a short DB error without SQLAlchemy's huge SQL/parameter dump."""

    orig = getattr(exc, "orig", None)
    if orig is not None:
        text = str(orig).strip().splitlines()[0] if str(orig).strip() else repr(orig)
        return f"{exc.__class__.__name__}: {orig.__class__.__name__}: {text}"
    text = exc.__class__.__name__
    return text


def _normalize_provider(provider: str) -> str:
    key = str(provider).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ps1": "panstarrs_dr2",
        "ps1_dr2": "panstarrs_dr2",
        "panstarrs": "panstarrs_dr2",
        "pan_starrs": "panstarrs_dr2",
        "pan_starrs_dr2": "panstarrs_dr2",
        "panstarrs_dr2": "panstarrs_dr2",
    }
    return aliases.get(key, key)


def _resolve_reference_photometry_cone(
    session: Session,
    *,
    field_id: int | None,
    series_id: int | None,
    ra_deg: float | None,
    dec_deg: float | None,
    radius_arcmin: float | None,
) -> dict:
    if field_id is not None and series_id is not None:
        raise ValueError("Use only one of --field-id and --series-id")
    if field_id is not None and (ra_deg is not None or dec_deg is not None):
        raise ValueError("Do not combine --field-id with manual --ra/--dec")
    if series_id is not None and (ra_deg is not None or dec_deg is not None):
        raise ValueError("Do not combine --series-id with manual --ra/--dec")

    if field_id is not None:
        row = session.execute(
            text(
                """
                SELECT field_id, ra_center_deg, dec_center_deg, radius_arcmin
                FROM fields
                WHERE field_id = :field_id
                """
            ),
            {"field_id": int(field_id)},
        ).mappings().first()
        if row is None:
            raise ValueError(f"No field found with field_id={field_id}")
        return {
            "field_id": int(row["field_id"]),
            "series_id": None,
            "ra_deg": float(row["ra_center_deg"]),
            "dec_deg": float(row["dec_center_deg"]),
            "radius_arcmin": float(radius_arcmin or row["radius_arcmin"] or 30.0),
        }

    if series_id is not None:
        row = session.execute(
            text(
                """
                SELECT
                    fs.series_id,
                    fs.field_id,
                    COALESCE(fs.ra_center_deg, f.ra_center_deg) AS ra_center_deg,
                    COALESCE(fs.dec_center_deg, f.dec_center_deg) AS dec_center_deg,
                    f.radius_arcmin AS field_radius_arcmin
                FROM field_series fs
                LEFT JOIN fields f ON f.field_id = fs.field_id
                WHERE fs.series_id = :series_id
                """
            ),
            {"series_id": int(series_id)},
        ).mappings().first()
        if row is None:
            raise ValueError(f"No field series found with series_id={series_id}")
        if row["ra_center_deg"] is None or row["dec_center_deg"] is None:
            raise ValueError(f"Series {series_id} has no usable sky center")
        return {
            "field_id": int(row["field_id"]) if row["field_id"] is not None else None,
            "series_id": int(row["series_id"]),
            "ra_deg": float(row["ra_center_deg"]),
            "dec_deg": float(row["dec_center_deg"]),
            "radius_arcmin": float(radius_arcmin or row["field_radius_arcmin"] or 30.0),
        }

    if ra_deg is None or dec_deg is None or radius_arcmin is None:
        raise ValueError("Use --field-id, --series-id, or all of --ra, --dec, --radius-arcmin")
    ra = float(ra_deg)
    dec = float(dec_deg)
    radius = float(radius_arcmin)
    if not (0.0 <= ra < 360.0):
        raise ValueError("RA must be in [0, 360) degrees")
    if not (-90.0 <= dec <= 90.0):
        raise ValueError("Dec must be in [-90, 90] degrees")
    if radius <= 0.0:
        raise ValueError("Radius must be positive")
    return {"field_id": None, "series_id": None, "ra_deg": ra, "dec_deg": dec, "radius_arcmin": radius}


def _fetch_panstarrs_dr2_mean_csv(
    ra_deg: float,
    dec_deg: float,
    radius_deg: float,
    *,
    require_bands: Iterable[str] | None = None,
    min_mag: dict[str, float] | None = None,
    max_mag: dict[str, float] | None = None,
    max_mag_err: dict[str, float] | None = None,
    min_detections: int | None = None,
    use_remote_filters: bool = True,
) -> tuple[pd.DataFrame, str, dict, dict[str, str | None]]:
    columns = [
        "objID",
        "raMean",
        "decMean",
        "nDetections",
        "qualityFlag",
        "objInfoFlag",
        "gMeanPSFMag",
        "gMeanPSFMagErr",
        "rMeanPSFMag",
        "rMeanPSFMagErr",
        "iMeanPSFMag",
        "iMeanPSFMagErr",
        "zMeanPSFMag",
        "zMeanPSFMagErr",
        "yMeanPSFMag",
        "yMeanPSFMagErr",
    ]
    params = {
        "ra": f"{float(ra_deg):.10f}",
        "dec": f"{float(dec_deg):.10f}",
        "radius": f"{float(radius_deg):.8f}",
        "columns": ",".join(columns),
    }
    remote_filter_params: dict[str, str] = {}
    if use_remote_filters:
        remote_filter_params = _make_panstarrs_remote_filter_params(
            require_bands=require_bands,
            min_mag=min_mag,
            max_mag=max_mag,
            max_mag_err=max_mag_err,
            min_detections=min_detections,
        )
        params.update(remote_filter_params)

    url = "https://catalogs.mast.stsci.edu/api/v0.1/panstarrs/dr2/mean.csv?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Grandgousier/0.5.34.2"})
    ssl_context, tls_info = _build_reference_https_context()
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        hint = ""
        if remote_filter_params:
            hint = " Remote filters were enabled; rerun with --no-remote-filters to debug/fallback to the full cone."
        tls_hint = _reference_tls_error_hint(exc, tls_info)
        raise RuntimeError(
            f"Failed to fetch Pan-STARRS DR2 CSV from MAST: {exc}.{hint}{tls_hint}"
        ) from exc
    df = pd.read_csv(io.StringIO(payload))
    return df, url, remote_filter_params, tls_info


def _build_reference_https_context() -> tuple[ssl.SSLContext, dict[str, str | None]]:
    """Build a verified HTTPS context suitable for Python installations on Windows.

    Python virtual environments on Windows do not always discover the native CA
    store correctly.  Prefer an explicitly configured bundle, then certifi when
    installed, and finally the interpreter default trust configuration.  TLS
    verification is never disabled.
    """

    for env_name in ("GRANDGOUSIER_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        raw = os.getenv(env_name)
        if not raw:
            continue
        ca_path = Path(raw).expanduser()
        if not ca_path.is_file():
            raise RuntimeError(f"{env_name} points to a missing CA bundle: {ca_path}")
        return ssl.create_default_context(cafile=str(ca_path)), {
            "source": f"env:{env_name}",
            "path": str(ca_path),
        }

    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return ssl.create_default_context(), {"source": "python_default", "path": None}

    ca_path = Path(certifi.where())
    if ca_path.is_file():
        return ssl.create_default_context(cafile=str(ca_path)), {
            "source": "certifi",
            "path": str(ca_path),
        }
    return ssl.create_default_context(), {"source": "python_default", "path": None}


def _reference_tls_error_hint(exc: BaseException, tls_info: dict[str, str | None]) -> str:
    reason = getattr(exc, "reason", exc)
    certificate_error = isinstance(reason, ssl.SSLCertVerificationError)
    certificate_error = certificate_error or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    if not certificate_error:
        return ""
    source = tls_info.get("source") or "unknown"
    path = tls_info.get("path")
    detail = f" CA source: {source}" + (f" ({path})" if path else "") + "."
    return (
        detail
        + " Upgrade the CA bundle with `python -m pip install --upgrade certifi`, "
        + "or set GRANDGOUSIER_CA_BUNDLE to a trusted PEM bundle used by this host."
    )


def _make_panstarrs_remote_filter_params(
    *,
    require_bands: Iterable[str] | None = None,
    min_mag: dict[str, float] | None = None,
    max_mag: dict[str, float] | None = None,
    max_mag_err: dict[str, float] | None = None,
    min_detections: int | None = None,
) -> dict[str, str]:
    """Translate normalized local filters to MAST Pan-STARRS query params.

    MAST's Pan-STARRS API accepts column constraints as query parameters using
    the ``column.operator=value`` convention, for example
    ``iMeanPSFMag.lte=19``.  The same local filters are applied again after
    download, so the remote limits are an efficiency layer rather than the only
    validation layer.
    """

    params: dict[str, str] = {}

    def add_param(key: str, value: float | int) -> None:
        params[key] = str(value)

    native_required_bands: list[str] = []
    for band in _normalize_band_list(require_bands):
        expanded = ("g", "r") if band in {"b", "v"} else (band,)
        for native_band in expanded:
            if native_band not in native_required_bands:
                native_required_bands.append(native_band)
    for band in native_required_bands:
        mag_col = _panstarrs_mag_column(band)
        add_param(f"{mag_col}.gt", -900.0)
        add_param(f"{mag_col}.lt", 90.0)

    # Synthetic Johnson B/V limits cannot be represented exactly in the remote
    # Pan-STARRS query.  They are applied after standardization below.
    for band, value in _normalize_band_float_mapping(min_mag).items():
        if band in {"b", "v"}:
            continue
        mag_col = _panstarrs_mag_column(band)
        add_param(f"{mag_col}.gte", float(value))

    for band, value in _normalize_band_float_mapping(max_mag).items():
        if band in {"b", "v"}:
            continue
        mag_col = _panstarrs_mag_column(band)
        add_param(f"{mag_col}.lte", float(value))

    for band, value in _normalize_band_float_mapping(max_mag_err).items():
        expanded = ("g", "r") if band in {"b", "v"} else (band,)
        for native_band in expanded:
            err_col = _panstarrs_magerr_column(native_band)
            add_param(f"{err_col}.gt", 0.0)
            previous = params.get(f"{err_col}.lte")
            limit = float(value)
            if previous is None or limit < float(previous):
                add_param(f"{err_col}.lte", limit)

    if min_detections is not None:
        add_param("nDetections.gte", int(min_detections))

    return params


def _panstarrs_mag_column(band: str) -> str:
    b = _normalize_reference_band(band)
    mapping = {
        "g": "gMeanPSFMag",
        "r": "rMeanPSFMag",
        "i": "iMeanPSFMag",
        "z": "zMeanPSFMag",
        "y": "yMeanPSFMag",
    }
    if b not in mapping:
        raise ValueError(f"Pan-STARRS DR2 provider cannot remote-filter unsupported band: {band!r}")
    return mapping[b]


def _panstarrs_magerr_column(band: str) -> str:
    b = _normalize_reference_band(band)
    mapping = {
        "g": "gMeanPSFMagErr",
        "r": "rMeanPSFMagErr",
        "i": "iMeanPSFMagErr",
        "z": "zMeanPSFMagErr",
        "y": "yMeanPSFMagErr",
    }
    if b not in mapping:
        raise ValueError(f"Pan-STARRS DR2 provider cannot remote-filter unsupported band error: {band!r}")
    return mapping[b]


def _synthesize_tonry2012_johnson_bv(
    *,
    mag_g: float | None,
    mag_r: float | None,
    err_g: float | None,
    err_r: float | None,
) -> dict[str, float | str | None]:
    if mag_g is None or mag_r is None:
        return {
            "mag_b": None,
            "err_b": None,
            "mag_v": None,
            "err_v": None,
            "synthetic_bv_transform": None,
        }

    color = float(mag_g) - float(mag_r)
    errors = {
        "g": float(err_g) if err_g is not None else 0.0,
        "r": float(err_r) if err_r is not None else 0.0,
    }
    values: dict[str, float | str | None] = {}
    for band, (
        base_band,
        a0,
        a1,
        a2,
        transform_scatter,
    ) in _TONRY2012_PS1_TO_JOHNSON.items():
        base_mag = float(mag_g) if base_band == "g" else float(mag_r)
        value = base_mag + a0 + a1 * color + a2 * color * color
        derivative_color = a1 + 2.0 * a2 * color
        if base_band == "g":
            derivative_g = 1.0 + derivative_color
            derivative_r = -derivative_color
        else:
            derivative_g = derivative_color
            derivative_r = 1.0 - derivative_color
        error = math.sqrt(
            (derivative_g * errors["g"]) ** 2
            + (derivative_r * errors["r"]) ** 2
            + float(transform_scatter) ** 2
        )
        values[f"mag_{band}"] = value
        values[f"err_{band}"] = error
    values["synthetic_bv_transform"] = "tonry2012_table6_stellar_vega"
    return values


def _standardize_panstarrs_reference_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in df.iterrows():
        ra = _to_float(row.get("raMean"))
        dec = _to_float(row.get("decMean"))
        obj_id = _to_text(row.get("objID"))
        if not obj_id or ra is None or dec is None:
            continue
        mags = {
            "g": _ps1_mag(row.get("gMeanPSFMag")),
            "r": _ps1_mag(row.get("rMeanPSFMag")),
            "i": _ps1_mag(row.get("iMeanPSFMag")),
            "z": _ps1_mag(row.get("zMeanPSFMag")),
            "y": _ps1_mag(row.get("yMeanPSFMag")),
        }
        errs = {
            "g": _ps1_magerr(row.get("gMeanPSFMagErr")),
            "r": _ps1_magerr(row.get("rMeanPSFMagErr")),
            "i": _ps1_magerr(row.get("iMeanPSFMagErr")),
            "z": _ps1_magerr(row.get("zMeanPSFMagErr")),
            "y": _ps1_magerr(row.get("yMeanPSFMagErr")),
        }
        synthetic_bv = _synthesize_tonry2012_johnson_bv(
            mag_g=mags["g"],
            mag_r=mags["r"],
            err_g=errs["g"],
            err_r=errs["r"],
        )
        rows.append(
            {
                "catalog_source_id": obj_id,
                "ra": ra,
                "dec": dec,
                "mag_g": mags["g"],
                "err_g": errs["g"],
                "mag_r": mags["r"],
                "err_r": errs["r"],
                "mag_b": synthetic_bv["mag_b"],
                "err_b": synthetic_bv["err_b"],
                "mag_v": synthetic_bv["mag_v"],
                "err_v": synthetic_bv["err_v"],
                "mag_i": mags["i"],
                "err_i": errs["i"],
                "mag_z": mags["z"],
                "err_z": errs["z"],
                "mag_y": mags["y"],
                "err_y": errs["y"],
                "color_g_r": _diff(mags["g"], mags["r"]),
                "color_r_i": _diff(mags["r"], mags["i"]),
                "color_i_z": _diff(mags["i"], mags["z"]),
                "n_detections": _to_float(row.get("nDetections")),
                "quality_flag": _to_text(row.get("qualityFlag")),
                "obj_info_flag": _to_text(row.get("objInfoFlag")),
                "synthetic_bv_transform": synthetic_bv["synthetic_bv_transform"],
                "raw_objID": obj_id,
            }
        )
    columns = [
        "catalog_source_id",
        "ra",
        "dec",
        "mag_g",
        "err_g",
        "mag_r",
        "err_r",
        "mag_b",
        "err_b",
        "mag_v",
        "err_v",
        "mag_i",
        "err_i",
        "mag_z",
        "err_z",
        "mag_y",
        "err_y",
        "color_g_r",
        "color_r_i",
        "color_i_z",
        "n_detections",
        "quality_flag",
        "obj_info_flag",
        "synthetic_bv_transform",
        "raw_objID",
    ]
    return pd.DataFrame(rows, columns=columns)


def _filter_reference_export_dataframe(
    df: pd.DataFrame,
    *,
    require_bands: Iterable[str] | None = None,
    min_mag: dict[str, float] | None = None,
    max_mag: dict[str, float] | None = None,
    max_mag_err: dict[str, float] | None = None,
    min_detections: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Apply science-oriented source limits to a normalized reference CSV.

    The Pan-STARRS cone search may return hundreds of thousands of faint
    objects near the pole.  For calibration we normally only need stars with
    usable photometry in the relevant band(s), reasonable magnitude errors and
    a small number of detections.  Filtering here keeps both the CSV and DB
    import manageable without changing the raw provider query semantics.
    """

    out = df.copy()
    summary = {
        "input_rows": int(len(out)),
        "require_bands": [],
        "min_mag": {},
        "max_mag": {},
        "max_mag_err": {},
        "min_detections": min_detections,
    }

    for band in _normalize_band_list(require_bands):
        col = f"mag_{band}"
        if col not in out.columns:
            raise ValueError(f"Cannot require band {band!r}: missing column {col!r}")
        before = len(out)
        out = out[out[col].notna()]
        summary["require_bands"].append({"band": band, "removed": int(before - len(out))})

    for band, value in _normalize_band_float_mapping(min_mag).items():
        col = f"mag_{band}"
        if col not in out.columns:
            raise ValueError(f"Cannot apply min_mag for band {band!r}: missing column {col!r}")
        before = len(out)
        out = out[out[col].notna() & (out[col].astype(float) >= float(value))]
        summary["min_mag"][band] = {"value": float(value), "removed": int(before - len(out))}

    for band, value in _normalize_band_float_mapping(max_mag).items():
        col = f"mag_{band}"
        if col not in out.columns:
            raise ValueError(f"Cannot apply max_mag for band {band!r}: missing column {col!r}")
        before = len(out)
        out = out[out[col].notna() & (out[col].astype(float) <= float(value))]
        summary["max_mag"][band] = {"value": float(value), "removed": int(before - len(out))}

    for band, value in _normalize_band_float_mapping(max_mag_err).items():
        col = f"err_{band}"
        if col not in out.columns:
            raise ValueError(f"Cannot apply max_mag_err for band {band!r}: missing column {col!r}")
        before = len(out)
        out = out[out[col].notna() & (out[col].astype(float) <= float(value))]
        summary["max_mag_err"][band] = {"value": float(value), "removed": int(before - len(out))}

    if min_detections is not None:
        if "n_detections" not in out.columns:
            raise ValueError("Cannot apply min_detections: missing column 'n_detections'")
        before = len(out)
        out = out[out["n_detections"].notna() & (out["n_detections"].astype(float) >= int(min_detections))]
        summary["min_detections_removed"] = int(before - len(out))

    summary["output_rows"] = int(len(out))
    summary["removed_rows"] = int(len(df) - len(out))
    return out.reset_index(drop=True), summary


def _normalize_band_list(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for item in values or []:
        for part in str(item).split(","):
            band = _normalize_reference_band(part)
            if band and band not in out:
                out.append(band)
    return out


def _normalize_band_float_mapping(values: dict[str, float] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for band, value in (values or {}).items():
        b = _normalize_reference_band(band)
        if not b:
            continue
        out[b] = float(value)
    return out


def _normalize_band_key(value: str | None) -> str:
    if value is None:
        raise ValueError("Reference band cannot be empty")
    band = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ps1_g": "g",
        "ps1_r": "r",
        "ps1_i": "i",
        "ps1_z": "z",
        "ps1_y": "y",
        "gaia_g": "g_gaia",
    }
    band = aliases.get(band, band)
    if not band:
        raise ValueError("Reference band cannot be empty")
    if len(band) > 32:
        raise ValueError(f"Reference band name is too long: {value!r}")
    return band


def _normalize_reference_band(value: str | None) -> str | None:
    if value is None:
        return None
    band = _normalize_band_key(value)
    if band not in set(_CANONICAL_BANDS):
        raise ValueError(f"Unsupported calibration/reference-filter band: {value!r}")
    return band




def _normalize_band_column_mapping(mapping: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for band, column in mapping.items():
        out[_normalize_band_key(band)] = str(column)
    return out

def _ps1_mag(value) -> float | None:
    x = _to_float(value)
    if x is None or x <= -900.0 or x >= 90.0:
        return None
    return x


def _ps1_magerr(value) -> float | None:
    x = _to_float(value)
    if x is None or x <= 0.0 or x >= 9.0:
        return None
    return x


def _diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _make_reference_csv_import_command(path: Path) -> str:
    return (
        f'.\\.venv\\Scripts\\python.exe -m grandgousier.cli import-reference-photometry `\n'
        f'  --file {path} `\n'
        f'  --catalog-name panstarrs_dr2 `\n'
        f'  --catalog-version dr2 `\n'
        f'  --photometric-system ps1_ab_plus_tonry2012_johnson_vega `\n'
        f'  --source-priority 10 `\n'
        f'  --ra-col ra `\n'
        f'  --dec-col dec `\n'
        f'  --source-id-col catalog_source_id `\n'
        f'  --mag-col g=mag_g --mag-col r=mag_r --mag-col b=mag_b --mag-col v=mag_v '
        f'--mag-col i=mag_i --mag-col z=mag_z --mag-col y=mag_y `\n'
        f'  --mag-err-col g=err_g --mag-err-col r=err_r --mag-err-col b=err_b '
        f'--mag-err-col v=err_v --mag-err-col i=err_i --mag-err-col z=err_z '
        f'--mag-err-col y=err_y `\n'
        f'  --color-col g_r=color_g_r --color-col r_i=color_r_i --color-col i_z=color_i_z `\n'
        f'  --quality-col n_detections=n_detections --quality-col quality_flag=quality_flag '
        f'--quality-col obj_info_flag=obj_info_flag '
        f'--quality-col synthetic_bv_transform=synthetic_bv_transform `\n'
        f'  --match-to-catalog gaia_dr3_master `\n'
        f'  --max-match-arcsec 1.0'
    )


def make_reference_photometry_uid(catalog_name: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{catalog_name.strip()}|{source_id.strip()}".encode("utf-8")).hexdigest()[:24]
    return f"rp_{digest}"


def _upsert_reference_catalog(
    session: Session,
    *,
    catalog_name: str,
    catalog_version: str | None,
    photometric_system: str | None,
    source_priority: int,
    metadata_json: dict,
) -> int:
    table = ReferencePhotometryCatalog.__table__
    stmt = insert(table).values(
        catalog_name=catalog_name,
        catalog_version=catalog_version,
        photometric_system=photometric_system,
        source_priority=source_priority,
        metadata_json=metadata_json,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.catalog_name],
        set_={
            "catalog_version": stmt.excluded.catalog_version,
            "photometric_system": stmt.excluded.photometric_system,
            "source_priority": stmt.excluded.source_priority,
            "metadata_json": stmt.excluded.metadata_json,
        },
    ).returning(table.c.refcat_id)
    return int(session.scalar(stmt))


def _load_match_catalog_sources(session: Session, catalog_name: str, df: pd.DataFrame, *, ra_col: str, dec_col: str) -> list[dict]:
    ras = [_to_float(v) for v in df[ra_col].head(100000).tolist()] if ra_col in df else []
    decs = [_to_float(v) for v in df[dec_col].head(100000).tolist()] if dec_col in df else []
    ras = [v for v in ras if v is not None]
    decs = [v for v in decs if v is not None]
    stmt = select(
        CatalogSource.catalog_source_id,
        CatalogSource.catalog_name,
        CatalogSource.source_id,
        CatalogSource.ra_deg,
        CatalogSource.dec_deg,
    ).where(CatalogSource.catalog_name == catalog_name)
    if ras and decs and (max(ras) - min(ras)) < 20.0:
        margin = 0.2
        stmt = stmt.where(CatalogSource.ra_deg >= min(ras) - margin, CatalogSource.ra_deg <= max(ras) + margin)
    if decs:
        margin = 0.2
        stmt = stmt.where(CatalogSource.dec_deg >= min(decs) - margin, CatalogSource.dec_deg <= max(decs) + margin)
    return [dict(row._mapping) for row in session.execute(stmt).all()]


def _nearest_catalog_source(ra: float, dec: float, sources: list[dict], *, max_match_arcsec: float) -> dict | None:
    best = None
    best_sep = None
    for src in sources:
        sep = angular_sep_arcsec(ra, dec, float(src["ra_deg"]), float(src["dec_deg"]))
        if best_sep is None or sep < best_sep:
            best = src
            best_sep = sep
    if best is None or best_sep is None or best_sep > max_match_arcsec:
        return None
    out = dict(best)
    out["sep_arcsec"] = best_sep
    return out


def angular_sep_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1 = math.radians(ra1)
    d1 = math.radians(dec1)
    r2 = math.radians(ra2)
    d2 = math.radians(dec2)
    sd = math.sin((d2 - d1) / 2.0)
    sr = math.sin((r2 - r1) / 2.0)
    a = sd * sd + math.cos(d1) * math.cos(d2) * sr * sr
    a = min(1.0, max(0.0, a))
    return math.degrees(2.0 * math.asin(math.sqrt(a))) * 3600.0


def _field_identity_source_ids(session: Session, *, field_id: int | None, identity_catalog_name: str) -> list[str]:
    sql = """
        SELECT DISTINCT cs.source_id
        FROM catalog_sources cs
        JOIN frame_catalog_links fcl ON fcl.catalog_source_id = cs.catalog_source_id
        JOIN field_series_frames fsf ON fsf.frame_id = fcl.frame_id
        JOIN field_series fs ON fs.series_id = fsf.series_id
        WHERE cs.catalog_name = :catalog_name
    """
    params = {"catalog_name": identity_catalog_name}
    if field_id is not None:
        sql += " AND fs.field_id = :field_id"
        params["field_id"] = int(field_id)
    rows = session.execute(text(sql), params).all()
    return [str(row[0]) for row in rows]


def _extract_mag_values(row, mapping: dict[str, str]) -> dict:
    out = {field: None for field in set(_CANONICAL_MAG_FIELDS.values())}
    for band, col in mapping.items():
        attr = _canonical_mag_attr(band)
        value = _to_float(row.get(col))
        if attr and value is not None:
            out[attr] = value
    return out


def _canonical_mag_attr(band: str) -> str | None:
    key = str(band).strip().lower().replace("-", "_").replace(" ", "_")
    return _CANONICAL_MAG_FIELDS.get(key)


def _require_columns(df: pd.DataFrame, cols: Iterable[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        available = ", ".join(str(col) for col in df.columns[:80])
        raise ValueError(f"Missing required reference photometry column(s): {missing}. Available columns start with: {available}")


def _extract_named_float_dict(row, mapping: dict[str, str]) -> dict:
    out = {}
    for name, col in mapping.items():
        value = _to_float(row.get(col))
        if value is not None:
            out[str(name)] = value
    return out


def _extract_named_value_dict(row, mapping: dict[str, str]) -> dict:
    out = {}
    for name, col in mapping.items():
        value = _jsonable(row.get(col))
        if value is not None:
            out[str(name)] = value
    return out


def _make_coordinate_source_id(catalog_name: str, ra: float, dec: float) -> str:
    text = f"{catalog_name}|{ra:.8f}|{dec:.8f}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"coord_{digest}"


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _to_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return format(value, ".0f")
    text = str(value).strip()
    return text


def _jsonable(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _chunks(items: list[dict], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _nonnull_case(column):
    from sqlalchemy import case

    return case((column.is_not(None), 1), else_=0)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    n = len(values)
    mid = n // 2
    if n % 2:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)



def _format_refphot_row(row: dict) -> dict:
    for key in list(row.keys()):
        if isinstance(row[key], float):
            ndigits = 4 if key.startswith("mag_") else 6
            row[key] = round(row[key], ndigits)
    return row
