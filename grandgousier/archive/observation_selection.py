from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import FieldSeriesFrame, Frame, Observation
from grandgousier.identity.observation import MASTER_IDENTITY_SCHEME
from grandgousier.io.parsing import parse_datetime


class ObservationScienceAction(str, Enum):
    """Decision for one file/frame at the Stage-2 membership boundary."""

    SELECTED = "SELECTED"
    SELECTED_LEGACY_NO_OBSERVATION_ID = "SELECTED_LEGACY_NO_OBSERVATION_ID"
    SELECTED_EXPLICIT_REASSIGN = "SELECTED_EXPLICIT_REASSIGN"
    ALREADY_REPRESENTED = "ALREADY_REPRESENTED"
    EXISTING_MULTIPLE_REPRESENTATIVES = "EXISTING_MULTIPLE_REPRESENTATIVES"
    AMBIGUOUS_MULTIPLE_PRODUCTS = "AMBIGUOUS_MULTIPLE_PRODUCTS"
    PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM = (
        "PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM"
    )
    AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM = (
        "AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM"
    )
    AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM = (
        "AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM"
    )
    MISSING_OBSERVATION_ID = "MISSING_OBSERVATION_ID"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"


@dataclass(frozen=True, slots=True)
class ScienceFileIdentityCandidate:
    """Read-only identity description for an input file before DB indexing."""

    path: str
    instrument: str | None
    frame_uid: str | None
    observation_uid: str | None
    identity_scheme: str | None
    native_identity: str | None
    date_obs: Any | None = None
    file_size_bytes: int | None = None
    naxis1: int | None = None
    naxis2: int | None = None
    object_name: str | None = None
    identity_status: str = "READY"
    identity_detail: str | None = None
    camera: str | None = None
    raw_relative_physical_key: str | None = None


_MASTER_RAW_RELATIVE_KEY_RE = re.compile(
    r"^rawdata/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<raw_id>\d+)\.(?:fit|fits|fts)(?:\.gz)?$"
)


def _master_date_obs_equal_by_calendar_second(
    first: datetime | None, second: datetime | None
) -> bool:
    """Compare MASTER DATE-OBS timestamps at whole-second precision."""

    return (
        first is not None
        and second is not None
        and first.replace(microsecond=0) == second.replace(microsecond=0)
    )


def _validate_master_raw_relative_physical_key(value: object) -> str:
    """Validate the canonical relative source key carried by physical MASTER provenance."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("MASTER raw physical key must be an exact canonical POSIX relative path")
    match = _MASTER_RAW_RELATIVE_KEY_RE.fullmatch(value)
    path = PurePosixPath(value)
    if match is None or path.is_absolute() or path.as_posix() != value:
        raise ValueError("invalid canonical MASTER raw-relative physical key")
    try:
        date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError as exc:
        raise ValueError("MASTER raw physical key contains an invalid archive date") from exc
    return value


def _master_raw_relative_key_from_ggraw(value: object) -> str | None:
    """Return a validated relative key only for the canonical MASTER GGRAW root."""

    prefix = "/master/imdata/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        return _validate_master_raw_relative_physical_key(value[len(prefix) :])
    except ValueError:
        return None


def _existing_master_frame_source_key(frame: Frame) -> tuple[str | None, str | None]:
    """Prefer structured MASTER provenance, checking it against any usable legacy GGRAW."""

    header = frame.header_json or {}
    structured_value = header.get("raw_relative_physical_key")
    structured_present = structured_value not in (None, "")
    structured_key = None
    if structured_present:
        try:
            structured_key = _validate_master_raw_relative_physical_key(structured_value)
        except ValueError:
            return None, "INVALID_STRUCTURED_SOURCE_CONTEXT"

    ggraw_key = _master_raw_relative_key_from_ggraw(header.get("GGRAW"))
    if structured_key is not None:
        if ggraw_key is not None and ggraw_key != structured_key:
            return None, "STRUCTURED_SOURCE_VS_GGRAW_CONFLICT"
        return structured_key, None
    return ggraw_key, None


def _camera_token(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _instrument_token(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text.casefold() or None


@dataclass(slots=True)
class ObservationScienceSelectionResult:
    selected_frame_ids: list[int] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def instrument_requires_observation_identity(instrument: str | None) -> bool:
    if instrument is None:
        return False
    normalized = str(instrument).strip()
    if not normalized:
        return False
    return normalized == "MASTER" or normalized.lower() == "robophot"


def select_persisted_science_frames(
    session: Session,
    frame_ids: Iterable[int],
    *,
    allow_reassign_existing_frame: bool = False,
    lock_observations: bool = False,
) -> ObservationScienceSelectionResult:
    """Select persisted Frames that may enter new ``field_series`` membership.

    The rule is deliberately conservative. One physical Observation may have
    multiple file products in ``frames``, but at most one product may be admitted
    automatically to science-series membership. Existing membership wins. When
    an unrepresented Observation already has multiple Frame products, no arbitrary
    representative is selected.

    This function does not mutate the database.
    """

    candidate_ids = sorted({int(value) for value in frame_ids})
    if not candidate_ids:
        return _finalize([], [])

    candidates = list(
        session.scalars(
            select(Frame).where(Frame.frame_id.in_(candidate_ids)).order_by(Frame.frame_id)
        )
    )
    found_ids = {int(frame.frame_id) for frame in candidates}
    missing_ids = sorted(set(candidate_ids) - found_ids)
    if missing_ids:
        raise ValueError(f"frame_ids not found for observation science selection: {missing_ids[:20]}")

    observation_ids = sorted(
        {int(frame.observation_id) for frame in candidates if frame.observation_id is not None}
    )
    products_by_observation: dict[int, list[Frame]] = {}
    observations_by_id: dict[int, Observation] = {}
    series_by_frame: dict[int, list[int]] = {}

    if observation_ids:
        observation_stmt = (
            select(Observation)
            .where(Observation.observation_id.in_(observation_ids))
            .order_by(Observation.observation_id)
        )
        if lock_observations:
            observation_stmt = observation_stmt.with_for_update()
        observations_by_id = {
            int(observation.observation_id): observation
            for observation in session.scalars(observation_stmt)
        }
        all_products = list(
            session.scalars(
                select(Frame)
                .where(Frame.observation_id.in_(observation_ids))
                .order_by(Frame.observation_id, Frame.frame_id)
            )
        )
        product_ids = [int(frame.frame_id) for frame in all_products]
        for frame in all_products:
            products_by_observation.setdefault(int(frame.observation_id), []).append(frame)

        if product_ids:
            memberships = list(
                session.execute(
                    select(FieldSeriesFrame.frame_id, FieldSeriesFrame.series_id)
                    .where(FieldSeriesFrame.frame_id.in_(product_ids))
                    .order_by(FieldSeriesFrame.frame_id, FieldSeriesFrame.series_id)
                ).all()
            )
            for frame_id, series_id in memberships:
                series_by_frame.setdefault(int(frame_id), []).append(int(series_id))

    rows: list[dict] = []
    selected_ids: list[int] = []

    for frame in candidates:
        frame_id = int(frame.frame_id)
        observation_id = int(frame.observation_id) if frame.observation_id is not None else None

        if observation_id is None:
            if instrument_requires_observation_identity(frame.instrument):
                action = ObservationScienceAction.MISSING_OBSERVATION_ID
                reason = (
                    "supported instrument requires a P4 physical Observation before "
                    "automatic science-series membership"
                )
            else:
                action = ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID
                reason = "legacy instrument without a P4 identity scheme keeps historical selection behavior"
                selected_ids.append(frame_id)
            rows.append(
                _persisted_row(
                    frame=frame,
                    action=action,
                    reason=reason,
                    product_frame_ids=[frame_id],
                    existing_series_ids=[],
                    selected_frame_id=frame_id if action == ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID else None,
                )
            )
            continue

        observation = observations_by_id.get(observation_id)
        products = products_by_observation.get(observation_id, [frame])
        product_ids = [int(product.frame_id) for product in products]
        existing_series_ids = sorted(
            {
                series_id
                for product_id in product_ids
                for series_id in series_by_frame.get(product_id, [])
            }
        )
        assigned_product_ids = sorted(
            product_id for product_id in product_ids if series_by_frame.get(product_id)
        )

        date_action, date_reason = _persisted_master_uniqnum_date_issue(
            observation.identity_scheme if observation is not None else None,
            products,
        )
        if date_action is not None:
            rows.append(
                _persisted_row(
                    frame=frame,
                    action=date_action,
                    reason=date_reason,
                    product_frame_ids=product_ids,
                    existing_series_ids=existing_series_ids,
                    selected_frame_id=None,
                    observation=observation,
                )
            )
            continue

        if len(assigned_product_ids) > 1:
            action = ObservationScienceAction.EXISTING_MULTIPLE_REPRESENTATIVES
            reason = (
                "physical Observation already has multiple Frame products in field_series_frames; "
                "historical double representation requires explicit audit"
            )
            selected_frame_id = None
        elif existing_series_ids:
            assigned_frame_id = assigned_product_ids[0]
            if allow_reassign_existing_frame and frame_id == assigned_frame_id:
                action = ObservationScienceAction.SELECTED_EXPLICIT_REASSIGN
                reason = (
                    "explicit include-assigned mode permits reassignment of the same existing "
                    "Frame product; alternate products remain blocked"
                )
                selected_frame_id = frame_id
                selected_ids.append(frame_id)
            else:
                action = ObservationScienceAction.ALREADY_REPRESENTED
                reason = (
                    "physical Observation already has a Frame product in field_series_frames; "
                    "existing science representative is retained"
                )
                selected_frame_id = assigned_frame_id
        elif len(product_ids) > 1:
            action = ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS
            reason = (
                "physical Observation has multiple unassigned Frame products; "
                "automatic representative selection is intentionally disabled"
            )
            selected_frame_id = None
        else:
            action = ObservationScienceAction.SELECTED
            reason = "single unrepresented Frame product for this physical Observation"
            selected_frame_id = frame_id
            selected_ids.append(frame_id)

        rows.append(
            _persisted_row(
                frame=frame,
                action=action,
                reason=reason,
                product_frame_ids=product_ids,
                existing_series_ids=existing_series_ids,
                selected_frame_id=selected_frame_id,
                observation=observation,
            )
        )

    return _finalize(selected_ids, rows)


def plan_file_science_selection(
    session: Session,
    candidates: Iterable[ScienceFileIdentityCandidate],
    *,
    allow_reassign_existing_frame: bool = False,
) -> ObservationScienceSelectionResult:
    """Plan observation-aware input selection for files before indexing.

    The planner mirrors the persisted selector without inserting Frames. Existing
    legacy ``frame_uid`` values are used to distinguish an exact already-indexed
    file from a new copied/processed file product.
    """

    items = list(candidates)
    if not items:
        return _finalize([], [])

    valid_items: list[ScienceFileIdentityCandidate] = []
    rows: list[dict] = []

    for item in items:
        if item.identity_status == "READY" and item.observation_uid and item.identity_scheme and item.native_identity:
            valid_items.append(item)
            continue
        if instrument_requires_observation_identity(item.instrument):
            rows.append(
                _plan_row(
                    item,
                    action=ObservationScienceAction.MISSING_OBSERVATION_ID,
                    reason=(
                        item.identity_detail
                        or "supported instrument has no usable P4 physical observation identity"
                    ),
                )
            )
        else:
            rows.append(
                _plan_row(
                    item,
                    action=ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID,
                    reason="legacy instrument without a P4 identity scheme keeps historical selection behavior",
                    selected_frame_uid=item.frame_uid,
                )
            )

    if not valid_items:
        return _finalize_plan(rows)

    # Group input files by exact physical identity. A conflicting UID vs
    # scheme/native lookup in the DB is a blocking integrity state.
    groups: dict[tuple[str, str, str], list[ScienceFileIdentityCandidate]] = {}
    for item in valid_items:
        key = (str(item.observation_uid), str(item.identity_scheme), str(item.native_identity))
        groups.setdefault(key, []).append(item)

    for (observation_uid, identity_scheme, native_identity), group in sorted(groups.items()):
        context_action, context_reason = _candidate_master_uniqnum_context_issue(
            identity_scheme, group
        )
        if context_action is not None:
            rows.extend(
                _plan_row(item, action=context_action, reason=context_reason)
                for item in group
            )
            continue

        by_uid = session.scalar(
            select(Observation).where(Observation.observation_uid == observation_uid)
        )
        by_pair = session.scalar(
            select(Observation).where(
                Observation.identity_scheme == identity_scheme,
                Observation.native_identity == native_identity,
            )
        )

        identity_conflict_reason = None
        if by_uid is not None and (
            str(by_uid.identity_scheme) != identity_scheme
            or str(by_uid.native_identity) != native_identity
        ):
            identity_conflict_reason = (
                "observation_uid resolves to a row with different identity_scheme/native_identity"
            )
        if by_pair is not None and str(by_pair.observation_uid) != observation_uid:
            identity_conflict_reason = (
                "(identity_scheme,native_identity) resolves to a row with different observation_uid"
            )
        if (
            by_uid is not None
            and by_pair is not None
            and int(by_uid.observation_id) != int(by_pair.observation_id)
        ):
            identity_conflict_reason = (
                "observation_uid and (identity_scheme,native_identity) resolve to different rows"
            )

        if identity_conflict_reason is not None:
            for item in group:
                rows.append(
                    _plan_row(
                        item,
                        action=ObservationScienceAction.IDENTITY_CONFLICT,
                        reason=identity_conflict_reason,
                    )
                )
            continue

        observation = by_uid or by_pair
        existing_products: list[Frame] = []
        existing_series_ids: list[int] = []
        assigned_product_ids: list[int] = []
        existing_frame_uids: set[str] = set()
        observation_id: int | None = None

        if observation is not None:
            observation_id = int(observation.observation_id)
            existing_products = list(
                session.scalars(
                    select(Frame)
                    .where(Frame.observation_id == observation_id)
                    .order_by(Frame.frame_id)
                )
            )

        # Legacy Frames may predate Observation links but still carry the
        # authoritative MASTER UNIQNUM in their FITS header JSON. Search only
        # this exact physical identity and only for physical MASTER candidates.
        if identity_scheme == MASTER_IDENTITY_SCHEME and any(
            item.raw_relative_physical_key not in (None, "") for item in group
        ):
            unlinked_products = list(
                session.scalars(
                    select(Frame)
                    .where(
                        Frame.observation_id.is_(None),
                        Frame.instrument == "MASTER",
                        Frame.header_json["UNIQNUM"].as_string() == native_identity,
                    )
                    .order_by(Frame.frame_id)
                )
            )
            known_product_ids = {int(frame.frame_id) for frame in existing_products}
            existing_products.extend(
                frame
                for frame in unlinked_products
                if int(frame.frame_id) not in known_product_ids
            )

        product_ids = [int(frame.frame_id) for frame in existing_products]
        existing_frame_uids = {
            str(frame.frame_uid) for frame in existing_products if frame.frame_uid not in (None, "")
        }
        if product_ids:
            memberships = list(
                session.execute(
                    select(FieldSeriesFrame.frame_id, FieldSeriesFrame.series_id)
                    .where(FieldSeriesFrame.frame_id.in_(product_ids))
                    .order_by(FieldSeriesFrame.frame_id, FieldSeriesFrame.series_id)
                ).all()
            )
            existing_series_ids = sorted({int(series_id) for _frame_id, series_id in memberships})
            assigned_product_ids = sorted({int(frame_id) for frame_id, _series_id in memberships})

        context_action, context_reason = _existing_master_uniqnum_context_issue(
            identity_scheme, group, existing_products, observation=observation
        )
        if context_action is not None:
            rows.extend(
                _plan_row(
                    item,
                    action=context_action,
                    reason=context_reason,
                    observation_id=observation_id,
                    product_frame_ids=[int(frame.frame_id) for frame in existing_products],
                )
                for item in group
            )
            continue

        exact_physical_duplicate = _existing_master_physical_duplicate(
            identity_scheme, group, existing_products, observation=observation
        )
        copy_like_items = {
            id(item)
            for item in group
            if any(_candidate_is_copy_like_of_frame(item, frame) for frame in existing_products)
        }
        new_frame_uids = {
            str(item.frame_uid)
            for item in group
            if id(item) not in copy_like_items
            and item.frame_uid not in (None, "")
            and str(item.frame_uid) not in existing_frame_uids
        }
        # If frame_uid is unavailable, each unmatched input item must conservatively
        # count as a new product.  A path-relocated exact copy is recognized by the
        # same non-locator product metadata and does not inflate projected_products.
        unknown_uid_products = sum(
            1 for item in group
            if id(item) not in copy_like_items and item.frame_uid in (None, "")
        )
        projected_products = len(existing_products) + len(new_frame_uids) + unknown_uid_products

        if exact_physical_duplicate:
            action = ObservationScienceAction.ALREADY_REPRESENTED
            reason = (
                "this exact MASTER physical observation is already durably indexed; "
                "the planner classifies it as already represented without selecting a new product"
            )
            selected_frame_id = int(existing_products[0].frame_id)
            selected_uid = str(existing_products[0].frame_uid)
        elif len(assigned_product_ids) > 1:
            action = ObservationScienceAction.EXISTING_MULTIPLE_REPRESENTATIVES
            reason = (
                "physical Observation already has multiple Frame products in field_series_frames; "
                "historical double representation requires explicit audit"
            )
            selected_frame_id = None
            selected_uid = None
        elif existing_series_ids:
            action = ObservationScienceAction.ALREADY_REPRESENTED
            reason = (
                "physical Observation is already represented in field_series_frames; "
                "input file will not create another science epoch"
            )
            selected_frame_id = assigned_product_ids[0]
            selected_uid = None
        elif projected_products > 1:
            action = ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS
            reason = (
                "physical Observation would have multiple unassigned file products; "
                "no automatic representative is selected"
            )
            selected_frame_id = None
            selected_uid = None
        else:
            action = ObservationScienceAction.SELECTED
            reason = "single unrepresented physical-observation product in projected ingest state"
            selected_frame_id = int(existing_products[0].frame_id) if existing_products else None
            selected_uid = group[0].frame_uid if len(group) == 1 else None

        product_frame_ids = [int(frame.frame_id) for frame in existing_products]
        assigned_frame_uid = None
        if len(assigned_product_ids) == 1:
            assigned_frame = next(
                (frame for frame in existing_products if int(frame.frame_id) == assigned_product_ids[0]),
                None,
            )
            if assigned_frame is not None:
                assigned_frame_uid = str(assigned_frame.frame_uid)

        for item in group:
            item_action = action
            item_reason = reason
            item_selected_frame_id = selected_frame_id
            item_selected_uid = selected_uid
            if (
                not exact_physical_duplicate
                and action == ObservationScienceAction.ALREADY_REPRESENTED
                and allow_reassign_existing_frame
                and assigned_frame_uid is not None
                and item.frame_uid is not None
                and str(item.frame_uid) == assigned_frame_uid
            ):
                item_action = ObservationScienceAction.SELECTED_EXPLICIT_REASSIGN
                item_reason = (
                    "explicit include-assigned mode permits reassignment of the same existing "
                    "Frame product; alternate products remain blocked"
                )
                item_selected_frame_id = assigned_product_ids[0]
                item_selected_uid = assigned_frame_uid
            rows.append(
                _plan_row(
                    item,
                    action=item_action,
                    reason=item_reason,
                    observation_id=observation_id,
                    product_frame_ids=product_frame_ids,
                    existing_series_ids=existing_series_ids,
                    selected_frame_id=item_selected_frame_id,
                    selected_frame_uid=item_selected_uid,
                )
            )

    return _finalize_plan(rows)


def _candidate_is_copy_like_of_frame(item: ScienceFileIdentityCandidate, frame: Frame) -> bool:
    """Return True only when all available non-locator product metadata agree."""

    comparisons = [
        (item.file_size_bytes, frame.file_size_bytes),
        (item.naxis1, (frame.header_json or {}).get("NAXIS1")),
        (item.naxis2, (frame.header_json or {}).get("NAXIS2")),
        (item.object_name, frame.object_name),
    ]
    compared = False
    for incoming, existing in comparisons:
        if incoming in (None, "") or existing in (None, ""):
            continue
        compared = True
        if str(incoming).strip() != str(existing).strip():
            return False
    return compared


def _existing_master_physical_duplicate(
    identity_scheme: str,
    group: list[ScienceFileIdentityCandidate],
    existing_products: list[Frame],
    *,
    observation: Observation | None,
) -> bool:
    """Recognize exact durable MASTER physical context even without series membership."""

    if identity_scheme != MASTER_IDENTITY_SCHEME or len(existing_products) != 1 or not group:
        return False

    candidate_contexts: set[tuple[datetime, str, str]] = set()
    for item in group:
        date_obs = parse_datetime(item.date_obs)
        camera = _camera_token(item.camera)
        try:
            source_key = _validate_master_raw_relative_physical_key(
                item.raw_relative_physical_key
            )
        except ValueError:
            return False
        if date_obs is None or camera is None:
            return False
        candidate_contexts.add((date_obs.replace(microsecond=0), camera, source_key))
    if len(candidate_contexts) != 1:
        return False

    frame = existing_products[0]
    existing_date = parse_datetime(frame.date_obs)
    existing_camera = _camera_token(frame.camera)
    if existing_camera is None and observation is not None:
        existing_camera = _camera_token(observation.camera_identity)
    existing_key, source_issue = _existing_master_frame_source_key(frame)
    if source_issue is not None or existing_key is None:
        return False
    candidate_date, candidate_camera, candidate_key = next(iter(candidate_contexts))
    return (
        _master_date_obs_equal_by_calendar_second(existing_date, candidate_date)
        and existing_camera == candidate_camera
        and existing_key == candidate_key
    )


def _candidate_master_uniqnum_context_issue(
    identity_scheme: str,
    group: list[ScienceFileIdentityCandidate],
) -> tuple[ObservationScienceAction | None, str | None]:
    if identity_scheme != MASTER_IDENTITY_SCHEME:
        return None, None
    has_physical_source = any(
        item.raw_relative_physical_key not in (None, "") for item in group
    )
    if has_physical_source:
        candidate_instruments = {_instrument_token(item.instrument) for item in group}
        if candidate_instruments != {"master"}:
            return (
                ObservationScienceAction.IDENTITY_CONFLICT,
                "physical MASTER identity is associated with a non-MASTER or missing candidate instrument",
            )
    dates = [parse_datetime(item.date_obs) for item in group]
    known_dates = sorted({value for value in dates if value is not None})
    if any(
        not _master_date_obs_equal_by_calendar_second(first, second)
        for index, first in enumerate(known_dates)
        for second in known_dates[index + 1 :]
    ):
        rendered = ", ".join(value.isoformat() for value in known_dates)
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "master_uniqnum_v1 candidates share UNIQNUM but have distinct known DATE-OBS values: "
            + rendered,
        )
    if any(value is None for value in dates):
        return (
            ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM,
            "master_uniqnum_v1 candidate DATE-OBS is missing or unparseable; a safe represented/no-op decision is unavailable",
        )

    cameras = [_camera_token(item.camera) for item in group]
    known_cameras = sorted({value for value in cameras if value is not None})
    if len(known_cameras) > 1:
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "master_uniqnum_v1 candidates share UNIQNUM but have distinct known cameras: "
            + ", ".join(known_cameras),
        )

    keys: list[str | None] = []
    for item in group:
        if item.raw_relative_physical_key in (None, ""):
            keys.append(None)
            continue
        try:
            keys.append(_validate_master_raw_relative_physical_key(item.raw_relative_physical_key))
        except ValueError as exc:
            return (
                ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
                f"candidate MASTER raw physical key is invalid: {exc}",
            )
    known_keys = sorted({value for value in keys if value is not None})
    if len(known_keys) > 1:
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "master_uniqnum_v1 candidates share UNIQNUM but have distinct known raw physical keys",
        )
    if known_keys and (any(value is None for value in keys) or any(value is None for value in cameras)):
        return (
            ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
            "physical MASTER candidate group lacks a complete camera/raw-key context",
        )
    return None, None


def _existing_master_uniqnum_context_issue(
    identity_scheme: str,
    group: list[ScienceFileIdentityCandidate],
    existing_products: list[Frame],
    *,
    observation: Observation | None = None,
) -> tuple[ObservationScienceAction | None, str | None]:
    if identity_scheme != MASTER_IDENTITY_SCHEME:
        return None, None
    if not existing_products and observation is None:
        return None, None
    has_physical_source = any(
        item.raw_relative_physical_key not in (None, "") for item in group
    )
    existing_keys: set[str] = set()
    if has_physical_source:
        candidate_instruments = {_instrument_token(item.instrument) for item in group}
        persisted_instruments = [
            *(
                [_instrument_token(observation.instrument)]
                if observation is not None
                else []
            ),
            *(_instrument_token(frame.instrument) for frame in existing_products),
        ]
        if (
            candidate_instruments != {"master"}
            or not persisted_instruments
            or any(value != "master" for value in persisted_instruments)
        ):
            return (
                ObservationScienceAction.IDENTITY_CONFLICT,
                "physical MASTER identity conflicts with persisted Observation/Frame instrument",
            )
        candidate_native_ids = {str(item.native_identity) for item in group}
        if len(candidate_native_ids) == 1:
            candidate_native_id = next(iter(candidate_native_ids))
            for frame in existing_products:
                header_uniqnum = (frame.header_json or {}).get("UNIQNUM")
                if header_uniqnum not in (None, "") and str(header_uniqnum) != candidate_native_id:
                    return (
                        ObservationScienceAction.IDENTITY_CONFLICT,
                        "linked MASTER Frame header UNIQNUM conflicts with the canonical Observation identity",
                    )
        for frame in existing_products:
            existing_key, source_issue = _existing_master_frame_source_key(frame)
            if source_issue == "STRUCTURED_SOURCE_VS_GGRAW_CONFLICT":
                return (
                    ObservationScienceAction.IDENTITY_CONFLICT,
                    "STRUCTURED_SOURCE_VS_GGRAW_CONFLICT",
                )
            if source_issue is not None:
                return (
                    ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
                    "persisted Frame has an invalid raw_relative_physical_key",
                )
            if existing_key is not None:
                existing_keys.add(existing_key)
    candidate_date = parse_datetime(group[0].date_obs)
    existing_dates = [parse_datetime(frame.date_obs) for frame in existing_products]
    observation_date = parse_datetime(observation.observed_at) if observation is not None else None
    known_dates = [value for value in existing_dates if value is not None]
    if observation_date is not None:
        known_dates.append(observation_date)
    if any(
        not _master_date_obs_equal_by_calendar_second(value, candidate_date)
        for value in known_dates
    ):
        rendered = ", ".join(
            sorted({value.isoformat() for value in known_dates if value is not None})
        )
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "legacy UNIQNUM physical-source/date collision: candidate DATE-OBS conflicts with "
            "existing Frame.date_obs for the same master_uniqnum_v1 identity: "
            + rendered,
        )
    candidate_cameras = {_camera_token(item.camera) for item in group}
    candidate_cameras.discard(None)
    existing_cameras = {
        value
        for value in (
            *(_camera_token(frame.camera) for frame in existing_products),
            _camera_token(observation.camera_identity) if observation is not None else None,
        )
        if value is not None
    }
    if candidate_cameras and any(value not in candidate_cameras for value in existing_cameras):
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "candidate camera conflicts with persisted MASTER observation/frame camera context",
        )

    candidate_keys: set[str] = set()
    for item in group:
        if item.raw_relative_physical_key in (None, ""):
            continue
        try:
            candidate_keys.add(_validate_master_raw_relative_physical_key(item.raw_relative_physical_key))
        except ValueError as exc:
            return (
                ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
                f"candidate MASTER raw physical key is invalid: {exc}",
            )
    if candidate_keys:
        if existing_keys and not existing_keys.issubset(candidate_keys):
            return (
                ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
                "legacy UNIQNUM physical-source/date collision: candidate raw physical source key "
                "conflicts with persisted Frame source context",
            )
        if not existing_keys:
            return (
                ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
                "existing MASTER Frame/Observation has no durable raw physical source context; exact duplicate cannot be established",
            )
        if candidate_cameras and not existing_cameras:
            return (
                ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM,
                "existing MASTER Frame/Observation has no durable camera context; exact duplicate cannot be established",
            )
    elif candidate_cameras and any(value not in candidate_cameras for value in existing_cameras):
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "candidate camera conflicts with persisted MASTER observation/frame camera context",
        )
    if any(value is None for value in existing_dates):
        return (
            ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM,
            "an existing Frame with the same master_uniqnum_v1 identity has missing or unparseable date_obs",
        )
    return None, None


def _persisted_master_uniqnum_date_issue(
    identity_scheme: str | None,
    products: list[Frame],
) -> tuple[ObservationScienceAction | None, str | None]:
    if identity_scheme != MASTER_IDENTITY_SCHEME or not products:
        return None, None
    dates = [parse_datetime(frame.date_obs) for frame in products]
    known_dates = sorted({value for value in dates if value is not None})
    if any(value is None for value in dates):
        return (
            ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM,
            "a persisted Frame with this master_uniqnum_v1 identity has missing or unparseable date_obs",
        )
    if any(
        not _master_date_obs_equal_by_calendar_second(first, second)
        for index, first in enumerate(known_dates)
        for second in known_dates[index + 1 :]
    ):
        rendered = ", ".join(value.isoformat() for value in known_dates)
        return (
            ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM,
            "persisted Frames with this master_uniqnum_v1 identity have distinct DATE-OBS values: "
            + rendered,
        )
    return None, None


def _persisted_row(
    *,
    frame: Frame,
    action: ObservationScienceAction,
    reason: str,
    product_frame_ids: list[int],
    existing_series_ids: list[int],
    selected_frame_id: int | None,
    observation: Observation | None = None,
) -> dict:
    return {
        "frame_id": int(frame.frame_id),
        "frame_uid": frame.frame_uid,
        "raw_path": frame.raw_path,
        "instrument": frame.instrument,
        "observation_id": int(frame.observation_id) if frame.observation_id is not None else None,
        "observation_uid": observation.observation_uid if observation is not None else None,
        "identity_scheme": observation.identity_scheme if observation is not None else None,
        "native_identity": observation.native_identity if observation is not None else None,
        "identity_status": "READY" if observation is not None else None,
        "action": action.value,
        "selected_frame_id": selected_frame_id,
        "product_frame_ids": list(product_frame_ids),
        "existing_series_ids": list(existing_series_ids),
        "reason": reason,
    }


def _plan_row(
    item: ScienceFileIdentityCandidate,
    *,
    action: ObservationScienceAction,
    reason: str,
    observation_id: int | None = None,
    product_frame_ids: list[int] | None = None,
    existing_series_ids: list[int] | None = None,
    selected_frame_id: int | None = None,
    selected_frame_uid: str | None = None,
) -> dict:
    return {
        "frame_id": None,
        "frame_uid": item.frame_uid,
        "raw_path": item.path,
        "instrument": item.instrument,
        "observation_id": observation_id,
        "observation_uid": item.observation_uid,
        "identity_scheme": item.identity_scheme,
        "native_identity": item.native_identity,
        "identity_status": item.identity_status,
        "camera": item.camera,
        "raw_relative_physical_key": item.raw_relative_physical_key,
        "date_obs": (
            item.date_obs.isoformat()
            if hasattr(item.date_obs, "isoformat")
            else item.date_obs
        ),
        "action": action.value,
        "selected_frame_id": selected_frame_id,
        "selected_frame_uid": selected_frame_uid,
        "product_frame_ids": list(product_frame_ids or []),
        "existing_series_ids": list(existing_series_ids or []),
        "reason": reason,
    }


def _finalize(selected_frame_ids: list[int], rows: list[dict]) -> ObservationScienceSelectionResult:
    return ObservationScienceSelectionResult(
        selected_frame_ids=sorted({int(value) for value in selected_frame_ids}),
        rows=rows,
        summary=_summary(rows, selected_frame_count=len(set(selected_frame_ids))),
    )


def _finalize_plan(rows: list[dict]) -> ObservationScienceSelectionResult:
    selected_rows = {
        str(row.get("frame_uid") or row.get("raw_path"))
        for row in rows
        if row.get("action")
        in {
            ObservationScienceAction.SELECTED.value,
            ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID.value,
            ObservationScienceAction.SELECTED_EXPLICIT_REASSIGN.value,
        }
    }
    return ObservationScienceSelectionResult(
        selected_frame_ids=[],
        rows=rows,
        summary=_summary(rows, selected_frame_count=len(selected_rows)),
    )


def _summary(rows: list[dict], *, selected_frame_count: int) -> dict:
    counts = {action.value: 0 for action in ObservationScienceAction}
    observation_ids: set[int] = set()
    physical_keys: set[tuple[str, str]] = set()
    for row in rows:
        action = str(row.get("action") or "")
        if action in counts:
            counts[action] += 1
        if row.get("observation_id") is not None:
            observation_ids.add(int(row["observation_id"]))
        scheme = row.get("identity_scheme")
        native = row.get("native_identity")
        if scheme and native:
            physical_keys.add((str(scheme), str(native)))

    blocked_actions = {
        ObservationScienceAction.ALREADY_REPRESENTED.value,
        ObservationScienceAction.EXISTING_MULTIPLE_REPRESENTATIVES.value,
        ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value,
        ObservationScienceAction.PHYSICAL_IDENTITY_COLLISION_UNDER_LEGACY_UNIQNUM.value,
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_DATE_UNDER_LEGACY_UNIQNUM.value,
        ObservationScienceAction.AMBIGUOUS_PHYSICAL_IDENTITY_CONTEXT_UNDER_LEGACY_UNIQNUM.value,
        ObservationScienceAction.MISSING_OBSERVATION_ID.value,
        ObservationScienceAction.IDENTITY_CONFLICT.value,
    }
    blocked_frames = sum(counts[name] for name in blocked_actions)
    return {
        "input_frames": len(rows),
        "selected_frames": int(selected_frame_count),
        "blocked_frames": int(blocked_frames),
        "known_observations": len(observation_ids),
        "physical_identity_groups": len(physical_keys),
        "actions": counts,
    }
