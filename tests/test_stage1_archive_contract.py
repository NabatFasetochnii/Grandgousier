from __future__ import annotations

import csv
from pathlib import Path

import pytest

try:
    from astropy.io import fits
    from grandgousier.archive.stage1_adapters import (
        FilesystemProductStore,
        FitsFilesystemMetadataProvider,
        MasterCacheAssetProvider,
        MasterManifestMetadataProvider,
        build_archive_source_runtime,
        load_instrument_profile,
    )
    ASTROPY_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "astropy":
        raise
    fits = None
    ASTROPY_AVAILABLE = False

pytestmark = pytest.mark.skipif(not ASTROPY_AVAILABLE, reason="astropy is not installed")
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    LocalAsset,
    ObservationRecord,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_planner import build_stage1_plan
from grandgousier.identity.observation import MASTER_IDENTITY_SCHEME, ROBOPHOT_IDENTITY_SCHEME


def _write_master_fits(path: Path, *, uniqnum: str = "203_17870") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = fits.Header()
    header["UNIQNUM"] = uniqnum
    header["IMAGETYP"] = "LIGHT"
    header["FILTER"] = "V"
    header["EXPTIME"] = 50.0
    header["ID_CAM"] = 204
    header["DATE-OBS"] = "2018-09-07T17:04:22"
    header["CRVAL1"] = 327.525
    header["CRVAL2"] = 72.183
    fits.PrimaryHDU(data=[[1.0]], header=header).writeto(path)


def _write_robophot_fits(path: Path, *, date_obs: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = fits.Header()
    header["SERNUM"] = "ML0742515"
    header["IMAGETYP"] = "LIGHT"
    header["FILTER"] = "i"
    header["EXPTIME"] = 30.0
    header["DATE-OBS"] = date_obs
    fits.PrimaryHDU(data=[[1.0]], header=header).writeto(path)


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def test_master_manifest_and_filesystem_map_to_same_p4_identity(tmp_path: Path) -> None:
    profile = load_instrument_profile("MASTER")
    local_root = tmp_path / "local"
    local_path = local_root / "night" / "frame.fits"
    _write_master_fits(local_path)

    local_provider = FitsFilesystemMetadataProvider(
        source_id="local-master",
        root=local_root,
        profile=profile,
        science_asset_role="processed",
    )
    local_record = list(local_provider.search(ArchiveRequest(source_id="local-master")))[0]

    manifest = tmp_path / "master.csv"
    _write_manifest(
        manifest,
        [
            {
                "uniqnum": "203_17870",
                "datetime": "2018-09-07T17:04:22",
                "filter": "V",
                "exptime": 50.0,
                "camera": 204,
                "coord2000": "21h50m06s +72d11m00s",
                "raw_path": "/master/raw/375506.fit.gz",
                "header_path": "/master/proc/375506_header.fits",
                "dark_path": "/master/raw/375453.fit.gz",
                "flat_path": "/master/raw/373624.fit.gz",
            }
        ],
    )
    master_provider = MasterManifestMetadataProvider(
        source_id="master-prod",
        manifest=manifest,
        profile=profile,
        cache_root=tmp_path / "cache",
    )
    master_record = list(master_provider.search(ArchiveRequest(source_id="master-prod")))[0]

    assert local_record.identity_status == master_record.identity_status == "READY"
    assert local_record.identity_scheme == master_record.identity_scheme == MASTER_IDENTITY_SCHEME
    assert local_record.p4_native_identity == master_record.p4_native_identity == "203_17870"
    assert local_record.observation_uid == master_record.observation_uid
    assert local_record.filter_name == master_record.filter_name == "V"
    assert local_record.exptime == master_record.exptime == 50.0


def test_filesystem_relocation_does_not_change_source_identity(tmp_path: Path) -> None:
    profile = load_instrument_profile("MASTER")
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    _write_master_fits(left_root / "night" / "frame.fits")
    _write_master_fits(right_root / "night" / "frame.fits")

    left = list(
        FitsFilesystemMetadataProvider(
            source_id="stable-source", root=left_root, profile=profile
        ).search(ArchiveRequest(source_id="stable-source"))
    )[0]
    right = list(
        FitsFilesystemMetadataProvider(
            source_id="stable-source", root=right_root, profile=profile
        ).search(ArchiveRequest(source_id="stable-source"))
    )[0]

    assert left.native_observation_id == right.native_observation_id == "203_17870"
    assert left.source_observation_uid == right.source_observation_uid
    assert left.observation_uid == right.observation_uid
    assert left.assets[0].locator == right.assets[0].locator == "night/frame.fits"


def test_plan_never_materializes_assets(tmp_path: Path) -> None:
    native = "203_17870"
    record = ObservationRecord(
        source_id="fake",
        instrument="MASTER",
        frame_type="science",
        input_state="processed",
        identity_status="READY",
        native_observation_id=native,
        source_observation_uid=make_source_observation_uid("fake", native),
        identity_scheme=MASTER_IDENTITY_SCHEME,
        p4_native_identity=native,
        observation_uid="obs_test",
        required_asset_roles=("processed",),
        assets=(AssetRef("fake", "processed", "filesystem", "frame.fits"),),
    )

    class Metadata:
        def search(self, request):
            return [record]

    class CountingAssets:
        def __init__(self):
            self.calls = 0

        def materialize(self, ref, destination=None):
            self.calls += 1
            return LocalAsset(ref=ref, path=Path(ref.locator))

    assets = CountingAssets()
    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="fake"),
        source=ArchiveSourceDescriptor(
            source_id="fake",
            adapter="fake",
            instrument_profile="MASTER",
            metadata_backend="fake",
            asset_backend="fake",
        ),
        metadata_provider=Metadata(),
        asset_provider=assets,
        product_store=FilesystemProductStore(root=tmp_path / "products"),
    )
    assert plan.status == "READY"
    assert assets.calls == 0


def test_master_manifest_preserves_separate_header_asset(tmp_path: Path) -> None:
    manifest = tmp_path / "master.csv"
    _write_manifest(
        manifest,
        [
            {
                "uniqnum": "203_17870",
                "raw_path": "/master/raw/375506.fit.gz",
                "header_path": "/master/proc/375506_header.fits",
                "dark_path": "/master/raw/375453.fit.gz",
                "flat_path": "/master/raw/373624.fit.gz",
            }
        ],
    )
    provider = MasterManifestMetadataProvider(
        source_id="master-prod",
        manifest=manifest,
        profile=load_instrument_profile("MASTER"),
        cache_root=tmp_path / "cache",
    )
    record = list(provider.search(ArchiveRequest(source_id="master-prod")))[0]
    assert record.asset("science_raw").locator == "/master/raw/375506.fit.gz"
    assert record.asset("processing_header").locator == "/master/proc/375506_header.fits"
    assert record.asset("processing_header").ref_uid != record.asset("science_raw").ref_uid


def test_product_store_is_independent_from_source_locator(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    product_root = tmp_path / "unrelated-products"
    _write_master_fits(source_root / "archive" / "frame.fits")
    record = list(
        FitsFilesystemMetadataProvider(
            source_id="local", root=source_root, profile=load_instrument_profile("MASTER")
        ).search(ArchiveRequest(source_id="local"))
    )[0]
    store = FilesystemProductStore(root=product_root)
    ref = store.plan_product(record)
    resolved = store.resolve(ref)
    assert resolved.is_relative_to(product_root)
    assert "archive/frame.fits" not in ref.locator
    assert str(source_root) not in ref.locator
    assert "/2018/09/07/" in f"/{ref.locator}"
    assert "/undated/" not in f"/{ref.locator}"


def test_master_missing_uniqnum_is_blocked_without_raw_id_fallback(tmp_path: Path) -> None:
    manifest = tmp_path / "master.csv"
    _write_manifest(
        manifest,
        [
            {
                "raw_id": 375506,
                "processed_frame_id": 131841,
                "raw_path": "/master/raw/375506.fit.gz",
                "header_path": "/master/proc/375506_header.fits",
                "dark_path": "/master/raw/375453.fit.gz",
                "flat_path": "/master/raw/373624.fit.gz",
            }
        ],
    )
    provider = MasterManifestMetadataProvider(
        source_id="master-prod",
        manifest=manifest,
        profile=load_instrument_profile("MASTER"),
        cache_root=tmp_path / "empty-cache",
    )
    record = list(provider.search(ArchiveRequest(source_id="master-prod")))[0]
    assert record.identity_status == "MISSING"
    assert record.native_observation_id is None
    assert record.source_observation_uid is None

    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="master-prod"),
        source=ArchiveSourceDescriptor(
            source_id="master-prod",
            adapter="master-manifest",
            instrument_profile="MASTER",
            metadata_backend="prepared-inventory",
            asset_backend="master-cache",
        ),
        metadata_provider=provider,
        asset_provider=object(),
        product_store=FilesystemProductStore(root=tmp_path / "products"),
    )
    assert plan.status == "BLOCKED"
    assert "path/raw-id fallback is forbidden" in plan.items[0].issues[0]


def test_master_cached_processing_header_can_supply_uniqnum(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    header_path = cache / "master" / "proc" / "375506_header.fits"
    _write_master_fits(header_path, uniqnum="203_17870")
    manifest = tmp_path / "master.csv"
    _write_manifest(
        manifest,
        [
            {
                "raw_id": 375506,
                "header_path": "/master/proc/375506_header.fits",
                "raw_path": "/master/raw/375506.fit.gz",
                "dark_path": "/master/raw/375453.fit.gz",
                "flat_path": "/master/raw/373624.fit.gz",
            }
        ],
    )
    provider = MasterManifestMetadataProvider(
        source_id="master-prod",
        manifest=manifest,
        profile=load_instrument_profile("MASTER"),
        cache_root=cache,
    )
    record = list(provider.search(ArchiveRequest(source_id="master-prod")))[0]
    assert record.identity_status == "READY"
    assert record.native_observation_id == "203_17870"


def test_robophot_timezone_less_date_obs_remains_utc(tmp_path: Path) -> None:
    root = tmp_path / "robophot"
    _write_robophot_fits(root / "frame.fits", date_obs="2026-03-28T21:44:39.568")
    record = list(
        FitsFilesystemMetadataProvider(
            source_id="robophot-local",
            root=root,
            profile=load_instrument_profile("RoboPhot"),
        ).search(ArchiveRequest(source_id="robophot-local"))
    )[0]
    assert record.identity_status == "READY"
    assert record.identity_scheme == ROBOPHOT_IDENTITY_SCHEME
    assert record.native_observation_id == "ML0742515|2026-03-28T21:44:39.568Z"


def test_robophot_nonzero_timezone_offset_is_not_reinterpreted(tmp_path: Path) -> None:
    root = tmp_path / "robophot"
    _write_robophot_fits(root / "frame.fits", date_obs="2026-03-28T21:44:39+05:00")
    record = list(
        FitsFilesystemMetadataProvider(
            source_id="robophot-local",
            root=root,
            profile=load_instrument_profile("RoboPhot"),
        ).search(ArchiveRequest(source_id="robophot-local"))
    )[0]
    assert record.identity_status == "INVALID"
    assert "must already be UTC" in str(record.identity_detail)


def test_archive_source_descriptor_redacts_embedded_url_credentials(tmp_path: Path) -> None:
    payload = {
        "archives": {
            "local": {
                "adapter": "fits-filesystem",
                "instrument_profile": "MASTER",
                "metadata": {
                    "backend": "fits-headers",
                    "root": str(tmp_path),
                    "endpoint": "https://user:secret@example.invalid/data",
                },
                "assets": {"backend": "filesystem", "root": str(tmp_path)},
            }
        }
    }
    runtime = build_archive_source_runtime(payload, "local")
    public = runtime.descriptor.to_dict()
    assert public["config"]["metadata"]["endpoint"] == "<configured>"
    assert "secret" not in str(public)


def test_adapter_factories_reject_incompatible_backends(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="metadata.backend=fits-headers"):
        build_archive_source_runtime(
            {
                "archives": {
                    "local": {
                        "adapter": "fits-filesystem",
                        "instrument_profile": "MASTER",
                        "metadata": {"backend": "sql", "root": str(tmp_path)},
                        "assets": {"backend": "filesystem", "root": str(tmp_path)},
                    }
                }
            },
            "local",
        )



def test_master_asset_provider_reuses_secondary_partial_cache(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    local = secondary / "master" / "imdata" / "rawdata" / "2018" / "09" / "07" / "375506.fit.gz"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"already-cached")
    ref = AssetRef(
        source_id="master-prod",
        role="science_raw",
        backend="master-cache",
        locator="/master/imdata/rawdata/2018/09/07/375506.fit.gz",
    )
    provider = MasterCacheAssetProvider(
        source_id="master-prod",
        cache_root=primary,
        search_roots=(secondary,),
        fetch_missing=False,
    )
    materialized = provider.materialize(ref)
    assert materialized.path == local
    assert not (primary / "master" / "imdata" / "rawdata" / "2018" / "09" / "07" / "375506.fit.gz").exists()


def test_master_asset_provider_fetches_only_missing_selected_asset(tmp_path: Path) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeResult:
        status = "DOWNLOADED"
        error = None

    class FakeTask:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    session = FakeSession()
    calls: list[str] = []

    class FakeTransport:
        MasterFetchTask = FakeTask

        @staticmethod
        def resolve_master_http_credentials():
            return "user", "password"

        @staticmethod
        def make_master_http_session(*, user, password, retries):
            assert (user, password, retries) == ("user", "password", 4)
            return session

        @staticmethod
        def master_remote_path_to_url(remote_path):
            return "https://example.invalid/data" + remote_path

        @staticmethod
        def download_master_task(task, *, session, timeout_connect, timeout_read, overwrite, checksum):
            calls.append(task.remote_path)
            assert session is not None
            assert overwrite is False
            assert checksum is False
            task.local_path.parent.mkdir(parents=True, exist_ok=True)
            task.local_path.write_bytes(b"downloaded-selected-asset")
            return FakeResult()

    ref = AssetRef(
        source_id="master-prod",
        role="science_raw",
        backend="master-cache",
        locator="/master/imdata/rawdata/2018/09/07/375506.fit.gz",
    )
    provider = MasterCacheAssetProvider(
        source_id="master-prod",
        cache_root=tmp_path / "cache",
        fetch_missing=True,
    )
    provider._repository_transport = lambda: FakeTransport  # type: ignore[method-assign]
    materialized = provider.materialize(ref)
    assert materialized.path.read_bytes() == b"downloaded-selected-asset"
    assert calls == [ref.locator]
    provider.close()
    assert session.closed is True


def test_master_runtime_separates_metadata_and_asset_cache_roots(tmp_path: Path) -> None:
    metadata_cache = tmp_path / "metadata-cache"
    asset_cache = tmp_path / "asset-cache"
    header_path = metadata_cache / "master" / "proc" / "375506_header.fits"
    _write_master_fits(header_path, uniqnum="203_17870")
    raw_path = asset_cache / "master" / "raw" / "375506.fit.gz"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"raw")
    manifest = tmp_path / "master.csv"
    _write_manifest(
        manifest,
        [
            {
                "raw_id": 375506,
                "header_path": "/master/proc/375506_header.fits",
                "raw_path": "/master/raw/375506.fit.gz",
                "dark_path": "/master/raw/375453.fit.gz",
                "flat_path": "/master/raw/373624.fit.gz",
            }
        ],
    )
    runtime = build_archive_source_runtime(
        {
            "archives": {
                "master-prod": {
                    "adapter": "master-manifest",
                    "instrument_profile": "MASTER",
                    "metadata": {
                        "backend": "prepared-inventory",
                        "manifest": str(manifest),
                        "cache_root": str(metadata_cache),
                    },
                    "assets": {
                        "backend": "master-cache",
                        "cache_root": str(asset_cache),
                        "fetch_missing": False,
                        "stage1_cache_max_bytes": 1234,
                        "stage1_cache_min_free_bytes": 5678,
                        "stage1_cache_retention_days": 3,
                    },
                }
            }
        },
        "master-prod",
    )
    record = list(runtime.metadata_provider.search(ArchiveRequest(source_id="master-prod")))[0]
    assert record.native_observation_id == "203_17870"
    raw_ref = next(ref for ref in record.assets if ref.role == "science_raw")
    assert runtime.asset_provider.materialize(raw_ref).path == raw_path
    header_ref = next(ref for ref in record.assets if ref.role == "processing_header")
    assert runtime.asset_provider.materialize(header_ref).path == header_path
    assert runtime.asset_provider.cache_policy.max_bytes == 1234
    assert runtime.asset_provider.cache_policy.min_free_bytes == 5678
    assert runtime.asset_provider.cache_policy.retention_days == 3
