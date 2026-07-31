from __future__ import annotations

from typing import Any


def summarize_candidate_ensemble_exclusions(
    exclusion_history: list[dict[str, Any]],
    convergence_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return top-level requested and actual exclusion provenance.

    The last history entry can be a terminal convergence check that did not run
    an ensemble calibration. Requested exclusions should still reflect that
    final cumulative check, while actual exclusions must describe the last
    completed calibration.
    """

    convergence_result = convergence_result or {}
    requested_ids = (
        list(exclusion_history[-1].get("requested_excluded_object_ids") or [])
        if exclusion_history
        else list(convergence_result.get("requested_excluded_object_ids") or [])
    )
    last_executed = next(
        (entry for entry in reversed(exclusion_history) if entry.get("executed")),
        None,
    )
    actual_ids = (
        list(last_executed.get("actually_excluded_reference_object_ids") or [])
        if last_executed
        else []
    )
    actual_iteration = (
        int(last_executed["iteration"])
        if last_executed and last_executed.get("iteration") is not None
        else None
    )
    return {
        "requested_excluded_object_ids": requested_ids,
        "actually_excluded_reference_object_ids": actual_ids,
        "actual_exclusions_iteration": actual_iteration,
    }
