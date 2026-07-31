import json
from pathlib import Path

from grandgousier.pipeline.provenance import summarize_candidate_ensemble_exclusions


def test_summary_uses_last_executed_iteration_for_actual_exclusions():
    history = [
        {
            "iteration": 6,
            "executed": True,
            "requested_excluded_object_ids": [1, 2, 3, 4],
            "actually_excluded_reference_object_ids": [1, 3, 4],
        },
        {
            "iteration": 7,
            "executed": False,
            "requested_excluded_object_ids": [1, 2, 3, 4],
            "actually_excluded_reference_object_ids": [],
            "stop_reason": "converged",
        },
    ]

    summary = summarize_candidate_ensemble_exclusions(history)

    assert summary == {
        "requested_excluded_object_ids": [1, 2, 3, 4],
        "actually_excluded_reference_object_ids": [1, 3, 4],
        "actual_exclusions_iteration": 6,
    }


def test_summary_falls_back_to_convergence_result_without_history():
    summary = summarize_candidate_ensemble_exclusions(
        [],
        {"requested_excluded_object_ids": [5, 8]},
    )

    assert summary == {
        "requested_excluded_object_ids": [5, 8],
        "actually_excluded_reference_object_ids": [],
        "actual_exclusions_iteration": None,
    }


def test_real_05332_manifest_history_summarizes_to_iteration_6():
    manifest = Path(__file__).resolve().parents[1] / "reports" / (
        "check_05332_bklyn_20260328_i_candidate_excluded_until_converged.json"
    )
    if not manifest.exists():
        return

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    history = payload["pipeline_results"][0]["quality_policy"][
        "candidate_ensemble_exclusion_history"
    ]
    summary = summarize_candidate_ensemble_exclusions(history)

    assert history[-1]["iteration"] == 7
    assert history[-1]["executed"] is False
    assert len(summary["requested_excluded_object_ids"]) == 16
    assert 6751 in summary["requested_excluded_object_ids"]
    assert summary["actual_exclusions_iteration"] == 6
    assert len(summary["actually_excluded_reference_object_ids"]) == 12
