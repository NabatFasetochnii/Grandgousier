from __future__ import annotations

import csv
import ctypes
import io
import json
import math
import os
import shutil
import stat
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import _chain_from_base, _latest_iteration
from grandgousier.db.models import CalibrationVersion
from grandgousier.interseries.planning import _load_series_candidates
from grandgousier.lightcurves.product import (
    _science_row_from_db,
    light_curve_reject_flags,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view
from grandgousier.visualization.lightcurve_plot import (
    export_global_lightcurve_plot,
    format_lightcurve_identity_subtitle,
)

M5_ALGORITHM = "global_object_lightcurve_v2"
M5_LINK_STATUS = "M4_EXACT_SOURCE_MATCH"
M5_DEFAULT_PLOT_MAX_POINTS = 5000
M5_STATUS_READY = "READY"
M5_STATUS_NO_ACCEPTED = "NO_ACCEPTED_POINTS_AFTER_QC"

GLOBAL_LIGHT_CURVE_COLUMNS: tuple[str, ...] = (
    "object_id",
    "catalog_name",
    "source_id",
    "frame_id",
    "science_measurement_id",
    "measurement_id",
    "series_id",
    "field_id",
    "field_name",
    "date_obs",
    "mjd",
    "jd",
    "filter_name",
    "instrument_unit_id",
    "instrument",
    "site",
    "tube",
    "camera",
    "science_calibration_version_id",
    "science_calibration_name",
    "interseries_generation_id",
    "interseries_generation_uid",
    "interseries_reliability",
    "interseries_graph_dof",
    "interseries_reference_series_id",
    "interseries_offset_mag",
    "mag_science",
    "mag_global",
    "mag_err_science",
    "mag_err_random",
    "mag_err_science_systematic",
    "mag_err_series_zp",
    "mag_err_effective",
    "series_zp_correlated",
    "m5_link_status",
    "x",
    "y",
    "flux",
    "flux_err",
    "mag_inst",
    "aperture_radius_pix",
    "local_background",
    "local_zp_correction",
    "centroid_dx_pix",
    "centroid_dy_pix",
    "quality_flags",
    "is_rejected",
    "reject_reasons",
)

REJECTION_DEBUG_COLUMNS: tuple[str, ...] = (
    "object_id",
    "series_id",
    "frame_id",
    "science_measurement_id",
    "science_calibration_version_id",
    "interseries_generation_id",
    "filter_name",
    "mjd",
    "mag_science",
    "mag_err_random",
    "quality_flags",
    "reject_reasons",
)

PLOT_DATA_COLUMNS: tuple[str, ...] = (
    "filter_name",
    "series_id",
    "plot_reduction",
    "mjd",
    "date_obs",
    "mag_global",
    "mag_err_random",
    "mag_err_series_zp",
    "n_points",
    "mag_min",
    "mag_max",
    "interseries_generation_id",
    "interseries_reliability",
)

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004

# Opt-in policy ceilings, not measured database counts or an RSS guarantee.
_M5_BUDGET_CSV_BYTES = 32 * 1024 * 1024
_M5_BUDGET_SUMMARY_BYTES = 1024 * 1024
_M5_BUDGET_JSON_BYTES = 64 * 1024 * 1024
_M5_BUDGET_ITEM_BYTES = 64 * 1024


@dataclass
class _M5ExportBudget:
    payload_bytes: int = 0
    science_rows: int = 0
    object_rows: int = 0
    weighted_base_rows: int = 0
    frame_terms: int = 0
    object_terms: int = 0


@dataclass(frozen=True)
class M5Generation:
    interseries_generation_id: int
    generation_uid: str
    field_id: int
    instrument_unit_id: int | None
    filter_name: str | None
    catalog_name: str | None
    reference_series_id: int
    reliability_status: str
    graph_dof: int
    series_sources: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["series_sources"] = [
            {"series_id": series_id, "science_calibration_version_id": cv_id}
            for series_id, cv_id in self.series_sources
        ]
        return row


@dataclass(frozen=True)
class SeriesCoverage:
    series_id: int
    filter_name: str | None
    current_science_calibration_version_id: int | None
    selected_science_calibration_version_id: int | None
    object_points_current_science: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GlobalObjectLightCurveResult:
    object_id: int
    field_id: int
    field_name: str | None
    algorithm: str
    csv_path: str
    summary_path: str
    status: str
    rows_source_exact_m4: int
    rows_written: int
    accepted_rows: int
    rejected_rows: int
    generation_ids: list[int]
    generations: list[M5Generation]
    filters: dict[str, int]
    science_read_source: str = "SCIENCE_PHOTOMETRY"
    rejection_debug_path: str | None = None
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    rejection_reasons_by_series: dict[str, dict[str, int]] = field(default_factory=dict)
    plot_paths: dict[str, str] = field(default_factory=dict)
    plot_data_paths: dict[str, str] = field(default_factory=dict)
    plot_reduction: dict[str, str] = field(default_factory=dict)
    plot_input_points: dict[str, int] = field(default_factory=dict)
    plot_output_points: dict[str, int] = field(default_factory=dict)
    plot_time_axis: dict[str, str] = field(default_factory=dict)
    plot_time_breaks: dict[str, int] = field(default_factory=dict)
    plot_style: dict[str, str] = field(default_factory=dict)
    plot_policy: dict[str, Any] = field(default_factory=dict)
    series_coverage: list[SeriesCoverage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection_provenance: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "object_id": self.object_id,
            "field_id": self.field_id,
            "field_name": self.field_name,
            "algorithm": self.algorithm,
            "csv_path": self.csv_path,
            "summary_path": self.summary_path,
            "status": self.status,
            "rows_source_exact_m4": self.rows_source_exact_m4,
            "rows_written": self.rows_written,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "rejection_debug_path": self.rejection_debug_path,
            "rejection_reasons": dict(self.rejection_reasons),
            "rejection_reasons_by_series": dict(self.rejection_reasons_by_series),
            "generation_ids": self.generation_ids,
            "generations": [row.to_dict() for row in self.generations],
            "filters": self.filters,
            "science_read_source": self.science_read_source,
            "plot_paths": self.plot_paths,
            "plot_data_paths": self.plot_data_paths,
            "plot_reduction": self.plot_reduction,
            "plot_input_points": self.plot_input_points,
            "plot_output_points": self.plot_output_points,
            "plot_time_axis": self.plot_time_axis,
            "plot_time_breaks": self.plot_time_breaks,
            "plot_style": self.plot_style,
            "plot_policy": self.plot_policy,
            "series_coverage": [row.to_dict() for row in self.series_coverage],
            "warnings": list(self.warnings),
            "error_semantics": {
                "mag_err_random": "per-measurement random component from Stage 2",
                "mag_err_science_systematic": "Stage-2 systematic component when available",
                "mag_err_series_zp": "M4 series zero-point uncertainty; correlated across all points of the series",
                "mag_err_effective": "convenience sqrt(mag_err_random^2 + mag_err_series_zp^2); not independent point noise",
            },
        }
        if self.selection_provenance is not None:
            result["selection_provenance"] = self.selection_provenance
        return result


@dataclass
class _BinAccumulator:
    filter_name: str
    series_id: int
    bin_index: int
    generation_id: int
    reliability: str
    zp_err: float | None
    n: int = 0
    sum_mjd: float = 0.0
    sum_mag: float = 0.0
    mean_mag: float = 0.0
    m2_mag: float = 0.0
    sum_random_var: float = 0.0
    n_random: int = 0
    mag_min: float | None = None
    mag_max: float | None = None

    def add(self, row: dict[str, Any]) -> None:
        mjd = float(row["mjd"])
        mag = float(row["mag_global"])
        self.n += 1
        self.sum_mjd += mjd
        self.sum_mag += mag
        delta = mag - self.mean_mag
        self.mean_mag += delta / self.n
        self.m2_mag += delta * (mag - self.mean_mag)
        self.mag_min = mag if self.mag_min is None else min(self.mag_min, mag)
        self.mag_max = mag if self.mag_max is None else max(self.mag_max, mag)
        random_err = _finite_or_none(row.get("mag_err_random"))
        if random_err is not None and random_err >= 0:
            self.sum_random_var += random_err * random_err
            self.n_random += 1

    def finalize(self) -> dict[str, Any]:
        sample_std = math.sqrt(self.m2_mag / (self.n - 1)) if self.n > 1 else 0.0
        sem_scatter = sample_std / math.sqrt(self.n) if self.n > 0 else None
        sem_measurement = None
        if self.n_random > 0:
            sem_measurement = math.sqrt(self.sum_random_var) / self.n_random
        random_sem = _max_finite(sem_scatter, sem_measurement)
        return {
            "filter_name": self.filter_name,
            "series_id": self.series_id,
            "plot_reduction": "time_bin_mean_envelope",
            "mjd": self.sum_mjd / self.n,
            "date_obs": None,
            "mag_global": self.sum_mag / self.n,
            "mag_err_random": random_sem,
            "mag_err_series_zp": self.zp_err,
            "n_points": self.n,
            "mag_min": self.mag_min,
            "mag_max": self.mag_max,
            "interseries_generation_id": self.generation_id,
            "interseries_reliability": self.reliability,
        }


def global_light_curve_columns() -> list[str]:
    return list(GLOBAL_LIGHT_CURVE_COLUMNS)


def rejection_debug_columns() -> list[str]:
    return list(REJECTION_DEBUG_COLUMNS)


def plot_data_columns() -> list[str]:
    return list(PLOT_DATA_COLUMNS)


def resolve_m5_generations(
    session: Session,
    *,
    field_id: int,
    generation_ids: Iterable[int] | None = None,
) -> list[M5Generation]:
    requested = sorted({int(value) for value in (generation_ids or [])})
    params: dict[str, Any] = {"field_id": int(field_id)}
    where = [
        "g.field_id = :field_id",
        "g.lifecycle_status = 'COMPLETE_IMMUTABLE'",
        "g.graph_status = 'OK'",
    ]
    if requested:
        where.append("g.interseries_generation_id = ANY(:generation_ids)")
        params["generation_ids"] = requested
    rows = session.execute(
        text(
            f"""
            SELECT
                g.interseries_generation_id,
                g.generation_uid,
                g.field_id,
                g.instrument_unit_id,
                g.filter_name,
                g.catalog_name,
                g.reference_series_id,
                g.n_series,
                g.reliability_status,
                g.graph_dof,
                s.field_id AS membership_field_id,
                s.series_id,
                s.science_calibration_version_id
            FROM interseries_calibration_generations g
            JOIN interseries_calibration_series s
              ON s.interseries_generation_id = g.interseries_generation_id
            WHERE {' AND '.join(where)}
            ORDER BY g.interseries_generation_id, s.series_id
            """
        ),
        params,
    ).mappings().all()
    if requested:
        found = {int(row["interseries_generation_id"]) for row in rows}
        missing = [value for value in requested if value not in found]
        if missing:
            raise ValueError(
                f"M5 generation selection does not resolve inside field_id={field_id}: {missing}"
            )
    if not rows:
        raise ValueError(f"No COMPLETE_IMMUTABLE M4 generations found for field_id={field_id}")

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["interseries_generation_id"])].append(dict(row))
    generations: list[M5Generation] = []
    for generation_id, group in grouped.items():
        first = group[0]
        if (any(row.get("series_id") is None or row.get("science_calibration_version_id") is None
                or (row.get("membership_field_id") is not None
                    and int(row["membership_field_id"]) != int(row["field_id"]))
                for row in group)
                or (first.get("n_series") is not None
                    and len(group) != int(first["n_series"]))):
            raise ValueError(f"M4 generation {generation_id} has orphaned or incomplete membership")
        generations.append(
            M5Generation(
                interseries_generation_id=generation_id,
                generation_uid=str(first["generation_uid"]),
                field_id=int(first["field_id"]),
                instrument_unit_id=_int_or_none(first.get("instrument_unit_id")),
                filter_name=_str_or_none(first.get("filter_name")),
                catalog_name=_str_or_none(first.get("catalog_name")),
                reference_series_id=int(first["reference_series_id"]),
                reliability_status=str(first["reliability_status"]),
                graph_dof=int(first["graph_dof"]),
                series_sources=tuple(
                    (int(row["series_id"]), int(row["science_calibration_version_id"]))
                    for row in group
                ),
            )
        )

    if not requested:
        by_scope: dict[tuple[Any, ...], list[M5Generation]] = defaultdict(list)
        for generation in generations:
            key = (
                generation.instrument_unit_id,
                (generation.filter_name or "").lower(),
                (generation.catalog_name or "").lower(),
            )
            by_scope[key].append(generation)
        ambiguous = [group for group in by_scope.values() if len(group) > 1]
        if ambiguous:
            parts = []
            for group in ambiguous:
                parts.append(
                    "/".join(str(row.interseries_generation_id) for row in group)
                    + f" (filter={group[0].filter_name}, unit={group[0].instrument_unit_id})"
                )
            raise ValueError(
                "Ambiguous M4 generations for automatic M5 selection: "
                + "; ".join(parts)
                + ". Provide explicit repeatable --generation-id values."
            )

    seen_series: dict[int, int] = {}
    for generation in generations:
        for series_id, _cv_id in generation.series_sources:
            other = seen_series.get(series_id)
            if other is not None and other != generation.interseries_generation_id:
                raise ValueError(
                    f"series_id={series_id} is covered by multiple selected M4 generations "
                    f"({other}, {generation.interseries_generation_id}); selection must be unambiguous"
                )
            seen_series[series_id] = generation.interseries_generation_id
    return sorted(generations, key=lambda row: row.interseries_generation_id)


def inspect_m5_series_coverage(
    session: Session,
    *,
    field_id: int,
    object_id: int,
    generations: Iterable[M5Generation],
    science_calibration_version_ids: list[int] | None = None,
) -> list[SeriesCoverage]:
    selected_sources: dict[int, int] = {}
    generations = list(generations)
    for generation in generations:
        for series_id, cv_id in generation.series_sources:
            selected_sources[series_id] = cv_id

    current_rows = _load_series_candidates(
        session,
        field_id=int(field_id),
        instrument_unit_id=None,
        filter_name=None,
        catalog_name=None,
        **({"science_calibration_version_ids": science_calibration_version_ids}
           if science_calibration_version_ids is not None else {}),
    )
    if science_calibration_version_ids is not None:
        if (len(current_rows) != len(selected_sources)
                or {int(row["series_id"]) for row in current_rows} != set(selected_sources)):
            raise ValueError("M5 explicit M4 sources do not cover every field series")
        scopes = {
            series_id: generation
            for generation in generations
            for series_id, _cv_id in generation.series_sources
        }
        for row in current_rows:
            series_id = int(row["series_id"])
            generation = scopes[series_id]
            if (row.get("_selection_status") is not None
                    or _int_or_none(row.get("calibration_version_id")) != selected_sources[series_id]):
                raise ValueError(f"M5 explicit M4 source is not the current SCIENCE head: {series_id}")
            if (generation.field_id != int(field_id)
                    or (generation.instrument_unit_id is not None
                        and _int_or_none(row.get("instrument_unit_id")) != generation.instrument_unit_id)
                    or (generation.filter_name is not None
                        and not _m5_same_scope_text(row.get("filter_name"), generation.filter_name))
                    or (generation.catalog_name is not None
                        and not _m5_same_scope_text(row.get("catalog_name"), generation.catalog_name))):
                raise ValueError(f"M5 explicit M4 source is outside generation scope: {series_id}")
    current_cv_ids = [
        int(row["calibration_version_id"])
        for row in current_rows
        if row.get("calibration_version_id") is not None
    ]
    object_counts: dict[int, int] = {}
    if current_cv_ids:
        count_rows = session.execute(
            text(
                """
                SELECT s.calibration_version_id, COUNT(*)::bigint AS n_points
                FROM science_photometry s
                JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
                WHERE bm.object_id = :object_id
                  AND s.calibration_version_id = ANY(:calibration_ids)
                GROUP BY s.calibration_version_id
                """
            ),
            {"object_id": int(object_id), "calibration_ids": current_cv_ids},
        ).mappings().all()
        object_counts.update({
            int(row["calibration_version_id"]): int(row["n_points"] or 0)
            for row in count_rows
        })

    coverage: list[SeriesCoverage] = []
    for row in current_rows:
        series_id = int(row["series_id"])
        current_cv = _int_or_none(row.get("calibration_version_id"))
        selected_cv = selected_sources.get(series_id)
        n_points = object_counts.get(current_cv or -1, 0)
        if n_points <= 0:
            status = "NO_OBJECT_POINTS_IN_CURRENT_SCIENCE"
        elif selected_cv is None:
            status = "NO_COMPATIBLE_INTERSERIES_GENERATION"
        elif current_cv != selected_cv:
            status = "M4_SOURCE_NOT_CURRENT_SCIENCE"
        else:
            status = "READY_EXACT_M4_SOURCE"
        coverage.append(
            SeriesCoverage(
                series_id=series_id,
                filter_name=_str_or_none(row.get("filter_name")),
                current_science_calibration_version_id=current_cv,
                selected_science_calibration_version_id=selected_cv,
                object_points_current_science=n_points,
                status=status,
            )
        )
    return coverage


def _m5_same_scope_text(actual: Any, required: str) -> bool:
    return actual is not None and str(actual).strip().casefold() == required.strip().casefold()


def _m5_explicit_m4_source_ids(
    session: Session, *, field_id: int, generations: list[M5Generation],
) -> list[int]:
    sources: dict[int, int] = {}
    for generation in generations:
        if generation.field_id != field_id:
            raise ValueError("M5 explicit M4 source belongs to another field")
        for series_id, cv_id in generation.series_sources:
            if series_id in sources or cv_id <= 0 or series_id <= 0:
                raise ValueError("M5 explicit M4 source repeats a series or has an invalid ID")
            sources[series_id] = cv_id
    ids = list(sources.values())
    if not 1 <= len(ids) <= 18 or len(set(ids)) != len(ids):
        raise ValueError("M5 explicit M4 sources require 1..18 distinct series and CV IDs")
    rows = session.execute(text(
        "SELECT calibration_version_id, field_id, series_id, instrument_unit_id, "
        "product_role, lifecycle_status FROM calibration_versions "
        "WHERE calibration_version_id = ANY(:ids)"
    ), {"ids": ids}).mappings().all()
    by_id = {int(row["calibration_version_id"]): row for row in rows}
    if len(rows) != len(ids) or len(by_id) != len(ids):
        raise ValueError("M5 explicit M4 source calibration metadata is missing or duplicated")
    for generation in generations:
        for series_id, cv_id in generation.series_sources:
            cv = by_id[cv_id]
            if (cv["field_id"] is None or int(cv["field_id"]) != field_id
                    or cv["series_id"] is None or int(cv["series_id"]) != series_id
                    or cv["product_role"] != "SCIENCE"
                    or cv["lifecycle_status"] != "COMPLETE_IMMUTABLE"
                    or (generation.instrument_unit_id is not None
                        and (cv["instrument_unit_id"] is None
                             or int(cv["instrument_unit_id"]) != generation.instrument_unit_id))):
                raise ValueError(f"M5 explicit M4 source CV is invalid or outside scope: {cv_id}")
    return ids


def _m5_budget_probe(
    session: Session,
    budget: _M5ExportBudget,
    source_sql: str,
    params: dict[str, Any],
    *,
    label: str,
    maximum: int,
    minimum: int = 0,
    json_columns: tuple[str, ...] = (),
    text_columns: tuple[str, ...] = (),
) -> int:
    """Exact scoped counts and decoded text sizes, before loading any payload rows.

    All SQL fragments here are internal constant column/relation names, never user input.
    pg_column_size is deliberately NOT used: TOAST-compressed JSON can expand on read.
    """
    lengths = [f"COALESCE(octet_length(({col})::text), 0)" for col in json_columns]
    lengths += [f"octet_length(COALESCE({col}, ''))" for col in text_columns]
    largest = f"GREATEST(0, {', '.join(lengths)})" if lengths else "0"
    total = " + ".join(lengths) if lengths else "0"
    row = session.execute(
        text(
            f"SELECT COUNT(*)::bigint AS n, COALESCE(MAX({largest}), 0)::bigint AS largest, "
            f"COALESCE(SUM({total}), 0)::bigint AS payload_bytes {source_sql}"
        ),
        params,
    ).mappings().first()
    if row is None:
        raise ValueError(f"M5 budget: missing {label} preflight result")
    count = int(row["n"])
    if not minimum <= count <= maximum:
        raise ValueError(f"M5 budget: {label} count outside [{minimum}, {maximum}]")
    if int(row["largest"]) > _M5_BUDGET_ITEM_BYTES:
        raise ValueError(f"M5 budget: {label} decoded payload exceeds per-item ceiling")
    budget.payload_bytes += int(row["payload_bytes"])
    if budget.payload_bytes > _M5_BUDGET_JSON_BYTES:
        raise ValueError("M5 budget: cumulative decoded payload exceeds ceiling")
    return count


def _m5_budget_early_scope(
    session: Session, *, field_id: int, generation_ids: Iterable[int] | None,
    budget: _M5ExportBudget,
    use_explicit_m4_source_heads: bool = False,
) -> dict[int, int]:
    """Bound the existing M5 generation and field-wide alias readers before their .all()."""
    _m5_budget_probe(
        session, budget, "FROM fields fld WHERE fld.field_id = :fid", {"fid": field_id},
        label="field", minimum=1, maximum=1, text_columns=("fld.field_name",),
    )
    field_series_count = _m5_budget_probe(
        session, budget, "FROM field_series fs WHERE fs.field_id = :fid", {"fid": field_id},
        label="field series", maximum=18,
    )
    multiplicity: dict[int, int] = {}
    if use_explicit_m4_source_heads:
        # Aggregate to at most 18 series BEFORE _load_series_candidates materializes
        # any alias/recipe JSON. This counts *all* SCIENCE alias rows, not just M4 IDs.
        rows = session.execute(text("""
            SELECT fs.series_id, COUNT(cpa.alias_id)::bigint AS n_alias,
                   COUNT(DISTINCT cv.calibration_version_id)::bigint AS n_distinct_cv,
                   COUNT(*) FILTER (WHERE cv.field_id IS DISTINCT FROM fs.field_id
                       OR (fs.instrument_unit_id IS NOT NULL AND cv.instrument_unit_id IS NOT NULL
                           AND fs.instrument_unit_id <> cv.instrument_unit_id))::bigint AS n_invalid,
                   COALESCE(MAX(GREATEST(
                       COALESCE(octet_length((cv.used_catalogs)::text), 0),
                       COALESCE(octet_length((pr.recipe_json)::text), 0),
                       octet_length(COALESCE(pr.catalog_name, '')))), 0)::bigint AS largest,
                   COALESCE(SUM(COALESCE(octet_length((cv.used_catalogs)::text), 0)
                       + COALESCE(octet_length((pr.recipe_json)::text), 0)
                       + octet_length(COALESCE(pr.catalog_name, ''))), 0)::bigint AS payload_bytes
            FROM field_series fs
            JOIN calibration_versions cv ON cv.series_id = fs.series_id
              AND cv.product_role = 'SCIENCE'
            JOIN calibration_product_aliases cpa
              ON cpa.head_calibration_version_id = cv.calibration_version_id
            LEFT JOIN pipeline_runs pr ON pr.pipeline_run_id = cv.pipeline_run_id
            WHERE fs.field_id = :fid
            GROUP BY fs.series_id ORDER BY fs.series_id
        """), {"fid": field_id}).mappings().all()
        if len(rows) != field_series_count or not 1 <= len(rows) <= 18:
            raise ValueError("M5 budget: explicit M4 aliases do not cover all field series")
        for row in rows:
            series_id = int(row["series_id"])
            n_alias = int(row["n_alias"])
            if (series_id in multiplicity or not 1 <= n_alias <= 2
                    or int(row["n_distinct_cv"]) != n_alias or int(row["n_invalid"]) != 0):
                raise ValueError(f"M5 budget: invalid SCIENCE alias multiplicity/scope: {series_id}")
            if int(row["largest"]) > _M5_BUDGET_ITEM_BYTES:
                raise ValueError("M5 budget: alias decoded payload exceeds per-item ceiling")
            budget.payload_bytes += int(row["payload_bytes"])
            if budget.payload_bytes > _M5_BUDGET_JSON_BYTES:
                raise ValueError("M5 budget: cumulative decoded payload exceeds ceiling")
            multiplicity[series_id] = n_alias
        if sum(multiplicity.values()) > 36:
            raise ValueError("M5 budget: field SCIENCE alias heads exceed opt-in ceiling")
    else:
        _m5_budget_probe(
            session, budget,
            """FROM field_series fs
               JOIN calibration_versions cv ON cv.series_id = fs.series_id
                 AND cv.product_role = 'SCIENCE'
               JOIN calibration_product_aliases cpa
                 ON cpa.head_calibration_version_id = cv.calibration_version_id
               LEFT JOIN pipeline_runs pr ON pr.pipeline_run_id = cv.pipeline_run_id
               WHERE fs.field_id = :fid""",
            {"fid": field_id}, label="field SCIENCE alias heads", maximum=18,
            json_columns=("cv.used_catalogs", "pr.recipe_json"),
            text_columns=("pr.catalog_name",),
        )
    selected = sorted({int(value) for value in (generation_ids or ())})
    if len(selected) > 18:
        raise ValueError("M5 budget: generation selection exceeds ceiling")
    clause = " AND g.interseries_generation_id = ANY(:ids)" if selected else ""
    _m5_budget_probe(
        session, budget,
        """FROM interseries_calibration_generations g
           LEFT JOIN interseries_calibration_series s
             ON s.interseries_generation_id = g.interseries_generation_id
           WHERE g.field_id = :fid AND g.lifecycle_status = 'COMPLETE_IMMUTABLE'
             AND g.graph_status = 'OK'""" + clause,
        {"fid": field_id, **({"ids": selected} if selected else {})},
        label="M4 generation memberships", minimum=1, maximum=18,
    )
    return multiplicity


def _m5_budget_preflight(
    session: Session, *, object_id: int, generations: list[M5Generation],
    budget: _M5ExportBudget,
) -> None:
    """Bound each SCIENCE/BASE load and each repeated normalized-stage term load.

    Must be run in the SAME Session/snapshot as the export. A future live child
    must independently enforce read-only snapshot, identity and timeouts.
    """
    sources = [int(cv) for gen in generations for _, cv in gen.series_sources]
    if not sources or len(sources) > 18 or len(set(sources)) != len(sources):
        raise ValueError("M5 budget: expected 1..18 distinct selected source CVs")
    base_ids: list[int] = []
    for cv_id in sources:
        cv_chain: list[int] = []
        seen: set[int] = set()
        current: int | None = cv_id
        while current is not None:
            if current in seen or len(cv_chain) >= 7:
                raise ValueError("M5 budget: cyclic or oversized calibration chain")
            seen.add(current)
            # Scalar metadata first: never materialize ORM JSON or unbounded text
            # before its SQL-side decoded-size preflight.
            meta = session.execute(text(
                "SELECT calibration_version_id, source_calibration_version_id, product_kind, "
                "product_role FROM calibration_versions WHERE calibration_version_id = :cv"
            ), {"cv": current}).mappings().first()
            if meta is None:
                raise ValueError("M5 budget: missing calibration source metadata")
            _m5_budget_probe(
                session, budget,
                "FROM calibration_versions cv WHERE cv.calibration_version_id = :cv",
                {"cv": current}, label="calibration metadata", minimum=1, maximum=1,
                json_columns=("cv.instrument_profile", "cv.used_catalogs", "cv.parameters"),
                text_columns=("cv.notes",),
            )
            cv_chain.append(current)
            kind = str(meta["product_kind"] or "").upper()
            if current == cv_id and str(meta["product_role"] or "").upper() != "SCIENCE":
                raise ValueError("M5 budget: selected calibration is not SCIENCE")
            if kind == "BASE_PHOTOMETRY":
                break
            current = (int(meta["source_calibration_version_id"])
                       if meta["source_calibration_version_id"] is not None else None)
        if not cv_chain or kind != "BASE_PHOTOMETRY":
            raise ValueError("M5 budget: calibration chain lacks BASE")
        if len(cv_chain) - 1 > 6:
            raise ValueError("M5 budget: more than six reconstruction stages per CV")
        base_id = cv_chain[-1]
        base_ids.append(base_id)
        budget.science_rows += _m5_budget_probe(
            session, budget,
            "FROM science_photometry s WHERE s.calibration_version_id = :cv",
            {"cv": cv_id}, label="SCIENCE per CV", minimum=1, maximum=600_000,
        )
        if budget.science_rows > 6_000_000:
            raise ValueError("M5 budget: selected SCIENCE total exceeds ceiling")
        points = _m5_budget_probe(
            session, budget,
            """FROM science_photometry s
               JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
               WHERE s.calibration_version_id = :cv AND bm.object_id = :obj""",
            {"cv": cv_id, "obj": object_id}, label="object SCIENCE points per CV",
            minimum=1, maximum=3000,
        )
        budget.object_rows += points
        if budget.object_rows > 3000:
            raise ValueError("M5 budget: total object source points exceed ceiling")
        mismatch = _m5_budget_probe(
            session, budget,
            """FROM science_photometry s
               JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
               WHERE s.calibration_version_id = :cv AND bm.object_id = :obj
                 AND bm.calibration_version_id <> :base""",
            {"cv": cv_id, "obj": object_id, "base": base_id},
            label="foreign BASE in object SCIENCE", maximum=0,
        )
        if mismatch:
            raise ValueError("M5 budget: SCIENCE refers to another BASE")
        base_count = _m5_budget_probe(
            session, budget,
            """FROM measurements bm WHERE bm.calibration_version_id = :base
               AND bm.object_id = :obj""",
            {"base": base_id, "obj": object_id}, label="object BASE rows per CV",
            minimum=1, maximum=1000, json_columns=("bm.flags",),
        )
        budget.weighted_base_rows += base_count
        if budget.weighted_base_rows > 6000:
            raise ValueError("M5 budget: weighted BASE rows exceed ceiling")
        # Guard a missing durable row before the reconstruction reader may
        # build a full per-object state and then discover the missing scalar.
        _m5_budget_probe(
            session, budget,
            """FROM measurements bm LEFT JOIN science_photometry s
                 ON s.base_measurement_id = bm.measurement_id AND s.calibration_version_id = :cv
               WHERE bm.calibration_version_id = :base AND bm.object_id = :obj
                 AND s.base_measurement_id IS NULL""",
            {"cv": cv_id, "base": base_id, "obj": object_id},
            label="missing durable SCIENCE rows", maximum=0,
        )
        # Reuse the exact reconstruction lineage and latest-iteration rules,
        # only AFTER all ORM metadata in this chain was size-checked above.
        chain = _chain_from_base(session, session.get(CalibrationVersion, cv_id))
        if [int(cv.calibration_version_id) for cv in reversed(chain)] != cv_chain:
            raise ValueError("M5 budget: reconstruction chain changed")
        for stage in chain[1:]:
            stage_id = int(stage.calibration_version_id)
            newest = session.execute(text(
                "SELECT calibration_iteration_id, iteration_kind FROM calibration_iterations "
                "WHERE calibration_version_id = :cv "
                "ORDER BY iteration_no DESC, calibration_iteration_id DESC LIMIT 1"
            ), {"cv": stage_id}).mappings().first()
            if newest is None:
                raise ValueError("M5 budget: missing normalized reconstruction iteration")
            iteration_id = int(newest["calibration_iteration_id"])
            kind = str(stage.product_kind or "").upper()
            iteration_kind = str(newest["iteration_kind"] or "").upper()
            if iteration_kind not in {"PHOTOMETRIC_FRAME_QC", "SPATIAL_ENSEMBLE"} and kind not in {
                "APERTURE_CORRECTION", "APERTURE_ZP", "ENSEMBLE", "COLOR_CALIBRATION",
            }:
                raise ValueError("M5 budget: unsupported reconstruction stage")
            _m5_budget_probe(
                session, budget,
                "FROM calibration_iterations it WHERE it.calibration_iteration_id = :it",
                {"it": iteration_id}, label="iteration metadata", minimum=1, maximum=1,
                json_columns=("it.parameters", "it.metrics"),
                text_columns=("it.convergence_reason",),
            )
            actual = _latest_iteration(session, stage_id)
            if actual is None or int(actual.calibration_iteration_id) != iteration_id:
                raise ValueError("M5 budget: latest iteration changed")
            budget.frame_terms += _m5_budget_probe(
                session, budget,
                "FROM calibration_frame_terms ft WHERE ft.calibration_iteration_id = :it",
                {"it": iteration_id}, label="frame terms per stage", maximum=30_000,
                json_columns=("ft.details",),
            )
            if budget.frame_terms > 30_000:
                raise ValueError("M5 budget: cumulative frame terms exceed ceiling")
            budget.object_terms += _m5_budget_probe(
                session, budget,
                "FROM calibration_iteration_objects ot WHERE ot.calibration_iteration_id = :it",
                {"it": iteration_id}, label="object terms per stage", maximum=100_000,
                json_columns=("ot.details",),
            )
            if budget.object_terms > 100_000:
                raise ValueError("M5 budget: cumulative object terms exceed ceiling")

    # The exact reader loads every series membership for every prepared frame,
    # not just the requested series. Cover that fan-out and its frame/field flags.
    _m5_budget_probe(
        session, budget,
        """FROM frames f JOIN field_series_frames fsf ON fsf.frame_id = f.frame_id
           JOIN field_series fs ON fs.series_id = fsf.series_id
           JOIN fields fld ON fld.field_id = fs.field_id
           WHERE f.frame_id IN (
               SELECT bm.frame_id FROM measurements bm
               WHERE bm.object_id = :obj AND bm.calibration_version_id = ANY(:bases)
           )""",
        {"obj": object_id, "bases": sorted(set(base_ids))},
        label="prepared frame memberships", maximum=30_000,
        json_columns=("f.quality_flags",), text_columns=("fld.field_name",),
    )
    _m5_budget_probe(
        session, budget,
        """FROM object_qc qc WHERE qc.object_id = :obj
           AND qc.calibration_version_id = ANY(:cvs) AND qc.algorithm = 'object_qc_v1'""",
        {"obj": object_id, "cvs": sources}, label="object QC rows", maximum=3000,
        json_columns=("qc.flags",),
    )


def _m5_budget_parent(root: Path) -> None:
    if not root.is_absolute() or ".." in root.parts or not root.name:
        raise ValueError("M5 budget requires an absolute, non-traversing output path")
    parent = root.parent
    for ancestor in (parent, *parent.parents):
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (getattr(info, "st_file_attributes", 0) & 0x400)):
            raise ValueError("M5 budget requires an existing non-reparse output parent")
    if os.path.lexists(root):
        raise FileExistsError(f"M5 output bundle already exists: {root}")
    # The old flat format could leave CSV, JSON or plots in this parent. Scan
    # only a bounded number of entries; do not accept an uninspectable parent.
    with os.scandir(parent) as entries:
        for index, entry in enumerate(entries):
            if index >= 1024:
                raise ValueError("M5 budget: output parent has too many entries")
            name = entry.name
            if (name == root.name + ".csv" or name.startswith(root.name + "_")
                    and name.endswith((".csv", ".json", ".png", ".pdf", ".svg"))):
                raise FileExistsError(f"M5 legacy flat output already exists: {entry.path}")


def _m5_budget_csv_bytes(row: dict[str, Any] | None) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(GLOBAL_LIGHT_CURVE_COLUMNS), extrasaction="ignore",
    )
    if row is None:
        writer.writeheader()
    else:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def export_global_object_light_curve(
    session: Session,
    *,
    object_id: int,
    field_id: int,
    out_root: str | Path,
    generation_ids: Iterable[int] | None = None,
    include_rejected: bool = False,
    include_rejected_debug: bool = False,
    allow_partial: bool = False,
    require_detection_support: bool = True,
    extra_reject_flags: str | None = None,
    plots: bool = True,
    plot_reduction: str = "auto",
    plot_max_points: int = M5_DEFAULT_PLOT_MAX_POINTS,
    plot_bin_minutes: float | None = None,
    plot_format: str = "png",
    plot_dpi: int = 160,
    plot_time_axis: str = "auto",
    preserve_staging_on_failure: bool = False,
    enforce_export_budget: bool = False,
    use_explicit_m4_source_heads: bool = False,
) -> GlobalObjectLightCurveResult:
    # Refuse incompatible opt-in BEFORE even a field/object database read or
    # parent inspection. The default export retains its existing behavior.
    if use_explicit_m4_source_heads and not enforce_export_budget:
        raise ValueError("M5 explicit M4 source heads require enforce_export_budget=True")
    if enforce_export_budget:
        if not preserve_staging_on_failure:
            raise ValueError("M5 budget requires preserve_staging_on_failure=True")
        if plots or include_rejected_debug:
            raise ValueError("M5 budget requires plots=False and include_rejected_debug=False")
        if len(str(extra_reject_flags or "").encode("utf-8")) > 1024:
            raise ValueError("M5 budget: extra reject flags exceed input ceiling")
        generation_ids = tuple(islice(generation_ids or (), 19))
        if len(generation_ids) > 18:
            raise ValueError("M5 budget: generation selection exceeds ceiling")
        if use_explicit_m4_source_heads:
            if allow_partial:
                raise ValueError("M5 explicit M4 source heads refuse allow_partial=True")
            if (not generation_ids or any(int(value) <= 0 for value in generation_ids)
                    or len({int(value) for value in generation_ids}) != len(generation_ids)):
                raise ValueError("M5 explicit M4 source heads require distinct explicit generation_ids")
    object_id = int(object_id)
    field_id = int(field_id)
    plot_reduction = str(plot_reduction).lower()
    if plot_reduction not in {"auto", "bin", "none"}:
        raise ValueError("plot_reduction must be one of: auto, bin, none")
    plot_max_points = int(plot_max_points)
    if plot_max_points < 1:
        raise ValueError("plot_max_points must be >= 1")
    if plot_bin_minutes is not None and float(plot_bin_minutes) <= 0:
        raise ValueError("plot_bin_minutes must be > 0")
    plot_time_axis = str(plot_time_axis).lower()
    if plot_time_axis not in {"auto", "continuous", "compressed"}:
        raise ValueError("plot_time_axis must be one of: auto, continuous, compressed")
    plot_format = str(plot_format).lower().lstrip(".")
    if plot_format not in {"png", "pdf", "svg"}:
        raise ValueError("plot_format must be png, pdf or svg")

    budget = _M5ExportBudget() if enforce_export_budget else None
    alias_multiplicity: dict[int, int] = {}
    if budget is not None:
        _m5_budget_parent(Path(out_root))
        alias_multiplicity = _m5_budget_early_scope(
            session, field_id=field_id, generation_ids=generation_ids, budget=budget,
            use_explicit_m4_source_heads=use_explicit_m4_source_heads,
        )
    field_row = session.execute(
        text("SELECT field_id, field_name FROM fields WHERE field_id = :field_id"),
        {"field_id": field_id},
    ).mappings().first()
    if field_row is None:
        raise ValueError(f"No field found with field_id={field_id}")
    object_row = session.execute(
        text(
            "SELECT object_id, object_uid, ra_deg, dec_deg "
            "FROM objects WHERE object_id = :object_id"
        ),
        {"object_id": object_id},
    ).mappings().first()
    if object_row is None:
        raise ValueError(f"No object found with object_id={object_id}")

    generations = resolve_m5_generations(
        session,
        field_id=field_id,
        generation_ids=generation_ids,
    )
    explicit_cv_ids: list[int] | None = None
    if use_explicit_m4_source_heads:
        if {row.interseries_generation_id for row in generations} != set(generation_ids or ()):
            raise ValueError("M5 explicit M4 generation selection changed")
        explicit_cv_ids = _m5_explicit_m4_source_ids(
            session, field_id=field_id, generations=generations,
        )
    coverage = inspect_m5_series_coverage(
        session,
        field_id=field_id,
        object_id=object_id,
        generations=generations,
        **({"science_calibration_version_ids": explicit_cv_ids}
           if explicit_cv_ids is not None else {}),
    )
    if use_explicit_m4_source_heads and (
        {row.series_id for row in coverage} != set(alias_multiplicity)
        or len(coverage) != len(alias_multiplicity)
    ):
        raise ValueError("M5 explicit M4 coverage differs from field alias inventory")
    blocking = [
        row for row in coverage
        if row.object_points_current_science > 0
        and row.status not in {"READY_EXACT_M4_SOURCE", "NO_OBJECT_POINTS_IN_CURRENT_SCIENCE"}
    ]
    warnings: list[str] = []
    if blocking:
        detail = ", ".join(
            f"series {row.series_id}: {row.status} "
            f"(current_cv={row.current_science_calibration_version_id}, m4_cv={row.selected_science_calibration_version_id}, "
            f"points={row.object_points_current_science})"
            for row in blocking
        )
        if not allow_partial:
            raise ValueError(
                "M5 refuses a partial/stale global light curve: " + detail
                + ". Persist a compatible M4 generation or re-run with --allow-partial to export only exact-linked series."
            )
        warnings.append("Partial M5 export: " + detail)

    if budget is not None:
        _m5_budget_preflight(
            session, object_id=object_id, generations=generations, budget=budget,
        )
    generation_ids_resolved = [row.interseries_generation_id for row in generations]
    stats = _load_plot_source_stats(
        session,
        object_id=object_id,
        field_id=field_id,
        generation_ids=generation_ids_resolved,
        generations=generations,
    )
    plot_plan = _build_plot_plan(
        stats,
        reduction=plot_reduction,
        max_points=plot_max_points,
        bin_minutes=plot_bin_minutes,
    )
    if not stats or sum(int(row.get("n_points") or 0) for row in stats) <= 0:
        raise ValueError(
            f"No exact M4-linked SCIENCE rows found for object_id={object_id}, field_id={field_id}"
        )
    if use_explicit_m4_source_heads and (
        budget is None or sum(int(row["n_points"]) for row in stats) != budget.object_rows
        or sum(row.object_points_current_science for row in coverage) != budget.object_rows
    ):
        raise ValueError("M5 explicit M4 source counts differ before output staging")
    plot_policy = _plot_policy_summary(
        plot_plan,
        reduction=plot_reduction,
        max_points=plot_max_points,
        bin_minutes=plot_bin_minutes,
        time_axis=plot_time_axis,
    )

    root = Path(out_root)
    if budget is not None:
        _m5_budget_parent(root)
    else:
        root.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(root):
        raise FileExistsError(f"M5 output bundle already exists: {root}")

    csv_path = root / f"{root.name}.csv"
    summary_path = root / f"{root.name}_summary.json"
    rejection_debug_path = (
        root / f"{root.name}_rejections.csv" if include_rejected_debug else None
    )
    configured_plot_paths: dict[str, tuple[Path, Path]] = {}
    if plots and plot_reduction != "none":
        for filter_name in sorted(plot_plan):
            safe_filter = _safe_filename_token(filter_name)
            configured_plot_paths[filter_name] = (
                root / f"{root.name}_{safe_filter}_plot.csv",
                root / f"{root.name}_{safe_filter}.{plot_format}",
            )
    expected_final_paths = [csv_path, summary_path]
    if rejection_debug_path is not None:
        expected_final_paths.append(rejection_debug_path)
    for plot_data_path, plot_path in configured_plot_paths.values():
        expected_final_paths.extend((plot_data_path, plot_path))
    for output_path in expected_final_paths:
        legacy_path = root.parent / output_path.name
        if os.path.lexists(legacy_path):
            raise FileExistsError(f"M5 legacy flat output already exists: {legacy_path}")

    reject_flags = _m5_reject_flags(
        require_detection_support=require_detection_support,
        extra_reject_flags=extra_reject_flags,
    )
    result = GlobalObjectLightCurveResult(
        object_id=object_id,
        field_id=field_id,
        field_name=_str_or_none(field_row.get("field_name")),
        algorithm=M5_ALGORITHM,
        csv_path=str(csv_path),
        summary_path=str(summary_path),
        status=M5_STATUS_READY,
        rows_source_exact_m4=0,
        rows_written=0,
        accepted_rows=0,
        rejected_rows=0,
        generation_ids=generation_ids_resolved,
        generations=list(generations),
        filters={},
        science_read_source="SCIENCE_PHOTOMETRY",
        plot_policy=plot_policy,
        series_coverage=coverage,
        warnings=warnings,
        rejection_debug_path=str(rejection_debug_path) if rejection_debug_path is not None else None,
        selection_provenance=({
            "mode": "EXPLICIT_M4_SOURCE_CURRENT_ALIAS_HEADS",
            "requested_generation_ids": list(generation_ids or ()),
            "selected_sources": [
                {"generation_id": generation.interseries_generation_id,
                 "series_id": series_id, "science_calibration_version_id": cv_id,
                 "current_alias_head_match": True,
                 "field_science_alias_rows": alias_multiplicity[series_id]}
                for generation in generations for series_id, cv_id in generation.series_sources
            ],
            "field_series_count": len(alias_multiplicity),
            "field_science_alias_rows": sum(alias_multiplicity.values()),
            "unselected_field_series": 0,
        } if use_explicit_m4_source_heads else None),
    )

    raw_plot_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bins: dict[tuple[str, int, int], _BinAccumulator] = {}
    plot_input_counts: dict[str, int] = defaultdict(int)
    filter_written_counts: dict[str, int] = defaultdict(int)

    rejection_counts: dict[str, int] = defaultdict(int)
    rejection_counts_by_series: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    plot_identity_by_filter: dict[str, set[tuple[str, str]]] = defaultdict(set)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{root.name}.m5-staging-", dir=root.parent)
    )
    staged_csv_path = staging_dir / csv_path.name
    staged_summary_path = staging_dir / summary_path.name
    staged_rejection_debug_path = (
        staging_dir / rejection_debug_path.name if rejection_debug_path is not None else None
    )
    try:
        with ExitStack() as stack:
            rejection_writer = None
            if staged_rejection_debug_path is not None:
                rejection_handle = stack.enter_context(
                    staged_rejection_debug_path.open("w", newline="", encoding="utf-8")
                )
                rejection_writer = csv.DictWriter(
                    rejection_handle,
                    fieldnames=list(REJECTION_DEBUG_COLUMNS),
                    extrasaction="ignore",
                )
                rejection_writer.writeheader()

            csv_file = (
                staged_csv_path.open("wb") if budget is not None
                else staged_csv_path.open("w", newline="", encoding="utf-8")
            )
            with csv_file as handle:
                if budget is not None:
                    csv_bytes = _m5_budget_csv_bytes(None)
                    if len(csv_bytes) > _M5_BUDGET_CSV_BYTES:
                        raise ValueError("M5 budget: main CSV header exceeds byte ceiling")
                    handle.write(csv_bytes)
                    written_bytes = len(csv_bytes)
                else:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=list(GLOBAL_LIGHT_CURVE_COLUMNS),
                        extrasaction="ignore",
                    )
                    writer.writeheader()
                for raw in _iter_exact_m4_rows(
                    session,
                    object_id=object_id,
                    field_id=field_id,
                    generation_ids=generation_ids_resolved,
                    generations=generations,
                ):
                    result.rows_source_exact_m4 += 1
                    if budget is not None and result.rows_source_exact_m4 > budget.object_rows:
                        raise ValueError("M5 budget: source rows exceeded exact preflight")
                    processed = _to_m5_row(raw, reject_flags=reject_flags, field_id=field_id)
                    filter_name = str(processed.get("filter_name") or "UNKNOWN")
                    catalog_name = processed.get("catalog_name")
                    source_id = processed.get("source_id")
                    if (
                        plots
                        and plot_reduction != "none"
                        and catalog_name is not None
                        and source_id is not None
                    ):
                        catalog_text = str(catalog_name).strip()
                        source_text = str(source_id).strip()
                        if catalog_text and source_text:
                            plot_identity_by_filter[filter_name].add(
                                (catalog_text, source_text)
                            )
                    if processed["is_rejected"]:
                        result.rejected_rows += 1
                        reasons = [
                            part
                            for part in str(processed.get("reject_reasons") or "").split(",")
                            if part
                        ]
                        if not reasons:
                            reasons = ["UNSPECIFIED_REJECTION"]
                        series_id = int(processed["series_id"])
                        for reason in reasons:
                            rejection_counts[reason] += 1
                            rejection_counts_by_series[series_id][reason] += 1
                        if rejection_writer is not None:
                            rejection_writer.writerow(_csv_safe_row(processed, REJECTION_DEBUG_COLUMNS))
                    else:
                        result.accepted_rows += 1
                        plot_input_counts[filter_name] += 1
                        if plots and plot_plan.get(filter_name, {}).get("mode") == "raw":
                            raw_plot_rows[filter_name].append(_plot_row_from_m5(processed))
                        elif plots and plot_plan.get(filter_name, {}).get("mode") == "bin":
                            _accumulate_plot_bin(bins, processed, plot_plan[filter_name])
                    if include_rejected or not processed["is_rejected"]:
                        safe_row = _csv_safe_row(processed, GLOBAL_LIGHT_CURVE_COLUMNS)
                        if budget is not None:
                            csv_bytes = _m5_budget_csv_bytes(safe_row)
                            if written_bytes + len(csv_bytes) > _M5_BUDGET_CSV_BYTES:
                                raise ValueError("M5 budget: main CSV exceeds byte ceiling")
                            handle.write(csv_bytes)
                            written_bytes += len(csv_bytes)
                        else:
                            writer.writerow(safe_row)
                        result.rows_written += 1
                        filter_written_counts[filter_name] += 1

        result.rejection_reasons = dict(sorted(rejection_counts.items()))
        result.rejection_reasons_by_series = {
            str(series_id): dict(sorted(counts.items()))
            for series_id, counts in sorted(rejection_counts_by_series.items())
        }
        if result.rows_source_exact_m4 > 0 and result.accepted_rows == 0:
            result.status = M5_STATUS_NO_ACCEPTED
            result.warnings.append(
                "All exact M4-linked SCIENCE rows were rejected by the active Stage-2/M5 QC policy; "
                "no plots are produced because plots use accepted points only."
            )

        result.filters = dict(sorted(filter_written_counts.items()))
        result.plot_input_points = dict(sorted(plot_input_counts.items()))

        if use_explicit_m4_source_heads:
            if (budget is None or result.rows_source_exact_m4 != budget.object_rows
                    or result.rows_source_exact_m4 != result.accepted_rows + result.rejected_rows
                    or result.rows_written != (result.accepted_rows
                                               + (result.rejected_rows if include_rejected else 0))
                    or sum(result.filters.values()) != result.rows_written):
                raise ValueError("M5 explicit M4 source counts do not reconcile")
            with staged_csv_path.open("r", newline="", encoding="utf-8") as csv_handle:
                csv_reader = csv.DictReader(csv_handle)
                if csv_reader.fieldnames != list(GLOBAL_LIGHT_CURVE_COLUMNS):
                    raise ValueError("M5 explicit M4 CSV header differs from schema")
                csv_records = 0
                for _row in csv_reader:
                    csv_records += 1
                    if csv_records > result.rows_written:
                        raise ValueError("M5 explicit M4 CSV exceeds recorded row count")
            if csv_records != result.rows_written:
                raise ValueError("M5 explicit M4 CSV record count does not reconcile")
            if result.selection_provenance is not None:
                result.selection_provenance["csv_records"] = csv_records

        if plots and plot_reduction != "none":
            binned_by_filter: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for accumulator in bins.values():
                binned_by_filter[accumulator.filter_name].append(accumulator.finalize())
            for rows in binned_by_filter.values():
                rows.sort(key=lambda row: (float(row["mjd"]), int(row["series_id"])))

            filter_names = sorted(set(raw_plot_rows) | set(binned_by_filter))
            for filter_name in filter_names:
                rows = raw_plot_rows.get(filter_name) or binned_by_filter.get(filter_name) or []
                if not rows:
                    continue
                mode = plot_plan[filter_name]["mode"]
                plot_data_path, plot_path = configured_plot_paths[filter_name]
                staged_plot_data_path = staging_dir / plot_data_path.name
                staged_plot_path = staging_dir / plot_path.name
                _write_plot_data(rows, staged_plot_data_path)
                reliabilities = sorted({str(row.get("interseries_reliability") or "UNKNOWN") for row in rows})
                source_pairs = plot_identity_by_filter.get(filter_name, set())
                catalog_name, source_id = _unique_m5_plot_source_pair(source_pairs)
                plot_result = export_global_lightcurve_plot(
                    rows,
                    staged_plot_path,
                    title=(
                        f"Object {object_id} - {filter_name}; M4 "
                        + ", ".join(reliabilities)
                    ),
                    subtitle=format_lightcurve_identity_subtitle(
                        object_uid=object_row.get("object_uid"),
                        ra_deg=object_row.get("ra_deg"),
                        dec_deg=object_row.get("dec_deg"),
                        catalog_name=catalog_name,
                        source_id=source_id,
                    ),
                    dpi=plot_dpi,
                    reduction_mode=mode,
                    time_axis_mode=plot_time_axis,
                )
                result.plot_paths[filter_name] = str(plot_path)
                result.plot_data_paths[filter_name] = str(plot_data_path)
                result.plot_reduction[filter_name] = mode
                result.plot_output_points[filter_name] = int(plot_result.n_points)
                result.plot_time_axis[filter_name] = str(plot_result.time_axis_mode or "continuous")
                result.plot_time_breaks[filter_name] = int(plot_result.n_time_breaks)
                result.plot_style[filter_name] = str(plot_result.plot_style or "series_detailed")

        summary = json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str)
        if budget is not None:
            summary_bytes = summary.encode("utf-8")
            if len(summary_bytes) > _M5_BUDGET_SUMMARY_BYTES:
                raise ValueError("M5 budget: summary exceeds byte ceiling")
            staged_summary_path.write_bytes(summary_bytes)
        else:
            staged_summary_path.write_text(summary, encoding="utf-8")
        _rename_directory_noreplace(staging_dir, root)
        return result
    finally:
        if not preserve_staging_on_failure:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _unique_m5_plot_source_pair(
    source_pairs: set[tuple[str, str]],
) -> tuple[str | None, str | None]:
    if len(source_pairs) != 1:
        return None, None
    return next(iter(source_pairs))


def _rename_directory_noreplace(source: str | Path, destination: str | Path) -> None:
    """Publish one staged directory without replacing an existing destination.

    Only platform-native no-replace operations are accepted. Any missing primitive,
    unsupported filesystem operation, or native error fails closed; this deliberately
    has no check-then-rename or file-by-file fallback.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    platform = sys.platform
    if platform == "win32":
        # Windows os.rename maps to the native no-replace move operation.
        os.rename(source_path, destination_path)
        return

    if platform == "linux":
        symbol = "renameat2"
        argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        try:
            native = getattr(ctypes.CDLL(None, use_errno=True), symbol)
        except (AttributeError, OSError) as exc:
            raise NotImplementedError(
                "Linux M5 publication requires libc renameat2(RENAME_NOREPLACE)"
            ) from exc
        native.argtypes = argtypes
        native.restype = ctypes.c_int
        ctypes.set_errno(0)
        return_code = native(
            _AT_FDCWD,
            os.fsencode(source_path),
            _AT_FDCWD,
            os.fsencode(destination_path),
            _RENAME_NOREPLACE,
        )
    elif platform == "darwin":
        symbol = "renamex_np"
        argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        try:
            native = getattr(ctypes.CDLL(None, use_errno=True), symbol)
        except (AttributeError, OSError) as exc:
            raise NotImplementedError(
                "macOS M5 publication requires libc renamex_np(RENAME_EXCL)"
            ) from exc
        native.argtypes = argtypes
        native.restype = ctypes.c_int
        ctypes.set_errno(0)
        return_code = native(
            os.fsencode(source_path),
            os.fsencode(destination_path),
            _RENAME_EXCL,
        )
    else:
        raise NotImplementedError(
            f"M5 atomic bundle publication has no native no-replace rename for {platform!r}"
        )

    if return_code != 0:
        native_errno = ctypes.get_errno()
        if native_errno:
            raise OSError(native_errno, os.strerror(native_errno), str(destination_path))
        raise OSError(
            f"libc {symbol} returned {return_code} without setting errno; "
            "M5 bundle publication failed closed"
        )


def _iter_exact_m4_rows(
    session: Session,
    *,
    object_id: int,
    field_id: int,
    generation_ids: list[int],
    generations: Iterable[M5Generation] | None = None,
) -> Iterator[dict[str, Any]]:
    del generation_ids  # Exact source membership is already carried by generations.
    yield from _iter_exact_m4_rows_from_science(
        session,
        object_id=object_id,
        field_id=field_id,
        generations=list(generations or ()),
    )


def _iter_exact_m4_rows_from_science(
    session: Session,
    *,
    object_id: int,
    field_id: int,
    generations: list[M5Generation],
) -> Iterator[dict[str, Any]]:
    if not generations:
        return

    generation_by_id = {row.interseries_generation_id: row for row in generations}
    source_rows = session.execute(
        text(
            """
            SELECT
                ics.interseries_generation_id,
                ics.series_id,
                ics.science_calibration_version_id,
                ics.offset_mag,
                ics.offset_err_mag,
                ics.field_id AS membership_field_id,
                g.field_id AS generation_field_id,
                g.analysis_algorithm
            FROM interseries_calibration_series ics
            JOIN interseries_calibration_generations g
              ON g.interseries_generation_id = ics.interseries_generation_id
            WHERE ics.interseries_generation_id = ANY(:generation_ids)
            ORDER BY ics.interseries_generation_id, ics.series_id
            """
        ),
        {"generation_ids": sorted(generation_by_id)},
    ).mappings().all()
    if any(
        row["series_id"] is None or row["science_calibration_version_id"] is None
        or (row.get("membership_field_id") is not None
            and int(row["membership_field_id"]) != int(row["generation_field_id"]))
        for row in source_rows
    ):
        raise RuntimeError("Persisted M4 membership was orphaned during durable SCIENCE M5 read")
    source_counts: dict[int, int] = defaultdict(int)
    for row in source_rows:
        source_counts[int(row["interseries_generation_id"])] += 1
    if any(
        source_counts.get(generation_id) != len(generation.series_sources)
        for generation_id, generation in generation_by_id.items()
    ):
        raise RuntimeError("Persisted M4 membership changed during durable SCIENCE M5 read")
    source_meta = {
        (int(row["interseries_generation_id"]), int(row["series_id"])): dict(row)
        for row in source_rows
    }

    prepared: list[tuple[M5Generation, int, int, Any]] = []
    for generation in generations:
        for series_id, cv_id in generation.series_sources:
            meta = source_meta.get((generation.interseries_generation_id, int(series_id)))
            if meta is None or int(meta["science_calibration_version_id"]) != int(cv_id):
                raise RuntimeError(
                    "Persisted M4 source metadata changed during durable SCIENCE M5 read: "
                    f"generation={generation.interseries_generation_id}, series={series_id}, cv={cv_id}"
                )
            view = load_calibration_photometry_view(
                session,
                calibration_version_id=int(cv_id),
                object_ids=[int(object_id)],
                prefer_science=True,
            )
            if view.source != "SCIENCE_PHOTOMETRY":
                raise RuntimeError(
                    "M5 durable SCIENCE path selected but CalibrationPhotometryView did not use science_photometry: "
                    f"calibration_version_id={cv_id}, source={view.source}"
                )
            for measurement in view.rows:
                prepared.append((generation, int(series_id), int(cv_id), measurement))

    if not prepared:
        return
    frame_ids = sorted({int(item[3].frame_id) for item in prepared})
    frame_rows = session.execute(
        text(
            """
            SELECT
                f.frame_id, f.date_obs, f.mjd, f.jd, f.instrument_unit_id,
                f.instrument, f.site, f.tube, f.camera, f.filter_name,
                f.quality_flags, fsf.series_id, fld.field_id, fld.field_name
            FROM frames f
            JOIN field_series_frames fsf ON fsf.frame_id = f.frame_id
            JOIN field_series fs ON fs.series_id = fsf.series_id
            JOIN fields fld ON fld.field_id = fs.field_id
            WHERE f.frame_id = ANY(:frame_ids)
            """
        ),
        {"frame_ids": frame_ids},
    ).mappings().all()
    frame_by_membership = {
        (int(row["frame_id"]), int(row["series_id"])): dict(row)
        for row in frame_rows
    }

    cv_ids = sorted({item[2] for item in prepared})
    qc_rows = session.execute(
        text(
            """
            SELECT object_id, calibration_version_id, catalog_name, instrument_unit_id, flags
            FROM object_qc
            WHERE object_id = :object_id
              AND calibration_version_id = ANY(:calibration_version_ids)
              AND algorithm = 'object_qc_v1'
            """
        ),
        {"object_id": int(object_id), "calibration_version_ids": cv_ids},
    ).mappings().all()
    qc_by_key = {
        (
            int(row["calibration_version_id"]),
            row["catalog_name"],
            int(row["instrument_unit_id"]) if row["instrument_unit_id"] is not None else None,
        ): dict(row["flags"] or {})
        for row in qc_rows
    }

    rows: list[dict[str, Any]] = []
    for generation, series_id, cv_id, measurement in prepared:
        frame = frame_by_membership.get((int(measurement.frame_id), int(series_id)))
        if frame is None or int(frame["field_id"]) != int(field_id):
            continue
        flags = dict(measurement.flags or {})
        qc_flags = qc_by_key.get(
            (
                int(cv_id),
                flags.get("catalog_name"),
                int(frame["instrument_unit_id"]) if frame["instrument_unit_id"] is not None else None,
            ),
            {},
        )
        meta = source_meta[(generation.interseries_generation_id, series_id)]
        base_id = int(measurement.base_measurement_id or measurement.measurement_id or 0)
        rows.append(
            {
                # science_photometry is keyed by immutable BASE identity; expose
                # that id in both compatibility identity columns.
                "science_measurement_id": base_id,
                "measurement_id": base_id,
                "object_id": int(measurement.object_id),
                "frame_id": int(measurement.frame_id),
                "series_id": int(series_id),
                "field_name": frame["field_name"],
                "calibration_name": None,
                "calibration_version_id": int(cv_id),
                "model_name": None,
                "date_obs": frame["date_obs"],
                "mjd": frame["mjd"],
                "jd": frame["jd"],
                "instrument_unit_id": frame["instrument_unit_id"],
                "instrument": frame["instrument"],
                "site": frame["site"],
                "tube": frame["tube"],
                "camera": frame["camera"],
                "filter_name": frame["filter_name"],
                "x": measurement.x,
                "y": measurement.y,
                "flux": measurement.flux,
                "flux_err": measurement.flux_err,
                "mag_inst": measurement.mag_inst,
                "mag_cal": measurement.mag_cal,
                "mag_err": measurement.mag_err,
                "mag_err_random": measurement.mag_err_random,
                "mag_err_systematic": measurement.mag_err_systematic,
                "aperture_radius_pix": measurement.aperture_radius_pix,
                "local_background": measurement.local_background,
                "local_zp_correction": measurement.local_zp_correction,
                "centroid_dx_pix": measurement.centroid_dx_pix,
                "centroid_dy_pix": measurement.centroid_dy_pix,
                "measurement_flags": flags,
                "frame_quality_flags": dict(frame["quality_flags"] or {}),
                "object_qc_flags": dict(qc_flags),
                "interseries_offset_mag": meta["offset_mag"],
                "interseries_offset_err": meta["offset_err_mag"],
                "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
                "interseries_algorithm": meta["analysis_algorithm"],
                "interseries_generation_id": generation.interseries_generation_id,
                "interseries_generation_uid": generation.generation_uid,
                "interseries_reliability": generation.reliability_status,
                "interseries_graph_dof": generation.graph_dof,
                "interseries_reference_series_id": generation.reference_series_id,
            }
        )

    # Fill calibration metadata in one compact query.
    calibration_rows = session.execute(
        text(
            """
            SELECT calibration_version_id, name, model_name
            FROM calibration_versions
            WHERE calibration_version_id = ANY(:calibration_version_ids)
            """
        ),
        {"calibration_version_ids": cv_ids},
    ).mappings().all()
    calibration_meta = {
        int(row["calibration_version_id"]): (row["name"], row["model_name"])
        for row in calibration_rows
    }
    for row in rows:
        name, model = calibration_meta.get(int(row["calibration_version_id"]), (None, None))
        row["calibration_name"] = name
        row["model_name"] = model

    rows.sort(
        key=lambda row: (
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
            int(row["science_measurement_id"]),
        )
    )
    yield from rows


def _load_plot_source_stats(
    session: Session,
    *,
    object_id: int,
    field_id: int,
    generation_ids: list[int],
    generations: Iterable[M5Generation] | None = None,
) -> list[dict[str, Any]]:
    source_cv_ids = sorted({
        int(cv_id)
        for generation in (generations or ())
        for _series_id, cv_id in generation.series_sources
    })
    if not source_cv_ids:
        return []
    rows = session.execute(
        text(
            """
            SELECT
                COALESCE(f.filter_name, 'UNKNOWN') AS filter_name,
                ics.series_id,
                COUNT(*)::bigint AS n_points,
                MIN(f.mjd) AS mjd_min,
                MAX(f.mjd) AS mjd_max
            FROM science_photometry s
            JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
            JOIN frames f ON f.frame_id = bm.frame_id
            JOIN interseries_calibration_series ics
              ON ics.science_calibration_version_id = s.calibration_version_id
            JOIN field_series_frames fsf
              ON fsf.frame_id = bm.frame_id
             AND fsf.series_id = ics.series_id
            JOIN field_series fs ON fs.series_id = fsf.series_id
            JOIN interseries_calibration_generations g
              ON g.interseries_generation_id = ics.interseries_generation_id
            WHERE bm.object_id = :object_id
              AND fs.field_id = :field_id
              AND g.interseries_generation_id = ANY(:generation_ids)
              AND s.calibration_version_id = ANY(:calibration_version_ids)
              AND g.lifecycle_status = 'COMPLETE_IMMUTABLE'
              AND g.graph_status = 'OK'
            GROUP BY COALESCE(f.filter_name, 'UNKNOWN'), ics.series_id
            ORDER BY COALESCE(f.filter_name, 'UNKNOWN'), ics.series_id
            """
        ),
        {
            "object_id": int(object_id),
            "field_id": int(field_id),
            "generation_ids": list(generation_ids),
            "calibration_version_ids": source_cv_ids,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _build_plot_plan(
    stats: list[dict[str, Any]],
    *,
    reduction: str,
    max_points: int,
    bin_minutes: float | None,
) -> dict[str, dict[str, Any]]:
    by_filter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in stats:
        by_filter[str(row.get("filter_name") or "UNKNOWN")].append(row)
    plan: dict[str, dict[str, Any]] = {}
    for filter_name, rows in by_filter.items():
        total = sum(int(row.get("n_points") or 0) for row in rows)
        if reduction == "none":
            mode = "none"
        elif reduction == "bin" or bin_minutes is not None or total > max_points:
            mode = "bin"
        else:
            mode = "raw"
        series_plan: dict[int, dict[str, Any]] = {}
        if mode == "bin":
            allocations = _allocate_plot_points(rows, max_points=max_points)
            for row in rows:
                series_id = int(row["series_id"])
                mjd_min = _finite_or_none(row.get("mjd_min"))
                mjd_max = _finite_or_none(row.get("mjd_max"))
                allocation = int(allocations.get(series_id) or 0)
                if allocation <= 0:
                    continue
                span = (mjd_max - mjd_min) if mjd_min is not None and mjd_max is not None else 0.0
                auto_width = span / allocation if span > 0 else 1.0 / 86400.0
                explicit_width = float(bin_minutes) / 1440.0 if bin_minutes is not None else 0.0
                # Explicit bin width is a minimum requested width; M5 may make
                # it coarser to respect the hard per-filter plot-point cap.
                width = max(auto_width, explicit_width, 1.0 / 86400.0)
                series_plan[series_id] = {
                    "mjd_min": mjd_min,
                    "bin_width_days": width,
                    "max_bins": allocation,
                }
        plan[filter_name] = {
            "mode": mode,
            "source_points": total,
            "series": series_plan,
        }
    return plan


def _plot_policy_summary(
    plan: dict[str, dict[str, Any]],
    *,
    reduction: str,
    max_points: int,
    bin_minutes: float | None,
    time_axis: str,
) -> dict[str, Any]:
    per_filter: dict[str, Any] = {}
    for filter_name, payload in sorted(plan.items()):
        series_summary: dict[str, Any] = {}
        for series_id, series_plan in sorted(payload.get("series", {}).items()):
            width_days = _finite_or_none(series_plan.get("bin_width_days"))
            series_summary[str(series_id)] = {
                "mjd_min": series_plan.get("mjd_min"),
                "bin_width_minutes": None if width_days is None else width_days * 1440.0,
                "max_bins": int(series_plan.get("max_bins") or 0),
            }
        per_filter[filter_name] = {
            "mode": payload.get("mode"),
            "source_points_pre_qc": int(payload.get("source_points") or 0),
            "series": series_summary,
        }
    return {
        "requested_reduction": reduction,
        "plot_max_points_per_filter": int(max_points),
        "plot_max_points_is_hard_cap": True,
        "requested_time_axis": time_axis,
        "explicit_bin_minutes": bin_minutes,
        "main_csv_downsampled": False,
        "plots_use_accepted_points_only": True,
        "bin_statistic": "arithmetic_mean_with_minmax_envelope",
        "bin_random_error": "max(sample_sem, propagated_random_sem)",
        "series_zp_error_policy": "stored separately; correlated; not drawn as independent point error",
        "large_series_overview_threshold": 12,
        "large_series_overview_style": "single-filter style, no per-series legend",
        "per_filter": per_filter,
    }


def _allocate_plot_points(rows: list[dict[str, Any]], *, max_points: int) -> dict[int, int]:
    """Allocate an exact per-filter bin budget across non-empty series.

    Every non-empty series receives at least one plot bin. Therefore a hard
    budget smaller than the number of non-empty series is impossible without
    dropping an entire series; M5 refuses that ambiguous visualization rather
    than silently hiding data.
    """

    max_points = int(max_points)
    if max_points < 1:
        raise ValueError("max_points must be >= 1")
    counts = {int(row["series_id"]): max(0, int(row.get("n_points") or 0)) for row in rows}
    positive = {series_id: count for series_id, count in counts.items() if count > 0}
    allocations = {series_id: 0 for series_id in counts}
    if not positive:
        return allocations
    if len(positive) > max_points:
        raise ValueError(
            f"plot_max_points={max_points} is smaller than the number of non-empty series="
            f"{len(positive)}; increase --plot-max-points so every series remains represented"
        )

    total = sum(positive.values())
    budget = min(max_points, total)
    for series_id in positive:
        allocations[series_id] = 1
    remaining = budget - len(positive)
    if remaining <= 0:
        return allocations

    capacities = {series_id: max(0, count - 1) for series_id, count in positive.items()}
    total_capacity = sum(capacities.values())
    if total_capacity <= 0:
        return allocations

    remainders: list[tuple[float, int]] = []
    used = 0
    for series_id, capacity in capacities.items():
        exact = remaining * capacity / total_capacity
        extra = min(capacity, int(math.floor(exact)))
        allocations[series_id] += extra
        used += extra
        remainders.append((exact - extra, series_id))

    left = remaining - used
    for _fraction, series_id in sorted(remainders, key=lambda item: (-item[0], item[1])):
        if left <= 0:
            break
        if allocations[series_id] < positive[series_id]:
            allocations[series_id] += 1
            left -= 1

    # Very defensive second pass for rare capacity/rounding corner cases.
    if left > 0:
        for series_id in sorted(positive, key=lambda sid: (-positive[sid], sid)):
            while left > 0 and allocations[series_id] < positive[series_id]:
                allocations[series_id] += 1
                left -= 1
            if left <= 0:
                break
    return allocations


def _accumulate_plot_bin(
    bins: dict[tuple[str, int, int], _BinAccumulator],
    row: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    if not (_is_finite(row.get("mjd")) and _is_finite(row.get("mag_global"))):
        return
    filter_name = str(row.get("filter_name") or "UNKNOWN")
    series_id = int(row["series_id"])
    series_plan = plan["series"].get(series_id)
    if not series_plan:
        return
    mjd_min = series_plan.get("mjd_min")
    width = float(series_plan["bin_width_days"])
    mjd = float(row["mjd"])
    max_bins = max(1, int(series_plan.get("max_bins") or 1))
    if mjd_min is None:
        bin_index = 0
    else:
        bin_index = max(0, int(math.floor((mjd - float(mjd_min)) / width)))
        bin_index = min(max_bins - 1, bin_index)
    key = (filter_name, series_id, bin_index)
    accumulator = bins.get(key)
    if accumulator is None:
        accumulator = _BinAccumulator(
            filter_name=filter_name,
            series_id=series_id,
            bin_index=bin_index,
            generation_id=int(row["interseries_generation_id"]),
            reliability=str(row["interseries_reliability"]),
            zp_err=_finite_or_none(row.get("mag_err_series_zp")),
        )
        bins[key] = accumulator
    accumulator.add(row)



def _m5_reject_flags(*, require_detection_support: bool, extra_reject_flags: str | None) -> set[str]:
    """Return the default M5 row-rejection policy.

    ``SATURATED_LIKE`` is an Object-QC/series-level diagnostic that means the
    object had at least one saturated or near-saturated epoch.  Rejecting that
    flag at row level would discard every epoch in the series.  M5 therefore
    uses the measurement-level ``SATURATED_OR_NEAR_SATURATED`` flag as the
    default saturation veto while keeping ``SATURATED_LIKE`` diagnostic.

    An explicit ``--extra-reject-flags SATURATED_LIKE`` request restores the
    stricter object-level veto for users who deliberately want it.
    """

    explicit_extra = {
        part.strip().upper()
        for part in str(extra_reject_flags or "").split(",")
        if part.strip()
    }
    flags = light_curve_reject_flags(
        require_detection_support=require_detection_support,
        extra_flags=extra_reject_flags,
    )
    if "SATURATED_LIKE" not in explicit_extra:
        flags.discard("SATURATED_LIKE")
    flags.add("SATURATED_OR_NEAR_SATURATED")
    return flags

def _to_m5_row(raw: dict[str, Any], *, reject_flags: set[str], field_id: int) -> dict[str, Any]:
    science = _science_row_from_db(raw, reject_flags=reject_flags)
    random_err = _finite_or_none(science.get("mag_err_random"))
    zp_err = _finite_or_none(raw.get("interseries_offset_err"))
    effective = None
    if random_err is not None and zp_err is not None:
        effective = math.hypot(random_err, zp_err)
    elif random_err is not None:
        effective = random_err
    elif zp_err is not None:
        effective = zp_err
    return {
        "object_id": int(raw["object_id"]),
        "catalog_name": science.get("catalog_name"),
        "source_id": science.get("source_id"),
        "frame_id": int(raw["frame_id"]),
        "science_measurement_id": int(raw["science_measurement_id"]),
        "measurement_id": raw.get("measurement_id"),
        "series_id": int(raw["series_id"]),
        "field_id": int(field_id),
        "field_name": science.get("field_name"),
        "date_obs": science.get("date_obs"),
        "mjd": raw.get("mjd"),
        "jd": raw.get("jd"),
        "filter_name": raw.get("filter_name"),
        "instrument_unit_id": raw.get("instrument_unit_id"),
        "instrument": raw.get("instrument"),
        "site": raw.get("site"),
        "tube": raw.get("tube"),
        "camera": raw.get("camera"),
        "science_calibration_version_id": int(raw["calibration_version_id"]),
        "science_calibration_name": raw.get("calibration_name"),
        "interseries_generation_id": int(raw["interseries_generation_id"]),
        "interseries_generation_uid": raw.get("interseries_generation_uid"),
        "interseries_reliability": raw.get("interseries_reliability"),
        "interseries_graph_dof": raw.get("interseries_graph_dof"),
        "interseries_reference_series_id": raw.get("interseries_reference_series_id"),
        "interseries_offset_mag": raw.get("interseries_offset_mag"),
        "mag_science": raw.get("mag_cal"),
        "mag_global": science.get("mag_global"),
        "mag_err_science": raw.get("mag_err"),
        "mag_err_random": random_err,
        "mag_err_science_systematic": raw.get("mag_err_systematic"),
        "mag_err_series_zp": zp_err,
        "mag_err_effective": effective,
        "series_zp_correlated": True,
        "m5_link_status": M5_LINK_STATUS,
        "x": raw.get("x"),
        "y": raw.get("y"),
        "flux": raw.get("flux"),
        "flux_err": raw.get("flux_err"),
        "mag_inst": raw.get("mag_inst"),
        "aperture_radius_pix": raw.get("aperture_radius_pix"),
        "local_background": raw.get("local_background"),
        "local_zp_correction": raw.get("local_zp_correction"),
        "centroid_dx_pix": raw.get("centroid_dx_pix"),
        "centroid_dy_pix": raw.get("centroid_dy_pix"),
        "quality_flags": science.get("quality_flags"),
        "is_rejected": bool(science.get("is_rejected")),
        "reject_reasons": science.get("reject_reasons"),
    }


def _plot_row_from_m5(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "filter_name": row.get("filter_name"),
        "series_id": row.get("series_id"),
        "plot_reduction": "raw",
        "mjd": row.get("mjd"),
        "date_obs": row.get("date_obs"),
        "mag_global": row.get("mag_global"),
        "mag_err_random": row.get("mag_err_random"),
        "mag_err_series_zp": row.get("mag_err_series_zp"),
        "n_points": 1,
        "mag_min": row.get("mag_global"),
        "mag_max": row.get("mag_global"),
        "interseries_generation_id": row.get("interseries_generation_id"),
        "interseries_reliability": row.get("interseries_reliability"),
    }


def _write_plot_data(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PLOT_DATA_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row, PLOT_DATA_COLUMNS))


def _csv_safe_row(row: dict[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in columns:
        value = row.get(key)
        if isinstance(value, (dict, list, tuple)):
            out[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            out[key] = value
    return out


def _safe_filename_token(value: str) -> str:
    text_value = str(value).strip() or "UNKNOWN"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text_value)


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _max_finite(*values: Any) -> float | None:
    finite = [value for value in (_finite_or_none(v) for v in values) if value is not None]
    return max(finite) if finite else None


def _is_finite(value: Any) -> bool:
    return _finite_or_none(value) is not None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value)
    return text_value if text_value else None
