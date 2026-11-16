from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest

from grandgousier.catalogs.gaia_query import GaiaWorkingCatalog
from grandgousier.pipeline import runner


def _gaia_row(
    source_id: str, ra_deg: float, magnitude: float = 15.0
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "ra": ra_deg,
        "dec": 0.0,
        "phot_g_mean_mag": magnitude,
    }


@dataclass(frozen=True)
class _Detection:
    source_detection_id: int
    ra_deg: float
    dec_deg: float = 0.0


def _config(**overrides) -> runner.FieldPipelineConfig:
    values = {
        "catalog_name": "gaia_dr3",
        "calibration_prefix": "working-gaia-test",
        "frame_ids": [11, 12],
        "run_pre_photometry_qc": False,
        "skip_wcs": True,
        "skip_catalog_frame_match": False,
        "skip_detection": False,
        "skip_detection_catalog_match": False,
        "skip_forced_photometry": True,
        "skip_aperture_correction": True,
        "skip_aperture_zp": True,
        "skip_ensemble": True,
        "skip_object_qc": True,
        "skip_aperture_stability_qc": True,
        "skip_field_report": True,
        "export_target_light_curve": False,
        "export_candidate_light_curves": False,
        "record_run": False,
        "no_file_log": True,
    }
    values.update(overrides)
    return runner.FieldPipelineConfig(**values)


def _set_new_promotion_diagnostics(diagnostics_out, source_ids):
    if diagnostics_out is not None:
        diagnostics_out.update(
            promoted_new=len(source_ids), promoted_existing=0
        )


def _install_fake_runner(monkeypatch, detections_by_frame, events=None):
    events = events if events is not None else []
    monkeypatch.setattr(runner, "_check_open_series_admission", lambda *_args: None)

    def select_frames(_session, **kwargs):
        events.append("load_eligible_frames")
        assert kwargs["only_wcs_ok"] is True
        return [SimpleNamespace(frame_id=frame_id) for frame_id in kwargs["frame_ids"]]

    def select_detections(_session, **kwargs):
        events.append("load_detection_rows")
        assert kwargs["algorithm"] == "numpy_localmax_v1"
        assert kwargs["min_snr"] is None
        assert kwargs["detection_run_id"] == 900
        return detections_by_frame

    monkeypatch.setattr(runner, "_select_detection_frames", select_frames)
    monkeypatch.setattr(runner, "_select_detections_bulk", select_detections)
    monkeypatch.setattr(
        runner,
        "detect_sources_in_frames",
        lambda *_args, **_kwargs: events.append("detect_sources")
        or SimpleNamespace(detection_run_id=900),
    )
    def match_catalog_detections(_session, **kwargs):
        events.append("db_detection_catalog_match")
        assert kwargs["source_detection_run_id"] == 900
        return SimpleNamespace(detection_run_id=901)

    monkeypatch.setattr(
        runner, "match_detections_to_catalog", match_catalog_detections
    )
    return events


def test_working_gaia_selection_is_ordered_per_frame_and_projects_only_promoted_ids(
    monkeypatch,
):
    source_id = "9007199254740993"
    catalog = GaiaWorkingCatalog([_gaia_row(source_id, 10.0)])
    detections = {
        11: [_Detection(101, 10.0)],
        12: [_Detection(202, 10.0)],
    }
    events = _install_fake_runner(monkeypatch, detections)
    per_frame_matches = []
    original_match = GaiaWorkingCatalog.select_unique_detection_matches

    def record_frame_match(self, *args, **kwargs):
        matches, diagnostics = original_match(self, *args, **kwargs)
        per_frame_matches.append(matches)
        return matches, diagnostics

    monkeypatch.setattr(
        GaiaWorkingCatalog, "select_unique_detection_matches", record_frame_match
    )
    promoted_inputs = []

    def promote(_session, actual_catalog, source_ids, *, diagnostics_out=None):
        events.append("promote")
        assert actual_catalog is catalog
        promoted_inputs.append(list(source_ids))
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {source_id: 701}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    projected = []

    def project(_session, **kwargs):
        events.append("project")
        projected.append(kwargs)
        return {"linked": 2}

    monkeypatch.setattr(runner, "match_catalog_to_frames", project)
    promoted_identity_capture = set()

    result = runner.run_field_pipeline(
        object(),
        _config(),
        working_catalog=catalog,
        working_catalog_promoted_source_ids=promoted_identity_capture,
    )

    assert promoted_identity_capture == {source_id}
    assert source_id not in json.dumps(asdict(result))
    assert [match[0][1] for match in per_frame_matches] == [source_id, source_id]
    assert promoted_inputs == [[source_id]]
    assert len(projected) == 1
    assert projected[0]["catalog_name"] == "gaia_dr3"
    assert projected[0]["catalog_source_ids"] == [701]
    assert projected[0]["frame_ids"] == [11, 12]
    assert events == [
        "detect_sources",
        "load_eligible_frames",
        "load_detection_rows",
        "promote",
        "project",
        "db_detection_catalog_match",
    ]
    assert result.status == "DONE"


def test_only_matched_working_sources_are_promoted_and_projected(monkeypatch):
    catalog = GaiaWorkingCatalog(
        [_gaia_row("101", 10.0), _gaia_row("202", 20.0)]
    )
    _install_fake_runner(monkeypatch, {11: [_Detection(101, 10.0)], 12: []})
    promoted_inputs = []

    def promote(_session, _catalog, source_ids, *, diagnostics_out=None):
        promoted_inputs.append(list(source_ids))
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {"101": 501}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    projected = []
    monkeypatch.setattr(
        runner,
        "match_catalog_to_frames",
        lambda _session, **kwargs: projected.append(kwargs) or {"linked": 1},
    )

    runner.run_field_pipeline(object(), _config(), working_catalog=catalog)

    assert promoted_inputs == [["101"]]
    assert projected[0]["catalog_source_ids"] == [501]


def test_selected_reference_callback_runs_after_promotion_and_preserves_ensemble_fallback(
    monkeypatch,
):
    catalog = GaiaWorkingCatalog([_gaia_row("101", 10.0)])
    events = _install_fake_runner(monkeypatch, {11: [_Detection(101, 10.0)]})
    def promote(_session, _catalog, source_ids, *, diagnostics_out=None):
        events.append("persist_selected")
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {source_id: 501 for source_id in source_ids}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    monkeypatch.setattr(runner, "match_catalog_to_frames", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runner,
        "apply_color_calibration",
        lambda *_args, **_kwargs: pytest.fail(
            "color calibration must not run when selected references are unavailable"
        ),
    )
    step_names = {}

    def insert_step(_session, _run_id, step_name, order_index):
        step_names[order_index] = step_name
        return order_index

    def finish_step(_session, step_id, _status, _payload, _error):
        if step_names[step_id] == "select_and_promote_working_gaia_sources":
            events.append("selection_commit")

    monkeypatch.setattr(runner, "_insert_pipeline_run", lambda *_args, **_kwargs: 9000)
    monkeypatch.setattr(runner, "_insert_pipeline_step", insert_step)
    monkeypatch.setattr(runner, "_finish_pipeline_step", finish_step)
    monkeypatch.setattr(runner, "_emit_pipeline_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_run", lambda *_args, **_kwargs: None)
    ensemble_calls = []

    def fake_ensemble(*_args, **_kwargs):
        ensemble_calls.append("ensemble")
        return SimpleNamespace(
            target_calibration_version_id=1,
            reference_object_ids=[1],
            reference_candidates=1,
            usable_reference_objects=1,
            selected_frames=1,
            frames_with_offsets=1,
            calibrated=1,
        )

    monkeypatch.setattr(runner, "ensemble_calibrate_measurements", fake_ensemble)
    monkeypatch.setattr(runner, "stamp_calibration_product", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "load_calibration_analysis_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(timings_sec={}),
    )
    monkeypatch.setattr(
        runner,
        "validate_random_error_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            summary_json=None,
            objects_csv=None,
            residual_floor_summary_json=None,
            residual_floor_objects_csv=None,
            residual_floor_frames_csv=None,
        ),
    )
    monkeypatch.setattr(
        runner,
        "materialize_science_photometry",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="DONE",
            science_rows_after=0,
            pruned_non_science_rows=0,
            to_dict=lambda: {},
        ),
    )
    callback_source_sets = []

    def reference_callback(source_ids):
        events.append("reference_callback")
        callback_source_sets.append(set(source_ids))
        return {
            "status": "MISSING_REFERENCE_PHOTOMETRY",
            "ready": False,
            "reason": "provider data unavailable",
        }

    class _Session:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def commit(self):
            return None

        def rollback(self):
            return None

    result = runner.run_field_pipeline(
        _Session(),
        _config(
            enable_color_calibration=True,
            color_calibration_requested=True,
            color_reference_catalog_name="panstarrs_dr2",
            color_reference_ready=None,
            record_run=True,
            skip_ensemble=False,
            ensemble_min_ref_stars_per_frame=1,
        ),
        working_catalog=catalog,
        reference_photometry_callback=reference_callback,
    )

    assert events.index("selection_commit") < events.index("reference_callback")
    assert callback_source_sets == [{"101"}]
    assert any(
        step.step_name == "ensure_selected_reference_photometry"
        for step in result.steps
    )
    assert not any(step.step_name == "apply_color_calibration" for step in result.steps)
    assert ensemble_calls == ["ensemble"]
    assert result.quality_policy["color_calibration_enabled_effective"] is False
    assert result.quality_policy["color_reference_ready"] is False
    assert result.quality_policy["color_calibration_skip_reason"] == "provider data unavailable"
    assert result.quality_policy["science_calibration"] == result.calibration_names["ensemble"]


def test_detection_matches_over_working_magnitude_limit_are_not_promoted(monkeypatch):
    catalog = GaiaWorkingCatalog(
        [_gaia_row("101", 10.0, 14.0), _gaia_row("202", 20.0, 18.0)]
    )
    _install_fake_runner(
        monkeypatch,
        {11: [_Detection(101, 10.0)], 12: [_Detection(202, 20.0)]},
    )
    promoted_inputs = []

    def promote(_session, _catalog, source_ids, *, diagnostics_out=None):
        promoted_inputs.append(list(source_ids))
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {"101": 501}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    projected = []
    monkeypatch.setattr(
        runner,
        "match_catalog_to_frames",
        lambda _session, **kwargs: projected.append(kwargs) or {"linked": 1},
    )

    runner.run_field_pipeline(
        object(),
        _config(working_catalog_mag_limit=16.0),
        working_catalog=catalog,
    )

    assert promoted_inputs == [["101"]]
    assert projected[0]["catalog_source_ids"] == [501]
    assert projected[0]["forced_catalog_source_ids"] is None


def test_exact_requested_target_is_added_to_selection_even_without_a_detection_match(
    monkeypatch,
):
    catalog = GaiaWorkingCatalog(
        [_gaia_row("101", 10.0, 15.0), _gaia_row("202", 30.0, 20.0)]
    )
    _install_fake_runner(monkeypatch, {11: [_Detection(101, 10.0)], 12: []})
    promoted_inputs = []

    def promote(_session, _catalog, source_ids, *, diagnostics_out=None):
        promoted_inputs.append(list(source_ids))
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {"101": 501, "202": 502}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    projected = []
    monkeypatch.setattr(
        runner,
        "match_catalog_to_frames",
        lambda _session, **kwargs: projected.append(kwargs) or {"linked": 2},
    )

    result = runner.run_field_pipeline(
        object(),
        _config(target_source_id="202", working_catalog_mag_limit=16.0),
        working_catalog=catalog,
    )

    assert promoted_inputs == [["101", "202"]]
    assert projected[0]["catalog_source_ids"] == [501, 502]
    assert projected[0]["forced_catalog_source_ids"] == [502]
    selection = next(
        step.result
        for step in result.steps
        if step.step_name == "select_and_promote_working_gaia_sources"
    )
    assert selection["explicit_target_included"] is True
    assert selection["selected_gaia_source_count"] == 2
    assert selection["matching_diagnostics"] == {
        "detections_considered": 1,
        "reciprocal_unique_matches": 1,
        "ambiguous_detections": 0,
        "conflicting_gaia_sources": 0,
    }
    assert selection["automatic_unique_sources"] == 1
    assert "target_source_included" not in selection
    assert "automatic_selected_unique_sources" not in selection
    assert selection["promoted_new"] == 2
    assert selection["promoted_existing"] == 0
    assert json.loads(json.dumps(selection)) == selection
    assert "202" not in json.dumps(selection)


def test_forced_target_survives_automatic_candidate_ambiguity_and_is_reported(monkeypatch):
    target_source_id = "202"
    catalog = GaiaWorkingCatalog(
        [
            _gaia_row("101", 10.0),
            _gaia_row(target_source_id, 10.0 + 0.25 / 3600.0),
        ]
    )
    _install_fake_runner(monkeypatch, {11: [_Detection(101, 10.0)], 12: []})
    promoted_inputs = []

    def promote(_session, _catalog, source_ids, *, diagnostics_out=None):
        promoted_inputs.append(list(source_ids))
        _set_new_promotion_diagnostics(diagnostics_out, source_ids)
        return {target_source_id: 502}

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", promote)
    projected = []
    monkeypatch.setattr(
        runner,
        "match_catalog_to_frames",
        lambda _session, **kwargs: projected.append(kwargs) or {"linked": 1},
    )

    result = runner.run_field_pipeline(
        object(),
        _config(target_source_id=target_source_id),
        working_catalog=catalog,
    )

    selection = next(
        step.result
        for step in result.steps
        if step.step_name == "select_and_promote_working_gaia_sources"
    )
    assert promoted_inputs == [[target_source_id]]
    assert projected[0]["catalog_source_ids"] == [502]
    assert projected[0]["forced_catalog_source_ids"] == [502]
    assert selection["matching_diagnostics"] == {
        "detections_considered": 1,
        "reciprocal_unique_matches": 0,
        "ambiguous_detections": 1,
        "conflicting_gaia_sources": 0,
    }
    assert selection["automatic_unique_sources"] == 0
    assert selection["explicit_target_included"] is True
    assert selection["selected_gaia_source_count"] == 1
    assert target_source_id not in json.dumps(selection)


def test_missing_requested_target_fails_before_pipeline_db_work(monkeypatch):
    catalog = GaiaWorkingCatalog([_gaia_row("101", 10.0)])
    database_admission_calls = []
    monkeypatch.setattr(
        runner,
        "_check_open_series_admission",
        lambda *_args: database_admission_calls.append("called"),
    )

    with pytest.raises(ValueError, match="target_source_id is absent"):
        runner.run_field_pipeline(
            object(),
            _config(target_source_id="202"),
            working_catalog=catalog,
        )

    assert database_admission_calls == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"catalog_name": "gaia_dr2"}, "requires catalog_name='gaia_dr3'"),
        ({"dry_run": True}, "does not support dry-run"),
        ({"skip_detection": True}, "requires source detection"),
        ({"skip_catalog_frame_match": True}, "requires catalog frame projection"),
        (
            {"skip_detection_catalog_match": True},
            "requires detection/catalog matching",
        ),
    ],
)
def test_incompatible_working_catalog_settings_are_rejected_before_db_work(
    monkeypatch, override, message
):
    catalog = GaiaWorkingCatalog([_gaia_row("101", 10.0)])
    database_admission_calls = []
    monkeypatch.setattr(
        runner,
        "_check_open_series_admission",
        lambda *_args: database_admission_calls.append("called"),
    )

    with pytest.raises(ValueError, match=message):
        runner.run_field_pipeline(object(), _config(**override), working_catalog=catalog)

    assert database_admission_calls == []


def test_empty_selection_keeps_existing_frame_links_available_without_projection(
    monkeypatch,
):
    catalog = GaiaWorkingCatalog([_gaia_row("101", 10.0)])
    events = _install_fake_runner(monkeypatch, {11: [], 12: []})

    def unexpected_promotion(*_args, **_kwargs):
        pytest.fail("an empty selection must not be promoted")

    def unexpected_projection(*_args, **_kwargs):
        pytest.fail("an empty selection must not invoke scoped projection")

    monkeypatch.setattr(runner, "promote_selected_gaia_sources", unexpected_promotion)
    monkeypatch.setattr(runner, "match_catalog_to_frames", unexpected_projection)

    result = runner.run_field_pipeline(object(), _config(), working_catalog=catalog)

    assert "db_detection_catalog_match" in events
    selection = next(
        step.result
        for step in result.steps
        if step.step_name == "select_and_promote_working_gaia_sources"
    )
    assert selection["selected_gaia_source_count"] == 0
    assert selection["promotion_status"] == "SKIPPED_NO_SELECTION"
    assert selection["matching_diagnostics"] == {
        "detections_considered": 0,
        "reciprocal_unique_matches": 0,
        "ambiguous_detections": 0,
        "conflicting_gaia_sources": 0,
    }
    assert selection["automatic_unique_sources"] == 0
    assert selection["explicit_target_included"] is False
    assert selection["promoted_new"] == 0
    assert selection["promoted_existing"] == 0


def test_working_catalog_and_arrays_never_enter_recipe_command_or_result_json(
    monkeypatch,
):
    source_id = "9007199254740993"
    catalog = GaiaWorkingCatalog([_gaia_row(source_id, 10.0)])
    _install_fake_runner(monkeypatch, {11: [_Detection(101, 10.0)], 12: []})
    monkeypatch.setattr(
        runner,
        "promote_selected_gaia_sources",
        lambda *_args, **_kwargs: {source_id: 701},
    )
    monkeypatch.setattr(
        runner,
        "match_catalog_to_frames",
        lambda *_args, **_kwargs: {"linked": 1},
    )
    captured_recipe = {}
    captured_summary = {}

    def capture_recipe(_session, **kwargs):
        captured_recipe.update(kwargs["recipe_json"])
        return 701

    def capture_finished_run(_session, _run_id, _status, summary_json):
        captured_summary.update(summary_json)

    class _FakeSession:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def commit(self):
            return None

        def rollback(self):
            return None

    monkeypatch.setattr(runner, "load_series_selection", lambda *_args: {
        "field_id": 44,
        "instrument_unit_id": 4,
        "frame_ids": [11, 12],
        "status": "READY",
    })
    monkeypatch.setattr(runner, "_check_open_series_admission", lambda *_args: None)
    monkeypatch.setattr(
        runner, "mark_series_pipeline_started", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runner, "mark_series_pipeline_result", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runner, "_insert_pipeline_run", capture_recipe)
    monkeypatch.setattr(runner, "_insert_pipeline_step", lambda *_args, **_kwargs: 702)
    monkeypatch.setattr(runner, "_finish_pipeline_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_finish_pipeline_run", capture_finished_run)
    monkeypatch.setattr(runner, "_emit_pipeline_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "register_run_log_artifacts", lambda *_args, **_kwargs: [])
    config = _config(
        record_run=True,
        field_id=44,
        series_id=17,
        command_json={"command": "test"},
        local_com_max_shift_pix=1.25,
        frame_shift_min_matches=24,
        frame_shift_max_robust_scatter_pix=0.3,
        frame_shift_max_absolute_pix=1.5,
        crowded_arcsec=12.0,
        blended_arcsec=6.0,
        dynamic_separation=False,
        dynamic_seeing_factor=1.7,
        dynamic_aperture_factor=0.8,
        dynamic_crowding_factor=1.3,
        fwhm_header_unit="pix",
    )
    compact_provenance = {
        "provenance_version": 1,
        "field_id": 44,
        "field_frame_count": 2,
        "frame_scope_sha256": "a" * 64,
        "footprint_sha256": "b" * 64,
        "gaia_release": "DR3",
        "gaia_table": "gaiadr3.gaia_source",
        "requested_columns_sha256": "c" * 64,
        "fetch_status": "SUCCEEDED_COMPLETE",
        "fetch_complete": True,
        "unique_sources": 1,
    }
    run_ids_by_series = {}

    result = runner.run_field_pipeline(
        _FakeSession(),
        config,
        working_catalog=catalog,
        working_catalog_provenance=compact_provenance,
        working_catalog_run_ids_by_series=run_ids_by_series,
    )

    recipe_json = json.dumps(captured_recipe)
    command_json = json.dumps(captured_recipe["command_json"])
    result_json = json.dumps(asdict(result))
    summary_json = json.dumps(captured_summary)
    assert "working_catalog" not in captured_recipe
    assert "catalog_source_ids" not in captured_recipe
    assert "working_catalog_provenance" not in asdict(config)
    assert captured_recipe["working_catalog_provenance"] == compact_provenance
    assert captured_recipe["quality_policy"]["working_catalog_provenance"] == (
        compact_provenance
    )
    assert run_ids_by_series == {17: 701}
    assert captured_recipe["astrometric_centering_algorithm"] == "gaia_wcs_com_v1"
    effective_centering = captured_recipe["quality_policy"]["astrometric_centering"]
    assert effective_centering["local_com_max_shift_pix"] == pytest.approx(1.25)
    assert effective_centering["frame_shift_min_matches"] == 24
    assert effective_centering["frame_shift_max_robust_scatter_pix"] == pytest.approx(0.3)
    assert effective_centering["frame_shift_max_absolute_pix"] == pytest.approx(1.5)
    assert effective_centering["crowded_arcsec"] == pytest.approx(12.0)
    assert effective_centering["blended_arcsec"] == pytest.approx(6.0)
    assert effective_centering["dynamic_separation"] is False
    assert effective_centering["dynamic_seeing_factor"] == pytest.approx(1.7)
    assert effective_centering["dynamic_aperture_factor"] == pytest.approx(0.8)
    assert effective_centering["dynamic_crowding_factor"] == pytest.approx(1.3)
    assert effective_centering["fwhm_header_unit"] == "pix"
    assert result.quality_policy["working_catalog_provenance"] == compact_provenance
    assert captured_summary["quality_policy"]["working_catalog_provenance"] == (
        compact_provenance
    )
    assert source_id not in recipe_json
    assert source_id not in command_json
    assert source_id not in result_json
    assert source_id not in summary_json
