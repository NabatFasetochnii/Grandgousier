from grandgousier.calibration.provenance import (
    get_source_calibration_name,
    get_source_calibration_version_id,
    set_source_calibration,
)
from grandgousier.db.models import CalibrationVersion


def make_cv(cv_id, name, *, parameters=None, source_id=None):
    return CalibrationVersion(
        calibration_version_id=cv_id,
        name=name,
        model_name="test",
        source_calibration_version_id=source_id,
        instrument_profile={},
        used_catalogs={},
        parameters=parameters or {},
    )


def test_set_source_calibration_writes_column_and_legacy_json():
    source = make_cv(7, "ensemble_aperture_gaia_g_v1")
    target = make_cv(8, "ps1_i_color_v1", parameters={"algorithm": "x"})

    set_source_calibration(target, source)

    assert target.source_calibration_version_id == 7
    assert target.parameters["source_calibration_version_id"] == 7
    assert target.parameters["source_calibration_name"] == "ensemble_aperture_gaia_g_v1"
    assert target.parameters["algorithm"] == "x"


def test_source_provenance_prefers_column_over_legacy_json_id():
    cv = make_cv(
        8,
        "target",
        source_id=12,
        parameters={"source_calibration_version_id": 7, "source_calibration_name": "legacy_source"},
    )

    assert get_source_calibration_version_id(cv) == 12
    assert get_source_calibration_name(cv) == "legacy_source"


def test_source_provenance_reads_legacy_json_without_column():
    cv = make_cv(
        8,
        "target",
        parameters={"source_calibration_version_id": "7", "source_calibration_name": "source"},
    )

    assert get_source_calibration_version_id(cv) == 7
    assert get_source_calibration_name(cv) == "source"


def test_derived_calibration_modules_expose_set_source_calibration():
    from grandgousier.calibration import aperture_zp, ensemble, preliminary

    assert callable(aperture_zp.set_source_calibration)
    assert callable(ensemble.set_source_calibration)
    assert callable(preliminary.set_source_calibration)
