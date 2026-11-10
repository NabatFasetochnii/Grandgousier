from __future__ import annotations

import math

import pytest

from grandgousier.api import (
    INTERSERIES_SIGN_CONVENTION,
    InterseriesObjectState,
    PairwiseOffset,
    compute_aperture_error_budget,
    compute_interseries_pair_offset,
    solve_interseries_offsets,
)
from grandgousier.photometry.error_model import aperture_error_budget


def test_public_api_import_and_sign_contract() -> None:
    import grandgousier.api as api

    assert api.compute_aperture_error_budget is compute_aperture_error_budget
    assert INTERSERIES_SIGN_CONVENTION == (
        "delta_ab=m_b-m_a; z_a-z_b=delta_ab; m_global=m_science+z_s"
    )


def test_public_error_budget_is_exact_adapter() -> None:
    kwargs = dict(
        net_flux_adu=12000.0,
        local_background_adu_per_pix=110.0,
        annulus_sigma_adu_per_pix=7.5,
        n_aperture_pixels=29,
        n_annulus_pixels=80,
        gain_e_per_adu=1.4,
        read_noise_e=8.3,
        telescope_aperture_m=0.6,
        site_altitude_m=290.0,
        airmass=1.15,
        exptime_s=30.0,
    )
    public = compute_aperture_error_budget(**kwargs)
    internal = aperture_error_budget(
        net_flux_adu=kwargs["net_flux_adu"],
        local_background_adu=kwargs["local_background_adu_per_pix"],
        annulus_sigma_adu=kwargs["annulus_sigma_adu_per_pix"],
        n_aperture_pixels=kwargs["n_aperture_pixels"],
        n_annulus_pixels=kwargs["n_annulus_pixels"],
        gain_e_per_adu=kwargs["gain_e_per_adu"],
        read_noise_e=kwargs["read_noise_e"],
        telescope_aperture_m=kwargs["telescope_aperture_m"],
        site_altitude_m=kwargs["site_altitude_m"],
        airmass=kwargs["airmass"],
        exptime_s=kwargs["exptime_s"],
    )
    assert public == internal


def test_public_error_budget_can_omit_legacy_comparison_without_changing_physics() -> None:
    kwargs = dict(
        net_flux_adu=12000.0,
        local_background_adu_per_pix=110.0,
        annulus_sigma_adu_per_pix=7.5,
        n_aperture_pixels=29,
        n_annulus_pixels=80,
        gain_e_per_adu=1.4,
        read_noise_e=8.3,
    )
    diagnostic = compute_aperture_error_budget(**kwargs)
    bulk = compute_aperture_error_budget(
        **kwargs, include_legacy_comparison=False
    )

    assert bulk.physical_total_variance_adu2 == diagnostic.physical_total_variance_adu2
    assert bulk.physical_mag_err == diagnostic.physical_mag_err
    assert bulk.selected_flux_err_adu == diagnostic.selected_flux_err_adu
    assert bulk.selected_mag_err == diagnostic.selected_mag_err
    assert bulk.legacy_mag_err is None
    assert bulk.physical_to_legacy_mag_err_ratio is None
    assert "legacy_mag_err" not in bulk.to_dict()
    assert "physical_to_legacy_mag_err_ratio" not in bulk.to_dict()


def _state(object_id: int, mag: float, err: float = 0.01, eligible: bool = True):
    return InterseriesObjectState(
        object_id=object_id,
        mag_median=mag,
        representative_err_mag=err,
        reference_eligible=eligible,
    )


def test_public_pair_offset_delegates_to_m2_sign_and_clipping() -> None:
    left = [_state(i, 15.0 + i * 0.01) for i in range(1, 7)]
    right = [_state(i, 15.2 + i * 0.01) for i in range(1, 7)]
    right[-1] = _state(6, 17.0, err=0.01)

    pair = compute_interseries_pair_offset(
        left,
        right,
        left_series_id=10,
        right_series_id=11,
        min_common_objects=5,
        clip_sigma=4.0,
        max_clip_iterations=5,
    )

    assert pair.status == "OK"
    assert pair.left_series_id == 10
    assert pair.right_series_id == 11
    assert pair.n_initial_calibrators == 6
    assert pair.n_used_calibrators == 5
    assert pair.n_rejected_calibrators == 1
    assert pair.delta_mag == pytest.approx(0.2, abs=1e-12)
    assert len(pair.calibrators) == 6
    assert sum(row.used for row in pair.calibrators) == 5


def test_public_pair_offset_rejects_duplicate_object_ids() -> None:
    with pytest.raises(ValueError, match="duplicate object_id=1"):
        compute_interseries_pair_offset(
            [_state(1, 15.0), _state(1, 15.1)],
            [_state(1, 15.2)],
            left_series_id=1,
            right_series_id=2,
            min_common_objects=1,
        )


def test_public_graph_solution_delegates_to_m3() -> None:
    pairs = (
        PairwiseOffset(1, 2, 0.10, 0.01, 0.01, 20, 20, 0, 1, "OK"),
        PairwiseOffset(2, 3, 0.20, 0.01, 0.01, 20, 20, 0, 1, "OK"),
        PairwiseOffset(1, 3, 0.30, 0.01, 0.01, 20, 20, 0, 1, "OK"),
    )
    graph = solve_interseries_offsets((1, 2, 3), pairs, reference_series_id=1)

    assert graph is not None
    assert graph.status == "OK"
    offsets = {row.series_id: row.offset_mag for row in graph.offsets}
    assert offsets[1] == pytest.approx(0.0, abs=1e-12)
    assert offsets[2] == pytest.approx(-0.10, abs=1e-12)
    assert offsets[3] == pytest.approx(-0.30, abs=1e-12)
    assert graph.dof == 1
    assert graph.chi2 == pytest.approx(0.0, abs=1e-20)


def test_public_graph_reference_validation() -> None:
    with pytest.raises(ValueError, match="reference_series_id=99"):
        solve_interseries_offsets(
            (1, 2),
            (PairwiseOffset(1, 2, 0.1, 0.01, 0.01, 20, 20, 0, 1, "OK"),),
            reference_series_id=99,
        )


def test_public_circular_aperture_matches_internal_kernel() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("astropy")
    pytest.importorskip("sqlalchemy")

    from grandgousier.api import measure_circular_aperture
    from grandgousier.photometry.forced_aperture import measure_one_source

    yy, xx = np.indices((31, 31), dtype=float)
    data = np.full((31, 31), 100.0, dtype=float)
    data += 5000.0 * np.exp(-((xx - 15.2) ** 2 + (yy - 14.8) ** 2) / (2.0 * 1.5**2))

    public = measure_circular_aperture(
        data,
        x_pix=15.2,
        y_pix=14.8,
        aperture_radius_pix=4.0,
        annulus_inner_pix=7.0,
        annulus_outer_pix=10.0,
        exptime_s=30.0,
        instrumental_zero_point_mag=25.0,
        saturation_level_adu=60000.0,
        gain_e_per_adu=1.4,
        read_noise_e=8.3,
        scintillation_enabled=False,
    )
    internal = measure_one_source(
        data,
        x=15.2,
        y=14.8,
        aperture_radius_pix=4.0,
        annulus_inner_pix=7.0,
        annulus_outer_pix=10.0,
        exptime=30.0,
        zero_point=25.0,
        saturation_level_adu=60000.0,
        gain_e_per_adu=1.4,
        read_noise_e=8.3,
        scintillation_enabled=False,
    )

    assert public.flux_adu == pytest.approx(internal.flux, rel=0, abs=1e-12)
    assert public.flux_err_adu == pytest.approx(internal.flux_err, rel=0, abs=1e-12)
    assert public.instrumental_mag == pytest.approx(internal.mag_inst, rel=0, abs=1e-12)
    assert public.mag_err == pytest.approx(internal.mag_err, rel=0, abs=1e-12)
    assert public.local_background_adu_per_pix == pytest.approx(
        internal.local_background, rel=0, abs=1e-12
    )
    assert public.flags == internal.flags
    assert public.error_budget is not None
    assert public.error_budget.to_dict() == internal.error_budget
