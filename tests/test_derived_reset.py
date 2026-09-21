from __future__ import annotations

from grandgousier.db.derived_reset import (
    PRESERVED_TABLES,
    STAGE2_DERIVED_TABLES,
    audit_stage2_reset,
    reset_stage2_derived_data,
)


class _ScalarResult:
    def __init__(self, value=0, *, rowcount=0):
        self._value = value
        self.rowcount = rowcount

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self):
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        normalized = " ".join(sql.split())
        if "SELECT COUNT(DISTINCT fsf.frame_id)::bigint" in normalized:
            return _ScalarResult(66)
        if normalized == "SELECT COUNT(*)::bigint FROM field_series":
            return _ScalarResult(4)
        if normalized == "SELECT COUNT(*)::bigint FROM fields":
            return _ScalarResult(2)
        if normalized.startswith("SELECT COUNT(*)::bigint FROM"):
            return _ScalarResult(7)
        if normalized.startswith("TRUNCATE TABLE"):
            return _ScalarResult(0)
        if normalized.startswith("UPDATE frames fr"):
            return _ScalarResult(0, rowcount=66)
        if normalized.startswith("UPDATE field_series"):
            return _ScalarResult(0, rowcount=4)
        if normalized.startswith("UPDATE fields"):
            return _ScalarResult(0, rowcount=2)
        raise AssertionError(f"Unexpected SQL: {normalized}")


def test_stage2_reset_boundary_is_explicit_and_preserves_identity_tables() -> None:
    assert "measurements" in STAGE2_DERIVED_TABLES
    assert "science_photometry" in STAGE2_DERIVED_TABLES
    assert "science_measurements" not in STAGE2_DERIVED_TABLES
    assert "calibration_versions" in STAGE2_DERIVED_TABLES
    assert "frame_catalog_links" in STAGE2_DERIVED_TABLES
    assert "pipeline_runs" in STAGE2_DERIVED_TABLES
    assert "batch_runs" in STAGE2_DERIVED_TABLES
    assert "interseries_calibration_edges" in STAGE2_DERIVED_TABLES
    assert "interseries_calibration_series" in STAGE2_DERIVED_TABLES
    assert "interseries_calibration_generations" in STAGE2_DERIVED_TABLES
    assert "interseries_calibration_offsets" not in STAGE2_DERIVED_TABLES

    assert "frames" in PRESERVED_TABLES
    assert "observations" in PRESERVED_TABLES
    assert "field_series" in PRESERVED_TABLES
    assert "field_series_frames" in PRESERVED_TABLES
    assert "catalog_sources" in PRESERVED_TABLES
    assert "objects" in PRESERVED_TABLES
    assert "schema_migrations" in PRESERVED_TABLES

    assert set(STAGE2_DERIVED_TABLES).isdisjoint(PRESERVED_TABLES)


def test_stage2_reset_audit_is_read_only() -> None:
    session = _FakeSession()
    audit = audit_stage2_reset(session)

    assert audit.total_derived_rows == 7 * len(STAGE2_DERIVED_TABLES)
    assert audit.n_frames_to_reset == 66
    assert audit.n_series_to_reset == 4
    assert audit.n_fields_to_reset == 2
    assert not any("TRUNCATE TABLE" in sql for sql in session.sql)
    assert not any("UPDATE frames" in sql for sql in session.sql)


def test_stage2_reset_uses_transactional_truncate_without_identity_restart_or_cascade() -> None:
    session = _FakeSession()
    result = reset_stage2_derived_data(session)

    truncate = next(sql for sql in session.sql if "TRUNCATE TABLE" in sql)
    for table_name in STAGE2_DERIVED_TABLES:
        assert table_name in truncate
    assert "CONTINUE IDENTITY" in truncate
    assert "RESTART IDENTITY" not in truncate
    assert "CASCADE" not in truncate

    frame_update = next(sql for sql in session.sql if "UPDATE frames fr" in sql)
    assert "processing_status = 'HEADER_READ'" in frame_update
    assert "wcs_status = 'NOT_CHECKED'" in frame_update
    assert "frame_pre_photometry_qc_v1" in frame_update
    assert "detection_catalog_qc" in frame_update

    series_update = next(sql for sql in session.sql if "UPDATE field_series" in sql)
    assert "status = 'READY'" in series_update
    assert "last_pipeline_run_id" in series_update

    assert result.executed is True
    assert result.frames_reset == 66
    assert result.series_reset == 4
    assert result.fields_reset == 2


def test_reset_derived_data_cli_requires_explicit_scope_and_all_series() -> None:
    import pytest

    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "reset-derived-data",
            "--scope",
            "stage2",
            "--all-series",
            "--execute",
            "--yes",
            "--format",
            "json",
        ]
    )
    assert args.scope == "stage2"
    assert args.all_series is True
    assert args.execute is True
    assert args.yes is True
    assert args.format == "json"


def test_no_preserved_table_has_foreign_key_to_a_truncated_table() -> None:
    from grandgousier.db.models import Base

    derived = set(STAGE2_DERIVED_TABLES)
    offenders = []
    for table in Base.metadata.tables.values():
        if table.name in derived:
            continue
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table.name in derived:
                offenders.append(
                    (table.name, foreign_key.parent.name, foreign_key.column.table.name)
                )
    assert offenders == []
