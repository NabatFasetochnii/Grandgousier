from __future__ import annotations

import enum
import logging
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io.fits import Header
from astropy.wcs import FITSFixedWarning, WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.models import Frame, ProcessingEvent, ProcessingEventLevel
from grandgousier.io.fits_header import read_primary_header

logger = logging.getLogger(__name__)


class WcsStatus(str, enum.Enum):
    NOT_CHECKED = "NOT_CHECKED"
    HEADER_WCS_OK = "HEADER_WCS_OK"
    HEADER_WCS_MISSING = "HEADER_WCS_MISSING"
    HEADER_WCS_BAD = "HEADER_WCS_BAD"
    SOLVED = "SOLVED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WcsCheckResult:
    status: WcsStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    center_ra_deg: float | None = None
    center_dec_deg: float | None = None


@dataclass(frozen=True)
class WcsDbCheckResult:
    selected: int = 0
    checked: int = 0
    updated: int = 0
    ok: int = 0
    missing: int = 0
    bad: int = 0
    failed: int = 0
    skipped_missing_file: int = 0


_REQUIRED_WCS_KEYS = ("CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2")
_CD_KEYS = ("CD1_1", "CD1_2", "CD2_1", "CD2_2")
_PC_KEYS = ("PC1_1", "PC1_2", "PC2_1", "PC2_2")
_CDELT_KEYS = ("CDELT1", "CDELT2")


def check_header_wcs(
    header: Header,
    *,
    min_pixel_scale_arcsec: float = 0.02,
    max_pixel_scale_arcsec: float = 60.0,
) -> WcsCheckResult:
    """Validate celestial WCS stored in a FITS header.

    This is a header-level check. It verifies that the WCS is syntactically usable,
    celestial, produces a finite sky coordinate at the image center and has a
    plausible pixel scale. It does not yet solve astrometry or cross-match Gaia.
    """

    missing_keys = [key for key in _REQUIRED_WCS_KEYS if key not in header]
    if missing_keys:
        return WcsCheckResult(
            status=WcsStatus.HEADER_WCS_MISSING,
            message="Required WCS keywords are missing.",
            details={"missing_keys": missing_keys},
        )

    has_cd = all(key in header for key in _CD_KEYS)
    has_pc_cdelt = all(key in header for key in _PC_KEYS) and all(key in header for key in _CDELT_KEYS)
    has_cdelt_only = all(key in header for key in _CDELT_KEYS)
    if not (has_cd or has_pc_cdelt or has_cdelt_only):
        return WcsCheckResult(
            status=WcsStatus.HEADER_WCS_BAD,
            message="WCS has no usable linear transform: missing CD or PC+CDELT/CDELT.",
            details={
                "has_cd": has_cd,
                "has_pc_cdelt": has_pc_cdelt,
                "has_cdelt_only": has_cdelt_only,
            },
        )

    naxis1 = _to_int(header.get("NAXIS1"))
    naxis2 = _to_int(header.get("NAXIS2"))
    if naxis1 is None or naxis2 is None or naxis1 <= 0 or naxis2 <= 0:
        return WcsCheckResult(
            status=WcsStatus.HEADER_WCS_BAD,
            message="NAXIS1/NAXIS2 are missing or invalid.",
            details={"naxis1": header.get("NAXIS1"), "naxis2": header.get("NAXIS2")},
        )

    ctype1 = str(header.get("CTYPE1", "")).upper()
    ctype2 = str(header.get("CTYPE2", "")).upper()
    if "RA" not in ctype1 or "DEC" not in ctype2:
        return WcsCheckResult(
            status=WcsStatus.HEADER_WCS_BAD,
            message="WCS is not a standard RA/DEC celestial WCS.",
            details={"ctype1": header.get("CTYPE1"), "ctype2": header.get("CTYPE2")},
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            wcs = WCS(header)
        celestial = wcs.celestial
        if not celestial.has_celestial:
            return WcsCheckResult(
                status=WcsStatus.HEADER_WCS_BAD,
                message="Astropy WCS object has no celestial component.",
            )

        # astropy.wcs pixel_to_world_values expects zero-based pixel coordinates.
        center_x = (float(naxis1) - 1.0) / 2.0
        center_y = (float(naxis2) - 1.0) / 2.0
        center_ra_deg, center_dec_deg = celestial.pixel_to_world_values(center_x, center_y)
        center_ra_deg = _normalize_ra(center_ra_deg)
        center_dec_deg = float(center_dec_deg)

        if not (_is_finite(center_ra_deg) and _is_finite(center_dec_deg)):
            return WcsCheckResult(
                status=WcsStatus.HEADER_WCS_BAD,
                message="WCS does not produce finite sky coordinates at image center.",
                details={"center_x": center_x, "center_y": center_y},
            )
        if not (-90.0 <= center_dec_deg <= 90.0):
            return WcsCheckResult(
                status=WcsStatus.HEADER_WCS_BAD,
                message="WCS image-center Dec is outside valid range.",
                details={"center_dec_deg": center_dec_deg},
                center_ra_deg=center_ra_deg,
                center_dec_deg=center_dec_deg,
            )

        scale_deg = np.asarray(proj_plane_pixel_scales(celestial), dtype=float)
        scale_arcsec = np.abs(scale_deg) * 3600.0
        finite_scales = [float(v) for v in scale_arcsec if _is_finite(v)]
        if len(finite_scales) != 2:
            return WcsCheckResult(
                status=WcsStatus.HEADER_WCS_BAD,
                message="WCS pixel scale is not finite.",
                details={"pixel_scale_arcsec": _jsonify(scale_arcsec.tolist())},
                center_ra_deg=center_ra_deg,
                center_dec_deg=center_dec_deg,
            )

        mean_scale = float(np.mean(finite_scales))
        if mean_scale < min_pixel_scale_arcsec or mean_scale > max_pixel_scale_arcsec:
            return WcsCheckResult(
                status=WcsStatus.HEADER_WCS_BAD,
                message="WCS pixel scale is outside plausible bounds.",
                details={
                    "pixel_scale_arcsec_x": finite_scales[0],
                    "pixel_scale_arcsec_y": finite_scales[1],
                    "pixel_scale_arcsec_mean": mean_scale,
                    "min_pixel_scale_arcsec": min_pixel_scale_arcsec,
                    "max_pixel_scale_arcsec": max_pixel_scale_arcsec,
                },
                center_ra_deg=center_ra_deg,
                center_dec_deg=center_dec_deg,
            )

        details = {
            "naxis1": naxis1,
            "naxis2": naxis2,
            "center_x_pix_zero_based": center_x,
            "center_y_pix_zero_based": center_y,
            "center_ra_deg": center_ra_deg,
            "center_dec_deg": center_dec_deg,
            "crval1": _to_float(header.get("CRVAL1")),
            "crval2": _to_float(header.get("CRVAL2")),
            "crpix1": _to_float(header.get("CRPIX1")),
            "crpix2": _to_float(header.get("CRPIX2")),
            "ctype1": header.get("CTYPE1"),
            "ctype2": header.get("CTYPE2"),
            "has_cd": has_cd,
            "has_pc_cdelt": has_pc_cdelt,
            "has_cdelt_only": has_cdelt_only,
            "pixel_scale_arcsec_x": finite_scales[0],
            "pixel_scale_arcsec_y": finite_scales[1],
            "pixel_scale_arcsec_mean": mean_scale,
            "has_sip": _has_sip(header),
        }
        return WcsCheckResult(
            status=WcsStatus.HEADER_WCS_OK,
            message="Header WCS is usable.",
            details=_jsonify(details),
            center_ra_deg=center_ra_deg,
            center_dec_deg=center_dec_deg,
        )
    except Exception as exc:
        logger.exception("Header WCS check failed")
        return WcsCheckResult(
            status=WcsStatus.FAILED,
            message="Exception during WCS validation.",
            details={"error": repr(exc)},
        )


def check_frames_wcs(
    session: Session,
    *,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    current_status: str | None = "NOT_CHECKED",
    limit: int | None = None,
    dry_run: bool = False,
    update_center: bool = True,
    record_events: bool = False,
) -> WcsDbCheckResult:
    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids is not None:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument:
        stmt = stmt.where(Frame.instrument == instrument)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if current_status:
        stmt = stmt.where(Frame.wcs_status == current_status)
    if limit is not None:
        stmt = stmt.limit(limit)

    frames = list(session.scalars(stmt))
    selected = len(frames)
    checked = updated = ok = missing = bad = failed = skipped_missing_file = 0

    for frame in tqdm(frames, desc="Checking WCS", unit="frame"):
        checked += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result = WcsCheckResult(
                status=WcsStatus.FAILED,
                message="Raw FITS file is not accessible on this machine.",
                details={"raw_path": frame.raw_path},
            )
            skipped_missing_file += 1
        else:
            try:
                header = read_primary_header(path)
                result = check_header_wcs(header)
            except Exception as exc:
                result = WcsCheckResult(
                    status=WcsStatus.FAILED,
                    message="Could not read FITS header for WCS check.",
                    details={"raw_path": frame.raw_path, "error": repr(exc)},
                )

        if result.status == WcsStatus.HEADER_WCS_OK:
            ok += 1
        elif result.status == WcsStatus.HEADER_WCS_MISSING:
            missing += 1
        elif result.status == WcsStatus.HEADER_WCS_BAD:
            bad += 1
        else:
            failed += 1

        if not dry_run:
            frame.wcs_status = result.status.value
            if update_center and result.status == WcsStatus.HEADER_WCS_OK:
                frame.ra_center_deg = result.center_ra_deg
                frame.dec_center_deg = result.center_dec_deg

            flags = dict(frame.quality_flags or {})
            flags["wcs_check"] = {
                "status": result.status.value,
                "message": result.message,
                "details": result.details,
            }
            frame.quality_flags = flags

            if record_events and result.status != WcsStatus.HEADER_WCS_OK:
                session.add(
                    ProcessingEvent(
                        frame_id=frame.frame_id,
                        stage="check_wcs",
                        level=ProcessingEventLevel.WARNING
                        if result.status in {WcsStatus.HEADER_WCS_MISSING, WcsStatus.HEADER_WCS_BAD}
                        else ProcessingEventLevel.ERROR,
                        message=result.message,
                        details={"status": result.status.value, **result.details},
                    )
                )
            updated += 1

    if not dry_run:
        session.commit()

    return WcsDbCheckResult(
        selected=selected,
        checked=checked,
        updated=updated,
        ok=ok,
        missing=missing,
        bad=bad,
        failed=failed,
        skipped_missing_file=skipped_missing_file,
    )


def _has_sip(header: Header) -> bool:
    return any(str(key).startswith(("A_", "B_", "AP_", "BP_")) for key in header.keys())


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _normalize_ra(value: Any) -> float:
    return float(value) % 360.0


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    return value
