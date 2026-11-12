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


def test_default_science_rejection_does_not_veto_on_detection_support_alone():
    from grandgousier.pipeline.runner import (
        FieldPipelineConfig,
        _effective_require_detection_support,
        _science_reject_flags,
    )

    config = FieldPipelineConfig(catalog_name="cat", calibration_prefix="policy")
    reject, reasons = evaluate_rejection(
        quality_flags={"WEAK_DETECTION_SUPPORT": True},
        mag=12.0,
        mag_err=0.01,
        reject_flags=_science_reject_flags(
            config, _effective_require_detection_support(config)
        ),
    )

    assert reject is False
    assert reasons == []


def test_compact_count_summary_matches_full_summary_with_merged_rejection_flags(
    monkeypatch,
):
    from types import SimpleNamespace

    from grandgousier.db.models import CalibrationVersion
    from grandgousier.lightcurves import product

    cv = SimpleNamespace(
        calibration_version_id=101,
        name="science_v",
        model_name="ensemble_test",
        series_id=55,
    )

    def measurement(
        measurement_id, object_id, frame_id, mag_cal, flags, *, mag_err_random=0.02
    ):
        return SimpleNamespace(
            measurement_id=measurement_id,
            base_measurement_id=measurement_id,
            object_id=object_id,
            frame_id=frame_id,
            x=1.0,
            y=2.0,
            flux=100.0,
            flux_err=1.0,
            mag_inst=mag_cal,
            mag_cal=mag_cal,
            mag_err=0.03,
            mag_err_random=mag_err_random,
            mag_err_systematic=None,
            aperture_radius_pix=3.0,
            local_background=10.0,
            local_zp_correction=None,
            centroid_dx_pix=0.0,
            centroid_dy_pix=0.0,
            flags=flags,
        )

    measurements = [
        measurement(1, 1, 1, 14.0, {"catalog_name": "gaia", "source_id": "s1"}),
        measurement(
            2,
            1,
            2,
            14.1,
            {"catalog_name": "gaia", "source_id": "s1", "NO_DETECTION_SUPPORT": True},
            mag_err_random=None,
        ),
        measurement(3, 2, 3, 15.0, {"catalog_name": "gaia", "source_id": "s2"}),
        measurement(4, 3, 4, 15.1, {"catalog_name": "gaia", "source_id": "s3"}),
        measurement(
            5,
            4,
            5,
            15.2,
            {"catalog_name": "gaia", "source_id": "s4", "EDGE": False},
        ),
        measurement(6, 5, 6, None, {"catalog_name": "gaia", "source_id": "s5"}),
        measurement(7, 6, 1, 16.0, {"catalog_name": "other", "source_id": "s6"}),
        measurement(8, 7, 7, 16.1, {"catalog_name": "gaia", "source_id": "s7"}),
        measurement(9, 8, 8, 16.2, {"catalog_name": "gaia", "source_id": "s8"}),
    ]
    frames = [
        SimpleNamespace(
            frame_id=frame_id,
            instrument_unit_id=9 if frame_id == 7 else 8,
            quality_flags={"EDGE": True} if frame_id in {4, 5} else {},
            date_obs=None,
            mjd=float(frame_id),
            jd=None,
            instrument="MASTER",
            site="test",
            tube="east",
            camera="204",
            filter_name="V",
        )
        for frame_id in range(1, 8)
    ]
    object_qc = SimpleNamespace(
        object_id=2,
        calibration_version_id=101,
        catalog_name="gaia",
        instrument_unit_id=8,
        flags={"HIGH_RMS": True},
    )
    scope_context_row = {
        "calibration_version_id": 101,
        "product_role": "SCIENCE",
        "calibration_field_id": 33,
        "calibration_series_id": 55,
        "calibration_instrument_unit_id": 8,
        "used_catalogs": {"catalog_name": "gaia"},
        "series_field_id": 33,
        "series_instrument_unit_id": 8,
        "series_filter_name": "V",
        "pipeline_catalog_name": "gaia",
    }
    generation_row = {
        "interseries_generation_id": 900,
        "field_id": 33,
        "instrument_unit_id": 8,
        "filter_name": "V",
        "catalog_name": "gaia",
        "analysis_algorithm": "interseries_graph_median_v1",
        "lifecycle_status": "COMPLETE_IMMUTABLE",
        "reliability_status": "REDUNDANT_GRAPH_OK",
        "n_series": 1,
        "membership_field_id": 33,
        "series_id": 55,
        "science_calibration_version_id": 101,
        "offset_mag": 0.1,
        "offset_err": 0.01,
    }

    class Result:
        def __init__(self, rows):
            self._rows = list(rows)

        def mappings(self):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def __init__(self):
            self.scalar_responses = iter(([cv], frames, [object_qc]))
            self.execute_responses = iter(
                ([scope_context_row], [generation_row], [(55, "field", 33)])
            )

        def get(self, model, key):
            assert model is CalibrationVersion
            assert key == 101
            return cv

        def scalars(self, _statement):
            return Result(next(self.scalar_responses))

        def execute(self, _statement, *_args, **_kwargs):
            return Result(next(self.execute_responses))

    monkeypatch.setattr(
        product,
        "_load_calibration_rows",
        lambda *_args, **_kwargs: [
            {
                "calibration_version_id": 101,
                "name": "science_v",
                "model_name": "ensemble_test",
                "instrument_unit_id": 8,
                "series_id": 55,
                "field_id": 33,
                "product_role": "SCIENCE",
                "is_current_alias_head": True,
            }
        ],
    )
    monkeypatch.setattr(product, "_calibration_chain_names", lambda *_args: ["science_v"])
    monkeypatch.setattr(
        product,
        "_resolve_field_series_filter",
        lambda *_args, **_kwargs: {
            "frame_ids": list(range(1, 9)),
            "series_id": 55,
            "field_name": "field",
        },
    )

    def fake_view(_session, *, frame_ids, **_kwargs):
        selected = set(frame_ids or ())
        return SimpleNamespace(
            rows=[row for row in measurements if not selected or row.frame_id in selected]
        )

    monkeypatch.setattr(product, "load_calibration_photometry_view", fake_view)
    selected_generations = []
    apply_selection = product._apply_interseries_selection_to_scope

    def record_selection(scope, selection):
        apply_selection(scope, selection)
        selected_generations.append(list(scope.interseries_generation_ids))

    monkeypatch.setattr(product, "_apply_interseries_selection_to_scope", record_selection)
    converted_rows = []
    science_row_from_db = product._science_row_from_db

    def record_science_row(*args, **kwargs):
        converted_rows.append(True)
        return science_row_from_db(*args, **kwargs)

    monkeypatch.setattr(product, "_science_row_from_db", record_science_row)
    reject_flags = product.light_curve_reject_flags(require_detection_support=False)

    compact = product.build_light_curve_count_summary(
        FakeSession(),
        calibration_names=["science_v"],
        catalog_name="gaia",
        series_id=55,
        reject_flags=reject_flags,
    )
    assert converted_rows == []

    full = product.build_light_curve_summary(
        FakeSession(),
        calibration_names=["science_v"],
        catalog_name="gaia",
        series_id=55,
        reject_flags=reject_flags,
    )

    assert (
        compact.total_points,
        compact.accepted_points,
        compact.rejected_points,
        compact.n_objects,
    ) == (
        full.total_points,
        full.accepted_points,
        full.rejected_points,
        full.n_objects,
    ) == (7, 4, 3, 6)
    assert full.scope.interseries_generation_ids == [900]
    assert selected_generations == [[900], [900]]
    assert len(converted_rows) == 7
    assert full.n_reject_reasons == {"EDGE": 1, "HIGH_RMS": 1, "NO_VALID_MAG": 1}


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
