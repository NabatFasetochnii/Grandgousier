from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from grandgousier.db.models import Base
from grandgousier.identity.backfill import build_observation_identity_plan_from_frames
from grandgousier.identity.observation import (
    MASTER_IDENTITY_SCHEME,
    ROBOPHOT_IDENTITY_SCHEME,
    ObservationIdentityError,
    build_frame_observation_identity,
    canonicalize_robophot_date_obs_utc,
)


def _frame(
    frame_id: int,
    *,
    instrument: str,
    header: dict,
    raw_path: str | None = None,
    frame_uid: str | None = None,
    observation_id: int | None = None,
    camera: str | None = None,
    filter_name: str | None = None,
    exptime: float | None = 180.0,
    file_size_bytes: int | None = 100,
    date_obs: datetime | None = None,
    tube: str | None = None,
    site: str | None = "KAO",
    object_name: str | None = "target",
):
    return SimpleNamespace(
        frame_id=frame_id,
        frame_uid=frame_uid or f"legacy-{frame_id}",
        raw_path=raw_path or f"C:/archive/{frame_id}.fits",
        observation_id=observation_id,
        instrument=instrument,
        header_json=header,
        camera=camera,
        filter_name=filter_name,
        exptime=exptime,
        file_size_bytes=file_size_bytes,
        date_obs=date_obs,
        tube=tube,
        site=site,
        object_name=object_name,
    )


def test_robophot_date_obs_without_timezone_is_utc_without_shift() -> None:
    assert canonicalize_robophot_date_obs_utc("2023-11-05T19:43:56.636") == "2023-11-05T19:43:56.636Z"


def test_robophot_date_obs_zero_offset_and_fraction_padding_are_canonicalized() -> None:
    assert canonicalize_robophot_date_obs_utc("2023-11-05 19:43:56.636000+00:00") == "2023-11-05T19:43:56.636Z"


def test_robophot_date_obs_nonzero_offset_is_rejected_in_v1() -> None:
    with pytest.raises(ObservationIdentityError, match="must already be UTC"):
        canonicalize_robophot_date_obs_utc("2023-11-05T19:43:56.636+05:00")


def test_robophot_identity_uses_header_sernum_and_header_utc_time() -> None:
    frame = _frame(
        1,
        instrument="RoboPhot",
        header={"SERNUM": "ML0742515", "DATE-OBS": "2023-11-05T19:43:56.636"},
        camera="something-else",
        date_obs=datetime(2023, 11, 6, 0, 43, 56, 636000, tzinfo=timezone.utc),
        filter_name="i",
    )
    identity = build_frame_observation_identity(frame)
    assert identity.identity_scheme == ROBOPHOT_IDENTITY_SCHEME
    assert identity.native_identity == "ML0742515|2023-11-05T19:43:56.636Z"
    assert identity.camera_identity == "ML0742515"
    assert identity.observed_at == datetime(2023, 11, 5, 19, 43, 56, 636000, tzinfo=timezone.utc)


def test_master_identity_is_uniqnum_only_and_path_independent() -> None:
    date_obs = datetime(2018, 9, 7, 17, 0, tzinfo=timezone.utc)
    left = _frame(
        1,
        instrument="MASTER",
        header={"UNIQNUM": "203_17870", "NAXIS1": 1000, "NAXIS2": 1000},
        raw_path="C:/copy-a/file.fits",
        frame_uid="legacy-a",
        file_size_bytes=100,
        camera="203",
        filter_name="V",
        date_obs=date_obs,
    )
    right = _frame(
        2,
        instrument="MASTER",
        header={"UNIQNUM": "203_17870", "NAXIS1": 1000, "NAXIS2": 1000},
        raw_path="D:/copy-b/file.fits",
        frame_uid="legacy-b",
        file_size_bytes=200,
        camera="203",
        filter_name="V",
        date_obs=date_obs,
    )
    a = build_frame_observation_identity(left)
    b = build_frame_observation_identity(right)
    assert a.identity_scheme == MASTER_IDENTITY_SCHEME
    assert a.native_identity == "203_17870"
    assert a.observation_uid == b.observation_uid


def test_master_same_metadata_different_paths_is_copy_like() -> None:
    date_obs = datetime(2018, 9, 7, 17, 0, tzinfo=timezone.utc)
    frames = [
        _frame(1, instrument="MASTER", header={"UNIQNUM": "x", "NAXIS1": 1000, "NAXIS2": 1000}, camera="203", filter_name="V", date_obs=date_obs, raw_path="C:/a.fits"),
        _frame(2, instrument="MASTER", header={"UNIQNUM": "x", "NAXIS1": 1000, "NAXIS2": 1000}, camera="203", filter_name="V", date_obs=date_obs, raw_path="D:/b.fits"),
    ]
    plan = build_observation_identity_plan_from_frames(frames, instruments=("MASTER",))
    assert plan.copy_like_groups == 1
    assert plan.product_variant_groups == 0
    assert plan.collision_groups == 0
    assert plan.can_execute


def test_master_different_file_size_is_product_variant_not_collision() -> None:
    date_obs = datetime(2018, 9, 7, 17, 0, tzinfo=timezone.utc)
    frames = [
        _frame(1, instrument="MASTER", header={"UNIQNUM": "x", "NAXIS1": 1000, "NAXIS2": 1000}, camera="203", filter_name="V", date_obs=date_obs, file_size_bytes=100),
        _frame(2, instrument="MASTER", header={"UNIQNUM": "x", "NAXIS1": 1000, "NAXIS2": 1000}, camera="203", filter_name="V", date_obs=date_obs, file_size_bytes=200),
    ]
    plan = build_observation_identity_plan_from_frames(frames, instruments=("MASTER",))
    assert plan.product_variant_groups == 1
    assert plan.groups[0].product_variants == ("file_size_bytes",)
    assert plan.collision_groups == 0
    assert plan.can_execute


def test_master_same_uniqnum_different_exposure_is_blocking_collision() -> None:
    date_obs = datetime(2018, 9, 7, 17, 0, tzinfo=timezone.utc)
    frames = [
        _frame(1, instrument="MASTER", header={"UNIQNUM": "x"}, camera="203", filter_name="V", date_obs=date_obs, exptime=60.0),
        _frame(2, instrument="MASTER", header={"UNIQNUM": "x"}, camera="203", filter_name="V", date_obs=date_obs, exptime=120.0),
    ]
    plan = build_observation_identity_plan_from_frames(frames, instruments=("MASTER",))
    assert plan.collision_groups == 1
    assert plan.groups[0].hard_conflicts == ("exptime",)
    assert not plan.can_execute


def test_robophot_two_cameras_same_date_obs_are_distinct_observations() -> None:
    frames = [
        _frame(1, instrument="RoboPhot", header={"SERNUM": "ML0742515", "DATE-OBS": "2026-03-28T21:44:39.568"}, camera="ML0742515", filter_name="i"),
        _frame(2, instrument="RoboPhot", header={"SERNUM": "ML0892515", "DATE-OBS": "2026-03-28T21:44:39.568"}, camera="ML0892515", filter_name="r"),
    ]
    plan = build_observation_identity_plan_from_frames(frames, instruments=("RoboPhot",))
    assert plan.observation_groups == 2
    assert plan.collision_groups == 0
    assert len({group.observation_uid for group in plan.groups}) == 2


def test_missing_native_identity_is_reported_but_does_not_block_other_frames() -> None:
    frames = [
        _frame(1, instrument="MASTER", header={"UNIQNUM": "x"}),
        _frame(2, instrument="MASTER", header={}),
    ]
    plan = build_observation_identity_plan_from_frames(frames, instruments=("MASTER",))
    assert plan.candidate_frames == 1
    assert plan.missing_identity_frames == 1
    assert plan.invalid_identity_frames == 0
    assert plan.can_execute
    assert plan.issues[0].issue_type == "MISSING_IDENTITY"


def test_existing_conflicting_frame_link_blocks_backfill() -> None:
    frame = _frame(1, instrument="MASTER", header={"UNIQNUM": "x"}, observation_id=99)
    plan = build_observation_identity_plan_from_frames([frame], instruments=("MASTER",), observations=())
    assert plan.link_conflict_frames == 1
    assert not plan.can_execute


def test_observation_schema_is_additive_to_frames() -> None:
    assert "observations" in Base.metadata.tables
    observations = Base.metadata.tables["observations"]
    frames = Base.metadata.tables["frames"]
    unique_names = {
        constraint.name
        for constraint in observations.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_observations_observation_uid" in unique_names
    assert "uq_observations_scheme_native_identity" in unique_names
    targets = {
        element.target_fullname
        for constraint in frames.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert "observations.observation_id" in targets
    assert frames.c.observation_id.nullable is True
