from types import SimpleNamespace

import pytest

from grandgousier.quality import candidate_triage as ct
from grandgousier.quality.lightcurve_quality import ObjectScatterRow


def _row(
    object_id: int,
    *,
    mag_rms: float,
    mag_mad: float,
    mag_err: float,
    mag_p10: float = 14.90,
    mag_p90: float = 15.00,
) -> ObjectScatterRow:
    return ObjectScatterRow(
        object_id=object_id,
        calibration_name="cal_v1",
        instrument_unit_id=1,
        instrument="RoboPhot",
        filter_name="i",
        catalog_name="test_catalog",
        source_id=str(object_id),
        ra_deg=1.0,
        dec_deg=2.0,
        n_measurements=70,
        mag_mean=15.0,
        mag_median=15.0,
        mag_rms=mag_rms,
        mag_mad=mag_mad,
        mag_err_median=mag_err,
        flux_median=None,
        ref_mag=15.0,
        color=0.0,
        mjd_min=1.0,
        mjd_max=2.0,
        mag_p10=mag_p10,
        mag_p90=mag_p90,
        robust_peak_to_peak=mag_p90 - mag_p10,
    )


def test_intrinsic_variability_channel_selects_moderate_high_snr_object(monkeypatch):
    rows = [_row(6751, mag_rms=0.041, mag_mad=0.027, mag_err=0.010, mag_p10=14.89, mag_p90=15.00)]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
    )

    assert len(result.rows) == 1
    assert result.rows[0]["object_id"] == 6751
    assert result.rows[0]["candidate_origin"] == "intrinsic_variability"
    assert result.rows[0]["rms_over_mag_err"] > 4.0
    assert result.rows[0]["mad_over_mag_err"] > 2.5
    assert result.rows[0]["robust_peak_to_peak"] > 0.08
    assert result.summary["intrinsic_variability_candidates"] == 1
    assert result.summary["intrinsic_only_candidates"] == 1


def test_intrinsic_variability_channel_uses_error_floor(monkeypatch):
    rows = [_row(100, mag_rms=0.012, mag_mad=0.009, mag_err=0.002)]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
        min_intrinsic_rms=None,
        intrinsic_mag_err_floor=0.01,
    )

    assert result.rows == []
    assert result.summary["intrinsic_variability_candidates"] == 0


def test_intrinsic_variability_channel_rejects_single_outlier_like_scatter(monkeypatch):
    rows = [_row(101, mag_rms=0.100, mag_mad=0.030, mag_err=0.010, mag_p10=14.99, mag_p90=15.03)]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
    )

    assert result.rows == []
    assert result.summary["intrinsic_variability_candidates"] == 0


def test_candidate_triage_columns_expose_origin_and_intrinsic_metrics():
    columns = ct.candidate_triage_columns()
    for name in (
        "candidate_origin",
        "is_target",
        "target_reason",
        "rms_over_mag_err",
        "mad_over_mag_err",
        "intrinsic_variability_score",
        "robust_peak_to_peak",
    ):
        assert name in columns


def test_low_amplitude_intrinsic_channel_selects_clean_bklyn_like_object(monkeypatch):
    rows = [_row(6751, mag_rms=0.035, mag_mad=0.031, mag_err=0.0143, mag_p10=14.84, mag_p90=14.93)]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
    )

    assert len(result.rows) == 1
    assert result.rows[0]["object_id"] == 6751
    assert result.rows[0]["candidate_origin"] == "low_amplitude_intrinsic"
    assert result.rows[0]["rms_over_mag_err"] > 2.3
    assert result.rows[0]["rms_over_mag_err"] < 4.0
    assert result.rows[0]["mad_over_mag_err"] > 2.0
    assert result.rows[0]["mad_over_mag_err"] < 2.5
    assert result.rows[0]["robust_peak_to_peak"] > 0.08
    assert result.summary["intrinsic_variability_candidates"] == 0
    assert result.summary["low_amplitude_intrinsic_candidates"] == 1
    assert result.summary["low_amplitude_only_candidates"] == 1


def test_missing_reference_residual_is_informational_not_rejection():
    reasons = ct._candidate_rejection_reasons(
        {"mag_median": 14.7, "abs_mag_ref_delta": None},
        SimpleNamespace(flags={}),
        exclude_flagged=False,
        exclude_crowded=False,
        exclude_edge=False,
        exclude_saturated=False,
        exclude_catalog_outliers=True,
        reject_aperture_unstable=False,
        require_detection_support=False,
        min_nearest_neighbor_arcsec=None,
        max_mag_median=22.5,
        max_ref_mag_delta=2.0,
    )

    assert "NO_REF_MAG_DELTA" not in reasons
    assert reasons == []


def test_low_amplitude_near_threshold_object_is_preserved_as_borderline(monkeypatch):
    rows = [_row(6751, mag_rms=0.03395, mag_mad=0.02806, mag_err=0.01454, mag_p10=14.65, mag_p90=14.736)]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["candidate_status"] == "BORDERLINE"
    assert row["candidate_origin"] == ""
    assert row["candidate_diagnostic_class"] == "BORDERLINE_LOW_AMPLITUDE"
    assert row["low_amplitude_failed_criteria"] == "mad_over_mag_err"
    assert row["low_amplitude_mad_over_mag_err_pass"] is False
    assert row["low_amplitude_rms_over_mag_err_pass"] is True
    assert row["low_amplitude_robust_peak_to_peak_pass"] is True
    assert row["low_amplitude_n_measurements_value"] == 70.0
    assert row["low_amplitude_n_measurements_threshold"] == 30.0
    assert row["low_amplitude_n_measurements_margin"] == 40.0
    assert row["low_amplitude_n_measurements_ratio"] == pytest.approx(70.0 / 30.0)
    assert row["low_amplitude_intrinsic_rms_value"] == pytest.approx(0.03395)
    assert row["low_amplitude_intrinsic_rms_threshold"] == pytest.approx(0.03)
    assert row["low_amplitude_intrinsic_rms_pass"] is True
    assert row["low_amplitude_rms_over_mag_err_value"] == pytest.approx(0.03395 / 0.01454)
    assert row["low_amplitude_rms_over_mag_err_threshold"] == pytest.approx(2.3)
    assert row["low_amplitude_mad_over_mag_err_value"] == pytest.approx(0.02806 / 0.01454)
    assert row["low_amplitude_mad_over_mag_err_threshold"] == pytest.approx(2.0)
    assert row["low_amplitude_mad_over_mag_err_ratio"] == pytest.approx((0.02806 / 0.01454) / 2.0)
    assert row["low_amplitude_robust_peak_to_peak_value"] == pytest.approx(0.086)
    assert row["low_amplitude_robust_peak_to_peak_threshold"] == pytest.approx(0.08)
    assert 0.9 < row["low_amplitude_min_threshold_ratio"] < 1.0
    for criterion in ct.LOW_AMPLITUDE_CRITERION_NAMES:
        for component in ("value", "threshold", "pass", "margin", "ratio"):
            assert f"low_amplitude_{criterion}_{component}" in ct.LOW_AMPLITUDE_DIAGNOSTIC_COLUMNS
    assert result.summary["borderline"] == 1
    assert result.summary["accepted"] == 0


def test_two_near_threshold_failures_are_preserved_as_diagnostic(monkeypatch):
    rows = [
        _row(
            6751,
            mag_rms=0.03363022,
            mag_mad=0.02813570,
            mag_err=0.01499514,
            mag_p10=14.84,
            mag_p90=14.926,
        )
    ]
    monkeypatch.setattr(ct, "load_object_scatter_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(ct, "build_variable_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(ct, "load_object_qc_map", lambda *args, **kwargs: {})

    result = ct.build_candidate_triage(
        object(),
        calibration_name="cal_v1",
        catalog_name="test_catalog",
        require_detection_support=False,
        min_measurements=10,
        diagnostic_max_failed_criteria=2,
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["candidate_status"] == "DIAGNOSTIC"
    assert row["candidate_origin"] == ""
    assert row["candidate_diagnostic_class"] == "NEAR_THRESHOLD_LOW_AMPLITUDE"
    assert row["low_amplitude_failed_criteria"] == "rms_over_mag_err,mad_over_mag_err"
    assert row["low_amplitude_failed_criteria_count"] == 2
    assert row["low_amplitude_near_threshold"] is True
    assert row["low_amplitude_borderline"] is False
    assert row["low_amplitude_min_threshold_ratio"] >= 0.9
    assert result.summary["near_threshold"] == 1
    assert result.summary["near_threshold_low_amplitude"] == 1
    assert result.summary["accepted"] == 0
    assert result.summary["borderline"] == 0
