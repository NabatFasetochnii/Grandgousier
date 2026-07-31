from grandgousier.db.models import ObjectQc
from grandgousier.quality.field_report import _object_qc_summary


class FakeScalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, rows_by_call):
        self.rows_by_call = list(rows_by_call)

    def scalars(self, _stmt):
        return FakeScalars(self.rows_by_call.pop(0))


def make_qc(object_id, cv_id, flags=None, details=None):
    return ObjectQc(
        object_id=object_id,
        calibration_version_id=cv_id,
        catalog_name="gaia_dr3_master",
        instrument_unit_id=1,
        algorithm="object_qc_v1",
        flags=flags or {},
        details=details or {},
    )


def test_field_report_counts_source_aperture_flags_when_target_lacks_layer():
    target = make_qc(10, 20, flags={"HIGH_RMS": True}, details={"detection_support": {}})
    source = make_qc(
        10,
        7,
        flags={"APERTURE_UNSTABLE": True, "APERTURE_MEDIAN_SHIFT": True},
        details={"aperture_stability": {"min_corr_with_ref": 0.2}},
    )
    session = FakeSession([[target], [source]])

    summary = _object_qc_summary(
        session,
        calibration_version_id=20,
        source_calibration_version_id=7,
        catalog_name="gaia_dr3_master",
        instrument_unit_id=1,
        algorithm="object_qc_v1",
    )

    assert summary["flag_counts"]["HIGH_RMS"] == 1
    assert summary["flag_counts"]["APERTURE_UNSTABLE"] == 1
    assert summary["flag_counts"]["APERTURE_MEDIAN_SHIFT"] == 1
    assert summary["n_source_aperture_stability_qc"] == 1
    assert summary["n_inherited_aperture_stability_qc"] == 1
