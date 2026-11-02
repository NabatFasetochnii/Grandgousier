from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes

from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection, Engine

from grandgousier import __version__
from grandgousier.db.models import SchemaMigration
from grandgousier.db.schema_contract import forbidden_tables
from grandgousier.resources import migration_resource_path, resource_path


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
    MigrationSpec(
        "mvp7_0_4_null_identity_constraints",
        "migrate_mvp7_0_4_null_identity_constraints.sql",
    ),
    MigrationSpec(
        "mvp7_0_5_interseries_membership_field_integrity",
        "migrate_mvp7_0_5_interseries_membership_field_integrity.sql",
    ),
    MigrationSpec(
        "mvp7_0_6_interseries_edge_delete_restrict",
        "migrate_mvp7_0_6_interseries_edge_delete_restrict.sql",
    ),
    MigrationSpec(
        "mvp7_0_7_interseries_edge_membership_integrity",
        "migrate_mvp7_0_7_interseries_edge_membership_integrity.sql",
    ),
    MigrationSpec(
        "mvp7_0_8_interseries_reference_cv_scope_integrity",
        "migrate_mvp7_0_8_interseries_reference_cv_scope_integrity.sql",
    ),
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
    canonical = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(canonical).hexdigest()


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


def ensure_ledger_table(engine: Engine | Connection) -> None:
    SchemaMigration.__table__.create(engine, checkfirst=True)


def _ledger_rows(engine: Engine) -> dict[str, dict]:
    with engine.connect() as conn:
        return _ledger_rows_from_connection(conn)


def _ledger_rows_from_connection(conn: Connection) -> dict[str, dict]:
    if "schema_migrations" not in inspect(conn).get_table_names():
        return {}
    rows = conn.execute(select(SchemaMigration.__table__)).mappings().all()
    return {str(row["migration_id"]): dict(row) for row in rows}


def _status_from_rows(
    rows: dict[str, dict], *, root: Path | None = None
) -> list[MigrationStatus]:
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


def _status_from_connection(
    conn: Connection, *, root: Path | None = None
) -> list[MigrationStatus]:
    return _status_from_rows(_ledger_rows_from_connection(conn), root=root)


def status(engine: Engine, *, root: Path | None = None) -> list[MigrationStatus]:
    validate_manifest(root=root)
    rows = _ledger_rows(engine)
    return _status_from_rows(rows, root=root)


def _legacy_baseline_issues(engine: Engine | Connection) -> list[str]:
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


def _assert_clean_epoch_upgrade_supported(engine: Engine | Connection) -> None:
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
    expected_pending_checksums: Mapping[str, str] | None = None,
) -> list[str]:
    """Apply pending managed migrations in manifest order.

    Historical migrations are never re-executed by this runner. Grandgousier 0.7
    refuses pre-clean-schema databases before any migration-ledger write; the
    historical adopt path remains only for schemas that do not contain retired
    0.6 compatibility relations. Its session advisory lock serializes only calls
    to apply_pending; init-db and reset-db do not participate in that lock.
    With expected_pending_checksums, the ledger must already exist and the exact
    pending managed IDs and captured SQL checksums must match before any write.
    """

    if expected_pending_checksums is not None and adopt_legacy:
        raise MigrationError("Pinned pending migrations cannot adopt legacy history")
    if expected_pending_checksums is not None and dry_run:
        raise MigrationError("Pinned pending checksums require a non-dry-run migration")
    if dry_run:
        validate_manifest(root=root)
        _assert_clean_epoch_upgrade_supported(engine)
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

    return _apply_pending_with_migration_lock(
        engine, adopt_legacy=adopt_legacy, root=root,
        expected_pending_checksums=expected_pending_checksums,
    )


# Reserved session advisory-lock key: the ASCII pair "GRAN" / "DMIG".
_MIGRATION_LOCK_NAMESPACE = 0x4752414E
_MIGRATION_LOCK_KEY = 0x444D4947


def _adopt_legacy_baseline_from_connection(
    conn: Connection,
    *,
    root: Path | None = None,
    _owner_guard: Callable[[], None] | None = None,
) -> int:
    validate_manifest(root=root)
    with conn.begin():
        if _owner_guard is not None:
            _owner_guard()
        issues = _legacy_baseline_issues(conn)
        if issues:
            raise MigrationError(
                "Refusing to adopt legacy migration history because the database does not match "
                "the expected pre-0.5.39.1 schema: " + "; ".join(issues)
            )
        existing = _ledger_rows_from_connection(conn)
        legacy = [spec for spec in MIGRATIONS if spec.legacy and spec.migration_id not in existing]
        for spec in legacy:
            if _owner_guard is not None:
                _owner_guard()
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


def _physical_migration_connection(conn: Connection) -> object:
    # Accessing conn.connection on an invalidated SQLAlchemy Connection can
    # transparently check out a NEW physical backend. Never do that here.
    if conn.closed or conn.invalidated:
        raise MigrationError("The schema migration advisory-lock backend is unavailable")
    try:
        driver = conn.connection.driver_connection
    except Exception:
        raise MigrationError("The schema migration physical backend cannot be verified") from None
    if driver is None:
        raise MigrationError("The schema migration physical backend cannot be verified")
    return driver


def _assert_migration_lock_owner(conn: Connection, driver: object, pid: int) -> None:
    if _physical_migration_connection(conn) is not driver:
        raise MigrationError("The schema migration advisory-lock backend changed")
    actual_pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
    if (
        type(actual_pid) is not int
        or actual_pid != pid
        or _physical_migration_connection(conn) is not driver
    ):
        raise MigrationError("The schema migration advisory-lock backend changed")


def _apply_pending_with_migration_lock(
    engine: Engine, *, adopt_legacy: bool, root: Path | None,
    expected_pending_checksums: Mapping[str, str] | None = None,
) -> list[str]:
    """Serialize apply_pending callers while keeping every DB operation on one connection.

    This lock covers only migrate-db/apply_pending work. It does not serialize
    init-db, reset-db, or direct use of the baseline/adoption helpers.
    """

    if expected_pending_checksums is not None:
        if adopt_legacy:
            raise MigrationError("Pinned pending migrations cannot adopt legacy history")
        if not isinstance(expected_pending_checksums, Mapping):
            raise MigrationError("Pinned pending checksums must be a mapping")
        # Freeze caller input for the entire locked operation.
        expected_pending_checksums = dict(expected_pending_checksums)
    if not callable(getattr(engine, "connect", None)):
        raise MigrationError(
            "migrate-db requires a lock-capable SQLAlchemy Engine for the fresh-baseline "
            "clean-schema migration runner"
        )

    lock_attempt_started = False
    lock_attempt_resolved = False
    lock_acquired = False
    owner_lost = False
    operation_error: BaseException | None = None
    with engine.connect() as conn:
        def check_inside_transaction() -> None:
            nonlocal owner_lost
            try:
                _assert_migration_lock_owner(conn, owner_driver, owner_pid)
            except BaseException:
                owner_lost = True
                raise

        def probe_between_transactions() -> None:
            nonlocal owner_lost
            try:
                if conn.in_transaction():
                    raise MigrationError("Schema migration owner probe found an open transaction")
                check_inside_transaction()  # SELECT starts an implicit read transaction.
                if not conn.in_transaction():
                    raise MigrationError("Schema migration owner probe did not start a transaction")
                conn.rollback()
                if conn.in_transaction():
                    raise MigrationError("Schema migration owner probe did not close its transaction")
            except BaseException:
                owner_lost = True
                # An unsuccessful probe cannot authorize the next transaction.
                try:
                    if conn.in_transaction():
                        conn.rollback()
                except BaseException:
                    pass
                raise

        try:
            owner_driver = _physical_migration_connection(conn)  # keep a strong reference
            lock_attempt_started = True
            lock_result = conn.exec_driver_sql(
                "SELECT pg_try_advisory_lock("
                f"{_MIGRATION_LOCK_NAMESPACE}, {_MIGRATION_LOCK_KEY}), pg_backend_pid()"
            )
            locked, pid = lock_result.one()
            if (
                type(locked) is not bool
                or type(pid) is not int
                or pid <= 0
                or _physical_migration_connection(conn) is not owner_driver
            ):
                raise MigrationError("The schema migration advisory-lock result is uncertain")
            owner_pid = pid
            lock_acquired = locked
            lock_attempt_resolved = True
            if not lock_acquired:
                raise MigrationError(
                    "Another migrate-db/apply_pending operation holds the schema migration lock"
                )
            # Session advisory locks survive transaction boundaries; commit this query
            # before beginning separately scoped inspection and migration transactions.
            conn.commit()
            probe_between_transactions()

            validate_manifest(root=root)
            with conn.begin():
                check_inside_transaction()
                _assert_clean_epoch_upgrade_supported(conn)
            probe_between_transactions()
            with conn.begin():
                check_inside_transaction()
                if expected_pending_checksums is None:
                    ensure_ledger_table(conn)
                elif "schema_migrations" not in inspect(conn).get_table_names():
                    raise MigrationError("Pinned migrations require an existing schema_migrations ledger")
            probe_between_transactions()

            if adopt_legacy:
                _adopt_legacy_baseline_from_connection(
                    conn, root=root, _owner_guard=check_inside_transaction
                )
                probe_between_transactions()

            with conn.begin():
                check_inside_transaction()
                rows = _ledger_rows_from_connection(conn)
            probe_between_transactions()
            applied_ids = set(rows)
            if expected_pending_checksums is not None:
                known_ids = {spec.migration_id for spec in MIGRATIONS}
                if applied_ids - known_ids:
                    raise MigrationError("Pinned migration ledger has unexpected history")
                missing_history = False
                for spec in MIGRATIONS:
                    row = rows.get(spec.migration_id)
                    if row is None:
                        missing_history = True
                    elif (
                        missing_history or row.get("filename") != spec.filename
                        or row.get("state") not in {"APPLIED", "BASELINED"}
                    ):
                        raise MigrationError("Pinned migration ledger has unexpected history")
            legacy_missing = [
                spec for spec in MIGRATIONS if spec.legacy and spec.migration_id not in applied_ids
            ]
            if legacy_missing:
                raise MigrationError(
                    "Legacy migration history is not present in schema_migrations; rerun with "
                    "--adopt-legacy after verifying this is the existing Grandgousier database"
                )

            with conn.begin():
                check_inside_transaction()
                drift = [
                    spec.migration_id
                    for spec in MIGRATIONS
                    if (row := rows.get(spec.migration_id)) is not None
                    and str(row.get("state") or "APPLIED") != "PENDING"
                    and row.get("checksum_sha256") != migration_checksum(spec, root=root)
                ]
            probe_between_transactions()
            if drift:
                raise MigrationError(
                    "Migration checksum drift detected: "
                    + ", ".join(drift)
                )

            pending = [
                spec for spec in MIGRATIONS if not spec.legacy and spec.migration_id not in applied_ids
            ]
            if (expected_pending_checksums is not None
                    and set(expected_pending_checksums) != {spec.migration_id for spec in pending}):
                raise MigrationError("Pinned pending migration IDs do not match the ledger")

            # Capture all pending files before the first migration transaction. The
            # decoded SQL and canonical checksum must come from the SAME read, even
            # if a path changes after this point (or between two migrations).
            captured: list[tuple[MigrationSpec, str, str]] = []
            for spec in pending:
                data = migration_path(spec, root=root).read_bytes()
                try:
                    sql = data.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise MigrationError(f"Managed migration {spec.filename} is not UTF-8") from None
                _assert_managed_sql(sql, spec)
                checksum = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
                if (expected_pending_checksums is not None
                        and (not isinstance(expected_pending_checksums[spec.migration_id], str)
                             or expected_pending_checksums[spec.migration_id] != checksum)):
                    raise MigrationError(f"Pinned pending migration checksum mismatch: {spec.migration_id}")
                captured.append((spec, sql, checksum))

            applied_now: list[str] = []
            for spec, sql, checksum in captured:
                started = time.perf_counter()
                with conn.begin():
                    check_inside_transaction()
                    driver_sql = _escape_percent_for_dbapi(sql, paramstyle=conn.dialect.paramstyle)
                    conn.exec_driver_sql(driver_sql)
                    check_inside_transaction()  # no ledger INSERT after lost SQL backend
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    conn.execute(
                        SchemaMigration.__table__.insert().values(
                            migration_id=spec.migration_id,
                            filename=spec.filename,
                            checksum_sha256=checksum,
                            state="APPLIED",
                            application_version=__version__,
                            execution_ms=elapsed_ms,
                            details={},
                        )
                    )
                probe_between_transactions()
                applied_ids.add(spec.migration_id)
                applied_now.append(spec.migration_id)
            return applied_now
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            lock_result_uncertain = lock_attempt_started and not lock_attempt_resolved
            try:
                if lock_result_uncertain or owner_lost:
                    # The try-lock may have succeeded even though its result was not
                    # received, or a different backend may have replaced the owner.
                    # Never try to unlock a replacement physical connection.
                    conn.invalidate()
                else:
                    if lock_acquired and _physical_migration_connection(conn) is not owner_driver:
                        raise MigrationError("The schema migration advisory-lock backend changed")
                    if conn.in_transaction():
                        conn.rollback()
                    if lock_acquired:
                        with conn.begin():
                            check_inside_transaction()  # first SQL in the unlock transaction
                            result = conn.exec_driver_sql(
                                "SELECT pg_advisory_unlock("
                                f"{_MIGRATION_LOCK_NAMESPACE}, {_MIGRATION_LOCK_KEY})"
                            )
                            if not bool(result.scalar_one()):
                                raise RuntimeError(
                                    "PostgreSQL reported that the migration session lock was not held"
                                )
            except BaseException as exc:
                cleanup_error = exc
                try:
                    if conn.in_transaction() and not lock_result_uncertain:
                        conn.rollback()
                except BaseException:
                    pass
                try:
                    conn.invalidate()
                except BaseException:
                    pass
            if cleanup_error is not None and operation_error is None:
                raise MigrationError(
                    "Could not release the migration advisory lock; the connection was discarded"
                ) from cleanup_error


def summarize_status(items: Iterable[MigrationStatus]) -> dict[str, int]:
    rows = list(items)
    return {
        "total": len(rows),
        "pending": sum(item.state == "PENDING" for item in rows),
        "baselined": sum(item.state == "BASELINED" for item in rows),
        "applied": sum(item.state == "APPLIED" for item in rows),
        "checksum_drift": sum(item.checksum_status == "DRIFT" for item in rows),
    }


# This opt-in verifier deliberately does NOT use status(), ensure_ledger_table(),
# Settings, SQLAlchemy engines, the migration runner, or a user-supplied root.
_P207_PRIVATE_CONFIG_ENV = "GRANDGOUSIER_P207_PRIVATE_CONFIG"
_P207_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_P207_URL_RE = re.compile(
    r"postgresql\+psycopg://grandgousier:(?P<password>(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)"
    r"@127\.0\.0\.1:5432/grandgousier_070_gpx_full\Z"
)
_P207_LAST_FIVE = (
    "mvp7_0_4_null_identity_constraints",
    "mvp7_0_5_interseries_membership_field_integrity",
    "mvp7_0_6_interseries_edge_delete_restrict",
    "mvp7_0_7_interseries_edge_membership_integrity",
    "mvp7_0_8_interseries_reference_cv_scope_integrity",
)
_P207_TARGET_DB = "grandgousier_070_gpx_full"
_P207_TARGET_USER = "grandgousier"
_P207_PROBE_SQL = (
    "SELECT current_database(), "
    "(SELECT oid::bigint FROM pg_database WHERE datname = current_database()), "
    "current_user, session_user, "
    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
    "current_setting('server_version_num')::integer, pg_backend_pid(), "
    "host(inet_server_addr()), inet_server_port(), "
    "current_setting('default_transaction_read_only') = 'on', "
    "current_setting('transaction_read_only') = 'on', "
    "current_setting('transaction_isolation') = 'repeatable read', "
    "current_setting('statement_timeout') = '5s', "
    "current_setting('lock_timeout') = '2s', "
    "current_setting('idle_in_transaction_session_timeout') = '10s'"
)
_P207_LEDGER_SQL = (
    "SELECT migration_id, filename, checksum_sha256, state "
    "FROM public.schema_migrations LIMIT 38"
)


def _p207_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _p207_private_source_path() -> Path:
    raw_path = os.environ.get(_P207_PRIVATE_CONFIG_ENV)
    if (not isinstance(raw_path, str) or not raw_path or raw_path != raw_path.strip()):
        raise ValueError("Guarded private source path is not explicitly configured")
    path = Path(raw_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Guarded private source path is invalid")
    return path


def _p207_path_snapshot(path: Path) -> tuple[os.stat_result, ...]:
    """Refuse any ancestor/final reparse point, non-regular source or unknown identity."""
    if path != _p207_private_source_path():
        raise ValueError("Guarded private source does not match the explicit path")
    chain = (*reversed(path.parents), path)
    result = []
    for entry in chain:
        info = os.lstat(entry)
        if info.st_ino == 0 or stat.S_ISLNK(info.st_mode):
            raise ValueError("Unverifiable guarded source path")
        if sys.platform == "win32":
            attributes = getattr(info, "st_file_attributes", None)
            if attributes is None or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Guarded source has a reparse point")
        if not (stat.S_ISREG(info.st_mode) if entry == path else stat.S_ISDIR(info.st_mode)):
            raise ValueError("Guarded source must be a regular file under directories")
        result.append(info)
    if not 0 < result[-1].st_size <= _P207_MAX_SOURCE_BYTES:
        raise ValueError("Guarded source size is invalid")
    return tuple(result)


def _p207_open_readonly(path: Path) -> int:
    if sys.platform != "win32":
        if not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("No no-follow open is available")
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))

    # Windows os.open does not guarantee O_NOFOLLOW. Open the final component
    # without following reparse points, and refuse its handle's reparse flag.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    get_info.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = (("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD))

    handle = create(str(path), 0x80000000, 0x00000001, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("Could not safely open guarded source")
    try:
        tag = AttributeTagInfo()
        if not get_info(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise OSError("Could not verify guarded source handle")
        if tag.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Guarded source handle is a reparse point")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # CRT now owns the handle.
        return descriptor
    finally:
        if handle is not None:
            close(handle)


def _p207_read_private_source() -> bytes:
    path = _p207_private_source_path()
    before = _p207_path_snapshot(path)
    fd = _p207_open_readonly(path)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _p207_identity(opened) != _p207_identity(before[-1]):
            raise ValueError("Guarded source changed during open")
        data = os.read(fd, _P207_MAX_SOURCE_BYTES + 1)
        if (len(data) != before[-1].st_size or len(data) > _P207_MAX_SOURCE_BYTES
                or os.read(fd, 1)):
            raise ValueError("Guarded source changed during read")
        after_open = os.fstat(fd)
        after_path = _p207_path_snapshot(path)
        if (_p207_identity(after_open) != _p207_identity(opened)
                or any(_p207_identity(a) != _p207_identity(b)
                       for a, b in zip(before, after_path, strict=True))):
            raise ValueError("Guarded source path changed during read")
        return data
    finally:
        os.close(fd)


def _p207_unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("Duplicate or invalid guarded config key")
        result[key] = value
    return result


def _p207_password_from_source(data: bytes) -> str:
    import yaml

    text = data.decode("utf-8", errors="strict")

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            pairs = [(self.construct_object(k, deep=deep), self.construct_object(v, deep=deep))
                     for k, v in node.value]
            return _p207_unique_pairs(pairs)

    if text.lstrip().startswith(("{", "[")):
        config = json.loads(
            text, object_pairs_hook=_p207_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Invalid JSON value")),
        )
    else:
        config = yaml.load(text, Loader=UniqueLoader)
    if type(config) is not dict or type(config.get("database_url")) is not str:
        raise ValueError("Guarded config has no single database_url")

    forbidden = {
        "database", "db", "db_url", "db_uri", "db_dsn", "dsn", "dbname",
        "postgres_url", "postgresql_url", "pgdsn", "connection_string", "connection_url",
        "host", "hostaddr", "port", "user", "username", "password",
        "pg_host", "pg_port", "pg_user", "pg_password",
    }
    seen: set[int] = set()

    def check(value: object, *, top: bool = False) -> None:
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise ValueError("Cyclic guarded config")
            seen.add(id(value))
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("Invalid guarded config key")
                normalized = key.casefold().replace("-", "_")
                compact = normalized.replace("_", "")
                if (not (top and key == "database_url")
                        and (normalized == "database_url" or normalized in forbidden
                             or normalized.startswith(("database_", "postgres_", "pg_"))
                             or compact.startswith(("grandgousierdatabase", "grandgousierdb"))
                             or compact in {"databaseurl", "databaseuri", "dburl", "dburi",
                                            "pgpassword", "pguser", "pghost", "pghostaddr",
                                            "pgport", "pgdatabase", "pgdbname", "pgservice"})):
                    raise ValueError("Alternate guarded database credentials")
                if top and key == "database_url":
                    continue
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif (isinstance(value, str)
              and re.search(r"postgres(?:ql)?(?:\+psycopg)?://", value, re.IGNORECASE)):
            raise ValueError("Alternate guarded database URL")

    check(config, top=True)
    match = _P207_URL_RE.fullmatch(config["database_url"])
    if match is None:
        raise ValueError("Guarded database target is not canonical")
    encoded = match.group("password")
    password = unquote_to_bytes(encoded).decode("utf-8", errors="strict")
    if (not password or any(ord(char) < 32 or ord(char) == 127 for char in password)
            or quote(password, safe="-._~") != encoded):
        raise ValueError("Guarded password encoding is not canonical")
    return password


def _p207_expected_ledger() -> tuple[tuple[MigrationSpec, str], ...]:
    if (len(MIGRATIONS) != 37 or sum(spec.legacy for spec in MIGRATIONS) != 16
            or tuple(spec.migration_id for spec in MIGRATIONS[-5:]) != _P207_LAST_FIVE
            or any(spec.filename != f"migrate_{spec.migration_id}.sql" for spec in MIGRATIONS)):
        raise ValueError("Guarded migration manifest is not the fixed 37-entry manifest")
    validate_manifest()
    return tuple((spec, migration_checksum(spec)) for spec in MIGRATIONS)


def _p207_ledger_state(rows: object, expected: tuple[tuple[MigrationSpec, str], ...]) -> str:
    if type(rows) not in (list, tuple) or len(rows) not in (35, 36, 37):
        raise ValueError("Guarded ledger length is not an allowed prefix")
    required = {spec.migration_id: (spec.filename, digest, "BASELINED" if i < 32 else "APPLIED")
                for i, (spec, digest) in enumerate(expected[:len(rows)])}
    if len(required) != len(rows) or len(expected) != 37:
        raise ValueError("Guarded ledger manifest is not unique")
    seen: set[str] = set()
    for row in rows:
        if type(row) not in (tuple, list) or len(row) != 4 or any(type(x) is not str for x in row):
            raise ValueError("Guarded ledger row is invalid")
        migration_id, filename, checksum, state = row
        if migration_id in seen or required.get(migration_id) != (filename, checksum, state):
            raise ValueError("Guarded ledger drift or extra history")
        seen.add(migration_id)
    if seen != required.keys():
        raise ValueError("Guarded ledger is not an exact logical prefix")
    return {35: "OLD35", 36: "PARTIAL36", 37: "NEW37"}[len(rows)]


def _p207_probe_failures(probe: object, conn: object) -> tuple[str, ...]:
    """Classify the existing aggregate probe predicates without rendering any row value.

    The ledger guard compares tuple slices to tuples: a shape-valid list therefore
    fails the aggregate guard even when every individual position matches.
    """
    if type(probe) not in (tuple, list) or len(probe) != 15:
        return ("ROW_SHAPE",)

    failed = []
    if probe[0] != _P207_TARGET_DB:
        failed.append("P00")
    if type(probe[1]) is not int or probe[1] != 23069087:
        failed.append("P01")
    if probe[2] != _P207_TARGET_USER:
        failed.append("P02")
    if probe[3] != _P207_TARGET_USER:
        failed.append("P03")
    if type(probe[4]) is not bool or probe[4] is not False:
        failed.append("P04")
    if type(probe[5]) is not int or not 180000 <= probe[5] < 190000:
        failed.append("P05")
    backend_pid = conn.info.backend_pid
    if (type(probe[6]) is not int or probe[6] <= 0
            or type(backend_pid) is not int or backend_pid != probe[6]):
        failed.append("P06")
    if probe[7] != "127.0.0.1":
        failed.append("P07")
    if type(probe[8]) is not int or probe[8] != 5432:
        failed.append("P08")
    for position in range(9, 15):
        if type(probe[position]) is not bool or probe[position] is not True:
            failed.append(f"P{position:02d}")
    if type(probe) is list:
        failed.append("ROW_LIST_SLICING")
    return tuple(failed)


def guarded_p207_test_readonly_probe() -> tuple[str, ...]:
    """Opt-in TEST-only backend probe; no ledger or science queries and no readiness claim."""
    if any(name.upper().startswith("PG") or name.upper() == "GRANDGOUSIER_DATABASE_URL"
           for name in os.environ):
        raise ValueError("Guarded PostgreSQL environment names are not allowed")
    password = _p207_password_from_source(_p207_read_private_source())
    _p207_expected_ledger()  # Source manifest gate only; never read the DB ledger.
    import psycopg  # No driver import until source, target and manifest checks pass.

    conn = None
    try:
        conn = psycopg.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=5432,
            dbname=_P207_TARGET_DB, user=_P207_TARGET_USER, password=password,
            connect_timeout=3, autocommit=True,
            options=("-c default_transaction_read_only=on -c statement_timeout=5000 "
                     "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"),
        )
        conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        probe = conn.execute(_P207_PROBE_SQL).fetchone()
        result = _p207_probe_failures(probe, conn)
    finally:
        if conn is not None:
            cleanup_failed = False
            try:
                conn.rollback()
            except BaseException:  # noqa: BLE001 - uncertain cleanup must still close
                cleanup_failed = True
            try:
                conn.close()
            except BaseException:  # noqa: BLE001 - uncertain cleanup must STOP
                cleanup_failed = True
            if cleanup_failed:
                raise RuntimeError("Guarded connection cleanup is uncertain") from None
    return result


def guarded_p207_test_readonly_ledger() -> str:
    """Opt-in TEST-only ledger evidence; never grants migration or science readiness."""
    if any(name.upper().startswith("PG") or name.upper() == "GRANDGOUSIER_DATABASE_URL"
           for name in os.environ):
        raise ValueError("Guarded PostgreSQL environment names are not allowed")
    password = _p207_password_from_source(_p207_read_private_source())
    expected = _p207_expected_ledger()
    import psycopg  # No driver import until source, target and manifest checks pass.

    conn = None
    try:
        conn = psycopg.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=5432,
            dbname=_P207_TARGET_DB, user=_P207_TARGET_USER, password=password,
            connect_timeout=3, autocommit=True,
            options=("-c default_transaction_read_only=on -c statement_timeout=5000 "
                     "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"),
        )
        conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        probe = conn.execute(_P207_PROBE_SQL).fetchone()
        if (type(probe) not in (tuple, list) or len(probe) != 15
                or probe[:5] != (_P207_TARGET_DB, 23069087, _P207_TARGET_USER,
                                  _P207_TARGET_USER, False)
                or type(probe[1]) is not int or type(probe[4]) is not bool
                or type(probe[5]) is not int or not 180000 <= probe[5] < 190000
                or type(probe[6]) is not int or probe[6] <= 0
                or type(conn.info.backend_pid) is not int or conn.info.backend_pid != probe[6]
                or probe[7:9] != ("127.0.0.1", 5432) or type(probe[8]) is not int
                or probe[9:] != (True, True, True, True, True, True)
                or any(type(value) is not bool for value in probe[9:])):
            raise ValueError("Guarded backend identity or read-only safety check failed")
        rows = conn.execute(_P207_LEDGER_SQL).fetchall()
        result = _p207_ledger_state(rows, expected)
    finally:
        if conn is not None:
            cleanup_failed = False
            try:
                conn.rollback()
            except BaseException:  # noqa: BLE001 - uncertain cleanup must still close
                cleanup_failed = True
            try:
                conn.close()
            except BaseException:  # noqa: BLE001 - uncertain cleanup must STOP
                cleanup_failed = True
            if cleanup_failed:
                raise RuntimeError("Guarded connection cleanup is uncertain") from None
    return result


# This third opt-in is deliberately independent of the existing probe/ledger modes.
# The migration bytes, not a guessed future schema, define its OLD35 predecessor.
_P207_INTEGRITY_SQL_SHA = (
    "8c987a810d1b93a43dbe9e3b8e295915af1dbd82e9b6dc3210330ba7b7bf13a7",
    "2f121e971506a71bb633084982510a88ce70b6fd41d154100e051bda8e437d61",
)
_P207_INTEGRITY_TABLES = (
    "calibration_versions", "field_series", "interseries_calibration_generations",
    "interseries_calibration_series", "interseries_calibration_edges",
)
_P207_INTEGRITY_NEW_NAMES = frozenset({
    "fk_interseries_edges_left_generation_member",
    "fk_interseries_edges_right_generation_member",
    "uq_calibration_versions_id_field",
    "uq_calibration_versions_id_field_series",
    "fk_interseries_generations_reference_member",
    "fk_interseries_series_science_cv_field",
    "fk_interseries_series_science_cv_field_series",
})
_P207_INTEGRITY_SIZES_SQL = (
    "SELECT c.relname::text, c.relkind::text, c.relispartition, "
    "pg_catalog.pg_total_relation_size(c.oid)::bigint "
    "FROM pg_catalog.pg_class AS c JOIN pg_catalog.pg_namespace AS n "
    "ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN "
    "('calibration_versions','field_series','interseries_calibration_generations',"
    "'interseries_calibration_series','interseries_calibration_edges') LIMIT 6"
)
_P207_INTEGRITY_NEW_SQL = (
    "SELECT c.conname::text FROM pg_catalog.pg_constraint AS c "
    "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.connamespace "
    "WHERE n.nspname='public' AND c.conname IN "
    "('fk_interseries_edges_left_generation_member',"
    "'fk_interseries_edges_right_generation_member',"
    "'uq_calibration_versions_id_field','uq_calibration_versions_id_field_series',"
    "'fk_interseries_generations_reference_member',"
    "'fk_interseries_series_science_cv_field',"
    "'fk_interseries_series_science_cv_field_series') LIMIT 8"
)
_P207_INTEGRITY_COLUMNS_SQL = (
    "SELECT c.relname::text, a.attname::text, a.atttypid::regtype::text, a.attnotnull "
    "FROM pg_catalog.pg_class AS c JOIN pg_catalog.pg_namespace AS n "
    "ON n.oid=c.relnamespace JOIN pg_catalog.pg_attribute AS a ON a.attrelid=c.oid "
    "WHERE n.nspname='public' AND c.relname IN "
    "('calibration_versions','field_series','interseries_calibration_generations',"
    "'interseries_calibration_series','interseries_calibration_edges') "
    "AND a.attnum>0 AND NOT a.attisdropped AND a.attname IN "
    "('calibration_version_id','field_id','series_id','product_role','lifecycle_status',"
    "'interseries_generation_id','reference_series_id','interseries_series_id',"
    "'science_calibration_version_id','is_reference_series','left_series_id',"
    "'right_series_id','pair_status') LIMIT 41"
)
_P207_INTEGRITY_CONSTRAINTS_SQL = (
    "SELECT child.relname::text, c.conname::text, c.contype::text, "
    "pn.nspname::text, parent.relname::text, "
    "ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY AS k(num,ord) "
    "JOIN pg_catalog.pg_attribute AS a ON a.attrelid=c.conrelid AND a.attnum=k.num "
    "ORDER BY k.ord), "
    "ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY AS k(num,ord) "
    "JOIN pg_catalog.pg_attribute AS a ON a.attrelid=c.confrelid AND a.attnum=k.num "
    "ORDER BY k.ord), "
    "CASE WHEN c.confdelsetcols IS NULL THEN NULL ELSE "
    "ARRAY(SELECT a.attname::text FROM unnest(c.confdelsetcols) WITH ORDINALITY AS k(num,ord) "
    "JOIN pg_catalog.pg_attribute AS a ON a.attrelid=c.conrelid AND a.attnum=k.num "
    "ORDER BY k.ord) END, "
    "c.confdeltype::text, c.confupdtype::text, c.confmatchtype::text, "
    "c.convalidated, c.condeferrable, c.condeferred, c.conislocal, c.coninhcount, "
    "c.conenforced, i.indisunique, i.indisvalid, "
    "i.indisready, i.indimmediate, "
    "CASE WHEN c.contype='c' THEN pg_catalog.pg_get_expr(c.conbin,c.conrelid) END "
    "FROM pg_catalog.pg_constraint AS c JOIN pg_catalog.pg_class AS child "
    "ON child.oid=c.conrelid JOIN pg_catalog.pg_namespace AS n "
    "ON n.oid=child.relnamespace LEFT JOIN pg_catalog.pg_class AS parent "
    "ON parent.oid=c.confrelid LEFT JOIN pg_catalog.pg_namespace AS pn "
    "ON pn.oid=parent.relnamespace LEFT JOIN pg_catalog.pg_index AS i "
    "ON i.indexrelid=c.conindid WHERE n.nspname='public' AND "
    "((c.contype='f' AND child.relname IN "
    "('interseries_calibration_generations','interseries_calibration_series',"
    "'interseries_calibration_edges')) OR c.conname IN "
    "('calibration_versions_pkey','fields_pkey','field_series_pkey',"
    "'uq_field_series_series_field',"
    "'uq_interseries_generations_id_field',"
    "'uq_interseries_calibration_series_generation_series',"
    "'uq_interseries_calibration_edges_generation_pair',"
    "'ck_interseries_calibration_edges_series_order')) LIMIT 33"
)
_P207_INTEGRITY_TRIGGERS_SQL = (
    "SELECT c.conname::text, child.relname::text, t.tgisinternal, "
    "t.tgenabled::text, t.tgtype::integer, t.tgdeferrable, t.tginitdeferred, "
    "r.relname::text, p.proname::text FROM pg_catalog.pg_trigger AS t "
    "JOIN pg_catalog.pg_constraint AS c ON c.oid=t.tgconstraint "
    "JOIN pg_catalog.pg_class AS child ON child.oid=c.conrelid "
    "JOIN pg_catalog.pg_namespace AS n ON n.oid=child.relnamespace "
    "JOIN pg_catalog.pg_class AS r ON r.oid=t.tgrelid "
    "JOIN pg_catalog.pg_proc AS p ON p.oid=t.tgfoid "
    "WHERE n.nspname='public' AND c.contype='f' AND child.relname IN "
    "('interseries_calibration_generations','interseries_calibration_series',"
    "'interseries_calibration_edges') LIMIT 37"
)
_P207_INTEGRITY_DATA = (
    ("calibration_versions", 10000,
     "calibration_version_id, field_id, series_id, product_role, lifecycle_status"),
    ("field_series", 18, "series_id, field_id"),
    ("interseries_calibration_generations", 4,
     "interseries_generation_id, field_id, reference_series_id"),
    ("interseries_calibration_series", 36,
     ("interseries_series_id, interseries_generation_id, field_id, series_id, "
      "science_calibration_version_id, is_reference_series")),
    ("interseries_calibration_edges", 144,
     "interseries_generation_id, left_series_id, right_series_id, pair_status"),
)


def _p207_integrity_source_gate(expected: tuple[tuple[MigrationSpec, str], ...]) -> None:
    for (spec, digest), pinned in zip(expected[-2:], _P207_INTEGRITY_SQL_SHA, strict=True):
        packaged = migration_path(spec).read_bytes()
        mirror = (Path(__file__).resolve().parents[2] / "scripts" / spec.filename).read_bytes()
        if (packaged != mirror or hashlib.sha256(packaged).hexdigest() != pinned
                or digest != pinned):
            raise ValueError("Guarded integrity migration source mismatch")


def _p207_integrity_catalog(conn: object) -> None:
    sizes = conn.execute(_P207_INTEGRITY_SIZES_SQL).fetchall()
    if (type(sizes) not in (list, tuple) or len(sizes) != 5
            or {row[0] for row in sizes} != set(_P207_INTEGRITY_TABLES)
            or any(len(row) != 4 or row[1] != "r" or row[2] is not False
                   or type(row[3]) is not int or not 0 <= row[3] <= 67108864
                   for row in sizes)):
        raise ValueError("Guarded integrity relation size or identity mismatch")
    if conn.execute(_P207_INTEGRITY_NEW_SQL).fetchall() != []:
        raise ValueError("Guarded integrity target constraints already exist")

    bigint = "bigint"
    columns = {
        ("calibration_versions", "calibration_version_id"): (bigint, True),
        ("calibration_versions", "field_id"): (bigint, False),
        ("calibration_versions", "series_id"): (bigint, False),
        ("calibration_versions", "product_role"): ("character varying", True),
        ("calibration_versions", "lifecycle_status"): ("character varying", True),
        ("field_series", "series_id"): (bigint, True),
        ("field_series", "field_id"): (bigint, True),
        ("interseries_calibration_generations", "interseries_generation_id"): (bigint, True),
        ("interseries_calibration_generations", "field_id"): (bigint, True),
        ("interseries_calibration_generations", "reference_series_id"): (bigint, True),
        ("interseries_calibration_series", "interseries_series_id"): (bigint, True),
        ("interseries_calibration_series", "interseries_generation_id"): (bigint, True),
        ("interseries_calibration_series", "field_id"): (bigint, True),
        ("interseries_calibration_series", "series_id"): (bigint, False),
        ("interseries_calibration_series", "science_calibration_version_id"): (bigint, True),
        ("interseries_calibration_series", "is_reference_series"): ("boolean", True),
        ("interseries_calibration_edges", "interseries_generation_id"): (bigint, True),
        ("interseries_calibration_edges", "left_series_id"): (bigint, True),
        ("interseries_calibration_edges", "right_series_id"): (bigint, True),
        ("interseries_calibration_edges", "pair_status"): ("character varying", True),
    }
    found = conn.execute(_P207_INTEGRITY_COLUMNS_SQL).fetchall()
    if (len(found) != len(columns) or len({(r[0], r[1]) for r in found}) != len(columns)
            or any(len(r) != 4 or columns.get((r[0], r[1])) != (r[2], r[3])
                   or type(r[3]) is not bool for r in found)):
        raise ValueError("Guarded integrity column contract mismatch")

    # (child, name or None, parent, ordered child/parent keys, delete action,
    # SET NULL column list). The unnamed FKs are discovered by their exact keys.
    fks = (
        ("interseries_calibration_generations", None, "fields",
         ("field_id",), ("field_id",), "c", None),
        ("interseries_calibration_generations", None, "instrument_units",
         ("instrument_unit_id",), ("instrument_unit_id",), "n", None),
        ("interseries_calibration_generations", "fk_interseries_generations_reference_series_field",
         "field_series", ("reference_series_id", "field_id"), ("series_id", "field_id"), "r", None),
        ("interseries_calibration_series", "fk_interseries_series_generation_field",
         "interseries_calibration_generations", ("interseries_generation_id", "field_id"),
         ("interseries_generation_id", "field_id"), "c", None),
        ("interseries_calibration_series", "fk_interseries_series_series_field",
         "field_series", ("series_id", "field_id"), ("series_id", "field_id"), "n", ("series_id",)),
        ("interseries_calibration_series", None, "calibration_versions",
         ("science_calibration_version_id",), ("calibration_version_id",), "r", None),
        ("interseries_calibration_edges", None, "interseries_calibration_generations",
         ("interseries_generation_id",), ("interseries_generation_id",), "c", None),
        ("interseries_calibration_edges", "fk_interseries_edges_left_series_restrict",
         "field_series", ("left_series_id",), ("series_id",), "r", None),
        ("interseries_calibration_edges", "fk_interseries_edges_right_series_restrict",
         "field_series", ("right_series_id",), ("series_id",), "r", None),
    )
    uniques = {
        ("calibration_versions", "calibration_versions_pkey"): ("calibration_version_id",),
        ("fields", "fields_pkey"): ("field_id",),
        ("field_series", "field_series_pkey"): ("series_id",),
        ("field_series", "uq_field_series_series_field"): ("series_id", "field_id"),
        ("interseries_calibration_generations", "uq_interseries_generations_id_field"):
            ("interseries_generation_id", "field_id"),
        ("interseries_calibration_series", "uq_interseries_calibration_series_generation_series"):
            ("interseries_generation_id", "series_id"),
        ("interseries_calibration_edges", "uq_interseries_calibration_edges_generation_pair"):
            ("interseries_generation_id", "left_series_id", "right_series_id"),
    }
    rows = conn.execute(_P207_INTEGRITY_CONSTRAINTS_SQL).fetchall()
    if len(rows) != len(fks) + len(uniques) + 1:
        raise ValueError("Guarded integrity constraint inventory mismatch")
    seen_fk: set[tuple[str, tuple[str, ...]]] = set()
    fk_names: dict[str, tuple[str, str, str]] = {}
    seen_unique: set[tuple[str, str]] = set()
    order_seen = False
    for row in rows:
        if (len(row) != 22 or row[3] not in (None, "public") or row[11] is not True
                or row[14] is not True or type(row[15]) is not int or row[15] != 0
                or row[16] is not True):
            raise ValueError("Guarded integrity constraint is not enforced or local")
        child, name, kind = row[:3]
        child_keys = tuple(row[5])
        if kind == "f":
            matches = [fk for fk in fks if fk[0] == child and fk[3] == child_keys]
            if (len(matches) != 1 or row[3] != "public"
                    or row[4] != matches[0][2]
                    or (matches[0][1] is not None and name != matches[0][1])
                    or tuple(row[6]) != matches[0][4]
                    or (None if row[7] is None else tuple(row[7])) != matches[0][6]
                    or row[8:11] != (matches[0][5], "a", "s")
                    or row[12] is not False or row[13] is not False
                    or (child, child_keys) in seen_fk or type(name) is not str):
                raise ValueError("Guarded integrity predecessor FK mismatch")
            seen_fk.add((child, child_keys))
            if name in fk_names:
                raise ValueError("Guarded integrity duplicate FK name")
            fk_names[name] = (child, matches[0][2], matches[0][5])
        elif kind in ("p", "u"):
            key = (child, name)
            if (key not in uniques or tuple(row[5]) != uniques[key]
                    or kind != ("p" if name.endswith("_pkey") else "u")
                    or row[12] is not False or row[13] is not False
                    or any(value is not True for value in row[17:21])
                    or key in seen_unique):
                raise ValueError("Guarded integrity parent key mismatch")
            seen_unique.add(key)
        elif kind == "c":
            expression = "".join(char for char in str(row[21]).lower() if char not in ' ()\t\r\n"')
            if (order_seen or (child, name) != ("interseries_calibration_edges",
                                              "ck_interseries_calibration_edges_series_order")
                    or expression != "left_series_id<right_series_id"
                    or row[12] is not False or row[13] is not False):
                raise ValueError("Guarded integrity edge order contract mismatch")
            order_seen = True
        else:
            raise ValueError("Guarded integrity unexpected constraint")
    if (len(seen_fk) != 9 or len(seen_unique) != len(uniques) or not order_seen):
        raise ValueError("Guarded integrity predecessor contract incomplete")

    trigger_rows = conn.execute(_P207_INTEGRITY_TRIGGERS_SQL).fetchall()
    if len(trigger_rows) != 4 * len(fk_names):
        raise ValueError("Guarded integrity RI trigger inventory mismatch")
    seen_triggers: set[tuple[str, str]] = set()
    for name, child, internal, enabled, tgtype, defer, initially, table, function in trigger_rows:
        if (name not in fk_names or child != fk_names[name][0] or internal is not True
                or enabled != "O" or type(tgtype) is not int
                or defer is not False or initially is not False):
            raise ValueError("Guarded integrity RI trigger is disabled or mismatched")
        _, parent, action = fk_names[name]
        checks = {
            (child, 5): "RI_FKey_check_ins", (child, 17): "RI_FKey_check_upd",
            (parent, 9): {"c": "RI_FKey_cascade_del", "r": "RI_FKey_restrict_del",
                          "n": "RI_FKey_setnull_del"}[action],
            (parent, 17): "RI_FKey_noaction_upd",
        }
        if checks.get((table, tgtype)) != function or (name, function) in seen_triggers:
            raise ValueError("Guarded integrity RI trigger function mismatch")
        seen_triggers.add((name, function))
    if len(seen_triggers) != 4 * len(fk_names):
        raise ValueError("Guarded integrity RI trigger coverage mismatch")


def _p207_integrity_rows(conn: object) -> tuple[int, int, int, int, int]:
    # Count with cap+1 BEFORE fetching any data row. No unbounded COUNT(*), JSON,
    # photometry/frame/artifact scan, status or generation filter, or SQL parameters.
    counts = []
    for table, cap, _ in _P207_INTEGRITY_DATA:
        sql = f"SELECT count(*) FROM (SELECT 1 FROM public.{table} LIMIT {cap + 1}) AS bounded"
        result = conn.execute(sql).fetchone()
        if (type(result) not in (tuple, list) or len(result) != 1
                or type(result[0]) is not int or result[0] > cap
                or (table != "calibration_versions" and result[0] != cap)):
            raise ValueError("Guarded integrity data row cap or baseline mismatch")
        counts.append(result[0])
    cv_count, series_count, gen_count, member_count, edge_count = counts
    data = []
    for (table, cap, columns), count in zip(_P207_INTEGRITY_DATA, counts, strict=True):
        rows = conn.execute(f"SELECT {columns} FROM public.{table} LIMIT {cap + 1}").fetchall()
        if type(rows) not in (list, tuple) or len(rows) != count:
            raise ValueError("Guarded integrity bounded data fetch mismatch")
        data.append(rows)
    cvs, series, generations, members, edges = data

    def indexed(rows, length, key):
        result = {}
        for row in rows:
            if (type(row) not in (tuple, list) or len(row) != length
                    or any(type(value) is not int or value <= 0 for value in key(row))
                    or key(row) in result):
                raise ValueError("Guarded integrity identity or data shape mismatch")
            result[key(row)] = row
        return result

    cv_by_id = indexed(cvs, 5, lambda r: (r[0],))
    series_by_id = indexed(series, 2, lambda r: (r[0],))
    gen_by_id = indexed(generations, 3, lambda r: (r[0],))
    member_by_gen_series = indexed(members, 6, lambda r: (r[1], r[3]))
    indexed(members, 6, lambda r: (r[0],))  # No duplicate membership PK.
    if any(row[1] is not None and (type(row[1]) is not int or row[1] <= 0)
           for row in cvs):
        raise ValueError("Guarded integrity CV field identity mismatch")
    if any(row[2] is not None and (type(row[2]) is not int or row[2] <= 0)
           for row in cvs):
        raise ValueError("Guarded integrity CV series identity mismatch")
    for g in generations:
        if (type(g[1]) is not int or g[1] <= 0 or type(g[2]) is not int or g[2] <= 0
                or (g[2],) not in series_by_id or series_by_id[(g[2],)][1] != g[1]):
            raise ValueError("Guarded integrity generation field/reference mismatch")
    reference_counts = {g[0]: 0 for g in generations}
    for m in members:
        # series_id is nullable in the actual schema, but all 36 OLD35 rows
        # historically had a series. Do not turn this into a schema claim.
        if (type(m[3]) is not int or m[3] <= 0 or type(m[2]) is not int or m[2] <= 0
                or type(m[4]) is not int or m[4] <= 0 or type(m[5]) is not bool):
            raise ValueError("Guarded integrity member identity/null historical mismatch")
        g = gen_by_id.get((m[1],))
        s = series_by_id.get((m[3],))
        cv = cv_by_id.get((m[4],))
        if (g is None or s is None or cv is None or g[1] != m[2] or s[1] != m[2]
                or cv[1:3] != (m[2], m[3])
                or cv[3:5] != ("SCIENCE", "COMPLETE_IMMUTABLE")):
            raise ValueError("Guarded integrity member SCIENCE CV scope/diagnostic mismatch")
        if m[5]:
            reference_counts[m[1]] += 1
            if m[3] != g[2]:
                raise ValueError("Guarded integrity reference flag mismatch")
    for g in generations:
        if ((g[0], g[2]) not in member_by_gen_series or reference_counts[g[0]] != 1):
            raise ValueError("Guarded integrity reference membership mismatch")
    seen_edges = set()
    for e in edges:
        if (type(e) not in (tuple, list) or len(e) != 4
                or any(type(x) is not int or x <= 0 for x in e[:3])
                or type(e[3]) is not str or not e[3] or e[1] >= e[2]):
            raise ValueError("Guarded integrity edge order/shape mismatch")
        identity = e[:3]
        if identity in seen_edges:
            raise ValueError("Guarded integrity duplicate all-status edge")
        seen_edges.add(identity)
        g = gen_by_id.get((e[0],))
        left = member_by_gen_series.get((e[0], e[1]))
        right = member_by_gen_series.get((e[0], e[2]))
        if (g is None or left is None or right is None
                or series_by_id.get((e[1],), (None, None))[1] != g[1]
                or series_by_id.get((e[2],), (None, None))[1] != g[1]
                or left[2] != g[1] or right[2] != g[1]):
            raise ValueError("Guarded integrity all-edge field/member scope mismatch")
    return cv_count, series_count, gen_count, member_count, edge_count


def guarded_p207_test_readonly_integrity() -> tuple[int, int, int, int, int]:
    """Bounded OLD35 TEST preflight only; never a migration/write-readiness gate."""
    if any(name.upper().startswith("PG") or name.upper() == "GRANDGOUSIER_DATABASE_URL"
           for name in os.environ):
        raise ValueError("Guarded PostgreSQL environment names are not allowed")
    expected = _p207_expected_ledger()
    _p207_integrity_source_gate(expected)
    password = _p207_password_from_source(_p207_read_private_source())
    import psycopg  # No driver import until fixed source, mirrors and URL are checked.

    conn = None
    try:
        conn = psycopg.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=5432,
            dbname=_P207_TARGET_DB, user=_P207_TARGET_USER, password=password,
            connect_timeout=3, autocommit=True,
            options=("-c default_transaction_read_only=on -c statement_timeout=5000 "
                     "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"),
        )
        conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        probe = conn.execute(_P207_PROBE_SQL).fetchone()
        # Keep the existing aggregate guard's tuple-slice behavior fail-closed;
        # the diagnostic probe helper is NOT a waiver for a list-shaped row.
        if (type(probe) not in (tuple, list) or len(probe) != 15
                or probe[:5] != (_P207_TARGET_DB, 23069087, _P207_TARGET_USER,
                                  _P207_TARGET_USER, False)
                or type(probe[1]) is not int or type(probe[4]) is not bool
                or type(probe[5]) is not int or not 180000 <= probe[5] < 190000
                or type(probe[6]) is not int or probe[6] <= 0
                or type(conn.info.backend_pid) is not int or conn.info.backend_pid != probe[6]
                or probe[7:9] != ("127.0.0.1", 5432) or type(probe[8]) is not int
                or probe[9:] != (True, True, True, True, True, True)
                or any(type(value) is not bool for value in probe[9:])):
            raise ValueError("Guarded backend identity or read-only safety check failed")
        rows = conn.execute(_P207_LEDGER_SQL).fetchall()
        if _p207_ledger_state(rows, expected) != "OLD35":
            raise ValueError("Guarded integrity requires exact OLD35")
        _p207_integrity_catalog(conn)
        result = _p207_integrity_rows(conn)
    finally:
        if conn is not None:
            cleanup_failed = False
            try:
                conn.rollback()
            except BaseException:  # noqa: BLE001 - cleanup uncertainty always STOPs
                cleanup_failed = True
            try:
                conn.close()
            except BaseException:  # noqa: BLE001 - cleanup uncertainty always STOPs
                cleanup_failed = True
            if cleanup_failed:
                raise RuntimeError("Guarded connection cleanup is uncertain") from None
    return (35, *result[:-1])
