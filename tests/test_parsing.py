import pytest

pytest.importorskip("astropy")

from grandgousier.io.parsing import parse_dec_deg, parse_ra_deg, to_float, to_int


def test_parse_numbers():
    assert to_float("1.5") == 1.5
    assert to_int("2.0") == 2
    assert to_float("bad") is None


def test_parse_ra_dec():
    assert abs(parse_ra_deg("12:00:00") - 180.0) < 1e-6
    assert parse_dec_deg("+30:00:00") == 30.0
