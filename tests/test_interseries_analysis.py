from __future__ import annotations

import math

from grandgousier.interseries.analysis import (
    SeriesObjectState,
    compute_pairwise_offset,
    solve_interseries_graph,
)


def _state(series_id: int, object_id: int, mag: float, *, eligible: bool = True) -> SeriesObjectState:
    return SeriesObjectState(
        series_id=series_id,
        calibration_version_id=100 + series_id,
        object_id=object_id,
        n_points=20,
        mag_median=mag,
        mag_mad=0.01,
        robust_sigma_mag=0.014826,
        mag_err_median=0.01,
        representative_err_mag=0.003,
        ensemble_reference=eligible,
        object_qc_flags=(),
        reference_eligible=eligible,
    )


def test_pairwise_offset_uses_additive_stage3_sign_and_clips_outlier() -> None:
    left = {i: _state(1, i, 12.0 + i * 0.01) for i in range(1, 31)}
    right = {i: _state(2, i, 12.2 + i * 0.01) for i in range(1, 31)}
    right[30] = _state(2, 30, 15.0)

    pair = compute_pairwise_offset(
        left,
        right,
        left_series_id=1,
        right_series_id=2,
        min_common_objects=20,
        clip_sigma=4.0,
    )

    assert pair.status == "OK"
    assert pair.n_used_calibrators == 29
    assert pair.n_rejected_calibrators == 1
    assert math.isclose(pair.delta_mag or 0.0, 0.2, abs_tol=1e-12)
    # delta_12 = m_2 - m_1 = +0.2 means z_1 - z_2 = +0.2.


def test_pairwise_offset_requires_reference_eligible_overlap() -> None:
    left = {i: _state(1, i, 12.0 + i * 0.01, eligible=i <= 10) for i in range(1, 31)}
    right = {i: _state(2, i, 12.2 + i * 0.01) for i in range(1, 31)}

    pair = compute_pairwise_offset(
        left,
        right,
        left_series_id=1,
        right_series_id=2,
        min_common_objects=20,
    )
    assert pair.status == "INSUFFICIENT_REFERENCE_OBJECTS"
    assert pair.n_initial_calibrators == 10


def _pair(left: int, right: int, delta: float, err: float = 0.01):
    from grandgousier.interseries.analysis import PairwiseOffset

    return PairwiseOffset(
        left_series_id=left,
        right_series_id=right,
        delta_mag=delta,
        offset_err_mag=err,
        scatter_mag=0.02,
        n_initial_calibrators=50,
        n_used_calibrators=50,
        n_rejected_calibrators=0,
        clip_iterations=1,
        status="OK",
    )


def test_graph_solution_recovers_additive_offsets_and_zero_closure() -> None:
    # True additive offsets: z1=0, z2=-0.2, z3=+0.1.
    # Therefore delta_12=z1-z2=+0.2; delta_13=-0.1; delta_23=-0.3.
    graph = solve_interseries_graph(
        [1, 2, 3],
        [_pair(1, 2, 0.2), _pair(1, 3, -0.1), _pair(2, 3, -0.3)],
        reference_series_id=1,
    )
    assert graph is not None
    assert graph.status == "OK"
    offsets = {row.series_id: row.offset_mag for row in graph.offsets}
    assert math.isclose(offsets[1], 0.0, abs_tol=1e-12)
    assert math.isclose(offsets[2], -0.2, abs_tol=1e-12)
    assert math.isclose(offsets[3], 0.1, abs_tol=1e-12)
    assert graph.reduced_chi2 is not None
    assert graph.reduced_chi2 < 1e-20


def test_graph_solution_flags_inconsistent_triangle() -> None:
    graph = solve_interseries_graph(
        [1, 2, 3],
        [_pair(1, 2, 0.2, 0.002), _pair(1, 3, -0.1, 0.002), _pair(2, 3, 0.1, 0.002)],
        reference_series_id=1,
    )
    assert graph is not None
    assert graph.status == "INCONSISTENT_GRAPH"
    assert graph.reduced_chi2 is not None and graph.reduced_chi2 > 5


def test_graph_solution_refuses_disconnected_edges() -> None:
    graph = solve_interseries_graph(
        [1, 2, 3],
        [_pair(1, 2, 0.2)],
        reference_series_id=1,
    )
    assert graph is not None
    assert graph.status == "DISCONNECTED_AFTER_REFERENCE_QC"
    assert graph.offsets == ()


def test_stage3_analysis_cli_contract() -> None:
    import pytest

    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "analyze-interseries-calibration",
            "--field-id",
            "7",
            "--filter",
            "V",
            "--reference-series-id",
            "12",
            "--clip-sigma",
            "3.5",
            "--out-root",
            "reports/stage3",
        ]
    )
    assert args.command == "analyze-interseries-calibration"
    assert args.field_id == 7
    assert args.filter_name == "V"
    assert args.reference_series_id == 12
    assert args.clip_sigma == 3.5
    assert args.out_root == "reports/stage3"


def test_analyze_interseries_calibration_end_to_end_with_database_rows() -> None:
    from datetime import datetime, timezone

    from grandgousier.interseries.analysis import analyze_interseries_calibration

    class FakeMappings:
        def __init__(self, rows):
            self.rows = rows
        def first(self):
            return self.rows[0] if self.rows else None
        def all(self):
            return list(self.rows)

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows
        def mappings(self):
            return FakeMappings(self.rows)

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM science_photometry_cache" in sql:
                return FakeResult([])
            if sql.strip().startswith("SELECT calibration_version_id, COUNT(*)::bigint AS n") and "FROM science_measurements" in sql:
                return FakeResult([
                    {"calibration_version_id": int(cv), "n": 1}
                    for cv in params.get("ids", [])
                ])
            if "FROM fields" in sql:
                return FakeResult([{"field_id": 9, "field_name": "GPX_TEST"}])
            if "FROM field_series fs" in sql:
                now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                return FakeResult([
                    {
                        "series_id": sid,
                        "instrument_unit_id": 5,
                        "filter_name": "R",
                        "n_frames": 20,
                        "time_start": now,
                        "time_end": now,
                        "calibration_version_id": 100 + sid,
                        "calibration_name": f"science_{sid}",
                        "catalog_name": "shared_catalog",
                    }
                    for sid in (1, 2, 3)
                ])
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return FakeResult([
                    {
                        "calibration_version_id": 100 + sid,
                        "n_science_points": 500,
                        "n_science_frames": 20,
                        "n_objects": 25,
                    }
                    for sid in (1, 2, 3)
                ])
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                return FakeResult([
                    {"calibration_version_id": 100 + sid, "object_id": obj, "n_points": 20}
                    for sid in (1, 2, 3)
                    for obj in range(1, 26)
                ])
            if "WITH valid AS" in sql and "percentile_cont" in sql:
                cv = int(params["calibration_version_id"])
                sid = cv - 100
                shifts = {1: 0.0, 2: 0.2, 3: -0.1}
                return FakeResult([
                    {
                        "object_id": obj,
                        "n_points": 20,
                        "mag_median": 12.0 + obj * 0.01 + shifts[sid],
                        "mag_mad": 0.01,
                        "mag_err_median": 0.01,
                    }
                    for obj in range(1, 26)
                ])
            if "WITH RECURSIVE chain" in sql:
                return FakeResult([{"object_id": obj} for obj in range(1, 26)])
            if "FROM object_qc" in sql:
                return FakeResult([{"object_id": obj, "flags": {}} for obj in range(1, 26)])
            raise AssertionError(f"Unexpected SQL: {sql}")

    result = analyze_interseries_calibration(
        FakeSession(),
        field_id=9,
        filter_name="R",
        min_points_per_object=5,
        min_common_objects=20,
    )
    assert result.status == "READY_M1_M3"
    assert result.selected_series_ids == (1, 2, 3)
    assert result.reference_series_id == 1
    assert len(result.series_summaries) == 3
    assert all(row.n_reference_eligible == 25 for row in result.series_summaries)
    pairs = {(row.left_series_id, row.right_series_id): row.delta_mag for row in result.pairwise_offsets}
    assert math.isclose(pairs[(1, 2)] or 0.0, 0.2, abs_tol=1e-12)
    assert math.isclose(pairs[(1, 3)] or 0.0, -0.1, abs_tol=1e-12)
    assert math.isclose(pairs[(2, 3)] or 0.0, -0.3, abs_tol=1e-12)
    assert result.graph_solution is not None
    assert result.graph_solution.status == "OK"


def test_component_selection_refuses_ambiguous_multiple_components() -> None:
    import pytest
    from grandgousier.interseries.analysis import _select_analysis_component
    from grandgousier.interseries.planning import InterseriesPlan

    plan = InterseriesPlan(
        field_id=1,
        field_name="F",
        min_points_per_object=5,
        min_common_objects=20,
        series=[],
        pairs=[],
        connected_components=[[1, 2], [3, 4]],
        recommended_reference_series_id=1,
        status="READY_FOR_PAIRWISE_MVP",
    )
    with pytest.raises(ValueError, match="Multiple independent eligible"):
        _select_analysis_component(plan, reference_series_id=None)
    assert _select_analysis_component(plan, reference_series_id=3) == ([3, 4], 3)
