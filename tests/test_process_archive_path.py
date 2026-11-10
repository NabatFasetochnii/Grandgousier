from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from astropy.io import fits
import numpy as np

from grandgousier.archive import auto_process as auto_process_module
from grandgousier.archive.auto_process import (
    _catalog_precondition_ok,
    _classify_science_status,
    _default_auto_catalog_name,
    _effective_auto_catalog_max_radius_deg,
    _effective_auto_catalog_mag_limit,
    _format_calibration_prefix,
    _inspect_existing_catalog,
    _merge_current_series_ids,
    _prepare_auto_catalog,
    discover_fits_files,
    DetectedArchiveFile,
    process_archive_path,
    scan_archive_path,
)
from grandgousier.io.archive_indexer import index_archive_paths
from grandgousier.instruments.base import InstrumentProfile


class FakeQuery:
    def __init__(self, exists=False):
        self.exists = exists

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return (1,) if self.exists else None

    def one_or_none(self):
        return None


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0
        self.flushes = 0

    def query(self, *_args, **_kwargs):
        return FakeQuery(False)

    def scalar(self, *_args, **_kwargs):
        return None

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def flush(self):
        self.flushes += 1


def _write_master_fits(path: Path, *, filt="I", camera="204"):
    hdu = fits.PrimaryHDU()
    h = hdu.header
    h["DATE-OBS"] = "2023-09-20T03:13:14"
    h["MJD"] = 60206.925859
    h["EXPTIME"] = 180.0
    h["FILTER"] = filt
    h["OBJNAME"] = "TIC420268415_01"
    h["IMAGETYP"] = "LIGHT"
    h["OBSERVAT"] = "KAO"
    h["AXIS"] = "EAST"
    h["ID_CAM"] = camera
    h["CRVAL1"] = 308.42
    h["CRVAL2"] = 87.01
    hdu.writeto(path)


def _write_robophot_fits(path: Path, *, filt="g", camera="ML0742515"):
    hdu = fits.PrimaryHDU()
    h = hdu.header
    h["DATE-OBS"] = "2024-01-01T20:00:00"
    h["MJD-OBS"] = 60310.8
    h["EXPTIME"] = 60.0
    h["FILTER"] = filt
    h["OBJECT"] = "RoboPhotTarget"
    h["IMAGETYP"] = "OBJECT"
    h["OBSERVAT"] = "KAO"
    h["TELESCOP"] = "APM-RoboPhot/TRIP"
    h["SERNUM"] = camera
    h["CRVAL1"] = 100.0
    h["CRVAL2"] = 30.0
    hdu.writeto(path)


def test_scan_archive_path_auto_detects_master_and_robophot(tmp_path):
    _write_master_fits(tmp_path / "master_I.fits")
    _write_robophot_fits(tmp_path / "rp_g.fit")

    detected, groups, warnings = scan_archive_path(tmp_path)

    assert warnings == []
    assert {item.profile_name for item in detected} == {"MASTER", "RoboPhot"}
    keys = {(g.profile_name, g.filter_name, g.camera, g.n_files) for g in groups}
    assert ("MASTER", "I", "204", 1) in keys
    assert ("RoboPhot", "g", "ML0742515", 1) in keys


def test_target_coordinate_pair_rejected_before_archive_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_process_module, "scan_archive_path",
                        lambda *_a, **_kw: pytest.fail("archive scanner must not run"))
    with pytest.raises(ValueError, match="supplied together"):
        process_archive_path(object(), root=tmp_path, target_ra_deg=20.0)


def test_open_series_are_reported_but_never_queued_for_pipeline():
    rows = [
        {"series_id": 11, "status": "READY"},
        {"series_id": 12, "status": "OPEN"},
        {"series_id": 13, "status": "PROCESSED"},
    ]

    finalized, open_ids = auto_process_module._separate_open_series(rows)

    assert [int(row["series_id"]) for row in finalized] == [11, 13]
    assert open_ids == [12]


def test_process_archive_path_forwards_streaming_and_skips_open_tail_pipeline(
    monkeypatch, tmp_path
):
    detected = _detected_master("204_STREAMING")
    source_path = Path(detected.path)
    captured = {}
    queued_ids = []
    stage2_calls = []

    monkeypatch.setattr(
        auto_process_module,
        "scan_archive_paths",
        lambda *_args, **_kwargs: ([detected], [], []),
    )
    monkeypatch.setattr(auto_process_module, "load_auto_profiles", lambda **_kwargs: {})
    monkeypatch.setattr(
        auto_process_module,
        "index_detected_archive_files",
        lambda *_args, **_kwargs: SimpleNamespace(
            indexed_paths=[str(source_path)],
            indexed_frame_ids=[101],
            index_results=[],
            failed_files=0,
        ),
    )
    monkeypatch.setattr(
        auto_process_module,
        "select_persisted_science_frames",
        lambda *_args, **_kwargs: SimpleNamespace(
            selected_frame_ids=[101], summary={}, rows=[]
        ),
    )
    monkeypatch.setattr(
        auto_process_module,
        "_series_ids_for_paths",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        auto_process_module,
        "_inspect_existing_catalog",
        lambda *_args, **_kwargs: {"status": "READY"},
    )
    monkeypatch.setattr(auto_process_module, "_catalog_precondition_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(
        auto_process_module,
        "_frame_ids_by_instrument_unit",
        lambda *_args, **_kwargs: {4: [101]},
    )

    def fake_build(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            rows=[{"series_id": 7, "status": "OPEN"}],
            summary=lambda: {"open_series_ids": [7]},
            observation_selection_rows=[],
        )

    monkeypatch.setattr(auto_process_module, "build_field_series", fake_build)
    monkeypatch.setattr(
        auto_process_module,
        "_frame_ids_by_series_for_paths",
        lambda *_args, **_kwargs: {},
    )

    def pipeline_rows(_session, series_ids):
        queued_ids.extend(series_ids)
        return []

    monkeypatch.setattr(auto_process_module, "_load_series_for_pipeline", pipeline_rows)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: stage2_calls.append("called"),
    )

    result = process_archive_path(
        object(),
        root=tmp_path,
        exact_paths=[source_path],
        catalog_name="existing-test-catalog",
        execute=True,
        min_frames_per_series=2,
        finalize_tail=False,
    )

    assert captured["finalize_tail"] is False
    assert result.open_series_ids == [7]
    assert queued_ids == []
    assert stage2_calls == []


def test_process_archive_path_plan_accepts_degree_pair_without_processing(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_process_module, "scan_archive_paths",
                        lambda *_a, **_kw: ([], [], []))
    monkeypatch.setattr(auto_process_module, "load_auto_profiles", lambda **_kw: {})
    monkeypatch.setattr(auto_process_module, "_default_auto_catalog_name",
                        lambda *_a, **_kw: "local_catalog")
    monkeypatch.setattr(auto_process_module, "plan_file_science_selection",
                        lambda *_a, **_kw: SimpleNamespace(summary={}, rows=[]))
    result = process_archive_path(object(), root=tmp_path, exact_paths=[], execute=False,
                                  target_ra_deg=0.0, target_dec_deg=90.0)
    assert result.status == "PLAN"
    assert result.stage2_profile == "bulk"
    assert result.effective_stage2_settings["profile"] == "bulk"
    assert result.effective_stage2_settings["random_error_validation"] is True
    assert result.effective_stage2_settings["random_error_legacy_comparison"] is False
    assert (
        result.effective_stage2_settings["random_error_residual_floor_diagnostics"]
        is True
    )
    assert (
        result.effective_stage2_settings[
            "random_error_validation_and_legacy_comparison"
        ]
        is False
    )
    assert result.effective_stage2_settings["random_error_diagnostic_exports"] is False
    assert result.effective_stage2_settings["light_curve_summary_row_materialization"] is False
    assert result.effective_stage2_settings["light_curve_summary_compact_counts"] is True
    assert not result.pipeline_results


def test_process_archive_path_cli_passes_coordinate_pair_before_db_open(monkeypatch):
    from grandgousier import cli

    parser = cli.build_parser()
    args = parser.parse_args(["process-archive-path", "--path", "synthetic",
                              "--target-ra-deg", "22.5", "--target-dec-deg", "67.25"])
    args.target_dec_deg = None
    monkeypatch.setattr(cli, "make_session_factory",
                        lambda *_a, **_kw: pytest.fail("invalid pair must not open DB"))
    assert cli.cmd_process_archive_path(args) == 2
    args.target_dec_deg = 67.25
    args.finalize_tail = False

    class FakeSessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli, "make_session_factory", lambda *_a: FakeSessionContext)
    observed = {}

    def fake_process(*_args, **kwargs):
        observed.update(kwargs)
        raise RuntimeError("synthetic handoff reached")

    monkeypatch.setattr(auto_process_module, "process_archive_path", fake_process)
    with pytest.raises(RuntimeError, match="synthetic handoff reached"):
        cli.cmd_process_archive_path(args)
    assert observed["target_ra_deg"] == 22.5
    assert observed["target_dec_deg"] == 67.25
    assert observed["execute"] is False
    assert observed["finalize_tail"] is False


def test_discover_fits_files_is_suffix_based_and_sorted(tmp_path):
    (tmp_path / "b.FITS").write_text("")
    (tmp_path / "a.fit").write_text("")
    (tmp_path / "ignore.txt").write_text("")

    paths = discover_fits_files(tmp_path)

    assert [p.name for p in paths] == ["a.fit", "b.FITS"]


def test_index_archive_paths_supports_explicit_mixed_file_lists(tmp_path):
    path = tmp_path / "master_I.fits"
    _write_master_fits(path)
    profile = InstrumentProfile.from_yaml("configs/master.yaml")
    session = FakeSession()

    result = index_archive_paths(session=session, paths=[path], profile=profile)

    assert result.discovered == 1
    assert result.inserted == 1
    assert result.failed == 0
    assert session.commits >= 1


def _detected_master(native_id, *, status="READY"):
    return DetectedArchiveFile(
        path=f"C:/archive/{native_id}.fits", profile_name="MASTER", instrument="MASTER",
        site="site", tube="tube", camera="204", filter_name="V", object_name="field",
        date_obs=None, mjd=None, image_type="LIGHT", observation_uid=f"master:{native_id}",
        identity_scheme="MASTER_UNIQNUM", native_identity=native_id,
        observation_identity_status=status,
    )


def _patch_native_scan(monkeypatch, ids):
    from grandgousier.archive import auto_process

    monkeypatch.setattr(auto_process, "scan_archive_path", lambda *_a, **_k: (
        [_detected_master(value) for value in ids], [], []
    ))
    monkeypatch.setattr(auto_process, "load_auto_profiles", lambda **_k: {
        "MASTER": InstrumentProfile.from_yaml("configs/master.yaml")
    })


def test_native_selector_indexes_only_exact_247_requested_ids(monkeypatch):
    ids = [f"U{i:03}" for i in range(272)]
    requested = ids[25:]
    _patch_native_scan(monkeypatch, ids)
    observed = []

    def index_spy(*, paths, **_kwargs):
        observed.extend(str(path) for path in paths)
        raise RuntimeError("index boundary reached")

    monkeypatch.setattr("grandgousier.archive.auto_process.index_archive_paths", index_spy)
    with pytest.raises(RuntimeError, match="index boundary reached"):
        process_archive_path(
            FakeSession(), root="C:/archive", execute=True,
            native_observation_ids=requested,
        )
    assert observed == [str(Path(f"C:/archive/{value}.fits")) for value in requested]
    assert len(observed) == 247
    assert all(str(Path(f"C:/archive/{value}.fits")) not in observed for value in ids[:25])


@pytest.mark.parametrize("requested", [
    ["U000", "U003"],                 # requested ID missing
    ["U000", "U001", "U001"],       # duplicate request
    ["U000", "U001", "OTHER"],      # unrequested identity
    ["U001", "U000"],                 # wrong order
])
def test_native_selector_rejects_invalid_lists_before_index(monkeypatch, requested):
    _patch_native_scan(monkeypatch, ["U000", "U001", "U002"])
    calls = []
    monkeypatch.setattr(
        "grandgousier.archive.auto_process.index_archive_paths",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(
            discovered=0, inserted=0, skipped_existing=0, failed=0
        ),
    )
    result = process_archive_path(
        FakeSession(), root="C:/archive", execute=True,
        native_observation_ids=requested,
    )
    assert result.status == "FAILED_PRECONDITION_NATIVE_OBSERVATION_SELECTION"
    assert calls == []


def test_native_selector_rejects_non_master_and_non_ready_before_index(monkeypatch):
    from grandgousier.archive import auto_process

    _patch_native_scan(monkeypatch, ["U000"])
    monkeypatch.setattr(auto_process, "scan_archive_path", lambda *a, **k: (
        [replace(_detected_master("U000"), profile_name="RoboPhot")], [], []
    ))
    calls = []
    monkeypatch.setattr("grandgousier.archive.auto_process.index_archive_paths", lambda **k: calls.append(k))
    result = process_archive_path(FakeSession(), root="C:/archive", execute=True,
                                  native_observation_ids=["U000"])
    assert result.status == "FAILED_PRECONDITION_NATIVE_OBSERVATION_SELECTION"
    assert calls == []

    _patch_native_scan(monkeypatch, ["U000"])
    monkeypatch.setattr(auto_process, "scan_archive_path", lambda *a, **k: (
        [_detected_master("U000", status="INVALID")], [], []
    ))
    result = process_archive_path(FakeSession(), root="C:/archive", execute=True,
                                  native_observation_ids=["U000"])
    assert result.status == "FAILED_PRECONDITION_NATIVE_OBSERVATION_SELECTION"
    assert calls == []


def test_default_auto_prefix_contains_instrument_date_field_filter_and_series_id():
    prefix = _format_calibration_prefix(
        None,
        {
            "series_id": 12,
            "field_id": 5,
            "field_name": "TIC420268415_01",
            "instrument": "MASTER",
            "tube": "WEST",
            "camera": "203",
            "filter_name": "B",
            "time_start": "2023-09-20T03:13:14+05:00",
        },
    )

    # Auto-processing names use the canonical UTC series date.  The input
    # instant is 2023-09-19T22:13:14Z even though its source offset is +05:00.
    assert prefix == "auto_master_20230919_TIC420268415_01_WEST_203_B_s12_v1"



def test_default_auto_catalog_name_uses_detected_group_metadata(tmp_path):
    _write_robophot_fits(tmp_path / "rp_i.fits", filt="i")
    _write_robophot_fits(tmp_path / "rp_i_2.fits", filt="i")

    _detected, groups, warnings = scan_archive_path(tmp_path, instrument_profile="RoboPhot")

    assert warnings == []
    assert _default_auto_catalog_name(groups, root=tmp_path) == "auto_catalog_robophot_20240101_RoboPhotTarget_i"


def test_auto_catalog_mag_limit_inherits_working_limit_unless_explicit():
    assert _effective_auto_catalog_mag_limit(explicit=None, working=19.0) == pytest.approx(19.0)
    assert _effective_auto_catalog_mag_limit(explicit=18.0, working=19.0) == pytest.approx(18.0)
    assert _effective_auto_catalog_mag_limit(explicit=None, working=None) is None


def test_auto_catalog_radius_default_is_master_aware():
    master = [SimpleNamespace(profile_name="MASTER", instrument="MASTER")]
    robophot = [SimpleNamespace(profile_name="RoboPhot", instrument="APM-RoboPhot")]

    assert _effective_auto_catalog_max_radius_deg(master, explicit=None) == pytest.approx(1.6)
    assert _effective_auto_catalog_max_radius_deg(robophot, explicit=None) == pytest.approx(1.0)
    assert _effective_auto_catalog_max_radius_deg(master, explicit=2.5) == pytest.approx(2.5)


def test_merge_current_series_ids_keeps_only_explicitly_supplied_series():
    assert _merge_current_series_ids([4, 4], [4, 9]) == [4, 9]
    assert _merge_current_series_ids([], []) == []


def test_classify_science_status_distinguishes_empty_science_products():
    assert _classify_science_status(0, 0, 0) == "WARNING_NO_MEASUREMENTS"
    assert _classify_science_status(10, 0, 1) == "WARNING_NO_ACCEPTED_POINTS"
    assert _classify_science_status(10, 3, 1) == "OK"


@pytest.mark.parametrize("stage2_profile", ["bulk", "diagnostic"])
@pytest.mark.parametrize(
    ("accepted_points", "expected_status"),
    [(4, "OK"), (0, "WARNING_NO_ACCEPTED_POINTS")],
)
def test_process_archive_path_uses_profile_appropriate_light_curve_summary(
    monkeypatch, tmp_path, stage2_profile, accepted_points, expected_status
):
    import json

    from grandgousier.lightcurves.product import LightCurveScope, LightCurveSummary

    source_path = Path("synthetic_master.fits")
    detected = _detected_master("SUMMARY_PATH")
    series = {
        "series_id": 12,
        "field_id": 3,
        "field_name": "synthetic_field",
        "instrument_unit_id": 4,
        "filter_name": "V",
        "instrument": "MASTER",
        "tube": "EAST",
        "camera": "204",
        "object_name": "synthetic",
        "time_start": "2024-01-01T00:00:00+00:00",
    }
    pipeline_result = SimpleNamespace(
        pipeline_run_id=None,
        status="DONE",
        calibration_names={"science": "science_12"},
        outputs={},
        quality_policy={"require_detection_support_effective": False},
        steps=[],
        log_files={},
    )
    summary_calls = {"compact": 0, "full": 0}

    def fake_compact_summary(_session, **_kwargs):
        summary_calls["compact"] += 1
        return SimpleNamespace(
            total_points=8,
            accepted_points=accepted_points,
            rejected_points=8 - accepted_points,
            n_objects=3,
        )

    def fake_full_summary(_session, **_kwargs):
        summary_calls["full"] += 1
        return LightCurveSummary(
            scope=LightCurveScope(
                calibration_names=["science_12"],
                calibration_version_ids=[101],
                series_id=12,
                field_name="synthetic_field",
            ),
            total_points=8,
            accepted_points=accepted_points,
            rejected_points=8 - accepted_points,
            n_objects=3,
            n_series=1,
            n_frames=8,
            n_calibrations=1,
            n_with_interseries_offset=0,
            n_without_interseries_offset=8,
            n_reject_reasons={"HIGH_RMS": 4},
            n_filters={"V": 8},
        )

    monkeypatch.setattr(
        auto_process_module,
        "scan_archive_paths",
        lambda *_args, **_kwargs: ([detected], [], []),
    )
    monkeypatch.setattr(auto_process_module, "load_auto_profiles", lambda **_kwargs: {})
    monkeypatch.setattr(
        auto_process_module,
        "index_detected_archive_files",
        lambda *_args, **_kwargs: SimpleNamespace(
            indexed_paths=[str(source_path)],
            indexed_frame_ids=[101],
            index_results=[],
            failed_files=0,
        ),
    )
    monkeypatch.setattr(
        auto_process_module,
        "select_persisted_science_frames",
        lambda *_args, **_kwargs: SimpleNamespace(
            selected_frame_ids=[101], summary={"selected_frames": 1}, rows=[]
        ),
    )
    monkeypatch.setattr(
        auto_process_module,
        "_observation_selection_warning",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(auto_process_module, "_series_ids_for_paths", lambda *_args: [])
    monkeypatch.setattr(
        auto_process_module,
        "_inspect_existing_catalog",
        lambda *_args, **_kwargs: {"status": "READY"},
    )
    monkeypatch.setattr(auto_process_module, "_catalog_precondition_ok", lambda *_a, **_k: True)
    monkeypatch.setattr(
        auto_process_module, "_frame_ids_by_instrument_unit", lambda *_args: {4: [101]}
    )
    monkeypatch.setattr(
        auto_process_module,
        "build_field_series",
        lambda *_args, **_kwargs: SimpleNamespace(
            rows=[{"series_id": 12, "status": "READY"}],
            summary=lambda: {"ready_series": 1},
            observation_selection_rows=[],
        ),
    )
    monkeypatch.setattr(
        auto_process_module, "_frame_ids_by_series_for_paths", lambda *_args: {12: [101]}
    )
    monkeypatch.setattr(
        auto_process_module, "_load_series_for_pipeline", lambda *_args: [series]
    )
    monkeypatch.setattr(
        auto_process_module, "run_field_pipeline", lambda *_args, **_kwargs: pipeline_result
    )
    monkeypatch.setattr(
        auto_process_module, "build_light_curve_count_summary", fake_compact_summary
    )
    monkeypatch.setattr(
        auto_process_module, "build_light_curve_summary", fake_full_summary
    )

    result = process_archive_path(
        object(),
        root=tmp_path,
        exact_paths=[source_path],
        catalog_name="synthetic_catalog",
        execute=True,
        out_root=tmp_path,
        stage2_profile=stage2_profile,
    )

    expected_calls = {"compact": 1, "full": 0} if stage2_profile == "bulk" else {
        "compact": 0,
        "full": 1,
    }
    assert summary_calls == expected_calls
    assert result.pipeline_results[0]["science_status"] == expected_status
    assert result.pipeline_results[0]["total_points"] == 8
    assert result.pipeline_results[0]["accepted_points"] == accepted_points
    assert result.pipeline_results[0]["rejected_points"] == 8 - accepted_points
    assert result.pipeline_results[0]["n_objects"] == 3

    if expected_status == "OK":
        assert result.warnings == []
    else:
        assert result.warnings == [
            f"series_id=12 science_status={expected_status} total_points=8 accepted_points=0"
        ]

    summary_path = result.pipeline_results[0]["light_curve_summary_json"]
    if stage2_profile == "bulk":
        assert summary_path is None
    else:
        assert summary_path is not None
        exported = json.loads(Path(summary_path).read_text(encoding="utf-8"))
        assert exported["total_points"] == 8
        assert exported["accepted_points"] == accepted_points
        assert exported["rejected_points"] == 8 - accepted_points
        assert exported["n_objects"] == 3



def test_prepare_auto_catalog_reuses_populated_exact_name_without_gaia(monkeypatch):
    calls = {"gaia": 0}

    monkeypatch.setattr(
        "grandgousier.archive.auto_process._inspect_existing_catalog",
        lambda _session, *, catalog_name: {
            "mode": "explicit_existing",
            "catalog_name": catalog_name,
            "status": "DONE",
            "inserted_or_updated": 123,
            "n_sources": 123,
        },
    )

    # Importing/querying Gaia must never be reached when the preserved catalog
    # is already populated.  Patching the query symbol gives the test a clear
    # failure mode if that contract regresses.
    import grandgousier.catalogs.gaia_query as gaia_query

    def _unexpected_gaia(*_args, **_kwargs):
        calls["gaia"] += 1
        raise AssertionError("Gaia query should not run for a populated preserved catalog")

    monkeypatch.setattr(gaia_query, "query_gaia_for_frames", _unexpected_gaia)

    row = _prepare_auto_catalog(
        object(),
        catalog_name="auto_catalog_master_20230916_TIC13533330_01_V",
        frame_ids=[1, 2, 3],
        enabled=True,
        margin_arcmin=10.0,
        mag_limit=None,
        max_rows_per_frame=50000,
        query_mode="frame_union",
        max_radius_deg=1.0,
        timeout_sec=120.0,
        warnings=[],
    )

    assert calls["gaia"] == 0
    assert row["mode"] == "auto_existing_reuse"
    assert row["status"] == "DONE"
    assert row["reused_existing"] is True
    assert row["n_sources"] == 123
    assert row["inserted_or_updated"] == 123
    assert row["queried_frames"] == 0
    assert row["queried_cones"] == 0
    assert _catalog_precondition_ok(row, allow_empty_catalog=False)


def test_process_archive_path_forwards_indexed_frame_ids_to_gaia(monkeypatch, tmp_path):
    indexed_frame_ids = [17, 9]
    source_path = tmp_path / "indexed.fits"
    detected = SimpleNamespace(path=str(source_path), profile_name="MASTER")
    monkeypatch.setattr(
        auto_process_module,
        "scan_archive_paths",
        lambda *_args, **_kwargs: ([detected], [], []),
    )
    monkeypatch.setattr(auto_process_module, "load_auto_profiles", lambda **_kwargs: {"MASTER": object()})
    monkeypatch.setattr(
        auto_process_module,
        "index_archive_paths",
        lambda **_kwargs: SimpleNamespace(discovered=1, inserted=1, skipped_existing=0, failed=0),
    )
    monkeypatch.setattr(
        auto_process_module,
        "_frame_ids_for_paths",
        lambda *_args: list(indexed_frame_ids),
    )
    monkeypatch.setattr(auto_process_module, "_series_ids_for_paths", lambda *_args: [])
    monkeypatch.setattr(
        auto_process_module,
        "select_persisted_science_frames",
        lambda *_args, **_kwargs: SimpleNamespace(
            selected_frame_ids=list(indexed_frame_ids), summary={}, rows=[]
        ),
    )
    monkeypatch.setattr(auto_process_module, "_observation_selection_warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auto_process_module, "_default_auto_catalog_name", lambda *_args, **_kwargs: "auto-test")
    monkeypatch.setattr(
        auto_process_module,
        "_inspect_existing_catalog",
        lambda *_args, **_kwargs: {"n_sources": 0},
    )
    monkeypatch.setattr(auto_process_module, "_frame_ids_by_instrument_unit", lambda *_args: {})
    monkeypatch.setattr(auto_process_module, "_frame_ids_by_series_for_paths", lambda *_args: {})
    monkeypatch.setattr(auto_process_module, "_load_series_for_pipeline", lambda *_args: [])
    monkeypatch.setattr(auto_process_module, "_write_manifest_if_requested", lambda *_args, **_kwargs: None)

    import grandgousier.catalogs.gaia_query as gaia_query_module

    observed_frame_ids: list[list[int] | None] = []

    def fake_gaia_query(_session, **kwargs):
        observed_frame_ids.append(kwargs["frame_ids"])
        return SimpleNamespace(
            selected_frames=len(indexed_frame_ids),
            queried_frames=len(indexed_frame_ids),
            queried_cones=1,
            row_limit_reached_cones=0,
            read_rows=2,
            inserted_or_updated=2,
            failed_frames=0,
            skipped_missing_file=0,
            skipped_oversized_cones=0,
            failed_footprint_reasons={},
            sample_failed_footprints=[],
            query_provider="test-fake",
            query_provider_failures={},
            query_mode="frame_union",
            effective_query_mode="frame_union",
        )

    monkeypatch.setattr(gaia_query_module, "query_gaia_for_frames", fake_gaia_query)

    process_archive_path(
        object(),
        root=tmp_path,
        exact_paths=[source_path],
        execute=True,
        auto_catalog_query=True,
        auto_catalog_max_radius_deg=1.0,
    )

    assert observed_frame_ids == [indexed_frame_ids]


@pytest.mark.parametrize("with_manifest", [False, True])
def test_process_archive_path_stops_on_index_failure(monkeypatch, with_manifest):
    source_path = Path("fake_failed_frame.fits")
    detected = SimpleNamespace(path=str(source_path), profile_name="MASTER")
    fake_session = object()
    manifest_out = Path("fake_manifest.json") if with_manifest else None
    manifest_calls = []

    monkeypatch.setattr(
        auto_process_module,
        "scan_archive_paths",
        lambda *_args, **_kwargs: ([detected], [], []),
    )
    monkeypatch.setattr(auto_process_module, "load_auto_profiles", lambda **_kwargs: {"MASTER": object()})
    monkeypatch.setattr(auto_process_module, "_default_auto_catalog_name", lambda *_args, **_kwargs: "auto-test")

    def fake_index(*, session, paths, profile, dry_run, progress_desc):
        assert session is fake_session
        assert paths == [source_path]
        assert profile is not None
        assert dry_run is False
        assert progress_desc == "Indexing MASTER"
        return SimpleNamespace(discovered=1, inserted=0, skipped_existing=0, failed=1)

    monkeypatch.setattr(auto_process_module, "index_archive_paths", fake_index)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("downstream must not run after index failure")

    for name in (
        "_frame_ids_for_paths", "_series_ids_for_paths", "select_persisted_science_frames",
        "_prepare_auto_catalog", "_inspect_existing_catalog", "build_field_series",
        "run_field_pipeline",
    ):
        monkeypatch.setattr(auto_process_module, name, unexpected)

    import grandgousier.catalogs.gaia_query as gaia_query_module

    monkeypatch.setattr(gaia_query_module, "query_gaia_for_frames", unexpected)
    monkeypatch.setattr(
        auto_process_module,
        "_write_manifest_if_requested",
        lambda result, path, *, session: manifest_calls.append((result, path, session)),
    )

    result = process_archive_path(
        fake_session, root=Path("fake_archive"), exact_paths=[source_path],
        execute=True, **({"manifest_out": manifest_out} if with_manifest else {}),
    )

    assert result.status == "FAILED_INDEX_FILES"
    assert result.failed_files == 1
    assert result.index_results == [
        {"profile_name": "MASTER", "discovered": 1, "inserted": 0, "skipped_existing": 0, "failed": 1}
    ]
    assert result.warnings == ["archive indexing failed for 1 file(s)"]
    assert str(source_path) not in str(result.warnings)
    assert result.catalog_results == []
    assert result.series_results == result.pipeline_results == []
    assert result.indexed_frame_ids == result.created_series_ids == result.processed_series_ids == []
    assert manifest_calls == ([(result, manifest_out, fake_session)] if with_manifest else [])
    assert result.manifest_path is None


def test_catalog_precondition_blocks_failed_or_empty_auto_catalogs():
    assert not _catalog_precondition_ok({"status": "FAILED", "inserted_or_updated": 0}, allow_empty_catalog=False)
    assert not _catalog_precondition_ok({"status": "DONE", "inserted_or_updated": 0}, allow_empty_catalog=False)
    assert _catalog_precondition_ok({"status": "DONE", "inserted_or_updated": 3}, allow_empty_catalog=False)
    assert _catalog_precondition_ok({"status": "FAILED", "inserted_or_updated": 0}, allow_empty_catalog=True)


def test_combine_gaia_cones_keeps_union_radius_reasonable():
    from grandgousier.catalogs.gaia_query import _combine_cones

    ra, dec, radius = _combine_cones([(10.0, 20.0, 0.1), (10.1, 20.0, 0.1)])

    assert 9.9 < ra < 10.2
    assert 19.9 < dec < 20.1
    assert radius > 0.1
    assert radius < 0.3


def test_gaia_union_fallback_clusters_repeated_pointings():
    from grandgousier.catalogs.gaia_query import _cluster_cones

    clusters, oversized = _cluster_cones(
        [(10.0, 20.0, 1.50), (10.01, 20.0, 1.50), (40.0, 20.0, 1.50)],
        max_radius_deg=1.6,
    )

    assert oversized == []
    assert sorted(len(cluster) for cluster in clusters) == [1, 2]


def test_gaia_frame_cone_falls_back_to_indexed_center_and_pixel_scale(tmp_path):
    from grandgousier.catalogs.gaia_query import _frame_cone_for_frame

    path = tmp_path / "robophot_without_wcs.fits"
    fits.PrimaryHDU(data=np.zeros((100, 200), dtype="uint16")).writeto(path)
    frame = SimpleNamespace(
        raw_path=str(path),
        ra_center_deg=100.0,
        dec_center_deg=30.0,
        instrument_unit=SimpleNamespace(pixel_scale_arcsec=0.65, config_json={}),
    )

    ra, dec, radius = _frame_cone_for_frame(frame, margin_arcmin=2.0)

    assert ra == pytest.approx(100.0)
    assert dec == pytest.approx(30.0)
    assert radius > 2.0 / 60.0
    assert radius < 0.1


def test_gaia_frame_cone_falls_back_to_crval_cd_scale_for_binned_robophot(tmp_path):
    from grandgousier.catalogs.gaia_query import _frame_cone_for_frame

    path = tmp_path / "robophot_crval_cd.fits"
    hdu = fits.PrimaryHDU(data=np.zeros((1024, 1024), dtype="uint16"))
    h = hdu.header
    h["CRVAL1"] = 22.2978515638
    h["CRVAL2"] = 67.2901728816
    h["BINNING"] = 2
    h["CD1_1"] = -8.55377776558e-06
    h["CD1_2"] = -0.000361458790837
    h["CD2_1"] = -0.000361596704705
    h["CD2_2"] = 8.29316895568e-06
    hdu.writeto(path)
    frame = SimpleNamespace(
        raw_path=str(path),
        ra_center_deg=None,
        dec_center_deg=None,
        instrument_unit=None,
    )

    ra, dec, radius = _frame_cone_for_frame(frame, margin_arcmin=2.0)

    assert ra == pytest.approx(22.2978515638)
    assert dec == pytest.approx(67.2901728816)
    assert 0.28 < radius < 0.35


def test_gaia_frame_cone_can_parse_robophot_target_header_without_crval(tmp_path):
    from grandgousier.catalogs.gaia_query import _frame_cone_for_frame

    path = tmp_path / "robophot_alpha_delta.fits"
    hdu = fits.PrimaryHDU(data=np.zeros((1024, 1024), dtype="uint16"))
    h = hdu.header
    h["ALPHA"] = "01 29 43.5"
    h["DELTA"] = "+67 15 30"
    h["BINNING"] = 2
    h["CD1_1"] = -8.55377776558e-06
    h["CD1_2"] = -0.000361458790837
    h["CD2_1"] = -0.000361596704705
    h["CD2_2"] = 8.29316895568e-06
    hdu.writeto(path)
    frame = SimpleNamespace(
        raw_path=str(path),
        ra_center_deg=None,
        dec_center_deg=None,
        instrument_unit=None,
    )

    ra, dec, radius = _frame_cone_for_frame(frame, margin_arcmin=2.0)

    assert ra == pytest.approx(22.43125)
    assert dec == pytest.approx(67.2583333333)
    assert 0.28 < radius < 0.35


def test_gaia_frame_cone_prefers_db_center_over_crval_reference_point(tmp_path):
    from grandgousier.catalogs.gaia_query import _frame_cone_for_frame, _frame_cone_for_frame_diagnostic

    path = tmp_path / "robophot_db_center_vs_crval.fits"
    hdu = fits.PrimaryHDU(data=np.zeros((1024, 1024), dtype="uint16"))
    h = hdu.header
    h["CRVAL1"] = 22.2978515638
    h["CRVAL2"] = 67.2901728816
    h["BINNING"] = 2
    h["CD1_1"] = -8.55377776558e-06
    h["CD1_2"] = -0.000361458790837
    h["CD2_1"] = -0.000361596704705
    h["CD2_2"] = 8.29316895568e-06
    hdu.writeto(path)
    frame = SimpleNamespace(
        raw_path=str(path),
        frame_id=106,
        ra_center_deg=22.4256125702872,
        dec_center_deg=67.25190793092416,
        instrument_unit_id=2,
        instrument_unit=SimpleNamespace(pixel_scale_arcsec=0.65, config_json={}),
    )

    ra, dec, radius = _frame_cone_for_frame(frame, margin_arcmin=2.0)
    cone, reason = _frame_cone_for_frame_diagnostic(frame, margin_arcmin=2.0)

    assert reason is None
    assert cone is not None
    assert ra == pytest.approx(22.4256125702872)
    assert dec == pytest.approx(67.25190793092416)
    assert 0.28 < radius < 0.35


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _CatalogCountSession:
    def __init__(self, count):
        self.count = count

    def execute(self, _statement):
        return _ScalarResult(self.count)


def test_inspect_existing_catalog_reports_ready_and_empty_namespaces():
    ready = _inspect_existing_catalog(_CatalogCountSession(42), catalog_name="catalog_ready")
    empty = _inspect_existing_catalog(_CatalogCountSession(0), catalog_name="catalog_empty")

    assert ready == {
        "mode": "explicit_existing",
        "catalog_name": "catalog_ready",
        "status": "DONE",
        "inserted_or_updated": 42,
        "n_sources": 42,
    }
    assert empty["status"] == "EMPTY"
    assert empty["n_sources"] == 0
    assert not _catalog_precondition_ok(empty, allow_empty_catalog=False)
    assert _catalog_precondition_ok(empty, allow_empty_catalog=True)


class _FakeTapJob:
    def __init__(self, rows):
        self._rows = rows

    def get_results(self):
        return self._rows


class _FailingTapClient:
    def launch_job(self, _query, **_kwargs):
        raise ConnectionError("provider unavailable")


class _WorkingTapClient:
    def __init__(self):
        self.queries = []

    def launch_job(self, query, **_kwargs):
        self.queries.append(query)
        return _FakeTapJob([{"source_id": 1}])


def test_gaia_query_falls_back_to_next_public_tap_provider():
    from grandgousier.catalogs.gaia_query import _GaiaTapProvider, _query_gaia_cone

    working = _WorkingTapClient()
    rows, provider_name, failures = _query_gaia_cone(
        [
            _GaiaTapProvider(name="first", client=_FailingTapClient()),
            _GaiaTapProvider(name="second", client=working),
        ],
        center_ra_deg=10.0,
        center_dec_deg=20.0,
        radius_deg=0.25,
        mag_limit=19.0,
        max_rows=2000,
    )

    assert rows == [{"source_id": 1}]
    assert provider_name == "second"
    assert failures["first"].startswith("ConnectionError:")
    assert "FROM gaiadr3.gaia_source" in working.queries[0]
    assert "phot_g_mean_mag <= 19.000000" in working.queries[0]


def test_gaia_query_reports_all_provider_failures():
    from grandgousier.catalogs.gaia_query import (
        _GaiaAllProvidersFailed,
        _GaiaTapProvider,
        _query_gaia_cone,
    )

    with pytest.raises(_GaiaAllProvidersFailed) as excinfo:
        _query_gaia_cone(
            [
                _GaiaTapProvider(name="first", client=_FailingTapClient()),
                _GaiaTapProvider(name="second", client=_FailingTapClient()),
            ],
            center_ra_deg=10.0,
            center_dec_deg=20.0,
            radius_deg=0.25,
            mag_limit=19.0,
            max_rows=2000,
        )

    assert set(excinfo.value.failures) == {"first", "second"}
