import pytest

pytest.importorskip("astropy")

from astropy.io.fits import Header

from grandgousier.instruments.base import InstrumentProfile


def test_profile_normalizes_filter(tmp_path):
    cfg = tmp_path / "instrument.yaml"
    cfg.write_text(
        """
name: TEST
version: 1
header_aliases:
  filter: [FILTER]
filter_aliases:
  C: clear
image_type_aliases:
  object: science
""",
        encoding="utf-8",
    )
    p = InstrumentProfile.from_yaml(cfg)
    h = Header()
    h["FILTER"] = "C"
    assert p.normalize_filter(p.get_header_value(h, "filter")) == "clear"
