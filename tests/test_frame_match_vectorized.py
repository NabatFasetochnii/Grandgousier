from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("astropy")

from grandgousier.catalogs import frame_match


def test_project_sources_uses_one_vectorized_wcs_call() -> None:
    class FakeWCS:
        def __init__(self):
            self.calls = 0

        def world_to_pixel_values(self, ra, dec):
            self.calls += 1
            assert isinstance(ra, np.ndarray)
            assert isinstance(dec, np.ndarray)
            return ra * 10.0, dec * 20.0

    wcs = FakeWCS()
    ra = np.asarray([1.0, 2.0, 3.0])
    dec = np.asarray([4.0, 5.0, 6.0])
    x, y, vectorized = frame_match._project_sources(wcs, source_ra=ra, source_dec=dec)

    assert vectorized is True
    assert wcs.calls == 1
    assert np.array_equal(x, np.asarray([10.0, 20.0, 30.0]))
    assert np.array_equal(y, np.asarray([80.0, 100.0, 120.0]))


def test_project_sources_falls_back_to_scalar_for_non_vector_wcs() -> None:
    class FakeWCS:
        def __init__(self):
            self.vector_calls = 0
            self.scalar_calls = 0

        def world_to_pixel_values(self, ra, dec):
            if isinstance(ra, np.ndarray):
                self.vector_calls += 1
                raise TypeError("scalar-only test WCS")
            self.scalar_calls += 1
            if float(ra) == 2.0:
                raise ValueError("bad source")
            return float(ra) + 1.0, float(dec) + 2.0

    wcs = FakeWCS()
    ra = np.asarray([1.0, 2.0, 3.0])
    dec = np.asarray([4.0, 5.0, 6.0])
    x, y, vectorized = frame_match._project_sources(wcs, source_ra=ra, source_dec=dec)

    assert vectorized is False
    assert wcs.vector_calls == 1
    assert wcs.scalar_calls == 3
    assert x[0] == 2.0 and y[0] == 6.0
    assert np.isnan(x[1]) and np.isnan(y[1])
    assert x[2] == 4.0 and y[2] == 8.0


def test_frame_wcs_prefers_indexed_header_json(monkeypatch, tmp_path) -> None:
    header_json = {
        "NAXIS": 2,
        "NAXIS1": 100,
        "NAXIS2": 80,
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": 10.0,
        "CRVAL2": 20.0,
        "CRPIX1": 50.0,
        "CRPIX2": 40.0,
        "CDELT1": -0.001,
        "CDELT2": 0.001,
    }
    frame = SimpleNamespace(header_json=header_json)
    raw_path = tmp_path / "frame.fits"
    raw_path.write_bytes(b"not read")

    def fail_open(*args, **kwargs):
        raise AssertionError("FITS file should not be opened when indexed WCS is complete")

    monkeypatch.setattr(frame_match.fits, "open", fail_open)
    wcs, nx, ny, source = frame_match._frame_wcs(frame, raw_path)

    assert wcs.has_celestial
    assert (nx, ny) == (100, 80)
    assert source == "DB_HEADER_JSON"
