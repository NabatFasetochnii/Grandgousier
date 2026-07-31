import pytest

pytest.importorskip("astropy")

from grandgousier.pipeline.runner import _candidate_object_ids_for_ensemble_exclusion


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
    _quality_policy_summary,
)


def test_candidate_excluded_calibration_name_is_stable_for_second_pass_and_iterates_afterwards():
    cfg = FieldPipelineConfig(catalog_name="cat", calibration_prefix="pfx", working_catalog_mag_limit=19.0)
    assert _candidate_excluded_calibration_name(cfg, iteration=2).endswith("_ensemble_g_candidate_excluded_v1")
    assert _candidate_excluded_calibration_name(cfg, iteration=3).endswith("_ensemble_g_candidate_excluded_iter3_v1")


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
            "will_run_iteration": True,
            "executed": True,
            "input_ensemble_calibration": None,
            "output_ensemble_calibration": "ens2",
            "new_requested_excluded_object_ids": [1, 2, 3],
            "new_requested_excluded_objects": 3,
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
    assert qp["candidate_ensemble_exclusion_iterations_executed"] == 1


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
