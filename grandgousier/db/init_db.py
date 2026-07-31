from __future__ import annotations

from grandgousier.db.models import Base
from grandgousier.db.session import make_engine


def init_db(database_url: str | None = None) -> None:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)


def reset_db(database_url: str | None = None) -> None:
    """Drop and recreate all Grandgousier tables.

    Development-only helper. It destroys frames, measurements, objects,
    calibration versions and processing events in the selected database.
    """

    engine = make_engine(database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
