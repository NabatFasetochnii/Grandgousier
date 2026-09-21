from __future__ import annotations

import csv
import re
from pathlib import Path, PurePosixPath
from typing import Iterator


def read_archive_manifest_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a prepared archive manifest without assuming a remote transport."""
    return list(iter_archive_manifest_csv(path))


def iter_archive_manifest_csv(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield prepared archive manifest rows without retaining the full manifest."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def manifest_path_to_local(path_value: str, root: str | Path) -> Path:
    """Map a manifest-relative POSIX-style path into a local cache root safely."""
    text = str(path_value).strip().replace("\\", "/")
    if not text:
        raise ValueError("Manifest path is empty")
    pure = PurePosixPath(text)
    parts = tuple(part for part in pure.parts if part not in ("/", ""))
    if any(part == ".." for part in parts):
        raise ValueError(f"Unsafe manifest path: {path_value!r}")
    if not parts:
        raise ValueError(f"Unsupported manifest path: {path_value!r}")
    return Path(root).joinpath(*parts)


def extract_numeric_frame_id(path_value: str | None) -> int | None:
    """Extract a leading numeric frame identifier from a FITS-like filename."""
    if not path_value:
        return None
    name = PurePosixPath(str(path_value).replace("\\", "/")).name
    match = re.match(r"^(\d+)(?:_header)?(?:\.fits?|\.fts)?(?:\.gz)?$", name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def infer_date_parts(path_value: str) -> tuple[str, str, str] | None:
    """Return the first YYYY/MM/DD component triple found in a manifest path."""
    pure = PurePosixPath(str(path_value).strip().replace("\\", "/"))
    parts = [part for part in pure.parts if part not in ("/", "")]
    for index in range(0, max(0, len(parts) - 2)):
        year, month, day = parts[index : index + 3]
        if (
            re.fullmatch(r"\d{4}", year)
            and re.fullmatch(r"\d{2}", month)
            and re.fullmatch(r"\d{2}", day)
        ):
            yi, mi, di = int(year), int(month), int(day)
            if 1900 <= yi <= 2200 and 1 <= mi <= 12 and 1 <= di <= 31:
                return year, month, day
    return None
