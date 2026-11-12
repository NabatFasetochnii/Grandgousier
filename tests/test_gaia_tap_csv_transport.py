from __future__ import annotations

import math
from types import SimpleNamespace

from grandgousier.catalogs import gaia_query as gq


class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "text/csv"):
        self.content = body
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None


def test_tap_csv_keeps_gaia_source_id_as_exact_text(monkeypatch):
    body = (
        b"source_id,ra,dec,ref_epoch,pmra,pmdec,phot_g_mean_mag,"
        b"phot_bp_mean_mag,phot_rp_mean_mag,ruwe,duplicated_source\n"
        b"6917529027641081856,123.5,-22.25,2016.0,1.2,-3.4,15.1,15.5,14.7,1.03,false\n"
    )
    seen = {}

    def fake_post(url, *, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers, timeout=timeout)
        return _FakeResponse(body)

    monkeypatch.setattr(gq.requests, "post", fake_post)
    client = gq._HttpCsvTapClient("https://example.invalid/tap", timeout_sec=17)
    rows = client.launch_job("SELECT TOP 1 * FROM gaiadr3.gaia_source", maxrec=1).get_results()

    assert seen["url"] == "https://example.invalid/tap/sync"
    assert seen["data"]["FORMAT"] == "csv"
    assert seen["headers"]["Accept"].startswith("text/csv")
    assert seen["timeout"] == 17.0
    assert rows[0]["source_id"] == "6917529027641081856"
    assert rows[0]["ra"] == 123.5
    assert rows[0]["duplicated_source"] is False


def test_tap_csv_xml_error_is_rejected(monkeypatch):
    def fake_post(url, *, data, headers, timeout):
        return _FakeResponse(b'<?xml version="1.0"?><VOTABLE><INFO value="ERROR"/></VOTABLE>', content_type="application/x-votable+xml")

    monkeypatch.setattr(gq.requests, "post", fake_post)
    client = gq._HttpCsvTapClient("https://example.invalid/tap")
    try:
        client.launch_job("SELECT 1", maxrec=1)
    except RuntimeError as exc:
        assert "XML instead of CSV" in str(exc)
    else:
        raise AssertionError("expected XML response rejection")


def test_provider_chain_uses_http_csv_mirrors_before_esac(monkeypatch):
    class _FakeGaia:
        pass

    # _build_gaia_tap_providers imports Gaia at call time.  The real astroquery
    # dependency is present in production; this test only verifies our ordering
    # and transport class when import succeeds in the test environment.
    providers = gq._build_gaia_tap_providers(23)
    assert providers[0].name == "gaia_aip_csv"
    assert isinstance(providers[0].client, gq._HttpCsvTapClient)
    assert providers[1].name == "gaia_ari_csv"
    assert isinstance(providers[1].client, gq._HttpCsvTapClient)
    assert providers[-1].name == "gaia_esac"


def _install_one_frame_footprint(monkeypatch, cone=(359.9, 82.0, 0.2)):
    frame = SimpleNamespace(frame_id=71, raw_path="unused.fits")
    monkeypatch.setattr(gq, "_select_frames", lambda *_a, **_k: [frame])
    monkeypatch.setattr(
        gq,
        "_frame_cone_for_frame_diagnostic",
        lambda *_a, **_k: (cone, None),
    )
    return frame


def test_gaia_query_skips_exact_single_cone_coverage_without_provider(monkeypatch):
    cone = (359.9, 82.0, 0.2)
    _install_one_frame_footprint(monkeypatch, cone)
    monkeypatch.setattr(
        gq,
        "_build_gaia_tap_providers",
        lambda *_a: (_ for _ in ()).throw(AssertionError("covered request must not build TAP providers")),
    )

    result = gq.query_gaia_for_frames(
        object(),
        frame_ids=[71],
        query_mode="per_frame",
        coverage_cones=[gq._cone_dict(cone)],
        commit=False,
    )

    assert result.skipped_already_covered is True
    assert result.coverage_complete is True
    assert result.skipped_covered_cones == 1
    assert result.queried_cones == 0
    assert result.completed_cones == []


def test_gaia_query_expands_whole_uncovered_cone_and_caps_maxrec(monkeypatch):
    cone = (10.0, -25.0, 0.12)
    _install_one_frame_footprint(monkeypatch, cone)
    monkeypatch.setattr(gq, "_build_gaia_tap_providers", lambda *_a: ["fake-provider"])
    observed = []

    def fake_query(_providers, **kwargs):
        observed.append(kwargs)
        return [], "fake-provider", {}

    monkeypatch.setattr(gq, "_query_gaia_cone", fake_query)
    result = gq.query_gaia_for_frames(
        object(),
        frame_ids=[71],
        query_mode="per_frame",
        max_rows_per_frame=50_000,
        coverage_cones=[(10.05, -25.0, 0.02)],
        commit=False,
    )

    assert result.effective_max_rows == 20_000
    assert result.coverage_complete is True
    assert len(observed) == 1
    assert observed[0]["max_rows"] == 20_000
    assert observed[0]["center_ra_deg"] == cone[0]
    assert observed[0]["center_dec_deg"] == cone[1]
    assert observed[0]["radius_deg"] == cone[2]
    assert result.completed_cones[0]["completion_method"] == "direct_query"


def test_gaia_query_failure_does_not_claim_complete_coverage(monkeypatch):
    _install_one_frame_footprint(monkeypatch, (12.0, 5.0, 0.1))
    monkeypatch.setattr(gq, "_build_gaia_tap_providers", lambda *_a: ["fake-provider"])

    def fail_query(*_a, **_k):
        raise gq._GaiaAllProvidersFailed({"fake": "offline"})

    monkeypatch.setattr(gq, "_query_gaia_cone", fail_query)
    result = gq.query_gaia_for_frames(
        object(), frame_ids=[71], query_mode="per_frame", commit=False
    )

    assert result.failed_frames == 1
    assert result.coverage_complete is False
    assert result.completed_cones == []
    assert result.failed_footprint_reasons["gaia_query_all_providers_failed"] == 1


def test_four_child_spherical_cover_handles_wrap_poles_and_large_caps():
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    cases = [
        (359.8, 0.0, 15.0),
        (25.0, 88.0, 20.0),
        (140.0, -89.0, 45.0),
        (17.0, 90.0, 179.0),
    ]
    for ra, dec, radius_deg in cases:
        children = gq._split_gaia_cone_four(ra, dec, radius_deg)
        radius = math.radians(radius_deg)
        offset = radius / 2.0
        bound = math.acos(
            max(
                -1.0,
                min(
                    1.0,
                    math.cos(radius) * math.cos(offset)
                    + math.sin(radius) * math.sin(offset) / math.sqrt(2.0),
                ),
            )
        )
        child_radius = math.radians(children[0][2])
        assert child_radius >= max(offset, bound)
        assert child_radius > max(offset, bound)
        assert child_radius < radius
        parent = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")
        child_centers = [
            SkyCoord(child_ra * u.deg, child_dec * u.deg, frame="icrs")
            for child_ra, child_dec, _child_radius in children
        ]
        for point_radius_deg in (0.0, radius_deg * 0.25, radius_deg * 0.5, radius_deg):
            for point_pa in range(0, 360, 15):
                point = parent.directional_offset_by(
                    point_pa * u.deg, point_radius_deg * u.deg
                )
                assert min(float(center.separation(point).deg) for center in child_centers) <= (
                    children[0][2] + 1e-9
                )


def test_four_child_spherical_cover_contains_small_radius_45_degree_boundary_points():
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    # The first case crosses the RA=0 seam; the second puts the northward child
    # center across the pole. Both radii are above the configured split floor.
    cases = [
        (359.9999998, 20.0, 1e-6),
        (123.4, 89.99999998, 1e-7),
    ]
    haversine_45 = math.sin(math.radians(45.0) / 2.0) ** 2
    for ra, dec, radius_deg in cases:
        children = gq._split_gaia_cone_four(ra, dec, radius_deg)
        radius = math.radians(radius_deg)
        offset = radius / 2.0
        haversine_bound = (
            math.sin((radius - offset) / 2.0) ** 2
            + math.sin(radius) * math.sin(offset) * haversine_45
        )
        bound = 2.0 * math.asin(math.sqrt(haversine_bound))
        child_radius = math.radians(children[0][2])
        assert child_radius >= bound
        assert child_radius > bound

        parent = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs")
        boundary_point = parent.directional_offset_by(
            45.0 * u.deg, radius_deg * u.deg
        )
        child_centers = [
            SkyCoord(child_ra * u.deg, child_dec * u.deg, frame="icrs")
            for child_ra, child_dec, _child_radius in children
        ]
        nearest_child_distance = min(
            float(center.separation(boundary_point).deg) for center in child_centers
        )
        assert nearest_child_distance <= children[0][2]

    assert min(ra for ra, _dec, _radius in gq._split_gaia_cone_four(*cases[0])) < 1e-5


def test_gaia_maxrec_recursively_splits_and_upserts_overlaps_over_20k(monkeypatch):
    _install_one_frame_footprint(monkeypatch, (201.0, 67.0, 0.4))
    monkeypatch.setattr(gq, "_build_gaia_tap_providers", lambda *_a: ["fake-provider"])
    seen = []
    unique_sources = set()
    insert_calls = []
    query_progress_bars = []

    class _RecordingProgress:
        def __init__(self, iterable=None, *, desc=None, **_kwargs):
            self.iterable = iterable
            self.desc = desc
            self.count = 0
            self.closed = False
            if desc == "Querying Gaia DR3 TAP":
                query_progress_bars.append(self)

        def __iter__(self):
            return iter(()) if self.iterable is None else iter(self.iterable)

        def update(self, count=1):
            self.count += count

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.closed = True

    monkeypatch.setattr(gq, "tqdm", _RecordingProgress)

    def fake_query(_providers, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return [f"root-{index}" for index in range(20_000)], "fake-provider", {}
        child_index = len(seen) - 2
        rows = [f"root-{index}" for index in range(5_000)]
        rows.extend(f"child-{child_index}-{index}" for index in range(3_000))
        return rows, "fake-provider", {}

    monkeypatch.setattr(gq, "_query_gaia_cone", fake_query)
    monkeypatch.setattr(
        gq,
        "_gaia_row_to_catalog_source",
        lambda source_id, *, catalog_name: {
            "catalog_name": catalog_name,
            "source_id": source_id,
        },
    )

    def fake_upsert(_session, rows):
        insert_calls.append(len(rows))
        unique_sources.update((row["catalog_name"], row["source_id"]) for row in rows)

    monkeypatch.setattr(gq, "_upsert_catalog_sources", fake_upsert)
    result = gq.query_gaia_for_frames(
        object(),
        catalog_name="auto_catalog_field_17",
        frame_ids=[71],
        query_mode="per_frame",
        max_rows_per_frame=50_000,
        commit=False,
    )

    assert result.effective_max_rows == 20_000
    assert result.row_limit_reached_cones == 1
    assert result.queried_cones == 5
    assert result.coverage_complete is True
    assert result.failed_frames == 0
    assert len(seen) == 5
    assert all(query["max_rows"] <= 20_000 for query in seen)
    assert result.completed_cones[0]["completion_method"] == "four_child_recursive_cover"
    assert len(result.completed_cones[0]["children"]) == 4
    assert len(unique_sources) == 32_000
    assert sum(insert_calls) == 52_000
    assert len(query_progress_bars) == 1
    assert query_progress_bars[0].count == 5
    assert query_progress_bars[0].closed is True

    timings = result.phase_timings_sec
    assert set(timings) == {
        "footprint_calculation",
        "tap_attempts",
        "catalog_source_upserts",
        "total_query",
    }
    assert all(math.isfinite(value) and value >= 0.0 for value in timings.values())
    assert timings["footprint_calculation"] > 0.0
    assert timings["tap_attempts"] > 0.0
    assert timings["catalog_source_upserts"] > 0.0
    assert timings["total_query"] >= (
        timings["tap_attempts"] + timings["catalog_source_upserts"]
    )


def test_gaia_maxrec_depth_limit_fails_closed(monkeypatch):
    _install_one_frame_footprint(monkeypatch, (0.0, 0.0, 0.01))
    monkeypatch.setattr(gq, "_build_gaia_tap_providers", lambda *_a: ["fake-provider"])
    monkeypatch.setattr(
        gq,
        "MAX_GAIA_SPLIT_DEPTH",
        0,
    )
    monkeypatch.setattr(
        gq,
        "_query_gaia_cone",
        lambda *_a, **_k: ([None] * 20_000, "fake-provider", {}),
    )
    monkeypatch.setattr(
        gq,
        "_gaia_row_to_catalog_source",
        lambda *_a, **_k: {},
    )
    result = gq.query_gaia_for_frames(
        object(), frame_ids=[71], query_mode="per_frame", commit=False, dry_run=True
    )
    assert result.coverage_complete is False
    assert result.completed_cones == []
    assert result.failed_frames == 1
    assert result.failed_footprint_reasons["maxrec_split_depth_exceeded"] == 1


def test_gaia_maxrec_minimum_radius_fails_closed(monkeypatch):
    _install_one_frame_footprint(monkeypatch, (0.0, 0.0, 0.01))
    monkeypatch.setattr(gq, "_build_gaia_tap_providers", lambda *_a: ["fake-provider"])
    monkeypatch.setattr(gq, "MIN_GAIA_SPLIT_RADIUS_DEG", 0.02)
    monkeypatch.setattr(
        gq,
        "_query_gaia_cone",
        lambda *_a, **_k: ([None] * 20_000, "fake-provider", {}),
    )
    monkeypatch.setattr(gq, "_gaia_row_to_catalog_source", lambda *_a, **_k: {})
    result = gq.query_gaia_for_frames(
        object(),
        frame_ids=[71],
        query_mode="per_frame",
        commit=False,
        dry_run=True,
    )
    assert result.coverage_complete is False
    assert result.completed_cones == []
    assert result.failed_frames == 1
    assert result.failed_footprint_reasons["maxrec_split_min_radius_reached"] == 1
