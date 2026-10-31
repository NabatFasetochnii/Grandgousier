from __future__ import annotations

import hashlib
import re
from pathlib import Path

from grandgousier.db.migrations import (
    MIGRATIONS,
    PRE_LEDGER_SCHEMA_TABLES,
    MigrationSpec,
    _escape_percent_for_dbapi,
    migration_checksum,
    migration_path,
    summarize_status,
    validate_manifest,
)
from grandgousier.db.models import Base
from grandgousier.db.schema_contract import CURRENT_SCHEMA_TABLES


def test_schema_migration_ledger_is_part_of_orm_contract() -> None:
    assert "schema_migrations" in Base.metadata.tables
    assert "schema_migrations" in CURRENT_SCHEMA_TABLES
    assert len(Base.metadata.tables) == 46


def test_migration_manifest_is_ordered_unique_and_complete() -> None:
    validate_manifest()
    ids = [spec.migration_id for spec in MIGRATIONS]
    filenames = [spec.filename for spec in MIGRATIONS]
    assert len(ids) == len(set(ids))
    assert len(filenames) == len(set(filenames))
    managed = [spec for spec in MIGRATIONS if not spec.legacy]
    assert len(MIGRATIONS) == 37
    assert len(managed) == 21
    assert [s.migration_id for s in MIGRATIONS[-9:]] == [
        "mvp7_0_0_clean_schema_epoch", "mvp7_0_1_detection_normalization",
        "mvp7_0_2_identity_locations", "mvp7_0_3_reference_photometry_normalization",
        "mvp7_0_4_null_identity_constraints", "mvp7_0_5_interseries_membership_field_integrity",
        "mvp7_0_6_interseries_edge_delete_restrict",
        "mvp7_0_7_interseries_edge_membership_integrity",
        "mvp7_0_8_interseries_reference_cv_scope_integrity",
    ]
    assert MIGRATIONS[-21].migration_id == "mvp5_39_1_schema_migration_ledger"
    assert all(not spec.legacy for spec in MIGRATIONS[-21:])
    assert all(spec.legacy for spec in MIGRATIONS[:-21])


def test_migration_checksums_are_sha256_of_repository_files() -> None:
    for spec in MIGRATIONS:
        digest = migration_checksum(spec)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert migration_path(spec).is_file()


def test_migration_checksum_only_canonicalizes_crlf(tmp_path: Path) -> None:
    spec = MigrationSpec("temporary", "temporary.sql")

    lf = b"SELECT 1;\n"
    (tmp_path / spec.filename).write_bytes(lf)
    lf_checksum = migration_checksum(spec, root=tmp_path)
    assert lf_checksum == hashlib.sha256(lf).hexdigest()

    (tmp_path / spec.filename).write_bytes(b"SELECT 1;\r\n")
    assert migration_checksum(spec, root=tmp_path) == lf_checksum

    (tmp_path / spec.filename).write_bytes(b"SELECT 2;\r\n")
    assert migration_checksum(spec, root=tmp_path) != lf_checksum

    (tmp_path / spec.filename).write_bytes(b"SELECT 1;")
    no_final_newline_checksum = migration_checksum(spec, root=tmp_path)
    assert no_final_newline_checksum != lf_checksum

    (tmp_path / spec.filename).write_bytes(b"SELECT 1;\r")
    assert migration_checksum(spec, root=tmp_path) != lf_checksum

    (tmp_path / spec.filename).write_bytes(b"SELECT 1;\r\nSELECT 2;")
    crlf_checksum = migration_checksum(spec, root=tmp_path)
    (tmp_path / spec.filename).write_bytes(b"SELECT 1;\nSELECT 2;")
    assert migration_checksum(spec, root=tmp_path) == crlf_checksum

    (tmp_path / spec.filename).write_bytes(b"SELECT 1; \n")
    assert migration_checksum(spec, root=tmp_path) != lf_checksum


def test_managed_migration_contains_no_psql_meta_commands() -> None:
    managed = [spec for spec in MIGRATIONS if not spec.legacy]
    assert managed
    for spec in managed:
        text = migration_path(spec).read_text(encoding="utf-8")
        assert not any(line.startswith("\\") for line in text.splitlines())


def test_detection_normalization_marker_fails_closed_for_old_layout() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp7_0_1_detection_normalization")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "to_regclass('public.detection_runs')" in sql
    assert "to_regclass('public.detection_run_frames')" in sql
    assert "source_detections" in sql
    assert "detection_catalog_matches" in sql
    assert "RAISE EXCEPTION" in sql
    assert "fresh" in sql.lower()



def test_reference_photometry_normalization_marker_fails_closed_for_wide_layout() -> None:
    spec = next(
        s for s in MIGRATIONS
        if s.migration_id == "mvp7_0_3_reference_photometry_normalization"
    )
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "to_regclass('public.reference_photometry')" in sql
    assert "to_regclass('public.photometric_bands')" in sql
    assert "to_regclass('public.reference_photometry_sources')" in sql
    assert "to_regclass('public.reference_photometry_points')" in sql
    assert "RAISE EXCEPTION" in sql
    assert "fresh" in sql.lower()

def test_validate_manifest_accepts_explicit_scripts_root(tmp_path: Path) -> None:
    for spec in MIGRATIONS:
        source = migration_path(spec)
        (tmp_path / spec.filename).write_bytes(source.read_bytes())
    validate_manifest(root=tmp_path)


def test_raw_migration_sql_escapes_percent_for_psycopg_paramstyles() -> None:
    sql = "SELECT 'forced_aperture_%', 10 % 3, '100%';"
    expected = "SELECT 'forced_aperture_%%', 10 %% 3, '100%%';"
    assert _escape_percent_for_dbapi(sql, paramstyle="pyformat") == expected
    assert _escape_percent_for_dbapi(sql, paramstyle="format") == expected


def test_raw_migration_sql_keeps_percent_for_non_percent_paramstyles() -> None:
    sql = "SELECT 'forced_aperture_%', 10 % 3;"
    assert _escape_percent_for_dbapi(sql, paramstyle="qmark") == sql
    assert _escape_percent_for_dbapi(sql, paramstyle="numeric_dollar") == sql
    assert _escape_percent_for_dbapi(sql, paramstyle=None) == sql


def test_mvp5_39_2_sql_is_safe_after_psycopg_percent_escaping() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_39_2_calibration_product_provenance")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "LIKE 'forced_aperture_%'" in sql
    escaped = _escape_percent_for_dbapi(sql, paramstyle="pyformat")
    assert "LIKE 'forced_aperture_%%'" in escaped


def test_legacy_adoption_contract_does_not_include_post_ledger_tables() -> None:
    assert "calibration_product_aliases" not in PRE_LEDGER_SCHEMA_TABLES
    assert "schema_migrations" not in PRE_LEDGER_SCHEMA_TABLES
    assert "calibration_versions" in PRE_LEDGER_SCHEMA_TABLES


def test_mvp5_39_5_migration_is_managed_and_contains_alias_backfill() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_39_5_calibration_immutable_generations")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS calibration_product_aliases" in sql
    assert "logical_name" in sql
    assert "generation_no" in sql
    assert "ON CONFLICT (logical_name) DO NOTHING" in sql


def test_mvp5_39_6_migration_contains_artifact_registry_and_publication_head() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_39_6_artifact_registry")
    assert spec.migration_id == "mvp5_39_6_artifact_registry"
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS artifacts" in sql
    assert "CREATE TABLE IF NOT EXISTS artifact_publications" in sql
    assert "sha256" in sql
    assert "publication_uri" in sql


def test_mvp5_39_7_migration_contains_db_native_batch_state() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_39_7_batch_orchestration")
    assert spec.migration_id == "mvp5_39_7_batch_orchestration"
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS batch_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS batch_inventory_items" in sql
    assert "CREATE TABLE IF NOT EXISTS batch_jobs" in sql
    assert "CREATE TABLE IF NOT EXISTS batch_job_attempts" in sql
    assert "pipeline_run_id BIGINT REFERENCES pipeline_runs" in sql
    assert "ADD COLUMN IF NOT EXISTS batch_run_id BIGINT" in sql
    assert "ADD COLUMN IF NOT EXISTS batch_job_attempt_id BIGINT" in sql


def test_mvp5_40_0_migration_contains_normalized_calibration_terms() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_40_0_normalized_calibration_terms")
    assert spec.migration_id == "mvp5_40_0_normalized_calibration_terms"
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS calibration_iterations" in sql
    assert "CREATE TABLE IF NOT EXISTS calibration_frame_terms" in sql
    assert "CREATE TABLE IF NOT EXISTS calibration_iteration_objects" in sql
    assert "uq_calibration_iterations_version_kind_no" in sql
    assert "uq_calibration_frame_terms_iteration_frame_kind" in sql
    assert "uq_calibration_iteration_objects_iteration_object" in sql


def test_mvp5_40_2_migration_contains_science_measurements() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_40_2_science_measurements")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS science_measurements" in sql
    assert "base_measurement_id" in sql
    assert "uq_science_measurements_version_object_frame" in sql
    assert "RECONSTRUCTED_NORMALIZED" in sql


def test_mvp5_40_13_migration_prunes_science_measurement_indexes() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_40_13_science_measurement_index_cleanup")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "DROP INDEX IF EXISTS ix_science_measurements_calibration" in sql
    assert "DROP INDEX IF EXISTS ix_science_measurements_object" in sql
    assert "DROP INDEX IF EXISTS ix_science_measurements_frame" in sql
    assert "DROP INDEX IF EXISTS ix_science_measurements_base" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_science_measurements_calibration_frame" in sql


def test_mvp5_46_0_migration_creates_immutable_interseries_generation_tables() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_46_0_interseries_calibration_generations")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS interseries_calibration_generations" in sql
    assert "CREATE TABLE IF NOT EXISTS interseries_calibration_series" in sql
    assert "CREATE TABLE IF NOT EXISTS interseries_calibration_edges" in sql
    assert "fingerprint_sha256" in sql
    assert "science_calibration_version_id" in sql
    assert "reliability_status" in sql
    assert "common_reference_object_ids" in sql


def test_mvp5_52_0_migration_adds_non_destructive_observation_identity_layer() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp5_52_0_observation_identity")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS observations" in sql
    assert "ADD COLUMN IF NOT EXISTS observation_id BIGINT" in sql
    assert "FOREIGN KEY (observation_id)" in sql
    assert "ON DELETE SET NULL" in sql
    assert "uq_observations_observation_uid" in sql
    assert "uq_observations_scheme_native_identity" in sql
    assert "DELETE FROM FRAMES" not in sql.upper()
    assert "UPDATE frames" not in sql


def test_mvp6_4_0_migration_creates_compact_science_cache() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp6_4_0_science_photometry_cache")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS science_photometry_cache" in sql
    assert "base_measurement_id BIGINT NOT NULL" in sql
    assert "mag_cal DOUBLE PRECISION" in sql
    assert "mag_err_random DOUBLE PRECISION" in sql
    for forbidden in ("object_id", "frame_id", "flags", "mag_inst", "flux_err", "local_zp_correction"):
        assert forbidden not in sql


def test_mvp6_4_3_migration_creates_factored_historical_flag_overrides() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp6_4_3_science_flag_overrides")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS science_flag_override_sets" in sql
    assert "CREATE TABLE IF NOT EXISTS science_flag_overrides" in sql
    assert "common_override_flags JSONB" in sql
    assert "common_removed_keys JSONB" in sql
    assert "PRIMARY KEY (calibration_version_id, base_measurement_id)" in sql
    assert "REFERENCES science_measurements" not in sql


def test_mvp7_0_0_migration_is_fresh_epoch_marker_only() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp7_0_0_clean_schema_epoch")
    sql = migration_path(spec).read_text(encoding="utf-8")
    assert "SELECT 1" in sql
    upper = sql.upper()
    for destructive in ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "DELETE FROM", "TRUNCATE", "UPDATE "):
        assert destructive not in upper


def test_mvp7_0_4_preflights_all_approved_nullable_identities_before_ddl() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp7_0_4_null_identity_constraints")
    sql = migration_path(spec).read_text(encoding="utf-8")
    upper = sql.upper()

    assert "CURRENT_SETTING('SERVER_VERSION_NUM')::INTEGER < 150000" in upper
    assert upper.index("SERVER_VERSION_NUM") < upper.index("ALTER TABLE")
    lock = re.search(r"LOCK TABLE(.*?)IN ACCESS EXCLUSIVE MODE", sql, flags=re.IGNORECASE | re.DOTALL)
    assert lock is not None
    lock_tables = re.findall(r"PUBLIC\.([A-Z_]+)", lock.group(1), flags=re.IGNORECASE)
    assert lock_tables == sorted(
        ["event_frame_coverage", "instrument_units", "object_qc"]
    )

    expected = {
        "instrument_units": (
            "instrument, site, tube, camera, filter_name, observing_mode",
            "uq_instrument_units_identity",
        ),
        "object_qc": (
            "object_id, catalog_name, instrument_unit_id, calibration_version_id, algorithm",
            "uq_object_qc_identity",
        ),
        "event_frame_coverage": (
            "event_id, frame_id, object_id, calibration_version_id",
            "uq_event_frame_coverage",
        ),
    }
    first_alter = upper.index("ALTER TABLE")
    assert lock.start() < first_alter
    assert "duplicate NULL-inclusive identity groups" in sql
    assert "manually and rerun" in sql
    for table_name, (columns, constraint_name) in expected.items():
        source = re.search(
            rf"FROM\s+public\.{table_name}\s+GROUP BY\s+([^;]+?)\s+HAVING\s+COUNT\(\*\)\s*>\s*1",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert source is not None
        assert source.group(1).replace("\n", " ").strip() == columns
        assert source.start() < first_alter
        assert constraint_name in sql
        assert "UNIQUE NULLS NOT DISTINCT" in upper
        replacement = re.search(
            rf"ALTER TABLE\s+public\.{table_name}\s+"
            rf"DROP CONSTRAINT\s+{constraint_name},\s+"
            rf"ADD CONSTRAINT\s+{constraint_name}\s+UNIQUE NULLS NOT DISTINCT\s*"
            rf"\(({re.escape(columns)})\)",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert replacement is not None

    for excluded in (
        "uq_calibration_versions_logical_generation",
        "uq_artifacts_run_uri",
        "uq_artifacts_batch_attempt_uri",
    ):
        assert excluded not in sql
    for forbidden in (r"\bUPDATE\b", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bBACKFILL\b"):
        assert re.search(forbidden, sql, flags=re.IGNORECASE) is None


def test_mvp7_0_5_sql_mirrors_and_preflight_contract() -> None:
    spec = next(
        spec for spec in MIGRATIONS
        if spec.migration_id == "mvp7_0_5_interseries_membership_field_integrity"
    )
    assert spec.migration_id == "mvp7_0_5_interseries_membership_field_integrity"
    packaged = migration_path(spec)
    mirror = Path(__file__).resolve().parents[1] / "scripts" / spec.filename
    assert packaged.read_bytes() == mirror.read_bytes()
    sql = packaged.read_text(encoding="utf-8")

    version = sql.index("current_setting('server_version_num')::integer < 150000")
    timeout = sql.index("SET LOCAL lock_timeout = '5s'")
    lock = sql.index("LOCK TABLE public.field_series")
    inspect_fks = sql.index("FROM pg_catalog.pg_constraint c", lock)
    reference_check = sql.index("M4 reference series is missing or belongs to another field")
    membership_check = sql.index("M4 membership has a missing parent")
    reference_member_check = sql.index("exactly one matching reference membership")
    add_column = sql.index("ALTER TABLE public.interseries_calibration_series ADD COLUMN field_id")
    backfill = sql.index("UPDATE public.interseries_calibration_series AS s")
    assert version < timeout < lock < inspect_fks < reference_check < membership_check
    assert membership_check < reference_member_check < add_column < backfill
    membership_start = sql.index("SELECT 1 FROM public.interseries_calibration_series s", reference_check)
    membership_preflight = sql[membership_start:membership_check]
    assert "LEFT JOIN public.calibration_versions cv" in membership_preflight
    assert "ON cv.calibration_version_id = s.science_calibration_version_id" in membership_preflight
    cv_field = membership_preflight.index("cv.field_id IS DISTINCT FROM g.field_id")
    cv_series = membership_preflight.index("cv.series_id IS DISTINCT FROM s.series_id")
    assert membership_start < membership_check < add_column
    assert cv_field < cv_series
    assert "pg_catalog.pg_attribute" in sql
    required_column_check = sql.index("FOR required_column IN")
    assert lock < required_column_check < inspect_fks < add_column
    required_columns = sql[required_column_check:sql.index("END LOOP;", required_column_check)]
    for table, column in (
        ("field_series", "series_id"),
        ("field_series", "field_id"),
        ("interseries_calibration_generations", "interseries_generation_id"),
        ("interseries_calibration_generations", "field_id"),
        ("interseries_calibration_generations", "reference_series_id"),
        ("interseries_calibration_series", "interseries_generation_id"),
        ("interseries_calibration_series", "series_id"),
        ("interseries_calibration_series", "science_calibration_version_id"),
    ):
        assert f"'public.{table}'::regclass, '{column}'" in required_columns
    assert "a.atttypid = 'pg_catalog.int8'::regtype AND a.attnotnull" in required_columns
    assert re.search(r"BOOL_AND\(\s*fk\.matches_contract\s*\)", sql, flags=re.IGNORECASE)
    assert len(re.findall(r"\bBOOL_AND\s*\(", sql, flags=re.IGNORECASE)) == 1
    assert sql.count("CROSS JOIN LATERAL (") == 2
    assert sql.count("array_agg(a.attname::text ORDER BY k.ord)") == 2
    assert "child_key.columns = expected.child_columns" in sql
    assert "parent_key.columns = expected.parent_columns" in sql
    assert "c.confdeltype = 'c'" in sql
    assert "a.attname::text = ANY(expected.child_columns)" in sql
    assert "actual.n <> 1" in sql and "format('ALTER TABLE" in sql
    assert "DROP CONSTRAINT %I" in sql
    assert "interseries_calibration_series_interseries_generation_id_fkey" not in sql
    assert "interseries_calibration_generations_reference_series_id_fkey" not in sql
    assert "REFERENCES public.field_series (series_id, field_id) ON DELETE RESTRICT" in sql
    assert "REFERENCES public.field_series (series_id, field_id) ON DELETE SET NULL (series_id)" in sql
    assert "ON DELETE CASCADE" in sql
    assert "ALTER COLUMN series_id DROP NOT NULL" in sql
    assert "ALTER COLUMN field_id SET NOT NULL" in sql


def test_mvp7_0_6_edge_restrict_sql_mirrors_and_preflight_before_ddl() -> None:
    spec = next(s for s in MIGRATIONS if s.migration_id == "mvp7_0_6_interseries_edge_delete_restrict")
    assert spec.migration_id == "mvp7_0_6_interseries_edge_delete_restrict"
    packaged = migration_path(spec)
    mirror = Path(__file__).resolve().parents[1] / "scripts" / spec.filename
    assert packaged.read_bytes() == mirror.read_bytes()
    sql = packaged.read_text(encoding="utf-8")
    upper = sql.upper()

    version_guard = sql.index("current_setting('server_version_num')::integer < 150000")
    timeout = sql.index("SET LOCAL lock_timeout = '5s'")
    locks = sql.index("LOCK TABLE public.field_series")
    catalog = sql.index("FROM pg_catalog.pg_constraint AS c", locks)
    row_preflight = sql.index("SELECT COUNT(*) FILTER (")
    first_ddl = upper.index("ALTER TABLE")
    assert version_guard < timeout < locks < catalog < row_preflight < first_ddl
    lock_list = sql[locks:sql.index("IN ACCESS EXCLUSIVE MODE", locks)]
    assert lock_list.index("public.field_series") < lock_list.index(
        "public.interseries_calibration_generations"
    ) < lock_list.index("public.interseries_calibration_edges")

    assert "WITH ORDINALITY" in sql
    assert "ARRAY_AGG(A.ATTNAME::TEXT ORDER BY K.ORD)" in upper
    assert "actual.n <> 1" in sql
    assert "c.confdeltype = expected_fk.delete_action" in sql
    assert "old_left_fk := actual.name" in sql
    assert "old_right_fk := actual.name" in sql
    assert "old_generation_fk := actual.name" in sql
    assert "format(" in sql and "DROP CONSTRAINT %I" in upper
    assert "LIMIT 10001" in sql and "edge_cap > 10000" in sql
    assert "field_id IS DISTINCT FROM g.field_id" in sql
    assert "e.left_series_id >= e.right_series_id" in sql
    assert "GROUP BY interseries_generation_id, left_series_id, right_series_id" in sql
    assert "pair_status" not in sql[sql.index("SELECT COUNT(*) FILTER ("):first_ddl]
    assert "ON DELETE RESTRICT NOT DEFERRABLE" in upper
    assert "fk_interseries_edges_left_series_restrict" in sql
    assert "fk_interseries_edges_right_series_restrict" in sql
    assert "ON DELETE CASCADE" not in upper  # The old generation FK is checked structurally.
    for generated_old_name in (
        "interseries_calibration_edges_left_series_id_fkey",
        "interseries_calibration_edges_right_series_id_fkey",
    ):
        assert generated_old_name not in sql

    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL)
    for forbidden in (
        r"\bUPDATE\s+(?:PUBLIC\.)?\w+\s+SET\b",
        r"\bDELETE\s+FROM\b", r"\bINSERT\s+INTO\b",
        r"\bTRUNCATE\s+", r"\bBACKFILL\b",
    ):
        assert re.search(forbidden, without_comments, flags=re.IGNORECASE) is None


def test_mvp7_0_7_resource_mirror_and_fixed_predecessor_manifest() -> None:
    spec = MIGRATIONS[-2]
    assert spec.migration_id == "mvp7_0_7_interseries_edge_membership_integrity"
    packaged = migration_path(spec).read_bytes()
    mirror = (Path(__file__).resolve().parents[1] / "scripts" / spec.filename).read_bytes()
    assert packaged == mirror
    assert b"\r" not in packaged  # Both mirrors are LF and have one canonical/raw digest.
    assert hashlib.sha256(packaged).hexdigest() == migration_checksum(spec)
    sql = packaged.decode("utf-8")
    expected_rows = re.findall(
        r"\('([^']+)', '([^']+\.sql)', '([0-9a-f]{64})'\)", sql
    )
    assert len(expected_rows) == 35
    assert expected_rows == [
        (s.migration_id, s.filename, migration_checksum(s)) for s in MIGRATIONS[:-2]
    ]

    timeout = sql.index("SET LOCAL statement_timeout = '30s'")
    lock_timeout = sql.index("SET LOCAL lock_timeout = '5s'")
    locks = re.findall(r"LOCK TABLE public\.([a-z_]+) IN (ACCESS EXCLUSIVE|SHARE) MODE;", sql)
    assert locks == [
        ("field_series", "ACCESS EXCLUSIVE"),
        ("interseries_calibration_generations", "ACCESS EXCLUSIVE"),
        ("interseries_calibration_series", "ACCESS EXCLUSIVE"),
        ("interseries_calibration_edges", "ACCESS EXCLUSIVE"),
        ("schema_migrations", "SHARE"),
    ]
    share = sql.index("LOCK TABLE public.schema_migrations IN SHARE MODE")
    preflight = sql.index("DO $$")
    first_ddl = sql.index("ALTER TABLE public.interseries_calibration_edges")
    assert timeout < lock_timeout < sql.index("LOCK TABLE") < share < preflight < first_ddl
    assert "current_setting('transaction_isolation') <> 'read committed'" in sql[share:first_ddl]
    assert "current_setting('server_version_num')::integer < 150000" in sql[share:first_ddl]
    assert "EXCEPT" in sql[share:first_ddl] and "UNION ALL" in sql[share:first_ddl]
    assert sql.count("FROM public.schema_migrations") >= 4
    assert "(SELECT COUNT(*) FROM public.schema_migrations) <> 35" in sql
    assert "state NOT IN ('APPLIED', 'BASELINED') OR state IS NULL" in sql
    assert "migration_id = 'mvp7_0_7_interseries_edge_membership_integrity'" in sql
    assert "pg_catalog.pg_total_relation_size(required.table_oid) > 67108864" in sql
    assert "LIMIT 10001" in sql and "edge_cap > 10000" in sql
    assert "c.conname = 'uq_interseries_calibration_series_generation_series'" in sql
    assert "i.indisvalid AND i.indisready AND i.indimmediate" in sql
    assert "c.connamespace" in sql and "ns.nspname = 'public'" in sql
    assert "<> 3 THEN" in sql and "c.confdeltype = expected_fk.delete_action" in sql
    assert "left_member.series_id = e.left_series_id" in sql
    assert "right_member.series_id = e.right_series_id" in sql
    assert "pair_status = 'OK'" not in sql
    assert "c.conenforced" not in sql
    assert "to_jsonb(c)->>'conenforced'" in sql
    assert sql.count("ADD CONSTRAINT fk_interseries_edges_") == 2
    for side in ("left", "right"):
        assert (
            f"FOREIGN KEY (interseries_generation_id, {side}_series_id)\n"
            "    REFERENCES public.interseries_calibration_series "
            "(interseries_generation_id, series_id)\n"
            "    ON DELETE NO ACTION ON UPDATE NO ACTION DEFERRABLE INITIALLY DEFERRED;"
        ) in sql
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL)
    for forbidden in (r"\bUPDATE\s+public\.", r"\bDELETE\s+FROM\b",
                      r"\bINSERT\s+INTO\b", r"\bTRUNCATE\b", r"\bCREATE\s+INDEX\b"):
        assert re.search(forbidden, without_comments, flags=re.IGNORECASE) is None


def test_mvp7_0_8_locked_preflight_mirrors_and_additive_keys() -> None:
    spec = MIGRATIONS[-1]
    assert spec.migration_id == "mvp7_0_8_interseries_reference_cv_scope_integrity"
    packaged = migration_path(spec).read_bytes()
    mirror = (Path(__file__).resolve().parents[1] / "scripts" / spec.filename).read_bytes()
    assert packaged == mirror and b"\r" not in packaged
    assert hashlib.sha256(packaged).hexdigest() == migration_checksum(spec)
    sql = packaged.decode("utf-8")
    assert re.findall(r"\('([^']+)', '([^']+\.sql)', '([0-9a-f]{64})'\)", sql) == [
        (s.migration_id, s.filename, migration_checksum(s)) for s in MIGRATIONS[:-1]
    ]
    locks = re.findall(r"LOCK TABLE public\.([a-z_]+) IN (ACCESS EXCLUSIVE|SHARE) MODE;", sql)
    assert locks == [(name, "ACCESS EXCLUSIVE") for name in (
        "calibration_versions", "field_series", "interseries_calibration_generations",
        "interseries_calibration_series", "interseries_calibration_edges",
    )] + [("schema_migrations", "SHARE")]
    first_ddl = sql.index("ALTER TABLE public.calibration_versions")
    assert sql.index("SET LOCAL statement_timeout = '30s'") < sql.index(
        "SET LOCAL lock_timeout = '5s'") < sql.index("LOCK TABLE") < sql.index(
        "LOCK TABLE public.schema_migrations") < sql.index("DO $$") < first_ddl
    preflight = sql[:first_ddl]
    for fragment in (
        "current_setting('server_version_num')::integer < 150000",
        "current_setting('transaction_isolation') <> 'read committed'",
        "(SELECT COUNT(*) FROM public.schema_migrations) <> 36",
        "state NOT IN ('APPLIED', 'BASELINED') OR state IS NULL",
        "migration_id = 'mvp7_0_8_interseries_reference_cv_scope_integrity'",
        "EXCEPT", "UNION ALL", "pg_catalog.pg_total_relation_size(required.table_oid) > 67108864",
        "i.indisvalid", "i.indisready", "i.indimmediate", "LIMIT %s",
        "bad_rows > 10000", "<> 5", "<> 3", "to_jsonb(c)->>'conenforced'",
        "cv.field_id IS DISTINCT FROM m.field_id", "cv.series_id IS DISTINCT FROM m.series_id",
        "m.series_id IS NOT NULL", "m.series_id = g.reference_series_id",
        "'public.interseries_calibration_edges'::regclass, 'interseries_generation_id', 'pg_catalog.int8'::regtype, TRUE",
        "'public.interseries_calibration_edges'::regclass, 'left_series_id', 'pg_catalog.int8'::regtype, TRUE",
        "'public.interseries_calibration_edges'::regclass, 'right_series_id', 'pg_catalog.int8'::regtype, TRUE",
        "'public.interseries_calibration_edges'::regclass, 'pair_status', 'pg_catalog.varchar'::regtype, TRUE",
        "c.conname = 'uq_interseries_calibration_edges_generation_pair'",
        "i.indisunique AND i.indisvalid", "AND i.indisready AND i.indimmediate",
        "c.conname = 'ck_interseries_calibration_edges_series_order'",
        "pg_catalog.pg_get_expr(c.conbin, c.conrelid)",
        "left_member.series_id = e.left_series_id",
        "right_member.series_id = e.right_series_id",
        "left_endpoint.field_id IS DISTINCT FROM g.field_id",
        "right_endpoint.field_id IS DISTINCT FROM g.field_id",
        "left_member.field_id IS DISTINCT FROM g.field_id",
        "right_member.field_id IS DISTINCT FROM g.field_id",
        "e.left_series_id >= e.right_series_id",
        "GROUP BY interseries_generation_id, left_series_id, right_series_id HAVING COUNT(*) > 1",
    ):
        assert fragment in preflight
    assert "pair_status = 'OK'" not in preflight
    assert preflight.index("bad_rows > 10000") < preflight.index(
        "FROM public.interseries_calibration_edges AS e")
    assert "c.conenforced" not in sql
    assert sql.count("ADD CONSTRAINT ") == 5
    assert "UNIQUE (calibration_version_id, field_id, series_id)" in sql
    assert "FOREIGN KEY (interseries_generation_id, reference_series_id)" in sql
    assert "DEFERRABLE INITIALLY DEFERRED;" in sql
    assert sql.count("MATCH SIMPLE ON DELETE NO ACTION ON UPDATE NO ACTION NOT DEFERRABLE;") == 2
    without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL)
    for forbidden in (r"\bUPDATE\s+public\.", r"\bDELETE\s+FROM\b",
                      r"\bINSERT\s+INTO\b", r"\bTRUNCATE\b", r"\bCREATE\s+INDEX\b"):
        assert re.search(forbidden, without_comments, flags=re.IGNORECASE) is None
