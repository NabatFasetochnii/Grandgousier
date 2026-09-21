from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier.quality import object_quality as oq


class _MappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return iter(self._rows)


class _ExecuteSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, _statement):
        self.calls += 1
        return _MappingResult(self.rows)


def test_detection_support_scalar_loader_preserves_best_per_frame_semantics():
    session = _ExecuteSession(
        [
            {"catalog_source_id": 7, "frame_id": 1, "sep_arcsec": 0.4, "snr": 10.0},
            {"catalog_source_id": 7, "frame_id": 1, "sep_arcsec": 0.2, "snr": 5.0},
            {"catalog_source_id": 7, "frame_id": 2, "sep_arcsec": 0.3, "snr": 20.0},
        ]
    )

    stats = oq._load_detection_support_stats(
        session,
        catalog_name="gaia",
        instrument_unit_id=3,
        frame_ids=[1, 2],
    )[7]

    assert session.calls == 1
    assert stats["n_detection_matches_raw"] == 3
    assert stats["n_detection_match_frames"] == 2
    assert stats["n_extra_detection_matches"] == 1
    assert stats["n_multiple_detection_frames"] == 1
    assert stats["det_sep_median_arcsec"] == pytest.approx(0.25)
    # SNR follows the minimum-separation match in each frame: 5 and 20.
    assert stats["det_snr_median"] == pytest.approx(12.5)
    assert stats["det_snr_max"] == pytest.approx(20.0)


def test_object_qc_support_snapshot_has_calibration_independent_scope(monkeypatch):
    sources = {
        "a": oq.ObjectQcCatalogSourceRow(1, "gaia", "a", 10.0, 20.0),
        "b": oq.ObjectQcCatalogSourceRow(2, "gaia", "b", 10.1, 20.0),
    }
    monkeypatch.setattr(
        oq,
        "_load_catalog_sources_by_source_id",
        lambda *args, **kwargs: sources,
    )
    monkeypatch.setattr(
        oq,
        "_build_neighbor_stats_with_diagnostics",
        lambda *args, **kwargs: (
            {"a": {"nearest_neighbor_arcsec": 1.0}},
            {
                "algorithm": "test",
                "catalog_sources": 2,
                "target_sources": 1,
                "pair_evaluations": 2,
                "cache_hit": False,
            },
        ),
    )
    monkeypatch.setattr(
        oq,
        "_load_detection_support_stats",
        lambda *args, **kwargs: {1: {"n_detection_match_frames": 2}},
    )

    snapshot = oq.load_object_qc_support_snapshot(
        object(),
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=[3, 1, 3, 2],
        target_source_ids={"a"},
    )

    assert snapshot.frame_ids == (1, 2, 3)
    assert snapshot.target_source_ids == ("a",)
    assert snapshot.catalog_sources_by_id is sources
    assert snapshot.matches(
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=[2, 1, 3],
        target_source_ids=["a"],
    )
    assert not snapshot.matches(
        catalog_name="gaia",
        instrument_unit_id=4,
        frame_ids=[1, 2],
        target_source_ids=["a"],
    )
    assert snapshot.timings_sec["_runner_cache_hit"] == 0.0
