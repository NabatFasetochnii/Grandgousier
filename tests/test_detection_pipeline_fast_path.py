from __future__ import annotations

import json
import math
import random

import numpy as np
import pytest

from grandgousier.db.detection_io import (
    DETECTION_CATALOG_MATCH_WRITE_COLUMNS,
    SOURCE_DETECTION_WRITE_COLUMNS,
    detection_catalog_match_copy_tuple,
    source_detection_copy_tuple,
)
from grandgousier.quality.detection_catalog_qc import (
    _select_catalog_sources_bulk,
    _select_detections_bulk,
)


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.execute_calls = 0

    def execute(self, statement):
        del statement
        rows = self._result_sets[self.execute_calls]
        self.execute_calls += 1
        return _FakeMappingResult(rows)


def _legacy_select(scored, *, max_sources, min_separation_pix):
    chosen = []
    sep2 = min_separation_pix * min_separation_pix
    for snr, y_int, x_int, peak in scored:
        if len(chosen) >= max_sources:
            break
        if sep2 > 0 and any(
            (x_int - x0) ** 2 + (y_int - y0) ** 2 < sep2
            for _, y0, x0, _ in chosen
        ):
            continue
        chosen.append((snr, float(y_int), float(x_int), peak))
    return chosen



def test_detection_run_fingerprint_is_stable_for_parameter_key_order():
    from grandgousier.db.detection_runs import detection_run_fingerprint

    left = detection_run_fingerprint(
        run_kind="SOURCE_DETECTION",
        algorithm="numpy_localmax_v1",
        parameters={"threshold_sigma": 5.0, "max_sources_per_frame": 2000},
    )
    right = detection_run_fingerprint(
        run_kind="SOURCE_DETECTION",
        algorithm="numpy_localmax_v1",
        parameters={"max_sources_per_frame": 2000, "threshold_sigma": 5.0},
    )
    assert left == right
    assert len(left) == 64

def test_spatial_hash_greedy_matches_legacy_selection_exactly():
    from grandgousier.photometry.detection_algorithms import select_separated_candidates

    rng = random.Random(42)
    scored = [
        (1000.0 - i, rng.randrange(0, 2048), rng.randrange(0, 2048), 50000.0 - i)
        for i in range(5000)
    ]
    # Preserve the same precondition as detect_sources_in_image.
    scored.sort(reverse=True)

    for separation in (0.0, 1.0, 2.5, 5.0, 11.0):
        expected = _legacy_select(scored, max_sources=2000, min_separation_pix=separation)
        actual = select_separated_candidates(
            scored,
            max_sources=2000,
            min_separation_pix=separation,
        )
        assert actual == expected


def test_spatial_hash_preserves_strict_boundary_rule():
    from grandgousier.photometry.detection_algorithms import select_separated_candidates

    scored = [
        (10.0, 0, 0, 10.0),
        (9.0, 0, 5, 9.0),  # exactly radius=5: accepted by legacy strict < test
        (8.0, 3, 4, 8.0),  # exactly radius=5 from first: accepted unless blocked by second
    ]
    assert select_separated_candidates(scored, max_sources=10, min_separation_pix=5.0) == _legacy_select(
        scored,
        max_sources=10,
        min_separation_pix=5.0,
    )


def test_detection_and_catalog_bulk_prefetch_are_scalar_and_grouped():
    detection_rows = [
        {
            "source_detection_id": 10,
            "frame_id": 1,
            "x": 11.0,
            "y": 12.0,
            "ra_deg": 10.0,
            "dec_deg": 20.0,
            "peak_adu": 123.0,
            "flux": 1000.0,
            "snr": 50.0,
            "algorithm": "numpy_localmax_v1",
        },
        {
            "source_detection_id": 20,
            "frame_id": 2,
            "x": 21.0,
            "y": 22.0,
            "ra_deg": 11.0,
            "dec_deg": 21.0,
            "peak_adu": None,
            "flux": None,
            "snr": None,
            "algorithm": "numpy_localmax_v1",
        },
    ]
    catalog_rows = [
        {
            "frame_id": 1,
            "catalog_source_id": 100,
            "x": 11.1,
            "y": 12.1,
            "source_id": "A",
            "ra_deg": 10.0,
            "dec_deg": 20.0,
            "ref_mags": {"G": 12.3},
        },
        {
            "frame_id": 2,
            "catalog_source_id": 100,
            "x": 21.1,
            "y": 22.1,
            "source_id": "A",
            "ra_deg": 10.0,
            "dec_deg": 20.0,
            "ref_mags": {"G": 12.3},
        },
    ]
    session = _FakeSession([detection_rows, catalog_rows])

    detections = _select_detections_bulk(
        session,
        frame_ids=[1, 2],
        algorithm="numpy_localmax_v1",
        min_snr=None,
    )
    catalogs = _select_catalog_sources_bulk(
        session,
        frame_ids=[1, 2],
        catalog_name="cat",
    )

    assert session.execute_calls == 2
    assert [row.source_detection_id for row in detections[1]] == [10]
    assert [row.source_detection_id for row in detections[2]] == [20]
    assert catalogs[1][0][0] is catalogs[2][0][0]
    assert catalogs[1][0][0].ref_mags == {"G": 12.3}
    assert catalogs[2][0][1].x == 21.1



def test_detection_image_factors_frame_diagnostics_out_of_source_flags(monkeypatch):
    pytest.importorskip("astropy")
    from grandgousier.photometry import source_detection as source_module

    data = np.arange(2500, dtype=float).reshape(50, 50)
    monkeypatch.setattr(source_module, "_local_maxima", lambda *_args, **_kwargs: [(25, 25)])
    monkeypatch.setattr(
        source_module,
        "_select_separated_candidates",
        lambda scored, **_kwargs: list(scored),
    )
    monkeypatch.setattr(source_module, "_centroid_com", lambda *_args, **_kwargs: (25.0, 25.0))
    monkeypatch.setattr(
        source_module,
        "_pixel_to_world_batch",
        lambda _wcs, centered: [(None, None) for _ in centered],
    )
    monkeypatch.setattr(
        source_module,
        "_measure_detection_apertures_batch",
        lambda *_args, **_kwargs: [
            source_module._DetectionApertureMeasurement(
                flux=1234.0,
                flux_err=12.0,
                local_background=100.0,
                flags={"SATURATED": False},
            )
        ],
    )

    diagnostics = {}
    detections = source_module.detect_sources_in_image(
        data,
        wcs=None,
        threshold_sigma=5.0,
        max_sources=2000,
        min_separation_pix=5.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        centroid_box_radius_pix=3,
        exptime=30.0,
        saturation_level_adu=None,
        diagnostics_out=diagnostics,
    )

    assert len(detections) == 1
    assert detections[0].flags == {"SATURATED": False}
    assert diagnostics["centroid_method"] == "positive_com"
    assert diagnostics["n_candidates_raw_before_cap"] == 1
    assert diagnostics["max_sources_per_frame"] == 2000
    assert diagnostics["detection_cap_reached"] is False
    for repeated in (
        "background_global",
        "sigma_global",
        "threshold_adu",
        "centroid_method",
        "n_candidates_raw_before_cap",
        "max_sources_per_frame",
        "DETECTION_CAP_REACHED",
    ):
        assert repeated not in detections[0].flags


def test_detection_image_records_zero_detection_reason_when_finite_pixels_are_insufficient():
    pytest.importorskip("astropy")
    from grandgousier.photometry import source_detection as source_module

    data = np.full((8, 8), np.nan, dtype=float)
    diagnostics = {}
    detections = source_module.detect_sources_in_image(
        data,
        wcs=None,
        threshold_sigma=5.0,
        max_sources=2000,
        min_separation_pix=5.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        centroid_box_radius_pix=3,
        exptime=30.0,
        saturation_level_adu=None,
        diagnostics_out=diagnostics,
    )

    assert detections == []
    assert diagnostics == {"empty_reason": "insufficient_finite_pixels", "finite_pixels": 0}

def test_detection_copy_tuples_match_normalized_column_order_and_serialize_row_flags():
    source_row = {
        "detection_run_frame_id": 11,
        "x": 2.0,
        "y": 3.0,
        "ra_deg": 10.0,
        "dec_deg": 20.0,
        "peak_adu": 100.0,
        "flux": 90.0,
        "flux_err": 3.0,
        "local_background": 5.0,
        "snr": 30.0,
        "npix": 25,
        "detection_status": "DETECTED",
        "flags": {"a": "µ", "z": 2},
    }
    copied = source_detection_copy_tuple(source_row)
    mapped = dict(zip(SOURCE_DETECTION_WRITE_COLUMNS, copied, strict=True))
    assert len(copied) == len(SOURCE_DETECTION_WRITE_COLUMNS)
    assert mapped["detection_run_frame_id"] == 11
    assert "algorithm" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert "parameters" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert "frame_id" not in SOURCE_DETECTION_WRITE_COLUMNS
    assert json.loads(mapped["flags"]) == {"a": "µ", "z": 2}

    match_row = {
        "detection_run_frame_id": 22,
        "source_detection_id": 2,
        "catalog_source_id": 3,
        "match_status": "MATCHED",
        "sep_arcsec": 0.2,
        "dx_pix": 0.1,
        "dy_pix": -0.1,
        "detection_mag_inst": -7.5,
        "catalog_mag": 12.0,
        "zero_point": 19.5,
    }
    copied_match = detection_catalog_match_copy_tuple(match_row)
    mapped_match = dict(zip(DETECTION_CATALOG_MATCH_WRITE_COLUMNS, copied_match, strict=True))
    assert len(copied_match) == len(DETECTION_CATALOG_MATCH_WRITE_COLUMNS)
    assert mapped_match["detection_run_frame_id"] == 22
    assert math.isclose(mapped_match["sep_arcsec"], 0.2)
    for removed in ("frame_id", "catalog_name", "algorithm", "details"):
        assert removed not in DETECTION_CATALOG_MATCH_WRITE_COLUMNS


def test_detection_copy_stages_stream_and_merge(monkeypatch):
    from grandgousier.db import detection_io

    class FakeCopy:
        def __init__(self):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write_row(self, row):
            self.rows.append(row)

    class FakeCursor:
        def __init__(self):
            self.executed = []
            self.copy_sql = []
            self.copy_objects = []
            self.closed = False

        def execute(self, sql):
            self.executed.append(sql)

        def copy(self, sql):
            self.copy_sql.append(sql)
            obj = FakeCopy()
            self.copy_objects.append(obj)
            return obj

        def close(self):
            self.closed = True

    class FakeDriverConnection:
        def __init__(self):
            self.cursors = []

        def cursor(self):
            cur = FakeCursor()
            self.cursors.append(cur)
            return cur

    driver = FakeDriverConnection()

    class FakeSAConnection:
        dialect = type("Dialect", (), {"name": "postgresql"})()
        connection = type("Proxy", (), {"driver_connection": driver})()

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

    session = FakeSession()
    assert detection_io.postgres_copy_capable(session) is True

    source_stage = detection_io.PostgresSourceDetectionCopyStage.create(session)
    source_stage.append_rows([
        {
            "detection_run_frame_id": 11,
            "x": 1.0,
            "y": 2.0,
            "ra_deg": 3.0,
            "dec_deg": 4.0,
            "peak_adu": 5.0,
            "flux": 6.0,
            "flux_err": 0.5,
            "local_background": 7.0,
            "snr": 8.0,
            "npix": 9,
            "detection_status": "DETECTED",
            "flags": {},
        }
    ])
    source_stage.merge()
    source_stage.close()
    assert source_stage.rows_staged == 1
    assert source_stage.copy_batches == 1

    match_stage = detection_io.PostgresDetectionCatalogMatchCopyStage.create(session)
    match_stage.append_rows([
        {
            "detection_run_frame_id": 22,
            "source_detection_id": 2,
            "catalog_source_id": 3,
            "match_status": "MATCHED",
            "sep_arcsec": 0.1,
            "dx_pix": 0.0,
            "dy_pix": 0.0,
            "detection_mag_inst": -5.0,
            "catalog_mag": 12.0,
            "zero_point": 17.0,
        }
    ])
    match_stage.merge()
    match_stage.close()
    assert match_stage.rows_staged == 1
    assert match_stage.copy_batches == 1

    sql = "\n".join(statement for cur in driver.cursors for statement in cur.executed)
    assert "INSERT INTO source_detections" in sql
    assert "INSERT INTO detection_catalog_matches" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_detection_catalog_matches_run_frame_catalog_source" in sql
