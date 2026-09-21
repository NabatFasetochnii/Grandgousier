from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.db.models import Base, Frame, FrameStatus, FrameType, Observation
import grandgousier.identity.linking as linking
from grandgousier.identity.linking import (
    ObservationLinkConflict,
    ObservationLinkStatus,
    ensure_observation_for_frame,
)
from grandgousier.identity.observation import build_frame_observation_identity


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


def _frame(
    name: str,
    *,
    frame_uid: str,
    uniqnum: str | None = "204_99999",
    exptime: float = 180.0,
    file_size_bytes: int = 100,
    naxis: int = 4098,
) -> Frame:
    header = {"NAXIS1": naxis, "NAXIS2": naxis}
    if uniqnum is not None:
        header["UNIQNUM"] = uniqnum
    return Frame(
        frame_uid=frame_uid,
        raw_path=f"C:/archive/{name}",
        file_size_bytes=file_size_bytes,
        file_mtime_utc=datetime(2026, 9, 10, tzinfo=timezone.utc),
        instrument="MASTER",
        site="KAO",
        tube="EAST",
        camera="204",
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=datetime(2023, 9, 20, 3, 13, 14, tzinfo=timezone.utc),
        exptime=exptime,
        object_name="TIC420268415_01",
        processing_status=FrameStatus.HEADER_READ,
        flat_status="MISSING_IN_HEADER",
        dark_status="MISSING_IN_HEADER",
        sexcat_status="MISSING_IN_HEADER",
        quality_flags={},
        header_json=header,
    )


def _persist_frame(session: Session, frame: Frame) -> Frame:
    session.add(frame)
    session.commit()
    return frame


def test_new_frame_creates_and_links_observation(db_session: Session) -> None:
    frame = _frame("new.fits", frame_uid="file-new", uniqnum="204_99999")

    result = ensure_observation_for_frame(db_session, frame)
    db_session.add(frame)
    db_session.commit()

    assert result.status == ObservationLinkStatus.CREATED
    assert result.observation_id is not None
    assert frame.observation_id == result.observation_id
    observation = db_session.scalar(select(Observation))
    assert observation is not None
    assert observation.native_identity == "204_99999"


def test_copy_like_frame_reuses_existing_observation(db_session: Session) -> None:
    first = _frame("a.fits", frame_uid="file-a", uniqnum="204_10001")
    ensure_observation_for_frame(db_session, first)
    _persist_frame(db_session, first)

    second = _frame("b.fits", frame_uid="file-b", uniqnum="204_10001")
    result = ensure_observation_for_frame(db_session, second)
    _persist_frame(db_session, second)

    assert result.status == ObservationLinkStatus.REUSED
    assert result.classification == "COPY_LIKE"
    frames = list(db_session.scalars(select(Frame).order_by(Frame.frame_id)))
    assert len(frames) == 2
    assert frames[0].observation_id == frames[1].observation_id
    assert len(list(db_session.scalars(select(Observation)))) == 1


def test_product_variant_reuses_existing_observation(db_session: Session) -> None:
    first = _frame(
        "small.fits",
        frame_uid="variant-small",
        uniqnum="203_17870",
        file_size_bytes=100,
        naxis=1000,
    )
    ensure_observation_for_frame(db_session, first)
    _persist_frame(db_session, first)

    second = _frame(
        "large.fits",
        frame_uid="variant-large",
        uniqnum="203_17870",
        file_size_bytes=200,
        naxis=1200,
    )
    result = ensure_observation_for_frame(db_session, second)
    _persist_frame(db_session, second)

    assert result.status == ObservationLinkStatus.REUSED
    assert result.classification == "PRODUCT_VARIANT"
    assert set(result.product_variants) == {"file_size_bytes", "naxis1", "naxis2"}
    assert len(list(db_session.scalars(select(Frame)))) == 2
    assert len(list(db_session.scalars(select(Observation)))) == 1


def test_unlinked_existing_frame_can_be_reconciled(db_session: Session) -> None:
    frame = _frame("existing.fits", frame_uid="file-existing", uniqnum="204_10002")
    identity = build_frame_observation_identity(frame)
    observation = Observation(
        observation_uid=identity.observation_uid,
        instrument=identity.instrument,
        identity_scheme=identity.identity_scheme,
        native_identity=identity.native_identity,
        camera_identity=identity.camera_identity,
        observed_at=identity.observed_at,
        identity_components=dict(identity.identity_components),
    )
    db_session.add_all([observation, frame])
    db_session.commit()
    assert frame.observation_id is None

    result = ensure_observation_for_frame(db_session, frame)
    db_session.commit()

    assert result.status == ObservationLinkStatus.REUSED
    assert frame.observation_id == observation.observation_id


def test_hard_collision_is_blocking(db_session: Session) -> None:
    first = _frame(
        "first.fits",
        frame_uid="collision-first",
        uniqnum="204_10003",
        exptime=180.0,
    )
    ensure_observation_for_frame(db_session, first)
    _persist_frame(db_session, first)

    second = _frame(
        "conflict.fits",
        frame_uid="collision-second",
        uniqnum="204_10003",
        exptime=60.0,
    )
    with pytest.raises(ObservationLinkConflict, match="exptime"):
        ensure_observation_for_frame(db_session, second)

    assert len(list(db_session.scalars(select(Frame)))) == 1
    assert len(list(db_session.scalars(select(Observation)))) == 1


def test_missing_identity_returns_unlinked_status_without_fallback(db_session: Session) -> None:
    frame = _frame("missing.fits", frame_uid="missing", uniqnum=None)

    result = ensure_observation_for_frame(db_session, frame)

    assert result.status == ObservationLinkStatus.MISSING_IDENTITY
    assert result.observation_id is None
    assert frame.observation_id is None
    assert len(list(db_session.scalars(select(Observation)))) == 0


def test_conflicting_existing_frame_link_is_blocking(db_session: Session) -> None:
    target = _frame("target.fits", frame_uid="target", uniqnum="204_10005")
    other = _frame("other.fits", frame_uid="other", uniqnum="204_OTHER")
    ensure_observation_for_frame(db_session, target)
    ensure_observation_for_frame(db_session, other)
    _persist_frame(db_session, target)
    _persist_frame(db_session, other)

    target.observation_id = other.observation_id
    db_session.commit()

    with pytest.raises(ObservationLinkConflict, match="already linked"):
        ensure_observation_for_frame(db_session, target)



def test_invalid_robophot_identity_returns_unlinked_status(db_session: Session) -> None:
    frame = Frame(
        frame_uid="rp-invalid",
        raw_path="C:/archive/rp-invalid.fits",
        instrument="RoboPhot",
        camera="ML0742515",
        filter_name="i",
        image_type=FrameType.SCIENCE,
        date_obs=datetime(2023, 11, 5, 19, 43, 56, tzinfo=timezone.utc),
        exptime=60.0,
        processing_status=FrameStatus.HEADER_READ,
        flat_status="MISSING_IN_HEADER",
        dark_status="MISSING_IN_HEADER",
        sexcat_status="MISSING_IN_HEADER",
        quality_flags={},
        header_json={
            "SERNUM": "ML0742515",
            "DATE-OBS": "2023-11-05T19:43:56+05:00",
        },
    )

    result = ensure_observation_for_frame(db_session, frame)

    assert result.status == ObservationLinkStatus.INVALID_IDENTITY
    assert result.observation_id is None
    assert frame.observation_id is None


def test_uid_and_scheme_native_lookup_split_is_blocking(db_session: Session) -> None:
    frame = _frame("split.fits", frame_uid="split", uniqnum="204_SPLIT")
    identity = build_frame_observation_identity(frame)
    other = _frame("other-split.fits", frame_uid="other-split", uniqnum="204_OTHER_SPLIT")
    other_identity = build_frame_observation_identity(other)

    by_uid = Observation(
        observation_uid=identity.observation_uid,
        instrument=identity.instrument,
        identity_scheme=other_identity.identity_scheme,
        native_identity=other_identity.native_identity,
        identity_components={},
    )
    by_pair = Observation(
        observation_uid=other_identity.observation_uid,
        instrument=identity.instrument,
        identity_scheme=identity.identity_scheme,
        native_identity=identity.native_identity,
        identity_components={},
    )
    db_session.add_all([by_uid, by_pair])
    db_session.commit()

    with pytest.raises(ObservationLinkConflict, match="resolve to different rows"):
        ensure_observation_for_frame(db_session, frame)


def test_unique_integrity_error_without_exact_identity_is_not_swallowed(monkeypatch) -> None:
    frame = _frame("race-fail.fits", frame_uid="race-fail", uniqnum="204_RACE_FAIL")

    class RaceSession:
        @contextmanager
        def begin_nested(self):
            yield

        def add(self, _obj):
            pass

        def flush(self):
            raise IntegrityError("INSERT observations", {}, Exception("unexpected integrity error"))

    session = RaceSession()
    monkeypatch.setattr(linking, "_resolve_observation", lambda *_args: None)

    with pytest.raises(IntegrityError):
        ensure_observation_for_frame(session, frame)

def test_observation_unique_race_recovers_only_when_exact_identity_appears(monkeypatch) -> None:
    frame = _frame("race.fits", frame_uid="race-frame", uniqnum="204_10004")
    identity = build_frame_observation_identity(frame)
    existing = Observation(
        observation_id=77,
        observation_uid=identity.observation_uid,
        instrument=identity.instrument,
        identity_scheme=identity.identity_scheme,
        native_identity=identity.native_identity,
        camera_identity=identity.camera_identity,
        observed_at=identity.observed_at,
        identity_components=dict(identity.identity_components),
    )

    class RaceSession:
        def __init__(self):
            self.resolve_calls = 0

        @contextmanager
        def begin_nested(self):
            yield

        def add(self, _obj):
            pass

        def flush(self):
            raise IntegrityError("INSERT observations", {}, Exception("unique race"))

        def scalars(self, _statement):
            return []

    session = RaceSession()

    def fake_resolve(_session, _identity):
        session.resolve_calls += 1
        return None if session.resolve_calls == 1 else existing

    monkeypatch.setattr(linking, "_resolve_observation", fake_resolve)

    result = ensure_observation_for_frame(session, frame)

    assert result.status == ObservationLinkStatus.REUSED
    assert result.observation_id == 77
    assert frame.observation_id == 77
    assert session.resolve_calls == 2
