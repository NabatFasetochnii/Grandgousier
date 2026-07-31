from __future__ import annotations

import hashlib
from pathlib import Path


def make_frame_uid(path: str | Path, file_size: int | None, mtime_ns: int | None) -> str:
    p = Path(path)
    payload = f"{p.as_posix()}|{file_size}|{mtime_ns}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
