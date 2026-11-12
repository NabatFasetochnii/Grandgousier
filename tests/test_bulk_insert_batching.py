from __future__ import annotations

from types import SimpleNamespace

from grandgousier.db.bulk import iter_sized_chunks, safe_bulk_insert_batch_size
from grandgousier.db.measurement_io import _assert_base_measurement_targets, upsert_measurement_rows
from grandgousier.quality.detection_catalog_qc import _candidate_pairs


class _BaseTargetResult:
    def __init__(self, calibration_ids):
        self._calibration_ids = sorted(calibration_ids)

    def all(self):
        return [
            (calibration_id, "BASE_PHOTOMETRY", "BASE", "FULL_MEASUREMENTS")
            for calibration_id in self._calibration_ids
        ]


class FakeSession:
    def __init__(self):
        self.statements = []

    def execute(self, stmt):
        if getattr(stmt, "is_select", False):
            return _BaseTargetResult({2})
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


def test_detection_catalog_candidate_pairs_uses_spherical_prefilter():
    from grandgousier.quality.detection_catalog_qc import CatalogMatchSource

    detections = [
        SimpleNamespace(x=10.0, y=10.0, ra_deg=1.0, dec_deg=1.0),
        SimpleNamespace(x=100.0, y=100.0, ra_deg=1.0, dec_deg=1.001),
    ]
    near_source = CatalogMatchSource(1, "near", 1.0 + 1.0 / 3600.0, 1.0, {})
    far_source = CatalogMatchSource(2, "far", 1.0, 1.1, {})
    catalog_rows = [
        near_source,
        far_source,
    ]

    pairs = _candidate_pairs(
        detections=detections,
        catalog_rows=catalog_rows,
        radius_arcsec=2.0,
    )

    assert pairs == [(detections[0], near_source)]


def test_measurement_copy_tuple_matches_column_order_and_serializes_flags() -> None:
    import json

    from grandgousier.db.measurement_io import MEASUREMENT_WRITE_COLUMNS, measurement_copy_tuple

    row = {
        "object_id": 1,
        "frame_id": 2,
        "calibration_version_id": 3,
        "x": 10.0,
        "y": 11.0,
        "flux": 100.0,
        "flux_err": 2.0,
        "mag_inst": 15.0,
        "mag_cal": None,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": None,
        "aperture_radius_pix": 3.0,
        "local_background": 20.0,
        "local_zp_correction": None,
        "centroid_dx_pix": None,
        "centroid_dy_pix": None,
        "flags": {"z": 2, "a": "µ"},
    }
    copied = measurement_copy_tuple(row)
    assert len(copied) == len(MEASUREMENT_WRITE_COLUMNS)
    mapped = dict(zip(MEASUREMENT_WRITE_COLUMNS, copied, strict=True))
    assert mapped["object_id"] == 1
    assert mapped["frame_id"] == 2
    assert mapped["calibration_version_id"] == 3
    assert json.loads(mapped["flags"]) == {"a": "µ", "z": 2}


def test_measurement_copy_stage_streams_multiple_batches_and_single_direct_insert(monkeypatch) -> None:
    from grandgousier.db import measurement_io as module

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

    cursors = []

    class FakeDriverConnection:
        def cursor(self):
            cursor = FakeCursor()
            cursors.append(cursor)
            return cursor

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

        def execute(self, stmt):
            return _BaseTargetResult({3})

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="base123"))
    session = FakeSession()
    assert module.postgres_copy_capable(session) is True
    stage = module.PostgresMeasurementCopyStage.create(session)

    base = {
        "object_id": 1,
        "frame_id": 2,
        "calibration_version_id": 3,
        "x": 1.0,
        "y": 2.0,
        "flux": 100.0,
        "flux_err": 3.0,
        "mag_inst": 15.0,
        "mag_cal": None,
        "mag_err": 0.03,
        "mag_err_random": 0.03,
        "mag_err_systematic": None,
        "aperture_radius_pix": 3.0,
        "local_background": 5.0,
        "local_zp_correction": None,
        "centroid_dx_pix": None,
        "centroid_dy_pix": None,
        "flags": {},
    }
    stage.append_rows([base])
    stage.append_rows([{**base, "object_id": 2}])
    stage.merge(upsert=False)
    stage.close()

    assert stage.rows_staged == 2
    assert stage.copy_batches == 2
    all_sql = [sql for cursor in cursors for sql in cursor.executed]
    assert any(sql.startswith('CREATE TEMP TABLE "gg_measurement_stage_base123"') for sql in all_sql)
    inserts = [sql for sql in all_sql if sql.startswith("INSERT INTO measurements")]
    assert len(inserts) == 1
    assert "ON CONFLICT" not in inserts[0]
    assert any(sql == 'DROP TABLE IF EXISTS "gg_measurement_stage_base123"' for sql in all_sql)
    copied_rows = [row for cursor in cursors for copy_obj in cursor.copy_objects for row in copy_obj.rows]
    assert len(copied_rows) == 2


def test_measurement_copy_stage_uses_upsert_for_preexisting_target(monkeypatch) -> None:
    from grandgousier.db import measurement_io as module

    class FakeCopy:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write_row(self, row):
            return None

    class FakeCursor:
        def __init__(self, statements):
            self.statements = statements

        def execute(self, sql):
            self.statements.append(sql)

        def copy(self, sql):
            return FakeCopy()

        def close(self):
            return None

    statements = []

    class FakeDriverConnection:
        def cursor(self):
            return FakeCursor(statements)

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

        def execute(self, stmt):
            return _BaseTargetResult({3})

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="upsert123"))
    stage = module.PostgresMeasurementCopyStage.create(FakeSession())
    row = {column: None for column in module.MEASUREMENT_WRITE_COLUMNS}
    row.update({"object_id": 1, "frame_id": 2, "calibration_version_id": 3, "flags": {}})
    stage.append_rows([row])
    stage.merge(upsert=True)
    stage.close()

    insert_sql = next(sql for sql in statements if sql.startswith("INSERT INTO measurements"))
    assert "ON CONFLICT ON CONSTRAINT uq_measurement_version DO UPDATE SET" in insert_sql
    assert '"flux" = EXCLUDED."flux"' in insert_sql


def test_frame_catalog_copy_stage_streams_batches_and_single_upsert(monkeypatch) -> None:
    from grandgousier.db import catalog_link_io as module

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
            self.copy_objects = []

        def execute(self, sql):
            self.executed.append(sql)

        def copy(self, sql):
            obj = FakeCopy()
            self.copy_objects.append(obj)
            return obj

        def close(self):
            return None

    cursors = []

    class FakeDriverConnection:
        def cursor(self):
            cursor = FakeCursor()
            cursors.append(cursor)
            return cursor

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="links123"))
    session = FakeSession()
    assert module.postgres_copy_capable(session) is True
    stage = module.PostgresFrameCatalogLinkCopyStage.create(session)

    base = {
        "frame_id": 1,
        "catalog_source_id": 2,
        "x": 10.0,
        "y": 20.0,
        "in_footprint": True,
        "margin_pix": 0.0,
        "match_status": "IN_FOOTPRINT",
        "details": {"ref_mag": 15.0, "mag_key": "G"},
    }
    stage.append_rows([base])
    stage.append_rows([{**base, "catalog_source_id": 3}])
    stage.merge(upsert=True)
    stage.close()

    assert stage.rows_staged == 2
    assert stage.copy_batches == 2
    all_sql = [sql for cursor in cursors for sql in cursor.executed]
    assert any(sql.startswith('CREATE TEMP TABLE "gg_frame_catalog_stage_links123"') for sql in all_sql)
    inserts = [sql for sql in all_sql if sql.startswith("INSERT INTO frame_catalog_links")]
    assert len(inserts) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_frame_catalog_links_frame_source" in inserts[0]
    copied_rows = [row for cursor in cursors for copy_obj in cursor.copy_objects for row in copy_obj.rows]
    assert len(copied_rows) == 2
    assert '"mag_key":"G"' in copied_rows[0][-1]


def test_frame_catalog_copy_stage_direct_insert_after_replace(monkeypatch) -> None:
    from grandgousier.db import catalog_link_io as module

    class FakeCopy:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write_row(self, row):
            return None

    statements = []

    class FakeCursor:
        def execute(self, sql):
            statements.append(sql)

        def copy(self, sql):
            return FakeCopy()

        def close(self):
            return None

    class FakeDriverConnection:
        def cursor(self):
            return FakeCursor()

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="links456"))
    stage = module.PostgresFrameCatalogLinkCopyStage.create(FakeSession())
    stage.append_rows([
        {
            "frame_id": 1,
            "catalog_source_id": 2,
            "x": 10.0,
            "y": 20.0,
            "in_footprint": True,
            "margin_pix": 0.0,
            "match_status": "IN_FOOTPRINT",
            "details": {},
        }
    ])
    stage.merge(upsert=False)
    stage.close()

    insert_sql = next(sql for sql in statements if sql.startswith("INSERT INTO frame_catalog_links"))
    assert "ON CONFLICT" not in insert_sql


def test_measurement_direct_copy_writer_streams_rows_without_staging() -> None:
    from grandgousier.db import measurement_io as module

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
            self.copy_sql = []
            self.copy_objects = []

        def copy(self, sql):
            self.copy_sql.append(sql)
            obj = FakeCopy()
            self.copy_objects.append(obj)
            return obj

        def close(self):
            return None

    cursors = []

    class FakeDriverConnection:
        def cursor(self):
            cursor = FakeCursor()
            cursors.append(cursor)
            return cursor

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

        def execute(self, stmt):
            return _BaseTargetResult({3})

    row = {column: None for column in module.MEASUREMENT_WRITE_COLUMNS}
    row.update(
        {
            "object_id": 1,
            "frame_id": 2,
            "calibration_version_id": 3,
            "flags": {"source": "test"},
        }
    )
    writer = module.PostgresMeasurementDirectCopyWriter.create(FakeSession())
    writer.append_rows([row, {**row, "object_id": 2}])

    assert writer.rows_written == 2
    assert writer.copy_batches == 1
    assert writer.copy_sec >= 0.0
    copy_sql = [sql for cursor in cursors for sql in cursor.copy_sql]
    assert len(copy_sql) == 1
    assert copy_sql[0].startswith('COPY measurements ("object_id", "frame_id", "calibration_version_id"')
    assert all("stage" not in sql.lower() for sql in copy_sql)
    copied_rows = [
        value
        for cursor in cursors
        for copy_obj in cursor.copy_objects
        for value in copy_obj.rows
    ]
    assert len(copied_rows) == 2


def test_measurement_target_guard_accepts_base_and_rejects_derived() -> None:
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Session:
        def __init__(self, product_kind, product_role, materialization_kind):
            self.values = (product_kind, product_role, materialization_kind)

        def execute(self, stmt):
            return Result([(7, *self.values)])

    rows = [{"calibration_version_id": 7}]
    assert _assert_base_measurement_targets(
        Session("BASE_PHOTOMETRY", "BASE", "FULL_MEASUREMENTS"), rows
    ) == {7}

    import pytest
    with pytest.raises(ValueError, match="BASE-only"):
        _assert_base_measurement_targets(
            Session("ENSEMBLE", "INTERMEDIATE", "FULL_MEASUREMENTS"), rows
        )


def test_measurement_target_guard_requires_calibration_version_id() -> None:
    import pytest
    with pytest.raises(ValueError, match="calibration_version_id is required"):
        _assert_base_measurement_targets(FakeSession(), [{"object_id": 1}])
