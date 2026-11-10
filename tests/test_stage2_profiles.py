from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.archive.auto_process import _pipeline_row, process_archive_path
from grandgousier.catalogs.gaia_query import query_gaia_for_frames
from grandgousier.cli import build_parser
from grandgousier.photometry.error_model import PHYSICAL_APERTURE_ERROR_MODEL
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


def test_bulk_profile_keeps_candidate_exclusion_input_and_random_error_validation():
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
    assert settings["random_error_validation"] is True
    assert settings["random_error_legacy_comparison"] is False
    assert settings["random_error_pairwise_residual_correlations"] is False
    assert settings["random_error_residual_floor_diagnostics"] is True
    assert settings["random_error_diagnostic_exports"] is False
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
    assert settings["random_error_legacy_comparison"] is True
    assert settings["random_error_pairwise_residual_correlations"] is True
    assert settings["random_error_residual_floor_diagnostics"] is True
    assert settings["random_error_validation_and_legacy_comparison"] is True


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
