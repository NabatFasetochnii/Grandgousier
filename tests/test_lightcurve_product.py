from grandgousier.lightcurves.product import (
    DEFAULT_REJECT_FLAGS,
    active_quality_flag_names,
    combine_mag_error,
    evaluate_rejection,
    light_curve_object_columns,
    light_curve_reject_flags,
    light_curve_summary_columns,
    merge_quality_flags,
    parse_reject_flags,
    science_light_curve_columns,
)


def test_science_light_curve_columns_include_global_product_fields():
    columns = science_light_curve_columns()
    for name in (
        "object_id",
        "source_id",
        "frame_id",
        "series_id",
        "mag_cal",
        "mag_global",
        "interseries_offset_mag",
        "quality_flags",
        "is_rejected",
        "reject_reasons",
    ):
        assert name in columns
    assert "n_points" in light_curve_object_columns()
    assert "section" in light_curve_summary_columns()


def test_parse_reject_flags_keeps_defaults_and_adds_custom_flags():
    flags = parse_reject_flags("CUSTOM_BAD, EDGE")
    assert "HIGH_RMS" in flags
    assert "EDGE" in flags
    assert "CUSTOM_BAD" in flags
    assert "APERTURE_UNSTABLE" in DEFAULT_REJECT_FLAGS


def test_merge_quality_flags_and_evaluate_rejection():
    flags = merge_quality_flags(
        measurement_flags={"catalog_name": "gaia", "source_id": "1", "SATURATED_LIKE": False},
        object_qc_flags={"APERTURE_UNSTABLE": True, "HIGH_RMS": 1},
        frame_quality_flags={"BAD_SKY": "yes"},
    )
    assert "catalog_name" not in flags
    assert "SOURCE_ID" not in flags
    assert active_quality_flag_names(flags) >= {"APERTURE_UNSTABLE", "HIGH_RMS", "BAD_SKY"}
    is_rejected, reasons = evaluate_rejection(
        quality_flags=flags,
        mag=15.0,
        mag_err=0.02,
        reject_flags={"APERTURE_UNSTABLE", "EDGE"},
    )
    assert is_rejected is True
    assert reasons == ["APERTURE_UNSTABLE"]


def test_evaluate_rejection_marks_invalid_magnitude():
    is_rejected, reasons = evaluate_rejection(
        quality_flags={},
        mag=None,
        mag_err=None,
        reject_flags=set(),
    )
    assert is_rejected is True
    assert reasons == ["NO_VALID_MAG"]


def test_combine_mag_error_quadrature_and_fallback():
    assert round(combine_mag_error(0.03, 0.04), 6) == 0.05
    assert combine_mag_error(0.03, None) == 0.03
    assert combine_mag_error(None, None) is None


def test_light_curve_reject_flags_can_make_detection_support_diagnostic_only():
    strict = light_curve_reject_flags(require_detection_support=True)
    relaxed = light_curve_reject_flags(require_detection_support=False)

    assert "NO_DETECTION_SUPPORT" in strict
    assert "WEAK_DETECTION_SUPPORT" in strict
    assert "NO_DETECTION_SUPPORT" not in relaxed
    assert "WEAK_DETECTION_SUPPORT" not in relaxed
    assert "HIGH_RMS" in relaxed
