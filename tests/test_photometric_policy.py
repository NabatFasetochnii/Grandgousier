import pytest

pytest.importorskip("astropy")

from grandgousier.pipeline.runner import (
    FieldPipelineConfig,
    PipelineRunResult,
    PipelineStepRecord,
    _color_calibration_target_name,
    _quality_policy_summary,
    _validate_color_calibration_config,
)


def test_color_target_name_is_source_specific_and_stable():
    assert _color_calibration_target_name(
        "ensemble_candidate_excluded_iter6_v1",
        reference_catalog_name="Pan-STARRS DR2",
        reference_band="i",
    ) == "ensemble_candidate_excluded_iter6_v1_pan_starrs_dr2_i_color_v3"


def test_color_target_name_uses_provider_label_and_fits_database_limit():
    source = (
        "auto_robophot_20260328_BK_Lyn_APM-RoboPhot_ML0742515_i_s5_v1_"
        "work_g19_max2000_edge20_ensemble_g_candidate_excluded_iter6_v1"
    )
    value = _color_calibration_target_name(
        source,
        reference_catalog_name="panstarrs_dr2__auto_catalog_robophot_20260402_bk_lyn_i__id_0123456789",
        reference_band="i",
    )

    assert len(value) <= 128
    assert "panstarrs_dr2_i_color" in value
    assert "__id_" not in value


def test_color_config_validation_requires_reference_model_fields():
    config = FieldPipelineConfig(
        catalog_name="gaia",
        calibration_prefix="p",
        enable_color_calibration=True,
    )

    with pytest.raises(ValueError, match="color_reference_catalog_name"):
        _validate_color_calibration_config(config)


def test_quality_policy_exposes_activated_science_calibration():
    config = FieldPipelineConfig(
        catalog_name="gaia",
        calibration_prefix="p",
        enable_color_calibration=True,
        color_reference_catalog_name="panstarrs_dr2",
        color_reference_band="i",
        color_index="r-i",
    )
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={
            "ensemble": "ensemble_iter6",
            "color": "ensemble_iter6_ps1_i_color",
            "science": "ensemble_iter6_ps1_i_color",
        },
        outputs={},
        steps=[
            PipelineStepRecord(
                step_name="apply_color_calibration",
                status="DONE",
                result={
                    "activated_as_science_calibration": True,
                    "color_term": 0.12,
                    "n_used_calibrator_objects": 42,
                    "n_frames_with_zp": 18,
                    "frame_model_residual_rms": 0.018,
                },
            )
        ],
    )

    summary = _quality_policy_summary(config, result)

    assert summary["color_calibration_activated"] is True
    assert summary["color_term"] == pytest.approx(0.12)
    assert summary["science_calibration"] == "ensemble_iter6_ps1_i_color"


def test_quality_policy_distinguishes_requested_and_effective_color_calibration():
    from grandgousier.pipeline.runner import (
        FieldPipelineConfig,
        PipelineRunResult,
        _quality_policy_summary,
    )

    config = FieldPipelineConfig(
        catalog_name="identity",
        calibration_prefix="cal",
        enable_color_calibration=False,
        color_calibration_requested=True,
        color_reference_ready=False,
        color_calibration_skip_reason="reference acquisition failed",
    )
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "ensemble"},
    )
    summary = _quality_policy_summary(config, result)
    assert summary["color_calibration_requested"] is True
    assert summary["color_calibration_enabled"] is False
    assert summary["color_calibration_enabled_effective"] is False
    assert summary["color_reference_ready"] is False
    assert summary["color_calibration_skip_reason"] == "reference acquisition failed"


def test_quality_policy_rebuild_preserves_aperture_correction_summary():
    config = FieldPipelineConfig(
        catalog_name="gaia",
        calibration_prefix="p",
    )
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={
            "raw": "raw_v1",
            "ensemble": "ensemble_v1",
            "science": "science_v1",
        },
        steps=[
            PipelineStepRecord(
                step_name="apply_aperture_correction",
                status="DONE",
                result={
                    "source_calibration_name": "raw_v1",
                    "target_calibration_name": "apcorr_v1",
                    "reference_radius_pix": 5.0,
                    "good_frame_fraction": 0.8571428571,
                    "median_residual_mad": 0.020285,
                    "median_spatial_peak_to_peak": 0.0,
                    "median_plane_spatial_peak_to_peak": 0.091401,
                    "frames_with_plane": 9,
                    "frames_with_constant": 51,
                    "frames_identity_fallback": 10,
                    "quality_gate_passed": True,
                    "quality_gate_failures": [],
                },
            )
        ],
    )

    summary = _quality_policy_summary(config, result)

    assert summary["aperture_correction_requested"] is True
    assert summary["aperture_correction_activated"] is True
    assert summary["aperture_correction_source_calibration"] == "raw_v1"
    assert summary["aperture_correction_target_calibration"] == "apcorr_v1"
    assert summary["aperture_correction_effective_calibration"] == "apcorr_v1"
    assert summary["aperture_correction_error_propagation"] == "differential_spatial_only_v1"
    assert summary["aperture_correction_median_plane_spatial_peak_to_peak"] == pytest.approx(0.091401)
    assert summary["aperture_correction_frames_with_plane"] == 9
    assert summary["aperture_correction_frames_with_constant"] == 51
    assert summary["aperture_correction_frames_identity_fallback"] == 10
