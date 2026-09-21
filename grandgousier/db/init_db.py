from __future__ import annotations

from sqlalchemy import inspect

from grandgousier.db.migrations import baseline_current_schema
from grandgousier.db.models import Base
from grandgousier.db.schema_contract import (
    CURRENT_SCHEMA_TABLES,
    assert_clean_schema_epoch,
    assert_schema_contract,
)
from grandgousier.db.session import make_engine


def init_db(database_url: str | None = None) -> None:
    """Create the complete current Grandgousier schema in a fresh database.

    A genuinely fresh schema is also baselined in ``schema_migrations``. Grandgousier
    0.7 is a fresh-baseline epoch: pre-0.7 compatibility schemas are rejected rather
    than upgraded in place.
    """

    engine = make_engine(database_url)
    existing_before = set(inspect(engine).get_table_names())
    assert_clean_schema_epoch(existing_before)
    existing_current = existing_before.intersection(CURRENT_SCHEMA_TABLES)
    if existing_current and existing_current != set(CURRENT_SCHEMA_TABLES):
        missing = sorted(set(CURRENT_SCHEMA_TABLES) - existing_current)
        raise RuntimeError(
            "Refusing to mutate a partial Grandgousier 0.7 schema in place; "
            "init-db only bootstraps an empty database or validates a complete clean schema. "
            "Missing tables: " + ", ".join(missing)
        )
    fresh = not existing_current
    Base.metadata.create_all(engine)
    assert_schema_contract(engine)
    if fresh:
        baseline_current_schema(engine, reason="fresh_orm_init")


def reset_db(database_url: str | None = None) -> None:
    """Drop and recreate all Grandgousier tables.

    Development-only helper. It destroys the complete current working schema,
    including field/series, pipeline provenance, event registry and migration ledger.
    """

    engine = make_engine(database_url)
    assert_clean_schema_epoch(inspect(engine).get_table_names())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    assert_schema_contract(engine)
    baseline_current_schema(engine, reason="orm_reset")
