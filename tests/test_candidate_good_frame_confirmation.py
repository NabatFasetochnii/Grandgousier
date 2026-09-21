import pytest

pytest.importorskip("astropy")

from grandgousier.pipeline.runner import (
    _apply_good_frame_candidate_confirmation,
    _good_frame_confirmation_summary,
)


def test_all_epoch_candidate_must_remain_accepted_on_good_frames():
    rows = [
        {"object_id": 1, "candidate_status": "ACCEPTED", "candidate_origin": "rms_excess"},
        {"object_id": 2, "candidate_status": "ACCEPTED", "candidate_origin": "intrinsic_variability"},
    ]
    good = [
        {"object_id": 1, "candidate_status": "ACCEPTED", "candidate_origin": "rms_excess"},
        {"object_id": 2, "candidate_status": "REJECTED", "candidate_origin": ""},
    ]
    out = _apply_good_frame_candidate_confirmation(rows, good, n_good_frames=17, min_good_frames=5)
    assert out[0]["candidate_status"] == "ACCEPTED"
    assert out[0]["good_frame_confirmation_status"] == "GOOD_FRAME_CONFIRMED"
    assert out[1]["candidate_status"] == "BORDERLINE"
    assert out[1]["candidate_diagnostic_class"] == "SYSTEMATICS_SENSITIVE"


def test_insufficient_good_frames_demotes_automatic_candidate_but_preserves_target():
    rows = [
        {"object_id": 1, "candidate_status": "ACCEPTED", "candidate_origin": "rms_excess"},
        {"object_id": 2, "candidate_status": "ACCEPTED", "candidate_origin": "target", "is_target": True},
    ]
    out = _apply_good_frame_candidate_confirmation(rows, [], n_good_frames=3, min_good_frames=5)
    assert out[0]["candidate_status"] == "BORDERLINE"
    assert out[0]["good_frame_confirmation_status"] == "INSUFFICIENT_GOOD_FRAME_SUPPORT"
    assert out[1]["candidate_status"] == "ACCEPTED"
    assert out[1]["good_frame_confirmation_status"] == "TARGET_PRESERVED"


def test_confirmation_summary_counts_final_accepted_membership():
    rows = [
        {"candidate_status": "ACCEPTED", "good_frame_confirmation_status": "GOOD_FRAME_CONFIRMED"},
        {"candidate_status": "BORDERLINE", "good_frame_confirmation_status": "SYSTEMATICS_SENSITIVE"},
    ]
    summary = _good_frame_confirmation_summary(rows, 17, 5)
    assert summary["accepted_candidates_after_confirmation"] == 1
    assert summary["status_counts"]["SYSTEMATICS_SENSITIVE"] == 1


def test_candidate_triage_export_columns_include_good_frame_confirmation_diagnostics():
    from grandgousier.quality.candidate_triage import candidate_triage_columns

    columns = candidate_triage_columns()
    for name in (
        "good_frame_confirmation_status",
        "good_frame_count",
        "good_frame_candidate_status",
        "good_frame_mag_rms",
        "good_frame_mag_mad",
        "good_frame_robust_peak_to_peak",
        "good_frame_intrinsic_variability_score",
    ):
        assert name in columns
