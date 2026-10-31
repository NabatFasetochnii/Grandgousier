from __future__ import annotations

from types import SimpleNamespace

from grandgousier.services import operations


class _Result:
    def __init__(self, *, first=None, all_rows=None):
        self._first = first
        self._all = list(all_rows or [])

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class _Session:
    def __init__(self, field, generations):
        self.field = field
        self.generations = generations
        self.calls = 0

    def execute(self, statement, params=None):
        self.calls += 1
        if self.calls == 1:
            return _Result(first=self.field)
        return _Result(all_rows=self.generations)


def _candidate(series_id, filter_name, *, status="READY", compatibility="READY_STAGE3_RECIPE"):
    instrument_unit_id = 7 if filter_name == "V" else 8
    return SimpleNamespace(
        series_id=series_id,
        filter_name=filter_name,
        instrument_unit_id=instrument_unit_id,
        status=status,
        recipe_compatibility=compatibility,
        to_dict=lambda: {
            "series_id": series_id,
            "filter_name": filter_name,
            "instrument_unit_id": instrument_unit_id,
            "status": status,
            "recipe_compatibility": compatibility,
            "calibration_version_id": 1000 + series_id,
            "recipe_compatibility_notes": [],
        },
    )


def test_build_field_status_reports_ready_when_latest_m4_matches_current_scope(monkeypatch):
    series = [_candidate(88, "V"), _candidate(92, "V"), _candidate(89, "R"), _candidate(93, "R")]
    plan = SimpleNamespace(
        series=series,
        status="READY_FOR_GRAPH_VALIDATION",
        n_series=4,
        n_ready_series=4,
        n_eligible_pairs=2,
        largest_component_size=2,
        recommended_reference_series_id=88,
        connected_components=[[88, 92], [89, 93]],
    )
    monkeypatch.setattr(operations, "build_interseries_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        operations,
        "verify_interseries_generation",
        lambda session, generation_id: SimpleNamespace(status="OK"),
    )
    monkeypatch.setattr(
        operations,
        "load_interseries_generation_series",
        lambda session, generation_id: (
            [{"series_id": 88}, {"series_id": 92}]
            if generation_id == 20
            else [{"series_id": 89}, {"series_id": 93}]
        ),
    )
    session = _Session(
        {
            "field_id": 33,
            "field_uid": "f33",
            "field_name": "GPX_TF17_2",
            "primary_object_name": "GPX_TF17_2",
            "ra_center_deg": 1.0,
            "dec_center_deg": 2.0,
            "radius_arcmin": 30.0,
            "reference_catalog_name": "shared",
            "status": "PROCESSED",
        },
        [
            {
                "interseries_generation_id": 20,
                "filter_name": "V",
                "instrument_unit_id": 7,
                "catalog_name": "shared",
                "reference_series_id": 88,
                "reliability_status": "REDUNDANT_GRAPH_OK",
                "n_series": 2,
                "n_edges": 1,
                "graph_dof": 0,
                "reduced_chi2": 1.0,
            },
            {
                "interseries_generation_id": 21,
                "filter_name": "R",
                "instrument_unit_id": 8,
                "catalog_name": "shared",
                "reference_series_id": 89,
                "reliability_status": "REDUNDANT_GRAPH_OK",
                "n_series": 2,
                "n_edges": 1,
                "graph_dof": 0,
                "reduced_chi2": 2.0,
            },
        ],
    )
    result = operations.build_field_status(session, field_id=33, catalog_name="shared")
    assert result.status == "READY"
    assert result.ready
    assert result.stage2["recipe_compatibility_counts"] == {"READY_STAGE3_RECIPE": 4}
    assert all(row["source_scope_matches"] for row in result.stage3)


def test_build_field_status_marks_stage3_stale_when_scope_differs(monkeypatch):
    series = [_candidate(88, "V"), _candidate(92, "V")]
    plan = SimpleNamespace(
        series=series,
        status="READY_FOR_GRAPH_VALIDATION",
        n_series=2,
        n_ready_series=2,
        n_eligible_pairs=1,
        largest_component_size=2,
        recommended_reference_series_id=88,
        connected_components=[[88, 92]],
    )
    monkeypatch.setattr(operations, "build_interseries_plan", lambda *a, **k: plan)
    monkeypatch.setattr(operations, "verify_interseries_generation", lambda *a, **k: SimpleNamespace(status="OK"))
    monkeypatch.setattr(operations, "load_interseries_generation_series", lambda *a, **k: [{"series_id": 88}])
    session = _Session(
        {"field_id": 33, "field_name": "x"},
        [{
            "interseries_generation_id": 20,
            "filter_name": "V",
            "instrument_unit_id": 7,
            "catalog_name": "shared",
            "reference_series_id": 88,
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "n_series": 1,
            "n_edges": 1,
            "graph_dof": 0,
            "reduced_chi2": 1.0,
        }],
    )
    result = operations.build_field_status(session, field_id=33)
    assert result.status == "STAGE3_STALE_OR_MISSING"
    assert not result.stage3[0]["source_scope_matches"]


def test_build_field_status_marks_orphaned_m4_stale_without_int_null(monkeypatch):
    plan = SimpleNamespace(
        series=[_candidate(88, "V"), _candidate(92, "V")],
        status="READY_FOR_GRAPH_VALIDATION", n_series=2, n_ready_series=2,
        n_eligible_pairs=1, largest_component_size=2,
        recommended_reference_series_id=88, connected_components=[[88, 92]],
    )
    monkeypatch.setattr(operations, "build_interseries_plan", lambda *a, **k: plan)
    monkeypatch.setattr(operations, "verify_interseries_generation",
                        lambda *a, **k: SimpleNamespace(status="FAILED"))
    monkeypatch.setattr(operations, "load_interseries_generation_series",
                        lambda *a, **k: [{"series_id": 88}, {"series_id": None}])
    generation = {
        "interseries_generation_id": 20, "filter_name": "V", "instrument_unit_id": 7,
        "catalog_name": "shared", "reference_series_id": 88,
        "reliability_status": "REDUNDANT_GRAPH_OK", "n_series": 2, "n_edges": 1,
        "graph_dof": 0, "reduced_chi2": 0.0,
    }
    result = operations.build_field_status(
        _Session({"field_id": 33, "field_name": "x"}, [generation]), field_id=33,
    )
    assert result.status == "STAGE3_STALE_OR_MISSING"
    assert result.stage3[0]["source_scope_matches"] is False


def test_build_field_status_keeps_same_filter_instrument_scopes_separate(monkeypatch):
    series = [
        SimpleNamespace(
            series_id=101,
            filter_name="V",
            instrument_unit_id=7,
            status="READY",
            recipe_compatibility="READY_STAGE3_RECIPE",
            to_dict=lambda: {
                "series_id": 101,
                "filter_name": "V",
                "instrument_unit_id": 7,
                "status": "READY",
                "recipe_compatibility": "READY_STAGE3_RECIPE",
            },
        ),
        SimpleNamespace(
            series_id=102,
            filter_name="V",
            instrument_unit_id=7,
            status="READY",
            recipe_compatibility="READY_STAGE3_RECIPE",
            to_dict=lambda: {
                "series_id": 102,
                "filter_name": "V",
                "instrument_unit_id": 7,
                "status": "READY",
                "recipe_compatibility": "READY_STAGE3_RECIPE",
            },
        ),
        SimpleNamespace(
            series_id=201,
            filter_name="V",
            instrument_unit_id=9,
            status="READY",
            recipe_compatibility="READY_STAGE3_RECIPE",
            to_dict=lambda: {
                "series_id": 201,
                "filter_name": "V",
                "instrument_unit_id": 9,
                "status": "READY",
                "recipe_compatibility": "READY_STAGE3_RECIPE",
            },
        ),
        SimpleNamespace(
            series_id=202,
            filter_name="V",
            instrument_unit_id=9,
            status="READY",
            recipe_compatibility="READY_STAGE3_RECIPE",
            to_dict=lambda: {
                "series_id": 202,
                "filter_name": "V",
                "instrument_unit_id": 9,
                "status": "READY",
                "recipe_compatibility": "READY_STAGE3_RECIPE",
            },
        ),
    ]
    plan = SimpleNamespace(
        series=series,
        status="READY_FOR_GRAPH_VALIDATION",
        n_series=4,
        n_ready_series=4,
        n_eligible_pairs=2,
        largest_component_size=2,
        recommended_reference_series_id=101,
        connected_components=[[101, 102], [201, 202]],
    )
    monkeypatch.setattr(operations, "build_interseries_plan", lambda *a, **k: plan)
    monkeypatch.setattr(operations, "verify_interseries_generation", lambda *a, **k: SimpleNamespace(status="OK"))

    def generation_series(session, generation_id):
        if generation_id == 31:
            return [{"series_id": 101}, {"series_id": 102}]
        return [{"series_id": 201}, {"series_id": 202}]

    monkeypatch.setattr(operations, "load_interseries_generation_series", generation_series)
    session = _Session(
        {"field_id": 44, "field_name": "two-instrument"},
        [
            {
                "interseries_generation_id": 31,
                "filter_name": "V",
                "instrument_unit_id": 7,
                "catalog_name": "shared",
                "reference_series_id": 101,
                "reliability_status": "REDUNDANT_GRAPH_OK",
                "n_series": 2,
                "n_edges": 1,
                "graph_dof": 0,
                "reduced_chi2": 1.0,
            },
            {
                "interseries_generation_id": 32,
                "filter_name": "V",
                "instrument_unit_id": 9,
                "catalog_name": "shared",
                "reference_series_id": 201,
                "reliability_status": "REDUNDANT_GRAPH_OK",
                "n_series": 2,
                "n_edges": 1,
                "graph_dof": 0,
                "reduced_chi2": 1.0,
            },
        ],
    )
    result = operations.build_field_status(session, field_id=44, catalog_name="shared")
    assert result.status == "READY"
    assert {(row["filter_name"], row["instrument_unit_id"]) for row in result.stage3} == {
        ("V", 7),
        ("V", 9),
    }
    assert all(row["source_scope_matches"] for row in result.stage3)
