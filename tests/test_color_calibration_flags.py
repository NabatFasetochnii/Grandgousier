import pytest

from grandgousier.calibration.color_calibration import (
    _build_reference_color_measurement_flags,
    normalize_reference_band,
    parse_color_index,
)


def test_reference_color_flags_keep_identity_and_strip_stale_provenance():
    flags = _build_reference_color_measurement_flags(
        source_flags={
            "catalog_name": "old_catalog_should_not_win",
            "source_id": "2304906332682275456",
            "mag_key": "G",
            "calibration_stage": "old_stage",
            "reference_band": "G",
            "reference_color_term": 999.0,
            "saturated": True,
            "edge_warning": "near edge",
        },
        source_measurement_id=123,
        source_calibration_version_id=7,
        source_calibration_name="ensemble_aperture_gaia_g_v1",
        identity_catalog_name="gaia_dr3_master",
        source_id="2304906332682275456",
        reference_catalog_name="ps1",
        reference_catalog_source_id="ps1:abc",
        reference_band="i",
        reference_mag=15.25,
        color_index="r_i",
        color_value=0.43,
        color0=0.50,
        color_term=0.12,
        frame_zp=24.1,
        correction=24.0916,
        color_outside_fit_range=False,
    )

    assert flags["catalog_name"] == "gaia_dr3_master"
    assert flags["source_id"] == "2304906332682275456"
    assert flags["calibration_stage"] == "reference_color_calibration"
    assert flags["calibration_method"] == "ensemble_preserving_reference_color_transform"
    assert flags["source_measurement_id"] == 123
    assert flags["source_calibration_version_id"] == 7
    assert flags["reference_catalog_name"] == "ps1"
    assert flags["reference_catalog_source_id"] == "ps1:abc"
    assert flags["reference_band"] == "i"
    assert flags["reference_mag"] == 15.25
    assert flags["reference_color_index"] == "r_i"
    assert flags["reference_color_value"] == 0.43
    assert flags["reference_color_applied_value"] == 0.43
    assert flags["reference_color_missing"] is False
    assert flags["reference_color_assumed_pivot"] is False
    assert flags["reference_color0"] == 0.50
    assert flags["reference_color_term"] == 0.12
    assert flags["color_calibration_algorithm"] == "ensemble_preserving_reference_transform_v3"
    assert flags["reference_frame_zp"] == 24.1
    assert flags["reference_color_correction"] == 24.0916
    assert flags["reference_color_outside_fit_range"] is False
    assert flags["reference_color_calibration_applied"] is True
    assert flags["reference_color_calibration_skip_reason"] is None

    assert flags["saturated"] is True
    assert flags["edge_warning"] == "near edge"
    assert "mag_key" not in flags


def test_reference_color_flags_null_nonfinite_numbers():
    flags = _build_reference_color_measurement_flags(
        source_flags={},
        source_measurement_id=None,
        source_calibration_version_id=1,
        source_calibration_name="src",
        identity_catalog_name="gaia_dr3_master",
        source_id=None,
        reference_catalog_name="ps1",
        reference_catalog_source_id=None,
        reference_band="i",
        reference_mag="not-a-number",
        color_index="r_i",
        color_value=float("nan"),
        color0=0.0,
        color_term=0.1,
        frame_zp=25.0,
        correction=25.0,
        color_outside_fit_range=True,
    )

    assert flags["source_measurement_id"] is None
    assert flags["source_id"] is None
    assert flags["reference_catalog_source_id"] is None
    assert flags["reference_mag"] is None
    assert flags["reference_color_value"] is None
    assert flags["reference_color_outside_fit_range"] is True


def test_reference_band_aliases_and_color_index_parsing():
    assert normalize_reference_band("PS1-i") == "i"
    assert normalize_reference_band("gaia g") == "g_gaia"
    assert parse_color_index("r-i") == ("r_i", ("r", "i"))
    assert parse_color_index("bp_rp") == ("bp_rp", ("bp", "rp"))
    assert parse_color_index("bprp") == ("bp_rp", None)


def test_reference_band_rejects_unknown_band():
    with pytest.raises(ValueError):
        normalize_reference_band("unknown")


def test_reference_color_flags_record_pivot_fallback_for_missing_color():
    flags = _build_reference_color_measurement_flags(
        source_flags={"catalog_name": "gaia_dr3_master", "source_id": "x"},
        source_measurement_id=10,
        source_calibration_version_id=2,
        source_calibration_name="ensemble",
        identity_catalog_name="gaia_dr3_master",
        source_id="x",
        reference_catalog_name="panstarrs_dr2",
        reference_catalog_source_id=None,
        reference_band="i",
        reference_mag=None,
        color_index="r_i",
        color_value=None,
        color0=0.55,
        color_term=0.12,
        frame_zp=24.0,
        correction=24.0,
        color_outside_fit_range=False,
        applied_color_value=0.55,
        color_missing=True,
    )

    assert flags["reference_color_value"] is None
    assert flags["reference_color_applied_value"] == 0.55
    assert flags["reference_color_missing"] is True
    assert flags["reference_color_assumed_pivot"] is True
    assert "pivot color" in flags["calibration_warning"]


def test_reference_color_flags_record_uncalibrated_row_without_losing_identity():
    flags = _build_reference_color_measurement_flags(
        source_flags={"catalog_name": "gaia_dr3_master", "source_id": "x", "non_positive_flux": True},
        source_measurement_id=11,
        source_calibration_version_id=2,
        source_calibration_name="ensemble",
        identity_catalog_name="gaia_dr3_master",
        source_id="x",
        reference_catalog_name="panstarrs_dr2",
        reference_catalog_source_id=None,
        reference_band="i",
        reference_mag=None,
        color_index="r_i",
        color_value=None,
        color0=0.55,
        color_term=0.12,
        frame_zp=24.0,
        correction=24.0,
        color_outside_fit_range=False,
        applied_color_value=0.55,
        color_missing=True,
        calibration_applied=False,
        calibration_skip_reason="NO_MAG_INST",
    )

    assert flags["catalog_name"] == "gaia_dr3_master"
    assert flags["source_id"] == "x"
    assert flags["non_positive_flux"] is True
    assert flags["reference_color_calibration_applied"] is False
    assert flags["reference_color_calibration_skip_reason"] == "NO_MAG_INST"
    assert "NO_MAG_INST" in flags["calibration_warning"]


def test_reference_color_flags_preserve_aperture_correction_provenance():
    source_aperture_flags = {
        "aperture_correction_applied": True,
        "aperture_correction_mag": -0.043,
        "aperture_correction_model": "plane",
        "aperture_correction_model_error": 0.012,
        "aperture_correction_model_error_total": 0.012,
        "aperture_correction_differential_error": 0.003,
        "aperture_correction_error_propagation": "differential_spatial_only_v1",
        "aperture_correction_fallback_reason": None,
    }
    flags = _build_reference_color_measurement_flags(
        source_flags={
            "catalog_name": "gaia_dr3_master",
            "source_id": "x",
            **source_aperture_flags,
        },
        source_measurement_id=10,
        source_calibration_version_id=2,
        source_calibration_name="ensemble_apcorr",
        identity_catalog_name="gaia_dr3_master",
        source_id="x",
        reference_catalog_name="panstarrs_dr2",
        reference_catalog_source_id="ps1:x",
        reference_band="i",
        reference_mag=14.0,
        color_index="r_i",
        color_value=0.2,
        color0=0.2,
        color_term=0.0,
        correction=-0.1,
        color_outside_fit_range=False,
        random_error=0.01,
        systematic_error=0.04,
    )

    for key, expected in source_aperture_flags.items():
        assert key in flags
        assert flags[key] == expected


def test_reference_color_flags_preserve_random_error_provenance():
    flags = _build_reference_color_measurement_flags(
        source_flags={
            "catalog_name": "identity",
            "source_id": "42",
            "random_error_model": "physical_aperture_v1",
            "random_error_physical_mag_err": 0.012,
            "random_error_legacy_mag_err": 0.010,
            "random_error_physical_to_legacy_mag_err_ratio": 1.2,
        },
        source_id="42",
        source_measurement_id=7,
        source_calibration_version_id=5,
        source_calibration_name="ensemble",
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_catalog_source_id="r42",
        reference_band="i",
        reference_mag=14.0,
        color_index="r-i",
        color_value=0.2,
        color0=0.1,
        color_term=0.0,
        fitted_color_term=0.0,
        fitted_color_term_error=None,
        transform_model="offset_only",
        correction=0.0,
        color_outside_fit_range=False,
        random_error=0.012,
        systematic_error=0.03,
    )

    assert flags["random_error_model"] == "physical_aperture_v1"
    assert flags["random_error_physical_mag_err"] == 0.012
    assert flags["random_error_legacy_mag_err"] == 0.010
    assert flags["random_error_physical_to_legacy_mag_err_ratio"] == 1.2
