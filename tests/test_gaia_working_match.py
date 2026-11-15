from __future__ import annotations

import numpy as np
import pytest

from grandgousier.catalogs import gaia_query as gq
from grandgousier.quality.detection_catalog_qc import _sky_sep_arcsec


def _row(source_id: str, ra_deg: float, dec_deg: float) -> dict[str, object]:
    return {
        "source_id": source_id,
        "ra": ra_deg,
        "dec": dec_deg,
        "phot_g_mean_mag": 15.0,
    }


def _exhaustive_greedy_oracle(
    catalog: gq.GaiaWorkingCatalog,
    detection_ids: list[int],
    ra_deg: list[float],
    dec_deg: list[float],
    radius_arcsec: float,
) -> list[tuple[int, str, float]]:
    candidates: list[tuple[float, int, int]] = []
    for detection_index, (ra, dec) in enumerate(zip(ra_deg, dec_deg)):
        for source_index, (source_ra, source_dec) in enumerate(
            zip(catalog.ra_deg, catalog.dec_deg)
        ):
            separation = _sky_sep_arcsec(ra, dec, source_ra, source_dec)
            if separation is not None and separation <= radius_arcsec:
                candidates.append((separation, detection_index, source_index))

    # Python's stable sort mirrors _match_frame's separation-only candidate sort.
    candidates.sort(key=lambda candidate: candidate[0])
    used_detections: set[int] = set()
    used_sources: set[int] = set()
    accepted = []
    for separation, detection_index, source_index in candidates:
        detection_id = detection_ids[detection_index]
        if detection_id in used_detections or source_index in used_sources:
            continue
        used_detections.add(detection_id)
        used_sources.add(source_index)
        accepted.append((detection_id, str(catalog.source_ids[source_index]), separation))
    return accepted


def test_working_catalog_enforces_one_to_one_detection_source_competition():
    source_id = "9007199254740993"
    catalog = gq.GaiaWorkingCatalog([_row(source_id, 10.0, 0.0)])

    matches = catalog.match_detections(
        [22, 11],
        [10.0 + 0.2 / 3600.0, 10.0 + 0.05 / 3600.0],
        [0.0, 0.0],
        radius_arcsec=1.0,
    )

    assert len(matches) == 1
    assert matches[0][0:2] == (11, source_id)
    assert matches[0][2] < 0.1


def test_second_nearest_source_is_accepted_after_nearest_is_consumed():
    nearest_source_id = "9007199254740993"
    second_source_id = "9007199254740995"
    catalog = gq.GaiaWorkingCatalog(
        [
            _row(nearest_source_id, 0.9 / 3600.0, 0.0),
            _row(second_source_id, 1.1 / 3600.0, 0.0),
        ]
    )

    matches = catalog.match_detections(
        [1, 2],
        [0.0, 0.8 / 3600.0],
        [0.0, 0.0],
        radius_arcsec=2.0,
    )

    assert [(detection_id, source_id) for detection_id, source_id, _sep in matches] == [
        (2, nearest_source_id),
        (1, second_source_id),
    ]
    assert [separation for _detection_id, _source_id, separation in matches] == pytest.approx(
        [0.1, 1.1], abs=1e-8
    )


def test_equal_separation_ties_are_deterministic_by_detection_then_source_order():
    rows = [_row("8", 0.0, 1.0 / 3600.0), _row("2", 0.0, -1.0 / 3600.0)]
    outcomes = []
    for ordered_rows in (rows, list(reversed(rows))):
        catalog = gq.GaiaWorkingCatalog(ordered_rows)
        outcomes.append(
            catalog.match_detections([20, 10], [0.0, 0.0], [0.0, 0.0], radius_arcsec=2.0)
        )

    assert outcomes[0] == outcomes[1]
    assert [(detection_id, source_id) for detection_id, source_id, _sep in outcomes[0]] == [
        (20, "2"),
        (10, "8"),
    ]


def test_working_catalog_match_handles_ra_wrap_and_poles():
    catalog = gq.GaiaWorkingCatalog(
        [_row("1", 359.9999, 0.0), _row("2", 200.0, 90.0)]
    )

    matches = catalog.match_detections(
        [101, 202],
        [0.0001, 10.0],
        [0.0, 90.0],
        radius_arcsec=1.0,
    )

    assert [(detection_id, source_id) for detection_id, source_id, _sep in matches] == [
        (202, "2"),
        (101, "1"),
    ]
    assert matches[0][2] < 1e-8
    assert matches[1][2] == pytest.approx(0.72, abs=1e-7)


def test_working_catalog_match_uses_an_inclusive_radius_cutoff():
    catalog = gq.GaiaWorkingCatalog([_row("1", 1.0 / 3600.0, 0.0)])
    boundary = _sky_sep_arcsec(0.0, 0.0, 1.0 / 3600.0, 0.0)
    assert boundary is not None

    at_boundary = catalog.match_detections(
        [1], [0.0], [0.0], radius_arcsec=boundary
    )
    just_outside = catalog.match_detections(
        [1], [0.0], [0.0], radius_arcsec=float(np.nextafter(boundary, 0.0))
    )

    assert at_boundary[0][0:2] == (1, "1")
    assert at_boundary[0][2] == boundary
    assert just_outside == []


def test_spatial_match_agrees_with_small_exhaustive_greedy_oracle():
    catalog = gq.GaiaWorkingCatalog(
        [
            _row("9007199254740993", 12.0, 0.0),
            _row("12", 12.0 + 0.8 / 3600.0, 0.0),
            _row("3", 359.999, 0.0),
            _row("15", 0.0002, 0.0),
            _row("7", 20.0, 89.9999),
            _row("21", 20.0 + 0.4 / 3600.0, 89.9999),
            _row("44", 123.0, -30.0),
        ]
    )
    detection_ids = [41, 5, 22, 6, 9, 17]
    ra_deg = [
        12.0 + 0.1 / 3600.0,
        12.0 + 0.6 / 3600.0,
        359.9995,
        0.0001,
        20.0 + 0.1 / 3600.0,
        123.0,
    ]
    dec_deg = [0.0, 0.0, 0.0, 0.0, 89.9999, -30.0]
    radius_arcsec = 2.0

    expected = _exhaustive_greedy_oracle(
        catalog, detection_ids, ra_deg, dec_deg, radius_arcsec
    )
    actual = catalog.match_detections(
        detection_ids, ra_deg, dec_deg, radius_arcsec=radius_arcsec
    )

    assert [(det_id, source_id) for det_id, source_id, _sep in actual] == [
        (det_id, source_id) for det_id, source_id, _sep in expected
    ]
    assert [sep for _det_id, _source_id, sep in actual] == pytest.approx(
        [sep for _det_id, _source_id, sep in expected], abs=1e-12
    )


def test_spatial_candidate_bound_avoids_all_detection_source_comparisons():
    source_count = 100
    catalog = gq.GaiaWorkingCatalog(
        _row(str(index + 1), float(index), 0.0) for index in range(source_count)
    )
    ra_deg = [float(index) for index in range(source_count)]

    matches = catalog.match_detections(
        list(range(source_count)),
        ra_deg,
        [0.0] * source_count,
        radius_arcsec=0.01,
        max_pair_comparisons=source_count,
    )

    assert len(matches) == source_count
    assert source_count * source_count > source_count
    assert all(separation == 0.0 for _detection_id, _source_id, separation in matches)


def test_match_rejects_duplicate_current_frame_detection_ids():
    catalog = gq.GaiaWorkingCatalog([_row("1", 0.0, 0.0)])

    with pytest.raises(ValueError, match="detection IDs must be unique"):
        catalog.match_detections([4, 4], [0.0, 0.0], [0.0, 0.0], radius_arcsec=1.0)
