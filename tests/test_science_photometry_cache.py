from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier.photometry import science_photometry as module


class _Mappings:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows=(), scalar=None, rowcount=0):
        self._rows = list(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def mappings(self):
        return _Mappings(self._rows)

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._scalar


def test_complete_science_ids_use_only_durable_table() -> None:
    class Session:
        def execute(self, statement, params):
            sql = str(statement)
            assert "FROM science_photometry" in sql
            assert "science_measurements" not in sql
            assert "science_photometry_cache" not in sql
            assert params == {"ids": [1, 2, 3]}
            return _Result([
                {"calibration_version_id": 1, "n": 10},
                {"calibration_version_id": 3, "n": 5},
            ])

    assert module.complete_science_calibration_ids(Session(), [3, 2, 1, 3]) == {1, 3}


def test_complete_science_ids_empty_scope_does_not_query() -> None:
    class NoQuerySession:
        def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("unexpected query")

    assert module.complete_science_calibration_ids(NoQuerySession(), []) == set()


def test_materialize_science_python_path_writes_all_changing_scalars(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as legacy

    target = SimpleNamespace(
        calibration_version_id=42,
        name="science42",
        series_id=5,
        product_role="SCIENCE",
        lifecycle_status="COMPLETE_IMMUTABLE",
    )
    reconstructed = SimpleNamespace(
        target_calibration_version_id=42,
        target_calibration_name="science42",
        base_calibration_version_id=7,
        states={
            (10, 100): {
                "base_measurement_id": 1001,
                "flux": 123.0,
                "flux_err": 1.2,
                "mag_inst": 14.0,
                "mag_cal": 14.2,
                "mag_err": 0.031,
                "mag_err_random": 0.03,
                "mag_err_systematic": 0.008,
                "local_zp_correction": 0.2,
                "flags": {"not": "stored"},
            },
            (11, 101): {
                "base_measurement_id": 1002,
                "flux": 456.0,
                "flux_err": 2.3,
                "mag_inst": 15.0,
                "mag_cal": 15.1,
                "mag_err": 0.041,
                "mag_err_random": 0.04,
                "mag_err_systematic": 0.009,
                "local_zp_correction": 0.1,
                "flags": {"not": "stored2"},
            },
        },
    )
    monkeypatch.setattr(legacy, "_resolve_cv", lambda *args, **kwargs: target)
    monkeypatch.setattr(legacy, "_postgres_server_reconstruction_capable", lambda session: False)
    monkeypatch.setattr(legacy, "_raise_if_empty_science_reconstruction", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "reconstruct_calibration_measurement_states", lambda *args, **kwargs: reconstructed)

    class Session:
        def __init__(self):
            self.rows = []
            self.sql = []

        def execute(self, statement, params=None):
            sql = str(statement)
            self.sql.append(sql)
            if sql.strip().startswith("DELETE FROM science_photometry") and "USING calibration_versions" not in sql:
                self.rows.clear()
                return _Result()
            if "DELETE FROM science_photometry sp" in sql and "USING calibration_versions" in sql:
                assert params == {"series_id": 5, "target_cv": 42}
                return _Result(rowcount=7)
            if sql.strip().startswith("INSERT INTO science_photometry"):
                assert isinstance(params, list)
                self.rows.extend(dict(row) for row in params)
                return _Result(rowcount=len(params))
            if "SELECT COUNT(*)::bigint FROM science_photometry" in sql:
                return _Result(scalar=len(self.rows))
            raise AssertionError(sql)

    session = Session()
    result = module.materialize_science_photometry(
        session, calibration_version_id=42, insert_batch_size=100
    )
    assert result.status == "OK"
    assert result.science_rows_after == 2
    assert result.pruned_non_science_rows == 7
    assert result.write_strategy == "PYTHON_RECONSTRUCTION_SCIENCE"
    assert set(session.rows[0]) == {
        "calibration_version_id",
        "base_measurement_id",
        *module.SCIENCE_SCALAR_COLUMNS,
    }
    assert all("flags" not in row for row in session.rows)
    assert all("science_measurements" not in sql for sql in session.sql)



def test_materialize_science_rejects_non_final_product(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as legacy

    target = SimpleNamespace(
        calibration_version_id=77,
        name="intermediate77",
        series_id=3,
        product_role="INTERMEDIATE",
        lifecycle_status="SUPERSEDED_IMMUTABLE",
    )
    monkeypatch.setattr(legacy, "_resolve_cv", lambda *args, **kwargs: target)

    class NoWriteSession:
        def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("durable writer must reject before SQL writes")

    import pytest

    with pytest.raises(ValueError, match="COMPLETE_IMMUTABLE SCIENCE"):
        module.materialize_science_photometry(NoWriteSession(), calibration_version_id=77)


@pytest.mark.parametrize(
    ("frame_ids", "message"),
    [
        ([], "must not be empty"),
        ([10], "exactly match"),
        ([10, 99], "exactly match"),
    ],
)
def test_materialize_science_rejects_incomplete_explicit_scope_before_delete(
    monkeypatch, frame_ids, message
):
    from grandgousier.photometry import science_materialization as legacy

    target = SimpleNamespace(
        calibration_version_id=42,
        name="science42",
        series_id=5,
        product_role="SCIENCE",
        lifecycle_status="COMPLETE_IMMUTABLE",
    )
    monkeypatch.setattr(legacy, "_resolve_cv", lambda *args, **kwargs: target)

    class Session:
        def __init__(self):
            self.deleted = False
            self.statements = []

        def execute(self, statement, params=None):
            sql = str(statement)
            self.statements.append(sql)
            if "FROM field_series_frames" in sql:
                assert params == {"series_id": 5}
                return _Result(rows=[(10,), (11,)])
            if sql.startswith("DELETE FROM science_photometry"):
                self.deleted = True
                raise AssertionError("scope validation must precede durable DELETE")
            raise AssertionError(sql)

    session = Session()
    with pytest.raises(ValueError, match=message):
        module.materialize_science_photometry(
            session,
            calibration_version_id=42,
            frame_ids=frame_ids,
        )
    assert not session.deleted
    assert not any(sql.startswith("DELETE FROM science_photometry") for sql in session.statements)


def test_materialize_science_rejects_explicit_scope_without_series_identity_before_delete(
    monkeypatch,
):
    from grandgousier.photometry import science_materialization as legacy

    target = SimpleNamespace(
        calibration_version_id=42,
        name="science42",
        series_id=None,
        product_role="SCIENCE",
        lifecycle_status="COMPLETE_IMMUTABLE",
    )
    monkeypatch.setattr(legacy, "_resolve_cv", lambda *args, **kwargs: target)

    class NoWriteSession:
        def execute(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("explicit unidentifiable scope must reject before SQL")

    with pytest.raises(ValueError, match="completeness cannot be established"):
        module.materialize_science_photometry(
            NoWriteSession(),
            calibration_version_id=42,
            frame_ids=[10],
        )


def test_pipeline_publishes_durable_science_after_candidate_export() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "pipeline" / "runner.py").read_text(encoding="utf-8")
    export_pos = source.index('add_step("export_candidate_light_curves"')
    materialize_pos = source.index('add_step("materialize_science_photometry"')
    assert export_pos < materialize_pos
    publication_block = source[source.rfind("def _science_materialization_step", 0, materialize_pos):materialize_pos]
    assert '_stamp_product(science_name, product_role="SCIENCE")' in publication_block
    assert 'prior_failed = any(step.status == "FAILED" for step in result.steps)' in source


def test_pipeline_has_one_durable_science_writer_and_no_wide_materialization() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "pipeline" / "runner.py").read_text(encoding="utf-8")
    assert "materialize_science_photometry(" in source
    assert "materialize_science_calibration(" not in source
    assert "materialize_science_photometry_cache(" not in source
    assert 'outputs["science_storage_mode"] = "DURABLE_NARROW"' in source
    # Only BASE is allowed to advertise FULL_MEASUREMENTS in the normal runner.
    assert source.count('materialization_kind="FULL_MEASUREMENTS"') == 1
