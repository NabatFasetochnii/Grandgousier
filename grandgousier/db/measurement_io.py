from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.db.bulk import iter_sized_chunks, row_column_count
from grandgousier.db.models import CalibrationVersion, Measurement


MEASUREMENT_WRITE_COLUMNS = (
    "object_id",
    "frame_id",
    "calibration_version_id",
    "x",
    "y",
    "flux",
    "flux_err",
    "mag_inst",
    "mag_cal",
    "mag_err",
    "mag_err_random",
    "mag_err_systematic",
    "aperture_radius_pix",
    "local_background",
    "local_zp_correction",
    "centroid_dx_pix",
    "centroid_dy_pix",
    "flags",
)

_MEASUREMENT_UPDATE_COLUMNS = tuple(
    column
    for column in MEASUREMENT_WRITE_COLUMNS
    if column not in {"object_id", "frame_id", "calibration_version_id"}
)

_BASE_PRODUCT_KIND = "BASE_PHOTOMETRY"
_BASE_PRODUCT_ROLE = "BASE"
_BASE_MATERIALIZATION_KIND = "FULL_MEASUREMENTS"


def _measurement_calibration_ids(rows: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for row in rows:
        value = row.get("calibration_version_id")
        if value is None:
            raise ValueError("measurements.calibration_version_id is required in clean schema 0.7")
        ids.add(int(value))
    return ids


def _assert_base_measurement_targets(
    session: Session,
    rows: list[dict[str, Any]],
    *,
    already_validated: set[int] | None = None,
) -> set[int]:
    calibration_ids = _measurement_calibration_ids(rows)
    pending = calibration_ids - set(already_validated or ())
    if not pending:
        return calibration_ids
    resolved = session.execute(
        select(
            CalibrationVersion.calibration_version_id,
            CalibrationVersion.product_kind,
            CalibrationVersion.product_role,
            CalibrationVersion.materialization_kind,
        ).where(CalibrationVersion.calibration_version_id.in_(sorted(pending)))
    ).all()
    by_id = {int(row[0]): row for row in resolved}
    missing = sorted(pending - set(by_id))
    if missing:
        raise ValueError(f"measurement write targets unknown calibration_version_id values: {missing}")
    invalid: list[str] = []
    for calibration_id in sorted(pending):
        row = by_id[calibration_id]
        kind = str(row[1] or "").upper()
        role = str(row[2] or "").upper()
        materialization = str(row[3] or "").upper()
        if (
            kind != _BASE_PRODUCT_KIND
            or role != _BASE_PRODUCT_ROLE
            or materialization != _BASE_MATERIALIZATION_KIND
        ):
            invalid.append(
                f"{calibration_id}(kind={kind or '-'}, role={role or '-'}, "
                f"materialization={materialization or '-'})"
            )
    if invalid:
        raise ValueError(
            "measurements is BASE-only in clean schema 0.7; refusing derived/full-copy writes for "
            + ", ".join(invalid)
        )
    return calibration_ids



def upsert_measurement_rows(
    session: Session,
    rows: list[dict],
    *,
    insert_batch_size: int | None = None,
) -> int:
    """Upsert measurement rows in PostgreSQL-safe chunks.

    Returns the number of executed INSERT/UPSERT batches.
    """

    if not rows:
        return 0
    _assert_base_measurement_targets(session, rows)
    table = Measurement.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(
        rows,
        n_columns=row_column_count(rows),
        requested=insert_batch_size,
    ):
        stmt = insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_measurement_version",
            set_={column: getattr(stmt.excluded, column) for column in _MEASUREMENT_UPDATE_COLUMNS},
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches


def postgres_copy_capable(session: Session) -> bool:
    """Return true only for a psycopg3-style PostgreSQL DBAPI connection."""

    try:
        sa_connection = session.connection()
        if str(sa_connection.dialect.name).lower() != "postgresql":
            return False
        driver_connection = _driver_connection(sa_connection)
        cursor = driver_connection.cursor()
        try:
            return callable(getattr(cursor, "copy", None))
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001 - capability detection must preserve fallback behavior.
        return False


def _driver_connection(sa_connection: Any) -> Any:
    proxied = sa_connection.connection
    return getattr(proxied, "driver_connection", proxied)


def measurement_copy_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    """Return one COPY row in exactly :data:`MEASUREMENT_WRITE_COLUMNS` order."""

    values = dict(row)
    # PostgreSQL's JSONB input function accepts JSON text. Serializing explicitly
    # avoids relying on psycopg adapter registration for plain Python dicts.
    values["flags"] = json.dumps(
        values.get("flags") or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return tuple(values.get(column) for column in MEASUREMENT_WRITE_COLUMNS)


@dataclass(slots=True)
class PostgresMeasurementDirectCopyWriter:
    """Direct COPY writer for a provably fresh immutable measurement generation.

    Unlike :class:`PostgresMeasurementCopyStage`, this writer sends rows straight
    to ``measurements``. It is intentionally insert-only and must only be used
    when the caller has established that the target calibration generation has
    no pre-existing measurements. Transaction rollback remains the safety net if
    a later step fails.
    """

    session: Session
    rows_written: int = 0
    copy_batches: int = 0
    copy_sec: float = 0.0
    validated_calibration_version_ids: set[int] = field(default_factory=set)

    @classmethod
    def create(cls, session: Session) -> "PostgresMeasurementDirectCopyWriter":
        if not postgres_copy_capable(session):
            raise RuntimeError("PostgreSQL psycopg COPY is not available for this session")
        return cls(session=session)

    @property
    def _sa_connection(self) -> Any:
        return self.session.connection()

    @property
    def _driver_connection(self) -> Any:
        return _driver_connection(self._sa_connection)

    @property
    def _columns_sql(self) -> str:
        return ", ".join(f'"{column}"' for column in MEASUREMENT_WRITE_COLUMNS)

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        validated = _assert_base_measurement_targets(
            self.session, rows, already_validated=self.validated_calibration_version_ids
        )
        self.validated_calibration_version_ids.update(validated)
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            with cursor.copy(
                f"COPY measurements ({self._columns_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(measurement_copy_tuple(row))
        finally:
            cursor.close()
        self.copy_sec += float(time.perf_counter() - started)
        self.rows_written += len(rows)
        self.copy_batches += 1


@dataclass(slots=True)
class PostgresMeasurementCopyStage:
    """Transaction-local COPY staging area for high-volume measurement upserts.

    The stage is intentionally narrow: no primary key, indexes, defaults, or
    foreign keys are copied from ``measurements``. Rows can be streamed into it
    in several moderate Python buffers while FITS processing continues, then one
    server-side ``INSERT .. SELECT .. ON CONFLICT`` publishes the whole batch.
    """

    session: Session
    stage_name: str
    rows_staged: int = 0
    copy_batches: int = 0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    cleanup_sec: float = 0.0
    merged: bool = False
    closed: bool = False
    validated_calibration_version_ids: set[int] = field(default_factory=set)

    @classmethod
    def create(cls, session: Session) -> "PostgresMeasurementCopyStage":
        if not postgres_copy_capable(session):
            raise RuntimeError("PostgreSQL psycopg COPY is not available for this session")
        stage = cls(session=session, stage_name=f"gg_measurement_stage_{uuid.uuid4().hex}")
        columns_sql = stage._columns_sql
        cursor = stage._driver_connection.cursor()
        try:
            cursor.execute(
                f"CREATE TEMP TABLE {stage._quoted_stage} ON COMMIT DROP AS "
                f"SELECT {columns_sql} FROM measurements WITH NO DATA"
            )
        finally:
            cursor.close()
        return stage

    @property
    def _sa_connection(self) -> Any:
        return self.session.connection()

    @property
    def _driver_connection(self) -> Any:
        return _driver_connection(self._sa_connection)

    @property
    def _quoted_stage(self) -> str:
        return f'"{self.stage_name}"'

    @property
    def _columns_sql(self) -> str:
        return ", ".join(f'"{column}"' for column in MEASUREMENT_WRITE_COLUMNS)

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.closed:
            raise RuntimeError("measurement COPY stage is already closed")
        if self.merged:
            raise RuntimeError("measurement COPY stage has already been merged")
        validated = _assert_base_measurement_targets(
            self.session, rows, already_validated=self.validated_calibration_version_ids
        )
        self.validated_calibration_version_ids.update(validated)
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            with cursor.copy(
                f"COPY {self._quoted_stage} ({self._columns_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(measurement_copy_tuple(row))
        finally:
            cursor.close()
        self.staging_copy_sec += float(time.perf_counter() - started)
        self.rows_staged += len(rows)
        self.copy_batches += 1

    def merge(self, *, upsert: bool = True) -> None:
        if self.closed:
            raise RuntimeError("measurement COPY stage is already closed")
        if self.merged:
            return
        if self.rows_staged <= 0:
            self.merged = True
            return
        conflict_sql = ""
        if upsert:
            update_sql = ", ".join(
                f'"{column}" = EXCLUDED."{column}"' for column in _MEASUREMENT_UPDATE_COLUMNS
            )
            conflict_sql = (
                " ON CONFLICT ON CONSTRAINT uq_measurement_version DO UPDATE SET "
                f"{update_sql}"
            )
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO measurements ({self._columns_sql}) "
                f"SELECT {self._columns_sql} FROM {self._quoted_stage}"
                f"{conflict_sql}"
            )
        finally:
            cursor.close()
        self.insert_select_sec += float(time.perf_counter() - started)
        self.merged = True

    def close(self) -> None:
        if self.closed:
            return
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            cursor.execute(f"DROP TABLE IF EXISTS {self._quoted_stage}")
        finally:
            cursor.close()
        self.cleanup_sec += float(time.perf_counter() - started)
        self.closed = True
