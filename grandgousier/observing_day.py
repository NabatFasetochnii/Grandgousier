"""Shared site-local operational-date policy for archive grouping and exports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_SITE_TIMEZONES = {"KAO": "Asia/Yekaterinburg"}


def _iana_timezone(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an IANA timezone name")
    timezone_name = value.strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a valid IANA timezone name, got {value!r}") from exc
    return timezone_name


def _parse_boundary(value: str | time) -> time:
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = time.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(
                "boundary_local_time must be an ISO local time such as '12:00:00'"
            ) from exc
    else:
        raise ValueError("boundary_local_time must be an ISO local time such as '12:00:00'")
    if parsed.tzinfo is not None:
        raise ValueError("boundary_local_time must be local wall time without a timezone offset")
    return parsed


@dataclass(frozen=True, slots=True)
class ObservingDayPolicy:
    """IANA site timezones and local wall-clock boundary for operational dates.

    Naive ``DATE-OBS`` values follow Grandgousier's established convention and are
    interpreted as UTC. Missing or unmapped sites are never silently treated as UTC.
    """

    boundary_local_time: str | time = "12:00:00"
    site_timezones: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_SITE_TIMEZONES)
    )
    default_timezone: str | None = None
    _boundary: time = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        boundary = _parse_boundary(self.boundary_local_time)
        canonical_boundary = boundary.isoformat()

        if not isinstance(self.site_timezones, Mapping):
            raise ValueError("site_timezones must be a mapping of site names to IANA timezones")
        normalized: dict[str, str] = {}
        seen: set[str] = set()
        for site, zone in self.site_timezones.items():
            site_name = str(site).strip()
            site_key = site_name.casefold()
            if not site_name:
                raise ValueError("site_timezones site names must not be empty")
            if site_key in seen:
                raise ValueError(f"Duplicate site key in site_timezones: {site_name!r}")
            seen.add(site_key)
            normalized[site_name] = _iana_timezone(zone, name=f"timezone for site {site_name!r}")

        default = self.default_timezone
        if default is not None:
            if isinstance(default, str) and not default.strip():
                default = None
            else:
                default = _iana_timezone(default, name="default_timezone")

        object.__setattr__(self, "boundary_local_time", canonical_boundary)
        object.__setattr__(self, "_boundary", boundary)
        object.__setattr__(self, "site_timezones", normalized)
        object.__setattr__(self, "default_timezone", default)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ObservingDayPolicy":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("observing_days must be a mapping")
        allowed = {"boundary_local_time", "site_timezones", "default_timezone"}
        unknown = sorted(str(key) for key in set(value) - allowed)
        if unknown:
            raise ValueError("Unknown observing_days key(s): " + ", ".join(unknown))
        return cls(
            boundary_local_time=value.get("boundary_local_time", "12:00:00"),
            site_timezones=value.get("site_timezones", _DEFAULT_SITE_TIMEZONES),
            default_timezone=value.get("default_timezone"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_local_time": self.boundary_local_time,
            "site_timezones": dict(self.site_timezones),
            "default_timezone": self.default_timezone,
        }

    @property
    def definition(self) -> str:
        return (
            "local civil date with boundary at "
            f"{self.boundary_local_time}; DATE-OBS is UTC"
        )


DEFAULT_OBSERVING_DAY_POLICY = ObservingDayPolicy()


def resolve_site_timezone(
    site: str | None,
    policy: ObservingDayPolicy,
    *,
    timezone_override: str | None = None,
) -> str:
    """Resolve an explicit override, configured site mapping, then configured default."""

    if timezone_override is not None:
        return _iana_timezone(timezone_override, name="timezone_override")

    site_name = str(site).strip() if site is not None else ""
    site_key = site_name.casefold()
    for configured_site, timezone_name in policy.site_timezones.items():
        if configured_site.casefold() == site_key and site_key:
            return timezone_name
    if policy.default_timezone is not None:
        return policy.default_timezone
    site_label = site_name or "<missing>"
    raise ValueError(
        f"No IANA timezone configured for site {site_label!r}; configure site_timezones, "
        "an explicit timezone_override, or default_timezone"
    )


def _as_utc_datetime(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"Invalid DATE-OBS value: {value!r}") from exc
    else:
        raise ValueError(f"Unsupported DATE-OBS value type: {type(value).__name__}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def operational_date(
    value_utc: datetime | date | str | None,
    site: str | None,
    policy: ObservingDayPolicy,
    *,
    timezone_override: str | None = None,
) -> date | None:
    """Return the site's civil date, assigning times before the boundary to the prior day."""

    if value_utc is None:
        return None
    utc_value = _as_utc_datetime(value_utc)
    zone_name = resolve_site_timezone(site, policy, timezone_override=timezone_override)
    local_value = utc_value.astimezone(ZoneInfo(zone_name))
    local_clock = local_value.timetz().replace(tzinfo=None)
    if local_clock < policy._boundary:
        return local_value.date() - timedelta(days=1)
    return local_value.date()


def operational_date_iso(
    value_utc: datetime | date | str | None,
    site: str | None,
    policy: ObservingDayPolicy,
    *,
    timezone_override: str | None = None,
) -> str | None:
    result = operational_date(
        value_utc,
        site,
        policy,
        timezone_override=timezone_override,
    )
    return result.isoformat() if result is not None else None
