from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.pipeline.runner import _candidate_object_ids_for_ensemble_exclusion
from grandgousier.calibration.ensemble import EnsembleCalibrationResult
from grandgousier.lightcurves.target import TargetResolution
import grandgousier.pipeline.runner as runner_module


def test_candidate_object_ids_for_ensemble_exclusion_uses_accepted_rows_and_target():
    rows = [
        {"candidate_status": "ACCEPTED", "object_id": "12", "candidate_origin": "low_amplitude_intrinsic"},
        {"candidate_status": "REJECTED", "object_id": 13, "candidate_origin": "rms_excess"},
        {"candidate_status": "accepted", "object_id": 14, "candidate_origin": "intrinsic_variability"},
        {"candidate_status": "ACCEPTED", "object_id": None},
        {"candidate_status": "ACCEPTED", "object_id": "bad"},
    ]
    assert _candidate_object_ids_for_ensemble_exclusion(rows, target_object_ids=[7, 12]) == [7, 12, 14]

from grandgousier.pipeline.runner import (
    FieldPipelineConfig,
    PipelineRunResult,
    PipelineStepRecord,
    _candidate_ensemble_exclusion_history,
    _candidate_excluded_calibration_name,
    _effective_reference_exclusion_ids,
    _quality_policy_summary,
)


def test_candidate_excluded_calibration_name_is_stable_for_second_pass_and_iterates_afterwards():
    cfg = FieldPipelineConfig(catalog_name="cat", calibration_prefix="pfx", working_catalog_mag_limit=19.0)
    assert _candidate_excluded_calibration_name(cfg, iteration=2).endswith("_ensemble_g_candidate_excluded_v1")
    assert _candidate_excluded_calibration_name(cfg, iteration=3).endswith("_ensemble_g_candidate_excluded_iter3_v1")


def test_effective_reference_exclusion_ids_distinguishes_unavailable_empty_and_intersection():
    assert _effective_reference_exclusion_ids([1, 2], None) is None
    assert _effective_reference_exclusion_ids([1, 2], {3, 4}) == []
    assert _effective_reference_exclusion_ids([3, 1, 3], {2, 3, 4}) == [3]


def test_candidate_ensemble_exclusion_history_separates_requested_and_actual():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "final"},
    )
    result.steps.extend(
        [
            PipelineStepRecord(
                step_name="select_candidate_reference_exclusions",
                status="DONE",
                result={
                    "iteration": 2,
                    "will_run_iteration": True,
                    "requested_excluded_object_ids": [1, 2, 3],
                    "new_requested_excluded_object_ids": [1, 2, 3],
                },
            ),
            PipelineStepRecord(
                step_name="ensemble_calibrate_candidate_excluded",
                status="DONE",
                result={
                    "candidate_ensemble_iteration": 2,
                    "target_calibration_name": "ens2",
                    "requested_excluded_object_ids": [1, 2, 3],
                    "actually_excluded_reference_object_ids": [1, 3],
                    "reference_candidates": 10,
                    "usable_reference_objects": 10,
                },
            ),
        ]
    )
    history = _candidate_ensemble_exclusion_history(result)
    assert history == [
        {
            "iteration": 2,
            "growth_policy": None,
            "will_run_iteration": True,
            "executed": True,
            "input_ensemble_calibration": None,
            "output_ensemble_calibration": "ens2",
            "new_requested_excluded_object_ids": [1, 2, 3],
            "new_requested_excluded_objects": 3,
            "suppressed_new_candidate_object_ids": [],
            "suppressed_new_candidate_objects": 0,
            "requested_excluded_object_ids": [1, 2, 3],
            "requested_excluded_objects": 3,
            "actually_excluded_reference_object_ids": [1, 3],
            "actually_excluded_reference_objects": 2,
            "reference_candidates_after_exclusion": 10,
            "usable_reference_objects": 10,
            "stop_reason": None,
        }
    ]
    cfg = FieldPipelineConfig(catalog_name="cat", calibration_prefix="p", exclude_candidates_from_ensemble=True)
    qp = _quality_policy_summary(cfg, result)
    assert qp["candidate_ensemble_requested_excluded_object_ids"] == [1, 2, 3]
    assert qp["candidate_ensemble_actual_excluded_reference_object_ids"] == [1, 3]
    assert qp["candidate_ensemble_actual_exclusions_iteration"] == 2
    assert qp["candidate_ensemble_exclusion_iterations_executed"] == 1


def test_quality_policy_uses_last_executed_iteration_for_actual_exclusions():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "ens_final"},
    )
    result.steps.extend(
        [
            PipelineStepRecord(
                step_name="select_candidate_reference_exclusions_iter6",
                status="DONE",
                result={
                    "iteration": 6,
                    "will_run_iteration": True,
                    "requested_excluded_object_ids": [1, 2, 3, 4],
                    "new_requested_excluded_object_ids": [4],
                },
            ),
            PipelineStepRecord(
                step_name="ensemble_calibrate_candidate_excluded_iter6",
                status="DONE",
                result={
                    "candidate_ensemble_iteration": 6,
                    "target_calibration_name": "ens6",
                    "requested_excluded_object_ids": [1, 2, 3, 4],
                    "actually_excluded_reference_object_ids": [1, 3, 4],
                    "reference_candidates": 20,
                    "usable_reference_objects": 17,
                },
            ),
            PipelineStepRecord(
                step_name="select_candidate_reference_exclusions_iter7",
                status="DONE",
                result={
                    "iteration": 7,
                    "will_run_iteration": False,
                    "requested_excluded_object_ids": [1, 2, 3, 4],
                    "new_requested_excluded_object_ids": [],
                    "stop_reason": "converged: no new accepted candidate reference exclusions",
                },
            ),
            PipelineStepRecord(
                step_name="candidate_ensemble_convergence",
                status="DONE",
                result={
                    "iteration_mode": "until_converged",
                    "until_converged": True,
                    "iteration_limit": 10,
                    "iterations_executed": 5,
                    "final_candidate_object_ids": [1, 2, 3, 4],
                    "final_candidate_objects": 4,
                    "requested_excluded_object_ids": [1, 2, 3, 4],
                    "requested_excluded_objects": 4,
                    "new_final_candidate_object_ids": [],
                    "new_final_candidate_objects": 0,
                    "converged": True,
                    "stop_reason": "converged",
                },
            ),
        ]
    )
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_until_converged=True,
    )

    qp = _quality_policy_summary(cfg, result)

    assert qp["candidate_ensemble_requested_excluded_object_ids"] == [1, 2, 3, 4]
    assert qp["candidate_ensemble_actual_excluded_reference_object_ids"] == [1, 3, 4]
    assert qp["candidate_ensemble_actual_excluded_reference_objects"] == 3
    assert qp["candidate_ensemble_actual_exclusions_iteration"] == 6
    assert qp["candidate_ensemble_converged"] is True


def test_quality_policy_records_until_converged_mode_and_final_convergence():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "ens_final"},
    )
    result.steps.extend(
        [
            PipelineStepRecord(
                step_name="ensemble_calibrate_candidate_excluded",
                status="DONE",
                result={
                    "candidate_ensemble_iteration": 2,
                    "target_calibration_name": "ens2",
                    "requested_excluded_object_ids": [1, 2],
                    "actually_excluded_reference_object_ids": [2],
                },
            ),
            PipelineStepRecord(
                step_name="candidate_ensemble_convergence",
                status="DONE",
                result={
                    "iteration_mode": "until_converged",
                    "until_converged": True,
                    "iteration_limit": 10,
                    "iterations_executed": 1,
                    "final_candidate_object_ids": [1, 2],
                    "final_candidate_objects": 2,
                    "requested_excluded_object_ids": [1, 2],
                    "requested_excluded_objects": 2,
                    "new_final_candidate_object_ids": [],
                    "new_final_candidate_objects": 0,
                    "converged": True,
                    "stop_reason": "converged",
                },
            ),
        ]
    )
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_until_converged=True,
        candidate_ensemble_convergence_max_iterations=10,
    )
    qp = _quality_policy_summary(cfg, result)
    assert qp["candidate_ensemble_iteration_mode"] == "until_converged"
    assert qp["candidate_ensemble_until_converged"] is True
    assert qp["candidate_ensemble_iteration_limit"] == 10
    assert qp["candidate_ensemble_converged"] is True
    assert qp["candidate_ensemble_stop_reason"] == "converged"
    assert qp["candidate_ensemble_new_final_candidate_object_ids"] == []


def test_quality_policy_marks_fixed_limit_with_new_final_candidates_not_converged():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "ens_final"},
    )
    result.steps.extend(
        [
            PipelineStepRecord(
                step_name="ensemble_calibrate_candidate_excluded",
                status="DONE",
                result={
                    "candidate_ensemble_iteration": 2,
                    "target_calibration_name": "ens2",
                    "requested_excluded_object_ids": [1, 2],
                    "actually_excluded_reference_object_ids": [2],
                },
            ),
            PipelineStepRecord(
                step_name="candidate_ensemble_convergence",
                status="DONE",
                result={
                    "iteration_mode": "fixed",
                    "until_converged": False,
                    "iteration_limit": 2,
                    "iterations_executed": 1,
                    "final_candidate_object_ids": [1, 2, 3],
                    "final_candidate_objects": 3,
                    "requested_excluded_object_ids": [1, 2],
                    "requested_excluded_objects": 2,
                    "new_final_candidate_object_ids": [3],
                    "new_final_candidate_objects": 1,
                    "converged": False,
                    "stop_reason": "fixed_iteration_limit_reached_with_new_candidates",
                },
            ),
        ]
    )
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_max_iterations=2,
    )
    qp = _quality_policy_summary(cfg, result)
    assert qp["candidate_ensemble_iteration_mode"] == "fixed"
    assert qp["candidate_ensemble_converged"] is False
    assert qp["candidate_ensemble_new_final_candidate_object_ids"] == [3]
    assert qp["candidate_ensemble_stop_reason"] == "fixed_iteration_limit_reached_with_new_candidates"


def test_quality_policy_distinguishes_initial_only_policy_stop_from_convergence():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "ens_final"},
    )
    result.steps.append(
        PipelineStepRecord(
            step_name="candidate_ensemble_convergence",
            status="DONE",
            result={
                "iteration_mode": "until_converged",
                "growth_policy": "initial_only",
                "until_converged": True,
                "iteration_limit": 10,
                "iterations_executed": 1,
                "final_candidate_object_ids": [1, 2, 3],
                "final_candidate_objects": 3,
                "requested_excluded_object_ids": [1, 2],
                "requested_excluded_objects": 2,
                "new_final_candidate_object_ids": [3],
                "new_final_candidate_objects": 1,
                "growth_suppressed": True,
                "growth_suppressed_candidate_object_ids": [3],
                "growth_suppressed_candidate_objects": 1,
                "converged": False,
                "policy_satisfied": True,
                "stop_status": "POLICY_STOPPED",
                "stop_reason": "initial_only_growth_policy_suppressed_new_candidates",
            },
        )
    )
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_until_converged=True,
        candidate_ensemble_growth_policy="initial_only",
    )

    qp = _quality_policy_summary(cfg, result)

    assert qp["candidate_ensemble_converged"] is False
    assert qp["candidate_ensemble_policy_satisfied"] is True
    assert qp["candidate_ensemble_stop_status"] == "POLICY_STOPPED"
    assert qp["candidate_ensemble_stop_reason"] == "initial_only_growth_policy_suppressed_new_candidates"


def test_quality_policy_records_noop_candidate_exclusion_guard():
    result = PipelineRunResult(
        run_uid="r",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"ensemble": "initial_ensemble"},
    )
    result.steps.extend(
        [
            PipelineStepRecord(
                step_name="select_candidate_reference_exclusions",
                status="DONE",
                result={
                    "iteration": 2,
                    "will_run_iteration": False,
                    "requested_excluded_object_ids": [11, 12],
                    "new_requested_excluded_object_ids": [11, 12],
                    "reference_precheck_available": True,
                    "input_reference_objects": 37,
                    "effective_reference_exclusion_object_ids": [],
                    "effective_reference_exclusion_objects": 0,
                    "stop_reason": "no_usable_reference_exclusions",
                },
            ),
            PipelineStepRecord(
                step_name="candidate_ensemble_convergence",
                status="DONE",
                result={
                    "iteration_mode": "until_converged",
                    "until_converged": True,
                    "iteration_limit": 10,
                    "iterations_executed": 0,
                    "final_candidate_object_ids": [11, 12],
                    "final_candidate_objects": 2,
                    "requested_excluded_object_ids": [11, 12],
                    "requested_excluded_objects": 2,
                    "new_final_candidate_object_ids": [],
                    "new_final_candidate_objects": 0,
                    "converged": True,
                    "policy_satisfied": True,
                    "no_op_iteration_skipped": True,
                    "stop_status": "NO_USABLE_REFERENCE_EXCLUSIONS",
                    "stop_reason": "no_usable_reference_exclusions",
                },
            ),
        ]
    )
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_until_converged=True,
    )

    history = _candidate_ensemble_exclusion_history(result)
    qp = _quality_policy_summary(cfg, result)

    assert history[0]["executed"] is False
    assert history[0]["stop_reason"] == "no_usable_reference_exclusions"
    assert qp["candidate_ensemble_exclusion_executed"] is False
    assert qp["candidate_ensemble_exclusion_iterations_executed"] == 0
    assert qp["candidate_ensemble_converged"] is True
    assert qp["candidate_ensemble_stop_status"] == "NO_USABLE_REFERENCE_EXCLUSIONS"
    assert qp["candidate_ensemble_stop_reason"] == "no_usable_reference_exclusions"
    assert qp["final_ensemble_calibration"] == "initial_ensemble"


def test_run_field_pipeline_skips_candidate_recalibration_when_exclusions_are_not_references(monkeypatch):
    calls = {"ensemble": 0, "object_qc": 0, "field_report": 0}

    class FakeSession:
        def rollback(self):
            return None

    def fake_ensemble(*args, **kwargs):
        calls["ensemble"] += 1
        return EnsembleCalibrationResult(
            target_calibration_name=kwargs["target_calibration_name"],
            reference_candidates=2,
            reference_object_ids=[1, 2],
            usable_reference_objects=2,
            selected_frames=1,
            frames_with_offsets=1,
            calibrated=1,
        )

    def fake_object_qc(*args, **kwargs):
        calls["object_qc"] += 1
        return {"objects": 3}

    def fake_field_report(*args, **kwargs):
        calls["field_report"] += 1
        return SimpleNamespace(
            summary_rows=[],
            rms_bins=[],
            blend_rows=[],
            candidate_rows=[
                {
                    "candidate_status": "ACCEPTED",
                    "object_id": 9,
                    "candidate_origin": "intrinsic_variability",
                }
            ],
            report={
                "metadata": {},
                "warnings": [],
                "candidate_triage": {},
                "blend_diagnostics": {},
            },
        )

    monkeypatch.setattr(runner_module, "ensemble_calibrate_measurements", fake_ensemble)
    monkeypatch.setattr(runner_module, "compute_object_qc", fake_object_qc)
    monkeypatch.setattr(runner_module, "build_field_qc_report", fake_field_report)
    monkeypatch.setattr(
        runner_module,
        "resolve_target_object",
        lambda *args, **kwargs: TargetResolution(resolved=False, reason="test"),
    )
    monkeypatch.setattr(
        runner_module,
        "load_calibration_analysis_snapshot",
        lambda *args, **kwargs: SimpleNamespace(timings_sec={}),
    )
    monkeypatch.setattr(
        runner_module,
        "validate_random_error_model",
        lambda *args, **kwargs: SimpleNamespace(
            summary_json=None,
            objects_csv=None,
            residual_floor_summary_json=None,
            residual_floor_objects_csv=None,
            residual_floor_frames_csv=None,
        ),
    )

    config = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        frame_ids=[1],
        run_pre_photometry_qc=False,
        skip_wcs=True,
        skip_catalog_frame_match=True,
        skip_detection=True,
        skip_detection_catalog_match=True,
        skip_forced_photometry=True,
        skip_aperture_correction=True,
        skip_aperture_zp=True,
        skip_aperture_stability_qc=True,
        ensemble_min_ref_stars_per_frame=2,
        exclude_candidates_from_ensemble=True,
        candidate_ensemble_until_converged=True,
        export_target_light_curve=False,
        export_candidate_light_curves=False,
        record_run=False,
        no_file_log=True,
    )

    result = runner_module.run_field_pipeline(FakeSession(), config)

    step_names = [step.step_name for step in result.steps]
    selection = next(
        step.result
        for step in result.steps
        if step.step_name == "select_candidate_reference_exclusions"
    )
    convergence = next(
        step.result
        for step in result.steps
        if step.step_name == "candidate_ensemble_convergence"
    )

    assert result.status == "DONE"
    assert calls == {"ensemble": 1, "object_qc": 1, "field_report": 1}
    assert "ensemble_calibrate_candidate_excluded" not in step_names
    assert "compute_object_qc_candidate_excluded" not in step_names
    assert "field_qc_report_candidate_excluded" not in step_names
    assert selection["effective_reference_exclusion_object_ids"] == []
    assert selection["will_run_iteration"] is False
    assert convergence["iterations_executed"] == 0
    assert convergence["stop_status"] == "NO_USABLE_REFERENCE_EXCLUSIONS"
    assert result.calibration_names["ensemble"].endswith("_ensemble_g_v1")
