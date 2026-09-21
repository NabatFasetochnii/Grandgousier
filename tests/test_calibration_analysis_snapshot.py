from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.models import Measurement
from grandgousier.quality import candidate_triage as candidate_triage_module
from grandgousier.quality.lightcurve_quality import (
    CalibrationAnalysisSnapshot,
    CalibrationFrameRow,
    CalibrationMeasurementRow,
    CalibrationObjectRow,
    _build_measurement_geometry_from_snapshot_rows,
    _build_object_scatter_rows_from_snapshot_rows,
    _fuse_compact_normalized_ensemble_rows,
)
from grandgousier.quality.random_error_validation import validate_random_error_model


def _measurement(frame_id: int, mag: float, x: float, y: float) -> CalibrationMeasurementRow:
    return CalibrationMeasurementRow(
        object_id=7,
        frame_id=frame_id,
        analysis_mag=mag,
        mag_cal=mag,
        mag_err_random=0.1,
        mag_err=0.2,
        mag_err_systematic=0.03,
        flux=1000.0,
        x=x,
        y=y,
        aperture_radius_pix=3.0,
        flags={
            "catalog_name": "gaia",
            "source_id": "source-7",
            "reference_mag": 10.1,
            "reference_color_value": 0.4,
            "reference_catalog_name": "panstarrs",
            "reference_band": "r",
        },
    )


def _snapshot() -> CalibrationAnalysisSnapshot:
    measurements = (
        _measurement(1, 9.0, 5.0, 6.0),
        _measurement(2, 10.0, 50.0, 55.0),
        _measurement(3, 11.0, 95.0, 94.0),
    )
    frames = {
        frame_id: CalibrationFrameRow(
            frame_id=frame_id,
            instrument_unit_id=4,
            mjd=60000.0 + frame_id,
            airmass=1.1,
            header_json={"NAXIS1": 100, "NAXIS2": 100, "FWHM": 2.5},
            quality_flags={},
            instrument="MASTER",
            filter_name="R",
            pixel_scale_arcsec=1.0,
        )
        for frame_id in (1, 2, 3)
    }
    objects = {
        7: CalibrationObjectRow(
            object_id=7,
            ra_deg=10.0,
            dec_deg=20.0,
            ref_mags={"G": 10.2},
            colors={"BP_RP": 0.5},
        )
    }
    object_rows = _build_object_scatter_rows_from_snapshot_rows(
        calibration_name="cal-v1",
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
        calibration_version_id=12,
        calibration_name="cal-v1",
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
        timings_sec={"_runner_cache_hit": 1.0},
    )


def test_snapshot_builders_preserve_scatter_and_geometry_semantics():
    snapshot = _snapshot()
    assert len(snapshot.object_rows) == 1
    row = snapshot.object_rows[0]
    assert row.object_id == 7
    assert row.n_measurements == 3
    assert row.mag_median == pytest.approx(10.0)
    assert row.mag_rms == pytest.approx(1.0)
    assert row.mag_err_median == pytest.approx(0.1)
    assert row.ref_mag == pytest.approx(10.1)
    assert row.color == pytest.approx(0.4)
    assert row.reference_catalog_name == "panstarrs"
    assert row.reference_band == "r"

    geometry = snapshot.measurement_geometry[7]
    assert geometry["median_x"] == pytest.approx(50.0)
    assert geometry["median_y"] == pytest.approx(55.0)
    assert geometry["edge_distance_pix_min"] == pytest.approx(4.0)
    assert geometry["aperture_radius_arcsec_median"] == pytest.approx(3.0)
    assert geometry["fwhm_arcsec_median"] == pytest.approx(2.5)


def test_snapshot_scope_normalizes_frame_ids():
    snapshot = _snapshot()
    assert snapshot.matches(
        calibration_name="cal-v1",
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=[3, 2, 1, 1],
        min_measurements=3,
    )
    assert not snapshot.matches(
        calibration_name="cal-v1",
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=[1, 2],
        min_measurements=3,
    )


def test_candidate_triage_reuses_supplied_object_rows(monkeypatch):
    monkeypatch.setattr(
        candidate_triage_module,
        "load_object_scatter_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected DB reload")),
    )
    monkeypatch.setattr(candidate_triage_module, "load_object_qc_map", lambda *args, **kwargs: {})

    result = candidate_triage_module.build_candidate_triage(
        object(),
        calibration_name="cal-v1",
        object_rows=[],
    )
    assert result.summary["objects_used"] == 0


def test_measurement_model_declares_calibration_first_index():
    index_names = {index.name for index in Measurement.__table__.indexes}
    assert "ix_measurements_calibration_object_frame" in index_names


def test_default_bulk_batch_size_is_raised_safely():
    assert DEFAULT_BULK_INSERT_BATCH_SIZE == 3000


class _ScalarOnlySession:
    def scalar(self, statement):
        return SimpleNamespace(calibration_version_id=12)


def test_random_error_validation_can_reuse_snapshot_without_measurement_query():
    snapshot = _snapshot()
    result = validate_random_error_model(
        _ScalarOnlySession(),
        calibration_name="cal-v1",
        frame_ids=[1, 2, 3],
        min_measurements_per_object=3,
        analysis_snapshot=snapshot,
    )
    assert result.analysis_snapshot_cache_hit is True
    assert result.analysis_snapshot_measurements == 3
    assert result.analysis_snapshot_algorithm == snapshot.algorithm



def test_fused_compact_normalized_ensemble_rows_preserve_qc_contract():
    aperture_term = SimpleNamespace(
        frame_id=1,
        accepted=True,
        model_name="constant",
        coefficient_0=0.1,
        coefficient_x=0.0,
        coefficient_y=0.0,
        details={"image_width": 100, "image_height": 100},
    )
    aperture_zp_term = SimpleNamespace(frame_id=1, accepted=True, offset_mag=1.0)
    ensemble_term = SimpleNamespace(frame_id=1, accepted=True, offset_mag=0.2)
    ensemble_object = SimpleNamespace(
        object_id=7,
        is_reference=True,
        requested_exclusion=False,
        effective_exclusion=False,
    )
    base_rows = [
        {
            "measurement_id": 101,
            "object_id": 7,
            "frame_id": 1,
            "x": 10.0,
            "y": 20.0,
            "flux": 1000.0,
            "mag_inst": 15.0,
            "mag_err": 0.03,
            "mag_err_random": 0.02,
            "mag_err_systematic": None,
            "aperture_radius_pix": 3.0,
            "catalog_name": "gaia",
            "source_id": "source-7",
            "saturated_or_near_saturated": False,
            "random_error_physical_mag_err": 0.018,
            "random_error_legacy_mag_err": 0.025,
        },
        {
            "object_id": 8,
            "frame_id": 1,
            "x": 11.0,
            "y": 21.0,
            "flux": 900.0,
            "mag_inst": 15.5,
            "mag_err": 0.04,
            "mag_err_random": 0.03,
            "mag_err_systematic": None,
            "aperture_radius_pix": 3.0,
            "catalog_name": "other",
            "source_id": "source-8",
        },
    ]

    rows = _fuse_compact_normalized_ensemble_rows(
        base_rows,
        aperture_terms={1: aperture_term},
        aperture_zp_terms={1: aperture_zp_term},
        ensemble_terms={1: ensemble_term},
        ensemble_object_terms={7: ensemble_object},
        aperture_zp_catalog_name="gaia",
        ensemble_catalog_name="gaia",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.object_id == 7
    assert row.frame_id == 1
    assert row.analysis_mag == pytest.approx(15.9)
    assert row.mag_cal == pytest.approx(15.9)
    assert row.source_measurement_id == 101
    assert row.mag_inst == pytest.approx(15.1)
    assert row.mag_err_random == pytest.approx(0.02)
    assert row.mag_err == pytest.approx(0.02)
    assert row.flux == pytest.approx(1000.0 * 10.0 ** (-0.4 * 0.1))
    assert row.x == pytest.approx(10.0)
    assert row.y == pytest.approx(20.0)
    assert row.aperture_radius_pix == pytest.approx(3.0)
    assert row.flags["catalog_name"] == "gaia"
    assert row.flags["source_id"] == "source-7"
    assert row.flags["random_error_physical_mag_err"] == pytest.approx(0.018)
    assert row.flags["random_error_legacy_mag_err"] == pytest.approx(0.025)
    assert row.flags["ensemble_object_is_reference"] is True
    assert row.flags["ensemble_object_excluded_from_reference"] is False


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _PreloadedSeedSession:
    def __init__(self, scalar_sequences):
        self._scalar_sequences = list(scalar_sequences)
        self.execute_called = False

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, _statement):
        return 0

    def scalars(self, _statement):
        if not self._scalar_sequences:
            raise AssertionError("unexpected scalars call")
        return _FakeScalarResult(self._scalar_sequences.pop(0))

    def execute(self, _statement):
        self.execute_called = True
        raise AssertionError("preloaded seed path must not issue BASE measurement query")


def test_fused_snapshot_preloaded_base_rows_skip_measurement_query(monkeypatch):
    from grandgousier.calibration import reconstruction as reconstruction_module
    from grandgousier.quality.lightcurve_quality import (
        _load_fused_compact_normalized_ensemble_rows,
    )

    base_cv = SimpleNamespace(
        calibration_version_id=1,
        product_kind="BASE_PHOTOMETRY",
        materialization_kind="FULL_MEASUREMENTS",
    )
    aperture_cv = SimpleNamespace(calibration_version_id=2, product_kind="APERTURE_CORRECTION")
    aperture_zp_cv = SimpleNamespace(calibration_version_id=3, product_kind="APERTURE_ZP")
    ensemble_cv = SimpleNamespace(calibration_version_id=4, product_kind="ENSEMBLE")
    target_cv = SimpleNamespace(
        calibration_version_id=4,
        product_kind="ENSEMBLE",
        materialization_kind="NORMALIZED_TERMS",
    )

    iterations = {
        2: SimpleNamespace(calibration_iteration_id=20, parameters={}),
        3: SimpleNamespace(calibration_iteration_id=30, parameters={"catalog_name": "gaia"}),
        4: SimpleNamespace(
            calibration_iteration_id=40,
            parameters={"catalog_name": "gaia", "prefer_mag_cal": True},
        ),
    }
    monkeypatch.setattr(
        reconstruction_module,
        "_chain_from_base",
        lambda _session, _cv: [base_cv, aperture_cv, aperture_zp_cv, ensemble_cv],
    )
    monkeypatch.setattr(
        reconstruction_module,
        "_latest_iteration",
        lambda _session, cv_id: iterations[int(cv_id)],
    )

    aperture_term = SimpleNamespace(
        frame_id=1,
        accepted=True,
        model_name="constant",
        coefficient_0=0.1,
        coefficient_x=0.0,
        coefficient_y=0.0,
        details={"image_width": 100, "image_height": 100},
    )
    zp_term = SimpleNamespace(frame_id=1, accepted=True, offset_mag=1.0)
    ensemble_term = SimpleNamespace(frame_id=1, accepted=True, offset_mag=0.2)
    object_term = SimpleNamespace(
        object_id=7,
        is_reference=True,
        requested_exclusion=False,
        effective_exclusion=False,
    )
    session = _PreloadedSeedSession(
        [[aperture_term], [zp_term], [ensemble_term], [object_term]]
    )
    preloaded = (
        {
            "measurement_id": 101,
            "object_id": 7,
            "frame_id": 1,
            "x": 10.0,
            "y": 20.0,
            "flux": 1000.0,
            "mag_inst": 15.0,
            "mag_err": 0.03,
            "mag_err_random": 0.02,
            "mag_err_systematic": None,
            "aperture_radius_pix": 3.0,
            "catalog_name": "gaia",
            "source_id": "source-7",
            "saturated_or_near_saturated": False,
        },
    )
    timings = {}
    rows = _load_fused_compact_normalized_ensemble_rows(
        session,
        calibration_version=target_cv,
        frame_ids=(1,),
        instrument_unit_id=None,
        y_key="mag_cal",
        timings=timings,
        preloaded_base_rows=preloaded,
    )

    assert session.execute_called is False
    assert timings["compact_normalized_base_query"] == 0.0
    assert timings["compact_normalized_seed_rows"] == 1.0
    assert len(rows) == 1
    assert rows[0].mag_cal == pytest.approx(15.9)
    assert rows[0].source_measurement_id == 101
