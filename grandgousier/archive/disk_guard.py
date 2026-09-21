from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_STAGE1_DISK_RESERVE_BYTES = 1 << 30
DEFAULT_UNKNOWN_ASSET_BYTES = 256 << 20
DEFAULT_UNKNOWN_PRODUCT_BYTES = 256 << 20
STAGE1_METADATA_OVERHEAD_BYTES = 64 << 10


@dataclass(frozen=True, slots=True)
class DiskRequirement:
    target: Path
    required_bytes: int
    label: str


@dataclass(frozen=True, slots=True)
class DiskDiagnostic:
    filesystem: str
    target: str
    required_bytes: int
    free_bytes: int
    reserve_bytes: int
    deficit_bytes: int


class DiskSpaceGuardError(RuntimeError):
    def __init__(self, diagnostics: tuple[DiskDiagnostic, ...], *, prefix: str | None = None) -> None:
        self.diagnostics = diagnostics
        details = "; ".join(
            f"{item.target}: "
            f"required={item.required_bytes}, free={item.free_bytes}, "
            f"reserve={item.reserve_bytes}, deficit={item.deficit_bytes}"
            for item in diagnostics
        )
        super().__init__(f"{prefix or 'Stage-1 disk preflight failed'}: {details}")


def normalize_disk_reserve(value: int | str | None) -> int:
    if value is None:
        return DEFAULT_STAGE1_DISK_RESERVE_BYTES
    reserve = int(value)
    if reserve < 0:
        raise ValueError("stage1_disk_reserve_bytes must be non-negative")
    return reserve


def estimate_size(value: object, *, fallback: int) -> int:
    try:
        size = int(value) if value not in {None, ""} else 0
    except (TypeError, ValueError):
        size = 0
    return size if size > 0 else int(fallback)


def _existing_parent(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.exists():
        probe = candidate if candidate.is_dir() else candidate.parent
    else:
        probe = candidate.parent
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            raise OSError(f"Cannot determine existing filesystem parent for {path}")
        probe = parent
    return probe.resolve()


def _filesystem_for(path: Path) -> tuple[int, Path]:
    parent = _existing_parent(path)
    return int(os.stat(parent).st_dev), parent


def check_disk_budget(
    requirements: Iterable[DiskRequirement],
    *,
    reserve_bytes: int | str | None = None,
) -> tuple[DiskDiagnostic, ...]:
    reserve = normalize_disk_reserve(reserve_bytes)
    grouped: dict[int, tuple[Path, list[DiskRequirement]]] = {}
    for requirement in requirements:
        required = int(requirement.required_bytes)
        if required < 0:
            raise ValueError("disk requirement must be non-negative")
        try:
            device, filesystem = _filesystem_for(requirement.target)
        except OSError as exc:
            raise DiskSpaceGuardError(
                (
                    DiskDiagnostic(
                        filesystem="<unknown>",
                        target=str(requirement.target),
                        required_bytes=required,
                        free_bytes=0,
                        reserve_bytes=reserve,
                        deficit_bytes=required + reserve,
                    ),
                ),
                prefix="Stage-1 disk preflight could not determine target filesystem",
            ) from exc
        if device not in grouped:
            grouped[device] = (filesystem, [])
        grouped[device][1].append(
            DiskRequirement(requirement.target, required, requirement.label)
        )

    diagnostics: list[DiskDiagnostic] = []
    successful: list[DiskDiagnostic] = []
    for filesystem, items in grouped.values():
        try:
            free_bytes = int(shutil.disk_usage(filesystem).free)
        except OSError as exc:
            required_bytes = sum(item.required_bytes for item in items)
            raise DiskSpaceGuardError(
                (
                    DiskDiagnostic(
                        filesystem=str(filesystem),
                        target=str(filesystem),
                        required_bytes=required_bytes,
                        free_bytes=0,
                        reserve_bytes=reserve,
                        deficit_bytes=required_bytes + reserve,
                    ),
                ),
                prefix=f"Stage-1 disk preflight could not inspect filesystem {filesystem}",
            ) from exc
        required_bytes = sum(item.required_bytes for item in items)
        deficit = max(0, required_bytes + reserve - free_bytes)
        if deficit:
            diagnostics.append(
                DiskDiagnostic(
                    filesystem=str(filesystem),
                    target=", ".join(str(item.target) for item in items),
                    required_bytes=required_bytes,
                    free_bytes=free_bytes,
                    reserve_bytes=reserve,
                    deficit_bytes=deficit,
                )
            )
        else:
            successful.append(
                DiskDiagnostic(
                    filesystem=str(filesystem),
                    target=", ".join(str(item.target) for item in items),
                    required_bytes=required_bytes,
                    free_bytes=free_bytes,
                    reserve_bytes=reserve,
                    deficit_bytes=0,
                )
            )
    if diagnostics:
        raise DiskSpaceGuardError(tuple(diagnostics))
    return tuple(successful)


__all__ = [
    "DEFAULT_STAGE1_DISK_RESERVE_BYTES",
    "DEFAULT_UNKNOWN_ASSET_BYTES",
    "DEFAULT_UNKNOWN_PRODUCT_BYTES",
    "DiskDiagnostic",
    "DiskRequirement",
    "DiskSpaceGuardError",
    "STAGE1_METADATA_OVERHEAD_BYTES",
    "check_disk_budget",
    "estimate_size",
    "normalize_disk_reserve",
]
