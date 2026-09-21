from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from grandgousier.db.models import Frame, Observation
from grandgousier.identity.consistency import existing_identity_conflict, group_consistency
from grandgousier.identity.observation import (
    ObservationIdentity,
    ObservationIdentityError,
    ObservationIdentityMissing,
    build_frame_observation_identity,
)


class ObservationLinkStatus(str, Enum):
    CREATED = "CREATED"
    REUSED = "REUSED"
    ALREADY_LINKED = "ALREADY_LINKED"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    INVALID_IDENTITY = "INVALID_IDENTITY"


class ObservationLinkConflict(RuntimeError):
    """A frame cannot be safely associated with the resolved physical observation."""


@dataclass(frozen=True, slots=True)
class ObservationLinkResult:
    status: ObservationLinkStatus
    observation_id: int | None = None
    observation_uid: str | None = None
    classification: str | None = None
    product_variants: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def linked(self) -> bool:
        return self.observation_id is not None


def validate_same_legacy_frame(existing: Frame, incoming: Frame) -> None:
    """Validate an on-disk file against the already indexed row with the same frame_uid."""

    try:
        existing_identity = build_frame_observation_identity(existing)
        incoming_identity = build_frame_observation_identity(incoming)
    except ObservationIdentityError:
        # Missing/invalid identity is intentionally non-strict for legacy ingest.
        # The persistence helper will leave such a frame unlinked and report the
        # corresponding warning status rather than inventing a fallback key.
        return

    if (
        existing_identity.observation_uid != incoming_identity.observation_uid
        or existing_identity.identity_scheme != incoming_identity.identity_scheme
        or existing_identity.native_identity != incoming_identity.native_identity
    ):
        raise ObservationLinkConflict(
            "legacy frame_uid resolves to different physical observation identity in DB and FITS"
        )

    hard, _soft, _classification = group_consistency([existing, incoming], incoming_identity)
    if hard:
        raise ObservationLinkConflict(
            "legacy frame_uid metadata conflict in: " + ",".join(hard)
        )


def ensure_observation_for_frame(session: Session, frame: Frame) -> ObservationLinkResult:
    """Create/reuse and attach the P4 physical Observation for one Frame.

    Missing/invalid identity metadata are returned as non-linking statuses so old
    ingestible files can remain file-indexed without invented fallback keys.
    Hard identity/link conflicts raise :class:`ObservationLinkConflict`.
    """

    try:
        identity = build_frame_observation_identity(frame)
    except ObservationIdentityMissing as exc:
        return ObservationLinkResult(
            status=ObservationLinkStatus.MISSING_IDENTITY,
            detail=str(exc),
        )
    except ObservationIdentityError as exc:
        return ObservationLinkResult(
            status=ObservationLinkStatus.INVALID_IDENTITY,
            detail=str(exc),
        )

    observation = _resolve_observation(session, identity)
    created = False
    if observation is None:
        observation, created = _create_observation_with_race_recovery(session, identity)

    _validate_observation_row(observation, identity)

    if frame.observation_id is not None and int(frame.observation_id) != int(observation.observation_id):
        raise ObservationLinkConflict(
            f"frame_id={frame.frame_id} is already linked to observation_id={frame.observation_id}, "
            f"expected {observation.observation_id}"
        )

    existing_products = list(
        session.scalars(
            select(Frame)
            .where(Frame.observation_id == observation.observation_id)
            .order_by(Frame.frame_id)
        )
    )
    compatibility_rows = list(existing_products)
    if all(product is not frame for product in compatibility_rows):
        compatibility_rows.append(frame)
    hard, soft, classification = group_consistency(compatibility_rows, identity)
    if hard:
        raise ObservationLinkConflict(
            f"physical observation {identity.native_identity!r} has incompatible metadata in: "
            + ",".join(hard)
        )

    already_linked = frame.observation_id is not None
    frame.observation_id = int(observation.observation_id)
    return ObservationLinkResult(
        status=(
            ObservationLinkStatus.CREATED
            if created
            else ObservationLinkStatus.ALREADY_LINKED
            if already_linked
            else ObservationLinkStatus.REUSED
        ),
        observation_id=int(observation.observation_id),
        observation_uid=identity.observation_uid,
        classification=classification,
        product_variants=soft,
    )


def _resolve_observation(session: Session, identity: ObservationIdentity) -> Observation | None:
    by_uid = session.scalar(
        select(Observation).where(Observation.observation_uid == identity.observation_uid)
    )
    by_pair = session.scalar(
        select(Observation).where(
            Observation.identity_scheme == identity.identity_scheme,
            Observation.native_identity == identity.native_identity,
        )
    )
    if (
        by_uid is not None
        and by_pair is not None
        and int(by_uid.observation_id) != int(by_pair.observation_id)
    ):
        raise ObservationLinkConflict(
            "observation_uid and scheme/native_identity resolve to different rows"
        )
    observation = by_uid or by_pair
    if observation is not None:
        _validate_observation_row(observation, identity)
    return observation


def _validate_observation_row(observation: Observation, identity: ObservationIdentity) -> None:
    conflict = existing_identity_conflict(observation, identity)
    if conflict is not None:
        raise ObservationLinkConflict(conflict)


def _new_observation(identity: ObservationIdentity) -> Observation:
    return Observation(
        observation_uid=identity.observation_uid,
        instrument=identity.instrument,
        identity_scheme=identity.identity_scheme,
        native_identity=identity.native_identity,
        camera_identity=identity.camera_identity,
        observed_at=identity.observed_at,
        identity_components=dict(identity.identity_components),
    )


def _create_observation_with_race_recovery(
    session: Session,
    identity: ObservationIdentity,
) -> tuple[Observation, bool]:
    """Insert an Observation under a SAVEPOINT, recovering only an exact unique race."""

    try:
        with session.begin_nested():
            observation = _new_observation(identity)
            session.add(observation)
            session.flush()
        return observation, True
    except IntegrityError:
        # The SAVEPOINT rollback keeps the outer per-file transaction usable.
        # Treat this as an expected race only if the exact identity now exists
        # and passes both unique-key lookup routes and P4a row validation.
        observation = _resolve_observation(session, identity)
        if observation is None:
            raise
        return observation, False
