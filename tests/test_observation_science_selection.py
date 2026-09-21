from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import BigInteger, create_engine, select
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
    FrameStatus,
    FrameType,
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
    Base.metadata.create_all(engine)
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
) -> Frame:
    frame = Frame(
        frame_uid=uid,
        raw_path=path,
        instrument=instrument,
        camera="204" if instrument == "MASTER" else None,
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=date_obs or datetime(2023, 9, 16, 16, 0, tzinfo=timezone.utc),
        mjd=60203.0,
        exptime=60.0,
        ra_center_deg=327.6,
        dec_center_deg=72.1,
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json={},
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
) -> ScienceFileIdentityCandidate:
    scheme = "master_uniqnum_v1"
    return ScienceFileIdentityCandidate(
        path=path,
        instrument=instrument,
        frame_uid=frame_uid,
        observation_uid=make_observation_uid(scheme, native) if status == "READY" else None,
        identity_scheme=scheme if status == "READY" else None,
        native_identity=native if status == "READY" else None,
        identity_status=status,
        identity_detail=None if status == "READY" else "missing UNIQNUM",
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
        file_size_bytes=123456,
        naxis1=4096,
        naxis2=4096,
        object_name="FIELD_A",
    )
    result = plan_file_science_selection(db_session, [candidate])

    assert result.rows[0]["action"] == ObservationScienceAction.SELECTED.value
    assert result.rows[0]["selected_frame_id"] == frame.frame_id
    assert result.rows[0]["product_frame_ids"] == [frame.frame_id]
