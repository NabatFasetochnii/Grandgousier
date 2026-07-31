import csv
import json

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
            {"candidate_status": "ACCEPTED", "candidate_origin": "low_amplitude_intrinsic", "object_id": 6751, "source_id": "702296666944246784", "mag_rms": 0.035, "robust_peak_to_peak": 0.09},
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
    assert product.candidate_plot_count == 1
    assert product.candidates_index_html == str(index_html)

    with summary_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["object_id"] == "6751"
    assert rows[0]["light_curve_csv"].endswith("object_6751_src_702296666944246784_light_curve.csv")
    assert rows[0]["light_curve_png"].endswith("object_6751_src_702296666944246784_light_curve.png")

    summary_payload = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary_payload["summary"]["candidates_summary_csv"] == str(summary_csv)
    assert summary_payload["summary"]["candidates_index_html"] == str(index_html)

    html_text = index_html.read_text(encoding="utf-8")
    assert "object_6751_src_702296666944246784_light_curve.png" in html_text

    payload = json.loads(object_json.read_text(encoding="utf-8"))
    assert payload["candidate"]["object_id"] == 6751
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
