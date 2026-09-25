from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from astropy.io import fits
import numpy as np

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
