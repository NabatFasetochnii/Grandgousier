from types import SimpleNamespace

import pytest

from grandgousier.lightcurves import target as target_module
from grandgousier.lightcurves.target import (
    TargetResolution,
    export_target_light_curve_product,
    resolve_target_object,
    validate_target_coordinates,
)


@pytest.fixture(autouse=True)
def _stub_light_curve_selection_preflight(monkeypatch):
    monkeypatch.setattr(
        "grandgousier.lightcurves.target.preflight_science_light_curve_selection",
        lambda *_args, **_kwargs: None,
    )


def test_target_export_prefers_explicit_frame_ids_over_series_selector(monkeypatch):
    calls = {}

    def fake_load_rows(session, **kwargs):
        calls.update(kwargs)
        return None, []

    monkeypatch.setattr(
        "grandgousier.lightcurves.target.load_science_light_curve_rows",
        fake_load_rows,
    )

    target = TargetResolution(resolved=True, object_id=6751)
    product = export_target_light_curve_product(
        object(),
        target=target,
        calibration_name="ensemble_v1",
        catalog_name="gaia",
        series_id=3,
        frame_ids=[19, 20, 21],
        reject_flags=set(),
    )

    assert product.rows_total == 0
    assert calls["series_id"] is None
    assert calls["frame_ids"] == [19, 20, 21]
    assert calls["object_id"] == 6751


def test_target_export_keeps_series_selector_when_no_explicit_frames(monkeypatch):
    calls = {}

    def fake_load_rows(session, **kwargs):
        calls.update(kwargs)
        return None, []

    monkeypatch.setattr(
        "grandgousier.lightcurves.target.load_science_light_curve_rows",
        fake_load_rows,
    )

    target = TargetResolution(resolved=True, object_id=6751)
    export_target_light_curve_product(
        object(),
        target=target,
        calibration_name="ensemble_v1",
        catalog_name="gaia",
        series_id=3,
        frame_ids=None,
        reject_flags=set(),
    )

    assert calls["series_id"] == 3
    assert calls["frame_ids"] is None


def test_cleanup_target_light_curve_artifacts_removes_stale_products(tmp_path):
    from grandgousier.lightcurves.target import cleanup_target_light_curve_artifacts

    prefix = "series_i_v1"
    stale_paths = [
        tmp_path / f"{prefix}_target_light_curve.csv",
        tmp_path / f"{prefix}_target_light_curve.json",
        tmp_path / f"{prefix}_target_light_curve.png",
    ]
    unrelated = tmp_path / "other_target_light_curve.csv"
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    result = cleanup_target_light_curve_artifacts(
        out_dir=tmp_path,
        calibration_prefix=prefix,
    )

    assert result["existing_artifacts"] == 3
    assert result["removed_artifacts"] == 3
    assert all(not path.exists() for path in stale_paths)
    assert unrelated.exists()


def test_cleanup_target_light_curve_artifacts_dry_run_preserves_products(tmp_path):
    from grandgousier.lightcurves.target import cleanup_target_light_curve_artifacts

    prefix = "series_i_v1"
    stale = tmp_path / f"{prefix}_target_light_curve.json"
    stale.write_text("stale", encoding="utf-8")

    result = cleanup_target_light_curve_artifacts(
        out_dir=tmp_path,
        calibration_prefix=prefix,
        dry_run=True,
    )

    assert result["existing_artifacts"] == 1
    assert result["removed_artifacts"] == 0
    assert stale.exists()


def test_target_resolution_negative_limit_without_explicit_id_skips_database():
    from grandgousier.lightcurves.target import resolve_target_object

    class FailingSession:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("database must not be queried when target resolution is disabled")

    result = resolve_target_object(
        FailingSession(),
        calibration_name="science_v1",
        catalog_name="gaia",
        max_sep_arcsec=-1.0,
    )

    assert result.resolved is False
    assert result.method == "disabled"
    assert "disabled" in str(result.reason)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _MeasuredSession:
    def __init__(
        self, objects, *, instruments=None, links=None, measurement_flags=None, headers=None
    ):
        self.objects = objects
        self.instruments = instruments or {10: "RoboPhot", 11: "RoboPhot"}
        self.links = links or {}
        self.measurement_flags = measurement_flags or {}
        self.headers = {frame_id: {} for frame_id in self.instruments}
        self.headers.update(headers or {})
        self.statements = []
        self.link_params = []

    def get(self, _model, _id):
        return SimpleNamespace(calibration_version_id=4)

    def scalars(self, _stmt):
        return _Rows([SimpleNamespace(object_id=key, ra_deg=ra, dec_deg=dec)
                      for key, (ra, dec, _source, _catalog, _frames) in self.objects.items()])

    def execute(self, stmt, *_args):
        sql = str(stmt)
        self.statements.append(sql)
        if "FROM frames" in sql:
            selected = set(next(iter(stmt.compile().params.values())))
            if "header_json" in sql:
                return _Rows(sorted((key, self.headers.get(key)) for key in selected if key in self.headers))
            return _Rows(sorted((key, value) for key, value in self.instruments.items()
                                if key in selected))
        if "object_catalog_links" in sql and "catalog_sources" in sql:
            # Synthetic link set is pre-scoped by the test's catalog name/source ID.
            self.link_params.append(stmt.compile().params)
            return _Rows(self.links.get("selected", []))
        raise AssertionError(f"unexpected query: {sql}")


def _resolve_measured(monkeypatch, session, **overrides):
    monkeypatch.setattr(target_module, "_load_calibration_rows", lambda *_args, **_kw: [
        {"calibration_version_id": 4}])

    def fake_view(_session, **_kw):
        return SimpleNamespace(rows=[
            SimpleNamespace(object_id=key, frame_id=frame_id,
                            flags=session.measurement_flags.get(
                                (key, frame_id), {"catalog_name": catalog, "source_id": source}
                            ))
            for key, (_ra, _dec, source, catalog, frames) in session.objects.items()
            for frame_id in frames
            if _kw["frame_ids"] is None or frame_id in _kw["frame_ids"]
        ])

    monkeypatch.setattr(target_module, "load_calibration_photometry_view", fake_view)
    frame_ids = overrides.pop("frame_ids", [10, 11])
    return resolve_target_object(session, calibration_name="cal", catalog_name="local_catalog",
                                 frame_ids=frame_ids, **overrides)


def test_robophot_sky_target_off_center_beats_bright_center_star(monkeypatch):
    # The input ID is a fixture, not a claim that different Gaia releases are identical.
    wanted = "531486745299867904"
    session = _MeasuredSession({
        2425: (10.0, 20.0, "center_bright", "local_catalog", [10, 11]),
        2275: (10.08, 20.0, wanted, "local_catalog", [10]),
    })
    result = _resolve_measured(monkeypatch, session, target_ra_deg=10.08, target_dec_deg=20.0)
    assert result.resolved and result.object_id == 2275
    assert result.method == result.coordinate_provenance == "explicit_object_coordinates"
    assert result.sep_arcsec == pytest.approx(0)
    assert result.to_dict()["coordinate_provenance"] == "explicit_object_coordinates"
    assert all("FROM frames" in sql for sql in session.statements)


def test_robophot_without_identity_does_not_choose_center(monkeypatch):
    session = _MeasuredSession({1: (10.0, 20.0, "bright", "local_catalog", [10, 11])})
    result = _resolve_measured(monkeypatch, session)
    assert not result.resolved and result.object_id is None
    assert result.method == "not_requested"


def test_robophot_close_pair_is_unresolved_even_with_override(monkeypatch):
    session = _MeasuredSession({
        1: (10.0, 20.0, "one", "local_catalog", [10, 11]),
        2: (10.0002, 20.0, "two", "local_catalog", [10, 11]),
    })
    result = _resolve_measured(monkeypatch, session, target_object_id=1,
                               target_ra_deg=10.0, target_dec_deg=20.0)
    assert not result.resolved and "multiple measured" in result.reason
    assert result.nearest_object_id == 1


def test_robophot_spherical_ra_wrap_and_high_declination(monkeypatch):
    session = _MeasuredSession({
        7: (0.01, 89.9, "wrap", "local_catalog", [10, 11]),
        8: (1.0, 89.9, "far", "local_catalog", [10, 11]),
    })
    result = _resolve_measured(monkeypatch, session, target_ra_deg=359.99,
                               target_dec_deg=89.9)
    assert result.resolved and result.object_id == 7
    assert 0 < result.sep_arcsec < 1


@pytest.mark.parametrize("instruments", [
    {10: "RoboPhot", 11: "MASTER"}, {10: "RoboPhot"},
    {10: "UNKNOWN", 11: "UNKNOWN"},
])
def test_scope_instrument_is_fail_closed(monkeypatch, instruments):
    session = _MeasuredSession({1: (10.0, 20.0, "one", "local_catalog", [10, 11])},
                               instruments=instruments)
    result = _resolve_measured(monkeypatch, session, target_object_id=1)
    assert not result.resolved and result.method == "frame_scope"


def test_explicit_object_and_unique_catalog_link_are_stable(monkeypatch):
    session = _MeasuredSession({
        7: (10.0, 20.0, "same", "local_catalog", [10]),
        8: (10.01, 20.0, "same", "local_catalog", [10, 11]),
    }, links={"selected": [(7, 900, 10.0001, 20.0)]})
    explicit = _resolve_measured(monkeypatch, session, target_object_id=7)
    scoped = _resolve_measured(monkeypatch, session, target_source_id="same")
    assert explicit.resolved and explicit.object_id == 7
    assert scoped.resolved and scoped.object_id == 7
    assert scoped.catalog_source_id == 900
    assert scoped.method == scoped.coordinate_provenance == "catalog_identifier"
    assert scoped.target_ra_deg == 10.0001 and scoped.target_dec_deg == 20.0
    assert 0 < scoped.sep_arcsec < 2
    assert explicit.target_ra_deg == 10.0 and explicit.target_dec_deg == 20.0
    assert explicit.coordinate_provenance == "object_record"
    assert any("catalog_sources" in sql and "object_catalog_links" in sql
               and "catalog_name" in sql and "source_id" in sql
               and "ra_deg" in sql and "dec_deg" in sql for sql in session.statements)
    assert "local_catalog" in session.link_params[-1].values()
    assert "same" in session.link_params[-1].values()


def test_same_catalog_source_and_object_resolves_across_two_filters(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "same", "local_catalog", [10, 11])},
                               links={"selected": [(7, 900, 10.0001, 20.0)]},
                               measurement_flags={
                                   (7, 10): {"catalog_name": "local_catalog", "source_id": "same",
                                             "catalog_source_id": "900"},
                                   (7, 11): {"catalog_name": "local_catalog", "source_id": "same",
                                             "catalog_source_id": 900},
                               })
    in_i = _resolve_measured(monkeypatch, session, frame_ids=[10], target_source_id="same")
    in_r = _resolve_measured(monkeypatch, session, frame_ids=[11], target_source_id="same")
    assert in_i.resolved and in_r.resolved
    assert in_i.object_id == in_r.object_id == 7
    assert in_i.catalog_source_id == in_r.catalog_source_id == 900
    assert in_i.n_measurements == in_r.n_measurements == 1
    assert (in_i.target_ra_deg, in_i.target_dec_deg) == (in_r.target_ra_deg, in_r.target_dec_deg)


@pytest.mark.parametrize("bad_flags", [
    {"catalog_name": "local_catalog", "source_id": "another", "catalog_source_id": 900},
    {"catalog_name": "local_catalog", "source_id": "same", "catalog_source_id": 901},
    {"catalog_name": "local_catalog", "source_id": "same", "catalog_source_id": "bad"},
    {"catalog_name": "local_catalog", "source_id": "same", "catalog_source_id": 900.5},
])
def test_linked_source_rejects_conflicting_measured_flags_in_either_filter(monkeypatch, bad_flags):
    session = _MeasuredSession({7: (10.0, 20.0, "same", "local_catalog", [10, 11])},
                               links={"selected": [(7, 900, 10.0, 20.0)]},
                               measurement_flags={
                                   (7, 10): {"catalog_name": "local_catalog", "source_id": "same",
                                             "catalog_source_id": 900},
                                   (7, 11): bad_flags,
                               })
    result = _resolve_measured(monkeypatch, session, target_source_id="same")
    assert not result.resolved and result.object_id is None
    assert result.target_ra_deg is None and result.target_dec_deg is None
    assert "contradict" in result.reason


def test_source_id_rejects_unreasonable_catalog_object_separation(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "same", "local_catalog", [10, 11])},
                               links={"selected": [(7, 900, 10.01, 20.0)]})
    result = _resolve_measured(monkeypatch, session, target_source_id="same")
    assert not result.resolved and result.target_ra_deg is None
    assert "over 2 arcsec" in result.reason


def test_explicit_source_and_sky_must_agree_with_each_other(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "same", "local_catalog", [10, 11])},
                               links={"selected": [(7, 900, 10.0004, 20.0)]})
    result = _resolve_measured(monkeypatch, session, target_source_id="same",
                               target_ra_deg=9.9996, target_dec_deg=20.0)
    assert not result.resolved and "disagree" in result.reason
    assert result.coordinate_provenance == "explicit_object_coordinates"


def test_conflicting_source_id_and_sky_override_fail_closed(monkeypatch):
    session = _MeasuredSession({
        7: (10.0, 20.0, "one", "local_catalog", [10, 11]),
        8: (10.01, 20.0, "two", "local_catalog", [10, 11]),
    }, links={"selected": [(7, 900, 10.0, 20.0)]})
    assert not _resolve_measured(monkeypatch, session, target_source_id="one",
                                 target_object_id=8).resolved
    assert not _resolve_measured(monkeypatch, session, target_source_id="one",
                                 target_ra_deg=10.01, target_dec_deg=20.0).resolved
    session.links["selected"] = [(7, 900, 10.0, 20.0), (8, 901, 10.01, 20.0)]
    assert not _resolve_measured(monkeypatch, session, target_source_id="one").resolved
    session.links["selected"] = []
    assert not _resolve_measured(monkeypatch, session, target_source_id="one").resolved


def test_explicit_object_without_measurements_is_unresolved(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "one", "local_catalog", [10, 11])})
    result = _resolve_measured(monkeypatch, session, target_object_id=99)
    assert not result.resolved and "no measurements" in result.reason


def test_sky_target_outside_two_arcsec_is_unresolved_not_nearest(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "near", "local_catalog", [10, 11])})
    result = _resolve_measured(monkeypatch, session, target_ra_deg=10.01,
                               target_dec_deg=20.0)
    assert not result.resolved and result.object_id is None
    assert result.nearest_object_id == 7 and result.nearest_sep_arcsec > 2


def test_source_id_requires_namespace(monkeypatch):
    session = _MeasuredSession({7: (10.0, 20.0, "one", "local_catalog", [10, 11])})
    monkeypatch.setattr(target_module, "_load_calibration_rows",
                        lambda *_a, **_kw: [{"calibration_version_id": 4}])
    monkeypatch.setattr(target_module, "load_calibration_photometry_view",
                        lambda *_a, **_kw: SimpleNamespace(rows=[
                            SimpleNamespace(object_id=7, frame_id=10,
                                            flags={"catalog_name": "local_catalog"})]))
    result = resolve_target_object(session, calibration_name="cal", frame_ids=[10],
                                   target_source_id="one")
    assert not result.resolved and "catalog_name" in result.reason
    assert session.link_params == []


@pytest.mark.parametrize("ra, dec", [
    (10, None), (None, 0), (360, 0), (-0.1, 0), (1, 90.1),
    (1, -90.1), (float("nan"), 0), (1, float("inf")),
])
def test_target_coordinate_pair_validation(ra, dec):
    with pytest.raises(ValueError):
        validate_target_coordinates(ra, dec)
    assert validate_target_coordinates(0, -90) == (0, -90)


@pytest.mark.parametrize(
    "instrument,headers",
    [
        (
            "MASTER",
            {
                10: {"ALPHA": "10 00 00", "DELTA": "+20 00 00", "CRVAL1": 150.0, "CRVAL2": 20.0},
                11: {"ALPHA": "10 00 00", "DELTA": "+20 00 00", "CRVAL1": 150.0, "CRVAL2": 20.0},
            },
        ),
        ("RoboPhot", {}),
    ],
)
def test_target_not_requested_returns_before_database_queries(
    monkeypatch, instrument, headers
):
    session = _MeasuredSession(
        {1: (150.0, 20.0, "bright", "local_catalog", [10, 11])},
        instruments={10: instrument, 11: instrument},
        headers=headers,
    )

    def unexpected_query(*_args, **_kwargs):
        raise AssertionError("no target selector must return before any database query")

    monkeypatch.setattr(target_module, "_load_calibration_rows", unexpected_query)
    result = resolve_target_object(
        session,
        calibration_name="cal",
        catalog_name="local_catalog",
        frame_ids=[10, 11],
        max_sep_arcsec=10000.0,
    )

    assert not result.resolved
    assert result.method == "not_requested"
    assert result.reason == "no target selector was supplied"
    assert result.target_ra_deg is None and result.target_dec_deg is None
    assert session.statements == []


def test_master_explicit_measured_object_remains_resolvable(monkeypatch):
    session = _MeasuredSession({1: (10.0, 20.0, "star", "local_catalog", [10, 11])},
                               instruments={10: "MASTER", 11: "MASTER"})
    result = _resolve_measured(monkeypatch, session, target_object_id=1,
                               max_sep_arcsec=-1.0)
    assert result.resolved and result.object_id == 1
    assert result.target_ra_deg == 10.0 and result.target_dec_deg == 20.0
    assert result.coordinate_provenance == "object_record"


def test_master_explicit_unmeasured_frame_participates_in_instrument_safety(monkeypatch):
    session = _MeasuredSession(
        {7: (150.0, 20.0, "target", "local_catalog", [10, 11])},
        instruments={10: "MASTER", 11: "MASTER", 12: "RoboPhot"},
    )

    result = _resolve_measured(
        monkeypatch, session, frame_ids=[10, 11, 12], target_object_id=7
    )

    assert not result.resolved and result.method == "frame_scope"


def test_explicit_master_target_inputs_remain_resolvable(monkeypatch):
    session = _MeasuredSession(
        {7: (150.0, 20.0, "target", "local_catalog", [10, 11])},
        instruments={10: "MASTER", 11: "MASTER"},
        links={"selected": [(7, 900, 150.0, 20.0)]},
        headers={
            10: {"ALPHA": "10 00 00", "DELTA": "+20 00 00", "CRVAL1": 150.0},
            11: {"ALPHA": "11 00 00", "DELTA": "+21 00 00", "CRVAL1": 151.0},
        },
    )

    by_object_id = _resolve_measured(monkeypatch, session, target_object_id=7)
    by_source_id = _resolve_measured(monkeypatch, session, target_source_id="target")
    by_coordinates = _resolve_measured(
        monkeypatch, session, target_ra_deg=150.0, target_dec_deg=20.0
    )

    assert by_object_id.resolved and by_object_id.method == "target_object_id"
    assert by_source_id.resolved and by_source_id.method == "catalog_identifier"
    assert by_coordinates.resolved and by_coordinates.method == "explicit_object_coordinates"
    assert not any("header_json" in sql for sql in session.statements)


def test_explicit_object_id_resolves_when_object_sky_coordinates_are_null(monkeypatch):
    session = _MeasuredSession({7: (None, None, "target", "local_catalog", [10, 11])})

    result = _resolve_measured(monkeypatch, session, target_object_id=7)

    assert result.resolved and result.object_id == 7
    assert result.method == "target_object_id"
    assert result.ra_deg is None and result.dec_deg is None
    assert result.target_ra_deg is None and result.target_dec_deg is None
    assert result.coordinate_provenance is None


def test_catalog_source_identity_resolves_when_both_position_pairs_are_unavailable(monkeypatch):
    session = _MeasuredSession(
        {7: (None, None, "target", "local_catalog", [10, 11])},
        links={"selected": [(7, 900, None, None)]},
    )

    result = _resolve_measured(monkeypatch, session, target_source_id="target")

    assert result.resolved and result.object_id == 7
    assert result.method == "catalog_identifier"
    assert result.catalog_source_id == 900
    assert result.target_ra_deg is None and result.target_dec_deg is None
    assert result.coordinate_provenance is None


def test_catalog_source_identity_uses_object_position_when_catalog_position_is_unavailable(
    monkeypatch,
):
    session = _MeasuredSession(
        {7: (10.0, 20.0, "target", "local_catalog", [10, 11])},
        links={"selected": [(7, 900, None, None)]},
    )

    result = _resolve_measured(monkeypatch, session, target_source_id="target")

    assert result.resolved and result.object_id == 7
    assert (result.target_ra_deg, result.target_dec_deg) == (10.0, 20.0)
    assert result.coordinate_provenance == "object_record"


def test_explicit_coordinates_with_unavailable_identity_position_remain_unresolved(monkeypatch):
    session = _MeasuredSession(
        {7: (None, None, "target", "local_catalog", [10, 11])},
        links={"selected": [(7, 900, None, None)]},
    )

    result = _resolve_measured(
        monkeypatch,
        session,
        target_source_id="target",
        target_ra_deg=150.0,
        target_dec_deg=20.0,
    )

    assert not result.resolved
    assert "cannot compare explicit target coordinates" in result.reason


def test_explicit_object_id_with_unavailable_position_cannot_compare_explicit_coordinates(
    monkeypatch,
):
    session = _MeasuredSession({7: (None, None, "target", "local_catalog", [10, 11])})

    result = _resolve_measured(
        monkeypatch,
        session,
        target_object_id=7,
        target_ra_deg=150.0,
        target_dec_deg=20.0,
    )

    assert not result.resolved
    assert "cannot compare explicit target coordinates" in result.reason


@pytest.mark.parametrize(
    "target,config_overrides,expected_method,expected_status",
    [
        (
            TargetResolution(
                resolved=False,
                method="not_requested",
                reason="no target selector was supplied",
            ),
            {},
            "not_requested",
            "UNRESOLVED_NO_TRUSTED_TARGET_INPUT",
        ),
        (
            TargetResolution(
                resolved=False,
                object_id=999,
                method="target_object_id",
                reason="target object has no measurements in selected calibration/frame set",
            ),
            {"target_object_id": 999},
            "target_object_id",
            "EXPLICIT_OBJECT_ID",
        ),
    ],
)
def test_runner_summary_distinguishes_not_requested_from_explicit_unresolved(
    target, config_overrides, expected_method, expected_status
):
    from grandgousier.pipeline.runner import (
        FieldPipelineConfig,
        PipelineRunResult,
        PipelineStepRecord,
        _quality_policy_summary,
        _run_summary,
    )

    config = FieldPipelineConfig(
        catalog_name="local_catalog",
        calibration_prefix="synthetic",
        target_ra_deg=None,
        target_dec_deg=None,
        **config_overrides,
    )
    result = PipelineRunResult(
        run_uid="test",
        pipeline_run_id=None,
        status="DONE",
        calibration_names={},
        steps=[PipelineStepRecord("resolve_target_object", "DONE", target.to_dict())],
    )
    result.quality_policy = _quality_policy_summary(config, result)

    policy = _run_summary(result)["quality_policy"]

    assert policy["target_provenance_status"] == expected_status
    assert policy["target_requested_provenance_status"] == expected_status
    assert policy["target_resolved"] is False
    assert policy["target_resolution_method"] == expected_method
    assert policy["target_coordinate_provenance"] is None
    assert policy["target_resolution_reason"] == target.reason


def test_runner_rejects_incomplete_target_pair_before_pipeline_activity():
    from grandgousier.pipeline.runner import FieldPipelineConfig, run_field_pipeline

    config = FieldPipelineConfig(catalog_name="local_catalog", calibration_prefix="synthetic",
                                 target_ra_deg=10.0)
    with pytest.raises(ValueError, match="supplied together"):
        run_field_pipeline(object(), config)


def test_runner_default_provenance_does_not_advertise_center_resolution():
    from grandgousier.pipeline.runner import FieldPipelineConfig, _target_provenance_status

    config = FieldPipelineConfig(catalog_name="local_catalog", calibration_prefix="synthetic")
    assert _target_provenance_status(config) == "UNRESOLVED_NO_TRUSTED_TARGET_INPUT"
    config.target_max_sep_arcsec = -1.0
    assert _target_provenance_status(config) == "DISABLED_UNRESOLVED"
