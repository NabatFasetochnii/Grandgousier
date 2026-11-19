from __future__ import annotations

from datetime import UTC, datetime

import pytest

from grandgousier.observing_day import (
    ObservingDayPolicy,
    operational_date_iso,
    resolve_site_timezone,
)


def test_kao_local_noon_boundary_is_0700_utc_and_site_keys_are_case_insensitive():
    policy = ObservingDayPolicy(site_timezones={"KAO": "Asia/Yekaterinburg"})

    assert operational_date_iso("2024-01-02T06:59:59Z", "kao", policy) == "2024-01-01"
    assert operational_date_iso("2024-01-02T07:00:00Z", "KAO", policy) == "2024-01-02"


def test_naive_robophot_date_obs_retains_utc_interpretation():
    policy = ObservingDayPolicy(site_timezones={"KAO": "Asia/Yekaterinburg"})
    naive = datetime(2024, 1, 2, 6, 59, 59)

    assert operational_date_iso(naive, "KAO", policy) == "2024-01-01"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-01-02T16:59:59Z", "2024-01-01"),
        ("2024-01-02T17:00:00Z", "2024-01-02"),
        ("2024-07-02T15:59:59Z", "2024-07-01"),
        ("2024-07-02T16:00:00Z", "2024-07-02"),
    ],
)
def test_iana_zoneinfo_applies_historical_dst_offsets(value, expected):
    policy = ObservingDayPolicy(site_timezones={"NY": "America/New_York"})

    assert operational_date_iso(value, "NY", policy) == expected


def test_unknown_site_requires_explicit_override_or_configured_default():
    policy = ObservingDayPolicy(site_timezones={"KAO": "Asia/Yekaterinburg"})

    with pytest.raises(ValueError, match="No IANA timezone configured for site 'UNKNOWN'"):
        resolve_site_timezone("UNKNOWN", policy)
    assert resolve_site_timezone("UNKNOWN", policy, timezone_override="Europe/Paris") == "Europe/Paris"
    assert operational_date_iso(
        "2024-01-02T07:00:00Z", "UNKNOWN", policy, timezone_override="Asia/Yekaterinburg"
    ) == "2024-01-02"

    defaulted = ObservingDayPolicy(default_timezone="UTC")
    assert resolve_site_timezone("UNKNOWN", defaulted) == "UTC"


def test_default_policy_preserves_kao_timezone_without_defaulting_unknown_sites():
    policy = ObservingDayPolicy()
    partial_config = ObservingDayPolicy.from_mapping({"boundary_local_time": "11:00:00"})

    assert policy.site_timezones == {"KAO": "Asia/Yekaterinburg"}
    assert partial_config.site_timezones == policy.site_timezones
    assert operational_date_iso("2024-01-02T07:00:00Z", "KAO", policy) == "2024-01-02"
    with pytest.raises(ValueError, match="No IANA timezone configured for site 'UNKNOWN'"):
        resolve_site_timezone("UNKNOWN", policy)


def test_fixed_offset_names_and_timezone_bearing_local_boundaries_are_rejected():
    with pytest.raises(ValueError, match="valid IANA timezone name"):
        ObservingDayPolicy(site_timezones={"KAO": "+05:00"})
    with pytest.raises(ValueError, match="without a timezone offset"):
        ObservingDayPolicy(boundary_local_time="12:00:00+05:00")


def test_none_date_does_not_need_site_timezone():
    assert operational_date_iso(None, None, ObservingDayPolicy()) is None


def test_aware_non_utc_timestamp_is_normalized_as_utc_before_site_conversion():
    policy = ObservingDayPolicy(site_timezones={"KAO": "Asia/Yekaterinburg"})
    local_equivalent = datetime.fromisoformat("2024-01-02T12:00:00+05:00")

    assert local_equivalent.astimezone(UTC).isoformat() == "2024-01-02T07:00:00+00:00"
    assert operational_date_iso(local_equivalent, "KAO", policy) == "2024-01-02"
