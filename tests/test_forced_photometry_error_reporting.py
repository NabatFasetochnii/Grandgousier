from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("astropy")

from grandgousier.archive import auto_process as auto_process_module
from grandgousier.archive.fields_series import _series_status_from_pipeline_status
from grandgousier.photometry import forced_aperture as forced_module
from grandgousier.pipeline import runner as runner_module


class _FakeSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))


class _FakeHduList:
    def __init__(self, data):
        self._items = [SimpleNamespace(data=data, header={})]

    def __enter__(self):
        return self._items

    def __exit__(self, *_args):
        return False


def _forced_photometry_result(monkeypatch, tmp_path, scenario: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    image_path = tmp_path / "private-image-path.fits"
    image_path.touch()
    frame = SimpleNamespace(
        frame_id=7,
        raw_path=str(image_path),
        exptime=10.0,
        airmass=1.2,
        gain=1.0,
        read_noise=2.0,
        instrument_unit=SimpleNamespace(
            saturation_level_adu=None,
            nominal_gain_e_per_adu=None,
            nominal_read_noise_e=None,
        ),
    )
    link = forced_module.ForcedFrameLink(
        frame_catalog_link_id=71,
        frame_id=7,
        catalog_source_id=17,
        x=20.0,
        y=21.0,
    )
    source = forced_module.ForcedCatalogSource(
        catalog_source_id=17,
        catalog_name="test-catalog",
        source_id="opaque-test-source",
        ra_deg=1.0,
        dec_deg=2.0,
        epoch=None,
        pmra_masyr=None,
        pmdec_masyr=None,
        ref_mags={},
        colors={},
    )

    monkeypatch.setattr(forced_module, "_select_frames", lambda *_args, **_kwargs: [frame])
    monkeypatch.setattr(
        forced_module,
        "_select_frame_links_bulk",
        lambda *_args, **_kwargs: {7: [(link, source)]},
    )
    monkeypatch.setattr(
        forced_module,
        "_get_or_create_calibration_version",
        lambda *_args, **_kwargs: SimpleNamespace(calibration_version_id=91),
    )
    monkeypatch.setattr(
        forced_module,
        "_existing_calibration_measurement_count",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(forced_module, "_get_or_create_object_id", lambda *_args, **_kwargs: 55)
    monkeypatch.setattr(forced_module, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(
        forced_module,
        "upsert_measurement_rows",
        lambda _session, rows, **_kwargs: 1 if rows else 0,
    )
    monkeypatch.setattr(forced_module, "_load_detection_rows", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(forced_module, "_build_psf_references", lambda *_args, **_kwargs: [])

    if scenario == "fits_read":
        def _raise_read_error(*_args, **_kwargs):
            raise ValueError(f"private exception text: {image_path}")

        monkeypatch.setattr(forced_module.fits, "open", _raise_read_error)
    else:
        data = np.ones(32) if scenario == "invalid_geometry" else np.ones((48, 48))
        monkeypatch.setattr(
            forced_module.fits,
            "open",
            lambda *_args, **_kwargs: _FakeHduList(data),
        )

    if scenario == "row_build":
        class _FailingFlags(dict):
            def get(self, key, default=None):
                if key == "psf_fit_centroid_dx_pix":
                    raise RuntimeError(f"private row error: {image_path}")
                return super().get(key, default)

        measurement_flags = _FailingFlags()
    else:
        measurement_flags = {}
    monkeypatch.setattr(
        forced_module,
        "measure_one_source",
        lambda *_args, **_kwargs: forced_module.ApertureMeasurement(
            flux=100.0,
            flux_err=2.0,
            mag_inst=10.0,
            mag_err=0.02,
            local_background=1.0,
            aperture_radius_pix=3.0,
            flags=measurement_flags,
            error_budget={},
        ),
    )

    estimator = "adaptive_ellipse" if scenario == "no_model" else "circular"
    return forced_module.run_forced_aperture_photometry(
        _FakeSession(),
        catalog_name="test-catalog",
        frame_ids=[7],
        instrument_unit_id=1,
        photometry_estimator=estimator,
        adaptive_fallback_to_circular=(scenario != "no_model"),
        calibration_name="test-calibration",
        reuse_existing=False,
    )


def test_failure_samples_are_capped_and_count_omitted_diagnostics():
    result = forced_module.ForcedPhotometryResult()

    for frame_id in range(25):
        forced_module._record_failure_sample(
            result,
            frame_id=frame_id,
            stage="fits_read",
            reason="fits_read_failed",
            exception=OSError("path and credential details must not be retained"),
        )

    assert len(result.failure_samples) == 20
    assert result.failure_samples_truncated == 5
    assert result.failure_samples[0] == {
        "frame_id": 0,
        "stage": "fits_read",
        "reason": "fits_read_failed",
        "exception_class": "OSError",
    }
    serialized = json.dumps(result.failure_samples)
    assert "credential" not in serialized
    assert "path and" not in serialized


@pytest.mark.parametrize(
    ("scenario", "expected_stage", "expected_reason", "expected_exception"),
    [
        ("fits_read", "fits_read", "fits_read_failed", "ValueError"),
        (
            "invalid_geometry",
            "image_geometry",
            "expected_two_dimensional_image",
            None,
        ),
        (
            "no_model",
            "psf_model",
            "adaptive_psf_model_unavailable_fallback_disabled",
            None,
        ),
        (
            "row_build",
            "measurement_row_build",
            "measurement_row_build_failed",
            "RuntimeError",
        ),
    ],
)
def test_forced_photometry_reports_expected_failures_without_exception_text(
    monkeypatch,
    tmp_path,
    scenario,
    expected_stage,
    expected_reason,
    expected_exception,
):
    result = _forced_photometry_result(monkeypatch, tmp_path, scenario)

    sample = result.failure_samples[0]
    assert sample["frame_id"] == 7
    assert sample["stage"] == expected_stage
    assert sample["reason"] == expected_reason
    assert sample["exception_class"] == expected_exception
    serialized = json.dumps(sample)
    assert "private-image-path" not in serialized
    assert "private exception text" not in serialized
    assert "private row error" not in serialized
    if scenario in {"fits_read", "invalid_geometry", "no_model"}:
        assert result.failed_frames == 1
        assert result.failed_measurements == 0
        assert result.measured == 0
    elif scenario == "row_build":
        assert result.failed_frames == 0
        assert result.failed_measurements == 1
        assert result.measured == 1
    if scenario == "row_build":
        assert sample["frame_catalog_link_id"] == 71
        assert sample["catalog_source_id"] == 17


def test_forced_photometry_counters_and_measurement_counts_keep_existing_semantics(
    monkeypatch, tmp_path
):
    row_failure = _forced_photometry_result(monkeypatch, tmp_path / "row", "row_build")
    successful = _forced_photometry_result(monkeypatch, tmp_path / "success", "success")

    assert (row_failure.failed_frames, row_failure.failed_measurements) == (0, 1)
    assert row_failure.measured == 1
    assert row_failure.inserted_or_updated == 0
    assert (successful.failed_frames, successful.failed_measurements) == (0, 0)
    assert successful.measured == 1
    assert successful.inserted_or_updated == 1
    assert successful.failure_samples == []


def test_forced_step_persists_warning_status_and_emits_warning_event():
    result_json = {
        "failed_frames": 1,
        "failed_measurements": 2,
        "failure_samples": [{"frame_id": 4, "stage": "fits_read"}],
        "failure_samples_truncated": 0,
    }
    assert runner_module._completed_step_status("run_forced_photometry", result_json) == (
        "DONE_WITH_WARNINGS"
    )
    warning_level, warning_event, warning_message = runner_module._completed_step_log(
        "run_forced_photometry", "DONE_WITH_WARNINGS", result_json
    )
    assert warning_level == "WARNING"
    assert warning_event == "step_done_with_warnings"
    assert "failed_frames=1" in warning_message
    assert "failed_measurements=2" in warning_message
    assert runner_module._completed_step_status("other_step", result_json) == "DONE"
    assert runner_module._completed_step_status(
        "run_forced_photometry", {"failed_frames": 0, "failed_measurements": 0}
    ) == "DONE"

    session = _FakeSession()
    runner_module._finish_pipeline_step(
        session, 12, "DONE_WITH_WARNINGS", result_json, error_message=None
    )
    persisted_sql, persisted_params = session.calls[-1]
    assert "UPDATE pipeline_run_steps" in persisted_sql
    assert persisted_params["status"] == "DONE_WITH_WARNINGS"
    assert json.loads(persisted_params["result_json"]) == result_json

    runner_module._emit_pipeline_log(
        session,
        None,
        pipeline_run_id=3,
        pipeline_run_step_id=12,
        step_name="run_forced_photometry",
        level=warning_level,
        event_type=warning_event,
        message=warning_message,
        context=result_json,
    )
    _log_sql, log_params = session.calls[-1]
    assert log_params["level"] == "WARNING"
    assert log_params["event_type"] == "step_done_with_warnings"


def test_pipeline_log_database_binding_sanitizes_normal_and_direct_failure_routes():
    database_url = (
        "postgresql://runner:SYNTHETIC_RUNNER_URL_PASSWORD@db.example.test/pipeline"
        "?api_key=SYNTHETIC_RUNNER_URL_QUERY"
    )
    normal_context = {
        "api_token": "SYNTHETIC_RUNNER_CONTEXT_TOKEN",
        "nested": [{"detail": f"connecting to {database_url}; password=SYNTHETIC_RUNNER_TEXT_PASSWORD"}],
    }
    session = _FakeSession()

    runner_module._emit_pipeline_log(
        session,
        None,
        pipeline_run_id=13,
        pipeline_run_step_id=21,
        step_name="normal_step",
        level="INFO",
        event_type="step_done",
        message=f"completed password=SYNTHETIC_RUNNER_MESSAGE_PASSWORD; {database_url}",
        context=normal_context,
    )
    normal_sql, normal_params = session.calls[-1]
    assert "INSERT INTO pipeline_log_events" in normal_sql
    normal_context_json = json.loads(normal_params["context_json"])
    assert normal_params["message"] == (
        "completed password=***; "
        "postgresql://runner:***@db.example.test/pipeline?api_key=***"
    )
    assert normal_context_json == {
        "api_token": "***",
        "nested": [
            {
                "detail": (
                    "connecting to postgresql://runner:***@db.example.test/pipeline?api_key=***; "
                    "password=***"
                )
            }
        ],
    }

    # The runner writes failure events directly rather than through
    # _emit_pipeline_log; sanitize at the shared database-binding boundary.
    runner_module._insert_pipeline_log_event(
        session,
        pipeline_run_id=13,
        pipeline_run_step_id=22,
        level="ERROR",
        event_type="step_failed",
        message="RuntimeError: token=SYNTHETIC_DIRECT_FAILURE_TOKEN",
        context_json={
            "error_message": "authorization: Bearer SYNTHETIC_DIRECT_FAILURE_BEARER",
            "diagnostics": {"client_secret": "SYNTHETIC_DIRECT_FAILURE_SECRET", "attempt": 2},
        },
    )
    failure_sql, failure_params = session.calls[-1]
    assert "INSERT INTO pipeline_log_events" in failure_sql
    failure_context_json = json.loads(failure_params["context_json"])
    all_bound_text = json.dumps([normal_params, failure_params])
    for sentinel in (
        "SYNTHETIC_RUNNER_URL_PASSWORD",
        "SYNTHETIC_RUNNER_URL_QUERY",
        "SYNTHETIC_RUNNER_CONTEXT_TOKEN",
        "SYNTHETIC_RUNNER_TEXT_PASSWORD",
        "SYNTHETIC_RUNNER_MESSAGE_PASSWORD",
        "SYNTHETIC_DIRECT_FAILURE_TOKEN",
        "SYNTHETIC_DIRECT_FAILURE_BEARER",
        "SYNTHETIC_DIRECT_FAILURE_SECRET",
    ):
        assert sentinel not in all_bound_text
    assert failure_params["message"] == "RuntimeError: token=***"
    assert failure_context_json == {
        "error_message": "authorization: Bearer ***",
        "diagnostics": {"client_secret": "***", "attempt": 2},
    }


def test_pipeline_warning_rollup_keeps_failure_and_no_usable_precedence():
    warning_step = runner_module.PipelineStepRecord(
        step_name="run_forced_photometry",
        status="DONE_WITH_WARNINGS",
        result={
            "failed_frames": 1,
            "failed_measurements": 2,
            "failure_samples": [
                {"frame_id": frame_id, "stage": "fits_read", "reason": "fits_read_failed"}
                for frame_id in range(25)
            ],
            "failure_samples_truncated": 3,
        },
    )
    summary = runner_module._pipeline_warning_summary([warning_step])

    assert summary is not None
    assert summary["requested_pipeline_status"] == "DONE_WITH_WARNINGS"
    assert summary["failed_frames"] == 1
    assert summary["failed_measurements"] == 2
    assert summary["failure_samples_truncated"] == 8
    assert len(summary["failure_samples"]) == 20
    assert summary["failure_samples"][0]["frame_id"] == 0
    pipeline_result = runner_module.PipelineRunResult(
        run_uid="run-1",
        pipeline_run_id=None,
        status="DONE_WITH_WARNINGS",
        calibration_names={},
        steps=[warning_step],
    )
    attached_summary = runner_module._attach_pipeline_warning_summary(pipeline_result)
    assert attached_summary == pipeline_result.quality_policy["pipeline_warning_summary"]
    assert attached_summary["pipeline_run_status"] == "DONE_WITH_WARNINGS"
    assert attached_summary["pipeline_status_rollup_applied"] is True

    clean_step = runner_module.PipelineStepRecord(step_name="clean", status="DONE")
    failed_step = runner_module.PipelineStepRecord(step_name="later", status="FAILED")
    assert runner_module._pipeline_status_from_steps(
        [clean_step], stop_due_to_no_usable_frames=False
    ) == "DONE"
    assert runner_module._pipeline_status_from_steps(
        [warning_step], stop_due_to_no_usable_frames=False
    ) == "DONE_WITH_WARNINGS"
    assert runner_module._pipeline_status_from_steps(
        [warning_step], stop_due_to_no_usable_frames=True
    ) == runner_module.NO_USABLE_FRAMES_STATUS
    assert runner_module._pipeline_status_from_steps(
        [warning_step, failed_step],
        stop_due_to_no_usable_frames=False,
    ) == "FAILED"
    assert runner_module._pipeline_status_from_steps(
        [warning_step, failed_step], stop_due_to_no_usable_frames=True
    ) == "FAILED"


def test_done_with_warnings_remains_a_supported_series_status():
    assert _series_status_from_pipeline_status("DONE_WITH_WARNINGS") == "DONE_WITH_WARNINGS"


def _run_mocked_archive(monkeypatch, pipeline_result):
    detected = SimpleNamespace(path="fake-archive/frame.fits", profile_name="MASTER")
    monkeypatch.setattr(
        auto_process_module,
        "scan_archive_path",
        lambda *_args, **_kwargs: ([detected], [], []),
    )
    monkeypatch.setattr(
        auto_process_module,
        "load_auto_profiles",
        lambda **_kwargs: {"MASTER": None},
    )
    monkeypatch.setattr(
        auto_process_module,
        "index_archive_paths",
        lambda **_kwargs: SimpleNamespace(
            discovered=1, inserted=1, skipped_existing=0, failed=0
        ),
    )
    monkeypatch.setattr(auto_process_module, "_frame_ids_for_paths", lambda *_args: [100])
    monkeypatch.setattr(auto_process_module, "_series_ids_for_paths", lambda *_args: [12])
    monkeypatch.setattr(
        auto_process_module,
        "_inspect_existing_catalog",
        lambda *_args, **_kwargs: {"status": "DONE", "n_sources": 1},
    )
    monkeypatch.setattr(auto_process_module, "_catalog_precondition_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(
        auto_process_module,
        "_frame_ids_by_series_for_paths",
        lambda *_args: {12: [100]},
    )
    monkeypatch.setattr(
        auto_process_module,
        "_load_series_for_pipeline",
        lambda *_args: [
            {
                "series_id": 12,
                "field_id": 3,
                "field_name": "mock-field",
                "instrument": "MASTER",
                "filter_name": "V",
            }
        ],
    )
    monkeypatch.setattr(
        auto_process_module, "_format_calibration_prefix", lambda *_args: "series_12"
    )
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args: pipeline_result,
    )

    return auto_process_module.process_archive_path(
        _FakeSession(),
        root="fake-archive",
        catalog_name="fake-catalog",
        execute=True,
        series_ids=[12],
        make_light_curve_summary=False,
        out_root="",
        log_dir="",
    )


def test_process_archive_path_rolls_up_warning_pipeline_and_exposes_bounded_diagnostics(
    monkeypatch,
):
    warning_step = runner_module.PipelineStepRecord(
        step_name="run_forced_photometry",
        status="DONE_WITH_WARNINGS",
        result={
            "failed_frames": 2,
            "failed_measurements": 5,
            "failure_samples": [
                {"frame_id": frame_id, "stage": "fits_read", "reason": "read_failed"}
                for frame_id in range(20)
            ],
            "failure_samples_truncated": 4,
        },
    )
    pipeline_result = runner_module.PipelineRunResult(
        run_uid="run-1",
        pipeline_run_id=7,
        status="DONE_WITH_WARNINGS",
        calibration_names={},
        steps=[warning_step],
    )
    runner_module._attach_pipeline_warning_summary(pipeline_result)

    result = _run_mocked_archive(monkeypatch, pipeline_result)

    assert result.status == "DONE_WITH_WARNINGS"
    assert result.pipeline_results[0]["status"] == "DONE_WITH_WARNINGS"
    assert result.pipeline_results[0]["science_status"] == "NO_LIGHT_CURVE_SUMMARY"
    summary = result.pipeline_results[0]["quality_policy"]["pipeline_warning_summary"]
    assert summary["pipeline_run_status"] == "DONE_WITH_WARNINGS"
    assert summary["pipeline_status_rollup_applied"] is True
    assert len(summary["failure_samples"]) == 20
    assert summary["failure_samples_truncated"] == 4
    assert len(result.warnings) == 2
    warning = next(item for item in result.warnings if "pipeline_status=" in item)
    assert "failed_frames=2" in warning
    assert "failed_measurements=5" in warning
    assert "frame_id=0 stage=fits_read" in warning
    assert "frame_id=2 stage=fits_read" in warning
    assert "frame_id=3" not in warning
    assert "samples_truncated=4" in warning
    assert "fake-archive" not in warning
    assert "series_id=12 science_status=NO_LIGHT_CURVE_SUMMARY" in result.warnings


@pytest.mark.parametrize(
    ("pipeline_status", "expected_archive_status"),
    [
        ("FAILED", "FAILED"),
        ("DONE_NO_SCIENCE_FRAMES", "DONE_WITH_WARNINGS"),
    ],
)
def test_process_archive_path_does_not_accept_failed_rows_and_preserves_no_science_status(
    monkeypatch, pipeline_status, expected_archive_status
):
    pipeline_result = runner_module.PipelineRunResult(
        run_uid="run-1",
        pipeline_run_id=7,
        status=pipeline_status,
        calibration_names={},
    )

    result = _run_mocked_archive(monkeypatch, pipeline_result)

    assert result.status == expected_archive_status
    assert result.pipeline_results[0]["status"] == pipeline_status
    if pipeline_status == "DONE_NO_SCIENCE_FRAMES":
        assert result.pipeline_results[0]["science_status"] == "BAD_FRAME_QC"


def test_process_archive_cli_returns_success_for_warning_result(monkeypatch, capsys):
    from grandgousier import cli

    args = cli.build_parser().parse_args(
        ["process-archive-path", "--path", "fake-archive", "--format", "json"]
    )
    archive_result = SimpleNamespace(
        status="DONE_WITH_WARNINGS",
        to_dict=lambda: {"status": "DONE_WITH_WARNINGS", "warnings": ["forced photometry warning"]},
    )

    class SessionContext:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli, "make_session_factory", lambda _url: lambda: SessionContext())
    monkeypatch.setattr(
        auto_process_module, "process_archive_path", lambda *_args, **_kwargs: archive_result
    )

    assert cli.cmd_process_archive_path(args) == 0
    output = capsys.readouterr().out
    assert '"status": "DONE_WITH_WARNINGS"' in output
    assert "forced photometry warning" in output
