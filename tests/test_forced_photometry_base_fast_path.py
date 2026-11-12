from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.photometry.forced_aperture import (
    _existing_measurement_counts_bulk,
    _measurement_scope_counts_are_complete,
    _select_frame_link_counts_bulk,
    _select_frame_links_bulk,
)


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.execute_calls = 0
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        rows = self._result_sets[self.execute_calls]
        self.execute_calls += 1
        return _FakeMappingResult(rows)


def test_bulk_frame_link_prefetch_preserves_per_frame_limit_and_source_identity():
    link_rows = [
        {
            "frame_catalog_link_id": 10,
            "frame_id": 1,
            "catalog_source_id": 100,
            "x": 11.0,
            "y": 12.0,
        },
        {
            "frame_catalog_link_id": 11,
            "frame_id": 1,
            "catalog_source_id": 101,
            "x": 13.0,
            "y": 14.0,
        },
        {
            "frame_catalog_link_id": 20,
            "frame_id": 2,
            "catalog_source_id": 100,
            "x": 21.0,
            "y": 22.0,
        },
    ]
    source_rows = [
        {
            "catalog_source_id": 100,
            "catalog_name": "cat",
            "source_id": "A",
            "ra_deg": 1.0,
            "dec_deg": 2.0,
            "epoch": None,
            "pmra_masyr": None,
            "pmdec_masyr": None,
            "ref_mags": {"V": 12.3},
            "colors": {"B-V": 0.4},
        }
    ]
    session = _FakeSession([link_rows, source_rows])

    grouped = _select_frame_links_bulk(
        session,
        frame_ids=[1, 2],
        catalog_name="cat",
        limit_per_frame=1,
    )

    assert session.execute_calls == 2
    assert [item[0].frame_catalog_link_id for item in grouped[1]] == [10]
    assert [item[0].frame_catalog_link_id for item in grouped[2]] == [20]
    assert grouped[1][0][1] is grouped[2][0][1]
    assert grouped[1][0][1].source_id == "A"
    assert grouped[1][0][1].ref_mags == {"V": 12.3}


def test_detection_eligibility_precedes_cap_and_explicit_target_is_prioritized():
    link_rows = [
        {
            "frame_catalog_link_id": 30,
            "frame_id": 1,
            "catalog_source_id": 102,
            "x": 3.0,
            "y": 3.0,
        },
        {
            "frame_catalog_link_id": 20,
            "frame_id": 1,
            "catalog_source_id": 101,
            "x": 2.0,
            "y": 2.0,
        },
        {
            "frame_catalog_link_id": 10,
            "frame_id": 1,
            "catalog_source_id": 100,
            "x": 1.0,
            "y": 1.0,
        },
    ]
    source_rows = [
        {
            "catalog_source_id": 100,
            "catalog_name": "field_catalog",
            "source_id": "supported-bright",
            "ra_deg": 1.0,
            "dec_deg": 2.0,
            "epoch": None,
            "pmra_masyr": None,
            "pmdec_masyr": None,
            "ref_mags": {"G": 10.0},
            "colors": {},
        },
        {
            "catalog_source_id": 101,
            "catalog_name": "field_catalog",
            "source_id": "supported-next",
            "ra_deg": 1.0,
            "dec_deg": 2.0,
            "epoch": None,
            "pmra_masyr": None,
            "pmdec_masyr": None,
            "ref_mags": {"G": 12.0},
            "colors": {},
        },
        {
            "catalog_source_id": 102,
            "catalog_name": "field_catalog",
            "source_id": "explicit-target",
            "ra_deg": 1.0,
            "dec_deg": 2.0,
            "epoch": None,
            "pmra_masyr": None,
            "pmdec_masyr": None,
            "ref_mags": {"G": 18.0},
            "colors": {},
        },
    ]
    session = _FakeSession([link_rows, source_rows])
    selection_stats = {}

    grouped = _select_frame_links_bulk(
        session,
        frame_ids=[1],
        catalog_name="field_catalog",
        limit_per_frame=2,
        eligible_catalog_source_ids={100, 101, 102},
        priority_catalog_source_ids={102},
        selection_stats=selection_stats,
    )

    assert "catalog_source_id" in str(session.statements[0])
    assert [item[0].catalog_source_id for item in grouped[1]] == [102, 100]
    assert selection_stats == {
        "eligible_links_before_cap": 3,
        "selected_links_after_cap": 2,
        "cap_dropped_links": 1,
    }


@pytest.mark.parametrize("limit", [None, 0], ids=["no-limit", "zero-disables-cap"])
def test_disabled_link_cap_keeps_every_eligible_link(limit):
    from grandgousier.photometry.forced_aperture import _select_frame_links_bulk

    link_rows = [
        {"frame_catalog_link_id": i, "frame_id": 1, "catalog_source_id": 100 + i,
         "x": float(i), "y": float(i)}
        for i in range(1, 4)
    ]
    source_rows = [
        {"catalog_source_id": 100 + i, "catalog_name": "cat", "source_id": str(i),
         "ra_deg": 1.0, "dec_deg": 2.0, "epoch": None, "pmra_masyr": None,
         "pmdec_masyr": None, "ref_mags": {}, "colors": {}}
        for i in range(1, 4)
    ]
    session = _FakeSession([link_rows, source_rows])
    stats = {}

    grouped = _select_frame_links_bulk(
        session,
        frame_ids=[1],
        catalog_name="cat",
        limit_per_frame=limit,
        eligible_catalog_source_ids={101, 102, 103},
        selection_stats=stats,
    )

    assert [item[0].catalog_source_id for item in grouped[1]] == [101, 102, 103]
    assert stats == {
        "eligible_links_before_cap": 3,
        "selected_links_after_cap": 3,
        "cap_dropped_links": 0,
    }


def test_zero_detection_explicit_source_enters_forced_identity_path(monkeypatch):
    from grandgousier.lightcurves.target import resolve_explicit_target_catalog_source
    from grandgousier.pipeline.runner import _eligible_catalog_sources_for_forced_photometry
    from grandgousier.photometry import forced_aperture as forced_module

    explicit_source_row = (201, "target-201", 10.0, 20.0, None)
    link_row = {
        "frame_catalog_link_id": 10,
        "frame_id": 1,
        "catalog_source_id": 201,
        "x": 25.0,
        "y": 25.0,
    }
    catalog_source_row = {
        "catalog_source_id": 201,
        "catalog_name": "cat",
        "source_id": "target-201",
        "ra_deg": 10.0,
        "dec_deg": 20.0,
        "epoch": None,
        "pmra_masyr": None,
        "pmdec_masyr": None,
        "ref_mags": {},
        "colors": {},
    }
    session = _FakeSession([[explicit_source_row], [link_row], [catalog_source_row]])
    selected_target = resolve_explicit_target_catalog_source(
        session,
        catalog_name="cat",
        target_source_id="target-201",
    )
    assert selected_target["eligible"] is True
    assert selected_target["identity_link_exists"] is False
    assert _eligible_catalog_sources_for_forced_photometry({}, selected_target) == [201]

    grouped = _select_frame_links_bulk(
        session,
        frame_ids=[1],
        catalog_name="cat",
        limit_per_frame=1,
        eligible_catalog_source_ids=[201],
        priority_catalog_source_ids=[201],
    )
    assert [item[0].catalog_source_id for item in grouped[1]] == [201]

    delegated = {}

    def fake_identity(_session, source, *, dry_run):
        delegated.update(source=source, dry_run=dry_run)
        return SimpleNamespace(object_id=777)

    monkeypatch.setattr(forced_module, "get_or_create_object_for_catalog_source", fake_identity)
    assert forced_module._get_or_create_object_id(
        session, grouped[1][0][1], dry_run=False
    ) == 777
    assert delegated["source"].catalog_source_id == 201
    assert delegated["dry_run"] is False


def test_bulk_reuse_counts_preserve_per_frame_limit_and_completeness():
    session = _FakeSession(
        [
            [
                {"frame_id": 1, "n_links": 12},
                {"frame_id": 2, "n_links": 3},
            ],
            [
                {"frame_id": 1, "n_measurements": 5},
                {"frame_id": 2, "n_measurements": 3},
            ],
        ]
    )

    required = _select_frame_link_counts_bulk(
        session,
        frame_ids=[1, 2, 3],
        catalog_name="cat",
        limit_per_frame=5,
    )
    existing = _existing_measurement_counts_bulk(
        session,
        calibration_version_id=7,
        frame_ids=[1, 2, 3],
    )

    assert session.execute_calls == 2
    assert required == {1: 5, 2: 3}
    assert existing == {1: 5, 2: 3}
    assert _measurement_scope_counts_are_complete(
        frame_ids=[1, 2, 3],
        required_link_counts=required,
        existing_measurement_counts=existing,
    )


def test_bulk_reuse_counts_detect_incomplete_frame():
    assert not _measurement_scope_counts_are_complete(
        frame_ids=[1, 2],
        required_link_counts={1: 5, 2: 4},
        existing_measurement_counts={1: 5, 2: 3},
    )
