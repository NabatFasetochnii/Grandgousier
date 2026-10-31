from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from astropy.io import fits

from grandgousier.archive.auto_process import _detected_file_from_header
from grandgousier.archive.manifest import extract_numeric_frame_id
from grandgousier.archive.master_reduction import validate_master_reduction_inputs
from grandgousier.archive.stage1_adapters import (
    _identity_fields,
    _optional_float,
    _request_matches,
    _text_or_none,
    load_instrument_profile,
)
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    AssetRef,
    LocalAsset,
    ObservationRecord,
)
from grandgousier.io.parsing import parse_datetime, parse_dec_deg, parse_ra_deg, to_int

MASTER_PHYSICAL_REQUIRED_ROLES = (
    "science_raw",
    "processing_header",
    "dark",
    "flat",
)
DEFAULT_MAX_MASTER_PHYSICAL_FILES = 100
MAX_MASTER_PHYSICAL_FILES = 10_000

_FITS_NAME_RE = re.compile(r"^\d+\.(?:fit|fits|fts)(?:\.gz)?$", re.IGNORECASE)
_RAW_RELATIVE_RE = re.compile(
    r"^rawdata/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<raw_id>\d+)\.(?:fit|fits|fts)(?:\.gz)?$",
    re.IGNORECASE,
)
_RAW_LOCATOR_RE = re.compile(
    r"^/master/imdata/rawdata/\d{4}/\d{2}/\d{2}/\d+\."
    r"(?:fit|fits|fts)(?:\.gz)?$",
    re.IGNORECASE,
)
_HEADER_LOCATOR_RE = re.compile(
    r"^/master/imdata/procdata/\d{4}/\d{2}/\d{2}/\d+_header\.fits$",
    re.IGNORECASE,
)
_PHYSICAL_RAW_KEY_RE = re.compile(
    r"^rawdata/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<raw_id>\d+)\.(?:fit|fits|fts)(?:\.gz)?$"
)
_PHYSICAL_RAW_LOCATOR_RE = re.compile(
    r"^/master/imdata/rawdata/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"\d+\.(?:fit|fits|fts)(?:\.gz)?$"
)
_PHYSICAL_HEADER_LOCATOR_RE = re.compile(
    r"^/master/imdata/procdata/\d{4}/\d{2}/\d{2}/\d+_header\.fits$"
)
_PHYSICAL_ROLES = MASTER_PHYSICAL_REQUIRED_ROLES
_ROLE_IMAGE_TYPES = {
    "science_raw": {"LIGHT", "OBJECT"},
    "dark": {"SDARK"},
    "flat": {"SFLAT"},
}


class _PhysicalAssetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MasterPhysicalAssetProvider:
    """Read selected local MASTER assets through canonical, root-contained locators."""

    def __init__(self, *, source_id: str, imdata_root: str | Path) -> None:
        self.source_id = source_id
        self.root = Path(imdata_root).expanduser().resolve(strict=False)

    def materialize(
        self, ref: AssetRef, destination: str | Path | None = None
    ) -> LocalAsset:
        del destination
        if ref.source_id != self.source_id:
            raise ValueError(f"Asset source mismatch: {ref.source_id!r} != {self.source_id!r}")
        if ref.backend != "master-physical":
            raise ValueError(f"Unsupported MASTER physical asset backend: {ref.backend!r}")
        if ref.role not in _PHYSICAL_ROLES:
            raise ValueError(f"Unsupported MASTER physical asset role: {ref.role!r}")
        path = _resolve_physical_asset(self.root, ref.role, ref.locator)
        try:
            header = fits.getheader(path, ext=0)
        except Exception as exc:
            raise _PhysicalAssetError(
                "FITS_HEADER_READ_ERROR",
                f"Could not read {ref.role} FITS primary header: {type(exc).__name__}",
            ) from exc
        _validate_physical_role_header(ref.role, header)
        return LocalAsset(ref=ref, path=path)


class MasterPhysicalMetadataProvider:
    """Bounded MASTER source over explicitly configured raw-relative physical keys."""

    def __init__(
        self,
        *,
        source_id: str,
        imdata_root: str | Path,
        raw_relative_physical_keys: list[str] | tuple[str, ...],
        profile=None,
    ) -> None:
        self.source_id = source_id
        self.root = Path(imdata_root).expanduser().resolve(strict=False)
        self.profile = profile or load_instrument_profile("MASTER")
        if self.profile.name != "MASTER":
            raise ValueError("master-physical adapter requires the MASTER instrument profile")
        if not isinstance(raw_relative_physical_keys, (list, tuple)) or not raw_relative_physical_keys:
            raise ValueError("raw_relative_physical_keys must be a nonempty list of exact raw paths")
        if len(raw_relative_physical_keys) > DEFAULT_MAX_MASTER_PHYSICAL_FILES:
            raise ValueError(
                "raw_relative_physical_keys must contain at most "
                f"{DEFAULT_MAX_MASTER_PHYSICAL_FILES} exact raw paths"
            )
        keys = tuple(_validate_raw_relative_physical_key(value) for value in raw_relative_physical_keys)
        if len(set(keys)) != len(keys):
            raise ValueError("raw_relative_physical_keys must not contain duplicates")
        self.raw_relative_physical_keys = keys

    def search(self, request: ArchiveRequest) -> Iterator[ObservationRecord]:
        if request.source_id != self.source_id:
            raise ValueError("ArchiveRequest source_id does not match MASTER physical source")
        emitted = 0
        for raw_key in self.raw_relative_physical_keys:
            record = self._record_for_key(raw_key)
            if _request_matches(record, request):
                yield record
                emitted += 1
                if request.limit is not None and emitted >= request.limit:
                    break

    def _record_for_key(self, raw_key: str) -> ObservationRecord:
        match = _PHYSICAL_RAW_KEY_RE.fullmatch(raw_key)
        assert match is not None
        raw_locator = f"/master/imdata/{raw_key}"
        header_locator = _header_locator_from_raw_key(match)
        assets = [
            _physical_ref(self.source_id, "science_raw", raw_locator, raw_key),
            _physical_ref(self.source_id, "processing_header", header_locator, raw_key),
        ]
        blockers: list[dict[str, str]] = []

        raw_header = _read_physical_header(
            self.root, "science_raw", raw_locator, blockers
        )
        processing_header = _read_physical_header(
            self.root, "processing_header", header_locator, blockers
        )
        if raw_header is not None:
            _validate_header_role("science_raw", raw_header, blockers)
        if processing_header is not None:
            _validate_header_role("processing_header", processing_header, blockers)

        calibration_headers: dict[str, Any | None] = {"dark": None, "flat": None}
        for role, field_name in (("dark", "dark_path"), ("flat", "flat_path")):
            locator_value = (
                self.profile.get_header_value(processing_header, field_name)
                if processing_header is not None
                else None
            )
            locator = _text_or_none(locator_value)
            if locator is None:
                reason = (
                    "processing header is unavailable"
                    if processing_header is None
                    else f"processing header has no direct {role.upper()} reference"
                )
                _add_stage1_blocker(
                    blockers,
                    "MISSING_CALIBRATION_REFERENCE",
                    reason,
                    role=role,
                )
                continue
            try:
                _validate_physical_locator(role, locator)
            except _PhysicalAssetError as exc:
                _add_stage1_blocker(blockers, exc.code, str(exc), role=role)
                continue
            assets.append(_physical_ref(self.source_id, role, locator, raw_key))
            calibration_headers[role] = _read_physical_header(
                self.root, role, locator, blockers
            )
            if calibration_headers[role] is not None:
                _validate_header_role(role, calibration_headers[role], blockers)

        if raw_header is not None and processing_header is not None:
            _check_optional_sidecar_identity(raw_header, processing_header, blockers)
        headers = {
            "science_raw": raw_header,
            "dark": calibration_headers["dark"],
            "flat": calibration_headers["flat"],
            "processing_header": processing_header,
        }
        _validate_required_header_cards(headers, blockers)
        _validate_header_geometry(headers, blockers)

        date_obs_raw = (
            self.profile.get_header_value(raw_header, "date_obs")
            if raw_header is not None
            else None
        )
        date_obs = parse_datetime(date_obs_raw)
        if date_obs is None:
            _add_stage1_blocker(
                blockers,
                "INVALID_RAW_DATE_OBS",
                "raw LIGHT/OBJECT FITS must contain a valid DATE-OBS",
                role="science_raw",
            )
        camera = _text_or_none(
            self.profile.get_header_value(raw_header, "camera")
            if raw_header is not None
            else None
        )
        identity = (
            _identity_fields(
                source_id=self.source_id,
                instrument="MASTER",
                camera=camera,
                date_obs=date_obs_raw,
                header=dict(raw_header),
            )
            if raw_header is not None
            else {
                "identity_status": "MISSING",
                "identity_detail": "raw FITS header is unavailable",
                "native_observation_id": None,
                "source_observation_uid": None,
                "identity_scheme": None,
                "p4_native_identity": None,
                "observation_uid": None,
            }
        )
        filter_raw = (
            self.profile.get_header_value(raw_header, "filter")
            if raw_header is not None
            else None
        )
        record_metadata: dict[str, Any] = {
            "raw_relative_physical_key": raw_key,
            "raw_id": match.group("raw_id"),
        }
        if blockers:
            record_metadata["stage1_blockers"] = blockers
        record = ObservationRecord(
            source_id=self.source_id,
            instrument="MASTER",
            frame_type="science",
            input_state="raw",
            identity_status=identity["identity_status"],
            identity_detail=identity["identity_detail"],
            native_observation_id=identity["native_observation_id"],
            source_observation_uid=identity["source_observation_uid"],
            identity_scheme=identity["identity_scheme"],
            p4_native_identity=identity["p4_native_identity"],
            observation_uid=identity["observation_uid"],
            site=_text_or_none(
                self.profile.get_header_value(raw_header, "site")
                if raw_header is not None
                else None
            ),
            tube=_text_or_none(
                self.profile.get_header_value(raw_header, "tube")
                if raw_header is not None
                else None
            ),
            camera=camera,
            filter_name=self.profile.normalize_filter(filter_raw),
            object_name=_text_or_none(
                self.profile.get_header_value(raw_header, "object")
                if raw_header is not None
                else None
            ),
            observed_at=date_obs.isoformat() if date_obs is not None else None,
            exptime=_optional_float(
                self.profile.get_header_value(raw_header, "exptime")
                if raw_header is not None
                else None
            ),
            ra_deg=parse_ra_deg(
                self.profile.get_header_value(raw_header, "ra_center")
                if raw_header is not None
                else None
            ),
            dec_deg=parse_dec_deg(
                self.profile.get_header_value(raw_header, "dec_center")
                if raw_header is not None
                else None
            ),
            ccd_temp=_optional_float(
                self.profile.get_header_value(raw_header, "ccd_temp")
                if raw_header is not None
                else None
            ),
            binning_x=to_int(
                self.profile.get_header_value(raw_header, "binning_x")
                if raw_header is not None
                else None
            ),
            binning_y=to_int(
                self.profile.get_header_value(raw_header, "binning_y")
                if raw_header is not None
                else None
            ),
            required_asset_roles=_PHYSICAL_ROLES,
            assets=tuple(assets),
            metadata=record_metadata,
        )
        return record


@dataclass(frozen=True)
class _HeaderOnlyGeometry:
    shape: tuple[int, int]

    @property
    def ndim(self) -> int:
        return 2


def _validate_raw_relative_physical_key(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("raw physical keys must be exact canonical POSIX relative paths")
    match = _PHYSICAL_RAW_KEY_RE.fullmatch(value)
    if match is None or not _valid_archive_date(match):
        raise ValueError(f"invalid canonical MASTER raw-relative physical key: {value!r}")
    return value


def _header_locator_from_raw_key(match: re.Match[str]) -> str:
    return (
        f"/master/imdata/procdata/{match.group('year')}/{match.group('month')}/"
        f"{match.group('day')}/{match.group('raw_id')}_header.fits"
    )


def _physical_ref(
    source_id: str, role: str, locator: str, raw_key: str
) -> AssetRef:
    return AssetRef(
        source_id=source_id,
        role=role,
        backend="master-physical",
        locator=locator,
        metadata={"raw_relative_physical_key": raw_key},
    )


def _validate_physical_locator(role: str, locator: str) -> None:
    if not isinstance(locator, str):
        raise _PhysicalAssetError("NONCANONICAL_LOCATOR", "physical asset locator must be text")
    if role == "processing_header":
        matched = _PHYSICAL_HEADER_LOCATOR_RE.fullmatch(locator)
    elif role in {"science_raw", "dark", "flat"}:
        matched = _PHYSICAL_RAW_LOCATOR_RE.fullmatch(locator)
    else:
        matched = None
    if matched is None:
        raise _PhysicalAssetError(
            "NONCANONICAL_LOCATOR",
            f"{role} locator is not in its canonical /master/imdata role shape",
        )
    if role in {"science_raw", "dark", "flat"} and not _valid_archive_date(matched):
        raise _PhysicalAssetError(
            "INVALID_ARCHIVE_DATE",
            f"{role} locator contains an impossible calendar date",
        )


def _resolve_physical_asset(root: Path, role: str, locator: str) -> Path:
    _validate_physical_locator(role, locator)
    pure = PurePosixPath(locator)
    if pure.parts[:3] != ("/", "master", "imdata"):
        raise _PhysicalAssetError("NONCANONICAL_LOCATOR", "locator is outside /master/imdata")
    try:
        resolved_root = root.resolve(strict=False)
        resolved = root.joinpath(*pure.parts[3:]).resolve(strict=False)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _PhysicalAssetError(
            "LOCATOR_ESCAPES_ROOT", "physical asset resolves outside configured imdata_root"
        ) from exc
    if not resolved.is_file():
        raise _PhysicalAssetError("ASSET_NOT_FOUND", f"{role} asset file is missing")
    return resolved


def _read_physical_header(
    root: Path,
    role: str,
    locator: str,
    blockers: list[dict[str, str]],
) -> Any | None:
    try:
        path = _resolve_physical_asset(root, role, locator)
    except _PhysicalAssetError as exc:
        _add_stage1_blocker(blockers, exc.code, str(exc), role=role)
        return None
    try:
        return fits.getheader(path, ext=0)
    except Exception as exc:  # noqa: BLE001 - keep the source item and report a blocker.
        _add_stage1_blocker(
            blockers,
            "FITS_HEADER_READ_ERROR",
            f"Could not read {role} FITS primary header: {type(exc).__name__}",
            role=role,
        )
        return None


def _validate_physical_role_header(role: str, header: Any) -> None:
    if role == "processing_header":
        try:
            no_primary_data = int(header.get("NAXIS", -1)) == 0
        except (TypeError, ValueError):
            no_primary_data = False
        if not no_primary_data:
            raise _PhysicalAssetError(
                "ROLE_MISMATCH", "processing_header FITS must have no primary image data"
            )
        return
    image_type = str(header.get("IMAGETYP") or "").strip().upper()
    if image_type not in _ROLE_IMAGE_TYPES[role]:
        expected = "/".join(sorted(_ROLE_IMAGE_TYPES[role]))
        raise _PhysicalAssetError(
            "ROLE_MISMATCH",
            f"{role} asset requires FITS IMAGETYP={expected}, got {image_type or '<missing>'}",
        )


def _validate_header_role(
    role: str, header: Any, blockers: list[dict[str, str]]
) -> None:
    try:
        _validate_physical_role_header(role, header)
    except _PhysicalAssetError as exc:
        _add_stage1_blocker(blockers, exc.code, str(exc), role=role)


def _header_shape(header: Any, role: str, blockers: list[dict[str, str]]) -> tuple[int, int]:
    try:
        naxis = int(header.get("NAXIS", 0))
        width = int(header.get("NAXIS1", 0))
        height = int(header.get("NAXIS2", 0))
    except (TypeError, ValueError):
        naxis, width, height = 0, 0, 0
    if naxis != 2 or width <= 0 or height <= 0:
        _add_stage1_blocker(
            blockers,
            "INVALID_IMAGE_GEOMETRY",
            f"{role} FITS must have positive two-dimensional NAXIS geometry",
            role=role,
        )
        return (0, 0)
    return (height, width)


def _validate_required_header_cards(
    headers: dict[str, Any | None], blockers: list[dict[str, str]]
) -> None:
    required = {
        "science_raw": ("ID_CAM", "XBINNING", "YBINNING", "EXPTIME", "FILTER"),
        "dark": ("ID_CAM", "XBINNING", "YBINNING", "EXPTIME"),
        "flat": ("ID_CAM", "XBINNING", "YBINNING", "FILTER"),
        "processing_header": (
            "ID_CAM", "XBINNING", "YBINNING", "EXPTIME", "FILTER"
        ),
    }
    for role, names in required.items():
        header = headers[role]
        if header is None:
            continue
        missing = [name for name in names if header.get(name) in (None, "")]
        if missing:
            _add_stage1_blocker(
                blockers,
                "REQUIRED_HEADER_CARD_MISSING",
                f"{role} FITS is missing required cards: {','.join(missing)}",
                role=role,
            )


def _check_optional_sidecar_identity(
    raw_header: Any, processing_header: Any, blockers: list[dict[str, str]]
) -> None:
    raw_uniqnum = _text_or_none(raw_header.get("UNIQNUM"))
    sidecar_uniqnum = _text_or_none(processing_header.get("UNIQNUM"))
    if sidecar_uniqnum is not None and sidecar_uniqnum != raw_uniqnum:
        _add_stage1_blocker(
            blockers,
            "SIDECAR_UNIQNUM_MISMATCH",
            "processing-header UNIQNUM does not match raw UNIQNUM",
            role="processing_header",
        )
    sidecar_date_raw = processing_header.get("DATE-OBS")
    if sidecar_date_raw not in (None, ""):
        raw_date = parse_datetime(raw_header.get("DATE-OBS"))
        sidecar_date = parse_datetime(sidecar_date_raw)
        if raw_date is None or sidecar_date is None or raw_date != sidecar_date:
            _add_stage1_blocker(
                blockers,
                "SIDECAR_DATE_OBS_MISMATCH",
                "processing-header DATE-OBS is invalid or does not match raw DATE-OBS",
                role="processing_header",
            )


def _validate_header_geometry(
    headers: dict[str, Any | None], blockers: list[dict[str, str]]
) -> None:
    if any(headers[role] is None for role in _PHYSICAL_ROLES):
        return
    raw_header = headers["science_raw"]
    dark_header = headers["dark"]
    flat_header = headers["flat"]
    processing_header = headers["processing_header"]
    shapes = {
        role: _header_shape(header, role, blockers)
        for role, header in headers.items()
        if role != "processing_header"
    }
    try:
        validate_master_reduction_inputs(
            task=SimpleNamespace(
                dark_remote_path=processing_header.get("DARK"),
                flat_remote_path=processing_header.get("FLAT"),
            ),
            raw_data=_HeaderOnlyGeometry(shapes["science_raw"]),
            dark_data=_HeaderOnlyGeometry(shapes["dark"]),
            flat_data=_HeaderOnlyGeometry(shapes["flat"]),
            raw_header=raw_header,
            dark_header=dark_header,
            flat_header=flat_header,
            master_header=processing_header,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        _add_stage1_blocker(
            blockers,
            "REDUCER_INPUT_VALIDATION_FAILED",
            str(exc),
        )


def _add_stage1_blocker(
    blockers: list[dict[str, str]],
    code: str,
    message: str,
    *,
    role: str | None = None,
) -> None:
    blocker = {"code": code, "message": message}
    if role is not None:
        blocker["role"] = role
    if blocker not in blockers:
        blockers.append(blocker)


def probe_master_physical_archive(
    imdata_root: str | Path,
    *,
    max_files_scanned: int = DEFAULT_MAX_MASTER_PHYSICAL_FILES,
) -> dict[str, Any]:
    """Inspect a local MASTER imdata tree using FITS headers only.

    This is a bounded, plan-only diagnostic.  It does not construct instrument
    units, perform Stage-1 reductions, contact a database, or access a network.
    """

    if type(max_files_scanned) is not int or not 1 <= max_files_scanned <= MAX_MASTER_PHYSICAL_FILES:
        raise ValueError(
            f"max_files_scanned must be an integer from 1 through {MAX_MASTER_PHYSICAL_FILES}"
        )
    supplied_root = Path(imdata_root)
    if not supplied_root.is_dir():
        raise ValueError("imdata_root must be an existing directory")
    root = supplied_root.resolve(strict=True)
    raw_root = root / "rawdata"
    if not raw_root.is_dir():
        raise ValueError("imdata_root must contain a rawdata directory")
    if not _is_within(raw_root.resolve(strict=True), root):
        raise ValueError("rawdata directory resolves outside imdata_root")

    discovery_errors: list[dict[str, str]] = []
    discovered_paths: list[Path] = []
    for path in _discover_raw_fits(raw_root, on_error=discovery_errors):
        discovered_paths.append(path)
        if len(discovered_paths) > max_files_scanned:
            break
    truncated = len(discovered_paths) > max_files_scanned
    discovered_raw_files_exact = not truncated and not discovery_errors
    scanned_paths = discovered_paths[:max_files_scanned]
    scanned = [
        (path.relative_to(root).as_posix(), path)
        for path in scanned_paths
    ]

    profile = load_instrument_profile("MASTER")
    rows = [
        _probe_raw_file(root, relative_key, path, profile)
        for relative_key, path in scanned
    ]

    item_errors = [
        {"raw_relative_physical_key": row["raw_relative_physical_key"], **error}
        for row in rows
        for error in row["errors"]
    ]
    all_errors = [*discovery_errors, *item_errors]
    incomplete_items = sum(
        row["technical_stage1_eligibility"] != "ELIGIBLE_TO_ATTEMPT"
        for row in rows
        if row.get("image_type", {}).get("normalized") == "science"
    )
    complete = not truncated and not all_errors and incomplete_items == 0
    status = (
        "PHYSICAL_INVENTORY_TRUNCATED"
        if truncated
        else (
            "PHYSICAL_INVENTORY_COMPLETE"
            if complete
            else "PHYSICAL_INVENTORY_INCOMPLETE"
        )
    )
    return {
        "schema": "grandgousier.master.physical_archive_probe.v1",
        "status": status,
        "inventory_complete": complete,
        "assessment_scope": "LOCAL_PHYSICAL_INVENTORY_ONLY",
        "stage2_science_readiness": "NOT_ASSESSED",
        "calibration_compatibility": "NOT_ASSESSED",
        "calibration_locator_contract": "UNRESOLVED_WITHOUT_REAL_ARCHIVE_EVIDENCE",
        "truncated": truncated,
        "imdata_root": str(root),
        "raw_root": str(raw_root),
        "master_sql_required": "NO",
        "network_required": "NO",
        "execution_performed": False,
        "instrument_unit_creation": "NOT_ATTEMPTED",
        "identity_contract": (
            "raw_relative_physical_key and paths are locators only; MASTER P4 identity is UNIQNUM"
        ),
        "required_stage1_asset_roles": list(MASTER_PHYSICAL_REQUIRED_ROLES),
        "max_files_scanned": max_files_scanned,
        "discovered_raw_files": len(discovered_paths),
        "discovered_raw_files_count_kind": (
            "EXACT" if discovered_raw_files_exact else "LOWER_BOUND"
        ),
        "scanned_raw_files": len(scanned),
        "incomplete_science_items": incomplete_items,
        "discovery_errors": discovery_errors,
        "errors": all_errors,
        "items": rows,
    }


def _discover_raw_fits(
    raw_root: Path,
    *,
    on_error: list[dict[str, str]],
) -> Iterator[Path]:
    """Yield raw FITS in stable path order without collecting archive-wide paths."""

    def record_scan_error(path: Path, error: OSError) -> None:
        on_error.append({
            "code": "DIRECTORY_SCAN_ERROR",
            "path": str(error.filename or path),
            "error_type": type(error).__name__,
        })

    def walk(directory: Path) -> Iterator[Path]:
        try:
            with os.scandir(directory) as entries:
                ordered_entries = []
                for entry in entries:
                    try:
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        record_scan_error(Path(entry.path), exc)
                        continue
                    order_name = entry.name + ("/" if is_directory else "")
                    ordered_entries.append((order_name.casefold(), order_name, entry, is_directory))
        except OSError as exc:
            record_scan_error(directory, exc)
            return

        ordered_entries.sort(key=lambda item: (item[0], item[1]))
        for _folded_name, _order_name, entry, is_directory in ordered_entries:
            path = Path(entry.path)
            if is_directory:
                yield from walk(path)
            elif _FITS_NAME_RE.fullmatch(entry.name):
                yield path

    yield from walk(raw_root)


def _probe_raw_file(
    root: Path,
    relative_key: str,
    raw_path: Path,
    profile,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    row: dict[str, Any] = {
        "raw_relative_physical_key": relative_key,
        "raw_absolute_path": str(raw_path.resolve(strict=False)),
        "raw_locator": None,
        "raw_numeric_id_locator_metadata": extract_numeric_frame_id(relative_key),
        "p4_identity": {
            "scheme": None,
            "native_identity": None,
            "status": "NOT_READ",
        },
        "date_obs": None,
        "camera": {"value": None, "value_status": "NOT_READ", "profile_support": "UNDECLARED"},
        "filter": {"raw": None, "normalized": None, "photometric_capability": "UNCONFIGURED"},
        "image_type": {"raw": None, "normalized": None, "status": "NOT_READ"},
        "assets": [],
        "technical_stage1_eligibility": "BLOCKED",
        "eligibility_basis": (
            "required asset files and readable raw/processing headers are prerequisites only; "
            "reduction compatibility/science is not validated"
        ),
        "errors": errors,
    }

    match = _RAW_RELATIVE_RE.fullmatch(relative_key)
    if match is None or not _valid_archive_date(match):
        _add_error(errors, "MALFORMED_RAW_RELATIVE_PATH")
        row["p4_identity"]["status"] = "NOT_EVALUATED"
        row["assets"] = [
            _asset_result(role, None, None, "MALFORMED_RAW_RELATIVE_PATH")
            for role in MASTER_PHYSICAL_REQUIRED_ROLES
        ]
        return row

    raw_locator = "/master/imdata/" + relative_key.replace("\\", "/")
    row["raw_locator"] = raw_locator
    raw_asset = _resolve_asset_locator("science_raw", raw_locator, root)
    row["assets"].append(raw_asset)
    if raw_asset["status"] != "RESOLVED":
        _add_error(errors, raw_asset["status"])
        row["p4_identity"]["status"] = "NOT_READ"
        row["assets"].extend(
            _asset_result(role, None, None, "NOT_EVALUATED")
            for role in MASTER_PHYSICAL_REQUIRED_ROLES[1:]
        )
        return row

    try:
        raw_header = fits.getheader(raw_asset["absolute_path"], ext=0)
    except Exception as exc:  # noqa: BLE001 - record and continue with the next physical file.
        raw_asset["status"] = "READ_ERROR"
        _add_error(errors, "RAW_HEADER_READ_ERROR", error_type=type(exc).__name__)
        row["p4_identity"]["status"] = "NOT_READ"
        row["assets"].extend(
            _asset_result(role, None, None, "NOT_EVALUATED")
            for role in MASTER_PHYSICAL_REQUIRED_ROLES[1:]
        )
        return row

    detected = _detected_file_from_header(raw_path, raw_header, profile)
    row["p4_identity"] = {
        "scheme": detected.identity_scheme,
        "native_identity": detected.native_identity,
        "status": detected.observation_identity_status,
        "detail": detected.observation_identity_detail,
    }
    row["date_obs"] = detected.date_obs.isoformat() if detected.date_obs is not None else None
    camera_raw = profile.get_header_value(raw_header, "camera")
    camera_value = None if camera_raw in (None, "") else str(camera_raw).strip()
    row["camera"] = {
        "value": camera_value,
        "value_status": "PRESENT" if camera_value else "MISSING",
        "profile_support": "UNDECLARED",
        "audit_context_203_204": (
            "MATCHES_AUDIT_CONTEXT_ONLY" if camera_value in {"203", "204"} else "NOT_AUDIT_CONTEXT_MATCH"
        ),
    }
    filter_raw = profile.get_header_value(raw_header, "filter")
    filter_normalized = profile.normalize_filter(filter_raw)
    row["filter"] = {
        "raw": None if filter_raw in (None, "") else str(filter_raw).strip(),
        "normalized": filter_normalized,
        "photometric_capability": _photometric_capability(profile, filter_normalized),
    }
    image_raw = profile.get_header_value(raw_header, "image_type")
    image_normalized = profile.normalize_image_type(image_raw)
    row["image_type"] = {
        "raw": None if image_raw in (None, "") else str(image_raw).strip(),
        "normalized": image_normalized,
        "status": "NORMALIZED" if image_normalized != "unknown" else "UNKNOWN_OR_UNCONFIGURED",
    }

    header_locator = _infer_master_header_locator(raw_locator)
    if header_locator is None:
        header_asset = _asset_result(
            "processing_header", None, None, "MALFORMED_HEADER_LOCATOR"
        )
    else:
        header_asset = _resolve_asset_locator("processing_header", header_locator, root)
    row["assets"].append(header_asset)
    processing_header = None
    if header_asset["status"] != "RESOLVED":
        _add_error(errors, header_asset["status"], role="processing_header")
    else:
        try:
            processing_header = fits.getheader(header_asset["absolute_path"], ext=0)
        except Exception as exc:  # noqa: BLE001 - keep the inventory moving.
            header_asset["status"] = "READ_ERROR"
            _add_error(errors, "PROCESSING_HEADER_READ_ERROR", error_type=type(exc).__name__)

    for role, profile_field in (("dark", "dark_path"), ("flat", "flat_path")):
        locator_value = (
            profile.get_header_value(processing_header, profile_field)
            if processing_header is not None
            else None
        )
        if locator_value in (None, ""):
            asset = _asset_result(role, None, None, "MISSING_LOCATOR")
        else:
            asset = _resolve_asset_locator(role, str(locator_value).strip(), root)
        row["assets"].append(asset)
        if asset["status"] != "RESOLVED":
            _add_error(errors, asset["status"], role=role)

    if row["p4_identity"]["status"] != "READY":
        _add_error(errors, "P4_IDENTITY_UNAVAILABLE")
    if image_normalized == "unknown":
        _add_error(errors, "IMAGE_TYPE_UNKNOWN")

    required_assets_resolved = all(
        asset["status"] == "RESOLVED" for asset in row["assets"]
    )
    headers_readable = all(
        asset["status"] == "RESOLVED"
        for asset in row["assets"][:2]
    )
    if (
        image_normalized == "science"
        and row["p4_identity"]["status"] == "READY"
        and required_assets_resolved
        and headers_readable
    ):
        row["technical_stage1_eligibility"] = "ELIGIBLE_TO_ATTEMPT"
    elif image_normalized in {"dark", "flat", "bias"}:
        row["technical_stage1_eligibility"] = "NOT_A_SCIENCE_INPUT"
    return row


def _resolve_asset_locator(role: str, locator: str, root: Path) -> dict[str, Any]:
    expected = _HEADER_LOCATOR_RE if role == "processing_header" else _RAW_LOCATOR_RE
    if not expected.fullmatch(locator):
        return _asset_result(role, None, None, "MALFORMED_LOCATOR")
    pure = PurePosixPath(locator)
    relative_parts = pure.parts[3:]
    if pure.parts[:3] != ("/", "master", "imdata") or not relative_parts:
        return _asset_result(role, locator, None, "MALFORMED_LOCATOR")
    candidate = root.joinpath(*relative_parts)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return _asset_result(role, locator, None, "LOCATOR_RESOLUTION_ERROR")
    if not _is_within(resolved, root):
        return _asset_result(role, locator, None, "LOCATOR_ESCAPES_ROOT")
    if not resolved.is_file():
        return _asset_result(role, locator, str(resolved), "ASSET_NOT_FOUND")
    return _asset_result(role, locator, str(resolved), "RESOLVED")


def _asset_result(
    role: str,
    locator: str | None,
    absolute_path: str | None,
    status: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "locator": locator,
        "absolute_path": absolute_path,
        "status": status,
    }


def _photometric_capability(profile, normalized_filter: str | None) -> str:
    raw = profile.raw_config if isinstance(profile.raw_config, dict) else {}
    calibration = raw.get("photometric_calibration")
    if not isinstance(calibration, dict) or calibration.get("enabled") is not True:
        return "DISABLED"
    filters = calibration.get("filters")
    if not isinstance(filters, dict) or normalized_filter is None:
        return "UNCONFIGURED"
    filter_config = filters.get(normalized_filter)
    if not isinstance(filter_config, dict):
        return "UNCONFIGURED"
    enabled = filter_config.get("enabled")
    if enabled is True:
        return "ENABLED"
    if enabled is False:
        return "DISABLED"
    return "UNCONFIGURED"


def _valid_archive_date(match: re.Match[str]) -> bool:
    try:
        date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError:
        return False
    return True


def _infer_master_header_locator(raw_locator: str | None) -> str | None:
    """Map only a canonical MASTER raw locator to its standard header locator."""

    if not raw_locator or not raw_locator.startswith("/master/imdata/"):
        return None
    relative_key = raw_locator.removeprefix("/master/imdata/")
    match = _RAW_RELATIVE_RE.fullmatch(relative_key)
    if match is None or not _valid_archive_date(match):
        return None
    return (
        f"/master/imdata/procdata/{match.group('year')}/{match.group('month')}/"
        f"{match.group('day')}/{match.group('raw_id')}_header.fits"
    )


def _add_error(errors: list[dict[str, str]], code: str, **details: str) -> None:
    error = {"code": code}
    error.update(details)
    errors.append(error)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "DEFAULT_MAX_MASTER_PHYSICAL_FILES",
    "MASTER_PHYSICAL_REQUIRED_ROLES",
    "MAX_MASTER_PHYSICAL_FILES",
    "MasterPhysicalAssetProvider",
    "MasterPhysicalMetadataProvider",
    "probe_master_physical_archive",
]
