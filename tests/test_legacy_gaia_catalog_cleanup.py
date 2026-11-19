from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from grandgousier.cli import build_parser, cmd_cleanup_legacy_gaia_catalogs
from grandgousier.db.legacy_gaia_catalog_cleanup import (
    DatabaseIdentity,
    IncomingForeignKey,
    LegacyGaiaCatalogCleanupError,
    _assert_apply_target,
    _database_identity,
    _delete_frame_links_sql,
    _delete_sources_sql,
    _durable_keep_condition,
    audit_legacy_gaia_catalogs,
    classify_source_disposition,
    is_legacy_catalog_name,
    validate_apply_acknowledgements,
)


def _identity(
    *, name: str = "grandgousier_test", protected_reasons: tuple[str, ...] = ()
) -> DatabaseIdentity:
    return DatabaseIdentity(
        database_name=name,
        database_oid=1234,
        db_user="tester",
        server_address="127.0.0.1",
        server_port=5432,
        server_version="PostgreSQL 18.0",
        server_version_num=180000,
        current_schema="public",
        configured_host="localhost",
        database_allows_connections=True,
        database_is_template=False,
        protected_reasons=protected_reasons,
    )


def test_legacy_catalog_prefix_is_literal_and_excludes_canonical_catalog() -> None:
    assert is_legacy_catalog_name("auto_catalog_field_7")
    assert is_legacy_catalog_name("auto_catalog_field_north")
    assert not is_legacy_catalog_name("autoXcatalogXfieldX7")
    assert not is_legacy_catalog_name("gaia_dr3")


@pytest.mark.parametrize(
    "reason",
    [
        "OBJECT_CATALOG_LINK",
        "ACCEPTED_DETECTION_CATALOG_MATCH",
        "REFERENCE_IDENTITY_LINK",
        "EXPLICIT_TARGET",
    ],
)
def test_durable_reasons_keep_source_but_frame_projection_is_not_a_keep_reason(reason: str) -> None:
    disposition = classify_source_disposition(
        durable_keep_reasons=[reason],
        incoming_fk_counts={"FRAME_PROJECTION": 4},
    )
    assert disposition.durable_keep
    assert not disposition.removable_frame_projection
    assert not disposition.removable_source


def test_unreferenced_frame_projection_is_removable_and_does_not_keep_source() -> None:
    disposition = classify_source_disposition(
        durable_keep_reasons=[], incoming_fk_counts={"FRAME_PROJECTION": 4}
    )
    assert not disposition.durable_keep
    assert disposition.removable_frame_projection
    assert disposition.removable_source


def test_nondurable_fk_reference_blocks_source_delete_but_not_frame_projection() -> None:
    disposition = classify_source_disposition(
        durable_keep_reasons=[],
        incoming_fk_counts={"FRAME_PROJECTION": 2, "DETECTION_MATCH": 1},
    )
    assert not disposition.durable_keep
    assert disposition.removable_frame_projection
    assert not disposition.removable_source
    assert disposition.blockers == ("DETECTION_MATCH",)


def test_unknown_foreign_key_blocks_source_delete_even_without_known_link_counts() -> None:
    disposition = classify_source_disposition(
        durable_keep_reasons=[], incoming_fk_counts={}, unknown_fk_present=True
    )
    assert not disposition.removable_source
    assert disposition.blockers == ("UNKNOWN_FOREIGN_KEY",)


def test_unknown_incoming_fk_is_reported_as_a_blocker_even_when_count_is_unknown() -> None:
    fk = IncomingForeignKey(
        target_table="catalog_sources",
        target_schema="public",
        child_schema="other_schema",
        child_table="user_relation",
        constraint_name="fk_user_relation_catalog_source",
        child_columns=("catalog_source_id",),
        target_columns=("catalog_source_id",),
        on_delete="RESTRICT",
        validated=False,
    )
    report = fk.to_dict(expected_schema="public", blocker_count=None)
    assert report["unknown_or_unvalidated"]
    assert report["blocks_source_delete"]


def test_keep_sql_uses_verified_durable_link_columns_and_accepted_status() -> None:
    sql = _durable_keep_condition("public")
    assert "object_catalog_links" in sql
    assert "match_status = 'MATCHED'" in sql
    assert "reference_photometry_sources" in sql
    assert "identity_catalog_source_id" in sql
    assert "keep_catalog_source_ids" in sql
    assert "keep_source_ids" in sql
    assert "pipeline_run.catalog_name = cs.catalog_name" in sql
    assert (
        "jsonb_typeof(pipeline_run.recipe_json #> "
        "'{quality_policy,target_source_id}') = 'string'"
    ) in sql
    assert (
        "pipeline_run.recipe_json #>> '{quality_policy,target_source_id}' = cs.source_id"
    ) in sql


def test_apply_requires_both_acknowledgements_before_any_connection() -> None:
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="--confirm-database"):
        validate_apply_acknowledgements(
            apply=True, confirm_database=None, confirm_catalog_cleanup=True
        )
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="--confirm-catalog-cleanup"):
        validate_apply_acknowledgements(
            apply=True, confirm_database="grandgousier_test", confirm_catalog_cleanup=False
        )
    validate_apply_acknowledgements(
        apply=False, confirm_database=None, confirm_catalog_cleanup=False
    )


def test_apply_requires_exact_database_name_and_rejects_protected_target() -> None:
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="exactly match"):
        _assert_apply_target(_identity(), "Grandgousier_test")
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="forbidden"):
        _assert_apply_target(
            _identity(name="grandgousier-test", protected_reasons=("database_name_protected_marker",)),
            "grandgousier-test",
        )


class _FakeResult:
    def __init__(self, *, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _FakeEngine:
    url = SimpleNamespace(host="localhost")


class _FakeConnection:
    engine = _FakeEngine()

    def __init__(self, *, identity_row, relations, foreign_keys, isolation_level="read committed"):
        self.identity_row = identity_row
        self.relations = relations
        self.foreign_keys = foreign_keys
        self.isolation_level = isolation_level
        self.statements: list[str] = []
        self.events: list[str] = []
        self.read_only_set = False

    def exec_driver_sql(self, sql):
        self.statements.append(sql)
        if sql == "SET TRANSACTION READ ONLY":
            self.read_only_set = True
        return _FakeResult()

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if sql == "SHOW transaction_isolation":
            return _FakeResult(scalar=self.isolation_level)
        if "FROM pg_catalog.pg_database AS d" in sql:
            return _FakeResult(rows=[self.identity_row])
        if "WHERE c.oid = to_regclass(:relation_name)" in sql:
            relation_name = (params or {})["relation_name"]
            row = self.relations.get(relation_name)
            return _FakeResult(rows=[row] if row is not None else [])
        if "FROM pg_catalog.pg_constraint AS constraint_row" in sql:
            self.events.append("incoming_fk_inventory")
            return _FakeResult(rows=self.foreign_keys)
        if "SELECT count(*)::bigint AS reference_count" in sql:
            return _FakeResult(scalar=0)
        if sql.lstrip().startswith("LOCK TABLE"):
            self.events.append("table_lock")
            return _FakeResult()
        pytest.fail(f"unexpected fake SQL: {sql[:120]}")


def _fake_identity_row(name="grandgousier_test"):
    return {
        "database_name": name,
        "database_oid": 1234,
        "db_user": "tester",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "server_version": "PostgreSQL 18.0",
        "server_version_num": 180000,
        "current_schema": "public",
        "database_comment": None,
        "database_allows_connections": True,
        "database_is_template": False,
    }


def _fake_relations():
    return {
        name: {
            "relation_oid": index,
            "schema_name": "public",
            "relation_name": name,
            "relation_kind": "r",
        }
        for index, name in enumerate(
            ("catalog_sources", "frame_catalog_links", "pipeline_runs"), start=10
        )
    }


def _fk_row(child_table, child_column, on_delete):
    return {
        "target_table": "catalog_sources",
        "target_schema": "public",
        "child_schema": "public",
        "child_table": child_table,
        "constraint_name": f"fk_{child_table}_catalog_sources",
        "child_columns": [child_column],
        "target_columns": ["catalog_source_id"],
        "on_delete": on_delete,
        "validated": True,
    }


def test_apply_fails_closed_on_unknown_incoming_fk_before_any_delete() -> None:
    foreign_keys = [
        _fk_row("frame_catalog_links", "catalog_source_id", "CASCADE"),
        _fk_row("detection_catalog_matches", "catalog_source_id", "CASCADE"),
        _fk_row("object_catalog_links", "catalog_source_id", "RESTRICT"),
        _fk_row("reference_photometry_sources", "identity_catalog_source_id", "RESTRICT"),
        _fk_row("unrecognized_child_table", "catalog_source_id", "RESTRICT"),
    ]
    connection = _FakeConnection(
        identity_row=_fake_identity_row(),
        relations=_fake_relations(),
        foreign_keys=foreign_keys,
    )

    with pytest.raises(LegacyGaiaCatalogCleanupError, match="unknown or unvalidated"):
        audit_legacy_gaia_catalogs(
            connection,
            apply=True,
            confirm_database="grandgousier_test",
            confirm_catalog_cleanup=True,
        )

    assert not any(statement.lstrip().startswith("DELETE") for statement in connection.statements)
    lock_sql = next(statement for statement in connection.statements if statement.lstrip().startswith("LOCK TABLE"))
    assert '"public"."pipeline_runs"' in lock_sql
    assert connection.events.index("table_lock") < connection.events.index("incoming_fk_inventory")


def test_apply_fails_closed_unless_transaction_is_read_committed() -> None:
    connection = _FakeConnection(
        identity_row=_fake_identity_row(),
        relations=_fake_relations(),
        foreign_keys=[],
        isolation_level="repeatable read",
    )

    with pytest.raises(LegacyGaiaCatalogCleanupError, match="requires READ COMMITTED"):
        audit_legacy_gaia_catalogs(
            connection,
            apply=True,
            confirm_database="grandgousier_test",
            confirm_catalog_cleanup=True,
        )

    assert connection.events == []
    assert not any(statement.lstrip().startswith(("LOCK TABLE", "DELETE")) for statement in connection.statements)


class _FullDryRunConnection(_FakeConnection):
    def __init__(self):
        super().__init__(
            identity_row=_fake_identity_row(),
            relations=_fake_relations(),
            foreign_keys=[
                _fk_row("frame_catalog_links", "catalog_source_id", "CASCADE"),
                _fk_row("detection_catalog_matches", "catalog_source_id", "CASCADE"),
                _fk_row("object_catalog_links", "catalog_source_id", "RESTRICT"),
                _fk_row("reference_photometry_sources", "identity_catalog_source_id", "RESTRICT"),
            ],
        )

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT count(*)::bigint AS reference_count" in sql:
            if '"frame_catalog_links" AS child' in sql:
                return _FakeResult(scalar=6)
            if '"detection_catalog_matches" AS child' in sql:
                return _FakeResult(scalar=1)
            if '"object_catalog_links" AS child' in sql:
                return _FakeResult(scalar=1)
            return _FakeResult(scalar=0)
        if "AS source_count" in sql:
            return _FakeResult(
                rows=[
                    {
                        "catalog_name": "auto_catalog_field_7",
                        "source_count": 4,
                        "object_link_sources": 1,
                        "accepted_match_sources": 1,
                        "reference_identity_sources": 0,
                        "explicit_target_sources": 0,
                        "pipeline_run_target_sources": 1,
                        "other_fk_protected_sources": 0,
                        "durable_keep_sources": 3,
                        "known_removable_sources": 1,
                        "blocked_by_detection_match_sources": 0,
                    }
                ]
            )
        if "AS frame_link_count" in sql:
            return _FakeResult(
                rows=[
                    {
                        "catalog_name": "auto_catalog_field_7",
                        "frame_link_count": 6,
                        "removable_frame_links": 2,
                    }
                ]
            )
        if "AS keep_reasons" in sql:
            return _FakeResult(
                rows=[
                    {
                        "catalog_source_id": 11,
                        "catalog_name": "auto_catalog_field_7",
                        "source_id": "gaia-11",
                        "keep_reasons": ["OBJECT_CATALOG_LINK"],
                    },
                    {
                        "catalog_source_id": 12,
                        "catalog_name": "auto_catalog_field_7",
                        "source_id": "gaia-12",
                        "keep_reasons": ["ACCEPTED_DETECTION_CATALOG_MATCH"],
                    },
                    {
                        "catalog_source_id": 13,
                        "catalog_name": "auto_catalog_field_7",
                        "source_id": "gaia-13",
                        "keep_reasons": ["PIPELINE_RUN_TARGET_SOURCE_ID"],
                    },
                ]
            )
        if "pg_relation_size(to_regclass(:qualified_name))" in sql:
            return _FakeResult(
                rows=[
                    {"table_bytes": 10_000, "index_bytes": 5_000, "total_bytes": 16_000}
                ]
            )
        if "FROM pg_catalog.pg_index AS index_row" in sql:
            return _FakeResult(rows=[{"index_name": "ix_fake", "bytes": 5_000}])
        if "recipe_json ? 'gaia_query_coverage_v1'" in sql:
            return _FakeResult(
                rows=[
                    {
                        "runs_with_legacy_coverage_key": 4,
                        "legacy_catalog_runs_with_key": 3,
                        "legacy_catalog_runs_complete": 2,
                        "legacy_catalog_runs_incomplete": 1,
                    }
                ]
            )
        if "SELECT count(*)::bigint FROM" in sql:
            if '"catalog_sources"' in sql and "WHERE catalog_name" in sql:
                return _FakeResult(scalar=7)
            if '"catalog_sources"' in sql:
                return _FakeResult(scalar=100)
            if '"frame_catalog_links"' in sql:
                return _FakeResult(scalar=1_000)
        return super().execute(statement, params)


def test_mocked_dry_run_reports_inventory_and_estimate_without_dml() -> None:
    connection = _FullDryRunConnection()
    report = audit_legacy_gaia_catalogs(connection)

    assert connection.read_only_set
    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert report["mode"] == "DRY_RUN_READ_ONLY"
    assert report["database"]["database_oid"] == 1234
    assert report["catalogs"][0]["catalog_name"] == "auto_catalog_field_7"
    assert report["catalogs"][0]["durable_keep_source_counts_by_reason"][
        "pipeline_run_target_source_id"
    ] == 1
    assert report["durable_keep_set"]["source_count"] == 3
    assert report["durable_keep_set"]["sources"][2]["reasons"] == [
        "PIPELINE_RUN_TARGET_SOURCE_ID"
    ]
    assert report["removable_frame_links"]["count"] == 2
    assert report["removable_catalog_sources"]["count"] == 1
    assert report["canonical_gaia_dr3_source_count"] == 7
    assert report["legacy_gaia_query_coverage_v1"]["legacy_catalog_runs_with_key"] == 3
    assert report["estimated_reclaimable_bytes"]["immediately_freed_bytes_by_delete"] == 0
    assert not any(statement.lstrip().startswith(("DELETE", "UPDATE", "INSERT")) for statement in connection.statements)


def test_dry_run_starts_read_only_before_identity_query_and_never_deletes() -> None:
    connection = _FakeConnection(
        identity_row={"database_name": None},
        relations={},
        foreign_keys=[],
    )
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="could not establish"):
        audit_legacy_gaia_catalogs(connection)
    assert connection.read_only_set
    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert not any(statement.lstrip().startswith(("DELETE", "UPDATE", "INSERT")) for statement in connection.statements)


def test_delete_statements_are_limited_to_legacy_source_and_frame_link_tables() -> None:
    frame_link_sql = _delete_frame_links_sql("public")
    source_sql = _delete_sources_sql("public")
    assert frame_link_sql.lstrip().startswith('DELETE FROM "public"."frame_catalog_links"')
    assert source_sql.lstrip().startswith('DELETE FROM "public"."catalog_sources"')
    for sql in (frame_link_sql, source_sql):
        assert "auto_catalog_field_" not in sql  # prefix arrives only as a bound value
        assert "canonical_catalog_name" in sql
        assert "objects" not in sql
        assert "measurements" not in sql
        assert "science_photometry" not in sql
        assert "gaia_dr3" not in sql
        assert "pipeline_run.catalog_name = cs.catalog_name" in sql
        assert (
            "jsonb_typeof(pipeline_run.recipe_json #> "
            "'{quality_policy,target_source_id}') = 'string'"
        ) in sql


def test_database_identity_marks_production_frozen_and_reference_names_protected() -> None:
    class IdentityConnection:
        engine = _FakeEngine()

        def execute(self, statement):
            assert isinstance(statement, type(text("SELECT 1")))
            return _FakeResult(rows=[_fake_identity_row(name="archive_frozen_reference")])

    identity = _database_identity(IdentityConnection())
    assert identity.protected_reasons
    with pytest.raises(LegacyGaiaCatalogCleanupError, match="forbidden"):
        _assert_apply_target(identity, identity.database_name)


def test_cli_command_defaults_to_dry_run_and_accepts_all_apply_flags() -> None:
    parser = build_parser()
    dry_run = parser.parse_args(["cleanup-legacy-gaia-catalogs"])
    assert not dry_run.apply
    assert dry_run.format == "json"
    assert dry_run.confirm_database is None
    assert not dry_run.confirm_catalog_cleanup

    apply = parser.parse_args(
        [
            "cleanup-legacy-gaia-catalogs",
            "--apply",
            "--confirm-database",
            "grandgousier_test",
            "--confirm-catalog-cleanup",
            "--keep-source-id",
            "123",
            "--keep-catalog-source-id",
            "55",
        ]
    )
    assert apply.apply
    assert apply.confirm_database == "grandgousier_test"
    assert apply.confirm_catalog_cleanup
    assert apply.keep_source_id == ["123"]
    assert apply.keep_catalog_source_id == [55]


def test_cli_rejects_incomplete_apply_flags_before_engine_creation(monkeypatch, capsys) -> None:
    import sqlalchemy

    monkeypatch.setattr(
        sqlalchemy,
        "create_engine",
        lambda *_args, **_kwargs: pytest.fail("invalid apply flags must not connect"),
    )
    args = SimpleNamespace(
        apply=True,
        confirm_database=None,
        confirm_catalog_cleanup=False,
        database_url=None,
        keep_source_id=[],
        keep_catalog_source_id=[],
        format="json",
    )
    assert cmd_cleanup_legacy_gaia_catalogs(args) == 2
    assert "--confirm-database" in capsys.readouterr().err
