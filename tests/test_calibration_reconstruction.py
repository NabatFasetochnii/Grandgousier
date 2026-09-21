from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from grandgousier.calibration.reconstruction import (
    ReconstructionStateError,
    _apply_aperture_correction,
    _apply_aperture_zp,
    _apply_color_calibration,
    _apply_ensemble,
    _apply_stage,
)


def _base_state() -> dict[tuple[int, int], dict]:
    return {
        (10, 100): {
            "object_id": 10,
            "frame_id": 100,
            "x": 50.0,
            "y": 25.0,
            "flux": 1000.0,
            "flux_err": 20.0,
            "mag_inst": 15.0,
            "mag_cal": None,
            "mag_err": 0.02,
            "mag_err_random": 0.02,
            "mag_err_systematic": None,
            "aperture_radius_pix": 3.0,
            "local_background": 100.0,
            "local_zp_correction": None,
            "centroid_dx_pix": 0.1,
            "centroid_dy_pix": -0.1,
            "flags": {"catalog_name": "cat"},
        }
    }


def test_reconstruction_aperture_zp_ensemble_color_chain() -> None:
    states = _base_state()
    ap_term = SimpleNamespace(
        frame_id=100,
        accepted=True,
        model_name="plane",
        coefficient_0=0.1,
        coefficient_x=0.02,
        coefficient_y=-0.01,
        details={
            "image_width": 101,
            "image_height": 51,
            "pivot_xnorm": 0.0,
            "pivot_ynorm": 0.0,
            "covariance": [[0.0, 0.0, 0.0], [0.0, 0.0004, 0.0], [0.0, 0.0, 0.0009]],
        },
    )
    states = _apply_aperture_correction(states, [ap_term])
    row = states[(10, 100)]
    # x and y are both detector pivots here, so only the constant correction remains.
    assert row["mag_inst"] == pytest.approx(15.1)
    assert row["flux"] == pytest.approx(1000.0 * 10.0 ** (-0.4 * 0.1))
    assert row["mag_err_random"] == pytest.approx(0.02)

    zp_term = SimpleNamespace(frame_id=100, accepted=True, offset_mag=2.5)
    states = _apply_aperture_zp(states, [zp_term], {"catalog_name": "cat"})
    row = states[(10, 100)]
    assert row["mag_cal"] == pytest.approx(17.6)
    assert row["local_zp_correction"] == pytest.approx(2.5)

    ensemble_term = SimpleNamespace(frame_id=100, accepted=True, offset_mag=0.15)
    states = _apply_ensemble(
        states,
        [ensemble_term],
        {"catalog_name": "cat", "prefer_mag_cal": True},
    )
    row = states[(10, 100)]
    assert row["mag_cal"] == pytest.approx(17.45)
    assert row["local_zp_correction"] == pytest.approx(2.35)

    color_object = SimpleNamespace(
        object_id=10,
        details={
            "applied_color_value": 0.8,
            "color_missing": False,
            "systematic_error": 0.03,
        },
    )
    states = _apply_color_calibration(
        states,
        [color_object],
        {"transform_offset": 0.2, "color_term": -0.1, "color0": 0.5},
    )
    row = states[(10, 100)]
    assert row["mag_cal"] == pytest.approx(17.62)
    assert row["mag_err"] == pytest.approx(0.02)
    assert row["mag_err_random"] == pytest.approx(0.02)
    assert row["mag_err_systematic"] == pytest.approx(0.03)


def test_aperture_plane_propagates_only_differential_slope_error() -> None:
    states = _base_state()
    states[(10, 100)]["x"] = 100.0
    states[(10, 100)]["y"] = 50.0
    term = SimpleNamespace(
        frame_id=100,
        accepted=True,
        model_name="plane",
        coefficient_0=0.0,
        coefficient_x=0.0,
        coefficient_y=0.0,
        details={
            "image_width": 101,
            "image_height": 51,
            "pivot_xnorm": 0.0,
            "pivot_ynorm": 0.0,
            "covariance": [[999.0, 0.0, 0.0], [0.0, 0.0004, 0.0], [0.0, 0.0, 0.0009]],
        },
    )
    out = _apply_aperture_correction(states, [term])[(10, 100)]
    expected_diff = math.sqrt(0.0004 + 0.0009)
    assert out["mag_err_random"] == pytest.approx(math.sqrt(0.02**2 + expected_diff**2))


def test_reconstruction_filters_rejected_aperture_zp_frames() -> None:
    states = _base_state()
    rejected = SimpleNamespace(frame_id=100, accepted=False, offset_mag=2.5)
    assert _apply_aperture_zp(states, [rejected], {"catalog_name": "cat"}) == {}


def test_color_reconstruction_requires_application_term_for_every_object() -> None:
    with pytest.raises(ReconstructionStateError, match="missing for 1 objects"):
        _apply_color_calibration(
            _base_state(),
            [],
            {"transform_offset": 0.0, "color_term": 0.0, "color0": 0.5},
        )


def test_color_missing_reference_uses_explicit_pivot_state() -> None:
    states = _base_state()
    states[(10, 100)]["mag_cal"] = 17.0
    term = SimpleNamespace(
        object_id=10,
        details={
            "applied_color_value": 0.5,
            "color_missing": True,
            "systematic_error": 0.07,
        },
    )
    out = _apply_color_calibration(
        states,
        [term],
        {"transform_offset": 0.25, "color_term": 0.3, "color0": 0.5},
    )[(10, 100)]
    assert out["mag_cal"] == pytest.approx(17.25)
    assert out["mag_err_systematic"] == pytest.approx(0.07)


def test_color_reconstruction_respects_identity_catalog_scope() -> None:
    states = _base_state()
    other = dict(states[(10, 100)])
    other["object_id"] = 11
    other["frame_id"] = 101
    other["mag_cal"] = 18.0
    other["flags"] = {"catalog_name": "other"}
    states[(11, 101)] = other
    states[(10, 100)]["mag_cal"] = 17.0
    term = SimpleNamespace(
        object_id=10,
        details={"applied_color_value": 0.5, "color_missing": False, "systematic_error": 0.02},
    )
    out = _apply_color_calibration(
        states,
        [term],
        {"transform_offset": 0.1, "color_term": 0.0, "color0": 0.5},
        {"identity_catalog_name": "cat"},
    )
    assert set(out) == {(10, 100)}


def test_normalized_residual_floor_provenance_is_projected_during_reconstruction() -> None:
    states = _base_state()
    states[(10, 100)]["mag_cal"] = 17.0
    states[(10, 100)]["flags"]["random_error_physical_mag_err"] = 0.015
    frame_term = SimpleNamespace(
        frame_id=100,
        accepted=True,
        offset_mag=0.1,
        n_reference=12,
        scatter_mad=0.02,
    )
    iteration = SimpleNamespace(
        parameters={"catalog_name": "cat", "prefer_mag_cal": True},
        metrics={
            "residual_noise_floor_algorithm": "reference_lower_envelope_frame_common_v2",
            "residual_noise_floor_error_basis": "physical_detector_error",
            "residual_noise_floor_mag": 0.01,
            "residual_noise_frame_common_sigma_mag": 0.004,
            "residual_noise_frame_common_supported": True,
            "residual_noise_frame_common_support_status": "SUPPORTED",
            "residual_noise_floor_after_frame_common_mag": 0.008,
            "residual_noise_independent_floor_mag": 0.007,
        },
    )
    out = _apply_stage(
        states,
        product_kind="ENSEMBLE",
        iteration=iteration,
        frame_terms=[frame_term],
        object_terms=[],
    )[(10, 100)]
    flags = out["flags"]
    assert flags["mag_err_epoch_floor"] == pytest.approx(0.01)
    assert flags["mag_err_detector"] == pytest.approx(0.015)
    assert flags["mag_err_effective_detector_only"] == pytest.approx(math.hypot(0.015, 0.01))
    assert flags["mag_err_effective_total"] == pytest.approx(math.hypot(0.02, 0.01))
    assert flags["residual_noise_frame_common_supported"] is True


def test_virtual_calibration_row_keeps_base_measurement_identity() -> None:
    from grandgousier.photometry.calibration_view import _row_from_state

    state = _base_state()[(10, 100)]
    state["measurement_id"] = 321
    state["base_measurement_id"] = 123
    row = _row_from_state(state)
    assert row.measurement_id == 321
    assert row.base_measurement_id == 123
