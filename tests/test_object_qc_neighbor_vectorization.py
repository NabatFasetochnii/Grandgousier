from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier.quality.object_quality import (
    _NEIGHBOR_STATS_CACHE,
    _angular_sep_arcsec,
    _build_neighbor_stats_with_diagnostics,
)


def _source(index: int, source_id: str, ra_deg: float, dec_deg: float):
    return SimpleNamespace(
        catalog_source_id=index,
        catalog_name="gaia_test",
        source_id=source_id,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
    )


def _brute_stats(sources, target_id: str):
    target = sources[target_id]
    seps = [
        _angular_sep_arcsec(target.ra_deg, target.dec_deg, other.ra_deg, other.dec_deg)
        for other in sources.values()
        if other.catalog_source_id != target.catalog_source_id
    ]
    return {
        "nearest_neighbor_arcsec": min(seps),
        "neighbors_within_3arcsec": sum(value <= 3.0 for value in seps),
        "neighbors_within_5arcsec": sum(value <= 5.0 for value in seps),
        "neighbors_within_10arcsec": sum(value <= 10.0 for value in seps),
    }


def test_vectorized_neighbor_stats_match_legacy_geometry_for_requested_sources():
    dec = 20.0
    ra_arcsec = 1.0 / (3600.0 * __import__("math").cos(__import__("math").radians(dec)))
    sources = {
        "a": _source(1, "a", 10.0, dec),
        "b": _source(2, "b", 10.0 + 2.0 * ra_arcsec, dec),
        "c": _source(3, "c", 10.0 + 4.0 * ra_arcsec, dec),
        "d": _source(4, "d", 10.0 + 9.0 * ra_arcsec, dec),
        "far": _source(5, "far", 11.0, dec + 1.0),
    }
    _NEIGHBOR_STATS_CACHE.clear()

    actual, diagnostics = _build_neighbor_stats_with_diagnostics(
        sources,
        target_source_ids={"a", "c"},
        block_size=1,
    )

    assert set(actual) == {"a", "c"}
    for source_id in actual:
        expected = _brute_stats(sources, source_id)
        assert actual[source_id]["nearest_neighbor_arcsec"] == pytest.approx(
            expected["nearest_neighbor_arcsec"], abs=2e-5
        )
        assert actual[source_id]["neighbors_within_3arcsec"] == expected["neighbors_within_3arcsec"]
        assert actual[source_id]["neighbors_within_5arcsec"] == expected["neighbors_within_5arcsec"]
        assert actual[source_id]["neighbors_within_10arcsec"] == expected["neighbors_within_10arcsec"]

    assert diagnostics["algorithm"] == "measured_targets_vectorized_haversine_v1"
    assert diagnostics["catalog_sources"] == 5
    assert diagnostics["target_sources"] == 2
    assert diagnostics["pair_evaluations"] == 10
    assert diagnostics["cache_hit"] is False


def test_vectorized_neighbor_stats_reuse_process_local_static_geometry_cache():
    sources = {
        "a": _source(1, "a", 10.0, 20.0),
        "b": _source(2, "b", 10.001, 20.0),
        "c": _source(3, "c", 10.1, 20.0),
    }
    _NEIGHBOR_STATS_CACHE.clear()

    first, first_diag = _build_neighbor_stats_with_diagnostics(sources, target_source_ids={"a", "b"})
    second, second_diag = _build_neighbor_stats_with_diagnostics(sources, target_source_ids={"a", "b"})

    assert first == second
    assert first_diag["cache_hit"] is False
    assert second_diag["cache_hit"] is True


def test_vectorized_neighbor_stats_scale_with_measured_targets_not_catalog_square():
    sources = {
        str(index): _source(index, str(index), 10.0 + index * 1e-4, 20.0)
        for index in range(1, 101)
    }
    _NEIGHBOR_STATS_CACHE.clear()

    _stats, diagnostics = _build_neighbor_stats_with_diagnostics(
        sources,
        target_source_ids={"1", "2", "3"},
    )

    assert diagnostics["pair_evaluations"] == 300
    assert diagnostics["pair_evaluations"] < len(sources) ** 2


def test_vectorized_neighbor_stats_match_brute_force_across_ra_wrap_and_high_declination():
    sources = {
        "a": _source(1, "a", 359.9997, 87.0),
        "b": _source(2, "b", 0.0003, 87.0),
        "c": _source(3, "c", 120.0, -45.0),
        "d": _source(4, "d", 120.001, -45.0),
        "e": _source(5, "e", 240.0, 5.0),
    }
    _NEIGHBOR_STATS_CACHE.clear()

    actual, _diagnostics = _build_neighbor_stats_with_diagnostics(
        sources,
        target_source_ids=set(sources),
    )

    for source_id in sources:
        expected = _brute_stats(sources, source_id)
        assert actual[source_id]["nearest_neighbor_arcsec"] == pytest.approx(
            expected["nearest_neighbor_arcsec"], abs=1e-7
        )
        assert actual[source_id]["neighbors_within_3arcsec"] == expected["neighbors_within_3arcsec"]
        assert actual[source_id]["neighbors_within_5arcsec"] == expected["neighbors_within_5arcsec"]
        assert actual[source_id]["neighbors_within_10arcsec"] == expected["neighbors_within_10arcsec"]
