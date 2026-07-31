from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.calibration.preliminary import load_lightcurve_rows
from grandgousier.db.models import CalibrationVersion, ObjectQc
from grandgousier.quality.aperture_sweep import run_aperture_radius_sweep


@dataclass
class ApertureStabilityQcResult:
    calibration_name: str
    catalog_name: str | None
    instrument_unit_id: int | None
    object_id: int | None = None
    source_id: str | None = None
    updated: int = 0
    aperture_unstable: int = 0
    aperture_median_shift: int = 0
    possible_blend: int = 0
    dry_run: bool = False
    metrics: dict[str, Any] | None = None


def compute_aperture_stability_qc(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    source_id: str | None = None,
    object_id: int | None = None,
    instrument_unit_id: int | None = None,
    radii: list[float],
    frame_ids: list[int] | None = None,
    sweep_calibration_prefix: str | None = None,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    mag_key: str = "G",
    algorithm: str = "object_qc_v1",
    min_stable_corr: float = 0.7,
    max_stable_rms_delta: float = 0.03,
    max_median_shift: float = 0.2,
    possible_blend_neighbor_arcsec: float | None = None,
    dry_run: bool = False,
) -> ApertureStabilityQcResult:
    """Append aperture-stability metrics and flags to an existing object_qc row.

    This function expects the per-radius ensemble calibration versions already to exist,
    e.g. from `aperture-radius-sweep`. It does not re-run photometry/calibration.
    """

    if source_id is None and object_id is None:
        raise ValueError("Provide source_id or object_id")
    if not radii:
        raise ValueError("At least one aperture radius is required")

    rows = load_lightcurve_rows(
        session,
        calibration_name=calibration_name,
        source_id=source_id,
        object_id=object_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        limit=1,
    )
    if not rows:
        raise ValueError("Object not found in base calibration")
    resolved_object_id = int(rows[0].object_id)
    resolved_source_id = str(rows[0].source_id) if rows[0].source_id is not None else source_id

    sweep = run_aperture_radius_sweep(
        session,
        catalog_name=catalog_name or rows[0].catalog_name,
        source_id=resolved_source_id,
        object_id=None if resolved_source_id is not None else resolved_object_id,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        radii=[float(r) for r in radii],
        calibration_prefix=sweep_calibration_prefix,
        annulus_inner_pix=annulus_inner_pix,
        annulus_outer_pix=annulus_outer_pix,
        mag_key=mag_key,
        skip_photometry=True,
        skip_calibration=True,
        dry_run=False,
    )
    metrics = _build_metrics(sweep.rows)

    unstable = False
    if metrics.get("min_corr_with_ref") is not None and float(metrics["min_corr_with_ref"]) < float(min_stable_corr):
        unstable = True
    if metrics.get("max_rms_delta_vs_ref") is not None and float(metrics["max_rms_delta_vs_ref"]) > float(max_stable_rms_delta):
        unstable = True
    median_shift = (
        metrics.get("max_abs_median_delta_vs_ref") is not None
        and float(metrics["max_abs_median_delta_vs_ref"]) > float(max_median_shift)
    )

    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")
    qc = session.scalar(
        select(ObjectQc).where(
            ObjectQc.object_id == resolved_object_id,
            ObjectQc.calibration_version_id == cv.calibration_version_id,
            ObjectQc.catalog_name == (catalog_name or rows[0].catalog_name),
            ObjectQc.instrument_unit_id == instrument_unit_id,
            ObjectQc.algorithm == algorithm,
        )
    )
    if qc is None:
        raise ValueError("Object QC row not found. Run compute-object-qc first.")

    flags = dict(qc.flags or {})
    details = dict(qc.details or {})
    neighbor_arcsec = qc.nearest_neighbor_arcsec
    possible_blend = False
    if unstable or median_shift:
        flags["APERTURE_UNSTABLE"] = bool(unstable)
        flags["APERTURE_MEDIAN_SHIFT"] = bool(median_shift)
        if possible_blend_neighbor_arcsec is not None:
            if neighbor_arcsec is not None and float(neighbor_arcsec) < float(possible_blend_neighbor_arcsec):
                possible_blend = True
        elif flags.get("CROWDED") or flags.get("BLENDED_NEIGHBOR") or flags.get("DYNAMIC_CROWDED") or flags.get("DYNAMIC_BLEND_RISK"):
            possible_blend = True
        if possible_blend:
            flags["POSSIBLE_BLEND"] = True

    metrics["thresholds"] = {
        "min_stable_corr": min_stable_corr,
        "max_stable_rms_delta": max_stable_rms_delta,
        "max_median_shift": max_median_shift,
        "possible_blend_neighbor_arcsec": possible_blend_neighbor_arcsec,
        "sweep_calibration_prefix": sweep_calibration_prefix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
        "mag_key": mag_key,
    }
    details["aperture_stability"] = metrics

    result = ApertureStabilityQcResult(
        calibration_name=calibration_name,
        catalog_name=catalog_name or rows[0].catalog_name,
        instrument_unit_id=instrument_unit_id,
        object_id=resolved_object_id,
        source_id=resolved_source_id,
        updated=0,
        aperture_unstable=int(bool(unstable)),
        aperture_median_shift=int(bool(median_shift)),
        possible_blend=int(bool(possible_blend)),
        dry_run=dry_run,
        metrics=metrics,
    )

    if not dry_run:
        qc.flags = flags
        qc.details = details
        session.add(qc)
        session.commit()
        result.updated = 1
    return result


def _build_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    radii = [float(r["radius_pix"]) for r in rows if r.get("radius_pix") is not None]
    rms_values = [float(r["mag_rms"]) for r in rows if r.get("mag_rms") is not None and _is_finite(r.get("mag_rms"))]
    mad_values = [float(r["mag_mad"]) for r in rows if r.get("mag_mad") is not None and _is_finite(r.get("mag_mad"))]
    corr_values = [float(r["corr_with_ref"]) for r in rows[1:] if r.get("corr_with_ref") is not None and _is_finite(r.get("corr_with_ref"))]
    rms_delta_values = [abs(float(r["rms_delta_vs_ref"])) for r in rows[1:] if r.get("rms_delta_vs_ref") is not None and _is_finite(r.get("rms_delta_vs_ref"))]
    med_delta_values = [abs(float(r["median_delta_vs_ref"])) for r in rows[1:] if r.get("median_delta_vs_ref") is not None and _is_finite(r.get("median_delta_vs_ref"))]
    return {
        "aperture_radii": radii,
        "aperture_rms_values": rms_values,
        "aperture_mad_values": mad_values,
        "min_corr_with_ref": min(corr_values) if corr_values else None,
        "max_rms_delta_vs_ref": max(rms_delta_values) if rms_delta_values else None,
        "max_abs_median_delta_vs_ref": max(med_delta_values) if med_delta_values else None,
        "rms_range": (max(rms_values) - min(rms_values)) if len(rms_values) >= 2 else None,
        "mad_range": (max(mad_values) - min(mad_values)) if len(mad_values) >= 2 else None,
        "per_radius": rows,
    }


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False
