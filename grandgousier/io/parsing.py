from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from astropy.coordinates import Angle
from astropy.time import Time
import astropy.units as u


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    # Canonical Stage-1 records serialize normalized UTC datetimes with an
    # explicit ISO-8601 offset (for example ``+00:00``).  Astropy's ``isot``
    # parser does not accept that suffix, so handle standard Python ISO forms
    # first and normalize them back to UTC.  Naive ISO values retain the
    # historical Grandgousier rule: interpret them as UTC.
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        iso_raw = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(iso_raw)
        except Exception:
            parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    try:
        t = Time(value, format="isot", scale="utc")
        return t.to_datetime(timezone=timezone.utc)
    except Exception:
        pass
    try:
        t = Time(value, scale="utc")
        return t.to_datetime(timezone=timezone.utc)
    except Exception:
        return None


def parse_ra_deg(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)):
        v = float(value)
        return v if 0 <= v <= 360 else None
    raw = str(value).strip()
    try:
        return float(raw)
    except Exception:
        pass
    try:
        return Angle(raw, unit=u.hourangle).degree
    except Exception:
        try:
            return Angle(raw, unit=u.deg).degree
        except Exception:
            return None


def parse_dec_deg(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)):
        v = float(value)
        return v if -90 <= v <= 90 else None
    raw = str(value).strip()
    try:
        v = float(raw)
        return v if -90 <= v <= 90 else None
    except Exception:
        pass
    try:
        return Angle(raw, unit=u.deg).degree
    except Exception:
        return None
