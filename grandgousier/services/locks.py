from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import text

from grandgousier.db.session import make_engine
from grandgousier.services.runtime import FieldArchiveJobConfig


class FieldArchiveJobLocked(RuntimeError):
    pass


def field_archive_lock_key(config: FieldArchiveJobConfig) -> int:
    """Stable signed BIGINT advisory-lock key for one field/archive/catalog scope."""

    root = str(Path(config.processed_root).expanduser().resolve(strict=False))
    payload = f"field_archive_v1|{config.field_id}|{config.catalog_name}|{root}".encode("utf-8")
    raw = int.from_bytes(hashlib.sha256(payload).digest()[:8], byteorder="big", signed=False)
    return raw if raw < 2**63 else raw - 2**64


@contextmanager
def field_archive_advisory_lock(
    config: FieldArchiveJobConfig,
) -> Iterator[int]:
    """Hold a PostgreSQL session-level advisory lock for the whole M8 job."""

    key = field_archive_lock_key(config)
    engine = make_engine(config.database_url)
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": key},
            ).scalar()
        )
        if not acquired:
            raise FieldArchiveJobLocked(
                "Another field-archive job already holds the production lock for "
                f"field_id={config.field_id}, catalog={config.catalog_name!r}, "
                f"processed_root={config.processed_root!r}"
            )
        yield key
    finally:
        if acquired:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": key},
                )
            except Exception:
                pass
        connection.close()
        engine.dispose()
