from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.archive.auto_process import _pipeline_row, process_archive_path
from grandgousier.catalogs.gaia_query import query_gaia_for_frames
from grandgousier.cli import build_parser
from grandgousier.artifacts import registry as artifact_registry
from grandgousier.calibration.aperture_correction import ApertureCorrectionResult
from grandgousier.photometry.error_model import PHYSICAL_APERTURE_ERROR_MODEL
from grandgousier.pipeline import runner as runner_module
from grandgousier.pipeline.runner import (
    FieldPipelineConfig,
    PipelineRunResult,
    PipelineStepRecord,
    _apply_stage2_profile,
)
from grandgousier.quality import field_report as field_report_module


def test_stage2_and_gaia_profile_defaults():
    parser = build_parser()
    archive_args = parser.parse_args(["process-archive-path", "--path", "archive"])
    handoff_args = parser.parse_args(
        ["stage1-handoff", "--execution-report", "stage1.json"]
    )
    gaia_args = parser.parse_args(["query-gaia-for-frames"])

    assert archive_args.stage2_profile == "bulk"
    assert archive_args.auto_catalog_max_rows_per_frame == 20_000
    assert handoff_args.stage2_profile == "bulk"
    assert gaia_args.max_rows_per_frame == 20_000
    assert inspect.signature(process_archive_path).parameters[
        "stage2_profile"
    ].default == "bulk"
    assert inspect.signature(process_archive_path).parameters[
        "auto_catalog_max_rows_per_frame"
    ].default == 20_000
    assert inspect.signature(query_gaia_for_frames).parameters[
        "max_rows_per_frame"
    ].default == 20_000
    assert FieldPipelineConfig(catalog_name="cat", calibration_prefix="p").stage2_profile == (
        "diagnostic"
    )


def test_bulk_profile_keeps_candidate_exclusion_input_but_skips_error_validation():
    original = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        stage2_profile="bulk",
        enable_psf_field_diagnostics=True,
        skip_aperture_stability_qc=False,
        export_candidate_light_curves=True,
        export_candidate_plots=True,
        forced_photometry_diagnostics=True,
        calibration_performance_diagnostics=True,
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_max_iterations=2,
    )

    selected, settings = _apply_stage2_profile(original)

    assert selected.stage2_profile == "bulk"
    assert selected.skip_field_report is False
    assert selected.skip_aperture_stability_qc is True
    assert selected.enable_psf_field_diagnostics is False
    assert selected.export_candidate_light_curves is False
    assert selected.export_candidate_plots is False
    assert selected.forced_photometry_diagnostics is False
    assert selected.calibration_performance_diagnostics is False
    assert settings["field_qc_detail_reports"] is False
    assert settings["field_qc_candidate_input"] is True
    assert settings["random_error_validation_and_legacy_comparison"] is False
    assert settings["random_error_validation"] is False
    assert settings["random_error_validation_status"] == "SKIPPED_BULK_PROFILE"
    assert settings["random_error_legacy_comparison"] is False
    assert settings["random_error_pairwise_residual_correlations"] is False
    assert settings["random_error_residual_floor_diagnostics"] is False
    assert settings["random_error_diagnostic_exports"] is False
    assert settings["aperture_correction_diagnostic_exports"] is False
    assert settings["light_curve_summary_row_materialization"] is False
    assert settings["light_curve_summary_compact_counts"] is True
    assert settings["light_curve_summary_json"] is False
    assert selected.random_error_model == PHYSICAL_APERTURE_ERROR_MODEL
    assert original.skip_aperture_stability_qc is False


def test_diagnostic_profile_keeps_legacy_error_comparison_enabled():
    config = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        stage2_profile="diagnostic",
    )

    selected, settings = _apply_stage2_profile(config)

    assert selected.stage2_profile == "diagnostic"
    assert settings["random_error_validation"] is True
    assert settings["random_error_validation_status"] == "ENABLED_DIAGNOSTIC_PROFILE"
    assert settings["random_error_legacy_comparison"] is True
    assert settings["random_error_pairwise_residual_correlations"] is True
    assert settings["random_error_residual_floor_diagnostics"] is True
    assert settings["random_error_validation_and_legacy_comparison"] is True


def test_bulk_aperture_correction_runs_without_outputs_or_export_tree_snapshots(
    monkeypatch, tmp_path
):
    calls = []
    snapshots = []
    log_registrations = []
    tree_snapshots = []

    class FakeSession:
        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(runner_module, "_check_open_series_admission", lambda *_: None)
    monkeypatch.setattr(runner_module, "_insert_pipeline_run", lambda *_a, **_k: 88)
    monkeypatch.setattr(runner_module, "_insert_pipeline_step", lambda *_a, **_k: 9)
    monkeypatch.setattr(runner_module, "_finish_pipeline_step", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "_finish_pipeline_run", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "_emit_pipeline_log", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "stamp_calibration_product", lambda *_a, **_k: None)

    def fake_register_logs(_session, *, log_files, **_kwargs):
        log_registrations.append(dict(log_files))
        return ["registered-log-provenance"]

    monkeypatch.setattr(runner_module, "register_run_log_artifacts", fake_register_logs)

    def fake_register_file(_session, **kwargs):
        snapshots.append(kwargs)
        return SimpleNamespace(artifact_id=len(snapshots))

    monkeypatch.setattr(artifact_registry, "register_file_artifact", fake_register_file)
    snapshot_changed_export_tree = runner_module.snapshot_changed_export_tree

    def record_tree_snapshot(_session, **kwargs):
        tree_snapshots.append(kwargs)
        return snapshot_changed_export_tree(_session, **kwargs)

    monkeypatch.setattr(
        runner_module, "snapshot_changed_export_tree", record_tree_snapshot
    )

    def fake_aperture_correction(_session, **kwargs):
        calls.append(kwargs)
        result = ApertureCorrectionResult(
            source_calibration_name=kwargs["source_calibration_name"],
            target_calibration_name=kwargs["target_calibration_name"],
            target_calibration_version_id=101,
            quality_gate_passed=False,
            reference_radius_pix=kwargs["reference_radius_pix"],
        )
        if kwargs["out_dir"] is not None:
            diagnostic_dir = Path(kwargs["out_dir"]) / "aperture_correction"
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            output_files = {
                "diagnostics_summary_json": "aperture_correction_summary.json",
                "diagnostics_frames_csv": "aperture_correction_frames.csv",
                "diagnostics_context_summary_json": "aperture_correction_context_summary.json",
                "diagnostics_context_frames_csv": "aperture_correction_context_frames.csv",
                "diagnostics_context_plot_png": "aperture_correction_context.png",
            }
            for attr, filename in output_files.items():
                path = diagnostic_dir / filename
                path.write_text("diagnostic", encoding="utf-8")
                setattr(result, attr, str(path))
        return result

    monkeypatch.setattr(
        runner_module, "apply_aperture_correction", fake_aperture_correction
    )

    results = {}
    for profile in ("bulk", "diagnostic"):
        output_dir = tmp_path / profile / "exports"
        config = FieldPipelineConfig(
            catalog_name="cat",
            calibration_prefix=f"p_{profile}",
            frame_ids=[1],
            stage2_profile=profile,
            run_pre_photometry_qc=False,
            skip_wcs=True,
            skip_catalog_frame_match=True,
            skip_detection=True,
            skip_detection_catalog_match=True,
            photometry_estimator="circular",
            skip_forced_photometry=True,
            skip_aperture_correction=False,
            skip_aperture_zp=True,
            skip_ensemble=True,
            skip_object_qc=True,
            skip_field_report=True,
            skip_aperture_stability_qc=True,
            record_run=True,
            no_file_log=True,
            log_dir=None,
            out_dir=str(output_dir),
            artifact_root=str(tmp_path / "immutable-artifacts" / profile),
        )
        results[profile] = runner_module.run_field_pipeline(FakeSession(), config)

    assert len(calls) == 2
    assert len(tree_snapshots) == 1
    assert tree_snapshots[0]["publication_root"] == tmp_path / "diagnostic" / "exports"
    assert calls[0]["out_dir"] is None
    assert calls[1]["out_dir"] == str(tmp_path / "diagnostic" / "exports")
    assert not (tmp_path / "bulk" / "exports" / "aperture_correction").exists()
    diagnostic_files = list(
        (tmp_path / "diagnostic" / "exports" / "aperture_correction").glob("*")
    )
    assert len(diagnostic_files) == 5
    assert {
        Path(item["publication_uri"]).name for item in snapshots
    } == {path.name for path in diagnostic_files}
    assert all(
        item["artifact_type"] == "APERTURE_CORRECTION_DIAGNOSTIC"
        for item in snapshots
    )
    assert all(item["pipeline_run_id"] == 88 for item in snapshots)
    assert all(Path(item["path"]).is_file() for item in snapshots)
    assert all(
        Path(item["path"]).read_text(encoding="utf-8") == "diagnostic"
        for item in snapshots
    )
    assert len(log_registrations) == 2
    assert results["bulk"].outputs["artifact_count"] == 1
    assert results["diagnostic"].outputs["artifact_count"] == 6
    assert "aperture_correction_summary_json" not in results["bulk"].outputs
    assert "aperture_correction_summary_json" in results["diagnostic"].outputs


def test_bulk_profile_disables_legacy_aperture_budget_in_forced_photometry(monkeypatch):
    observed = []

    class FakeSession:
        def rollback(self):
            pass

    monkeypatch.setattr(runner_module, "_check_open_series_admission", lambda *_: None)

    def fake_forced_photometry(_session, **kwargs):
        observed.append(kwargs["include_legacy_comparison"])
        return SimpleNamespace(
            diagnostics_summary_json=None,
            diagnostics_frames_csv=None,
            calibration_version_id=None,
        )

    monkeypatch.setattr(
        runner_module, "run_forced_aperture_photometry", fake_forced_photometry
    )
    config = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p_bulk",
        frame_ids=[1],
        stage2_profile="bulk",
        run_pre_photometry_qc=False,
        skip_wcs=True,
        skip_catalog_frame_match=True,
        skip_detection=True,
        skip_detection_catalog_match=True,
        photometry_estimator="circular",
        skip_forced_photometry=False,
        skip_aperture_correction=True,
        skip_aperture_zp=True,
        skip_ensemble=True,
        skip_object_qc=True,
        skip_field_report=True,
        skip_aperture_stability_qc=True,
        record_run=False,
        no_file_log=True,
        log_dir=None,
    )

    runner_module.run_field_pipeline(FakeSession(), config)

    assert observed == [False]


@pytest.mark.parametrize(
    "overrides",
    [
        {"skip_field_report": True},
        {"skip_object_qc": True},
        {"skip_ensemble": True},
        {"candidate_ensemble_max_iterations": 1},
    ],
)
def test_candidate_exclusion_fails_closed_when_required_inputs_are_disabled(overrides):
    options = {"candidate_ensemble_max_iterations": 2, **overrides}
    config = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        stage2_profile="bulk",
        exclude_candidates_from_ensemble=True,
        **options,
    )

    with pytest.raises(ValueError, match="exclude_candidates_from_ensemble"):
        _apply_stage2_profile(config)


def test_bulk_field_qc_skips_diagnostic_work_but_retains_candidate_rows_and_warnings(
    monkeypatch,
):
    candidate_rows = [{"object_id": 17, "candidate_status": "ACCEPTED"}]
    object_rows = [SimpleNamespace(mag_rms=0.2, mag_mad=0.1)]
    calls = {"triage": 0}

    monkeypatch.setattr(
        field_report_module,
        "resolve_source_calibration",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(field_report_module, "_calibration_chain_names", lambda *_a, **_k: ["cv"])
    monkeypatch.setattr(field_report_module, "_resolve_field_series_filter", lambda *_a, **_k: None)
    monkeypatch.setattr(field_report_module, "_frame_summary", lambda *_a, **_k: [])
    monkeypatch.setattr(field_report_module, "_frame_qc_summary", lambda *_a, **_k: {})
    monkeypatch.setattr(field_report_module, "_measurement_summary", lambda *_a, **_k: {})
    monkeypatch.setattr(field_report_module, "load_object_scatter_rows", lambda *_a, **_k: object_rows)
    monkeypatch.setattr(field_report_module, "_object_qc_summary", lambda *_a, **_k: {})

    def fake_triage(*_args, **_kwargs):
        calls["triage"] += 1
        return SimpleNamespace(rows=candidate_rows, summary={"accepted": 1})

    monkeypatch.setattr(field_report_module, "build_candidate_triage", fake_triage)
    monkeypatch.setattr(
        field_report_module,
        "_build_warnings",
        lambda **_kwargs: ["strict QC warning retained"],
    )
    for name in (
        "build_rms_mag_summary",
        "build_blend_diagnostic_rows",
        "summarize_blend_diagnostics",
        "_summary_rows",
    ):
        monkeypatch.setattr(
            field_report_module,
            name,
            lambda *_a, _name=name, **_k: pytest.fail(f"diagnostic helper ran: {_name}"),
        )

    session = SimpleNamespace(
        scalar=lambda *_args, **_kwargs: SimpleNamespace(
            calibration_version_id=1,
            name="cv",
        )
    )
    report = field_report_module.build_field_qc_report(
        session,
        calibration_name="cv",
        include_diagnostics=False,
    )

    assert calls["triage"] == 1
    assert report.candidate_rows is candidate_rows
    assert report.report["warnings"] == ["strict QC warning retained"]
    assert report.report["candidate_triage"] == {"accepted": 1}
    assert report.report["blend_diagnostics"] == {}
    assert "top_candidates" not in report.report
    assert report.rms_bins == []
    assert report.blend_rows == []
    assert report.summary_rows == []


def test_compact_bulk_rows_preserve_status_and_warning_evidence():
    result = PipelineRunResult(
        run_uid="run",
        pipeline_run_id=None,
        status="DONE_WITH_WARNINGS",
        calibration_names={"ensemble": "ens"},
        quality_policy={"pipeline_warning_summary": {"warnings": ["science incomplete"]}},
        steps=[
            PipelineStepRecord(
                step_name="validate_random_error_model",
                status="WARNING",
                result={
                    "status": "WARNING",
                    "science_status": "OK",
                    "warnings": ["legacy comparison below threshold"],
                    "row_details": [{"large": "diagnostic payload"}],
                },
            )
        ],
    )

    row = _pipeline_row({"series_id": 3}, "prefix", result, compact=True)

    assert row["status"] == "DONE_WITH_WARNINGS"
    assert row["quality_policy"]["pipeline_warning_summary"]["warnings"] == [
        "science incomplete"
    ]
    assert row["steps"][0]["status"] == "WARNING"
    assert row["steps"][0]["result"] == {
        "status": "WARNING",
        "science_status": "OK",
        "warnings": ["legacy comparison below threshold"],
    }
