from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.db.models import CatalogSource


@dataclass
class ImportCatalogResult:
    catalog_name: str
    path: str
    read_rows: int = 0
    valid_rows: int = 0
    inserted_or_updated: int = 0
    skipped_bad_coordinates: int = 0
    dry_run: bool = False


def read_catalog_table(path: str | Path) -> pd.DataFrame:
    """Read a local catalog table.

    Supported MVP formats: CSV/TSV/TXT via pandas and FITS/ECSV/VOTable via astropy.table.
    """

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(p)
    if suffix in {".tsv", ".txt"}:
        return pd.read_csv(p, sep=None, engine="python")

    from astropy.table import Table

    table = Table.read(p)
    return table.to_pandas()


def import_catalog_sources(
    session: Session,
    *,
    path: str | Path,
    catalog_name: str,
    ra_col: str = "ra",
    dec_col: str = "dec",
    source_id_col: str | None = None,
    pmra_col: str | None = None,
    pmdec_col: str | None = None,
    epoch_col: str | None = None,
    mag_cols: dict[str, str] | None = None,
    color_cols: dict[str, str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> ImportCatalogResult:
    df = read_catalog_table(path)
    result = ImportCatalogResult(catalog_name=catalog_name, path=str(path), read_rows=len(df), dry_run=dry_run)
    if limit is not None:
        df = df.head(limit)

    _require_columns(df, [ra_col, dec_col])
    if source_id_col:
        _require_columns(df, [source_id_col])
    for col in [pmra_col, pmdec_col, epoch_col]:
        if col:
            _require_columns(df, [col])
    for mapping in [mag_cols or {}, color_cols or {}]:
        _require_columns(df, list(mapping.values()))

    rows = []
    for idx, row in df.iterrows():
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

        ref_mags = _extract_named_float_dict(row, mag_cols or {})
        colors = _extract_named_float_dict(row, color_cols or {})
        raw_json = {str(k): _jsonable(v) for k, v in row.to_dict().items()}
        source_uid = make_catalog_source_uid(catalog_name, source_id)

        rows.append(
            {
                "catalog_name": catalog_name,
                "source_id": source_id,
                "source_uid": source_uid,
                "ra_deg": ra,
                "dec_deg": dec,
                "epoch": _to_float(row.get(epoch_col)) if epoch_col else None,
                "pmra_masyr": _to_float(row.get(pmra_col)) if pmra_col else None,
                "pmdec_masyr": _to_float(row.get(pmdec_col)) if pmdec_col else None,
                "ref_mags": ref_mags,
                "colors": colors,
                "source_flags": {},
                "raw_json": raw_json,
            }
        )

    result.valid_rows = len(rows)
    if dry_run or not rows:
        return result

    table = CatalogSource.__table__
    chunk_size = 1000
    for chunk in _chunks(rows, chunk_size):
        stmt = insert(table).values(chunk)
        update_cols = {
            "source_uid": stmt.excluded.source_uid,
            "ra_deg": stmt.excluded.ra_deg,
            "dec_deg": stmt.excluded.dec_deg,
            "epoch": stmt.excluded.epoch,
            "pmra_masyr": stmt.excluded.pmra_masyr,
            "pmdec_masyr": stmt.excluded.pmdec_masyr,
            "ref_mags": stmt.excluded.ref_mags,
            "colors": stmt.excluded.colors,
            "source_flags": stmt.excluded.source_flags,
            "raw_json": stmt.excluded.raw_json,
        }
        stmt = stmt.on_conflict_do_update(
            constraint="uq_catalog_sources_catalog_source",
            set_=update_cols,
        )
        session.execute(stmt)
        result.inserted_or_updated += len(chunk)
    session.commit()
    return result


def make_catalog_source_uid(catalog_name: str, source_id: str) -> str:
    normalized = f"{catalog_name.strip()}|{source_id.strip()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"cs_{digest}"


def _require_columns(df: pd.DataFrame, cols: Iterable[str]) -> None:
    missing = [col for col in cols if col not in df.columns]
    if missing:
        available = ", ".join(str(col) for col in df.columns[:50])
        raise ValueError(f"Missing required catalog column(s): {missing}. Available columns start with: {available}")


def _extract_named_float_dict(row, mapping: dict[str, str]) -> dict:
    out = {}
    for name, col in mapping.items():
        value = _to_float(row.get(col))
        if value is not None:
            out[name] = value
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
    text = str(value).strip()
    if not text:
        return ""
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
