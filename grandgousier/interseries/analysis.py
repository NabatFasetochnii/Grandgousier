from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
import csv
import json
import math

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.interseries.planning import (
    InterseriesPlan,
    InterseriesSeriesCandidate,
    build_interseries_plan,
)
from grandgousier.lightcurves.product import DEFAULT_REJECT_FLAGS


INTERSERIES_ANALYSIS_ALGORITHM = "interseries_graph_median_v1"
INTERSERIES_SIGN_CONVENTION = "delta_ab=m_b-m_a; z_a-z_b=delta_ab; m_global=m_science+z_s"
ROBUST_SIGMA_FACTOR = 1.4826
MEDIAN_STANDARD_ERROR_FACTOR = 1.2533141373155

SERIES_SUMMARY_COLUMNS: tuple[str, ...] = (
    "series_id",
    "instrument_unit_id",
    "filter_name",
    "catalog_name",
    "calibration_version_id",
    "calibration_name",
    "n_frames",
    "n_science_points",
    "n_objects",
    "n_state_objects",
    "n_ensemble_references",
    "n_reference_eligible",
    "median_points_per_object",
    "median_within_series_robust_sigma_mag",
    "reference_source",
    "time_start",
    "time_end",
)

OBJECT_STATE_COLUMNS: tuple[str, ...] = (
    "series_id",
    "calibration_version_id",
    "object_id",
    "n_points",
    "mag_median",
    "mag_mad",
    "robust_sigma_mag",
    "mag_err_median",
    "representative_err_mag",
    "ensemble_reference",
    "object_qc_flags",
    "reference_eligible",
)

PAIR_OFFSET_COLUMNS: tuple[str, ...] = (
    "left_series_id",
    "right_series_id",
    "delta_mag",
    "offset_err_mag",
    "scatter_mag",
    "n_initial_calibrators",
    "n_used_calibrators",
    "n_rejected_calibrators",
    "clip_iterations",
    "status",
)

PAIR_CALIBRATOR_COLUMNS: tuple[str, ...] = (
    "left_series_id",
    "right_series_id",
    "object_id",
    "left_mag_median",
    "right_mag_median",
    "difference_mag",
    "difference_err_mag",
    "residual_mag",
    "used",
)

GRAPH_OFFSET_COLUMNS: tuple[str, ...] = (
    "series_id",
    "offset_mag",
    "offset_err_mag",
    "is_reference_series",
    "n_incident_edges",
)

GRAPH_EDGE_COLUMNS: tuple[str, ...] = (
    "left_series_id",
    "right_series_id",
    "measured_delta_mag",
    "measured_err_mag",
    "predicted_delta_mag",
    "closure_residual_mag",
    "standardized_residual",
)


@dataclass(frozen=True)
class SeriesObjectState:
    series_id: int
    calibration_version_id: int
    object_id: int
    n_points: int
    mag_median: float
    mag_mad: float
    robust_sigma_mag: float
    mag_err_median: float | None
    representative_err_mag: float
    ensemble_reference: bool
    object_qc_flags: tuple[str, ...]
    reference_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["object_qc_flags"] = ",".join(self.object_qc_flags)
        return row


@dataclass(frozen=True)
class SeriesSummary:
    series_id: int
    instrument_unit_id: int | None
    filter_name: str | None
    catalog_name: str | None
    calibration_version_id: int
    calibration_name: str
    n_frames: int
    n_science_points: int
    n_objects: int
    n_state_objects: int
    n_ensemble_references: int
    n_reference_eligible: int
    median_points_per_object: float | None
    median_within_series_robust_sigma_mag: float | None
    reference_source: str
    time_start: str | None
    time_end: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PairCalibrator:
    left_series_id: int
    right_series_id: int
    object_id: int
    left_mag_median: float
    right_mag_median: float
    difference_mag: float
    difference_err_mag: float
    residual_mag: float
    used: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PairwiseOffset:
    left_series_id: int
    right_series_id: int
    delta_mag: float | None
    offset_err_mag: float | None
    scatter_mag: float | None
    n_initial_calibrators: int
    n_used_calibrators: int
    n_rejected_calibrators: int
    clip_iterations: int
    status: str
    calibrators: tuple[PairCalibrator, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("calibrators", None)
        return row


@dataclass(frozen=True)
class GraphSeriesOffset:
    series_id: int
    offset_mag: float
    offset_err_mag: float | None
    is_reference_series: bool
    n_incident_edges: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphEdgeDiagnostic:
    left_series_id: int
    right_series_id: int
    measured_delta_mag: float
    measured_err_mag: float
    predicted_delta_mag: float
    closure_residual_mag: float
    standardized_residual: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphSolution:
    reference_series_id: int
    status: str
    n_series: int
    n_edges: int
    dof: int
    chi2: float
    reduced_chi2: float | None
    max_abs_standardized_residual: float | None
    offsets: tuple[GraphSeriesOffset, ...]
    edges: tuple[GraphEdgeDiagnostic, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_series_id": self.reference_series_id,
            "status": self.status,
            "n_series": self.n_series,
            "n_edges": self.n_edges,
            "dof": self.dof,
            "chi2": self.chi2,
            "reduced_chi2": self.reduced_chi2,
            "max_abs_standardized_residual": self.max_abs_standardized_residual,
            "offsets": [row.to_dict() for row in self.offsets],
            "edges": [row.to_dict() for row in self.edges],
        }


@dataclass(frozen=True)
class InterseriesAnalysis:
    field_id: int
    field_name: str | None
    algorithm: str
    sign_convention: str
    selected_series_ids: tuple[int, ...]
    reference_series_id: int
    status: str
    min_points_per_object: int
    min_common_objects: int
    clip_sigma: float
    max_clip_iterations: int
    plan: InterseriesPlan
    series_summaries: tuple[SeriesSummary, ...]
    object_states: tuple[SeriesObjectState, ...]
    pairwise_offsets: tuple[PairwiseOffset, ...]
    graph_solution: GraphSolution | None

    def to_dict(self, *, include_object_states: bool = False, include_calibrators: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "field_id": self.field_id,
            "field_name": self.field_name,
            "algorithm": self.algorithm,
            "sign_convention": self.sign_convention,
            "selected_series_ids": list(self.selected_series_ids),
            "reference_series_id": self.reference_series_id,
            "status": self.status,
            "min_points_per_object": self.min_points_per_object,
            "min_common_objects": self.min_common_objects,
            "clip_sigma": self.clip_sigma,
            "max_clip_iterations": self.max_clip_iterations,
            "plan": self.plan.to_dict(),
            "series_summaries": [row.to_dict() for row in self.series_summaries],
            "pairwise_offsets": [row.to_dict() for row in self.pairwise_offsets],
            "graph_solution": self.graph_solution.to_dict() if self.graph_solution is not None else None,
        }
        if include_object_states:
            payload["object_states"] = [row.to_dict() for row in self.object_states]
        if include_calibrators:
            payload["pairwise_calibrators"] = [
                calibrator.to_dict()
                for pair in self.pairwise_offsets
                for calibrator in pair.calibrators
            ]
        return payload


def analyze_interseries_calibration(
    session: Session,
    *,
    field_id: int | None = None,
    field_name: str | None = None,
    instrument_unit_id: int | None = None,
    filter_name: str | None = None,
    catalog_name: str | None = None,
    reference_series_id: int | None = None,
    min_points_per_object: int = 5,
    min_common_objects: int = 20,
    clip_sigma: float = 4.0,
    max_clip_iterations: int = 5,
    required_stage2_recipe: str | None = None,
    science_calibration_version_ids: list[int] | None = None,
) -> InterseriesAnalysis:
    """Run read-only Stage-3 M1--M3 analysis on published Stage-2 SCIENCE products."""

    min_points_per_object = max(1, int(min_points_per_object))
    min_common_objects = max(2, int(min_common_objects))
    clip_sigma = float(clip_sigma)
    max_clip_iterations = max(1, int(max_clip_iterations))
    if not math.isfinite(clip_sigma) or clip_sigma <= 0:
        raise ValueError("clip_sigma must be a positive finite value")

    plan = build_interseries_plan(
        session,
        field_id=field_id,
        field_name=field_name,
        instrument_unit_id=instrument_unit_id,
        filter_name=filter_name,
        catalog_name=catalog_name,
        min_points_per_object=min_points_per_object,
        min_common_objects=min_common_objects,
        required_stage2_recipe=required_stage2_recipe,
        science_calibration_version_ids=science_calibration_version_ids,
    )
    selected_ids, resolved_reference = _select_analysis_component(
        plan,
        reference_series_id=reference_series_id,
    )
    selected_series = [row for row in plan.series if row.series_id in set(selected_ids)]
    by_series = {row.series_id: row for row in selected_series}

    object_states: list[SeriesObjectState] = []
    summaries: list[SeriesSummary] = []
    states_by_series: dict[int, dict[int, SeriesObjectState]] = {}
    for series in selected_series:
        if series.calibration_version_id is None or series.calibration_name is None:
            raise ValueError(f"Series {series.series_id} has no published SCIENCE calibration")
        aggregate_rows = _load_series_object_aggregates(
            session,
            calibration_version_id=int(series.calibration_version_id),
            min_points_per_object=min_points_per_object,
        )
        ensemble_refs, reference_source = _load_nearest_ensemble_reference_ids(
            session,
            calibration_version_id=int(series.calibration_version_id),
        )
        qc_flags = _load_object_qc_flags(
            session,
            calibration_version_id=int(series.calibration_version_id),
        )
        state_rows: list[SeriesObjectState] = []
        for raw in aggregate_rows:
            object_id = int(raw["object_id"])
            n_points = int(raw["n_points"])
            mag_median = float(raw["mag_median"])
            mad = max(0.0, float(raw.get("mag_mad") or 0.0))
            robust_sigma = ROBUST_SIGMA_FACTOR * mad
            median_err = _finite_or_none(raw.get("mag_err_median"))
            representative_err = _representative_error(
                n_points=n_points,
                robust_sigma=robust_sigma,
                median_measurement_err=median_err,
            )
            flags = tuple(sorted(qc_flags.get(object_id, ())))
            ensemble_reference = object_id in ensemble_refs
            eligible = ensemble_reference and not _has_reference_veto(flags)
            state_rows.append(
                SeriesObjectState(
                    series_id=series.series_id,
                    calibration_version_id=int(series.calibration_version_id),
                    object_id=object_id,
                    n_points=n_points,
                    mag_median=mag_median,
                    mag_mad=mad,
                    robust_sigma_mag=robust_sigma,
                    mag_err_median=median_err,
                    representative_err_mag=representative_err,
                    ensemble_reference=ensemble_reference,
                    object_qc_flags=flags,
                    reference_eligible=eligible,
                )
            )
        states_by_series[series.series_id] = {row.object_id: row for row in state_rows}
        object_states.extend(state_rows)
        points = [row.n_points for row in state_rows]
        scatters = [row.robust_sigma_mag for row in state_rows if math.isfinite(row.robust_sigma_mag)]
        summaries.append(
            SeriesSummary(
                series_id=series.series_id,
                instrument_unit_id=series.instrument_unit_id,
                filter_name=series.filter_name,
                catalog_name=series.catalog_name,
                calibration_version_id=int(series.calibration_version_id),
                calibration_name=str(series.calibration_name),
                n_frames=series.n_frames,
                n_science_points=series.n_science_points,
                n_objects=series.n_objects,
                n_state_objects=len(state_rows),
                n_ensemble_references=len(ensemble_refs),
                n_reference_eligible=sum(1 for row in state_rows if row.reference_eligible),
                median_points_per_object=float(np.median(points)) if points else None,
                median_within_series_robust_sigma_mag=float(np.median(scatters)) if scatters else None,
                reference_source=reference_source,
                time_start=series.time_start,
                time_end=series.time_end,
            )
        )

    eligible_pairs = {
        (pair.left_series_id, pair.right_series_id)
        for pair in plan.pairs
        if pair.eligible and pair.left_series_id in by_series and pair.right_series_id in by_series
    }
    pairwise: list[PairwiseOffset] = []
    for left_id, right_id in sorted(eligible_pairs):
        pairwise.append(
            compute_pairwise_offset(
                states_by_series.get(left_id, {}),
                states_by_series.get(right_id, {}),
                left_series_id=left_id,
                right_series_id=right_id,
                min_common_objects=min_common_objects,
                clip_sigma=clip_sigma,
                max_clip_iterations=max_clip_iterations,
            )
        )

    graph = solve_interseries_graph(
        selected_ids,
        pairwise,
        reference_series_id=resolved_reference,
    )
    if graph is None:
        status = "NO_CONNECTED_REFERENCE_GRAPH"
    elif graph.status == "OK":
        status = "READY_M1_M3"
    else:
        status = graph.status
    return InterseriesAnalysis(
        field_id=plan.field_id,
        field_name=plan.field_name,
        algorithm=INTERSERIES_ANALYSIS_ALGORITHM,
        sign_convention=INTERSERIES_SIGN_CONVENTION,
        selected_series_ids=tuple(selected_ids),
        reference_series_id=resolved_reference,
        status=status,
        min_points_per_object=min_points_per_object,
        min_common_objects=min_common_objects,
        clip_sigma=clip_sigma,
        max_clip_iterations=max_clip_iterations,
        plan=plan,
        series_summaries=tuple(summaries),
        object_states=tuple(object_states),
        pairwise_offsets=tuple(pairwise),
        graph_solution=graph,
    )


def compute_pairwise_offset(
    left_states: dict[int, SeriesObjectState],
    right_states: dict[int, SeriesObjectState],
    *,
    left_series_id: int,
    right_series_id: int,
    min_common_objects: int = 20,
    clip_sigma: float = 4.0,
    max_clip_iterations: int = 5,
) -> PairwiseOffset:
    common_ids = sorted(
        object_id
        for object_id in (set(left_states) & set(right_states))
        if left_states[object_id].reference_eligible and right_states[object_id].reference_eligible
    )
    n_initial = len(common_ids)
    if n_initial < int(min_common_objects):
        return PairwiseOffset(
            left_series_id=left_series_id,
            right_series_id=right_series_id,
            delta_mag=None,
            offset_err_mag=None,
            scatter_mag=None,
            n_initial_calibrators=n_initial,
            n_used_calibrators=0,
            n_rejected_calibrators=0,
            clip_iterations=0,
            status="INSUFFICIENT_REFERENCE_OBJECTS",
            calibrators=(),
        )

    differences = np.asarray(
        [right_states[obj].mag_median - left_states[obj].mag_median for obj in common_ids],
        dtype=float,
    )
    diff_errs = np.asarray(
        [
            math.hypot(
                left_states[obj].representative_err_mag,
                right_states[obj].representative_err_mag,
            )
            for obj in common_ids
        ],
        dtype=float,
    )
    finite = np.isfinite(differences) & np.isfinite(diff_errs)
    mask = finite.copy()
    iterations = 0
    for iteration in range(max(1, int(max_clip_iterations))):
        values = differences[mask]
        if values.size < int(min_common_objects):
            break
        center = float(np.median(values))
        mad = float(np.median(np.abs(values - center)))
        robust_sigma = ROBUST_SIGMA_FACTOR * mad
        iterations = iteration + 1
        uncertainty_scale = float(np.median(diff_errs[mask])) if np.any(mask) else 0.0
        clipping_scale = max(
            robust_sigma if math.isfinite(robust_sigma) else 0.0,
            uncertainty_scale if math.isfinite(uncertainty_scale) else 0.0,
            1.0e-6,
        )
        candidate = finite & (np.abs(differences - center) <= float(clip_sigma) * clipping_scale)
        if np.array_equal(candidate, mask):
            break
        mask = candidate

    n_used = int(mask.sum())
    if n_used < int(min_common_objects):
        return PairwiseOffset(
            left_series_id=left_series_id,
            right_series_id=right_series_id,
            delta_mag=None,
            offset_err_mag=None,
            scatter_mag=None,
            n_initial_calibrators=n_initial,
            n_used_calibrators=n_used,
            n_rejected_calibrators=n_initial - n_used,
            clip_iterations=iterations,
            status="INSUFFICIENT_AFTER_CLIPPING",
            calibrators=(),
        )

    used_values = differences[mask]
    delta = float(np.median(used_values))
    mad = float(np.median(np.abs(used_values - delta)))
    scatter = ROBUST_SIGMA_FACTOR * mad
    scatter_sem = MEDIAN_STANDARD_ERROR_FACTOR * scatter / math.sqrt(n_used)
    measurement_sem = float(np.median(diff_errs[mask])) / math.sqrt(n_used)
    offset_err = max(scatter_sem, measurement_sem, 1.0e-6)
    calibrators = tuple(
        PairCalibrator(
            left_series_id=left_series_id,
            right_series_id=right_series_id,
            object_id=object_id,
            left_mag_median=left_states[object_id].mag_median,
            right_mag_median=right_states[object_id].mag_median,
            difference_mag=float(differences[index]),
            difference_err_mag=float(diff_errs[index]),
            residual_mag=float(differences[index] - delta),
            used=bool(mask[index]),
        )
        for index, object_id in enumerate(common_ids)
        if finite[index]
    )
    return PairwiseOffset(
        left_series_id=left_series_id,
        right_series_id=right_series_id,
        delta_mag=delta,
        offset_err_mag=offset_err,
        scatter_mag=scatter,
        n_initial_calibrators=n_initial,
        n_used_calibrators=n_used,
        n_rejected_calibrators=n_initial - n_used,
        clip_iterations=iterations,
        status="OK",
        calibrators=calibrators,
    )


def solve_interseries_graph(
    series_ids: Iterable[int],
    pairwise_offsets: Iterable[PairwiseOffset],
    *,
    reference_series_id: int,
) -> GraphSolution | None:
    nodes = sorted({int(value) for value in series_ids})
    reference_series_id = int(reference_series_id)
    if reference_series_id not in nodes:
        raise ValueError(f"reference_series_id={reference_series_id} is not in selected series")
    edges = [
        row
        for row in pairwise_offsets
        if row.status == "OK"
        and row.delta_mag is not None
        and row.offset_err_mag is not None
        and row.offset_err_mag > 0
        and row.left_series_id in nodes
        and row.right_series_id in nodes
    ]
    if len(nodes) < 2 or not edges:
        return None
    if not _graph_is_connected(nodes, edges):
        return GraphSolution(
            reference_series_id=reference_series_id,
            status="DISCONNECTED_AFTER_REFERENCE_QC",
            n_series=len(nodes),
            n_edges=len(edges),
            dof=max(0, len(edges) - (len(nodes) - 1)),
            chi2=0.0,
            reduced_chi2=None,
            max_abs_standardized_residual=None,
            offsets=(),
            edges=(),
        )

    unknown_nodes = [node for node in nodes if node != reference_series_id]
    column = {node: index for index, node in enumerate(unknown_nodes)}
    matrix = np.zeros((len(edges), len(unknown_nodes)), dtype=float)
    target = np.zeros(len(edges), dtype=float)
    sigma = np.zeros(len(edges), dtype=float)
    for row_index, edge in enumerate(edges):
        if edge.left_series_id != reference_series_id:
            matrix[row_index, column[edge.left_series_id]] += 1.0
        if edge.right_series_id != reference_series_id:
            matrix[row_index, column[edge.right_series_id]] -= 1.0
        target[row_index] = float(edge.delta_mag)
        sigma[row_index] = max(float(edge.offset_err_mag), 1.0e-6)

    weighted_matrix = matrix / sigma[:, None]
    weighted_target = target / sigma
    solution, _, rank, _ = np.linalg.lstsq(weighted_matrix, weighted_target, rcond=None)
    if int(rank) < len(unknown_nodes):
        return GraphSolution(
            reference_series_id=reference_series_id,
            status="SINGULAR_GRAPH",
            n_series=len(nodes),
            n_edges=len(edges),
            dof=max(0, len(edges) - (len(nodes) - 1)),
            chi2=0.0,
            reduced_chi2=None,
            max_abs_standardized_residual=None,
            offsets=(),
            edges=(),
        )

    z = {reference_series_id: 0.0}
    z.update({node: float(solution[column[node]]) for node in unknown_nodes})
    predicted = matrix @ solution
    residual = predicted - target
    standardized = residual / sigma
    chi2 = float(np.sum(standardized**2))
    dof = max(0, len(edges) - len(unknown_nodes))
    reduced = (chi2 / dof) if dof > 0 else None
    scale = max(1.0, float(reduced)) if reduced is not None and math.isfinite(reduced) else 1.0
    normal = weighted_matrix.T @ weighted_matrix
    covariance = np.linalg.pinv(normal) * scale

    incident = {node: 0 for node in nodes}
    for edge in edges:
        incident[edge.left_series_id] += 1
        incident[edge.right_series_id] += 1
    offsets: list[GraphSeriesOffset] = []
    for node in nodes:
        if node == reference_series_id:
            err: float | None = 0.0
        else:
            variance = float(covariance[column[node], column[node]])
            err = math.sqrt(max(0.0, variance)) if math.isfinite(variance) else None
        offsets.append(
            GraphSeriesOffset(
                series_id=node,
                offset_mag=float(z[node]),
                offset_err_mag=err,
                is_reference_series=node == reference_series_id,
                n_incident_edges=incident[node],
            )
        )
    diagnostics = tuple(
        GraphEdgeDiagnostic(
            left_series_id=edge.left_series_id,
            right_series_id=edge.right_series_id,
            measured_delta_mag=float(edge.delta_mag),
            measured_err_mag=float(edge.offset_err_mag),
            predicted_delta_mag=float(z[edge.left_series_id] - z[edge.right_series_id]),
            closure_residual_mag=float(z[edge.left_series_id] - z[edge.right_series_id] - float(edge.delta_mag)),
            standardized_residual=float(
                (z[edge.left_series_id] - z[edge.right_series_id] - float(edge.delta_mag))
                / float(edge.offset_err_mag)
            ),
        )
        for edge in edges
    )
    max_std = max((abs(row.standardized_residual) for row in diagnostics), default=0.0)
    status = "OK"
    if (reduced is not None and reduced > 5.0) or max_std > 5.0:
        status = "INCONSISTENT_GRAPH"
    return GraphSolution(
        reference_series_id=reference_series_id,
        status=status,
        n_series=len(nodes),
        n_edges=len(edges),
        dof=dof,
        chi2=chi2,
        reduced_chi2=reduced,
        max_abs_standardized_residual=max_std,
        offsets=tuple(offsets),
        edges=diagnostics,
    )


def export_interseries_analysis(analysis: InterseriesAnalysis, out_root: str | Path) -> dict[str, Path]:
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    paths["series_summary"] = _write_csv(
        root / "series_summary.csv",
        [row.to_dict() for row in analysis.series_summaries],
        SERIES_SUMMARY_COLUMNS,
    )
    paths["series_object_states"] = _write_csv(
        root / "series_object_states.csv",
        [row.to_dict() for row in analysis.object_states],
        OBJECT_STATE_COLUMNS,
    )
    paths["pairwise_offsets"] = _write_csv(
        root / "pairwise_offsets.csv",
        [row.to_dict() for row in analysis.pairwise_offsets],
        PAIR_OFFSET_COLUMNS,
    )
    paths["pairwise_calibrators"] = _write_csv(
        root / "pairwise_calibrators.csv",
        [cal.to_dict() for pair in analysis.pairwise_offsets for cal in pair.calibrators],
        PAIR_CALIBRATOR_COLUMNS,
    )
    graph_offsets = [] if analysis.graph_solution is None else [row.to_dict() for row in analysis.graph_solution.offsets]
    graph_edges = [] if analysis.graph_solution is None else [row.to_dict() for row in analysis.graph_solution.edges]
    paths["graph_offsets"] = _write_csv(root / "graph_offsets.csv", graph_offsets, GRAPH_OFFSET_COLUMNS)
    paths["graph_edges"] = _write_csv(root / "graph_edges.csv", graph_edges, GRAPH_EDGE_COLUMNS)
    summary_path = root / "interseries_summary.json"
    summary_path.write_text(
        json.dumps(analysis.to_dict(include_object_states=False, include_calibrators=False), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["summary"] = summary_path
    return paths


def _select_analysis_component(
    plan: InterseriesPlan,
    *,
    reference_series_id: int | None,
) -> tuple[list[int], int]:
    components = [list(component) for component in plan.connected_components if len(component) >= 2]
    if reference_series_id is not None:
        ref = int(reference_series_id)
        matches = [component for component in components if ref in component]
        if not matches:
            raise ValueError(
                f"reference_series_id={ref} is not in an eligible Stage-3 component; "
                "narrow the field/filter/instrument/catalog scope or inspect plan-interseries-calibration"
            )
        return sorted(matches[0]), ref
    if not components:
        raise ValueError(
            "No compatible Stage-3 component with at least two series. "
            "Run plan-interseries-calibration and rebuild/add compatible Stage-2 SCIENCE series first."
        )
    if len(components) > 1:
        rendered = "; ".join(",".join(str(value) for value in component) for component in components)
        raise ValueError(
            "Multiple independent eligible Stage-3 components are present: "
            f"{rendered}. Narrow --filter/--instrument-unit-id/--catalog-name or set --reference-series-id."
        )
    component = sorted(components[0])
    recommended = plan.recommended_reference_series_id
    if recommended not in component:
        recommended = component[0]
    return component, int(recommended)


def _load_series_object_aggregates(
    session: Session,
    *,
    calibration_version_id: int,
    min_points_per_object: int,
) -> list[dict[str, Any]]:
    cv_id = int(calibration_version_id)
    source_sql = """
        SELECT
            bm.object_id,
            s.mag_cal::double precision AS mag_cal,
            COALESCE(s.mag_err_random, s.mag_err)::double precision AS mag_err
        FROM science_photometry s
        JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
        WHERE s.calibration_version_id = :calibration_version_id
          AND s.mag_cal IS NOT NULL
          AND s.mag_cal NOT IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)
    """
    sql = text(
        f"""
        WITH valid AS (
            {source_sql}
        ), med AS (
            SELECT
                object_id,
                COUNT(*)::int AS n_points,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY mag_cal) AS mag_median,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY mag_err)
                    FILTER (WHERE mag_err IS NOT NULL AND mag_err >= 0 AND mag_err NOT IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)) AS mag_err_median
            FROM valid
            GROUP BY object_id
            HAVING COUNT(*) >= :min_points_per_object
        )
        SELECT
            med.object_id,
            med.n_points,
            med.mag_median,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(valid.mag_cal - med.mag_median)) AS mag_mad,
            med.mag_err_median
        FROM med
        JOIN valid USING (object_id)
        GROUP BY med.object_id, med.n_points, med.mag_median, med.mag_err_median
        ORDER BY med.object_id
        """
    )
    return [
        dict(row)
        for row in session.execute(
            sql,
            {
                "calibration_version_id": cv_id,
                "min_points_per_object": int(min_points_per_object),
            },
        ).mappings().all()
    ]


def _load_nearest_ensemble_reference_ids(
    session: Session,
    *,
    calibration_version_id: int,
) -> tuple[set[int], str]:
    rows = session.execute(
        text(
            """
            WITH RECURSIVE chain AS (
                SELECT calibration_version_id, source_calibration_version_id, 0 AS depth
                FROM calibration_versions
                WHERE calibration_version_id = :calibration_version_id
                UNION ALL
                SELECT parent.calibration_version_id, parent.source_calibration_version_id, chain.depth + 1
                FROM calibration_versions parent
                JOIN chain ON chain.source_calibration_version_id = parent.calibration_version_id
                WHERE chain.depth < 64
            ), chosen AS (
                SELECT ci.calibration_iteration_id, chain.depth, ci.iteration_no
                FROM chain
                JOIN calibration_iterations ci
                  ON ci.calibration_version_id = chain.calibration_version_id
                WHERE ci.iteration_kind = 'ENSEMBLE'
                  AND ci.status = 'COMPLETE'
                  AND ci.is_final = TRUE
                ORDER BY chain.depth ASC, ci.iteration_no DESC, ci.calibration_iteration_id DESC
                LIMIT 1
            )
            SELECT cio.object_id
            FROM chosen
            JOIN calibration_iteration_objects cio
              ON cio.calibration_iteration_id = chosen.calibration_iteration_id
            WHERE cio.is_reference = TRUE
              AND cio.effective_exclusion = FALSE
            ORDER BY cio.object_id
            """
        ),
        {"calibration_version_id": int(calibration_version_id)},
    ).mappings().all()
    refs = {int(row["object_id"]) for row in rows}
    return refs, ("NEAREST_FINAL_ENSEMBLE" if refs else "NO_ENSEMBLE_REFERENCE_MEMBERSHIP")


def _load_object_qc_flags(
    session: Session,
    *,
    calibration_version_id: int,
) -> dict[int, tuple[str, ...]]:
    rows = session.execute(
        text(
            """
            SELECT object_id, flags
            FROM object_qc
            WHERE calibration_version_id = :calibration_version_id
              AND algorithm = 'object_qc_v1'
            ORDER BY object_id
            """
        ),
        {"calibration_version_id": int(calibration_version_id)},
    ).mappings().all()
    result: dict[int, tuple[str, ...]] = {}
    for row in rows:
        flags = row.get("flags") or {}
        active = tuple(sorted(str(key) for key, value in flags.items() if bool(value)))
        result[int(row["object_id"])] = active
    return result


def _representative_error(
    *,
    n_points: int,
    robust_sigma: float,
    median_measurement_err: float | None,
) -> float:
    n = max(1, int(n_points))
    scatter_term = MEDIAN_STANDARD_ERROR_FACTOR * max(0.0, float(robust_sigma)) / math.sqrt(n)
    measurement_term = (
        max(0.0, float(median_measurement_err)) / math.sqrt(n)
        if median_measurement_err is not None and math.isfinite(median_measurement_err)
        else 0.0
    )
    return max(scatter_term, measurement_term, 1.0e-6)


def _has_reference_veto(flags: Iterable[str]) -> bool:
    active = {str(flag) for flag in flags}
    return bool(active & set(DEFAULT_REJECT_FLAGS))


def _graph_is_connected(nodes: list[int], edges: list[PairwiseOffset]) -> bool:
    adjacency: dict[int, set[int]] = {node: set() for node in nodes}
    for edge in edges:
        adjacency[edge.left_series_id].add(edge.right_series_id)
        adjacency[edge.right_series_id].add(edge.left_series_id)
    seen: set[int] = set()
    stack = [nodes[0]]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return seen == set(nodes)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: Iterable[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(columns)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
