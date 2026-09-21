from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier.quality.lightcurve_quality import (
    CalibrationAnalysisSnapshot,
    CalibrationFrameRow,
    CalibrationMeasurementRow,
    CalibrationObjectRow,
    _build_measurement_geometry_from_snapshot_rows,
    _build_object_scatter_rows_from_snapshot_rows,
    derive_color_calibration_analysis_snapshot,
)


class _MappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return iter(self._rows)


class _Session:
    def __init__(self):
        self._scalar_calls = 0

    def scalar(self, _statement):
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            return SimpleNamespace(
                calibration_version_id=20,
                name="color-v1",
                product_kind="COLOR_CALIBRATION",
                source_calibration_version_id=10,
            )
        if self._scalar_calls == 2:
            return SimpleNamespace(
                calibration_iteration_id=30,
                metrics={
                    "transform_offset": 0.2,
                    "color_term": -0.1,
                    "color0": 0.5,
                    "fitted_color_term": -0.1,
                },
                parameters={
                    "identity_catalog_name": "gaia",
                    "reference_catalog_name": "panstarrs",
                    "reference_band": "r",
                    "color_index": "g-r",
                    "selected_transform_model": "linear",
                },
            )
        raise AssertionError("unexpected scalar call")

    def execute(self, _statement):
        return _MappingResult(
            [
                {
                    "object_id": 7,
                    "details": {
                        "applied_color_value": 0.8,
                        "color_missing": False,
                        "systematic_error": 0.03,
                        "reference_mag": 10.4,
                        "reference_color_value": 0.8,
                        "reference_catalog_source_id": 123,
                    },
                }
            ]
        )


def _source_snapshot() -> CalibrationAnalysisSnapshot:
    measurements = tuple(
        CalibrationMeasurementRow(
            object_id=7,
            frame_id=frame_id,
            analysis_mag=mag,
            mag_cal=mag,
            mag_err_random=0.02,
            mag_err=0.02,
            mag_err_systematic=None,
            flux=1000.0,
            x=10.0 + frame_id,
            y=20.0 + frame_id,
            aperture_radius_pix=3.0,
            flags={
                "catalog_name": "gaia",
                "source_id": "source-7",
                "ensemble_object_is_reference": True,
                "random_error_physical_mag_err": 0.02,
            },
        )
        for frame_id, mag in ((1, 12.0), (2, 12.1), (3, 11.9))
    )
    frames = {
        frame_id: CalibrationFrameRow(
            frame_id=frame_id,
            instrument_unit_id=4,
            mjd=60000.0 + frame_id,
            airmass=1.1,
            header_json={"NAXIS1": 100, "NAXIS2": 100},
            quality_flags={},
            instrument="MASTER",
            filter_name="V",
            pixel_scale_arcsec=1.0,
        )
        for frame_id in (1, 2, 3)
    }
    objects = {
        7: CalibrationObjectRow(
            object_id=7,
            ra_deg=10.0,
            dec_deg=20.0,
            ref_mags={"G": 12.2},
            colors={"BP_RP": 0.7},
        )
    }
    object_rows = _build_object_scatter_rows_from_snapshot_rows(
        calibration_name="ensemble-v1",
        catalog_name="gaia",
        min_measurements=3,
        mag_key="G",
        color_key="BP_RP",
        measurement_rows=measurements,
        frames_by_id=frames,
        objects_by_id=objects,
    )
    geometry = _build_measurement_geometry_from_snapshot_rows(
        catalog_name="gaia",
        measurement_rows=measurements,
        frames_by_id=frames,
        fwhm_header_unit="arcsec",
    )
    return CalibrationAnalysisSnapshot(
        calibration_version_id=10,
        calibration_name="ensemble-v1",
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=(1, 2, 3),
        min_measurements=3,
        mag_key="G",
        color_key="BP_RP",
        y_key="mag_cal",
        fwhm_header_unit="arcsec",
        measurement_rows=measurements,
        frames_by_id=frames,
        objects_by_id=objects,
        object_rows=object_rows,
        measurement_geometry=geometry,
    )


def test_color_snapshot_is_derived_from_cached_parent_without_photometry_reload():
    source = _source_snapshot()
    derived = derive_color_calibration_analysis_snapshot(
        _Session(),
        source_snapshot=source,
        target_calibration_name="color-v1",
    )

    assert derived is not None
    assert derived.calibration_version_id == 20
    assert derived.algorithm == "calibration_analysis_snapshot_v3_incremental_color"
    assert derived.materialization_source == "DERIVED_COLOR_FROM_CACHED_PARENT"
    assert derived.measurement_geometry is source.measurement_geometry
    assert derived.frames_by_id is source.frames_by_id
    assert derived.objects_by_id is source.objects_by_id
    assert derived.timings_sec["load_measurement_rows"] == 0.0
    assert derived.timings_sec["load_object_rows"] == 0.0
    assert derived.timings_sec["load_frame_rows"] == 0.0
    assert derived.timings_sec["_runner_derived"] == 1.0

    # correction = 0.2 - 0.1 * (0.8 - 0.5) = 0.17
    assert [row.mag_cal for row in derived.measurement_rows] == pytest.approx(
        [12.17, 12.27, 12.07]
    )
    assert all(row.mag_err_systematic == pytest.approx(0.03) for row in derived.measurement_rows)
    assert all(row.flags["reference_catalog_name"] == "panstarrs" for row in derived.measurement_rows)
    assert all(row.flags["reference_mag"] == pytest.approx(10.4) for row in derived.measurement_rows)
    assert len(derived.object_rows) == 1
    assert derived.object_rows[0].ref_mag == pytest.approx(10.4)
    assert derived.object_rows[0].color == pytest.approx(0.8)
    assert derived.object_rows[0].reference_catalog_name == "panstarrs"
