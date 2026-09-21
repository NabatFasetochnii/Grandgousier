from __future__ import annotations

from grandgousier.lightcurves.candidates import _candidate_reject_flags_for_row
from grandgousier.lightcurves.product import DEFAULT_REJECT_FLAGS


def test_good_frame_confirmed_candidate_keeps_high_rms_as_diagnostic_only():
    flags, override = _candidate_reject_flags_for_row(
        {
            "candidate_status": "ACCEPTED",
            "good_frame_confirmation_status": "GOOD_FRAME_CONFIRMED",
        },
        None,
    )
    assert "HIGH_RMS" not in flags
    assert "FAINT_NOISY" in flags
    assert override == "HIGH_RMS_DIAGNOSTIC_ONLY_FOR_GOOD_FRAME_CONFIRMED"


def test_unconfirmed_candidate_preserves_default_high_rms_rejection():
    flags, override = _candidate_reject_flags_for_row(
        {
            "candidate_status": "ACCEPTED",
            "good_frame_confirmation_status": "SYSTEMATICS_SENSITIVE",
        },
        None,
    )
    assert flags == set(DEFAULT_REJECT_FLAGS)
    assert "HIGH_RMS" in flags
    assert override is None


def test_explicit_custom_reject_flags_are_preserved_except_high_rms_for_confirmed_candidate():
    flags, override = _candidate_reject_flags_for_row(
        {
            "candidate_status": "ACCEPTED",
            "good_frame_confirmation_status": "GOOD_FRAME_CONFIRMED",
        },
        {"HIGH_RMS", "EDGE", "CUSTOM_FLAG"},
    )
    assert flags == {"EDGE", "CUSTOM_FLAG"}
    assert override is not None


def test_confirmed_candidate_override_only_removes_high_rms_from_point_rejection():
    from grandgousier.lightcurves.product import evaluate_rejection

    flags, _ = _candidate_reject_flags_for_row(
        {
            "candidate_status": "ACCEPTED",
            "good_frame_confirmation_status": "GOOD_FRAME_CONFIRMED",
        },
        None,
    )
    rejected, reasons = evaluate_rejection(
        quality_flags={"HIGH_RMS": True},
        mag=15.2,
        mag_err=0.02,
        reject_flags=flags,
    )
    assert rejected is False
    assert reasons == []

    rejected_edge, reasons_edge = evaluate_rejection(
        quality_flags={"HIGH_RMS": True, "EDGE": True},
        mag=15.2,
        mag_err=0.02,
        reject_flags=flags,
    )
    assert rejected_edge is True
    assert reasons_edge == ["EDGE"]
