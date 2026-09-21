from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from grandgousier.db.models import Frame, Observation
from grandgousier.identity.observation import ObservationIdentity


def _normalized_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float):
        return round(value, 12)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _distinct_nonempty(frames: Iterable[Frame], getter: Callable[[Frame], Any]) -> set[Any]:
    values = {_normalized_value(getter(frame)) for frame in frames}
    values.discard(None)
    return values


def group_consistency(
    frames: Iterable[Frame],
    identity: ObservationIdentity,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Classify file products sharing one proposed physical observation identity.

    These are the P4a compatibility semantics. Filesystem/product differences are
    deliberately soft; physical metadata differences are blocking collisions.
    """

    frame_rows = list(frames)
    hard_getters: dict[str, Callable[[Frame], Any]] = {
        "site": lambda f: f.site,
        "tube": lambda f: f.tube,
        "camera": lambda f: f.camera,
        "filter_name": lambda f: f.filter_name,
        "exptime": lambda f: f.exptime,
    }
    # MASTER native identity does not contain time, so independent observation
    # timestamps are a hard collision signal. RoboPhot DATE-OBS is already part
    # of its native identity and is taken directly from header_json.
    if identity.instrument == "MASTER":
        hard_getters["date_obs"] = lambda f: f.date_obs

    soft_getters: dict[str, Callable[[Frame], Any]] = {
        "file_size_bytes": lambda f: f.file_size_bytes,
        "naxis1": lambda f: (f.header_json or {}).get("NAXIS1"),
        "naxis2": lambda f: (f.header_json or {}).get("NAXIS2"),
        "object_name": lambda f: f.object_name,
    }

    hard = tuple(
        sorted(
            name
            for name, getter in hard_getters.items()
            if len(_distinct_nonempty(frame_rows, getter)) > 1
        )
    )
    soft = tuple(
        sorted(
            name
            for name, getter in soft_getters.items()
            if len(_distinct_nonempty(frame_rows, getter)) > 1
        )
    )

    if hard:
        classification = "COLLISION"
    elif len(frame_rows) == 1:
        classification = "SINGLE"
    elif soft:
        classification = "PRODUCT_VARIANT"
    else:
        classification = "COPY_LIKE"
    return hard, soft, classification


def existing_identity_conflict(existing: Observation, identity: ObservationIdentity) -> str | None:
    """Return the exact P4a identity-row mismatch diagnostic, if any."""

    mismatches: list[str] = []
    if existing.observation_uid != identity.observation_uid:
        mismatches.append("observation_uid")
    if existing.instrument != identity.instrument:
        mismatches.append("instrument")
    if existing.identity_scheme != identity.identity_scheme:
        mismatches.append("identity_scheme")
    if existing.native_identity != identity.native_identity:
        mismatches.append("native_identity")
    if mismatches:
        return "existing observation differs in: " + ",".join(mismatches)
    return None
