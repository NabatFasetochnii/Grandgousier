from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from grandgousier import __version__
from grandgousier.resources import migration_resource_path, resource_path
from grandgousier.db.models import SchemaMigration
from grandgousier.db.schema_contract import forbidden_tables


@dataclass(frozen=True, slots=True)
class MigrationSpec:
    migration_id: str
    filename: str
    legacy: bool = False


# Migrations before MVP5.39.1 predate the managed ledger. They may be adopted
# only after the current legacy schema is structurally validated.
MIGRATIONS: tuple[MigrationSpec, ...] = (
    MigrationSpec("mvp1_1_instrument_units", "migrate_mvp1_1_instrument_units.sql", legacy=True),
    MigrationSpec("mvp3_catalogs", "migrate_mvp3_catalogs.sql", legacy=True),
    MigrationSpec("mvp5_detection_catalog", "migrate_mvp5_detection_catalog.sql", legacy=True),
    MigrationSpec("mvp5_1_detection_catalog_qc", "migrate_mvp5_1_detection_catalog_qc.sql", legacy=True),
    MigrationSpec("mvp5_6_object_qc", "migrate_mvp5_6_object_qc.sql", legacy=True),
    MigrationSpec("mvp5_16_event_registry", "migrate_mvp5_16_event_registry.sql", legacy=True),
    MigrationSpec("mvp5_20_event_detection_links", "migrate_mvp5_20_event_detection_links.sql", legacy=True),
    MigrationSpec("mvp5_21_pipeline_runs", "migrate_mvp5_21_pipeline_runs.sql", legacy=True),
    MigrationSpec("mvp5_24_detection_idempotency", "migrate_mvp5_24_detection_idempotency.sql", legacy=True),
    MigrationSpec("mvp5_25_pipeline_logging", "migrate_mvp5_25_pipeline_logging.sql", legacy=True),
    MigrationSpec("mvp5_26_fields_series", "migrate_mvp5_26_fields_series.sql", legacy=True),
    MigrationSpec("mvp5_28_reference_photometry", "migrate_mvp5_28_reference_photometry.sql", legacy=True),
    MigrationSpec("mvp5_30_5_calibration_provenance", "migrate_mvp5_30_5_calibration_provenance.sql", legacy=True),
    MigrationSpec("mvp5_31_0_lightcurve_product", "migrate_mvp5_31_0_lightcurve_product.sql", legacy=True),
    MigrationSpec("mvp5_34_2_measurement_error_components", "migrate_mvp5_34_2_measurement_error_components.sql", legacy=True),
    MigrationSpec("mvp5_38_3_measurement_calibration_index", "migrate_mvp5_38_3_measurement_calibration_index.sql", legacy=True),
    MigrationSpec("mvp5_39_1_schema_migration_ledger", "migrate_mvp5_39_1_schema_migration_ledger.sql"),
    MigrationSpec("mvp5_39_2_calibration_product_provenance", "migrate_mvp5_39_2_calibration_product_provenance.sql"),
    MigrationSpec("mvp5_39_5_calibration_immutable_generations", "migrate_mvp5_39_5_calibration_immutable_generations.sql"),
    MigrationSpec("mvp5_39_6_artifact_registry", "migrate_mvp5_39_6_artifact_registry.sql"),
    MigrationSpec("mvp5_39_7_batch_orchestration", "migrate_mvp5_39_7_batch_orchestration.sql"),
    MigrationSpec("mvp5_40_0_normalized_calibration_terms", "migrate_mvp5_40_0_normalized_calibration_terms.sql"),
    MigrationSpec("mvp5_40_2_science_measurements", "migrate_mvp5_40_2_science_measurements.sql"),
    MigrationSpec("mvp5_40_13_science_measurement_index_cleanup", "migrate_mvp5_40_13_science_measurement_index_cleanup.sql"),
    MigrationSpec("mvp5_46_0_interseries_calibration_generations", "migrate_mvp5_46_0_interseries_calibration_generations.sql"),
    MigrationSpec("mvp5_52_0_observation_identity", "migrate_mvp5_52_0_observation_identity.sql"),
    MigrationSpec("mvp6_4_0_science_photometry_cache", "migrate_mvp6_4_0_science_photometry_cache.sql"),
    MigrationSpec("mvp6_4_3_science_flag_overrides", "migrate_mvp6_4_3_science_flag_overrides.sql"),
    MigrationSpec("mvp7_0_0_clean_schema_epoch", "migrate_mvp7_0_0_clean_schema_epoch.sql"),
    MigrationSpec("mvp7_0_1_detection_normalization", "migrate_mvp7_0_1_detection_normalization.sql"),
    MigrationSpec("mvp7_0_2_identity_locations", "migrate_mvp7_0_2_identity_locations.sql"),
    MigrationSpec("mvp7_0_3_reference_photometry_normalization", "migrate_mvp7_0_3_reference_photometry_normalization.sql"),
)


_PSQL_META_RE = re.compile(r"^\\", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    migration_id: str
    filename: str
    state: str
    checksum_status: str
    legacy: bool


class MigrationError(RuntimeError):
    pass


# Exact table contract that existed before the managed ledger was introduced.
# Do not derive this from CURRENT_SCHEMA_TABLES: later managed migrations add
# tables that, by definition, cannot be required before --adopt-legacy runs.
PRE_LEDGER_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "instrument_units",
        "frames",
        "processing_events",
        "catalog_sources",
        "reference_photometry_catalogs",
        "reference_photometry",
        "frame_catalog_links",
        "source_detections",
        "detection_catalog_matches",
        "frame_qc",
        "objects",
        "object_qc",
        "measurements",
        "calibration_versions",
        "interseries_calibration_offsets",
        "fields",
        "field_series",
        "field_series_frames",
        "pipeline_runs",
        "pipeline_run_steps",
        "pipeline_log_events",
        "event_registry",
        "event_object_links",
        "event_frame_coverage",
        "event_detection_links",
    }
)


def migrations_dir() -> Path:
    return resource_path("migrations")


def migration_path(spec: MigrationSpec, *, root: Path | None = None) -> Path:
    if root is not None:
        return root / spec.filename
    return migration_resource_path(spec.filename)


def migration_checksum(spec: MigrationSpec, *, root: Path | None = None) -> str:
    data = migration_path(spec, root=root).read_bytes()
    return hashlib.sha256(data).hexdigest()


def validate_manifest(*, root: Path | None = None) -> None:
    ids = [spec.migration_id for spec in MIGRATIONS]
    files = [spec.filename for spec in MIGRATIONS]
    if len(ids) != len(set(ids)):
        raise MigrationError("Duplicate migration_id in MIGRATIONS manifest")
    if len(files) != len(set(files)):
        raise MigrationError("Duplicate migration filename in MIGRATIONS manifest")
    missing = [spec.filename for spec in MIGRATIONS if not migration_path(spec, root=root).is_file()]
    if missing:
        raise MigrationError("Migration files are missing: " + ", ".join(missing))


def ensure_ledger_table(engine: Engine) -> None:
    SchemaMigration.__table__.create(engine, checkfirst=True)


def _ledger_rows(engine: Engine) -> dict[str, dict]:
    if "schema_migrations" not in inspect(engine).get_table_names():
        return {}
    with engine.connect() as conn:
        rows = conn.execute(select(SchemaMigration.__table__)).mappings().all()
    return {str(row["migration_id"]): dict(row) for row in rows}


def status(engine: Engine, *, root: Path | None = None) -> list[MigrationStatus]:
    validate_manifest(root=root)
    rows = _ledger_rows(engine)
    result: list[MigrationStatus] = []
    for spec in MIGRATIONS:
        row = rows.get(spec.migration_id)
        expected = migration_checksum(spec, root=root)
        if row is None:
            state = "PENDING"
            checksum_status = "-"
        else:
            state = str(row.get("state") or "APPLIED")
            checksum_status = "OK" if row.get("checksum_sha256") == expected else "DRIFT"
        result.append(
            MigrationStatus(
                migration_id=spec.migration_id,
                filename=spec.filename,
                state=state,
                checksum_status=checksum_status,
                legacy=spec.legacy,
            )
        )
    return result


def _legacy_baseline_issues(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required_tables = set(PRE_LEDGER_SCHEMA_TABLES)
    issues = [f"missing table {name}" for name in sorted(required_tables - tables)]
    if issues:
        return issues

    required_columns = {
        "frames": {"instrument_unit_id"},
        "measurements": {"mag_err_random", "mag_err_systematic"},
        "calibration_versions": {"source_calibration_version_id"},
        "pipeline_runs": {
            "field_id",
            "series_id",
            "log_dir",
            "log_file",
            "jsonl_log_file",
            "error_log_file",
            "command_json_file",
            "environment_json_file",
        },
    }
    for table_name, required in required_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        for column in sorted(required - actual):
            issues.append(f"missing column {table_name}.{column}")

    measurement_indexes = {index["name"] for index in inspector.get_indexes("measurements")}
    if "ix_measurements_calibration_object_frame" not in measurement_indexes:
        issues.append("missing index measurements.ix_measurements_calibration_object_frame")

    dcm_unique_names = {
        constraint.get("name") for constraint in inspector.get_unique_constraints("detection_catalog_matches")
    }
    if "uq_detection_catalog_matches_frame_source_catalog_algorithm" not in dcm_unique_names:
        issues.append(
            "missing unique constraint "
            "detection_catalog_matches.uq_detection_catalog_matches_frame_source_catalog_algorithm"
        )
    return issues


def adopt_legacy_baseline(engine: Engine, *, root: Path | None = None) -> int:
    """Record validated pre-ledger migrations without re-running historical SQL."""

    validate_manifest(root=root)
    ensure_ledger_table(engine)
    issues = _legacy_baseline_issues(engine)
    if issues:
        raise MigrationError(
            "Refusing to adopt legacy migration history because the database does not match "
            "the expected pre-0.5.39.1 schema: " + "; ".join(issues)
        )
    existing = _ledger_rows(engine)
    legacy = [spec for spec in MIGRATIONS if spec.legacy and spec.migration_id not in existing]
    if not legacy:
        return 0
    with engine.begin() as conn:
        for spec in legacy:
            conn.execute(
                SchemaMigration.__table__.insert().values(
                    migration_id=spec.migration_id,
                    filename=spec.filename,
                    checksum_sha256=migration_checksum(spec, root=root),
                    state="BASELINED",
                    application_version=__version__,
                    execution_ms=0.0,
                    details={"reason": "validated_pre_0_5_39_1_schema"},
                )
            )
    return len(legacy)


def baseline_current_schema(engine: Engine, *, reason: str, root: Path | None = None) -> int:
    """Populate an empty ledger for a schema created directly from current ORM metadata."""

    validate_manifest(root=root)
    ensure_ledger_table(engine)
    existing = _ledger_rows(engine)
    if existing:
        return 0
    with engine.begin() as conn:
        for spec in MIGRATIONS:
            conn.execute(
                SchemaMigration.__table__.insert().values(
                    migration_id=spec.migration_id,
                    filename=spec.filename,
                    checksum_sha256=migration_checksum(spec, root=root),
                    state="BASELINED",
                    application_version=__version__,
                    execution_ms=0.0,
                    details={"reason": reason},
                )
            )
    return len(MIGRATIONS)


def _assert_managed_sql(sql: str, spec: MigrationSpec) -> None:
    if _PSQL_META_RE.search(sql):
        raise MigrationError(
            f"Managed migration {spec.filename} contains a psql meta-command; "
            "managed migrations must be executable through the PostgreSQL driver"
        )


def _escape_percent_for_dbapi(sql: str, *, paramstyle: str | None) -> str:
    """Preserve literal percent characters in raw SQL passed to DBAPI drivers.

    psycopg/psycopg2 use ``pyformat``/``format`` placeholder parsing even when a
    migration has no bound parameters. A normal PostgreSQL LIKE pattern such as
    ``'forced_aperture_%'`` is therefore parsed as a malformed placeholder by
    ``cursor.execute()`` when it is sent through ``exec_driver_sql``. Doubling
    every percent is the DBAPI escape for a literal percent and the driver sends
    the original SQL text to PostgreSQL. Managed migration files themselves stay
    ordinary PostgreSQL SQL and never contain DBAPI placeholders.
    """

    if paramstyle in {"format", "pyformat"}:
        return sql.replace("%", "%%")
    return sql


def _assert_clean_epoch_upgrade_supported(engine: Engine) -> None:
    """Fail before writes when migrate-db is pointed at a pre-0.7 schema."""

    legacy = forbidden_tables(inspect(engine).get_table_names())
    if legacy:
        raise MigrationError(
            "Grandgousier 0.7 is a fresh-baseline clean-schema release; "
            "in-place migration from a 0.6 compatibility schema is unsupported. "
            "Create a separate empty database and run init-db. Legacy tables present: "
            + ", ".join(legacy)
        )


def apply_pending(
    engine: Engine,
    *,
    adopt_legacy: bool = False,
    dry_run: bool = False,
    root: Path | None = None,
) -> list[str]:
    """Apply pending managed migrations in manifest order.

    Historical migrations are never re-executed by this runner. Grandgousier 0.7
    refuses pre-clean-schema databases before any migration-ledger write; the
    historical adopt path remains only for schemas that do not contain retired
    0.6 compatibility relations.
    """

    validate_manifest(root=root)
    _assert_clean_epoch_upgrade_supported(engine)
    if dry_run:
        rows = _ledger_rows(engine)
        applied_ids = set(rows)
        legacy_missing = [s for s in MIGRATIONS if s.legacy and s.migration_id not in applied_ids]
        if legacy_missing:
            if not adopt_legacy:
                raise MigrationError(
                    "Legacy migration history is not present in schema_migrations; "
                    "rerun with --adopt-legacy after verifying this is the existing Grandgousier database"
                )
            issues = _legacy_baseline_issues(engine)
            if issues:
                raise MigrationError(
                    "Refusing to adopt legacy migration history because the database does not match "
                    "the expected pre-0.5.39.1 schema: " + "; ".join(issues)
                )
        return [s.migration_id for s in MIGRATIONS if not s.legacy and s.migration_id not in applied_ids]

    ensure_ledger_table(engine)
    if adopt_legacy:
        adopt_legacy_baseline(engine, root=root)

    rows = _ledger_rows(engine)
    applied_ids = set(rows)
    legacy_missing = [s for s in MIGRATIONS if s.legacy and s.migration_id not in applied_ids]
    if legacy_missing:
        raise MigrationError(
            "Legacy migration history is not present in schema_migrations; "
            "rerun with --adopt-legacy after verifying this is the existing Grandgousier database"
        )

    drift = [item for item in status(engine, root=root) if item.state != "PENDING" and item.checksum_status == "DRIFT"]
    if drift:
        raise MigrationError(
            "Migration checksum drift detected: " + ", ".join(item.migration_id for item in drift)
        )

    applied_now: list[str] = []
    for spec in MIGRATIONS:
        if spec.legacy or spec.migration_id in applied_ids:
            continue
        path = migration_path(spec, root=root)
        sql = path.read_text(encoding="utf-8")
        _assert_managed_sql(sql, spec)
        started = time.perf_counter()
        with engine.begin() as conn:
            driver_sql = _escape_percent_for_dbapi(sql, paramstyle=conn.dialect.paramstyle)
            conn.exec_driver_sql(driver_sql)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            conn.execute(
                SchemaMigration.__table__.insert().values(
                    migration_id=spec.migration_id,
                    filename=spec.filename,
                    checksum_sha256=migration_checksum(spec, root=root),
                    state="APPLIED",
                    application_version=__version__,
                    execution_ms=elapsed_ms,
                    details={},
                )
            )
        applied_ids.add(spec.migration_id)
        applied_now.append(spec.migration_id)
    return applied_now


def summarize_status(items: Iterable[MigrationStatus]) -> dict[str, int]:
    rows = list(items)
    return {
        "total": len(rows),
        "pending": sum(item.state == "PENDING" for item in rows),
        "baselined": sum(item.state == "BASELINED" for item in rows),
        "applied": sum(item.state == "APPLIED" for item in rows),
        "checksum_drift": sum(item.checksum_status == "DRIFT" for item in rows),
    }
