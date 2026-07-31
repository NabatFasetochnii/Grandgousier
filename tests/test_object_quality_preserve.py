from grandgousier.db.models import CalibrationVersion, ObjectQc
from grandgousier.quality.object_quality import (
    _infer_source_aperture_calibration_name,
    _merge_preserved_qc_details,
    _preserve_aperture_stability_flags,
    _select_aperture_stability_qc,
)


def make_qc(*, calibration_version_id=1, flags=None, details=None):
    return ObjectQc(
        object_id=10,
        calibration_version_id=calibration_version_id,
        catalog_name="gaia_dr3_master",
        instrument_unit_id=1,
        algorithm="object_qc_v1",
        flags=flags or {},
        details=details or {},
    )


def test_preserve_aperture_stability_flags_from_existing_qc():
    qc = make_qc(
        flags={"APERTURE_UNSTABLE": True, "APERTURE_MEDIAN_SHIFT": True, "POSSIBLE_BLEND": True},
        details={"aperture_stability": {"best_aperture_radius_pix": 3.0}},
    )
    flags = {"HIGH_RMS": True}

    assert _preserve_aperture_stability_flags(qc, flags) is True
    assert flags["HIGH_RMS"] is True
    assert flags["APERTURE_UNSTABLE"] is True
    assert flags["APERTURE_MEDIAN_SHIFT"] is True
    assert flags["POSSIBLE_BLEND"] is True


def test_preserve_aperture_stability_ignores_plain_object_qc_without_layer():
    qc = make_qc(flags={"APERTURE_UNSTABLE": True}, details={"detection_support": {}})
    flags = {}

    assert _preserve_aperture_stability_flags(qc, flags) is False
    assert flags == {}


def test_select_aperture_stability_prefers_target_over_source():
    target = make_qc(
        calibration_version_id=20,
        details={"aperture_stability": {"origin": "target"}},
    )
    source = make_qc(
        calibration_version_id=10,
        details={"aperture_stability": {"origin": "source"}},
    )

    assert _select_aperture_stability_qc(target, source) is target
    assert _select_aperture_stability_qc(None, source) is source


def test_merge_preserved_qc_details_records_source_provenance():
    source = make_qc(
        calibration_version_id=10,
        flags={"APERTURE_UNSTABLE": True},
        details={"aperture_stability": {"rms_ratio": 1.8}},
    )

    merged = _merge_preserved_qc_details(
        {"detection_support": {"n_detection_match_frames": 18}},
        existing_qc=None,
        source_aperture_qc=source,
        source_aperture_calibration_name="ensemble_aperture_gaia_g_v1",
    )

    assert merged["detection_support"]["n_detection_match_frames"] == 18
    assert merged["aperture_stability"] == {"rms_ratio": 1.8}
    assert merged["aperture_stability_provenance"] == {
        "preserved_from_calibration_version_id": 10,
        "preserved_from_source_calibration": True,
        "source_calibration_name": "ensemble_aperture_gaia_g_v1",
    }


def test_infer_source_aperture_calibration_name_from_derived_calibration():
    cv = CalibrationVersion(
        calibration_version_id=30,
        name="ps1_i_color_v1",
        model_name="reference_color_frame_zp_v1",
        instrument_profile={},
        used_catalogs={},
        parameters={"source_calibration_name": "ensemble_aperture_gaia_g_v1"},
    )

    assert _infer_source_aperture_calibration_name(cv, "ps1_i_color_v1") == "ensemble_aperture_gaia_g_v1"


def test_infer_source_aperture_calibration_name_avoids_self_reference():
    cv = CalibrationVersion(
        calibration_version_id=30,
        name="same",
        model_name="reference_color_frame_zp_v1",
        instrument_profile={},
        used_catalogs={},
        parameters={"source_calibration_name": "same"},
    )

    assert _infer_source_aperture_calibration_name(cv, "same") is None
