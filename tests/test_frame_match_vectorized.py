from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

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


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _CaptureSession:
    def __init__(self, source_rows, *, delete_rowcount=0):
        self.source_rows = source_rows
        self.delete_rowcount = delete_rowcount
        self.statements = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(statement)
        if statement.is_select:
            return _RowsResult(self.source_rows)
        return SimpleNamespace(rowcount=self.delete_rowcount)

    def commit(self):
        self.commits += 1


def _source_row(catalog_source_id, *, ra_deg=10.0, mag=12.0):
    return SimpleNamespace(
        _mapping={
            "catalog_source_id": catalog_source_id,
            "ra_deg": ra_deg,
            "dec_deg": 20.0,
            "ref_mags": {"G": mag},
        }
    )


def _postgres_sql(statement):
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_match_catalog_source_selection_is_unscoped_when_ids_are_omitted(monkeypatch) -> None:
    session = _CaptureSession([_source_row(source_id) for source_id in [101, 102]])
    monkeypatch.setattr(frame_match, "_select_frames", lambda *_args, **_kwargs: [])

    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="gaia_dr3",
        dry_run=True,
    )

    assert result.selected_sources == 2
    source_sql = _postgres_sql(session.statements[0])
    assert "catalog_sources.catalog_name = 'gaia_dr3'" in source_sql
    assert "catalog_sources.catalog_source_id IN (" not in source_sql


def test_match_catalog_rejects_empty_catalog_source_ids_before_session_access() -> None:
    with pytest.raises(ValueError, match="explicit catalog_source_ids selection must not be empty"):
        frame_match.match_catalog_to_frames(
            object(),
            catalog_name="gaia_dr3",
            catalog_source_ids=[],
        )


def test_replace_existing_deletes_only_explicit_source_links(monkeypatch, tmp_path) -> None:
    scope_ids = [101, 103]
    session = _CaptureSession(
        [_source_row(source_id) for source_id in scope_ids],
        delete_rowcount=2,
    )
    raw_path = tmp_path / "frame.fits"
    raw_path.write_bytes(b"test")
    frame = SimpleNamespace(frame_id=7, raw_path=str(raw_path))
    upsert_rows = []

    monkeypatch.setattr(frame_match, "_select_frames", lambda *_args, **_kwargs: [frame])
    monkeypatch.setattr(
        frame_match,
        "_frame_wcs",
        lambda *_args, **_kwargs: (SimpleNamespace(has_celestial=True), 100, 100, "DB_HEADER_JSON"),
    )
    monkeypatch.setattr(
        frame_match,
        "_project_sources",
        lambda _wcs, *, source_ra, source_dec: (
            np.asarray([10.0, 20.0]),
            np.asarray([30.0, 40.0]),
            True,
        ),
    )
    monkeypatch.setattr(frame_match, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(frame_match, "tqdm", lambda frames, **_kwargs: frames)

    def capture_upsert(_session, rows, *, insert_batch_size=None):
        upsert_rows.extend(rows)
        return 1

    monkeypatch.setattr(frame_match, "_upsert_frame_catalog_links", capture_upsert)

    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="gaia_dr3",
        catalog_source_ids=scope_ids,
        only_wcs_ok=False,
        replace_existing=True,
    )

    assert result.selected_sources == 2
    assert result.linked == 2
    assert result.deleted_existing == 2
    assert session.commits == 1
    assert [row["catalog_source_id"] for row in upsert_rows] == scope_ids

    source_sql, delete_sql = map(_postgres_sql, session.statements)
    assert "catalog_sources.catalog_source_id IN (101, 103)" in source_sql
    assert "frame_catalog_links.frame_id IN (7)" in delete_sql
    assert "catalog_sources.catalog_name = 'gaia_dr3'" in delete_sql
    assert "catalog_sources.catalog_source_id IN (101, 103)" in delete_sql
    assert "catalog_sources.catalog_source_id IN (101, 102, 103)" not in delete_sql

    # Source 102 shares the namespace but is outside the delete predicate.
    existing_same_catalog_links = [101, 102, 103]
    deleted_link_ids = [
        source_id for source_id in existing_same_catalog_links if source_id in scope_ids
    ]
    preserved_link_ids = [
        source_id for source_id in existing_same_catalog_links if source_id not in scope_ids
    ]
    assert deleted_link_ids == [101, 103]
    assert preserved_link_ids == [102]


def test_forced_source_bypasses_magnitude_and_cap_without_widening_delete_scope(
    monkeypatch, tmp_path
) -> None:
    catalog_source_ids = [101, 102, 103, 104]
    session = _CaptureSession(
        [
            _source_row(101, ra_deg=10.0, mag=12.0),
            _source_row(102, ra_deg=20.0, mag=13.0),
            _source_row(103, ra_deg=30.0, mag=20.0),
            _source_row(104, ra_deg=40.0, mag=21.0),
        ],
        delete_rowcount=2,
    )
    raw_path = tmp_path / "frame.fits"
    raw_path.write_bytes(b"test")
    frame = SimpleNamespace(frame_id=7, raw_path=str(raw_path))
    projected_ra = []
    upsert_rows = []

    monkeypatch.setattr(frame_match, "_select_frames", lambda *_args, **_kwargs: [frame])
    monkeypatch.setattr(
        frame_match,
        "_frame_wcs",
        lambda *_args, **_kwargs: (SimpleNamespace(has_celestial=True), 100, 100, "DB_HEADER_JSON"),
    )

    def project(_wcs, *, source_ra, source_dec):
        projected_ra.extend(source_ra.tolist())
        return np.arange(len(source_ra), dtype=float), np.ones(len(source_dec)), True

    monkeypatch.setattr(frame_match, "_project_sources", project)
    monkeypatch.setattr(frame_match, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(frame_match, "tqdm", lambda frames, **_kwargs: frames)

    def capture_upsert(_session, rows, *, insert_batch_size=None):
        upsert_rows.extend(rows)
        return 1

    monkeypatch.setattr(frame_match, "_upsert_frame_catalog_links", capture_upsert)

    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="gaia_dr3",
        catalog_source_ids=catalog_source_ids,
        forced_catalog_source_ids=[103],
        frame_ids=[7],
        only_wcs_ok=False,
        working_mag_limit=15.0,
        max_sources_per_frame=1,
        replace_existing=True,
    )

    assert projected_ra == [10.0, 20.0, 30.0]
    assert [row["catalog_source_id"] for row in upsert_rows] == [101, 103]
    assert result.mag_limited_sources == 3
    assert result.limited_links == 1
    assert result.linked == 2
    assert result.deleted_existing == 2

    source_sql, delete_sql = map(_postgres_sql, session.statements)
    assert "catalog_sources.catalog_source_id IN (101, 102, 103, 104)" in source_sql
    assert "catalog_sources.catalog_source_id IN (101, 102, 103, 104)" in delete_sql
    assert "catalog_sources.catalog_source_id IN (101, 102, 103, 104, 105)" not in delete_sql


def test_forced_source_must_be_inside_explicit_catalog_scope() -> None:
    with pytest.raises(ValueError, match="must be a subset of catalog_source_ids"):
        frame_match.match_catalog_to_frames(
            object(),
            catalog_name="gaia_dr3",
            catalog_source_ids=[101],
            forced_catalog_source_ids=[102],
        )


def test_non_forced_sources_keep_existing_magnitude_and_cap_behavior(
    monkeypatch, tmp_path
) -> None:
    session = _CaptureSession(
        [
            _source_row(101, ra_deg=10.0, mag=12.0),
            _source_row(102, ra_deg=20.0, mag=13.0),
            _source_row(103, ra_deg=30.0, mag=20.0),
        ]
    )
    raw_path = tmp_path / "frame.fits"
    raw_path.write_bytes(b"test")
    frame = SimpleNamespace(frame_id=7, raw_path=str(raw_path))
    upsert_rows = []

    monkeypatch.setattr(frame_match, "_select_frames", lambda *_args, **_kwargs: [frame])
    monkeypatch.setattr(
        frame_match,
        "_frame_wcs",
        lambda *_args, **_kwargs: (SimpleNamespace(has_celestial=True), 100, 100, "DB_HEADER_JSON"),
    )
    monkeypatch.setattr(
        frame_match,
        "_project_sources",
        lambda _wcs, *, source_ra, source_dec: (
            np.arange(len(source_ra), dtype=float),
            np.ones(len(source_dec)),
            True,
        ),
    )
    monkeypatch.setattr(frame_match, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(frame_match, "tqdm", lambda frames, **_kwargs: frames)

    def capture_upsert(_session, rows, *, insert_batch_size=None):
        upsert_rows.extend(rows)
        return 1

    monkeypatch.setattr(frame_match, "_upsert_frame_catalog_links", capture_upsert)

    result = frame_match.match_catalog_to_frames(
        session,
        catalog_name="gaia_dr3",
        catalog_source_ids=[101, 102, 103],
        frame_ids=[7],
        only_wcs_ok=False,
        working_mag_limit=15.0,
        max_sources_per_frame=1,
    )

    assert [row["catalog_source_id"] for row in upsert_rows] == [101]
    assert result.mag_limited_sources == 2
    assert result.limited_links == 1
    assert result.linked == 1
