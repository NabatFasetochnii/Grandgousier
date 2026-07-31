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
from grandgousier.quality.frame_pre_qc import analyze_frame_image


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


def test_working_catalog_tag_changes_default_calibration_names():
    cfg = FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="prefix",
        working_catalog_mag_limit=19.0,
        max_working_sources_per_frame=2000,
        catalog_edge_margin_pix=20.0,
    )

    names = _calibration_names(cfg)

    assert "_work_g19_max2000_edge20_" in names["raw"]
    assert names["raw"].endswith("raw_aperture_r3_ann8_12_v1")
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
