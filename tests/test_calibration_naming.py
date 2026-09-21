from grandgousier.calibration.naming import (
    CALIBRATION_NAME_MAX_LENGTH,
    candidate_excluded_calibration_name,
    compact_generated_calibration_name,
)


def test_short_generated_calibration_name_is_unchanged():
    assert compact_generated_calibration_name("short_v1", semantic_suffix="_v1") == "short_v1"


def test_long_generated_calibration_name_is_deterministic_and_preserves_suffix():
    full = "x" * 180 + "_ensemble_g_v1"
    first = compact_generated_calibration_name(full, semantic_suffix="_ensemble_g_v1")
    second = compact_generated_calibration_name(full, semantic_suffix="_ensemble_g_v1")

    assert first == second
    assert len(first) <= CALIBRATION_NAME_MAX_LENGTH
    assert first.endswith("_ensemble_g_v1")
    assert "_h" in first


def test_real_bklyn_apcorr_candidate_name_fits_database_limit():
    ensemble_name = (
        "auto_robophot_20260328_BK_Lyn_APM-RoboPhot_ML0742515_i_s5_v1"
        "_work_g19_max2000_edge20_apcorr_r3_to_r5_ensemble_g_v1"
    )
    candidate_name = candidate_excluded_calibration_name(ensemble_name, iteration=2)

    assert len(ensemble_name) <= CALIBRATION_NAME_MAX_LENGTH
    assert len(candidate_name) <= CALIBRATION_NAME_MAX_LENGTH
    assert candidate_name.endswith("_candx_v1")
    assert "apcorr_r3_to_r5" in candidate_name


def test_later_bklyn_apcorr_candidate_iteration_also_fits():
    ensemble_name = (
        "auto_robophot_20260328_BK_Lyn_APM-RoboPhot_ML0742515_i_s5_v1"
        "_work_g19_max2000_edge20_apcorr_r3_to_r5_ensemble_g_v1"
    )
    candidate_name = candidate_excluded_calibration_name(ensemble_name, iteration=10)

    assert len(candidate_name) <= CALIBRATION_NAME_MAX_LENGTH
    assert candidate_name.endswith("_candx_iter10_v1")


def test_historical_candidate_name_is_preserved_when_it_fits():
    assert candidate_excluded_calibration_name("short_ensemble_g_v1", iteration=2) == (
        "short_ensemble_g_candidate_excluded_v1"
    )
