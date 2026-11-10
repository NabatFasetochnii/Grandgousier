from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.archive.observation_selection import (
    ObservationScienceAction,
    ObservationScienceSelectionResult,
    ScienceFileIdentityCandidate,
    plan_file_science_selection,
    select_persisted_science_frames,
)
from grandgousier.db.models import (
    Base,
    Field,
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    FrameLocation,
    FrameStatus,
    FrameType,
    InstrumentUnit,
    Observation,
)
from grandgousier.identity.observation import make_observation_uid


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    # Only create tables used by these tests; unrelated PostgreSQL-specific metadata
    # remains untouched rather than weakening its production constraint for SQLite.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[
            InstrumentUnit.__table__,
            Observation.__table__,
            Frame.__table__,
            FrameLocation.__table__,
            Field.__table__,
            FieldSeries.__table__,
            FieldSeriesFrame.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


def _observation(session: Session, native: str) -> Observation:
    scheme = "master_uniqnum_v1"
    obs = Observation(
        observation_uid=make_observation_uid(scheme, native),
        instrument="MASTER",
        identity_scheme=scheme,
        native_identity=native,
        camera_identity="204",
        observed_at=datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        identity_components={"UNIQNUM": native},
    )
    session.add(obs)
    session.flush()
    return obs


def _frame(
    session: Session,
    *,
    uid: str,
    path: str,
    observation: Observation | None = None,
    instrument: str = "MASTER",
    date_obs: datetime | None = None,
    camera: str | None = None,
    raw_relative_physical_key: str | None = None,
    header_uniqnum: str | None = None,
    ggraw: str | None = None,
) -> Frame:
    header = {}
    if header_uniqnum is not None:
        header["UNIQNUM"] = header_uniqnum
    if raw_relative_physical_key is not None:
        header["raw_relative_physical_key"] = raw_relative_physical_key
    if ggraw is not None:
        header["GGRAW"] = ggraw
    frame = Frame(
        frame_uid=uid,
        raw_path=path,
        instrument=instrument,
        camera=(camera if camera is not None else "204") if instrument == "MASTER" else camera,
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=date_obs or datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        mjd=60203.0,
        exptime=60.0,
        ra_center_deg=327.6,
        dec_center_deg=72.1,
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json=header,
    )
    if observation is not None:
        frame.observation_id = observation.observation_id
    session.add(frame)
    session.flush()
    return frame


def _assign_to_series(session: Session, frame: Frame, *, series_uid: str = "series-test") -> FieldSeries:
    field = Field(
        field_uid=f"field-{series_uid}",
        field_name="test",
        ra_center_deg=327.6,
        dec_center_deg=72.1,
        radius_arcmin=30.0,
        metadata_json={},
    )
    session.add(field)
    session.flush()
    series = FieldSeries(
        field_id=field.field_id,
        series_uid=series_uid,
        filter_name="V",
        n_frames=1,
        grouping_method="test",
        metadata_json={},
    )
    session.add(series)
    session.flush()
    session.add(FieldSeriesFrame(series_id=series.series_id, frame_id=frame.frame_id, order_index=1))
    session.flush()
    return series


def test_single_unrepresented_observation_is_selected(db_session: Session) -> None:
    obs = _observation(db_session, "204_1")
    frame = _frame(db_session, uid="f1", path="C:/a.fits", observation=obs)

    result = select_persisted_science_frames(db_session, [frame.frame_id])

    assert result.selected_frame_ids == [frame.frame_id]
    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED.value
    assert result.summary["selected_frames"] == 1
    assert result.summary["blocked_frames"] == 0


def test_existing_series_representative_blocks_alternate_product(db_session: Session) -> None:
    obs = _observation(db_session, "204_2")
    original = _frame(db_session, uid="f2a", path="C:/original.fits", observation=obs)
    duplicate = _frame(db_session, uid="f2b", path="C:/copy.fits", observation=obs)
    series = _assign_to_series(db_session, original)

    result = select_persisted_science_frames(db_session, [duplicate.frame_id])

    assert result.selected_frame_ids == []
    row = result.rows[0]
    assert row["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert row["selected_frame_id"] == original.frame_id
    assert row["existing_series_ids"] == [series.series_id]
    assert row["product_frame_ids"] == [original.frame_id, duplicate.frame_id]


def test_multiple_unassigned_products_are_ambiguous(db_session: Session) -> None:
    obs = _observation(db_session, "204_3")
    first = _frame(db_session, uid="f3a", path="C:/a.fits", observation=obs)
    second = _frame(db_session, uid="f3b", path="C:/b.fits", observation=obs)

    result = select_persisted_science_frames(db_session, [first.frame_id, second.frame_id])

    assert result.selected_frame_ids == []
    assert {row["action"] for row in result.rows} == {
        ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value
    }
    assert result.summary["blocked_frames"] == 2


def test_supported_instrument_without_observation_is_blocked(db_session: Session) -> None:
    frame = _frame(db_session, uid="missing", path="C:/missing.fits", observation=None, instrument="RoboPhot")

    result = select_persisted_science_frames(db_session, [frame.frame_id])

    assert result.selected_frame_ids == []
    assert result.rows[0]["action"] == ObservationScienceAction.MISSING_OBSERVATION_ID.value


def test_legacy_instrument_without_observation_keeps_old_behavior(db_session: Session) -> None:
    frame = _frame(db_session, uid="legacy", path="C:/legacy.fits", observation=None, instrument="OtherScope")

    result = select_persisted_science_frames(db_session, [frame.frame_id])

    assert result.selected_frame_ids == [frame.frame_id]
    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID.value


def test_distinct_observations_with_same_timestamp_are_both_selected(db_session: Session) -> None:
    time = datetime(2026, 4, 2, 16, 55, 17, 268000, tzinfo=timezone.utc)
    obs1 = _observation(db_session, "204_A")
    obs2 = _observation(db_session, "204_B")
    f1 = _frame(db_session, uid="same-time-a", path="C:/a.fits", observation=obs1, date_obs=time)
    f2 = _frame(db_session, uid="same-time-b", path="C:/b.fits", observation=obs2, date_obs=time)

    result = select_persisted_science_frames(db_session, [f1.frame_id, f2.frame_id])

    assert result.selected_frame_ids == [f1.frame_id, f2.frame_id]


def _candidate(
    native: str,
    *,
    frame_uid: str,
    path: str,
    status: str = "READY",
    instrument: str = "MASTER",
    date_obs: datetime | None = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
    camera: str | None = None,
    raw_relative_physical_key: str | None = None,
) -> ScienceFileIdentityCandidate:
    scheme = "master_uniqnum_v1"
    return ScienceFileIdentityCandidate(
        path=path,
        instrument=instrument,
        frame_uid=frame_uid,
        observation_uid=make_observation_uid(scheme, native) if status == "READY" else None,
        identity_scheme=scheme if status == "READY" else None,
        native_identity=native if status == "READY" else None,
        date_obs=date_obs,
        camera=camera,
        raw_relative_physical_key=raw_relative_physical_key,
        identity_status=status,
        identity_detail=None if status == "READY" else "missing UNIQNUM",
    )


def test_plan_blocks_distinct_master_dates_before_database_lookup() -> None:
    class NoQuerySession:
        def scalar(self, *_args, **_kwargs):
            raise AssertionError("conflicting input dates must be blocked before DB lookup")

    result = plan_file_science_selection(
        NoQuerySession(),
        [
            _candidate("204_COLLIDE", frame_uid="a", path="C:/a.fits"),
            _candidate(
                "204_COLLIDE",
                frame_uid="b",
                path="C:/b.fits",
                date_obs=datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    collision = ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    assert {row["action"] for row in result.rows} == {collision}
    assert result.summary["selected_frames"] == 0
    assert result.summary["blocked_frames"] == 2
    assert all("DATE-OBS" in row["reason"] for row in result.rows)


@pytest.mark.parametrize(
    ("candidate_date", "expected_action"),
    [
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 22, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value,
            id="same-calendar-second",
        ),
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 23, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
            id="next-calendar-second",
        ),
    ],
)
def test_candidate_master_dates_compare_by_calendar_second(
    db_session: Session, candidate_date: datetime, expected_action: str
) -> None:
    first_date = datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=timezone.utc)

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                "204_CANDIDATE_DATE_PRECISION",
                frame_uid="candidate-date-a",
                path="C:/candidate-date-a.fits",
                date_obs=first_date,
            ),
            _candidate(
                "204_CANDIDATE_DATE_PRECISION",
                frame_uid="candidate-date-b",
                path="C:/candidate-date-b.fits",
                date_obs=candidate_date,
            ),
        ],
    )

    assert {row["action"] for row in result.rows} == {expected_action}
    if expected_action == ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value:
        assert first_date.isoformat() in result.rows[0]["reason"]
        assert candidate_date.isoformat() in result.rows[0]["reason"]


def test_plan_blocks_missing_master_date_instead_of_safe_noop() -> None:
    class NoQuerySession:
        def scalar(self, *_args, **_kwargs):
            raise AssertionError("missing candidate DATE-OBS must be blocked before DB lookup")

    result = plan_file_science_selection(
        NoQuerySession(),
        [_candidate("204_UNDATED", frame_uid="undated", path="C:/undated.fits", date_obs=None)],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM.value
    )
    assert result.summary["selected_frames"] == 0


@pytest.mark.parametrize(
    ("candidate_camera", "existing_camera", "candidate_key", "existing_key", "expected"),
    [
        (
            "203",
            "204",
            "rawdata/2023/09/16/100.fit.gz",
            "rawdata/2023/09/16/100.fit.gz",
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
        ),
        (
            "204",
            "204",
            "rawdata/2023/09/16/100.fit.gz",
            "rawdata/2023/09/16/101.fit.gz",
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
        ),
        (
            "204",
            "204",
            "rawdata/2023/09/16/100.fit.gz",
            None,
            ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value,
        ),
    ],
)
def test_physical_master_context_mismatch_or_absence_blocks_represented_noop(
    db_session: Session, candidate_camera, existing_camera, candidate_key, existing_key, expected
) -> None:
    native = "204_PHYSICAL_CONTEXT"
    observation = _observation(db_session, native)
    observation.camera_identity = existing_camera
    frame = _frame(
        db_session,
        uid="physical-context-frame",
        path="C:/existing-physical.fits",
        observation=observation,
        camera=existing_camera,
        raw_relative_physical_key=existing_key,
    )
    if existing_camera is None:
        frame.camera = None
    _assign_to_series(db_session, frame)

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-physical-frame",
                path="C:/incoming-physical.fits",
                camera=candidate_camera,
                raw_relative_physical_key=candidate_key,
            )
        ],
    )

    assert result.rows[0]["action"] == expected
    assert result.summary["selected_frames"] == 0
    assert result.summary["blocked_frames"] == 1


def test_physical_master_context_without_persisted_camera_is_ambiguous(db_session: Session) -> None:
    native = "204_PHYSICAL_UNKNOWN_CAMERA"
    observation = _observation(db_session, native)
    observation.camera_identity = None
    frame = _frame(
        db_session,
        uid="physical-unknown-camera-frame",
        path="C:/existing-unknown-camera.fits",
        observation=observation,
        raw_relative_physical_key="rawdata/2023/09/16/100.fit.gz",
    )
    frame.camera = None
    _assign_to_series(db_session, frame)

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-unknown-camera",
                path="C:/incoming-unknown-camera.fits",
                camera="204",
                raw_relative_physical_key="rawdata/2023/09/16/100.fit.gz",
            )
        ],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value
    )
    assert result.summary["selected_frames"] == 0


def test_exact_master_physical_context_is_represented_without_series_membership(
    db_session: Session,
) -> None:
    native = "204_UNASSIGNED_PHYSICAL_DUPLICATE"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    source_key = "rawdata/2023/09/16/100.fit.gz"
    observation = _observation(db_session, native)
    observation.observed_at = date_obs
    frame = _frame(
        db_session,
        uid="unassigned-physical-existing",
        path="C:/existing-product.fits",
        observation=observation,
        camera="204",
        date_obs=date_obs,
        raw_relative_physical_key=source_key,
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="replayed-product-uid",
                path="C:/replayed-product.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=source_key,
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id
    assert result.summary["selected_frames"] == 0


@pytest.mark.parametrize(
    ("candidate_date", "expected_action"),
    [
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 22, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.ALREADY_REPRESENTED.value,
            id="same-calendar-second",
        ),
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 23, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
            id="next-calendar-second",
        ),
    ],
)
def test_existing_master_physical_duplicate_compares_dates_by_calendar_second(
    db_session: Session, candidate_date: datetime, expected_action: str
) -> None:
    native = "204_EXISTING_DATE_PRECISION"
    existing_date = datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=timezone.utc)
    raw_key = "rawdata/2018/09/07/375506.fit.gz"
    observation = _observation(db_session, native)
    observation.observed_at = existing_date
    _frame(
        db_session,
        uid="existing-date-precision",
        path="C:/existing-date-precision.fits",
        observation=observation,
        date_obs=existing_date,
        camera="204",
        raw_relative_physical_key=raw_key,
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-date-precision",
                path="D:/incoming-date-precision.fits",
                date_obs=candidate_date,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    assert result.rows[0]["action"] == expected_action
    if expected_action == ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value:
        assert existing_date.isoformat() in result.rows[0]["reason"]


def test_existing_master_duplicate_groups_candidate_dates_by_calendar_second(
    db_session: Session,
) -> None:
    native = "204_EXISTING_CANDIDATE_CONTEXT_PRECISION"
    existing_date = datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=timezone.utc)
    raw_key = "rawdata/2018/09/07/375506.fit.gz"
    observation = _observation(db_session, native)
    observation.observed_at = existing_date
    frame = _frame(
        db_session,
        uid="existing-candidate-context-precision",
        path="C:/existing-candidate-context-precision.fits",
        observation=observation,
        date_obs=existing_date,
        camera="204",
        raw_relative_physical_key=raw_key,
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-candidate-context-a",
                path="D:/incoming-candidate-context-a.fits",
                date_obs=existing_date,
                camera="204",
                raw_relative_physical_key=raw_key,
            ),
            _candidate(
                native,
                frame_uid="incoming-candidate-context-b",
                path="D:/incoming-candidate-context-b.fits",
                date_obs=existing_date.replace(microsecond=1000),
                camera="204",
                raw_relative_physical_key=raw_key,
            ),
        ],
    )

    assert len(result.rows) == 2
    assert {row["action"] for row in result.rows} == {
        ObservationScienceAction.ALREADY_REPRESENTED.value
    }
    assert {row["selected_frame_id"] for row in result.rows} == {frame.frame_id}


def test_exact_unlinked_master_physical_frame_is_safe_duplicate_noop(
    db_session: Session,
) -> None:
    native = "204_UNLINKED_PHYSICAL_DUPLICATE"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    source_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="unlinked-physical-exact",
        path="C:/legacy-unlinked.fits",
        instrument="MASTER",
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=source_key,
        header_uniqnum=native,
    )
    before = (
        db_session.query(Frame).count(),
        dict(frame.header_json),
        db_session.query(FieldSeriesFrame).count(),
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-unlinked-physical-copy",
                path="D:/incoming-product.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=source_key,
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id
    assert result.rows[0]["product_frame_ids"] == [frame.frame_id]
    assert result.selected_frame_ids == []
    assert result.summary["selected_frames"] == 0
    assert (
        db_session.query(Frame).count(),
        dict(frame.header_json),
        db_session.query(FieldSeriesFrame).count(),
    ) == before


def test_unlinked_master_uniqnum_without_raw_key_is_ambiguous_not_selected(
    db_session: Session,
) -> None:
    native = "204_UNLINKED_MISSING_SOURCE_KEY"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    frame = _frame(
        db_session,
        uid="unlinked-physical-missing-key",
        path="C:/legacy-missing-key.fits",
        instrument="MASTER",
        date_obs=date_obs,
        camera="204",
        header_uniqnum=native,
    )
    before = (db_session.query(Frame).count(), dict(frame.header_json))

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-missing-key-copy",
                path="D:/incoming-product.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key="rawdata/2023/09/16/100.fit.gz",
            )
        ],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value
    )
    assert result.rows[0]["selected_frame_id"] is None
    assert result.selected_frame_ids == []
    assert result.summary["selected_frames"] == 0
    assert (db_session.query(Frame).count(), dict(frame.header_json)) == before


@pytest.mark.parametrize(
    ("existing_date", "existing_key"),
    [
        (
            datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
            "rawdata/2023/09/16/100.fit.gz",
        ),
        (
            datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
            "rawdata/2023/09/16/101.fit.gz",
        ),
    ],
)
def test_unlinked_master_uniqnum_with_known_context_mismatch_is_collision(
    db_session: Session,
    existing_date: datetime,
    existing_key: str,
) -> None:
    native = "204_UNLINKED_CONTEXT_COLLISION"
    candidate_date = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    candidate_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="unlinked-physical-collision",
        path="C:/legacy-collision.fits",
        instrument="MASTER",
        date_obs=existing_date,
        camera="204",
        raw_relative_physical_key=existing_key,
        header_uniqnum=native,
    )
    before = (db_session.query(Frame).count(), dict(frame.header_json), frame.date_obs)

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-collision-copy",
                path="D:/incoming-product.fits",
                date_obs=candidate_date,
                camera="204",
                raw_relative_physical_key=candidate_key,
            )
        ],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )
    assert result.rows[0]["selected_frame_id"] is None
    assert result.selected_frame_ids == []
    assert result.summary["selected_frames"] == 0
    assert (db_session.query(Frame).count(), dict(frame.header_json), frame.date_obs) == before


def test_plan_blocks_master_date_conflict_with_existing_frame(db_session: Session) -> None:
    obs = _observation(db_session, "204_EXISTING_DATE")
    _frame(db_session, uid="existing-date", path="C:/existing.fits", observation=obs)

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                "204_EXISTING_DATE",
                frame_uid="incoming-date",
                path="C:/incoming.fits",
                date_obs=datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
            )
        ],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )
    assert result.summary["selected_frames"] == 0


def test_plan_blocks_missing_existing_master_date(db_session: Session) -> None:
    obs = _observation(db_session, "204_EXISTING_UNDATED")
    existing = _frame(db_session, uid="existing-undated", path="C:/existing.fits", observation=obs)
    existing.date_obs = None
    db_session.flush()

    result = plan_file_science_selection(
        db_session,
        [_candidate("204_EXISTING_UNDATED", frame_uid="incoming", path="C:/incoming.fits")],
    )

    assert result.rows[0]["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM.value
    )

    different_known_time = plan_file_science_selection(
        db_session,
        [
            _candidate(
                "204_EXISTING_UNDATED",
                frame_uid="incoming-known-time-conflict",
                path="C:/incoming-known-time-conflict.fits",
                date_obs=datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
            )
        ],
    )
    assert different_known_time.rows[0]["action"] == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )


def test_persisted_selector_blocks_master_products_with_distinct_dates(db_session: Session) -> None:
    obs = _observation(db_session, "204_PERSISTED_COLLISION")
    first = _frame(db_session, uid="persisted-a", path="C:/a.fits", observation=obs)
    second = _frame(
        db_session,
        uid="persisted-b",
        path="C:/b.fits",
        observation=obs,
        date_obs=datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
    )

    result = select_persisted_science_frames(db_session, [first.frame_id, second.frame_id])

    assert result.selected_frame_ids == []
    assert {row["action"] for row in result.rows} == {
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    }
    assert result.summary["blocked_frames"] == 2


@pytest.mark.parametrize(
    ("second_date", "expected_action"),
    [
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 22, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value,
            id="same-calendar-second",
        ),
        pytest.param(
            datetime(2018, 9, 7, 17, 4, 23, 1000, tzinfo=timezone.utc),
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
            id="next-calendar-second",
        ),
    ],
)
def test_persisted_master_dates_compare_by_calendar_second(
    db_session: Session, second_date: datetime, expected_action: str
) -> None:
    observation = _observation(db_session, "204_PERSISTED_DATE_PRECISION")
    first_date = datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=timezone.utc)
    first = _frame(
        db_session,
        uid="persisted-date-precision-a",
        path="C:/persisted-date-precision-a.fits",
        observation=observation,
        date_obs=first_date,
    )
    second = _frame(
        db_session,
        uid="persisted-date-precision-b",
        path="C:/persisted-date-precision-b.fits",
        observation=observation,
        date_obs=second_date,
    )

    result = select_persisted_science_frames(db_session, [first.frame_id, second.frame_id])

    assert {row["action"] for row in result.rows} == {expected_action}
    if expected_action == ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value:
        assert first_date.isoformat() in result.rows[0]["reason"]
        assert second_date.isoformat() in result.rows[0]["reason"]


def test_persisted_non_master_dates_are_not_compared_with_master_precision(
    db_session: Session,
) -> None:
    observation = _observation(db_session, "204_NON_MASTER_DATE_PRECISION")
    observation.identity_scheme = "robophot_sernum_dateobs_v1"
    observation.observation_uid = make_observation_uid(
        observation.identity_scheme, observation.native_identity
    )
    first = _frame(
        db_session,
        uid="persisted-non-master-date-a",
        path="C:/persisted-non-master-date-a.fits",
        observation=observation,
        date_obs=datetime(2018, 9, 7, 17, 4, 22, 999000, tzinfo=timezone.utc),
    )
    second = _frame(
        db_session,
        uid="persisted-non-master-date-b",
        path="C:/persisted-non-master-date-b.fits",
        observation=observation,
        date_obs=datetime(2018, 9, 7, 17, 4, 22, 1000, tzinfo=timezone.utc),
    )

    result = select_persisted_science_frames(db_session, [first.frame_id, second.frame_id])

    assert {row["action"] for row in result.rows} == {
        ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value
    }


def test_persisted_selector_blocks_missing_master_date_even_when_represented(
    db_session: Session,
) -> None:
    obs = _observation(db_session, "204_PERSISTED_UNDATED")
    frame = _frame(db_session, uid="persisted-undated", path="C:/undated.fits", observation=obs)
    _assign_to_series(db_session, frame)
    frame.date_obs = None
    db_session.flush()

    result = select_persisted_science_frames(db_session, [frame.frame_id])

    assert result.selected_frame_ids == []
    assert result.rows[0]["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM.value
    )


def test_plan_exact_existing_unassigned_product_is_selectable(db_session: Session) -> None:
    obs = _observation(db_session, "204_P1")
    _frame(db_session, uid="plan-exact", path="C:/same.fits", observation=obs)

    result = plan_file_science_selection(
        db_session,
        [_candidate("204_P1", frame_uid="plan-exact", path="C:/same.fits")],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED.value
    assert result.summary["selected_frames"] == 1


def test_plan_new_copy_of_unassigned_existing_product_is_ambiguous(db_session: Session) -> None:
    obs = _observation(db_session, "204_P2")
    _frame(db_session, uid="plan-old", path="C:/old.fits", observation=obs)

    result = plan_file_science_selection(
        db_session,
        [_candidate("204_P2", frame_uid="plan-new", path="C:/new.fits")],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value


def test_plan_new_copy_of_assigned_observation_is_already_represented(db_session: Session) -> None:
    obs = _observation(db_session, "204_P3")
    original = _frame(db_session, uid="plan-assigned", path="C:/old.fits", observation=obs)
    series = _assign_to_series(db_session, original, series_uid="series-plan")

    result = plan_file_science_selection(
        db_session,
        [_candidate("204_P3", frame_uid="plan-copy", path="C:/copy.fits")],
    )

    row = result.rows[0]
    assert row["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert row["existing_series_ids"] == [series.series_id]


def test_plan_two_new_products_for_same_new_observation_is_ambiguous(db_session: Session) -> None:
    result = plan_file_science_selection(
        db_session,
        [
            _candidate("204_P4", frame_uid="plan-a", path="C:/a.fits"),
            _candidate("204_P4", frame_uid="plan-b", path="C:/b.fits"),
        ],
    )

    assert {row["action"] for row in result.rows} == {
        ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value
    }
    assert result.summary["blocked_frames"] == 2


def test_plan_missing_identity_for_master_is_blocking(db_session: Session) -> None:
    result = plan_file_science_selection(
        db_session,
        [_candidate("unused", frame_uid="missing-plan", path="C:/missing.fits", status="MISSING")],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.MISSING_OBSERVATION_ID.value


def test_build_field_series_filters_frames_through_observation_guard(monkeypatch) -> None:
    import grandgousier.archive.fields_series as fields_series

    frames = [
        {
            "frame_id": 1,
            "instrument_unit_id": 7,
            "instrument": "MASTER",
            "site": "KAO",
            "tube": "EAST",
            "camera": "204",
            "filter_name": "V",
            "object_name": "GPX",
            "ra_center_deg": 327.6,
            "dec_center_deg": 72.1,
            "date_obs": datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
            "mjd": 60203.0,
            "exptime": 60.0,
            "wcs_status": "VALID",
            "already_assigned": False,
        },
        {
            "frame_id": 2,
            "instrument_unit_id": 7,
            "instrument": "MASTER",
            "site": "KAO",
            "tube": "EAST",
            "camera": "204",
            "filter_name": "V",
            "object_name": "GPX",
            "ra_center_deg": 327.6,
            "dec_center_deg": 72.1,
            "date_obs": datetime(2023, 9, 16, 16, 1, tzinfo=timezone.utc),
            "mjd": 60203.001,
            "exptime": 60.0,
            "wcs_status": "VALID",
            "already_assigned": False,
        },
    ]
    monkeypatch.setattr(fields_series, "_load_candidate_frames", lambda *_args, **_kwargs: frames)
    monkeypatch.setattr(fields_series, "_load_frame_selection_diagnostics", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(fields_series, "_load_fields", lambda _session: [])
    monkeypatch.setattr(
        fields_series,
        "select_persisted_science_frames",
        lambda _session, _ids, **_kwargs: ObservationScienceSelectionResult(
            selected_frame_ids=[1],
            rows=[{"frame_id": 1, "action": "SELECTED"}, {"frame_id": 2, "action": "ALREADY_REPRESENTED"}],
            summary={"input_frames": 2, "selected_frames": 1, "blocked_frames": 1, "actions": {}},
        ),
    )

    result = fields_series.build_field_series(object(), dry_run=True, min_frames_per_series=1)

    assert result.observation_selection["selected_frames"] == 1
    assert result.observation_selection["blocked_frames"] == 1
    assert result.created_series == 1
    assert result.linked_frames == 1
    assert result.rows[0]["frame_id_first"] == 1
    assert result.rows[0]["frame_id_last"] == 1


def test_plan_uid_only_mismatched_existing_row_is_identity_conflict(db_session: Session) -> None:
    candidate = _candidate("204_SPLIT_PLAN", frame_uid="split-plan", path="C:/split.fits")
    bad = Observation(
        observation_uid=candidate.observation_uid,
        instrument="MASTER",
        identity_scheme="master_uniqnum_v1",
        native_identity="DIFFERENT_NATIVE",
        identity_components={},
    )
    db_session.add(bad)
    db_session.flush()

    result = plan_file_science_selection(db_session, [candidate])

    assert result.rows[0]["action"] == ObservationScienceAction.IDENTITY_CONFLICT.value
    assert result.summary["blocked_frames"] == 1


def test_existing_multiple_series_representatives_are_explicit_conflict(db_session: Session) -> None:
    obs = _observation(db_session, "204_DOUBLE")
    first = _frame(db_session, uid="double-a", path="C:/double-a.fits", observation=obs)
    second = _frame(db_session, uid="double-b", path="C:/double-b.fits", observation=obs)
    _assign_to_series(db_session, first, series_uid="double-series-a")
    _assign_to_series(db_session, second, series_uid="double-series-b")

    result = select_persisted_science_frames(db_session, [first.frame_id])

    assert result.selected_frame_ids == []
    assert result.rows[0]["action"] == ObservationScienceAction.EXISTING_MULTIPLE_REPRESENTATIVES.value
    assert result.summary["blocked_frames"] == 1


def test_process_archive_path_plans_and_executes_guard_before_new_series_membership() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "archive" / "auto_process.py").read_text(encoding="utf-8")
    process_start = source.index("def process_archive_path(")
    process_end = source.index("\ndef process_archive_summary_rows", process_start)
    body = source[process_start:process_end]

    assert body.index("plan_file_science_selection(") < body.index("if not execute:") + body[body.index("if not execute:"):].index("return result")
    assert body.index("select_persisted_science_frames(") < body.index("_prepare_auto_catalog(")
    assert "_frame_ids_by_instrument_unit(session, science_input_frame_ids)" in body
    assert "if not science_input_frame_ids:" in body
    assert "pipeline_series_ids = sorted(set(created_series_ids))" in body
    assert "_merge_current_series_ids(\n            created_series_ids, existing_input_series_ids\n        )" not in body



def test_process_archive_path_replay_does_not_queue_historical_series() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "archive" / "auto_process.py").read_text(encoding="utf-8")
    process_start = source.index("def process_archive_path(")
    process_end = source.index("\ndef process_archive_summary_rows", process_start)
    body = source[process_start:process_end]

    guard_pos = body.index("if not science_input_frame_ids:")
    catalog_pos = body.index("if catalog_name is None:")
    queue_pos = body.index("pipeline_series_ids = sorted(set(created_series_ids))")
    load_pos = body.index("for series in _load_series_for_pipeline(session, pipeline_series_ids):")

    assert guard_pos < catalog_pos
    assert queue_pos < load_pos
    assert "pipeline_series_ids = current_series_ids" not in body


def test_manifest_writes_observation_selection_csv_when_requested() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "archive" / "auto_process.py").read_text(encoding="utf-8")
    assert "def _write_observation_selection_csv(" in source
    assert 'f"{manifest_path.stem}_observation_selection.csv"' in source
    assert 'result.observation_selection_csv_path = str(selection_csv)' in source


def test_persisted_selection_exports_physical_identity_diagnostics(db_session: Session) -> None:
    obs = _observation(db_session, "204_DIAGNOSTIC")
    frame = _frame(db_session, uid="diag-a", path="C:/diag-a.fits", observation=obs)

    result = select_persisted_science_frames(db_session, [frame.frame_id])

    row = result.rows[0]
    assert row["observation_uid"] == obs.observation_uid
    assert row["identity_scheme"] == "master_uniqnum_v1"
    assert row["native_identity"] == "204_DIAGNOSTIC"
    assert row["identity_status"] == "READY"
    assert result.summary["physical_identity_groups"] == 1


def test_explicit_reassign_preserves_same_frame_but_not_alternate_product(db_session: Session) -> None:
    obs = _observation(db_session, "204_REASSIGN")
    assigned = _frame(db_session, uid="reassign-a", path="C:/reassign-a.fits", observation=obs)
    alternate = _frame(db_session, uid="reassign-b", path="C:/reassign-b.fits", observation=obs)
    _assign_to_series(db_session, assigned, series_uid="reassign-series")

    result = select_persisted_science_frames(
        db_session,
        [assigned.frame_id, alternate.frame_id],
        allow_reassign_existing_frame=True,
    )

    rows = {row["frame_id"]: row for row in result.rows}
    assert result.selected_frame_ids == [assigned.frame_id]
    assert rows[assigned.frame_id]["action"] == ObservationScienceAction.SELECTED_EXPLICIT_REASSIGN.value
    assert rows[alternate.frame_id]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value


def test_plan_explicit_reassign_selects_only_exact_assigned_frame_uid(db_session: Session) -> None:
    obs = _observation(db_session, "204_REASSIGN_PLAN")
    assigned = _frame(db_session, uid="reassign-plan-a", path="C:/reassign-plan-a.fits", observation=obs)
    _assign_to_series(db_session, assigned, series_uid="reassign-plan-series")

    exact = _candidate("204_REASSIGN_PLAN", frame_uid="reassign-plan-a", path="C:/reassign-plan-a.fits")
    alternate = _candidate("204_REASSIGN_PLAN", frame_uid="reassign-plan-b", path="C:/reassign-plan-b.fits")
    result = plan_file_science_selection(
        db_session,
        [exact, alternate],
        allow_reassign_existing_frame=True,
    )

    rows = {row["frame_uid"]: row for row in result.rows}
    assert rows["reassign-plan-a"]["action"] == ObservationScienceAction.SELECTED_EXPLICIT_REASSIGN.value
    assert rows["reassign-plan-b"]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert result.summary["selected_frames"] == 1
    assert result.summary["blocked_frames"] == 1


def test_series_uid_distinguishes_grouping_semantics() -> None:
    from grandgousier.archive.fields_series import _make_series_uid

    common = dict(
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        mjd_start=58368.1,
        mjd_end=58368.2,
        frame_ids=[1, 2, 3, 4],
    )
    by_night = _make_series_uid(**common, grouping_method="night_filter")
    by_observation = _make_series_uid(**common, grouping_method="observation_filter")
    assert by_night != by_observation


def test_stable_series_uid_is_anchored_to_first_database_frame() -> None:
    from grandgousier.archive.fields_series import _make_stable_series_uid

    common = {
        "field_id": 33,
        "instrument_unit_id": 7,
        "filter_name": "V",
        "grouping_method": "center_time_v1",
        "first_frame_id": 1001,
    }
    initial = _make_stable_series_uid(**common)
    after_append = _make_stable_series_uid(**common)
    different_anchor = _make_stable_series_uid(**{**common, "first_frame_id": 1002})

    assert initial == after_append
    assert initial != different_anchor


def test_field_series_membership_writer_conflicts_on_membership_pair() -> None:
    from pathlib import Path
    import grandgousier.archive.fields_series as fields_series

    source = Path(fields_series.__file__).read_text(encoding="utf-8")
    assert "ON CONFLICT (series_id, frame_id) DO UPDATE" in source
    assert "ON CONFLICT (frame_id) DO UPDATE" not in source


def test_plan_path_relocated_copy_reuses_existing_product_metadata(db_session: Session) -> None:
    obs = _observation(db_session, "204_RELOCATE")
    frame = Frame(
        frame_uid="old-path-sensitive-uid",
        raw_path="C:/old/location.fits",
        file_size_bytes=123456,
        instrument="MASTER",
        camera="204",
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        mjd=60203.0,
        exptime=60.0,
        object_name="FIELD_A",
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json={"NAXIS1": 4096, "NAXIS2": 4096, "UNIQNUM": "204_RELOCATE"},
        observation_id=obs.observation_id,
    )
    db_session.add(frame)
    db_session.flush()

    candidate = ScienceFileIdentityCandidate(
        path="D:/moved/location.fits",
        instrument="MASTER",
        frame_uid="new-path-sensitive-uid",
        observation_uid=obs.observation_uid,
        identity_scheme=obs.identity_scheme,
        native_identity=obs.native_identity,
        date_obs=datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        file_size_bytes=123456,
        naxis1=4096,
        naxis2=4096,
        object_name="FIELD_A",
    )
    result = plan_file_science_selection(db_session, [candidate])

    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id
    assert result.rows[0]["product_frame_ids"] == [frame.frame_id]


@pytest.mark.parametrize(
    ("field_ids", "expected_ids"),
    [([101, 101], [101]), (None, [101, 202])],
)
def test_field_summary_scopes_grouped_scan_and_updates(field_ids, expected_ids) -> None:
    from grandgousier.archive import fields_series

    class FakeSession:
        def __init__(self):
            self.updates = []
            self.grouped_stmt = None

        def execute(self, statement, params=None):
            sql = str(statement)
            if "JOIN field_series s ON s.field_id = f.field_id" in sql:
                self.grouped_stmt = statement
                if field_ids is None:
                    assert "WHERE f.field_id IN" not in sql
                    assert params == {}
                    ids = [101, 202]
                else:
                    assert sql.index("WHERE f.field_id IN") < sql.index("GROUP BY f.field_id")
                    assert params == {"field_ids": [101]}
                    ids = params["field_ids"]
                rows = [{"field_id": field_id} for field_id in ids]
            elif "SELECT ra_center_deg, dec_center_deg FROM field_series" in sql:
                assert params["field_id"] in expected_ids
                rows = (
                    [{"ra_center_deg": ra, "dec_center_deg": dec} for ra, dec in [(359, 10), (1, 20)]]
                    if params["field_id"] == 101
                    else [{"ra_center_deg": 100, "dec_center_deg": 30}]
                )
            elif "UPDATE fields" in sql:
                assert "WHERE field_id = :field_id" in sql
                self.updates.append(params)
                return None
            else:
                raise AssertionError(f"unexpected SQL: {sql}")
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    session = FakeSession()
    fields_series._refresh_field_summaries(session, field_ids=field_ids)

    assert [row["field_id"] for row in session.updates] == expected_ids
    assert abs((session.updates[0]["ra_center_deg"] + 180) % 360 - 180) < 1e-6
    assert session.updates[0]["dec_center_deg"] == 15
    if field_ids is not None:
        bound = session.grouped_stmt._bindparams["field_ids"]
        assert bound.expanding and isinstance(bound.type, BigInteger)


def test_field_summary_empty_scope_never_touches_session() -> None:
    from grandgousier.archive import fields_series

    class NoSqlSession:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("empty scope must not SELECT or UPDATE")

    for scope in ([], iter(())):
        fields_series._refresh_field_summaries(NoSqlSession(), field_ids=scope)


def test_build_field_series_refreshes_only_persisted_series_fields_for_each_unit(monkeypatch) -> None:
    from grandgousier.archive import fields_series

    def frame(frame_id, field_id, unit):
        return {
            "frame_id": frame_id, "field_id": field_id, "instrument_unit_id": unit,
            "filter_name": "V", "ra_center_deg": 50.0, "dec_center_deg": 20.0,
            "mjd": 60000.0, "already_assigned": False,
        }

    frames_by_unit = {
        7: [frame(1, 101, 7), frame(2, 101, 7), frame(3, 102, 7)],
        8: [frame(4, 103, 8), frame(5, 103, 8)],
        9: [frame(6, 202, 9)],
    }
    monkeypatch.setattr(
        fields_series, "_load_candidate_frames",
        lambda _session, *, instrument_unit_id, **_kw: frames_by_unit[instrument_unit_id],
    )
    monkeypatch.setattr(fields_series, "_load_frame_selection_diagnostics", lambda *_a, **_kw: {})
    monkeypatch.setattr(
        fields_series, "select_persisted_science_frames",
        lambda _session, ids, **_kw: ObservationScienceSelectionResult(
            selected_frame_ids=ids, rows=[], summary={}
        ),
    )
    monkeypatch.setattr(
        fields_series, "_load_fields",
        lambda _session: [{"field_id": field_id} for field_id in (101, 102, 103, 202)],
    )
    monkeypatch.setattr(
        fields_series, "_find_or_create_field",
        lambda _session, *, frame, field_cache, **_kw: (field_cache[frame["field_id"]], False),
    )

    def fake_create(_session, *, result, field_id, frames, min_frames, **_kw):
        if len(frames) >= min_frames:
            result.rows.append({"field_id": field_id, "series_id": field_id + 1000})

    scopes = []
    commits = []
    monkeypatch.setattr(fields_series, "_create_series_group", fake_create)
    monkeypatch.setattr(
        fields_series, "_refresh_field_summaries",
        lambda _session, field_ids=None: scopes.append(field_ids),
    )
    session = SimpleNamespace(commit=lambda: commits.append(True))

    for unit in (7, 8, 9):
        fields_series.build_field_series(session, instrument_unit_id=unit, min_frames_per_series=2)

    assert scopes == [{101}, {103}, set()]
    assert len(commits) == 3


def test_legacy_ggraw_exact_duplicate_is_already_represented(db_session: Session) -> None:
    native = "204_LEGACY_GGRAW_DUPLICATE"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    raw_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="legacy-ggraw-duplicate",
        path="C:/legacy-ggraw.fits",
        date_obs=date_obs,
        camera="204",
        header_uniqnum=native,
        ggraw=f"/master/imdata/{raw_key}",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-ggraw-duplicate",
                path="D:/incoming.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert row["action"] != ObservationScienceAction.SELECTED.value
    assert row["selected_frame_id"] == frame.frame_id
    assert result.selected_frame_ids == []


@pytest.mark.parametrize(
    ("existing_raw_key", "existing_date"),
    [
        (
            "rawdata/2023/09/16/101.fit.gz",
            datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        ),
        (
            "rawdata/2023/09/16/100.fit.gz",
            datetime(2023, 9, 17, 16, 0, tzinfo=timezone.utc),
        ),
    ],
    ids=["different-source", "different-date"],
)
def test_legacy_ggraw_source_or_date_mismatch_is_physical_collision(
    db_session: Session, existing_raw_key: str, existing_date: datetime
) -> None:
    native = "204_LEGACY_GGRAW_COLLISION"
    candidate_date = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    candidate_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="legacy-ggraw-collision",
        path="C:/legacy-ggraw-collision.fits",
        date_obs=existing_date,
        camera="204",
        header_uniqnum=native,
        ggraw=f"/master/imdata/{existing_raw_key}",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-ggraw-collision",
                path="D:/incoming-collision.fits",
                date_obs=candidate_date,
                camera="204",
                raw_relative_physical_key=candidate_key,
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == (
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value
    )
    assert "physical-source/date collision" in row["reason"]
    assert result.selected_frame_ids == []


@pytest.mark.parametrize(
    "ggraw",
    [
        None,
        "/other/root/rawdata/2023/09/16/100.fit.gz",
        "/master/imdata/rawdata/2023/09/16/../100.fit.gz",
    ],
    ids=["missing", "wrong-root", "traversal"],
)
def test_legacy_ggraw_without_usable_source_is_context_ambiguous(
    db_session: Session, ggraw: str | None
) -> None:
    native = "204_LEGACY_GGRAW_NO_CONTEXT"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    frame = _frame(
        db_session,
        uid="legacy-ggraw-no-context",
        path="C:/legacy-no-context.fits",
        date_obs=date_obs,
        camera="204",
        header_uniqnum=native,
        ggraw=ggraw,
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-no-context",
                path="D:/incoming-no-context.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key="rawdata/2023/09/16/100.fit.gz",
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value
    )
    assert "no durable raw physical source context" in row["reason"]
    assert row["action"] not in {
        ObservationScienceAction.SELECTED.value,
        ObservationScienceAction.ALREADY_REPRESENTED.value,
    }


def test_structured_source_conflicting_with_valid_ggraw_is_identity_conflict(
    db_session: Session,
) -> None:
    native = "204_STRUCTURED_GGRAW_CONFLICT"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    structured_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="structured-ggraw-conflict",
        path="C:/structured-ggraw-conflict.fits",
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=structured_key,
        header_uniqnum=native,
        ggraw="/master/imdata/rawdata/2023/09/16/101.fit.gz",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-structured-ggraw-conflict",
                path="D:/incoming-structured-ggraw-conflict.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=structured_key,
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == ObservationScienceAction.IDENTITY_CONFLICT.value
    assert "STRUCTURED_SOURCE_VS_GGRAW_CONFLICT" in row["reason"]


def test_invalid_ggraw_does_not_override_matching_structured_source(db_session: Session) -> None:
    native = "204_STRUCTURED_WITH_INVALID_GGRAW"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    raw_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="structured-invalid-ggraw",
        path="C:/structured-invalid-ggraw.fits",
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=raw_key,
        header_uniqnum=native,
        ggraw="/outside/master/imdata/rawdata/2023/09/16/999.fit.gz",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-structured-invalid-ggraw",
                path="D:/incoming-structured-invalid-ggraw.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id


@pytest.mark.parametrize("structured_value", [None, ""], ids=["null", "empty"])
def test_null_or_empty_structured_source_falls_back_to_valid_ggraw(
    db_session: Session, structured_value: str | None
) -> None:
    native = "204_NULL_EMPTY_STRUCTURED_GGRAW_FALLBACK"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    raw_key = "rawdata/2023/09/16/100.fit.gz"
    frame = _frame(
        db_session,
        uid="null-empty-structured-ggraw-fallback",
        path="C:/null-empty-structured-ggraw-fallback.fits",
        date_obs=date_obs,
        camera="204",
        header_uniqnum=native,
        ggraw=f"/master/imdata/{raw_key}",
    )
    frame.header_json = {
        **frame.header_json,
        "raw_relative_physical_key": structured_value,
    }
    db_session.flush()

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-null-empty-structured-ggraw-fallback",
                path="D:/incoming-null-empty-structured-ggraw-fallback.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.ALREADY_REPRESENTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id


def test_malformed_structured_source_does_not_fall_back_to_valid_ggraw(db_session: Session) -> None:
    native = "204_MALFORMED_STRUCTURED_SOURCE"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    frame = _frame(
        db_session,
        uid="malformed-structured-source",
        path="C:/malformed-structured-source.fits",
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key="rawdata/2023/09/16/../100.fit.gz",
        header_uniqnum=native,
        ggraw="/master/imdata/rawdata/2023/09/16/100.fit.gz",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-malformed-structured-source",
                path="D:/incoming-malformed-structured-source.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key="rawdata/2023/09/16/100.fit.gz",
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value
    )
    assert "invalid raw_relative_physical_key" in row["reason"]
    assert row["selected_frame_id"] is None


def test_unmatched_new_physical_master_candidate_is_selected(db_session: Session) -> None:
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                "204_NEW_PHYSICAL_MASTER",
                frame_uid="new-physical-master",
                path="D:/new-physical-master.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key="rawdata/2023/09/16/200.fit.gz",
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED.value
    assert result.summary["selected_frames"] == 1


def test_competing_master_products_remain_multiple_product_ambiguity(db_session: Session) -> None:
    native = "204_COMPETING_PHYSICAL_PRODUCTS"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    raw_key = "rawdata/2023/09/16/300.fit.gz"
    observation = _observation(db_session, native)
    observation.observed_at = date_obs
    first = _frame(
        db_session,
        uid="competing-physical-a",
        path="C:/competing-physical-a.fits",
        observation=observation,
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=raw_key,
    )
    second = _frame(
        db_session,
        uid="competing-physical-b",
        path="C:/competing-physical-b.fits",
        observation=observation,
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=raw_key,
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-competing-physical",
                path="D:/incoming-competing-physical.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    assert result.rows[0]["action"] == ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value
    assert result.rows[0]["product_frame_ids"] == [first.frame_id, second.frame_id]
    assert result.rows[0]["action"] != (
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value
    )


def test_linked_frame_with_contradictory_header_uniqnum_is_identity_conflict(
    db_session: Session,
) -> None:
    native = "204_LINKED_HEADER_UNIQNUM"
    date_obs = datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc)
    raw_key = "rawdata/2023/09/16/400.fit.gz"
    observation = _observation(db_session, native)
    observation.observed_at = date_obs
    _frame(
        db_session,
        uid="linked-header-uniqnum-conflict",
        path="C:/linked-header-uniqnum-conflict.fits",
        observation=observation,
        date_obs=date_obs,
        camera="204",
        raw_relative_physical_key=raw_key,
        header_uniqnum="204_DIFFERENT_HEADER_UNIQNUM",
    )

    result = plan_file_science_selection(
        db_session,
        [
            _candidate(
                native,
                frame_uid="incoming-linked-header-conflict",
                path="D:/incoming-linked-header-conflict.fits",
                date_obs=date_obs,
                camera="204",
                raw_relative_physical_key=raw_key,
            )
        ],
    )

    row = result.rows[0]
    assert row["action"] == ObservationScienceAction.IDENTITY_CONFLICT.value
    assert "header UNIQNUM conflicts" in row["reason"]


def test_frame_selection_diagnostics_reports_frame_and_center_counts(db_session: Session) -> None:
    from grandgousier.archive import fields_series

    _frame(db_session, uid="diagnostic-centered", path="C:/diagnostic-centered.fits")
    missing_center = _frame(
        db_session,
        uid="diagnostic-missing-center",
        path="C:/diagnostic-missing-center.fits",
    )
    missing_center.ra_center_deg = None
    db_session.flush()

    diagnostics = fields_series._load_frame_selection_diagnostics(
        db_session,
        instrument_unit_id=None,
        frame_ids=None,
        only_unassigned=False,
    )

    assert diagnostics == {
        "SCIENCE": {"n_frames": 2, "n_with_center": 1, "n_without_center": 1}
    }
