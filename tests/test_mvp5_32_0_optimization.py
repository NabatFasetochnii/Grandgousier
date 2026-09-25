from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

import numpy as np

from grandgousier.catalogs.frame_match import _source_mag
from grandgousier.pipeline.runner import (
    FieldPipelineConfig,
    _calibration_names,
    _effective_require_detection_support,
    _frame_ids_for_step,
)
from grandgousier.quality.frame_pre_qc import analyze_frame_image, _effective_max_local_maxima


def test_frame_pre_qc_records_detection_cap_before_detection_save_limit():
    rng = np.random.default_rng(123)
    data = rng.normal(1000.0, 3.0, size=(80, 80))
    for y in range(5, 75, 4):
        for x in range(5, 75, 4):
            data[y, x] = 1200.0

    qc = analyze_frame_image(
        data,
        threshold_sigma=5.0,
        detection_cap=50,
        max_local_maxima=200,
        max_bright_fraction=0.5,
        max_saturated_fraction=None,
        min_finite_fraction=0.95,
    )

    assert qc["n_local_maxima_before_cap"] > 50
    assert qc["flags"]["DETECTION_CAP_REACHED"] is True
    assert qc["severity"] in {"WARNING", "BAD"}


def test_pre_qc_excess_detection_limit_scales_only_for_large_detectors():
    assert _effective_max_local_maxima(5000, n_pix=1024 * 1024) == 5000
    assert _effective_max_local_maxima(5000, n_pix=2048 * 2048) == 5000
    assert _effective_max_local_maxima(5000, n_pix=4098 * 4098) > 20000
    assert _effective_max_local_maxima(None, n_pix=4098 * 4098) is None


def test_working_catalog_tag_changes_explicit_circular_calibration_names():
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="prefix",
        working_catalog_mag_limit=19.0,
        max_working_sources_per_frame=2000,
        catalog_edge_margin_pix=20.0,
        photometry_estimator="circular",
    )

    names = _calibration_names(cfg)

    assert "_work_g19_max2000_edge20_" in names["raw"]
    assert names["raw"].endswith("raw_aperture_r3_ann8_12_errphys_v1")
    assert "_errphys_v1_apcorr_r3_to_r5_sv2_" in names["ensemble"]
    assert "_work_g19_max2000_edge20_" in names["ensemble"]


def test_source_mag_accepts_common_gaia_keys():
    assert _source_mag({"ref_mags": {"G": 17.2}}, "G") == 17.2
    assert _source_mag({"ref_mags": {"phot_g_mean_mag": "18.1"}}, "G") == 18.1
    assert _source_mag({"ref_mags": {"r": 15.0}}, "G") is None



def test_empty_frame_selection_remains_explicit_after_pre_qc_exclusion():
    assert _frame_ids_for_step([]) == []
    assert _frame_ids_for_step([106, 107]) == [106, 107]


def test_detection_support_requirement_follows_detection_branch_by_default():
    forced_only = FieldPipelineConfig(catalog_name="cat", calibration_prefix="p", skip_detection=True, skip_detection_catalog_match=True)
    detection_enabled = FieldPipelineConfig(catalog_name="cat", calibration_prefix="p", skip_detection=False, skip_detection_catalog_match=False)
    explicit_strict = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="p",
        skip_detection=True,
        skip_detection_catalog_match=True,
        require_detection_support=True,
    )

    assert _effective_require_detection_support(forced_only) is False
    assert _effective_require_detection_support(detection_enabled) is True
    assert _effective_require_detection_support(explicit_strict) is True


def test_detection_pre_qc_seed_preserves_detection_results_exactly():
    from grandgousier.photometry.source_detection import detect_sources_in_image

    rng = np.random.default_rng(321)
    data = rng.normal(1000.0, 3.0, size=(96, 96))
    for y in range(8, 88, 8):
        for x in range(8, 88, 8):
            data[y, x] = 1150.0 + ((x + y) % 7)

    qc = analyze_frame_image(
        data,
        threshold_sigma=5.0,
        detection_cap=2000,
        max_local_maxima=5000,
        max_bright_fraction=0.5,
        max_saturated_fraction=None,
        min_finite_fraction=0.95,
        capture_detection_seed=True,
    )
    seed = qc["_detection_seed"]

    kwargs = dict(
        wcs=None,
        threshold_sigma=5.0,
        max_sources=2000,
        min_separation_pix=5.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        centroid_box_radius_pix=3,
        exptime=1.0,
        saturation_level_adu=None,
    )
    baseline = detect_sources_in_image(data, **kwargs)
    seeded = detect_sources_in_image(data, candidate_seed=seed, **kwargs)

    assert seeded == baseline


def test_detection_vectorized_aperture_batch_matches_scalar_measurements():
    from grandgousier.photometry.forced_aperture import measure_one_source
    from grandgousier.photometry.source_detection import _measure_detection_apertures_batch

    rng = np.random.default_rng(9981)
    data = rng.normal(500.0, 17.0, size=(128, 128))
    centered = [
        (40.0, 40.25, 41.75, 900.0),
        (35.0, 63.60, 64.20, 875.0),
        (30.0, 92.10, 81.35, 850.0),
        # Deliberately close to an edge: this row must use the exact scalar fallback.
        (25.0, 5.20, 7.40, 825.0),
    ]
    for _, x, y, peak in centered:
        data[int(round(y)), int(round(x))] = peak
    # Exercise finite-pixel filtering without making the annulus degenerate.
    data[49, 49] = np.nan

    timings: dict[str, float] = {}
    counters: dict[str, int] = {}
    actual = _measure_detection_apertures_batch(
        data,
        centered,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        exptime=60.0,
        saturation_level_adu=880.0,
        performance_timings=timings,
        performance_counters=counters,
    )

    expected = [
        measure_one_source(
            data,
            x=x,
            y=y,
            aperture_radius_pix=3.0,
            annulus_inner_pix=8.0,
            annulus_outer_pix=12.0,
            exptime=60.0,
            zero_point=0.0,
            saturation_level_adu=880.0,
        )
        for _, x, y, _ in centered
    ]

    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got.flux == pytest.approx(want.flux, rel=1e-13, abs=1e-10)
        assert got.flux_err == pytest.approx(want.flux_err, rel=1e-13, abs=1e-10)
        assert got.local_background == pytest.approx(
            want.local_background, rel=1e-13, abs=1e-10
        )
        assert got.flags == want.flags

    assert counters["aperture_batch_sources"] == 3
    assert counters["aperture_scalar_fallback_sources"] == 1
    assert counters["aperture_batch_chunks"] == 1
    assert timings["aperture_batch_geometry"] >= 0.0
    assert timings["aperture_batch_statistics"] >= 0.0



def test_aperture_correction_reference_batch_matches_scalar_measurements():
    from grandgousier.calibration.aperture_correction import (
        _measure_reference_apertures_batch,
    )
    from grandgousier.photometry.forced_aperture import measure_one_source

    rng = np.random.default_rng(41041)
    data = rng.normal(700.0, 13.0, size=(128, 128))
    coords = [
        (40.25, 41.75),
        (63.60, 64.20),
        (92.10, 81.35),
        # Exact scalar fallback for the edge case.
        (5.20, 7.40),
    ]
    for x, y in coords:
        data[int(round(y)), int(round(x))] = 950.0
    data[49, 49] = np.nan
    rows = [SimpleNamespace(x=x, y=y) for x, y in coords]

    timings: dict[str, float] = {}
    counters: dict[str, int] = {}
    actual = _measure_reference_apertures_batch(
        data,
        rows,
        aperture_radius_pix=5.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        exptime=60.0,
        saturation_level_adu=925.0,
        performance_timings=timings,
        performance_counters=counters,
    )
    expected = [
        measure_one_source(
            data,
            x=x,
            y=y,
            aperture_radius_pix=5.0,
            annulus_inner_pix=8.0,
            annulus_outer_pix=12.0,
            exptime=60.0,
            zero_point=0.0,
            saturation_level_adu=925.0,
        )
        for x, y in coords
    ]

    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got is not None
        assert got.mag_inst == pytest.approx(want.mag_inst, rel=1e-13, abs=1e-12)
        assert got.local_background == pytest.approx(
            want.local_background, rel=1e-13, abs=1e-12
        )
        assert got.flags == want.flags

    assert counters["batch_sources"] == 3
    assert counters["scalar_fallback_sources"] == 1
    assert counters["batch_chunks"] == 1
    assert timings["reference_batch_geometry"] >= 0.0
    assert timings["reference_batch_statistics"] >= 0.0
    assert timings["reference_batch_finalize"] >= 0.0
    assert timings["reference_scalar_fallback"] >= 0.0
