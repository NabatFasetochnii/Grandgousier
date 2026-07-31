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
    assert flags["calibration_method"] == "reference_color_frame_zp"
    assert flags["source_measurement_id"] == 123
    assert flags["source_calibration_version_id"] == 7
    assert flags["reference_catalog_name"] == "ps1"
    assert flags["reference_catalog_source_id"] == "ps1:abc"
    assert flags["reference_band"] == "i"
    assert flags["reference_mag"] == 15.25
    assert flags["reference_color_index"] == "r_i"
    assert flags["reference_color_value"] == 0.43
    assert flags["reference_color0"] == 0.50
    assert flags["reference_color_term"] == 0.12
    assert flags["reference_frame_zp"] == 24.1
    assert flags["reference_color_correction"] == 24.0916
    assert flags["reference_color_outside_fit_range"] is False

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
