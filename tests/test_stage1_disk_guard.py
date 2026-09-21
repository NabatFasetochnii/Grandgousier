from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.archive import disk_guard
from grandgousier.archive.disk_guard import (
    DEFAULT_UNKNOWN_ASSET_BYTES,
    DiskRequirement,
    DiskSpaceGuardError,
    check_disk_budget,
    estimate_size,
    normalize_disk_reserve,
)
from grandgousier.archive.stage1_adapters import FilesystemProductStore, MasterCacheAssetProvider
from grandgousier.archive.stage1_contract import AssetRef


def _usage(free: int):
    return SimpleNamespace(free=free)


def test_enough_space_and_exact_boundary_are_allowed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda _path: _usage(150))

    diagnostics = check_disk_budget(
        (DiskRequirement(tmp_path / "output.fits", 100, "output"),), reserve_bytes=50
    )

    assert diagnostics[0].deficit_bytes == 0
    assert diagnostics[0].required_bytes == 100


def test_insufficient_space_reports_required_free_reserve_and_deficit(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda _path: _usage(149))

    with pytest.raises(DiskSpaceGuardError, match="required=100, free=149, reserve=50, deficit=1"):
        check_disk_budget(
            (DiskRequirement(tmp_path / "output.fits", 100, "output"),), reserve_bytes=50
        )


def test_same_filesystem_requirements_are_aggregated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(disk_guard, "_filesystem_for", lambda path: (1, tmp_path))
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda _path: _usage(149))

    with pytest.raises(DiskSpaceGuardError, match="required=100, free=149"):
        check_disk_budget(
            (
                DiskRequirement(tmp_path / "cache" / "asset.part", 60, "asset"),
                DiskRequirement(tmp_path / "products" / "output.part", 40, "product"),
            ),
            reserve_bytes=50,
        )


def test_different_filesystems_are_checked_independently(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setattr(
        disk_guard,
        "_filesystem_for",
        lambda path: (1 if "first" in str(path) else 2, first if "first" in str(path) else second),
    )
    monkeypatch.setattr(
        disk_guard.shutil,
        "disk_usage",
        lambda path: _usage(100 if path == first else 200),
    )

    diagnostics = check_disk_budget(
        (
            DiskRequirement(first / "asset.part", 50, "asset"),
            DiskRequirement(second / "output.part", 100, "output"),
        ),
        reserve_bytes=50,
    )

    assert len(diagnostics) == 2


def test_missing_target_uses_existing_parent(monkeypatch, tmp_path: Path) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(
        disk_guard.shutil,
        "disk_usage",
        lambda path: (observed.append(Path(path)) or _usage(200)),
    )

    check_disk_budget(
        (DiskRequirement(tmp_path / "not-yet-created" / "output.fits", 1, "output"),),
        reserve_bytes=1,
    )

    assert observed == [tmp_path.resolve()]


def test_unknown_size_uses_conservative_fallback() -> None:
    assert estimate_size(None, fallback=DEFAULT_UNKNOWN_ASSET_BYTES) == DEFAULT_UNKNOWN_ASSET_BYTES
    assert estimate_size("invalid", fallback=DEFAULT_UNKNOWN_ASSET_BYTES) == DEFAULT_UNKNOWN_ASSET_BYTES


def test_negative_reserve_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        normalize_disk_reserve(-1)


def test_zero_and_explicit_reserve_values_are_preserved(tmp_path: Path) -> None:
    assert FilesystemProductStore(root=tmp_path, disk_reserve_bytes=0).disk_reserve_bytes == 0
    assert FilesystemProductStore(root=tmp_path, disk_reserve_bytes=123).disk_reserve_bytes == 123


def test_filesystem_resolution_failure_is_fail_safe(monkeypatch, tmp_path: Path) -> None:
    def fail_resolution(_path):
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(disk_guard, "_filesystem_for", fail_resolution)

    with pytest.raises(DiskSpaceGuardError, match="could not determine target filesystem"):
        check_disk_budget((DiskRequirement(tmp_path / "output", 10, "output"),), reserve_bytes=5)


def test_master_asset_provider_preflights_before_missing_asset_download(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeTransport:
        MasterFetchTask = object

        @staticmethod
        def master_remote_path_to_url(remote_path):
            return "https://example.invalid/data" + remote_path

        @staticmethod
        def download_master_task(*args, **kwargs):
            raise AssertionError("download must not start after disk preflight failure")

    monkeypatch.setattr(disk_guard.shutil, "disk_usage", lambda _path: _usage(1))
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
        disk_reserve_bytes=0,
    )
    provider._repository_transport = lambda: FakeTransport  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="disk preflight"):
        provider.materialize(ref)

    assert not (tmp_path / "cache").exists()
