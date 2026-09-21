from __future__ import annotations

from types import SimpleNamespace

from grandgousier.calibration.reconstruction import _RECONSTRUCTED_FIELDS, _apply_ensemble
from grandgousier.photometry.science_materialization import (
    ScienceMaterializationResult,
    _apply_shadow_comparison,
)


def _state(**overrides):
    row = {
        "measurement_id": 101,
        "base_measurement_id": 101,
        "object_id": 1,
        "frame_id": 2,
        "x": 10.0,
        "y": 11.0,
        "flux": 1000.0,
        "flux_err": 12.0,
        "mag_inst": 15.0,
        "mag_cal": 17.0,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.03,
        "aperture_radius_pix": 3.0,
        "local_background": 100.0,
        "local_zp_correction": 2.0,
        "centroid_dx_pix": 0.1,
        "centroid_dy_pix": -0.1,
        "flags": {"catalog_name": "cat"},
        "_base_flags": {"catalog_name": "cat"},
        "_base_calibration_version_id": 3,
    }
    row.update(overrides)
    return row


def _legacy_from_state(row):
    return SimpleNamespace(
        measurement_id=999,
        object_id=row["object_id"],
        frame_id=row["frame_id"],
        flags={"catalog_name": "cat", "legacy": True},
        **{field: row.get(field) for field in _RECONSTRUCTED_FIELDS},
    )


def test_science_shadow_comparison_accepts_numeric_tolerance() -> None:
    row = _state()
    legacy = _legacy_from_state(row)
    legacy.mag_err = row["mag_err"] + 1e-12
    result = ScienceMaterializationResult(
        calibration_version_id=7,
        calibration_name="science",
        base_calibration_version_id=3,
    )
    _apply_shadow_comparison(result, {(1, 2): row}, {(1, 2): legacy}, tolerance=1e-10)
    assert result.mismatches == 0
    assert result.max_abs_delta["mag_err"] > 0


def test_science_shadow_comparison_detects_structure_and_scalar_drift() -> None:
    row = _state()
    legacy = _legacy_from_state(row)
    legacy.mag_cal = 99.0
    result = ScienceMaterializationResult(
        calibration_version_id=7,
        calibration_name="science",
        base_calibration_version_id=3,
    )
    _apply_shadow_comparison(
        result,
        {(1, 2): row, (2, 3): _state(object_id=2, frame_id=3)},
        {(1, 2): legacy, (9, 9): _legacy_from_state(_state(object_id=9, frame_id=9))},
        tolerance=1e-10,
    )
    assert result.missing_legacy_rows == 1
    assert result.extra_legacy_rows == 1
    assert result.scalar_mismatches == 1
    assert result.mismatches == 3


def test_reconstruction_stages_preserve_base_measurement_identity() -> None:
    states = {(1, 2): _state()}
    term = SimpleNamespace(frame_id=2, accepted=True, offset_mag=0.25)
    out = _apply_ensemble(states, [term], {"catalog_name": "cat", "prefer_mag_cal": True})
    assert out[(1, 2)]["base_measurement_id"] == 101
    assert out[(1, 2)]["measurement_id"] == 101


def test_science_copy_tuple_matches_column_order_and_serializes_flags() -> None:
    import json

    from grandgousier.photometry.science_materialization import (
        _SCIENCE_WRITE_COLUMNS,
        _science_copy_tuple,
    )

    state = _state(
        flags={"catalog_name": "cat", "z": 2, "a": "µ"},
        _base_flags={"catalog_name": "cat"},
    )
    row = _science_copy_tuple(
        calibration_version_id=77,
        state=state,
        legacy_measurement=None,
        materialization_source="RECONSTRUCTED_NORMALIZED",
    )
    assert len(row) == len(_SCIENCE_WRITE_COLUMNS)
    mapped = dict(zip(_SCIENCE_WRITE_COLUMNS, row, strict=True))
    assert mapped["calibration_version_id"] == 77
    assert mapped["base_measurement_id"] == 101
    assert mapped["object_id"] == 1
    assert mapped["frame_id"] == 2
    assert json.loads(mapped["flags"]) == {
        "a": "µ",
        "science_base_calibration_version_id": 3,
        "science_calibration_version_id": 77,
        "science_provenance_normalized": True,
        "z": 2,
    }
    assert mapped["materialization_source"] == "RECONSTRUCTED_NORMALIZED"


def test_postgres_copy_writer_uses_temp_stage_copy_and_single_insert(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

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
            self.copy_sql = None
            self.copy_obj = FakeCopy()
            self.closed = False

        def execute(self, sql):
            self.executed.append(sql)

        def copy(self, sql):
            self.copy_sql = sql
            return self.copy_obj

        def close(self):
            self.closed = True

    cursor = FakeCursor()

    class FakeDriverConnection:
        def cursor(self):
            return cursor

    class FakeSAConnection:
        dialect = SimpleNamespace(name="postgresql")
        connection = SimpleNamespace(driver_connection=FakeDriverConnection())

    class FakeSession:
        def connection(self):
            return FakeSAConnection()

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="abc123"))
    states = {(1, 2): _state()}
    copy_sec, insert_sec, cleanup_sec = module._write_science_rows_via_postgres_copy(
        FakeSession(),
        calibration_version_id=77,
        ordered_keys=[(1, 2)],
        states=states,
        legacy={},
        materialization_source="RECONSTRUCTED_NORMALIZED",
    )

    assert copy_sec >= 0
    assert insert_sec >= 0
    assert cleanup_sec >= 0
    assert cursor.closed is True
    assert cursor.copy_sql is not None
    assert 'COPY "gg_science_stage_abc123"' in cursor.copy_sql
    assert len(cursor.copy_obj.rows) == 1
    assert cursor.executed[0].startswith('CREATE TEMP TABLE "gg_science_stage_abc123"')
    inserts = [sql for sql in cursor.executed if sql.startswith("INSERT INTO science_measurements")]
    assert len(inserts) == 1
    assert "SELECT" in inserts[0]
    assert cursor.executed[-1] == 'DROP TABLE "gg_science_stage_abc123"'


def test_science_materialization_result_reports_copy_timing_fields() -> None:
    result = ScienceMaterializationResult(
        calibration_version_id=7,
        calibration_name="science",
        base_calibration_version_id=3,
        write_strategy="POSTGRESQL_COPY_STAGING",
        reconstruction_sec=1.0,
        staging_copy_sec=2.0,
        insert_select_sec=3.0,
        finalize_sec=4.0,
        total_sec=10.0,
    )
    payload = result.to_dict()
    assert payload["write_strategy"] == "POSTGRESQL_COPY_STAGING"
    assert payload["reconstruction_sec"] == 1.0
    assert payload["staging_copy_sec"] == 2.0
    assert payload["insert_select_sec"] == 3.0
    assert payload["finalize_sec"] == 4.0
    assert payload["total_sec"] == 10.0


def test_postgres_server_reconstruction_builds_direct_insert_select() -> None:
    from grandgousier.photometry.science_materialization import (
        _ServerReconstructionPlan,
        _ServerReconstructionStage,
        _build_postgres_server_reconstruction_sql,
    )

    plan = _ServerReconstructionPlan(
        target_calibration_version_id=50,
        target_calibration_name="science",
        base_calibration_version_id=10,
        frame_ids=(2, 1),
        stages=(
            _ServerReconstructionStage(11, "APERTURE_CORRECTION", 101, {}, {}),
            _ServerReconstructionStage(
                12, "APERTURE_ZP", 102, {"catalog_name": "cat"}, {}
            ),
            _ServerReconstructionStage(
                13,
                "ENSEMBLE",
                103,
                {"catalog_name": "cat", "prefer_mag_cal": True},
                {"residual_noise_floor_mag": 0.01},
            ),
            _ServerReconstructionStage(
                14,
                "COLOR_CALIBRATION",
                104,
                {
                    "identity_catalog_name": "cat",
                    "reference_catalog_name": "ps1",
                    "reference_band": "g",
                    "color_index": "g-r",
                },
                {"transform_offset": 1.2, "color_term": 0.1, "color0": 0.5},
            ),
        ),
    )

    sql, params = _build_postgres_server_reconstruction_sql(plan)

    assert "INSERT INTO science_measurements" in sql
    assert "COPY " not in sql
    assert "m.frame_id IN (1,2)" in sql
    assert "calibration_frame_terms ft_src" in sql
    assert "calibration_iteration_objects io_src" in sql
    assert "io4_lookup AS MATERIALIZED (" not in sql
    assert "io4_map AS MATERIALIZED (" in sql
    assert "jsonb_object_agg(io_src.object_id::text, io_src.details)" in sql
    assert "io_src.calibration_iteration_id = 104" in sql
    assert "CROSS JOIN LATERAL jsonb_to_record(" in sql
    assert "io4_map.by_object -> (s3.object_id::text)" in sql
    assert "LIMIT 1" not in sql
    assert "s0 AS MATERIALIZED (" in sql
    assert "ft1_map AS MATERIALIZED (" in sql
    assert "ft2_map AS MATERIALIZED (" in sql
    assert "ft3_map AS MATERIALIZED (" in sql
    assert "s1 AS NOT MATERIALIZED (" in sql
    assert "s2 AS NOT MATERIALIZED (" in sql
    assert "s3 AS NOT MATERIALIZED (" in sql
    assert "s4 AS NOT MATERIALIZED (" in sql
    assert "ft_src.term_kind = 'APERTURE_CORRECTION'" in sql
    assert "ft_src.term_kind = 'APERTURE_ZP'" in sql
    assert "ft_src.term_kind = 'ENSEMBLE_OFFSET'" in sql
    assert "JOIN calibration_frame_terms ft" not in sql
    assert "ft1_map.by_frame -> (s0.frame_id::text)" in sql
    assert "ft2_map.by_frame -> (s1.frame_id::text)" in sql
    assert "ft3_map.by_frame -> (s2.frame_id::text)" in sql
    assert "science_provenance_normalized" in sql
    assert "residual_noise_floor_applied" in sql
    assert params["target_calibration_version_id"] == 50
    assert params["base_calibration_version_id"] == 10
    assert params["materialization_source"] == "POSTGRESQL_SERVER_RECONSTRUCTION_V7_MAP"
    assert len(params["materialization_source"]) <= 48
    assert "s4.flags || s4.science_extra_flags" not in sql
    assert "s4.science_extra_flags || jsonb_build_object" in sql
    assert params["catalog_name_2"] == "cat"
    assert params["catalog_name_3"] == "cat"
    assert params["identity_catalog_4"] == "cat"


def test_postgres_server_reconstruction_supports_ensemble_science_target() -> None:
    from grandgousier.photometry.science_materialization import (
        _ServerReconstructionPlan,
        _ServerReconstructionStage,
        _build_postgres_server_reconstruction_sql,
    )

    plan = _ServerReconstructionPlan(
        target_calibration_version_id=13,
        target_calibration_name="ensemble-science",
        base_calibration_version_id=10,
        frame_ids=(),
        stages=(
            _ServerReconstructionStage(11, "APERTURE_CORRECTION", 101, {}, {}),
            _ServerReconstructionStage(12, "APERTURE_ZP", 102, {}, {}),
            _ServerReconstructionStage(
                13, "ENSEMBLE", 103, {"prefer_mag_cal": True}, {}
            ),
        ),
    )

    sql, _params = _build_postgres_server_reconstruction_sql(plan)
    assert "FROM s3" in sql
    assert "calibration_iteration_objects io" not in sql
    assert "POSTGRESQL_SERVER_RECONSTRUCTION_V1" not in sql  # bound parameter, not SQL literal


def test_postgres_server_reconstruction_uses_compact_frame_maps_without_wide_stage_spills() -> None:
    from grandgousier.photometry.science_materialization import (
        _ServerReconstructionPlan,
        _ServerReconstructionStage,
        _build_postgres_server_reconstruction_sql,
    )

    plan = _ServerReconstructionPlan(
        target_calibration_version_id=13,
        target_calibration_name="ensemble-science",
        base_calibration_version_id=10,
        frame_ids=(1,),
        stages=(
            _ServerReconstructionStage(11, "APERTURE_CORRECTION", 101, {}, {}),
            _ServerReconstructionStage(12, "APERTURE_ZP", 102, {"catalog_name": "cat"}, {}),
            _ServerReconstructionStage(13, "ENSEMBLE", 103, {"catalog_name": "cat"}, {}),
        ),
    )

    sql, params = _build_postgres_server_reconstruction_sql(plan)

    # BASE is the only 50k-row materialization.  The three normalized frame
    # stages are inline expressions backed by tiny one-row JSONB frame maps.
    assert "s0 AS MATERIALIZED (" in sql
    assert "s1 AS NOT MATERIALIZED (" in sql
    assert "s2 AS NOT MATERIALIZED (" in sql
    assert "s3 AS NOT MATERIALIZED (" in sql
    assert "jsonb_object_agg(" in sql
    assert sql.count("AS by_frame") == 3
    assert sql.count("CROSS JOIN LATERAL jsonb_to_record(") == 3
    assert "JOIN calibration_frame_terms ft" not in sql
    assert params["materialization_source"] == "POSTGRESQL_SERVER_RECONSTRUCTION_V7_MAP"


def test_science_materialization_result_reports_server_reconstruction_strategy() -> None:
    result = ScienceMaterializationResult(
        calibration_version_id=7,
        calibration_name="science",
        base_calibration_version_id=3,
        write_strategy="POSTGRESQL_SERVER_INSERT_SELECT",
        reconstruction_strategy="POSTGRESQL_CTE_FRAME_MAP_COLOR_MAP_INLINE_V7",
        science_flags_strategy="COMPACT_NORMALIZED_SUMMARY_V2",
    )
    payload = result.to_dict()
    assert payload["write_strategy"] == "POSTGRESQL_SERVER_INSERT_SELECT"
    assert payload["reconstruction_strategy"] == "POSTGRESQL_CTE_FRAME_MAP_COLOR_MAP_INLINE_V7"
    assert payload["science_flags_strategy"] == "COMPACT_NORMALIZED_SUMMARY_V2"


def test_materialize_science_prefers_postgres_server_reconstruction_when_not_shadowing(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    plan = module._ServerReconstructionPlan(
        target_calibration_version_id=50,
        target_calibration_name="science",
        base_calibration_version_id=10,
        stages=(
            module._ServerReconstructionStage(11, "ENSEMBLE", 101, {}, {}),
        ),
        frame_ids=(),
    )
    expected = ScienceMaterializationResult(
        calibration_version_id=50,
        calibration_name="science",
        base_calibration_version_id=10,
        status="OK",
        write_strategy="POSTGRESQL_SERVER_INSERT_SELECT",
    )

    monkeypatch.setattr(module, "_postgres_server_reconstruction_capable", lambda session: True)
    monkeypatch.setattr(module, "_resolve_cv", lambda *args, **kwargs: SimpleNamespace(calibration_version_id=50, name="science"))
    monkeypatch.setattr(module, "_build_server_reconstruction_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(module, "_materialize_science_via_postgres_server_reconstruction", lambda *args, **kwargs: expected)
    monkeypatch.setattr(
        module,
        "reconstruct_calibration_measurement_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Python reconstruction should not run")),
    )

    result = module.materialize_science_calibration(
        SimpleNamespace(), calibration_name="science", shadow_compare_legacy=False
    )
    assert result is expected



def test_postgres_server_reconstruction_casts_jsonb_bound_parameters() -> None:
    """jsonb_build_object(any) cannot infer psycopg bind types without casts.

    In particular nullable diagnostics such as residual_independent_floor must be
    explicitly typed or PostgreSQL raises IndeterminateDatatype before execution.
    """
    from grandgousier.photometry.science_materialization import (
        _ServerReconstructionPlan,
        _ServerReconstructionStage,
        _build_postgres_server_reconstruction_sql,
    )

    plan = _ServerReconstructionPlan(
        target_calibration_version_id=50,
        target_calibration_name="science",
        base_calibration_version_id=10,
        frame_ids=(1,),
        stages=(
            _ServerReconstructionStage(11, "APERTURE_CORRECTION", 101, {}, {}),
            _ServerReconstructionStage(12, "APERTURE_ZP", 102, {"catalog_name": "cat"}, {}),
            _ServerReconstructionStage(
                13,
                "ENSEMBLE",
                103,
                {"catalog_name": "cat", "prefer_mag_cal": True},
                {
                    "residual_noise_floor_mag": 0.01,
                    "residual_noise_floor_algorithm": "algo",
                    "residual_noise_floor_error_basis": "physical_detector_error",
                    "residual_noise_frame_common_sigma_mag": None,
                    "residual_noise_frame_common_supported": False,
                    "residual_noise_frame_common_support_status": "NOT_SUPPORTED",
                    "residual_noise_floor_after_frame_common_mag": 0.008,
                    "residual_noise_independent_floor_mag": None,
                },
            ),
            _ServerReconstructionStage(
                14,
                "COLOR_CALIBRATION",
                104,
                {
                    "identity_catalog_name": "cat",
                    "reference_catalog_name": "ps1",
                    "reference_band": "v",
                    "color_index": "g_r",
                },
                {
                    "transform_offset": 0.4,
                    "color_term": -0.1,
                    "color0": 0.9,
                    "fitted_color_term": -0.1,
                    "fitted_color_term_err": None,
                    "transform_offset_err": None,
                    "color_term_err": None,
                    "transform_residual_robust_sigma": None,
                },
            ),
        ),
    )

    sql, params = _build_postgres_server_reconstruction_sql(plan)

    assert "'reference_catalog_name', CAST(:reference_catalog_4 AS text)" in sql
    assert "'reference_band', CAST(:reference_band_4 AS text)" in sql
    assert "'reference_color_index', CAST(:color_index_4 AS text)" in sql
    assert "'reference_fitted_color_term_error', CAST(:fitted_color_term_err_4 AS double precision)" in sql
    assert "'reference_transform_systematic_floor', CAST(:transform_floor_4 AS double precision)" in sql
    assert "'residual_noise_floor_algorithm', CAST(:residual_floor_algorithm AS text)" in sql
    assert "'residual_noise_frame_common_supported', CAST(:residual_frame_common_supported AS boolean)" in sql
    assert "'residual_noise_independent_floor_mag', CAST(:residual_independent_floor AS double precision)" in sql
    assert "'science_base_calibration_version_id', CAST(:base_calibration_version_id AS bigint)" in sql
    assert params["fitted_color_term_err_4"] is None
    assert params["transform_floor_4"] is None
    assert params["residual_independent_floor"] is None


def test_effective_science_flags_merge_base_and_compact_science_payload() -> None:
    from grandgousier.photometry.science_flags import merge_effective_science_flags

    merged = merge_effective_science_flags(
        {
            "catalog_name": "gaia",
            "source_id": "42",
            "random_error_physical_mag_err": 0.012,
            "shared": "base",
        },
        {
            "reference_catalog_name": "ps1",
            "science_provenance_normalized": True,
            "shared": "science",
        },
    )

    assert merged["catalog_name"] == "gaia"
    assert merged["source_id"] == "42"
    assert merged["random_error_physical_mag_err"] == 0.012
    assert merged["reference_catalog_name"] == "ps1"
    assert merged["science_provenance_normalized"] is True
    assert merged["shared"] == "science"


def test_compact_science_flags_against_base_preserves_effective_overlay() -> None:
    from grandgousier.photometry.science_flags import (
        compact_science_flags_against_base,
        merge_effective_science_flags,
    )

    base = {
        "catalog_name": "gaia",
        "same": 1,
        "changed": "base",
        "nested": {"a": 1},
    }
    effective = {
        "catalog_name": "gaia",
        "same": 1,
        "changed": "science",
        "nested": {"a": 1},
        "explicit_none": None,
        "new": [1, 2],
    }

    compact = compact_science_flags_against_base(base, effective)

    assert compact == {
        "changed": "science",
        "explicit_none": None,
        "new": [1, 2],
    }
    assert merge_effective_science_flags(base, compact) == effective


def test_compact_science_flags_rejects_missing_base_key() -> None:
    import pytest

    from grandgousier.photometry.science_flags import compact_science_flags_against_base

    with pytest.raises(ValueError, match="retain every BASE key"):
        compact_science_flags_against_base(
            {"base_a": 1, "base_b": 2},
            {"base_a": 1, "science_only": 3},
        )


def test_python_science_row_does_not_copy_legacy_or_unchanged_base_flags() -> None:
    from grandgousier.photometry.science_materialization import _science_row_dict

    state = _state(
        flags={
            "catalog_name": "cat",
            "base_only": 5,
            "changed": "science",
            "science_specific": 12,
        },
        _base_flags={
            "catalog_name": "cat",
            "base_only": 5,
            "changed": "base",
        },
    )
    legacy = _legacy_from_state(state)
    legacy.flags = {"legacy_payload": "must-not-be-copied"}

    row = _science_row_dict(
        calibration_version_id=7,
        state=state,
        legacy_measurement=legacy,
        materialization_source="RECONSTRUCTED_NORMALIZED",
    )

    assert row["source_measurement_id"] == 999
    assert row["flags"] == {
        "changed": "science",
        "science_specific": 12,
        "science_provenance_normalized": True,
        "science_base_calibration_version_id": 3,
        "science_calibration_version_id": 7,
    }


def test_effective_science_flags_accepts_missing_base_for_legacy_rows() -> None:
    from grandgousier.photometry.science_flags import merge_effective_science_flags

    assert merge_effective_science_flags(None, {"legacy": True}) == {"legacy": True}


def test_server_reconstruction_select_sql_keeps_ctes_and_removes_insert() -> None:
    from grandgousier.photometry.science_materialization import (
        _server_reconstruction_select_sql,
    )

    insert_sql = "WITH s0 AS (SELECT 1 AS x) INSERT INTO science_measurements (object_id) SELECT x FROM s0"
    select_sql = _server_reconstruction_select_sql(insert_sql)

    assert select_sql == "WITH s0 AS (SELECT 1 AS x) SELECT x FROM s0"
    assert "INSERT INTO science_measurements" not in select_sql


def test_explain_summary_exposes_buffers_wal_and_triggers() -> None:
    from grandgousier.photometry.science_materialization import _summarize_explain_json

    payload = [
        {
            "Plan": {
                "Node Type": "ModifyTable",
                "Operation": "Insert",
                "Actual Total Time": 30.0,
                "Actual Rows": 0,
                "Actual Loops": 1,
                "Shared Hit Blocks": 100,
                "Shared Read Blocks": 20,
                "Shared Dirtied Blocks": 50,
                "Shared Written Blocks": 10,
                "WAL Records": 500,
                "WAL FPI": 3,
                "WAL Bytes": 123456,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "measurements",
                        "Actual Total Time": 8.0,
                        "Actual Rows": 50565,
                        "Actual Loops": 1,
                        "Shared Hit Blocks": 80,
                    }
                ],
            },
            "Planning Time": 1.2,
            "Execution Time": 31.5,
            "Triggers": [
                {"Trigger Name": "RI_ConstraintTrigger", "Time": 4.2, "Calls": 50565}
            ],
        }
    ]

    summary = _summarize_explain_json(payload)
    assert summary["planning_time_ms"] == 1.2
    assert summary["execution_time_ms"] == 31.5
    assert summary["root"]["Shared Read Blocks"] == 20
    assert summary["root"]["WAL Bytes"] == 123456
    assert summary["top_nodes"][0]["Node Type"] == "ModifyTable"
    assert summary["triggers"][0]["Calls"] == 50565


def test_science_materialization_result_reports_diagnostics() -> None:
    result = ScienceMaterializationResult(
        calibration_version_id=7,
        calibration_name="science",
        base_calibration_version_id=3,
        diagnostics_enabled=True,
        diagnostics_sec=12.5,
        diagnostics={"mode": "EXPLAIN_ANALYZE_BUFFERS_WAL_V1"},
    )
    payload = result.to_dict()
    assert payload["diagnostics_enabled"] is True
    assert payload["diagnostics_sec"] == 12.5
    assert payload["diagnostics"]["mode"] == "EXPLAIN_ANALYZE_BUFFERS_WAL_V1"


def test_materialize_science_forwards_server_diagnostics_flag(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    plan = module._ServerReconstructionPlan(
        target_calibration_version_id=50,
        target_calibration_name="science",
        base_calibration_version_id=10,
        stages=(module._ServerReconstructionStage(11, "ENSEMBLE", 101, {}, {}),),
        frame_ids=(),
    )
    expected = ScienceMaterializationResult(
        calibration_version_id=50,
        calibration_name="science",
        base_calibration_version_id=10,
        status="OK",
    )
    captured = {}

    monkeypatch.setattr(module, "_postgres_server_reconstruction_capable", lambda session: True)
    monkeypatch.setattr(
        module,
        "_resolve_cv",
        lambda *args, **kwargs: SimpleNamespace(calibration_version_id=50, name="science"),
    )
    monkeypatch.setattr(module, "_build_server_reconstruction_plan", lambda *args, **kwargs: plan)

    def fake_materialize(*args, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(module, "_materialize_science_via_postgres_server_reconstruction", fake_materialize)
    result = module.materialize_science_calibration(
        SimpleNamespace(),
        calibration_name="science",
        shadow_compare_legacy=False,
        diagnostics=True,
    )
    assert result is expected
    assert captured["diagnostics"] is True


def test_postgres_server_plan_falls_back_for_photometric_frame_qc_iteration(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    base = SimpleNamespace(calibration_version_id=10, product_kind="BASE_PHOTOMETRY")
    target = SimpleNamespace(
        calibration_version_id=11,
        product_kind="ENSEMBLE",
        name="science_photqc",
    )
    iteration = SimpleNamespace(
        calibration_iteration_id=101,
        iteration_kind="PHOTOMETRIC_FRAME_QC",
        parameters={},
        metrics={},
    )
    monkeypatch.setattr(module, "_chain_from_base", lambda *args, **kwargs: [base, target])
    monkeypatch.setattr(module, "_latest_iteration", lambda *args, **kwargs: iteration)

    plan = module._build_server_reconstruction_plan(
        SimpleNamespace(), target_cv=target, frame_ids=[1, 2]
    )

    assert plan is None


def test_postgres_server_plan_falls_back_for_spatial_ensemble_iteration(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    base = SimpleNamespace(calibration_version_id=10, product_kind="BASE_PHOTOMETRY")
    target = SimpleNamespace(
        calibration_version_id=11,
        product_kind="ENSEMBLE",
        name="science_spatial",
    )
    iteration = SimpleNamespace(
        calibration_iteration_id=101,
        iteration_kind="SPATIAL_ENSEMBLE",
        parameters={},
        metrics={},
    )
    monkeypatch.setattr(module, "_chain_from_base", lambda *args, **kwargs: [base, target])
    monkeypatch.setattr(module, "_latest_iteration", lambda *args, **kwargs: iteration)

    plan = module._build_server_reconstruction_plan(
        SimpleNamespace(), target_cv=target, frame_ids=[1, 2]
    )

    assert plan is None


def test_empty_science_reconstruction_is_not_reported_as_ok() -> None:
    import pytest
    from grandgousier.photometry import science_materialization as module

    class FakeSession:
        def scalar(self, statement):
            return 58000

    with pytest.raises(ValueError, match="reconstructed zero rows"):
        module._raise_if_empty_science_reconstruction(
            FakeSession(),
            base_calibration_version_id=987,
            frame_ids=[1, 2],
            reconstructed_rows=0,
            calibration_name="science_photqc",
        )


def test_shadow_baseline_prefers_target_legacy_rows(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    legacy = {(1, 2): _legacy_from_state(_state())}
    monkeypatch.setattr(
        module,
        "_load_existing_science_measurements",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("existing science must not be loaded when legacy target exists")
        ),
    )

    baseline, source = module._resolve_shadow_comparison_baseline(
        SimpleNamespace(),
        calibration_version_id=7,
        frame_ids=None,
        legacy=legacy,
    )

    assert baseline is legacy
    assert source == "LEGACY_MEASUREMENTS"


def test_shadow_baseline_falls_back_to_existing_science_when_target_legacy_is_empty(
    monkeypatch,
) -> None:
    from grandgousier.photometry import science_materialization as module

    science = {(1, 2): SimpleNamespace(object_id=1, frame_id=2)}
    captured = {}

    def fake_load(session, *, calibration_version_id, frame_ids=None):
        captured["calibration_version_id"] = calibration_version_id
        captured["frame_ids"] = frame_ids
        return science

    monkeypatch.setattr(module, "_load_existing_science_measurements", fake_load)

    baseline, source = module._resolve_shadow_comparison_baseline(
        SimpleNamespace(),
        calibration_version_id=1094,
        frame_ids=[10, 11],
        legacy={},
    )

    assert baseline is science
    assert source == "EXISTING_SCIENCE_MEASUREMENTS"
    assert captured == {"calibration_version_id": 1094, "frame_ids": [10, 11]}


def test_shadow_baseline_reports_none_when_no_comparison_product_exists(monkeypatch) -> None:
    from grandgousier.photometry import science_materialization as module

    monkeypatch.setattr(
        module,
        "_load_existing_science_measurements",
        lambda *args, **kwargs: {},
    )

    baseline, source = module._resolve_shadow_comparison_baseline(
        SimpleNamespace(),
        calibration_version_id=7,
        frame_ids=None,
        legacy={},
    )

    assert baseline == {}
    assert source == "NONE"


def test_science_materialization_result_reports_shadow_baseline() -> None:
    result = ScienceMaterializationResult(
        calibration_version_id=1094,
        calibration_name="science",
        base_calibration_version_id=1088,
        shadow_checked=True,
        shadow_comparison_source="EXISTING_SCIENCE_MEASUREMENTS",
        shadow_baseline_rows=222000,
    )

    payload = result.to_dict()
    assert payload["shadow_checked"] is True
    assert payload["shadow_comparison_source"] == "EXISTING_SCIENCE_MEASUREMENTS"
    assert payload["shadow_baseline_rows"] == 222000


def test_shadow_existing_science_flags_are_preserved_exactly_before_compaction() -> None:
    from grandgousier.photometry import science_materialization as module
    from grandgousier.photometry.science_flags import merge_effective_science_flags

    state = _state(
        flags={
            "catalog_name": "cat",
            "base_only": 5,
            "reconstructed_only": "new-reconstruction-value",
        },
        _base_flags={
            "catalog_name": "cat",
            "base_only": 5,
            "changed": "base",
        },
    )
    baseline = SimpleNamespace(
        object_id=1,
        frame_id=2,
        base_measurement_id=101,
        flags={
            "catalog_name": "cat",
            "base_only": 5,
            "changed": "historical-science",
            "historical_only": {"v": 1},
        },
    )

    module._attach_shadow_effective_flags(
        {(1, 2): state},
        {(1, 2): baseline},
        shadow_source="EXISTING_SCIENCE_MEASUREMENTS",
    )
    row = module._science_row_dict(
        calibration_version_id=7,
        state=state,
        legacy_measurement=None,
        materialization_source="RECONSTRUCTED_NORMALIZED_SHADOW",
    )

    expected_effective = merge_effective_science_flags(
        state["_base_flags"], baseline.flags
    )
    actual_effective = merge_effective_science_flags(
        state["_base_flags"], row["flags"]
    )
    assert actual_effective == expected_effective
    assert "reconstructed_only" not in actual_effective
    assert "science_provenance_normalized" not in actual_effective
    assert row["flags"] == {
        "changed": "historical-science",
        "historical_only": {"v": 1},
    }


def test_shadow_legacy_flags_are_preserved_exactly_before_compaction() -> None:
    from grandgousier.photometry import science_materialization as module
    from grandgousier.photometry.science_flags import merge_effective_science_flags

    state = _state(
        _base_flags={"catalog_name": "cat", "same": 1, "changed": "base"},
        flags={"catalog_name": "cat", "same": 1, "changed": "reconstructed"},
    )
    legacy = _legacy_from_state(state)
    legacy.flags = {
        "catalog_name": "cat",
        "same": 1,
        "changed": "legacy",
        "legacy_only": True,
    }

    module._attach_shadow_effective_flags(
        {(1, 2): state},
        {(1, 2): legacy},
        shadow_source="LEGACY_MEASUREMENTS",
    )
    row = module._science_row_dict(
        calibration_version_id=7,
        state=state,
        legacy_measurement=legacy,
        materialization_source="RECONSTRUCTED_NORMALIZED_SHADOW",
    )

    assert merge_effective_science_flags(state["_base_flags"], row["flags"]) == legacy.flags
    assert row["flags"] == {"changed": "legacy", "legacy_only": True}


def test_shadow_existing_science_rejects_base_identity_drift() -> None:
    import pytest
    from grandgousier.photometry import science_materialization as module

    state = _state(base_measurement_id=101)
    baseline = SimpleNamespace(
        object_id=1,
        frame_id=2,
        base_measurement_id=999,
        flags={},
    )

    with pytest.raises(ValueError, match="BASE identity differs"):
        module._attach_shadow_effective_flags(
            {(1, 2): state},
            {(1, 2): baseline},
            shadow_source="EXISTING_SCIENCE_MEASUREMENTS",
        )
