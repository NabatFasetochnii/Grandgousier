from __future__ import annotations

from pathlib import Path
from typing import Any

from astropy.io import fits


def read_primary_header(path: str | Path) -> fits.Header:
    # memmap=False avoids keeping many open file handles during archive indexing.
    return fits.getheader(path, ext=0, memmap=False)


def header_to_jsonable(header: fits.Header) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for card in header.cards:
        key = str(card.keyword)
        if key in {"", "COMMENT", "HISTORY"}:
            continue
        value = card.value
        try:
            if hasattr(value, "item"):
                value = value.item()
            result[key] = value
        except Exception:
            result[key] = str(value)
    return result
