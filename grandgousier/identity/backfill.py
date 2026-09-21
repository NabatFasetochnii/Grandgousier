from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from grandgousier.db.models import Frame, Observation
from grandgousier.identity.consistency import existing_identity_conflict, group_consistency
from grandgousier.identity.observation import (
    MASTER_IDENTITY_SCHEME,
    ROBOPHOT_IDENTITY_SCHEME,
    ObservationIdentity,
    ObservationIdentityError,
    ObservationIdentityMissing,
    build_frame_observation_identity,
)

SUPPORTED_IDENTITY_INSTRUMENTS = ("MASTER", "RoboPhot")
_SUPPORTED_SCHEMES = (MASTER_IDENTITY_SCHEME, ROBOPHOT_IDENTITY_SCHEME)


class ObservationIdentitySchemaError(RuntimeError):
    pass


class ObservationIdentityBackfillError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FrameIdentityIssue:
    frame_id: int
    instrument: str
    raw_path: str
    issue_type: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObservationGroupPlan:
    observation_uid: str
    instrument: str
    identity_scheme: str
    native_identity: str
    camera_identity: str | None
    observed_at: datetime | None
    identity_components: dict[str, str]
    frame_ids: tuple[int, ...]
    classification: str
    hard_conflicts: tuple[str, ...]
    product_variants: tuple[str, ...]
    existing_observation_id: int | None
    already_linked_frames: int
    unlinked_frames: int
    link_conflict_frame_ids: tuple[int, ...]
    existing_identity_conflict: str | None

    @property
    def n_frames(self) -> int:
        return len(self.frame_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_uid": self.observation_uid,
            "instrument": self.instrument,
            "identity_scheme": self.identity_scheme,
            "native_identity": self.native_identity,
            "camera_identity": self.camera_identity,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "identity_components": dict(self.identity_components),
            "n_frames": self.n_frames,
            "frame_ids": list(self.frame_ids),
            "classification": self.classification,
            "hard_conflicts": list(self.hard_conflicts),
            "product_variants": list(self.product_variants),
            "existing_observation_id": self.existing_observation_id,
            "already_linked_frames": self.already_linked_frames,
            "unlinked_frames": self.unlinked_frames,
            "link_conflict_frame_ids": list(self.link_conflict_frame_ids),
            "existing_identity_conflict": self.existing_identity_conflict,
        }


@dataclass(frozen=True, slots=True)
class ObservationIdentityPlan:
    instruments: tuple[str, ...]
    frames_scanned: int
    candidate_frames: int
    missing_identity_frames: int
    invalid_identity_frames: int
    observation_groups: int
    single_frame_groups: int
    copy_like_groups: int
    product_variant_groups: int
    collision_groups: int
    existing_observations: int
    already_linked_frames: int
    unlinked_candidate_frames: int
    link_conflict_frames: int
    existing_identity_conflicts: int
    groups: tuple[ObservationGroupPlan, ...]
    issues: tuple[FrameIdentityIssue, ...]

    @property
    def can_execute(self) -> bool:
        return (
            self.invalid_identity_frames == 0
            and self.collision_groups == 0
            and self.link_conflict_frames == 0
            and self.existing_identity_conflicts == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruments": list(self.instruments),
            "frames_scanned": self.frames_scanned,
            "candidate_frames": self.candidate_frames,
            "missing_identity_frames": self.missing_identity_frames,
            "invalid_identity_frames": self.invalid_identity_frames,
            "observation_groups": self.observation_groups,
            "single_frame_groups": self.single_frame_groups,
            "copy_like_groups": self.copy_like_groups,
            "product_variant_groups": self.product_variant_groups,
            "collision_groups": self.collision_groups,
            "existing_observations": self.existing_observations,
            "already_linked_frames": self.already_linked_frames,
            "unlinked_candidate_frames": self.unlinked_candidate_frames,
            "link_conflict_frames": self.link_conflict_frames,
            "existing_identity_conflicts": self.existing_identity_conflicts,
            "can_execute": self.can_execute,
        }


@dataclass(frozen=True, slots=True)
class ObservationIdentityBackfillResult:
    executed: bool
    observations_created: int
    observations_reused: int
    frames_linked: int
    frames_already_linked: int
    frames_left_without_identity: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assert_observation_identity_schema(session: Session) -> None:
    bind = session.get_bind()
    inspector = inspect(bind)
    if "observations" not in inspector.get_table_names():
        raise ObservationIdentitySchemaError(
            "Observation identity schema is not installed. Run `grandgousier migrate-db` first."
        )
    frame_columns = {column["name"] for column in inspector.get_columns("frames")}
    if "observation_id" not in frame_columns:
        raise ObservationIdentitySchemaError(
            "frames.observation_id is missing. Run `grandgousier migrate-db` first."
        )



def build_observation_identity_plan_from_frames(
    frames: Iterable[Frame],
    *,
    instruments: Iterable[str] = SUPPORTED_IDENTITY_INSTRUMENTS,
    observations: Iterable[Observation] = (),
) -> ObservationIdentityPlan:
    """Build a deterministic read-only plan from frame/observation objects.

    Kept independent of SQL execution so identity/collision semantics can be
    regression-tested without a PostgreSQL server.
    """

    selected = tuple(dict.fromkeys(str(item) for item in instruments))
    unsupported = [item for item in selected if item not in SUPPORTED_IDENTITY_INSTRUMENTS]
    if unsupported:
        raise ValueError("Unsupported observation-identity instruments: " + ", ".join(unsupported))
    if not selected:
        raise ValueError("At least one instrument is required")

    frame_rows = [frame for frame in frames if str(frame.instrument) in selected]
    frame_rows.sort(key=lambda frame: int(frame.frame_id))

    issues: list[FrameIdentityIssue] = []
    grouped: dict[str, list[tuple[Frame, ObservationIdentity]]] = defaultdict(list)
    invalid = missing = 0
    for frame in frame_rows:
        try:
            identity = build_frame_observation_identity(frame)
        except ObservationIdentityMissing as exc:
            missing += 1
            issues.append(
                FrameIdentityIssue(
                    frame_id=int(frame.frame_id),
                    instrument=str(frame.instrument),
                    raw_path=str(frame.raw_path),
                    issue_type="MISSING_IDENTITY",
                    detail=str(exc),
                )
            )
            continue
        except ObservationIdentityError as exc:
            invalid += 1
            issues.append(
                FrameIdentityIssue(
                    frame_id=int(frame.frame_id),
                    instrument=str(frame.instrument),
                    raw_path=str(frame.raw_path),
                    issue_type="INVALID_IDENTITY",
                    detail=str(exc),
                )
            )
            continue
        grouped[identity.observation_uid].append((frame, identity))

    existing_rows = list(observations)
    by_uid = {row.observation_uid: row for row in existing_rows}
    by_pair = {(row.identity_scheme, row.native_identity): row for row in existing_rows}
    linked_by_id = {int(row.observation_id): row for row in existing_rows}

    group_plans: list[ObservationGroupPlan] = []
    for observation_uid, rows in sorted(grouped.items(), key=lambda item: min(int(pair[0].frame_id) for pair in item[1])):
        group_frames = [pair[0] for pair in rows]
        identities = [pair[1] for pair in rows]
        identity = identities[0]

        hard, soft, classification = group_consistency(group_frames, identity)
        identity_conflict: str | None = None
        for other in identities[1:]:
            if (
                other.identity_scheme != identity.identity_scheme
                or other.native_identity != identity.native_identity
                or other.instrument != identity.instrument
            ):
                hard = tuple(sorted(set(hard) | {"identity_hash_collision"}))
                classification = "COLLISION"
                break

        existing_uid = by_uid.get(observation_uid)
        existing_pair = by_pair.get((identity.identity_scheme, identity.native_identity))
        existing = existing_uid or existing_pair
        if existing_uid is not None and existing_pair is not None and existing_uid.observation_id != existing_pair.observation_id:
            identity_conflict = "observation_uid and scheme/native_identity resolve to different rows"
        elif existing is not None:
            identity_conflict = existing_identity_conflict(existing, identity)

        already = 0
        unlinked = 0
        link_conflicts: list[int] = []
        expected_existing_id = int(existing.observation_id) if existing is not None else None
        for frame in group_frames:
            if frame.observation_id is None:
                unlinked += 1
                continue
            linked = linked_by_id.get(int(frame.observation_id))
            if linked is not None and linked.observation_uid == observation_uid:
                already += 1
            elif expected_existing_id is not None and int(frame.observation_id) == expected_existing_id:
                already += 1
            else:
                link_conflicts.append(int(frame.frame_id))

        group_plans.append(
            ObservationGroupPlan(
                observation_uid=observation_uid,
                instrument=identity.instrument,
                identity_scheme=identity.identity_scheme,
                native_identity=identity.native_identity,
                camera_identity=identity.camera_identity,
                observed_at=identity.observed_at,
                identity_components=dict(identity.identity_components),
                frame_ids=tuple(int(frame.frame_id) for frame in group_frames),
                classification=classification,
                hard_conflicts=hard,
                product_variants=soft,
                existing_observation_id=expected_existing_id,
                already_linked_frames=already,
                unlinked_frames=unlinked,
                link_conflict_frame_ids=tuple(link_conflicts),
                existing_identity_conflict=identity_conflict,
            )
        )

    counts = defaultdict(int)
    for group in group_plans:
        counts[group.classification] += 1

    return ObservationIdentityPlan(
        instruments=selected,
        frames_scanned=len(frame_rows),
        candidate_frames=sum(group.n_frames for group in group_plans),
        missing_identity_frames=missing,
        invalid_identity_frames=invalid,
        observation_groups=len(group_plans),
        single_frame_groups=counts["SINGLE"],
        copy_like_groups=counts["COPY_LIKE"],
        product_variant_groups=counts["PRODUCT_VARIANT"],
        collision_groups=counts["COLLISION"],
        existing_observations=sum(group.existing_observation_id is not None for group in group_plans),
        already_linked_frames=sum(group.already_linked_frames for group in group_plans),
        unlinked_candidate_frames=sum(group.unlinked_frames for group in group_plans),
        link_conflict_frames=sum(len(group.link_conflict_frame_ids) for group in group_plans),
        existing_identity_conflicts=sum(group.existing_identity_conflict is not None for group in group_plans),
        groups=tuple(group_plans),
        issues=tuple(issues),
    )


def build_observation_identity_plan(
    session: Session,
    *,
    instruments: Iterable[str] = SUPPORTED_IDENTITY_INSTRUMENTS,
) -> ObservationIdentityPlan:
    assert_observation_identity_schema(session)
    selected = tuple(dict.fromkeys(str(item) for item in instruments))
    frames = list(
        session.scalars(
            select(Frame).where(Frame.instrument.in_(selected)).order_by(Frame.frame_id)
        )
    )
    linked_ids = {int(frame.observation_id) for frame in frames if frame.observation_id is not None}
    observations = list(
        session.scalars(select(Observation).where(Observation.identity_scheme.in_(_SUPPORTED_SCHEMES)))
    )
    known_ids = {int(row.observation_id) for row in observations}
    missing_linked_ids = linked_ids - known_ids
    if missing_linked_ids:
        observations.extend(
            session.scalars(
                select(Observation).where(Observation.observation_id.in_(sorted(missing_linked_ids)))
            )
        )
    return build_observation_identity_plan_from_frames(
        frames,
        instruments=selected,
        observations=observations,
    )


def execute_observation_identity_backfill(
    session: Session,
    plan: ObservationIdentityPlan,
) -> ObservationIdentityBackfillResult:
    if not plan.can_execute:
        raise ObservationIdentityBackfillError(
            "Refusing observation-identity backfill because the plan contains collisions, "
            "invalid identities, existing-identity conflicts or conflicting frame links."
        )

    created = reused = linked = already = 0
    for group in plan.groups:
        observation = session.scalar(
            select(Observation).where(Observation.observation_uid == group.observation_uid)
        )
        if observation is None:
            observation = Observation(
                observation_uid=group.observation_uid,
                instrument=group.instrument,
                identity_scheme=group.identity_scheme,
                native_identity=group.native_identity,
                camera_identity=group.camera_identity,
                observed_at=group.observed_at,
                identity_components=dict(group.identity_components),
            )
            session.add(observation)
            session.flush()
            created += 1
        else:
            expected = ObservationIdentity(
                instrument=group.instrument,
                identity_scheme=group.identity_scheme,
                native_identity=group.native_identity,
                observation_uid=group.observation_uid,
                camera_identity=group.camera_identity,
                observed_at=group.observed_at,
                identity_components=dict(group.identity_components),
            )
            conflict = existing_identity_conflict(observation, expected)
            if conflict is not None:
                raise ObservationIdentityBackfillError(conflict)
            reused += 1

        frame_rows = list(
            session.scalars(select(Frame).where(Frame.frame_id.in_(group.frame_ids)).order_by(Frame.frame_id))
        )
        if len(frame_rows) != len(group.frame_ids):
            raise ObservationIdentityBackfillError(
                f"Frame set changed during backfill for {group.observation_uid}; aborting transaction"
            )
        for frame in frame_rows:
            if frame.observation_id is None:
                frame.observation_id = observation.observation_id
                linked += 1
            elif int(frame.observation_id) == int(observation.observation_id):
                already += 1
            else:
                raise ObservationIdentityBackfillError(
                    f"frame_id={frame.frame_id} is already linked to observation_id={frame.observation_id}, "
                    f"expected {observation.observation_id}"
                )

    return ObservationIdentityBackfillResult(
        executed=True,
        observations_created=created,
        observations_reused=reused,
        frames_linked=linked,
        frames_already_linked=already,
        frames_left_without_identity=plan.missing_identity_frames,
    )


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_observation_identity_report(
    plan: ObservationIdentityPlan,
    *,
    out_root: str | Path,
    result: ObservationIdentityBackfillResult | None = None,
) -> Path:
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "plan": plan.to_dict(),
        "result": result.to_dict() if result is not None else {"executed": False},
    }
    (root / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    group_rows: list[dict[str, Any]] = []
    for group in plan.groups:
        row = group.to_dict()
        row["identity_components"] = json.dumps(row["identity_components"], ensure_ascii=False, sort_keys=True)
        row["frame_ids"] = ";".join(str(value) for value in group.frame_ids)
        row["hard_conflicts"] = ";".join(group.hard_conflicts)
        row["product_variants"] = ";".join(group.product_variants)
        row["link_conflict_frame_ids"] = ";".join(str(value) for value in group.link_conflict_frame_ids)
        group_rows.append(row)
    _write_csv(
        root / "observation_groups.csv",
        group_rows,
        [
            "observation_uid",
            "instrument",
            "identity_scheme",
            "native_identity",
            "camera_identity",
            "observed_at",
            "identity_components",
            "n_frames",
            "frame_ids",
            "classification",
            "hard_conflicts",
            "product_variants",
            "existing_observation_id",
            "already_linked_frames",
            "unlinked_frames",
            "link_conflict_frame_ids",
            "existing_identity_conflict",
        ],
    )
    _write_csv(
        root / "frame_identity_issues.csv",
        (issue.to_dict() for issue in plan.issues),
        ["frame_id", "instrument", "raw_path", "issue_type", "detail"],
    )
    return root
