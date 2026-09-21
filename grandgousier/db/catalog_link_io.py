from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from grandgousier.db.models import FrameCatalogLink


FRAME_CATALOG_LINK_WRITE_COLUMNS = (
    "frame_id",
    "catalog_source_id",
    "x",
    "y",
    "in_footprint",
    "margin_pix",
    "match_status",
    "details",
)

_FRAME_CATALOG_LINK_UPDATE_COLUMNS = tuple(
    column
    for column in FRAME_CATALOG_LINK_WRITE_COLUMNS
    if column not in {"frame_id", "catalog_source_id"}
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


def frame_catalog_link_copy_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    values = dict(row)
    values["details"] = json.dumps(
        values.get("details") or {},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return tuple(values.get(column) for column in FRAME_CATALOG_LINK_WRITE_COLUMNS)


@dataclass(slots=True)
class PostgresFrameCatalogLinkCopyStage:
    """COPY staging area for high-volume frame↔catalog projection rows.

    The temporary table carries only the columns required by the logical link.
    It has no indexes, foreign keys, defaults, or identity column. Rows are
    streamed while frames are projected, then published with one server-side
    ``INSERT .. SELECT``. This avoids one parameter-heavy UPSERT per frame.
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
    def create(cls, session: Any) -> "PostgresFrameCatalogLinkCopyStage":
        if not postgres_copy_capable(session):
            raise RuntimeError("PostgreSQL psycopg COPY is not available for this session")
        stage = cls(session=session, stage_name=f"gg_frame_catalog_stage_{uuid.uuid4().hex}")
        cursor = stage._driver_connection.cursor()
        try:
            cursor.execute(
                f"CREATE TEMP TABLE {stage._quoted_stage} ON COMMIT DROP AS "
                f"SELECT {stage._columns_sql} FROM frame_catalog_links WITH NO DATA"
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
        return ", ".join(f'"{column}"' for column in FRAME_CATALOG_LINK_WRITE_COLUMNS)

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.closed:
            raise RuntimeError("frame-catalog COPY stage is already closed")
        if self.merged:
            raise RuntimeError("frame-catalog COPY stage has already been merged")
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            with cursor.copy(
                f"COPY {self._quoted_stage} ({self._columns_sql}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(frame_catalog_link_copy_tuple(row))
        finally:
            cursor.close()
        self.staging_copy_sec += float(time.perf_counter() - started)
        self.rows_staged += len(rows)
        self.copy_batches += 1

    def merge(self, *, upsert: bool = True) -> None:
        if self.closed:
            raise RuntimeError("frame-catalog COPY stage is already closed")
        if self.merged:
            return
        if self.rows_staged <= 0:
            self.merged = True
            return
        conflict_sql = ""
        if upsert:
            update_sql = ", ".join(
                f'"{column}" = EXCLUDED."{column}"'
                for column in _FRAME_CATALOG_LINK_UPDATE_COLUMNS
            )
            conflict_sql = (
                " ON CONFLICT ON CONSTRAINT uq_frame_catalog_links_frame_source DO UPDATE SET "
                f"{update_sql}"
            )
        started = time.perf_counter()
        cursor = self._driver_connection.cursor()
        try:
            cursor.execute(
                f"INSERT INTO frame_catalog_links ({self._columns_sql}) "
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
