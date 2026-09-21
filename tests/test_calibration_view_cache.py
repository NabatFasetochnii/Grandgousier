from types import SimpleNamespace

from grandgousier.photometry import calibration_view as cvmod


class _Mappings:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def execute(self, statement):
        return _Mappings([
            {
                "base_measurement_id": 101,
                "flux": 900.0,
                "flux_err": 9.0,
                "mag_inst": 12.1,
                "mag_cal": 12.345,
                "mag_err": 0.025,
                "mag_err_random": 0.023,
                "mag_err_systematic": 0.01,
                "local_zp_correction": 0.15,
            },
        ])


def test_science_view_prefers_durable_science_and_overlays_all_stored_scalars(monkeypatch):
    calibration = SimpleNamespace(
        calibration_version_id=7,
        name="science_v",
        product_kind="ENSEMBLE",
        product_role="SCIENCE",
    )
    state = {
        "measurement_id": 101,
        "base_measurement_id": 101,
        "object_id": 42,
        "frame_id": 1001,
        "x": 10.0,
        "y": 11.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 99.0,
        "mag_err": 0.99,
        "mag_err_random": 0.99,
        "mag_err_systematic": 0.02,
        "aperture_radius_pix": 4.0,
        "local_background": 2.0,
        "local_zp_correction": 0.2,
        "centroid_dx_pix": 0.01,
        "centroid_dy_pix": -0.01,
        "flags": {"catalog_name": "shared", "source_id": "42"},
    }
    monkeypatch.setattr(cvmod, "_resolve_cv", lambda *args, **kwargs: calibration)
    monkeypatch.setattr(cvmod, "complete_science_calibration_ids", lambda *args, **kwargs: {7})
    monkeypatch.setattr(
        cvmod,
        "reconstruct_calibration_measurement_states",
        lambda *args, **kwargs: SimpleNamespace(states={(42, 1001): state}),
    )

    view = cvmod.load_calibration_photometry_view(
        _Session(), calibration_version_id=7, object_ids=[42], prefer_cache=True
    )
    assert view.source == "SCIENCE_PHOTOMETRY"
    assert len(view.rows) == 1
    row = view.rows[0]
    assert row.base_measurement_id == 101
    assert row.flux == 900.0
    assert row.mag_cal == 12.345
    assert row.mag_err == 0.025
    assert row.mag_err_random == 0.023
    assert row.local_zp_correction == 0.15
    # Geometry and flags are inherited from BASE + normalized reconstruction.
    assert row.x == 10.0
    assert row.aperture_radius_pix == 4.0
    assert row.flags["source_id"] == "42"
