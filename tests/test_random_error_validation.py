import pytest

from grandgousier.quality import random_error_validation
from grandgousier.quality.random_error_validation import summarize_random_error_rows


def test_physical_error_validation_compares_normalized_reference_residuals():
    rows = []
    normalized_pattern = [-2.0, -1.0, 0.0, 1.0, 2.0]
    for object_id, center in ((1, 12.0), (2, 14.0), (3, 16.0)):
        for normalized in normalized_pattern:
            physical_err = 0.02
            rows.append(
                {
                    "object_id": object_id,
                    "mag": center + normalized * physical_err,
                    "physical_err": physical_err,
                    "legacy_err": physical_err / 2.0,
                }
            )

    result = summarize_random_error_rows(rows, min_measurements_per_object=5)

    assert result["n_reference_objects"] == 3
    assert result["n_measurements"] == 15
    assert result["degrees_of_freedom"] == 12
    assert result["physical_normalized_robust_sigma"] == pytest.approx(1.4826)
    assert result["legacy_normalized_robust_sigma"] == pytest.approx(2.9652)
    assert result["legacy_normalized_robust_sigma"] == pytest.approx(
        2.0 * result["physical_normalized_robust_sigma"]
    )
    assert result["physical_closer_to_unity"] is True
    assert len(result["object_rows"]) == 3
    assert result["magnitude_bins"]


def test_validation_ignores_incomplete_objects():
    rows = [
        {"object_id": 1, "mag": 12.0, "physical_err": 0.01, "legacy_err": 0.01},
        {"object_id": 1, "mag": 12.1, "physical_err": 0.01, "legacy_err": 0.01},
        {"object_id": 2, "mag": 13.0, "physical_err": None, "legacy_err": 0.01},
    ]

    result = summarize_random_error_rows(rows, min_measurements_per_object=3)

    assert result["n_reference_objects"] == 0
    assert result["n_measurements"] == 0
    assert result["physical_normalized_robust_sigma"] is None
    assert result["legacy_normalized_robust_sigma"] is None


def test_residual_floor_lower_envelope_and_frame_common_diagnostics():
    import numpy as np

    rng = np.random.default_rng(12345)
    rows = []
    for frame_index in range(40):
        frame_common = 0.010 * np.sin(2.0 * np.pi * frame_index / 13.0)
        for object_index in range(12):
            center = 12.2 + (object_index // 4) * 2.0 + (object_index % 4) * 0.08
            independent_sigma = 0.018 if object_index % 4 < 3 else 0.050
            rows.append(
                {
                    "object_id": object_index + 1,
                    "frame_id": frame_index + 1,
                    "mag": center + frame_common + rng.normal(0.0, independent_sigma),
                    "physical_err": 0.010,
                    "legacy_err": 0.008,
                    "total_err": 0.012,
                    "x": 100.0 + object_index * 100.0,
                    "y": 200.0 + object_index * 50.0,
                    "mjd": 60000.0 + frame_index / 1440.0,
                    "airmass": 1.0 + frame_index / 100.0,
                    "background_median": 1000.0 + frame_index,
                    "background_mad_sigma": 20.0,
                    "astrom_rms_arcsec": 0.5,
                    "psf_fwhm_pix": 4.0 + frame_index / 100.0,
                    "psf_ellipticity": 0.1,
                }
            )

    result = summarize_random_error_rows(
        rows,
        min_measurements_per_object=20,
        residual_floor_bin_width=1.0,
        residual_floor_lower_quantile=0.25,
        residual_floor_min_objects_per_bin=3,
    )

    assert result["residual_floor_status"] == "OK"
    assert result["n_reference_objects"] == 12
    assert result["residual_floor_suggested_mag"] is not None
    assert 0.005 < result["residual_floor_suggested_mag"] < 0.040
    assert result["residual_floor_after_frame_common_mag"] is not None
    assert result["residual_floor_frame_common_supported"] is False
    assert result["residual_floor_frame_common_support_status"] == "NOT_SUPPORTED"
    assert result["residual_floor_independent_mag"] is None
    assert result["residual_floor_frame_common_sigma_mag"] is not None
    assert result["residual_floor_calibrator_objects"] >= 3
    assert result["residual_floor_frames_with_common_residual"] == 40
    assert result["residual_floor_effective_normalized_robust_sigma"] is not None
    assert result["residual_floor_effective_reduced_chi2"] is not None
    assert result["residual_floor_effective_total_normalized_robust_sigma"] < result[
        "residual_floor_effective_detector_only_normalized_robust_sigma"
    ]
    assert result["residual_floor_pairwise_pairs"] > 0
    assert result["residual_floor_bins"]
    assert result["residual_floor_after_common_bins"]
    assert result["residual_floor_frame_rows"]
    assert result["residual_floor_associations"]
    assert all("mag_err_detector_median" in row for row in result["object_rows"])
    assert all("mag_err_epoch_floor_suggested" in row for row in result["object_rows"])
    assert all("mag_err_effective_suggested_median" in row for row in result["object_rows"])
    assert all(
        row["mag_err_effective_suggested_median"]
        == row["mag_err_effective_total_suggested_median"]
        for row in result["object_rows"]
    )


def test_frame_common_is_supported_only_when_floor_and_correlation_improve():
    import numpy as np

    rng = np.random.default_rng(1)
    rows = []
    for frame_index in range(60):
        frame_common = 0.030 * np.sin(2.0 * np.pi * frame_index / 17.0)
        for object_index in range(12):
            center = 12.0 + (object_index // 4) * 2.0 + (object_index % 4) * 0.08
            rows.append(
                {
                    "object_id": object_index + 1,
                    "frame_id": frame_index + 1,
                    "mag": center + frame_common + rng.normal(0.0, 0.004),
                    "physical_err": 0.002,
                    "legacy_err": 0.002,
                    "total_err": 0.003,
                }
            )

    result = summarize_random_error_rows(
        rows,
        min_measurements_per_object=20,
        residual_floor_bin_width=1.0,
        residual_floor_lower_quantile=0.25,
        residual_floor_min_objects_per_bin=3,
    )

    assert result["residual_floor_frame_common_supported"] is True
    assert result["residual_floor_frame_common_support_status"] == "SUPPORTED"
    assert result["residual_floor_independent_mag"] == pytest.approx(
        result["residual_floor_after_frame_common_mag"]
    )
    assert result["residual_floor_frame_common_floor_reduction_fraction"] >= 0.05
    assert result["residual_floor_frame_common_abs_correlation_reduction"] >= 0.01


def _validation_snapshot():
    return type(
        "Snapshot",
        (),
        {
            "calibration_version_id": 23,
            "frame_ids": None,
            "measurement_rows": (),
            "frames_by_id": {},
            "algorithm": "fake-validation-snapshot",
            "timings_sec": {},
        },
    )()


class _ValidationSession:
    def __init__(self):
        self.read_calls = 0

    def scalar(self, _statement):
        self.read_calls += 1
        return type("Calibration", (), {"calibration_version_id": 23})()


def test_suggested_residual_floor_can_skip_provenance_annotation(monkeypatch):
    annotation_calls = []
    monkeypatch.setattr(
        random_error_validation,
        "summarize_random_error_rows",
        lambda *_args, **_kwargs: {
            "n_reference_objects": 4,
            "residual_floor_suggested_mag": 0.021,
        },
    )
    monkeypatch.setattr(
        random_error_validation,
        "annotate_residual_floor_provenance",
        lambda *_args, **_kwargs: annotation_calls.append(True),
    )
    session = _ValidationSession()

    result = random_error_validation.validate_random_error_model(
        session,
        calibration_name="diagnostic-calibration",
        diagnostic_calibration_names=["diagnostic-calibration"],
        analysis_snapshot=_validation_snapshot(),
        annotate_residual_floor=False,
    )

    assert session.read_calls == 1
    assert annotation_calls == []
    assert result.status == "OK"
    assert result.residual_floor_suggested_mag == pytest.approx(0.021)
    assert result.residual_floor_annotation_calibrations == 0
    assert result.residual_floor_annotation_measurements == 0


def test_residual_floor_annotation_remains_enabled_by_default(monkeypatch):
    annotation_calls = []
    monkeypatch.setattr(
        random_error_validation,
        "summarize_random_error_rows",
        lambda *_args, **_kwargs: {
            "n_reference_objects": 4,
            "residual_floor_suggested_mag": 0.018,
        },
    )

    def annotate(*_args, **_kwargs):
        annotation_calls.append(True)
        return {"calibrations": 2, "measurements": 7}

    monkeypatch.setattr(
        random_error_validation,
        "annotate_residual_floor_provenance",
        annotate,
    )

    result = random_error_validation.validate_random_error_model(
        _ValidationSession(),
        calibration_name="diagnostic-calibration",
        diagnostic_calibration_names=["diagnostic-calibration"],
        analysis_snapshot=_validation_snapshot(),
    )

    assert annotation_calls == [True]
    assert result.residual_floor_suggested_mag == pytest.approx(0.018)
    assert result.residual_floor_annotation_calibrations == 2
    assert result.residual_floor_annotation_measurements == 7
