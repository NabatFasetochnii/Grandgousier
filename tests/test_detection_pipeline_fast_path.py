from __future__ import annotations

import json
import math
import random

import numpy as np
import pytest

from grandgousier.db.detection_io import (
    DETECTION_CATALOG_MATCH_WRITE_COLUMNS,
    SOURCE_DETECTION_WRITE_COLUMNS,
    detection_catalog_match_copy_tuple,
    source_detection_copy_tuple,
)
from grandgousier.quality.detection_catalog_qc import (
    CatalogMatchSource,
    DetectionMatchInput,
    _CatalogSkyIndex,
    _candidate_pairs,
    _match_frame,
    aggregate_detection_catalog_support,
    _select_catalog_sources_bulk,
    _select_detections_bulk,
)


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.execute_calls = 0
        self.statements = []
        self.parameters = []

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        self.parameters.append(parameters)
        rows = self._result_sets[self.execute_calls]
        self.execute_calls += 1
        return _FakeMappingResult(rows)


class _SupportAggregationSession:
    """Mock the grouped query using source/frame pairs, including duplicate matches."""

    def __init__(self, matched_rows):
        self.matched_rows = list(matched_rows)
        self.statements = []
        self.parameters = []

    def execute(self, statement, parameters):
        self.statements.append(statement)
        self.parameters.append(parameters)
        selected_frames = (
            None if parameters.get("frame_ids") is None else set(parameters["frame_ids"])
        )
        frames_by_source = {}
        for source_id, frame_id in self.matched_rows:
            if selected_frames is None or frame_id in selected_frames:
                frames_by_source.setdefault(source_id, set()).add(frame_id)
        return _FakeMappingResult(
            [(source_id, len(frame_ids)) for source_id, frame_ids in sorted(frames_by_source.items())]
        )


@pytest.mark.parametrize(
    ("matched_rows", "expected_support"),
    [
        ([(100, 4), (100, 4)], {100: 1}),  # one supported frame, repeated match rows count once
        ([(100, frame_id) for frame_id in range(1, 11)], {100: 10}),
        ([], {}),
    ],
    ids=["support-1-of-10-repeat-frame-once", "support-10-of-10", "support-0-of-10"],
)
def test_detection_catalog_support_counts_distinct_frames(matched_rows, expected_support):
    session = _SupportAggregationSession(matched_rows)

    support = aggregate_detection_catalog_support(
        session,
        catalog_name="auto_catalog_field_17",
        detection_run_id=55,
        frame_ids=list(range(1, 11)),
    )

    assert support == expected_support
    sql = str(session.statements[0])
    assert "COUNT(DISTINCT fr.frame_id)" in sql
    assert "observation_uid" not in sql
    assert "observations" not in sql
    assert session.parameters[0] == {
        "detection_run_id": 55,
        "catalog_name": "auto_catalog_field_17",
        "frame_ids": list(range(1, 11)),
    }


def test_detection_catalog_support_empty_scope_does_not_query():
    session = _FakeSession([])
    assert aggregate_detection_catalog_support(
        session,
        catalog_name="cat",
        detection_run_id=55,
        frame_ids=[],
    ) == {}
    assert session.execute_calls == 0


def test_detection_catalog_support_uses_complete_match_run_when_frame_scope_is_unspecified():
    session = _SupportAggregationSession([(100, 1), (100, 2)])

    support = aggregate_detection_catalog_support(
        session,
        catalog_name="cat",
        detection_run_id=55,
        frame_ids=None,
    )

    assert support == {100: 2}
    assert "fr.frame_id IN" not in str(session.statements[0])
    assert "frame_ids" not in session.parameters[0]


def test_zero_detection_support_keeps_uniquely_selected_explicit_source_eligible():
    from grandgousier.pipeline.runner import _eligible_catalog_sources_for_forced_photometry

    explicit_target = {
        "eligible": True,
        "catalog_source_id": 100,
        "identity_link_exists": False,
    }

    assert _eligible_catalog_sources_for_forced_photometry({}, explicit_target) == [100]
    assert _eligible_catalog_sources_for_forced_photometry(
        {}, {"eligible": False, "catalog_source_id": 101}
    ) == []


def _legacy_select(scored, *, max_sources, min_separation_pix):
    chosen = []
    sep2 = min_separation_pix * min_separation_pix
    for snr, y_int, x_int, peak in scored:
        if len(chosen) >= max_sources:
            break
        if sep2 > 0 and any(
            (x_int - x0) ** 2 + (y_int - y0) ** 2 < sep2
            for _, y0, x0, _ in chosen
        ):
            continue
        chosen.append((snr, float(y_int), float(x_int), peak))
    return chosen



def test_detection_run_fingerprint_is_stable_for_parameter_key_order():
    from grandgousier.db.detection_runs import detection_run_fingerprint

    left = detection_run_fingerprint(
        run_kind="SOURCE_DETECTION",
        algorithm="numpy_localmax_v1",
        parameters={"threshold_sigma": 5.0, "max_sources_per_frame": 2000},
    )
    right = detection_run_fingerprint(
        run_kind="SOURCE_DETECTION",
        algorithm="numpy_localmax_v1",
        parameters={"max_sources_per_frame": 2000, "threshold_sigma": 5.0},
    )
    assert left == right
    assert len(left) == 64

def test_spatial_hash_greedy_matches_legacy_selection_exactly():
    from grandgousier.photometry.detection_algorithms import select_separated_candidates

    rng = random.Random(42)
    scored = [
        (1000.0 - i, rng.randrange(0, 2048), rng.randrange(0, 2048), 50000.0 - i)
        for i in range(5000)
    ]
    # Preserve the same precondition as detect_sources_in_image.
    scored.sort(reverse=True)

    for separation in (0.0, 1.0, 2.5, 5.0, 11.0):
        expected = _legacy_select(scored, max_sources=2000, min_separation_pix=separation)
        actual = select_separated_candidates(
            scored,
            max_sources=2000,
            min_separation_pix=separation,
        )
        assert actual == expected


def test_spatial_hash_preserves_strict_boundary_rule():
    from grandgousier.photometry.detection_algorithms import select_separated_candidates

    scored = [
        (10.0, 0, 0, 10.0),
        (9.0, 0, 5, 9.0),  # exactly radius=5: accepted by legacy strict < test
        (8.0, 3, 4, 8.0),  # exactly radius=5 from first: accepted unless blocked by second
    ]
    assert select_separated_candidates(scored, max_sources=10, min_separation_pix=5.0) == _legacy_select(
        scored,
        max_sources=10,
        min_separation_pix=5.0,
    )


def test_detection_and_catalog_bulk_prefetch_are_scalar_and_sky_scoped():
    detection_rows = [
        {
            "source_detection_id": 10,
            "frame_id": 1,
            "x": 11.0,
            "y": 12.0,
            "ra_deg": 10.0,
            "dec_deg": 20.0,
            "peak_adu": 123.0,
            "flux": 1000.0,
            "snr": 50.0,
            "algorithm": "numpy_localmax_v1",
        },
        {
            "source_detection_id": 20,
            "frame_id": 2,
            "x": 21.0,
            "y": 22.0,
            "ra_deg": 11.0,
            "dec_deg": 21.0,
            "peak_adu": None,
            "flux": None,
            "snr": None,
            "algorithm": "numpy_localmax_v1",
        },
    ]
    catalog_rows = [
        {
            "catalog_source_id": 100,
            "source_id": "A",
            "ra_deg": 10.0001,
            "dec_deg": 20.0001,
            "ref_mags": {"G": 12.3},
        },
    ]
    session = _FakeSession([detection_rows, catalog_rows])

    detections = _select_detections_bulk(
        session,
        frame_ids=[1, 2],
        algorithm="numpy_localmax_v1",
        min_snr=None,
    )
    catalogs = _select_catalog_sources_bulk(
        session,
        detections_by_frame=detections,
        catalog_name="cat",
        radius_arcsec=2.0,
        mag_key="G",
        working_mag_limit=19.0,
        include_catalog_source_ids=[200],
    )

    assert session.execute_calls == 2
    assert [row.source_detection_id for row in detections[1]] == [10]
    assert [row.source_detection_id for row in detections[2]] == [20]
    assert len(catalogs) == 1
    assert catalogs[0].catalog_source_id == 100
    assert catalogs[0].ref_mags == {"G": 12.3}
    sql = str(session.statements[1]).lower()
    assert "catalog_sources.ra_deg" in sql
    assert "catalog_sources.dec_deg" in sql
    assert "catalog_sources.catalog_source_id in" in sql
    assert "frame_catalog_links" not in sql
    assert "ref_mags" in sql
    assert "btrim(" in sql


def test_run_frame_diagnostics_count_unique_candidates_for_each_frame(monkeypatch):
    from types import SimpleNamespace

    import grandgousier.quality.detection_catalog_qc as module

    frames = [
        SimpleNamespace(frame_id=1, quality_flags={}),
        SimpleNamespace(frame_id=2, quality_flags={}),
    ]
    detections = {
        1: [DetectionMatchInput(101, 1, 1.0, 1.0, 5.0, 0.0, None, 100.0, 10.0, "test")],
        2: [DetectionMatchInput(201, 2, 1.0, 1.0, 25.0, 0.0, None, 100.0, 10.0, "test")],
    }
    catalog_sources = [
        CatalogMatchSource(11, "frame-1", 5.0, 0.0, {"G": 12.0}),
        CatalogMatchSource(22, "frame-2a", 25.0, 0.0, {"G": 12.0}),
        CatalogMatchSource(23, "frame-2b", 25.0 + 1.0 / 3600.0, 0.0, {"G": 12.0}),
    ]
    run_frame_diagnostics = {}
    monkeypatch.setattr(module, "_select_frames", lambda *_args, **_kwargs: frames)
    monkeypatch.setattr(
        module, "_select_detections_bulk", lambda *_args, **_kwargs: detections
    )
    monkeypatch.setattr(
        module,
        "_select_catalog_sources_bulk",
        lambda *_args, **_kwargs: catalog_sources,
    )
    monkeypatch.setattr(
        module,
        "create_detection_run",
        lambda *_args, **_kwargs: SimpleNamespace(detection_run_id=70),
    )

    def capture_run_frame(_session, *, frame_id, diagnostics, **_kwargs):
        run_frame_diagnostics[int(frame_id)] = diagnostics
        return SimpleNamespace(detection_run_frame_id=100 + int(frame_id))

    monkeypatch.setattr(module, "create_detection_run_frame", capture_run_frame)
    monkeypatch.setattr(module, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(module, "_project_catalog_sources_to_frame", lambda *_args: {})
    monkeypatch.setattr(
        module, "_upsert_detection_catalog_matches", lambda *_args, **_kwargs: 1
    )
    monkeypatch.setattr(module, "_upsert_frame_qc", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_merge_frame_quality_flags", lambda *_args: None)

    class FakeSession:
        def commit(self):
            pass

    module.match_detections_to_catalog(
        FakeSession(),
        catalog_name="cat",
        frame_ids=[1, 2],
        radius_arcsec=2.0,
    )

    assert run_frame_diagnostics[1]["n_catalog_sources"] == 1
    assert run_frame_diagnostics[2]["n_catalog_sources"] == 2


def test_direct_sky_match_is_nearest_one_to_one_and_radius_bounded(monkeypatch):
    import grandgousier.quality.detection_catalog_qc as module

    detections = [
        DetectionMatchInput(100, 1, 0.0, 0.0, 0.0, 0.0, None, 100.0, 10.0, "test"),
        DetectionMatchInput(200, 1, 1.0, 0.0, 1.5 / 3600.0, 0.0, None, 100.0, 10.0, "test"),
    ]
    sources = [
        CatalogMatchSource(10, "s10", 0.7 / 3600.0, 0.0, {"G": 12.0}),
        CatalogMatchSource(20, "s20", -0.5 / 3600.0, 0.0, {"G": 13.0}),
        CatalogMatchSource(30, "s30", 3.6 / 3600.0, 0.0, {"G": 14.0}),
    ]
    index = _CatalogSkyIndex(sources, radius_arcsec=2.0)
    pairs = _candidate_pairs(
        detections=detections,
        catalog_rows=sources,
        radius_arcsec=2.0,
        spatial_index=index,
    )
    monkeypatch.setattr(module, "_project_catalog_sources_to_frame", lambda *_args: {})

    rows = _match_frame(
        frame=object(),
        detections=detections,
        candidate_pairs=pairs,
        radius_arcsec=2.0,
        mag_key="G",
    )

    assert {
        (row["source_detection_id"], row["catalog_source_id"])
        for row in rows
    } == {(100, 20), (200, 10)}
    assert len({row["source_detection_id"] for row in rows}) == len(rows)
    assert len({row["catalog_source_id"] for row in rows}) == len(rows)
    assert all(row["sep_arcsec"] <= 2.0 for row in rows)
    assert 30 not in {row["catalog_source_id"] for row in rows}


def test_spherical_spatial_hash_checks_local_cells_not_all_catalog_pairs():
    sources = [
        CatalogMatchSource(index, str(index), 20.0 + index * 0.3, 0.0, {})
        for index in range(1000)
    ]
    sources.append(CatalogMatchSource(2000, "near", 0.0, 0.0, {}))
    index = _CatalogSkyIndex(sources, radius_arcsec=2.0)
    detection = DetectionMatchInput(1, 1, 0.0, 0.0, 0.0, 0.0, None, None, None, "test")

    pairs = _candidate_pairs(
        detections=[detection],
        catalog_rows=sources,
        radius_arcsec=2.0,
        spatial_index=index,
    )

    assert pairs == [(detection, sources[-1])]
    assert index.total_query_checks < len(sources) // 10


def test_detection_catalog_candidate_scope_wraps_ra_zero_and_expands_match_radius():
    from grandgousier.quality.detection_catalog_qc import _sky_query_boxes_for_positions

    boxes = _sky_query_boxes_for_positions(
        [(359.999, 20.0), (0.001, 20.0)],
        radius_arcsec=2.0,
    )

    assert len(boxes) == 2
    assert boxes[0].ra_min_deg > boxes[0].ra_max_deg or boxes[0].ra_max_deg == 360.0
    assert all(box.dec_min_deg < 20.0 < box.dec_max_deg for box in boxes)


def test_pipeline_quality_policy_exposes_selection_measurement_ensemble_and_science_counts():
    from grandgousier.pipeline.runner import (
        FieldPipelineConfig,
        PipelineRunResult,
        PipelineStepRecord,
        _quality_policy_summary,
    )

    result = PipelineRunResult(
        run_uid="telemetry-test",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={},
        steps=[
            PipelineStepRecord(
                "match_detections_to_catalog",
                "DONE",
                {
                    "selected_frames": 4,
                    "prefetched_detection_rows": 15,
                    "prefetched_catalog_rows": 20,
                    "selected_catalog_sources": 20,
                    "matched": 12,
                    "spatial_index_sources": 20,
                    "spatial_candidate_checks": 100,
                    "candidate_pairs": 18,
                    "frame_load_sec": 0.1,
                    "detection_load_sec": 0.2,
                    "catalog_load_sec": 0.3,
                    "matching_sec": 0.4,
                    "catalog_projection_sec": 0.05,
                },
            ),
            PipelineStepRecord(
                "aggregate_detection_support",
                "DONE",
                {"status": "DONE", "supported_catalog_sources": 4},
            ),
            PipelineStepRecord(
                "match_catalog_to_frames",
                "DONE",
                {
                    "selected_sources": 4,
                    "linked": 3,
                    "limited_links": 1,
                    "source_load_sec": 0.11,
                    "header_wcs_sec": 0.12,
                    "wcs_projection_sec": 0.13,
                    "row_build_sec": 0.14,
                    "delete_existing_sec": 0.15,
                    "staging_copy_sec": 0.16,
                    "insert_select_sec": 0.17,
                    "write_finalize_sec": 0.18,
                },
            ),
            PipelineStepRecord(
                "run_forced_photometry",
                "DONE",
                {
                    "selected_links": 3,
                    "eligible_links_before_cap": 5,
                    "selected_links_after_cap": 3,
                    "cap_dropped_links": 2,
                    "measured": 3,
                    "failed_measurements": 1,
                },
            ),
            PipelineStepRecord(
                "ensemble_calibrate",
                "DONE",
                {
                    "selected_measurements": 30,
                    "selected_objects": 10,
                    "reference_candidates": 8,
                    "usable_reference_objects": 7,
                    "selected_frames": 4,
                    "frames_with_offsets": 4,
                    "calibrated": 28,
                    "skipped_no_frame_offset": 2,
                },
            ),
            PipelineStepRecord(
                "field_qc_report",
                "DONE",
                {
                    "candidate_triage": {
                        "accepted": 2,
                        "borderline": 1,
                        "rejected": 3,
                        "rejection_counts": {"HIGH_RMS": 3},
                    }
                },
            ),
            PipelineStepRecord(
                "materialize_science_photometry",
                "DONE",
                {
                    "science_acceptance_rejection": {
                        "total_points": 40,
                        "accepted_points": 35,
                        "rejected_points": 5,
                    }
                },
            ),
        ],
    )

    diagnostics = _quality_policy_summary(
        FieldPipelineConfig(catalog_name="cat", calibration_prefix="telemetry"),
        result,
    )

    assert diagnostics["selection_diagnostics"]["detection_support"][
        "supported_catalog_sources"
    ] == 4
    assert diagnostics["selection_diagnostics"]["catalog_projection"]["limited_links"] == 1
    direct_match = diagnostics["selection_diagnostics"]["direct_detection_matching"]
    assert direct_match["loaded_detections"] == 15
    assert direct_match["loaded_catalog_sources"] == 20
    assert direct_match["matches"] == 12
    assert direct_match["spatial_candidate_checks"] == 100
    assert direct_match["candidate_pairs"] == 18
    assert direct_match["phase_timings_sec"]["catalog_projection"] == 0.05
    projection_timings = diagnostics["selection_diagnostics"]["catalog_projection"][
        "phase_timings_sec"
    ]
    assert projection_timings["source_load"] == 0.11
    assert projection_timings["wcs_projection"] == 0.13
    assert diagnostics["measurement_diagnostics"]["measured"] == 3
    assert diagnostics["measurement_diagnostics"]["failed_measurements"] == 1
    assert diagnostics["ensemble_diagnostics"]["calibrated"] == 28
    assert diagnostics["candidate_acceptance_rejection"]["rejected"] == 3
    assert diagnostics["candidate_acceptance_rejection"]["rejection_counts"] == {
        "HIGH_RMS": 3
    }
    assert diagnostics["science_acceptance_rejection"]["accepted_points"] == 35
    assert diagnostics["science_acceptance_rejection"]["rejected_points"] == 5
    assert diagnostics["detection_support_selection_enabled"] is True
    assert diagnostics["detection_support_selection"] == {
        "status": "DONE",
        "supported_catalog_sources": 4,
    }
    assert diagnostics["require_detection_support_requested"] is None
    assert diagnostics["require_detection_support_effective"] is False


def test_runner_detection_support_veto_requires_explicit_opt_in():
    from grandgousier.pipeline.runner import (
        FieldPipelineConfig,
        _effective_require_detection_support,
        _science_reject_flags,
    )

    common = {
        "catalog_name": "cat",
        "calibration_prefix": "detection-policy",
        "skip_detection": False,
        "skip_detection_catalog_match": False,
    }
    default = FieldPipelineConfig(**common)
    explicit_true = FieldPipelineConfig(**common, require_detection_support=True)
    explicit_false = FieldPipelineConfig(**common, require_detection_support=False)

    assert default.require_detection_support is None
    assert _effective_require_detection_support(default) is False
    assert _science_reject_flags(default, False).isdisjoint(
        {"NO_DETECTION_SUPPORT", "WEAK_DETECTION_SUPPORT"}
    )
    assert _effective_require_detection_support(explicit_true) is True
    assert {"NO_DETECTION_SUPPORT", "WEAK_DETECTION_SUPPORT"} <= _science_reject_flags(
        explicit_true, _effective_require_detection_support(explicit_true)
    )
    assert _effective_require_detection_support(explicit_false) is False
    assert _science_reject_flags(
        explicit_false, _effective_require_detection_support(explicit_false)
    ).isdisjoint({"NO_DETECTION_SUPPORT", "WEAK_DETECTION_SUPPORT"})



def test_detection_image_factors_frame_diagnostics_out_of_source_flags(monkeypatch):
    pytest.importorskip("astropy")
    from grandgousier.photometry import source_detection as source_module

    data = np.arange(2500, dtype=float).reshape(50, 50)
    monkeypatch.setattr(source_module, "_local_maxima", lambda *_args, **_kwargs: [(25, 25)])
    monkeypatch.setattr(
        source_module,
        "_select_separated_candidates",
        lambda scored, **_kwargs: list(scored),
    )
    monkeypatch.setattr(source_module, "_centroid_com", lambda *_args, **_kwargs: (25.0, 25.0))
    monkeypatch.setattr(
        source_module,
        "_pixel_to_world_batch",
        lambda _wcs, centered: [(None, None) for _ in centered],
    )
    monkeypatch.setattr(
        source_module,
        "_measure_detection_apertures_batch",
        lambda *_args, **_kwargs: [
            source_module._DetectionApertureMeasurement(
                flux=1234.0,
                flux_err=12.0,
                local_background=100.0,
                flags={"SATURATED": False},
            )
        ],
    )

    diagnostics = {}
    detections = source_module.detect_sources_in_image(
        data,
        wcs=None,
        threshold_sigma=5.0,
        max_sources=2000,
        min_separation_pix=5.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        centroid_box_radius_pix=3,
        exptime=30.0,
        saturation_level_adu=None,
        diagnostics_out=diagnostics,
    )

    assert len(detections) == 1
    assert detections[0].flags == {"SATURATED": False}
    assert diagnostics["centroid_method"] == "positive_com"
    assert diagnostics["n_candidates_raw_before_cap"] == 1
    assert diagnostics["max_sources_per_frame"] == 2000
    assert diagnostics["detection_cap_reached"] is False
    for repeated in (
        "background_global",
        "sigma_global",
        "threshold_adu",
        "centroid_method",
        "n_candidates_raw_before_cap",
        "max_sources_per_frame",
        "DETECTION_CAP_REACHED",
    ):
        assert repeated not in detections[0].flags


def test_detection_image_records_zero_detection_reason_when_finite_pixels_are_insufficient():
    pytest.importorskip("astropy")
    from grandgousier.photometry import source_detection as source_module

    data = np.full((8, 8), np.nan, dtype=float)
    diagnostics = {}
    detections = source_module.detect_sources_in_image(
        data,
        wcs=None,
        threshold_sigma=5.0,
        max_sources=2000,
        min_separation_pix=5.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        centroid_box_radius_pix=3,
        exptime=30.0,
        saturation_level_adu=None,
        diagnostics_out=diagnostics,
    )

    assert detections == []
    assert diagnostics == {"empty_reason": "insufficient_finite_pixels", "finite_pixels": 0}

def test_detection_copy_tuples_match_normalized_column_order_and_serialize_row_flags():
    source_row = {
        "detection_run_frame_id": 11,
        "x": 2.0,
        "y": 3.0,
        "ra_deg": 10.0,
        "dec_deg": 20.0,
        "peak_adu": 100.0,
        "flux": 90.0,
        "flux_err": 3.0,
        "local_background": 5.0,
        "snr": 30.0,
        "npix": 25,
        "detection_status": "DETECTED",
        "flags": {"a": "µ", "z": 2},
    }
    copied = source_detection_copy_tuple(source_row)
    mapped = dict(zip(SOURCE_DETECTION_WRITE_COLUMNS, copied, strict=True))
    assert len(copied) == len(SOURCE_DETECTION_WRITE_COLUMNS)
    assert mapped["detection_run_frame_id"] == 11
    assert "algorithm" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert "parameters" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert "frame_id" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert json.loads(mapped["flags"]) == {"a": "µ", "z": 2}

    match_row = {
        "detection_run_frame_id": 22,
        "source_detection_id": 2,
        "catalog_source_id": 3,
        "match_status": "MATCHED",
        "sep_arcsec": 0.2,
        "dx_pix": 0.1,
        "dy_pix": -0.1,
        "detection_mag_inst": -7.5,
        "catalog_mag": 12.0,
        "zero_point": 19.5,
    }
    copied_match = detection_catalog_match_copy_tuple(match_row)
    mapped_match = dict(zip(DETECTION_CATALOG_MATCH_WRITE_COLUMNS, copied_match, strict=True))
    assert len(copied_match) == len(DETECTION_CATALOG_MATCH_WRITE_COLUMNS)
    assert mapped_match["detection_run_frame_id"] == 22
    assert math.isclose(mapped_match["sep_arcsec"], 0.2)
    for removed in ("frame_id", "catalog_name", "algorithm", "details"):
        assert removed not in DETECTION_CATALOG_MATCH_WRITE_COLUMNS


def test_detection_copy_stages_stream_and_merge(monkeypatch):
    from grandgousier.db import detection_io

    class FakeCopy:
        def __init__(self):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write_row(self, row):
            self.rows.append(row)

    class FakeCursor:
        def __init__(self):
            self.executed = []
            self.copy_sql = []
            self.copy_objects = []
            self.closed = False

        def execute(self, sql):
            self.executed.append(sql)

        def copy(self, sql):
            self.copy_sql.append(sql)
            obj = FakeCopy()
            self.copy_objects.append(obj)
            return obj

        def close(self):
            self.closed = True

    class FakeDriverConnection:
        def __init__(self):
            self.cursors = []

        def cursor(self):
            cur = FakeCursor()
            self.cursors.append(cur)
            return cur

    driver = FakeDriverConnection()

    class FakeSAConnection:
        dialect = type("Dialect", (), {"name": "postgresql"})()
        connection = type("Proxy", (), {"driver_connection": driver})()

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

    session = FakeSession()
    assert detection_io.postgres_copy_capable(session) is True

    source_stage = detection_io.PostgresSourceDetectionCopyStage.create(session)
    source_stage.append_rows([
        {
            "detection_run_frame_id": 11,
            "x": 1.0,
            "y": 2.0,
            "ra_deg": 3.0,
            "dec_deg": 4.0,
            "peak_adu": 5.0,
            "flux": 6.0,
            "flux_err": 0.5,
            "local_background": 7.0,
            "snr": 8.0,
            "npix": 9,
            "detection_status": "DETECTED",
            "flags": {},
        }
    ])
    source_stage.merge()
    source_stage.close()
    assert source_stage.rows_staged == 1
    assert source_stage.copy_batches == 1

    match_stage = detection_io.PostgresDetectionCatalogMatchCopyStage.create(session)
    match_stage.append_rows([
        {
            "detection_run_frame_id": 22,
            "source_detection_id": 2,
            "catalog_source_id": 3,
            "match_status": "MATCHED",
            "sep_arcsec": 0.1,
            "dx_pix": 0.0,
            "dy_pix": 0.0,
            "detection_mag_inst": -5.0,
            "catalog_mag": 12.0,
            "zero_point": 17.0,
        }
    ])
    match_stage.merge()
    match_stage.close()
    assert match_stage.rows_staged == 1
    assert match_stage.copy_batches == 1

    sql = "\n".join(statement for cur in driver.cursors for statement in cur.executed)
    assert "INSERT INTO source_detections" in sql
    assert "INSERT INTO detection_catalog_matches" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_detection_catalog_matches_run_frame_catalog_source" in sql
