from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import _RECONSTRUCTED_FIELDS
from grandgousier.photometry import science_materialization as module
from grandgousier.photometry.science_photometry import SCIENCE_SCALAR_COLUMNS


def _state(**overrides):
    values = {
        "measurement_id": 101,
        "base_measurement_id": 101,
        "object_id": 1,
        "frame_id": 10,
        "x": 100.0,
        "y": 200.0,
        "flux": 1200.0,
        "flux_err": 12.0,
        "mag_inst": 14.2,
        "mag_cal": 14.5,
        "mag_err": 0.03,
        "mag_err_random": 0.028,
        "mag_err_systematic": 0.011,
        "aperture_radius_pix": 3.0,
        "local_background": 42.0,
        "local_zp_correction": 0.3,
        "centroid_dx_pix": 0.1,
        "centroid_dy_pix": -0.2,
        "flags": {},
    }
    values.update(overrides)
    return values


def _clean_schema_session(*, stored_mag_cal: float = 14.5) -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    reconstructed_fields_sql = ",\n".join(
        f"{name} REAL" for name in _RECONSTRUCTED_FIELDS
    )
    science_fields_sql = ",\n".join(
        f"{name} REAL" for name in SCIENCE_SCALAR_COLUMNS
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE measurements (
                    measurement_id INTEGER PRIMARY KEY,
                    calibration_version_id INTEGER NOT NULL,
                    object_id INTEGER NOT NULL,
                    frame_id INTEGER NOT NULL,
                    {reconstructed_fields_sql}
                )
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TABLE science_photometry (
                    calibration_version_id INTEGER NOT NULL,
                    base_measurement_id INTEGER NOT NULL,
                    {science_fields_sql},
                    PRIMARY KEY (calibration_version_id, base_measurement_id)
                )
                """
            )
        )
        state = _state()
        measurement_columns = [
            "measurement_id",
            "calibration_version_id",
            "object_id",
            "frame_id",
            *_RECONSTRUCTED_FIELDS,
        ]
        conn.execute(
            text(
                "INSERT INTO measurements ("
                + ",".join(measurement_columns)
                + ") VALUES ("
                + ",".join(f":{name}" for name in measurement_columns)
                + ")"
            ),
            {
                **state,
                "calibration_version_id": 29,
            },
        )
        science_values = {
            name: state[name] for name in SCIENCE_SCALAR_COLUMNS
        }
        science_values["mag_cal"] = stored_mag_cal
        science_columns = [
            "calibration_version_id",
            "base_measurement_id",
            *SCIENCE_SCALAR_COLUMNS,
        ]
        conn.execute(
            text(
                "INSERT INTO science_photometry ("
                + ",".join(science_columns)
                + ") VALUES ("
                + ",".join(f":{name}" for name in science_columns)
                + ")"
            ),
            {
                "calibration_version_id": 50,
                "base_measurement_id": 101,
                **science_values,
            },
        )
    return Session(engine)


def test_clean_schema_parity_uses_science_photometry_not_removed_wide_table(monkeypatch) -> None:
    session = _clean_schema_session()
    state = _state()
    monkeypatch.setattr(
        module,
        "_resolve_cv",
        lambda *args, **kwargs: SimpleNamespace(calibration_version_id=50, name="science50"),
    )
    monkeypatch.setattr(
        module,
        "reconstruct_calibration_measurement_states",
        lambda *args, **kwargs: SimpleNamespace(
            base_calibration_version_id=29,
            states={(1, 10): state},
        ),
    )

    try:
        report = module.science_materialization_parity_report(
            session, calibration_version_id=50
        )
    finally:
        session.close()

    assert report["status"] == "OK"
    assert report["science_storage_table"] == "science_photometry"
    assert report["science_photometry"] == 1
    assert report["comparison_source"] == "SCIENCE_PHOTOMETRY_VS_RECONSTRUCTED_NORMALIZED"
    assert report["mismatches"] == 0


def test_clean_schema_parity_detects_durable_scalar_mismatch(monkeypatch) -> None:
    session = _clean_schema_session(stored_mag_cal=99.0)
    monkeypatch.setattr(
        module,
        "_resolve_cv",
        lambda *args, **kwargs: SimpleNamespace(calibration_version_id=50, name="science50"),
    )
    monkeypatch.setattr(
        module,
        "reconstruct_calibration_measurement_states",
        lambda *args, **kwargs: SimpleNamespace(
            base_calibration_version_id=29,
            states={(1, 10): _state()},
        ),
    )

    try:
        report = module.science_materialization_parity_report(
            session, calibration_version_id=50
        )
    finally:
        session.close()

    assert report["status"] == "MISMATCH"
    assert report["scalar_mismatches"] == 1
    assert report["max_abs_delta"]["mag_cal"] == 84.5
