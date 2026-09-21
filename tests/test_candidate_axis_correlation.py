from types import SimpleNamespace

import pytest

from grandgousier.quality import candidate_axis_correlation as cac


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeExecute:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _FakeScalars(self._values)


class _FakeSession:
    def __init__(self, frames):
        self.frames = frames

    def execute(self, _stmt):
        return _FakeExecute(self.frames)


def _row(object_id, source_id):
    return {
        "object_id": object_id,
        "source_id": source_id,
        "candidate_status": "ACCEPTED",
        "good_frame_confirmation_status": "GOOD_FRAME_CONFIRMED",
    }


def _frames(n, *, width=4096, height=4096):
    return [
        SimpleNamespace(frame_id=i, header_json={"NAXIS1": width, "NAXIS2": height})
        for i in range(1, n + 1)
    ]


def test_axis_bin_correlation_flags_same_x_bin_without_changing_acceptance(monkeypatch):
    frames = _frames(10)
    measurements = []
    # Objects 1 and 2 share an X neighbourhood only through the shifted bins.
    # Their changes are linearly and rank correlated, so the pair mode is BOTH.
    seq = [0.0, 0.2, -0.1, 0.4, -0.2, 0.3, 0.1, -0.3, 0.5, -0.4]
    for frame_id, delta in enumerate(seq, start=1):
        measurements.append(SimpleNamespace(object_id=1, frame_id=frame_id, mag_cal=12.0 + delta, x=1250.0, y=500.0))
        measurements.append(SimpleNamespace(object_id=2, frame_id=frame_id, mag_cal=13.0 + 2.0 * delta, x=1290.0, y=1600.0))
        measurements.append(SimpleNamespace(object_id=3, frame_id=frame_id, mag_cal=14.0 + (0.05 if frame_id % 2 else -0.05), x=3000.0, y=3000.0))
    monkeypatch.setattr(
        cac,
        "load_calibration_photometry_view",
        lambda *args, **kwargs: SimpleNamespace(rows=measurements),
    )
    result = cac.annotate_candidate_axis_correlations(
        _FakeSession(frames),
        candidate_rows=[_row(1, "s1"), _row(2, "s2"), _row(3, "s3")],
        calibration_name="test",
        good_frame_ids=range(1, 11),
    )
    by_id = {row["object_id"]: row for row in result.candidate_rows}
    assert by_id[1]["candidate_status"] == "ACCEPTED"
    assert by_id[2]["candidate_status"] == "ACCEPTED"
    assert by_id[1]["candidate_axis_correlation_status"] == "CORRELATED_X"
    assert by_id[2]["candidate_axis_correlation_status"] == "CORRELATED_X"
    assert by_id[1]["candidate_axis_correlation_mode"] == "BOTH"
    assert by_id[1]["candidate_x_axis_correlation_mode"] == "BOTH"
    assert by_id[1]["candidate_n_x_correlated_candidates"] == 1
    assert by_id[1]["candidate_n_x_spearman_correlated_candidates"] == 1
    assert by_id[1]["candidate_n_x_pearson_correlated_candidates"] == 1
    assert by_id[1]["candidate_n_y_correlated_candidates"] == 0
    assert by_id[1]["candidate_max_x_bin_spearman_rho"] == pytest.approx(1.0)
    assert by_id[1]["candidate_max_x_bin_pearson_r"] == pytest.approx(1.0)
    assert by_id[1]["candidate_max_x_bin_spearman_object_id"] == 2
    assert by_id[1]["candidate_max_x_bin_pearson_object_id"] == 2
    assert by_id[1]["candidate_max_x_bin_correlated_object_id"] == 2
    assert by_id[1]["candidate_x_bin"] != by_id[2]["candidate_x_bin"]
    assert by_id[1]["candidate_x_bin_shifted"] == by_id[2]["candidate_x_bin_shifted"]
    pair = next(row for row in result.pair_rows if row["axis"] == "X")
    assert "HALF_SHIFTED" in pair["bin_schemes"]
    assert pair["correlation_mode"] == "BOTH"
    assert pair["spearman_correlated"] is True
    assert pair["pearson_correlated"] is True
    assert by_id[3]["candidate_axis_correlation_status"] == "NO_STRONG_AXIS_BIN_CORRELATION"
    assert result.summary["candidates_correlated_any"] == 2
    assert result.summary["candidates_correlated_spearman"] == 2
    assert result.summary["candidates_correlated_pearson"] == 2
    assert result.summary["diagnostic_only"] is True


def test_axis_correlation_pearson_detects_shared_single_epoch_excursion(monkeypatch):
    frames = _frames(13)
    # The first 12 epochs have unrelated small structure; the last GOOD epoch is a
    # common high-amplitude excursion. Pearson is intentionally sensitive to this
    # while Spearman does not pass the 0.8 threshold.
    seq_a = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.0, 0.015, -0.015, 0.025, -0.025, 0.005, -2.5]
    seq_b = [-0.03, 0.01, -0.02, 0.025, -0.01, 0.03, -0.025, 0.02, 0.0, -0.015, 0.005, -0.005, -1.3]
    measurements = []
    for frame_id, (delta_a, delta_b) in enumerate(zip(seq_a, seq_b), start=1):
        measurements.append(SimpleNamespace(object_id=1, frame_id=frame_id, mag_cal=14.8 + delta_a, x=1242.0, y=500.0))
        measurements.append(SimpleNamespace(object_id=2, frame_id=frame_id, mag_cal=13.4 + delta_b, x=1291.0, y=1700.0))
    monkeypatch.setattr(cac, "load_calibration_photometry_view", lambda *args, **kwargs: SimpleNamespace(rows=measurements))

    result = cac.annotate_candidate_axis_correlations(
        _FakeSession(frames),
        candidate_rows=[_row(1, "s1"), _row(2, "s2")],
        calibration_name="test",
        good_frame_ids=range(1, 14),
    )

    pair = next(row for row in result.pair_rows if row["axis"] == "X")
    assert abs(pair["spearman_rho"]) < 0.8
    assert pair["pearson_r"] > 0.99
    assert pair["spearman_correlated"] is False
    assert pair["pearson_correlated"] is True
    assert pair["correlation_mode"] == "PEARSON"
    by_id = {row["object_id"]: row for row in result.candidate_rows}
    assert by_id[1]["candidate_axis_correlation_mode"] == "PEARSON"
    assert by_id[1]["candidate_max_x_bin_pearson_r"] > 0.99
    assert by_id[1]["candidate_n_x_spearman_correlated_candidates"] == 0
    assert by_id[1]["candidate_n_x_pearson_correlated_candidates"] == 1
    assert result.summary["pearson_correlated_pairs"] >= 1


def test_axis_correlation_can_be_spearman_only(monkeypatch):
    frames = _frames(10, width=1000, height=1000)
    measurements = []
    for frame_id in range(1, 11):
        x = frame_id - 1
        # Strictly monotonic, but strongly nonlinear: Spearman=1 while Pearson<0.8.
        delta_a = float(x)
        delta_b = float(3 ** x)
        measurements.append(SimpleNamespace(object_id=1, frame_id=frame_id, mag_cal=10.0 + delta_a, x=100.0, y=100.0))
        measurements.append(SimpleNamespace(object_id=2, frame_id=frame_id, mag_cal=11.0 + delta_b, x=120.0, y=700.0))
    monkeypatch.setattr(cac, "load_calibration_photometry_view", lambda *args, **kwargs: SimpleNamespace(rows=measurements))

    result = cac.annotate_candidate_axis_correlations(
        _FakeSession(frames),
        candidate_rows=[_row(1, "s1"), _row(2, "s2")],
        calibration_name="test",
        good_frame_ids=range(1, 11),
    )
    pair = next(row for row in result.pair_rows if row["axis"] == "X")
    assert pair["spearman_rho"] == pytest.approx(1.0)
    assert abs(pair["pearson_r"]) < 0.8
    assert pair["correlation_mode"] == "SPEARMAN"
    by_id = {row["object_id"]: row for row in result.candidate_rows}
    assert by_id[1]["candidate_axis_correlation_mode"] == "SPEARMAN"


def test_axis_correlation_requires_minimum_common_good_epochs(monkeypatch):
    frames = _frames(7, width=1000, height=1000)
    measurements = []
    for frame_id in range(1, 8):
        measurements.append(SimpleNamespace(object_id=1, frame_id=frame_id, mag_cal=10.0 + frame_id, x=100.0, y=100.0))
        measurements.append(SimpleNamespace(object_id=2, frame_id=frame_id, mag_cal=11.0 + frame_id, x=120.0, y=700.0))
    monkeypatch.setattr(cac, "load_calibration_photometry_view", lambda *args, **kwargs: SimpleNamespace(rows=measurements))
    result = cac.annotate_candidate_axis_correlations(
        _FakeSession(frames),
        candidate_rows=[_row(1, "s1"), _row(2, "s2")],
        calibration_name="test",
        good_frame_ids=range(1, 8),
        min_common_good_epochs=8,
    )
    pair = next(row for row in result.pair_rows if row["axis"] == "X")
    assert pair["n_common_good_epochs"] == 7
    assert pair["spearman_rho"] is None
    assert pair["pearson_r"] is None
    assert pair["correlation_mode"] == "NONE"
    assert result.summary["correlated_pairs"] == 0


def test_correlation_helpers_handle_ties_negative_and_constant_sequences():
    assert cac._spearman([1, 2, 2, 4, 5], [5, 4, 4, 2, 1]) == pytest.approx(-1.0)
    assert cac._pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert cac._pearson([1, 1, 1], [1, 2, 3]) is None


def test_dual_metric_columns_reach_pair_and_candidate_exports(tmp_path):
    from grandgousier.lightcurves.candidates import candidate_summary_columns
    from grandgousier.quality.candidate_triage import candidate_triage_columns

    result = cac.CandidateAxisCorrelationResult(
        candidate_rows=[{"object_id": 1, "candidate_axis_correlation_mode": "PEARSON"}],
        pair_rows=[{
            "axis": "X",
            "bin_index": 1,
            "bin_schemes": "PRIMARY",
            "bin_contexts": "PRIMARY:1",
            "object_id_a": 1,
            "object_id_b": 2,
            "source_id_a": "s1",
            "source_id_b": "s2",
            "n_common_good_epochs": 10,
            "spearman_rho": 0.2,
            "abs_spearman_rho": 0.2,
            "pearson_r": 0.95,
            "abs_pearson_r": 0.95,
            "spearman_correlated": False,
            "pearson_correlated": True,
            "correlation_mode": "PEARSON",
            "correlated": True,
        }],
        summary={"algorithm": cac.CANDIDATE_AXIS_CORRELATION_ALGORITHM},
    )
    outputs = cac.export_candidate_axis_correlation(result, tmp_path)
    header = (tmp_path / "candidate_axis_pairs.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "pearson_r" in header
    assert "correlation_mode" in header
    assert outputs["pairs_csv"].endswith("candidate_axis_pairs.csv")
    for columns in (candidate_triage_columns(), candidate_summary_columns()):
        assert "candidate_axis_correlation_mode" in columns
        assert "candidate_max_x_bin_pearson_r" in columns
        assert "candidate_n_y_pearson_correlated_candidates" in columns
