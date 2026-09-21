from grandgousier.pipeline.candidate_ensemble_policy import apply_candidate_ensemble_growth_policy


def test_initial_only_accepts_initial_candidates_and_suppresses_later_growth():
    initial = apply_candidate_ensemble_growth_policy(
        previous_ids=set(),
        discovered_ids={1, 2, 3},
        iteration=2,
        policy="initial_only",
        target_object_ids={9},
    )
    assert initial == {
        "requested_ids": [1, 2, 3, 9],
        "new_ids": [1, 2, 3, 9],
        "suppressed_ids": [],
    }

    later = apply_candidate_ensemble_growth_policy(
        previous_ids=set(initial["requested_ids"]),
        discovered_ids={1, 2, 3, 4, 5},
        iteration=3,
        policy="initial_only",
        target_object_ids={9},
    )
    assert later == {
        "requested_ids": [1, 2, 3, 9],
        "new_ids": [],
        "suppressed_ids": [4, 5],
    }


def test_all_policy_preserves_legacy_iterative_growth():
    result = apply_candidate_ensemble_growth_policy(
        previous_ids={1, 2},
        discovered_ids={2, 3, 4},
        iteration=4,
        policy="all",
        target_object_ids=set(),
    )
    assert result == {
        "requested_ids": [1, 2, 3, 4],
        "new_ids": [3, 4],
        "suppressed_ids": [],
    }
