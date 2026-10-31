from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from grandgousier.db.models import CalibrationVersion, Measurement



SPATIAL_ENSEMBLE_ALGORITHM = "robust_zero_mean_spatial_residual_v3"
DEFAULT_BULK_INSERT_BATCH_SIZE = 1000


@dataclass
class SpatialEnsembleFrameStat:
    frame_id: int
    n_reference_candidates: int = 0
    n_reference_used: int = 0
    selected_model: str = "identity"
    selection_reason: str | None = None
    accepted: bool = False
    fallback_reason: str | None = None
    identity_bic: float | None = None
    plane_bic: float | None = None
    quadratic_bic: float | None = None
    selected_bic: float | None = None
    bic_improvement: float | None = None
    residual_mad_before: float | None = None
    residual_mad_after: float | None = None
    residual_mad_delta: float | None = None
    residual_mad_fractional_improvement: float | None = None
    residual_rms_before: float | None = None
    residual_rms_after: float | None = None
    residual_rms_delta: float | None = None
    proposed_model: str | None = None
    proposed_residual_mad_after: float | None = None
    proposed_residual_mad_delta: float | None = None
    proposed_residual_mad_fractional_improvement: float | None = None
    proposed_residual_rms_after: float | None = None
    proposed_residual_rms_delta: float | None = None
    proposed_spatial_peak_to_peak: float | None = None
    proposed_max_abs_spatial_deviation: float | None = None
    spatial_peak_to_peak: float | None = None
    max_abs_spatial_deviation: float | None = None
    coefficient_x: float | None = None
    coefficient_y: float | None = None
    coefficient_xx: float | None = None
    coefficient_xy: float | None = None
    coefficient_yy: float | None = None
    pivot_x_norm: float | None = None
    pivot_y_norm: float | None = None
    model_error_median: float | None = None


@dataclass
class SpatialEnsembleCorrectionResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_name: str | None = None
    instrument_unit_id: int | None = None
    selected_frames: int = 0
    checked_frames: int = 0
    frames_with_plane: int = 0
    frames_with_quadratic: int = 0
    frames_with_identity: int = 0
    frames_robust_non_degradation_fallback: int = 0
    frames_invalid: int = 0
    selected_measurements: int = 0
    calibrated: int = 0
    copied_identity: int = 0
    inserted_or_updated: int = 0
    deleted_existing: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_objects: int = 0
    actually_excluded_reference_object_ids: list[int] = field(default_factory=list)
    actually_excluded_reference_objects: int = 0
    good_frame_fraction: float | None = None
    median_residual_mad_before: float | None = None
    median_residual_mad_after: float | None = None
    median_spatial_peak_to_peak: float | None = None
    quality_gate_passed: bool = False
    quality_gate_failures: list[str] = field(default_factory=list)
    quality_gate_thresholds: dict[str, Any] = field(default_factory=dict)
    global_identity_fallback: bool = False
    diagnostics_summary_json: str | None = None
    diagnostics_frames_csv: str | None = None
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    normalized_write_status: str | None = None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def apply_spatial_ensemble_correction(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    reference_object_ids: Iterable[int],
    frame_ids: list[int] | None = None,
    instrument_unit_id: int | None = None,
    max_order: int = 2,
    min_ref_stars_per_frame: int = 30,
    clip_sigma: float = 3.5,
    n_iterations: int = 3,
    min_bic_improvement: float = 6.0,
    min_spatial_peak_to_peak: float = 0.003,
    max_residual_mad: float | None = 0.05,
    max_residual_mad_degradation: float | None = 0.0,
    max_abs_spatial_deviation: float = 0.15,
    min_good_frame_fraction: float = 0.8,
    out_dir: str | Path | None = None,
    diagnostics_label: str | None = None,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    immutable_generation: bool = False,
) -> SpatialEnsembleCorrectionResult:
    """Remove a zero-mean per-frame spatial residual from an ensemble calibration.

    The conventional ensemble calibration remains responsible for the frame-common
    zero point.  This stage fits only centered detector basis functions, so the
    applied correction has zero weighted mean over reference stars on every frame.
    """

    if frame_ids is not None and not frame_ids:
        raise ValueError("explicit frame_ids selection must not be empty")

    from sqlalchemy import select
    from grandgousier.db.models import CalibrationVersion

    if max_order not in {1, 2}:
        raise ValueError("max_order must be 1 or 2")
    if min_ref_stars_per_frame < 6:
        raise ValueError("min_ref_stars_per_frame must be >= 6")
    if clip_sigma <= 0 or n_iterations < 1:
        raise ValueError("clip_sigma must be positive and n_iterations must be >= 1")
    if min_bic_improvement < 0 or min_spatial_peak_to_peak < 0:
        raise ValueError("BIC and spatial-amplitude thresholds must be non-negative")
    if max_residual_mad_degradation is not None and max_residual_mad_degradation < 0:
        raise ValueError("max_residual_mad_degradation must be non-negative or None")
    if max_abs_spatial_deviation <= 0:
        raise ValueError("max_abs_spatial_deviation must be positive")
    if not 0 <= min_good_frame_fraction <= 1:
        raise ValueError("min_good_frame_fraction must be between 0 and 1")

    reference_ids = sorted({int(value) for value in reference_object_ids})
    result = SpatialEnsembleCorrectionResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        instrument_unit_id=instrument_unit_id,
        reference_object_ids=reference_ids,
        insert_batch_size=int(insert_batch_size),
        dry_run=bool(dry_run),
    )

    source_cv = session.scalar(
        select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name)
    )
    if source_cv is None:
        raise ValueError(f"Source calibration not found: {source_calibration_name!r}")
    result.source_calibration_version_id = int(source_cv.calibration_version_id)

    selected_frame_ids = _resolve_frame_ids(
        session,
        calibration_version_id=int(source_cv.calibration_version_id),
        frame_ids=frame_ids,
        instrument_unit_id=instrument_unit_id,
    )
    result.selected_frames = len(selected_frame_ids)

    target_cv = _get_or_create_target_calibration(
        session,
        name=target_calibration_name,
        source_cv=source_cv,
        instrument_unit_id=instrument_unit_id,
        parameters={
            "algorithm": SPATIAL_ENSEMBLE_ALGORITHM,
            "max_order": int(max_order),
            "min_ref_stars_per_frame": int(min_ref_stars_per_frame),
            "clip_sigma": float(clip_sigma),
            "n_iterations": int(n_iterations),
            "min_bic_improvement": float(min_bic_improvement),
            "min_spatial_peak_to_peak": float(min_spatial_peak_to_peak),
            "max_residual_mad": max_residual_mad,
            "max_residual_mad_degradation": max_residual_mad_degradation,
            "max_abs_spatial_deviation": float(max_abs_spatial_deviation),
            "min_good_frame_fraction": float(min_good_frame_fraction),
            "reference_object_ids": reference_ids,
        },
        dry_run=dry_run,
        immutable_generation=immutable_generation,
    )
    if target_cv is not None:
        result.target_calibration_version_id = int(target_cv.calibration_version_id)

    if dry_run or not selected_frame_ids:
        return result

    rows = _load_source_measurements(
        session,
        calibration_version_id=int(source_cv.calibration_version_id),
        frame_ids=selected_frame_ids,
    )
    result.selected_measurements = len(rows)
    if not rows:
        result.quality_gate_failures.append("no source measurements")
        return result

    bounds = _detector_bounds(rows)
    object_centers = _reference_object_centers(rows, reference_ids)
    by_frame: dict[int, list[Measurement]] = {}
    for measurement in rows:
        by_frame.setdefault(int(measurement.frame_id), []).append(measurement)

    frame_stats: list[SpatialEnsembleFrameStat] = []
    frame_models: dict[int, dict[str, Any]] = {}
    for frame_id in selected_frame_ids:
        frame_rows = by_frame.get(int(frame_id), [])
        reference_rows = [
            measurement
            for measurement in frame_rows
            if int(measurement.object_id) in object_centers
            and _finite(measurement.mag_cal)
            and _finite(measurement.x)
            and _finite(measurement.y)
        ]
        fit_rows = [
            {
                "x": float(measurement.x),
                "y": float(measurement.y),
                "residual": float(measurement.mag_cal) - object_centers[int(measurement.object_id)],
                "weight": _measurement_weight(measurement),
            }
            for measurement in reference_rows
        ]
        model = fit_spatial_ensemble_model(
            fit_rows,
            bounds=bounds,
            max_order=max_order,
            min_ref_stars=min_ref_stars_per_frame,
            clip_sigma=clip_sigma,
            n_iterations=n_iterations,
            min_bic_improvement=min_bic_improvement,
            min_spatial_peak_to_peak=min_spatial_peak_to_peak,
            max_residual_mad=max_residual_mad,
            max_residual_mad_degradation=max_residual_mad_degradation,
            max_abs_spatial_deviation=max_abs_spatial_deviation,
        )
        frame_models[int(frame_id)] = model
        frame_stats.append(_frame_stat(int(frame_id), model))

    result.checked_frames = len(frame_stats)
    valid_stats = [row for row in frame_stats if row.accepted]
    result.good_frame_fraction = (
        len(valid_stats) / len(frame_stats) if frame_stats else 0.0
    )
    result.quality_gate_thresholds = {
        "min_good_frame_fraction": float(min_good_frame_fraction),
        "max_residual_mad": max_residual_mad,
        "max_residual_mad_degradation": max_residual_mad_degradation,
        "max_abs_spatial_deviation": float(max_abs_spatial_deviation),
    }
    if result.good_frame_fraction < float(min_good_frame_fraction):
        result.quality_gate_failures.append(
            f"good_frame_fraction={result.good_frame_fraction:.4f} < {min_good_frame_fraction:.4f}"
        )
    result.quality_gate_passed = not result.quality_gate_failures
    result.global_identity_fallback = not result.quality_gate_passed

    if result.global_identity_fallback:
        result.warnings.append(
            "Spatial-ensemble quality gate failed; final calibration is an identity copy of the conventional ensemble calibration."
        )

    result.frames_with_plane = sum(
        1 for row in frame_stats if row.selected_model == "plane" and row.accepted
    )
    result.frames_with_quadratic = sum(
        1 for row in frame_stats if row.selected_model == "quadratic" and row.accepted
    )
    result.frames_with_identity = sum(
        1 for row in frame_stats if row.selected_model == "identity" and row.accepted
    )
    result.frames_robust_non_degradation_fallback = sum(
        1 for row in frame_stats if row.fallback_reason == "robust_non_degradation_gate"
    )
    result.frames_invalid = len(frame_stats) - len(valid_stats)
    result.median_residual_mad_before = _median(
        row.residual_mad_before for row in valid_stats
    )
    result.median_residual_mad_after = _median(
        row.residual_mad_after for row in valid_stats
    )
    result.median_spatial_peak_to_peak = _median(
        row.spatial_peak_to_peak
        for row in valid_stats
        if row.selected_model != "identity"
    )

    target_id = int(target_cv.calibration_version_id)
    # 0.7 clean storage never publishes derived full Measurement rows.  Count
    # effective corrected/identity rows for diagnostics while persisting only
    # the normalized spatial model below.
    result.deleted_existing = 0
    for measurement in rows:
        frame_id = int(measurement.frame_id)
        model = frame_models.get(frame_id) or {"selected_model": "identity", "accepted": False}
        use_spatial = bool(
            result.quality_gate_passed
            and model.get("accepted")
            and model.get("selected_model") in {"plane", "quadratic"}
            and _finite(measurement.x)
            and _finite(measurement.y)
        )
        if use_spatial:
            result.calibrated += 1
        else:
            result.copied_identity += 1
    result.insert_batches = 0
    result.inserted_or_updated = 0

    # Persist the compact spatial model so the immutable calibration chain is
    # reconstructed without intermediate Measurement rows.  No schema extension is needed: the generic frame-term columns
    # hold the constant/linear coefficients and the quadratic coefficients live
    # in JSON details.  detector_bounds are common to the whole iteration.
    from grandgousier.calibration.normalized_terms import replace_calibration_iteration

    normalized = replace_calibration_iteration(
        session,
        calibration_version_id=target_id,
        source_calibration_version_id=int(source_cv.calibration_version_id),
        iteration_kind="SPATIAL_ENSEMBLE",
        iteration_no=1,
        algorithm=SPATIAL_ENSEMBLE_ALGORITHM,
        status="COMPLETE",
        convergence_status=(
            "QUALITY_GATE_PASSED" if result.quality_gate_passed else "GLOBAL_IDENTITY_FALLBACK"
        ),
        parameters={
            "algorithm": SPATIAL_ENSEMBLE_ALGORITHM,
            "detector_bounds": [float(value) for value in bounds],
            "max_order": int(max_order),
            "min_ref_stars_per_frame": int(min_ref_stars_per_frame),
            "clip_sigma": float(clip_sigma),
            "n_iterations": int(n_iterations),
            "min_bic_improvement": float(min_bic_improvement),
            "min_spatial_peak_to_peak": float(min_spatial_peak_to_peak),
            "max_residual_mad": max_residual_mad,
            "max_residual_mad_degradation": max_residual_mad_degradation,
            "max_abs_spatial_deviation": float(max_abs_spatial_deviation),
            "min_good_frame_fraction": float(min_good_frame_fraction),
            "zero_mean_basis": True,
            "application_semantics": "mag_out = mag_source - spatial_correction(x_norm,y_norm)",
        },
        metrics={
            "quality_gate_passed": bool(result.quality_gate_passed),
            "global_identity_fallback": bool(result.global_identity_fallback),
            "good_frame_fraction": result.good_frame_fraction,
            "frames_with_plane": int(result.frames_with_plane),
            "frames_with_quadratic": int(result.frames_with_quadratic),
            "frames_with_identity": int(result.frames_with_identity),
            "frames_robust_non_degradation_fallback": int(
                result.frames_robust_non_degradation_fallback
            ),
            "frames_invalid": int(result.frames_invalid),
            "median_residual_mad_before": result.median_residual_mad_before,
            "median_residual_mad_after": result.median_residual_mad_after,
            "median_spatial_peak_to_peak": result.median_spatial_peak_to_peak,
        },
        frame_terms=[
            _normalized_spatial_frame_term(
                frame_id=int(frame_id),
                model=frame_models.get(int(frame_id))
                or {"selected_model": "identity", "accepted": False},
                bounds=bounds,
                global_quality_gate_passed=bool(result.quality_gate_passed),
            )
            for frame_id in selected_frame_ids
        ],
    )
    result.normalized_calibration_iteration_id = int(normalized.calibration_iteration_id)
    result.normalized_frame_terms = int(normalized.frame_terms)
    result.normalized_write_status = "NORMALIZED_ONLY"

    if out_dir:
        summary_path, frames_path = export_spatial_ensemble_diagnostics(
            result,
            frame_stats,
            out_dir=out_dir,
            diagnostics_label=diagnostics_label,
        )
        result.diagnostics_summary_json = str(summary_path)
        result.diagnostics_frames_csv = str(frames_path)

    return result


def fit_spatial_ensemble_model(
    rows: list[dict[str, Any]],
    *,
    bounds: tuple[float, float, float, float] | None = None,
    max_order: int = 2,
    min_ref_stars: int = 30,
    clip_sigma: float = 3.5,
    n_iterations: int = 3,
    min_bic_improvement: float = 6.0,
    min_spatial_peak_to_peak: float = 0.003,
    max_residual_mad: float | None = 0.05,
    max_residual_mad_degradation: float | None = 0.0,
    max_abs_spatial_deviation: float = 0.15,
) -> dict[str, Any]:
    if max_residual_mad_degradation is not None and max_residual_mad_degradation < 0:
        raise ValueError("max_residual_mad_degradation must be non-negative or None")

    finite_rows = []
    for row in rows:
        x = _finite_or_none(row.get("x"))
        y = _finite_or_none(row.get("y"))
        residual = _finite_or_none(row.get("residual"))
        weight = _finite_or_none(row.get("weight"))
        if x is None or y is None or residual is None:
            continue
        finite_rows.append((x, y, residual, weight if weight is not None and weight > 0 else 1.0))

    n = len(finite_rows)
    empty = {
        "accepted": False,
        "selected_model": "identity",
        "selection_reason": None,
        "fallback_reason": None,
        "n_candidates": n,
        "n_used": 0,
    }
    if n < int(min_ref_stars):
        empty["fallback_reason"] = f"insufficient_reference_stars:{n}<{min_ref_stars}"
        return empty

    arr = np.asarray(finite_rows, dtype=float)
    x, y, residual, weight = arr.T
    if bounds is None:
        bounds = (float(np.min(x)), float(np.max(x)), float(np.min(y)), float(np.max(y)))
    xn, yn = _normalize_xy(x, y, bounds)

    identity = _robust_spatial_fit(
        xn,
        yn,
        residual,
        weight,
        order=0,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
    )
    candidates: dict[str, dict[str, Any]] = {"identity": identity}
    candidates["plane"] = _robust_spatial_fit(
        xn,
        yn,
        residual,
        weight,
        order=1,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
    )
    if max_order >= 2:
        candidates["quadratic"] = _robust_spatial_fit(
            xn,
            yn,
            residual,
            weight,
            order=2,
            clip_sigma=clip_sigma,
            n_iterations=n_iterations,
        )

    best_name = "identity"
    best = identity
    for name in ("plane", "quadratic"):
        fit = candidates.get(name)
        if not fit or fit.get("bic") is None:
            continue
        if best.get("bic") is None or float(fit["bic"]) < float(best["bic"]):
            best_name, best = name, fit

    identity_bic = _finite_or_none(identity.get("bic"))
    best_bic = _finite_or_none(best.get("bic"))
    improvement = (
        identity_bic - best_bic
        if identity_bic is not None and best_bic is not None
        else 0.0
    )

    selected_name = best_name
    selection_reason = "best_bic"
    if best_name != "identity" and improvement < float(min_bic_improvement):
        selected_name = "identity"
        selection_reason = "insufficient_bic_improvement"

    selected = candidates[selected_name]
    spatial_ptp, max_abs_dev = _spatial_model_extent(selected, order=_order_for_name(selected_name))
    if selected_name != "identity" and spatial_ptp < float(min_spatial_peak_to_peak):
        selected_name = "identity"
        selected = identity
        selection_reason = "spatial_amplitude_below_threshold"
        spatial_ptp, max_abs_dev = 0.0, 0.0
    if selected_name != "identity" and max_abs_dev > float(max_abs_spatial_deviation):
        selected_name = "identity"
        selected = identity
        selection_reason = "spatial_safety_limit"
        spatial_ptp, max_abs_dev = 0.0, 0.0

    # 0.5.44.9: BIC alone is not sufficient for science acceptance.  A spatial
    # surface may fit structured residuals while slightly increasing the robust
    # frame scatter.  Preserve such a frame as valid, but fall back to identity
    # unless the proposed spatial model is non-degrading in residual MAD.
    proposed_name = selected_name
    proposed = selected
    proposed_spatial_ptp = spatial_ptp
    proposed_max_abs_dev = max_abs_dev
    identity_mad = _finite_or_none(identity.get("residual_mad"))
    proposed_mad = _finite_or_none(proposed.get("residual_mad"))
    fallback_reason = None
    if (
        selected_name != "identity"
        and max_residual_mad_degradation is not None
        and identity_mad is not None
        and proposed_mad is not None
        and proposed_mad > identity_mad + float(max_residual_mad_degradation)
    ):
        selected_name = "identity"
        selected = identity
        selection_reason = "robust_non_degradation_gate"
        fallback_reason = "robust_non_degradation_gate"
        spatial_ptp, max_abs_dev = 0.0, 0.0

    residual_mad_after = _finite_or_none(selected.get("residual_mad"))
    accepted = True
    if max_residual_mad is not None and (
        residual_mad_after is None or residual_mad_after > float(max_residual_mad)
    ):
        accepted = False
        fallback_reason = "residual_mad_quality_gate"
        selected_name = "identity"
        selected = identity
        selection_reason = "invalid_frame_identity_fallback"
        spatial_ptp, max_abs_dev = 0.0, 0.0

    selected_mad = _finite_or_none(selected.get("residual_mad"))
    selected_rms = _finite_or_none(selected.get("residual_rms"))
    identity_rms = _finite_or_none(identity.get("residual_rms"))
    proposed_rms = _finite_or_none(proposed.get("residual_rms"))
    residual_mad_delta = _difference(selected_mad, identity_mad)
    residual_rms_delta = _difference(selected_rms, identity_rms)
    proposed_mad_delta = _difference(proposed_mad, identity_mad)
    proposed_rms_delta = _difference(proposed_rms, identity_rms)

    coeff = list(selected.get("coefficients") or [])
    cov = selected.get("covariance")
    result = {
        "accepted": accepted,
        "selected_model": selected_name,
        "selection_reason": selection_reason,
        "fallback_reason": fallback_reason,
        "n_candidates": n,
        "n_used": int(selected.get("n_used") or 0),
        "identity_bic": identity.get("bic"),
        "plane_bic": candidates.get("plane", {}).get("bic"),
        "quadratic_bic": candidates.get("quadratic", {}).get("bic"),
        "selected_bic": selected.get("bic"),
        "bic_improvement": improvement if selected_name != "identity" else 0.0,
        "residual_mad_before": identity.get("residual_mad"),
        "residual_mad_after": selected.get("residual_mad"),
        "residual_mad_delta": residual_mad_delta,
        "residual_mad_fractional_improvement": _fractional_improvement(identity_mad, selected_mad),
        "residual_rms_before": identity.get("residual_rms"),
        "residual_rms_after": selected.get("residual_rms"),
        "residual_rms_delta": residual_rms_delta,
        "proposed_model": proposed_name,
        "proposed_residual_mad_after": proposed.get("residual_mad"),
        "proposed_residual_mad_delta": proposed_mad_delta,
        "proposed_residual_mad_fractional_improvement": _fractional_improvement(identity_mad, proposed_mad),
        "proposed_residual_rms_after": proposed.get("residual_rms"),
        "proposed_residual_rms_delta": proposed_rms_delta,
        "proposed_spatial_peak_to_peak": proposed_spatial_ptp,
        "proposed_max_abs_spatial_deviation": proposed_max_abs_dev,
        "spatial_peak_to_peak": spatial_ptp,
        "max_abs_spatial_deviation": max_abs_dev,
        "coefficients": coeff,
        "covariance": cov,
        "basis_means": selected.get("basis_means"),
        "pivot_x_norm": selected.get("pivot_x_norm"),
        "pivot_y_norm": selected.get("pivot_y_norm"),
        "order": _order_for_name(selected_name),
    }
    result.update(_coefficient_mapping(coeff, _order_for_name(selected_name)))
    return result


def predict_spatial_ensemble_correction(
    model: dict[str, Any],
    *,
    x: float,
    y: float,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float]:
    order = int(model.get("order") or 0)
    if order <= 0:
        return 0.0, 0.0
    xn, yn = _normalize_xy(np.asarray([x]), np.asarray([y]), bounds)
    raw = _basis_terms(xn, yn, order=order)[0]
    means = np.asarray(model.get("basis_means") or np.zeros(raw.shape[0]), dtype=float)
    centered = raw - means
    coeff = np.asarray(model.get("coefficients") or [], dtype=float)
    if coeff.size < centered.size + 1:
        return 0.0, 0.0
    correction = float(np.dot(centered, coeff[1 : centered.size + 1]))
    covariance = model.get("covariance")
    error = 0.0
    if covariance is not None:
        cov = np.asarray(covariance, dtype=float)
        vector = np.concatenate([[0.0], centered])
        if cov.ndim == 2 and cov.shape[0] >= vector.size and cov.shape[1] >= vector.size:
            variance = float(vector @ cov[: vector.size, : vector.size] @ vector)
            if math.isfinite(variance) and variance > 0:
                error = math.sqrt(variance)
    return correction, error


def export_spatial_ensemble_diagnostics(
    result: SpatialEnsembleCorrectionResult,
    frame_stats: list[SpatialEnsembleFrameStat],
    *,
    out_dir: str | Path,
    diagnostics_label: str | None = None,
) -> tuple[Path, Path]:
    target = Path(out_dir) / "spatial_ensemble"
    if diagnostics_label:
        safe_label = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in str(diagnostics_label)
        ).strip("_") or "run"
        target = target / safe_label
    target.mkdir(parents=True, exist_ok=True)
    summary_path = target / "spatial_ensemble_summary.json"
    frames_path = target / "spatial_ensemble_frames.csv"
    summary_path.write_text(
        json.dumps(
            {"summary": asdict(result), "frames": [asdict(row) for row in frame_stats]},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    fieldnames = list(SpatialEnsembleFrameStat.__dataclass_fields__.keys())
    with frames_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in frame_stats:
            writer.writerow(asdict(row))
    return summary_path, frames_path


def _robust_spatial_fit(
    x: np.ndarray,
    y: np.ndarray,
    residual: np.ndarray,
    weight: np.ndarray,
    *,
    order: int,
    clip_sigma: float,
    n_iterations: int,
) -> dict[str, Any]:
    mask = np.isfinite(x) & np.isfinite(y) & np.isfinite(residual) & np.isfinite(weight) & (weight > 0)
    coefficients = None
    covariance = None
    basis_means = np.zeros(0, dtype=float)
    pivot_x = None
    pivot_y = None
    for _ in range(max(1, int(n_iterations))):
        if int(mask.sum()) < max(3, 1 + _basis_term_count(order)):
            break
        w = weight[mask]
        xm = x[mask]
        ym = y[mask]
        rm = residual[mask]
        pivot_x = float(np.average(xm, weights=w))
        pivot_y = float(np.average(ym, weights=w))
        raw_basis = _basis_terms(xm, ym, order=order)
        if raw_basis.size:
            basis_means = np.average(raw_basis, axis=0, weights=w)
            centered = raw_basis - basis_means
            design = np.column_stack([np.ones(len(rm)), centered])
        else:
            basis_means = np.zeros(0, dtype=float)
            design = np.ones((len(rm), 1), dtype=float)
        sqrt_w = np.sqrt(w)
        wd = design * sqrt_w[:, None]
        wr = rm * sqrt_w
        try:
            coefficients, *_ = np.linalg.lstsq(wd, wr, rcond=None)
        except np.linalg.LinAlgError:
            break
        predicted = design @ coefficients
        fit_residual = rm - predicted
        med = float(np.median(fit_residual))
        mad = float(np.median(np.abs(fit_residual - med)))
        sigma = 1.4826 * mad
        if not math.isfinite(sigma) or sigma <= 0:
            break
        local_keep = np.abs(fit_residual - med) <= float(clip_sigma) * sigma
        indices = np.flatnonzero(mask)
        new_mask = mask.copy()
        new_mask[indices] = local_keep
        if np.array_equal(new_mask, mask):
            break
        mask = new_mask

    if coefficients is None or int(mask.sum()) < max(3, 1 + _basis_term_count(order)):
        return {"bic": None, "n_used": int(mask.sum()), "coefficients": []}

    w = weight[mask]
    xm, ym, rm = x[mask], y[mask], residual[mask]
    raw_basis = _basis_terms(xm, ym, order=order)
    if raw_basis.size:
        basis_means = np.average(raw_basis, axis=0, weights=w)
        centered = raw_basis - basis_means
        design = np.column_stack([np.ones(len(rm)), centered])
    else:
        basis_means = np.zeros(0, dtype=float)
        design = np.ones((len(rm), 1), dtype=float)
    sqrt_w = np.sqrt(w)
    wd = design * sqrt_w[:, None]
    wr = rm * sqrt_w
    coefficients, *_ = np.linalg.lstsq(wd, wr, rcond=None)
    predicted = design @ coefficients
    fit_residual = rm - predicted
    rss = float(np.sum(fit_residual**2))
    n = len(rm)
    k = design.shape[1]
    variance = max(rss / max(1, n - k), 1e-16)
    try:
        covariance = variance * np.linalg.pinv(wd.T @ wd)
    except np.linalg.LinAlgError:
        covariance = None
    bic = float(n * math.log(max(rss / max(1, n), 1e-16)) + k * math.log(max(n, 1)))
    return {
        "bic": bic,
        "n_used": int(n),
        "coefficients": coefficients.tolist(),
        "covariance": covariance.tolist() if covariance is not None else None,
        "basis_means": basis_means.tolist(),
        "pivot_x_norm": float(np.average(xm, weights=w)),
        "pivot_y_norm": float(np.average(ym, weights=w)),
        "residual_mad": _mad(fit_residual),
        "residual_rms": _rms(fit_residual),
        "order": int(order),
    }


def _spatial_model_extent(model: dict[str, Any], *, order: int) -> tuple[float, float]:
    if order <= 0:
        return 0.0, 0.0
    coeff = np.asarray(model.get("coefficients") or [], dtype=float)
    if coeff.size < 1 + _basis_term_count(order):
        return 0.0, 0.0
    grid = np.asarray(
        [(x, y) for y in np.linspace(-1.0, 1.0, 9) for x in np.linspace(-1.0, 1.0, 9)],
        dtype=float,
    )
    raw = _basis_terms(grid[:, 0], grid[:, 1], order=order)
    means = np.asarray(model.get("basis_means") or np.zeros(raw.shape[1]), dtype=float)
    pred = (raw - means) @ coeff[1 : 1 + raw.shape[1]]
    return float(np.ptp(pred)), float(np.max(np.abs(pred)))


def _basis_terms(x: np.ndarray, y: np.ndarray, *, order: int) -> np.ndarray:
    if order <= 0:
        return np.empty((len(np.atleast_1d(x)), 0), dtype=float)
    columns = [np.asarray(x, dtype=float), np.asarray(y, dtype=float)]
    if order >= 2:
        columns.extend([x * x, x * y, y * y])
    return np.column_stack(columns)


def _basis_term_count(order: int) -> int:
    return 0 if order <= 0 else (2 if order == 1 else 5)


def _order_for_name(name: str) -> int:
    return {"identity": 0, "plane": 1, "quadratic": 2}.get(str(name), 0)


def _coefficient_mapping(coefficients: list[float], order: int) -> dict[str, float | None]:
    values = list(coefficients[1:]) if len(coefficients) > 1 else []
    keys = ["coefficient_x", "coefficient_y", "coefficient_xx", "coefficient_xy", "coefficient_yy"]
    result: dict[str, float | None] = {key: None for key in keys}
    for key, value in zip(keys[: _basis_term_count(order)], values):
        result[key] = float(value)
    return result


def _difference(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return float(after - before)


def _fractional_improvement(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before <= 0:
        return None
    return float((before - after) / before)


def _frame_stat(frame_id: int, model: dict[str, Any]) -> SpatialEnsembleFrameStat:
    return SpatialEnsembleFrameStat(
        frame_id=int(frame_id),
        n_reference_candidates=int(model.get("n_candidates") or 0),
        n_reference_used=int(model.get("n_used") or 0),
        selected_model=str(model.get("selected_model") or "identity"),
        selection_reason=model.get("selection_reason"),
        accepted=bool(model.get("accepted")),
        fallback_reason=model.get("fallback_reason"),
        identity_bic=_finite_or_none(model.get("identity_bic")),
        plane_bic=_finite_or_none(model.get("plane_bic")),
        quadratic_bic=_finite_or_none(model.get("quadratic_bic")),
        selected_bic=_finite_or_none(model.get("selected_bic")),
        bic_improvement=_finite_or_none(model.get("bic_improvement")),
        residual_mad_before=_finite_or_none(model.get("residual_mad_before")),
        residual_mad_after=_finite_or_none(model.get("residual_mad_after")),
        residual_mad_delta=_finite_or_none(model.get("residual_mad_delta")),
        residual_mad_fractional_improvement=_finite_or_none(model.get("residual_mad_fractional_improvement")),
        residual_rms_before=_finite_or_none(model.get("residual_rms_before")),
        residual_rms_after=_finite_or_none(model.get("residual_rms_after")),
        residual_rms_delta=_finite_or_none(model.get("residual_rms_delta")),
        proposed_model=model.get("proposed_model"),
        proposed_residual_mad_after=_finite_or_none(model.get("proposed_residual_mad_after")),
        proposed_residual_mad_delta=_finite_or_none(model.get("proposed_residual_mad_delta")),
        proposed_residual_mad_fractional_improvement=_finite_or_none(model.get("proposed_residual_mad_fractional_improvement")),
        proposed_residual_rms_after=_finite_or_none(model.get("proposed_residual_rms_after")),
        proposed_residual_rms_delta=_finite_or_none(model.get("proposed_residual_rms_delta")),
        proposed_spatial_peak_to_peak=_finite_or_none(model.get("proposed_spatial_peak_to_peak")),
        proposed_max_abs_spatial_deviation=_finite_or_none(model.get("proposed_max_abs_spatial_deviation")),
        spatial_peak_to_peak=_finite_or_none(model.get("spatial_peak_to_peak")),
        max_abs_spatial_deviation=_finite_or_none(model.get("max_abs_spatial_deviation")),
        coefficient_x=_finite_or_none(model.get("coefficient_x")),
        coefficient_y=_finite_or_none(model.get("coefficient_y")),
        coefficient_xx=_finite_or_none(model.get("coefficient_xx")),
        coefficient_xy=_finite_or_none(model.get("coefficient_xy")),
        coefficient_yy=_finite_or_none(model.get("coefficient_yy")),
        pivot_x_norm=_finite_or_none(model.get("pivot_x_norm")),
        pivot_y_norm=_finite_or_none(model.get("pivot_y_norm")),
        model_error_median=None,
    )


def _normalized_spatial_frame_term(
    *,
    frame_id: int,
    model: dict[str, Any],
    bounds: tuple[float, float, float, float],
    global_quality_gate_passed: bool,
):
    """Encode one selected spatial model as a normalized frame term."""

    from grandgousier.calibration.normalized_terms import FrameTermInput

    selected_model = str(model.get("selected_model") or "identity")
    order = _order_for_name(selected_model)
    use_spatial = bool(
        global_quality_gate_passed
        and model.get("accepted")
        and selected_model in {"plane", "quadratic"}
    )

    coefficient_0 = 0.0
    coefficient_x = 0.0
    coefficient_y = 0.0
    coefficient_xx = 0.0
    coefficient_xy = 0.0
    coefficient_yy = 0.0
    if use_spatial:
        coefficient_count = _basis_term_count(order)
        coefficients = np.asarray(model.get("coefficients") or [], dtype=float)
        basis_means = np.asarray(model.get("basis_means") or [], dtype=float)
        slopes = coefficients[1 : coefficient_count + 1]
        if slopes.size != coefficient_count:
            use_spatial = False
        else:
            if basis_means.size < coefficient_count:
                basis_means = np.pad(
                    basis_means, (0, coefficient_count - basis_means.size), constant_values=0.0
                )
            coefficient_0 = float(-np.dot(basis_means[:coefficient_count], slopes))
            values = [float(value) for value in slopes]
            coefficient_x = values[0] if len(values) > 0 else 0.0
            coefficient_y = values[1] if len(values) > 1 else 0.0
            coefficient_xx = values[2] if len(values) > 2 else 0.0
            coefficient_xy = values[3] if len(values) > 3 else 0.0
            coefficient_yy = values[4] if len(values) > 4 else 0.0

    if use_spatial:
        quality_status = "APPLIED"
    elif not global_quality_gate_passed:
        quality_status = "GLOBAL_IDENTITY_FALLBACK"
    elif model.get("fallback_reason"):
        quality_status = str(model.get("fallback_reason")).upper()
    elif selected_model == "identity":
        quality_status = "IDENTITY"
    else:
        quality_status = "INVALID"

    return FrameTermInput(
        frame_id=int(frame_id),
        term_kind="SPATIAL_ENSEMBLE",
        model_name=selected_model,
        coefficient_0=float(coefficient_0),
        coefficient_x=float(coefficient_x),
        coefficient_y=float(coefficient_y),
        scatter_mad=_finite_or_none(model.get("residual_mad_after")),
        scatter_rms=_finite_or_none(model.get("residual_rms_after")),
        n_candidates=int(model.get("n_candidates") or 0),
        n_used=int(model.get("n_used") or 0),
        n_reference=int(model.get("n_used") or 0),
        accepted=bool(use_spatial),
        quality_status=quality_status,
        details={
            "algorithm": SPATIAL_ENSEMBLE_ALGORITHM,
            "selected_model": selected_model,
            "proposed_model": model.get("proposed_model"),
            "selection_reason": model.get("selection_reason"),
            "fallback_reason": model.get("fallback_reason"),
            "order": int(order if use_spatial else 0),
            "coefficient_xx": float(coefficient_xx),
            "coefficient_xy": float(coefficient_xy),
            "coefficient_yy": float(coefficient_yy),
            "detector_bounds": [float(value) for value in bounds],
            "spatial_peak_to_peak": _finite_or_none(model.get("spatial_peak_to_peak")),
            "max_abs_spatial_deviation": _finite_or_none(
                model.get("max_abs_spatial_deviation")
            ),
            "residual_mad_before": _finite_or_none(model.get("residual_mad_before")),
            "residual_mad_after": _finite_or_none(model.get("residual_mad_after")),
            "residual_mad_delta": _finite_or_none(model.get("residual_mad_delta")),
            "residual_rms_before": _finite_or_none(model.get("residual_rms_before")),
            "residual_rms_after": _finite_or_none(model.get("residual_rms_after")),
            "residual_rms_delta": _finite_or_none(model.get("residual_rms_delta")),
        },
    )


def _get_or_create_target_calibration(
    session: Session,
    *,
    name: str,
    source_cv: CalibrationVersion,
    instrument_unit_id: int | None,
    parameters: dict[str, Any],
    dry_run: bool,
    immutable_generation: bool = False,
) -> CalibrationVersion | None:
    from sqlalchemy import select
    from grandgousier.calibration.provenance import (
        prepare_calibration_version_for_write,
        set_source_calibration,
    )
    from grandgousier.db.models import CalibrationVersion

    if dry_run:
        return None

    full_parameters = {
        "source_calibration_name": source_cv.name,
        "source_calibration_version_id": int(source_cv.calibration_version_id),
        **parameters,
    }
    notes = (
        "Zero-mean per-frame spatial residual correction applied after conventional ensemble calibration."
    )

    if immutable_generation:
        # Main-pipeline runs must obey the same immutable-generation contract as
        # aperture/ensemble/color materializers.  A finalized logical head gets a
        # new pending generation; an unfinished compatibility/materializing row
        # from an interrupted run can be safely reused and rewritten before it is
        # finalized.  This makes process-archive-path restartable without changing
        # the stable logical spatial calibration name.
        return prepare_calibration_version_for_write(
            session,
            logical_name=name,
            model_name=SPATIAL_ENSEMBLE_ALGORITHM,
            instrument_unit_id=instrument_unit_id,
            instrument_profile={},
            used_catalogs={},
            parameters=full_parameters,
            notes=notes,
            source_cv=source_cv,
            immutable_generation=True,
        )

    # Preserve the legacy/library-call behavior outside immutable pipeline mode.
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    if cv is None:
        cv = CalibrationVersion(
            name=name,
            model_name=SPATIAL_ENSEMBLE_ALGORITHM,
            instrument_unit_id=instrument_unit_id,
            instrument_profile={},
            used_catalogs={},
            parameters=full_parameters,
            notes=notes,
        )
        set_source_calibration(cv, source_cv)
        session.add(cv)
        session.flush()
    else:
        existing = cv.parameters if isinstance(cv.parameters, dict) else {}
        if existing and existing != full_parameters:
            raise ValueError(
                f"Spatial-ensemble calibration name {name!r} already exists with different parameters."
            )
        set_source_calibration(cv, source_cv)
    return cv


def _resolve_frame_ids(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
) -> list[int]:
    if frame_ids is not None:
        return sorted({int(value) for value in frame_ids})
    from grandgousier.db.models import Frame
    from grandgousier.photometry.calibration_view import load_calibration_photometry_view

    view = load_calibration_photometry_view(
        session, calibration_version_id=int(calibration_version_id)
    )
    resolved = sorted({int(row.frame_id) for row in view.rows})
    if instrument_unit_id is None or not resolved:
        return resolved
    from sqlalchemy import select
    allowed = set(
        int(value)
        for value in session.scalars(
            select(Frame.frame_id).where(
                Frame.frame_id.in_(resolved),
                Frame.instrument_unit_id == int(instrument_unit_id),
            )
        ).all()
    )
    return [frame_id for frame_id in resolved if frame_id in allowed]


def _load_source_measurements(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: list[int],
) -> list[Any]:
    from grandgousier.photometry.calibration_view import load_calibration_photometry_view

    return list(
        load_calibration_photometry_view(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
        ).rows
    )


def _reference_object_centers(rows: list[Any], reference_ids: list[int]) -> dict[int, float]:
    allowed = set(reference_ids)
    values: dict[int, list[float]] = {}
    for measurement in rows:
        object_id = int(measurement.object_id)
        if object_id not in allowed or not _finite(measurement.mag_cal):
            continue
        values.setdefault(object_id, []).append(float(measurement.mag_cal))
    return {object_id: float(median(mags)) for object_id, mags in values.items() if mags}


def _detector_bounds(rows: list[Any]) -> tuple[float, float, float, float]:
    xs = [float(row.x) for row in rows if _finite(row.x)]
    ys = [float(row.y) for row in rows if _finite(row.y)]
    if not xs or not ys:
        return (0.0, 1.0, 0.0, 1.0)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmax <= xmin:
        xmax = xmin + 1.0
    if ymax <= ymin:
        ymax = ymin + 1.0
    return float(xmin), float(xmax), float(ymin), float(ymax)


def _normalize_xy(
    x: np.ndarray,
    y: np.ndarray,
    bounds: tuple[float, float, float, float],
) -> tuple[np.ndarray, np.ndarray]:
    xmin, xmax, ymin, ymax = [float(value) for value in bounds]
    xn = 2.0 * (np.asarray(x, dtype=float) - xmin) / max(xmax - xmin, 1e-12) - 1.0
    yn = 2.0 * (np.asarray(y, dtype=float) - ymin) / max(ymax - ymin, 1e-12) - 1.0
    return xn, yn


def _measurement_weight(measurement: Any) -> float:
    err = _finite_or_none(measurement.mag_err_random)
    if err is None or err <= 0:
        err = _finite_or_none(measurement.mag_err)
    if err is None or err <= 0:
        return 1.0
    return float(1.0 / max(err * err, 1e-8))


def _finite(value: Any) -> bool:
    return _finite_or_none(value) is not None


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _median(values: Iterable[Any]) -> float | None:
    finite = [_finite_or_none(value) for value in values]
    finite = [value for value in finite if value is not None]
    return float(median(finite)) if finite else None


def _mad(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    med = float(np.median(values))
    return float(np.median(np.abs(values - med)))


def _rms(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    return float(np.sqrt(np.mean(np.asarray(values, dtype=float) ** 2)))
