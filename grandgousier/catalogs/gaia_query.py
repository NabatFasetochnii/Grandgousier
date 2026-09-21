from __future__ import annotations

import csv
import io
import math
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy import units as u
from astropy.wcs import FITSFixedWarning, WCS
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload
from tqdm import tqdm

from grandgousier.catalogs.frame_match import match_catalog_to_frames
from grandgousier.catalogs.import_catalog import make_catalog_source_uid
from grandgousier.db.models import CatalogSource, Frame


@dataclass
class GaiaQueryResult:
    selected_frames: int = 0
    queried_frames: int = 0
    queried_cones: int = 0
    row_limit_reached_cones: int = 0
    read_rows: int = 0
    inserted_or_updated: int = 0
    linked: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    skipped_oversized_cones: int = 0
    failed_footprint_reasons: dict[str, int] = field(default_factory=dict)
    sample_failed_footprints: list[dict[str, Any]] = field(default_factory=list)
    query_provider: str | None = None
    query_provider_failures: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    catalog_name: str = "gaia_dr3"
    query_mode: str = "per_frame"
    effective_query_mode: str | None = None


@dataclass(frozen=True)
class _GaiaTapProvider:
    name: str
    client: Any
    table_name: str = "gaiadr3.gaia_source"


class _StaticTapJob:
    """Minimal TAP-job compatibility wrapper for an already materialized result."""

    def __init__(self, rows: list["_TapCsvRow"]):
        self._rows = rows

    def get_results(self) -> list["_TapCsvRow"]:
        return self._rows


class _TapCsvRow(dict):
    """Mapping row with the ``colnames`` attribute expected by Astropy rows."""

    @property
    def colnames(self) -> list[str]:
        return list(self.keys())


class _HttpCsvTapClient:
    """Small synchronous TAP client that requests CSV explicitly.

    ``astroquery.utils.tap.Tap`` currently exposes two platform/service
    interoperability problems relevant to the clean rebuild: Gaia@AIP can reject
    its default Accept header with HTTP 406, while the ARI VOTable/BINARY parser
    can overflow on Windows when a 64-bit Gaia ``source_id`` is decoded through a
    C ``long``.  The TAP protocol itself is fine.  Requesting CSV over the standard
    ``/sync`` endpoint avoids both client-side failure modes and keeps ``source_id``
    as an exact decimal string.
    """

    def __init__(self, base_url: str, *, timeout_sec: float | None = 120.0):
        self.base_url = base_url.rstrip("/")
        self.sync_url = f"{self.base_url}/sync"
        self.timeout = None if timeout_sec is None else float(timeout_sec)

    def launch_job(self, query: str, maxrec: int | None = None):
        rows = _tap_sync_csv_query(
            self.sync_url,
            query=query,
            max_rows=maxrec,
            timeout_sec=self.timeout,
        )
        return _StaticTapJob(rows)


_NUMERIC_TAP_COLUMNS = {
    "ra",
    "dec",
    "ref_epoch",
    "pmra",
    "pmdec",
    "phot_g_mean_mag",
    "phot_bp_mean_mag",
    "phot_rp_mean_mag",
    "ruwe",
}


def _tap_sync_csv_query(
    sync_url: str,
    *,
    query: str,
    max_rows: int | None,
    timeout_sec: float | None,
) -> list[_TapCsvRow]:
    payload = {
        "REQUEST": "doQuery",
        "LANG": "ADQL",
        "QUERY": query,
        "FORMAT": "csv",
    }
    if max_rows is not None:
        payload["MAXREC"] = str(int(max_rows))

    headers = {
        "Accept": "text/csv, text/plain;q=0.9, */*;q=0.1",
        "User-Agent": "Grandgousier/0.5.41.4",
    }
    response = requests.post(
        sync_url,
        data=payload,
        headers=headers,
        timeout=timeout_sec,
    )
    response.raise_for_status()
    text = response.content.decode(response.encoding or "utf-8-sig", errors="replace")
    stripped = text.lstrip()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if stripped.startswith("<?xml") or "votable" in content_type or "xml" in content_type:
        detail = re.sub(r"\s+", " ", stripped)[:500]
        raise RuntimeError(f"TAP endpoint returned XML instead of CSV: {detail}")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("TAP CSV response has no header")
    rows: list[_TapCsvRow] = []
    for record in reader:
        row = _TapCsvRow()
        for key, raw_value in record.items():
            name = (key or "").strip()
            value = raw_value.strip() if isinstance(raw_value, str) else raw_value
            if value in (None, ""):
                row[name] = None
            elif name == "source_id":
                # Keep the 64-bit Gaia identifier out of platform-sized C integer
                # conversion paths.  Grandgousier stores source_id as text anyway.
                row[name] = str(value)
            elif name == "duplicated_source":
                row[name] = str(value).strip().lower() in {"1", "true", "t", "yes"}
            elif name in _NUMERIC_TAP_COLUMNS:
                row[name] = float(value)
            else:
                row[name] = value
        rows.append(row)
    return rows


class _GaiaAllProvidersFailed(RuntimeError):
    def __init__(self, failures: dict[str, str]):
        self.failures = dict(failures)
        details = "; ".join(f"{name}={message}" for name, message in self.failures.items())
        super().__init__(f"all Gaia TAP providers failed: {details}")


def query_gaia_for_frames(
    session: Session,
    *,
    catalog_name: str = "gaia_dr3",
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    margin_arcmin: float = 1.0,
    mag_limit: float | None = None,
    max_rows_per_frame: int = 50000,
    query_mode: str = "per_frame",
    max_radius_deg: float | None = None,
    gaia_timeout_sec: float | None = 120.0,
    match: bool = False,
    match_margin_pix: float = 2.0,
    limit_frames: int | None = None,
    dry_run: bool = False,
) -> GaiaQueryResult:
    """Query Gaia DR3 cone catalogs for selected frame footprints and insert catalog_sources.

    This function intentionally depends on astroquery only at runtime. Grandgousier can be
    installed and used for local catalogs without astroquery; this command fails with a clear
    message if the optional dependency is absent.
    """

    providers = _build_gaia_tap_providers(gaia_timeout_sec)

    if margin_arcmin < 0:
        raise ValueError("margin_arcmin must be non-negative")
    if max_rows_per_frame <= 0:
        raise ValueError("max_rows_per_frame must be positive")
    if max_radius_deg is not None and max_radius_deg <= 0:
        raise ValueError("max_radius_deg must be positive when provided")
    query_mode = (query_mode or "per_frame").strip().lower()
    if query_mode not in {"per_frame", "frame_union"}:
        raise ValueError("query_mode must be one of: per_frame, frame_union")

    result = GaiaQueryResult(dry_run=dry_run, catalog_name=catalog_name, query_mode=query_mode, effective_query_mode=query_mode)
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.selected_frames = len(frames)

    if query_mode == "frame_union":
        _query_gaia_for_frame_union(
            session,
            providers,
            frames=frames,
            result=result,
            margin_arcmin=margin_arcmin,
            mag_limit=mag_limit,
            max_rows=max_rows_per_frame,
            max_radius_deg=max_radius_deg,
        )
        if not dry_run:
            session.commit()
        if match and not dry_run and result.failed_frames == 0 and result.inserted_or_updated > 0:
            match_result = match_catalog_to_frames(
                session,
                catalog_name=catalog_name,
                frame_ids=frame_ids,
                instrument=instrument,
                instrument_unit_id=instrument_unit_id,
                only_wcs_ok=only_wcs_ok,
                margin_pix=match_margin_pix,
                limit_frames=limit_frames,
                limit_sources=None,
                dry_run=False,
            )
            result.linked = match_result.linked
        return result

    for frame in tqdm(frames, desc="Querying Gaia DR3", unit="frame"):
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        try:
            footprint, reason = _frame_cone_for_frame_diagnostic(frame, margin_arcmin=margin_arcmin)
            if footprint is None:
                _record_failed_footprint(result, frame, reason or "unknown")
                continue
            center_ra, center_dec, radius_deg = footprint
            if max_radius_deg is not None and radius_deg > max_radius_deg:
                result.skipped_oversized_cones += 1
                _record_failed_footprint(result, frame, "oversized_cone")
                continue
            table, provider_name, provider_failures = _query_gaia_cone(
                providers,
                center_ra_deg=center_ra,
                center_dec_deg=center_dec,
                radius_deg=radius_deg,
                mag_limit=mag_limit,
                max_rows=max_rows_per_frame,
            )
            _record_query_provider(result, provider_name, provider_failures)
            result.queried_frames += 1
            result.queried_cones += 1
            result.read_rows += len(table)
            if len(table) >= int(max_rows_per_frame):
                result.row_limit_reached_cones += 1
            rows = [_gaia_row_to_catalog_source(row, catalog_name=catalog_name) for row in table]
            if rows and not dry_run:
                _upsert_catalog_sources(session, rows)
                result.inserted_or_updated += len(rows)
        except _GaiaAllProvidersFailed as exc:
            result.query_provider_failures.update(exc.failures)
            _record_failed_footprint(result, frame, "gaia_query_all_providers_failed")
        except Exception as exc:
            _record_failed_footprint(result, frame, f"exception:{type(exc).__name__}")

    if not dry_run:
        session.commit()

    if match and not dry_run and result.failed_frames == 0:
        match_result = match_catalog_to_frames(
            session,
            catalog_name=catalog_name,
            frame_ids=frame_ids,
            instrument=instrument,
            instrument_unit_id=instrument_unit_id,
            only_wcs_ok=only_wcs_ok,
            margin_pix=match_margin_pix,
            limit_frames=limit_frames,
            limit_sources=None,
            dry_run=False,
        )
        result.linked = match_result.linked
    return result



def _query_gaia_for_frame_union(
    session: Session,
    providers: list[_GaiaTapProvider],
    *,
    frames: list[Frame],
    result: GaiaQueryResult,
    margin_arcmin: float,
    mag_limit: float | None,
    max_rows: int,
    max_radius_deg: float | None,
) -> None:
    cones: list[tuple[float, float, float]] = []
    for frame in tqdm(frames, desc="Computing Gaia footprint", unit="frame"):
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        footprint, reason = _frame_cone_for_frame_diagnostic(frame, margin_arcmin=margin_arcmin)
        if footprint is None:
            _record_failed_footprint(result, frame, reason or "unknown")
            continue
        cones.append(footprint)

    if not cones:
        return

    center_ra, center_dec, radius_deg = _combine_cones(cones)
    if max_radius_deg is not None and radius_deg > max_radius_deg:
        # A wide multi-night archive can exceed the safety radius even when each
        # individual full-frame footprint is valid.  Falling back to spatial
        # clusters preserves the safety guard without exploding into one Gaia
        # request per frame for repeated pointings.
        result.effective_query_mode = "frame_clusters"
        clusters, oversized = _cluster_cones(cones, max_radius_deg=float(max_radius_deg))
        result.skipped_oversized_cones += len(oversized)
        result.failed_frames += len(oversized)
        if oversized:
            result.failed_footprint_reasons["oversized_individual_cone"] = (
                result.failed_footprint_reasons.get("oversized_individual_cone", 0) + len(oversized)
            )
        for cluster in clusters:
            _query_gaia_cone_group(
                session,
                providers,
                cones=cluster,
                result=result,
                mag_limit=mag_limit,
                max_rows=max_rows,
            )
        return

    _query_gaia_cone_group(
        session,
        providers,
        cones=cones,
        result=result,
        mag_limit=mag_limit,
        max_rows=max_rows,
    )



def _query_gaia_cone_group(
    session: Session,
    providers: list[_GaiaTapProvider],
    *,
    cones: list[tuple[float, float, float]],
    result: GaiaQueryResult,
    mag_limit: float | None,
    max_rows: int,
) -> None:
    if not cones:
        return
    center_ra, center_dec, radius_deg = _combine_cones(cones)
    try:
        table, provider_name, provider_failures = _query_gaia_cone(
            providers,
            center_ra_deg=center_ra,
            center_dec_deg=center_dec,
            radius_deg=radius_deg,
            mag_limit=mag_limit,
            max_rows=max_rows,
        )
        _record_query_provider(result, provider_name, provider_failures)
        result.queried_frames += len(cones)
        result.queried_cones += 1
        result.read_rows += len(table)
        if len(table) >= int(max_rows):
            result.row_limit_reached_cones += 1
        rows = [_gaia_row_to_catalog_source(row, catalog_name=result.catalog_name) for row in table]
        if rows and not result.dry_run:
            _upsert_catalog_sources(session, rows)
            result.inserted_or_updated += len(rows)
    except _GaiaAllProvidersFailed as exc:
        result.query_provider_failures.update(exc.failures)
        result.failed_frames += len(cones)
        reason = "gaia_query_all_providers_failed"
        result.failed_footprint_reasons[reason] = result.failed_footprint_reasons.get(reason, 0) + len(cones)
    except Exception as exc:
        result.failed_frames += len(cones)
        reason = f"gaia_query_exception:{type(exc).__name__}"
        result.failed_footprint_reasons[reason] = result.failed_footprint_reasons.get(reason, 0) + len(cones)


def _cluster_cones(
    cones: list[tuple[float, float, float]], *, max_radius_deg: float
) -> tuple[list[list[tuple[float, float, float]]], list[tuple[float, float, float]]]:
    """Greedily group repeated/nearby frame footprints under a safe cone radius."""

    clusters: list[list[tuple[float, float, float]]] = []
    oversized: list[tuple[float, float, float]] = []
    for cone in cones:
        if float(cone[2]) > float(max_radius_deg):
            oversized.append(cone)
            continue
        best_index = None
        best_radius = None
        for idx, cluster in enumerate(clusters):
            candidate = cluster + [cone]
            _ra, _dec, radius = _combine_cones(candidate)
            if radius <= max_radius_deg and (best_radius is None or radius < best_radius):
                best_index = idx
                best_radius = radius
        if best_index is None:
            clusters.append([cone])
        else:
            clusters[best_index].append(cone)
    return clusters, oversized

def _combine_cones(cones: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if not cones:
        raise ValueError("cannot combine an empty cone list")
    if len(cones) == 1:
        return cones[0]

    centers = SkyCoord([ra for ra, _dec, _r in cones] * u.deg, [dec for _ra, dec, _r in cones] * u.deg, frame="icrs")
    cart = centers.cartesian
    center = SkyCoord(
        x=float(cart.x.value.mean()),
        y=float(cart.y.value.mean()),
        z=float(cart.z.value.mean()),
        representation_type="cartesian",
        frame="icrs",
    ).icrs
    center_sph = center.spherical
    center_coord = SkyCoord(center_sph.lon, center_sph.lat, frame="icrs")
    radius_deg = max(
        float(center_coord.separation(SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")).deg) + float(radius)
        for ra, dec, radius in cones
    )
    return float(center_sph.lon.deg) % 360.0, float(center_sph.lat.deg), radius_deg


def _configure_gaia_timeout(Gaia, timeout_sec: float | None) -> None:
    if timeout_sec is None:
        return
    try:
        timeout = float(timeout_sec)
    except Exception:
        return
    if timeout <= 0:
        return
    for attr in ("TIMEOUT", "timeout"):
        if hasattr(Gaia, attr):
            try:
                setattr(Gaia, attr, timeout)
            except Exception:
                pass
    tap = getattr(Gaia, "_tap", None)
    if tap is not None and hasattr(tap, "timeout"):
        try:
            tap.timeout = timeout
        except Exception:
            pass


def _record_failed_footprint(result: GaiaQueryResult, frame: Frame, reason: str) -> None:
    result.failed_frames += 1
    result.failed_footprint_reasons[reason] = result.failed_footprint_reasons.get(reason, 0) + 1
    if len(result.sample_failed_footprints) < 10:
        result.sample_failed_footprints.append(
            {
                "frame_id": getattr(frame, "frame_id", None),
                "raw_path": getattr(frame, "raw_path", None),
                "reason": reason,
                "ra_center_deg": _finite_float(getattr(frame, "ra_center_deg", None)),
                "dec_center_deg": _finite_float(getattr(frame, "dec_center_deg", None)),
                "instrument_unit_id": getattr(frame, "instrument_unit_id", None),
            }
        )


def _frame_cone_for_frame(frame: Frame, *, margin_arcmin: float) -> tuple[float, float, float] | None:
    cone, _reason = _frame_cone_for_frame_diagnostic(frame, margin_arcmin=margin_arcmin)
    return cone


def _frame_cone_for_frame_diagnostic(
    frame: Frame, *, margin_arcmin: float
) -> tuple[tuple[float, float, float] | None, str | None]:
    """Return a Gaia cone and, on failure, a compact diagnostic reason.

    For catalog bootstrapping the most robust center is the already indexed
    pointing center from ``frames.ra_center_deg/dec_center_deg``.  FITS WCS is
    still useful for the footprint radius, but it must not be a hard precondition:
    valid RoboPhot rows can have DB centers even when Astropy WCS footprint
    construction is fragile.
    """

    path = Path(frame.raw_path)
    if not path.exists():
        return None, "missing_file"

    try:
        with fits.open(path, memmap=False) as hdul:
            header = hdul[0].header
    except Exception as exc:
        return None, f"header_read_failed:{type(exc).__name__}"

    # Prefer the DB center.  Header CRVAL/ALPHA/DELTA are fallbacks only: CRVAL is
    # a WCS reference point, not necessarily the center of the image.
    center = _frame_center_from_indexed_columns(frame)
    center_source = "db_center"
    if center is None:
        center = _frame_center_from_header(header)
        center_source = "crval"
    if center is None:
        center = _frame_center_from_target_header(header)
        center_source = "target_header"
    if center is None:
        return None, "missing_center"

    radius_result = _radius_from_header_or_wcs(frame, path, header, margin_arcmin=margin_arcmin)
    if radius_result[0] is None:
        return None, radius_result[1] or "missing_radius"
    radius_deg = radius_result[0]
    if not math.isfinite(radius_deg) or radius_deg <= 0:
        return None, "invalid_radius"

    ra, dec = center
    if not math.isfinite(ra) or not math.isfinite(dec) or not (-90.0 <= dec <= 90.0):
        return None, f"invalid_center:{center_source}"
    return (ra % 360.0, dec, radius_deg), None


def _radius_from_header_or_wcs(
    frame: Frame, path: Path, header: fits.Header, *, margin_arcmin: float
) -> tuple[float | None, str | None]:
    naxis1 = _positive_int(header.get("NAXIS1"))
    naxis2 = _positive_int(header.get("NAXIS2"))
    if naxis1 is None or naxis2 is None:
        return None, "missing_shape"

    pixel_scale_arcsec = _header_pixel_scale_arcsec(header) or _frame_pixel_scale_arcsec(frame, header=header)
    if pixel_scale_arcsec is not None and pixel_scale_arcsec > 0:
        half_diagonal_pix = 0.5 * math.hypot(float(naxis1), float(naxis2))
        return half_diagonal_pix * pixel_scale_arcsec / 3600.0 + margin_arcmin / 60.0, None

    # If no header/profile scale is available, try a full WCS footprint radius.  The
    # center from WCS is intentionally ignored by the caller.
    try:
        cone = _frame_cone(path, margin_arcmin=margin_arcmin)
    except Exception:
        cone = None
    if cone is not None:
        return cone[2], None

    return None, "missing_scale"


def _positive_int(value: Any) -> int | None:
    try:
        n = int(value)
    except Exception:
        return None
    return n if n > 0 else None


def _frame_center_from_indexed_columns(frame: Frame) -> tuple[float, float] | None:
    ra = _finite_float(getattr(frame, "ra_center_deg", None))
    dec = _finite_float(getattr(frame, "dec_center_deg", None))
    if ra is None or dec is None:
        return None
    if not (-90.0 <= dec <= 90.0):
        return None
    return ra % 360.0, dec


def _frame_center_from_target_header(header: fits.Header) -> tuple[float, float] | None:
    """Parse common target-pointing keywords as a last-resort catalog center.

    RoboPhot headers often contain ALPHA/DELTA in sexagesimal form.  CRVAL is
    preferred, but using these target coordinates is better than producing no
    Gaia query at all for bootstrapping an identity catalog.
    """

    ra_value = header.get("ALPHA") or header.get("RA") or header.get("OBJCTRA")
    dec_value = header.get("DELTA") or header.get("DEC") or header.get("OBJCTDEC")
    if ra_value is None or dec_value is None:
        return None
    try:
        coord = SkyCoord(str(ra_value), str(dec_value), unit=(u.hourangle, u.deg), frame="icrs")
    except Exception:
        try:
            coord = SkyCoord(str(ra_value), str(dec_value), unit=(u.deg, u.deg), frame="icrs")
        except Exception:
            return None
    ra = float(coord.ra.deg) % 360.0
    dec = float(coord.dec.deg)
    if not math.isfinite(ra) or not math.isfinite(dec) or not (-90.0 <= dec <= 90.0):
        return None
    return ra, dec


def _frame_pixel_scale_arcsec(frame: Frame, *, header: fits.Header | None = None) -> float | None:
    unit = getattr(frame, "instrument_unit", None)
    value = _finite_float(getattr(unit, "pixel_scale_arcsec", None)) if unit is not None else None
    if value is not None:
        return _apply_binning(value, header)
    config = getattr(unit, "config_json", None) if unit is not None else None
    if isinstance(config, dict):
        value = _finite_float(config.get("pixel_scale_arcsec"))
        if value is not None:
            return _apply_binning(value, header)
    return None


def _frame_center_from_header(header: fits.Header) -> tuple[float, float] | None:
    ra = _finite_float(header.get("CRVAL1"))
    dec = _finite_float(header.get("CRVAL2"))
    if ra is None or dec is None:
        return None
    if not (-90.0 <= dec <= 90.0):
        return None
    return ra % 360.0, dec


def _header_pixel_scale_arcsec(header: fits.Header) -> float | None:
    # Prefer the solved WCS matrix.  For binned RoboPhot frames this correctly
    # yields the post-binning scale (about 1.3 arcsec/pix), while the instrument
    # profile stores the unbinned nominal scale (about 0.65 arcsec/pix).
    cd11 = _finite_float(header.get("CD1_1"))
    cd12 = _finite_float(header.get("CD1_2"))
    cd21 = _finite_float(header.get("CD2_1"))
    cd22 = _finite_float(header.get("CD2_2"))
    scales: list[float] = []
    if cd11 is not None and cd21 is not None:
        scales.append(math.hypot(cd11, cd21) * 3600.0)
    if cd12 is not None and cd22 is not None:
        scales.append(math.hypot(cd12, cd22) * 3600.0)
    scales = [v for v in scales if math.isfinite(v) and v > 0]
    if scales:
        return sum(scales) / len(scales)

    # Fall back to CDELT if present.
    cdelt1 = _finite_float(header.get("CDELT1"))
    cdelt2 = _finite_float(header.get("CDELT2"))
    scales = [abs(v) * 3600.0 for v in (cdelt1, cdelt2) if v is not None and math.isfinite(v) and v != 0]
    if scales:
        return sum(scales) / len(scales)
    return None


def _apply_binning(pixel_scale_arcsec: float, header: fits.Header | None) -> float:
    if header is None:
        return pixel_scale_arcsec
    binning = _finite_float(header.get("BINNING"))
    if binning is None or binning <= 0:
        bx = _finite_float(header.get("XBINNING") or header.get("XBIN"))
        by = _finite_float(header.get("YBINNING") or header.get("YBIN"))
        vals = [v for v in (bx, by) if v is not None and v > 0]
        if vals:
            binning = sum(vals) / len(vals)
    if binning is None or binning <= 1:
        return pixel_scale_arcsec
    return pixel_scale_arcsec * binning


def _finite_float(value: Any) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _frame_cone(path: Path, *, margin_arcmin: float) -> tuple[float, float, float] | None:
    with fits.open(path, memmap=False) as hdul:
        header = hdul[0].header
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        wcs = WCS(header)
    if not wcs.has_celestial:
        return None
    naxis1 = int(header.get("NAXIS1", 0) or 0)
    naxis2 = int(header.get("NAXIS2", 0) or 0)
    if naxis1 <= 0 or naxis2 <= 0:
        return None
    center_x = (naxis1 - 1) / 2.0
    center_y = (naxis2 - 1) / 2.0
    center_ra, center_dec = wcs.pixel_to_world_values(center_x, center_y)
    corners = [(0, 0), (naxis1 - 1, 0), (0, naxis2 - 1), (naxis1 - 1, naxis2 - 1)]
    corner_ra, corner_dec = wcs.pixel_to_world_values(
        [x for x, _ in corners],
        [y for _, y in corners],
    )
    c0 = SkyCoord(float(center_ra) * u.deg, float(center_dec) * u.deg, frame="icrs")
    cc = SkyCoord(corner_ra * u.deg, corner_dec * u.deg, frame="icrs")
    radius_deg = float(max(c0.separation(cc).deg)) + margin_arcmin / 60.0
    if not math.isfinite(radius_deg) or radius_deg <= 0:
        return None
    return float(center_ra) % 360.0, float(center_dec), radius_deg


def _build_gaia_tap_providers(timeout_sec: float | None) -> list[_GaiaTapProvider]:
    """Build an ordered public Gaia DR3 TAP provider chain.

    Public mirrors are queried through explicit synchronous TAP/CSV requests.
    This avoids provider/client-specific VOTable problems while preserving the
    same Gaia DR3 source identifiers and column semantics.  ESAC remains the
    final astroquery-backed fallback.
    """

    try:
        from astroquery.gaia import Gaia
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "query-gaia-for-frames requires astroquery. Install it with: "
            "python -m pip install astroquery"
        ) from exc

    providers: list[_GaiaTapProvider] = [
        _GaiaTapProvider(
            name="gaia_aip_csv",
            client=_HttpCsvTapClient("https://gaia.aip.de/tap", timeout_sec=timeout_sec),
        ),
        _GaiaTapProvider(
            name="gaia_ari_csv",
            client=_HttpCsvTapClient("https://gaia.ari.uni-heidelberg.de/tap", timeout_sec=timeout_sec),
        ),
    ]

    _configure_gaia_timeout(Gaia, timeout_sec)
    providers.append(_GaiaTapProvider(name="gaia_esac", client=Gaia))
    return providers


def _record_query_provider(
    result: GaiaQueryResult,
    provider_name: str,
    provider_failures: dict[str, str],
) -> None:
    result.query_provider_failures.update(provider_failures)
    if result.query_provider is None:
        result.query_provider = provider_name
    elif result.query_provider != provider_name:
        result.query_provider = "mixed"


def _query_gaia_cone(
    providers: list[_GaiaTapProvider],
    *,
    center_ra_deg: float,
    center_dec_deg: float,
    radius_deg: float,
    mag_limit: float | None,
    max_rows: int,
):
    if not providers:
        raise RuntimeError("no Gaia TAP providers configured")

    failures: dict[str, str] = {}
    for provider in providers:
        query = _build_gaia_cone_adql(
            table_name=provider.table_name,
            center_ra_deg=center_ra_deg,
            center_dec_deg=center_dec_deg,
            radius_deg=radius_deg,
            mag_limit=mag_limit,
            max_rows=max_rows,
        )
        try:
            job = _launch_tap_job(provider.client, query=query, max_rows=max_rows)
            return job.get_results(), provider.name, failures
        except Exception as exc:
            failures[provider.name] = f"{type(exc).__name__}: {exc}"

    raise _GaiaAllProvidersFailed(failures)


def _launch_tap_job(client: Any, *, query: str, max_rows: int):
    """Launch a small public TAP query with broad astroquery compatibility."""

    launch_sync = getattr(client, "launch_job", None)
    if callable(launch_sync):
        try:
            return launch_sync(query, maxrec=max_rows)
        except TypeError:
            return launch_sync(query)

    launch_async = getattr(client, "launch_job_async", None)
    if callable(launch_async):
        try:
            return launch_async(query, maxrec=max_rows)
        except TypeError:
            return launch_async(query)
    raise RuntimeError("TAP client exposes neither launch_job nor launch_job_async")


def _build_gaia_cone_adql(
    *,
    table_name: str,
    center_ra_deg: float,
    center_dec_deg: float,
    radius_deg: float,
    mag_limit: float | None,
    max_rows: int,
) -> str:
    mag_clause = ""
    if mag_limit is not None:
        mag_clause = f"AND phot_g_mean_mag <= {float(mag_limit):.6f}"
    return f"""
        SELECT TOP {int(max_rows)}
            source_id,
            ra,
            dec,
            ref_epoch,
            pmra,
            pmdec,
            phot_g_mean_mag,
            phot_bp_mean_mag,
            phot_rp_mean_mag,
            ruwe,
            duplicated_source
        FROM {table_name}
        WHERE 1 = CONTAINS(
            POINT('ICRS', ra, dec),
            CIRCLE('ICRS', {center_ra_deg:.10f}, {center_dec_deg:.10f}, {radius_deg:.10f})
        )
        {mag_clause}
        ORDER BY phot_g_mean_mag ASC
    """


def _gaia_row_to_catalog_source(row, *, catalog_name: str) -> dict:
    source_id = str(row["source_id"])
    g = _to_float(_row_get(row, "phot_g_mean_mag"))
    bp = _to_float(_row_get(row, "phot_bp_mean_mag"))
    rp = _to_float(_row_get(row, "phot_rp_mean_mag"))
    ref_mags = {}
    if g is not None:
        ref_mags["G"] = g
    if bp is not None:
        ref_mags["BP"] = bp
    if rp is not None:
        ref_mags["RP"] = rp
    colors = {}
    if bp is not None and rp is not None:
        colors["BP_RP"] = bp - rp
    raw_json = {name: _jsonable(row[name]) for name in row.colnames}
    return {
        "catalog_name": catalog_name,
        "source_id": source_id,
        "source_uid": make_catalog_source_uid(catalog_name, source_id),
        "ra_deg": float(row["ra"]),
        "dec_deg": float(row["dec"]),
        "epoch": _to_float(_row_get(row, "ref_epoch")),
        "pmra_masyr": _to_float(_row_get(row, "pmra")),
        "pmdec_masyr": _to_float(_row_get(row, "pmdec")),
        "ref_mags": ref_mags,
        "colors": colors,
        "source_flags": {
            "gaia_ruwe": _to_float(_row_get(row, "ruwe")),
            "gaia_duplicated_source": bool(_row_get(row, "duplicated_source")) if _row_get(row, "duplicated_source") is not None else None,
        },
        "raw_json": raw_json,
    }


def _upsert_catalog_sources(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    table = CatalogSource.__table__
    chunk_size = 1000
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        stmt = insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_catalog_sources_catalog_source",
            set_={
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
            },
        )
        session.execute(stmt)


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).options(selectinload(Frame.instrument_unit)).order_by(Frame.frame_id)
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


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        # astropy masked constants can fail on float but expose mask.
        if getattr(value, "mask", False):
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


def _jsonable(value):
    try:
        if getattr(value, "mask", False):
            return None
    except Exception:
        pass
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row_get(row, name: str):
    try:
        if name in row.colnames:
            return row[name]
    except Exception:
        pass
    return None
