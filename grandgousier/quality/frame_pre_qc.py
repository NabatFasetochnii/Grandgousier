from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.models import Frame
from grandgousier.photometry.source_detection import _local_maxima


PRE_QC_ALGORITHM = "frame_pre_photometry_qc_v1"
PRE_QC_POLICY_VERSION = "area_scaled_large_detector_v2"
PRE_QC_REFERENCE_PIXELS = 2048 * 2048


@dataclass
class FramePreQcResult:
    selected_frames: int = 0
    checked_frames: int = 0
    ok_frames: int = 0
    bad_frames: int = 0
    warning_frames: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    detection_cap_reached_frames: int = 0
    excess_detection_frames: int = 0
    bad_background_frames: int = 0
    saturated_fraction_frames: int = 0
    bad_frame_ids: list[int] = field(default_factory=list)
    warning_frame_ids: list[int] = field(default_factory=list)
    sample_bad_frames: list[dict[str, Any]] = field(default_factory=list)
    # Transient in-process handoff to source detection. This is deliberately
    # excluded from summary()/Frame.quality_flags so candidate coordinate lists
    # never become persistent QC payload.
    detection_seeds: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)
    dry_run: bool = False
    selected_frame_ids: list[int] = field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "selected_frames": self.selected_frames,
            "checked_frames": self.checked_frames,
            "ok_frames": self.ok_frames,
            "warning_frames": self.warning_frames,
            "bad_frames": self.bad_frames,
            "bad_frame_ids": self.bad_frame_ids[:50],
            "warning_frame_ids": self.warning_frame_ids[:50],
            "detection_cap_reached_frames": self.detection_cap_reached_frames,
            "excess_detection_frames": self.excess_detection_frames,
            "bad_background_frames": self.bad_background_frames,
            "saturated_fraction_frames": self.saturated_fraction_frames,
            "skipped_missing_file": self.skipped_missing_file,
            "failed_frames": self.failed_frames,
            "sample_bad_frames": self.sample_bad_frames[:10],
            "dry_run": self.dry_run,
        }


def run_frame_pre_qc(
    session: Session,
    *,
    frame_ids: list[int] | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    threshold_sigma: float = 5.0,
    detection_cap: int = 2000,
    max_local_maxima: int | None = 5000,
    max_bright_fraction: float | None = 0.08,
    max_saturated_fraction: float | None = 0.001,
    min_finite_fraction: float = 0.95,
    sample_bad_limit: int = 10,
    dry_run: bool = False,
    capture_detection_seeds: bool = False,
) -> FramePreQcResult:
    """Run cheap frame-level QC before expensive forced photometry.

    The goal is not to replace full photometric QC.  It catches obvious bad inputs
    and records diagnostics that are otherwise hidden by the source-detection cap
    (for example a frame can save 2000 detections both when it has 2100 candidates
    and when it has 50000 ice/noise candidates).
    """

    if frame_ids is not None and not frame_ids:
        raise ValueError("explicit frame_ids selection must not be empty")

    if threshold_sigma <= 0:
        raise ValueError("threshold_sigma must be positive")
    if detection_cap <= 0:
        raise ValueError("detection_cap must be positive")

    result = FramePreQcResult(dry_run=dry_run)
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
    )
    result.selected_frames = len(frames)
    result.selected_frame_ids = [int(frame.frame_id) for frame in frames]

    for frame in tqdm(frames, desc="Pre-photometry frame QC", unit="frame"):
        result.checked_frames += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue
        try:
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
            if data.ndim != 2:
                result.failed_frames += 1
                continue
            qc = analyze_frame_image(
                data,
                threshold_sigma=threshold_sigma,
                detection_cap=detection_cap,
                max_local_maxima=max_local_maxima,
                max_bright_fraction=max_bright_fraction,
                max_saturated_fraction=max_saturated_fraction,
                min_finite_fraction=min_finite_fraction,
                capture_detection_seed=capture_detection_seeds,
            )
            detection_seed = qc.pop("_detection_seed", None)
            if isinstance(detection_seed, dict):
                try:
                    stat = path.stat()
                    detection_seed["raw_path"] = str(path)
                    detection_seed["file_size"] = int(stat.st_size)
                    detection_seed["file_mtime_ns"] = int(stat.st_mtime_ns)
                except OSError:
                    pass
                result.detection_seeds[int(frame.frame_id)] = detection_seed
            flags = dict(qc.get("flags", {}))
            severity = str(qc.get("severity", "OK"))
            if flags.get("DETECTION_CAP_REACHED"):
                result.detection_cap_reached_frames += 1
            if flags.get("BAD_EXCESS_DETECTIONS"):
                result.excess_detection_frames += 1
            if flags.get("BAD_BACKGROUND_STRUCTURE"):
                result.bad_background_frames += 1
            if flags.get("BAD_SATURATED_FRACTION"):
                result.saturated_fraction_frames += 1

            if severity == "BAD":
                result.bad_frames += 1
                result.bad_frame_ids.append(int(frame.frame_id))
                if len(result.sample_bad_frames) < sample_bad_limit:
                    result.sample_bad_frames.append({"frame_id": int(frame.frame_id), **_compact_qc(qc)})
            elif severity == "WARNING":
                result.warning_frames += 1
                result.warning_frame_ids.append(int(frame.frame_id))
            else:
                result.ok_frames += 1

            if not dry_run:
                existing = dict(frame.quality_flags or {})
                existing[PRE_QC_ALGORITHM] = _json_safe(qc)
                if severity == "BAD":
                    existing["BAD_PRE_PHOTOMETRY_QC"] = True
                elif "BAD_PRE_PHOTOMETRY_QC" in existing:
                    existing.pop("BAD_PRE_PHOTOMETRY_QC", None)
                frame.quality_flags = existing
        except Exception:
            result.failed_frames += 1

    if not dry_run:
        session.commit()
    return result


def analyze_frame_image(
    data: np.ndarray,
    *,
    threshold_sigma: float,
    detection_cap: int,
    max_local_maxima: int | None,
    max_bright_fraction: float | None,
    max_saturated_fraction: float | None,
    min_finite_fraction: float,
    capture_detection_seed: bool = False,
) -> dict[str, Any]:
    finite = np.isfinite(data)
    n_pix = int(data.size)
    n_finite = int(finite.sum())
    finite_fraction = float(n_finite / n_pix) if n_pix else 0.0
    flags: dict[str, bool] = {}
    if n_finite < 100:
        return {
            "algorithm": PRE_QC_ALGORITHM,
            "policy_version": PRE_QC_POLICY_VERSION,
            "severity": "BAD",
            "flags": {"BAD_TOO_FEW_FINITE_PIXELS": True},
            "n_pix": n_pix,
            "n_finite": n_finite,
            "finite_fraction": finite_fraction,
        }

    values = data[finite]
    median = float(np.nanmedian(values))
    mad = float(np.nanmedian(np.abs(values - median)))
    sigma = float(1.4826 * mad)
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = float(np.nanstd(values))
    if not math.isfinite(sigma) or sigma <= 0:
        return {
            "algorithm": PRE_QC_ALGORITHM,
            "policy_version": PRE_QC_POLICY_VERSION,
            "severity": "BAD",
            "flags": {"BAD_ZERO_BACKGROUND_SCALE": True},
            "n_pix": n_pix,
            "n_finite": n_finite,
            "finite_fraction": finite_fraction,
            "background_median": median,
        }

    p01, p05, p95, p99, p999 = [float(v) for v in np.nanpercentile(values, [1, 5, 95, 99, 99.9])]
    threshold = float(median + threshold_sigma * sigma)
    bright_mask = finite & (data > threshold)
    bright_fraction = float(np.count_nonzero(bright_mask) / max(1, n_finite))
    local_maxima = _local_maxima(data, threshold=threshold)
    n_local_maxima = int(len(local_maxima))
    detection_cap_reached = n_local_maxima >= int(detection_cap)

    if finite_fraction < min_finite_fraction:
        flags["BAD_LOW_FINITE_FRACTION"] = True
    if detection_cap_reached:
        flags["DETECTION_CAP_REACHED"] = True
    max_local_maxima_effective = _effective_max_local_maxima(max_local_maxima, n_pix=n_pix)
    if max_local_maxima_effective is not None and n_local_maxima > max_local_maxima_effective:
        flags["BAD_EXCESS_DETECTIONS"] = True
    if max_bright_fraction is not None and bright_fraction > float(max_bright_fraction):
        flags["BAD_BACKGROUND_STRUCTURE"] = True

    saturated_fraction = None
    finite_max = float(np.nanmax(values))
    if math.isfinite(finite_max) and finite_max > median:
        # No reliable saturation keyword is available here; this is a robust, keyword-free
        # proxy for pathological frames dominated by extremely bright structures.
        sat_proxy = median + 0.98 * (finite_max - median)
        saturated_fraction = float(np.count_nonzero(finite & (data >= sat_proxy)) / max(1, n_finite))
        if max_saturated_fraction is not None and saturated_fraction > float(max_saturated_fraction):
            flags["BAD_SATURATED_FRACTION"] = True

    bad_keys = {"BAD_LOW_FINITE_FRACTION", "BAD_EXCESS_DETECTIONS", "BAD_BACKGROUND_STRUCTURE", "BAD_SATURATED_FRACTION"}
    if any(flags.get(k) for k in bad_keys):
        severity = "BAD"
    elif flags.get("DETECTION_CAP_REACHED"):
        severity = "WARNING"
    else:
        severity = "OK"

    result = {
        "algorithm": PRE_QC_ALGORITHM,
        "policy_version": PRE_QC_POLICY_VERSION,
        "severity": severity,
        "flags": flags,
        "n_pix": n_pix,
        "n_finite": n_finite,
        "finite_fraction": finite_fraction,
        "background_median": median,
        "background_mad_sigma": sigma,
        "p01": p01,
        "p05": p05,
        "p95": p95,
        "p99": p99,
        "p999": p999,
        "threshold_sigma": float(threshold_sigma),
        "threshold_adu": threshold,
        "n_local_maxima_before_cap": n_local_maxima,
        "local_maxima_per_mpix": float(n_local_maxima / max(1.0, n_pix / 1_000_000.0)),
        "max_local_maxima_configured": None if max_local_maxima is None else int(max_local_maxima),
        "max_local_maxima_effective": max_local_maxima_effective,
        "detection_cap": int(detection_cap),
        "bright_fraction": bright_fraction,
        "saturated_fraction_proxy": saturated_fraction,
    }
    if capture_detection_seed:
        # Same threshold and same _local_maxima implementation are used by the
        # immediately following detection step. Keep the coordinates only in
        # memory; run_frame_pre_qc removes this private key before persistence.
        result["_detection_seed"] = {
            "threshold_sigma": float(threshold_sigma),
            "background": float(median),
            "sigma": float(sigma),
            "threshold": float(threshold),
            "candidates": tuple((int(y), int(x)) for y, x in local_maxima),
        }
    return result


def filter_bad_pre_qc_frame_ids(session: Session, frame_ids: list[int]) -> list[int]:
    """Return frame IDs not marked BAD by the current pre-photometry QC algorithm."""

    if not frame_ids:
        return []
    frames = list(session.scalars(select(Frame).where(Frame.frame_id.in_(frame_ids))).all())
    bad = set()
    for frame in frames:
        flags = frame.quality_flags or {}
        qc = flags.get(PRE_QC_ALGORITHM) if isinstance(flags, dict) else None
        if isinstance(qc, dict) and qc.get("severity") == "BAD":
            bad.add(int(frame.frame_id))
    return [int(fid) for fid in frame_ids if int(fid) not in bad]



def _effective_max_local_maxima(max_local_maxima: int | None, *, n_pix: int) -> int | None:
    """Scale the excess-detection guard for detectors larger than 2048x2048.

    The historical threshold is preserved exactly for images up to the reference
    area. Larger detectors get the same allowed local-maximum density rather than
    being penalized solely for having more pixels.
    """

    if max_local_maxima is None:
        return None
    configured = int(max_local_maxima)
    if configured < 0:
        raise ValueError("max_local_maxima must be non-negative when provided")
    scale = max(1.0, float(max(0, int(n_pix))) / float(PRE_QC_REFERENCE_PIXELS))
    return int(math.ceil(configured * scale))

def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids is not None:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if only_wcs_ok:
        stmt = stmt.where(Frame.wcs_status == "HEADER_WCS_OK")
    return list(session.scalars(stmt).all())


def _compact_qc(qc: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "severity",
        "flags",
        "background_median",
        "background_mad_sigma",
        "n_local_maxima_before_cap",
        "local_maxima_per_mpix",
        "max_local_maxima_effective",
        "detection_cap",
        "bright_fraction",
        "finite_fraction",
    )
    return {k: qc.get(k) for k in keys if k in qc}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value
