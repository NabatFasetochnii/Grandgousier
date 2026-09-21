from __future__ import annotations

import csv
import math

from grandgousier.calibration.photometric_frame_qc import (
    _load_psf_context,
    classify_photometric_frame,
    derive_residual_qc_limits,
)


def _classify(**overrides):
    payload = dict(
        n_reference=30,
        residual_robust_sigma_mag=0.011,
        spatial_peak_to_peak_mag=0.0,
        min_reference_stars_per_frame=20,
        baseline_residual_robust_sigma_mag=0.011,
        suspect_residual_limit_mag=0.020,
        bad_residual_limit_mag=0.060,
        absolute_bad_residual_sigma_mag=0.100,
        spatial_suspect_peak_to_peak_mag=0.030,
        spatial_bad_peak_to_peak_mag=0.100,
    )
    payload.update(overrides)
    return classify_photometric_frame(**payload)


def test_residual_qc_limits_use_series_baseline_and_relative_floor():
    baseline, frame_mad, suspect, bad = derive_residual_qc_limits(
        [0.010, 0.011, 0.012, 0.0105, 0.0115]
    )
    assert math.isclose(baseline, 0.011, abs_tol=1e-12)
    assert math.isclose(frame_mad, 0.0005, abs_tol=1e-12)
    assert math.isclose(suspect, 0.0165, abs_tol=1e-12)
    assert math.isclose(bad, 0.0275, abs_tol=1e-12)


def test_suspect_frame_keeps_excess_scatter_as_diagnostic_only():
    status, reasons, sigma = _classify(
        residual_robust_sigma_mag=0.030,
        spatial_peak_to_peak_mag=0.040,
    )
    assert status == "SUSPECT"
    assert reasons == ["HIGH_RESIDUAL_SCATTER", "SPATIAL_SYSTEMATIC"]
    assert math.isclose(sigma, math.sqrt(0.030**2 - 0.011**2), rel_tol=0, abs_tol=1e-12)


def test_spatial_warning_alone_does_not_invent_epoch_uncertainty():
    status, reasons, sigma = _classify(spatial_peak_to_peak_mag=0.040)
    assert status == "SUSPECT"
    assert reasons == ["SPATIAL_SYSTEMATIC"]
    assert sigma == 0.0


def test_relative_bad_threshold_is_suspect_only():
    status, reasons, sigma = _classify(
        residual_robust_sigma_mag=0.080,
        bad_residual_limit_mag=0.060,
    )
    assert status == "SUSPECT"
    assert "VERY_HIGH_RELATIVE_RESIDUAL_SCATTER" in reasons
    assert "ABSOLUTE_HIGH_RESIDUAL_SCATTER" not in reasons
    assert sigma > 0.0


def test_severe_spatial_structure_is_suspect_only():
    status, reasons, sigma = _classify(
        residual_robust_sigma_mag=0.030,
        spatial_peak_to_peak_mag=0.140,
    )
    assert status == "SUSPECT"
    assert "SPATIAL_SYSTEMATIC" in reasons
    assert "SEVERE_SPATIAL_SYSTEMATIC" in reasons
    assert sigma > 0.0


def test_absolute_catastrophic_residual_is_bad():
    status, reasons, sigma = _classify(residual_robust_sigma_mag=0.120)
    assert status == "BAD"
    assert "ABSOLUTE_HIGH_RESIDUAL_SCATTER" in reasons
    assert sigma > 0.0


def test_low_reference_count_only_hard_rejects_below_five():
    suspect = _classify(n_reference=8)
    bad = _classify(n_reference=4)
    assert suspect[0] == "SUSPECT"
    assert "LOW_REFERENCE_COUNT" in suspect[1]
    assert bad[0] == "BAD"
    assert "LOW_REFERENCE_COUNT_HARD" in bad[1]


def test_missing_residual_scatter_is_hard_bad():
    status, reasons, _ = _classify(residual_robust_sigma_mag=None)
    assert status == "BAD"
    assert "NO_RESIDUAL_SCATTER" in reasons


def test_dedicated_psf_csv_is_merged_without_rereading_fits(tmp_path):
    aperture = tmp_path / "aperture.csv"
    with aperture.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["frame_id", "psf_fwhm_pix_median"])
        writer.writeheader()
        writer.writerow({"frame_id": 10, "psf_fwhm_pix_median": 3.1})

    psf = tmp_path / "psf_frames.csv"
    with psf.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "frame_id",
                "n_sources_used",
                "fwhm_major_pix_median",
                "ellipticity_median",
                "fwhm_spatial_peak_to_peak_pix",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "frame_id": 10,
                "n_sources_used": 123,
                "fwhm_major_pix_median": 3.5,
                "ellipticity_median": 0.12,
                "fwhm_spatial_peak_to_peak_pix": 0.8,
            }
        )

    merged = _load_psf_context(
        aperture_context_frames_csv=aperture,
        psf_frames_csv=psf,
    )
    assert merged[10]["psf_fwhm_pix_median"] == "3.1"
    assert merged[10]["psf_shape_sources"] == "123"
    assert merged[10]["psf_fwhm_major_pix_median"] == "3.5"
    assert merged[10]["psf_ellipticity_median"] == "0.12"
    assert merged[10]["psf_fwhm_spatial_peak_to_peak_pix"] == "0.8"



def test_real_psf_field_columns_select_minimum_bic_spatial_summary(tmp_path):
    psf = tmp_path / "psf_frames.csv"
    with psf.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "frame_id",
                "n_psf_sources",
                "fwhm_geom_median",
                "orientation_mean_deg",
                "fwhm_geom_constant_status",
                "fwhm_geom_constant_bic",
                "fwhm_geom_constant_spatial_peak_to_peak",
                "fwhm_geom_plane_status",
                "fwhm_geom_plane_bic",
                "fwhm_geom_plane_spatial_peak_to_peak",
                "fwhm_geom_quadratic_status",
                "fwhm_geom_quadratic_bic",
                "fwhm_geom_quadratic_spatial_peak_to_peak",
                "ellipticity_constant_status",
                "ellipticity_constant_bic",
                "ellipticity_constant_spatial_peak_to_peak",
                "ellipticity_plane_status",
                "ellipticity_plane_bic",
                "ellipticity_plane_spatial_peak_to_peak",
                "ellipticity_quadratic_status",
                "ellipticity_quadratic_bic",
                "ellipticity_quadratic_spatial_peak_to_peak",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "frame_id": 10,
                "n_psf_sources": 480,
                "fwhm_geom_median": 3.35,
                "orientation_mean_deg": 12.5,
                "fwhm_geom_constant_status": "OK",
                "fwhm_geom_constant_bic": -1600,
                "fwhm_geom_constant_spatial_peak_to_peak": 0.0,
                "fwhm_geom_plane_status": "OK",
                "fwhm_geom_plane_bic": -1770,
                "fwhm_geom_plane_spatial_peak_to_peak": 0.35,
                "fwhm_geom_quadratic_status": "OK",
                "fwhm_geom_quadratic_bic": -2190,
                "fwhm_geom_quadratic_spatial_peak_to_peak": 0.78,
                "ellipticity_constant_status": "OK",
                "ellipticity_constant_bic": -3330,
                "ellipticity_constant_spatial_peak_to_peak": 0.0,
                "ellipticity_plane_status": "OK",
                "ellipticity_plane_bic": -3390,
                "ellipticity_plane_spatial_peak_to_peak": 0.046,
                "ellipticity_quadratic_status": "OK",
                "ellipticity_quadratic_bic": -3520,
                "ellipticity_quadratic_spatial_peak_to_peak": 0.085,
            }
        )

    merged = _load_psf_context(
        aperture_context_frames_csv=None,
        psf_frames_csv=psf,
    )
    row = merged[10]
    assert row["psf_shape_sources"] == "480"
    assert row["psf_fwhm_pix_median"] == "3.35"
    assert row["psf_position_angle_deg"] == "12.5"
    assert row["psf_fwhm_spatial_model"] == "quadratic"
    assert row["psf_fwhm_spatial_peak_to_peak_pix"] == 0.78
    assert row["psf_ellipticity_spatial_model"] == "quadratic"
    assert row["psf_ellipticity_spatial_peak_to_peak"] == 0.085
