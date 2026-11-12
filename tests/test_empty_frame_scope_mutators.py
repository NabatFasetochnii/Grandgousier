from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.archive import fields_series
from grandgousier.astrometry import wcs_check
from grandgousier.calibration import (
    aperture_correction,
    aperture_zp,
    ensemble,
    photometric_frame_qc,
    preliminary,
    reconstruction,
    spatial_ensemble,
)
from grandgousier.catalogs import frame_match, gaia_query
from grandgousier.photometry import forced_aperture, source_detection
from grandgousier.pipeline import runner as runner_module
from grandgousier.quality import (
    aperture_stability,
    aperture_sweep,
    detection_catalog_qc,
    frame_pre_qc,
    object_quality,
)


class _NoSessionOperations:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"unexpected session operation: {name}")


@pytest.mark.parametrize(
    ("helper", "kwargs"),
    [
        (
            forced_aperture.run_forced_aperture_photometry,
            {"photometry_estimator": "invalid"},
        ),
        (source_detection.detect_sources_in_frames, {"threshold_sigma": 0}),
    ],
)
def test_mutating_helpers_reject_explicit_empty_scope_before_session_or_other_validation(
    helper, kwargs
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        helper(session, frame_ids=[], **kwargs)

    assert session.calls == []


def test_build_field_series_rejects_explicit_empty_scope_before_session_calls():
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        fields_series.build_field_series(session, frame_ids=[])

    assert session.calls == []


def test_build_field_series_keeps_none_as_unscoped_without_database_logic(monkeypatch):
    session = _NoSessionOperations()

    def stop_before_database_logic(*_args, **_kwargs):
        raise RuntimeError("candidate frame loader reached")

    monkeypatch.setattr(fields_series, "_load_candidate_frames", stop_before_database_logic)

    with pytest.raises(RuntimeError, match="candidate frame loader reached"):
        fields_series.build_field_series(session, frame_ids=None)

    assert session.calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_preliminary_calibration_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        preliminary.calibrate_measurements_from_frame_qc(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_preliminary_calibration_keeps_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    selected_scopes = []

    def stop_at_qc_selector(_session, *, frame_ids, **_kwargs):
        selected_scopes.append(frame_ids)
        raise RuntimeError("QC selector reached")

    monkeypatch.setattr(preliminary, "_load_frame_qc_map", stop_at_qc_selector)

    with pytest.raises(RuntimeError, match="QC selector reached"):
        preliminary.calibrate_measurements_from_frame_qc(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=None,
            dry_run=True,
        )

    assert selected_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_photometric_frame_qc_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        photometric_frame_qc.apply_photometric_frame_qc(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            reference_object_ids=[1],
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_photometric_frame_qc_preserves_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    observed_scopes = []

    def stop_at_reconstruction(_session, *, frame_ids, **_kwargs):
        observed_scopes.append(frame_ids)
        raise RuntimeError("reconstruction reached")

    monkeypatch.setattr(
        reconstruction,
        "reconstruct_calibration_measurement_states",
        stop_at_reconstruction,
    )

    with pytest.raises(RuntimeError, match="reconstruction reached"):
        photometric_frame_qc.apply_photometric_frame_qc(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            reference_object_ids=[1],
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_aperture_zp_rejects_explicit_empty_scope_before_session_operations(dry_run):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        aperture_zp.aperture_zp_calibrate(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_aperture_zp_preserves_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    observed_scopes = []

    def stop_at_calibration_view(_session, *, frame_ids, **_kwargs):
        observed_scopes.append(frame_ids)
        raise RuntimeError("calibration view reached")

    monkeypatch.setattr(
        aperture_zp,
        "load_calibration_photometry_view",
        stop_at_calibration_view,
    )

    with pytest.raises(RuntimeError, match="calibration view reached"):
        aperture_zp.aperture_zp_calibrate(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_ensemble_calibration_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        ensemble.ensemble_calibrate_measurements(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_ensemble_calibration_preserves_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    observed_scopes = []

    def stop_at_source_measurements(_session, *, frame_ids, **_kwargs):
        observed_scopes.append(frame_ids)
        raise RuntimeError("source measurement selector reached")

    monkeypatch.setattr(ensemble, "_load_source_measurements", stop_at_source_measurements)

    with pytest.raises(RuntimeError, match="source measurement selector reached"):
        ensemble.ensemble_calibrate_measurements(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            catalog_name="catalog",
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_aperture_correction_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        aperture_correction.apply_aperture_correction(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_aperture_correction_preserves_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    observed_scopes = []

    def stop_at_source_selector(_session, *, frame_ids, **_kwargs):
        observed_scopes.append(frame_ids)
        raise RuntimeError("aperture source selector reached")

    monkeypatch.setattr(
        aperture_correction,
        "_load_aperture_source_rows_scalar",
        stop_at_source_selector,
    )

    with pytest.raises(RuntimeError, match="aperture source selector reached"):
        aperture_correction.apply_aperture_correction(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_spatial_ensemble_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        spatial_ensemble.apply_spatial_ensemble_correction(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            reference_object_ids=[1],
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_spatial_ensemble_preserves_none_as_unscoped(monkeypatch):
    session = SimpleNamespace(
        scalar=lambda _statement: SimpleNamespace(calibration_version_id=42)
    )
    observed_scopes = []

    def stop_at_scope_resolver(
        _session, *, calibration_version_id, frame_ids, instrument_unit_id
    ):
        observed_scopes.append(frame_ids)
        raise RuntimeError("spatial frame scope resolved")

    monkeypatch.setattr(spatial_ensemble, "_resolve_frame_ids", stop_at_scope_resolver)

    with pytest.raises(RuntimeError, match="spatial frame scope resolved"):
        spatial_ensemble.apply_spatial_ensemble_correction(
            session,
            source_calibration_name="source",
            target_calibration_name="target",
            reference_object_ids=[1],
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_object_qc_rejects_explicit_empty_scope_before_session_operations(dry_run):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        object_quality.compute_object_qc(
            session,
            calibration_name="calibration",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_aperture_sweep_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        aperture_sweep.run_aperture_radius_sweep(
            session,
            catalog_name="catalog",
            object_id=1,
            radii=[3.0],
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_aperture_sweep_preserves_none_as_unscoped(monkeypatch):
    session = _NoSessionOperations()
    observed_scopes = []

    def stop_at_photometry(_session, **kwargs):
        observed_scopes.append(kwargs["frame_ids"])
        raise RuntimeError("forced photometry reached")

    monkeypatch.setattr(aperture_sweep, "run_forced_aperture_photometry", stop_at_photometry)

    with pytest.raises(RuntimeError, match="forced photometry reached"):
        aperture_sweep.run_aperture_radius_sweep(
            session,
            catalog_name="catalog",
            object_id=1,
            radii=[3.0],
            frame_ids=None,
            skip_calibration=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
def test_aperture_stability_rejects_explicit_empty_scope_before_session_operations(
    dry_run,
):
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        aperture_stability.compute_aperture_stability_qc(
            session,
            calibration_name="calibration",
            object_id=1,
            radii=[3.0],
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []


def test_aperture_stability_preserves_none_as_unscoped(monkeypatch):
    session = _NoSessionOperations()
    observed_scopes = []

    def stop_at_lightcurve_load(_session, **kwargs):
        observed_scopes.append(kwargs["frame_ids"])
        raise RuntimeError("lightcurve reader reached")

    monkeypatch.setattr(aperture_stability, "load_lightcurve_rows", stop_at_lightcurve_load)

    with pytest.raises(RuntimeError, match="lightcurve reader reached"):
        aperture_stability.compute_aperture_stability_qc(
            session,
            calibration_name="calibration",
            object_id=1,
            radii=[3.0],
            frame_ids=None,
            dry_run=True,
        )

    assert observed_scopes == [None]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("helper_name", ["catalog_match", "gaia_query"])
def test_catalog_mutators_reject_empty_scope_before_session_or_provider(
    monkeypatch, helper_name, dry_run
):
    session = _NoSessionOperations()
    provider_calls: list[float | None] = []
    if helper_name == "catalog_match":
        helper = frame_match.match_catalog_to_frames
        helper_kwargs = {"catalog_name": "cat"}
    else:
        helper = gaia_query.query_gaia_for_frames
        helper_kwargs = {}
        monkeypatch.setattr(
            gaia_query,
            "_build_gaia_tap_providers",
            lambda timeout: provider_calls.append(timeout) or [],
        )

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        helper(session, frame_ids=[], dry_run=dry_run, **helper_kwargs)

    assert session.calls == []
    assert provider_calls == []


@pytest.mark.parametrize("api_name", ["catalog_match", "gaia_query"])
@pytest.mark.parametrize("frame_ids", [None, [7, 2]])
def test_catalog_apis_preserve_unscoped_and_exact_frame_ids(
    monkeypatch, api_name, frame_ids
):
    observed_frame_ids: list[list[int] | None] = []

    def stop_after_selection(_session, *, frame_ids, **_kwargs):
        observed_frame_ids.append(None if frame_ids is None else list(frame_ids))
        raise RuntimeError("frame selection reached")

    if api_name == "catalog_match":
        monkeypatch.setattr(frame_match, "_select_frames", stop_after_selection)
        helper = frame_match.match_catalog_to_frames
        helper_kwargs = {"catalog_name": "cat"}
    else:
        monkeypatch.setattr(gaia_query, "_select_frames", stop_after_selection)
        monkeypatch.setattr(gaia_query, "_build_gaia_tap_providers", lambda _timeout: [])
        helper = gaia_query.query_gaia_for_frames
        helper_kwargs = {}

    with pytest.raises(RuntimeError, match="frame selection reached"):
        helper(_NoSessionOperations(), frame_ids=frame_ids, **helper_kwargs)

    assert observed_frame_ids == [frame_ids]


class _EmptyScalarResult:
    @staticmethod
    def all():
        return []

    def __iter__(self):
        return iter(self.all())


class _SelectCaptureSession:
    def __init__(self):
        self.statements = []

    def scalars(self, statement):
        self.statements.append(statement)
        return _EmptyScalarResult()


@pytest.mark.parametrize("selector", [frame_match._select_frames, gaia_query._select_frames])
@pytest.mark.parametrize("frame_ids", [None, [], [7, 2]])
def test_catalog_frame_selectors_distinguish_unscoped_from_explicit_ids(selector, frame_ids):
    session = _SelectCaptureSession()

    selector(
        session,
        frame_ids=frame_ids,
        instrument=None,
        instrument_unit_id=None,
        only_wcs_ok=False,
        limit=None,
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    if frame_ids is None:
        assert not statement._where_criteria
    else:
        compiled = statement.compile(compile_kwargs={"render_postcompile": True})
        assert list(compiled.params.values()) == frame_ids


@pytest.mark.parametrize("dry_run", [False, True])
def test_detection_catalog_match_rejects_empty_scope_before_session_or_selector(
    monkeypatch, dry_run
):
    session = _NoSessionOperations()
    selector_calls = []
    monkeypatch.setattr(
        detection_catalog_qc,
        "_select_frames",
        lambda *_args, **_kwargs: selector_calls.append(True),
    )

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        detection_catalog_qc.match_detections_to_catalog(
            session,
            catalog_name="cat",
            frame_ids=[],
            dry_run=dry_run,
        )

    assert session.calls == []
    assert selector_calls == []


@pytest.mark.parametrize("frame_ids", [None, [], [7, 2]])
def test_detection_catalog_frame_selector_distinguishes_none_and_explicit_ids(frame_ids):
    session = _SelectCaptureSession()

    detection_catalog_qc._select_frames(
        session,
        frame_ids=frame_ids,
        instrument=None,
        instrument_unit_id=None,
        only_wcs_ok=False,
        limit=None,
    )

    assert len(session.statements) == 1
    statement = session.statements[0]
    if frame_ids is None:
        assert not statement._where_criteria
    else:
        assert statement._where_criteria
        if frame_ids:
            compiled = statement.compile(compile_kwargs={"render_postcompile": True})
            assert list(compiled.params.values()) == frame_ids


def test_check_frames_wcs_rejects_explicit_empty_scope_before_session_operations():
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        wcs_check.check_frames_wcs(session, frame_ids=[])

    assert session.calls == []


def test_run_frame_pre_qc_rejects_explicit_empty_scope_before_session_operations():
    session = _NoSessionOperations()

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        frame_pre_qc.run_frame_pre_qc(session, frame_ids=[])

    assert session.calls == []


@pytest.mark.parametrize("frame_ids", [None, [7, 2]])
def test_check_frames_wcs_selector_distinguishes_unscoped_and_explicit_ids(frame_ids):
    session = _SelectCaptureSession()

    result = wcs_check.check_frames_wcs(
        session,
        frame_ids=frame_ids,
        current_status=None,
        dry_run=True,
    )

    assert result.selected == 0
    assert len(session.statements) == 1
    statement = session.statements[0]
    if frame_ids is None:
        assert not statement._where_criteria
    else:
        compiled = statement.compile(compile_kwargs={"render_postcompile": True})
        assert list(compiled.params.values()) == frame_ids


@pytest.mark.parametrize("helper_name", ["forced", "detection"])
@pytest.mark.parametrize(
    ("frame_ids", "expected_frame_ids"),
    [(None, [1, 2]), ([2], [2])],
)
def test_mutating_helpers_keep_unscoped_and_nonempty_frame_selection(
    monkeypatch, tmp_path, helper_name, frame_ids, expected_frame_ids
):
    available_frames = [
        SimpleNamespace(frame_id=frame_id, raw_path=str(tmp_path / f"missing-{frame_id}.fits"))
        for frame_id in (1, 2)
    ]
    selection_arguments: list[list[int] | None] = []

    def select_frames(_session, *, frame_ids, **_kwargs):
        selection_arguments.append(None if frame_ids is None else list(frame_ids))
        if frame_ids is None:
            return available_frames
        selected = set(frame_ids)
        return [frame for frame in available_frames if frame.frame_id in selected]

    session = _NoSessionOperations()
    if helper_name == "forced":
        monkeypatch.setattr(forced_aperture, "_select_frames", select_frames)
        monkeypatch.setattr(
            forced_aperture,
            "_select_frame_links_bulk",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(forced_aperture, "_load_detection_rows", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(forced_aperture, "postgres_copy_capable", lambda _session: False)

        result = forced_aperture.run_forced_aperture_photometry(
            session,
            frame_ids=frame_ids,
            dry_run=True,
        )
        assert result.selected_frames == len(expected_frame_ids)
    else:
        monkeypatch.setattr(source_detection, "_select_frames", select_frames)

        result = source_detection.detect_sources_in_frames(
            session,
            frame_ids=frame_ids,
            dry_run=True,
        )
        assert result.selected_frames == len(expected_frame_ids)
        assert result.checked_frames == len(expected_frame_ids)

    assert selection_arguments == [frame_ids]
    assert session.calls == []


def _runner_config(*, frame_ids, series_id, run_detection, run_forced_photometry):
    return runner_module.FieldPipelineConfig(
        catalog_name="cat",
        calibration_prefix="scope-test",
        photometry_estimator="circular",
        frame_ids=frame_ids,
        series_id=series_id,
        run_pre_photometry_qc=False,
        skip_wcs=True,
        skip_catalog_frame_match=True,
        skip_detection=not run_detection,
        skip_detection_catalog_match=True,
        skip_forced_photometry=not run_forced_photometry,
        skip_aperture_correction=True,
        skip_aperture_zp=True,
        skip_ensemble=True,
        skip_object_qc=True,
        skip_aperture_stability_qc=True,
        skip_field_report=True,
        export_target_light_curve=False,
        export_candidate_light_curves=False,
        record_run=False,
        no_file_log=True,
    )


@pytest.mark.parametrize(
    "target",
    ["wcs", "detection", "forced_photometry", "catalog_match", "detection_catalog_match"],
)
@pytest.mark.parametrize(
    ("frame_ids", "series_id", "series_frame_ids", "expected_frame_ids"),
    [
        (None, None, None, None),
        ([8, 9], None, None, [8, 9]),
        (None, 7, [12, 13], [12, 13]),
    ],
)
def test_runner_passes_none_only_for_unscoped_mutator_calls(
    monkeypatch,
    target,
    frame_ids,
    series_id,
    series_frame_ids,
    expected_frame_ids,
):
    observed_frame_ids: list[list[int] | None] = []
    observed_kwargs: list[dict] = []
    monkeypatch.setattr(runner_module, "_check_open_series_admission", lambda *_args: None)
    if series_id is not None:
        monkeypatch.setattr(
            runner_module,
            "load_series_selection",
            lambda *_args: {
                "frame_ids": series_frame_ids,
                "field_id": 1,
                "instrument_unit_id": None,
            },
        )

    def capture_call(_session, **kwargs):
        observed_kwargs.append(dict(kwargs))
        value = kwargs["frame_ids"]
        observed_frame_ids.append(None if value is None else list(value))
        if target == "wcs":
            return {"selected": 0}
        if target == "forced_photometry":
            return forced_aperture.ForcedPhotometryResult()
        if target == "catalog_match":
            return frame_match.MatchCatalogResult()
        return {"selected_frames": 0}

    run_detection = target == "detection"
    run_forced_photometry = target == "forced_photometry"
    if target == "wcs":
        monkeypatch.setattr(runner_module, "check_frames_wcs", capture_call)
    elif run_detection:
        monkeypatch.setattr(runner_module, "detect_sources_in_frames", capture_call)
    elif run_forced_photometry:
        monkeypatch.setattr(runner_module, "run_forced_aperture_photometry", capture_call)
    elif target == "catalog_match":
        monkeypatch.setattr(runner_module, "match_catalog_to_frames", capture_call)
    else:
        monkeypatch.setattr(runner_module, "match_detections_to_catalog", capture_call)

    config = _runner_config(
        frame_ids=frame_ids,
        series_id=series_id,
        run_detection=run_detection,
        run_forced_photometry=run_forced_photometry,
    )
    if target == "catalog_match":
        config.skip_catalog_frame_match = False
    elif target == "detection_catalog_match":
        config.skip_detection_catalog_match = False
    elif target == "wcs":
        config.skip_wcs = False

    runner_module.run_field_pipeline(_NoSessionOperations(), config)

    assert observed_frame_ids == [expected_frame_ids]
    if target == "catalog_match":
        assert observed_kwargs[0]["max_sources_per_frame"] is None


def test_runner_skips_both_mutators_when_pre_qc_eliminates_the_resolved_scope(monkeypatch):
    mutator_calls: list[str] = []
    monkeypatch.setattr(runner_module, "_check_open_series_admission", lambda *_args: None)
    monkeypatch.setattr(
        runner_module,
        "run_frame_pre_qc",
        lambda *_args, **_kwargs: SimpleNamespace(
            summary=lambda: {"selected_frames": 1},
            selected_frame_ids=[10],
            bad_frame_ids=[10],
            detection_seeds={},
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "filter_bad_pre_qc_frame_ids",
        lambda _session, _frame_ids: [],
    )
    monkeypatch.setattr(
        runner_module,
        "detect_sources_in_frames",
        lambda *_args, **_kwargs: mutator_calls.append("detection"),
    )
    monkeypatch.setattr(
        runner_module,
        "run_forced_aperture_photometry",
        lambda *_args, **_kwargs: mutator_calls.append("forced_photometry"),
    )
    monkeypatch.setattr(
        runner_module,
        "match_detections_to_catalog",
        lambda *_args, **_kwargs: mutator_calls.append("detection_catalog_match"),
    )

    config = _runner_config(
        frame_ids=[10],
        series_id=None,
        run_detection=True,
        run_forced_photometry=True,
    )
    config.run_pre_photometry_qc = True
    config.exclude_bad_pre_qc_frames = True
    config.skip_detection_catalog_match = False

    result = runner_module.run_field_pipeline(_NoSessionOperations(), config)

    assert mutator_calls == []
    assert result.status == runner_module.NO_USABLE_FRAMES_STATUS
    assert "stop_no_usable_frames" in [step.step_name for step in result.steps]


def test_runner_detects_matches_once_then_aggregates_physical_support(monkeypatch):
    calls = []
    monkeypatch.setattr(runner_module, "_check_open_series_admission", lambda *_args: None)
    monkeypatch.setattr(
        runner_module,
        "detect_sources_in_frames",
        lambda *_args, **_kwargs: calls.append("detect") or {"selected_frames": 1},
    )
    monkeypatch.setattr(
        runner_module,
        "match_detections_to_catalog",
        lambda *_args, **_kwargs: calls.append("match") or {"detection_run_id": 44},
    )
    monkeypatch.setattr(
        runner_module,
        "aggregate_detection_catalog_support",
        lambda *_args, **_kwargs: calls.append("support") or {77: 1},
    )

    config = _runner_config(
        frame_ids=[10],
        series_id=None,
        run_detection=True,
        run_forced_photometry=False,
    )
    config.skip_detection_catalog_match = False

    result = runner_module.run_field_pipeline(_NoSessionOperations(), config)

    assert calls == ["detect", "match", "support"]
    selection = result.quality_policy["detection_support_selection"]
    assert selection["status"] == "DONE"
    assert selection["supported_catalog_sources"] == 1
    assert selection["eligible_catalog_sources"] == 1
    assert selection["detection_run_id"] == 44


def test_runner_rejects_explicit_empty_scope_before_session_operations():
    session = _NoSessionOperations()
    config = _runner_config(
        frame_ids=[],
        series_id=None,
        run_detection=False,
        run_forced_photometry=False,
    )

    with pytest.raises(ValueError, match="explicit frame_ids selection must not be empty"):
        runner_module.run_field_pipeline(session, config)

    assert session.calls == []
