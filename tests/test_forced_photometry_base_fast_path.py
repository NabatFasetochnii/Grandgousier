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

    def execute(self, statement):
        del statement
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
