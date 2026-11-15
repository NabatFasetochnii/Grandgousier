from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from grandgousier.astrometry.gaia_wcs_com import (
    GAIA_WCS_COM_ALGORITHM,
    GAIA_WCS_COM_POLICY,
    GaiaWcsComCenter,
    build_gaia_wcs_com_refinements,
    compose_final_center,
)
from grandgousier.catalogs.gaia_query import GaiaWorkingCatalog
from grandgousier.photometry import forced_aperture
from grandgousier.pipeline.runner import _working_catalog_neighbor_completeness_proven
from grandgousier.quality.detection_catalog_qc import (
    CatalogMatchLink,
    CatalogMatchSource,
    DetectionMatchInput,
    _match_frame,
)


def _frame():
    return SimpleNamespace(
        frame_id=7,
        header_json={"FWHM": 2.0},
        instrument_unit=SimpleNamespace(pixel_scale_arcsec=1.0),
        quality_flags={},
    )


def _fixture(
    n: int = 20,
    *,
    dx: float = 0.25,
    dy: float = -0.1,
    neighbor: float = 30.0,
):
    links = []
    rows = []
    source_ids = {}
    neighbors = {}
    for index in range(n):
        catalog_source_id = 1000 + index
        source_id = str(9000 + index)
        links.append(
            SimpleNamespace(
                frame_catalog_link_id=2000 + index,
                frame_id=7,
                catalog_source_id=catalog_source_id,
                x=10.0 + index,
                y=20.0 + index,
            )
        )
        source_ids[catalog_source_id] = source_id
        neighbors[source_id] = neighbor
        rows.append(
            {
                "frame_id": 7,
                "source_detection_id": 3000 + index,
                "catalog_source_id": catalog_source_id,
                "source_id": source_id,
                "source_detection_run_id": 11,
                "catalog_match_run_id": 22,
                "match_status": "MATCHED",
                "x": 10.0 + index + dx,
                "y": 20.0 + index + dy,
                "flags": {},
            }
        )
    return links, rows, source_ids, neighbors


def _candidate_evidence(
    rows,
    *,
    radius=2.0,
    detection_count_by_index=None,
    catalog_count_by_index=None,
):
    accepted_pairs = []
    for index, row in enumerate(rows):
        if (
            row.get("source_detection_run_id") != 11
            or row.get("catalog_match_run_id") != 22
            or row.get("match_status") != "MATCHED"
        ):
            continue
        accepted_pairs.append(
            {
                "source_detection_id": row["source_detection_id"],
                "catalog_source_id": row["catalog_source_id"],
                "detection_candidate_count": (detection_count_by_index or {}).get(
                    index, 1
                ),
                "catalog_candidate_count": (catalog_count_by_index or {}).get(index, 1),
            }
        )
    return {
        7: {
            "frame_id": 7,
            "source_detection_run_id": 11,
            "catalog_match_run_id": 22,
            "radius_arcsec": radius,
            "complete": True,
            "accepted_pairs": accepted_pairs,
        }
    }


def _refinements(
    links,
    rows,
    source_ids,
    neighbors,
    *,
    complete=True,
    policy=None,
    query_margin_arcmin=1.0,
    frame=None,
    candidate_pair_evidence_by_frame=None,
):
    if candidate_pair_evidence_by_frame is None:
        candidate_pair_evidence_by_frame = _candidate_evidence(rows)
    return build_gaia_wcs_com_refinements(
        {7: links},
        {7: rows},
        {7: _frame() if frame is None else frame},
        candidate_pair_evidence_by_frame=candidate_pair_evidence_by_frame,
        source_ids_by_catalog_source_id=source_ids,
        nearest_neighbor_by_source_id=neighbors,
        neighbor_completeness_proven=complete,
        neighbor_query_margin_arcmin=query_margin_arcmin,
        source_detection_run_id=11,
        catalog_match_run_id=22,
        aperture_radius_pix=3.0,
        policy=policy,
    )


def test_frame_shift_and_local_com_use_exact_unsaturated_matches():
    links, rows, source_ids, neighbors = _fixture()
    refinements = _refinements(links, rows, source_ids, neighbors)

    first = refinements[links[0].frame_catalog_link_id]
    assert first.method == "local_com"
    assert first.x == pytest.approx(links[0].x + 0.25)
    assert first.y == pytest.approx(links[0].y - 0.1)
    assert first.provenance["frame_shift"]["accepted"] is True
    assert first.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 20
    assert first.local_dx_pix == pytest.approx(0.0)
    assert first.provenance["position_method"] == "local_com"
    assert first.provenance["position_fallback_reason"] is None
    assert first.provenance["source_detection_run_id"] == 11
    assert first.provenance["catalog_match_run_id"] == 22
    assert first.provenance["source_detection_id"] == 3000
    assert first.provenance["catalog_source_id"] == 1000
    assert first.provenance["source_id"] == "9000"


def test_unproven_neighbor_completeness_uses_frame_shift_not_local_com():
    links, rows, source_ids, neighbors = _fixture()
    refinements = _refinements(links, rows, source_ids, neighbors, complete=False)

    first = refinements[links[0].frame_catalog_link_id]
    assert first.method == "frame_shift"
    assert first.x == pytest.approx(links[0].x + 0.25)
    assert first.provenance["local_com_rejection_reason"] == "neighbor_completeness_unproven"


def test_saturated_and_blended_sources_only_veto_local_com():
    links, rows, source_ids, neighbors = _fixture(21)
    rows[0]["flags"] = {"saturated_or_near_saturated": True}
    neighbors[source_ids[links[1].catalog_source_id]] = 4.0
    refinements = _refinements(links, rows, source_ids, neighbors)

    saturated = refinements[links[0].frame_catalog_link_id]
    blended = refinements[links[1].frame_catalog_link_id]
    assert saturated.method == "frame_shift"
    assert saturated.provenance["local_com_rejection_reason"] == "saturated_detection"
    assert blended.method == "frame_shift"
    assert blended.provenance["local_com_rejection_reason"] == "blend_veto"
    assert refinements[links[2].frame_catalog_link_id].method == "local_com"
    assert saturated.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 20


def test_dynamic_crowding_veto_matches_qc_threshold_factors():
    links, rows, source_ids, neighbors = _fixture(21)
    _frame().header_json["FWHM"] = 4.0
    frame = _frame()
    frame.header_json["FWHM"] = 4.0
    neighbors[source_ids[links[0].catalog_source_id]] = 7.0
    result = _refinements(links, rows, source_ids, neighbors, frame=frame)
    first = result[links[0].frame_catalog_link_id]
    diagnostics = first.provenance["local_com_neighbor_diagnostics"]
    assert diagnostics["dynamic_blend_arcsec"] == pytest.approx(6.0)
    assert diagnostics["dynamic_crowded_arcsec"] == pytest.approx(9.0)
    assert diagnostics["crowded"] is True
    assert diagnostics["dynamic_crowded"] is True
    assert first.method == "frame_shift"


def test_static_crowding_veto_applies_without_dynamic_thresholds():
    links, rows, source_ids, neighbors = _fixture()
    neighbors[source_ids[links[0].catalog_source_id]] = 7.0

    refinements = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        policy={"dynamic_separation": False},
    )

    first = refinements[links[0].frame_catalog_link_id]
    diagnostics = first.provenance["local_com_neighbor_diagnostics"]
    assert diagnostics["crowded"] is True
    assert diagnostics["blended"] is False
    assert "dynamic_blend_arcsec" not in diagnostics
    assert "dynamic_crowded_arcsec" not in diagnostics
    assert first.method == "frame_shift"
    assert first.provenance["local_com_rejection_reason"] == "crowded_neighbor_veto"
    assert first.provenance["position_method"] == "frame_shift"
    assert first.provenance["position_fallback_reason"] == "crowded_neighbor_veto"


def test_zero_dynamic_blend_threshold_has_no_separation_index():
    links, rows, source_ids, neighbors = _fixture()

    refinements = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        policy={"dynamic_seeing_factor": 0.0, "dynamic_aperture_factor": 0.0},
    )

    first = refinements[links[0].frame_catalog_link_id]
    diagnostics = first.provenance["local_com_neighbor_diagnostics"]
    assert diagnostics["dynamic_blend_arcsec"] == 0.0
    assert diagnostics["dynamic_blend_risk"] is False
    assert diagnostics["separation_index"] is None
    assert first.method == "local_com"


def test_stale_and_ambiguous_match_mappings_are_rejected():
    links, rows, source_ids, neighbors = _fixture(20)
    rows[0]["source_detection_run_id"] = 10
    stale = _refinements(links, rows, source_ids, neighbors)
    assert stale[links[0].frame_catalog_link_id].method == "wcs"
    assert (
        stale[links[0].frame_catalog_link_id].provenance[
            "local_com_rejection_reason"
        ]
        == "no_current_accepted_match"
    )

    links, rows, source_ids, neighbors = _fixture(20)
    duplicate = dict(rows[0])
    duplicate["source_detection_id"] = 99999
    rows.append(duplicate)
    ambiguous = _refinements(links, rows, source_ids, neighbors)
    assert ambiguous[links[0].frame_catalog_link_id].method == "wcs"
    assert (
        ambiguous[links[0].frame_catalog_link_id].provenance[
            "local_com_rejection_reason"
        ]
        == "ambiguous_current_match"
    )


def test_second_detection_competing_for_isolated_catalog_pair_is_excluded_from_shift():
    links, rows, source_ids, neighbors = _fixture(21)
    evidence = _candidate_evidence(rows, catalog_count_by_index={0: 2})

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        candidate_pair_evidence_by_frame=evidence,
    )[links[0].frame_catalog_link_id]

    assert first.method == "frame_shift"
    assert first.provenance["local_com_rejection_reason"] == (
        "ambiguous_candidate_match"
    )
    assert first.provenance["frame_shift"]["accepted"] is True
    assert first.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 20


def test_unique_candidate_pair_control_keeps_local_com_enabled():
    links, rows, source_ids, neighbors = _fixture(1)

    first = _refinements(links, rows, source_ids, neighbors)[
        links[0].frame_catalog_link_id
    ]

    assert first.method == "local_com"
    assert first.provenance["local_com_rejection_reason"] is None


@pytest.mark.parametrize(
    (
        "detection_offsets",
        "catalog_offsets",
        "expected_detection_count",
        "expected_catalog_count",
    ),
    [
        ([0.0, 0.2], [0.0], 1, 2),
        ([0.0], [0.0, 0.2], 2, 1),
        ([0.0], [0.0], 1, 1),
    ],
    ids=["competing-detection", "competing-catalog", "unique-control"],
)
def test_matcher_candidate_evidence_counts_alternatives(
    detection_offsets,
    catalog_offsets,
    expected_detection_count,
    expected_catalog_count,
):
    detections = [
        DetectionMatchInput(
            source_detection_id=101 + index,
            frame_id=7,
            x=10.0 + index,
            y=20.0,
            ra_deg=offset / 3600.0,
            dec_deg=0.0,
            peak_adu=None,
            flux=100.0,
            snr=10.0,
            algorithm="numpy_localmax_v1",
        )
        for index, offset in enumerate(detection_offsets)
    ]
    catalog_rows = [
        (
            CatalogMatchSource(
                catalog_source_id=201 + index,
                source_id=str(9000 + index),
                ra_deg=offset / 3600.0,
                dec_deg=0.0,
                ref_mags={"G": 15.0},
            ),
            CatalogMatchLink(
                frame_id=7,
                catalog_source_id=201 + index,
                x=10.0 + index,
                y=20.0,
            ),
        )
        for index, offset in enumerate(catalog_offsets)
    ]
    evidence = []

    matches = _match_frame(
        frame=SimpleNamespace(instrument_unit=None),
        detections=detections,
        catalog_rows=catalog_rows,
        radius_arcsec=1.0,
        mag_key="G",
        accepted_pair_evidence=evidence,
    )

    assert len(matches) == len(evidence) == 1
    assert evidence[0]["detection_candidate_count"] == expected_detection_count
    assert evidence[0]["catalog_candidate_count"] == expected_catalog_count


def test_competing_catalog_source_for_one_detection_is_ambiguous():
    links, rows, source_ids, neighbors = _fixture(21)
    evidence = _candidate_evidence(rows, detection_count_by_index={0: 2})

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        candidate_pair_evidence_by_frame=evidence,
    )[links[0].frame_catalog_link_id]

    assert first.method == "frame_shift"
    assert first.provenance["local_com_rejection_reason"] == (
        "ambiguous_candidate_match"
    )
    assert first.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 20


def test_missing_match_radius_fails_local_com_and_frame_shift_closed():
    links, rows, source_ids, neighbors = _fixture(21)
    evidence = _candidate_evidence(rows, radius=None)

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        candidate_pair_evidence_by_frame=evidence,
    )[links[0].frame_catalog_link_id]

    assert first.method == "wcs"
    assert first.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 0
    assert (
        first.provenance["local_com_rejection_reason"]
        == "candidate_radius_unavailable_or_invalid"
    )


def test_missing_candidate_evidence_fails_closed():
    links, rows, source_ids, neighbors = _fixture(21)

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        candidate_pair_evidence_by_frame={},
    )[links[0].frame_catalog_link_id]

    assert first.method == "wcs"
    assert first.provenance["frame_shift"]["n_current_unsaturated_accepted_matches"] == 0
    assert first.provenance["local_com_rejection_reason"] == (
        "candidate_evidence_unavailable"
    )


def test_frame_shift_scatter_and_magnitude_gates_fail_closed():
    links, rows, source_ids, neighbors = _fixture(20, dx=2.1, dy=0.0)
    large = _refinements(links, rows, source_ids, neighbors)
    assert large[links[0].frame_catalog_link_id].method == "wcs"
    assert (
        large[links[0].frame_catalog_link_id].provenance["frame_shift"]["reason"]
        == "absolute_frame_shift_exceeds_limit"
    )

    links, rows, source_ids, neighbors = _fixture(20, dx=0.0, dy=0.0)
    for row, value in zip(rows, [0.0] * 10 + [1.0] * 10, strict=True):
        row["x"] += value
    scattered = _refinements(links, rows, source_ids, neighbors)
    assert scattered[links[0].frame_catalog_link_id].provenance["frame_shift"]["reason"] == (
        "robust_scatter_exceeds_limit"
    )


def test_local_shift_limit_falls_back_without_rejecting_measurement():
    links, rows, source_ids, neighbors = _fixture(21)
    rows[0]["x"] += 3.0
    refinements = _refinements(links, rows, source_ids, neighbors)
    first = refinements[links[0].frame_catalog_link_id]
    assert first.method == "frame_shift"
    assert first.provenance["local_com_rejection_reason"] == "local_com_shift_exceeds_limit"
    assert first.x == pytest.approx(links[0].x + 0.25)


def test_local_com_limit_uses_total_offset_from_original_wcs_link():
    links, rows, source_ids, neighbors = _fixture(21, dx=1.5, dy=0.0)
    rows[0]["x"] += 0.9

    refinements = _refinements(links, rows, source_ids, neighbors)

    first = refinements[links[0].frame_catalog_link_id]
    diagnostics = first.provenance["local_com_neighbor_diagnostics"]
    assert first.provenance["frame_shift"]["accepted"] is True
    assert first.method == "frame_shift"
    assert first.x == pytest.approx(links[0].x + 1.5)
    assert diagnostics["original_link_offset_magnitude_pix"] == pytest.approx(2.4)
    assert diagnostics["residual_magnitude_pix"] == pytest.approx(0.9)
    assert first.provenance["local_com_rejection_reason"] == "local_com_shift_exceeds_limit"


def test_local_shift_limit_override_is_applied_and_recorded():
    links, rows, source_ids, neighbors = _fixture(21)
    rows[0]["x"] += 0.6

    refinements = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        policy={"local_com_max_shift_pix": 0.2},
    )

    first = refinements[links[0].frame_catalog_link_id]
    assert first.method == "frame_shift"
    assert first.x == pytest.approx(links[0].x + 0.25)
    assert first.provenance["local_com_rejection_reason"] == "local_com_shift_exceeds_limit"
    assert first.provenance["policy"]["local_com_max_shift_pix"] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "existing_parameters",
    [
        {"astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM},
        {
            "astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM,
            "astrometric_centering_policy": {
                **GAIA_WCS_COM_POLICY,
                "local_com_max_shift_pix": 1.0,
            },
        },
    ],
    ids=["missing-policy", "different-policy"],
)
def test_mutable_calibration_rejects_incompatible_centering_policy(
    monkeypatch, existing_parameters
):
    existing = SimpleNamespace(
        calibration_version_id=91,
        lifecycle_status="LEGACY_MUTABLE",
        parameters=existing_parameters,
    )

    class _Session:
        def scalar(self, _statement):
            return existing

    monkeypatch.setattr(
        forced_aperture,
        "prepare_calibration_version_for_write",
        lambda *_args, **_kwargs: pytest.fail("incompatible calibration must not be reused"),
    )

    with pytest.raises(ValueError, match="incompatible astrometric centering provenance"):
        forced_aperture._get_or_create_calibration_version(
            _Session(),
            name="center-v10",
            instrument_unit_id=1,
            parameters={
                "astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM,
                "astrometric_centering_policy": GAIA_WCS_COM_POLICY,
            },
        )


def test_mutable_calibration_with_matching_centering_policy_is_reused(monkeypatch):
    existing = SimpleNamespace(
        calibration_version_id=91,
        lifecycle_status="LEGACY_MUTABLE",
        parameters={
            "astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM,
            "astrometric_centering_policy": GAIA_WCS_COM_POLICY,
        },
        product_kind="BASE_PHOTOMETRY",
        product_role="BASE",
        materialization_kind="FULL_MEASUREMENTS",
    )

    class _Session:
        def scalar(self, _statement):
            return existing

        def flush(self):
            return None

    monkeypatch.setattr(
        forced_aperture,
        "prepare_calibration_version_for_write",
        lambda *_args, **_kwargs: existing,
    )

    result = forced_aperture._get_or_create_calibration_version(
        _Session(),
        name="center-v10",
        instrument_unit_id=1,
        parameters={
            "astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM,
            "astrometric_centering_policy": GAIA_WCS_COM_POLICY,
        },
    )

    assert result is existing


def test_final_center_composes_psf_residual_and_full_link_offset():
    final_x, final_y, full_dx, full_dy, provenance = compose_final_center(
        original_x=10.0,
        original_y=20.0,
        base_x=11.0,
        base_y=19.5,
        psf_residual_dx_pix=0.25,
        psf_residual_dy_pix=-0.125,
        provenance={"method": "local_com", "frame_shift_dx_pix": 0.4},
    )
    assert (final_x, final_y) == pytest.approx((11.25, 19.375))
    assert (full_dx, full_dy) == pytest.approx((1.25, -0.625))
    assert provenance["psf_residual_dx_pix"] == pytest.approx(0.25)
    assert provenance["full_offset_dx_pix"] == pytest.approx(full_dx)


def test_working_catalog_neighbors_are_in_memory_and_completeness_is_provenance_gated():
    catalog = GaiaWorkingCatalog(
        [
            {"source_id": "101", "ra": 10.0, "dec": 0.0, "phot_g_mean_mag": 14.0},
            {"source_id": "202", "ra": 10.001, "dec": 0.0, "phot_g_mean_mag": 15.0},
            {"source_id": "303", "ra": 11.0, "dec": 0.0, "phot_g_mean_mag": 16.0},
        ]
    )
    neighbors = catalog.nearest_neighbor_separations(["101", "303", "999"])
    assert neighbors["101"] == pytest.approx(3.6, abs=0.01)
    assert neighbors["303"] > 1000.0
    assert neighbors["999"] is None

    provenance = {
        "catalog_name": "gaia_dr3",
        "query_mode": "field_union",
        "fetch_status": "SUCCEEDED_COMPLETE",
        "fetch_complete": True,
        "coverage_complete": True,
        "new_source_search_performed": True,
        "partial_rows_discarded": False,
        "magnitude_limit": None,
        "field_id": 7,
        "field_frame_count": 24,
        "margin_arcmin": 1.0,
        "unique_sources": len(catalog),
    }
    assert _working_catalog_neighbor_completeness_proven(
        catalog, provenance, field_id=7, selected_frame_count=12
    )
    insufficient_static_margin = dict(provenance, margin_arcmin=0.15)
    assert not _working_catalog_neighbor_completeness_proven(
        catalog, insufficient_static_margin, field_id=7, selected_frame_count=12
    )
    configured_static_margin = dict(provenance, margin_arcmin=0.19)
    assert not _working_catalog_neighbor_completeness_proven(
        catalog,
        configured_static_margin,
        field_id=7,
        selected_frame_count=12,
        crowded_arcsec=12.0,
        blended_arcsec=6.0,
    )
    offline = dict(provenance, new_source_search_performed=False, coverage_complete=False)
    assert not _working_catalog_neighbor_completeness_proven(
        catalog, offline, field_id=7, selected_frame_count=12
    )
    magnitude_limited = dict(provenance, magnitude_limit=19.0)
    assert not _working_catalog_neighbor_completeness_proven(
        catalog, magnitude_limited, field_id=7, selected_frame_count=12
    )
    assert not _working_catalog_neighbor_completeness_proven(
        catalog, provenance, field_id=7, selected_frame_count=0
    )
    fractional_scope = dict(provenance, field_frame_count=12.5)
    assert not _working_catalog_neighbor_completeness_proven(
        catalog, fractional_scope, field_id=7, selected_frame_count=12
    )
    catalog.close()


def test_static_margin_shortfall_preserves_exact_frame_shift():
    links, rows, source_ids, neighbors = _fixture()

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        query_margin_arcmin=0.15,
    )[links[0].frame_catalog_link_id]

    assert first.method == "frame_shift"
    assert first.provenance["frame_shift_applied"] is True
    assert (
        first.provenance["local_com_rejection_reason"]
        == "neighbor_query_margin_insufficient"
    )


def test_dynamic_guard_must_fit_the_query_pad_with_sufficient_pad_control():
    links, rows, source_ids, neighbors = _fixture()
    policy = {
        "crowded_arcsec": 2.0,
        "blended_arcsec": 1.0,
        "dynamic_crowding_factor": 4.0,
    }

    insufficient = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        policy=policy,
        query_margin_arcmin=0.1,
    )[links[0].frame_catalog_link_id]
    assert insufficient.method == "frame_shift"
    assert insufficient.provenance["frame_shift_applied"] is True
    assert insufficient.provenance["local_com_neighbor_diagnostics"][
        "dynamic_crowded_arcsec"
    ] == pytest.approx(12.0)
    assert insufficient.provenance["local_com_neighbor_diagnostics"][
        "required_neighbor_guard_arcsec"
    ] == pytest.approx(12.0)
    assert (
        insufficient.provenance["local_com_rejection_reason"]
        == "neighbor_query_margin_insufficient"
    )

    sufficient = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        policy=policy,
        query_margin_arcmin=0.25,
    )[links[0].frame_catalog_link_id]
    assert sufficient.method == "local_com"
    assert sufficient.provenance["local_com_neighbor_diagnostics"][
        "neighbor_margin_sufficient"
    ] is True


@pytest.mark.parametrize(
    ("match_count", "expected_method"),
    [(20, "frame_shift"), (19, "wcs")],
)
def test_unavailable_dynamic_guard_disables_only_local_com(
    match_count, expected_method
):
    links, rows, source_ids, neighbors = _fixture(match_count)
    frame_without_dynamic_inputs = SimpleNamespace(
        frame_id=7,
        header_json={},
        instrument_unit=None,
        quality_flags={},
    )

    first = _refinements(
        links,
        rows,
        source_ids,
        neighbors,
        frame=frame_without_dynamic_inputs,
    )[links[0].frame_catalog_link_id]

    assert first.method == expected_method
    assert first.provenance["frame_shift_applied"] is (expected_method == "frame_shift")
    assert (
        first.provenance["local_com_rejection_reason"]
        == "dynamic_neighbor_guard_unavailable"
    )


def test_missing_neighbor_entry_blocks_local_com_even_with_complete_catalog_claim():
    links, _rows, source_ids, neighbors = _fixture()
    neighbors.pop(source_ids[links[0].catalog_source_id])

    first = _refinements(links, _rows, source_ids, neighbors)[
        links[0].frame_catalog_link_id
    ]

    assert first.method == "frame_shift"
    assert first.x == pytest.approx(links[0].x + 0.25)
    assert (
        first.provenance["local_com_rejection_reason"]
        == "neighbor_identity_absent_from_complete_catalog"
    )


@pytest.mark.parametrize(
    "photometry_estimator",
    ["circular", "adaptive_ellipse", "spatial_psf", "empirical_psf"],
)
@pytest.mark.parametrize(
    ("position_method", "fallback_reason"),
    [
        ("local_com", None),
        ("frame_shift", "crowded_neighbor_veto"),
        ("wcs", "insufficient_current_unsaturated_matches"),
    ],
)
def test_forced_photometry_uses_and_persists_refined_center_and_run_provenance(
    monkeypatch, tmp_path, photometry_estimator, position_method, fallback_reason
):
    raw_path = tmp_path / "center-test.fits"
    raw_path.touch()
    frame = SimpleNamespace(
        frame_id=7,
        raw_path=str(raw_path),
        exptime=10.0,
        airmass=1.0,
        gain=1.0,
        read_noise=2.0,
        header_json={},
        quality_flags={},
        instrument_unit=SimpleNamespace(
            saturation_level_adu=None,
            nominal_gain_e_per_adu=1.0,
            nominal_read_noise_e=2.0,
            pixel_scale_arcsec=1.0,
        ),
    )
    link = forced_aperture.ForcedFrameLink(
        frame_catalog_link_id=71,
        frame_id=7,
        catalog_source_id=17,
        x=10.0,
        y=20.0,
    )
    source = forced_aperture.ForcedCatalogSource(
        catalog_source_id=17,
        catalog_name="gaia_dr3",
        source_id="9000",
        ra_deg=10.0,
        dec_deg=0.0,
        epoch=None,
        pmra_masyr=None,
        pmdec_masyr=None,
        ref_mags={},
        colors={},
    )
    exact_run_ids = {}
    measured_centers = []
    persisted_rows = []
    calibration_parameters = {}

    class _FakeSession:
        def flush(self):
            return None

        def commit(self):
            return None

    class _FakeHduList:
        def __enter__(self):
            return [SimpleNamespace(data=np.ones((32, 32)), header={})]

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        forced_aperture, "_select_frames", lambda *_args, **_kwargs: [frame]
    )
    monkeypatch.setattr(
        forced_aperture,
        "_select_frame_links_bulk",
        lambda *_args, **_kwargs: {7: [(link, source)]},
    )
    monkeypatch.setattr(
        forced_aperture,
        "_get_or_create_calibration_version",
        lambda *_args, **kwargs: calibration_parameters.update(kwargs["parameters"])
        or SimpleNamespace(calibration_version_id=91),
    )
    monkeypatch.setattr(
        forced_aperture,
        "_existing_calibration_measurement_count",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        forced_aperture, "_get_or_create_object_id", lambda *_args, **_kwargs: 55
    )
    monkeypatch.setattr(forced_aperture, "postgres_copy_capable", lambda _session: False)
    monkeypatch.setattr(
        forced_aperture,
        "_select_current_gaia_wcs_com_matches",
        lambda _session, **kwargs: exact_run_ids.update(kwargs) or {7: []},
    )

    def refinements(_links, _matches, _frames, **kwargs):
        assert kwargs["source_detection_run_id"] == 11
        assert kwargs["catalog_match_run_id"] == 22
        return {
            71: GaiaWcsComCenter(
                x=10.25,
                y=19.9,
                frame_dx_pix=0.0,
                frame_dy_pix=0.0,
                local_dx_pix=0.25,
                local_dy_pix=-0.1,
                method=position_method,
                provenance={
                    "algorithm": "gaia_wcs_com_v1",
                    "method": position_method,
                    "position_method": position_method,
                    "position_fallback_reason": fallback_reason,
                    "source_detection_run_id": 11,
                    "catalog_match_run_id": 22,
                },
            )
        }

    monkeypatch.setattr(forced_aperture, "build_gaia_wcs_com_refinements", refinements)
    monkeypatch.setattr(
        forced_aperture, "_load_detection_rows", lambda *_args, **_kwargs: {7: []}
    )
    monkeypatch.setattr(
        forced_aperture, "_build_psf_references", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        forced_aperture, "build_empirical_psf_field", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        forced_aperture.fits, "open", lambda *_args, **_kwargs: _FakeHduList()
    )

    def measure(_data, *, x, y, **_kwargs):
        measured_centers.append((x, y))
        return forced_aperture.ApertureMeasurement(
            flux=100.0,
            flux_err=1.0,
            mag_inst=10.0,
            mag_err=0.01,
            local_background=1.0,
            aperture_radius_pix=3.0,
            flags={},
            error_budget={},
        )

    monkeypatch.setattr(forced_aperture, "measure_one_source", measure)
    monkeypatch.setattr(
        forced_aperture,
        "upsert_measurement_rows",
        lambda _session, rows, **_kwargs: persisted_rows.extend(rows) or 1,
    )

    forced_aperture.run_forced_aperture_photometry(
        _FakeSession(),
        catalog_name="gaia_dr3",
        frame_ids=[7],
        instrument_unit_id=1,
        photometry_estimator=photometry_estimator,
        calibration_name="center-test",
        reuse_existing=False,
        source_detection_run_id=11,
        catalog_match_run_id=22,
        adaptive_min_psf_references=6,
        astrometric_centering_policy={
            "local_com_max_shift_pix": 1.25,
            "frame_shift_min_matches": 24,
            "frame_shift_max_robust_scatter_pix": 0.3,
            "frame_shift_max_absolute_pix": 1.5,
            "crowded_arcsec": 12.0,
            "blended_arcsec": 6.0,
            "dynamic_separation": False,
            "dynamic_seeing_factor": 1.7,
            "dynamic_aperture_factor": 0.8,
            "dynamic_crowding_factor": 1.3,
            "fwhm_header_unit": "pix",
        },
    )

    assert exact_run_ids == {
        "frame_ids": [7],
        "source_detection_run_id": 11,
        "catalog_match_run_id": 22,
    }
    assert measured_centers == [(10.25, 19.9)]
    assert len(persisted_rows) == 1
    assert calibration_parameters["astrometric_centering_policy"][
        "local_com_max_shift_pix"
    ] == pytest.approx(1.25)
    assert calibration_parameters["astrometric_centering_policy"][
        "frame_shift_min_matches"
    ] == 24
    assert calibration_parameters["astrometric_centering_policy"][
        "frame_shift_max_robust_scatter_pix"
    ] == pytest.approx(0.3)
    assert calibration_parameters["astrometric_centering_policy"][
        "frame_shift_max_absolute_pix"
    ] == pytest.approx(1.5)
    assert calibration_parameters["astrometric_centering_policy"][
        "crowded_arcsec"
    ] == pytest.approx(12.0)
    assert calibration_parameters["astrometric_centering_policy"][
        "fwhm_header_unit"
    ] == "pix"
    row = persisted_rows[0]
    assert (row["x"], row["y"]) == pytest.approx((10.25, 19.9))
    assert (row["centroid_dx_pix"], row["centroid_dy_pix"]) == pytest.approx(
        (0.25, -0.1)
    )
    centering = row["flags"]["astrometric_centering"]
    assert centering["method"] == position_method
    assert centering["position_method"] == position_method
    assert centering["position_fallback_reason"] == fallback_reason
    assert centering["source_detection_run_id"] == 11
    assert centering["catalog_match_run_id"] == 22
    assert row["flags"]["photometry_estimator_requested"] == photometry_estimator
    if photometry_estimator == "circular":
        assert "photometry_estimator" not in row["flags"]
    else:
        fallback_estimator_name = (
            "adaptive" if photometry_estimator == "adaptive_ellipse" else photometry_estimator
        )
        assert row["flags"]["photometry_estimator"] == (
            f"circular_fallback_from_{fallback_estimator_name}"
        )
