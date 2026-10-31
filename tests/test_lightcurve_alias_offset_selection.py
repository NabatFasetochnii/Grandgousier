from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.lightcurves import candidates, product, target
from grandgousier.lightcurves.product import (
    LightCurveSelectionError,
    _load_calibration_rows,
    _resolve_interseries_generation_selection,
    _science_row_from_db,
    combine_mag_error,
)


class _MappingsResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)


class _ScalarsResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)


class _ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.execute_calls = 0

    def execute(self, *_args, **_kwargs):
        if self.execute_calls >= len(self.responses):
            raise AssertionError("unexpected database query")
        rows = self.responses[self.execute_calls]
        self.execute_calls += 1
        return _MappingsResult(rows)


def _version(
    version_id: int,
    name: str,
    *,
    model: str = "ensemble_v1",
    series_id: int = 1,
    role: str = "SCIENCE",
) -> dict:
    return {
        "calibration_version_id": version_id,
        "name": name,
        "model_name": model,
        "instrument_unit_id": 8,
        "field_id": 33,
        "series_id": series_id,
        "product_role": role,
        "created_at": f"2026-01-{version_id:02d}",
    }


def test_exact_logical_and_archived_physical_calibration_names_are_explicit():
    logical = _ScriptedSession(
        [
            [{"logical_name": "science_v", "head_id": 101}],
            [_version(101, "science_v_g2")],
            [{"head_id": 101, "logical_name": "science_v"}],
        ]
    )
    logical_rows = _load_calibration_rows(
        logical,
        calibration_names=["science_v"],
        calibration_prefixes=[],
        model_name=None,
    )
    assert [row["calibration_version_id"] for row in logical_rows] == [101]
    assert logical_rows[0]["is_current_alias_head"] is True

    archived = _ScriptedSession(
        [
            [],
            [{"calibration_version_id": 88}],
            [_version(88, "science_v_g1")],
            [],
        ]
    )
    archived_rows = _load_calibration_rows(
        archived,
        calibration_names=["science_v_g1"],
        calibration_prefixes=[],
        model_name=None,
    )
    assert [row["calibration_version_id"] for row in archived_rows] == [88]
    assert archived_rows[0]["is_current_alias_head"] is False


def test_prefix_model_and_selector_intersection_only_return_alias_heads():
    session = _ScriptedSession(
        [
            [
                {"calibration_version_id": 101},
                {"calibration_version_id": 103},
            ],
            [_version(101, "science_v_g2"), _version(103, "science_r_g2", series_id=2)],
            [
                {"head_id": 101, "logical_name": "science_v"},
                {"head_id": 103, "logical_name": "science_r"},
            ],
        ]
    )
    rows = _load_calibration_rows(
        session,
        calibration_names=[],
        calibration_prefixes=["science_"],
        model_name="ensemble_v1",
    )
    assert [row["calibration_version_id"] for row in rows] == [101, 103]
    assert all(row["is_current_alias_head"] for row in rows)

    intersection = _ScriptedSession(
        [
            [{"logical_name": "science_v", "head_id": 101}],
            [{"calibration_version_id": 103}],
            [_version(101, "science_v_g2"), _version(103, "science_r_g2", series_id=2)],
            [
                {"head_id": 101, "logical_name": "science_v"},
                {"head_id": 103, "logical_name": "science_r"},
            ],
        ]
    )
    assert _load_calibration_rows(
        intersection,
        calibration_names=["science_v"],
        calibration_prefixes=["science_r"],
        model_name=None,
    ) == []


def test_multiple_current_heads_for_one_series_refuse_broad_selection(monkeypatch):
    session = _ScriptedSession(
        [
            [{"calibration_version_id": 101}, {"calibration_version_id": 102}],
            [_version(101, "science_v_g2"), _version(102, "science_v_alt")],
            [
                {"head_id": 101, "logical_name": "science_v"},
                {"head_id": 102, "logical_name": "science_v_alt"},
            ],
        ]
    )
    monkeypatch.setattr(product, "_load_calibration_rows", _load_calibration_rows)
    with pytest.raises(LightCurveSelectionError, match="Multiple current SCIENCE alias heads"):
        product.resolve_light_curve_scope(
            session,
            calibration_prefixes=["science_v"],
        )
    assert session.execute_calls == 3


_SCOPE_V = (33, 8, "V", "gaia")


def _context(
    cv_id: int,
    series_id: int,
    *,
    scope_key=_SCOPE_V,
    role: str = "SCIENCE",
) -> dict:
    return {
        "calibration_version_id": cv_id,
        "product_role": role,
        "series_id": series_id,
        "scope_key": scope_key,
    }


def _generation(
    generation_id: int,
    source_keys: set[tuple[int, int]],
    *,
    scope_key=_SCOPE_V,
    algorithm: str = "interseries_graph_median_v1",
    lifecycle: str = "COMPLETE_IMMUTABLE",
) -> dict:
    return {
        "interseries_generation_id": generation_id,
        "field_id": scope_key[0],
        "instrument_unit_id": scope_key[1],
        "filter_name": scope_key[2],
        "catalog_name": scope_key[3],
        "analysis_algorithm": algorithm,
        "lifecycle_status": lifecycle,
        "reliability_status": "REDUNDANT_GRAPH_OK",
        "scope_key": scope_key,
        "sources": {
            key: {
                "interseries_generation_id": generation_id,
                "series_id": key[0],
                "calibration_version_id": key[1],
                "offset_mag": 0.125,
                "offset_err": 0.025,
                "status": "REDUNDANT_GRAPH_OK",
                "algorithm": algorithm,
            }
            for key in source_keys
        },
    }


def test_m4_selection_refuses_orphaned_member_instead_of_filtering_it() -> None:
    rows = [
        {
            "interseries_generation_id": 3, "field_id": 33, "instrument_unit_id": 8,
            "filter_name": "V", "catalog_name": "gaia",
            "analysis_algorithm": "interseries_graph_median_v1",
            "lifecycle_status": "COMPLETE_IMMUTABLE", "reliability_status": "REDUNDANT_GRAPH_OK",
            "n_series": 2, "membership_field_id": 33, "series_id": 1,
            "science_calibration_version_id": 101, "offset_mag": 0.125, "offset_err": 0.025,
        },
        {
            "interseries_generation_id": 3, "field_id": 33, "instrument_unit_id": 8,
            "filter_name": "V", "catalog_name": "gaia",
            "analysis_algorithm": "interseries_graph_median_v1",
            "lifecycle_status": "COMPLETE_IMMUTABLE", "reliability_status": "REDUNDANT_GRAPH_OK",
            "n_series": 2, "membership_field_id": 33, "series_id": None,
            "science_calibration_version_id": 102, "offset_mag": 0.2, "offset_err": 0.025,
        },
    ]

    class FakeSession:
        def execute(self, _statement):
            return _MappingsResult(rows)

    contexts = {101: _context(101, 1)}
    records = product._load_interseries_generation_records(
        FakeSession(), scope_contexts=contexts, explicit_generation_ids=None,
    )
    assert records[3]["broken_membership"] is True
    for explicit in (None, [3]):
        with pytest.raises(LightCurveSelectionError, match="orphaned or incomplete membership"):
            _resolve_interseries_generation_selection(
                scope_contexts=contexts,
                current_science_calibration_version_ids={101},
                generation_records=records,
                offset_algorithm="interseries_graph_median_v1",
                explicit_generation_ids=explicit,
            )


def test_unique_generation_autoselects_only_exact_generation_series_cv_sources():
    contexts = {101: _context(101, 1)}
    current_ids = {101}
    current_generation = _generation(3, {(1, 101)})
    archived_generation = _generation(1, {(1, 88)})
    selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids=current_ids,
        generation_records={1: archived_generation, 3: current_generation},
        offset_algorithm="interseries_graph_median_v1",
        explicit_generation_ids=None,
    )
    assert selection["generation_by_scope"][_SCOPE_V]["interseries_generation_id"] == 3
    assert (3, 1, 101) in selection["source_offsets"]
    assert (3, 1, 88) not in selection["source_offsets"]


def test_multiple_eligible_generations_require_explicit_selection():
    contexts = {101: _context(101, 1)}
    records = {
        3: _generation(3, {(1, 101)}),
        4: _generation(4, {(1, 101)}),
    }
    with pytest.raises(LightCurveSelectionError, match="Multiple eligible M4 generations"):
        _resolve_interseries_generation_selection(
            scope_contexts=contexts,
            current_science_calibration_version_ids={101},
            generation_records=records,
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=None,
        )

    selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids={101},
        generation_records=records,
        offset_algorithm="interseries_graph_median_v1",
        explicit_generation_ids=[4],
    )
    assert selection["generation_by_scope"][_SCOPE_V]["interseries_generation_id"] == 4


def test_generation_cannot_borrow_an_offset_from_an_older_cv_or_other_generation():
    with pytest.raises(LightCurveSelectionError, match="does not cover every selected SCIENCE source"):
        _resolve_interseries_generation_selection(
            scope_contexts={101: _context(101, 1)},
            current_science_calibration_version_ids={101},
            generation_records={7: _generation(7, {(1, 88)})},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=[7],
        )

    with pytest.raises(LightCurveSelectionError, match="No complete M4 generation"):
        _resolve_interseries_generation_selection(
            scope_contexts={101: _context(101, 1)},
            current_science_calibration_version_ids={101},
            generation_records={7: _generation(7, {(1, 88)})},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=None,
        )


def test_incomplete_scope_coverage_refuses_whole_selection():
    contexts = {101: _context(101, 1), 102: _context(102, 2)}
    with pytest.raises(LightCurveSelectionError, match="does not cover every selected SCIENCE source"):
        _resolve_interseries_generation_selection(
            scope_contexts=contexts,
            current_science_calibration_version_ids={101, 102},
            generation_records={5: _generation(5, {(1, 101)})},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=[5],
        )


def test_mixed_current_and_archived_sources_require_complete_generation_coverage():
    contexts = {
        101: _context(101, 1),
        88: _context(88, 1),
    }

    with pytest.raises(LightCurveSelectionError, match="No complete M4 generation"):
        _resolve_interseries_generation_selection(
            scope_contexts=contexts,
            current_science_calibration_version_ids={101},
            generation_records={3: _generation(3, {(1, 101)})},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=None,
        )


def test_exact_archived_selection_with_in_scope_generation_missing_source_refuses():
    with pytest.raises(LightCurveSelectionError, match="No complete M4 generation"):
        _resolve_interseries_generation_selection(
            scope_contexts={88: _context(88, 1)},
            current_science_calibration_version_ids=set(),
            generation_records={3: _generation(3, {(1, 101)})},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=None,
        )


def test_generation_scope_algorithm_and_lifecycle_validation_are_exact():
    wrong_scope = _generation(5, {(1, 101)}, scope_key=(33, 8, "R", "gaia"))
    with pytest.raises(LightCurveSelectionError, match="outside the selected"):
        _resolve_interseries_generation_selection(
            scope_contexts={101: _context(101, 1)},
            current_science_calibration_version_ids={101},
            generation_records={5: wrong_scope},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=[5],
        )

    wrong_algorithm = _generation(6, {(1, 101)}, algorithm="another_algorithm")
    with pytest.raises(LightCurveSelectionError, match="uses algorithm"):
        _resolve_interseries_generation_selection(
            scope_contexts={101: _context(101, 1)},
            current_science_calibration_version_ids={101},
            generation_records={6: wrong_algorithm},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=[6],
        )

    incomplete = _generation(8, {(1, 101)}, lifecycle="BUILDING")
    with pytest.raises(LightCurveSelectionError, match="not COMPLETE_IMMUTABLE"):
        _resolve_interseries_generation_selection(
            scope_contexts={101: _context(101, 1)},
            current_science_calibration_version_ids={101},
            generation_records={8: incomplete},
            offset_algorithm="interseries_graph_median_v1",
            explicit_generation_ids=[8],
        )


def test_no_generation_is_visible_while_independent_complete_scope_proceeds():
    scope_r = (33, 8, "R", "gaia")
    contexts = {101: _context(101, 1), 202: _context(202, 2, scope_key=scope_r)}
    selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids={101, 202},
        generation_records={3: _generation(3, {(1, 101)})},
        offset_algorithm="interseries_graph_median_v1",
        explicit_generation_ids=None,
    )
    assert selection["generation_by_scope"][_SCOPE_V]["interseries_generation_id"] == 3
    assert selection["status_by_scope"][scope_r] == "NO_M4_GENERATION"
    assert any("NO_M4_GENERATION" in warning for warning in selection["warnings"])


def test_repeatable_generation_ids_select_one_generation_per_distinct_scope():
    scope_r = (33, 8, "R", "gaia")
    contexts = {101: _context(101, 1), 202: _context(202, 2, scope_key=scope_r)}
    selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids={101, 202},
        generation_records={
            3: _generation(3, {(1, 101)}),
            4: _generation(4, {(2, 202)}, scope_key=scope_r),
        },
        offset_algorithm="interseries_graph_median_v1",
        explicit_generation_ids=[3, 4],
    )
    assert selection["generation_by_scope"][_SCOPE_V]["interseries_generation_id"] == 3
    assert selection["generation_by_scope"][scope_r]["interseries_generation_id"] == 4


def test_explicit_generation_in_one_scope_does_not_disable_unique_autoselection_elsewhere():
    scope_r = (33, 8, "R", "gaia")
    contexts = {
        101: _context(101, 1),
        202: _context(202, 2, scope_key=scope_r),
    }
    generation_rows = [
        {
            "interseries_generation_id": 3,
            "field_id": 33,
            "instrument_unit_id": 8,
            "filter_name": "V",
            "catalog_name": "gaia",
            "analysis_algorithm": "interseries_graph_median_v1",
            "lifecycle_status": "COMPLETE_IMMUTABLE",
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "series_id": 1,
            "science_calibration_version_id": 101,
            "offset_mag": 0.125,
            "offset_err": 0.025,
        },
        {
            "interseries_generation_id": 4,
            "field_id": 33,
            "instrument_unit_id": 8,
            "filter_name": "R",
            "catalog_name": "gaia",
            "analysis_algorithm": "interseries_graph_median_v1",
            "lifecycle_status": "COMPLETE_IMMUTABLE",
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "series_id": 2,
            "science_calibration_version_id": 202,
            "offset_mag": 0.25,
            "offset_err": 0.05,
        },
    ]

    class FakeSession:
        def execute(self, statement):
            assert " OR " in str(statement)
            return _MappingsResult(generation_rows)

    generation_records = product._load_interseries_generation_records(
        FakeSession(),
        scope_contexts=contexts,
        explicit_generation_ids=[3],
    )
    selection = _resolve_interseries_generation_selection(
        scope_contexts=contexts,
        current_science_calibration_version_ids={101, 202},
        generation_records=generation_records,
        offset_algorithm="interseries_graph_median_v1",
        explicit_generation_ids=[3],
    )

    assert selection["generation_by_scope"][_SCOPE_V]["interseries_generation_id"] == 3
    assert selection["generation_by_scope"][scope_r]["interseries_generation_id"] == 4


@pytest.mark.parametrize(
    ("generation_rows", "expected_generation_id", "expected_status"),
    [
        (
            [
                {
                    "interseries_generation_id": 3,
                    "field_id": 33,
                    "instrument_unit_id": 8,
                    "filter_name": "V",
                    "catalog_name": "gaia",
                    "analysis_algorithm": "interseries_graph_median_v1",
                    "lifecycle_status": "COMPLETE_IMMUTABLE",
                    "reliability_status": "REDUNDANT_GRAPH_OK",
                    "series_id": 1,
                    "science_calibration_version_id": 101,
                    "offset_mag": 0.125,
                    "offset_err": 0.025,
                }
            ],
            3,
            "REDUNDANT_GRAPH_OK",
        ),
        ([], None, "NO_M4_GENERATION"),
    ],
)
def test_raw_reader_emits_exact_generation_provenance_or_stage2_only_status(
    monkeypatch, generation_rows, expected_generation_id, expected_status
):
    calibration = SimpleNamespace(
        calibration_version_id=101,
        name="science_v_g2",
        model_name="ensemble_v1",
        series_id=1,
    )
    measurement = SimpleNamespace(
        measurement_id=301,
        base_measurement_id=301,
        object_id=7,
        frame_id=21,
        flags={"catalog_name": "gaia", "source_id": "source-7"},
        x=1.0,
        y=2.0,
        flux=100.0,
        flux_err=2.0,
        mag_inst=13.0,
        mag_cal=12.8,
        mag_err=0.03,
        mag_err_random=0.03,
        mag_err_systematic=0.04,
        aperture_radius_pix=3.0,
        local_background=1.0,
        local_zp_correction=None,
        centroid_dx_pix=None,
        centroid_dy_pix=None,
    )
    frame = SimpleNamespace(
        frame_id=21,
        date_obs="2026-01-01T00:00:00",
        mjd=60000.0,
        jd=2460000.5,
        instrument_unit_id=8,
        instrument="MASTER",
        site="site",
        tube="tube",
        camera="camera",
        filter_name="V",
        quality_flags={},
    )
    monkeypatch.setattr(
        product,
        "load_calibration_photometry_view",
        lambda *_args, **_kwargs: SimpleNamespace(rows=[measurement]),
    )
    context_row = {
        "calibration_version_id": 101,
        "product_role": "SCIENCE",
        "calibration_field_id": 33,
        "calibration_series_id": 1,
        "calibration_instrument_unit_id": 8,
        "used_catalogs": {"catalog_name": "gaia"},
        "series_field_id": 33,
        "series_instrument_unit_id": 8,
        "series_filter_name": "V",
        "pipeline_catalog_name": "gaia",
    }

    class FakeSession:
        def __init__(self):
            self.scalar_call = 0
            self.execute_call = 0

        def scalars(self, *_args, **_kwargs):
            self.scalar_call += 1
            rows = (
                [calibration]
                if self.scalar_call == 1
                else ([frame] if self.scalar_call == 2 else [])
            )
            return _ScalarsResult(rows)

        def execute(self, *_args, **_kwargs):
            self.execute_call += 1
            rows = (
                [context_row]
                if self.execute_call == 1
                else (generation_rows if self.execute_call == 2 else [(1, "field", 33)])
            )
            return _MappingsResult(rows)

    scope = product.LightCurveScope(
        calibration_version_ids=[101],
        current_science_calibration_version_ids=[101],
    )
    rows = product._load_raw_light_curve_rows_virtual(
        FakeSession(),
        scope=scope,
        source_id=None,
        object_id=None,
        object_qc_algorithm="object_qc_v1",
        offset_algorithm="interseries_graph_median_v1",
    )
    assert len(rows) == 1
    assert rows[0]["interseries_generation_id"] == expected_generation_id
    assert rows[0]["interseries_offset_status"] == expected_status
    if expected_generation_id is not None:
        assert rows[0]["interseries_offset_mag"] == pytest.approx(0.125)


def test_raw_reader_refuses_old_cv_offset_before_returning_partial_rows(monkeypatch):
    calibration = SimpleNamespace(
        calibration_version_id=101,
        name="science_v_g2",
        model_name="ensemble_v1",
        series_id=1,
    )
    measurement = SimpleNamespace(flags={"catalog_name": "gaia"})
    monkeypatch.setattr(
        product,
        "load_calibration_photometry_view",
        lambda *_args, **_kwargs: SimpleNamespace(rows=[measurement]),
    )
    context_row = {
        "calibration_version_id": 101,
        "product_role": "SCIENCE",
        "calibration_field_id": 33,
        "calibration_series_id": 1,
        "calibration_instrument_unit_id": 8,
        "used_catalogs": {"catalog_name": "gaia"},
        "series_field_id": 33,
        "series_instrument_unit_id": 8,
        "series_filter_name": "V",
        "pipeline_catalog_name": "gaia",
    }
    generation_row = {
        "interseries_generation_id": 3,
        "field_id": 33,
        "instrument_unit_id": 8,
        "filter_name": "V",
        "catalog_name": "gaia",
        "analysis_algorithm": "interseries_graph_median_v1",
        "lifecycle_status": "COMPLETE_IMMUTABLE",
        "reliability_status": "REDUNDANT_GRAPH_OK",
        "series_id": 1,
        "science_calibration_version_id": 88,
        "offset_mag": 0.125,
        "offset_err": 0.025,
    }

    class FakeSession:
        def __init__(self):
            self.scalar_call = 0
            self.execute_call = 0

        def scalars(self, *_args, **_kwargs):
            self.scalar_call += 1
            return _ScalarsResult([calibration] if self.scalar_call == 1 else [])

        def execute(self, *_args, **_kwargs):
            self.execute_call += 1
            rows = [context_row] if self.execute_call == 1 else [generation_row]
            return _MappingsResult(rows)

    with pytest.raises(LightCurveSelectionError, match="No complete M4 generation"):
        product._load_raw_light_curve_rows_virtual(
            FakeSession(),
            scope=product.LightCurveScope(
                calibration_version_ids=[101],
                current_science_calibration_version_ids=[101],
            ),
            source_id=None,
            object_id=None,
            object_qc_algorithm="object_qc_v1",
            offset_algorithm="interseries_graph_median_v1",
        )


def test_m4_and_global_magnitude_error_equations_remain_unchanged():
    random_err = 0.03
    science_systematic_err = 0.04
    offset_err = 0.05
    row = _science_row_from_db(
        {
            "object_id": 1,
            "frame_id": 2,
            "series_id": 3,
            "mag_cal": 12.3,
            "mag_err_random": random_err,
            "mag_err_systematic": science_systematic_err,
            "interseries_offset_mag": 0.25,
            "interseries_offset_err": offset_err,
            "interseries_offset_status": "REDUNDANT_GRAPH_OK",
            "interseries_generation_id": 9,
            "measurement_flags": {},
            "object_qc_flags": {},
            "frame_quality_flags": {},
        },
        reject_flags=set(),
    )
    expected_systematic = combine_mag_error(science_systematic_err, offset_err)
    assert row["mag_global"] == pytest.approx(12.55)
    assert row["mag_global_err_systematic"] == pytest.approx(expected_systematic)
    assert row["mag_global_err"] == pytest.approx(combine_mag_error(random_err, expected_systematic))
    assert row["interseries_generation_id"] == 9

    stage2_only = _science_row_from_db(
        {
            "object_id": 1,
            "frame_id": 2,
            "series_id": 3,
            "mag_cal": 12.3,
            "mag_err_random": random_err,
            "interseries_offset_mag": None,
            "interseries_offset_status": "NO_M4_GENERATION",
            "measurement_flags": {},
            "object_qc_flags": {},
            "frame_quality_flags": {},
        },
        reject_flags=set(),
    )
    assert stage2_only["mag_global"] == pytest.approx(12.3)
    assert stage2_only["interseries_offset_status"] == "NO_M4_GENERATION"


def test_candidate_incomplete_m4_preflight_refuses_before_creating_output(monkeypatch, tmp_path):
    selected_head = _version(101, "science_v_g2")
    selected_head["is_current_alias_head"] = True
    selected_head["alias_logical_names"] = ["science_v"]
    monkeypatch.setattr(
        product,
        "_load_calibration_rows",
        lambda *_args, **_kwargs: [selected_head],
    )
    monkeypatch.setattr(product, "_calibration_chain_names", lambda *_args: ["science_v_g2"])
    monkeypatch.setattr(product, "_resolve_field_series_filter", lambda *_args, **_kwargs: None)

    context_row = {
        "calibration_version_id": 101,
        "product_role": "SCIENCE",
        "calibration_field_id": 33,
        "calibration_series_id": 1,
        "calibration_instrument_unit_id": 8,
        "used_catalogs": {"catalog_name": "gaia"},
        "series_field_id": 33,
        "series_instrument_unit_id": 8,
        "series_filter_name": "V",
        "pipeline_catalog_name": "gaia",
    }
    generation_row = {
        "interseries_generation_id": 7,
        "field_id": 33,
        "instrument_unit_id": 8,
        "filter_name": "V",
        "catalog_name": "gaia",
        "analysis_algorithm": "interseries_graph_median_v1",
        "lifecycle_status": "COMPLETE_IMMUTABLE",
        "reliability_status": "REDUNDANT_GRAPH_OK",
        "series_id": 1,
        "science_calibration_version_id": 88,
        "offset_mag": 0.1,
        "offset_err": 0.02,
    }

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def get(self, *_args):
            return SimpleNamespace(calibration_version_id=101, name="science_v_g2")

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return _MappingsResult([context_row] if self.calls == 1 else [generation_row])

    output = tmp_path / "not-created" / "candidates"
    with pytest.raises(LightCurveSelectionError, match="No complete M4 generation"):
        candidates.export_accepted_candidate_light_curves(
            FakeSession(),
            candidate_rows=[],
            calibration_name="science_v",
            catalog_name="gaia",
            series_id=1,
            out_dir=output,
            export_plots=False,
        )
    assert not output.parent.exists()


def test_target_generation_refusal_happens_before_product_creation(monkeypatch, tmp_path):
    output = tmp_path / "target.json"

    def refuse(*_args, **_kwargs):
        raise LightCurveSelectionError("incomplete M4 coverage")

    monkeypatch.setattr(target, "preflight_science_light_curve_selection", refuse)
    with pytest.raises(LightCurveSelectionError, match="incomplete M4 coverage"):
        target.export_target_light_curve_product(
            object(),
            target=target.TargetResolution(resolved=True, object_id=1),
            calibration_name="science_v",
            interseries_generation_ids=[7],
            out_json=output,
        )
    assert not output.exists()


def test_unresolved_target_skips_m4_preflight_and_preserves_optional_json_warning(
    monkeypatch, tmp_path
):
    output = tmp_path / "unresolved-target.json"

    def unexpected_preflight(*_args, **_kwargs):
        raise AssertionError("unresolved targets must not run M4 preflight")

    monkeypatch.setattr(target, "preflight_science_light_curve_selection", unexpected_preflight)
    product_result = target.export_target_light_curve_product(
        object(),
        target=target.TargetResolution(resolved=False, reason="target not found"),
        calibration_name="science_v",
        interseries_generation_ids=[7],
        out_csv=tmp_path / "not-created.csv",
        out_json=output,
    )

    assert product_result.interseries_generation_ids == []
    assert product_result.interseries_generation_scopes == []
    assert product_result.warnings == ["target not found"]
    assert product_result.json_path == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["interseries_generation_ids"] == []
    assert not (tmp_path / "not-created.csv").exists()


def test_target_and_candidate_apis_thread_explicit_generation_ids(monkeypatch, tmp_path):
    selected_scope = SimpleNamespace(
        interseries_generation_ids=[17],
        interseries_generation_scopes=[{"interseries_generation_id": 17}],
        warnings=[],
    )
    preflight_calls = []
    monkeypatch.setattr(
        target,
        "preflight_science_light_curve_selection",
        lambda _session, **kwargs: preflight_calls.append(kwargs) or selected_scope,
    )
    target_calls = []
    monkeypatch.setattr(
        target,
        "load_science_light_curve_rows",
        lambda _session, **kwargs: (
            selected_scope,
            target_calls.append(kwargs) or [],
        ),
    )
    target_product = target.export_target_light_curve_product(
        object(),
        target=target.TargetResolution(resolved=True, object_id=1),
        calibration_name="science_v",
        series_id=1,
        interseries_generation_ids=[17],
    )
    assert preflight_calls[-1]["interseries_generation_ids"] == [17]
    assert target_calls[-1]["interseries_generation_ids"] == [17]
    assert target_product.to_dict()["interseries_generation_ids"] == [17]

    monkeypatch.setattr(
        candidates,
        "preflight_science_light_curve_selection",
        lambda _session, **kwargs: preflight_calls.append(kwargs) or selected_scope,
    )
    candidate_calls = []
    candidate_row = {
        "object_id": 1,
        "catalog_name": "gaia",
        "source_id": "1",
        "series_id": 1,
        "frame_id": 2,
        "measurement_id": 3,
        "mag_cal": 12.3,
        "mag_err": 0.03,
        "mag_global": 12.3,
        "mag_global_err": 0.03,
        "mag_global_err_random": 0.03,
        "mag_global_err_systematic": None,
        "is_rejected": False,
        "reject_reasons": "",
    }

    def fake_candidate_load(_session, **kwargs):
        candidate_calls.append(kwargs)
        return selected_scope, [candidate_row]

    monkeypatch.setattr(candidates, "load_science_light_curve_rows", fake_candidate_load)
    candidate_product = candidates.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[{"candidate_status": "ACCEPTED", "object_id": 1, "source_id": "1"}],
        calibration_name="science_v",
        catalog_name="gaia",
        series_id=1,
        interseries_generation_ids=[17],
        out_dir=tmp_path / "candidates",
        export_plots=False,
    )
    assert candidate_calls[0]["interseries_generation_ids"] == [17]
    assert candidate_product.interseries_generation_ids == [17]
    assert Path(candidate_product.candidates_summary_json).exists()


def test_generic_cli_generation_option_is_repeatable_but_not_on_process_archive():
    from grandgousier.cli import build_parser

    parser = build_parser()
    for argv in (
        [
            "list-light-curve-objects",
            "--calibration-name",
            "science_v",
            "--interseries-generation-id",
            "3",
            "--interseries-generation-id",
            "4",
        ],
        [
            "light-curve-summary",
            "--calibration-name",
            "science_v",
            "--interseries-generation-id",
            "3",
            "--interseries-generation-id",
            "4",
        ],
        [
            "export-light-curve",
            "--calibration-name",
            "science_v",
            "--interseries-generation-id",
            "3",
            "--interseries-generation-id",
            "4",
            "--out",
            "unused.csv",
        ],
    ):
        args = parser.parse_args(argv)
        assert args.interseries_generation_id == [3, 4]

    process_args = parser.parse_args(["process-archive-path", "--path", "not-read"])
    assert not hasattr(process_args, "interseries_generation_id")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "process-archive-path",
                "--path",
                "not-read",
                "--interseries-generation-id",
                "3",
            ]
        )


def test_pipeline_config_accepts_direct_generation_selection_without_cli_plumbing():
    import ast

    from grandgousier.pipeline.runner import FieldPipelineConfig

    config = FieldPipelineConfig(
        catalog_name="gaia",
        calibration_prefix="science",
        interseries_generation_ids=[3, 4],
    )
    assert config.interseries_generation_ids == [3, 4]

    runner_path = Path(__file__).parents[1] / "grandgousier" / "pipeline" / "runner.py"
    runner_tree = ast.parse(runner_path.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in runner_tree.body if isinstance(node, ast.FunctionDef)
    }
    wrapper = functions["run_field_pipeline"]
    delegated_returns = [
        node.value for node in ast.walk(wrapper)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_run_field_pipeline"
    ]
    assert len(delegated_returns) == 2
    for call in delegated_returns:
        assert [arg.id for arg in call.args if isinstance(arg, ast.Name)] == ["session", "config"]
        assert len(call.args) == 2 and not call.keywords

    implementation = functions["_run_field_pipeline"]
    export_calls = [
        node
        for node in ast.walk(implementation)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {"export_target_light_curve_product", "export_accepted_candidate_light_curves"}
    ]
    assert len(export_calls) == 4
    for call in export_calls:
        generation_selector = next(
            keyword.value
            for keyword in call.keywords
            if keyword.arg == "interseries_generation_ids"
        )
        assert isinstance(generation_selector, ast.Attribute)
        assert generation_selector.attr == "interseries_generation_ids"
        assert isinstance(generation_selector.value, ast.Name)
        assert generation_selector.value.id == "config"
