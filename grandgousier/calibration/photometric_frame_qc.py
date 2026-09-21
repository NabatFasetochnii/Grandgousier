from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import numpy as np


PHOTOMETRIC_FRAME_QC_ALGORITHM = "post_ensemble_photometric_frame_qc_v3"
PHOTOMETRIC_FRAME_QC_FLAG = "frame_photometric_qc_v1"
FRAME_PSF_SUMMARY_FLAG = "frame_psf_summary_v1"
BAD_FRAME_REJECT_FLAG = "PHOTOMETRIC_FRAME_BAD"


@dataclass
class PhotometricFrameQcStat:
    frame_id: int
    status: str = "GOOD"
    science_action: str = "KEEP"
    reasons: list[str] = field(default_factory=list)
    n_reference_candidates: int = 0
    n_reference_used: int = 0
    residual_median_mag: float | None = None
    residual_mad_mag: float | None = None
    residual_robust_sigma_mag: float | None = None
    residual_rms_mag: float | None = None
    series_residual_robust_sigma_median_mag: float | None = None
    series_residual_robust_sigma_mad_mag: float | None = None
    suspect_residual_limit_mag: float | None = None
    bad_residual_limit_mag: float | None = None
    sigma_systematic_mag: float = 0.0
    sigma_frame_systematic_mag: float = 0.0
    absolute_bad_residual_limit_mag: float | None = None
    spatial_model: str = "identity"
    spatial_bic_improvement: float | None = None
    spatial_peak_to_peak_mag: float | None = None
    spatial_max_abs_deviation_mag: float | None = None
    psf_shape_sources: int | None = None
    psf_fwhm_pix_median: float | None = None
    psf_fwhm_arcsec_median: float | None = None
    psf_fwhm_major_pix_median: float | None = None
    psf_fwhm_minor_pix_median: float | None = None
    psf_ellipticity_median: float | None = None
    psf_position_angle_deg: float | None = None
    psf_fwhm_spatial_model: str | None = None
    psf_fwhm_spatial_peak_to_peak_pix: float | None = None
    psf_ellipticity_spatial_model: str | None = None
    psf_ellipticity_spatial_peak_to_peak: float | None = None


@dataclass
class PhotometricFrameQcResult:
    algorithm: str = PHOTOMETRIC_FRAME_QC_ALGORITHM
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_name: str | None = None
    instrument_unit_id: int | None = None
    reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_objects: int = 0
    actually_excluded_reference_object_ids: list[int] = field(default_factory=list)
    actually_excluded_reference_objects: int = 0
    base_ensemble_reference_candidates: int = 0
    base_ensemble_usable_reference_objects: int = 0
    base_ensemble_required_references_per_frame: int = 0
    base_ensemble_selected_frames: int = 0
    base_ensemble_frames_with_offsets: int = 0
    base_ensemble_calibrated: int = 0
    base_ensemble_target_calibration_version_id: int | None = None
    selected_frames: int = 0
    checked_frames: int = 0
    series_status: str = "GOOD"
    good_frames: int = 0
    suspect_frames: int = 0
    bad_frames: int = 0
    good_frame_ids: list[int] = field(default_factory=list)
    suspect_frame_ids: list[int] = field(default_factory=list)
    bad_frame_ids: list[int] = field(default_factory=list)
    inflated_frames: int = 0
    median_residual_robust_sigma_mag: float | None = None
    frame_scatter_mad_mag: float | None = None
    suspect_residual_limit_mag: float | None = None
    bad_residual_limit_mag: float | None = None
    absolute_bad_residual_limit_mag: float | None = None
    median_sigma_systematic_mag: float | None = None
    max_sigma_systematic_mag: float | None = None
    median_sigma_frame_systematic_mag: float | None = None
    max_sigma_frame_systematic_mag: float | None = None
    frames_with_spatial_systematic: int = 0
    frame_psf_summaries: int = 0
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    diagnostics_summary_json: str | None = None
    diagnostics_frames_csv: str | None = None
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)


def apply_photometric_frame_qc(
    session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    reference_object_ids: Iterable[int],
    frame_ids: list[int] | None = None,
    instrument_unit_id: int | None = None,
    min_reference_stars_per_frame: int = 20,
    suspect_sigma: float = 3.0,
    bad_sigma: float = 6.0,
    suspect_ratio: float = 1.5,
    bad_ratio: float = 2.5,
    absolute_suspect_residual_sigma_mag: float = 0.05,
    absolute_bad_residual_sigma_mag: float = 0.10,
    spatial_suspect_peak_to_peak_mag: float = 0.03,
    spatial_bad_peak_to_peak_mag: float = 0.10,
    aperture_context_frames_csv: str | Path | None = None,
    psf_frames_csv: str | Path | None = None,
    out_dir: str | Path | None = None,
    diagnostics_label: str | None = None,
    dry_run: bool = False,
    immutable_generation: bool = False,
) -> PhotometricFrameQcResult:
    """Create a conservative post-ensemble QC layer without changing magnitudes.

    GOOD rows pass through unchanged. SUSPECT frames keep both their magnitudes and
    their original measurement uncertainties: excess frame scatter is stored only as
    a diagnostic quantity and is never folded into ``mag_err``/``mag_err_random``.
    Relative/spatial anomalies can make a frame SUSPECT but never BAD by themselves.
    BAD is reserved for hard failures (catastrophic absolute residual scatter, no
    usable residual scatter, or too few references). Source measurements remain
    untouched.
    """

    if min_reference_stars_per_frame < 5:
        raise ValueError("min_reference_stars_per_frame must be >= 5")
    if suspect_sigma <= 0 or bad_sigma <= suspect_sigma:
        raise ValueError("require 0 < suspect_sigma < bad_sigma")
    if suspect_ratio <= 1 or bad_ratio <= suspect_ratio:
        raise ValueError("require 1 < suspect_ratio < bad_ratio")
    if absolute_suspect_residual_sigma_mag <= 0 or absolute_bad_residual_sigma_mag <= absolute_suspect_residual_sigma_mag:
        raise ValueError("absolute residual limits must be positive and ordered")

    from sqlalchemy import select
    from grandgousier.calibration.reconstruction import reconstruct_calibration_measurement_states
    from grandgousier.calibration.spatial_ensemble import fit_spatial_ensemble_model
    from grandgousier.calibration.normalized_terms import replace_calibration_iteration
    from grandgousier.calibration.provenance import prepare_calibration_version_for_write
    from grandgousier.db.models import CalibrationVersion

    reference_ids = sorted({int(v) for v in reference_object_ids})
    result = PhotometricFrameQcResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        instrument_unit_id=instrument_unit_id,
        reference_object_ids=reference_ids,
        dry_run=bool(dry_run),
    )

    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration not found: {source_calibration_name!r}")
    result.source_calibration_version_id = int(source_cv.calibration_version_id)

    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_version_id=int(source_cv.calibration_version_id),
        frame_ids=frame_ids,
    )
    rows = list(reconstructed.states.values())
    selected_frame_ids = sorted({int(row["frame_id"]) for row in rows})
    result.selected_frames = len(selected_frame_ids)
    if not rows:
        result.warnings.append("no reconstructed source measurements")
        return result

    by_object: dict[int, list[float]] = {}
    for row in rows:
        object_id = int(row["object_id"])
        mag = _finite_or_none(row.get("mag_cal"))
        if object_id in reference_ids and mag is not None:
            by_object.setdefault(object_id, []).append(mag)
    object_centers = {object_id: float(median(values)) for object_id, values in by_object.items() if values}

    bounds = _detector_bounds(rows)
    by_frame: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_frame.setdefault(int(row["frame_id"]), []).append(row)

    psf_context = _load_psf_context(
        aperture_context_frames_csv=aperture_context_frames_csv,
        psf_frames_csv=psf_frames_csv,
    )
    preliminary: list[dict[str, Any]] = []
    for frame_id in selected_frame_ids:
        frame_rows = by_frame.get(frame_id, [])
        ref_rows = []
        residuals: list[float] = []
        for row in frame_rows:
            object_id = int(row["object_id"])
            mag = _finite_or_none(row.get("mag_cal"))
            if object_id not in object_centers or mag is None:
                continue
            residual = float(mag - object_centers[object_id])
            residuals.append(residual)
            x = _finite_or_none(row.get("x"))
            y = _finite_or_none(row.get("y"))
            if x is not None and y is not None:
                ref_rows.append({"x": x, "y": y, "residual": residual, "weight": _weight(row)})

        residual_med = _median(residuals)
        residual_mad = _mad(residuals)
        residual_sigma = (1.4826 * residual_mad) if residual_mad is not None else None
        residual_rms = _rms(residuals)
        spatial = fit_spatial_ensemble_model(
            ref_rows,
            bounds=bounds,
            max_order=2,
            min_ref_stars=max(6, int(min_reference_stars_per_frame)),
            clip_sigma=3.5,
            n_iterations=3,
            min_bic_improvement=6.0,
            min_spatial_peak_to_peak=0.003,
            max_residual_mad=None,
            max_residual_mad_degradation=None,
            max_abs_spatial_deviation=0.5,
        )
        spatial_ptp = _finite_or_none(spatial.get("proposed_spatial_peak_to_peak"))
        if spatial_ptp is None:
            spatial_ptp = _finite_or_none(spatial.get("spatial_peak_to_peak"))
        preliminary.append({
            "frame_id": frame_id,
            "n_reference_candidates": len(residuals),
            "n_reference_used": int(spatial.get("n_used") or len(residuals)),
            "residual_median_mag": residual_med,
            "residual_mad_mag": residual_mad,
            "residual_robust_sigma_mag": residual_sigma,
            "residual_rms_mag": residual_rms,
            "spatial_model": str(spatial.get("proposed_model") or spatial.get("selected_model") or "identity"),
            "spatial_bic_improvement": _finite_or_none(spatial.get("bic_improvement")),
            "spatial_peak_to_peak_mag": spatial_ptp,
            "spatial_max_abs_deviation_mag": _finite_or_none(spatial.get("proposed_max_abs_spatial_deviation") or spatial.get("max_abs_spatial_deviation")),
        })

    frame_sigmas = [float(row["residual_robust_sigma_mag"]) for row in preliminary if _finite(row.get("residual_robust_sigma_mag"))]
    baseline, frame_scatter_mad, suspect_limit, bad_limit = derive_residual_qc_limits(
        frame_sigmas,
        suspect_sigma=suspect_sigma,
        bad_sigma=bad_sigma,
        suspect_ratio=suspect_ratio,
        bad_ratio=bad_ratio,
        absolute_suspect_residual_sigma_mag=absolute_suspect_residual_sigma_mag,
        absolute_bad_residual_sigma_mag=absolute_bad_residual_sigma_mag,
    )
    result.median_residual_robust_sigma_mag = baseline
    result.frame_scatter_mad_mag = frame_scatter_mad
    result.suspect_residual_limit_mag = suspect_limit
    result.bad_residual_limit_mag = bad_limit

    stats: list[PhotometricFrameQcStat] = []
    sigma_values: list[float] = []
    for item in preliminary:
        residual_sigma = _finite_or_none(item.get("residual_robust_sigma_mag"))
        n_ref = int(item.get("n_reference_candidates") or 0)
        spatial_ptp = _finite_or_none(item.get("spatial_peak_to_peak_mag"))
        status, reasons, sigma_sys = classify_photometric_frame(
            n_reference=n_ref,
            residual_robust_sigma_mag=residual_sigma,
            spatial_peak_to_peak_mag=spatial_ptp,
            min_reference_stars_per_frame=min_reference_stars_per_frame,
            baseline_residual_robust_sigma_mag=baseline,
            suspect_residual_limit_mag=suspect_limit,
            bad_residual_limit_mag=bad_limit,
            absolute_bad_residual_sigma_mag=absolute_bad_residual_sigma_mag,
            spatial_suspect_peak_to_peak_mag=spatial_suspect_peak_to_peak_mag,
            spatial_bad_peak_to_peak_mag=spatial_bad_peak_to_peak_mag,
        )
        if sigma_sys > 0:
            sigma_values.append(sigma_sys)

        action = (
            "REJECT"
            if status == "BAD"
            else ("KEEP_FLAGGED" if status == "SUSPECT" else "KEEP")
        )
        ctx = psf_context.get(int(item["frame_id"]), {})
        stat = PhotometricFrameQcStat(
            **item,
            status=status,
            science_action=action,
            reasons=sorted(set(reasons)),
            series_residual_robust_sigma_median_mag=baseline,
            series_residual_robust_sigma_mad_mag=frame_scatter_mad,
            suspect_residual_limit_mag=suspect_limit,
            bad_residual_limit_mag=bad_limit,
            sigma_systematic_mag=float(sigma_sys),
            sigma_frame_systematic_mag=float(sigma_sys),
            absolute_bad_residual_limit_mag=float(absolute_bad_residual_sigma_mag),
            psf_shape_sources=_int_or_none(ctx.get("psf_shape_sources")),
            psf_fwhm_pix_median=_finite_or_none(ctx.get("psf_fwhm_pix_median") or ctx.get("psf_fwhm_pix")),
            psf_fwhm_arcsec_median=_finite_or_none(ctx.get("psf_fwhm_arcsec_median")),
            psf_fwhm_major_pix_median=_finite_or_none(ctx.get("psf_fwhm_major_pix_median")),
            psf_fwhm_minor_pix_median=_finite_or_none(ctx.get("psf_fwhm_minor_pix_median")),
            psf_ellipticity_median=_finite_or_none(ctx.get("psf_ellipticity_median") or ctx.get("psf_ellipticity")),
            psf_position_angle_deg=_finite_or_none(ctx.get("psf_position_angle_deg")),
            psf_fwhm_spatial_model=_str_or_none(ctx.get("psf_fwhm_spatial_model")),
            psf_fwhm_spatial_peak_to_peak_pix=_finite_or_none(ctx.get("psf_fwhm_spatial_peak_to_peak_pix")),
            psf_ellipticity_spatial_model=_str_or_none(ctx.get("psf_ellipticity_spatial_model")),
            psf_ellipticity_spatial_peak_to_peak=_finite_or_none(ctx.get("psf_ellipticity_spatial_peak_to_peak")),
        )
        stats.append(stat)

    result.checked_frames = len(stats)
    result.good_frame_ids = [int(row.frame_id) for row in stats if row.status == "GOOD"]
    result.suspect_frame_ids = [int(row.frame_id) for row in stats if row.status == "SUSPECT"]
    result.bad_frame_ids = [int(row.frame_id) for row in stats if row.status == "BAD"]
    result.good_frames = len(result.good_frame_ids)
    result.suspect_frames = len(result.suspect_frame_ids)
    result.bad_frames = len(result.bad_frame_ids)
    result.series_status = (
        "BAD"
        if result.checked_frames > 0 and result.bad_frames == result.checked_frames
        else ("DEGRADED" if result.suspect_frames > 0 or result.bad_frames > 0 else "GOOD")
    )
    result.inflated_frames = 0
    result.frames_with_spatial_systematic = sum("SPATIAL_SYSTEMATIC" in row.reasons for row in stats)
    result.frame_psf_summaries = sum(row.psf_fwhm_pix_median is not None or row.psf_ellipticity_median is not None for row in stats)
    result.median_sigma_systematic_mag = _median(sigma_values)
    result.max_sigma_systematic_mag = max(sigma_values) if sigma_values else None
    result.median_sigma_frame_systematic_mag = result.median_sigma_systematic_mag
    result.max_sigma_frame_systematic_mag = result.max_sigma_systematic_mag
    result.absolute_bad_residual_limit_mag = float(absolute_bad_residual_sigma_mag)

    parameters = {
        "algorithm": PHOTOMETRIC_FRAME_QC_ALGORITHM,
        "min_reference_stars_per_frame": int(min_reference_stars_per_frame),
        "suspect_sigma": float(suspect_sigma),
        "bad_sigma": float(bad_sigma),
        "suspect_ratio": float(suspect_ratio),
        "bad_ratio": float(bad_ratio),
        "absolute_suspect_residual_sigma_mag": float(absolute_suspect_residual_sigma_mag),
        "absolute_bad_residual_sigma_mag": float(absolute_bad_residual_sigma_mag),
        "spatial_suspect_peak_to_peak_mag": float(spatial_suspect_peak_to_peak_mag),
        "spatial_bad_peak_to_peak_mag": float(spatial_bad_peak_to_peak_mag),
        "error_semantics": "suspect: frame_epoch_sigma=sqrt(max(Rj^2-R0^2,0)) is diagnostic-only and never modifies mag_err or mag_err_random; bad only on hard failures and exposes mag_cal=NULL",
        "rejection_semantics": "relative residual and spatial anomalies are SUSPECT-only; BAD requires absolute/hard failure",
        "magnitude_correction": "none",
    }

    target_cv = None
    if not dry_run:
        target_cv = prepare_calibration_version_for_write(
            session,
            logical_name=target_calibration_name,
            model_name=PHOTOMETRIC_FRAME_QC_ALGORITHM,
            instrument_unit_id=instrument_unit_id,
            instrument_profile={},
            used_catalogs={},
            parameters={
                "source_calibration_name": source_cv.name,
                "source_calibration_version_id": int(source_cv.calibration_version_id),
                **parameters,
            },
            notes="Post-ensemble frame QC: no magnitude or uncertainty correction; suspicious frames are flagged diagnostically and hard-bad frames are rejected in the derived science scale.",
            source_cv=source_cv,
            immutable_generation=bool(immutable_generation),
        )
        result.target_calibration_version_id = int(target_cv.calibration_version_id)
        normalized = replace_calibration_iteration(
            session,
            calibration_version_id=int(target_cv.calibration_version_id),
            source_calibration_version_id=int(source_cv.calibration_version_id),
            iteration_kind="PHOTOMETRIC_FRAME_QC",
            iteration_no=1,
            algorithm=PHOTOMETRIC_FRAME_QC_ALGORITHM,
            status="COMPLETE",
            convergence_status="QC_CLASSIFIED",
            parameters=parameters,
            metrics={
                "series_status": result.series_status,
                "good_frames": result.good_frames,
                "suspect_frames": result.suspect_frames,
                "bad_frames": result.bad_frames,
                "median_residual_robust_sigma_mag": baseline,
                "frame_scatter_mad_mag": frame_scatter_mad,
                "suspect_residual_limit_mag": suspect_limit,
                "bad_residual_limit_mag": bad_limit,
                "absolute_bad_residual_limit_mag": result.absolute_bad_residual_limit_mag,
                "median_sigma_systematic_mag": result.median_sigma_systematic_mag,
                "max_sigma_systematic_mag": result.max_sigma_systematic_mag,
                "median_sigma_frame_systematic_mag": result.median_sigma_frame_systematic_mag,
                "max_sigma_frame_systematic_mag": result.max_sigma_frame_systematic_mag,
                "frames_with_spatial_systematic": result.frames_with_spatial_systematic,
            },
            frame_terms=[_frame_term(row) for row in stats],
        )
        result.normalized_calibration_iteration_id = int(normalized.calibration_iteration_id)
        result.normalized_frame_terms = int(normalized.frame_terms)
        _persist_frame_flags(session, stats)
        session.commit()

    if out_dir:
        summary_path, frames_path = export_photometric_frame_qc(result, stats, out_dir=out_dir, diagnostics_label=diagnostics_label)
        result.diagnostics_summary_json = str(summary_path)
        result.diagnostics_frames_csv = str(frames_path)
    return result


def derive_residual_qc_limits(
    frame_sigmas: Iterable[Any],
    *,
    suspect_sigma: float = 3.0,
    bad_sigma: float = 6.0,
    suspect_ratio: float = 1.5,
    bad_ratio: float = 2.5,
    absolute_suspect_residual_sigma_mag: float = 0.05,
    absolute_bad_residual_sigma_mag: float = 0.10,
) -> tuple[float | None, float | None, float, float]:
    """Return series baseline, frame-scatter MAD and conservative QC limits."""

    clean = [float(value) for value in frame_sigmas if _finite(value)]
    baseline = _median(clean)
    frame_scatter_mad = _mad(clean)
    robust_frame_scale = 1.4826 * frame_scatter_mad if frame_scatter_mad is not None else 0.0
    if baseline is None:
        return (
            None,
            frame_scatter_mad,
            float(absolute_suspect_residual_sigma_mag),
            float(absolute_bad_residual_sigma_mag),
        )
    suspect_limit = min(
        float(absolute_suspect_residual_sigma_mag),
        max(
            float(baseline) * float(suspect_ratio),
            float(baseline) + float(suspect_sigma) * robust_frame_scale,
        ),
    )
    bad_limit = min(
        float(absolute_bad_residual_sigma_mag),
        max(
            float(baseline) * float(bad_ratio),
            float(baseline) + float(bad_sigma) * robust_frame_scale,
        ),
    )
    bad_limit = max(float(bad_limit), float(suspect_limit) * 1.25)
    return float(baseline), frame_scatter_mad, float(suspect_limit), float(bad_limit)


def classify_photometric_frame(
    *,
    n_reference: int,
    residual_robust_sigma_mag: float | None,
    spatial_peak_to_peak_mag: float | None,
    min_reference_stars_per_frame: int,
    baseline_residual_robust_sigma_mag: float | None,
    suspect_residual_limit_mag: float,
    bad_residual_limit_mag: float,
    absolute_bad_residual_sigma_mag: float,
    spatial_suspect_peak_to_peak_mag: float,
    spatial_bad_peak_to_peak_mag: float,
) -> tuple[str, list[str], float]:
    """Classify one frame without applying any magnitude correction.

    Relative residual and spatial anomalies are evidence that an epoch deserves
    lower statistical weight, not sufficient evidence to delete it. ``BAD`` is
    reserved for hard failures only.
    """

    reasons: list[str] = []
    status = "GOOD"
    n_reference = int(n_reference)
    hard_min_references = 5
    if n_reference < hard_min_references:
        status = "BAD"
        reasons.append("LOW_REFERENCE_COUNT_HARD")
    elif n_reference < int(min_reference_stars_per_frame):
        status = "SUSPECT"
        reasons.append("LOW_REFERENCE_COUNT")

    residual_sigma = _finite_or_none(residual_robust_sigma_mag)
    if residual_sigma is None:
        status = "BAD"
        reasons.append("NO_RESIDUAL_SCATTER")
    elif residual_sigma >= float(absolute_bad_residual_sigma_mag):
        status = "BAD"
        reasons.append("ABSOLUTE_HIGH_RESIDUAL_SCATTER")
    else:
        if residual_sigma >= float(suspect_residual_limit_mag) and status != "BAD":
            status = "SUSPECT"
            reasons.append("HIGH_RESIDUAL_SCATTER")
        if residual_sigma >= float(bad_residual_limit_mag) and status != "BAD":
            status = "SUSPECT"
            reasons.append("VERY_HIGH_RELATIVE_RESIDUAL_SCATTER")

    spatial_ptp = _finite_or_none(spatial_peak_to_peak_mag)
    if spatial_ptp is not None and spatial_ptp >= float(spatial_suspect_peak_to_peak_mag):
        reasons.append("SPATIAL_SYSTEMATIC")
        if spatial_ptp >= float(spatial_bad_peak_to_peak_mag):
            reasons.append("SEVERE_SPATIAL_SYSTEMATIC")
        if status == "GOOD":
            status = "SUSPECT"

    sigma_sys = 0.0
    baseline = _finite_or_none(baseline_residual_robust_sigma_mag)
    if status in {"SUSPECT", "BAD"} and residual_sigma is not None and baseline is not None:
        sigma_sys = math.sqrt(max(residual_sigma * residual_sigma - baseline * baseline, 0.0))
    return status, sorted(set(reasons)), float(sigma_sys)

def export_photometric_frame_qc(result: PhotometricFrameQcResult, stats: list[PhotometricFrameQcStat], *, out_dir: str | Path, diagnostics_label: str | None = None) -> tuple[Path, Path]:
    root = Path(out_dir) / "photometric_frame_qc"
    if diagnostics_label:
        root = root / str(diagnostics_label)
    root.mkdir(parents=True, exist_ok=True)
    summary = root / "photometric_frame_qc_summary.json"
    frames = root / "photometric_frame_qc_frames.csv"
    summary.write_text(json.dumps({"summary": asdict(result), "frames": [asdict(row) for row in stats]}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with frames.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PhotometricFrameQcStat.__dataclass_fields__))
        writer.writeheader()
        for row in stats:
            payload = asdict(row)
            payload["reasons"] = ";".join(row.reasons)
            writer.writerow(payload)
    return summary, frames


def _frame_term(row: PhotometricFrameQcStat):
    from grandgousier.calibration.normalized_terms import FrameTermInput
    return FrameTermInput(
        frame_id=int(row.frame_id),
        term_kind="PHOTOMETRIC_FRAME_QC",
        model_name=row.status.lower(),
        coefficient_0=0.0,
        scatter_mad=row.residual_mad_mag,
        scatter_rms=row.residual_rms_mag,
        n_candidates=int(row.n_reference_candidates),
        n_used=int(row.n_reference_used),
        n_reference=int(row.n_reference_used),
        accepted=row.status != "BAD",
        quality_status=row.status,
        details={
            "algorithm": PHOTOMETRIC_FRAME_QC_ALGORITHM,
            "status": row.status,
            "science_action": row.science_action,
            "reasons": list(row.reasons),
            "sigma_systematic_mag": float(row.sigma_systematic_mag),
            "sigma_frame_systematic_mag": float(row.sigma_frame_systematic_mag),
            "absolute_bad_residual_limit_mag": row.absolute_bad_residual_limit_mag,
            "residual_robust_sigma_mag": row.residual_robust_sigma_mag,
            "series_residual_robust_sigma_median_mag": row.series_residual_robust_sigma_median_mag,
            "spatial_model": row.spatial_model,
            "spatial_bic_improvement": row.spatial_bic_improvement,
            "spatial_peak_to_peak_mag": row.spatial_peak_to_peak_mag,
            "psf_summary": _psf_summary(row),
        },
    )


def _persist_frame_flags(session, stats: list[PhotometricFrameQcStat]) -> None:
    from sqlalchemy import text
    statement = text("""
        UPDATE frames
        SET quality_flags = COALESCE(quality_flags, '{}'::jsonb) || CAST(:payload AS jsonb)
        WHERE frame_id = :frame_id
    """)
    for row in stats:
        qc = {
            "algorithm": PHOTOMETRIC_FRAME_QC_ALGORITHM,
            "status": row.status,
            "science_action": row.science_action,
            "reasons": list(row.reasons),
            "n_reference_used": row.n_reference_used,
            "residual_robust_sigma_mag": row.residual_robust_sigma_mag,
            "series_residual_robust_sigma_median_mag": row.series_residual_robust_sigma_median_mag,
            "suspect_residual_limit_mag": row.suspect_residual_limit_mag,
            "bad_residual_limit_mag": row.bad_residual_limit_mag,
            "sigma_systematic_mag": row.sigma_systematic_mag,
            "sigma_frame_systematic_mag": row.sigma_frame_systematic_mag,
            "absolute_bad_residual_limit_mag": row.absolute_bad_residual_limit_mag,
            "spatial_model": row.spatial_model,
            "spatial_bic_improvement": row.spatial_bic_improvement,
            "spatial_peak_to_peak_mag": row.spatial_peak_to_peak_mag,
        }
        payload = {PHOTOMETRIC_FRAME_QC_FLAG: qc, FRAME_PSF_SUMMARY_FLAG: _psf_summary(row)}
        if row.status == "BAD":
            payload[BAD_FRAME_REJECT_FLAG] = True
        else:
            payload[BAD_FRAME_REJECT_FLAG] = False
        payload["PHOTOMETRIC_FRAME_SUSPECT"] = row.status == "SUSPECT"
        session.execute(statement, {"frame_id": int(row.frame_id), "payload": json.dumps(payload, ensure_ascii=False)})


def _psf_summary(row: PhotometricFrameQcStat) -> dict[str, Any]:
    return {
        "n_psf_sources": row.psf_shape_sources,
        "fwhm_pix_median": row.psf_fwhm_pix_median,
        "fwhm_arcsec_median": row.psf_fwhm_arcsec_median,
        "fwhm_major_pix_median": row.psf_fwhm_major_pix_median,
        "fwhm_minor_pix_median": row.psf_fwhm_minor_pix_median,
        "ellipticity_median": row.psf_ellipticity_median,
        "position_angle_deg": row.psf_position_angle_deg,
        "fwhm_spatial_model": row.psf_fwhm_spatial_model,
        "fwhm_spatial_peak_to_peak_pix": row.psf_fwhm_spatial_peak_to_peak_pix,
        "ellipticity_spatial_model": row.psf_ellipticity_spatial_model,
        "ellipticity_spatial_peak_to_peak": row.psf_ellipticity_spatial_peak_to_peak,
    }


def _load_psf_context(
    *,
    aperture_context_frames_csv: str | Path | None,
    psf_frames_csv: str | Path | None,
) -> dict[int, dict[str, Any]]:
    """Merge existing aperture context and dedicated PSF-field summaries.

    The dedicated PSF diagnostic file is optional and column names have evolved,
    so aliases are normalized defensively. No FITS file is reread here.
    """

    merged: dict[int, dict[str, Any]] = {}

    def read_rows(path: str | Path | None) -> list[dict[str, Any]]:
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        with p.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row.get("frame_id")]

    for raw in read_rows(aperture_context_frames_csv):
        merged[int(raw["frame_id"])] = dict(raw)

    for raw in read_rows(psf_frames_csv):
        frame_id = int(raw["frame_id"])
        row = merged.setdefault(frame_id, {})
        normalized = {
            "psf_shape_sources": _first_present(
                raw,
                "psf_shape_sources", "n_psf_sources", "n_shape_sources",
                "n_sources_used", "n_used", "n_sources",
            ),
            "psf_fwhm_pix_median": _first_present(
                raw,
                "psf_fwhm_pix_median", "fwhm_pix_median", "fwhm_median_pix",
                "fwhm_geom_median", "fwhm_geometric_median_pix",
                "fwhm_geom_median_pix", "fwhm_median",
            ),
            "psf_fwhm_arcsec_median": _first_present(
                raw,
                "psf_fwhm_arcsec_median", "fwhm_arcsec_median", "fwhm_median_arcsec",
            ),
            "psf_fwhm_major_pix_median": _first_present(
                raw,
                "psf_fwhm_major_pix_median", "fwhm_major_pix_median", "fwhm_major_median",
            ),
            "psf_fwhm_minor_pix_median": _first_present(
                raw,
                "psf_fwhm_minor_pix_median", "fwhm_minor_pix_median", "fwhm_minor_median",
            ),
            "psf_ellipticity_median": _first_present(
                raw,
                "psf_ellipticity_median", "ellipticity_median", "median_ellipticity",
            ),
            "psf_position_angle_deg": _first_present(
                raw,
                "psf_position_angle_deg", "position_angle_deg", "theta_deg",
                "orientation_mean_deg", "orientation_deg", "mean_position_angle_deg",
            ),
            "psf_fwhm_spatial_peak_to_peak_pix": _first_present(
                raw,
                "psf_fwhm_spatial_peak_to_peak_pix",
                "fwhm_spatial_peak_to_peak_pix",
                "fwhm_spatial_peak_to_peak",
            ),
            "psf_ellipticity_spatial_peak_to_peak": _first_present(
                raw,
                "psf_ellipticity_spatial_peak_to_peak",
                "ellipticity_spatial_peak_to_peak",
            ),
        }
        fwhm_model, fwhm_ptp = _best_psf_spatial_fit(raw, "fwhm_geom")
        ellip_model, ellip_ptp = _best_psf_spatial_fit(raw, "ellipticity")
        if normalized.get("psf_fwhm_spatial_peak_to_peak_pix") in (None, "") and fwhm_ptp is not None:
            normalized["psf_fwhm_spatial_peak_to_peak_pix"] = fwhm_ptp
        if fwhm_model is not None:
            normalized["psf_fwhm_spatial_model"] = fwhm_model
        if normalized.get("psf_ellipticity_spatial_peak_to_peak") in (None, "") and ellip_ptp is not None:
            normalized["psf_ellipticity_spatial_peak_to_peak"] = ellip_ptp
        if ellip_model is not None:
            normalized["psf_ellipticity_spatial_model"] = ellip_model
        for key, value in normalized.items():
            if value not in (None, ""):
                row[key] = value
    return merged


def _best_psf_spatial_fit(row: dict[str, Any], prefix: str) -> tuple[str | None, float | None]:
    """Select the successful PSF constant/plane/quadratic model with minimum BIC."""

    candidates: list[tuple[float, str, float | None]] = []
    for model in ("constant", "plane", "quadratic"):
        status = str(row.get(f"{prefix}_{model}_status") or "").upper()
        bic = _finite_or_none(row.get(f"{prefix}_{model}_bic"))
        if status not in {"OK", "ACCEPTED"} or bic is None:
            continue
        ptp = _finite_or_none(row.get(f"{prefix}_{model}_spatial_peak_to_peak"))
        candidates.append((bic, model, ptp))
    if not candidates:
        return None, None
    _bic, model, ptp = min(candidates, key=lambda item: item[0])
    return model, ptp


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None

def _str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _detector_bounds(rows: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    xs = [float(row["x"]) for row in rows if _finite(row.get("x"))]
    ys = [float(row["y"]) for row in rows if _finite(row.get("y"))]
    if not xs or not ys:
        return (0.0, 1.0, 0.0, 1.0)
    return min(xs), max(xs), min(ys), max(ys)


def _weight(row: dict[str, Any]) -> float:
    err = _finite_or_none(row.get("mag_err_random"))
    if err is None or err <= 0:
        err = _finite_or_none(row.get("mag_err"))
    return 1.0 / max(float(err or 1.0) ** 2, 1e-8)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _finite_or_none(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except Exception:
        return None


def _median(values: Iterable[Any]) -> float | None:
    clean = [float(v) for v in values if _finite(v)]
    return float(median(clean)) if clean else None


def _mad(values: Iterable[Any]) -> float | None:
    clean = [float(v) for v in values if _finite(v)]
    if not clean:
        return None
    med = float(median(clean))
    return float(median(abs(v - med) for v in clean))


def _rms(values: Iterable[Any]) -> float | None:
    clean = np.asarray([float(v) for v in values if _finite(v)], dtype=float)
    if clean.size < 2:
        return None
    return float(np.sqrt(np.mean((clean - np.mean(clean)) ** 2)))
