"""Stable public numerical interface for Stage-3 M2/M3 inter-series calibration.

The public dataclasses intentionally contain only inputs/outputs needed by the robust
pair-offset and graph solver.  Database identity, persistence and M4/M5 provenance are
outside this API.  The numerical work delegates to the production-v1 implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

INTERSERIES_SIGN_CONVENTION = "delta_ab=m_b-m_a; z_a-z_b=delta_ab; m_global=m_science+z_s"


@dataclass(frozen=True, slots=True)
class InterseriesObjectState:
    """One M1-like object state supplied to public M2.

    ``mag_median`` and ``representative_err_mag`` are magnitudes.  Only objects with
    ``reference_eligible=True`` in both series participate in the pair solution.
    The caller is responsible for constructing scientifically appropriate M1-like
    representative states; this API does not infer reference eligibility.
    """

    object_id: int
    mag_median: float
    representative_err_mag: float
    reference_eligible: bool = True


@dataclass(frozen=True, slots=True)
class PairCalibrator:
    object_id: int
    left_mag_median: float
    right_mag_median: float
    difference_mag: float
    difference_err_mag: float
    residual_mag: float
    used: bool


@dataclass(frozen=True, slots=True)
class PairwiseOffset:
    """Robust M2 offset using ``delta_ab = m_b - m_a``."""

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


@dataclass(frozen=True, slots=True)
class GraphSeriesOffset:
    series_id: int
    offset_mag: float
    offset_err_mag: float | None
    is_reference_series: bool
    n_incident_edges: int


@dataclass(frozen=True, slots=True)
class GraphEdgeDiagnostic:
    left_series_id: int
    right_series_id: int
    measured_delta_mag: float
    measured_err_mag: float
    predicted_delta_mag: float
    closure_residual_mag: float
    standardized_residual: float


@dataclass(frozen=True, slots=True)
class GraphSolution:
    """M3 graph solution with ``z_ref=0`` and ``z_a-z_b=delta_ab``."""

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


def compute_interseries_pair_offset(
    left_states: Iterable[InterseriesObjectState],
    right_states: Iterable[InterseriesObjectState],
    *,
    left_series_id: int,
    right_series_id: int,
    min_common_objects: int = 20,
    clip_sigma: float = 4.0,
    max_clip_iterations: int = 5,
) -> PairwiseOffset:
    """Compute the production-v1 robust M2 pair offset without database access.

    The caller supplies already-aggregated M1-like states.  Magnitudes and errors are
    in mag.  No automatic threshold relaxation is performed.

    Raises
    ------
    ValueError
        If an input iterable contains duplicate ``object_id`` values.
    """

    from grandgousier.interseries.analysis import (
        SeriesObjectState as _SeriesObjectState,
        compute_pairwise_offset as _compute_pairwise_offset,
    )

    def build(
        states: Iterable[InterseriesObjectState],
        series_id: int,
    ) -> dict[int, _SeriesObjectState]:
        mapped: dict[int, _SeriesObjectState] = {}
        for item in states:
            object_id = int(item.object_id)
            if object_id in mapped:
                raise ValueError(f"duplicate object_id={object_id} in series {series_id}")
            mapped[object_id] = _SeriesObjectState(
                series_id=int(series_id),
                calibration_version_id=0,
                object_id=object_id,
                n_points=1,
                mag_median=float(item.mag_median),
                mag_mad=0.0,
                robust_sigma_mag=0.0,
                mag_err_median=float(item.representative_err_mag),
                representative_err_mag=float(item.representative_err_mag),
                ensemble_reference=bool(item.reference_eligible),
                object_qc_flags=(),
                reference_eligible=bool(item.reference_eligible),
            )
        return mapped

    result = _compute_pairwise_offset(
        build(left_states, left_series_id),
        build(right_states, right_series_id),
        left_series_id=int(left_series_id),
        right_series_id=int(right_series_id),
        min_common_objects=int(min_common_objects),
        clip_sigma=float(clip_sigma),
        max_clip_iterations=int(max_clip_iterations),
    )
    return _pair_from_internal(result)


def solve_interseries_offsets(
    series_ids: Iterable[int],
    pairwise_offsets: Iterable[PairwiseOffset],
    *,
    reference_series_id: int,
) -> GraphSolution | None:
    """Solve the production-v1 weighted M3 graph without persistence.

    Only pair rows with ``status == 'OK'`` and positive finite uncertainty are used,
    matching the internal M3 implementation.  The reference series has ``z=0``.
    Returned ``offset_err_mag`` already includes the existing
    ``max(1, reduced_chi2)`` covariance scaling when graph DoF is positive.

    Raises
    ------
    ValueError
        If ``reference_series_id`` is not in ``series_ids``.
    """

    from grandgousier.interseries.analysis import (
        PairwiseOffset as _PairwiseOffset,
        solve_interseries_graph as _solve_interseries_graph,
    )

    internal_pairs = tuple(
        _PairwiseOffset(
            left_series_id=int(row.left_series_id),
            right_series_id=int(row.right_series_id),
            delta_mag=row.delta_mag,
            offset_err_mag=row.offset_err_mag,
            scatter_mag=row.scatter_mag,
            n_initial_calibrators=int(row.n_initial_calibrators),
            n_used_calibrators=int(row.n_used_calibrators),
            n_rejected_calibrators=int(row.n_rejected_calibrators),
            clip_iterations=int(row.clip_iterations),
            status=str(row.status),
            calibrators=(),
        )
        for row in pairwise_offsets
    )
    result = _solve_interseries_graph(
        series_ids,
        internal_pairs,
        reference_series_id=int(reference_series_id),
    )
    if result is None:
        return None
    return GraphSolution(
        reference_series_id=result.reference_series_id,
        status=result.status,
        n_series=result.n_series,
        n_edges=result.n_edges,
        dof=result.dof,
        chi2=result.chi2,
        reduced_chi2=result.reduced_chi2,
        max_abs_standardized_residual=result.max_abs_standardized_residual,
        offsets=tuple(
            GraphSeriesOffset(
                series_id=row.series_id,
                offset_mag=row.offset_mag,
                offset_err_mag=row.offset_err_mag,
                is_reference_series=row.is_reference_series,
                n_incident_edges=row.n_incident_edges,
            )
            for row in result.offsets
        ),
        edges=tuple(
            GraphEdgeDiagnostic(
                left_series_id=row.left_series_id,
                right_series_id=row.right_series_id,
                measured_delta_mag=row.measured_delta_mag,
                measured_err_mag=row.measured_err_mag,
                predicted_delta_mag=row.predicted_delta_mag,
                closure_residual_mag=row.closure_residual_mag,
                standardized_residual=row.standardized_residual,
            )
            for row in result.edges
        ),
    )


def _pair_from_internal(result) -> PairwiseOffset:
    return PairwiseOffset(
        left_series_id=result.left_series_id,
        right_series_id=result.right_series_id,
        delta_mag=result.delta_mag,
        offset_err_mag=result.offset_err_mag,
        scatter_mag=result.scatter_mag,
        n_initial_calibrators=result.n_initial_calibrators,
        n_used_calibrators=result.n_used_calibrators,
        n_rejected_calibrators=result.n_rejected_calibrators,
        clip_iterations=result.clip_iterations,
        status=result.status,
        calibrators=tuple(
            PairCalibrator(
                object_id=row.object_id,
                left_mag_median=row.left_mag_median,
                right_mag_median=row.right_mag_median,
                difference_mag=row.difference_mag,
                difference_err_mag=row.difference_err_mag,
                residual_mag=row.residual_mag,
                used=row.used,
            )
            for row in result.calibrators
        ),
    )


__all__ = [
    "GraphEdgeDiagnostic",
    "GraphSeriesOffset",
    "GraphSolution",
    "INTERSERIES_SIGN_CONVENTION",
    "InterseriesObjectState",
    "PairCalibrator",
    "PairwiseOffset",
    "compute_interseries_pair_offset",
    "solve_interseries_offsets",
]
