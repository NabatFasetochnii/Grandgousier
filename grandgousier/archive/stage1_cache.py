from __future__ import annotations

import heapq
import json
import os
import shutil
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CACHE_MARKER_NAME = ".grandgousier-master-cache.json"
CACHE_MARKER_SCHEMA = "grandgousier.stage1.master-cache.v1"
DOWNLOAD_PART_METADATA_SUFFIX = ".part.meta.json"


@dataclass(frozen=True, slots=True)
class Stage1CachePolicy:
    max_bytes: int | None = None
    min_free_bytes: int = 1 << 30
    retention_days: float = 30.0
    cleanup_grace_seconds: float = 3600.0
    cleanup_interval_seconds: float = 900.0
    cleanup_margin_bytes: int = 64 << 20
    candidate_limit: int = 4096

    def __post_init__(self) -> None:
        if self.max_bytes is not None and self.max_bytes < 0:
            raise ValueError("stage1_cache_max_bytes must be non-negative or null")
        if self.min_free_bytes < 0:
            raise ValueError("stage1_cache_min_free_bytes must be non-negative")
        if self.retention_days < 0:
            raise ValueError("stage1_cache_retention_days must be non-negative")
        if self.cleanup_grace_seconds < 0:
            raise ValueError("stage1_cache_cleanup_grace_seconds must be non-negative")
        if self.cleanup_interval_seconds < 0:
            raise ValueError("stage1_cache_cleanup_interval_seconds must be non-negative")
        if self.cleanup_margin_bytes < 0:
            raise ValueError("stage1_cache_cleanup_margin_bytes must be non-negative")
        if self.candidate_limit < 1:
            raise ValueError("stage1_cache_candidate_limit must be positive")


@dataclass(frozen=True, slots=True)
class Stage1CacheCleanupSummary:
    scanned: int = 0
    eligible: int = 0
    removed: int = 0
    bytes_freed: int = 0
    preserved_active: int = 0
    preserved_resumable: int = 0
    skipped_unsafe: int = 0
    failures: int = 0
    dry_run: bool = False

    def merge(self, other: "Stage1CacheCleanupSummary") -> "Stage1CacheCleanupSummary":
        return Stage1CacheCleanupSummary(
            scanned=self.scanned + other.scanned,
            eligible=self.eligible + other.eligible,
            removed=self.removed + other.removed,
            bytes_freed=self.bytes_freed + other.bytes_freed,
            preserved_active=self.preserved_active + other.preserved_active,
            preserved_resumable=self.preserved_resumable + other.preserved_resumable,
            skipped_unsafe=self.skipped_unsafe + other.skipped_unsafe,
            failures=self.failures + other.failures,
            dry_run=self.dry_run or other.dry_run,
        )


def normalize_cache_policy(
    *,
    max_bytes: int | str | None = None,
    min_free_bytes: int | str | None = None,
    retention_days: float | str | None = None,
    cleanup_grace_seconds: float | str | None = None,
    cleanup_interval_seconds: float | str | None = None,
    cleanup_margin_bytes: int | str | None = None,
    candidate_limit: int | str | None = None,
    default_min_free_bytes: int = 1 << 30,
) -> Stage1CachePolicy:
    return Stage1CachePolicy(
        max_bytes=None if max_bytes in {None, ""} else int(max_bytes),
        min_free_bytes=(
            int(default_min_free_bytes) if min_free_bytes in {None, ""} else int(min_free_bytes)
        ),
        retention_days=30.0 if retention_days in {None, ""} else float(retention_days),
        cleanup_grace_seconds=(
            3600.0 if cleanup_grace_seconds in {None, ""} else float(cleanup_grace_seconds)
        ),
        cleanup_interval_seconds=(
            900.0 if cleanup_interval_seconds in {None, ""} else float(cleanup_interval_seconds)
        ),
        cleanup_margin_bytes=(
            64 << 20 if cleanup_margin_bytes in {None, ""} else int(cleanup_margin_bytes)
        ),
        candidate_limit=4096 if candidate_limit in {None, ""} else int(candidate_limit),
    )


def _resolved_root(root: str | Path) -> Path | None:
    candidate = Path(root).expanduser()
    if candidate.exists() and _is_reparse_point(candidate):
        return None
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return None


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat_module.S_ISLNK(metadata.st_mode):
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _marker_path(root: Path) -> Path:
    return root / CACHE_MARKER_NAME


def claim_cache_root(root: str | Path, *, source_id: str) -> bool:
    """Claim a configured cache root, or validate an existing claim."""
    resolved = _resolved_root(root)
    if resolved is None:
        return False
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        marker = _marker_path(resolved)
        if _is_reparse_point(marker):
            return False
        if marker.exists():
            payload = json.loads(marker.read_text(encoding="utf-8"))
            return (
                payload.get("schema") == CACHE_MARKER_SCHEMA
                and payload.get("root") == str(resolved)
                and payload.get("source_id") == str(source_id)
            )
        marker_part = marker.with_name(marker.name + ".part")
        marker_part.write_text(
            json.dumps(
                {"schema": CACHE_MARKER_SCHEMA, "root": str(resolved), "source_id": str(source_id)},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(marker_part, marker)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def is_owned_cache_root(root: str | Path, *, source_id: str | None = None) -> bool:
    resolved = _resolved_root(root)
    if resolved is None:
        return False
    marker = _marker_path(resolved)
    if _is_reparse_point(marker) or not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return (
        payload.get("schema") == CACHE_MARKER_SCHEMA
        and payload.get("root") == str(resolved)
        and (source_id is None or payload.get("source_id") == str(source_id))
    )


def _iter_files(root: Path) -> Iterable[tuple[Path, os.stat_result]]:
    try:
        entries = os.scandir(root)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if _is_reparse_point(Path(entry.path)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from _iter_files(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path), entry.stat(follow_symlinks=False)
            except OSError:
                continue


def _download_part_metadata(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _valid_resumable_part(path: Path) -> bool:
    metadata = _download_part_metadata(path)
    if _is_reparse_point(metadata) or not metadata.is_file():
        return False
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return (
        payload.get("schema") == CACHE_MARKER_SCHEMA
        and bool(str(payload.get("remote_path") or "").strip())
        and size > 0
    )


def _candidate_push(heap: list[tuple[float, str, Path, int]], item: tuple[float, str, Path, int], limit: int) -> None:
    heapq.heappush(heap, (-item[0], item[1], item[2], item[3]))
    if len(heap) > limit:
        heapq.heappop(heap)


def cleanup_master_cache(
    root: str | Path,
    *,
    policy: Stage1CachePolicy,
    required_bytes: int = 0,
    active_paths: Iterable[str | Path] = (),
    source_id: str | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> Stage1CacheCleanupSummary:
    """Evict only old, reusable files from an explicitly claimed cache root."""
    resolved = _resolved_root(root)
    if resolved is None or not is_owned_cache_root(resolved, source_id=source_id):
        return Stage1CacheCleanupSummary(skipped_unsafe=1, dry_run=dry_run)
    active = {Path(path).expanduser().resolve(strict=False) for path in active_paths}
    timestamp = time.time() if now is None else float(now)
    cutoff = timestamp - max(policy.retention_days * 86400.0, policy.cleanup_grace_seconds)
    total_bytes = 0
    scanned = 0
    eligible = 0
    preserved_active = 0
    preserved_resumable = 0
    skipped_unsafe = 0
    heap: list[tuple[float, str, Path, int]] = []
    for path, stat in _iter_files(resolved):
        if path == _marker_path(resolved):
            continue
        if path.name.endswith(DOWNLOAD_PART_METADATA_SUFFIX):
            part_path = path.with_name(path.name[: -len(".meta.json")])
            if part_path.exists():
                continue
        scanned += 1
        total_bytes += int(stat.st_size)
        if path.suffix == ".part":
            if path.resolve(strict=False) in active:
                preserved_active += 1
            elif _valid_resumable_part(path):
                preserved_resumable += 1
            else:
                skipped_unsafe += 1
            continue
        if path.resolve(strict=False) in active:
            preserved_active += 1
            continue
        if stat.st_mtime > cutoff:
            continue
        eligible += 1
        _candidate_push(heap, (stat.st_mtime, str(path), path, int(stat.st_size)), policy.candidate_limit)
    try:
        free_bytes = int(shutil.disk_usage(resolved).free)
    except OSError:
        return Stage1CacheCleanupSummary(
            scanned=scanned,
            eligible=eligible,
            preserved_active=preserved_active,
            preserved_resumable=preserved_resumable,
            skipped_unsafe=skipped_unsafe + 1,
            failures=1,
            dry_run=dry_run,
        )
    quota_deficit = max(0, total_bytes - policy.max_bytes) if policy.max_bytes is not None else 0
    free_deficit = max(0, int(required_bytes) + policy.min_free_bytes - free_bytes)
    target = max(quota_deficit, free_deficit)
    if target > 0 and policy.cleanup_margin_bytes:
        target += policy.cleanup_margin_bytes
    removed = 0
    bytes_freed = 0
    failures = 0
    for _, _, path, size in sorted(heap, key=lambda item: (item[0] * -1, item[1])):
        if bytes_freed >= target:
            break
        if not _inside(resolved, path) or _is_reparse_point(path) or not path.is_file():
            skipped_unsafe += 1
            continue
        try:
            if not dry_run:
                path.unlink()
            removed += 1
            bytes_freed += size
        except OSError:
            failures += 1
            skipped_unsafe += 1
            continue
    return Stage1CacheCleanupSummary(
        scanned=scanned,
        eligible=eligible,
        removed=removed,
        bytes_freed=bytes_freed,
        preserved_active=preserved_active,
        preserved_resumable=preserved_resumable,
        skipped_unsafe=skipped_unsafe,
        failures=failures,
        dry_run=dry_run,
    )


def cleanup_atomic_output_parts(
    root: str | Path,
    *,
    grace_seconds: float = 3600.0,
    active_paths: Iterable[str | Path] = (),
    dry_run: bool = False,
    now: float | None = None,
) -> Stage1CacheCleanupSummary:
    """Clean only stale atomic output parts below an explicit ProductStore root."""
    resolved = _resolved_root(root)
    if resolved is None or not resolved.is_dir():
        return Stage1CacheCleanupSummary(skipped_unsafe=1, dry_run=dry_run)
    active = {Path(path).expanduser().resolve(strict=False) for path in active_paths}
    cutoff = (time.time() if now is None else float(now)) - max(0.0, float(grace_seconds))
    scanned = eligible = removed = bytes_freed = preserved_active = skipped_unsafe = failures = 0
    for path, stat in _iter_files(resolved):
        is_product_part = (
            path.name.endswith((".fits.part", ".fit.part", ".fit.gz.part"))
            or (".fits." in path.name and path.name.endswith(".part"))
        )
        if not is_product_part or path.name.endswith(DOWNLOAD_PART_METADATA_SUFFIX):
            continue
        scanned += 1
        if path.resolve(strict=False) in active or stat.st_mtime > cutoff:
            preserved_active += 1
            continue
        if not _inside(resolved, path) or _is_reparse_point(path):
            skipped_unsafe += 1
            continue
        try:
            if not dry_run:
                path.unlink()
            eligible += 1
            removed += 1
            bytes_freed += int(stat.st_size)
        except OSError:
            failures += 1
    return Stage1CacheCleanupSummary(
        scanned=scanned,
        eligible=eligible,
        removed=removed,
        bytes_freed=bytes_freed,
        preserved_active=preserved_active,
        skipped_unsafe=skipped_unsafe,
        failures=failures,
        dry_run=dry_run,
    )


__all__ = [
    "CACHE_MARKER_NAME",
    "CACHE_MARKER_SCHEMA",
    "DOWNLOAD_PART_METADATA_SUFFIX",
    "Stage1CacheCleanupSummary",
    "Stage1CachePolicy",
    "claim_cache_root",
    "cleanup_atomic_output_parts",
    "cleanup_master_cache",
    "is_owned_cache_root",
    "normalize_cache_policy",
]
