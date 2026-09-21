from __future__ import annotations

from pathlib import Path

import pytest

from grandgousier.lightcurves import global_object as m5


class _MappingsResult:
    def __init__(self, rows):
        self.rows = list(rows)
    def mappings(self):
        return self
    def all(self):
        return list(self.rows)
    def first(self):
        return self.rows[0] if self.rows else None
    def __iter__(self):
        return iter(self.rows)


def test_m5_columns_keep_random_and_correlated_zp_separate() -> None:
    columns = m5.global_light_curve_columns()
    for name in (
        "mag_science",
        "mag_global",
        "mag_err_random",
        "mag_err_science_systematic",
        "mag_err_series_zp",
        "mag_err_effective",
        "series_zp_correlated",
        "science_calibration_version_id",
        "interseries_generation_id",
        "interseries_reliability",
        "m5_link_status",
    ):
        assert name in columns


def test_to_m5_row_applies_additive_offset_and_keeps_error_semantics() -> None:
    raw = {
        "science_measurement_id": 10,
        "measurement_id": 10,
        "object_id": 5,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.345,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.003,
        "interseries_offset_err": 0.004,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 2,
        "interseries_generation_uid": "m4_x",
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "interseries_graph_dof": 0,
        "interseries_reference_series_id": 76,
    }
    row = m5._to_m5_row(raw, reject_flags=set(), field_id=33)
    assert row["mag_science"] == pytest.approx(12.345)
    assert row["mag_global"] == pytest.approx(12.348)
    assert row["mag_err_random"] == pytest.approx(0.02)
    assert row["mag_err_series_zp"] == pytest.approx(0.004)
    assert row["mag_err_effective"] == pytest.approx((0.02**2 + 0.004**2) ** 0.5)
    assert row["series_zp_correlated"] is True
    assert row["m5_link_status"] == "M4_EXACT_SOURCE_MATCH"
    assert row["is_rejected"] is False


def test_auto_generation_selection_refuses_ambiguous_same_scope() -> None:
    rows = []
    for gid, uid, cv in ((1, "m4_a", 960), (3, "m4_b", 1001)):
        rows.append({
            "interseries_generation_id": gid,
            "generation_uid": uid,
            "field_id": 33,
            "instrument_unit_id": 7,
            "filter_name": "V",
            "catalog_name": "shared",
            "reference_series_id": 76,
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "graph_dof": 1,
            "series_id": 76,
            "science_calibration_version_id": cv,
        })

    class FakeSession:
        def execute(self, statement, params):
            return _MappingsResult(rows)

    with pytest.raises(ValueError, match="Ambiguous M4 generations"):
        m5.resolve_m5_generations(FakeSession(), field_id=33)


def test_explicit_generation_selection_allows_distinct_filter_scopes() -> None:
    rows = [
        {
            "interseries_generation_id": 1,
            "generation_uid": "m4_r",
            "field_id": 33,
            "instrument_unit_id": 8,
            "filter_name": "R",
            "catalog_name": "shared",
            "reference_series_id": 77,
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "graph_dof": 1,
            "series_id": 77,
            "science_calibration_version_id": 967,
        },
        {
            "interseries_generation_id": 2,
            "generation_uid": "m4_v",
            "field_id": 33,
            "instrument_unit_id": 7,
            "filter_name": "V",
            "catalog_name": "shared",
            "reference_series_id": 76,
            "reliability_status": "SPARSE_GRAPH_NO_CLOSURE",
            "graph_dof": 0,
            "series_id": 76,
            "science_calibration_version_id": 960,
        },
    ]

    class FakeSession:
        def execute(self, statement, params):
            assert params["generation_ids"] == [1, 2]
            return _MappingsResult(rows)

    resolved = m5.resolve_m5_generations(FakeSession(), field_id=33, generation_ids=[2, 1])
    assert [row.interseries_generation_id for row in resolved] == [1, 2]


def test_plot_plan_switches_to_binning_without_limiting_main_csv() -> None:
    stats = [
        {"filter_name": "V", "series_id": 76, "n_points": 7000, "mjd_min": 60000.0, "mjd_max": 60001.0},
        {"filter_name": "V", "series_id": 78, "n_points": 3000, "mjd_min": 60002.0, "mjd_max": 60003.0},
        {"filter_name": "R", "series_id": 77, "n_points": 100, "mjd_min": 60000.0, "mjd_max": 60001.0},
    ]
    plan = m5._build_plot_plan(stats, reduction="auto", max_points=5000, bin_minutes=None)
    assert plan["V"]["mode"] == "bin"
    assert plan["R"]["mode"] == "raw"
    allocations = m5._allocate_plot_points(stats[:2], max_points=5000)
    assert sum(allocations.values()) == 5000
    assert allocations[76] > allocations[78]


def test_streaming_bin_accumulator_keeps_mean_envelope_and_random_sem() -> None:
    plan = {
        "mode": "bin",
        "series": {76: {"mjd_min": 60000.0, "bin_width_days": 1.0}},
    }
    bins = {}
    base = {
        "filter_name": "V",
        "series_id": 76,
        "interseries_generation_id": 2,
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "mag_err_series_zp": 0.004,
    }
    for mjd, mag in ((60000.1, 12.0), (60000.2, 12.2), (60000.3, 11.8)):
        row = dict(base, mjd=mjd, mag_global=mag, mag_err_random=0.03)
        m5._accumulate_plot_bin(bins, row, plan)
    assert len(bins) == 1
    reduced = next(iter(bins.values())).finalize()
    assert reduced["n_points"] == 3
    assert reduced["mag_global"] == pytest.approx(12.0)
    assert reduced["mag_min"] == pytest.approx(11.8)
    assert reduced["mag_max"] == pytest.approx(12.2)
    assert reduced["mag_err_series_zp"] == pytest.approx(0.004)
    assert reduced["mag_err_random"] > 0


def test_m5_series_coverage_detects_stale_and_unlinked_current_science(monkeypatch) -> None:
    current = [
        {"series_id": 76, "filter_name": "V", "calibration_version_id": 960},
        {"series_id": 78, "filter_name": "V", "calibration_version_id": 999},
        {"series_id": 79, "filter_name": "V", "calibration_version_id": 981},
    ]
    monkeypatch.setattr(m5, "_load_series_candidates", lambda *args, **kwargs: current)

    generation = m5.M5Generation(
        interseries_generation_id=2,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="SPARSE_GRAPH_NO_CLOSURE",
        graph_dof=0,
        series_sources=((76, 960), (78, 991)),
    )

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM science_photometry_cache" in sql:
                return _MappingsResult([])
            if "AS n_points" in sql:
                return _MappingsResult([
                    {"calibration_version_id": 960, "n_points": 10},
                    {"calibration_version_id": 999, "n_points": 12},
                    {"calibration_version_id": 981, "n_points": 8},
                ])
            if "FROM science_measurements" in sql:
                return _MappingsResult([
                    {"calibration_version_id": 960, "n": 10},
                    {"calibration_version_id": 999, "n": 12},
                    {"calibration_version_id": 981, "n": 8},
                ])
            return _MappingsResult([])

    coverage = m5.inspect_m5_series_coverage(
        FakeSession(), field_id=33, object_id=42, generations=[generation]
    )
    statuses = {row.series_id: row.status for row in coverage}
    assert statuses[76] == "READY_EXACT_M4_SOURCE"
    assert statuses[78] == "M4_SOURCE_NOT_CURRENT_SCIENCE"
    assert statuses[79] == "NO_COMPATIBLE_INTERSERIES_GENERATION"


def test_global_plot_export_uses_existing_matplotlib_infrastructure(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = [
        {
            "mjd": 60000.0,
            "mag_global": 12.0,
            "mag_err_random": 0.02,
            "mag_err_series_zp": 0.003,
            "series_id": 76,
            "mag_min": 11.98,
            "mag_max": 12.02,
        },
        {
            "mjd": 60000.1,
            "mag_global": 12.1,
            "mag_err_random": 0.02,
            "mag_err_series_zp": 0.003,
            "series_id": 76,
            "mag_min": 12.05,
            "mag_max": 12.14,
        },
    ]
    out = tmp_path / "global.png"
    result = export_global_lightcurve_plot(rows, out, reduction_mode="bin")
    assert out.exists()
    assert out.stat().st_size > 0
    assert result.n_points == 2
    assert result.inverted_y is True


def test_m5_cli_contract() -> None:
    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    args = build_parser().parse_args([
        "export-global-object-light-curve",
        "--object-id", "42",
        "--field-id", "33",
        "--generation-id", "1",
        "--generation-id", "2",
        "--out-root", "reports/object42",
        "--plot-max-points", "3000",
        "--plot-reduction", "auto",
        "--plot-time-axis", "compressed",
        "--include-rejected-debug",
    ])
    assert args.object_id == 42
    assert args.field_id == 33
    assert args.generation_ids == [1, 2]
    assert args.plot_max_points == 3000
    assert args.plot_reduction == "auto"
    assert args.plot_time_axis == "compressed"
    assert args.include_rejected_debug is True


def test_plot_budget_is_strict_even_for_series_endpoints() -> None:
    stats = [
        {"filter_name": "V", "series_id": 76, "n_points": 25, "mjd_min": 60000.0, "mjd_max": 60000.2},
        {"filter_name": "V", "series_id": 78, "n_points": 29, "mjd_min": 60003.0, "mjd_max": 60003.3},
        {"filter_name": "V", "series_id": 79, "n_points": 10, "mjd_min": 60003.5, "mjd_max": 60003.6},
    ]
    plan = m5._build_plot_plan(stats, reduction="bin", max_points=20, bin_minutes=None)
    assert sum(int(row["max_bins"]) for row in plan["V"]["series"].values()) == 20

    bins = {}
    for stat in stats:
        n = int(stat["n_points"])
        start = float(stat["mjd_min"])
        end = float(stat["mjd_max"])
        for i in range(n):
            frac = i / (n - 1) if n > 1 else 0.0
            row = {
                "filter_name": "V",
                "series_id": int(stat["series_id"]),
                "interseries_generation_id": 2,
                "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
                "mag_err_series_zp": 0.003,
                "mag_err_random": 0.02,
                "mjd": start + frac * (end - start),
                "mag_global": 12.0 + 0.01 * frac,
            }
            m5._accumulate_plot_bin(bins, row, plan["V"])
    assert len(bins) <= 20
    per_series = {}
    for _filter, series_id, _bin_index in bins:
        per_series[series_id] = per_series.get(series_id, 0) + 1
    for series_id, series_plan in plan["V"]["series"].items():
        assert per_series.get(series_id, 0) <= int(series_plan["max_bins"])


def test_plot_budget_refuses_to_hide_entire_series() -> None:
    stats = [
        {"filter_name": "V", "series_id": 1, "n_points": 10, "mjd_min": 1.0, "mjd_max": 1.1},
        {"filter_name": "V", "series_id": 2, "n_points": 10, "mjd_min": 2.0, "mjd_max": 2.1},
        {"filter_name": "V", "series_id": 3, "n_points": 10, "mjd_min": 3.0, "mjd_max": 3.1},
    ]
    with pytest.raises(ValueError, match="smaller than the number of non-empty series"):
        m5._build_plot_plan(stats, reduction="bin", max_points=2, bin_minutes=None)


def test_compressed_time_axis_preserves_intraseries_time_and_shortens_large_gaps() -> None:
    from grandgousier.visualization.lightcurve_plot import _build_global_time_axis

    rows = [
        {"series_id": 76, "mjd": 60000.0, "mag_global": 12.0},
        {"series_id": 76, "mjd": 60000.1, "mag_global": 12.1},
        {"series_id": 78, "mjd": 60003.0, "mag_global": 12.0},
        {"series_id": 78, "mjd": 60003.2, "mag_global": 12.1},
        {"series_id": 79, "mjd": 60003.5, "mag_global": 12.0},
        {"series_id": 79, "mjd": 60003.6, "mag_global": 12.1},
    ]
    axis = _build_global_time_axis(rows, requested_mode="auto")
    assert axis["mode"] == "compressed"
    assert len(axis["break_positions"]) == 2
    xs = list(axis["x_values"])
    assert xs[1] - xs[0] == pytest.approx(0.1)
    assert xs[3] - xs[2] == pytest.approx(0.2)
    assert xs[-1] - xs[0] < 1.0
    assert len(axis["tick_positions"]) == 3


def test_global_plot_reports_compressed_time_axis(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = [
        {"mjd": 60000.0, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": 76},
        {"mjd": 60000.1, "mag_global": 12.1, "mag_err_random": 0.02, "series_id": 76},
        {"mjd": 60003.0, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": 78},
        {"mjd": 60003.1, "mag_global": 12.1, "mag_err_random": 0.02, "series_id": 78},
    ]
    out = tmp_path / "compressed.png"
    result = export_global_lightcurve_plot(rows, out, time_axis_mode="auto")
    assert out.exists()
    assert result.time_axis_mode == "compressed"
    assert result.n_time_breaks == 1
    assert result.x_key == "mjd_compressed"


def test_global_plot_switches_to_archive_overview_for_many_series(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = []
    for series_id in range(1, 14):
        base = 60000.0 + series_id
        rows.extend([
            {"mjd": base, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": series_id},
            {"mjd": base + 0.05, "mag_global": 12.01, "mag_err_random": 0.02, "series_id": series_id},
        ])
    out = tmp_path / "overview.png"
    result = export_global_lightcurve_plot(rows, out, time_axis_mode="auto")
    assert out.exists()
    assert result.plot_style == "archive_overview"
    assert result.time_axis_mode == "compressed"
    assert result.n_time_breaks >= 1

def test_export_m5_writes_exact_csv_and_summary_without_downsampling(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_r",
        field_id=33,
        instrument_unit_id=8,
        filter_name="R",
        catalog_name="shared",
        reference_series_id=77,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((77, 967),),
    )
    coverage = [
        m5.SeriesCoverage(77, "R", 967, 967, 2, "READY_EXACT_M4_SOURCE")
    ]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "R", "series_id": 77, "n_points": 2, "mjd_min": 60000.0, "mjd_max": 60000.1}
        ],
    )

    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 42,
        "frame_id": 100,
        "series_id": 77,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_r",
        "calibration_version_id": 967,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 8,
        "instrument": "MASTER",
        "site": "X",
        "tube": "WEST",
        "camera": "203",
        "filter_name": "R",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.3,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.004,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 1,
        "interseries_generation_uid": "m4_r",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 1,
        "interseries_reference_series_id": 77,
    }
    rejected = dict(base)
    rejected.update(
        science_measurement_id=2,
        measurement_id=2,
        frame_id=101,
        mjd=60000.1,
        measurement_flags={"catalog_name": "shared", "source_id": "42", "SATURATED_OR_NEAR_SATURATED": True},
    )
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([base, rejected]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 42}])
            raise AssertionError(str(statement))

    root = tmp_path / "object42"
    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=42,
        field_id=33,
        out_root=root,
        include_rejected=False,
        plots=False,
    )
    assert result.status == m5.M5_STATUS_READY
    assert result.rows_source_exact_m4 == 2
    assert result.accepted_rows == 1
    assert result.rejected_rows == 1
    assert result.rows_written == 1
    assert result.plot_policy["main_csv_downsampled"] is False
    csv_lines = Path(result.csv_path).read_text(encoding="utf-8").splitlines()
    assert len(csv_lines) == 2
    summary = Path(result.summary_path).read_text(encoding="utf-8")
    assert "REDUNDANT_GRAPH_OK" in summary
    assert "correlated" in summary



def test_m5_saturation_policy_rejects_epoch_flag_not_object_diagnostic() -> None:
    reject_flags = m5._m5_reject_flags(
        require_detection_support=True,
        extra_reject_flags=None,
    )
    assert "SATURATED_LIKE" not in reject_flags
    assert "SATURATED_OR_NEAR_SATURATED" in reject_flags

    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 29786,
        "frame_id": 100,
        "series_id": 82,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 998,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 58372.7,
        "jd": 2458373.2,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "KAO",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 10.0,
        "mag_cal": 10.48,
        "mag_err": 0.01,
        "mag_err_random": 0.01,
        "mag_err_systematic": 0.0,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "29786"},
        "frame_quality_flags": {},
        "object_qc_flags": {"SATURATED_LIKE": True},
        "interseries_offset_mag": -0.0034,
        "interseries_offset_err": 0.0004,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 6,
        "interseries_generation_uid": "m4_v_scale",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 2,
        "interseries_reference_series_id": 76,
    }
    accepted = m5._to_m5_row(base, reject_flags=reject_flags, field_id=33)
    assert accepted["is_rejected"] is False
    assert "SATURATED_LIKE" in accepted["quality_flags"]

    saturated_epoch = dict(base)
    saturated_epoch["science_measurement_id"] = 2
    saturated_epoch["measurement_id"] = 2
    saturated_epoch["frame_id"] = 101
    saturated_epoch["measurement_flags"] = {
        "catalog_name": "shared",
        "source_id": "29786",
        "SATURATED_OR_NEAR_SATURATED": True,
    }
    rejected = m5._to_m5_row(saturated_epoch, reject_flags=reject_flags, field_id=33)
    assert rejected["is_rejected"] is True
    assert rejected["reject_reasons"] == "SATURATED_OR_NEAR_SATURATED"


def test_m5_explicit_extra_flag_can_restore_saturated_like_veto() -> None:
    reject_flags = m5._m5_reject_flags(
        require_detection_support=True,
        extra_reject_flags="SATURATED_LIKE",
    )
    assert "SATURATED_LIKE" in reject_flags
    assert "SATURATED_OR_NEAR_SATURATED" in reject_flags

def test_export_m5_all_rejected_reports_reasons_and_debug(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=2,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="SPARSE_GRAPH_NO_CLOSURE",
        graph_dof=0,
        series_sources=((76, 960),),
    )
    coverage = [m5.SeriesCoverage(76, "V", 960, 960, 2, "READY_EXACT_M4_SOURCE")]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "V", "series_id": 76, "n_points": 2, "mjd_min": 60000.0, "mjd_max": 60000.1}
        ],
    )
    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 29700,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 7.5,
        "mag_cal": 7.7,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {
            "HIGH_RMS": True,
            "SATURATED_LIKE": True,
            "NO_DETECTION_SUPPORT": True,
        },
        "interseries_offset_mag": 0.004,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 2,
        "interseries_generation_uid": "m4_v",
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "interseries_graph_dof": 0,
        "interseries_reference_series_id": 76,
    }
    second = dict(base, science_measurement_id=2, measurement_id=2, frame_id=101, mjd=60000.1)
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([base, second]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 29700}])
            raise AssertionError(str(statement))

    root = tmp_path / "object29700"
    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=29700,
        field_id=33,
        out_root=root,
        include_rejected_debug=True,
        plots=True,
    )
    assert result.status == m5.M5_STATUS_NO_ACCEPTED
    assert result.rows_source_exact_m4 == 2
    assert result.rows_written == 0
    assert result.accepted_rows == 0
    assert result.rejected_rows == 2
    assert result.rejection_reasons == {
        "HIGH_RMS": 2,
        "NO_DETECTION_SUPPORT": 2,
    }
    assert "SATURATED_LIKE" not in result.rejection_reasons_by_series["76"]
    assert result.plot_paths == {}
    debug_path = Path(result.rejection_debug_path)
    assert debug_path.exists()
    lines = debug_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "SATURATED_LIKE" in lines[1]  # preserved as diagnostic evidence in quality_flags
    summary = Path(result.summary_path).read_text(encoding="utf-8")
    assert "NO_ACCEPTED_POINTS_AFTER_QC" in summary
    assert "SATURATED_LIKE" not in result.rejection_reasons


def test_m5_rejection_debug_columns_include_qc_evidence() -> None:
    columns = m5.rejection_debug_columns()
    assert "quality_flags" in columns
    assert "reject_reasons" in columns
    assert "science_calibration_version_id" in columns
    assert "interseries_generation_id" in columns


def test_export_m5_marks_durable_science_read_source(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((76, 960),),
    )
    coverage = [m5.SeriesCoverage(76, "V", 960, 960, 1, "READY_EXACT_M4_SOURCE")]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "V", "series_id": 76, "n_points": 1, "mjd_min": 60000.0, "mjd_max": 60000.0}
        ],
    )
    raw = {
        "science_measurement_id": 123,
        "measurement_id": 123,
        "object_id": 42,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.3,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.0,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 1,
        "interseries_generation_uid": "m4_v",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 1,
        "interseries_reference_series_id": 76,
    }
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([raw]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 42}])
            raise AssertionError(str(statement))

    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=42,
        field_id=33,
        out_root=tmp_path / "cache_object42",
        plots=False,
    )
    assert result.status == m5.M5_STATUS_READY
    assert result.science_read_source == "SCIENCE_PHOTOMETRY"
    assert result.accepted_rows == 1


def test_m5_science_iterator_rebuilds_exact_generation_row(monkeypatch) -> None:
    from types import SimpleNamespace

    generation = m5.M5Generation(
        interseries_generation_id=20,
        generation_uid="m4_v20",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=88,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=27,
        series_sources=((92, 1107),),
    )
    measurement = SimpleNamespace(
        measurement_id=501,
        base_measurement_id=501,
        object_id=29786,
        frame_id=7134,
        x=10.0,
        y=20.0,
        flux=1000.0,
        flux_err=10.0,
        mag_inst=12.0,
        mag_cal=12.34,
        mag_err=0.02,
        mag_err_random=0.02,
        mag_err_systematic=0.01,
        aperture_radius_pix=4.0,
        local_background=3.0,
        local_zp_correction=0.2,
        centroid_dx_pix=0.01,
        centroid_dy_pix=-0.01,
        flags={"catalog_name": "shared", "source_id": "42", "saturated_or_near_saturated": True},
    )
    monkeypatch.setattr(
        m5,
        "load_calibration_photometry_view",
        lambda *args, **kwargs: SimpleNamespace(
            source="SCIENCE_PHOTOMETRY",
            rows=[measurement],
        ),
    )

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM interseries_calibration_series ics" in sql:
                return _MappingsResult([{
                    "interseries_generation_id": 20,
                    "series_id": 92,
                    "science_calibration_version_id": 1107,
                    "offset_mag": 0.003,
                    "offset_err_mag": 0.004,
                    "analysis_algorithm": "interseries_graph_v1",
                }])
            if "FROM frames f" in sql:
                return _MappingsResult([{
                    "frame_id": 7134,
                    "date_obs": None,
                    "mjd": 60000.0,
                    "jd": 2460000.5,
                    "instrument_unit_id": 7,
                    "instrument": "MASTER",
                    "site": "X",
                    "tube": "EAST",
                    "camera": "204",
                    "filter_name": "V",
                    "quality_flags": {},
                    "series_id": 92,
                    "field_id": 33,
                    "field_name": "GPX_TF17_2",
                }])
            if "FROM object_qc" in sql:
                return _MappingsResult([{
                    "object_id": 29786,
                    "calibration_version_id": 1107,
                    "catalog_name": "shared",
                    "instrument_unit_id": 7,
                    "flags": {"HIGH_RMS": False},
                }])
            if "FROM calibration_versions" in sql:
                return _MappingsResult([{
                    "calibration_version_id": 1107,
                    "name": "science_v",
                    "model_name": "ensemble",
                }])
            raise AssertionError(sql)

    rows = list(m5._iter_exact_m4_rows_from_science(
        FakeSession(), object_id=29786, field_id=33, generations=[generation]
    ))
    assert len(rows) == 1
    row = rows[0]
    assert row["science_measurement_id"] == 501
    assert row["measurement_id"] == 501
    assert row["calibration_version_id"] == 1107
    assert row["interseries_generation_id"] == 20
    assert row["interseries_offset_mag"] == pytest.approx(0.003)
    assert row["interseries_offset_err"] == pytest.approx(0.004)
    assert row["interseries_algorithm"] == "interseries_graph_v1"
    assert row["measurement_flags"]["source_id"] == "42"
