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
