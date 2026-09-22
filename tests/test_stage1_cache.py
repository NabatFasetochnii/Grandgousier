from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.archive import stage1_cache
from grandgousier.archive.stage1_cache import (
    CACHE_MARKER_SCHEMA,
    Stage1CachePolicy,
    claim_cache_root,
    cleanup_atomic_output_parts,
    cleanup_master_cache,
    normalize_cache_policy,
)
from grandgousier.archive.stage1_adapters import MasterCacheAssetProvider
from grandgousier.archive.stage1_contract import AssetRef


def _owned_cache(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    assert claim_cache_root(root, source_id="master-test")
    return root


def _old(path: Path, now: float) -> None:
    os.utime(path, (now - 10_000, now - 10_000))


def _policy(**overrides) -> Stage1CachePolicy:
    values = {
        "max_bytes": None,
        "min_free_bytes": 0,
        "retention_days": 0,
        "cleanup_grace_seconds": 0,
        "cleanup_interval_seconds": 0,
        "cleanup_margin_bytes": 0,
        "candidate_limit": 16,
    }
    values.update(overrides)
    return Stage1CachePolicy(**values)


def test_owned_cache_eviction_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = _owned_cache(tmp_path)
    now = time.time()
    oldest = root / "oldest.fit"
    newest = root / "newest.fit"
    oldest.write_bytes(b"123456")
    newest.write_bytes(b"abcdef")
    _old(oldest, now)
    _old(newest, now)
    os.utime(newest, (now - 5_000, now - 5_000))

    result = cleanup_master_cache(
        root,
        policy=_policy(max_bytes=6),
        source_id="master-test",
        now=now,
    )
    assert result.removed == 1
    assert result.bytes_freed == 6
    assert not oldest.exists()
    assert newest.exists()
    assert root.is_dir()


def test_cache_under_quota_recent_active_and_dry_run_are_preserved(tmp_path: Path) -> None:
    root = _owned_cache(tmp_path)
    now = time.time()
    recent = root / "recent.fit"
    active = root / "active.fit"
    recent.write_bytes(b"recent")
    active.write_bytes(b"active")
    os.utime(recent, (now + 100, now + 100))
    os.utime(active, (now + 100, now + 100))
    result = cleanup_master_cache(
        root,
        policy=_policy(max_bytes=1024, retention_days=10),
        active_paths=(active,),
        source_id="master-test",
        now=now,
    )
    assert result.removed == 0
    assert recent.exists() and active.exists()

    stale = root / "stale.fit"
    stale.write_bytes(b"stale")
    _old(stale, now)
    result = cleanup_master_cache(
        root,
        policy=_policy(max_bytes=1, cleanup_margin_bytes=0),
        source_id="master-test",
        dry_run=True,
        now=now,
    )
    assert result.removed == 1
    assert stale.exists()


def test_cache_min_free_pressure_and_unknown_owner_are_fail_safe(monkeypatch, tmp_path: Path) -> None:
    root = _owned_cache(tmp_path)
    stale = root / "stale.fit"
    stale.write_bytes(b"1234567890")
    now = time.time()
    _old(stale, now)
    usage = type("Usage", (), {"free": 10})()
    monkeypatch.setattr(stage1_cache.shutil, "disk_usage", lambda _: usage)
    result = cleanup_master_cache(
        root,
        policy=_policy(min_free_bytes=20),
        source_id="master-test",
        now=now,
    )
    assert result.removed == 1
    assert not stale.exists()

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    unsafe = unknown / "unsafe.fit"
    unsafe.write_bytes(b"unsafe")
    result = cleanup_master_cache(
        unknown,
        policy=_policy(max_bytes=0),
        now=now,
    )
    assert result.removed == 0
    assert result.skipped_unsafe == 1
    assert unsafe.exists()


def test_resumable_download_part_is_preserved_and_invalid_part_is_not_deleted(tmp_path: Path) -> None:
    root = _owned_cache(tmp_path)
    part = root / "asset.fit.part"
    part.write_bytes(b"partial")
    part.with_name(part.name + ".meta.json").write_text(
        '{"schema": "' + CACHE_MARKER_SCHEMA + '", "remote_path": "/master/x.fit"}\n',
        encoding="utf-8",
    )
    invalid = root / "unknown.fit.part"
    invalid.write_bytes(b"unknown")
    result = cleanup_master_cache(
        root,
        policy=_policy(max_bytes=0),
        source_id="master-test",
        now=time.time(),
    )
    assert result.preserved_resumable == 1
    assert result.skipped_unsafe >= 1
    assert part.exists() and invalid.exists()


def test_product_part_cleanup_is_owned_bounded_and_never_removes_final(tmp_path: Path) -> None:
    root = tmp_path / "products"
    root.mkdir()
    now = time.time()
    stale = root / "stale.fits.part"
    fresh = root / "fresh.fits.part"
    final = root / "final.fits"
    checkpoint = root / "run.checkpoint.sqlite3.part"
    stale.write_bytes(b"stale")
    fresh.write_bytes(b"fresh")
    final.write_bytes(b"final")
    checkpoint.write_bytes(b"checkpoint")
    _old(stale, now)
    os.utime(fresh, (now + 100, now + 100))
    result = cleanup_atomic_output_parts(root, grace_seconds=0, now=now)
    assert result.removed == 1
    assert not stale.exists() and fresh.exists()
    assert final.exists()
    assert checkpoint.exists()


def test_product_part_symlink_escape_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "products"
    outside = tmp_path / "outside.part"
    root.mkdir()
    outside.write_bytes(b"outside")
    link = root / "escape.part"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    result = cleanup_atomic_output_parts(root, grace_seconds=0, now=time.time())
    assert result.removed == 0
    assert outside.exists()


def test_cache_policy_validation_and_scale_iterator(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        normalize_cache_policy(retention_days=-1)
    with pytest.raises(ValueError):
        normalize_cache_policy(min_free_bytes=-1)

    root = _owned_cache(tmp_path)
    shared = root / "shared.fit"
    shared.write_bytes(b"x")
    stat = shared.stat()
    calls = 50_000

    def synthetic(_root):
        for _ in range(calls):
            yield shared, stat

    monkeypatch.setattr(stage1_cache, "_iter_files", synthetic)
    result = cleanup_master_cache(
        root,
        policy=_policy(max_bytes=None),
        source_id="master-test",
        now=time.time(),
    )
    assert result.scanned == calls
    assert shared.exists()


def test_master_provider_maintenance_claims_only_configured_cache(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    provider = MasterCacheAssetProvider(
        source_id="master-test",
        cache_root=root,
        fetch_missing=False,
        cache_policy=_policy(max_bytes=0),
    )
    assert provider.maintain_cache().skipped_unsafe == 0
    assert (root / ".grandgousier-master-cache.json").is_file()
    provider.close()


def test_master_provider_retries_disk_guard_after_cache_eviction(monkeypatch, tmp_path: Path) -> None:
    root = _owned_cache(tmp_path)
    stale = root / "old.fit"
    stale.write_bytes(b"old")
    _old(stale, time.time())

    class FakeTask:
        def __init__(self, **values):
            self.__dict__.update(values)

    class FakeTransport:
        MasterFetchTask = FakeTask

        @staticmethod
        def master_remote_path_to_url(remote_path):
            return "https://example.invalid" + remote_path

        @staticmethod
        def download_master_task(task, **kwargs):
            del kwargs
            task.local_path.parent.mkdir(parents=True, exist_ok=True)
            task.local_path.write_bytes(b"downloaded")
            return SimpleNamespace(status="DOWNLOADED")

    def usage(_path):
        return type("Usage", (), {"free": 10 if stale.exists() else 100})()

    monkeypatch.setattr(stage1_cache.shutil, "disk_usage", usage)
    provider = MasterCacheAssetProvider(
        source_id="master-test",
        cache_root=root,
        disk_reserve_bytes=20,
        cache_policy=_policy(min_free_bytes=20),
    )
    provider._repository_transport = lambda: FakeTransport  # type: ignore[method-assign]
    provider._http_session = lambda: object()  # type: ignore[method-assign]
    materialized = provider.materialize(
        AssetRef(
            source_id="master-test",
            role="science_raw",
            backend="master-cache",
            locator="/master/raw/new.fit",
            metadata={"size_bytes": 1},
        )
    )
    assert materialized.path.read_bytes() == b"downloaded"
    assert not stale.exists()
    provider.close()
