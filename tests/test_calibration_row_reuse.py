import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.calibration import aperture_correction as aperture_module
from grandgousier.calibration import color_calibration as color_module
from grandgousier.calibration.color_calibration import ColorCalibrationDiagnosticsResult


def test_color_row_selector_preserves_reference_filter_then_limit_semantics():
    rows = [
        {"measurement_id": 1, "mag_inst": None, "reference_catalog_source_id": None, "reference_mag": None},
        {"measurement_id": 2, "mag_inst": 12.0, "reference_catalog_source_id": 20, "reference_mag": None},
        {"measurement_id": 3, "mag_inst": None, "reference_catalog_source_id": 30, "reference_mag": 14.2},
        {"measurement_id": 4, "mag_inst": 13.0, "reference_catalog_source_id": 40, "reference_mag": 14.3},
        {"measurement_id": 5, "mag_inst": 13.1, "reference_catalog_source_id": 50, "reference_mag": 14.4},
    ]

    diagnostics = color_module._select_color_measurement_rows(
        rows,
        require_reference=True,
        require_mag_inst=False,
        limit=2,
    )
    application = color_module._select_color_measurement_rows(
        rows,
        require_reference=False,
        require_mag_inst=False,
        limit=2,
    )
    strict = color_module._select_color_measurement_rows(
        rows,
        require_reference=True,
        require_mag_inst=True,
        limit=1,
    )

    assert [row["measurement_id"] for row in diagnostics] == [3, 4]
    assert [row["measurement_id"] for row in application] == [1, 2]
    assert [row["measurement_id"] for row in strict] == [4]


def test_apply_color_calibration_loads_shared_superset_once(monkeypatch):
    source_cv = SimpleNamespace(calibration_version_id=77, name="ensemble")

    class FakeSession:
        def scalar(self, _stmt):
            return source_cv

    shared_rows = [
        {
            "measurement_id": 1,
            "object_id": 10,
            "frame_id": 100,
            "mag_inst": 15.0,
            "mag_cal": 14.9,
            "reference_catalog_source_id": 11,
            "reference_mag": 14.8,
        }
    ]
    load_calls = []
    diagnostic_preloaded = []

    def fake_load(_session, **kwargs):
        load_calls.append(kwargs)
        return shared_rows

    def fake_diagnostics(_session, **kwargs):
        diagnostic_preloaded.append(kwargs.get("_preloaded_rows"))
        return ColorCalibrationDiagnosticsResult(
            calibration_name="ensemble",
            series_id=None,
            identity_catalog_name="identity",
            reference_catalog_name="reference",
            reference_band="i",
            color_index="r-i",
        )

    monkeypatch.setattr(color_module, "_load_color_measurement_rows", fake_load)
    monkeypatch.setattr(color_module, "compute_color_calibration_diagnostics", fake_diagnostics)

    result = color_module.apply_color_calibration(
        FakeSession(),
        source_calibration_name="ensemble",
        target_calibration_name="science",
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="i",
        color_index="r-i",
        dry_run=True,
    )

    assert len(load_calls) == 1
    assert load_calls[0]["require_reference"] is False
    assert load_calls[0]["require_mag_inst"] is False
    assert load_calls[0]["limit"] is None
    assert diagnostic_preloaded == [shared_rows]
    assert result.measurement_row_read_strategy == "SHARED_SUPERSET_SINGLE_LOAD_V1"
    assert result.shared_measurement_rows == 1


def test_aperture_scalar_loader_uses_measurement_scalars_and_one_frame_lookup():
    measurement_rows = [
        SimpleNamespace(frame_id=101, object_id=1),
        SimpleNamespace(frame_id=101, object_id=2),
        SimpleNamespace(frame_id=102, object_id=1),
    ]
    frame_rows = [
        SimpleNamespace(frame_id=101, raw_path="a.fits"),
        SimpleNamespace(frame_id=102, raw_path="b.fits"),
    ]

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class FakeSession:
        def __init__(self):
            self.statements = []

        def execute(self, stmt):
            self.statements.append(stmt)
            if len(self.statements) == 1:
                return Result(measurement_rows)
            return Result(frame_rows)

    session = FakeSession()
    rows, frames = aperture_module._load_aperture_source_rows_scalar(
        session,
        source_calibration_version_id=42,
        frame_ids=[101, 102],
        instrument_unit_id=None,
    )

    assert rows == measurement_rows
    assert sorted(frames) == [101, 102]
    assert len(session.statements) == 2

    measurement_sql = str(session.statements[0])
    frame_sql = str(session.statements[1])
    assert "measurements.flags" in measurement_sql
    assert "measurements.mag_cal" not in measurement_sql
    assert "frames.raw_path" not in measurement_sql
    assert "frame_locations.locator AS raw_path" in frame_sql
    assert "frames.quality_flags" in frame_sql


def test_color_server_core_sql_is_narrow_and_defers_low_cardinality_joins():
    sql = color_module._build_color_server_core_sql(
        "WITH s0 AS MATERIALIZED (SELECT 1) SELECT 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21",
        require_mag_inst=False,
    )

    assert "INSERT INTO science_measurements" not in sql
    assert "AS r (reconstruction_calibration_version_id" in sql
    assert "JOIN measurements bm" in sql
    assert "bm.measurement_id = r.base_measurement_id" in sql
    assert "JOIN frames fr" in sql
    assert "FROM object_qc" not in sql
    assert "FROM reference_photometry" not in sql
    assert "LEFT JOIN LATERAL" not in sql
    assert "jsonb_strip_nulls(jsonb_build_object" in sql
    assert "'saturated', bm.flags -> 'saturated'" in sql
    assert "bm.flags AS measurement_flags" not in sql
    assert "ORDER BY r.object_id" not in sql


def test_color_normalized_loader_prefers_postgres_fused_compact_reconstruction(monkeypatch):
    source_cv = SimpleNamespace(calibration_version_id=77, name="ensemble")
    scalar_values = iter([source_cv, 0])

    class FakeSession:
        def scalar(self, _stmt):
            return next(scalar_values)

    expected = [{"measurement_id": 1, "object_id": 10, "frame_id": 100}]
    calls = []

    monkeypatch.setattr(color_module, "_session_dialect_name", lambda _session: "postgresql")

    def fake_compact(_session, **kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_compact_reconstruction",
        fake_compact,
    )
    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_server_reconstruction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("V3 fallback should not run")),
    )
    metadata = {}
    rows = color_module._load_color_measurement_rows(
        FakeSession(),
        calibration_name="ensemble",
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="v",
        series_id=8,
        limit=None,
        require_reference=False,
        require_mag_inst=False,
        prefer_server_normalized=True,
        _read_metadata=metadata,
    )

    assert rows == expected
    assert len(calls) == 1
    assert calls[0]["calibration_version"] is source_cv
    assert metadata["strategy"] == "SHARED_SUPERSET_POSTGRES_FUSED_COMPACT_RECONSTRUCTION_V4"


def test_color_normalized_loader_keeps_v3_server_fallback(monkeypatch):
    source_cv = SimpleNamespace(calibration_version_id=77, name="ensemble")
    scalar_values = iter([source_cv, 0])

    class FakeSession:
        def scalar(self, _stmt):
            return next(scalar_values)

    expected = [{"measurement_id": 1, "object_id": 10, "frame_id": 100}]
    monkeypatch.setattr(color_module, "_session_dialect_name", lambda _session: "postgresql")
    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_compact_reconstruction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_server_reconstruction",
        lambda *_args, **_kwargs: expected,
    )
    metadata = {}
    rows = color_module._load_color_measurement_rows(
        FakeSession(),
        calibration_name="ensemble",
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="v",
        series_id=8,
        limit=None,
        require_reference=False,
        require_mag_inst=False,
        prefer_server_normalized=True,
        _read_metadata=metadata,
    )

    assert rows == expected
    assert metadata["strategy"] == "SHARED_SUPERSET_POSTGRES_HYBRID_RECONSTRUCTION_V3"


def test_color_fused_compact_reconstruction_matches_stage_scalar_semantics():
    aperture_term = SimpleNamespace(
        frame_id=100,
        accepted=True,
        model_name="constant",
        coefficient_0=0.1,
        coefficient_x=0.0,
        coefficient_y=0.0,
        offset_mag=None,
        details={"image_width": 100, "image_height": 100},
    )
    zp_term = SimpleNamespace(frame_id=100, accepted=True, offset_mag=20.0)
    ensemble_term = SimpleNamespace(frame_id=100, accepted=True, offset_mag=0.05)
    rows = [
        {
            "measurement_id": 7,
            "object_id": 10,
            "frame_id": 100,
            "x": 50.0,
            "y": 50.0,
            "mag_inst": 10.0,
            "mag_err": 0.02,
            "mag_err_random": 0.02,
            "measurement_catalog_name": "identity",
            "source_id": "123",
            "saturated": False,
            "near_saturated": False,
            "saturated_or_near_saturated": False,
            "mjd": 60000.0,
            "date_obs": "2023-01-01T00:00:00",
            "instrument_unit_id": 5,
            "filter_name": "V",
        }
    ]

    out = color_module._fuse_compact_color_ensemble_rows(
        rows,
        aperture_terms={100: aperture_term},
        aperture_zp_terms={100: zp_term},
        ensemble_terms={100: ensemble_term},
        aperture_zp_catalog_name="identity",
        ensemble_catalog_name="identity",
        ensemble_prefer_mag_cal=True,
    )

    assert len(out) == 1
    row = out[0]
    assert row["mag_inst"] == pytest.approx(10.1)
    assert row["mag_cal"] == pytest.approx(30.05)
    assert row["mag_err"] == pytest.approx(0.02)
    assert row["mag_err_random"] == pytest.approx(0.02)
    assert row["measurement_flags"] == {
        "catalog_name": "identity",
        "source_id": "123",
        "saturated": False,
        "near_saturated": False,
        "saturated_or_near_saturated": False,
    }


def test_color_hybrid_object_qc_selection_preserves_lateral_preference_order():
    fallback = SimpleNamespace(
        calibration_version_id=None,
        instrument_unit_id=None,
        updated_at=None,
        object_qc_id=999,
    )
    calibration_only = SimpleNamespace(
        calibration_version_id=77,
        instrument_unit_id=None,
        updated_at=None,
        object_qc_id=1,
    )
    exact = SimpleNamespace(
        calibration_version_id=77,
        instrument_unit_id=5,
        updated_at=None,
        object_qc_id=2,
    )
    other_instrument = SimpleNamespace(
        calibration_version_id=77,
        instrument_unit_id=6,
        updated_at=None,
        object_qc_id=1000,
    )

    selected = color_module._best_color_object_qc(
        [fallback, calibration_only, exact, other_instrument],
        calibration_version_id=77,
        instrument_unit_id=5,
    )

    assert selected is exact


def test_color_normalized_loader_prefers_runner_analysis_snapshot(monkeypatch):
    source_cv = SimpleNamespace(calibration_version_id=77, name="ensemble")

    class FakeSession:
        def scalar(self, _stmt):
            return source_cv

    snapshot = SimpleNamespace(calibration_version_id=77, calibration_name="ensemble")
    expected = [{"measurement_id": 1, "object_id": 10, "frame_id": 100}]
    calls = []

    def fake_snapshot_loader(_session, **kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_analysis_snapshot",
        fake_snapshot_loader,
    )
    monkeypatch.setattr(
        color_module,
        "_load_color_measurement_rows_from_compact_reconstruction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("V4 compact fallback should not run")
        ),
    )
    metadata = {}
    rows = color_module._load_color_measurement_rows(
        FakeSession(),
        calibration_name="ensemble",
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="v",
        series_id=8,
        limit=None,
        require_reference=False,
        require_mag_inst=False,
        prefer_server_normalized=True,
        _read_metadata=metadata,
        _preloaded_analysis_snapshot=snapshot,
    )

    assert rows == expected
    assert calls[0]["analysis_snapshot"] is snapshot
    assert metadata["strategy"] == "SHARED_SUPERSET_RUNNER_ANALYSIS_SNAPSHOT_V5"


def test_color_analysis_snapshot_rebuilds_narrow_v4_core_contract(monkeypatch):
    measurement = SimpleNamespace(
        source_measurement_id=7,
        object_id=10,
        frame_id=100,
        mag_inst=15.1,
        mag_cal=14.9,
        mag_err_random=0.02,
        mag_err=0.03,
        flags={
            "catalog_name": "identity",
            "source_id": "123",
            "saturated": False,
            "near_saturated": True,
            "saturated_or_near_saturated": True,
            # Must not leak richer analysis/QC state into color source flags.
            "ensemble_object_is_reference": True,
        },
    )
    frame = SimpleNamespace(
        frame_id=100,
        mjd=60000.0,
        date_obs="2023-01-01T00:00:00",
        instrument_unit_id=5,
        filter_name="V",
    )
    snapshot = SimpleNamespace(
        calibration_version_id=77,
        calibration_name="ensemble",
        y_key="mag_cal",
        measurement_rows=(measurement,),
        frames_by_id={100: frame},
    )
    cv = SimpleNamespace(calibration_version_id=77, name="ensemble")

    class ScalarList:
        def all(self):
            return [100]

    class FakeSession:
        def scalars(self, _stmt):
            return ScalarList()

    captured = {}

    def fake_decorate(_session, rows, **kwargs):
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        return rows

    monkeypatch.setattr(color_module, "_decorate_color_server_core_rows", fake_decorate)
    rows = color_module._load_color_measurement_rows_from_analysis_snapshot(
        FakeSession(),
        analysis_snapshot=snapshot,
        calibration_version=cv,
        identity_catalog_name="identity",
        reference_catalog_name="reference",
        reference_band="v",
        series_id=8,
        limit=None,
        require_reference=False,
        require_mag_inst=False,
    )

    assert rows == captured["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["measurement_id"] == 7
    assert row["object_id"] == 10
    assert row["frame_id"] == 100
    assert row["mag_inst"] == pytest.approx(15.1)
    assert row["mag_cal"] == pytest.approx(14.9)
    assert row["mag_err_random"] == pytest.approx(0.02)
    assert row["mjd"] == pytest.approx(60000.0)
    assert row["date_obs"] == "2023-01-01T00:00:00"
    assert row["measurement_flags"] == {
        "catalog_name": "identity",
        "source_id": "123",
        "saturated": False,
        "near_saturated": True,
        "saturated_or_near_saturated": True,
    }


def test_runner_routes_preloaded_analysis_snapshot_to_color_only():
    runner_path = Path(__file__).resolve().parents[1] / "grandgousier" / "pipeline" / "runner.py"
    module_tree = ast.parse(runner_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in module_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_field_pipeline"
    )
    tree = function
    calls: dict[str, list[set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        calls.setdefault(node.func.id, []).append(
            {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        )

    aperture_calls = calls.get("apply_aperture_correction", [])
    color_calls = calls.get("apply_color_calibration", [])
    assert aperture_calls
    assert color_calls
    assert all(
        "_preloaded_analysis_snapshot" not in keywords
        for keywords in aperture_calls
    )
    assert any(
        "_preloaded_analysis_snapshot" in keywords
        for keywords in color_calls
    )
