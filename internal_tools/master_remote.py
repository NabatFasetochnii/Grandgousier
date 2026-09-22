from __future__ import annotations

import csv
import getpass
import hashlib
import json
import math
import os
import re
import time
from html import unescape
from html.parser import HTMLParser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_MASTER_HTTP_BASE_URL = "https://master.kourovka.ru"
MASTER_HTTP_COORD_SEARCH_HALF_SIZE_DEG = 0.95
MASTER_DB_ENV = "GRANDGOUSIER_MASTER_DATABASE_URL"
MASTER_HTTP_USER_ENV = "GRANDGOUSIER_MASTER_HTTP_USER"
MASTER_HTTP_PASSWORD_ENV = "GRANDGOUSIER_MASTER_HTTP_PASSWORD"
MASTER_HTTP_BASE_URL_ENV = "GRANDGOUSIER_MASTER_HTTP_BASE_URL"
MASTER_CACHE_MARKER_SCHEMA = "grandgousier.stage1.master-cache.v1"

INVENTORY_COLUMNS = [
    "processed_frame_id",
    "datetime",
    "night_utc",
    "coord2000",
    "filter",
    "exptime",
    "fwhm",
    "ell",
    "zpoint",
    "background",
    "raw_path",
    "header_path",
    "dark_path",
    "flat_path",
    "raw_id",
    "dark_id",
    "flat_id",
    "raw_url",
    "header_url",
    "dark_url",
    "flat_url",
]

FETCH_REPORT_COLUMNS = [
    "remote_path",
    "url",
    "local_path",
    "roles",
    "status",
    "size_bytes",
    "sha256",
    "error",
]


class MasterRemoteCredentialError(RuntimeError):
    """Raised when remote MASTER fetch credentials are not configured."""


class MasterTransportError(RuntimeError):
    """Raised when an asset response cannot be safely resumed or promoted."""


@dataclass(frozen=True)
class TransportProgress:
    """Current bounded state for one MASTER asset transfer."""

    remote_path: str
    expected_bytes: int | None
    existing_offset: int
    downloaded_this_attempt: int
    total_downloaded: int
    elapsed: float
    recent_throughput: float | None
    attempt: int
    last_progress_monotonic: float
    event: str
    reason: str | None = None


@dataclass(frozen=True)
class MasterFetchTask:
    remote_path: str
    url: str
    local_path: Path
    roles: tuple[str, ...]


@dataclass(frozen=True)
class MasterDownloadResult:
    remote_path: str
    url: str
    local_path: Path
    roles: tuple[str, ...]
    status: str
    size_bytes: int | None = None
    sha256: str | None = None
    error: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "remote_path": self.remote_path,
            "url": self.url,
            "local_path": str(self.local_path),
            "roles": ",".join(self.roles),
            "status": self.status,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "error": self.error,
        }


def normalize_master_database_url(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise ValueError("MASTER database URL is empty")
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if value.startswith(prefix):
            return "postgresql://" + value[len(prefix) :]
    return value


def get_master_database_url(explicit: str | None = None, *, required: bool = True) -> str | None:
    value = explicit or os.getenv(MASTER_DB_ENV)
    if not value:
        if required:
            raise RuntimeError(
                f"MASTER database URL is required; pass --master-database-url or set {MASTER_DB_ENV}"
            )
        return None
    return normalize_master_database_url(value)


def get_master_http_base_url(explicit: str | None = None) -> str:
    return (explicit or os.getenv(MASTER_HTTP_BASE_URL_ENV) or DEFAULT_MASTER_HTTP_BASE_URL).rstrip("/")


def resolve_master_http_credentials(
    *,
    user: str | None = None,
    password: str | None = None,
    prompt_password: bool = False,
) -> tuple[str, str]:
    resolved_user = _configured_credential(user) or _configured_credential(
        os.getenv(MASTER_HTTP_USER_ENV)
    )
    resolved_password = _configured_credential(password) or _configured_credential(
        os.getenv(MASTER_HTTP_PASSWORD_ENV)
    )
    if not resolved_user:
        raise MasterRemoteCredentialError(
            "MASTER HTTP credentials are required to fetch missing assets. "
            f"Configure {MASTER_HTTP_USER_ENV} and {MASTER_HTTP_PASSWORD_ENV}."
        )
    if not resolved_password and prompt_password:
        resolved_password = getpass.getpass(f"MASTER HTTP password for {resolved_user}: ")
        resolved_password = _configured_credential(resolved_password)
    if not resolved_password:
        raise MasterRemoteCredentialError(
            "MASTER HTTP password is required to fetch missing assets. "
            f"Configure {MASTER_HTTP_PASSWORD_ENV}."
        )
    return resolved_user, resolved_password


def _configured_credential(value: str | None) -> str | None:
    """Treat missing and whitespace-only runtime credentials as unset."""
    if value is None or not value.strip():
        return None
    return value


def master_remote_path_to_url(remote_path: str | None, *, base_url: str | None = None) -> str | None:
    if remote_path is None:
        return None
    path = str(remote_path).strip()
    if not path:
        return None
    parsed = urlparse(path)
    if parsed.scheme in {"http", "https"}:
        return path
    if path.startswith("master/"):
        path = "/" + path
    if not path.startswith("/master/"):
        return None
    return f"{get_master_http_base_url(base_url)}/data{path}"


def master_remote_path_to_local(remote_path: str, root: str | Path) -> Path:
    path = str(remote_path).strip().replace("\\", "/")
    if path.startswith("master/"):
        path = "/" + path
    if not path.startswith("/master/"):
        raise ValueError(f"Unsupported MASTER remote path: {remote_path!r}")
    pure = PurePosixPath(path)
    if ".." in pure.parts:
        raise ValueError(f"Unsafe MASTER remote path: {remote_path!r}")
    return Path(root).joinpath(*pure.parts[1:])


def extract_master_raw_id(remote_path: str | None) -> int | None:
    if not remote_path:
        return None
    name = PurePosixPath(str(remote_path).replace("\\", "/")).name
    match = re.match(r"^(\d+)(?:_header)?(?:\.fits?|\.fts)?(?:\.gz)?$", name, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _build_sbox_strings(ra_deg: float, dec_deg: float, radius_deg: float) -> list[str]:
    if not math.isfinite(ra_deg) or not 0.0 <= ra_deg < 360.0:
        raise ValueError("RA must be finite and in [0, 360) degrees")
    if not math.isfinite(dec_deg) or not -90.0 <= dec_deg <= 90.0:
        raise ValueError("Dec must be finite and in [-90, 90] degrees")
    if not math.isfinite(radius_deg) or radius_deg <= 0.0 or radius_deg > 20.0:
        raise ValueError("radius_deg must be finite and in (0, 20]")

    dec_min = max(-90.0, dec_deg - radius_deg)
    dec_max = min(90.0, dec_deg + radius_deg)
    cos_dec = abs(math.cos(math.radians(dec_deg)))
    if cos_dec < 1.0e-4:
        ra_half = 180.0
    else:
        ra_half = min(180.0, radius_deg / cos_dec)

    def make_box(ra_min: float, ra_max: float) -> str:
        return f"(({ra_min:.12f} d, {dec_min:.12f} d), ({ra_max:.12f} d, {dec_max:.12f} d))"

    if ra_half >= 180.0:
        return [make_box(0.0, 359.999999999999)]

    ra_min = ra_deg - ra_half
    ra_max = ra_deg + ra_half
    if ra_min < 0.0:
        return [make_box(ra_min + 360.0, 359.999999999999), make_box(0.0, ra_max)]
    if ra_max >= 360.0:
        return [make_box(ra_min, 359.999999999999), make_box(0.0, ra_max - 360.0)]
    return [make_box(ra_min, ra_max)]


def _build_master_search_sql(
    *,
    n_boxes: int,
    include_start: bool,
    include_end: bool,
    include_filters: bool,
    include_limit: bool,
) -> str:
    box_predicate = " OR ".join(
        f"processed_frames.coord2000 @ %(box_{idx})s::sbox" for idx in range(n_boxes)
    )
    where = [f"({box_predicate})", "processed_frames.datetime IS NOT NULL"]
    if include_start:
        where.append("processed_frames.datetime >= %(datetime_start)s")
    if include_end:
        where.append("processed_frames.datetime < %(datetime_end)s")
    if include_filters:
        where.append("processed_frames.filter = ANY(%(filters)s)")

    limit_sql = "\nLIMIT %(limit)s" if include_limit else ""
    return f"""
SELECT
    processed_frames.id AS processed_frame_id,
    to_char(processed_frames.datetime, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS datetime,
    processed_frames.coord2000::text AS coord2000,
    processed_frames.filter AS filter,
    processed_frames.exptime AS exptime,
    processed_frames.sexfwhm AS fwhm,
    processed_frames.sexell AS ell,
    processed_frames.sexzeropoint AS zpoint,
    processed_frames.sexbackground AS background,
    processed_frames.raw_path::text AS raw_path,
    processed_frames.header_path::text AS header_path,
    processed_frames.proc_dark::text AS dark_path,
    processed_frames.proc_flat::text AS flat_path
FROM processed_frames
WHERE {' AND '.join(where)}
ORDER BY processed_frames.datetime ASC, processed_frames.id ASC{limit_sql};
""".strip()


def _night_utc_from_datetime(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return str(value)[:10] if len(str(value)) >= 10 else str(value)
    return (dt - timedelta(hours=12)).date().isoformat()


def _normalize_inventory_row(row: dict[str, object], *, base_url: str | None = None) -> dict[str, object]:
    out = dict(row)
    out["night_utc"] = _night_utc_from_datetime(out.get("datetime"))
    for role, key in (
        ("raw", "raw_path"),
        ("header", "header_path"),
        ("dark", "dark_path"),
        ("flat", "flat_path"),
    ):
        path_value = out.get(key)
        path_text = None if path_value is None else str(path_value)
        derived_id = extract_master_raw_id(path_text)
        if derived_id is not None:
            out[f"{role}_id"] = derived_id
        else:
            out[f"{role}_id"] = out.get(f"{role}_id")
        out[f"{role}_url"] = master_remote_path_to_url(path_text, base_url=base_url)
    return {column: out.get(column) for column in INVENTORY_COLUMNS}


def search_master_archive(
    *,
    database_url: str,
    ra_deg: float,
    dec_deg: float,
    radius_deg: float = 0.8,
    datetime_start: str | None = None,
    datetime_end: str | None = None,
    filters: Sequence[str] | None = None,
    limit: int | None = None,
    base_url: str | None = None,
    connect_fn: Callable[..., object] | None = None,
) -> list[dict[str, object]]:
    boxes = _build_sbox_strings(float(ra_deg), float(dec_deg), float(radius_deg))
    filter_values = [str(value).strip() for value in (filters or []) if str(value).strip()]
    if limit is not None and int(limit) <= 0:
        raise ValueError("limit must be positive")

    params: dict[str, object] = {f"box_{idx}": value for idx, value in enumerate(boxes)}
    if datetime_start:
        params["datetime_start"] = datetime_start
    if datetime_end:
        params["datetime_end"] = datetime_end
    if filter_values:
        params["filters"] = filter_values
    if limit is not None:
        params["limit"] = int(limit)

    sql = _build_master_search_sql(
        n_boxes=len(boxes),
        include_start=bool(datetime_start),
        include_end=bool(datetime_end),
        include_filters=bool(filter_values),
        include_limit=limit is not None,
    )

    if connect_fn is None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - core project dependency
            raise RuntimeError("MASTER archive search requires psycopg>=3.1") from exc
        connect_fn = psycopg.connect
        row_factory = dict_row
    else:
        row_factory = None

    with connect_fn(normalize_master_database_url(database_url)) as conn:
        cursor_kwargs = {"row_factory": row_factory} if row_factory is not None else {}
        with conn.cursor(**cursor_kwargs) as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    return [_normalize_inventory_row(dict(row), base_url=base_url) for row in rows]



class _MasterProcessingHtmlParser(HTMLParser):
    """Tolerant parser for MASTER processing.php table rows.

    Production MASTER has historically used ``<tr class="n">`` for result rows,
    but that CSS marker is presentation-only and is not a stable data contract.
    Collect every table row and let :func:`parse_master_processing_html` identify
    science rows by their raw-frame path signature.  A small row/cell stack keeps
    nested search-form tables from corrupting the outer row.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, object]]] = []
        self._row_stack: list[list[dict[str, object]]] = []
        self._cell_stack: list[tuple[list[dict[str, object]], dict[str, object]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "tr":
            self._row_stack.append([])
            return
        if not self._row_stack:
            return
        if tag == "td":
            cell = {"text": [], "links": []}
            self._cell_stack.append((self._row_stack[-1], cell))
            return
        if tag == "a" and self._cell_stack:
            links = self._cell_stack[-1][1]["links"]
            assert isinstance(links, list)
            links.append(
                {
                    "href": str(attrs_dict.get("href", "")),
                    "title": str(attrs_dict.get("title", "")),
                }
            )

    def handle_data(self, data: str) -> None:
        if not self._cell_stack:
            return
        text = self._cell_stack[-1][1]["text"]
        assert isinstance(text, list)
        text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "td" and self._cell_stack:
            owner_row, cell = self._cell_stack.pop()
            text = cell["text"]
            assert isinstance(text, list)
            cell["text"] = " ".join("".join(text).split())
            owner_row.append(cell)
            return
        if tag == "tr" and self._row_stack:
            row = self._row_stack.pop()
            if row:
                self.rows.append(row)


def _float_or_none(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_or_none(value: object) -> int | None:
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


_MASTER_RAW_PATH_RE = re.compile(
    r"(/master/imdata/rawdata/[^\s<>\"']+?\.(?:fits?|fts)(?:\.gz)?)",
    re.IGNORECASE,
)
_MASTER_HEADER_PATH_RE = re.compile(
    r"(/master/imdata/procdata/[^\s<>\"']+?_header\.(?:fits?|fts)(?:\.gz)?)",
    re.IGNORECASE,
)


def _cell_links(cell: dict[str, object]) -> list[dict[str, object]]:
    links = cell.get("links") or []
    return links if isinstance(links, list) else []


def _extract_master_path_from_fragments(
    fragments: Iterable[object],
    pattern: re.Pattern[str],
) -> str | None:
    for fragment in fragments:
        text = unquote(unescape(str(fragment or "")))
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _raw_path_from_cell(cell: dict[str, object]) -> str | None:
    fragments: list[object] = [cell.get("text")]
    for link in _cell_links(cell):
        fragments.extend((link.get("href"), link.get("title")))
    return _extract_master_path_from_fragments(fragments, _MASTER_RAW_PATH_RE)


def _header_path_from_links(links: Sequence[dict[str, object]]) -> str | None:
    for link in links:
        href = unescape(str(link.get("href") or ""))
        if "Show_Header.php" in href or "header=" in href:
            values = parse_qs(urlparse(href).query).get("header")
            if values:
                value = unquote(values[0]).strip()
                if value.startswith("/master/"):
                    return value
        direct = _extract_master_path_from_fragments((href,), _MASTER_HEADER_PATH_RE)
        if direct:
            return direct
    return None


def _header_path_from_cells(cells: Sequence[dict[str, object]]) -> str | None:
    for cell in cells:
        path = _header_path_from_links(_cell_links(cell))
        if path:
            return path
        direct = _extract_master_path_from_fragments((cell.get("text"),), _MASTER_HEADER_PATH_RE)
        if direct:
            return direct
    return None


def infer_master_header_path(raw_path: str | None) -> str | None:
    """Infer the canonical processed-header path from a canonical MASTER raw path.

    Production processing.php may omit the Header action link even though the
    standard ``procdata/YYYY/MM/DD/<raw_id>_header.fits`` file exists.  Keep
    this fallback deliberately strict so unrelated or noncanonical paths are
    never rewritten heuristically.
    """

    if not raw_path:
        return None
    path = unquote(unescape(str(raw_path).strip())).replace("\\", "/")
    match = re.fullmatch(
        r"/master/imdata/rawdata/(\d{4})/(\d{2})/(\d{2})/(\d+)\.(?:fits?|fts)(?:\.gz)?",
        path,
        re.IGNORECASE,
    )
    if not match:
        return None
    year, month, day, raw_id = match.groups()
    return f"/master/imdata/procdata/{year}/{month}/{day}/{raw_id}_header.fits"


def parse_master_processing_html(html_text: str) -> list[dict[str, object]]:
    parser = _MasterProcessingHtmlParser()
    parser.feed(html_text)
    result: list[dict[str, object]] = []
    for cells in parser.rows:
        # Stable columns before Raw: id, datetime, coord, filter, exptime, FWHM,
        # ellipticity, zeropoint, background.  The presentation-only Ext column
        # and CSS class have changed between MASTER deployments, so locate Raw by
        # its path signature instead of hard-coding tr.class or column 10.
        if len(cells) < 12:
            continue
        raw_index = None
        raw_path = None
        late_raw_candidates: list[tuple[int, str]] = []
        for idx, cell in enumerate(cells):
            candidate = _raw_path_from_cell(cell)
            if not candidate:
                continue
            if idx >= 9 and idx + 2 < len(cells):
                raw_index = idx
                raw_path = candidate
                break
            late_raw_candidates.append((idx, candidate))

        # A production row may display only ``380242.fit.gz`` in the Raw cell
        # while the canonical /data/master/... path exists only in the trailing
        # action links.  Match that path back to its basename cell.
        if raw_index is None:
            for action_idx, candidate in late_raw_candidates:
                candidate_id = extract_master_raw_id(candidate)
                if candidate_id is None:
                    continue
                for idx in range(9, min(action_idx, len(cells) - 2)):
                    if extract_master_raw_id(str(cells[idx].get("text") or "")) == candidate_id:
                        raw_index = idx
                        raw_path = candidate
                        break
                if raw_index is not None:
                    break

        if raw_index is None or raw_index < 9 or raw_index + 2 >= len(cells):
            continue

        processed_frame_id = _int_or_none(cells[0].get("text"))
        if processed_frame_id is None:
            continue
        dark_id = _int_or_none(cells[raw_index + 1].get("text"))
        flat_id = _int_or_none(cells[raw_index + 2].get("text"))
        header_path = _header_path_from_cells(cells[raw_index + 3 :])
        if header_path is None:
            header_path = infer_master_header_path(raw_path)
        result.append(
            {
                "processed_frame_id": processed_frame_id,
                "datetime": str(cells[1].get("text") or "").strip(),
                "coord2000": str(cells[2].get("text") or "").strip(),
                "filter": str(cells[3].get("text") or "").strip(),
                "exptime": _float_or_none(cells[4].get("text")),
                "fwhm": _float_or_none(cells[5].get("text")),
                "ell": _float_or_none(cells[6].get("text")),
                "zpoint": _float_or_none(cells[7].get("text")),
                "background": _float_or_none(cells[8].get("text")),
                "raw_path": raw_path,
                "header_path": header_path,
                "dark_path": None,
                "flat_path": None,
                "dark_id": dark_id,
                "flat_id": flat_id,
            }
        )
    return result


def _parse_master_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid MASTER datetime bound: {value!r}") from exc


def _validate_master_datetime_sql(value: str) -> str:
    return _parse_master_datetime(value).strftime("%Y-%m-%d %H:%M:%S")


def _master_coord2000_to_degrees(value: object) -> tuple[float, float] | None:
    """Parse the coordinate strings emitted by MASTER processing.php."""

    text = " ".join(str(value or "").strip().split())
    if not text:
        return None

    decimal_match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
        text,
    )
    if decimal_match:
        ra_deg = float(decimal_match.group(1)) % 360.0
        dec_deg = float(decimal_match.group(2))
        if -90.0 <= dec_deg <= 90.0:
            return ra_deg, dec_deg
        return None

    hms_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)h\s*(\d+(?:\.\d+)?)m\s*(\d+(?:\.\d+)?)s\s*"
        r"([+-]?\d+(?:\.\d+)?)d\s*(\d+(?:\.\d+)?)m\s*(\d+(?:\.\d+)?)s",
        text,
        re.IGNORECASE,
    )
    if not hms_match:
        return None

    ra_h = float(hms_match.group(1))
    ra_m = float(hms_match.group(2))
    ra_s = float(hms_match.group(3))
    dec_d_text = hms_match.group(4)
    dec_m = float(hms_match.group(5))
    dec_s = float(hms_match.group(6))
    ra_deg = (15.0 * (ra_h + ra_m / 60.0 + ra_s / 3600.0)) % 360.0
    dec_sign = -1.0 if dec_d_text.startswith("-") else 1.0
    dec_deg = dec_sign * (abs(float(dec_d_text)) + dec_m / 60.0 + dec_s / 3600.0)
    if not -90.0 <= dec_deg <= 90.0:
        return None
    return ra_deg, dec_deg


def _master_row_matches_http_request(
    row: dict[str, object],
    *,
    ra_deg: float,
    dec_deg: float,
    radius_deg: float,
    datetime_start: str | None,
    datetime_end: str | None,
    filters: Sequence[str] | None,
) -> bool:
    coord = _master_coord2000_to_degrees(row.get("coord2000"))
    if coord is None:
        return False
    row_ra, row_dec = coord
    if abs(row_dec - float(dec_deg)) > float(radius_deg) + 1.0e-12:
        return False
    cos_dec = abs(math.cos(math.radians(float(dec_deg))))
    ra_half = 180.0 if cos_dec < 1.0e-4 else min(180.0, float(radius_deg) / cos_dec)
    delta_ra = abs(((row_ra - float(ra_deg) + 180.0) % 360.0) - 180.0)
    if delta_ra > ra_half + 1.0e-12:
        return False

    filter_values = {str(value).strip() for value in (filters or []) if str(value).strip()}
    if filter_values and str(row.get("filter") or "").strip() not in filter_values:
        return False

    row_datetime_text = str(row.get("datetime") or "").strip()
    if datetime_start or datetime_end:
        if not row_datetime_text:
            return False
        try:
            row_datetime = _parse_master_datetime(row_datetime_text)
        except ValueError:
            return False
        if datetime_start and row_datetime < _parse_master_datetime(datetime_start):
            return False
        if datetime_end and row_datetime >= _parse_master_datetime(datetime_end):
            return False
    return True


def _master_processing_has_next_page(html_text: str, current_page: int) -> bool:
    next_page = int(current_page) + 1
    text = unescape(html_text)
    return bool(re.search(rf"processing\.php\?page={next_page}(?:&|\")", text, re.IGNORECASE))


def _extract_raw_path_for_id(html_text: str, raw_id: int) -> str | None:
    text = unquote(unescape(html_text))
    pattern = re.compile(
        rf"(/master/imdata/rawdata/\d{{4}}/\d{{2}}/\d{{2}}/{int(raw_id)}\.(?:fits?|fts)(?:\.gz)?)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def resolve_master_raw_path_http(
    *,
    session,
    raw_id: int,
    base_url: str | None = None,
    timeout_connect: float = 15.0,
    timeout_read: float = 180.0,
) -> str | None:
    url = f"{get_master_http_base_url(base_url)}/master2/index.php"
    response = session.get(
        url,
        params={"where_long": f"id = {int(raw_id)}"},
        timeout=(float(timeout_connect), float(timeout_read)),
    )
    if response.status_code == 401:
        raise RuntimeError("HTTP 401 Unauthorized while resolving MASTER calibration frame")
    response.raise_for_status()
    return _extract_raw_path_for_id(response.text, int(raw_id))


def search_master_archive_http(
    *,
    session,
    ra_deg: float,
    dec_deg: float,
    radius_deg: float = 0.8,
    datetime_start: str | None = None,
    datetime_end: str | None = None,
    filters: Sequence[str] | None = None,
    limit: int | None = None,
    base_url: str | None = None,
    page_size: int = 500,
    timeout_connect: float = 15.0,
    timeout_read: float = 180.0,
) -> list[dict[str, object]]:
    if limit is not None and int(limit) <= 0:
        raise ValueError("limit must be positive")
    page_size = int(page_size)
    if page_size <= 0 or page_size > 5000:
        raise ValueError("page_size must be in [1, 5000]")
    if float(radius_deg) > MASTER_HTTP_COORD_SEARCH_HALF_SIZE_DEG:
        raise ValueError(
            "MASTER HTTPS discovery currently supports radius_deg <= "
            f"{MASTER_HTTP_COORD_SEARCH_HALF_SIZE_DEG:.2f}; use PostgreSQL transport for wider searches"
        )
    # Validate the same coordinate/radius domain as the PostgreSQL transport.
    _build_sbox_strings(float(ra_deg), float(dec_deg), float(radius_deg))
    if datetime_start:
        _parse_master_datetime(datetime_start)
    if datetime_end:
        _parse_master_datetime(datetime_end)

    # The real MASTER processing.php UI exposes coord2000 as its supported spatial-search
    # field.  Passing a raw pgSphere predicate through where_long returns an empty result
    # on the production site even though the same position has rows.  Query the official
    # coord2000 field, whose server-side box is +/-0.95 deg, then apply the requested
    # (normally 0.8 deg) box and optional date/filter constraints locally.
    request_page_size = page_size
    coord_query = f"{float(ra_deg):.8f} {float(dec_deg):.8f}"
    url = f"{get_master_http_base_url(base_url)}/master2/processing.php"
    rows_by_id: dict[int, dict[str, object]] = {}
    page = 0
    while True:
        response = session.get(
            url,
            params={
                "page": page,
                "coord2000": coord_query,
                "filter": "any",
                "order_by": "processed_frames.datetime",
                "maxrec": request_page_size,
                "subm": "Search",
            },
            timeout=(float(timeout_connect), float(timeout_read)),
        )
        if response.status_code == 401:
            raise RuntimeError("HTTP 401 Unauthorized while searching MASTER archive")
        response.raise_for_status()
        page_rows = parse_master_processing_html(response.text)
        if not page_rows:
            if "/master/imdata/rawdata/" in unquote(unescape(response.text)):
                raise RuntimeError(
                    "MASTER processing.php returned frame paths but no rows could be parsed; "
                    "the HTML layout may have changed"
                )
            break
        for row in page_rows:
            if not _master_row_matches_http_request(
                row,
                ra_deg=float(ra_deg),
                dec_deg=float(dec_deg),
                radius_deg=float(radius_deg),
                datetime_start=datetime_start,
                datetime_end=datetime_end,
                filters=filters,
            ):
                continue
            processed_frame_id = int(row["processed_frame_id"])
            rows_by_id[processed_frame_id] = row
            if limit is not None and len(rows_by_id) >= int(limit):
                break
        if limit is not None and len(rows_by_id) >= int(limit):
            break
        if not _master_processing_has_next_page(response.text, page) and len(page_rows) < request_page_size:
            break
        page += 1

    rows = list(rows_by_id.values())
    rows.sort(key=lambda row: (str(row.get("datetime") or ""), int(row.get("processed_frame_id") or 0)))
    if limit is not None:
        rows = rows[: int(limit)]

    calibration_ids = sorted(
        {
            int(value)
            for row in rows
            for value in (row.get("dark_id"), row.get("flat_id"))
            if value is not None
        }
    )
    resolved: dict[int, str | None] = {}
    for raw_id in calibration_ids:
        resolved[raw_id] = resolve_master_raw_path_http(
            session=session,
            raw_id=raw_id,
            base_url=base_url,
            timeout_connect=timeout_connect,
            timeout_read=timeout_read,
        )
    for row in rows:
        dark_id = row.get("dark_id")
        flat_id = row.get("flat_id")
        if dark_id is not None:
            row["dark_path"] = resolved.get(int(dark_id))
        if flat_id is not None:
            row["flat_path"] = resolved.get(int(flat_id))

    return [_normalize_inventory_row(row, base_url=base_url) for row in rows]


def write_master_inventory_csv(rows: Iterable[dict[str, object]], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in INVENTORY_COLUMNS})
    return out


def read_master_inventory_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def summarize_master_inventory(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row.get("night_utc") or ""), str(row.get("filter") or ""))
        item = grouped.setdefault(
            key,
            {
                "night_utc": key[0],
                "filter": key[1],
                "n_frames": 0,
                "unique_darks": set(),
                "unique_flats": set(),
            },
        )
        item["n_frames"] = int(item["n_frames"]) + 1
        if row.get("dark_path"):
            item["unique_darks"].add(str(row["dark_path"]))
        if row.get("flat_path"):
            item["unique_flats"].add(str(row["flat_path"]))

    result: list[dict[str, object]] = []
    for key in sorted(grouped):
        item = grouped[key]
        result.append(
            {
                "night_utc": item["night_utc"],
                "filter": item["filter"],
                "n_frames": item["n_frames"],
                "unique_darks": len(item["unique_darks"]),
                "unique_flats": len(item["unique_flats"]),
            }
        )
    return result



def summarize_master_observing_series(
    rows: Sequence[dict[str, object]],
    *,
    max_field_separation_arcmin: float = 30.0,
    max_series_gap_minutes: float = 60.0,
) -> list[dict[str, object]]:
    """Build provisional observing series directly from a MASTER inventory.

    The remote inventory does not expose tube/camera metadata, so this is a
    pre-download grouping by sky center, filter and time continuity. Exact
    Stage-2 field_series remain authoritative after FITS headers are indexed.
    """

    if max_field_separation_arcmin <= 0:
        raise ValueError("max_field_separation_arcmin must be positive")
    if max_series_gap_minutes <= 0:
        raise ValueError("max_series_gap_minutes must be positive")

    prepared: list[dict[str, object]] = []
    for row in rows:
        dt = _inventory_datetime(row.get("datetime"))
        coord = _inventory_radec_deg(row.get("coord2000"))
        if dt is None or coord is None:
            continue
        prepared.append({"row": row, "dt": dt, "coord": coord})
    prepared.sort(key=lambda item: (item["dt"], int(item["row"].get("processed_frame_id") or 0)))

    field_clusters: list[dict[str, object]] = []
    for item in prepared:
        coord = item["coord"]
        best_idx = None
        best_sep = None
        for idx, cluster in enumerate(field_clusters):
            sep = _angular_separation_arcmin(coord, cluster["center"])
            if sep <= max_field_separation_arcmin and (best_sep is None or sep < best_sep):
                best_idx = idx
                best_sep = sep
        if best_idx is None:
            field_clusters.append({"center": coord, "items": [item]})
        else:
            cluster = field_clusters[best_idx]
            cluster["items"].append(item)
            cluster["center"] = _mean_radec_deg([entry["coord"] for entry in cluster["items"]])

    series_groups: list[dict[str, object]] = []
    for field_index, cluster in enumerate(field_clusters, start=1):
        by_filter: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in cluster["items"]:
            by_filter[str(item["row"].get("filter") or "")].append(item)
        for filter_name, items in sorted(by_filter.items()):
            items.sort(key=lambda entry: entry["dt"])
            current: list[dict[str, object]] = []
            prev_dt = None
            for item in items:
                if prev_dt is not None:
                    gap_min = (item["dt"] - prev_dt).total_seconds() / 60.0
                    if gap_min > max_series_gap_minutes:
                        series_groups.append(_master_series_row(field_index, filter_name, current))
                        current = []
                current.append(item)
                prev_dt = item["dt"]
            if current:
                series_groups.append(_master_series_row(field_index, filter_name, current))

    series_groups.sort(key=lambda row: (str(row["datetime_start"]), int(row["field_cluster"]), str(row["filter"])))
    for index, row in enumerate(series_groups, start=1):
        row["archive_series"] = index
    return series_groups


def _inventory_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _inventory_radec_deg(value: object) -> tuple[float, float] | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # MASTER HTTPS currently emits strings like
    # "21h 50m 06s +72d 11m 00s". Keep a decimal fallback for exported CSVs.
    match = re.match(
        r"^\s*(\d+(?:\.\d+)?)h\s*(\d+(?:\.\d+)?)m\s*(\d+(?:\.\d+)?)s\s*([+-]?\d+(?:\.\d+)?)d\s*(\d+(?:\.\d+)?)m\s*(\d+(?:\.\d+)?)s\s*$",
        text,
        re.IGNORECASE,
    )
    if match:
        rh, rm, rs, dd_text, dm, ds = match.groups()
        ra_deg = 15.0 * (float(rh) + float(rm) / 60.0 + float(rs) / 3600.0)
        dd = float(dd_text)
        sign = -1.0 if str(dd_text).startswith("-") else 1.0
        dec_deg = sign * (abs(dd) + float(dm) / 60.0 + float(ds) / 3600.0)
        return ra_deg % 360.0, dec_deg
    decimal = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[, ]\s*([+-]?\d+(?:\.\d+)?)\s*$", text)
    if decimal:
        ra_deg, dec_deg = map(float, decimal.groups())
        if 0.0 <= ra_deg < 360.0 and -90.0 <= dec_deg <= 90.0:
            return ra_deg, dec_deg
    return None


def _angular_separation_arcmin(a: tuple[float, float], b: tuple[float, float]) -> float:
    ra1, dec1 = map(math.radians, a)
    ra2, dec2 = map(math.radians, b)
    cos_sep = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    sep = math.acos(max(-1.0, min(1.0, cos_sep)))
    return math.degrees(sep) * 60.0


def _mean_radec_deg(coords: Sequence[tuple[float, float]]) -> tuple[float, float]:
    if not coords:
        raise ValueError("cannot average an empty coordinate list")
    x = y = z = 0.0
    for ra_deg, dec_deg in coords:
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)
        x += math.cos(dec) * math.cos(ra)
        y += math.cos(dec) * math.sin(ra)
        z += math.sin(dec)
    x /= len(coords)
    y /= len(coords)
    z /= len(coords)
    ra = math.degrees(math.atan2(y, x)) % 360.0
    dec = math.degrees(math.atan2(z, math.hypot(x, y)))
    return ra, dec


def _master_series_row(field_index: int, filter_name: str, items) -> dict[str, object]:
    center_ra, center_dec = _mean_radec_deg([item["coord"] for item in items])
    dt_start = items[0]["dt"]
    dt_end = items[-1]["dt"]
    rows = [item["row"] for item in items]
    darks = {str(row.get("dark_path")) for row in rows if row.get("dark_path")}
    flats = {str(row.get("flat_path")) for row in rows if row.get("flat_path")}
    raw_ids = [int(row["raw_id"]) for row in rows if row.get("raw_id") not in (None, "")]
    return {
        "archive_series": None,
        "field_cluster": field_index,
        "night_utc": _night_utc_from_datetime(dt_start),
        "filter": filter_name,
        "n_frames": len(rows),
        "datetime_start": dt_start.isoformat(sep=" "),
        "datetime_end": dt_end.isoformat(sep=" "),
        "duration_min": round((dt_end - dt_start).total_seconds() / 60.0, 3),
        "center_ra_deg": round(center_ra, 7),
        "center_dec_deg": round(center_dec, 7),
        "raw_id_first": min(raw_ids) if raw_ids else None,
        "raw_id_last": max(raw_ids) if raw_ids else None,
        "unique_darks": len(darks),
        "unique_flats": len(flats),
    }


def build_master_fetch_tasks(
    rows: Sequence[dict[str, object]],
    *,
    output_root: str | Path,
    roles: Sequence[str] = ("raw", "header", "dark", "flat"),
    base_url: str | None = None,
) -> list[MasterFetchTask]:
    selected_roles = tuple(dict.fromkeys(str(role).strip().lower() for role in roles if str(role).strip()))
    allowed = {"raw", "header", "dark", "flat"}
    unknown = [role for role in selected_roles if role not in allowed]
    if unknown:
        raise ValueError(f"Unsupported MASTER fetch roles: {', '.join(unknown)}")

    grouped_roles: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for role in selected_roles:
            path_value = row.get(f"{role}_path")
            if path_value is None or not str(path_value).strip():
                continue
            grouped_roles[str(path_value).strip()].add(role)

    tasks: list[MasterFetchTask] = []
    for remote_path in sorted(grouped_roles):
        url = master_remote_path_to_url(remote_path, base_url=base_url)
        if not url:
            raise ValueError(f"Cannot map MASTER path to HTTP URL: {remote_path!r}")
        local_path = master_remote_path_to_local(remote_path, output_root)
        tasks.append(
            MasterFetchTask(
                remote_path=remote_path,
                url=url,
                local_path=local_path,
                roles=tuple(sorted(grouped_roles[remote_path])),
            )
        )
    return tasks


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def make_master_http_session(*, user: str, password: str, retries: int = 4):
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - guarded by package dependency
        raise RuntimeError("MASTER archive download requires requests>=2.31") from exc

    session = requests.Session()
    session.auth = (user, password)
    retry = Retry(
        total=max(0, int(retries)),
        connect=max(0, int(retries)),
        read=max(0, int(retries)),
        status=max(0, int(retries)),
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Grandgousier-MASTER-archive/0.5.41.6"})
    return session


def download_master_task(
    task: MasterFetchTask,
    *,
    session,
    timeout_connect: float = 15.0,
    timeout_read: float = 180.0,
    overwrite: bool = False,
    checksum: bool = True,
    chunk_size: int = 1024 * 1024,
    max_attempts: int = 1,
    stall_timeout_seconds: float = 300.0,
    progress_interval_seconds: float = 15.0,
    progress_callback: Callable[[TransportProgress], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> MasterDownloadResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if stall_timeout_seconds <= 0:
        raise ValueError("stall_timeout_seconds must be positive")
    if progress_interval_seconds <= 0:
        raise ValueError("progress_interval_seconds must be positive")

    final_path = task.local_path
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists() and final_path.is_file() and final_path.stat().st_size > 0 and not overwrite:
        final_part = final_path.with_name(final_path.name + ".part")
        final_part.unlink(missing_ok=True)
        final_part.with_name(final_part.name + ".meta.json").unlink(missing_ok=True)
        return MasterDownloadResult(
            remote_path=task.remote_path,
            url=task.url,
            local_path=final_path,
            roles=task.roles,
            status="SKIPPED_EXISTING",
            size_bytes=final_path.stat().st_size,
            sha256=_sha256_file(final_path) if checksum else None,
        )

    part_path = final_path.with_name(final_path.name + ".part")
    metadata_path = part_path.with_name(part_path.name + ".meta.json")
    if overwrite and part_path.exists():
        part_path.unlink()
        metadata_path.unlink(missing_ok=True)
    metadata = _read_transport_metadata(metadata_path)
    initial_offset = part_path.stat().st_size if part_path.exists() else 0
    last_error: Exception | None = None

    def emit(progress: TransportProgress) -> None:
        if progress_callback is not None:
            progress_callback(progress)

    for attempt in range(1, max_attempts + 1):
        resume_from = part_path.stat().st_size if part_path.exists() else 0
        started = monotonic()
        last_progress = started
        last_report = started - progress_interval_seconds
        downloaded_this_attempt = 0
        expected_total = _optional_positive_int(metadata.get("expected_bytes"))
        try:
            headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
            response = session.get(
                task.url,
                stream=True,
                headers=headers,
                timeout=(float(timeout_connect), float(timeout_read)),
            )
            with response:
                if response.status_code == 401:
                    raise RuntimeError("HTTP 401 Unauthorized")
                if response.status_code == 404:
                    raise RuntimeError("HTTP 404 Not Found")
                response.raise_for_status()

                expected_total, append = _validate_master_response(
                    response,
                    requested_offset=resume_from,
                    prior_metadata=metadata,
                )
                if resume_from > 0 and response.status_code == 200:
                    emit(_make_progress(
                        task.remote_path, expected_total, resume_from, downloaded_this_attempt,
                        part_path.stat().st_size, started, attempt, last_progress,
                        "RANGE_IGNORED_RESTARTING_FROM_ZERO", monotonic(),
                    ))
                    part_path.unlink(missing_ok=True)
                    metadata = {}
                    resume_from = 0
                    downloaded_this_attempt = 0
                    append = False

                current_size = part_path.stat().st_size if part_path.exists() else 0
                metadata = _write_transport_metadata(
                    metadata_path,
                    task=task,
                    expected_bytes=expected_total,
                    current_size=current_size,
                    response=response,
                    attempts=attempt,
                )
                mode = "ab" if append else "wb"
                with part_path.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        now = monotonic()
                        if now - last_progress > stall_timeout_seconds:
                            raise MasterTransportError(
                                f"MASTER transfer stalled for {stall_timeout_seconds:.0f}s"
                            )
                        if not chunk:
                            continue
                        handle.write(chunk)
                        handle.flush()
                        downloaded_this_attempt += len(chunk)
                        last_progress = now
                        current_size += len(chunk)
                        if now - last_report >= progress_interval_seconds:
                            emit(_make_progress(
                                task.remote_path, expected_total, resume_from,
                                downloaded_this_attempt, current_size, started, attempt,
                                last_progress, "PROGRESS", now,
                            ))
                            last_report = now
                            _write_transport_metadata(
                                metadata_path,
                                task=task,
                                expected_bytes=expected_total,
                                current_size=current_size,
                                response=response,
                                attempts=attempt,
                                last_progress_monotonic=last_progress,
                            )

            actual_size = part_path.stat().st_size if part_path.exists() else 0
            if actual_size <= 0:
                raise MasterTransportError("Downloaded file is empty")
            if expected_total is not None and actual_size != expected_total:
                raise MasterTransportError(
                    f"Downloaded size {actual_size} does not match expected {expected_total}"
                )
            os.replace(part_path, final_path)
            metadata_path.unlink(missing_ok=True)
            now = monotonic()
            emit(_make_progress(
                task.remote_path, expected_total, resume_from, downloaded_this_attempt,
                actual_size, started, attempt, last_progress, "COMPLETE", now,
            ))
            return MasterDownloadResult(
                remote_path=task.remote_path,
                url=task.url,
                local_path=final_path,
                roles=task.roles,
                status="DOWNLOADED_RESUMED" if initial_offset > 0 and append else "DOWNLOADED",
                size_bytes=actual_size,
                sha256=_sha256_file(final_path) if checksum else None,
            )
        except Exception as exc:
            last_error = exc
            current_size = part_path.stat().st_size if part_path.exists() else 0
            _write_transport_metadata(
                metadata_path,
                task=task,
                expected_bytes=expected_total,
                current_size=current_size,
                response=None,
                attempts=attempt,
                last_progress_monotonic=last_progress,
            )
            if attempt < max_attempts:
                now = monotonic()
                emit(_make_progress(
                    task.remote_path, expected_total, resume_from, downloaded_this_attempt,
                    current_size, started, attempt, last_progress, "RETRY", now,
                    reason=_safe_transport_error(exc, task.url),
                ))
                continue
            break

    return MasterDownloadResult(
        remote_path=task.remote_path,
        url=task.url,
        local_path=final_path,
        roles=task.roles,
        status="FAILED",
        size_bytes=part_path.stat().st_size if part_path.exists() else None,
        error=_safe_transport_error(last_error, task.url) if last_error else "transfer failed",
    )


_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


def _optional_positive_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _response_header(response, name: str) -> str | None:
    value = response.headers.get(name)
    return str(value).strip() if value not in {None, ""} else None


def _safe_transport_error(error: Exception, url: str) -> str:
    """Keep transport diagnostics useful without exposing the request URL."""
    detail = str(error).replace(url, "<redacted-url>")
    detail = re.sub(r"https?://\S+", "<redacted-url>", detail)
    return f"{type(error).__name__}: {detail}"


def _validate_master_response(response, *, requested_offset: int, prior_metadata: dict[str, object]):
    content_range = _response_header(response, "Content-Range")
    content_length = _optional_positive_int(_response_header(response, "Content-Length"))
    etag = _response_header(response, "ETag")
    last_modified = _response_header(response, "Last-Modified")
    old_etag = str(prior_metadata.get("etag") or "").strip() or None
    old_last_modified = str(prior_metadata.get("last_modified") or "").strip() or None
    remote_changed = (old_etag and etag != old_etag) or (
        old_last_modified and last_modified != old_last_modified
    )
    if requested_offset > 0 and remote_changed:
        raise MasterTransportError("Remote MASTER asset changed while resuming")

    if response.status_code == 206:
        if not content_range:
            raise MasterTransportError("206 response is missing Content-Range")
        match = _CONTENT_RANGE_RE.fullmatch(content_range)
        if not match or match.group(3) == "*":
            raise MasterTransportError(f"Invalid Content-Range: {content_range}")
        start, end, total = (int(value) for value in match.groups())
        if start != requested_offset or end < start or total <= end:
            raise MasterTransportError(f"Unexpected Content-Range: {content_range}")
        segment_size = end - start + 1
        if content_length is not None and content_length != segment_size:
            raise MasterTransportError("Content-Length does not match Content-Range")
        return total, requested_offset > 0
    if response.status_code == 200:
        if requested_offset > 0:
            return content_length, False
        return content_length, False
    raise MasterTransportError(f"Unexpected HTTP status {response.status_code}")


def _read_transport_metadata(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_transport_metadata(
    path: Path,
    *,
    task: MasterFetchTask,
    expected_bytes: int | None,
    current_size: int,
    response,
    attempts: int,
    last_progress_monotonic: float | None = None,
) -> dict[str, object]:
    previous = _read_transport_metadata(path)
    payload = {
        "schema": MASTER_CACHE_MARKER_SCHEMA,
        "remote_path": task.remote_path,
        "roles": list(task.roles),
        "expected_bytes": expected_bytes,
        "current_size": current_size,
        "attempts": attempts,
        "etag": _response_header(response, "ETag") if response is not None else previous.get("etag"),
        "last_modified": (
            _response_header(response, "Last-Modified")
            if response is not None else previous.get("last_modified")
        ),
    }
    if last_progress_monotonic is not None:
        payload["last_progress_monotonic"] = last_progress_monotonic
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _make_progress(
    remote_path: str,
    expected_bytes: int | None,
    existing_offset: int,
    downloaded_this_attempt: int,
    total_downloaded: int,
    started: float,
    attempt: int,
    last_progress: float,
    event: str,
    now: float,
    reason: str | None = None,
) -> TransportProgress:
    elapsed = max(now - started, 0.0)
    recent_throughput = (
        downloaded_this_attempt / 1048576.0 / elapsed if elapsed > 0 else None
    )
    return TransportProgress(
        remote_path=remote_path,
        expected_bytes=expected_bytes,
        existing_offset=existing_offset,
        downloaded_this_attempt=downloaded_this_attempt,
        total_downloaded=total_downloaded,
        elapsed=elapsed,
        recent_throughput=recent_throughput,
        attempt=attempt,
        last_progress_monotonic=last_progress,
        event=event,
        reason=reason,
    )


def write_master_fetch_report(results: Iterable[MasterDownloadResult], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FETCH_REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(result.as_row())
    return out
