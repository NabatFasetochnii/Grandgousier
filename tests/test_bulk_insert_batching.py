from __future__ import annotations

from types import SimpleNamespace

from grandgousier.db.bulk import iter_sized_chunks, safe_bulk_insert_batch_size
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.quality.detection_catalog_qc import _candidate_pairs


class FakeSession:
    def __init__(self):
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return None


def test_safe_bulk_insert_batch_size_respects_postgresql_parameter_limit():
    assert safe_bulk_insert_batch_size(n_columns=16, requested=10000) == 3750
    assert safe_bulk_insert_batch_size(n_columns=16, requested=1000) == 1000


def test_iter_sized_chunks_uses_safe_size():
    rows = list(range(9000))
    chunks = list(iter_sized_chunks(rows, n_columns=16, requested=10000))
    assert [len(c) for c in chunks] == [3750, 3750, 1500]


def test_measurement_upsert_is_chunked():
    rows = [
        {
            "object_id": i,
            "frame_id": 1,
            "calibration_version_id": 2,
            "x": 1.0,
            "y": 2.0,
            "flux": 100.0,
            "flux_err": 10.0,
            "mag_inst": 15.0,
            "mag_cal": None,
            "mag_err": 0.1,
            "aperture_radius_pix": 3.0,
            "local_background": 5.0,
            "local_zp_correction": None,
            "centroid_dx_pix": None,
            "centroid_dy_pix": None,
            "flags": {},
        }
        for i in range(2501)
    ]
    session = FakeSession()
    n_batches = upsert_measurement_rows(session, rows, insert_batch_size=1000)
    assert n_batches == 3
    assert len(session.statements) == 3


def test_detection_catalog_candidate_pairs_uses_pixel_prefilter():
    frame = SimpleNamespace(instrument_unit=SimpleNamespace(pixel_scale_arcsec=1.0))
    detections = [
        SimpleNamespace(x=10.0, y=10.0, ra_deg=1.0, dec_deg=1.0),
        SimpleNamespace(x=100.0, y=100.0, ra_deg=1.0, dec_deg=1.0),
    ]
    near_source = SimpleNamespace(catalog_source_id=1)
    far_source = SimpleNamespace(catalog_source_id=2)
    catalog_rows = [
        (near_source, SimpleNamespace(x=11.0, y=10.5)),
        (far_source, SimpleNamespace(x=500.0, y=500.0)),
    ]

    pairs = _candidate_pairs(frame=frame, detections=detections, catalog_rows=catalog_rows, radius_arcsec=2.0)

    assert pairs == [(detections[0], near_source, catalog_rows[0][1])]
