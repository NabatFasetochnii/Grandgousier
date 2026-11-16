from __future__ import annotations

import asyncio
import csv
import inspect
import io
import math
import re
import time
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import urljoin

import numpy as np
import requests
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Session, selectinload
from tqdm import tqdm

from grandgousier.catalogs.frame_match import match_catalog_to_frames
from grandgousier.catalogs.import_catalog import make_catalog_source_uid
from grandgousier.db.models import CatalogSource, Frame, ObjectCatalogLink


GAIA_DR3_RELEASE = "DR3"
GAIA_DR3_TABLE = "gaiadr3.gaia_source"
GAIA_WORKING_CATALOG_COLUMNS = ("source_id", "ra", "dec", "phot_g_mean_mag")


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
    table_name: str = GAIA_DR3_TABLE


class _StaticTapJob:
    """Minimal TAP-job compatibility wrapper for a materialized TAP response."""

    def __init__(self, response: _TapResponse):
        self.response = response

    def get_results(self) -> list[_TapCsvRow]:
        if not self.response.complete:
            status = self.response.query_status or "unknown status"
            raise _TapIncompleteResponseError(
                f"TAP {self.response.format} response is incomplete ({status})",
                query_status=self.response.query_status,
            )
        return self.response.rows


class _TapCsvRow(dict):
    """Mapping row with the ``colnames`` attribute expected by Astropy rows."""

    @property
    def colnames(self) -> list[str]:
        return list(self.keys())


class GaiaWorkingCatalog:
    """Compact, in-memory Gaia identity/position snapshot with explicit lifecycle.

    Source identifiers are fixed-width NumPy Unicode text (never floating point or
    platform-sized integers); the three numeric columns are contiguous float64
    arrays.  This object deliberately has no dataclass/payload serializer.
    """

    __slots__ = (
        "_closed",
        "_dec_deg",
        "_g_mag",
        "_ra_deg",
        "_source_ids",
        "_spatial_index",
    )

    def __init__(self, rows: Iterable[Any] = ()) -> None:
        self._closed = False
        self._spatial_index = None
        unique: dict[str, tuple[float, float, float]] = {}
        for row in rows:
            source_id, ra, dec, g_mag = _normalise_working_catalog_row(row)
            candidate = (ra, dec, g_mag)
            previous = unique.get(source_id)
            if previous is None or _working_row_order(candidate) < _working_row_order(previous):
                unique[source_id] = candidate
        self._set_arrays(unique)

    @classmethod
    def _from_unique_rows(
        cls, rows: Mapping[str, tuple[float, float, float]]
    ) -> GaiaWorkingCatalog:
        catalog = cls.__new__(cls)
        catalog._closed = False
        catalog._spatial_index = None
        catalog._set_arrays(rows)
        return catalog

    def _set_arrays(self, rows: Mapping[str, tuple[float, float, float]]) -> None:
        self._spatial_index = None
        source_ids = sorted(rows, key=lambda source_id: (int(source_id), source_id))
        width = max((len(source_id) for source_id in source_ids), default=1)
        self._source_ids = np.asarray(source_ids, dtype=f"<U{width}")
        self._ra_deg = np.asarray([rows[source_id][0] for source_id in source_ids], dtype=np.float64)
        self._dec_deg = np.asarray([rows[source_id][1] for source_id in source_ids], dtype=np.float64)
        self._g_mag = np.asarray([rows[source_id][2] for source_id in source_ids], dtype=np.float64)
        for array in (self._source_ids, self._ra_deg, self._dec_deg, self._g_mag):
            array.flags.writeable = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Gaia working catalog is closed")

    @staticmethod
    def _readonly_view(array: np.ndarray) -> np.ndarray:
        view = array.view()
        view.flags.writeable = False
        return view

    @property
    def source_ids(self) -> np.ndarray:
        self._ensure_open()
        return self._readonly_view(self._source_ids)

    @property
    def ra_deg(self) -> np.ndarray:
        self._ensure_open()
        return self._readonly_view(self._ra_deg)

    @property
    def dec_deg(self) -> np.ndarray:
        self._ensure_open()
        return self._readonly_view(self._dec_deg)

    @property
    def g_mag(self) -> np.ndarray:
        self._ensure_open()
        return self._readonly_view(self._g_mag)

    def __len__(self) -> int:
        self._ensure_open()
        return len(self._source_ids)

    def clear(self) -> None:
        """Release all rows while leaving this catalog open and reusable as empty."""

        self._ensure_open()
        self._set_arrays({})

    def close(self) -> None:
        """Release owned arrays and permanently disable access to this catalog."""

        if not self._closed:
            self._set_arrays({})
            self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def match_nearest(
        self,
        ra_deg: Any,
        dec_deg: Any,
        *,
        radius_arcsec: float,
        max_positions: int = 100_000,
        max_pair_comparisons: int = 100_000_000,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return nearest catalog indices and separations, bounded by radius/work.

        Unmatched positions have index ``-1`` and infinite separation.  The
        spherical neighbor grid handles RA wrap and polar coordinates without
        allocating an all-pairs matrix.  ``max_pair_comparisons`` caps exact
        candidate distance evaluations; ties resolve to the first
        (source-ID-sorted) row.
        """

        self._ensure_open()
        try:
            radius = float(radius_arcsec)
        except (TypeError, ValueError):
            raise ValueError("radius_arcsec must be a finite positive number") from None
        if not math.isfinite(radius) or radius <= 0 or radius > 180.0 * 3600.0:
            raise ValueError("radius_arcsec must be a finite positive number")
        if isinstance(max_positions, bool) or not isinstance(max_positions, (int, np.integer)):
            raise TypeError("max_positions must be a positive integer")
        if max_positions <= 0:
            raise ValueError("max_positions must be a positive integer")
        if (
            isinstance(max_pair_comparisons, bool)
            or not isinstance(max_pair_comparisons, (int, np.integer))
        ):
            raise TypeError("max_pair_comparisons must be a positive integer")
        if max_pair_comparisons <= 0:
            raise ValueError("max_pair_comparisons must be a positive integer")

        try:
            query_ra = np.asarray(ra_deg, dtype=np.float64)
            query_dec = np.asarray(dec_deg, dtype=np.float64)
        except (TypeError, ValueError):
            raise ValueError("input coordinates must be numeric one-dimensional arrays") from None
        if query_ra.ndim != 1 or query_dec.ndim != 1 or query_ra.shape != query_dec.shape:
            raise ValueError("input RA/Dec must be same-length one-dimensional arrays")
        if len(query_ra) > max_positions:
            raise ValueError("input position count exceeds max_positions")
        if not np.all(np.isfinite(query_ra)) or not np.all(np.isfinite(query_dec)):
            raise ValueError("input coordinates must be finite")
        if np.any((query_dec < -90.0) | (query_dec > 90.0)):
            raise ValueError("input declinations must be within [-90, 90] degrees")

        indices = np.full(len(query_ra), -1, dtype=np.int64)
        separations = np.full(len(query_ra), np.inf, dtype=np.float64)
        if not len(query_ra) or not len(self._source_ids):
            return indices, separations
        max_chord = 2.0 * math.sin(math.radians(radius / 3600.0) / 2.0)
        search_chord = max_chord + 1e-15
        spatial_index = self._spatial_index
        if spatial_index is None or spatial_index[0] != search_chord:
            source_vectors = _unit_sky_vectors(self._ra_deg, self._dec_deg)
            source_cells = np.floor((source_vectors + 1.0) / search_chord).astype(np.int64)
            buckets: dict[tuple[int, int, int], list[int]] = {}
            for source_index, cell in enumerate(source_cells):
                key = (int(cell[0]), int(cell[1]), int(cell[2]))
                buckets.setdefault(key, []).append(source_index)
            spatial_index = (search_chord, buckets)
            self._spatial_index = spatial_index

        buckets = spatial_index[1]
        arcsec_per_radian = 180.0 * 3600.0 / math.pi
        tie_rtol = 32.0 * np.finfo(np.float64).eps
        candidate_evaluations = 0
        for query_index, (query_ra_value, query_dec_value) in enumerate(zip(query_ra, query_dec)):
            query_ra_rad = math.radians(float(np.remainder(query_ra_value, 360.0)))
            query_dec_rad = math.radians(float(query_dec_value))
            query_cos_dec_for_vector = math.cos(query_dec_rad)
            query_vector = np.asarray(
                (
                    query_cos_dec_for_vector * math.cos(query_ra_rad),
                    query_cos_dec_for_vector * math.sin(query_ra_rad),
                    math.sin(query_dec_rad),
                ),
                dtype=np.float64,
            )
            lower = (
                np.floor((query_vector - search_chord + 1.0) / search_chord).astype(np.int64) - 1
            )
            upper = (
                np.floor((query_vector + search_chord + 1.0) / search_chord).astype(np.int64) + 1
            )
            candidates: list[int] = []
            remaining_work = max_pair_comparisons - candidate_evaluations
            for x_cell in range(int(lower[0]), int(upper[0]) + 1):
                for y_cell in range(int(lower[1]), int(upper[1]) + 1):
                    for z_cell in range(int(lower[2]), int(upper[2]) + 1):
                        bucket = buckets.get((x_cell, y_cell, z_cell), ())
                        if len(candidates) + len(bucket) > remaining_work:
                            raise ValueError("nearest-match work exceeds max_pair_comparisons")
                        candidates.extend(bucket)
            if not candidates:
                continue
            candidate_evaluations += len(candidates)

            candidate_indices = np.sort(np.asarray(candidates, dtype=np.int64))
            query_ra_normalized = float(np.remainder(query_ra_value, 360.0))
            delta_ra_deg = np.remainder(
                self._ra_deg[candidate_indices] - query_ra_normalized + 180.0, 360.0
            ) - 180.0
            catalog_dec_rad = np.deg2rad(self._dec_deg[candidate_indices])
            delta_dec_half = (catalog_dec_rad - query_dec_rad) / 2.0
            delta_ra_half = np.deg2rad(delta_ra_deg) / 2.0
            query_cos_dec = 0.0 if abs(float(query_dec_value)) == 90.0 else math.cos(query_dec_rad)
            catalog_cos_dec = np.cos(catalog_dec_rad)
            catalog_cos_dec[np.abs(self._dec_deg[candidate_indices]) == 90.0] = 0.0
            haversine = np.sin(delta_dec_half) ** 2 + (
                query_cos_dec * catalog_cos_dec * np.sin(delta_ra_half) ** 2
            )
            np.clip(haversine, 0.0, 1.0, out=haversine)

            best_haversine = float(np.min(haversine))
            tied = np.flatnonzero(
                np.isclose(haversine, best_haversine, rtol=tie_rtol, atol=0.0)
            )
            best_local = int(tied[0])
            best_chord = 2.0 * math.sqrt(float(haversine[best_local]))
            if best_chord <= search_chord:
                indices[query_index] = candidate_indices[best_local]
                angle = 2.0 * math.atan2(
                    math.sqrt(float(haversine[best_local])),
                    math.sqrt(max(0.0, 1.0 - float(haversine[best_local]))),
                )
                separations[query_index] = angle * arcsec_per_radian
        return indices, separations

    def match_detections(
        self,
        detection_ids: Sequence[int],
        ra_deg: Any,
        dec_deg: Any,
        *,
        radius_arcsec: float,
        max_positions: int = 100_000,
        max_pair_comparisons: int = 100_000_000,
    ) -> list[tuple[int, str, float]]:
        """Return the legacy deterministic nearest assignment for diagnostics only.

        Promotion authority uses :meth:`select_unique_detection_matches` instead:
        greedy nearest assignment is useful to inspect, but must not resolve a
        candidate graph with ambiguity or source competition.
        """

        normalized_detection_ids, candidates = self._detection_candidate_edges(
            detection_ids,
            ra_deg,
            dec_deg,
            radius_arcsec=radius_arcsec,
            max_positions=max_positions,
            max_pair_comparisons=max_pair_comparisons,
        )
        used_detections: set[int] = set()
        used_sources: set[int] = set()
        accepted: list[tuple[int, str, float]] = []
        for separation, query_index, source_index in candidates:
            detection_id = normalized_detection_ids[query_index]
            if detection_id in used_detections or source_index in used_sources:
                continue
            used_detections.add(detection_id)
            used_sources.add(source_index)
            accepted.append(
                (detection_id, str(self._source_ids[source_index]), float(separation))
            )
        return accepted

    def select_unique_detection_matches(
        self,
        detection_ids: Sequence[int],
        ra_deg: Any,
        dec_deg: Any,
        *,
        radius_arcsec: float,
        max_positions: int = 100_000,
        max_pair_comparisons: int = 100_000_000,
    ) -> tuple[list[tuple[int, str, float]], dict[str, int]]:
        """Select only reciprocal degree-one pairs from this frame's radius graph.

        ``reciprocal_unique_matches`` counts pairs whose detection and Gaia candidate
        degrees are both one. ``ambiguous_detections`` counts detections with multiple
        Gaia candidates, while ``conflicting_gaia_sources`` counts Gaia sources that
        candidate-match multiple detections. Ambiguities are never resolved by distance
        or stable ordering. The diagnostics contain bounded counts only.
        """

        normalized_detection_ids, candidates = self._detection_candidate_edges(
            detection_ids,
            ra_deg,
            dec_deg,
            radius_arcsec=radius_arcsec,
            max_positions=max_positions,
            max_pair_comparisons=max_pair_comparisons,
        )
        detection_candidate_counts = [0] * len(normalized_detection_ids)
        source_candidate_counts: dict[int, int] = {}
        for _separation, detection_index, source_index in candidates:
            detection_candidate_counts[detection_index] += 1
            source_candidate_counts[source_index] = (
                source_candidate_counts.get(source_index, 0) + 1
            )

        selected_candidates = [
            (separation, detection_index, source_index)
            for separation, detection_index, source_index in candidates
            if detection_candidate_counts[detection_index] == 1
            and source_candidate_counts[source_index] == 1
        ]
        selected_candidates.sort(key=lambda candidate: (candidate[1], candidate[2]))
        matches = [
            (
                normalized_detection_ids[detection_index],
                str(self._source_ids[source_index]),
                float(separation),
            )
            for separation, detection_index, source_index in selected_candidates
        ]
        diagnostics = {
            "detections_considered": len(normalized_detection_ids),
            "reciprocal_unique_matches": len(matches),
            "ambiguous_detections": sum(
                count > 1 for count in detection_candidate_counts
            ),
            "conflicting_gaia_sources": sum(
                count > 1 for count in source_candidate_counts.values()
            ),
        }
        return matches, diagnostics

    def _detection_candidate_edges(
        self,
        detection_ids: Sequence[int],
        ra_deg: Any,
        dec_deg: Any,
        *,
        radius_arcsec: float,
        max_positions: int,
        max_pair_comparisons: int,
    ) -> tuple[list[int], list[tuple[float, int, int]]]:
        """Build the bounded in-radius candidate graph for one frame."""

        self._ensure_open()
        try:
            radius = float(radius_arcsec)
        except (TypeError, ValueError):
            raise ValueError("radius_arcsec must be a finite positive number") from None
        if not math.isfinite(radius) or radius <= 0 or radius > 180.0 * 3600.0:
            raise ValueError("radius_arcsec must be a finite positive number")
        if isinstance(max_positions, bool) or not isinstance(max_positions, (int, np.integer)):
            raise TypeError("max_positions must be a positive integer")
        if max_positions <= 0:
            raise ValueError("max_positions must be a positive integer")
        if (
            isinstance(max_pair_comparisons, bool)
            or not isinstance(max_pair_comparisons, (int, np.integer))
        ):
            raise TypeError("max_pair_comparisons must be a positive integer")
        if max_pair_comparisons <= 0:
            raise ValueError("max_pair_comparisons must be a positive integer")

        try:
            query_ra = np.asarray(ra_deg, dtype=np.float64)
            query_dec = np.asarray(dec_deg, dtype=np.float64)
        except (TypeError, ValueError):
            raise ValueError("input coordinates must be numeric one-dimensional arrays") from None
        if query_ra.ndim != 1 or query_dec.ndim != 1 or query_ra.shape != query_dec.shape:
            raise ValueError("input RA/Dec must be same-length one-dimensional arrays")
        if len(query_ra) > max_positions:
            raise ValueError("input position count exceeds max_positions")
        if len(detection_ids) != len(query_ra):
            raise ValueError("detection IDs and input coordinates must have equal lengths")
        if not np.all(np.isfinite(query_ra)) or not np.all(np.isfinite(query_dec)):
            raise ValueError("input coordinates must be finite")
        if np.any((query_dec < -90.0) | (query_dec > 90.0)):
            raise ValueError("input declinations must be within [-90, 90] degrees")

        normalized_detection_ids: list[int] = []
        for detection_id in detection_ids:
            if isinstance(detection_id, bool) or not isinstance(detection_id, (int, np.integer)):
                raise TypeError("detection IDs must be integers")
            normalized_detection_ids.append(int(detection_id))
        if len(set(normalized_detection_ids)) != len(normalized_detection_ids):
            raise ValueError("detection IDs must be unique within the frame")
        if not len(query_ra) or not len(self._source_ids):
            return normalized_detection_ids, []

        max_chord = 2.0 * math.sin(math.radians(radius / 3600.0) / 2.0)
        search_chord = max_chord + 1e-15
        spatial_index = self._spatial_index
        if spatial_index is None or spatial_index[0] != search_chord:
            source_vectors = _unit_sky_vectors(self._ra_deg, self._dec_deg)
            source_cells = np.floor((source_vectors + 1.0) / search_chord).astype(np.int64)
            buckets: dict[tuple[int, int, int], list[int]] = {}
            for source_index, cell in enumerate(source_cells):
                key = (int(cell[0]), int(cell[1]), int(cell[2]))
                buckets.setdefault(key, []).append(source_index)
            spatial_index = (search_chord, buckets)
            self._spatial_index = spatial_index

        buckets = spatial_index[1]
        candidate_evaluations = 0
        candidates: list[tuple[float, int, int]] = []
        for query_index, (query_ra_value, query_dec_value) in enumerate(zip(query_ra, query_dec)):
            query_ra_rad = math.radians(float(np.remainder(query_ra_value, 360.0)))
            query_dec_rad = math.radians(float(query_dec_value))
            query_cos_dec = math.cos(query_dec_rad)
            query_vector = np.asarray(
                (
                    query_cos_dec * math.cos(query_ra_rad),
                    query_cos_dec * math.sin(query_ra_rad),
                    math.sin(query_dec_rad),
                ),
                dtype=np.float64,
            )
            lower = (
                np.floor((query_vector - search_chord + 1.0) / search_chord).astype(np.int64) - 1
            )
            upper = (
                np.floor((query_vector + search_chord + 1.0) / search_chord).astype(np.int64) + 1
            )
            source_candidates: list[int] = []
            remaining_work = max_pair_comparisons - candidate_evaluations
            for x_cell in range(int(lower[0]), int(upper[0]) + 1):
                for y_cell in range(int(lower[1]), int(upper[1]) + 1):
                    for z_cell in range(int(lower[2]), int(upper[2]) + 1):
                        bucket = buckets.get((x_cell, y_cell, z_cell), ())
                        if len(source_candidates) + len(bucket) > remaining_work:
                            raise ValueError("detection-match work exceeds max_pair_comparisons")
                        source_candidates.extend(bucket)
            if not source_candidates:
                continue
            candidate_evaluations += len(source_candidates)

            # Sorting the grid hits restores the catalog's stable source-ID order.
            for source_index in sorted(source_candidates):
                separation = _working_sky_sep_arcsec(
                    float(query_ra_value),
                    float(query_dec_value),
                    float(self._ra_deg[source_index]),
                    float(self._dec_deg[source_index]),
                )
                if separation <= radius:
                    candidates.append((separation, query_index, source_index))

        # Keep diagnostic nearest assignment ties stable without letting this
        # ordering influence reciprocal-unique promotion selection.
        candidates.sort(key=lambda candidate: (candidate[0], candidate[1], candidate[2]))
        return normalized_detection_ids, candidates

    def nearest_neighbor_separations(
        self, source_ids: Iterable[str]
    ) -> dict[str, float | None]:
        """Return each requested source's nearest *other* row separation in arcsec.

        This operates only on the in-memory catalog. Callers must separately prove
        that the catalog is complete for the requested footprint before treating a
        missing/large separation as evidence of isolation.
        """

        self._ensure_open()
        requested = list(dict.fromkeys(str(source_id) for source_id in source_ids))
        index_by_id = {
            str(source_id): index for index, source_id in enumerate(self._source_ids)
        }
        output: dict[str, float | None] = {source_id: None for source_id in requested}
        valid = [
            (source_id, index_by_id[source_id])
            for source_id in requested
            if source_id in index_by_id
        ]
        if len(self._source_ids) < 2 or not valid:
            return output

        catalog_coordinates = SkyCoord(
            ra=self._ra_deg * u.deg,
            dec=self._dec_deg * u.deg,
            frame="icrs",
        )
        target_indices = np.asarray([index for _source_id, index in valid], dtype=np.int64)
        target_coordinates = SkyCoord(
            ra=self._ra_deg[target_indices] * u.deg,
            dec=self._dec_deg[target_indices] * u.deg,
            frame="icrs",
        )
        _nearest_indices, separations, _ = target_coordinates.match_to_catalog_sky(
            catalog_coordinates,
            nthneighbor=2,
        )
        for (source_id, _index), separation in zip(valid, separations.arcsec, strict=True):
            value = float(separation)
            output[source_id] = value if math.isfinite(value) else None
        return output


@dataclass(frozen=True)
class _TapResponse:
    rows: list[_TapCsvRow]
    format: str
    complete: bool
    query_status: str | None = None


class _TapIncompleteResponseError(RuntimeError):
    """Raised instead of exposing partial rows as a successful TAP result."""

    def __init__(self, message: str, *, query_status: str | None = None):
        self.query_status = query_status
        super().__init__(message)


class _TapQueryStatusError(RuntimeError):
    """A TAP protocol error with only its bounded, sanitized INFO detail."""


class _HttpCsvTapClient:
    """Small synchronous TAP client that requests CSV and safely accepts VOTable.

    ``astroquery.utils.tap.Tap`` currently exposes two platform/service
    interoperability problems relevant to the clean rebuild: Gaia@AIP can reject
    its default Accept header with HTTP 406, while the ARI VOTable/BINARY parser
    can overflow on Windows when a 64-bit Gaia ``source_id`` is decoded through a
    C ``long``.  The TAP protocol itself is fine.  Requesting CSV over the standard
    ``/sync`` endpoint avoids both client-side failure modes. Some services still
    answer with VOTable XML, so response parsing is selected from the body and
    Content-Type; Gaia ``source_id`` remains an exact decimal string in either format.
    """

    def __init__(self, base_url: str, *, timeout_sec: float | None = 120.0):
        self.base_url = base_url.rstrip("/")
        self.sync_url = f"{self.base_url}/sync"
        self.timeout = None if timeout_sec is None else float(timeout_sec)

    def launch_job(self, query: str, maxrec: int | None = None):
        response = _tap_sync_response(
            self.sync_url,
            query=query,
            max_rows=maxrec,
            timeout_sec=self.timeout,
        )
        return _StaticTapJob(response)


class _HttpAsyncTapClient:
    """Small UWS/TAP async transport that keeps QUERY_STATUS in memory."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float | None = 120.0,
        max_wait_sec: float = 1800.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.async_url = f"{self.base_url}/async"
        self.timeout = None if timeout_sec is None else float(timeout_sec)
        self.max_wait_sec = float(max_wait_sec)

    def launch_job_async(
        self,
        query: str,
        *,
        maxrec: int,
        dump_to_file: bool = False,
        output_format: str = "votable",
    ) -> _StaticTapJob:
        if dump_to_file:
            raise ValueError("Gaia working-catalog TAP results must remain in memory")
        output_format = str(output_format).strip().casefold()
        if output_format not in {"votable", "votable_plain"}:
            raise ValueError("Gaia working-catalog TAP transport requires a VOTable response")
        payload = {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": output_format,
            "QUERY": query,
            "MAXREC": str(int(maxrec)),
            "PHASE": "RUN",
        }
        headers = {
            "Accept": "application/x-votable+xml, text/xml;q=0.9, */*;q=0.1",
            "User-Agent": "Grandgousier/0.7",
        }
        response = requests.post(
            self.async_url,
            data=payload,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=False,
        )
        status_code = getattr(response, "status_code", None)
        response_headers = getattr(response, "headers", {})
        location = response_headers.get("Location") or response_headers.get("location")
        if status_code != 303 or not location:
            if status_code is not None and status_code >= 400:
                raise RuntimeError(f"TAP async submission failed (HTTP status {status_code})")
            raise RuntimeError("TAP async submission did not return a UWS job location")

        job_url = urljoin(f"{self.async_url}/", str(location))
        phase_url = f"{job_url.rstrip('/')}/phase"
        deadline = time.monotonic() + self.max_wait_sec
        while True:
            phase_response = requests.get(
                phase_url,
                headers=headers,
                timeout=self.timeout,
            )
            phase_status = getattr(phase_response, "status_code", None)
            if phase_status != 200:
                if phase_status is not None and phase_status >= 400:
                    raise RuntimeError(f"TAP async phase request failed (HTTP status {phase_status})")
                raise RuntimeError("TAP async phase response was invalid")
            phase = _tap_response_text(phase_response).strip().upper()
            if phase == "COMPLETED":
                break
            if phase in {"ERROR", "ABORTED", "UNKNOWN"}:
                raise RuntimeError("TAP async job did not complete successfully")
            if phase not in {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED"}:
                raise RuntimeError("TAP async job returned an unsupported phase")
            if time.monotonic() >= deadline:
                raise TimeoutError("TAP async job exceeded its bounded wait time")
            time.sleep(0.25)

        result_response = requests.get(
            f"{job_url.rstrip('/')}/results/result",
            headers=headers,
            timeout=self.timeout,
        )
        result_status = getattr(result_response, "status_code", None)
        if result_status != 200:
            if result_status is not None and result_status >= 400:
                raise RuntimeError(f"TAP async result request failed (HTTP status {result_status})")
            raise RuntimeError("TAP async result response was invalid")
        body = getattr(result_response, "content", b"") or b""
        if isinstance(body, str):
            body = body.encode(getattr(result_response, "encoding", None) or "utf-8")
        result_headers = getattr(result_response, "headers", {})
        parsed = _parse_tap_response(
            body,
            content_type=result_headers.get("Content-Type") or "",
            encoding=getattr(result_response, "encoding", None) or "utf-8-sig",
        )
        if parsed.format == "csv" and len(parsed.rows) >= int(maxrec):
            parsed = _TapResponse(
                rows=parsed.rows,
                format=parsed.format,
                complete=False,
                query_status="MAXREC_REACHED",
            )
        return _StaticTapJob(parsed)


def _tap_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    body = getattr(response, "content", b"") or b""
    if isinstance(body, str):
        return body
    try:
        return body.decode(getattr(response, "encoding", None) or "utf-8", errors="strict")
    except (LookupError, UnicodeDecodeError):
        raise RuntimeError("TAP async phase response was not valid text") from None


_GAIA_TAP_COLUMNS = ("source_id", "ra", "dec", "phot_g_mean_mag")


def _tap_sync_response(
    sync_url: str,
    *,
    query: str,
    max_rows: int | None,
    timeout_sec: float | None,
) -> _TapResponse:
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
    try:
        response.raise_for_status()
    except requests.HTTPError:
        status = getattr(response, "status_code", None)
        raise RuntimeError(f"TAP HTTP request failed (status {status})") from None

    body = response.content or b""
    if isinstance(body, str):
        body = body.encode(response.encoding or "utf-8")
    parsed = _parse_tap_response(
        body,
        content_type=response.headers.get("Content-Type") or "",
        encoding=response.encoding or "utf-8-sig",
    )
    if parsed.format == "csv" and max_rows is not None and len(parsed.rows) >= int(max_rows):
        # CSV does not carry TAP QUERY_STATUS.  If the transport cap is reached,
        # completeness cannot be proven, so the caller must try another provider.
        return _TapResponse(
            rows=parsed.rows,
            format=parsed.format,
            complete=False,
            query_status="MAXREC_REACHED",
        )
    return parsed


def _tap_sync_csv_query(
    sync_url: str,
    *,
    query: str,
    max_rows: int | None,
    timeout_sec: float | None,
) -> list[_TapCsvRow]:
    """Legacy row-list adapter; incomplete TAP responses raise instead of leaking rows."""

    response = _tap_sync_response(
        sync_url,
        query=query,
        max_rows=max_rows,
        timeout_sec=timeout_sec,
    )
    if not response.complete:
        status = response.query_status or "unknown status"
        raise _TapIncompleteResponseError(
            f"TAP {response.format} response is incomplete ({status})",
            query_status=response.query_status,
        )
    return response.rows


def _parse_tap_response(body: bytes, *, content_type: str, encoding: str) -> _TapResponse:
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    prefix = body.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if not prefix:
        raise RuntimeError("TAP response is empty")
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise RuntimeError("TAP endpoint returned HTML instead of catalog data")

    try:
        text = body.decode(encoding or "utf-8-sig", errors="strict").lstrip("\ufeff")
    except (LookupError, UnicodeDecodeError):
        text = None

    # A valid required CSV header is stronger evidence than a mislabeled MIME type.
    if text is not None and _csv_header_indices(text) is not None:
        return _parse_tap_csv(text)

    if prefix.startswith(b"<") or "xml" in media_type or "votable" in media_type:
        return _parse_tap_votable(body)
    if "csv" in media_type and text is not None:
        raise RuntimeError("TAP CSV response is missing required columns")
    raise RuntimeError("TAP response has an unknown or unsupported format")


def _csv_header_indices(text: str) -> dict[str, int] | None:
    try:
        header = next(csv.reader(io.StringIO(text), strict=True), None)
    except csv.Error:
        return None
    if not header:
        return None
    names = [(name or "").strip().casefold() for name in header]
    if len(names) != len(set(names)):
        return None
    if not set(_GAIA_TAP_COLUMNS).issubset(names):
        return None
    return {name: index for index, name in enumerate(names)}


def _parse_tap_csv(text: str) -> _TapResponse:
    reader = csv.reader(io.StringIO(text), strict=True)
    try:
        header = next(reader, None)
    except csv.Error:
        raise RuntimeError("TAP CSV response is malformed") from None
    if not header:
        raise RuntimeError("TAP CSV response has no header")
    names = [(name or "").strip().casefold() for name in header]
    if len(names) != len(set(names)):
        raise RuntimeError("TAP CSV response has duplicate column names")
    if not set(_GAIA_TAP_COLUMNS).issubset(names):
        raise RuntimeError("TAP CSV response is missing required columns")
    indices = {name: index for index, name in enumerate(names)}

    rows: list[_TapCsvRow] = []
    try:
        for values in reader:
            if not values:
                continue
            if len(values) != len(names):
                raise RuntimeError("TAP CSV response has a malformed row")
            rows.append(_tap_row_from_values(values, indices))
    except csv.Error:
        raise RuntimeError("TAP CSV response is malformed") from None
    return _TapResponse(rows=rows, format="csv", complete=True, query_status="OK")


def _tap_row_from_values(values: list[str], indices: dict[str, int]) -> _TapCsvRow:
    row = _TapCsvRow()
    for name in _GAIA_TAP_COLUMNS:
        raw_value = values[indices[name]].strip()
        if not raw_value:
            if name == "source_id":
                raise RuntimeError("TAP response contains an invalid source_id")
            row[name] = None
        elif name == "source_id":
            if re.fullmatch(r"[0-9]+", raw_value) is None:
                raise RuntimeError("TAP response contains an invalid source_id")
            # Keep Gaia's 64-bit key as the exact decimal text returned by TAP.
            row[name] = raw_value
        else:
            try:
                row[name] = float(raw_value)
            except ValueError:
                raise RuntimeError("TAP response contains an invalid numeric value") from None
    return row


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].rsplit(":", maxsplit=1)[-1].upper()


def _parse_tap_votable(body: bytes) -> _TapResponse:
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise RuntimeError("TAP XML response is malformed") from None
    if _xml_local_name(root.tag) != "VOTABLE":
        raise RuntimeError("TAP XML response is not a VOTable")

    status_infos = [
        node
        for node in root.iter()
        if _xml_local_name(node.tag) == "INFO"
        and (node.attrib.get("name") or "").strip().casefold() == "query_status"
    ]
    if not status_infos:
        raise RuntimeError("TAP VOTable is missing QUERY_STATUS")
    statuses = [(node.attrib.get("value") or "").strip().upper() for node in status_infos]
    if any(status == "ERROR" for status in statuses):
        info = next(node for node, status in zip(status_infos, statuses) if status == "ERROR")
        detail = _sanitize_diagnostic_text(" ".join("".join(info.itertext()).split()))
        if not detail:
            detail = "service did not provide an error detail"
        raise _TapQueryStatusError(f"TAP VOTable QUERY_STATUS=ERROR: {detail}")
    overflow_status = next(
        (
            status
            for status in statuses
            if status in {"OVERFLOW", "MAXREC", "MAXREC_REACHED", "MAXREC EXCEEDED"}
        ),
        None,
    )
    if overflow_status is not None:
        return _TapResponse(rows=[], format="votable", complete=False, query_status=overflow_status)
    if any(status != "OK" for status in statuses):
        raise RuntimeError("TAP VOTable has an unsupported QUERY_STATUS")

    return _TapResponse(
        rows=_parse_votable_rows(root, body),
        format="votable",
        complete=True,
        query_status="OK",
    )


def _parse_votable_rows(root: ET.Element, body: bytes) -> list[_TapCsvRow]:
    tables = [
        node
        for node in root.iter()
        if _xml_local_name(node.tag) == "TABLE"
        and any(_xml_local_name(child.tag) == "DATA" for child in node)
    ]
    if len(tables) != 1:
        raise RuntimeError("TAP VOTable must contain exactly one result table")
    table = tables[0]
    fields = [child for child in table if _xml_local_name(child.tag) == "FIELD"]
    names = [
        (field.attrib.get("name") or field.attrib.get("ID") or "").strip().casefold()
        for field in fields
    ]
    if len(names) != len(set(names)) or not set(_GAIA_TAP_COLUMNS).issubset(names):
        raise RuntimeError("TAP VOTable is missing required or has duplicate columns")
    indices = {name: index for index, name in enumerate(names)}
    data = next(child for child in table if _xml_local_name(child.tag) == "DATA")
    tabledata = next(
        (child for child in data if _xml_local_name(child.tag) == "TABLEDATA"),
        None,
    )
    if tabledata is not None:
        rows: list[_TapCsvRow] = []
        for tr in tabledata:
            if _xml_local_name(tr.tag) != "TR":
                continue
            values = [child.text or "" for child in tr if _xml_local_name(child.tag) == "TD"]
            if len(values) != len(fields):
                raise RuntimeError("TAP VOTable contains a malformed row")
            rows.append(_tap_row_from_values(values, indices))
        return rows

    # TABLEDATA is common for Gaia TAP mirrors.  Astropy handles the other legal
    # serializations (BINARY/BINARY2); source_id is converted directly to decimal text.
    return _parse_votable_binary(body)


def _parse_votable_binary(body: bytes) -> list[_TapCsvRow]:
    try:
        root = ET.fromstring(body)
        if any(
            _xml_local_name(node.tag) == "STREAM"
            and any(name.rsplit("}", maxsplit=1)[-1].casefold() == "href" for name in node.attrib)
            for node in root.iter()
        ):
            raise ValueError("external VOTable streams are unsupported")
        from astropy.io.votable import parse as parse_votable

        votable = parse_votable(io.BytesIO(body), verify="exception")
        tables = list(votable.iter_tables())
        if len(tables) != 1:
            raise ValueError("expected one result table")
        table = tables[0].to_table(use_names_over_ids=True)
        names = [str(name).strip().casefold() for name in table.colnames]
        if len(names) != len(set(names)) or not set(_GAIA_TAP_COLUMNS).issubset(names):
            raise ValueError("required columns are missing or duplicated")
        indices = {name: index for index, name in enumerate(names)}
        rows: list[_TapCsvRow] = []
        for record in table:
            values = []
            for name in _GAIA_TAP_COLUMNS:
                value = record[table.colnames[indices[name]]]
                if getattr(value, "mask", False):
                    values.append("")
                else:
                    values.append(str(value))
            rows.append(_tap_row_from_values(values, {name: i for i, name in enumerate(_GAIA_TAP_COLUMNS)}))
        return rows
    except Exception:  # noqa: BLE001 - sanitize all parser failures; exception text may echo response data.
        raise RuntimeError("TAP VOTable binary data could not be parsed") from None


def _sanitize_diagnostic_text(value: str) -> str:
    value = re.sub(r"https?://\S+", "[URL omitted]", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(?i)\b(password|passwd|token|api[_ -]?key|authorization|credential)"
        r"(\s*[:=]\s*|\s+)(?:(?:bearer|basic)\s+)?[^\s,;]+",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if "<" in value or ">" in value:
        return "service returned an error"
    return value[:240]


class _GaiaAllProvidersFailed(RuntimeError):
    def __init__(self, failures: dict[str, str]):
        self.failures = dict(failures)
        details = "; ".join(f"{name}={message}" for name, message in self.failures.items())
        super().__init__(f"all Gaia TAP providers failed: {details}")


class GaiaWorkingCatalogFetchError(RuntimeError):
    """Sanitized failure; no partial working catalog is retained or returned."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = dict(diagnostics)
        super().__init__("Gaia working-catalog fetch failed; no partial catalog returned")


async def fetch_gaia_working_catalog(
    cones: Iterable[tuple[float, float, float]],
    *,
    max_radius_deg: float,
    providers: Sequence[_GaiaTapProvider] | None = None,
    mag_limit: float | None = None,
    maxrec: int = 200_000,
    max_subdivision_depth: int = 12,
    max_physical_parts: int = 4096,
    gaia_timeout_sec: float = 120.0,
) -> tuple[GaiaWorkingCatalog, dict[str, Any]]:
    """Fetch a complete in-memory Gaia catalog for selected spherical footprints.

    ``cones`` contains already-selected ``(ra_deg, dec_deg, radius_deg)`` footprints;
    ``max_radius_deg`` is a required finite positive bound for each cone and for a
    conservative spherical cap enclosing the complete selected field;
    no frame selection, database session, persistence, or filesystem output is used.
    One logical field query is issued first through async TAP.  An explicit TAP
    OVERFLOW/MAXREC response triggers deterministic, half-open RA/Dec tiling of the
    whole requested cone union.  Parts are queried sequentially and never overlap.

    The default MAXREC is intentionally high (200,000) and remains configurable.
    Diagnostics count one logical field query separately from physical spatial
    parts; provider retries are counted in ``provider_attempts``. All diagnostics
    contain only JSON-safe scalars/lists/dicts.
    """

    if isinstance(max_radius_deg, (bool, np.bool_)):
        raise TypeError("max_radius_deg must be finite and positive")
    try:
        max_radius_deg = float(max_radius_deg)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_radius_deg must be finite and positive") from None
    if not math.isfinite(max_radius_deg) or max_radius_deg <= 0.0:
        raise ValueError("max_radius_deg must be finite and positive")

    try:
        raw_cones = list(cones)
    except TypeError:
        raise ValueError("cones must be an iterable of (ra_deg, dec_deg, radius_deg)") from None
    selected_cones = _normalise_working_cones(raw_cones)
    if not selected_cones:
        raise ValueError("at least one selected Gaia cone is required")
    if any(radius > max_radius_deg for _ra, _dec, radius in selected_cones):
        raise ValueError("an individual Gaia cone exceeds max_radius_deg")
    try:
        center_ra, center_dec, aggregate_radius = _combine_cones(selected_cones)
        center_is_finite = math.isfinite(center_ra) and math.isfinite(center_dec)
        aggregate_is_finite = math.isfinite(aggregate_radius)
        if len(selected_cones) > 1:
            centers = _unit_sky_vectors(
                np.asarray([ra for ra, _dec, _radius in selected_cones], dtype=np.float64),
                np.asarray([dec for _ra, dec, _radius in selected_cones], dtype=np.float64),
            )
            centroid_norm = float(np.linalg.norm(centers.mean(axis=0)))
            centroid_is_determinate = (
                math.isfinite(centroid_norm)
                and centroid_norm > 32.0 * np.finfo(np.float64).eps
            )
        else:
            centroid_is_determinate = True
    except Exception:  # noqa: BLE001 - unsafe footprint geometry must fail closed.
        raise ValueError("aggregate Gaia field footprint geometry is indeterminate") from None
    if not center_is_finite or not aggregate_is_finite or not centroid_is_determinate:
        raise ValueError("aggregate Gaia field footprint geometry is indeterminate")
    if aggregate_radius > max_radius_deg:
        raise ValueError("aggregate Gaia field footprint exceeds max_radius_deg")
    if isinstance(maxrec, bool) or not isinstance(maxrec, (int, np.integer)) or maxrec <= 0:
        raise ValueError("maxrec must be a positive integer")
    if (
        isinstance(max_subdivision_depth, bool)
        or not isinstance(max_subdivision_depth, (int, np.integer))
        or max_subdivision_depth < 1
        or max_subdivision_depth > 24
    ):
        raise ValueError("max_subdivision_depth must be an integer from 1 through 24")
    if (
        isinstance(max_physical_parts, bool)
        or not isinstance(max_physical_parts, (int, np.integer))
        or max_physical_parts <= 0
    ):
        raise ValueError("max_physical_parts must be a positive integer")
    if mag_limit is not None:
        try:
            mag_limit = float(mag_limit)
        except (TypeError, ValueError):
            raise ValueError("mag_limit must be finite when provided") from None
        if not math.isfinite(mag_limit):
            raise ValueError("mag_limit must be finite when provided")
    if isinstance(gaia_timeout_sec, bool):
        raise TypeError("gaia_timeout_sec must be finite and positive")
    try:
        gaia_timeout_sec = float(gaia_timeout_sec)
    except (TypeError, ValueError):
        raise ValueError("gaia_timeout_sec must be finite and positive") from None
    if not math.isfinite(gaia_timeout_sec) or gaia_timeout_sec <= 0:
        raise ValueError("gaia_timeout_sec must be finite and positive")

    diagnostics: dict[str, Any] = {
        "logical_queries": 1,
        "physical_parts": 0,
        "provider_attempts": 0,
        "overflow_splits": 0,
        "rows_received": 0,
        "unique_sources": 0,
        "maxrec": int(maxrec),
        "raw_cones": len(raw_cones),
        "unique_footprints": len(selected_cones),
        "providers_used": [],
        "provider_failures": [],
        "provider_failures_omitted": 0,
        "overflow_events": [],
        "partial_rows_discarded": False,
    }
    rows_by_id: dict[str, tuple[float, float, float]] = {}
    catalog: GaiaWorkingCatalog | None = None
    succeeded = False
    try:
        ordered_providers = list(providers) if providers is not None else (
            _build_gaia_working_catalog_providers(gaia_timeout_sec)
        )
        if not ordered_providers:
            raise RuntimeError("no async Gaia TAP providers configured")

        def initial_query(provider: _GaiaTapProvider) -> str:
            return _build_gaia_working_catalog_adql(
                table_name=provider.table_name,
                cones=selected_cones,
                mag_limit=mag_limit,
            )

        initial_rows = await _query_gaia_working_catalog_part(
            ordered_providers,
            query_for_provider=initial_query,
            maxrec=int(maxrec),
            diagnostics=diagnostics,
            max_physical_parts=int(max_physical_parts),
        )
        if initial_rows is None:
            diagnostics["overflow_splits"] += 1
            for tile in _subdivide_sky_tile((0.0, 360.0, -90.0, 90.0)):
                if _sky_tile_intersects_cones(tile, selected_cones):
                    await _fetch_gaia_working_catalog_tile(
                        tile,
                        depth=1,
                        cones=selected_cones,
                        providers=ordered_providers,
                        mag_limit=mag_limit,
                        maxrec=int(maxrec),
                        max_subdivision_depth=int(max_subdivision_depth),
                        max_physical_parts=int(max_physical_parts),
                        diagnostics=diagnostics,
                        rows_by_id=rows_by_id,
                    )
        else:
            diagnostics["rows_received"] += len(initial_rows)
            _merge_working_catalog_rows(rows_by_id, initial_rows)

        catalog = GaiaWorkingCatalog._from_unique_rows(rows_by_id)
        diagnostics["unique_sources"] = len(catalog)
        succeeded = True
        return catalog, diagnostics
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - discard partial rows and expose only sanitized diagnostics.
        rows_by_id.clear()
        if catalog is not None:
            catalog.clear()
        diagnostics["partial_rows_discarded"] = True
        diagnostics["failure_type"] = type(exc).__name__
        raise GaiaWorkingCatalogFetchError(diagnostics) from None
    finally:
        if not succeeded:
            rows_by_id.clear()
            if catalog is not None:
                catalog.clear()


def _build_gaia_working_catalog_providers(timeout_sec: float) -> list[_GaiaTapProvider]:
    """Return an async-TAP provider without touching files or a database."""

    endpoints = (
        ("gaia_aip_async", "https://gaia.aip.de/tap"),
        ("gaia_ari_async", "https://gaia.ari.uni-heidelberg.de/tap"),
        ("gaia_esac_async", "https://gea.esac.esa.int/tap-server/tap"),
    )
    return [
        _GaiaTapProvider(
            name=name,
            client=_HttpAsyncTapClient(url, timeout_sec=timeout_sec),
        )
        for name, url in endpoints
    ]


def _normalise_working_cones(
    cones: Iterable[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    try:
        requested = list(cones)
    except TypeError:
        raise ValueError("cones must be an iterable of (ra_deg, dec_deg, radius_deg)") from None
    normalized: set[tuple[float, float, float]] = set()
    for cone in requested:
        try:
            values = tuple(cone)
        except TypeError:
            raise ValueError("each Gaia cone must contain RA, Dec, and radius") from None
        if len(values) != 3 or any(isinstance(value, (bool, np.bool_)) for value in values):
            raise ValueError("each Gaia cone must contain numeric RA, Dec, and radius")
        try:
            ra, dec, radius = (float(value) for value in values)
        except (TypeError, ValueError):
            raise ValueError("each Gaia cone must contain numeric RA, Dec, and radius") from None
        if not all(math.isfinite(value) for value in (ra, dec, radius)):
            raise ValueError("Gaia cone coordinates and radius must be finite")
        if dec < -90.0 or dec > 90.0 or radius <= 0.0 or radius > 180.0:
            raise ValueError("Gaia cone Dec must be within [-90, 90] and radius within (0, 180]")
        dec = 0.0 if dec == 0.0 else dec
        normalized.add((ra % 360.0, dec, radius))
    return sorted(normalized)


def _normalise_working_catalog_row(row: Any) -> tuple[str, float, float, float]:
    source_id_value = _working_row_value(row, "source_id")
    if isinstance(source_id_value, bytes):
        try:
            source_id_value = source_id_value.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("TAP response contains an invalid Gaia source_id") from None
    if isinstance(source_id_value, (int, np.integer)) and not isinstance(
        source_id_value, (bool, np.bool_)
    ):
        source_id = str(int(source_id_value))
    elif isinstance(source_id_value, str):
        source_id = source_id_value.strip()
    else:
        raise TypeError("TAP response contains an invalid Gaia source_id")
    if re.fullmatch(r"[0-9]+", source_id) is None:
        raise ValueError("TAP response contains an invalid Gaia source_id")

    ra = _working_numeric_value(_working_row_value(row, "ra"), required=True)
    dec = _working_numeric_value(_working_row_value(row, "dec"), required=True)
    g_mag = _working_numeric_value(_working_row_value(row, "phot_g_mean_mag"), required=False)
    if dec is None or dec < -90.0 or dec > 90.0:
        raise ValueError("TAP response contains an invalid Gaia position")
    return source_id, float(ra) % 360.0, dec, float(g_mag) if g_mag is not None else math.nan


def _working_row_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _working_numeric_value(value: Any, *, required: bool) -> float | None:
    if value is None or np.ma.is_masked(value):
        if required:
            raise ValueError("TAP response contains an invalid Gaia position")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        if required:
            raise ValueError("TAP response contains an invalid Gaia position") from None
        return None
    if not math.isfinite(number):
        if required:
            raise ValueError("TAP response contains an invalid Gaia position")
        return None
    return 0.0 if number == 0.0 else number


def _working_row_order(row: tuple[float, float, float]) -> tuple[float, float, bool, float]:
    ra, dec, g_mag = row
    has_no_g_mag = not math.isfinite(g_mag)
    return ra, dec, has_no_g_mag, 0.0 if has_no_g_mag else g_mag


def _merge_working_catalog_rows(
    rows_by_id: dict[str, tuple[float, float, float]], rows: Iterable[Any]
) -> None:
    for row in rows:
        source_id, ra, dec, g_mag = _normalise_working_catalog_row(row)
        candidate = (ra, dec, g_mag)
        previous = rows_by_id.get(source_id)
        if previous is None or _working_row_order(candidate) < _working_row_order(previous):
            rows_by_id[source_id] = candidate


def _working_sky_sep_arcsec(
    ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float
) -> float:
    """Return a pair separation using the existing detection-QC haversine formula."""

    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    delta_ra = (ra1 - ra2 + math.pi) % (2.0 * math.pi) - math.pi
    delta_dec = dec1 - dec2
    haversine = math.sin(delta_dec / 2.0) ** 2 + math.cos(dec1) * math.cos(dec2) * math.sin(
        delta_ra / 2.0
    ) ** 2
    haversine = min(1.0, max(0.0, haversine))
    return math.degrees(2.0 * math.asin(math.sqrt(haversine))) * 3600.0


def _unit_sky_vectors(ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    ra_rad = np.deg2rad(np.remainder(ra_deg, 360.0))
    dec_rad = np.deg2rad(dec_deg)
    cos_dec = np.cos(dec_rad)
    return np.column_stack((cos_dec * np.cos(ra_rad), cos_dec * np.sin(ra_rad), np.sin(dec_rad)))


def _build_gaia_working_catalog_adql(
    *,
    table_name: str,
    cones: Sequence[tuple[float, float, float]],
    mag_limit: float | None,
    tile: tuple[float, float, float, float] | None = None,
) -> str:
    cone_predicates = [
        "CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {center_ra:.17g}, {center_dec:.17g}, {radius:.17g})) = 1"
        for center_ra, center_dec, radius in cones
    ]
    predicates = ["(" + " OR ".join(cone_predicates) + ")"]
    if tile is not None:
        ra_low, ra_high, dec_low, dec_high = tile
        ra_upper = "<=" if ra_high == 360.0 else "<"
        dec_upper = "<=" if dec_high == 90.0 else "<"
        predicates.extend(
            (
                f"ra >= {ra_low:.17g}",
                f"ra {ra_upper} {ra_high:.17g}",
                f"dec >= {dec_low:.17g}",
                f"dec {dec_upper} {dec_high:.17g}",
            )
        )
    if mag_limit is not None:
        predicates.append(f"phot_g_mean_mag <= {mag_limit:.17g}")
    return """
        SELECT {columns}
        FROM {table_name}
        WHERE {where_clause}
    """.format(
        columns=", ".join(GAIA_WORKING_CATALOG_COLUMNS),
        table_name=table_name,
        where_clause=" AND ".join(predicates),
    )


def _working_cone_bounds(
    cone: tuple[float, float, float],
) -> tuple[tuple[tuple[float, float], ...], float, float]:
    ra, dec, radius = cone
    dec_low = max(-90.0, dec - radius)
    dec_high = min(90.0, dec + radius)
    if radius >= 90.0 - abs(dec):
        return ((0.0, 360.0),), dec_low, dec_high
    radius_rad = math.radians(radius)
    dec_rad = math.radians(dec)
    ratio = min(1.0, math.sin(radius_rad) / math.cos(dec_rad))
    ra_half_width = math.degrees(math.asin(ratio))
    ra_low = ra - ra_half_width
    ra_high = ra + ra_half_width
    if ra_low < 0.0:
        ra_intervals = ((0.0, ra_high), (ra_low + 360.0, 360.0))
    elif ra_high >= 360.0:
        ra_intervals = ((0.0, ra_high - 360.0), (ra_low, 360.0))
    else:
        ra_intervals = ((ra_low, ra_high),)
    return ra_intervals, dec_low, dec_high


def _sky_tile_intersects_cones(
    tile: tuple[float, float, float, float], cones: Sequence[tuple[float, float, float]]
) -> bool:
    ra_low, ra_high, dec_low, dec_high = tile
    for cone in cones:
        ra_intervals, cone_dec_low, cone_dec_high = _working_cone_bounds(cone)
        if dec_high < cone_dec_low or dec_low > cone_dec_high:
            continue
        if any(ra_high >= low and ra_low <= high for low, high in ra_intervals):
            return True
    return False


def _subdivide_sky_tile(
    tile: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], ...]:
    ra_low, ra_high, dec_low, dec_high = tile
    ra_mid = (ra_low + ra_high) / 2.0
    dec_mid = (dec_low + dec_high) / 2.0
    if ra_mid in (ra_low, ra_high) or dec_mid in (dec_low, dec_high):
        return ()
    return (
        (ra_low, ra_mid, dec_low, dec_mid),
        (ra_low, ra_mid, dec_mid, dec_high),
        (ra_mid, ra_high, dec_low, dec_mid),
        (ra_mid, ra_high, dec_mid, dec_high),
    )


def _async_tap_kwargs(method: Any, *, maxrec: int) -> dict[str, Any]:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return {"maxrec": maxrec, "dump_to_file": False, "output_format": "votable"}
    parameters = signature.parameters
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    kwargs: dict[str, Any] = {}
    if "maxrec" in parameters or accepts_kwargs:
        kwargs["maxrec"] = maxrec
    else:
        raise RuntimeError("async TAP provider does not accept the configured MAXREC")
    if "dump_to_file" in parameters or accepts_kwargs:
        kwargs["dump_to_file"] = False
    if "output_format" in parameters or accepts_kwargs:
        kwargs["output_format"] = "votable"
    return kwargs


async def _call_async_tap(method: Any, query: str, kwargs: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(method):
        result = await method(query, **kwargs)
    else:
        result = await asyncio.to_thread(method, query, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_async_job_results(job: Any) -> Any:
    get_results = getattr(job, "get_results", None)
    if not callable(get_results):
        raise TypeError("async TAP job does not expose get_results")
    if inspect.iscoroutinefunction(get_results):
        result = await get_results()
    else:
        result = await asyncio.to_thread(get_results)
    if inspect.isawaitable(result):
        return await result
    return result


def _tap_result_status(job: Any, results: Any = None) -> str | None:
    objects = (getattr(job, "response", None), job, getattr(results, "meta", None), results)
    for value in objects:
        if isinstance(value, Mapping):
            for key, status in value.items():
                if str(key).strip().casefold() == "query_status":
                    if isinstance(status, Mapping):
                        status = status.get("value")
                    return str(status).strip().upper()
        else:
            status = getattr(value, "query_status", None)
            if status is not None:
                return str(status).strip().upper()
    return None


def _is_tap_overflow_status(status: str | None) -> bool:
    return status in {"OVERFLOW", "MAXREC", "MAXREC_REACHED", "MAXREC EXCEEDED"}


def _tap_exception_status(exc: Exception) -> str | None:
    status = getattr(exc, "query_status", None)
    if status is not None:
        normalized = str(status).strip().upper()
        if normalized in {"OK", "ERROR", "OVERFLOW", "MAXREC", "MAXREC_REACHED", "MAXREC EXCEEDED"}:
            return normalized
    match = re.search(
        r"\bQUERY_STATUS\s*=\s*(OVERFLOW|MAXREC(?:_REACHED|\s+EXCEEDED)?)\b",
        str(exc),
        flags=re.IGNORECASE,
    )
    return match.group(1).upper().replace(" ", "_") if match else None


def _record_provider_failure(diagnostics: dict[str, Any], provider_name: str, exc: Exception) -> None:
    failures = diagnostics["provider_failures"]
    if len(failures) < 100:
        failures.append({"provider": provider_name, "error": _format_provider_failure(exc)})
    else:
        diagnostics["provider_failures_omitted"] += 1


async def _query_gaia_working_catalog_part(
    providers: Sequence[_GaiaTapProvider],
    *,
    query_for_provider: Callable[[_GaiaTapProvider], str],
    maxrec: int,
    diagnostics: dict[str, Any],
    max_physical_parts: int,
) -> list[Any] | None:
    if diagnostics["physical_parts"] >= max_physical_parts:
        raise RuntimeError("maximum physical Gaia TAP part count exceeded")
    diagnostics["physical_parts"] += 1
    part_number = diagnostics["physical_parts"]
    for provider in providers:
        provider_name = _sanitize_diagnostic_text(str(provider.name)) or "unnamed provider"
        client = provider.client
        method = getattr(client, "launch_job_async", None)
        if not callable(method):
            _record_provider_failure(
                diagnostics,
                provider_name,
                RuntimeError("async TAP provider does not expose launch_job_async"),
            )
            continue
        diagnostics["provider_attempts"] += 1
        try:
            query = query_for_provider(provider)
            job = await _call_async_tap(method, query, _async_tap_kwargs(method, maxrec=maxrec))
            status = _tap_result_status(job)
            if _is_tap_overflow_status(status):
                diagnostics["overflow_events"].append(
                    {"part": part_number, "provider": provider_name, "query_status": status}
                )
                _append_once(diagnostics["providers_used"], provider_name)
                return None
            if status == "ERROR":
                raise _TapQueryStatusError("TAP async job reports QUERY_STATUS=ERROR")

            response = getattr(job, "response", None)
            if isinstance(response, _TapResponse) and not response.complete:
                if _is_tap_overflow_status(response.query_status):
                    diagnostics["overflow_events"].append(
                        {
                            "part": part_number,
                            "provider": provider_name,
                            "query_status": response.query_status,
                        }
                    )
                    _append_once(diagnostics["providers_used"], provider_name)
                    return None
                raise _TapIncompleteResponseError(
                    "TAP response is incomplete",
                    query_status=response.query_status,
                )

            results = await _call_async_job_results(job)
            status = _tap_result_status(job, results)
            if _is_tap_overflow_status(status):
                diagnostics["overflow_events"].append(
                    {"part": part_number, "provider": provider_name, "query_status": status}
                )
                _append_once(diagnostics["providers_used"], provider_name)
                return None
            if status == "ERROR":
                raise _TapQueryStatusError("TAP async result reports QUERY_STATUS=ERROR")
            if isinstance(response, _TapResponse) and not response.complete:
                raise _TapIncompleteResponseError(
                    "TAP response is incomplete",
                    query_status=response.query_status,
                )

            rows = list(results)
            if len(rows) >= maxrec and status not in {"OK"}:
                diagnostics["overflow_events"].append(
                    {"part": part_number, "provider": provider_name, "query_status": "MAXREC_REACHED"}
                )
                _append_once(diagnostics["providers_used"], provider_name)
                return None
            _append_once(diagnostics["providers_used"], provider_name)
            return rows
        except _TapIncompleteResponseError as exc:
            status = _tap_exception_status(exc)
            if _is_tap_overflow_status(status):
                diagnostics["overflow_events"].append(
                    {"part": part_number, "provider": provider_name, "query_status": status}
                )
                _append_once(diagnostics["providers_used"], provider_name)
                return None
            _record_provider_failure(diagnostics, provider_name, exc)
        except Exception as exc:  # noqa: BLE001 - providers are isolated and failures are sanitized.
            status = _tap_exception_status(exc)
            if _is_tap_overflow_status(status):
                diagnostics["overflow_events"].append(
                    {"part": part_number, "provider": provider_name, "query_status": status}
                )
                _append_once(diagnostics["providers_used"], provider_name)
                return None
            _record_provider_failure(diagnostics, provider_name, exc)
    raise _GaiaAllProvidersFailed({"all": "async TAP provider request failed"})


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


async def _fetch_gaia_working_catalog_tile(
    tile: tuple[float, float, float, float],
    *,
    depth: int,
    cones: Sequence[tuple[float, float, float]],
    providers: Sequence[_GaiaTapProvider],
    mag_limit: float | None,
    maxrec: int,
    max_subdivision_depth: int,
    max_physical_parts: int,
    diagnostics: dict[str, Any],
    rows_by_id: dict[str, tuple[float, float, float]],
) -> None:
    if not _sky_tile_intersects_cones(tile, cones):
        return
    def tile_query(provider: _GaiaTapProvider) -> str:
        return _build_gaia_working_catalog_adql(
            table_name=provider.table_name,
            cones=cones,
            mag_limit=mag_limit,
            tile=tile,
        )

    rows = await _query_gaia_working_catalog_part(
        providers,
        query_for_provider=tile_query,
        maxrec=maxrec,
        diagnostics=diagnostics,
        max_physical_parts=max_physical_parts,
    )
    if rows is not None:
        diagnostics["rows_received"] += len(rows)
        _merge_working_catalog_rows(rows_by_id, rows)
        return

    if depth >= max_subdivision_depth:
        raise RuntimeError("Gaia TAP overflow remained after maximum spatial subdivision")
    diagnostics["overflow_splits"] += 1
    children = _subdivide_sky_tile(tile)
    intersecting = [child for child in children if _sky_tile_intersects_cones(child, cones)]
    if not intersecting:
        raise RuntimeError("Gaia spatial subdivision produced no coverage for an overflowing part")
    for child in intersecting:
        await _fetch_gaia_working_catalog_tile(
            child,
            depth=depth + 1,
            cones=cones,
            providers=providers,
            mag_limit=mag_limit,
            maxrec=maxrec,
            max_subdivision_depth=max_subdivision_depth,
            max_physical_parts=max_physical_parts,
            diagnostics=diagnostics,
            rows_by_id=rows_by_id,
        )


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

    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

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


def _format_provider_failure(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return f"{type(exc).__name__}: HTTP status {status}"
    message = str(exc)
    safe_runtime_prefixes = (
        "TAP HTTP request failed",
        "TAP response",
        "TAP CSV",
        "TAP VOTable",
        "TAP XML",
        "TAP endpoint",
    )
    has_safe_detail = isinstance(
        exc,
        (
            _TapQueryStatusError,
            _TapIncompleteResponseError,
            ConnectionError,
            TimeoutError,
            requests.ConnectionError,
            requests.Timeout,
        ),
    ) or (isinstance(exc, RuntimeError) and message.startswith(safe_runtime_prefixes))
    detail = (
        _sanitize_diagnostic_text(message)
        if has_safe_detail
        else "provider request or response failed"
    )
    return f"{type(exc).__name__}: {detail}"


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
            rows = job.get_results()
            response = getattr(job, "response", None)
            if not isinstance(response, _TapResponse) and len(rows) >= max_rows:
                raise _TapIncompleteResponseError(
                    "TAP response reached MAXREC without a completeness status"
                )
            return rows, provider.name, failures
        except Exception as exc:
            failures[provider.name] = _format_provider_failure(exc)

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
    # max_rows remains a transport-level MAXREC compatibility setting; it must
    # never silently change the ADQL projection or add a TOP row limit.
    return f"""
        SELECT
            source_id,
            ra,
            dec,
            phot_g_mean_mag
        FROM {table_name}
        WHERE 1 = CONTAINS(
            POINT('ICRS', ra, dec),
            CIRCLE('ICRS', {center_ra_deg:.10f}, {center_dec_deg:.10f}, {radius_deg:.10f})
        )
        {mag_clause}
    """


def _gaia_row_to_catalog_source(row, *, catalog_name: str) -> dict:
    source_id = str(row["source_id"])
    g = _to_float(_row_get(row, "phot_g_mean_mag"))
    ref_mags = {"G": g} if g is not None else {}
    return {
        "catalog_name": catalog_name,
        "source_id": source_id,
        "source_uid": make_catalog_source_uid(catalog_name, source_id),
        "ra_deg": float(row["ra"]),
        "dec_deg": float(row["dec"]),
        "epoch": 2016.0,
        "pmra_masyr": None,
        "pmdec_masyr": None,
        "ref_mags": ref_mags,
        "colors": {},
        "source_flags": {},
        "raw_json": {},
    }


def promote_selected_gaia_sources(
    session: Session,
    working_catalog: GaiaWorkingCatalog,
    source_ids: Iterable[str | int],
    *,
    diagnostics_out: dict[str, int] | None = None,
) -> dict[str, int]:
    """Persist only selected working-catalog rows in the canonical Gaia namespace.

    Existing canonical and per-field legacy links are inspected for this exact
    source-ID selection before any upsert or link is staged.  An unambiguous
    legacy object association is reused without changing the Object or removing
    historical links.  This helper deliberately leaves transaction commit to its caller.
    """

    if not isinstance(working_catalog, GaiaWorkingCatalog):
        raise TypeError("working_catalog must be a GaiaWorkingCatalog")
    catalog_source_ids = working_catalog.source_ids  # Also rejects a closed catalog.
    selected_ids = _normalise_selected_gaia_source_ids(source_ids)
    if diagnostics_out is not None:
        diagnostics_out.update({"promoted_new": 0, "promoted_existing": 0})
    if not selected_ids:
        return {}

    selected_set = set(selected_ids)
    positions: dict[str, int] = {}
    for index, source_id_value in enumerate(catalog_source_ids):
        source_id = str(source_id_value)
        if source_id in selected_set:
            positions[source_id] = index
    missing_ids = selected_set.difference(positions)
    if missing_ids:
        missing = ", ".join(sorted(missing_ids, key=lambda value: (int(value), value)))
        raise ValueError(f"selected Gaia source IDs are absent from the working catalog: {missing}")

    ra_values = working_catalog.ra_deg
    dec_values = working_catalog.dec_deg
    g_values = working_catalog.g_mag
    rows = [
        _gaia_row_to_catalog_source(
            _TapCsvRow(
                source_id=source_id,
                ra=float(ra_values[positions[source_id]]),
                dec=float(dec_values[positions[source_id]]),
                phot_g_mean_mag=float(g_values[positions[source_id]]),
            ),
            catalog_name="gaia_dr3",
        )
        for source_id in selected_ids
    ]

    legacy_catalog = CatalogSource.catalog_name.startswith(
        "auto_catalog_field_", autoescape=True
    )
    existing_stmt = (
        select(
            CatalogSource.catalog_name,
            CatalogSource.source_id,
            CatalogSource.catalog_source_id,
            ObjectCatalogLink.object_id,
        )
        .outerjoin(
            ObjectCatalogLink,
            ObjectCatalogLink.catalog_source_id == CatalogSource.catalog_source_id,
        )
        .where(
            CatalogSource.source_id.in_(selected_ids),
            or_(CatalogSource.catalog_name == "gaia_dr3", legacy_catalog),
        )
    )
    with session.no_autoflush:
        existing_rows = session.execute(existing_stmt).all()

    canonical_source_ids: set[str] = set()
    canonical_objects: dict[str, set[int]] = {}
    legacy_objects: dict[str, set[int]] = {}
    legacy_names: dict[str, set[str]] = {}
    for catalog_name_value, source_id_value, _catalog_source_id, object_id_value in existing_rows:
        source_id = str(source_id_value)
        catalog_name = str(catalog_name_value)
        if catalog_name == "gaia_dr3":
            canonical_source_ids.add(source_id)
        if object_id_value is None:
            continue
        object_id = int(object_id_value)
        if catalog_name == "gaia_dr3":
            canonical_objects.setdefault(source_id, set()).add(object_id)
        elif catalog_name.startswith("auto_catalog_field_"):
            legacy_objects.setdefault(source_id, set()).add(object_id)
            legacy_names.setdefault(source_id, set()).add(catalog_name)

    conflicts = []
    for source_id in selected_ids:
        canonical = canonical_objects.get(source_id, set())
        legacy = legacy_objects.get(source_id, set())
        if len(canonical) > 1 or len(legacy) > 1 or (canonical and legacy and canonical != legacy):
            conflicts.append(source_id)
    if conflicts:
        raise RuntimeError(
            "conflicting canonical/legacy Gaia object links for source IDs: "
            + ", ".join(conflicts)
        )

    if diagnostics_out is not None:
        diagnostics_out.update(
            {
                "promoted_new": len(selected_set.difference(canonical_source_ids)),
                "promoted_existing": len(selected_set.intersection(canonical_source_ids)),
            }
        )

    source_id_map = _upsert_selected_gaia_catalog_sources(session, rows)
    if set(source_id_map) != selected_set:
        raise RuntimeError("canonical Gaia upsert did not return every selected source ID")

    for source_id in selected_ids:
        legacy = legacy_objects.get(source_id, set())
        if legacy and not canonical_objects.get(source_id):
            session.add(
                ObjectCatalogLink(
                    object_id=next(iter(legacy)),
                    catalog_source_id=source_id_map[source_id],
                    association_method="LEGACY_GAIA_SOURCE_ID",
                    separation_arcsec=None,
                    comparison_epoch=None,
                    is_primary=False,
                    evidence={
                        "match": "exact_gaia_source_id",
                        "legacy_catalog_names": sorted(legacy_names[source_id]),
                    },
                )
            )
    return source_id_map


def _normalise_selected_gaia_source_ids(source_ids: Iterable[str | int]) -> list[str]:
    if isinstance(source_ids, (str, int, np.integer)) and not isinstance(
        source_ids, (bool, np.bool_)
    ):
        values = [source_ids]
    elif isinstance(source_ids, (bool, np.bool_)):
        raise TypeError("selected Gaia source IDs must be decimal strings or integers")
    else:
        try:
            values = list(source_ids)
        except TypeError:
            raise TypeError("selected Gaia source IDs must be iterable") from None

    normalized: set[str] = set()
    for value in values:
        if isinstance(value, (bool, np.bool_)):
            raise TypeError("selected Gaia source IDs must be decimal strings or integers")
        if isinstance(value, (int, np.integer)):
            source_id = str(int(value))
        elif isinstance(value, str):
            source_id = value
        else:
            raise TypeError("selected Gaia source IDs must be decimal strings or integers")
        if re.fullmatch(r"[0-9]+", source_id) is None:
            raise ValueError("selected Gaia source IDs must contain decimal digits only")
        normalized.add(source_id)
    return sorted(normalized, key=lambda value: (int(value), value))


def _upsert_selected_gaia_catalog_sources(
    session: Session, rows: list[dict]
) -> dict[str, int]:
    """Insert selected Gaia rows and minimally refresh known DR3 fields.

    Conflicts update only G photometry and epoch-2016 positions. In particular,
    proper motion, other photometry/colors, source metadata, and object links are
    not replaced by the deliberately compact working-catalog payload.
    """

    table = CatalogSource.__table__
    source_id_map: dict[str, int] = {}
    for start in range(0, len(rows), 1000):
        chunk = rows[start : start + 1000]
        stmt = insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_catalog_sources_catalog_source",
            set_={
                "ra_deg": case(
                    (table.c.epoch == 2016.0, stmt.excluded.ra_deg),
                    else_=table.c.ra_deg,
                ),
                "dec_deg": case(
                    (table.c.epoch == 2016.0, stmt.excluded.dec_deg),
                    else_=table.c.dec_deg,
                ),
                "ref_mags": func.coalesce(
                    table.c.ref_mags, literal({}, type_=JSONB)
                ).op("||")(stmt.excluded.ref_mags),
            },
        ).returning(table.c.source_id, table.c.catalog_source_id)
        for returned_row in session.execute(stmt).all():
            source_id_map[str(returned_row[0])] = int(returned_row[1])
    return source_id_map


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
