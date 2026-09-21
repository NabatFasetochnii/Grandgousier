from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from grandgousier import __version__
from grandgousier.archive.disk_guard import (
    DEFAULT_STAGE1_DISK_RESERVE_BYTES,
    DEFAULT_UNKNOWN_PRODUCT_BYTES,
    DiskRequirement,
    STAGE1_METADATA_OVERHEAD_BYTES,
    check_disk_budget,
)
from grandgousier.archive.manifest import extract_numeric_frame_id, infer_date_parts, manifest_path_to_local

REDUCTION_ALGORITHM = "MASTER_DARK_FLAT_V1"
DQ_SCHEMA = "MASTER_DQ_V2"
DQ_RAW_INVALID = np.uint8(1 << 0)
DQ_DARK_INVALID = np.uint8(1 << 1)
DQ_FLAT_INVALID = np.uint8(1 << 2)
DQ_RAW_SATURATED = np.uint8(1 << 3)
DQ_CALIBRATED_INVALID = np.uint8(1 << 4)

REDUCTION_REPORT_COLUMNS = [
    "processed_frame_id",
    "raw_id",
    "raw_path",
    "header_path",
    "dark_path",
    "flat_path",
    "output_path",
    "status",
    "n_pixels",
    "n_invalid_flat",
    "n_saturated",
    "calibrated_median_adu",
    "dark_temperature_delta_c",
    "raw_deleted",
    "raw_deleted_bytes",
    "error",
]

REDUCTION_METRIC_KEYS = {
    "n_pixels": "GGNPIX",
    "n_invalid_flat": "GGNIFLAT",
    "n_saturated": "GGNSAT",
    "calibrated_median_adu": "GGMEDADU",
    "dark_temperature_delta_c": "GGDTEMP",
}


@dataclass(frozen=True)
class MasterReductionTask:
    processed_frame_id: str | None
    raw_id: int
    raw_remote_path: str
    header_remote_path: str
    dark_remote_path: str
    flat_remote_path: str
    raw_local_path: Path
    header_local_path: Path
    dark_local_path: Path
    flat_local_path: Path
    output_path: Path


@dataclass(frozen=True)
class MasterReductionResult:
    task: MasterReductionTask
    status: str
    n_pixels: int | None = None
    n_invalid_flat: int | None = None
    n_saturated: int | None = None
    calibrated_median_adu: float | None = None
    dark_temperature_delta_c: float | None = None
    raw_deleted: bool = False
    raw_deleted_bytes: int | None = None
    error: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "processed_frame_id": self.task.processed_frame_id,
            "raw_id": self.task.raw_id,
            "raw_path": self.task.raw_remote_path,
            "header_path": self.task.header_remote_path,
            "dark_path": self.task.dark_remote_path,
            "flat_path": self.task.flat_remote_path,
            "output_path": str(self.task.output_path),
            "status": self.status,
            "n_pixels": self.n_pixels,
            "n_invalid_flat": self.n_invalid_flat,
            "n_saturated": self.n_saturated,
            "calibrated_median_adu": self.calibrated_median_adu,
            "dark_temperature_delta_c": self.dark_temperature_delta_c,
            "raw_deleted": self.raw_deleted,
            "raw_deleted_bytes": self.raw_deleted_bytes,
            "error": self.error,
        }


def _clean_remote_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("master/"):
        text = "/" + text
    return text


def _output_path_for_raw(raw_remote_path: str, output_root: str | Path, raw_id: int) -> Path:
    date_parts = infer_date_parts(_clean_remote_path(raw_remote_path))
    if date_parts is not None:
        return Path(output_root).joinpath(*date_parts, f"{raw_id}_processed.fits")
    return Path(output_root) / f"{raw_id}_processed.fits"


def build_master_reduction_tasks(
    rows: Sequence[Mapping[str, object]],
    *,
    cache_root: str | Path,
    output_root: str | Path | None = None,
    limit: int | None = None,
) -> list[MasterReductionTask]:
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    resolved_output_root = Path(output_root) if output_root is not None else Path(cache_root) / "processed"
    if limit == 0:
        return []
    tasks: list[MasterReductionTask] = []
    by_raw: dict[str, MasterReductionTask] = {}

    for row in rows:
        raw = _clean_remote_path(row.get("raw_path"))
        header = _clean_remote_path(row.get("header_path"))
        dark = _clean_remote_path(row.get("dark_path"))
        flat = _clean_remote_path(row.get("flat_path"))
        missing = [name for name, value in (("raw", raw), ("header", header), ("dark", dark), ("flat", flat)) if not value]
        if missing:
            frame = row.get("processed_frame_id") or row.get("raw_id") or "?"
            raise ValueError(
                f"MASTER reduction precondition failed for frame {frame}: missing {','.join(missing)} path(s); "
                "provide a complete prepared reduction manifest"
            )
        raw_id = extract_numeric_frame_id(raw)
        if raw_id is None:
            raise ValueError(f"Cannot extract MASTER raw id from {raw!r}")
        task = MasterReductionTask(
            processed_frame_id=str(row.get("processed_frame_id") or "") or None,
            raw_id=raw_id,
            raw_remote_path=raw,
            header_remote_path=header,
            dark_remote_path=dark,
            flat_remote_path=flat,
            raw_local_path=manifest_path_to_local(raw, cache_root),
            header_local_path=manifest_path_to_local(header, cache_root),
            dark_local_path=manifest_path_to_local(dark, cache_root),
            flat_local_path=manifest_path_to_local(flat, cache_root),
            output_path=_output_path_for_raw(raw, resolved_output_root, raw_id),
        )
        previous = by_raw.get(raw)
        if previous is not None:
            if (
                previous.header_remote_path != task.header_remote_path
                or previous.dark_remote_path != task.dark_remote_path
                or previous.flat_remote_path != task.flat_remote_path
            ):
                raise ValueError(f"Conflicting calibration references for repeated raw frame {raw}")
            continue
        by_raw[raw] = task
        tasks.append(task)
        if limit is not None and len(tasks) >= limit:
            break
    return tasks


def _header_text(header: Mapping[str, object], key: str) -> str:
    return str(header.get(key, "") or "").strip()


def _header_float(header: Mapping[str, object], key: str) -> float | None:
    value = header.get(key)
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _same_text(a: object, b: object) -> bool:
    return str(a or "").strip().upper() == str(b or "").strip().upper()


def _assert_equal_header_value(
    raw_header: Mapping[str, object],
    other_header: Mapping[str, object],
    key: str,
    *,
    label: str,
    allow_blank_other: bool = False,
) -> None:
    raw_value = raw_header.get(key)
    other_value = other_header.get(key)
    raw_text = str(raw_value or "").strip()
    other_text = str(other_value or "").strip()
    if allow_blank_other and not other_text:
        return
    if raw_text and not other_text:
        raise ValueError(f"{label} {key} is missing; raw={raw_value!r}")
    if raw_text and other_text and not _same_text(raw_value, other_value):
        raise ValueError(f"{label} {key} mismatch: raw={raw_value!r}, {label.lower()}={other_value!r}")


def validate_master_reduction_inputs(
    *,
    task: MasterReductionTask,
    raw_data: np.ndarray,
    dark_data: np.ndarray,
    flat_data: np.ndarray,
    raw_header: Mapping[str, object],
    dark_header: Mapping[str, object],
    flat_header: Mapping[str, object],
    master_header: Mapping[str, object],
    max_dark_temperature_delta_c: float | None = 5.0,
) -> float | None:
    if raw_data.ndim != 2:
        raise ValueError(f"raw frame must be 2-D, got shape={raw_data.shape}")
    if dark_data.shape != raw_data.shape:
        raise ValueError(f"dark geometry mismatch: raw={raw_data.shape}, dark={dark_data.shape}")
    if flat_data.shape != raw_data.shape:
        raise ValueError(f"flat geometry mismatch: raw={raw_data.shape}, flat={flat_data.shape}")

    raw_type = _header_text(raw_header, "IMAGETYP").upper()
    dark_type = _header_text(dark_header, "IMAGETYP").upper()
    flat_type = _header_text(flat_header, "IMAGETYP").upper()
    if raw_type not in {"LIGHT", "OBJECT"}:
        raise ValueError(f"raw IMAGETYP must be LIGHT/OBJECT, got {raw_type!r}")
    if dark_type != "SDARK":
        raise ValueError(f"dark IMAGETYP must be SDARK, got {dark_type!r}")
    if flat_type != "SFLAT":
        raise ValueError(f"flat IMAGETYP must be SFLAT, got {flat_type!r}")

    for key in ("ID_CAM", "XBINNING", "YBINNING"):
        _assert_equal_header_value(raw_header, dark_header, key, label="dark")
        _assert_equal_header_value(raw_header, flat_header, key, label="flat")
        _assert_equal_header_value(raw_header, master_header, key, label="header")
    _assert_equal_header_value(raw_header, flat_header, "FILTER", label="flat")
    _assert_equal_header_value(raw_header, master_header, "FILTER", label="header")

    raw_exptime = _header_float(raw_header, "EXPTIME")
    dark_exptime = _header_float(dark_header, "EXPTIME")
    header_exptime = _header_float(master_header, "EXPTIME")
    if raw_exptime is None or dark_exptime is None:
        raise ValueError(f"raw/dark EXPTIME is required, got raw={raw_header.get('EXPTIME')!r}, dark={dark_header.get('EXPTIME')!r}")
    tolerance = max(1.0e-3, 1.0e-6 * max(abs(raw_exptime), abs(dark_exptime), 1.0))
    if abs(raw_exptime - dark_exptime) > tolerance:
        raise ValueError(
            f"dark EXPTIME mismatch: raw={raw_exptime:g}s, dark={dark_exptime:g}s; "
            "automatic dark scaling is intentionally disabled"
        )
    if header_exptime is None:
        raise ValueError("processing header EXPTIME is required")
    if abs(raw_exptime - header_exptime) > 1.0e-3:
        raise ValueError(f"header EXPTIME mismatch: raw={raw_exptime:g}s, header={header_exptime:g}s")

    imagew = master_header.get("IMAGEW")
    imageh = master_header.get("IMAGEH")
    if imagew not in (None, "") and int(imagew) != raw_data.shape[1]:
        raise ValueError(f"header IMAGEW mismatch: raw={raw_data.shape[1]}, header={imagew}")
    if imageh not in (None, "") and int(imageh) != raw_data.shape[0]:
        raise ValueError(f"header IMAGEH mismatch: raw={raw_data.shape[0]}, header={imageh}")

    required_wcs = ("CTYPE1", "CTYPE2", "CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2")
    missing_wcs = [key for key in required_wcs if master_header.get(key) in (None, "")]
    if missing_wcs:
        raise ValueError(f"processing header is missing solved WCS keys: {','.join(missing_wcs)}")
    has_cd = all(master_header.get(key) not in (None, "") for key in ("CD1_1", "CD1_2", "CD2_1", "CD2_2"))
    has_cdelt = all(master_header.get(key) not in (None, "") for key in ("CDELT1", "CDELT2"))
    if not has_cd and not has_cdelt:
        raise ValueError("processing header solved WCS has neither complete CD matrix nor CDELT1/2")

    header_dark = _clean_remote_path(master_header.get("DARK"))
    header_flat = _clean_remote_path(master_header.get("FLAT"))
    if header_dark and header_dark != _clean_remote_path(task.dark_remote_path):
        raise ValueError(f"header DARK reference mismatch: manifest={task.dark_remote_path!r}, header={header_dark!r}")
    if header_flat and header_flat != _clean_remote_path(task.flat_remote_path):
        raise ValueError(f"header FLAT reference mismatch: manifest={task.flat_remote_path!r}, header={header_flat!r}")

    raw_temp = _header_float(raw_header, "CCD-TEMP")
    dark_temp = _header_float(dark_header, "CCD-TEMP")
    temp_delta = None if raw_temp is None or dark_temp is None else abs(raw_temp - dark_temp)
    if (
        temp_delta is not None
        and max_dark_temperature_delta_c is not None
        and max_dark_temperature_delta_c >= 0
        and temp_delta > max_dark_temperature_delta_c
    ):
        raise ValueError(
            f"dark CCD-TEMP mismatch: |raw-dark|={temp_delta:.3f} C exceeds "
            f"{max_dark_temperature_delta_c:.3f} C"
        )
    return temp_delta


def physical_saturation_level(header: Mapping[str, object]) -> float | None:
    try:
        bitpix = int(header.get("BITPIX", 0) or 0)
    except (TypeError, ValueError):
        return None
    if bitpix not in {8, 16, 32, 64}:
        return None
    bscale = _header_float(header, "BSCALE")
    bzero = _header_float(header, "BZERO")
    bscale = 1.0 if bscale is None else bscale
    bzero = 0.0 if bzero is None else bzero
    if bitpix == 8:
        stored_max = 255.0
    else:
        stored_max = float((1 << (bitpix - 1)) - 1)
    if bscale <= 0:
        return None
    return bzero + bscale * stored_max


def calibrate_master_arrays(
    raw_data: np.ndarray,
    dark_data: np.ndarray,
    flat_data: np.ndarray,
    *,
    saturation_level: float | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, int | float | None]]:
    raw = np.asarray(raw_data)
    dark = np.asarray(dark_data)
    flat = np.asarray(flat_data)
    if raw.shape != dark.shape or raw.shape != flat.shape:
        raise ValueError("raw/dark/flat shapes must match")

    raw_invalid = ~np.isfinite(raw)
    dark_invalid = ~np.isfinite(dark)
    flat_invalid = ~np.isfinite(flat) | (flat <= 0)
    valid = ~(raw_invalid | dark_invalid | flat_invalid)
    if not np.any(valid):
        raise ValueError("no valid pixels remain after raw/dark/flat validity checks")

    calibrated = np.asarray(raw, dtype=np.float32).copy()
    calibrated -= np.asarray(dark, dtype=np.float32)
    np.divide(calibrated, np.asarray(flat, dtype=np.float32), out=calibrated, where=~flat_invalid)
    calibrated[raw_invalid | dark_invalid | flat_invalid] = np.nan

    dq = np.zeros(raw.shape, dtype=np.uint8)
    dq[raw_invalid] |= DQ_RAW_INVALID
    dq[dark_invalid] |= DQ_DARK_INVALID
    dq[flat_invalid] |= DQ_FLAT_INVALID
    saturated = np.zeros(raw.shape, dtype=bool)
    if saturation_level is not None and math.isfinite(float(saturation_level)):
        saturated = np.asarray(raw, dtype=np.float64) >= float(saturation_level)
        dq[saturated] |= DQ_RAW_SATURATED
    calibrated_invalid = ~np.isfinite(calibrated)
    dq[calibrated_invalid] |= DQ_CALIBRATED_INVALID

    median = float(np.nanmedian(calibrated)) if np.any(np.isfinite(calibrated)) else math.nan
    stats: dict[str, int | float | None] = {
        "n_pixels": int(raw.size),
        "n_invalid_flat": int(np.count_nonzero(flat_invalid)),
        "n_saturated": int(np.count_nonzero(saturated)),
        "calibrated_median_adu": median,
    }
    return calibrated, dq, stats


def _is_wcs_keyword(key: str) -> bool:
    normalized = str(key or "").strip().upper()
    if normalized in {"WCSAXES", "LONPOLE", "LATPOLE", "RADESYS", "RADECSYS", "EQUINOX"}:
        return True
    return bool(
        re.match(
            r"^(?:CTYPE\d+|CRPIX\d+|CRVAL\d+|CDELT\d+|CROTA\d+|CUNIT\d+|CD\d+_\d+|PC\d+_\d+|PV\d+_\d+|PS\d+_\d+|"
            r"A_ORDER|B_ORDER|AP_ORDER|BP_ORDER|A_\d+_\d+|B_\d+_\d+|AP_\d+_\d+|BP_\d+_\d+)$",
            normalized,
        )
    )


def _copy_processing_header(raw_header, master_header):
    # Import-time dependency on astropy is intentionally avoided; this helper operates on Header-like objects.
    output = raw_header.copy()
    # The raw frame carries an approximate telescope WCS.  Remove it completely before overlaying
    # the solved TAN-SIP WCS from *_header.fits so stale CDELT/CROTA/PC cards cannot coexist with CD/SIP.
    if master_header.get("CTYPE1") and master_header.get("CTYPE2"):
        for key in list(output.keys()):
            if _is_wcs_keyword(key):
                del output[key]
    structural = {
        "SIMPLE",
        "BITPIX",
        "NAXIS",
        "NAXIS1",
        "NAXIS2",
        "NAXIS3",
        "EXTEND",
        "BZERO",
        "BSCALE",
        "BLANK",
        "CHECKSUM",
        "DATASUM",
        "DATE",
    }
    master_date = master_header.get("DATE")
    for card in getattr(master_header, "cards", []):
        key = str(card.keyword or "").strip()
        if not key or key in structural or key in {"COMMENT", "HISTORY", "END"}:
            continue
        try:
            output[key] = (card.value, card.comment)
        except Exception:
            output[key] = card.value
    for key in ("BZERO", "BSCALE", "BLANK", "CHECKSUM", "DATASUM"):
        if key in output:
            del output[key]
    if master_date:
        output["MSHDRDAT"] = (str(master_date), "MASTER processing-header creation date")
    output["DATE"] = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "Grandgousier output creation UTC")
    output["GGSTAGE"] = ("1B", "Grandgousier processing stage")
    output["GGALG"] = (REDUCTION_ALGORITHM, "Grandgousier reduction algorithm")
    output["GGVER"] = (__version__, "Grandgousier version")
    output["GGDQSCH"] = (DQ_SCHEMA, "DQ extension bit schema")
    output["GGDQDT"] = ("UINT8", "DQ storage dtype")
    output["GGMSMETA"] = (True, "MASTER metadata copied")
    return output


def _write_reduction_metrics(header, *, stats: Mapping[str, int | float | None], temp_delta: float | None) -> None:
    header[REDUCTION_METRIC_KEYS["n_pixels"]] = (int(stats["n_pixels"]), "Stage 1B pixel count")
    header[REDUCTION_METRIC_KEYS["n_invalid_flat"]] = (int(stats["n_invalid_flat"]), "Invalid flat pixels")
    header[REDUCTION_METRIC_KEYS["n_saturated"]] = (int(stats["n_saturated"]), "Raw saturated pixels")
    header[REDUCTION_METRIC_KEYS["calibrated_median_adu"]] = (
        float(stats["calibrated_median_adu"]),
        "Calibrated median ADU",
    )
    if temp_delta is not None and math.isfinite(float(temp_delta)):
        header[REDUCTION_METRIC_KEYS["dark_temperature_delta_c"]] = (float(temp_delta), "Abs raw-dark temp C")
    elif REDUCTION_METRIC_KEYS["dark_temperature_delta_c"] in header:
        del header[REDUCTION_METRIC_KEYS["dark_temperature_delta_c"]]


def reduction_metrics_from_header(header: Mapping[str, object]) -> dict[str, int | float | None] | None:
    required = (
        REDUCTION_METRIC_KEYS["n_pixels"],
        REDUCTION_METRIC_KEYS["n_invalid_flat"],
        REDUCTION_METRIC_KEYS["n_saturated"],
        REDUCTION_METRIC_KEYS["calibrated_median_adu"],
    )
    if any(header.get(key) in (None, "") for key in required):
        return None
    try:
        n_pixels = int(header[REDUCTION_METRIC_KEYS["n_pixels"]])
        n_invalid_flat = int(header[REDUCTION_METRIC_KEYS["n_invalid_flat"]])
        n_saturated = int(header[REDUCTION_METRIC_KEYS["n_saturated"]])
        calibrated_median_adu = float(header[REDUCTION_METRIC_KEYS["calibrated_median_adu"]])
    except (TypeError, ValueError, KeyError):
        return None
    if (
        n_pixels < 0
        or n_invalid_flat < 0
        or n_saturated < 0
        or n_invalid_flat > n_pixels
        or n_saturated > n_pixels
        or not math.isfinite(calibrated_median_adu)
    ):
        return None
    temp_delta = _header_float(header, REDUCTION_METRIC_KEYS["dark_temperature_delta_c"])
    return {
        "n_pixels": n_pixels,
        "n_invalid_flat": n_invalid_flat,
        "n_saturated": n_saturated,
        "calibrated_median_adu": calibrated_median_adu,
        "dark_temperature_delta_c": temp_delta,
    }


def _existing_output_result(task: MasterReductionTask) -> MasterReductionResult:
    fits = _require_astropy_fits()
    try:
        header = fits.getheader(task.output_path, ext=0)
    except Exception as exc:
        return MasterReductionResult(
            task=task,
            status="FAILED",
            error=f"cannot read existing processed FITS header: {type(exc).__name__}: {exc}",
        )

    stage = str(header.get("GGSTAGE", "") or "").strip()
    if stage != "1B":
        return MasterReductionResult(
            task=task,
            status="FAILED",
            error=f"existing processed FITS GGSTAGE mismatch: expected='1B', actual={stage!r}",
        )
    algorithm = str(header.get("GGALG", "") or "").strip()
    if algorithm != REDUCTION_ALGORITHM:
        return MasterReductionResult(
            task=task,
            status="FAILED",
            error=f"existing processed FITS GGALG mismatch: expected={REDUCTION_ALGORITHM!r}, actual={algorithm!r}",
        )

    provenance_checks = (
        ("GGRAW", task.raw_remote_path),
        ("GGHDR", task.header_remote_path),
        ("DARK", task.dark_remote_path),
        ("FLAT", task.flat_remote_path),
    )
    for key, expected in provenance_checks:
        actual = _clean_remote_path(header.get(key))
        if not actual:
            return MasterReductionResult(
                task=task,
                status="FAILED",
                error=f"existing processed FITS is missing required provenance card {key}",
            )
        if actual != _clean_remote_path(expected):
            return MasterReductionResult(
                task=task,
                status="FAILED",
                error=(
                    f"existing processed FITS {key} mismatch: expected={expected!r}, actual={actual!r}; "
                    "use --overwrite only after review"
                ),
            )

    metrics = reduction_metrics_from_header(header)
    if metrics is None:
        return MasterReductionResult(task=task, status="SKIPPED_EXISTING")
    return MasterReductionResult(task=task, status="SKIPPED_EXISTING", **metrics)


def inspect_existing_master_output(task: MasterReductionTask) -> MasterReductionResult:
    """Validate an existing Stage-1B output without loading science/DQ arrays."""
    if not task.output_path.is_file() or task.output_path.stat().st_size <= 0:
        return MasterReductionResult(task=task, status="FAILED", error="processed Stage-1B FITS is missing")
    return _existing_output_result(task)


def compact_existing_master_output(task: MasterReductionTask) -> MasterReductionResult:
    """Rewrite a validated Stage-1B output with uint8 DQ without recalibrating pixels.

    This is intentionally independent of the raw/cache inputs so a large legacy
    uint16-DQ archive can be compacted after raw data have been pruned.
    """
    existing = _existing_output_result(task)
    if existing.status == "FAILED":
        return existing

    fits = _require_astropy_fits()
    temp_path = task.output_path.with_name(task.output_path.name + ".compact.part")
    try:
        check_disk_budget(
            (
                DiskRequirement(
                    target=temp_path,
                    required_bytes=max(
                        DEFAULT_UNKNOWN_PRODUCT_BYTES,
                        int(task.output_path.stat().st_size),
                    ),
                    label="MASTER compacted product",
                ),
                DiskRequirement(
                    target=task.output_path,
                    required_bytes=STAGE1_METADATA_OVERHEAD_BYTES,
                    label="MASTER compact metadata",
                ),
            ),
            reserve_bytes=DEFAULT_STAGE1_DISK_RESERVE_BYTES,
        )
        with fits.open(task.output_path, memmap=False, uint=True) as hdul:
            if len(hdul) < 2 or str(hdul[1].name or "").upper() != "DQ" or hdul[1].data is None:
                raise ValueError("processed FITS must contain a DQ image extension")
            dq = np.asarray(hdul[1].data)
            if np.any(~np.isfinite(dq)):
                raise ValueError("DQ contains non-finite values")
            if np.any(dq < 0) or np.any(dq > 255):
                raise ValueError("DQ values do not fit uint8")
            already_compact = dq.dtype == np.dtype(np.uint8) and str(hdul[0].header.get("GGDQSCH", "")) == DQ_SCHEMA
            if already_compact:
                return existing

            primary_header = hdul[0].header.copy()
            primary_header["GGDQSCH"] = (DQ_SCHEMA, "DQ extension bit schema")
            primary_header["GGDQDT"] = ("UINT8", "DQ storage dtype")
            primary_header["GGVER"] = (__version__, "Grandgousier version")
            primary_data = hdul[0].data
            dq_header = hdul[1].header.copy()
            dq8 = np.asarray(dq, dtype=np.uint8)

            if temp_path.exists():
                temp_path.unlink()
            out = fits.HDUList(
                [
                    fits.PrimaryHDU(data=primary_data, header=primary_header),
                    fits.ImageHDU(data=dq8, header=dq_header, name="DQ"),
                ]
            )
            out.writeto(temp_path, overwrite=True, checksum=True, output_verify="exception")
        os.replace(temp_path, task.output_path)
        return replace(existing, status="COMPACTED_EXISTING")
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return MasterReductionResult(task=task, status="FAILED", error=f"compact failed: {type(exc).__name__}: {exc}")


def delete_master_raw_after_valid_output(task: MasterReductionTask) -> tuple[bool, int]:
    """Delete one science raw only after validating the corresponding processed FITS."""
    existing = _existing_output_result(task)
    if existing.status == "FAILED":
        raise ValueError(existing.error or "processed FITS validation failed")
    if not task.raw_local_path.exists():
        return False, 0
    size = int(task.raw_local_path.stat().st_size)
    task.raw_local_path.unlink()
    return True, size


def _require_astropy_fits():
    try:
        from astropy.io import fits
    except ImportError as exc:  # pragma: no cover - project dependency, environment guard.
        raise RuntimeError("astropy is required for master-archive-reduce") from exc
    return fits


def reduce_master_task(
    task: MasterReductionTask,
    *,
    overwrite: bool = False,
    max_dark_temperature_delta_c: float | None = 5.0,
    disk_reserve_bytes: int | str | None = None,
) -> MasterReductionResult:
    if task.output_path.exists() and task.output_path.stat().st_size > 0 and not overwrite:
        return _existing_output_result(task)
    for label, path in (
        ("raw", task.raw_local_path),
        ("header", task.header_local_path),
        ("dark", task.dark_local_path),
        ("flat", task.flat_local_path),
    ):
        if not path.is_file() or path.stat().st_size <= 0:
            return MasterReductionResult(task=task, status="FAILED", error=f"missing local {label}: {path}")

    fits = _require_astropy_fits()
    try:
        check_disk_budget(
            (
                DiskRequirement(
                    target=task.output_path,
                    required_bytes=max(
                        DEFAULT_UNKNOWN_PRODUCT_BYTES,
                        max(int(path.stat().st_size) for path in (
                            task.raw_local_path,
                            task.header_local_path,
                            task.dark_local_path,
                            task.flat_local_path,
                        )),
                    ),
                    label="MASTER reduced product",
                ),
            ),
            reserve_bytes=(
                DEFAULT_STAGE1_DISK_RESERVE_BYTES
                if disk_reserve_bytes is None
                else disk_reserve_bytes
            ),
        )
        with fits.open(task.raw_local_path, memmap=False, uint=True) as hdul:
            raw_header = hdul[0].header.copy()
            raw_data = np.array(hdul[0].data, copy=True)
        with fits.open(task.dark_local_path, memmap=False, uint=True) as hdul:
            dark_header = hdul[0].header.copy()
            dark_data = np.array(hdul[0].data, copy=True)
        with fits.open(task.flat_local_path, memmap=False, uint=True) as hdul:
            flat_header = hdul[0].header.copy()
            flat_data = np.array(hdul[0].data, copy=True)
        with fits.open(task.header_local_path, memmap=False, uint=True) as hdul:
            master_header = hdul[0].header.copy()
            if hdul[0].data is not None:
                raise ValueError("MASTER *_header.fits must not contain primary image data")

        temp_delta = validate_master_reduction_inputs(
            task=task,
            raw_data=raw_data,
            dark_data=dark_data,
            flat_data=flat_data,
            raw_header=raw_header,
            dark_header=dark_header,
            flat_header=flat_header,
            master_header=master_header,
            max_dark_temperature_delta_c=max_dark_temperature_delta_c,
        )
        calibrated, dq, stats = calibrate_master_arrays(
            raw_data,
            dark_data,
            flat_data,
            saturation_level=physical_saturation_level(raw_header),
        )
        out_header = _copy_processing_header(raw_header, master_header)
        out_header["DARK"] = task.dark_remote_path
        out_header["FLAT"] = task.flat_remote_path
        out_header["GGRAW"] = task.raw_remote_path
        out_header["GGHDR"] = task.header_remote_path
        out_header["GGDRKSC"] = (False, "Dark scaling applied")
        out_header["GGFLNRM"] = (False, "Additional flat normalization applied")
        _write_reduction_metrics(out_header, stats=stats, temp_delta=temp_delta)
        out_header.add_history("Grandgousier Stage 1B: calibrated=(raw-SDARK)/SFLAT")
        out_header.add_history("SDARK exposure must match raw; no automatic dark scaling")
        out_header.add_history("SFLAT is treated as already normalized; no renormalization")

        dq_header = fits.Header()
        dq_header["EXTNAME"] = "DQ"
        dq_header["DQBIT0"] = "RAW_INVALID"
        dq_header["DQBIT1"] = "DARK_INVALID"
        dq_header["DQBIT2"] = "FLAT_INVALID"
        dq_header["DQBIT3"] = "RAW_SATURATED"
        dq_header["DQBIT4"] = "CAL_INVALID"

        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = task.output_path.with_name(task.output_path.name + ".part")
        if temp_path.exists():
            temp_path.unlink()
        hdul_out = fits.HDUList(
            [
                fits.PrimaryHDU(data=calibrated.astype(np.float32, copy=False), header=out_header),
                fits.ImageHDU(data=dq, header=dq_header, name="DQ"),
            ]
        )
        hdul_out.writeto(temp_path, overwrite=True, checksum=True, output_verify="exception")
        os.replace(temp_path, task.output_path)
        return MasterReductionResult(
            task=task,
            status="REDUCED",
            n_pixels=int(stats["n_pixels"]),
            n_invalid_flat=int(stats["n_invalid_flat"]),
            n_saturated=int(stats["n_saturated"]),
            calibrated_median_adu=float(stats["calibrated_median_adu"]),
            dark_temperature_delta_c=temp_delta,
        )
    except Exception as exc:
        temp_path = task.output_path.with_name(task.output_path.name + ".part")
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return MasterReductionResult(task=task, status="FAILED", error=f"{type(exc).__name__}: {exc}")


def attach_raw_cleanup(result: MasterReductionResult) -> MasterReductionResult:
    """Delete raw for a successful/validated output and annotate the result."""
    if result.status not in {"REDUCED", "SKIPPED_EXISTING", "COMPACTED_EXISTING"}:
        return result
    try:
        deleted, size = delete_master_raw_after_valid_output(result.task)
    except Exception as exc:
        return replace(result, status="FAILED", error=f"raw cleanup failed: {type(exc).__name__}: {exc}")
    return replace(result, raw_deleted=deleted, raw_deleted_bytes=size)


def write_master_reduction_report(results: Sequence[MasterReductionResult], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REDUCTION_REPORT_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(result.as_row())
    return out
