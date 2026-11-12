from __future__ import annotations

import csv
import io
import math
import re
from time import perf_counter
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


MAX_GAIA_QUERY_ROWS = 20_000
MAX_GAIA_SPLIT_DEPTH = 16
MAX_GAIA_SPLIT_QUERIES_PER_CONE = 4096
MIN_GAIA_SPLIT_RADIUS_DEG = 1e-8


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
    effective_max_rows: int = MAX_GAIA_QUERY_ROWS
    skipped_covered_cones: int = 0
    skipped_already_covered: bool = False
    query_disabled: bool = False
    coverage_complete: bool = False
    completed_cones: list[dict[str, Any]] = field(default_factory=list)
    cone_attempts: list[dict[str, Any]] = field(default_factory=list)
    phase_timings_sec: dict[str, float] = field(
        default_factory=lambda: {
            "footprint_calculation": 0.0,
            "tap_attempts": 0.0,
            "catalog_source_upserts": 0.0,
            "total_query": 0.0,
        }
    )


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
    max_rows_per_frame: int = 20000,
    query_mode: str = "per_frame",
    max_radius_deg: float | None = None,
    gaia_timeout_sec: float | None = 120.0,
    match: bool = False,
    match_margin_pix: float = 2.0,
    limit_frames: int | None = None,
    dry_run: bool = False,
    coverage_cones: list[dict[str, Any] | tuple[float, float, float]] | None = None,
    commit: bool = True,
    enabled: bool = True,
) -> GaiaQueryResult:
    """Query Gaia DR3 cones for selected frames and insert catalog sources.

    This function intentionally depends on astroquery only at runtime. Grandgousier can be
    installed and used for local catalogs without astroquery; this command fails with a clear
    message if the optional dependency is absent. Each TAP request is capped at 20,000 rows;
    a response reaching that cap is recursively split using a four-child spherical cover.
    Optional ``coverage_cones`` are already-validated durable COMPLETE cones; a request is
    skipped only when one such cone contains the entire actual query cone.
    """

    query_started = perf_counter()
    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")
    if margin_arcmin < 0:
        raise ValueError("margin_arcmin must be non-negative")
    if max_rows_per_frame <= 0:
        raise ValueError("max_rows_per_frame must be positive")
    if max_radius_deg is not None and max_radius_deg <= 0:
        raise ValueError("max_radius_deg must be positive when provided")
    query_mode = (query_mode or "per_frame").strip().lower()
    if query_mode not in {"per_frame", "frame_union"}:
        raise ValueError("query_mode must be one of: per_frame, frame_union")
    if not commit and match:
        raise ValueError("commit=False cannot be combined with catalog matching")

    effective_max_rows = min(int(max_rows_per_frame), MAX_GAIA_QUERY_ROWS)
    if effective_max_rows <= 0:
        raise ValueError("max_rows_per_frame must be positive")

    result = GaiaQueryResult(
        dry_run=dry_run,
        catalog_name=catalog_name,
        query_mode=query_mode,
        effective_query_mode=query_mode,
        effective_max_rows=effective_max_rows,
    )
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.selected_frames = len(frames)
    requests: list[dict[str, Any]] = []
    frames_by_id = {int(frame.frame_id): frame for frame in frames}
    footprint_started = perf_counter()
    for frame in tqdm(frames, desc="Computing Gaia footprints", unit="frame"):
        footprint, reason = _frame_cone_for_frame_diagnostic(
            frame, margin_arcmin=margin_arcmin
        )
        if footprint is None:
            _record_failed_footprint(result, frame, reason or "unknown")
            if reason == "missing_file":
                result.skipped_missing_file += 1
            continue
        requests.append(
            {
                "cone": _cone_dict(footprint),
                "frame_ids": [int(frame.frame_id)],
            }
        )
    result.phase_timings_sec["footprint_calculation"] += (
        perf_counter() - footprint_started
    )

    if query_mode == "frame_union":
        if requests and max_radius_deg is not None:
            requested_union = _combine_cones(
                [_cone_tuple(item["cone"]) for item in requests]
            )
            if requested_union[2] > float(max_radius_deg):
                result.effective_query_mode = "frame_clusters"
        requests, oversized = _frame_union_requests(
            requests, max_radius_deg=max_radius_deg
        )
        for item in oversized:
            result.skipped_oversized_cones += 1
            for frame_id in item["frame_ids"]:
                frame = frames_by_id.get(int(frame_id))
                if frame is not None:
                    _record_failed_footprint(result, frame, "oversized_cone")
    elif max_radius_deg is not None:
        kept_requests = []
        for item in requests:
            if float(item["cone"]["radius_deg"]) > float(max_radius_deg):
                result.skipped_oversized_cones += 1
                for frame_id in item["frame_ids"]:
                    frame = frames_by_id.get(int(frame_id))
                    if frame is not None:
                        _record_failed_footprint(result, frame, "oversized_cone")
            else:
                kept_requests.append(item)
        requests = kept_requests

    uncovered_requests: list[dict[str, Any]] = []
    for request in requests:
        if _covered_by_one_cone(request["cone"], coverage_cones or []):
            result.skipped_covered_cones += 1
            continue
        uncovered_requests.append(request)

    if requests and not uncovered_requests and result.failed_frames == 0:
        result.skipped_already_covered = True
        result.coverage_complete = True
    elif uncovered_requests:
        if not enabled:
            result.query_disabled = True
        else:
            providers = _build_gaia_tap_providers(gaia_timeout_sec)
            with tqdm(desc="Querying Gaia DR3 TAP", unit="attempt") as progress:
                for request in uncovered_requests:
                    result.queried_frames += len(request["frame_ids"])
                    proof = _query_gaia_cone_recursive(
                        session,
                        providers,
                        cone=request["cone"],
                        result=result,
                        catalog_name=catalog_name,
                        mag_limit=mag_limit,
                        max_rows=effective_max_rows,
                        dry_run=dry_run,
                        depth=0,
                        frame_ids=request["frame_ids"],
                        query_count=[0],
                        progress=progress,
                    )
                    if proof is None:
                        result.failed_frames += len(request["frame_ids"])
                    else:
                        result.completed_cones.append(proof)

            result.coverage_complete = (
                result.failed_frames == 0
                and len(result.completed_cones) == len(uncovered_requests)
            )

    if not dry_run and commit:
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
    result.phase_timings_sec["total_query"] = perf_counter() - query_started
    return result


def _frame_union_requests(
    requests: list[dict[str, Any]], *, max_radius_deg: float | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build actual cone requests for frame-union mode, retaining frame provenance."""

    if not requests:
        return [], []
    combined = _combine_cones([_cone_tuple(item["cone"]) for item in requests])
    if max_radius_deg is None or combined[2] <= float(max_radius_deg):
        return [
            {
                "cone": _cone_dict(combined),
                "frame_ids": sorted(
                    int(frame_id)
                    for item in requests
                    for frame_id in item["frame_ids"]
                ),
            }
        ], []

    clusters: list[list[dict[str, Any]]] = []
    oversized: list[dict[str, Any]] = []
    for request in requests:
        cone = request["cone"]
        if float(cone["radius_deg"]) > float(max_radius_deg):
            oversized.append(request)
            continue
        best_index = None
        best_radius = None
        for index, cluster in enumerate(clusters):
            candidate = cluster + [request]
            radius = _combine_cones(
                [_cone_tuple(item["cone"]) for item in candidate]
            )[2]
            if radius <= float(max_radius_deg) and (
                best_radius is None or radius < best_radius
            ):
                best_index = index
                best_radius = radius
        if best_index is None:
            clusters.append([request])
        else:
            clusters[best_index].append(request)
    grouped = []
    for cluster in clusters:
        grouped.append(
            {
                "cone": _cone_dict(
                    _combine_cones([_cone_tuple(item["cone"]) for item in cluster])
                ),
                "frame_ids": sorted(
                    int(frame_id)
                    for item in cluster
                    for frame_id in item["frame_ids"]
                ),
            }
        )
    return grouped, oversized


def _query_gaia_cone_recursive(
    session: Session,
    providers: list[_GaiaTapProvider],
    *,
    cone: dict[str, float],
    result: GaiaQueryResult,
    catalog_name: str,
    mag_limit: float | None,
    max_rows: int,
    dry_run: bool,
    depth: int,
    frame_ids: list[int],
    query_count: list[int],
    progress: Any,
) -> dict[str, Any] | None:
    center_ra, center_dec, radius_deg = _cone_tuple(cone)
    if not _valid_cone(center_ra, center_dec, radius_deg):
        result.failed_footprint_reasons["invalid_query_cone"] = (
            result.failed_footprint_reasons.get("invalid_query_cone", 0) + 1
        )
        result.cone_attempts.append(
            {"cone": dict(cone), "depth": depth, "status": "FAILED_INVALID_GEOMETRY"}
        )
        return None
    if query_count[0] >= MAX_GAIA_SPLIT_QUERIES_PER_CONE:
        result.failed_footprint_reasons["maxrec_split_query_budget"] = (
            result.failed_footprint_reasons.get("maxrec_split_query_budget", 0) + 1
        )
        result.cone_attempts.append(
            {"cone": dict(cone), "depth": depth, "status": "FAILED_QUERY_BUDGET"}
        )
        return None
    query_count[0] += 1
    attempt = {
        "cone": dict(cone),
        "depth": depth,
        "frame_ids": list(frame_ids),
        "maxrec": int(max_rows),
        "status": "RUNNING",
    }
    result.cone_attempts.append(attempt)
    result.queried_cones += 1
    progress.update(1)
    try:
        tap_attempt_started = perf_counter()
        try:
            table, provider_name, provider_failures = _query_gaia_cone(
                providers,
                center_ra_deg=center_ra,
                center_dec_deg=center_dec,
                radius_deg=radius_deg,
                mag_limit=mag_limit,
                max_rows=max_rows,
            )
        finally:
            result.phase_timings_sec["tap_attempts"] += (
                perf_counter() - tap_attempt_started
            )
        _record_query_provider(result, provider_name, provider_failures)
        result.read_rows += len(table)
        rows = [_gaia_row_to_catalog_source(row, catalog_name=catalog_name) for row in table]
        if rows and not dry_run:
            upsert_started = perf_counter()
            try:
                _upsert_catalog_sources(session, rows)
            finally:
                result.phase_timings_sec["catalog_source_upserts"] += (
                    perf_counter() - upsert_started
                )
            result.inserted_or_updated += len(rows)
        attempt.update(
            {
                "provider": provider_name,
                "provider_failures": dict(provider_failures),
                "rows_returned": len(table),
            }
        )
        if len(table) >= int(max_rows):
            result.row_limit_reached_cones += 1
            attempt["status"] = "MAXREC_TRUNCATED"
            if depth >= MAX_GAIA_SPLIT_DEPTH or radius_deg <= MIN_GAIA_SPLIT_RADIUS_DEG:
                reason = (
                    "maxrec_split_depth_exceeded"
                    if depth >= MAX_GAIA_SPLIT_DEPTH
                    else "maxrec_split_min_radius_reached"
                )
                result.failed_footprint_reasons[reason] = (
                    result.failed_footprint_reasons.get(reason, 0) + 1
                )
                return None
            try:
                children = _split_gaia_cone_four(center_ra, center_dec, radius_deg)
            except ValueError:
                result.failed_footprint_reasons["maxrec_split_invalid_geometry"] = (
                    result.failed_footprint_reasons.get("maxrec_split_invalid_geometry", 0) + 1
                )
                return None
            child_proofs = []
            for child in children:
                child_proof = _query_gaia_cone_recursive(
                    session,
                    providers,
                    cone=_cone_dict(child),
                    result=result,
                    catalog_name=catalog_name,
                    mag_limit=mag_limit,
                    max_rows=max_rows,
                    dry_run=dry_run,
                    depth=depth + 1,
                    frame_ids=frame_ids,
                    query_count=query_count,
                    progress=progress,
                )
                if child_proof is None:
                    child_proofs = []
                    break
                child_proofs.append(child_proof)
            if len(child_proofs) != 4:
                return None
            attempt["children"] = [child["cone"] for child in child_proofs]
            return {
                "cone": dict(cone),
                "status": "COMPLETE",
                "completion_method": "four_child_recursive_cover",
                "query_outcome": "MAXREC_SPLIT",
                "rows_returned": len(table),
                "maxrec_per_query": int(max_rows),
                "provider": provider_name,
                "provider_failures": dict(provider_failures),
                "frame_ids": list(frame_ids),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "children": child_proofs,
            }
        attempt["status"] = "COMPLETE_BELOW_MAXREC"
        return {
            "cone": dict(cone),
            "status": "COMPLETE",
            "completion_method": "direct_query",
            "query_outcome": "BELOW_MAXREC",
            "rows_returned": len(table),
            "maxrec_per_query": int(max_rows),
            "provider": provider_name,
            "provider_failures": dict(provider_failures),
            "frame_ids": list(frame_ids),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    except _GaiaAllProvidersFailed as exc:
        result.query_provider_failures.update(exc.failures)
        reason = "gaia_query_all_providers_failed"
        result.failed_footprint_reasons[reason] = result.failed_footprint_reasons.get(reason, 0) + 1
        attempt.update({"status": "FAILED", "provider_failures": dict(exc.failures)})
        return None
    except Exception as exc:
        reason = f"gaia_query_exception:{type(exc).__name__}"
        result.failed_footprint_reasons[reason] = result.failed_footprint_reasons.get(reason, 0) + 1
        attempt.update({"status": "FAILED", "error_type": type(exc).__name__})
        return None


def _split_gaia_cone_four(
    center_ra_deg: float, center_dec_deg: float, radius_deg: float
) -> list[tuple[float, float, float]]:
    """Return four overlapping spherical child caps that cover the parent.

    Child centers are geodesic offsets by R/2 at PAs 0/90/180/270 degrees. Any
    parent point is at PA delta <=45 degrees from its nearest child center. With
    d=R/2, the haversine identity gives
    ``hav(delta) = hav(R-d) + sin(R)*sin(d)*hav(45 degrees)``. This form avoids
    the small-angle cancellation of the cosine law. The child radius is the
    maximum of that bound and R/2, plus a 1e-12-radian (or relative 1e-12)
    outward safety margin. Degrees are used at the API/storage boundary; the
    bound is evaluated in radians. Astropy directional offsets handle RA wrap
    and polar centers without a planar approximation.
    """

    if not _valid_cone(center_ra_deg, center_dec_deg, radius_deg):
        raise ValueError("cannot split invalid Gaia cone geometry")
    radius = math.radians(float(radius_deg))
    offset = radius / 2.0
    haversine_bound = (
        math.sin((radius - offset) / 2.0) ** 2
        + math.sin(radius)
        * math.sin(offset)
        * math.sin(math.radians(45.0) / 2.0) ** 2
    )
    bound = 2.0 * math.asin(math.sqrt(max(0.0, min(1.0, haversine_bound))))
    safety_margin = max(1e-12, radius * 1e-12)
    child_radius = min(math.pi, max(offset, bound) + safety_margin)
    if child_radius >= radius:
        raise ValueError("four-child Gaia split does not shrink this cone")

    parent = SkyCoord(
        float(center_ra_deg) % 360.0 * u.deg,
        float(center_dec_deg) * u.deg,
        frame="icrs",
    )
    children = []
    for position_angle_deg in (0.0, 90.0, 180.0, 270.0):
        child_center = parent.directional_offset_by(
            float(position_angle_deg) * u.deg, offset * u.rad
        )
        ra = float(child_center.ra.deg) % 360.0
        dec = float(child_center.dec.deg)
        child = (ra, dec, math.degrees(child_radius))
        if not _valid_cone(*child):
            raise ValueError("four-child Gaia split generated invalid geometry")
        children.append(child)
    return children


def _valid_cone(ra_deg: float, dec_deg: float, radius_deg: float) -> bool:
    try:
        values = (float(ra_deg), float(dec_deg), float(radius_deg))
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) for value in values)
        and -90.0 <= values[1] <= 90.0
        and 0.0 < values[2] <= 180.0
    )


def _cone_dict(cone: tuple[float, float, float]) -> dict[str, float]:
    ra, dec, radius = (float(value) for value in cone)
    if not _valid_cone(ra, dec, radius):
        raise ValueError("invalid Gaia cone geometry")
    return {"ra_deg": ra % 360.0, "dec_deg": dec, "radius_deg": radius}


def _cone_tuple(cone: dict[str, Any] | tuple[float, float, float]) -> tuple[float, float, float]:
    if isinstance(cone, dict):
        return (
            float(cone["ra_deg"]),
            float(cone["dec_deg"]),
            float(cone["radius_deg"]),
        )
    return tuple(float(value) for value in cone)  # type: ignore[return-value]


def _covered_by_one_cone(
    requested: dict[str, Any] | tuple[float, float, float],
    coverage_cones: list[dict[str, Any] | tuple[float, float, float]],
) -> bool:
    req_ra, req_dec, req_radius = _cone_tuple(requested)
    if not _valid_cone(req_ra, req_dec, req_radius):
        return False
    req_ra %= 360.0
    for saved in coverage_cones:
        try:
            old_ra, old_dec, old_radius = _cone_tuple(saved)
        except (KeyError, TypeError, ValueError):
            continue
        if not _valid_cone(old_ra, old_dec, old_radius):
            continue
        old_ra %= 360.0
        if old_ra == req_ra and old_dec == req_dec and old_radius >= req_radius:
            return True
        old_center = SkyCoord(old_ra * u.deg, old_dec * u.deg, frame="icrs")
        req_center = SkyCoord(req_ra * u.deg, req_dec * u.deg, frame="icrs")
        separation = float(old_center.separation(req_center).deg)
        # The negative tolerance makes non-identical containment conservative;
        # exact repeated coordinates/radii take the equality branch above.
        if separation + req_radius <= old_radius - 1e-12:
            return True
    return False


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
    max_rows = min(int(max_rows), MAX_GAIA_QUERY_ROWS)
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")

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

    max_rows = min(int(max_rows), MAX_GAIA_QUERY_ROWS)
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")

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
    max_rows = min(int(max_rows), MAX_GAIA_QUERY_ROWS)
    if max_rows <= 0:
        raise ValueError("max_rows must be positive")
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
