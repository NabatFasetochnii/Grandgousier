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


def test_working_mag_sql_preserves_configured_alias_order_and_python_whitespace_semantics():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    mag_key = "instrument_mag"
    assert frame_match._source_mag_keys(mag_key) == [
        "instrument_mag",
        "INSTRUMENT_MAG",
        "G",
        "g",
        "phot_g_mean_mag",
    ]
    assert frame_match._source_mag(
        {"ref_mags": {"instrument_mag": " 18.5 ", "G": 10.0}}, mag_key
    ) == 18.5
    assert frame_match._source_mag({"ref_mags": {"G": " 18.5 "}}, mag_key) == 18.5

    statement = select(frame_match._source_mag_sql_expression(mag_key) <= 19.0)
    sql = statement.compile(dialect=postgresql.dialect()).string.lower()
    assert "btrim(" in sql
    assert "catalog_sources.ref_mags" in sql


def test_final_projection_applies_eligible_and_include_ids_in_catalog_sql(monkeypatch):
    from sqlalchemy.dialects import postgresql

    class FakeRows:
        def all(self):
            return []

    class FakeSession:
        def __init__(self):
            self.statement = None

        def execute(self, statement, *_args, **_kwargs):
            self.statement = statement
            return FakeRows()

        def commit(self):
            pass

    session = FakeSession()
    monkeypatch.setattr(frame_match, "_select_frames", lambda *_args, **_kwargs: [])
    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="cat",
        frame_ids=[7],
        eligible_catalog_source_ids=[10, 11],
        include_catalog_source_ids=[12],
        working_mag_limit=19.0,
        replace_existing=True,
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    sql = compiled.string
    assert "catalog_sources.catalog_source_id IN" in sql
    assert "catalog_sources.ref_mags" in sql
    assert compiled.params["catalog_source_id_1"] == [10, 11, 12]
    assert result.selected_sources == 0
    assert result.selected_frames == 0


def test_empty_support_projection_replaces_stale_catalog_links(monkeypatch, tmp_path):
    class FakeRows:
        def all(self):
            return []

    class FakeSession:
        def __init__(self):
            self.executed = []

        def execute(self, statement, *_args, **_kwargs):
            self.executed.append(str(statement))
            if str(statement).startswith("DELETE FROM frame_catalog_links"):
                return SimpleNamespace(rowcount=3)
            return FakeRows()

        def commit(self):
            pass

    raw_path = tmp_path / "frame.fits"
    raw_path.write_bytes(b"synthetic")
    monkeypatch.setattr(
        frame_match,
        "_select_frames",
        lambda *_args, **_kwargs: [
            SimpleNamespace(frame_id=7, raw_path=raw_path, header_json={})
        ],
    )
    monkeypatch.setattr(
        frame_match,
        "_frame_wcs",
        lambda *_args, **_kwargs: (SimpleNamespace(has_celestial=True), 10, 10, "DB_HEADER_JSON"),
    )
    monkeypatch.setattr(
        frame_match,
        "_project_sources",
        lambda _wcs, *, source_ra, source_dec: (
            np.asarray([], dtype=float),
            np.asarray([], dtype=float),
            True,
        ),
    )
    monkeypatch.setattr(frame_match, "postgres_copy_capable", lambda _session: False)
    session = FakeSession()

    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="cat",
        frame_ids=[7],
        eligible_catalog_source_ids=[],
        include_catalog_source_ids=[],
        replace_existing=True,
    )

    assert result.deleted_existing == 3
    assert any(sql.startswith("DELETE FROM frame_catalog_links") for sql in session.executed)
