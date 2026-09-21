from __future__ import annotations

import csv
from pathlib import Path

import pytest

from grandgousier.quality.candidate_evidence import (
    build_candidate_evidence_report,
    compare_light_curves,
    export_candidate_evidence_report,
)


def _write_series(
    root: Path,
    *,
    label: str,
    source_id: str,
    filter_name: str,
    camera: str,
    series_id: int,
    values: list[float],
    status: str = "ACCEPTED",
    residual_floor_status: str = "FAIL",
    mag_median: float | None = None,
    candidate_origin: str = "intrinsic_variability",
    is_target: bool = False,
) -> Path:
    series_dir = root / label
    candidates_dir = series_dir / "candidates"
    candidates_dir.mkdir(parents=True)
    summary_path = candidates_dir / "candidates_summary.csv"
    summary_columns = [
        "candidate_status",
        "candidate_origin",
        "object_id",
        "catalog_name",
        "source_id",
        "mag_median",
        "residual_floor_ab_status",
        "is_target",
    ]
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_columns)
        writer.writeheader()
        writer.writerow(
            {
                "candidate_status": status,
                "candidate_origin": candidate_origin,
                "object_id": 10 + series_id,
                "catalog_name": "shared",
                "source_id": source_id,
                "mag_median": 14.0 + 0.01 * series_id if mag_median is None else mag_median,
                "residual_floor_ab_status": residual_floor_status,
                "is_target": str(is_target),
            }
        )
    light_curve_path = candidates_dir / f"object_{10 + series_id}_src_{source_id}_light_curve.csv"
    columns = [
        "source_id",
        "series_id",
        "field_name",
        "filter_name",
        "camera",
        "mjd",
        "mag_cal",
        "is_rejected",
    ]
    with light_curve_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for index, value in enumerate(values):
            writer.writerow(
                {
                    "source_id": source_id,
                    "series_id": series_id,
                    "field_name": "FIELD",
                    "filter_name": filter_name,
                    "camera": camera,
                    "mjd": 60000.0 + index / 86400.0,
                    "mag_cal": value,
                    "is_rejected": "False",
                }
            )
    return series_dir


def test_compare_light_curves_pairs_and_correlates_simultaneous_points():
    curve_a = [
        {"mjd": 60000.0 + index / 86400.0, "mag_cal": value, "is_rejected": False}
        for index, value in enumerate([1.0, 2.0, 3.0, 4.0])
    ]
    curve_b = [
        {"mjd": 60000.0 + index / 86400.0, "mag_cal": value, "is_rejected": False}
        for index, value in enumerate([2.0, 4.0, 6.0, 8.0])
    ]

    result = compare_light_curves(curve_a, curve_b, max_time_separation_sec=0.1)

    assert result["n_pairs"] == 4
    assert result["pearson_r"] == pytest.approx(1.0)
    assert result["spearman_rho"] == pytest.approx(1.0)
    assert result["max_time_separation_sec"] == pytest.approx(0.0)


def test_cross_series_report_prioritizes_multi_filter_coherence(tmp_path: Path):
    source_id = "123456"
    series_i = _write_series(
        tmp_path,
        label="series_i",
        source_id=source_id,
        filter_name="i",
        camera="CAM_I",
        series_id=1,
        values=[10.0, 10.1, 9.9, 10.2, 9.8, 10.15, 9.85, 10.05, 9.95, 10.12],
        status="DIAGNOSTIC",
    )
    series_r = _write_series(
        tmp_path,
        label="series_r",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=2,
        values=[11.0, 11.1, 10.9, 11.2, 10.8, 11.15, 10.85, 11.05, 10.95, 11.12],
        status="ACCEPTED",
    )

    report = build_candidate_evidence_report(
        [series_i, series_r],
        max_time_separation_sec=1.0,
        min_pairs=10,
        min_correlation=0.25,
    )

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row["source_id"] == source_id
    assert row["evidence_status"] == "COHERENT_MULTI_FILTER"
    assert row["multi_filter_coherent"] is True
    assert row["best_pair_n_pairs"] == 10
    assert row["best_pair_pearson_r"] == pytest.approx(1.0)
    assert row["best_pair_spearman_rho"] == pytest.approx(1.0)

    outputs = export_candidate_evidence_report(report, out_prefix=tmp_path / "report")
    assert Path(outputs["json"]).exists()
    assert Path(outputs["candidates_csv"]).exists()
    assert Path(outputs["pairs_csv"]).exists()


def test_real_bklyn_exports_are_detected_as_coherent_when_available():
    export_root = Path("/mnt/data/inspect_exports1")
    series = [
        export_root / "auto_robophot_20260328_BK_Lyn_APM-RoboPhot_ML0742515_i_s5_v1",
        export_root / "auto_robophot_20260328_BK_Lyn_APM-RoboPhot_ML0892515_r_s6_v1",
        export_root / "auto_robophot_20260402_BK_Lyn_APM-RoboPhot_ML0892515_r_s7_v1",
    ]
    if not all(path.exists() for path in series):
        pytest.skip("real BK Lyn export bundle is unavailable")

    report = build_candidate_evidence_report(series)
    bklyn = next(row for row in report.rows if row["source_id"] == "702296666944246784")

    assert bklyn["evidence_status"] == "TARGET_COHERENT_MULTI_FILTER"
    assert bklyn["best_pair_n_pairs"] == 70
    assert bklyn["best_pair_pearson_r"] == pytest.approx(0.2836037502412263)
    assert bklyn["best_pair_spearman_rho"] == pytest.approx(0.30098853993526375)
    assert bklyn["auto_accepted_series_count"] == 2
    assert bklyn["forced_target_series_count"] == 1
    assert bklyn["same_filter_max_median_delta"] == pytest.approx(0.05875252148077692)
    assert bklyn["all_filter_median_range"] == pytest.approx(0.23597494470097935)
    assert bklyn["multi_filter_significant_positive"] is True


def test_explicit_target_source_id_promotes_evidence_status(tmp_path: Path):
    source_id = "987654"
    series_i = _write_series(
        tmp_path,
        label="target_i",
        source_id=source_id,
        filter_name="i",
        camera="CAM_I",
        series_id=11,
        values=[10.0, 10.1, 9.9, 10.2, 9.8, 10.15, 9.85, 10.05, 9.95, 10.12],
        status="DIAGNOSTIC",
    )
    series_r = _write_series(
        tmp_path,
        label="target_r",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=12,
        values=[11.0, 11.1, 10.9, 11.2, 10.8, 11.15, 10.85, 11.05, 10.95, 11.12],
        status="ACCEPTED",
    )

    report = build_candidate_evidence_report(
        [series_i, series_r],
        max_time_separation_sec=1.0,
        min_pairs=10,
        target_source_ids=[source_id],
    )

    assert report.rows[0]["is_target"] is True
    assert report.rows[0]["evidence_status"] == "TARGET_COHERENT_MULTI_FILTER"
    assert report.parameters["target_source_ids"] == [source_id]


def test_candidate_evidence_rejects_duplicate_series_and_invalid_thresholds(tmp_path: Path):
    series = _write_series(
        tmp_path,
        label="series",
        source_id="123",
        filter_name="r",
        camera="CAM",
        series_id=1,
        values=[1.0, 1.1, 0.9],
    )

    with pytest.raises(ValueError, match="must be unique"):
        build_candidate_evidence_report([series, series])
    with pytest.raises(ValueError, match="max_time_separation_sec"):
        build_candidate_evidence_report([series, tmp_path / "other"], max_time_separation_sec=-1)
    with pytest.raises(ValueError, match="min_pairs"):
        build_candidate_evidence_report([series, tmp_path / "other"], min_pairs=2)
    with pytest.raises(ValueError, match="min_correlation"):
        build_candidate_evidence_report([series, tmp_path / "other"], min_correlation=1.1)
    with pytest.raises(ValueError, match="max_fdr_q"):
        build_candidate_evidence_report([series, tmp_path / "other"], max_fdr_q=0.0)
    with pytest.raises(ValueError, match="min_repeat_series"):
        build_candidate_evidence_report([series, tmp_path / "other"], min_repeat_series=1)


def test_median_magnitude_range_is_filter_aware(tmp_path: Path):
    source_id = "filter-aware"
    series_i = _write_series(
        tmp_path,
        label="series_i",
        source_id=source_id,
        filter_name="i",
        camera="CAM_I",
        series_id=1,
        values=[10.0, 10.1, 9.9, 10.2],
        mag_median=14.70,
    )
    series_r1 = _write_series(
        tmp_path,
        label="series_r1",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=2,
        values=[11.0, 11.1, 10.9, 11.2],
        mag_median=14.46,
    )
    series_r2 = _write_series(
        tmp_path,
        label="series_r2",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=3,
        values=[11.0, 11.1, 10.9, 11.2],
        mag_median=14.52,
    )

    report = build_candidate_evidence_report(
        [series_i, series_r1, series_r2],
        min_pairs=3,
    )

    row = report.rows[0]
    assert row["all_filter_median_range"] == pytest.approx(0.24)
    assert row["same_filter_max_median_delta"] == pytest.approx(0.06)
    assert row["same_filter_median_range_by_filter"] == {"r": pytest.approx(0.06)}
    assert row["same_filter_comparable_filter_count"] == 1
    assert row["median_mag_range"] == pytest.approx(0.06)
    assert row["median_mag_range_semantics"] == "same_filter_max_median_delta_v2"


def test_forced_target_is_not_counted_as_auto_accepted(tmp_path: Path):
    source_id = "target"
    forced = _write_series(
        tmp_path,
        label="forced",
        source_id=source_id,
        filter_name="i",
        camera="CAM_I",
        series_id=1,
        values=[10.0, 10.1, 9.9, 10.2],
        candidate_origin="target",
        is_target=True,
    )
    automatic = _write_series(
        tmp_path,
        label="automatic",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=2,
        values=[11.0, 11.1, 10.9, 11.2],
        candidate_origin="low_amplitude_intrinsic,target",
        is_target=True,
    )

    report = build_candidate_evidence_report(
        [forced, automatic],
        min_pairs=3,
        min_correlation=0.99,
        target_source_ids=[source_id],
    )

    row = report.rows[0]
    assert row["accepted_series_count"] == 2
    assert row["auto_accepted_series_count"] == 1
    assert row["forced_target_series_count"] == 1
    assert row["repeated_accepted"] is False
    assert row["evidence_status"] == "TARGET_COHERENT_MULTI_FILTER"


def test_pair_report_adds_fdr_and_signed_significance(tmp_path: Path):
    source_id = "positive"
    series_i = _write_series(
        tmp_path,
        label="positive_i",
        source_id=source_id,
        filter_name="i",
        camera="CAM_I",
        series_id=1,
        values=[10.0, 10.1, 9.9, 10.2, 9.8, 10.3, 9.7, 10.4, 9.6, 10.5],
    )
    series_r = _write_series(
        tmp_path,
        label="positive_r",
        source_id=source_id,
        filter_name="r",
        camera="CAM_R",
        series_id=2,
        values=[11.0, 11.1, 10.9, 11.2, 10.8, 11.3, 10.7, 11.4, 10.6, 11.5],
    )

    report = build_candidate_evidence_report(
        [series_i, series_r],
        max_time_separation_sec=1.0,
        min_pairs=10,
        min_correlation=0.25,
        max_fdr_q=0.05,
    )

    pair = report.pair_rows[0]
    assert pair["pearson_p"] == pytest.approx(0.0)
    assert pair["spearman_p"] == pytest.approx(0.0)
    assert pair["pearson_fdr_q"] == pytest.approx(0.0)
    assert pair["spearman_fdr_q"] == pytest.approx(0.0)
    assert pair["significant_positive"] is True
    assert pair["significant_negative"] is False
    assert pair["significance_status"] == "SIGNIFICANT_POSITIVE"
    row = report.rows[0]
    assert row["multi_filter_significant_positive"] is True
    assert row["multi_filter_significant_negative"] is False
    assert row["best_pair_significance_status"] == "SIGNIFICANT_POSITIVE"
