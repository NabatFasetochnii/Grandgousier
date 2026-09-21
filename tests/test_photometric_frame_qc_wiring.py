from __future__ import annotations

from pathlib import Path


def _text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_cli_and_process_archive_expose_single_opt_in_switch():
    cli = _text("grandgousier/cli.py")
    auto = _text("grandgousier/archive/auto_process.py")
    assert cli.count('"--enable-photometric-frame-qc"') == 2
    assert "enable_photometric_frame_qc=args.enable_photometric_frame_qc" in cli
    assert "enable_photometric_frame_qc: bool = False" in auto
    assert "enable_photometric_frame_qc=enable_photometric_frame_qc" in auto
    assert "photometric_frame_qc_sigma_frame_diag_median" in auto
    assert "photometric_frame_qc_absolute_bad_limit" in auto
    assert "candidate_systematics_sensitive" in auto
    assert "photometric_frame_qc_error_policy" in auto
    assert "photometric_frame_qc_psf_summaries" in auto


def test_runner_uses_new_photqc_generation_and_dedicated_psf_summary():
    runner = _text("grandgousier/pipeline/runner.py")
    assert "apply_photometric_frame_qc(" in runner
    assert "_photqc_v3" in runner
    assert "_pre_photqc_v3" in runner
    assert "_photqc_v1" not in runner
    assert 'psf_frames_csv=outputs.get("psf_frames_csv")' in runner
    assert "BAD_FRAME_REJECT_FLAG" in runner
    assert "mutually exclusive" in runner
    assert "enable_spatial_ensemble and config.enable_photometric_frame_qc" in runner
    assert '"hard_failures_only_v2"' in runner
    assert "_apply_good_frame_candidate_confirmation" in runner
    assert "candidate_good_frame_confirmation" in runner
    assert 'row[f"good_frame_{field_name}"] = (good_row or {}).get(field_name)' in runner


def test_reconstruction_preserves_good_and_suspect_errors_and_keeps_frame_scatter_diagnostic_only():
    source = _text("grandgousier/calibration/reconstruction.py")
    start = source.index("def _apply_photometric_frame_qc(")
    end = source.index("def _apply_spatial_ensemble(", start)
    block = source[start:end]
    assert 'iteration_kind == "PHOTOMETRIC_FRAME_QC"' in source
    assert 'row["mag_cal"] = None' in block
    assert 'status == "BAD"' in block
    assert 'row["mag_err_random"] = detector_random_err' in block
    assert 'row["mag_err"] = prior_mag_err' in block
    assert 'row["mag_err_systematic"] = prior_systematic' in block
    assert '"photometric_frame_qc_sigma_frame_diagnostic_mag": sigma_frame' in block
    assert '"photometric_frame_qc_error_policy": "diagnostic_only_v3"' in block
    assert "_quadrature(detector_random_err, sigma_frame)" not in block
    assert 'row["mag_cal"] = _add' not in block
    assert "spatial_ensemble_prediction" not in block


def test_frame_qc_policy_is_hard_reject_only_and_persists_psf_payloads_without_schema_change():
    source = _text("grandgousier/calibration/photometric_frame_qc.py")
    assert 'PHOTOMETRIC_FRAME_QC_ALGORITHM = "post_ensemble_photometric_frame_qc_v3"' in source
    assert 'PHOTOMETRIC_FRAME_QC_FLAG = "frame_photometric_qc_v1"' in source
    assert 'FRAME_PSF_SUMMARY_FLAG = "frame_psf_summary_v1"' in source
    assert 'iteration_kind="PHOTOMETRIC_FRAME_QC"' in source
    assert '"rejection_semantics": "relative residual and spatial anomalies are SUSPECT-only; BAD requires absolute/hard failure"' in source
    assert 'reasons.append("ABSOLUTE_HIGH_RESIDUAL_SCATTER")' in source
    assert 'reasons.append("SEVERE_SPATIAL_SYSTEMATIC")' in source
    assert 'psf_frames_csv: str | Path | None = None' in source
    assert '"fwhm_spatial_peak_to_peak_pix"' in source
    assert '"magnitude_correction": "none"' in source
    assert "UPDATE frames" in source
    assert "ALTER TABLE" not in source


def test_candidate_confirmation_is_applied_before_reference_exclusion_and_export():
    runner = _text("grandgousier/pipeline/runner.py")
    field_pos = runner.index("confirmed_rows, confirmation_summary = _good_frame_confirmed_candidate_rows")
    exclusion_pos = runner.index("def _select_candidate_reference_exclusions_step", field_pos)
    export_pos = runner.index("def _candidate_light_curve_export_step", exclusion_pos)
    assert field_pos < exclusion_pos < export_pos
    assert 'candidate_rows=confirmed_rows' in runner
    assert 'annotate_candidate_axis_correlations(' in runner
    assert 'candidate_rows=correlation_rows' in runner
    assert '"diagnostic_only_no_error_inflation_v3"' in runner
