from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.interseries.planning import _load_series_candidates
from grandgousier.photometry.calibration_view import load_calibration_photometry_view
from grandgousier.lightcurves.product import (
    _science_row_from_db,
    light_curve_reject_flags,
)
from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot


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

    def to_dict(self) -> dict[str, Any]:
        return {
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
                g.reliability_status,
                g.graph_dof,
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
) -> list[SeriesCoverage]:
    selected_sources: dict[int, int] = {}
    for generation in generations:
        for series_id, cv_id in generation.series_sources:
            selected_sources[series_id] = cv_id

    current_rows = _load_series_candidates(
        session,
        field_id=int(field_id),
        instrument_unit_id=None,
        filter_name=None,
        catalog_name=None,
    )
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
) -> GlobalObjectLightCurveResult:
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

    field_row = session.execute(
        text("SELECT field_id, field_name FROM fields WHERE field_id = :field_id"),
        {"field_id": field_id},
    ).mappings().first()
    if field_row is None:
        raise ValueError(f"No field found with field_id={field_id}")
    object_row = session.execute(
        text("SELECT object_id FROM objects WHERE object_id = :object_id"),
        {"object_id": object_id},
    ).mappings().first()
    if object_row is None:
        raise ValueError(f"No object found with object_id={object_id}")

    generations = resolve_m5_generations(
        session,
        field_id=field_id,
        generation_ids=generation_ids,
    )
    coverage = inspect_m5_series_coverage(
        session,
        field_id=field_id,
        object_id=object_id,
        generations=generations,
    )
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
    plot_policy = _plot_policy_summary(
        plot_plan,
        reduction=plot_reduction,
        max_points=plot_max_points,
        bin_minutes=plot_bin_minutes,
        time_axis=plot_time_axis,
    )

    root = Path(out_root)
    root.parent.mkdir(parents=True, exist_ok=True)
    csv_path = Path(f"{root}.csv")
    summary_path = Path(f"{root}_summary.json")
    rejection_debug_path = Path(f"{root}_rejections.csv") if include_rejected_debug else None

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
    )

    raw_plot_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    bins: dict[tuple[str, int, int], _BinAccumulator] = {}
    plot_input_counts: dict[str, int] = defaultdict(int)
    filter_written_counts: dict[str, int] = defaultdict(int)

    rejection_counts: dict[str, int] = defaultdict(int)
    rejection_counts_by_series: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rejection_handle = None
    rejection_writer = None
    try:
        if rejection_debug_path is not None:
            rejection_handle = rejection_debug_path.open("w", newline="", encoding="utf-8")
            rejection_writer = csv.DictWriter(
                rejection_handle,
                fieldnames=list(REJECTION_DEBUG_COLUMNS),
                extrasaction="ignore",
            )
            rejection_writer.writeheader()

        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(GLOBAL_LIGHT_CURVE_COLUMNS), extrasaction="ignore")
            writer.writeheader()
            for raw in _iter_exact_m4_rows(
                session,
                object_id=object_id,
                field_id=field_id,
                generation_ids=generation_ids_resolved,
                generations=generations,
            ):
                result.rows_source_exact_m4 += 1
                processed = _to_m5_row(raw, reject_flags=reject_flags, field_id=field_id)
                filter_name = str(processed.get("filter_name") or "UNKNOWN")
                if processed["is_rejected"]:
                    result.rejected_rows += 1
                    reasons = [part for part in str(processed.get("reject_reasons") or "").split(",") if part]
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
                    writer.writerow(_csv_safe_row(processed, GLOBAL_LIGHT_CURVE_COLUMNS))
                    result.rows_written += 1
                    filter_written_counts[filter_name] += 1
    finally:
        if rejection_handle is not None:
            rejection_handle.close()

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
            safe_filter = _safe_filename_token(filter_name)
            plot_data_path = Path(f"{root}_{safe_filter}_plot.csv")
            _write_plot_data(rows, plot_data_path)
            plot_path = Path(f"{root}_{safe_filter}.{plot_format}")
            reliabilities = sorted({str(row.get("interseries_reliability") or "UNKNOWN") for row in rows})
            plot_result = export_global_lightcurve_plot(
                rows,
                plot_path,
                title=(
                    f"Object {object_id} - {filter_name}; M4 "
                    + ", ".join(reliabilities)
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

    summary_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


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
