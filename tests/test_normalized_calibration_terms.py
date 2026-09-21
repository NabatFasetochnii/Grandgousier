from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from grandgousier.calibration.aperture_correction import ApertureCorrectionResult
from grandgousier.calibration.aperture_zp import (
    ApertureZpCalibrationResult,
    _fuse_compact_aperture_correction_rows,
)
from grandgousier.calibration.color_calibration import (
    ColorCalibrationApplyResult,
    ColorCalibrationDiagnosticsResult,
)
from grandgousier.calibration.ensemble import (
    EnsembleCalibrationResult,
    _fuse_compact_aperture_zp_source_rows,
    ensemble_calibrate_measurements,
)
from grandgousier.calibration.normalized_terms import _equivalent, _json_safe


def test_calibration_results_expose_dual_write_provenance() -> None:
    aperture = ApertureCorrectionResult()
    zp = ApertureZpCalibrationResult()
    ensemble = EnsembleCalibrationResult()
    color = ColorCalibrationApplyResult(
        source_calibration_name="src",
        target_calibration_name="dst",
        series_id=1,
        identity_catalog_name="gaia",
        reference_catalog_name="ps1",
        reference_band="r",
        color_index="g-r",
    )
    for result in (aperture, zp, ensemble, color):
        assert result.normalized_calibration_iteration_id is None
        assert result.normalized_frame_terms == 0
        assert result.normalized_object_terms == 0
        assert result.normalized_write_status == "NOT_WRITTEN"


def test_ensemble_significant_pass_number_is_explicit_and_validated() -> None:
    assert EnsembleCalibrationResult().calibration_iteration_no == 1
    with pytest.raises(ValueError, match="calibration_iteration_no"):
        ensemble_calibrate_measurements(  # type: ignore[arg-type]
            None,
            source_calibration_name="src",
            target_calibration_name="dst",
            calibration_iteration_no=0,
        )


def test_color_diagnostics_keep_compact_calibrator_membership_ids() -> None:
    diag = ColorCalibrationDiagnosticsResult(
        calibration_name="c",
        series_id=1,
        identity_catalog_name="gaia",
        reference_catalog_name="ps1",
        reference_band="r",
        color_index="g-r",
    )
    assert diag.ensemble_transform_candidate_object_ids == []
    assert diag.ensemble_transform_used_object_ids == []
    assert diag.ensemble_transform_clipped_object_ids == []


def test_normalized_json_sanitizes_nonfinite_values() -> None:
    payload = _json_safe({"nan": math.nan, "inf": math.inf, "nested": [1.0, -math.inf]})
    assert payload == {"nan": None, "inf": None, "nested": [1.0, None]}


def test_parity_equivalence_uses_numeric_tolerance_and_boolean_semantics() -> None:
    assert _equivalent(1.0, 1.0 + 1e-12, 1e-10)
    assert not _equivalent(1.0, 1.1, 1e-10)
    assert _equivalent(True, 1, 1e-10)
    assert not _equivalent(False, 1, 1e-10)


def test_field_pipeline_defaults_to_compact_intermediate_materialization() -> None:
    pytest.importorskip("astropy")
    from grandgousier.pipeline.runner import FieldPipelineConfig

    config = FieldPipelineConfig(catalog_name="cat", calibration_prefix="p")
    assert config.materialize_intermediate_calibrations is False



def test_compact_aperture_zp_source_fusion_matches_aperture_correction_semantics() -> None:
    aperture_term = SimpleNamespace(
        frame_id=10,
        accepted=True,
        model_name="constant",
        coefficient_0=0.125,
        coefficient_x=0.0,
        coefficient_y=0.0,
        details={"image_width": 100, "image_height": 100},
    )
    rows = _fuse_compact_aperture_correction_rows(
        [
            {
                "object_id": 1,
                "frame_id": 10,
                "x": 50.0,
                "y": 50.0,
                "mag_inst": -10.0,
                "mag_err": 0.03,
                "mag_err_random": 0.02,
                "catalog_name": "cat",
                "saturated_or_near_saturated": False,
            },
            {
                "object_id": 2,
                "frame_id": 11,
                "x": 10.0,
                "y": 10.0,
                "mag_inst": -9.0,
                "mag_err": 0.04,
                "mag_err_random": None,
                "catalog_name": "other",
                "saturated_or_near_saturated": True,
            },
        ],
        aperture_terms={10: aperture_term},
    )

    assert len(rows) == 2
    assert rows[0].object_id == 1
    assert rows[0].frame_id == 10
    assert rows[0].mag_inst == pytest.approx(-9.875)
    assert rows[0].mag_err == pytest.approx(0.02)
    assert rows[0].flags == {
        "catalog_name": "cat",
        "saturated_or_near_saturated": False,
    }
    assert rows[1].mag_inst == pytest.approx(-9.0)
    assert rows[1].mag_err == pytest.approx(0.04)
    assert rows[1].flags["saturated_or_near_saturated"] is True

def test_compact_ensemble_source_fusion_matches_aperture_zp_scalar_semantics() -> None:
    aperture_term = SimpleNamespace(
        frame_id=10,
        accepted=True,
        model_name="constant",
        coefficient_0=0.125,
        coefficient_x=0.0,
        coefficient_y=0.0,
        details={"image_width": 100, "image_height": 100},
    )
    zp_term = SimpleNamespace(frame_id=10, accepted=True, offset_mag=25.0)
    result = EnsembleCalibrationResult()

    rows = _fuse_compact_aperture_zp_source_rows(
        [
            {
                "object_id": 1,
                "frame_id": 10,
                "x": 50.0,
                "y": 50.0,
                "mag_inst": -10.0,
                "catalog_name": "cat",
            },
            {
                "object_id": 2,
                "frame_id": 10,
                "x": 50.0,
                "y": 50.0,
                "mag_inst": -9.0,
                "catalog_name": "other",
            },
        ],
        aperture_terms={10: aperture_term},
        aperture_zp_terms={10: zp_term},
        aperture_zp_catalog_name="cat",
        requested_catalog_name="cat",
        prefer_mag_cal=True,
        result=result,
    )

    assert len(rows) == 1
    assert rows[0].obj.object_id == 1
    assert rows[0].frame.frame_id == 10
    assert rows[0].measurement is None
    assert rows[0].value == pytest.approx(15.125)
    # The row rejected by APERTURE_ZP is not an ensemble-stage catalog skip.
    assert result.skipped_other_catalog == 0

    result_inst = EnsembleCalibrationResult()
    rows_inst = _fuse_compact_aperture_zp_source_rows(
        [
            {
                "object_id": 1,
                "frame_id": 10,
                "x": 50.0,
                "y": 50.0,
                "mag_inst": -10.0,
                "catalog_name": "cat",
            }
        ],
        aperture_terms={10: aperture_term},
        aperture_zp_terms={10: zp_term},
        aperture_zp_catalog_name="cat",
        requested_catalog_name="cat",
        prefer_mag_cal=False,
        result=result_inst,
    )
    assert rows_inst[0].value == pytest.approx(-9.875)
