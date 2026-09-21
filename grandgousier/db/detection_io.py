from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


SOURCE_DETECTION_WRITE_COLUMNS = (
    "detection_run_frame_id",
    "x",
    "y",
    "ra_deg",
    "dec_deg",
    "peak_adu",
    "flux",
    "flux_err",
    "local_background",
    "snr",
    "npix",
    "detection_status",
    "flags",
)

DETECTION_CATALOG_MATCH_WRITE_COLUMNS = (
    "detection_run_frame_id",
    "source_detection_id",
    "catalog_source_id",
    "match_status",
    "sep_arcsec",
    "dx_pix",
    "dy_pix",
    "detection_mag_inst",
    "catalog_mag",
    "zero_point",
)

_DETECTION_CATALOG_MATCH_UPDATE_COLUMNS = tuple(
    column
    for column in DETECTION_CATALOG_MATCH_WRITE_COLUMNS
    if column not in {"detection_run_frame_id", "catalog_source_id"}
)



def postgres_copy_capable(session: Any) -> bool:
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


def _json_text(value: Any) -> str:
    return json.dumps(
        value or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def source_detection_copy_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    values = dict(row)
    values["flags"] = _json_text(values.get("flags"))
    return tuple(values.get(column) for column in SOURCE_DETECTION_WRITE_COLUMNS)


def detection_catalog_match_copy_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    values = dict(row)
    return tuple(values.get(column) for column in DETECTION_CATALOG_MATCH_WRITE_COLUMNS)


@dataclass(slots=True)
class PostgresSourceDetectionCopyStage:
    """Transaction-local COPY stage for source detections.

    Detection rows are streamed while FITS frames are processed. Publication is
    delayed until all successful frames are known, which lets callers replace
    existing detections with one set-based DELETE instead of one DELETE per frame.
    """

    session: Any
    stage_name: str
    rows_staged: int = 0
    copy_batches: int = 0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    cleanup_sec: float = 0.0
    merged: bool = False
    closed: bool = False

    @classmethod
    def create(cls, session: Any) -> "PostgresSourceDetectionCopyStage":
        if not postgres_copy_capable(session):
            raise RuntimeError("PostgreSQL psycopg COPY is not available for this session")
        stage = cls(session=session, stage_name=f"gg_source_detection_stage_{uuid.uuid4().hex}")
        cursor = stage._driver_connection.cursor()
        try:
            cursor.execute(
                f"CREATE TEMP TABLE {stage._quoted_stage} ON COMMIT DROP AS "
                f"SELECT {stage._columns_sql} FROM source_detections WITH NO DATA"
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
        return ", ".join(f'"{column}"' for column in SOURCE_DETECTION_WRITE_COLUMNS)

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.closed:
            raise RuntimeError("source-detection COPY stage is already closed")
        if self.merged:
            raise RuntimeError("source-detection COPY stage has already been merged")
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            with cursor.copy(
                f"COPY {self._quoted_stage} ({self._columns_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(source_detection_copy_tuple(row))
        finally:
            cursor.close()
        self.staging_copy_sec += float(time.perf_counter() - started)
        self.rows_staged += len(rows)
        self.copy_batches += 1

    def merge(self) -> None:
        if self.closed:
            raise RuntimeError("source-detection COPY stage is already closed")
        if self.merged:
            return
        if self.rows_staged <= 0:
            self.merged = True
            return
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO source_detections ({self._columns_sql}) "
                f"SELECT {self._columns_sql} FROM {self._quoted_stage}"
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


@dataclass(slots=True)
class PostgresDetectionCatalogMatchCopyStage:
    """Transaction-local COPY stage for detection↔catalog match UPSERTs."""

    session: Any
    stage_name: str
    rows_staged: int = 0
    copy_batches: int = 0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    cleanup_sec: float = 0.0
    merged: bool = False
    closed: bool = False

    @classmethod
    def create(cls, session: Any) -> "PostgresDetectionCatalogMatchCopyStage":
        if not postgres_copy_capable(session):
            raise RuntimeError("PostgreSQL psycopg COPY is not available for this session")
        stage = cls(session=session, stage_name=f"gg_detection_match_stage_{uuid.uuid4().hex}")
        cursor = stage._driver_connection.cursor()
        try:
            cursor.execute(
                f"CREATE TEMP TABLE {stage._quoted_stage} ON COMMIT DROP AS "
                f"SELECT {stage._columns_sql} FROM detection_catalog_matches WITH NO DATA"
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
        return ", ".join(f'"{column}"' for column in DETECTION_CATALOG_MATCH_WRITE_COLUMNS)

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.closed:
            raise RuntimeError("detection-match COPY stage is already closed")
        if self.merged:
            raise RuntimeError("detection-match COPY stage has already been merged")
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            with cursor.copy(
                f"COPY {self._quoted_stage} ({self._columns_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(detection_catalog_match_copy_tuple(row))
        finally:
            cursor.close()
        self.staging_copy_sec += float(time.perf_counter() - started)
        self.rows_staged += len(rows)
        self.copy_batches += 1

    def merge(self) -> None:
        if self.closed:
            raise RuntimeError("detection-match COPY stage is already closed")
        if self.merged:
            return
        if self.rows_staged <= 0:
            self.merged = True
            return
        update_sql = ", ".join(
            f'"{column}" = EXCLUDED."{column}"'
            for column in _DETECTION_CATALOG_MATCH_UPDATE_COLUMNS
        )
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO detection_catalog_matches ({self._columns_sql}) "
                f"SELECT {self._columns_sql} FROM {self._quoted_stage} "
                "ON CONFLICT ON CONSTRAINT "
                "uq_detection_catalog_matches_run_frame_catalog_source "
                f"DO UPDATE SET {update_sql}"
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
