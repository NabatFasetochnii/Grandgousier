from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

MASTER_IDENTITY_SCHEME = "master_uniqnum_v1"
ROBOPHOT_IDENTITY_SCHEME = "robophot_sernum_dateobs_v1"

_SUPPORTED_INSTRUMENTS = {"MASTER", "RoboPhot"}
_UTC_ISO_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ]"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<zone>Z|[+-]\d{2}:\d{2})?$",
    re.IGNORECASE,
)


class ObservationIdentityError(ValueError):
    """Base error for an invalid physical-observation identity."""


class ObservationIdentityMissing(ObservationIdentityError):
    """Required native identity metadata are absent from the stored FITS header."""


@dataclass(frozen=True, slots=True)
class ObservationIdentity:
    instrument: str
    identity_scheme: str
    native_identity: str
    observation_uid: str
    camera_identity: str | None
    observed_at: datetime | None
    identity_components: dict[str, str]


def _normalize_token(value: Any, *, field: str) -> str:
    if value is None:
        raise ObservationIdentityMissing(f"missing {field}")
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        raise ObservationIdentityMissing(f"missing {field}")
    return text


def _header_value(header_json: Mapping[str, Any], key: str) -> Any:
    if key in header_json:
        return header_json.get(key)
    upper = key.upper()
    for candidate, value in header_json.items():
        if str(candidate).upper() == upper:
            return value
    return None


def canonicalize_robophot_date_obs_utc(value: Any) -> str:
    """Return the canonical UTC timestamp used by RoboPhot observation identity.

    RoboPhot FITS ``DATE-OBS`` is defined by the instrument contract as UTC. A
    timezone-less FITS timestamp is therefore interpreted as UTC, not local KAO
    civil time. Explicit ``Z``/zero offsets are accepted; non-zero offsets are
    rejected so an accidental timezone reinterpretation cannot silently alter a
    stable identity.

    Fractional seconds are preserved but insignificant trailing zeros are removed,
    making e.g. ``.636`` and ``.636000`` the same instant/identity.
    """

    raw = _normalize_token(value, field="DATE-OBS")
    match = _UTC_ISO_RE.fullmatch(raw)
    if match is None:
        raise ObservationIdentityError(f"invalid RoboPhot DATE-OBS: {raw!r}")

    zone = (match.group("zone") or "").upper()
    if zone not in {"", "Z", "+00:00", "-00:00"}:
        raise ObservationIdentityError(
            "RoboPhot DATE-OBS must already be UTC; non-zero timezone offset "
            f"is not allowed in identity v1: {raw!r}"
        )

    date_part = match.group("date")
    time_part = match.group("time")
    fraction = match.group("fraction") or ""

    # Validate calendar/time fields. Python's datetime supports the precision we
    # need for validation; the original fractional digits are retained below.
    validate_fraction = (fraction[1:] + "000000")[:6] if fraction else "000000"
    try:
        datetime.fromisoformat(f"{date_part}T{time_part}.{validate_fraction}")
    except ValueError as exc:
        raise ObservationIdentityError(f"invalid RoboPhot DATE-OBS: {raw!r}") from exc

    if fraction:
        digits = fraction[1:].rstrip("0")
        fraction = f".{digits}" if digits else ""
    return f"{date_part}T{time_part}{fraction}Z"


def _canonical_utc_to_datetime(value: str) -> datetime:
    base = value[:-1] if value.endswith("Z") else value
    # DB DateTime has microsecond resolution. Identity keeps the full canonical
    # string independently, so this conversion cannot reduce UID precision.
    if "." in base:
        prefix, fraction = base.split(".", 1)
        base = f"{prefix}.{(fraction + '000000')[:6]}"
    return datetime.fromisoformat(base).replace(tzinfo=timezone.utc)


def make_observation_uid(identity_scheme: str, native_identity: str) -> str:
    scheme = _normalize_token(identity_scheme, field="identity_scheme")
    native = _normalize_token(native_identity, field="native_identity")
    payload = f"grandgousier-observation|{scheme}|{native}".encode("utf-8")
    return "obs_" + hashlib.sha256(payload).hexdigest()


def build_frame_observation_identity(frame: Any) -> ObservationIdentity:
    """Build the P4a stable identity from an indexed ``Frame``-like object.

    Source of truth is ``frame.header_json``. Path, file size, mtime and legacy
    ``frame_uid`` never participate in this identity.
    """

    instrument_raw = _normalize_token(getattr(frame, "instrument", None), field="instrument")
    instrument = "MASTER" if instrument_raw.upper() == "MASTER" else instrument_raw
    if instrument_raw.lower() == "robophot":
        instrument = "RoboPhot"
    if instrument not in _SUPPORTED_INSTRUMENTS:
        raise ObservationIdentityError(
            f"unsupported instrument for observation identity v1: {instrument_raw!r}"
        )

    header = getattr(frame, "header_json", None) or {}
    if not isinstance(header, Mapping):
        raise ObservationIdentityError("frame.header_json must be a mapping")

    if instrument == "MASTER":
        uniqnum = _normalize_token(_header_value(header, "UNIQNUM"), field="UNIQNUM")
        scheme = MASTER_IDENTITY_SCHEME
        native = uniqnum
        camera = getattr(frame, "camera", None)
        camera_identity = str(camera).strip() if camera not in (None, "") else None
        observed_at = getattr(frame, "date_obs", None)
        components = {"UNIQNUM": uniqnum}
    else:
        sernum = _normalize_token(_header_value(header, "SERNUM"), field="SERNUM")
        date_obs_utc = canonicalize_robophot_date_obs_utc(_header_value(header, "DATE-OBS"))
        scheme = ROBOPHOT_IDENTITY_SCHEME
        native = f"{sernum}|{date_obs_utc}"
        camera_identity = sernum
        observed_at = _canonical_utc_to_datetime(date_obs_utc)
        components = {"SERNUM": sernum, "DATE-OBS-UTC": date_obs_utc}

    return ObservationIdentity(
        instrument=instrument,
        identity_scheme=scheme,
        native_identity=native,
        observation_uid=make_observation_uid(scheme, native),
        camera_identity=camera_identity,
        observed_at=observed_at,
        identity_components=components,
    )
