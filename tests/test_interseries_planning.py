from __future__ import annotations

import pytest

from grandgousier.interseries.planning import (
    InterseriesSeriesCandidate,
    _build_pair_candidates,
    _choose_reference_series,
    _eligible_connected_components,
    _plan_status,
)


def _science_head(
    series_id: int,
    calibration_version_id: int,
    *,
    field_id: int = 7,
    instrument_unit_id: int = 10,
    filter_name: str = "V",
    catalog_name: str = "catalog_a",
    logical_name: str | None = None,
    **extra,
) -> dict:
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "series_id": series_id,
        "series_field_id": field_id,
        "series_instrument_unit_id": instrument_unit_id,
        "filter_name": filter_name,
        "n_frames": 50,
        "time_start": now,
        "time_end": now,
        "alias_id": calibration_version_id + 1000,
        "alias_logical_name": logical_name or f"science_{series_id}",
        "calibration_version_id": calibration_version_id,
        "calibration_name": logical_name or f"science_{series_id}",
        "calibration_field_id": field_id,
        "calibration_series_id": series_id,
        "calibration_product_role": "SCIENCE",
        "calibration_instrument_unit_id": instrument_unit_id,
        "catalog_name": catalog_name,
        "pipeline_run_id": 2000 + series_id,
        "pipeline_status": "DONE",
        "recipe_json": None,
        "has_science_alias_head": True,
        "has_legacy_science_products": False,
        # Deliberately irrelevant to alias resolution.
        "finalized_at": now,
        "generation_no": 1,
    }
    row.update(extra)
    return row


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _AliasHeadSession:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = dict(params)
        return _FakeResult(self.rows)


def _series(
    series_id: int,
    calibration_version_id: int,
    *,
    instrument_unit_id: int = 10,
    filter_name: str = "V",
    catalog_name: str = "gaia_master",
    n_usable_objects: int = 100,
    n_science_points: int = 1000,
) -> InterseriesSeriesCandidate:
    return InterseriesSeriesCandidate(
        series_id=series_id,
        instrument_unit_id=instrument_unit_id,
        filter_name=filter_name,
        catalog_name=catalog_name,
        n_frames=50,
        calibration_version_id=calibration_version_id,
        calibration_name=f"science_{series_id}",
        n_science_points=n_science_points,
        n_science_frames=50,
        n_objects=n_usable_objects,
        n_usable_objects=n_usable_objects,
        status="READY",
    )


def test_two_compatible_series_are_ready_for_pairwise_mvp() -> None:
    series = [_series(1, 101), _series(2, 102)]
    usable = {
        101: set(range(1, 101)),
        102: set(range(31, 131)),
    }
    pairs = _build_pair_candidates(series, usable_by_calibration=usable, min_common_objects=20)
    components = _eligible_connected_components(series, pairs)

    assert len(pairs) == 1
    assert pairs[0].n_common_objects == 70
    assert pairs[0].eligible is True
    assert pairs[0].reason == "OK"
    assert components == [[1, 2]]
    assert _plan_status(series, pairs, components) == "READY_FOR_PAIRWISE_MVP"
    assert _choose_reference_series(series, components) == 1


def test_three_series_connected_by_pairwise_overlap_enable_graph_validation() -> None:
    series = [
        _series(1, 101, n_usable_objects=90),
        _series(2, 102, n_usable_objects=120),
        _series(3, 103, n_usable_objects=80),
    ]
    usable = {
        101: set(range(1, 81)),
        102: set(range(41, 141)),
        103: set(range(101, 181)),
    }
    pairs = _build_pair_candidates(series, usable_by_calibration=usable, min_common_objects=20)
    components = _eligible_connected_components(series, pairs)

    assert [(p.left_series_id, p.right_series_id, p.eligible) for p in pairs] == [
        (1, 2, True),
        (1, 3, False),
        (2, 3, True),
    ]
    assert components == [[1, 2, 3]]
    assert _plan_status(series, pairs, components) == "READY_FOR_GRAPH_VALIDATION"
    assert _choose_reference_series(series, components) == 2


def test_filter_or_catalog_mismatch_blocks_direct_stage3_pair() -> None:
    series = [
        _series(1, 101, filter_name="V", catalog_name="catalog_a"),
        _series(2, 102, filter_name="R", catalog_name="catalog_b"),
    ]
    usable = {101: set(range(100)), 102: set(range(100))}
    pairs = _build_pair_candidates(series, usable_by_calibration=usable, min_common_objects=20)
    components = _eligible_connected_components(series, pairs)

    assert pairs[0].eligible is False
    assert "FILTER_MISMATCH" in pairs[0].reason
    assert "CATALOG_MISMATCH" in pairs[0].reason
    assert components == [[1], [2]]
    assert _plan_status(series, pairs, components) == "NO_ELIGIBLE_SERIES_OVERLAP"


def test_stage3_planner_cli_contract() -> None:
    import pytest

    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "plan-interseries-calibration",
            "--field-id",
            "7",
            "--filter",
            "V",
            "--min-points-per-object",
            "4",
            "--min-common-objects",
            "25",
            "--science-calibration-version-id",
            "101",
            "--science-calibration-version-id",
            "102",
            "--format",
            "json",
        ]
    )

    assert args.command == "plan-interseries-calibration"
    assert args.field_id == 7
    assert args.field_name is None
    assert args.filter_name == "V"
    assert args.min_points_per_object == 4
    assert args.min_common_objects == 25
    assert args.science_calibration_version_id == [101, 102]
    assert args.format == "json"


def test_build_interseries_plan_from_database_rows() -> None:
    from grandgousier.interseries.planning import build_interseries_plan

    class FakeMappings:
        def __init__(self, rows):
            self._rows = rows

        def first(self):
            return self._rows[0] if self._rows else None

        def all(self):
            return list(self._rows)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return FakeMappings(self._rows)

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
                return FakeResult([{"field_id": 7, "field_name": "TEST_FIELD"}])
            if "FROM field_series fs" in sql:
                first = _science_head(1, 101)
                second = _science_head(2, 102)
                second["n_frames"] = 55
                return FakeResult([first, second])
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return FakeResult(
                    [
                        {
                            "calibration_version_id": 101,
                            "n_science_points": 500,
                            "n_science_frames": 50,
                            "n_objects": 50,
                        },
                        {
                            "calibration_version_id": 102,
                            "n_science_points": 550,
                            "n_science_frames": 55,
                            "n_objects": 50,
                        },
                    ]
                )
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                rows = []
                for cv_id, object_ids in ((101, range(1, 51)), (102, range(21, 71))):
                    rows.extend(
                        {
                            "calibration_version_id": cv_id,
                            "object_id": object_id,
                            "n_points": 10,
                        }
                        for object_id in object_ids
                    )
                return FakeResult(rows)
            raise AssertionError(f"Unexpected SQL: {sql}")

    plan = build_interseries_plan(
        FakeSession(),
        field_id=7,
        min_points_per_object=5,
        min_common_objects=20,
    )

    assert plan.field_name == "TEST_FIELD"
    assert plan.status == "READY_FOR_PAIRWISE_MVP"
    assert plan.n_ready_series == 2
    assert [row.calibration_version_id for row in plan.series] == [101, 102]
    assert plan.pairs[0].n_common_objects == 30
    assert plan.pairs[0].eligible is True
    assert plan.connected_components == [[1, 2]]


def test_stage3_science_selector_uses_alias_head_not_timestamp_or_generation_chronology() -> None:
    from grandgousier.interseries.planning import _load_series_candidates
    session = _AliasHeadSession(
        [
            _science_head(
                78,
                991,
                field_id=33,
                filter_name="V",
                catalog_name="shared_catalog",
                logical_name="current_logical_head",
                finalized_at="2020-01-01T00:00:00+00:00",
                generation_no=1,
            )
        ]
    )
    rows = _load_series_candidates(
        session,
        field_id=33,
        instrument_unit_id=None,
        filter_name="V",
        catalog_name="shared_catalog",
    )

    assert len(rows) == 1
    assert rows[0]["calibration_version_id"] == 991
    assert rows[0]["calibration_name"] == "current_logical_head"
    assert "calibration_product_aliases" in session.sql
    assert "head_calibration_version_id" in session.sql
    assert "ORDER BY cv.finalized_at" not in session.sql
    assert "cv.generation_no" not in session.sql
    assert "cv.materialized_at" not in session.sql
    assert "pr.catalog_name" in session.sql
    assert session.params == {"field_id": 33, "filter_name": "V"}


def test_stage3_reports_missing_materialization_on_selected_science_generation() -> None:
    from grandgousier.interseries.planning import build_interseries_plan

    class FakeMappings:
        def __init__(self, rows):
            self._rows = rows

        def first(self):
            return self._rows[0] if self._rows else None

        def all(self):
            return list(self._rows)

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return FakeMappings(self._rows)

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
                return FakeResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if "FROM field_series fs" in sql:
                return FakeResult(
                    [
                        _science_head(
                            78,
                            991,
                            field_id=33,
                            instrument_unit_id=7,
                            filter_name="V",
                            catalog_name="shared_catalog",
                            logical_name="science_photqc",
                            n_frames=29,
                        )
                    ]
                )
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return FakeResult([])
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                return FakeResult([])
            raise AssertionError(f"Unexpected SQL: {sql}")

    plan = build_interseries_plan(
        FakeSession(),
        field_id=33,
        filter_name="V",
        catalog_name="shared_catalog",
    )

    assert plan.n_series == 1
    assert plan.series[0].calibration_version_id == 991
    assert plan.series[0].status == "NO_SCIENCE_ROWS"


def test_required_stage2_recipe_excludes_incompatible_series_from_ready_graph() -> None:
    from grandgousier.interseries.planning import build_interseries_plan

    class FakeMappings:
        def __init__(self, rows): self._rows = rows
        def first(self): return self._rows[0] if self._rows else None
        def all(self): return list(self._rows)
    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return FakeMappings(self._rows)
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
                return FakeResult([{"field_id": 7, "field_name": "TEST_FIELD"}])
            if "FROM field_series fs" in sql:
                good = {
                    "recipe_version": "field_pipeline_v9",
                    "photometry_estimator": "adaptive_ellipse",
                    "aperture_correction_plane_policy": "evidence",
                    "working_catalog_mag_limit": 19.0,
                    "max_working_sources_per_frame": 2000,
                    "catalog_edge_margin_pix": 20.0,
                    "exclude_bad_pre_qc_frames": True,
                    "enable_psf_field_diagnostics": True,
                    "ensemble_min_ref_stars_per_frame": 30,
                    "exclude_candidates_from_ensemble": True,
                    "candidate_ensemble_until_converged": True,
                    "candidate_ensemble_convergence_max_iterations": 10,
                    "candidate_ensemble_growth_policy": "initial_only",
                    "enable_photometric_frame_qc": True,
                    "color_calibration_requested": True,
                    "enable_spatial_ensemble": False,
                    "quality_policy": {"detection_branch_enabled": True},
                }
                bad = dict(good)
                bad["photometry_estimator"] = "circular"
                bad["quality_policy"] = {"detection_branch_enabled": False}
                first = _science_head(1, 101)
                first["recipe_json"] = good
                second = _science_head(2, 102)
                second["recipe_json"] = bad
                return FakeResult([first, second])
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return FakeResult([
                    {"calibration_version_id": 101, "n_science_points": 500, "n_science_frames": 50, "n_objects": 50},
                    {"calibration_version_id": 102, "n_science_points": 500, "n_science_frames": 50, "n_objects": 50},
                ])
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                return FakeResult([
                    {"calibration_version_id": cv, "object_id": oid, "n_points": 10}
                    for cv in (101, 102) for oid in range(1, 51)
                ])
            raise AssertionError(sql)

    plan = build_interseries_plan(
        FakeSession(), field_id=7, min_points_per_object=5, min_common_objects=20,
        required_stage2_recipe="stage3_scale_v1",
    )
    by_id = {row.series_id: row for row in plan.series}
    assert by_id[1].status == "READY"
    assert by_id[1].recipe_compatibility == "READY_STAGE3_RECIPE"
    assert by_id[2].status == "RECIPE_MISMATCH"
    assert by_id[2].recipe_compatibility == "RECIPE_MISMATCH"
    assert plan.n_ready_series == 1
    assert plan.status == "NEED_MORE_READY_SERIES"


def test_one_alias_head_per_series_is_selected() -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    rows = _load_series_candidates(
        _AliasHeadSession([_science_head(1, 101), _science_head(2, 202)]),
        field_id=7,
        instrument_unit_id=None,
        filter_name=None,
        catalog_name=None,
    )

    assert [(row["series_id"], row["calibration_version_id"]) for row in rows] == [(1, 101), (2, 202)]


def test_legacy_science_without_alias_warns_and_skips_without_fallback() -> None:
    import pytest

    from grandgousier.interseries.planning import _load_series_candidates

    legacy_only = {
        "series_id": 1,
        "series_field_id": 7,
        "series_instrument_unit_id": 10,
        "filter_name": "V",
        "n_frames": 50,
        "has_science_alias_head": False,
        "has_legacy_science_products": True,
    }
    with pytest.warns(UserWarning, match="legacy SCIENCE products.*skipping"):
        rows = _load_series_candidates(
            _AliasHeadSession([legacy_only]),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
        )

    assert rows[0]["calibration_version_id"] is None
    assert rows[0]["_selection_status"] == "LEGACY_SCIENCE_SKIPPED"


def test_archive_generations_behind_live_alias_do_not_warn_as_legacy() -> None:
    import warnings

    from grandgousier.interseries.planning import _load_series_candidates

    live_head_with_archive_history = _science_head(
        1,
        101,
        has_science_alias_head=True,
        has_legacy_science_products=False,
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        rows = _load_series_candidates(
            _AliasHeadSession([live_head_with_archive_history]),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
        )

    assert not captured
    assert rows[0]["calibration_version_id"] == 101


def test_multiple_current_alias_heads_without_explicit_ids_refuse_automatic_choice() -> None:
    import pytest

    from grandgousier.interseries.planning import _load_series_candidates

    rows = [
        _science_head(1, 101, logical_name="science_current_a"),
        _science_head(1, 102, logical_name="science_current_b"),
    ]
    with pytest.raises(ValueError, match=r"101.*science_current_a.*102.*science_current_b"):
        _load_series_candidates(
            _AliasHeadSession(rows),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
        )


def test_explicit_one_head_per_series_selects_requested_candidates() -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    rows = [
        _science_head(1, 101, logical_name="science_old_alias"),
        _science_head(1, 102, logical_name="science_current_alias"),
        _science_head(2, 201),
    ]
    selected = _load_series_candidates(
        _AliasHeadSession(rows),
        field_id=7,
        instrument_unit_id=10,
        filter_name="V",
        catalog_name="catalog_a",
        science_calibration_version_ids=[102, 201],
    )

    assert [(row["series_id"], row["calibration_version_id"]) for row in selected] == [(1, 102), (2, 201)]


def test_explicitly_unselected_series_remain_visible_without_replacement() -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    rows = _load_series_candidates(
        _AliasHeadSession([_science_head(1, 101), _science_head(2, 202)]),
        field_id=7,
        instrument_unit_id=None,
        filter_name=None,
        catalog_name=None,
        science_calibration_version_ids=[101],
    )

    assert len(rows) == 2
    assert rows[0]["calibration_version_id"] == 101
    assert rows[1]["series_id"] == 2
    assert rows[1]["calibration_version_id"] is None
    assert rows[1]["_selection_status"] == "UNSELECTED"


def test_plan_exposes_explicitly_unselected_series_without_replacement() -> None:
    import warnings

    from grandgousier.interseries.planning import build_interseries_plan

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM fields" in sql:
                return _FakeResult([{"field_id": 7, "field_name": "TEST_FIELD"}])
            if "FROM field_series fs" in sql:
                return _FakeResult([_science_head(1, 101), _science_head(2, 202)])
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return _FakeResult([])
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                return _FakeResult([])
            raise AssertionError(f"Unexpected SQL: {sql}")

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        plan = build_interseries_plan(
            FakeSession(),
            field_id=7,
            science_calibration_version_ids=[101],
        )

    assert not captured
    assert plan.n_series == 2
    assert plan.series[0].calibration_version_id == 101
    assert plan.series[1].series_id == 2
    assert plan.series[1].calibration_version_id is None
    assert plan.series[1].status == "UNSELECTED"


def test_explicit_selection_warns_and_marks_unselected_legacy_series() -> None:
    from grandgousier.interseries.planning import build_interseries_plan

    legacy_only = {
        "series_id": 2,
        "series_field_id": 7,
        "series_instrument_unit_id": 10,
        "filter_name": "V",
        "n_frames": 40,
        "time_start": None,
        "time_end": None,
        "alias_id": None,
        "alias_logical_name": None,
        "calibration_version_id": None,
        "has_science_alias_head": False,
        "has_legacy_science_products": True,
    }

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM fields" in sql:
                return _FakeResult([{"field_id": 7, "field_name": "TEST_FIELD"}])
            if "FROM field_series fs" in sql:
                return _FakeResult([_science_head(1, 101), legacy_only])
            if "COUNT(DISTINCT bm.frame_id)" in sql:
                return _FakeResult([])
            if "GROUP BY s.calibration_version_id, bm.object_id" in sql:
                return _FakeResult([])
            raise AssertionError(f"Unexpected SQL: {sql}")

    with pytest.warns(UserWarning, match="legacy SCIENCE products.*skipping"):
        plan = build_interseries_plan(
            FakeSession(),
            field_id=7,
            science_calibration_version_ids=[101],
        )

    assert plan.n_series == 2
    selected, legacy_skipped = plan.series
    assert selected.calibration_version_id == 101
    assert selected.status == "NO_SCIENCE_ROWS"
    assert legacy_skipped.series_id == 2
    assert legacy_skipped.calibration_version_id is None
    assert legacy_skipped.status == "LEGACY_SCIENCE_SKIPPED"


def test_automatic_selection_rejects_conflicting_non_null_instrument_scopes() -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    conflicting_head = _science_head(
        1,
        101,
        series_instrument_unit_id=10,
        calibration_instrument_unit_id=20,
    )
    with pytest.raises(ValueError, match="instrument_unit_id differs"):
        _load_series_candidates(
            _AliasHeadSession([conflicting_head]),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
        )


def test_explicit_selection_rejects_conflicting_non_null_instrument_scopes() -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    conflicting_head = _science_head(
        1,
        101,
        series_instrument_unit_id=10,
        calibration_instrument_unit_id=20,
    )
    with pytest.raises(ValueError, match="instrument_unit_id differs"):
        _load_series_candidates(
            _AliasHeadSession([conflicting_head]),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
            science_calibration_version_ids=[101],
        )


@pytest.mark.parametrize(
    ("series_unit", "calibration_unit"),
    [(10, 10), (10, None), (None, 10)],
)
def test_matching_or_one_null_instrument_scopes_keep_fallback_behavior(
    series_unit,
    calibration_unit,
) -> None:
    from grandgousier.interseries.planning import _load_series_candidates

    head = _science_head(
        1,
        101,
        series_instrument_unit_id=series_unit,
        calibration_instrument_unit_id=calibration_unit,
    )
    automatic = _load_series_candidates(
        _AliasHeadSession([head]),
        field_id=7,
        instrument_unit_id=None,
        filter_name=None,
        catalog_name=None,
    )
    explicit = _load_series_candidates(
        _AliasHeadSession([head]),
        field_id=7,
        instrument_unit_id=10,
        filter_name=None,
        catalog_name=None,
        science_calibration_version_ids=[101],
    )

    assert automatic[0]["calibration_version_id"] == 101
    assert explicit[0]["calibration_version_id"] == 101


@pytest.mark.parametrize(
    ("rows", "selected_ids", "instrument_unit_id", "catalog_name", "message"),
    [
        ([], [999], None, None, "not a current SCIENCE alias head"),
        (
            [_science_head(1, 901, calibration_product_role="INTERMEDIATE")],
            [901],
            None,
            None,
            "product_role",
        ),
        (
            [_science_head(1, 902, catalog_name="catalog_b")],
            [902],
            None,
            "catalog_a",
            "catalog_name",
        ),
        (
            [
                _science_head(
                    1,
                    903,
                    series_instrument_unit_id=None,
                    calibration_instrument_unit_id=99,
                )
            ],
            [903],
            10,
            None,
            "instrument_unit_id",
        ),
        (
            [
                _science_head(
                    1,
                    904,
                    series_instrument_unit_id=10,
                    calibration_instrument_unit_id=None,
                )
            ],
            [904],
            20,
            None,
            "series_instrument_unit_id",
        ),
    ],
)
def test_invalid_explicit_heads_fail_closed(
    rows,
    selected_ids,
    instrument_unit_id,
    catalog_name,
    message,
) -> None:
    import pytest

    from grandgousier.interseries.planning import _load_series_candidates

    with pytest.raises(ValueError, match=message):
        _load_series_candidates(
            _AliasHeadSession(rows),
            field_id=7,
            instrument_unit_id=instrument_unit_id,
            filter_name=None,
            catalog_name=catalog_name,
            science_calibration_version_ids=selected_ids,
        )


def test_explicit_multiple_heads_for_one_series_fail_closed() -> None:
    import pytest

    from grandgousier.interseries.planning import _load_series_candidates

    with pytest.raises(ValueError, match=r"one explicit.*series 1 has IDs 101 and 102"):
        _load_series_candidates(
            _AliasHeadSession([_science_head(1, 101), _science_head(1, 102)]),
            field_id=7,
            instrument_unit_id=None,
            filter_name=None,
            catalog_name=None,
            science_calibration_version_ids=[101, 102],
        )
