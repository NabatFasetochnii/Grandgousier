from __future__ import annotations

from typing import Any


def normalize_candidate_ensemble_growth_policy(value: Any) -> str:
    policy = str(value or "initial_only").strip().lower()
    if policy not in {"initial_only", "all"}:
        raise ValueError(
            "candidate_ensemble_growth_policy must be one of: initial_only, all"
        )
    return policy


def apply_candidate_ensemble_growth_policy(
    *,
    previous_ids: set[int],
    discovered_ids: set[int],
    iteration: int,
    policy: str,
    target_object_ids: set[int] | None = None,
) -> dict[str, list[int]]:
    normalized = normalize_candidate_ensemble_growth_policy(policy)
    targets = {int(value) for value in (target_object_ids or set())}
    discovered = {int(value) for value in discovered_ids} | targets
    previous = {int(value) for value in previous_ids}
    if int(iteration) <= 2 or normalized == "all":
        requested = previous | discovered
        new_ids = requested - previous
        suppressed: set[int] = set()
    else:
        requested = previous
        new_ids = set()
        suppressed = discovered - previous
    return {
        "requested_ids": sorted(requested),
        "new_ids": sorted(new_ids),
        "suppressed_ids": sorted(suppressed),
    }
