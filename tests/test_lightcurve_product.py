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
        "mag_err_effective_detector_only",
        "mag_err_effective_total",
        "mag_global",
        "interseries_offset_mag",
        "interseries_generation_id",
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


def test_science_lightcurve_reader_has_no_legacy_storage_path() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "lightcurves" / "product.py").read_text(encoding="utf-8")
    assert "science_measurements" not in source
    assert "science_photometry_cache" not in source
    assert "interseries_calibration_offsets" not in source
    assert "load_calibration_photometry_view" in source
    assert "InterseriesCalibrationSeries" in source


def test_raw_lightcurve_always_uses_storage_independent_view(monkeypatch):
    from grandgousier.lightcurves import product

    scope = product.LightCurveScope(calibration_version_ids=[7, 8])
    sentinel = [{"source": "virtual"}]
    monkeypatch.setattr(
        product,
        "_load_raw_light_curve_rows_virtual",
        lambda *args, **kwargs: sentinel,
    )

    rows = product._load_raw_light_curve_rows(
        object(),
        scope=scope,
        source_id=None,
        object_id=42,
        object_qc_algorithm="object_qc_v1",
        offset_algorithm="interseries_graph_median_v1",
    )
    assert rows is sentinel


def test_lightcurve_reader_avoids_raw_postgresql_array_and_nullable_predicate_binds() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "grandgousier" / "lightcurves" / "product.py").read_text(encoding="utf-8")
    assert "ANY(:calibration_names)" not in source
    assert "science_calibration_version_id = ANY(:cv_ids)" not in source
    assert "(:algorithm IS NULL OR" not in source
    assert "DISTINCT ON" not in source
    assert "g.created_at DESC" not in source
    assert "InterseriesCalibrationSeries.science_calibration_version_id" in source


def test_resolve_lightcurve_scope_passes_calibration_object_to_chain_resolver(monkeypatch) -> None:
    from types import SimpleNamespace
    from grandgousier.lightcurves import product

    calibration = SimpleNamespace(calibration_version_id=7, name="ens")

    class FakeSession:
        def get(self, model, key):
            assert key == 7
            return calibration

    monkeypatch.setattr(
        product,
        "_load_calibration_rows",
        lambda *args, **kwargs: [
            {
                "calibration_version_id": 7,
                "name": "ens",
                "model_name": "ensemble_differential_v1",
                "instrument_unit_id": 1,
            }
        ],
    )

    seen = []

    def fake_chain(session, cv):
        seen.append(cv)
        return ["ens", "base"]

    monkeypatch.setattr(product, "_calibration_chain_names", fake_chain)
    monkeypatch.setattr(product, "_resolve_field_series_filter", lambda *args, **kwargs: None)

    scope = product.resolve_light_curve_scope(FakeSession(), calibration_names=["ens"])

    assert seen == [calibration]
    assert scope.calibration_version_ids == [7]
