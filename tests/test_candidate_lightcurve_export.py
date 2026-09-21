import csv
import json

import pytest

from grandgousier.lightcurves import candidates as cand


def test_candidate_rank_orders_targets_then_combined_then_intrinsic():
    rows = [
        {"candidate_status": "ACCEPTED", "candidate_origin": "low_amplitude_intrinsic", "object_id": 3, "robust_peak_to_peak": 0.09, "rms_over_mag_err": 2.4},
        {"candidate_status": "ACCEPTED", "candidate_origin": "target", "object_id": 1},
        {"candidate_status": "ACCEPTED", "candidate_origin": "rms_excess,intrinsic_variability", "object_id": 2, "rms_excess": 3.0},
    ]

    ranked = cand.rank_candidate_rows(rows)

    assert [row["object_id"] for row in ranked] == [1, 2, 3]
    assert ranked[0]["rank"] == 1
    assert ranked[0]["candidate_rank_score"] > ranked[-1]["candidate_rank_score"]


def test_export_accepted_candidate_light_curves_writes_summary_and_per_object_files(monkeypatch, tmp_path):
    stale = tmp_path / "object_999_src_old_light_curve.csv"
    stale.write_text("stale", encoding="utf-8")
    calls = []

    def fake_load_rows(session, **kwargs):
        calls.append(kwargs)
        object_id = kwargs["object_id"]
        return None, [
            {
                "object_id": object_id,
                "catalog_name": "gaia",
                "source_id": str(object_id),
                "frame_id": 1,
                "series_id": 3,
                "measurement_id": 10,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:00:00",
                "mjd": 60000.0,
                "jd": None,
                "filter_name": "i",
                "instrument_unit_id": 2,
                "instrument": "RoboPhot",
                "site": "KAO",
                "tube": "APM",
                "camera": "ML",
                "x": 1.0,
                "y": 2.0,
                "flux": 100.0,
                "flux_err": 3.0,
                "mag_inst": 15.0,
                "mag_cal": 14.9,
                "mag_err": 0.01,
                "mag_global": 14.9,
                "mag_global_err": 0.01,
                "mag_global_err_random": 0.01,
                "mag_global_err_systematic": 0.04,
                "interseries_offset_mag": None,
                "interseries_offset_err": None,
                "interseries_offset_status": None,
                "interseries_algorithm": None,
                "aperture_radius_pix": 3.0,
                "local_background": 1.0,
                "local_zp_correction": None,
                "centroid_dx_pix": None,
                "centroid_dy_pix": None,
                "object_qc_flags": "",
                "measurement_flags": "{}",
                "frame_quality_flags": "{}",
                "quality_flags": "{}",
                "is_rejected": False,
                "reject_reasons": "",
            },
            {
                "object_id": object_id,
                "catalog_name": "gaia",
                "source_id": str(object_id),
                "frame_id": 2,
                "series_id": 3,
                "measurement_id": 11,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:01:00",
                "mjd": 60000.1,
                "jd": None,
                "filter_name": "i",
                "instrument_unit_id": 2,
                "instrument": "RoboPhot",
                "site": "KAO",
                "tube": "APM",
                "camera": "ML",
                "x": 1.0,
                "y": 2.0,
                "flux": 105.0,
                "flux_err": 3.0,
                "mag_inst": 14.9,
                "mag_cal": 14.8,
                "mag_err": 0.01,
                "mag_global": 14.8,
                "mag_global_err": 0.01,
                "mag_global_err_random": 0.01,
                "mag_global_err_systematic": 0.04,
                "interseries_offset_mag": None,
                "interseries_offset_err": None,
                "interseries_offset_status": None,
                "interseries_algorithm": None,
                "aperture_radius_pix": 3.0,
                "local_background": 1.0,
                "local_zp_correction": None,
                "centroid_dx_pix": None,
                "centroid_dy_pix": None,
                "object_qc_flags": "",
                "measurement_flags": "{}",
                "frame_quality_flags": "{}",
                "quality_flags": "{}",
                "is_rejected": False,
                "reject_reasons": "",
            },
        ]

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)

    def fake_plot(rows, path, **kwargs):
        path.write_bytes(b"png")

    monkeypatch.setattr(cand, "_export_candidate_light_curve_plot", fake_plot)

    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[
            {
                "candidate_status": "ACCEPTED",
                "candidate_origin": "low_amplitude_intrinsic",
                "object_id": 6751,
                "source_id": "702296666944246784",
                "mag_median": 99.0,
                "mag_rms": 9.0,
                "mag_mad": 8.0,
                "mag_p10": 97.0,
                "mag_p90": 101.0,
                "robust_peak_to_peak": 4.0,
                "mag_err_median": 3.0,
                "mag_err_systematic_median": None,
            },
            {"candidate_status": "REJECTED", "candidate_origin": "rms_excess", "object_id": 10, "source_id": "10"},
        ],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        frame_ids=[1, 2],
        reject_flags=set(),
        out_dir=tmp_path,
    )

    assert product.accepted_candidates == 1
    assert product.exported_light_curves == 1
    assert calls[0]["series_id"] is None
    assert calls[0]["frame_ids"] == [1, 2]
    summary_csv = tmp_path / "candidates_summary.csv"
    summary_json = tmp_path / "candidates_summary.json"
    index_html = tmp_path / "index.html"
    light_curve_csv = tmp_path / "object_6751_src_702296666944246784_light_curve.csv"
    object_json = tmp_path / "object_6751_src_702296666944246784_summary.json"
    object_png = tmp_path / "object_6751_src_702296666944246784_light_curve.png"
    assert summary_csv.exists()
    assert summary_json.exists()
    assert index_html.exists()
    assert light_curve_csv.exists()
    assert object_json.exists()
    assert object_png.exists()
    assert not stale.exists()
    assert product.candidate_plot_count == 1
    assert product.candidates_index_html == str(index_html)

    with summary_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["object_id"] == "6751"
    assert rows[0]["light_curve_csv"].endswith("object_6751_src_702296666944246784_light_curve.csv")
    assert rows[0]["light_curve_png"].endswith("object_6751_src_702296666944246784_light_curve.png")
    # Selection diagnostics came from a deliberately different magnitude scale.
    # Public summary photometry must describe the actual exported science curve.
    assert float(rows[0]["mag_median"]) == pytest.approx(14.85)
    assert float(rows[0]["mag_rms"]) < 0.1
    assert float(rows[0]["mag_p10"]) == pytest.approx(14.81)
    assert float(rows[0]["mag_p90"]) == pytest.approx(14.89)
    assert float(rows[0]["mag_err_median"]) == 0.01
    assert float(rows[0]["mag_err_systematic_median"]) == 0.04

    summary_payload = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary_payload["summary"]["candidates_summary_csv"] == str(summary_csv)
    assert summary_payload["summary"]["candidates_index_html"] == str(index_html)

    html_text = index_html.read_text(encoding="utf-8")
    assert "object_6751_src_702296666944246784_light_curve.png" in html_text

    payload = json.loads(object_json.read_text(encoding="utf-8"))
    assert payload["candidate"]["object_id"] == 6751
    assert payload["candidate"]["mag_median"] == pytest.approx(14.85)
    assert payload["candidate"]["mag_err_systematic_median"] == 0.04
    assert payload["light_curve"]["rows_total"] == 2


def test_export_marks_candidate_with_no_accepted_points_as_diagnostic(monkeypatch, tmp_path):
    def fake_load_rows(session, **kwargs):
        object_id = kwargs["object_id"]
        return None, [
            {
                "object_id": object_id,
                "catalog_name": "gaia",
                "source_id": str(object_id),
                "frame_id": 1,
                "series_id": 3,
                "measurement_id": 10,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:00:00",
                "mjd": 60000.0,
                "jd": None,
                "filter_name": "i",
                "instrument_unit_id": 2,
                "instrument": "RoboPhot",
                "site": "KAO",
                "tube": "APM",
                "camera": "ML",
                "x": 1.0,
                "y": 2.0,
                "flux": 100.0,
                "flux_err": 3.0,
                "mag_inst": 15.0,
                "mag_cal": 14.9,
                "mag_err": 0.01,
                "mag_global": 14.9,
                "mag_global_err": 0.01,
                "interseries_offset_mag": None,
                "interseries_offset_err": None,
                "interseries_offset_status": None,
                "interseries_algorithm": None,
                "aperture_radius_pix": 3.0,
                "local_background": 1.0,
                "local_zp_correction": None,
                "centroid_dx_pix": None,
                "centroid_dy_pix": None,
                "object_qc_flags": "HIGH_RMS",
                "measurement_flags": "{}",
                "frame_quality_flags": "{}",
                "quality_flags": "{}",
                "is_rejected": True,
                "reject_reasons": "WEAK_OR_NO_DETECTION_SUPPORT",
            }
        ]

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)

    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[
            {"candidate_status": "ACCEPTED", "candidate_origin": "rms_excess", "object_id": 6849, "source_id": "702316148915929728"},
        ],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        reject_flags={"WEAK_OR_NO_DETECTION_SUPPORT"},
        out_dir=tmp_path,
    )

    assert product.triage_accepted_candidates == 1
    assert product.accepted_candidates == 0
    assert product.exported_light_curves == 0
    assert product.diagnostic_light_curves == 1
    assert product.rejected_no_accepted_points == 1

    with (tmp_path / "candidates_summary.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["candidate_export_status"] == "REJECTED_NO_ACCEPTED_POINTS"
    assert rows[0]["accepted_fraction"] == "0.0"

    payload = json.loads((tmp_path / "object_6849_src_702316148915929728_summary.json").read_text(encoding="utf-8"))
    assert payload["candidate_export_status"] == "REJECTED_NO_ACCEPTED_POINTS"
    assert payload["light_curve"]["rows_accepted"] == 0


def test_export_candidates_index_html_escapes_and_links(tmp_path):
    png = tmp_path / "candidate.png"
    png.write_bytes(b"png")
    rows = [
        {
            "rank": 1,
            "candidate_status": "ACCEPTED",
            "candidate_export_status": "EXPORTED",
            "candidate_origin": "low_amplitude_intrinsic",
            "object_id": "1<bad>",
            "source_id": "702",
            "light_curve_png": str(png),
            "light_curve_csv": str(tmp_path / "candidate.csv"),
            "light_curve_json": str(tmp_path / "candidate.json"),
        }
    ]
    count = cand.export_candidates_index_html(rows, tmp_path / "index.html", summary={"candidate_plot_count": 1})
    assert count == 1
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "candidate.png" in html
    assert "1&lt;bad&gt;" in html


def test_export_can_skip_candidate_png_plots(monkeypatch, tmp_path):
    def fake_load_rows(session, **kwargs):
        return None, [
            {
                "object_id": kwargs["object_id"],
                "catalog_name": "gaia",
                "source_id": str(kwargs["object_id"]),
                "frame_id": 1,
                "series_id": 3,
                "measurement_id": 10,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:00:00",
                "mjd": 60000.0,
                "mag_cal": 14.9,
                "mag_err": 0.01,
                "mag_global": 14.9,
                "mag_global_err": 0.01,
                "is_rejected": False,
                "reject_reasons": "",
            }
        ]

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)

    def fail_plot(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("plot function should not be called")

    monkeypatch.setattr(cand, "_export_candidate_light_curve_plot", fail_plot)

    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[{"candidate_status": "ACCEPTED", "candidate_origin": "intrinsic_variability", "object_id": 1, "source_id": "1"}],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        reject_flags=set(),
        out_dir=tmp_path,
        export_plots=False,
    )

    assert product.plot_backend == "disabled"
    assert product.candidate_plot_count == 0
    assert product.candidate_plot_warnings_count == 0
    assert product.summary_rows[0]["candidate_export_status"] == "EXPORTED"
    assert product.summary_rows[0]["light_curve_png"] is None


def test_export_warns_once_when_candidate_plot_backend_missing(monkeypatch, tmp_path):
    def fake_load_rows(session, **kwargs):
        return None, [
            {
                "object_id": kwargs["object_id"],
                "catalog_name": "gaia",
                "source_id": str(kwargs["object_id"]),
                "frame_id": 1,
                "series_id": 3,
                "measurement_id": 10,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:00:00",
                "mjd": 60000.0,
                "mag_cal": 14.9,
                "mag_err": 0.01,
                "mag_global": 14.9,
                "mag_global_err": 0.01,
                "is_rejected": False,
                "reject_reasons": "",
            }
        ]

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)
    monkeypatch.setattr(cand, "_candidate_plot_backend", lambda *, enabled=True: ("unavailable", "candidate plotting skipped: matplotlib is unavailable"))

    def fail_plot(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("plot function should not be called when backend is unavailable")

    monkeypatch.setattr(cand, "_export_candidate_light_curve_plot", fail_plot)

    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[
            {"candidate_status": "ACCEPTED", "candidate_origin": "intrinsic_variability", "object_id": 1, "source_id": "1"},
            {"candidate_status": "ACCEPTED", "candidate_origin": "intrinsic_variability", "object_id": 2, "source_id": "2"},
        ],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        reject_flags=set(),
        out_dir=tmp_path,
        export_plots=True,
    )

    assert product.plot_backend == "unavailable"
    assert product.candidate_plot_count == 0
    assert product.candidate_plot_warnings_count == 1
    assert len(product.warnings) == 1
    assert product.accepted_candidates == 2
    assert product.summary_rows[0]["light_curve_png"] is None
    assert product.summary_rows[1]["light_curve_png"] is None


def test_export_preserves_borderline_candidate_in_separate_products(monkeypatch, tmp_path):
    def fake_load_rows(session, **kwargs):
        return None, [
            {
                "object_id": kwargs["object_id"],
                "catalog_name": "gaia",
                "source_id": str(kwargs["object_id"]),
                "frame_id": 1,
                "series_id": 3,
                "measurement_id": 10,
                "calibration_name": "ens",
                "calibration_version_id": 99,
                "model_name": "ensemble",
                "date_obs": "2026-01-01T00:00:00",
                "mjd": 60000.0,
                "mag_cal": 14.9,
                "mag_err": 0.01,
                "mag_global": 14.9,
                "mag_global_err": 0.01,
                "mag_global_err_random": 0.01,
                "mag_global_err_systematic": 0.04,
                "is_rejected": False,
                "reject_reasons": "",
            }
        ]

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)
    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[
            {
                "candidate_status": "BORDERLINE",
                "candidate_origin": "",
                "candidate_diagnostic_class": "BORDERLINE_LOW_AMPLITUDE",
                "object_id": 6751,
                "source_id": "702296666944246784",
                "low_amplitude_failed_criteria": "mad_over_mag_err",
                "low_amplitude_failed_criteria_count": 1,
                "low_amplitude_min_threshold_ratio": 0.965,
                "low_amplitude_borderline_threshold_ratio": 0.9,
                "low_amplitude_n_measurements_value": 70.0,
                "low_amplitude_n_measurements_threshold": 30.0,
                "low_amplitude_n_measurements_pass": True,
                "low_amplitude_n_measurements_margin": 40.0,
                "low_amplitude_n_measurements_ratio": 70.0 / 30.0,
                "low_amplitude_intrinsic_rms_value": 0.03395,
                "low_amplitude_intrinsic_rms_threshold": 0.03,
                "low_amplitude_intrinsic_rms_pass": True,
                "low_amplitude_intrinsic_rms_margin": 0.00395,
                "low_amplitude_intrinsic_rms_ratio": 0.03395 / 0.03,
                "low_amplitude_rms_over_mag_err_value": 2.334,
                "low_amplitude_rms_over_mag_err_threshold": 2.3,
                "low_amplitude_rms_over_mag_err_pass": True,
                "low_amplitude_rms_over_mag_err_margin": 0.034,
                "low_amplitude_rms_over_mag_err_ratio": 2.334 / 2.3,
                "low_amplitude_mad_over_mag_err_value": 1.93,
                "low_amplitude_mad_over_mag_err_threshold": 2.0,
                "low_amplitude_mad_over_mag_err_pass": False,
                "low_amplitude_mad_over_mag_err_margin": -0.07,
                "low_amplitude_mad_over_mag_err_ratio": 0.965,
                "low_amplitude_robust_peak_to_peak_value": 0.0856,
                "low_amplitude_robust_peak_to_peak_threshold": 0.08,
                "low_amplitude_robust_peak_to_peak_pass": True,
                "low_amplitude_robust_peak_to_peak_margin": 0.0056,
                "low_amplitude_robust_peak_to_peak_ratio": 1.07,
            }
        ],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        reject_flags=set(),
        out_dir=tmp_path,
        export_plots=False,
        membership_history=[{"stage_index": 1, "accepted_object_ids": [], "borderline_object_ids": [6751]}],
    )

    assert product.triage_accepted_candidates == 0
    assert product.accepted_candidates == 0
    assert product.borderline_candidates == 1
    assert product.diagnostic_light_curves == 1
    assert (tmp_path / "borderline_candidates.csv").exists()
    assert (tmp_path / "borderline_candidates.json").exists()
    assert (tmp_path / "candidate_membership_history.json").exists()
    payload = json.loads((tmp_path / "borderline_candidates.json").read_text(encoding="utf-8"))
    assert payload["candidates"][0]["object_id"] == 6751
    assert payload["candidates"][0]["candidate_export_status"] == "BORDERLINE_EXPORTED"
    assert payload["candidates"][0]["low_amplitude_mad_over_mag_err_value"] == pytest.approx(1.93)
    assert payload["candidates"][0]["low_amplitude_mad_over_mag_err_threshold"] == pytest.approx(2.0)
    assert payload["candidates"][0]["low_amplitude_mad_over_mag_err_pass"] is False
    assert payload["candidates"][0]["low_amplitude_mad_over_mag_err_ratio"] == pytest.approx(0.965)

    with (tmp_path / "borderline_candidates.csv").open(newline="", encoding="utf-8") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["low_amplitude_mad_over_mag_err_value"] == "1.93"
    assert csv_row["low_amplitude_mad_over_mag_err_threshold"] == "2.0"
    assert csv_row["low_amplitude_mad_over_mag_err_pass"] == "False"
    assert csv_row["low_amplitude_mad_over_mag_err_ratio"] == "0.965"
    assert csv_row["low_amplitude_n_measurements_value"] == "70.0"
    assert csv_row["low_amplitude_intrinsic_rms_threshold"] == "0.03"


def test_export_preserves_near_threshold_diagnostic_and_floor_ab(monkeypatch, tmp_path):
    def fake_load_rows(session, **kwargs):
        object_id = kwargs["object_id"]
        mags = [14.84, 14.88, 14.91, 14.86, 14.90]
        rows = []
        for index, mag in enumerate(mags, start=1):
            rows.append(
                {
                    "object_id": object_id,
                    "catalog_name": "gaia",
                    "source_id": str(object_id),
                    "frame_id": index,
                    "series_id": 3,
                    "measurement_id": index,
                    "calibration_name": "ens",
                    "calibration_version_id": 99,
                    "model_name": "ensemble",
                    "date_obs": f"2026-01-01T00:0{index}:00",
                    "mjd": 60000.0 + index / 1440.0,
                    "mag_cal": mag,
                    "mag_err": 0.015,
                    "mag_global": mag,
                    "mag_global_err": 0.015,
                    "mag_global_err_random": 0.020,
                    "mag_global_err_systematic": 0.04,
                    "mag_err_detector": 0.015,
                    "mag_err_epoch_floor": 0.025,
                    "mag_err_effective_detector_only": (0.015**2 + 0.025**2) ** 0.5,
                    "mag_err_effective_total": (0.020**2 + 0.025**2) ** 0.5,
                    "mag_err_effective": (0.020**2 + 0.025**2) ** 0.5,
                    "residual_noise_floor_applied": False,
                    "is_rejected": False,
                    "reject_reasons": "",
                }
            )
        return None, rows

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fake_load_rows)

    product = cand.export_accepted_candidate_light_curves(
        object(),
        candidate_rows=[
            {
                "candidate_status": "DIAGNOSTIC",
                "candidate_diagnostic_class": "NEAR_THRESHOLD_LOW_AMPLITUDE",
                "candidate_origin": "",
                "object_id": 6751,
                "source_id": "702296666944246784",
                "n_measurements": 70,
                "low_amplitude_n_measurements_threshold": 30.0,
                "low_amplitude_intrinsic_rms_threshold": 0.03,
                "low_amplitude_rms_over_mag_err_threshold": 2.3,
                "low_amplitude_mad_over_mag_err_threshold": 2.0,
                "low_amplitude_robust_peak_to_peak_threshold": 0.08,
            }
        ],
        calibration_name="ens",
        catalog_name="gaia",
        series_id=3,
        reject_flags=set(),
        out_dir=tmp_path,
        export_plots=False,
    )

    assert product.accepted_candidates == 0
    assert product.near_threshold_candidates == 1
    assert product.diagnostic_light_curves == 1
    assert (tmp_path / "near_threshold_candidates.csv").exists()
    assert (tmp_path / "near_threshold_candidates.json").exists()
    assert (tmp_path / "residual_floor_candidate_ab.csv").exists()
    assert (tmp_path / "residual_floor_candidate_ab.json").exists()

    with (tmp_path / "near_threshold_candidates.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["candidate_status"] == "DIAGNOSTIC"
    assert rows[0]["candidate_export_status"] == "DIAGNOSTIC_EXPORTED"
    assert float(rows[0]["mag_err_detector_median"]) == pytest.approx(0.015)
    assert float(rows[0]["mag_err_epoch_floor_median"]) == pytest.approx(0.025)
    assert float(rows[0]["mag_err_effective_detector_only_median"]) == pytest.approx(
        (0.015**2 + 0.025**2) ** 0.5
    )
    assert float(rows[0]["mag_err_effective_total_median"]) == pytest.approx(
        (0.020**2 + 0.025**2) ** 0.5
    )
    assert float(rows[0]["mag_err_effective_median"]) == pytest.approx(
        (0.020**2 + 0.025**2) ** 0.5
    )
    expected_rms = float(rows[0]["mag_rms"]) / max(
        (0.020**2 + 0.025**2) ** 0.5, 0.01
    )
    assert float(rows[0]["residual_floor_ab_rms_over_mag_err"]) == pytest.approx(
        expected_rms
    )
    assert rows[0]["residual_floor_ab_status"] in {"PASS", "FAIL"}

    ab_payload = json.loads((tmp_path / "residual_floor_candidate_ab.json").read_text(encoding="utf-8"))
    assert ab_payload["summary"]["floor_applied"] is False
    assert ab_payload["summary"]["count"] == 1


def test_candidate_export_does_not_swallow_database_errors(monkeypatch, tmp_path):
    from sqlalchemy.exc import SQLAlchemyError

    def fail_load_rows(session, **kwargs):
        raise SQLAlchemyError("primary database failure")

    monkeypatch.setattr(cand, "load_science_light_curve_rows", fail_load_rows)

    with pytest.raises(SQLAlchemyError, match="primary database failure"):
        cand.export_accepted_candidate_light_curves(
            object(),
            candidate_rows=[
                {
                    "candidate_status": "ACCEPTED",
                    "candidate_origin": "intrinsic_variability",
                    "object_id": 42,
                    "source_id": "42",
                }
            ],
            calibration_name="ens",
            catalog_name="gaia",
            frame_ids=[1, 2],
            out_dir=tmp_path / "candidates",
            export_plots=False,
        )

    assert not (tmp_path / "candidates").exists()
    assert not list(tmp_path.glob(".candidates.staging-*"))
