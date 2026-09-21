import pytest

pytest.importorskip("astropy")

from grandgousier.io.parsing import parse_datetime, parse_dec_deg, parse_ra_deg, to_float, to_int


def test_parse_numbers():
    assert to_float("1.5") == 1.5
    assert to_int("2.0") == 2
    assert to_float("bad") is None


def test_parse_ra_dec():
    assert abs(parse_ra_deg("12:00:00") - 180.0) < 1e-6
    assert parse_dec_deg("+30:00:00") == 30.0


def test_parse_datetime_accepts_canonical_utc_offset():
    parsed = parse_datetime("2018-09-07T17:04:22+00:00")
    assert parsed is not None
    assert parsed.isoformat() == "2018-09-07T17:04:22+00:00"


def test_parse_datetime_normalizes_nonzero_offset_to_utc():
    parsed = parse_datetime("2018-09-07T22:04:22+05:00")
    assert parsed is not None
    assert parsed.isoformat() == "2018-09-07T17:04:22+00:00"
