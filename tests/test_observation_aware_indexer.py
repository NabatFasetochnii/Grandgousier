from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("astropy")

from astropy.io import fits
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.db.models import Base, Frame, FrameLocation, Observation, ProcessingEvent
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.archive_indexer import index_archive_paths


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _profile() -> InstrumentProfile:
    return InstrumentProfile.from_yaml("configs/master.yaml")


def _write_master(
    path: Path,
    *,
    uniqnum: str | None,
    exptime: float = 180.0,
    payload: str | None = None,
) -> None:
    hdu = fits.PrimaryHDU()
    header = hdu.header
    header["DATE-OBS"] = "2023-09-20T03:13:14"
    header["MJD"] = 60206.925859
    header["EXPTIME"] = exptime
    header["FILTER"] = "V"
    header["OBJNAME"] = "TIC420268415_01"
    header["IMAGETYP"] = "LIGHT"
    header["OBSERVAT"] = "KAO"
    header["AXIS"] = "EAST"
    header["ID_CAM"] = "204"
    if uniqnum is not None:
        header["UNIQNUM"] = uniqnum
    if payload is not None:
        header["HISTORY"] = payload
    hdu.writeto(path)


def test_indexer_copy_move_reuses_observation(tmp_path: Path, db_session: Session) -> None:
    original = tmp_path / "original.fits"
    copied = tmp_path / "copy.fits"
    _write_master(original, uniqnum="204_20001")
    shutil.copy2(original, copied)

    first = index_archive_paths(db_session, [original], _profile())
    second = index_archive_paths(db_session, [copied], _profile())

    assert first.inserted == 1
    assert second.inserted == 0 and second.skipped_existing == 1
    frames = list(db_session.scalars(select(Frame).order_by(Frame.frame_id)))
    assert len(frames) == 1
    locations = list(db_session.scalars(select(FrameLocation).order_by(FrameLocation.frame_location_id)))
    assert len(locations) == 2
    assert {Path(row.locator).name for row in locations} == {"original.fits", "copy.fits"}
    assert sum(1 for row in locations if row.is_primary) == 1
    assert frames[0].observation_id is not None
    assert len(list(db_session.scalars(select(Observation)))) == 1


def test_indexer_exact_existing_repairs_observation_link(tmp_path: Path, db_session: Session) -> None:
    path = tmp_path / "existing.fits"
    _write_master(path, uniqnum="204_20002")
    index_archive_paths(db_session, [path], _profile())
    frame = db_session.scalar(select(Frame))
    observation = db_session.scalar(select(Observation))
    assert frame is not None and observation is not None
    expected = observation.observation_id
    frame.observation_id = None
    db_session.commit()

    result = index_archive_paths(db_session, [path], _profile())

    assert result.skipped_existing == 1
    repaired = db_session.scalar(select(Frame))
    assert repaired is not None and repaired.observation_id == expected


def test_indexer_collision_is_failed_not_skipped(tmp_path: Path, db_session: Session) -> None:
    first = tmp_path / "first.fits"
    conflict = tmp_path / "conflict.fits"
    _write_master(first, uniqnum="204_20003", exptime=180.0)
    _write_master(conflict, uniqnum="204_20003", exptime=60.0)
    index_archive_paths(db_session, [first], _profile())

    result = index_archive_paths(db_session, [conflict], _profile())

    assert result.failed == 1 and result.skipped_existing == 0
    assert len(list(db_session.scalars(select(Frame)))) == 1
    event = db_session.scalar(select(ProcessingEvent).where(ProcessingEvent.stage == "index_archive"))
    assert event is not None
    assert event.details["error_type"] == "ObservationLinkConflict"


def test_indexer_missing_identity_keeps_frame_unlinked_with_warning(
    tmp_path: Path, db_session: Session
) -> None:
    path = tmp_path / "missing.fits"
    _write_master(path, uniqnum=None)

    result = index_archive_paths(db_session, [path], _profile())

    assert result.inserted == 1 and result.failed == 0
    frame = db_session.scalar(select(Frame))
    assert frame is not None and frame.observation_id is None
    warning = db_session.scalar(
        select(ProcessingEvent).where(ProcessingEvent.stage == "observation_identity")
    )
    assert warning is not None
    assert warning.details["status"] == "MISSING_IDENTITY"
