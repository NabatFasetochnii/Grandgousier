from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from grandgousier.calibration.aperture_zp import aperture_zp_calibrate
from grandgousier.calibration.ensemble import ensemble_calibrate_measurements
from grandgousier.calibration.preliminary import load_lightcurve_rows
from grandgousier.photometry.forced_aperture import run_forced_aperture_photometry


@dataclass
class ApertureSweepResult:
    rows: list[dict[str, Any]]
    summary: dict[str, Any]


def run_aperture_radius_sweep(
    session: Session,
    *,
    catalog_name: str,
    source_id: str | None = None,
    object_id: int | None = None,
    instrument_unit_id: int | None = None,
    radii: list[float],
    frame_ids: list[int] | None = None,
    calibration_prefix: str | None = None,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    mag_key: str = "G",
    min_ref_stars_per_frame: int = 30,
    max_mag_err: float | None = 0.08,
    max_zp_mad: float | None = None,
    clip_sigma: float = 3.0,
    n_iterations: int = 3,
    min_ref_measurements: int = 10,
    ensemble_min_ref_stars_per_frame: int = 30,
    max_ref_rms: float | None = 0.08,
    max_ref_mad: float | None = 0.05,
    skip_photometry: bool = False,
    skip_calibration: bool = False,
    dry_run: bool = False,
) -> ApertureSweepResult:
    """Run and compare forced-aperture light curves for several aperture radii."""

    if frame_ids is not None and not frame_ids:
        raise ValueError("explicit frame_ids selection must not be empty")

    if source_id is None and object_id is None:
        raise ValueError("Provide source_id or object_id")
    if not radii:
        raise ValueError("At least one aperture radius is required")

    rows: list[dict[str, Any]] = []
    curves_by_radius: dict[float, dict[int, float]] = {}

    for radius in radii:
        radius = float(radius)
        radius_tag = _fmt_float(radius)
        ann_in_tag = _fmt_float(annulus_inner_pix)
        ann_out_tag = _fmt_float(annulus_outer_pix)
        raw_name, preliminary_name, ensemble_name = radius_calibration_names(
            radius=radius,
            annulus_inner_pix=annulus_inner_pix,
            annulus_outer_pix=annulus_outer_pix,
            mag_key=mag_key,
            calibration_prefix=calibration_prefix,
        )

        phot_result = None
        aperture_zp_result = None
        ensemble_result = None

        if not skip_photometry:
            phot_result = run_forced_aperture_photometry(
                session,
                catalog_name=catalog_name,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
                aperture_radius_pix=radius,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                calibration_name=raw_name,
                dry_run=dry_run,
            )

        if not skip_calibration:
            aperture_zp_result = aperture_zp_calibrate(
                session,
                source_calibration_name=raw_name,
                target_calibration_name=preliminary_name,
                catalog_name=catalog_name,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
                mag_key=mag_key,
                min_ref_stars_per_frame=min_ref_stars_per_frame,
                max_mag_err=max_mag_err,
                max_zp_mad=max_zp_mad,
                clip_sigma=clip_sigma,
                n_iterations=n_iterations,
                dry_run=dry_run,
            )
            ensemble_result = ensemble_calibrate_measurements(
                session,
                source_calibration_name=preliminary_name,
                target_calibration_name=ensemble_name,
                catalog_name=catalog_name,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
                min_ref_measurements=min_ref_measurements,
                min_ref_stars_per_frame=ensemble_min_ref_stars_per_frame,
                max_ref_rms=max_ref_rms,
                max_ref_mad=max_ref_mad,
                n_iterations=n_iterations,
                dry_run=dry_run,
            )

        lc_rows = []
        if not dry_run:
            lc_rows = load_lightcurve_rows(
                session,
                calibration_name=ensemble_name,
                source_id=source_id,
                object_id=object_id,
                catalog_name=catalog_name,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
            )

        mags = [float(r.mag_cal) for r in lc_rows if r.mag_cal is not None and _is_finite(r.mag_cal)]
        mag_errs = [float(r.mag_err) for r in lc_rows if r.mag_err is not None and _is_finite(r.mag_err)]
        curves_by_radius[radius] = {
            int(r.frame_id): float(r.mag_cal)
            for r in lc_rows
            if r.mag_cal is not None and _is_finite(r.mag_cal)
        }

        rows.append(
            {
                "radius_pix": radius,
                "raw_calibration_name": raw_name,
                "preliminary_calibration_name": preliminary_name,
                "ensemble_calibration_name": ensemble_name,
                "n_points": len(mags),
                "mag_median": _median(mags),
                "mag_rms": _sample_rms(mags),
                "mag_mad": _mad(mags),
                "mag_err_median": _median(mag_errs),
                "phot_measured": getattr(phot_result, "measured", None),
                "phot_inserted_or_updated": getattr(phot_result, "inserted_or_updated", None),
                "aperture_zp_frames_with_zp": getattr(aperture_zp_result, "frames_with_zp", None),
                "aperture_zp_calibrated": getattr(aperture_zp_result, "calibrated", None),
                "aperture_zp_inserted_or_updated": getattr(aperture_zp_result, "inserted_or_updated", None),
                "aperture_zp_frame_zp_median": getattr(aperture_zp_result, "frame_zp_median", None),
                "aperture_zp_frame_zp_mad": getattr(aperture_zp_result, "frame_zp_mad", None),
                "ensemble_frames_with_offsets": getattr(ensemble_result, "frames_with_offsets", None),
                "ensemble_calibrated": getattr(ensemble_result, "calibrated", None),
                "ensemble_inserted_or_updated": getattr(ensemble_result, "inserted_or_updated", None),
            }
        )

    reference_radius = float(radii[0])
    ref_curve = curves_by_radius.get(reference_radius, {})
    for row in rows:
        radius = float(row["radius_pix"])
        curve = curves_by_radius.get(radius, {})
        if radius == reference_radius:
            row["corr_with_ref"] = 1.0 if curve else None
            row["median_delta_vs_ref"] = 0.0 if curve else None
            row["rms_delta_vs_ref"] = 0.0 if curve else None
        else:
            stats = _compare_curves(ref_curve, curve)
            row["corr_with_ref"] = stats.get("corr")
            row["median_delta_vs_ref"] = stats.get("median_delta")
            row["rms_delta_vs_ref"] = stats.get("rms_delta")

    summary = {
        "catalog_name": catalog_name,
        "source_id": source_id,
        "object_id": object_id,
        "instrument_unit_id": instrument_unit_id,
        "radii": [float(r) for r in radii],
        "reference_radius_pix": reference_radius,
        "calibration_prefix": calibration_prefix,
        "dry_run": dry_run,
        "skip_photometry": skip_photometry,
        "skip_calibration": skip_calibration,
    }
    return ApertureSweepResult(rows=rows, summary=summary)


def export_aperture_sweep_csv(rows: list[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = aperture_sweep_columns()
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def aperture_sweep_columns() -> list[str]:
    return [
        "radius_pix",
        "n_points",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mag_err_median",
        "corr_with_ref",
        "median_delta_vs_ref",
        "rms_delta_vs_ref",
        "raw_calibration_name",
        "preliminary_calibration_name",
        "ensemble_calibration_name",
        "phot_measured",
        "phot_inserted_or_updated",
        "aperture_zp_frames_with_zp",
        "aperture_zp_calibrated",
        "aperture_zp_inserted_or_updated",
        "aperture_zp_frame_zp_median",
        "aperture_zp_frame_zp_mad",
        "ensemble_frames_with_offsets",
        "ensemble_calibrated",
        "ensemble_inserted_or_updated",
    ]


def radius_calibration_names(
    *,
    radius: float,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    mag_key: str = "G",
    calibration_prefix: str | None = None,
) -> tuple[str, str, str]:
    """Return raw, aperture-ZP, and ensemble calibration names for one sweep radius.

    The historical aperture-radius sweep used global calibration names. A caller may
    now pass `calibration_prefix` to keep batch/pipeline stability checks isolated
    from other fields or runs while keeping the old naming scheme as the default.
    """

    radius_tag = _fmt_float(radius)
    ann_in_tag = _fmt_float(annulus_inner_pix)
    ann_out_tag = _fmt_float(annulus_outer_pix)
    mag_tag = str(mag_key).lower()
    if calibration_prefix:
        prefix = str(calibration_prefix).strip().rstrip("_")
        return (
            f"{prefix}_raw_aperture_r{radius_tag}_ann{ann_in_tag}_{ann_out_tag}_v1",
            f"{prefix}_aperture_r{radius_tag}_{mag_tag}_zp_v1",
            f"{prefix}_ensemble_r{radius_tag}_{mag_tag}_v1",
        )
    return (
        f"raw_aperture_r{radius_tag}_ann{ann_in_tag}_{ann_out_tag}_v1",
        f"preliminary_aperture_r{radius_tag}_gaia_{mag_tag}_zp_v1",
        f"ensemble_aperture_r{radius_tag}_gaia_{mag_tag}_v1",
    )


def parse_radii(text: str) -> list[float]:
    out: list[float] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    if not out:
        raise ValueError("No radii parsed")
    return out


def _compare_curves(ref: dict[int, float], other: dict[int, float]) -> dict[str, float | None]:
    common = sorted(set(ref) & set(other))
    if not common:
        return {"corr": None, "median_delta": None, "rms_delta": None}
    x = [ref[k] for k in common]
    y = [other[k] for k in common]
    deltas = [y_i - x_i for x_i, y_i in zip(x, y)]
    return {
        "corr": _corr(x, y),
        "median_delta": _median(deltas),
        "rms_delta": _sample_rms(deltas),
    }


def _corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return None
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    sx2 = sum((v - mx) ** 2 for v in x)
    sy2 = sum((v - my) ** 2 for v in y)
    if sx2 <= 0 or sy2 <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx2 * sy2)


def _fmt_float(value: float) -> str:
    x = float(value)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.3f}".rstrip("0").rstrip(".").replace(".", "p")


def _median(values: list[float]) -> float | None:
    vals = sorted(float(v) for v in values if _is_finite(v))
    if not vals:
        return None
    n = len(vals)
    if n % 2:
        return float(vals[n // 2])
    return float((vals[n // 2 - 1] + vals[n // 2]) / 2.0)


def _mad(values: list[float]) -> float | None:
    med = _median(values)
    if med is None:
        return None
    return _median([abs(float(v) - med) for v in values])


def _sample_rms(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False
