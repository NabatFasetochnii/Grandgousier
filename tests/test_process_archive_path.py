import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

import numpy as np
from astropy.io import fits

from grandgousier.archive import auto_process as auto_process_module
from grandgousier.archive.auto_process import (
    DetectedArchiveFile,
    _catalog_precondition_ok,
    _classify_science_status,
    _default_auto_catalog_name,
    _effective_auto_catalog_max_radius_deg,
    _effective_auto_catalog_mag_limit,
    _format_calibration_prefix,
    _inspect_existing_catalog,
    _merge_current_series_ids,
    discover_fits_files,
    process_archive_path,
    scan_archive_path,
)
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.archive_indexer import index_archive_paths


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


class _WorkingCatalogPersistenceSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.pipeline_runs = {}
        self.update_statements = []

    def register_pipeline_run(self, *, pipeline_run_id, field_id, series_id, provenance):
        snapshot = json.loads(json.dumps(provenance))
        self.pipeline_runs[pipeline_run_id] = {
            "field_id": field_id,
            "series_id": series_id,
            "catalog_name": "gaia_dr3",
            "recipe_json": {
                "working_catalog_provenance": snapshot,
                "quality_policy": {"working_catalog_provenance": snapshot},
            },
        }

    def execute(self, statement, parameters):
        sql = str(statement)
        assert "UPDATE pipeline_runs" in sql
        assert "pipeline_run_id = :pipeline_run_id" in sql
        assert "field_id = :field_id" in sql
        assert "series_id = :series_id" in sql
        assert "catalog_name = 'gaia_dr3'" in sql
        assert "jsonb_set" in sql
        self.update_statements.append((sql, dict(parameters)))
        run = self.pipeline_runs.get(parameters["pipeline_run_id"])
        if (
            run is None
            or run["field_id"] != parameters["field_id"]
            or run["series_id"] != parameters["series_id"]
            or run["catalog_name"] != "gaia_dr3"
        ):
            return SimpleNamespace(rowcount=0)
        provenance = json.loads(parameters["provenance_json"])
        recipe = dict(run["recipe_json"])
        recipe["working_catalog_provenance"] = provenance
        quality_policy = dict(recipe["quality_policy"])
        quality_policy["working_catalog_provenance"] = provenance
        recipe["quality_policy"] = quality_policy
        run["recipe_json"] = recipe
        return SimpleNamespace(rowcount=1)


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
    assert not result.pipeline_results


def test_process_archive_path_cli_passes_coordinate_pair_before_db_open(monkeypatch):
    from grandgousier import cli

    parser = cli.build_parser()
    args = parser.parse_args(["process-archive-path", "--path", "synthetic",
                              "--target-ra-deg", "22.5", "--target-dec-deg", "67.25",
                              "--local-com-max-shift-pix", "1.25",
                              "--frame-shift-min-matches", "24",
                              "--frame-shift-max-robust-scatter-pix", "0.3",
                              "--frame-shift-max-absolute-pix", "1.5",
                              "--crowded-arcsec", "12", "--blended-arcsec", "6",
                              "--disable-dynamic-separation",
                              "--dynamic-seeing-factor", "1.7",
                              "--dynamic-aperture-factor", "0.8",
                              "--dynamic-crowding-factor", "1.3",
                              "--fwhm-header-unit", "pix"])
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
    assert observed["local_com_max_shift_pix"] == 1.25
    assert observed["frame_shift_min_matches"] == 24
    assert observed["frame_shift_max_robust_scatter_pix"] == 0.3
    assert observed["frame_shift_max_absolute_pix"] == 1.5
    assert observed["crowded_arcsec"] == 12.0
    assert observed["blended_arcsec"] == 6.0
    assert observed["dynamic_separation"] is False
    assert observed["dynamic_seeing_factor"] == 1.7
    assert observed["dynamic_aperture_factor"] == 0.8
    assert observed["dynamic_crowding_factor"] == 1.3
    assert observed["fwhm_header_unit"] == "pix"


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



def _install_auto_field_harness(
    monkeypatch, tmp_path, series_rows, frame_ids_by_series, *, indexed_frame_ids=None
):
    from grandgousier.catalogs import gaia_query

    source_path = tmp_path / "auto_working_catalog.fits"
    detected = SimpleNamespace(path=str(source_path), profile_name="MASTER")
    all_frame_ids = sorted(
        {int(frame_id) for values in frame_ids_by_series.values() for frame_id in values}
    )
    selected_indexed_frame_ids = (
        list(indexed_frame_ids) if indexed_frame_ids is not None else all_frame_ids
    )
    selected_frame_queries = []
    exact_cone_calls = []
    lifecycle_events = []
    catalog_fields = {}

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
            indexed_frame_ids=selected_indexed_frame_ids,
            index_results=[],
            failed_files=0,
        ),
    )
    monkeypatch.setattr(
        auto_process_module,
        "_series_ids_for_paths",
        lambda *_args, **_kwargs: [int(row["series_id"]) for row in series_rows],
    )
    monkeypatch.setattr(
        auto_process_module,
        "_frame_ids_by_series_for_paths",
        lambda *_args, **_kwargs: frame_ids_by_series,
    )
    monkeypatch.setattr(
        auto_process_module,
        "_load_series_for_pipeline",
        lambda *_args, **_kwargs: [dict(row) for row in series_rows],
    )
    monkeypatch.setattr(
        auto_process_module, "_write_manifest_if_requested", lambda *_args, **_kwargs: None
    )

    def select_frames(_session, *, frame_ids, **kwargs):
        assert kwargs["only_wcs_ok"] is False
        selected_frame_queries.append(list(frame_ids))
        return [
            SimpleNamespace(
                frame_id=frame_id,
                raw_path=f"fake-frame-{frame_id}.fits",
                instrument_unit_id=4,
            )
            for frame_id in frame_ids
        ]

    def exact_wcs_cone(path, *, margin_arcmin):
        path = Path(path)
        exact_cone_calls.append((path, margin_arcmin))
        frame_id = int(path.stem.rsplit("-", 1)[1])
        return frame_id / 10000.0, 0.0, 0.1 + margin_arcmin / 60.0

    monkeypatch.setattr(gaia_query, "_select_frames", select_frames)
    monkeypatch.setattr(
        gaia_query,
        "_frame_cone_for_frame_diagnostic",
        lambda *_args, **_kwargs: pytest.fail(
            "automatic transient Gaia must use exact WCS-corner cones"
        ),
    )
    monkeypatch.setattr(gaia_query, "_frame_cone", exact_wcs_cone)
    original_clear = gaia_query.GaiaWorkingCatalog.clear
    original_close = gaia_query.GaiaWorkingCatalog.close

    def track_clear(catalog):
        lifecycle_events.append(("clear", catalog_fields.get(id(catalog))))
        return original_clear(catalog)

    def track_close(catalog):
        lifecycle_events.append(("close", catalog_fields.get(id(catalog))))
        return original_close(catalog)

    monkeypatch.setattr(gaia_query.GaiaWorkingCatalog, "clear", track_clear)
    monkeypatch.setattr(gaia_query.GaiaWorkingCatalog, "close", track_close)
    monkeypatch.setattr(
        gaia_query,
        "query_gaia_for_frames",
        lambda *_args, **_kwargs: pytest.fail("legacy full-catalog Gaia query must not run"),
    )
    monkeypatch.setattr(
        gaia_query,
        "_upsert_catalog_sources",
        lambda *_args, **_kwargs: pytest.fail("transient Gaia rows must not be fully upserted"),
    )
    return (
        source_path,
        gaia_query,
        selected_frame_queries,
        lifecycle_events,
        catalog_fields,
        exact_cone_calls,
    )


def _auto_series(series_id, field_id):
    return {
        "series_id": series_id,
        "field_id": field_id,
        "field_name": f"field-{field_id}",
        "instrument_unit_id": 4,
        "instrument": "MASTER",
        "site": "test",
        "tube": "tube",
        "camera": "camera",
        "filter_name": "V",
        "ra_center_deg": 10.0,
        "dec_center_deg": 0.0,
        "radius_arcmin": 10.0,
        "time_start": None,
        "time_end": None,
        "mjd_start": None,
        "mjd_end": None,
        "n_frames": 1,
        "status": "READY",
    }


def test_auto_working_catalog_hashes_are_deterministic_and_scope_sensitive():
    from grandgousier.catalogs import gaia_query

    frame_hash = auto_process_module._working_catalog_frame_scope_sha256
    footprint_hash = auto_process_module._working_catalog_footprint_sha256
    assert frame_hash([9, 3, 9]) == frame_hash([3, 9])
    assert frame_hash([3, 9]) != frame_hash([3, 10])

    first_cones = [(359.75, 12.0, 0.125), (10.0, -3.0, 0.25)]
    reordered_cones = [(10.0, -3.0, 0.25), (359.75, 12.0, 0.125)]
    assert footprint_hash(gaia_query, first_cones) == footprint_hash(
        gaia_query, reordered_cones
    )
    assert footprint_hash(gaia_query, first_cones) == footprint_hash(
        gaia_query, [*first_cones, first_cones[0]]
    )
    assert footprint_hash(gaia_query, first_cones) != footprint_hash(
        gaia_query, [(*first_cones[0][:2], 0.126), first_cones[1]]
    )


def _fake_pipeline_result():
    return SimpleNamespace(
        pipeline_run_id=None,
        status="DONE",
        calibration_names={},
        outputs={},
        log_files={},
        steps=[],
        quality_policy={},
    )


def test_auto_gaia_field_union_uses_only_selected_frames_for_each_field(
    monkeypatch, tmp_path
):
    series_rows = [_auto_series(3, 20), _auto_series(2, 10), _auto_series(1, 10)]
    frame_ids_by_series = {1: [101], 2: [202, 999], 3: [303]}
    source_path, gaia_query, _frame_queries, _lifecycle, _catalog_fields, _cone_calls = (
        _install_auto_field_harness(
            monkeypatch,
            tmp_path,
            series_rows,
            frame_ids_by_series,
            indexed_frame_ids=[101, 202, 303],
        )
    )

    frame_field_ids = {101: 10, 202: 10, 999: 10, 303: 20}
    selected_frames_by_field = []

    def select_frames(_session, *, frame_ids, **_kwargs):
        selected_frame_ids = [int(frame_id) for frame_id in frame_ids]
        selected_fields = tuple(
            sorted({frame_field_ids[frame_id] for frame_id in selected_frame_ids})
        )
        selected_frames_by_field.append((selected_fields, selected_frame_ids))
        return [
            SimpleNamespace(
                frame_id=frame_id,
                raw_path=f"fake-frame-{frame_id}.fits",
                instrument_unit_id=4,
            )
            for frame_id in selected_frame_ids
        ]

    monkeypatch.setattr(gaia_query, "_select_frames", select_frames)
    fetched_frame_ids_by_field = []

    async def fake_fetch(cones, **_kwargs):
        cones = list(cones)
        fetched_frame_ids_by_field.append(
            [round(float(ra) * 10000) for ra, _dec, _radius in cones]
        )
        return gaia_query.GaiaWorkingCatalog(
            [
                {
                    "source_id": str(len(fetched_frame_ids_by_field)),
                    "ra": cones[0][0],
                    "dec": cones[0][1],
                    "phot_g_mean_mag": 15.0,
                }
            ]
        ), {"logical_queries": 1, "rows_received": 1}

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", fake_fetch)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: _fake_pipeline_result(),
    )

    process_archive_path(
        FakeSession(),
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1, 2, 3],
        execute=True,
        make_light_curve_summary=False,
    )

    assert selected_frames_by_field == [((10,), [101, 202]), ((20,), [303])]
    assert fetched_frame_ids_by_field == [[101, 202], [303]]


def test_auto_gaia_fetches_once_per_field_and_processes_field_major_with_close(monkeypatch, tmp_path):
    series_rows = [_auto_series(3, 20), _auto_series(2, 10), _auto_series(1, 10)]
    frame_map = {1: [101], 2: [202, 999], 3: [303]}
    source_path, gaia_query, frame_queries, lifecycle, catalog_fields, exact_cone_calls = (
        _install_auto_field_harness(
            monkeypatch,
            tmp_path,
            series_rows,
            frame_map,
            indexed_frame_ids=[101, 202, 303],
        )
    )
    events = lifecycle
    fetch_calls = []
    provenance_calls = []
    pipeline_configs = []
    catalogs = {}
    field_by_ra = {0.0101: 10, 0.0202: 10, 0.0303: 20}

    async def fake_fetch(cones, **kwargs):
        cones = list(cones)
        field_id = field_by_ra[cones[0][0]]
        fetch_calls.append((field_id, cones, kwargs))
        events.append(("fetch", field_id))
        source_ids = (
            ("9007199254740993", "9007199254740995")
            if field_id == 10
            else ("9007199254740997",)
        )
        rows = [
            {
                "source_id": source_id,
                "ra": cones[0][0],
                "dec": cones[0][1],
                "phot_g_mean_mag": 15.0,
            }
            for source_id in source_ids
        ]
        if field_id == 10:
            rows.append(dict(rows[0]))
        catalog = gaia_query.GaiaWorkingCatalog(rows)
        catalogs[field_id] = catalog
        catalog_fields[id(catalog)] = field_id
        return catalog, {
            "logical_queries": 1,
            "physical_parts": 2 if field_id == 10 else 1,
            "provider_attempts": 2 if field_id == 10 else 1,
            "provider_failures": (
                [{"provider": "gaia_ari_async", "error": "TimeoutError: timeout"}]
                if field_id == 10
                else []
            ),
            "providers_used": ["gaia_esac_async"],
            "overflow_splits": 1 if field_id == 10 else 0,
            "overflow_events": (
                [{"part": 1, "provider": "gaia_aip_async", "query_status": "OVERFLOW"}]
                if field_id == 10
                else []
            ),
            "rows_received": len(rows),
            "partial_rows_discarded": False,
        }

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", fake_fetch)
    series_fields = {int(row["series_id"]): int(row["field_id"]) for row in series_rows}

    def fake_pipeline(
        _session,
        config,
        *,
        working_catalog,
        working_catalog_provenance,
        working_catalog_promoted_source_ids,
        working_catalog_run_ids_by_series,
    ):
        field_id = series_fields[int(config.series_id)]
        assert working_catalog is catalogs[field_id]
        assert config.catalog_name == "gaia_dr3"
        pipeline_configs.append(config)
        provenance_calls.append((field_id, working_catalog_provenance))
        events.append(("pipeline", int(config.series_id), field_id))
        pipeline_run_id = 1000 + int(config.series_id)
        working_catalog_run_ids_by_series[int(config.series_id)] = pipeline_run_id
        session.register_pipeline_run(
            pipeline_run_id=pipeline_run_id,
            field_id=field_id,
            series_id=int(config.series_id),
            provenance=working_catalog_provenance,
        )
        selected_by_series = {
            1: {"9007199254740993"},
            2: {"9007199254740993"},
            3: set(),
        }
        selected_ids = selected_by_series[int(config.series_id)]
        working_catalog_promoted_source_ids.update(selected_ids)
        pipeline_result = _fake_pipeline_result()
        pipeline_result.steps = [
            SimpleNamespace(
                step_name="select_and_promote_working_gaia_sources",
                status="DONE",
                result={
                    "selected_gaia_source_count": len(selected_ids),
                    "canonical_catalog_source_count": len(selected_ids),
                    "promotion_status": "PROMOTED" if selected_ids else "SKIPPED_NO_SELECTION",
                },
            )
        ]
        return pipeline_result

    monkeypatch.setattr(auto_process_module, "run_field_pipeline", fake_pipeline)
    session = _WorkingCatalogPersistenceSession()
    result = process_archive_path(
        session,
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1, 2, 3],
        execute=True,
        make_light_curve_summary=False,
        local_com_max_shift_pix=1.25,
        frame_shift_min_matches=24,
        frame_shift_max_robust_scatter_pix=0.3,
        frame_shift_max_absolute_pix=1.5,
        crowded_arcsec=12.0,
        blended_arcsec=6.0,
        dynamic_separation=False,
        dynamic_seeing_factor=1.7,
        dynamic_aperture_factor=0.8,
        dynamic_crowding_factor=1.3,
        fwhm_header_unit="pix",
    )

    assert len(pipeline_configs) == 3
    for config in pipeline_configs:
        assert config.local_com_max_shift_pix == 1.25
        assert config.frame_shift_min_matches == 24
        assert config.frame_shift_max_robust_scatter_pix == 0.3
        assert config.frame_shift_max_absolute_pix == 1.5
        assert config.crowded_arcsec == 12.0
        assert config.blended_arcsec == 6.0
        assert config.dynamic_separation is False
        assert config.dynamic_seeing_factor == 1.7
        assert config.dynamic_aperture_factor == 0.8
        assert config.dynamic_crowding_factor == 1.3
        assert config.fwhm_header_unit == "pix"
    assert [field_id for field_id, _cones, _kwargs in fetch_calls] == [10, 20]
    assert [call[2]["max_radius_deg"] for call in fetch_calls] == [1.0, 1.0]
    assert [call[2]["maxrec"] for call in fetch_calls] == [50000, 50000]
    assert [call[1] for call in fetch_calls] == [
        [(0.0101, 0.0, 0.13333333333333333), (0.0202, 0.0, 0.13333333333333333)],
        [(0.0303, 0.0, 0.13333333333333333)],
    ]
    assert [field_id for field_id, _provenance in provenance_calls] == [10, 10, 20]
    archive_provenance = [
        row["working_catalog_provenance"] for row in result.catalog_results
    ]
    assert [provenance for _field_id, provenance in provenance_calls] == [
        archive_provenance[0],
        archive_provenance[0],
        archive_provenance[1],
    ]
    first_provenance = archive_provenance[0]
    assert first_provenance["field_id"] == 10
    assert first_provenance["field_frame_count"] == 2
    assert first_provenance["query_mode"] == "field_union"
    assert first_provenance["frame_scope_sha256"] == (
        auto_process_module._working_catalog_frame_scope_sha256([101, 202])
    )
    assert first_provenance["footprint_sha256"] == (
        auto_process_module._working_catalog_footprint_sha256(
            gaia_query, fetch_calls[0][1]
        )
    )
    assert first_provenance["gaia_release"] == "DR3"
    assert first_provenance["gaia_table"] == "gaiadr3.gaia_source"
    assert first_provenance["requested_columns"] == list(
        gaia_query.GAIA_WORKING_CATALOG_COLUMNS
    )
    assert first_provenance["requested_columns_sha256"] == (
        auto_process_module._working_catalog_hash_json(
            list(gaia_query.GAIA_WORKING_CATALOG_COLUMNS)
        )
    )
    assert first_provenance["providers_used"] == ["gaia_esac_async"]
    assert first_provenance["providers_attempted"] == [
        "gaia_esac_async", "gaia_ari_async"
    ]
    assert first_provenance["elapsed_seconds"] >= 0.0
    assert first_provenance["logical_acquisitions"] == 1
    assert first_provenance["margin_arcmin"] == 2.0
    assert first_provenance["magnitude_limit"] == 19.0
    assert first_provenance["max_radius_deg"] == 1.0
    assert first_provenance["maxrec"] == 50000
    assert first_provenance["fetch_status"] == "SUCCEEDED_COMPLETE"
    assert first_provenance["fetch_complete"] is True
    assert first_provenance["logical_queries"] == 1
    assert first_provenance["physical_parts"] == 2
    assert first_provenance["provider_attempts"] == 2
    assert first_provenance["provider_failures"] == 1
    assert first_provenance["overflow_splits"] == 1
    assert first_provenance["rows_received"] == 3
    assert first_provenance["unique_rows"] == 2
    assert first_provenance["unique_sources"] == 2
    assert first_provenance["duplicate_rows"] == 1
    assert first_provenance["field_acquisition_unique_identity_count"] == 2
    assert first_provenance["working_catalog_sources"] == 2
    assert first_provenance["overflow_occurred"] is True
    assert first_provenance["overflow_splits"] == 1
    assert first_provenance["overflow_statuses"] == ["OVERFLOW"]
    assert first_provenance["position_policy"]["coordinate_frame"] == "ICRS"
    assert first_provenance["position_policy"]["proper_motion_correction_applied"] is False
    assert [
        (
            item["series_id"],
            item["selected_identity_count"],
            item["promoted_identity_count"],
        )
        for item in first_provenance["series_selection"]
    ] == [(1, 1, 1), (2, 1, 1)]
    assert first_provenance["series_selection"][0]["matching_policy"]["algorithm"] == (
        "per_frame_greedy_one_to_one_haversine"
    )
    assert first_provenance["field_promoted_unique_identity_count"] == 1
    assert first_provenance["field_discarded_transient_identity_count"] == 1
    assert first_provenance["field_promotion_accounting_complete"] is True
    assert first_provenance["partial_rows_discarded"] is False
    second_provenance = archive_provenance[1]
    assert second_provenance["unique_sources"] == 1
    assert second_provenance["field_promoted_unique_identity_count"] == 0
    assert second_provenance["field_discarded_transient_identity_count"] == 1
    assert [
        (
            item["series_id"],
            item["selected_identity_count"],
            item["promoted_identity_count"],
        )
        for item in second_provenance["series_selection"]
    ] == [(3, 0, 0)]
    assert json.loads(json.dumps(first_provenance)) == first_provenance
    assert "9007199254740993" not in json.dumps(first_provenance)
    assert exact_cone_calls == [
        (Path("fake-frame-101.fits"), 2.0),
        (Path("fake-frame-202.fits"), 2.0),
        (Path("fake-frame-303.fits"), 2.0),
    ]
    assert frame_queries == [[101, 202], [303]]
    assert events == [
        ("fetch", 10),
        ("pipeline", 1, 10),
        ("pipeline", 2, 10),
        ("clear", 10),
        ("close", 10),
        ("fetch", 20),
        ("pipeline", 3, 20),
        ("clear", 20),
        ("close", 20),
    ]
    assert result.catalog_name == "gaia_dr3"
    assert len(result.catalog_results) == 2
    assert [row["logical_queries"] for row in result.catalog_results] == [1, 1]
    assert all("inserted_or_updated" not in row for row in result.catalog_results)
    assert "9007199254740993" not in repr(result.to_dict())
    assert [run_id for _sql, params in session.update_statements for run_id in [
        params["pipeline_run_id"]
    ]] == [1001, 1002, 1003]
    for pipeline_run_id, catalog_index in ((1001, 0), (1002, 0), (1003, 1)):
        recipe = session.pipeline_runs[pipeline_run_id]["recipe_json"]
        persisted_provenance = recipe["working_catalog_provenance"]
        assert persisted_provenance == archive_provenance[catalog_index]
        assert recipe["quality_policy"]["working_catalog_provenance"] == (
            persisted_provenance
        )
        assert "9007199254740993" not in json.dumps(recipe)
    assert session.commits == 2
    for catalog in catalogs.values():
        with pytest.raises(RuntimeError, match="closed"):
            _ = catalog.source_ids


def test_auto_working_catalog_closes_after_pipeline_failure(monkeypatch, tmp_path):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, lifecycle, catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    created = []

    async def fake_fetch(_cones, **_kwargs):
        catalog = gaia_query.GaiaWorkingCatalog(
            [{"source_id": "42", "ra": 0.01, "dec": 0.0, "phot_g_mean_mag": 15.0}]
        )
        catalog_fields[id(catalog)] = 10
        created.append(catalog)
        return catalog, {"logical_queries": 1, "rows_received": 1}

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", fake_fetch)
    session = _WorkingCatalogPersistenceSession()

    def failing_pipeline(
        _session,
        config,
        *,
        working_catalog,
        working_catalog_provenance,
        working_catalog_promoted_source_ids,
        working_catalog_run_ids_by_series,
    ):
        pipeline_run_id = 7001
        working_catalog_run_ids_by_series[int(config.series_id)] = pipeline_run_id
        session.register_pipeline_run(
            pipeline_run_id=pipeline_run_id,
            field_id=int(working_catalog_provenance["field_id"]),
            series_id=int(config.series_id),
            provenance=working_catalog_provenance,
        )
        raise RuntimeError("synthetic pipeline failure")

    monkeypatch.setattr(auto_process_module, "run_field_pipeline", failing_pipeline)
    result = process_archive_path(
        session, root=tmp_path, exact_paths=[source_path], series_ids=[1],
        execute=True, make_light_curve_summary=False,
    )

    assert result.status == "FAILED"
    assert lifecycle == [("clear", 10), ("close", 10)]
    assert [params["pipeline_run_id"] for _sql, params in session.update_statements] == [
        7001
    ]
    persisted = session.pipeline_runs[7001]["recipe_json"][
        "working_catalog_provenance"
    ]
    assert persisted["field_promotion_accounting_complete"] is False
    assert persisted["field_promoted_unique_identity_count"] is None
    assert persisted["field_discarded_transient_identity_count"] is None
    assert persisted["field_acquisition_unique_identity_count"] == 1
    selection = persisted["series_selection"][0]
    assert selection["series_id"] == 1
    assert selection["selection_step_status"] == "NOT_REPORTED"
    assert selection["selected_identity_count"] is None
    assert selection["promoted_identity_count"] is None
    assert session.pipeline_runs[7001]["recipe_json"]["quality_policy"][
        "working_catalog_provenance"
    ] == persisted
    assert len(created) == 1
    with pytest.raises(RuntimeError, match="closed"):
        _ = created[0].source_ids


def test_auto_working_catalog_closes_when_fetch_result_processing_fails(monkeypatch, tmp_path):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, lifecycle, catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    created = gaia_query.GaiaWorkingCatalog(
        [{"source_id": "42", "ra": 0.01, "dec": 0.0, "phot_g_mean_mag": 15.0}]
    )
    catalog_fields[id(created)] = 10

    async def malformed_fetch_result(_cones, **_kwargs):
        return created, None

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", malformed_fetch_result)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: pytest.fail("pipeline must not run after acquisition failure"),
    )
    result = process_archive_path(
        FakeSession(), root=tmp_path, exact_paths=[source_path], series_ids=[1],
        execute=True, make_light_curve_summary=False,
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert result.catalog_results[0]["status"] == "FAILED"
    assert lifecycle == [("clear", 10), ("close", 10)]
    with pytest.raises(RuntimeError, match="closed"):
        _ = created.source_ids


def test_auto_working_catalog_closes_on_catalog_precondition_early_return(monkeypatch, tmp_path):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, lifecycle, catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    empty_catalog = gaia_query.GaiaWorkingCatalog()
    catalog_fields[id(empty_catalog)] = 10

    async def empty_fetch(_cones, **_kwargs):
        return empty_catalog, {
            "logical_queries": 1,
            "physical_parts": 1,
            "provider_attempts": 1,
            "provider_failures": [],
            "overflow_splits": 0,
            "rows_received": 0,
        }

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", empty_fetch)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: pytest.fail("empty catalog precondition must return early"),
    )
    result = process_archive_path(
        FakeSession(), root=tmp_path, exact_paths=[source_path], series_ids=[1],
        execute=True, make_light_curve_summary=False,
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert lifecycle == [("clear", 10), ("close", 10)]
    with pytest.raises(RuntimeError, match="closed"):
        _ = empty_catalog.source_ids


def test_auto_working_catalog_fetch_failure_stops_without_pipeline(monkeypatch, tmp_path):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, _lifecycle, _catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    fetch_calls = []

    async def failed_fetch(*_args, **_kwargs):
        fetch_calls.append("called")
        raise gaia_query.GaiaWorkingCatalogFetchError(
            {"logical_queries": 1, "partial_rows_discarded": True, "failure_type": "ProviderError"}
        )

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", failed_fetch)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: pytest.fail("pipeline must not run after fetch failure"),
    )
    result = process_archive_path(
        FakeSession(), root=tmp_path, exact_paths=[source_path], series_ids=[1],
        execute=True, make_light_curve_summary=False,
    )

    assert fetch_calls == ["called"]
    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert result.catalog_results[0]["failure_type"] == "ProviderError"
    assert result.catalog_results[0]["partial_rows_discarded"] is True


def test_transient_gaia_reference_callback_waits_for_selected_persisted_identities(monkeypatch, tmp_path):
    from grandgousier.calibration.photometric_policy import ColorCalibrationPolicy

    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, _lifecycle, catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    monkeypatch.setattr(
        auto_process_module,
        "load_auto_profiles",
        lambda **_kwargs: {"MASTER": SimpleNamespace(raw_config={})},
    )
    policy = ColorCalibrationPolicy(
        enabled=True,
        reference_catalog_name="panstarrs_dr2",
        reference_band="g",
        color_index="g-r",
        require_bands=("g", "r"),
    )
    monkeypatch.setattr(
        auto_process_module,
        "resolve_color_calibration_policy",
        lambda *_args, **_kwargs: policy,
    )
    events = []

    def fake_ensure(_session, **kwargs):
        events.append("reference_provider_and_import")
        assert kwargs["selected_identity_source_ids"] == {"77"}
        return {
            "status": "MISSING_REFERENCE_PHOTOMETRY",
            "ready": False,
            "reason": "selected reference photometry unavailable",
        }

    monkeypatch.setattr(
        auto_process_module, "_ensure_reference_photometry_for_series", fake_ensure
    )

    async def fake_fetch(cones, **_kwargs):
        catalog = gaia_query.GaiaWorkingCatalog(
            [{"source_id": "77", "ra": cones[0][0], "dec": 0.0, "phot_g_mean_mag": 15.0}]
        )
        catalog_fields[id(catalog)] = 10
        return catalog, {
            "logical_queries": 1,
            "physical_parts": 1,
            "provider_attempts": 1,
            "provider_failures": [],
            "overflow_splits": 0,
            "rows_received": 1,
        }

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", fake_fetch)
    configs = []

    def fake_pipeline(
        _session,
        config,
        *,
        working_catalog,
        working_catalog_provenance,
        working_catalog_promoted_source_ids,
        working_catalog_run_ids_by_series,
        reference_photometry_callback,
    ):
        configs.append(config)
        assert isinstance(working_catalog_provenance, dict)
        assert len(working_catalog) == 1
        events.append("pipeline_started")
        events.append("selected_identity_persisted")
        reference_photometry_callback({"77"})
        return _fake_pipeline_result()

    monkeypatch.setattr(auto_process_module, "run_field_pipeline", fake_pipeline)
    result = process_archive_path(
        FakeSession(),
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1],
        execute=True,
        make_light_curve_summary=False,
        enable_color_calibration=True,
    )

    assert len(configs) == 1
    assert configs[0].color_calibration_requested is True
    assert configs[0].enable_color_calibration is True
    assert result.reference_photometry_results[0]["status"] == "MISSING_REFERENCE_PHOTOMETRY"
    assert events == [
        "pipeline_started",
        "selected_identity_persisted",
        "reference_provider_and_import",
    ]


def test_explicit_catalog_path_does_not_fetch_or_pass_working_catalog(monkeypatch, tmp_path):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, _frame_queries, _lifecycle, _catalog_fields, _cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101]})
    )
    monkeypatch.setattr(
        auto_process_module,
        "_inspect_existing_catalog",
        lambda _session, *, catalog_name: {
            "mode": "explicit_existing",
            "catalog_name": catalog_name,
            "status": "DONE",
            "inserted_or_updated": 5,
            "n_sources": 5,
        },
    )
    monkeypatch.setattr(
        gaia_query,
        "fetch_gaia_working_catalog",
        lambda *_args, **_kwargs: pytest.fail("explicit catalogs must not fetch transient Gaia"),
    )
    observed = []

    def fake_pipeline(_session, config, **kwargs):
        observed.append((config.catalog_name, kwargs))
        return _fake_pipeline_result()

    monkeypatch.setattr(auto_process_module, "run_field_pipeline", fake_pipeline)
    result = process_archive_path(
        FakeSession(),
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1],
        catalog_name="named-reference",
        execute=True,
        make_light_curve_summary=False,
    )

    assert result.catalog_name == "named-reference"
    assert observed == [("named-reference", {})]
    assert result.catalog_results[0]["mode"] == "explicit_existing"


def test_auto_working_catalog_wcs_footprint_failure_skips_fetch_and_pipeline(
    monkeypatch, tmp_path
):
    series_rows = [_auto_series(1, 10)]
    source_path, gaia_query, frame_queries, _lifecycle, _catalog_fields, exact_cone_calls = (
        _install_auto_field_harness(monkeypatch, tmp_path, series_rows, {1: [101, 102]})
    )

    def missing_wcs_cone(path, *, margin_arcmin):
        path = Path(path)
        exact_cone_calls.append((path, margin_arcmin))
        if path.name == "fake-frame-102.fits":
            return None
        frame_id = int(path.stem.rsplit("-", 1)[1])
        return frame_id / 10000.0, 0.0, 0.1 + margin_arcmin / 60.0

    monkeypatch.setattr(gaia_query, "_frame_cone", missing_wcs_cone)
    fetch_calls = []
    pipeline_calls = []

    async def fake_fetch(cones, **kwargs):
        fetch_calls.append((list(cones), kwargs))
        return gaia_query.GaiaWorkingCatalog(), {"logical_queries": 1, "rows_received": 0}

    monkeypatch.setattr(gaia_query, "fetch_gaia_working_catalog", fake_fetch)
    monkeypatch.setattr(
        auto_process_module,
        "run_field_pipeline",
        lambda *_args, **_kwargs: pipeline_calls.append("called"),
    )

    result = process_archive_path(
        FakeSession(),
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1],
        execute=True,
        make_light_curve_summary=False,
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert result.pipeline_results == []
    assert result.catalog_results[0]["status"] == "FAILED_FOOTPRINTS"
    assert result.catalog_results[0]["cone_count"] == 1
    assert result.catalog_results[0]["failed_footprints"] == 1
    assert result.catalog_results[0]["failed_footprint_reasons"] == {
        "invalid_wcs_footprint": 1
    }
    assert frame_queries == [[101, 102]]
    assert exact_cone_calls == [
        (Path("fake-frame-101.fits"), 2.0),
        (Path("fake-frame-102.fits"), 2.0),
    ]
    assert fetch_calls == []
    assert pipeline_calls == []


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
        "_inspect_existing_catalog", "build_field_series",
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


class _OfflineEvidenceResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _OfflineEvidenceSession(FakeSession):
    def __init__(self, evidence):
        super().__init__()
        self.evidence = evidence
        self.statements = []

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        self.statements.append((sql, statement))
        if "FROM pipeline_runs" in sql:
            rows = self.evidence["pipeline_runs"]
        elif "FROM pipeline_run_steps" in sql:
            rows = self.evidence["pipeline_steps"]
        elif "FROM detection_runs" in sql:
            rows = self.evidence["detection_runs"]
        elif "FROM detection_run_frames" in sql:
            rows = self.evidence["detection_run_frames"]
        elif "FROM detection_catalog_matches" in sql:
            rows = self.evidence["detection_catalog_matches"]
        elif "FROM catalog_sources" in sql:
            rows = (
                self.evidence["target_catalog_sources"]
                if "catalog_sources.source_id =" in sql
                else self.evidence["matched_catalog_sources"]
            )
        elif "FROM frame_catalog_links" in sql:
            rows = self.evidence["frame_catalog_links"]
        else:
            raise AssertionError(f"unexpected offline reuse query: {sql}")
        return _OfflineEvidenceResult(rows)


def _offline_reuse_evidence(monkeypatch, tmp_path, **overrides):
    from grandgousier.catalogs import gaia_query
    from grandgousier.db.detection_runs import (
        CATALOG_MATCH_RUN,
        SOURCE_DETECTION_RUN,
        algorithm_version,
        detection_run_fingerprint,
    )
    from grandgousier.photometry.source_detection import DEFAULT_ALGORITHM
    from grandgousier.quality.detection_catalog_qc import MATCH_ALGORITHM

    source_path, gaia_query, _frame_queries, _lifecycle, _catalog_fields, _cone_calls = (
        _install_auto_field_harness(
            monkeypatch, tmp_path, [_auto_series(1, 10)], {1: [101]}
        )
    )
    cone = (0.0101, 0.0, 0.1 + 2.0 / 60.0)
    provenance = {
        "provenance_version": 1,
        "field_id": 10,
        "field_frame_count": 1,
        "query_mode": "field_union",
        "frame_scope_sha256": auto_process_module._working_catalog_frame_scope_sha256(
            [101]
        ),
        "footprint_sha256": auto_process_module._working_catalog_footprint_sha256(
            gaia_query, [cone]
        ),
        "catalog_name": "gaia_dr3",
        "gaia_release": gaia_query.GAIA_DR3_RELEASE,
        "gaia_table": gaia_query.GAIA_DR3_TABLE,
        "requested_columns_sha256": auto_process_module._working_catalog_hash_json(
            list(gaia_query.GAIA_WORKING_CATALOG_COLUMNS)
        ),
        "margin_arcmin": 2.0,
        "magnitude_limit": 19.0,
        "max_radius_deg": 1.0,
        "maxrec": 50000,
        "fetch_status": "SUCCEEDED_COMPLETE",
        "fetch_complete": True,
        "partial_rows_discarded": False,
        "unique_sources": 1,
        "working_catalog_sources": 1,
    }
    provenance.update(overrides.get("provenance", {}))

    recipe = {
        "catalog_name": "gaia_dr3",
        "field_id": 10,
        "series_id": 1,
        "instrument_unit_id": 4,
        "frame_ids": [101],
        "dry_run": False,
        "skip_detection": False,
        "skip_detection_catalog_match": False,
        "skip_catalog_frame_match": False,
        "margin_pix": 2.0,
        "mag_key": "G",
        "working_catalog_mag_limit": 19.0,
        "max_working_sources_per_frame": 2000,
        "catalog_edge_margin_pix": 20.0,
        "replace_frame_catalog_links": True,
        "detection_threshold_sigma": 5.0,
        "detection_max_sources_per_frame": 2000,
        "detection_min_separation_pix": 5.0,
        "detection_match_radius_arcsec": 2.0,
        "aperture_radius_pix": 3.0,
        "annulus_inner_pix": 8.0,
        "annulus_outer_pix": 12.0,
        "gaia_query_coverage_v1": {"coverage_complete": True},
        "working_catalog_provenance": provenance,
        "quality_policy": {"working_catalog_provenance": provenance},
    }
    recipe.update(overrides.get("recipe", {}))
    pipeline_run = {
        "pipeline_run_id": 100,
        "field_id": 10,
        "series_id": 1,
        "instrument_unit_id": 4,
        "catalog_name": "gaia_dr3",
        "status": "DONE",
        "recipe_json": recipe,
    }
    detector_parameters = {
        "threshold_sigma": 5.0,
        "max_sources_per_frame": 2000,
        "min_separation_pix": 5.0,
        "aperture_radius_pix": 3.0,
        "annulus_inner_pix": 8.0,
        "annulus_outer_pix": 12.0,
        "centroid_box_radius_pix": 3,
    }
    matcher_parameters = {
        "detection_algorithm": DEFAULT_ALGORITHM,
        "radius_arcsec": 2.0,
        "mag_key": "G",
        "min_snr": None,
    }
    source_run = {
        "detection_run_id": 11,
        "pipeline_run_id": 100,
        "run_kind": SOURCE_DETECTION_RUN,
        "catalog_name": None,
        "algorithm": DEFAULT_ALGORITHM,
        "algorithm_version": algorithm_version(DEFAULT_ALGORITHM),
        "parameters": detector_parameters,
        "fingerprint": detection_run_fingerprint(
            run_kind=SOURCE_DETECTION_RUN,
            algorithm=DEFAULT_ALGORITHM,
            parameters=detector_parameters,
        ),
    }
    match_run = {
        "detection_run_id": 12,
        "pipeline_run_id": 100,
        "run_kind": CATALOG_MATCH_RUN,
        "catalog_name": "gaia_dr3",
        "algorithm": MATCH_ALGORITHM,
        "algorithm_version": algorithm_version(MATCH_ALGORITHM),
        "parameters": matcher_parameters,
        "fingerprint": detection_run_fingerprint(
            run_kind=CATALOG_MATCH_RUN,
            algorithm=MATCH_ALGORITHM,
            parameters=matcher_parameters,
            catalog_name="gaia_dr3",
        ),
    }
    steps = [
        {
            "pipeline_run_id": 100,
            "step_name": "detect_sources",
            "status": "DONE",
            "result_json": {
                "detection_run_id": 11,
                "selected_frames": 1,
                "checked_frames": 1,
                "failed_frames": 0,
                "skipped_missing_file": 0,
            },
        },
        {
            "pipeline_run_id": 100,
            "step_name": "select_and_promote_working_gaia_sources",
            "status": "DONE",
            "result_json": {
                "selected_gaia_source_count": 1,
                "canonical_catalog_source_count": 1,
            },
        },
        {
            "pipeline_run_id": 100,
            "step_name": "project_selected_working_gaia_sources",
            "status": "DONE",
            "result_json": {
                "selected_frames": 1,
                "checked_frames": 1,
                "failed_frames": 0,
                "skipped_missing_file": 0,
                "selected_sources": 1,
                "linked": 1,
            },
        },
        {
            "pipeline_run_id": 100,
            "step_name": "match_detections_to_catalog",
            "status": "DONE",
            "result_json": {
                "detection_run_id": 12,
                "selected_frames": 1,
                "checked_frames": 1,
                "failed_frames": 0,
                "matched": 1,
            },
        },
    ]
    steps = overrides.get("pipeline_steps", steps)
    match_frame_status = overrides.get("match_frame_status", "DONE")
    run_frames = [
        {
            "detection_run_frame_id": 1101,
            "detection_run_id": 11,
            "frame_id": 101,
            "status": "DONE",
            "diagnostics": {},
        },
        {
            "detection_run_frame_id": 1201,
            "detection_run_id": 12,
            "frame_id": 101,
            "status": match_frame_status,
            "diagnostics": {"n_matched": 1},
        },
    ]
    if overrides.get("missing_match_frame"):
        run_frames = run_frames[:1]
    matched_catalog_source = {
        "catalog_source_id": 501,
        "catalog_name": "gaia_dr3",
        "source_id": "77",
        "ra_deg": 0.0101,
        "dec_deg": 0.0,
        "ref_mags": {"G": 15.0},
    }
    target_catalog_source = {
        "catalog_source_id": 502,
        "catalog_name": "gaia_dr3",
        "source_id": "88",
        "ra_deg": 0.011,
        "dec_deg": 0.0,
        "ref_mags": {"G": 16.0},
    }
    details = {
        "mag_key": "G",
        "working_mag_limit": 19.0,
        "max_sources_per_frame": 2000,
        "edge_margin_pix": 20.0,
    }
    link = {
        "frame_id": 101,
        "catalog_source_id": 501,
        "in_footprint": True,
        "margin_pix": 2.0,
        "match_status": "IN_FOOTPRINT",
        "details": details,
    }
    evidence = {
        "pipeline_runs": [pipeline_run],
        "pipeline_steps": steps,
        "detection_runs": [source_run, match_run],
        "detection_run_frames": run_frames,
        "detection_catalog_matches": [
            {
                "frame_id": 101,
                "detection_run_frame_id": 1201,
                "source_detection_id": 6101,
                "source_detection_run_frame_id": 1101,
                "joined_source_detection_run_frame_id": 1101,
                "source_detection_run_id": 11,
                "source_frame_id": 101,
                "catalog_source_id": 501,
                "match_status": "MATCHED",
            }
        ],
        "matched_catalog_sources": [matched_catalog_source],
        "target_catalog_sources": [target_catalog_source],
        "frame_catalog_links": [link],
    }
    evidence.update(overrides.get("evidence", {}))
    return source_path, gaia_query, _OfflineEvidenceSession(evidence)


def _append_completed_offline_run(
    evidence,
    *,
    pipeline_run_id=200,
    source_run_id=21,
    match_run_id=22,
    source_frame_id=2101,
    match_frame_id=2201,
    catalog_source_id=501,
    source_id="77",
    provenance_changes=None,
):
    from copy import deepcopy

    original_run = evidence["pipeline_runs"][0]
    original_pipeline_id = original_run["pipeline_run_id"]
    recipe = deepcopy(original_run["recipe_json"])
    provenance = deepcopy(recipe["working_catalog_provenance"])
    provenance.update(
        {
            "reuse_mode": "completed_pipeline_catalog_match",
            "new_source_search_performed": False,
            "coverage_complete": False,
            "per_frame_match_coverage_complete": True,
            "reused_catalog_source_count": 1,
            "explicit_target_included": False,
        }
    )
    provenance.update(provenance_changes or {})
    recipe["working_catalog_provenance"] = provenance
    recipe["quality_policy"]["working_catalog_provenance"] = provenance
    evidence["pipeline_runs"].append(
        {
            **deepcopy(original_run),
            "pipeline_run_id": pipeline_run_id,
            "recipe_json": recipe,
        }
    )

    for step in list(evidence["pipeline_steps"]):
        if step["pipeline_run_id"] != original_pipeline_id:
            continue
        copied_step = deepcopy(step)
        copied_step["pipeline_run_id"] = pipeline_run_id
        if copied_step["step_name"] == "detect_sources":
            copied_step["result_json"]["detection_run_id"] = source_run_id
        elif copied_step["step_name"] == "match_detections_to_catalog":
            copied_step["result_json"]["detection_run_id"] = match_run_id
        evidence["pipeline_steps"].append(copied_step)

    for detection_run in evidence["detection_runs"][:2]:
        copied_run = deepcopy(detection_run)
        copied_run["pipeline_run_id"] = pipeline_run_id
        copied_run["detection_run_id"] = (
            source_run_id if detection_run["detection_run_id"] == 11 else match_run_id
        )
        evidence["detection_runs"].append(copied_run)

    evidence["detection_run_frames"].extend(
        [
            {
                "detection_run_frame_id": source_frame_id,
                "detection_run_id": source_run_id,
                "frame_id": 101,
                "status": "DONE",
                "diagnostics": {},
            },
            {
                "detection_run_frame_id": match_frame_id,
                "detection_run_id": match_run_id,
                "frame_id": 101,
                "status": "DONE",
                "diagnostics": {"n_matched": 1},
            },
        ]
    )
    evidence["detection_catalog_matches"].append(
        {
            "frame_id": 101,
            "detection_run_frame_id": match_frame_id,
            "source_detection_id": source_frame_id + 5000,
            "source_detection_run_frame_id": source_frame_id,
            "joined_source_detection_run_frame_id": source_frame_id,
            "source_detection_run_id": source_run_id,
            "source_frame_id": 101,
            "catalog_source_id": catalog_source_id,
            "match_status": "MATCHED",
        }
    )
    if catalog_source_id != 501:
        evidence["matched_catalog_sources"].append(
            {
                "catalog_source_id": catalog_source_id,
                "catalog_name": "gaia_dr3",
                "source_id": source_id,
                "ra_deg": 0.012,
                "dec_deg": 0.0,
                "ref_mags": {"G": 15.5},
            }
        )
        link = deepcopy(evidence["frame_catalog_links"][0])
        link["catalog_source_id"] = catalog_source_id
        evidence["frame_catalog_links"].append(link)


def _run_offline_reuse(
    monkeypatch,
    tmp_path,
    *,
    target_source_id=None,
    evidence_overrides=None,
    allow_empty_catalog=False,
):
    from grandgousier.catalogs import gaia_query

    source_path, _gaia_query, session = _offline_reuse_evidence(
        monkeypatch, tmp_path, **(evidence_overrides or {})
    )
    for prior_run in (evidence_overrides or {}).get("prior_offline_runs", []):
        _append_completed_offline_run(session.evidence, **prior_run)
    monkeypatch.setattr(
        gaia_query,
        "fetch_gaia_working_catalog",
        lambda *_args, **_kwargs: pytest.fail("offline reuse must not issue Gaia TAP"),
    )
    observed = []

    def fake_pipeline(
        _session,
        config,
        *,
        working_catalog,
        working_catalog_provenance,
        working_catalog_promoted_source_ids,
        working_catalog_run_ids_by_series,
    ):
        observed.append(
            {
                "source_ids": [str(value) for value in working_catalog.source_ids],
                "provenance": working_catalog_provenance,
                "target_source_id": config.target_source_id,
            }
        )
        return _fake_pipeline_result()

    monkeypatch.setattr(auto_process_module, "run_field_pipeline", fake_pipeline)
    result = process_archive_path(
        session,
        root=tmp_path,
        exact_paths=[source_path],
        series_ids=[1],
        execute=True,
        make_light_curve_summary=False,
        auto_catalog_query=False,
        allow_empty_catalog=allow_empty_catalog,
        target_source_id=target_source_id,
    )
    return result, observed, session


def test_offline_gaia_reuse_uses_only_completed_scoped_match_identities(
    monkeypatch, tmp_path
):
    result, observed, session = _run_offline_reuse(monkeypatch, tmp_path)

    assert result.status == "DONE_WITH_WARNINGS"
    assert observed[0]["source_ids"] == ["77"]
    assert observed[0]["provenance"]["new_source_search_performed"] is False
    assert observed[0]["provenance"]["coverage_complete"] is False
    assert result.catalog_results[0]["new_source_search_performed"] is False
    assert result.catalog_results[0]["coverage_complete"] is False
    assert result.catalog_results[0]["per_frame_match_coverage_complete"] is True
    assert result.catalog_results[0]["working_catalog_provenance"][
        "new_source_search_performed"
    ] is False
    assert result.catalog_results[0]["working_catalog_provenance"][
        "coverage_complete"
    ] is False
    assert result.catalog_results[0]["working_catalog_provenance"][
        "field_discarded_transient_identity_count"
    ] is None
    assert not any("gaiadr3.gaia_source" in sql for sql, _ in session.statements)
    catalog_queries = [sql for sql, _ in session.statements if "FROM catalog_sources" in sql]
    assert catalog_queries
    assert all(
        "catalog_source_id IN" in sql or "source_id =" in sql
        for sql in catalog_queries
    )


def test_second_offline_gaia_reuse_accepts_matching_online_and_offline_history(
    monkeypatch, tmp_path
):
    result, observed, session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"prior_offline_runs": [{}]},
    )

    assert result.status == "DONE_WITH_WARNINGS"
    assert observed[0]["source_ids"] == ["77"]
    assert observed[0]["provenance"]["new_source_search_performed"] is False
    assert observed[0]["provenance"]["coverage_complete"] is False
    assert observed[0]["provenance"]["per_frame_match_coverage_complete"] is True
    assert result.catalog_results[0]["new_source_search_performed"] is False
    assert result.catalog_results[0]["coverage_complete"] is False
    online_provenance = session.evidence["pipeline_runs"][0]["recipe_json"][
        "working_catalog_provenance"
    ]
    reused_provenance = result.catalog_results[0]["working_catalog_provenance"]
    assert reused_provenance["frame_scope_sha256"] == online_provenance["frame_scope_sha256"]
    assert reused_provenance["footprint_sha256"] == online_provenance["footprint_sha256"]
    assert reused_provenance["fetch_status"] == online_provenance["fetch_status"]
    assert reused_provenance["fetch_complete"] is online_provenance["fetch_complete"]
    assert result.catalog_results[0]["reused_pipeline_count"] == 1


@pytest.mark.parametrize(
    ("prior_run", "expected_reason"),
    [
        (
            {"provenance_changes": {"provider_attempts": 2}},
            "AMBIGUOUS_QUERY_PROVENANCE",
        ),
        (
            {"catalog_source_id": 503, "source_id": "99"},
            "AMBIGUOUS_COMPLETED_FRAME_EVIDENCE",
        ),
    ],
)
def test_offline_gaia_reuse_refuses_conflicting_completed_history(
    monkeypatch, tmp_path, prior_run, expected_reason
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"prior_offline_runs": [prior_run]},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == expected_reason
    assert result.catalog_results[0]["new_source_search_performed"] is False
    assert result.catalog_results[0]["coverage_complete"] is False


@pytest.mark.parametrize(
    ("provenance_change", "expected_reason"),
    [
        ({"frame_scope_sha256": "f" * 64}, "FIELD_FRAME_SCOPE_MISMATCH"),
        ({"footprint_sha256": "e" * 64}, "WCS_FOOTPRINT_MISMATCH"),
    ],
)
def test_offline_gaia_reuse_refuses_scope_or_footprint_mismatch(
    monkeypatch, tmp_path, provenance_change, expected_reason
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"provenance": provenance_change},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == expected_reason
    assert result.catalog_results[0]["new_source_search_performed"] is False
    assert result.catalog_results[0]["coverage_complete"] is False


def test_offline_gaia_reuse_refuses_incompatible_matcher_configuration(
    monkeypatch, tmp_path
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"recipe": {"detection_match_radius_arcsec": 3.0}},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == (
        "PIPELINE_CONFIGURATION_MISMATCH"
    )


@pytest.mark.parametrize("allow_empty_catalog", [False, True])
def test_offline_gaia_reuse_requires_explicit_empty_flag_without_compatible_evidence(
    monkeypatch, tmp_path, allow_empty_catalog
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        allow_empty_catalog=allow_empty_catalog,
        evidence_overrides={"evidence": {"pipeline_runs": []}},
    )

    row = result.catalog_results[0]
    assert row["reuse_compatible"] is False
    assert row["refusal_reason"] == "NO_COMPLETED_PIPELINE_EVIDENCE"
    if allow_empty_catalog:
        assert result.status == "DONE_WITH_WARNINGS"
        assert len(observed) == 1
        assert observed[0]["source_ids"] == []
        assert row["empty_catalog_debug_fallback"] is True
        assert row["new_source_search_performed"] is False
        assert row["coverage_complete"] is False
        assert row["per_frame_match_coverage_complete"] is False
        assert observed[0]["provenance"]["new_source_search_performed"] is False
        assert observed[0]["provenance"]["coverage_complete"] is False
    else:
        assert result.status == "FAILED_PRECONDITION_CATALOG"
        assert observed == []
        assert "empty_catalog_debug_fallback" not in row


def test_legacy_query_coverage_diagnostic_cannot_replace_compact_fetch_proof(
    monkeypatch, tmp_path
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"provenance": {"fetch_complete": False}},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == "FETCH_PROVENANCE_INCOMPLETE"


@pytest.mark.parametrize(
    ("frame_overrides", "expected_reason"),
    [
        ({"missing_match_frame": True}, "INCOMPLETE_SUCCESSFUL_FRAME_COVERAGE"),
        ({"match_frame_status": "FAILED"}, "INCOMPLETE_SUCCESSFUL_FRAME_COVERAGE"),
    ],
)
def test_offline_gaia_reuse_refuses_incomplete_or_failed_frame_coverage(
    monkeypatch, tmp_path, frame_overrides, expected_reason
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides=frame_overrides,
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == expected_reason


def test_offline_gaia_reuse_refuses_missing_match_row_despite_positive_diagnostics(
    monkeypatch, tmp_path
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"evidence": {"detection_catalog_matches": []}},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == "MATCH_ROW_COUNT_MISMATCH"


@pytest.mark.parametrize(
    "source_evidence_change",
    [
        {
            "source_detection_run_frame_id": 2101,
            "joined_source_detection_run_frame_id": 2101,
            "source_detection_run_id": 21,
            "source_frame_id": 101,
        },
        {
            "source_detection_run_frame_id": 1102,
            "joined_source_detection_run_frame_id": 1102,
            "source_detection_run_id": 11,
            "source_frame_id": 102,
        },
    ],
)
def test_offline_gaia_reuse_refuses_match_from_different_detection_run_or_frame(
    monkeypatch, tmp_path, source_evidence_change
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={
            "evidence": {
                "detection_catalog_matches": [
                    {
                        "frame_id": 101,
                        "detection_run_frame_id": 1201,
                        "source_detection_id": 6101,
                        "source_detection_run_frame_id": 1101,
                        "joined_source_detection_run_frame_id": 1101,
                        "source_detection_run_id": 11,
                        "source_frame_id": 101,
                        "catalog_source_id": 501,
                        "match_status": "MATCHED",
                        **source_evidence_change,
                    }
                ]
            }
        },
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert (
        result.catalog_results[0]["refusal_reason"]
        == "MATCH_SOURCE_DETECTION_SCOPE_MISMATCH"
    )


@pytest.mark.parametrize(
    ("evidence_change", "expected_reason"),
    [
        ({"matched_catalog_sources": []}, "MATCHED_GAIA_IDENTITY_MISSING"),
        ({"frame_catalog_links": []}, "MATCHED_FRAME_LINK_MISSING_OR_INCOMPATIBLE"),
    ],
)
def test_offline_gaia_reuse_refuses_missing_identity_or_corresponding_link(
    monkeypatch, tmp_path, evidence_change, expected_reason
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        evidence_overrides={"evidence": evidence_change},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == expected_reason


def test_offline_gaia_reuse_includes_target_only_with_canonical_row_and_scoped_link(
    monkeypatch, tmp_path
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        target_source_id="88",
        evidence_overrides={
            "evidence": {
                "frame_catalog_links": [
                    {
                        "frame_id": 101,
                        "catalog_source_id": 501,
                        "in_footprint": True,
                        "margin_pix": 2.0,
                        "match_status": "IN_FOOTPRINT",
                        "details": {
                            "mag_key": "G",
                            "working_mag_limit": 19.0,
                            "max_sources_per_frame": 2000,
                            "edge_margin_pix": 20.0,
                        },
                    },
                    {
                        "frame_id": 101,
                        "catalog_source_id": 502,
                        "in_footprint": True,
                        "margin_pix": 2.0,
                        "match_status": "IN_FOOTPRINT",
                        "details": {
                            "mag_key": "G",
                            "working_mag_limit": 19.0,
                            "max_sources_per_frame": 2000,
                            "edge_margin_pix": 20.0,
                        },
                    },
                ]
            }
        },
    )

    assert result.status == "DONE_WITH_WARNINGS"
    assert observed[0]["source_ids"] == ["77", "88"]
    assert observed[0]["provenance"]["explicit_target_included"] is True


def test_offline_gaia_reuse_refuses_unscoped_target_and_never_promotes_it(
    monkeypatch, tmp_path
):
    result, observed, session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        target_source_id="88",
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == (
        "EXPLICIT_TARGET_HAS_NO_EXACT_SCOPE_LINK"
    )
    assert all(
        "catalog_source_id IN" in sql or "source_id =" in sql
        for sql, _ in session.statements
        if "FROM catalog_sources" in sql
    )


def test_offline_gaia_reuse_refuses_target_without_canonical_identity(
    monkeypatch, tmp_path
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        target_source_id="88",
        evidence_overrides={"evidence": {"target_catalog_sources": []}},
    )

    assert result.status == "FAILED_PRECONDITION_CATALOG"
    assert observed == []
    assert result.catalog_results[0]["refusal_reason"] == (
        "EXPLICIT_TARGET_NOT_CANONICAL"
    )


@pytest.mark.parametrize("allow_empty_catalog", [False, True])
def test_explicit_empty_catalog_debugging_escape_does_not_reuse_incomplete_evidence(
    monkeypatch, tmp_path, allow_empty_catalog
):
    result, observed, _session = _run_offline_reuse(
        monkeypatch,
        tmp_path,
        allow_empty_catalog=allow_empty_catalog,
        evidence_overrides={
            "evidence": {
                "detection_catalog_matches": [],
                "matched_catalog_sources": [],
                "frame_catalog_links": [],
            }
        },
    )

    row = result.catalog_results[0]
    assert bool(observed) is allow_empty_catalog
    assert row["reuse_compatible"] is False
    assert row["refusal_reason"] == "MATCH_ROW_COUNT_MISMATCH"
    if allow_empty_catalog:
        assert result.status == "DONE_WITH_WARNINGS"
        assert observed[0]["source_ids"] == []
        assert row["empty_catalog_debug_fallback"] is True
        assert row["new_source_search_performed"] is False
        assert row["coverage_complete"] is False
    else:
        assert result.status == "FAILED_PRECONDITION_CATALOG"
