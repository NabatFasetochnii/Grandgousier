from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.interseries.persistence import (
    RELIABILITY_REDUNDANT,
    RELIABILITY_SPARSE,
    build_generation_input_snapshot,
    classify_graph_reliability,
    generation_fingerprint,
)

STAGE2_RECIPE = "stage3_scale_v1"


def _analysis_for_fingerprint(*, reference_series_id: int = 77, second_cv: int = 976):
    summaries = (
        SimpleNamespace(series_id=77, calibration_version_id=967, instrument_unit_id=8, filter_name="R", catalog_name="shared"),
        SimpleNamespace(series_id=80, calibration_version_id=second_cv, instrument_unit_id=8, filter_name="R", catalog_name="shared"),
        SimpleNamespace(series_id=81, calibration_version_id=986, instrument_unit_id=8, filter_name="R", catalog_name="shared"),
    )
    return SimpleNamespace(
        algorithm="interseries_graph_median_v1",
        sign_convention="delta_ab=m_b-m_a; z_a-z_b=delta_ab; m_global=m_science+z_s",
        field_id=33,
        reference_series_id=reference_series_id,
        min_points_per_object=5,
        min_common_objects=20,
        clip_sigma=4.0,
        max_clip_iterations=5,
        selected_series_ids=(77, 80, 81),
        series_summaries=summaries,
    )


def test_generation_fingerprint_is_stable_and_science_identity_sensitive() -> None:
    first = build_generation_input_snapshot(_analysis_for_fingerprint())
    same = build_generation_input_snapshot(_analysis_for_fingerprint())
    changed_science = build_generation_input_snapshot(_analysis_for_fingerprint(second_cv=999))
    changed_gauge = build_generation_input_snapshot(_analysis_for_fingerprint(reference_series_id=80))

    assert generation_fingerprint(first) == generation_fingerprint(same)
    assert generation_fingerprint(first) != generation_fingerprint(changed_science)
    assert generation_fingerprint(first) != generation_fingerprint(changed_gauge)
    assert first["sources"] == [
        {"series_id": 77, "calibration_version_id": 967},
        {"series_id": 80, "calibration_version_id": 976},
        {"series_id": 81, "calibration_version_id": 986},
    ]


def test_graph_reliability_distinguishes_triangle_from_tree() -> None:
    assert classify_graph_reliability(n_series=3, n_edges=3, dof=1, graph_status="OK") == RELIABILITY_REDUNDANT
    assert classify_graph_reliability(n_series=3, n_edges=2, dof=0, graph_status="OK") == RELIABILITY_SPARSE
    assert classify_graph_reliability(n_series=3, n_edges=1, dof=0, graph_status="OK") == "DISCONNECTED"


def test_gpx_tf17_m4_baseline_fixture_preserves_validated_reliability_contract() -> None:
    path = Path(__file__).parent / "fixtures" / "interseries_gpx_tf17_m4_baseline.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    r = payload["R"]
    v = payload["V"]
    assert r["selected_series_ids"] == [77, 80, 81]
    assert r["source_calibration_version_ids"] == [967, 976, 986]
    assert r["graph_dof"] == 1
    assert r["expected_reliability"] == RELIABILITY_REDUNDANT
    assert v["selected_series_ids"] == [76, 78, 79]
    assert v["source_calibration_version_ids"] == [960, 991, 981]
    assert v["min_common_objects"] == 15
    assert v["graph_dof"] == 0
    assert v["expected_reliability"] == RELIABILITY_SPARSE


def test_persist_sparse_graph_requires_explicit_acknowledgement(monkeypatch) -> None:
    from grandgousier.interseries import persistence

    graph = SimpleNamespace(status="OK", n_series=3, n_edges=2, dof=0, offsets=(1, 2, 3))
    analysis = SimpleNamespace(status="READY_M1_M3", graph_solution=graph, selected_series_ids=(76, 78, 79))
    monkeypatch.setattr(persistence, "analyze_interseries_calibration", lambda *args, **kwargs: analysis)

    with pytest.raises(ValueError, match="SPARSE_GRAPH_NO_CLOSURE"):
        persistence.persist_interseries_calibration(
            object(),
            field_id=33,
            allow_sparse_graph=False,
            required_stage2_recipe=STAGE2_RECIPE,
        )


def test_persist_without_required_recipe_refuses_before_analysis_or_database_calls(
    monkeypatch,
) -> None:
    from grandgousier.interseries import persistence

    def unexpected_analysis(*args, **kwargs):  # pragma: no cover
        raise AssertionError("recipe guard must run before analysis")

    monkeypatch.setattr(persistence, "analyze_interseries_calibration", unexpected_analysis)

    class NoDatabaseOperations:
        def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("recipe guard must run before database operations")

    with pytest.raises(ValueError, match="required_stage2_recipe is required"):
        persistence.persist_interseries_calibration(NoDatabaseOperations(), field_id=33)


def test_stage3_m4_cli_contract() -> None:
    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "persist-interseries-calibration",
        "--field-id", "33",
        "--filter", "V",
        "--reference-series-id", "76",
        "--min-common-objects", "15",
        "--required-stage2-recipe", STAGE2_RECIPE,
        "--allow-sparse-graph",
    ])
    assert args.command == "persist-interseries-calibration"
    assert args.field_id == 33
    assert args.filter_name == "V"
    assert args.reference_series_id == 76
    assert args.min_common_objects == 15
    assert args.required_stage2_recipe == STAGE2_RECIPE
    assert args.allow_sparse_graph is True

    show = parser.parse_args(["show-interseries-calibration", "--generation-id", "4"])
    assert show.generation_id == 4
    verify = parser.parse_args(["verify-interseries-calibration", "--generation-id", "4"])
    assert verify.generation_id == 4


def _minimal_redundant_analysis():
    from grandgousier.interseries.analysis import (
        GraphEdgeDiagnostic,
        GraphSeriesOffset,
        GraphSolution,
        PairCalibrator,
        PairwiseOffset,
        SeriesObjectState,
        SeriesSummary,
    )

    summaries = tuple(
        SeriesSummary(
            series_id=sid,
            instrument_unit_id=8,
            filter_name="R",
            catalog_name="shared",
            calibration_version_id=cv,
            calibration_name=f"science_{sid}",
            n_frames=10,
            n_science_points=100,
            n_objects=10,
            n_state_objects=10,
            n_ensemble_references=10,
            n_reference_eligible=10,
            median_points_per_object=10.0,
            median_within_series_robust_sigma_mag=0.01,
            reference_source="NEAREST_FINAL_ENSEMBLE",
            time_start=None,
            time_end=None,
        )
        for sid, cv in ((77, 967), (80, 976), (81, 986))
    )
    states = tuple(
        SeriesObjectState(
            series_id=sid,
            calibration_version_id=cv,
            object_id=obj,
            n_points=10,
            mag_median=12.0 + 0.01 * obj,
            mag_mad=0.01,
            robust_sigma_mag=0.014826,
            mag_err_median=0.01,
            representative_err_mag=0.003,
            ensemble_reference=True,
            object_qc_flags=(),
            reference_eligible=True,
        )
        for sid, cv in ((77, 967), (80, 976), (81, 986))
        for obj in (1, 2, 3)
    )
    pairs = tuple(
        PairwiseOffset(
            left_series_id=a,
            right_series_id=b,
            delta_mag=delta,
            offset_err_mag=0.01,
            scatter_mag=0.01,
            n_initial_calibrators=3,
            n_used_calibrators=3,
            n_rejected_calibrators=0,
            clip_iterations=1,
            status="OK",
            calibrators=tuple(
                PairCalibrator(a, b, obj, 12.0, 12.0 + delta, delta, 0.01, 0.0, True)
                for obj in (1, 2, 3)
            ),
        )
        # Keep one valid analysis edge in non-canonical planner orientation to
        # reproduce the real GPX case (92,78) that M4 must normalize for storage.
        for a, b, delta in ((77, 80, -0.01), (77, 81, -0.02), (81, 80, 0.012))
    )
    offsets = (
        GraphSeriesOffset(77, 0.0, 0.0, True, 2),
        GraphSeriesOffset(80, 0.01, 0.01, False, 2),
        GraphSeriesOffset(81, 0.02, 0.01, False, 2),
    )
    edges = (
        GraphEdgeDiagnostic(77, 80, -0.01, 0.01, -0.01, 0.0, 0.0),
        GraphEdgeDiagnostic(77, 81, -0.02, 0.01, -0.02, 0.0, 0.0),
        # Non-canonical orientation with non-zero closure diagnostics proves
        # that persistence flips every signed quantity, not only delta_mag.
        GraphEdgeDiagnostic(81, 80, 0.012, 0.01, 0.01, -0.002, -0.2),
    )
    graph = GraphSolution(77, "OK", 3, 3, 1, 0.0, 0.0, 0.0, offsets, edges)

    class FakeAnalysis(SimpleNamespace):
        def to_dict(self, **kwargs):
            return {"field_id": self.field_id, "status": self.status, "selected_series_ids": list(self.selected_series_ids)}

    return FakeAnalysis(
        field_id=33,
        field_name="GPX_TF17_2",
        algorithm="interseries_graph_median_v1",
        sign_convention="delta_ab=m_b-m_a; z_a-z_b=delta_ab; m_global=m_science+z_s",
        selected_series_ids=(77, 80, 81),
        reference_series_id=77,
        status="READY_M1_M3",
        min_points_per_object=5,
        min_common_objects=20,
        clip_sigma=4.0,
        max_clip_iterations=5,
        series_summaries=summaries,
        object_states=states,
        pairwise_offsets=pairs,
        graph_solution=graph,
    )


class _MappingsResult:
    def __init__(self, rows):
        self.rows = list(rows)
    def mappings(self):
        return self
    def first(self):
        return self.rows[0] if self.rows else None
    def all(self):
        return list(self.rows)


def test_persist_redundant_generation_writes_exact_sources_and_edges(monkeypatch) -> None:
    from grandgousier.interseries import persistence

    analysis = _minimal_redundant_analysis()
    captured_analysis_kwargs = {}

    def fake_analyze(*args, **kwargs):
        captured_analysis_kwargs.update(kwargs)
        return analysis

    monkeypatch.setattr(persistence, "analyze_interseries_calibration", fake_analyze)

    class FakeSession:
        def __init__(self):
            self.series_params = None
            self.edge_params = None
            self.insert_order = []
        def execute(self, statement, params=None):
            sql = str(statement)
            if "INSERT INTO interseries_calibration_generations" in sql:
                self.insert_order.append("generation")
                assert params["reference_series_id"] == 77
                return _MappingsResult([{"interseries_generation_id": 42}])
            if "INSERT INTO interseries_calibration_series" in sql:
                self.insert_order.append("members")
                assert "interseries_generation_id, field_id, series_id" in sql
                self.series_params = params
                return _MappingsResult([])
            if "INSERT INTO interseries_calibration_edges" in sql:
                self.insert_order.append("edges")
                self.edge_params = params
                return _MappingsResult([])
            if "FROM interseries_calibration_generations g" in sql and "JOIN fields" in sql:
                return _MappingsResult([{
                    "interseries_generation_id": 42,
                    "generation_uid": "m4_test",
                    "fingerprint_sha256": "f" * 64,
                    "field_id": 33,
                    "field_name": "GPX_TF17_2",
                    "instrument_unit_id": 8,
                    "filter_name": "R",
                    "catalog_name": "shared",
                    "analysis_algorithm": "interseries_graph_median_v1",
                    "reference_series_id": 77,
                    "analysis_status": "READY_M1_M3",
                    "graph_status": "OK",
                    "reliability_status": RELIABILITY_REDUNDANT,
                    "n_series": 3,
                    "n_edges": 3,
                    "graph_dof": 1,
                    "chi2": 0.0,
                    "reduced_chi2": 0.0,
                    "max_abs_standardized_residual": 0.0,
                    "min_points_per_object": 5,
                    "min_common_objects": 20,
                    "clip_sigma": 4.0,
                    "max_clip_iterations": 5,
                    "lifecycle_status": "COMPLETE_IMMUTABLE",
                }])
            raise AssertionError(sql)

    session = FakeSession()
    result = persistence.persist_interseries_calibration(
        session,
        field_id=33,
        required_stage2_recipe=STAGE2_RECIPE,
        science_calibration_version_ids=[967, 976, 986],
    )
    assert result.created is True
    assert session.insert_order == ["generation", "members", "edges"]
    assert [row["is_reference_series"] for row in session.series_params] == [True, False, False]
    assert result.reliability_status == RELIABILITY_REDUNDANT
    assert captured_analysis_kwargs["required_stage2_recipe"] == STAGE2_RECIPE
    assert captured_analysis_kwargs["science_calibration_version_ids"] == [967, 976, 986]
    assert [row["science_calibration_version_id"] for row in session.series_params] == [967, 976, 986]
    assert {row["field_id"] for row in session.series_params} == {33}
    assert len(session.edge_params) == 3
    assert all(row["left_series_id"] < row["right_series_id"] for row in session.edge_params)
    canonical_80_81 = next(
        row for row in session.edge_params
        if row["left_series_id"] == 80 and row["right_series_id"] == 81
    )
    assert canonical_80_81["measured_delta_mag"] == pytest.approx(-0.012)
    assert canonical_80_81["predicted_delta_mag"] == pytest.approx(-0.01)
    assert canonical_80_81["closure_residual_mag"] == pytest.approx(0.002)
    assert canonical_80_81["standardized_residual"] == pytest.approx(0.2)
    assert all(json.loads(row["common_reference_object_ids"]) == [1, 2, 3] for row in session.edge_params)
    assert all(json.loads(row["used_object_ids"]) == [1, 2, 3] for row in session.edge_params)


def test_persist_is_idempotent_for_existing_fingerprint(monkeypatch) -> None:
    from grandgousier.interseries import persistence

    analysis = _minimal_redundant_analysis()
    monkeypatch.setattr(persistence, "analyze_interseries_calibration", lambda *args, **kwargs: analysis)
    snapshot = persistence.build_generation_input_snapshot(analysis)
    fingerprint = persistence.generation_fingerprint(snapshot)

    class FakeSession:
        child_inserts = 0
        def execute(self, statement, params=None):
            sql = str(statement)
            if "INSERT INTO interseries_calibration_generations" in sql:
                return _MappingsResult([])
            if "SELECT interseries_generation_id, input_snapshot" in sql:
                return _MappingsResult([{"interseries_generation_id": 42, "input_snapshot": snapshot}])
            if "INSERT INTO interseries_calibration_series" in sql or "INSERT INTO interseries_calibration_edges" in sql:
                self.child_inserts += 1
                return _MappingsResult([])
            if "FROM interseries_calibration_generations g" in sql and "JOIN fields" in sql:
                return _MappingsResult([{
                    "interseries_generation_id": 42,
                    "generation_uid": f"m4_{fingerprint}",
                    "fingerprint_sha256": fingerprint,
                    "field_id": 33,
                    "field_name": "GPX_TF17_2",
                    "instrument_unit_id": 8,
                    "filter_name": "R",
                    "catalog_name": "shared",
                    "analysis_algorithm": "interseries_graph_median_v1",
                    "reference_series_id": 77,
                    "analysis_status": "READY_M1_M3",
                    "graph_status": "OK",
                    "reliability_status": RELIABILITY_REDUNDANT,
                    "n_series": 3,
                    "n_edges": 3,
                    "graph_dof": 1,
                    "chi2": 0.0,
                    "reduced_chi2": 0.0,
                    "max_abs_standardized_residual": 0.0,
                    "min_points_per_object": 5,
                    "min_common_objects": 20,
                    "clip_sigma": 4.0,
                    "max_clip_iterations": 5,
                    "lifecycle_status": "COMPLETE_IMMUTABLE",
                }])
            raise AssertionError(sql)

    session = FakeSession()
    result = persistence.persist_interseries_calibration(
        session, field_id=33, required_stage2_recipe=STAGE2_RECIPE
    )
    assert result.created is False
    assert result.interseries_generation_id == 42
    assert session.child_inserts == 0


def test_verify_orphaned_m4_membership_reports_failure_without_converting_null(monkeypatch) -> None:
    from grandgousier.interseries import persistence

    class FakeSession:
        def execute(self, statement, _params):
            assert "SELECT g.*, f.field_name" in str(statement)
            return _MappingsResult([{
                "generation_uid": "m4_orphan",
                "input_snapshot": {},
                "fingerprint_sha256": persistence.generation_fingerprint({}),
                "reliability_status": RELIABILITY_REDUNDANT,
            }])

    monkeypatch.setattr(persistence, "load_interseries_generation_series", lambda *_args, **_kwargs: [
        {"field_id": 33, "series_id": None, "science_calibration_version_id": 967}
    ])
    result = persistence.verify_interseries_generation(FakeSession(), generation_id=42)
    assert result.status == "FAILED"
    assert result.source_calibrations_ok is False and result.graph_ok is False
    assert result.details == {
        "failure_reason": "ORPHAN_M4_SERIES",
        "orphan_memberships": [{"field_id": 33, "science_calibration_version_id": 967}],
    }


@pytest.mark.parametrize("cv_series_id", [77, 80])
def test_verify_refuses_stale_cv_or_missing_edge_endpoint(monkeypatch, cv_series_id) -> None:
    from grandgousier.interseries import persistence

    snapshot = {"sources": [{"series_id": 77, "calibration_version_id": 967}]}
    generation = {
        "generation_uid": "m4_test", "input_snapshot": snapshot,
        "fingerprint_sha256": persistence.generation_fingerprint(snapshot),
        "field_id": 33, "reference_series_id": 77,
        "n_series": 1, "n_edges": 1, "graph_dof": 0, "chi2": 0.0,
        "max_abs_standardized_residual": None, "reduced_chi2": None,
        "reliability_status": RELIABILITY_REDUNDANT,
    }
    member = {
        "field_id": 33, "series_id": 77, "science_calibration_version_id": 967,
        "is_reference_series": True, "offset_mag": 0.0, "n_science_points": 1,
    }
    edge = {"left_series_id": 77, "right_series_id": 80, "pair_status": "OK"}
    monkeypatch.setattr(persistence, "load_interseries_generation_series",
                        lambda *_args, **_kwargs: [member])
    monkeypatch.setattr(persistence, "load_interseries_generation_edges",
                        lambda *_args, **_kwargs: [edge])

    class FakeSession:
        def execute(self, statement, _params):
            sql = str(statement)
            if "SELECT g.*, f.field_name" in sql:
                return _MappingsResult([generation])
            if "FROM calibration_versions cv" in sql:
                return _MappingsResult([{
                    "series_id": cv_series_id, "field_id": 33, "product_role": "SCIENCE",
                    "lifecycle_status": "COMPLETE_IMMUTABLE", "n_science_rows": 1,
                }])
            raise AssertionError(sql)

    result = persistence.verify_interseries_generation(FakeSession(), generation_id=42)
    assert result.status == "FAILED" and result.graph_ok is False
    assert result.details["edge_checks"] == [
        {"left_series_id": 77, "right_series_id": 80, "ok": False}
    ]
    if cv_series_id != 77:
        assert result.source_calibrations_ok is False
