from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")

POSTGRESQL_MAX_BIND_PARAMETERS = 65535
# Keep headroom for dialect-generated bind parameters and future extra columns.
POSTGRESQL_SAFE_BIND_PARAMETERS = 60000
DEFAULT_BULK_INSERT_BATCH_SIZE = 1000


def validated_batch_size(batch_size: int | None) -> int:
    """Return a positive user-facing batch size.

    ``None`` means the project default.  The low-level safety cap is applied by
    ``safe_bulk_insert_batch_size`` because it depends on the number of columns.
    """

    if batch_size is None:
        return DEFAULT_BULK_INSERT_BATCH_SIZE
    value = int(batch_size)
    if value <= 0:
        raise ValueError("bulk insert batch size must be positive")
    return value


def safe_bulk_insert_batch_size(
    *,
    n_columns: int,
    requested: int | None = None,
    max_parameters: int = POSTGRESQL_SAFE_BIND_PARAMETERS,
) -> int:
    """Choose a PostgreSQL-safe multi-row INSERT chunk size.

    psycopg/PostgreSQL cannot execute statements with more than 65535 bind
    parameters.  SQLAlchemy's PostgreSQL INSERT .. VALUES .. ON CONFLICT path
    expands every row/column pair into bind parameters, so large catalogs must
    be written in chunks.
    """

    cols = max(1, int(n_columns))
    requested_size = validated_batch_size(requested)
    safe_by_params = max(1, int(max_parameters) // cols)
    return max(1, min(requested_size, safe_by_params))


def iter_chunks(rows: Sequence[T], chunk_size: int) -> Iterator[Sequence[T]]:
    """Yield fixed-size chunks from an indexable row sequence."""

    size = validated_batch_size(chunk_size)
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def iter_sized_chunks(rows: Sequence[T], *, n_columns: int, requested: int | None = None) -> Iterator[Sequence[T]]:
    """Yield chunks sized both by requested row count and PostgreSQL bind limits."""

    size = safe_bulk_insert_batch_size(n_columns=n_columns, requested=requested)
    yield from iter_chunks(rows, size)


def row_column_count(rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    return len(rows[0])
