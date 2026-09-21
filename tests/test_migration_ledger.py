from __future__ import annotations

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
    assert len(MIGRATIONS) == 32
    assert len(managed) == 16
    assert MIGRATIONS[-1].migration_id == "mvp7_0_3_reference_photometry_normalization"
    assert MIGRATIONS[-2].migration_id == "mvp7_0_2_identity_locations"
    assert MIGRATIONS[-3].migration_id == "mvp7_0_1_detection_normalization"
    assert MIGRATIONS[-4].migration_id == "mvp7_0_0_clean_schema_epoch"
    assert MIGRATIONS[-5].migration_id == "mvp6_4_3_science_flag_overrides"
    assert MIGRATIONS[-6].migration_id == "mvp6_4_0_science_photometry_cache"
    assert MIGRATIONS[-7].migration_id == "mvp5_52_0_observation_identity"
    assert MIGRATIONS[-8].migration_id == "mvp5_46_0_interseries_calibration_generations"
    assert MIGRATIONS[-9].migration_id == "mvp5_40_13_science_measurement_index_cleanup"
    assert MIGRATIONS[-10].migration_id == "mvp5_40_2_science_measurements"
    assert MIGRATIONS[-11].migration_id == "mvp5_40_0_normalized_calibration_terms"
    assert MIGRATIONS[-12].migration_id == "mvp5_39_7_batch_orchestration"
    assert MIGRATIONS[-13].migration_id == "mvp5_39_6_artifact_registry"
    assert MIGRATIONS[-14].migration_id == "mvp5_39_5_calibration_immutable_generations"
    assert MIGRATIONS[-15].migration_id == "mvp5_39_2_calibration_product_provenance"
    assert MIGRATIONS[-16].migration_id == "mvp5_39_1_schema_migration_ledger"
    assert all(not spec.legacy for spec in MIGRATIONS[-16:])
    assert all(spec.legacy for spec in MIGRATIONS[:-16])


def test_migration_checksums_are_sha256_of_repository_files() -> None:
    for spec in MIGRATIONS:
        digest = migration_checksum(spec)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert migration_path(spec).is_file()


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
