from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationIteration,
    CalibrationVersion,
    Frame,
    Measurement,
)
from grandgousier.quality.lightcurve_quality import (
    CalibrationAnalysisSnapshot,
    load_calibration_analysis_snapshot,
)


RESIDUAL_FLOOR_ALGORITHM = "reference_lower_envelope_frame_common_v2"
RESIDUAL_FLOOR_ERROR_BASIS = "physical_detector_error"
FRAME_COMMON_MIN_FLOOR_REDUCTION_FRACTION = 0.05
FRAME_COMMON_MIN_ABS_CORRELATION_REDUCTION = 0.01


@dataclass
class RandomErrorValidationResult:
    calibration_name: str
    calibration_version_id: int | None = None
    status: str = "NOT_RUN"
    evidence_level: str = "diagnostic"
    n_reference_objects: int = 0
    n_measurements: int = 0
    degrees_of_freedom: int = 0
    min_measurements_per_object: int = 10
    physical_normalized_robust_sigma: float | None = None
    legacy_normalized_robust_sigma: float | None = None
    physical_normalized_rms: float | None = None
    legacy_normalized_rms: float | None = None
    physical_reduced_chi2: float | None = None
    legacy_reduced_chi2: float | None = None
    physical_error_scale_recommendation: float | None = None
    legacy_error_scale_recommendation: float | None = None
    physical_log_distance_to_unity: float | None = None
    legacy_log_distance_to_unity: float | None = None
    physical_closer_to_unity: bool | None = None
    residual_floor_status: str = "NOT_RUN"
    residual_floor_algorithm: str = RESIDUAL_FLOOR_ALGORITHM
    residual_floor_error_basis: str = RESIDUAL_FLOOR_ERROR_BASIS
    residual_floor_bin_width: float = 1.0
    residual_floor_lower_quantile: float = 0.25
    residual_floor_min_objects_per_bin: int = 3
    residual_floor_suggested_mag: float | None = None
    residual_floor_after_frame_common_mag: float | None = None
    residual_floor_independent_mag: float | None = None
    residual_floor_frame_common_sigma_mag: float | None = None
    residual_floor_frame_common_supported: bool | None = None
    residual_floor_frame_common_support_status: str = "NOT_EVALUATED"
    residual_floor_frame_common_support_reason: str | None = None
    residual_floor_frame_common_min_floor_reduction_fraction: float = (
        FRAME_COMMON_MIN_FLOOR_REDUCTION_FRACTION
    )
    residual_floor_frame_common_min_abs_correlation_reduction: float = (
        FRAME_COMMON_MIN_ABS_CORRELATION_REDUCTION
    )
    residual_floor_frame_common_floor_reduction_mag: float | None = None
    residual_floor_frame_common_floor_reduction_fraction: float | None = None
    residual_floor_frame_common_abs_correlation_reduction: float | None = None
    residual_floor_calibrator_objects: int = 0
    residual_floor_frames_with_common_residual: int = 0
    residual_floor_effective_detector_only_normalized_robust_sigma: float | None = None
    residual_floor_effective_detector_only_reduced_chi2: float | None = None
    residual_floor_effective_total_normalized_robust_sigma: float | None = None
    residual_floor_effective_total_reduced_chi2: float | None = None
    residual_floor_effective_normalized_robust_sigma: float | None = None
    residual_floor_effective_reduced_chi2: float | None = None
    residual_floor_pairwise_correlation_median: float | None = None
    residual_floor_pairwise_abs_correlation_median: float | None = None
    residual_floor_pairwise_abs_correlation_p90: float | None = None
    residual_floor_pairwise_correlation_after_common_median: float | None = None
    residual_floor_pairwise_abs_correlation_after_common_median: float | None = None
    residual_floor_pairwise_pairs: int | None = 0
    residual_floor_pairwise_correlations_evaluated: bool = True
    residual_floor_annotation_measurements: int = 0
    residual_floor_annotation_calibrations: int = 0
    analysis_snapshot_algorithm: str | None = None
    analysis_snapshot_cache_hit: bool = False
    analysis_snapshot_measurements: int = 0
    object_rows: list[dict[str, Any]] = field(default_factory=list)
    magnitude_bins: list[dict[str, Any]] = field(default_factory=list)
    residual_floor_bins: list[dict[str, Any]] = field(default_factory=list)
    residual_floor_after_common_bins: list[dict[str, Any]] = field(default_factory=list)
    residual_floor_frame_rows: list[dict[str, Any]] = field(default_factory=list)
    residual_floor_associations: list[dict[str, Any]] = field(default_factory=list)
    summary_json: str | None = None
    objects_csv: str | None = None
    residual_floor_summary_json: str | None = None
    residual_floor_objects_csv: str | None = None
    residual_floor_frames_csv: str | None = None
    warnings: list[str] = field(default_factory=list)


def validate_random_error_model(
    session: Session,
    *,
    calibration_name: str,
    frame_ids: list[int] | None = None,
    min_measurements_per_object: int = 10,
    out_dir: str | Path | None = None,
    residual_floor_bin_width: float = 1.0,
    residual_floor_lower_quantile: float = 0.25,
    residual_floor_min_objects_per_bin: int = 3,
    aperture_context_frames_csv: str | Path | None = None,
    diagnostic_calibration_names: list[str] | None = None,
    analysis_snapshot: CalibrationAnalysisSnapshot | None = None,
    annotate_residual_floor: bool = True,
    include_legacy_comparison: bool = True,
    include_pairwise_residual_correlations: bool = True,
) -> RandomErrorValidationResult:
    """Validate random errors and estimate an additive residual floor.

    The residual-floor branch is diagnostic only. It estimates a lower envelope
    of reference-star scatter, measures a frame-common residual from quiet stars,
    and annotates selected calibration branches with detector/floor/effective
    uncertainty provenance without changing ``mag_err_random``.
    """

    result = RandomErrorValidationResult(
        calibration_name=calibration_name,
        min_measurements_per_object=int(min_measurements_per_object),
        residual_floor_bin_width=float(residual_floor_bin_width),
        residual_floor_lower_quantile=float(residual_floor_lower_quantile),
        residual_floor_min_objects_per_bin=int(residual_floor_min_objects_per_bin),
    )
    cv = session.scalar(
        select(CalibrationVersion).where(CalibrationVersion.name == calibration_name)
    )
    if cv is None:
        result.status = "CALIBRATION_NOT_FOUND"
        result.warnings.append(f"calibration not found: {calibration_name}")
        return result
    result.calibration_version_id = int(cv.calibration_version_id)

    normalized_frame_ids = (
        tuple(sorted({int(value) for value in frame_ids})) if frame_ids else None
    )
    if analysis_snapshot is not None:
        if analysis_snapshot.calibration_version_id != int(cv.calibration_version_id):
            raise ValueError(
                "analysis_snapshot calibration does not match validate_random_error_model"
            )
        if analysis_snapshot.frame_ids != normalized_frame_ids:
            raise ValueError(
                "analysis_snapshot frame scope does not match validate_random_error_model"
            )
        measurement_rows = (
            (measurement, analysis_snapshot.frames_by_id.get(measurement.frame_id))
            for measurement in analysis_snapshot.measurement_rows
        )
        result.analysis_snapshot_algorithm = analysis_snapshot.algorithm
        result.analysis_snapshot_cache_hit = bool(
            analysis_snapshot.timings_sec.get("_runner_cache_hit", 1.0)
        )
        result.analysis_snapshot_measurements = len(analysis_snapshot.measurement_rows)
    else:
        analysis_snapshot = load_calibration_analysis_snapshot(
            session,
            calibration_name=calibration_name,
            calibration_version_id=int(cv.calibration_version_id),
            frame_ids=normalized_frame_ids,
            min_measurements=int(min_measurements_per_object),
        )
        measurement_rows = (
            (measurement, analysis_snapshot.frames_by_id.get(measurement.frame_id))
            for measurement in analysis_snapshot.measurement_rows
        )
        result.analysis_snapshot_algorithm = analysis_snapshot.algorithm
        result.analysis_snapshot_cache_hit = False
        result.analysis_snapshot_measurements = len(analysis_snapshot.measurement_rows)
    frame_context = _load_frame_context(aperture_context_frames_csv)

    raw_rows: list[dict[str, Any]] = []
    for measurement, frame in measurement_rows:
        if frame is None:
            continue
        flags = measurement.flags if isinstance(measurement.flags, dict) else {}
        if not bool(flags.get("ensemble_object_is_reference")):
            continue
        if bool(flags.get("ensemble_object_excluded_from_reference")):
            continue
        mag = _finite_or_none(measurement.mag_cal)
        physical_err = _positive_or_none(flags.get("random_error_physical_mag_err"))
        if physical_err is None:
            physical_err = _positive_or_none(
                measurement.mag_err_random
                if measurement.mag_err_random is not None
                else measurement.mag_err
            )
        legacy_err = _positive_or_none(flags.get("random_error_legacy_mag_err"))
        if mag is None or physical_err is None or legacy_err is None:
            continue
        total_err = _positive_or_none(
            measurement.mag_err_random
            if measurement.mag_err_random is not None
            else measurement.mag_err
        )
        if total_err is None:
            total_err = physical_err
        if _measurement_is_hard_rejected(flags):
            continue
        pre_qc = _frame_pre_qc(frame.quality_flags)
        detection_qc = _frame_detection_qc(frame.quality_flags)
        context = frame_context.get(int(measurement.frame_id), {})
        raw_rows.append(
            {
                "object_id": int(measurement.object_id),
                "frame_id": int(measurement.frame_id),
                "mag": mag,
                "physical_err": physical_err,
                "legacy_err": legacy_err,
                "total_err": total_err,
                "x": _finite_or_none(measurement.x),
                "y": _finite_or_none(measurement.y),
                "mjd": _finite_or_none(frame.mjd),
                "airmass": _finite_or_none(frame.airmass),
                "background_median": _finite_or_none(pre_qc.get("background_median")),
                "background_mad_sigma": _finite_or_none(pre_qc.get("background_mad_sigma")),
                "astrom_rms_arcsec": _finite_or_none(detection_qc.get("astrom_rms_arcsec")),
                "psf_fwhm_pix": _finite_or_none(
                    context.get("psf_fwhm_pix_median")
                    or context.get("psf_fwhm_pix")
                ),
                "psf_ellipticity": _finite_or_none(
                    context.get("psf_ellipticity_median")
                    or context.get("psf_ellipticity")
                ),
            }
        )

    summary = summarize_random_error_rows(
        raw_rows,
        min_measurements_per_object=int(min_measurements_per_object),
        residual_floor_bin_width=float(residual_floor_bin_width),
        residual_floor_lower_quantile=float(residual_floor_lower_quantile),
        residual_floor_min_objects_per_bin=int(residual_floor_min_objects_per_bin),
        include_legacy_comparison=include_legacy_comparison,
        include_pairwise_residual_correlations=(
            include_pairwise_residual_correlations
        ),
    )
    for key, value in summary.items():
        if hasattr(result, key):
            setattr(result, key, value)

    if result.n_reference_objects < 3:
        result.status = "INSUFFICIENT_REFERENCE_OBJECTS"
        result.residual_floor_status = "INSUFFICIENT_REFERENCE_OBJECTS"
        result.warnings.append(
            f"only {result.n_reference_objects} reference objects have sufficient measurements"
        )
    else:
        result.status = "OK"

    annotation_names = list(dict.fromkeys(
        str(name) for name in (diagnostic_calibration_names or []) if name
    ))
    if (
        annotate_residual_floor
        and result.residual_floor_suggested_mag is not None
        and annotation_names
    ):
        annotations = annotate_residual_floor_provenance(
            session,
            calibration_names=annotation_names,
            floor_mag=result.residual_floor_suggested_mag,
            frame_common_sigma_mag=result.residual_floor_frame_common_sigma_mag,
            after_frame_common_floor_mag=result.residual_floor_after_frame_common_mag,
            independent_floor_mag=result.residual_floor_independent_mag,
            frame_common_supported=result.residual_floor_frame_common_supported,
            frame_common_support_status=result.residual_floor_frame_common_support_status,
        )
        result.residual_floor_annotation_measurements = annotations["measurements"]
        result.residual_floor_annotation_calibrations = annotations["calibrations"]

    if out_dir is not None:
        _write_validation_outputs(result, Path(out_dir))
    return result


def summarize_random_error_rows(
    rows: Iterable[dict[str, Any]],
    *,
    min_measurements_per_object: int = 10,
    residual_floor_bin_width: float = 1.0,
    residual_floor_lower_quantile: float = 0.25,
    residual_floor_min_objects_per_bin: int = 3,
    include_legacy_comparison: bool = True,
    include_pairwise_residual_correlations: bool = True,
) -> dict[str, Any]:
    clean_rows: list[dict[str, Any]] = []
    by_object: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            object_id = int(row["object_id"])
        except (KeyError, TypeError, ValueError):
            continue
        mag = _finite_or_none(row.get("mag"))
        physical_err = _positive_or_none(row.get("physical_err"))
        legacy_err = _positive_or_none(row.get("legacy_err"))
        total_err = _positive_or_none(row.get("total_err"))
        if mag is None or physical_err is None or legacy_err is None:
            continue
        if total_err is None:
            total_err = physical_err
        clean = dict(row)
        clean.update(
            {
                "object_id": object_id,
                "mag": mag,
                "physical_err": physical_err,
                "legacy_err": legacy_err,
                "total_err": total_err,
            }
        )
        clean_rows.append(clean)
        by_object.setdefault(object_id, []).append(clean)

    object_rows: list[dict[str, Any]] = []
    residual_rows: list[dict[str, Any]] = []
    physical_norm_all: list[float] = []
    legacy_norm_all: list[float] = []
    for object_id, object_measurements in sorted(by_object.items()):
        if len(object_measurements) < int(min_measurements_per_object):
            continue
        mags = np.asarray([row["mag"] for row in object_measurements], dtype=float)
        physical_errs = np.asarray(
            [row["physical_err"] for row in object_measurements], dtype=float
        )
        total_errs = np.asarray(
            [row["total_err"] for row in object_measurements], dtype=float
        )
        center = float(np.median(mags))
        residuals = mags - center
        physical_norm = residuals / physical_errs
        physical_norm_all.extend(float(value) for value in physical_norm)
        legacy_metrics: dict[str, Any] = {}
        if include_legacy_comparison:
            legacy_errs = np.asarray(
                [row["legacy_err"] for row in object_measurements], dtype=float
            )
            legacy_norm = residuals / legacy_errs
            legacy_norm_all.extend(float(value) for value in legacy_norm)
            legacy_metrics = {
                "legacy_mag_err_median": float(np.median(legacy_errs)),
                "legacy_normalized_robust_sigma": _robust_sigma(legacy_norm),
                "legacy_normalized_rms": _rms(legacy_norm),
                "legacy_reduced_chi2": _reduced_chi2(
                    legacy_norm, fitted_parameters=1
                ),
            }
        observed_robust_sigma = _robust_sigma(residuals)
        detector_err = float(np.median(physical_errs))
        raw_floor = _quadrature_excess(observed_robust_sigma, detector_err)
        object_row = {
            "object_id": object_id,
            "n_measurements": len(object_measurements),
            "mag_median": center,
            "x_median": _median_or_none(row.get("x") for row in object_measurements),
            "y_median": _median_or_none(row.get("y") for row in object_measurements),
            "observed_rms": _rms(residuals),
            "observed_mad": _mad(residuals),
            "observed_robust_sigma": observed_robust_sigma,
            "physical_mag_err_median": detector_err,
            **(
                {"legacy_mag_err_median": legacy_metrics["legacy_mag_err_median"]}
                if include_legacy_comparison
                else {}
            ),
            "mag_err_detector_median": detector_err,
            "mag_err_random_median": float(np.median(total_errs)),
            "residual_floor_raw_mag": raw_floor,
            "physical_normalized_robust_sigma": _robust_sigma(physical_norm),
            **(
                {
                    "legacy_normalized_robust_sigma": legacy_metrics[
                        "legacy_normalized_robust_sigma"
                    ]
                }
                if include_legacy_comparison
                else {}
            ),
            "physical_normalized_rms": _rms(physical_norm),
            **(
                {"legacy_normalized_rms": legacy_metrics["legacy_normalized_rms"]}
                if include_legacy_comparison
                else {}
            ),
            "physical_reduced_chi2": _reduced_chi2(
                physical_norm, fitted_parameters=1
            ),
            **(
                {"legacy_reduced_chi2": legacy_metrics["legacy_reduced_chi2"]}
                if include_legacy_comparison
                else {}
            ),
        }
        object_rows.append(object_row)
        for source, residual in zip(object_measurements, residuals):
            rr = dict(source)
            rr["residual"] = float(residual)
            residual_rows.append(rr)

    floor_summary = _estimate_lower_envelope(
        object_rows,
        value_key="residual_floor_raw_mag",
        bin_width=residual_floor_bin_width,
        lower_quantile=residual_floor_lower_quantile,
        min_objects_per_bin=residual_floor_min_objects_per_bin,
        calibrator_key="residual_floor_is_calibrator",
    )
    floor_mag = floor_summary["floor_mag"]
    floor_ids = set(floor_summary["calibrator_object_ids"])

    frame_common = _frame_common_rows(residual_rows, floor_ids)
    frame_common_map = {
        int(row["frame_id"]): row["frame_common_residual_mag"]
        for row in frame_common
        if row.get("frame_common_residual_mag") is not None
    }
    frame_common_sigma = _robust_sigma(
        row["frame_common_residual_mag"]
        for row in frame_common
        if row.get("frame_common_residual_mag") is not None
    )

    decommon_by_object: dict[int, list[float]] = {}
    for row in residual_rows:
        common = frame_common_map.get(int(row.get("frame_id") or -1))
        if common is None:
            continue
        value = float(row["residual"]) - float(common)
        row["residual_after_frame_common"] = value
        decommon_by_object.setdefault(int(row["object_id"]), []).append(value)
    object_by_id = {int(row["object_id"]): row for row in object_rows}
    for object_id, values in decommon_by_object.items():
        object_row = object_by_id.get(object_id)
        if object_row is None:
            continue
        robust = _robust_sigma(values)
        detector = _positive_or_none(object_row.get("physical_mag_err_median"))
        object_row["observed_robust_sigma_after_frame_common"] = robust
        object_row["residual_floor_after_frame_common_mag"] = _quadrature_excess(
            robust, detector
        )

    decommon_floor_summary = _estimate_lower_envelope(
        object_rows,
        value_key="residual_floor_after_frame_common_mag",
        bin_width=residual_floor_bin_width,
        lower_quantile=residual_floor_lower_quantile,
        min_objects_per_bin=residual_floor_min_objects_per_bin,
        calibrator_key="residual_floor_after_common_is_calibrator",
    )

    effective_detector_only_norm_all: list[float] = []
    effective_total_norm_all: list[float] = []
    for row in residual_rows:
        if floor_mag is None:
            continue
        effective_detector_only_err = math.hypot(
            float(row["physical_err"]), float(floor_mag)
        )
        effective_total_err = math.hypot(float(row["total_err"]), float(floor_mag))
        row["effective_detector_only_err"] = effective_detector_only_err
        row["effective_total_err"] = effective_total_err
        effective_detector_only_norm_all.append(
            float(row["residual"]) / effective_detector_only_err
        )
        effective_total_norm_all.append(float(row["residual"]) / effective_total_err)
    for object_row in object_rows:
        if floor_mag is None:
            object_row["mag_err_epoch_floor_suggested"] = None
            object_row["mag_err_effective_detector_only_suggested_median"] = None
            object_row["mag_err_effective_total_suggested_median"] = None
            object_row["mag_err_effective_suggested_median"] = None
            object_row["effective_detector_only_normalized_robust_sigma"] = None
            object_row["effective_detector_only_reduced_chi2"] = None
            object_row["effective_total_normalized_robust_sigma"] = None
            object_row["effective_total_reduced_chi2"] = None
            object_row["effective_normalized_robust_sigma"] = None
            object_row["effective_reduced_chi2"] = None
            continue
        object_row["mag_err_epoch_floor_suggested"] = floor_mag
        effective_detector_only_median = math.hypot(
            float(object_row["physical_mag_err_median"]), floor_mag
        )
        effective_total_median = math.hypot(
            float(object_row["mag_err_random_median"]), floor_mag
        )
        object_row["mag_err_effective_detector_only_suggested_median"] = (
            effective_detector_only_median
        )
        object_row["mag_err_effective_total_suggested_median"] = effective_total_median
        object_row["mag_err_effective_suggested_median"] = effective_total_median
        detector_only_values = [
            row["residual"] / math.hypot(float(row["physical_err"]), floor_mag)
            for row in residual_rows
            if int(row["object_id"]) == int(object_row["object_id"])
        ]
        total_values = [
            row["residual"] / math.hypot(float(row["total_err"]), floor_mag)
            for row in residual_rows
            if int(row["object_id"]) == int(object_row["object_id"])
        ]
        object_row["effective_detector_only_normalized_robust_sigma"] = _robust_sigma(
            detector_only_values
        )
        object_row["effective_detector_only_reduced_chi2"] = _reduced_chi2(
            detector_only_values, fitted_parameters=1
        )
        object_row["effective_total_normalized_robust_sigma"] = _robust_sigma(
            total_values
        )
        object_row["effective_total_reduced_chi2"] = _reduced_chi2(
            total_values, fitted_parameters=1
        )
        object_row["effective_normalized_robust_sigma"] = object_row[
            "effective_total_normalized_robust_sigma"
        ]
        object_row["effective_reduced_chi2"] = object_row[
            "effective_total_reduced_chi2"
        ]

    if include_pairwise_residual_correlations:
        raw_pairwise = _pairwise_residual_correlations(
            residual_rows, floor_ids, after_common=False
        )
        decommon_pairwise = _pairwise_residual_correlations(
            residual_rows, floor_ids, after_common=True
        )
    else:
        raw_pairwise = {
            "n_pairs": None,
            "median": None,
            "abs_median": None,
            "abs_p90": None,
        }
        decommon_pairwise = raw_pairwise
    frame_common_support = _evaluate_frame_common_support(
        floor_mag=floor_mag,
        floor_after_common_mag=decommon_floor_summary["floor_mag"],
        pairwise_abs_correlation_median=raw_pairwise["abs_median"],
        pairwise_abs_correlation_after_common_median=decommon_pairwise["abs_median"],
    )
    associations = _residual_floor_associations(object_rows, frame_common)

    physical_sigma = _robust_sigma(physical_norm_all)
    physical_distance = _log_distance_to_unity(physical_sigma)
    legacy_summary_metrics: dict[str, Any] = {}
    if include_legacy_comparison:
        legacy_sigma = _robust_sigma(legacy_norm_all)
        legacy_distance = _log_distance_to_unity(legacy_sigma)
        physical_closer = None
        if physical_distance is not None and legacy_distance is not None:
            physical_closer = bool(physical_distance < legacy_distance)
        legacy_summary_metrics = {
            "legacy_normalized_robust_sigma": legacy_sigma,
            "legacy_normalized_rms": _rms(legacy_norm_all),
            "legacy_reduced_chi2": _reduced_chi2(
                legacy_norm_all, fitted_parameters=len(object_rows)
            ),
            "legacy_error_scale_recommendation": legacy_sigma,
            "legacy_log_distance_to_unity": legacy_distance,
            "physical_closer_to_unity": physical_closer,
        }

    degrees_of_freedom = max(0, len(physical_norm_all) - len(object_rows))
    residual_status = "OK" if floor_mag is not None and floor_ids else "INSUFFICIENT_LOWER_ENVELOPE"
    return {
        "n_reference_objects": len(object_rows),
        "n_measurements": len(physical_norm_all),
        "degrees_of_freedom": degrees_of_freedom,
        "physical_normalized_robust_sigma": physical_sigma,
        **(
            {"legacy_normalized_robust_sigma": legacy_summary_metrics[
                "legacy_normalized_robust_sigma"
            ]}
            if include_legacy_comparison
            else {}
        ),
        "physical_normalized_rms": _rms(physical_norm_all),
        **(
            {"legacy_normalized_rms": legacy_summary_metrics["legacy_normalized_rms"]}
            if include_legacy_comparison
            else {}
        ),
        "physical_reduced_chi2": _reduced_chi2(
            physical_norm_all, fitted_parameters=len(object_rows)
        ),
        **(
            {
                "legacy_reduced_chi2": legacy_summary_metrics[
                    "legacy_reduced_chi2"
                ]
            }
            if include_legacy_comparison
            else {}
        ),
        "physical_error_scale_recommendation": physical_sigma,
        **(
            {
                "legacy_error_scale_recommendation": legacy_summary_metrics[
                    "legacy_error_scale_recommendation"
                ]
            }
            if include_legacy_comparison
            else {}
        ),
        "physical_log_distance_to_unity": physical_distance,
        **(
            {
                "legacy_log_distance_to_unity": legacy_summary_metrics[
                    "legacy_log_distance_to_unity"
                ]
            }
            if include_legacy_comparison
            else {}
        ),
        **(
            {
                "physical_closer_to_unity": legacy_summary_metrics[
                    "physical_closer_to_unity"
                ]
            }
            if include_legacy_comparison
            else {}
        ),
        "residual_floor_status": residual_status,
        "residual_floor_suggested_mag": floor_mag,
        "residual_floor_after_frame_common_mag": decommon_floor_summary["floor_mag"],
        "residual_floor_independent_mag": (
            decommon_floor_summary["floor_mag"]
            if frame_common_support["supported"] is True
            else None
        ),
        "residual_floor_frame_common_sigma_mag": frame_common_sigma,
        "residual_floor_frame_common_supported": frame_common_support["supported"],
        "residual_floor_frame_common_support_status": frame_common_support["status"],
        "residual_floor_frame_common_support_reason": frame_common_support["reason"],
        "residual_floor_pairwise_correlations_evaluated": (
            include_pairwise_residual_correlations
        ),
        "residual_floor_frame_common_min_floor_reduction_fraction": FRAME_COMMON_MIN_FLOOR_REDUCTION_FRACTION,
        "residual_floor_frame_common_min_abs_correlation_reduction": FRAME_COMMON_MIN_ABS_CORRELATION_REDUCTION,
        "residual_floor_frame_common_floor_reduction_mag": frame_common_support[
            "floor_reduction_mag"
        ],
        "residual_floor_frame_common_floor_reduction_fraction": frame_common_support[
            "floor_reduction_fraction"
        ],
        "residual_floor_frame_common_abs_correlation_reduction": frame_common_support[
            "abs_correlation_reduction"
        ],
        "residual_floor_calibrator_objects": len(floor_ids),
        "residual_floor_frames_with_common_residual": len(frame_common_map),
        "residual_floor_effective_detector_only_normalized_robust_sigma": _robust_sigma(
            effective_detector_only_norm_all
        ),
        "residual_floor_effective_detector_only_reduced_chi2": _reduced_chi2(
            effective_detector_only_norm_all, fitted_parameters=len(object_rows)
        ),
        "residual_floor_effective_total_normalized_robust_sigma": _robust_sigma(
            effective_total_norm_all
        ),
        "residual_floor_effective_total_reduced_chi2": _reduced_chi2(
            effective_total_norm_all, fitted_parameters=len(object_rows)
        ),
        "residual_floor_effective_normalized_robust_sigma": _robust_sigma(
            effective_total_norm_all
        ),
        "residual_floor_effective_reduced_chi2": _reduced_chi2(
            effective_total_norm_all, fitted_parameters=len(object_rows)
        ),
        "residual_floor_pairwise_correlation_median": raw_pairwise["median"],
        "residual_floor_pairwise_abs_correlation_median": raw_pairwise["abs_median"],
        "residual_floor_pairwise_abs_correlation_p90": raw_pairwise["abs_p90"],
        "residual_floor_pairwise_correlation_after_common_median": decommon_pairwise["median"],
        "residual_floor_pairwise_abs_correlation_after_common_median": decommon_pairwise["abs_median"],
        "residual_floor_pairwise_pairs": raw_pairwise["n_pairs"],
        "object_rows": object_rows,
        "magnitude_bins": _magnitude_bins(
            object_rows,
            include_legacy_comparison=include_legacy_comparison,
        ),
        "residual_floor_bins": floor_summary["bins"],
        "residual_floor_after_common_bins": decommon_floor_summary["bins"],
        "residual_floor_frame_rows": frame_common,
        "residual_floor_associations": associations,
    }


def annotate_residual_floor_provenance(
    session: Session,
    *,
    calibration_names: list[str],
    floor_mag: float,
    frame_common_sigma_mag: float | None,
    after_frame_common_floor_mag: float | None,
    independent_floor_mag: float | None,
    frame_common_supported: bool | None,
    frame_common_support_status: str,
) -> dict[str, int]:
    floor = _positive_or_none(floor_mag)
    if floor is None:
        return {"calibrations": 0, "measurements": 0}
    calibration_ids = list(
        session.scalars(
            select(CalibrationVersion.calibration_version_id).where(
                CalibrationVersion.name.in_(list(dict.fromkeys(calibration_names)))
            )
        ).all()
    )
    if not calibration_ids:
        return {"calibrations": 0, "measurements": 0}

    # D3 compact-mode provenance: persist the residual-floor model once per
    # normalized calibration iteration instead of requiring a repeated flag
    # payload on every intermediate Measurement row. Reconstruction projects
    # these values back onto row-level flags when a consumer needs them.
    normalized_payload = {
        "residual_noise_floor_algorithm": RESIDUAL_FLOOR_ALGORITHM,
        "residual_noise_floor_error_basis": RESIDUAL_FLOOR_ERROR_BASIS,
        "residual_noise_floor_mag": floor,
        "residual_noise_frame_common_sigma_mag": frame_common_sigma_mag,
        "residual_noise_frame_common_supported": frame_common_supported,
        "residual_noise_frame_common_support_status": frame_common_support_status,
        "residual_noise_floor_after_frame_common_mag": after_frame_common_floor_mag,
        "residual_noise_independent_floor_mag": independent_floor_mag,
    }
    for calibration_id in calibration_ids:
        iteration = session.scalar(
            select(CalibrationIteration)
            .where(
                CalibrationIteration.calibration_version_id == int(calibration_id)
            )
            .order_by(
                CalibrationIteration.iteration_no.desc(),
                CalibrationIteration.calibration_iteration_id.desc(),
            )
            .limit(1)
        )
        if iteration is not None:
            metrics = dict(iteration.metrics or {})
            metrics.update(normalized_payload)
            iteration.metrics = metrics

    measurements = list(
        session.scalars(
            select(Measurement).where(
                Measurement.calibration_version_id.in_([int(v) for v in calibration_ids])
            )
        ).all()
    )
    updated = 0
    for measurement in measurements:
        flags = dict(measurement.flags or {})
        detector = _positive_or_none(flags.get("random_error_physical_mag_err"))
        if detector is None:
            detector = _positive_or_none(
                measurement.mag_err_random
                if measurement.mag_err_random is not None
                else measurement.mag_err
            )
        if detector is None:
            continue
        total_random = _positive_or_none(
            measurement.mag_err_random
            if measurement.mag_err_random is not None
            else measurement.mag_err
        )
        if total_random is None:
            total_random = detector
        effective_detector_only = math.hypot(detector, floor)
        effective_total = math.hypot(total_random, floor)
        flags.update(
            {
                "residual_noise_floor_algorithm": RESIDUAL_FLOOR_ALGORITHM,
                "residual_noise_floor_error_basis": RESIDUAL_FLOOR_ERROR_BASIS,
                "residual_noise_floor_applied": False,
                "mag_err_detector": detector,
                "mag_err_epoch_floor": floor,
                "mag_err_effective_detector_only": effective_detector_only,
                "mag_err_effective_total": effective_total,
                "mag_err_effective": effective_total,
                "residual_noise_frame_common_sigma_mag": frame_common_sigma_mag,
                "residual_noise_frame_common_supported": frame_common_supported,
                "residual_noise_frame_common_support_status": frame_common_support_status,
                "residual_noise_floor_after_frame_common_mag": after_frame_common_floor_mag,
                "residual_noise_independent_floor_mag": independent_floor_mag,
            }
        )
        measurement.flags = flags
        updated += 1
    session.flush()
    return {"calibrations": len(calibration_ids), "measurements": updated}


def _evaluate_frame_common_support(
    *,
    floor_mag: float | None,
    floor_after_common_mag: float | None,
    pairwise_abs_correlation_median: float | None,
    pairwise_abs_correlation_after_common_median: float | None,
) -> dict[str, Any]:
    floor = _finite_or_none(floor_mag)
    after = _finite_or_none(floor_after_common_mag)
    corr = _finite_or_none(pairwise_abs_correlation_median)
    corr_after = _finite_or_none(pairwise_abs_correlation_after_common_median)
    floor_comparison_available = (
        floor is not None and floor > 0 and after is not None and after >= 0
    )
    floor_reduction_mag = floor - after if floor_comparison_available else None
    floor_reduction_fraction = (
        floor_reduction_mag / floor
        if floor_comparison_available and floor_reduction_mag is not None
        else None
    )
    if (
        not floor_comparison_available
        or corr is None
        or corr_after is None
    ):
        return {
            "supported": None,
            "status": "INSUFFICIENT_DATA",
            "reason": "frame-common support requires both floor and pairwise-correlation comparisons",
            "floor_reduction_mag": floor_reduction_mag,
            "floor_reduction_fraction": floor_reduction_fraction,
            "abs_correlation_reduction": None,
        }

    assert floor_reduction_mag is not None
    assert floor_reduction_fraction is not None
    abs_correlation_reduction = corr - corr_after
    floor_pass = (
        floor_reduction_fraction is not None
        and floor_reduction_fraction >= FRAME_COMMON_MIN_FLOOR_REDUCTION_FRACTION
    )
    correlation_pass = (
        abs_correlation_reduction >= FRAME_COMMON_MIN_ABS_CORRELATION_REDUCTION
    )
    supported = bool(floor_pass and correlation_pass)
    failed: list[str] = []
    if not floor_pass:
        failed.append("floor_reduction")
    if not correlation_pass:
        failed.append("pairwise_abs_correlation_reduction")
    return {
        "supported": supported,
        "status": "SUPPORTED" if supported else "NOT_SUPPORTED",
        "reason": "criteria passed" if supported else "failed: " + ",".join(failed),
        "floor_reduction_mag": floor_reduction_mag,
        "floor_reduction_fraction": floor_reduction_fraction,
        "abs_correlation_reduction": abs_correlation_reduction,
    }


def _estimate_lower_envelope(
    object_rows: list[dict[str, Any]],
    *,
    value_key: str,
    bin_width: float,
    lower_quantile: float,
    min_objects_per_bin: int,
    calibrator_key: str,
) -> dict[str, Any]:
    width = float(bin_width) if bin_width and float(bin_width) > 0 else 1.0
    quantile = min(0.5, max(0.0, float(lower_quantile)))
    minimum = max(2, int(min_objects_per_bin))
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in object_rows:
        mag = _finite_or_none(row.get("mag_median"))
        value = _finite_or_none(row.get(value_key))
        row[calibrator_key] = False
        if mag is None or value is None:
            continue
        groups.setdefault(int(math.floor(mag / width)), []).append(row)
    bins: list[dict[str, Any]] = []
    calibrator_ids: list[int] = []
    envelope_values: list[float] = []
    for bin_index, group in sorted(groups.items()):
        values = np.asarray([float(row[value_key]) for row in group], dtype=float)
        if values.size < minimum:
            bins.append(
                {
                    "mag_min": bin_index * width,
                    "mag_max": (bin_index + 1) * width,
                    "n_objects": int(values.size),
                    "status": "INSUFFICIENT_OBJECTS",
                    "lower_quantile": quantile,
                    "floor_mag": None,
                    "calibrator_objects": 0,
                }
            )
            continue
        threshold = float(np.quantile(values, quantile))
        envelope_values.append(threshold)
        selected = 0
        for row in group:
            if float(row[value_key]) <= threshold + 1e-15:
                row[calibrator_key] = True
                calibrator_ids.append(int(row["object_id"]))
                selected += 1
        bins.append(
            {
                "mag_min": bin_index * width,
                "mag_max": (bin_index + 1) * width,
                "n_objects": int(values.size),
                "status": "OK",
                "lower_quantile": quantile,
                "floor_mag": threshold,
                "floor_median_mag": float(np.median(values)),
                "floor_min_mag": float(np.min(values)),
                "calibrator_objects": selected,
            }
        )
    floor = float(np.median(np.asarray(envelope_values, dtype=float))) if envelope_values else None
    return {
        "floor_mag": floor,
        "calibrator_object_ids": sorted(set(calibrator_ids)),
        "bins": bins,
    }


def _frame_common_rows(
    residual_rows: list[dict[str, Any]], calibrator_ids: set[int]
) -> list[dict[str, Any]]:
    by_frame: dict[int, list[dict[str, Any]]] = {}
    for row in residual_rows:
        if int(row["object_id"]) not in calibrator_ids:
            continue
        frame_id = int(row.get("frame_id") or -1)
        if frame_id < 0:
            continue
        by_frame.setdefault(frame_id, []).append(row)
    out: list[dict[str, Any]] = []
    for frame_id, rows in sorted(by_frame.items()):
        if len(rows) < 3:
            continue
        first = rows[0]
        residuals = [float(row["residual"]) for row in rows]
        out.append(
            {
                "frame_id": frame_id,
                "n_floor_calibrators": len(rows),
                "frame_common_residual_mag": float(np.median(residuals)),
                "frame_common_residual_robust_sigma_mag": _robust_sigma(residuals),
                "mjd": first.get("mjd"),
                "airmass": first.get("airmass"),
                "background_median": first.get("background_median"),
                "background_mad_sigma": first.get("background_mad_sigma"),
                "astrom_rms_arcsec": first.get("astrom_rms_arcsec"),
                "psf_fwhm_pix": first.get("psf_fwhm_pix"),
                "psf_ellipticity": first.get("psf_ellipticity"),
            }
        )
    return out


def _pairwise_residual_correlations(
    residual_rows: list[dict[str, Any]],
    calibrator_ids: set[int],
    *,
    after_common: bool,
) -> dict[str, Any]:
    key = "residual_after_frame_common" if after_common else "residual"
    by_object: dict[int, dict[int, float]] = {}
    for row in residual_rows:
        object_id = int(row["object_id"])
        if object_id not in calibrator_ids:
            continue
        value = _finite_or_none(row.get(key))
        frame_id = row.get("frame_id")
        if value is None or frame_id is None:
            continue
        by_object.setdefault(object_id, {})[int(frame_id)] = value
    correlations: list[float] = []
    for left, right in combinations(sorted(by_object), 2):
        common = sorted(set(by_object[left]) & set(by_object[right]))
        if len(common) < 10:
            continue
        a = np.asarray([by_object[left][frame] for frame in common], dtype=float)
        b = np.asarray([by_object[right][frame] for frame in common], dtype=float)
        if np.std(a) <= 0 or np.std(b) <= 0:
            continue
        corr = float(np.corrcoef(a, b)[0, 1])
        if math.isfinite(corr):
            correlations.append(corr)
    if not correlations:
        return {"n_pairs": 0, "median": None, "abs_median": None, "abs_p90": None}
    arr = np.asarray(correlations, dtype=float)
    return {
        "n_pairs": int(arr.size),
        "median": float(np.median(arr)),
        "abs_median": float(np.median(np.abs(arr))),
        "abs_p90": float(np.quantile(np.abs(arr), 0.9)),
    }


def _residual_floor_associations(
    object_rows: list[dict[str, Any]], frame_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    associations: list[dict[str, Any]] = []
    for x in ("mag_median", "x_median", "y_median"):
        associations.append(
            _association_row(
                object_rows,
                scope="reference_objects",
                x=x,
                y="residual_floor_raw_mag",
            )
        )
    for x in (
        "mjd",
        "airmass",
        "background_median",
        "background_mad_sigma",
        "astrom_rms_arcsec",
        "psf_fwhm_pix",
        "psf_ellipticity",
    ):
        associations.append(
            _association_row(
                frame_rows,
                scope="floor_calibrator_frames",
                x=x,
                y="frame_common_residual_mag",
            )
        )
        associations.append(
            _association_row(
                [
                    {**row, "abs_frame_common_residual_mag": abs(float(row["frame_common_residual_mag"]))}
                    for row in frame_rows
                    if _finite_or_none(row.get("frame_common_residual_mag")) is not None
                ],
                scope="floor_calibrator_frames",
                x=x,
                y="abs_frame_common_residual_mag",
            )
        )
    return associations


def _association_row(
    rows: list[dict[str, Any]], *, scope: str, x: str, y: str
) -> dict[str, Any]:
    pairs = [
        (float(xv), float(yv))
        for row in rows
        if (xv := _finite_or_none(row.get(x))) is not None
        and (yv := _finite_or_none(row.get(y))) is not None
    ]
    return {
        "scope": scope,
        "x": x,
        "y": y,
        "n": len(pairs),
        "spearman_rho": _spearman(pairs),
    }


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    x = np.asarray([pair[0] for pair in pairs], dtype=float)
    y = np.asarray([pair[1] for pair in pairs], dtype=float)
    xr = _rankdata(x)
    yr = _rankdata(y)
    if np.std(xr) <= 0 or np.std(yr) <= 0:
        return None
    value = float(np.corrcoef(xr, yr)[0, 1])
    return value if math.isfinite(value) else None


def _rankdata(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    start = 0
    while start < values.size:
        end = start + 1
        while end < values.size and values[order[end]] == values[order[start]]:
            end += 1
        rank = 0.5 * (start + end - 1) + 1.0
        ranks[order[start:end]] = rank
        start = end
    return ranks


def _write_validation_outputs(result: RandomErrorValidationResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    objects_path = out_dir / "random_error_validation_objects.csv"
    summary_path = out_dir / "random_error_validation_summary.json"
    floor_objects_path = out_dir / "residual_noise_floor_objects.csv"
    floor_frames_path = out_dir / "residual_noise_floor_frames.csv"
    floor_summary_path = out_dir / "residual_noise_floor_summary.json"

    object_fieldnames = _fieldnames_for_rows(result.object_rows)
    _write_csv(result.object_rows, objects_path, object_fieldnames)
    _write_csv(result.object_rows, floor_objects_path, object_fieldnames)
    frame_fieldnames = _fieldnames_for_rows(result.residual_floor_frame_rows)
    _write_csv(result.residual_floor_frame_rows, floor_frames_path, frame_fieldnames)

    result.objects_csv = str(objects_path)
    result.summary_json = str(summary_path)
    result.residual_floor_objects_csv = str(floor_objects_path)
    result.residual_floor_frames_csv = str(floor_frames_path)
    result.residual_floor_summary_json = str(floor_summary_path)

    validation_payload = {
        "algorithm": "ensemble_reference_normalized_residuals_v2",
        "status": result.status,
        "evidence_level": result.evidence_level,
        "causal_inference_allowed": False,
        "calibration_name": result.calibration_name,
        "calibration_version_id": result.calibration_version_id,
        "n_reference_objects": result.n_reference_objects,
        "n_measurements": result.n_measurements,
        "degrees_of_freedom": result.degrees_of_freedom,
        "min_measurements_per_object": result.min_measurements_per_object,
        "physical_normalized_robust_sigma": result.physical_normalized_robust_sigma,
        "legacy_normalized_robust_sigma": result.legacy_normalized_robust_sigma,
        "physical_normalized_rms": result.physical_normalized_rms,
        "legacy_normalized_rms": result.legacy_normalized_rms,
        "physical_reduced_chi2": result.physical_reduced_chi2,
        "legacy_reduced_chi2": result.legacy_reduced_chi2,
        "physical_error_scale_recommendation": result.physical_error_scale_recommendation,
        "legacy_error_scale_recommendation": result.legacy_error_scale_recommendation,
        "physical_log_distance_to_unity": result.physical_log_distance_to_unity,
        "legacy_log_distance_to_unity": result.legacy_log_distance_to_unity,
        "physical_closer_to_unity": result.physical_closer_to_unity,
        "residual_floor_summary_json": str(floor_summary_path),
        "interpretation": (
            "The physical/legacy comparison and residual-floor estimate are diagnostic. "
            "Neither a scale factor nor the additive floor is applied to mag_err_random."
        ),
        "warnings": result.warnings,
        "magnitude_bins": result.magnitude_bins,
        "objects_csv": str(objects_path),
        "object_rows": result.object_rows,
    }
    floor_payload = {
        "algorithm": result.residual_floor_algorithm,
        "error_basis": result.residual_floor_error_basis,
        "status": result.residual_floor_status,
        "evidence_level": "diagnostic",
        "causal_inference_allowed": False,
        "applied_to_mag_err_random": False,
        "calibration_name": result.calibration_name,
        "bin_width": result.residual_floor_bin_width,
        "lower_quantile": result.residual_floor_lower_quantile,
        "min_objects_per_bin": result.residual_floor_min_objects_per_bin,
        "suggested_epoch_floor_mag": result.residual_floor_suggested_mag,
        "measured_floor_after_frame_common_mag": result.residual_floor_after_frame_common_mag,
        "suggested_independent_floor_mag": result.residual_floor_independent_mag,
        "frame_common_sigma_mag": result.residual_floor_frame_common_sigma_mag,
        "frame_common_supported": result.residual_floor_frame_common_supported,
        "frame_common_support_status": result.residual_floor_frame_common_support_status,
        "frame_common_support_reason": result.residual_floor_frame_common_support_reason,
        "frame_common_support_thresholds": {
            "min_floor_reduction_fraction": result.residual_floor_frame_common_min_floor_reduction_fraction,
            "min_abs_correlation_reduction": result.residual_floor_frame_common_min_abs_correlation_reduction,
        },
        "frame_common_floor_reduction_mag": result.residual_floor_frame_common_floor_reduction_mag,
        "frame_common_floor_reduction_fraction": result.residual_floor_frame_common_floor_reduction_fraction,
        "frame_common_abs_correlation_reduction": result.residual_floor_frame_common_abs_correlation_reduction,
        "floor_calibrator_objects": result.residual_floor_calibrator_objects,
        "frames_with_common_residual": result.residual_floor_frames_with_common_residual,
        "effective_detector_only_normalized_robust_sigma": result.residual_floor_effective_detector_only_normalized_robust_sigma,
        "effective_detector_only_reduced_chi2": result.residual_floor_effective_detector_only_reduced_chi2,
        "effective_total_normalized_robust_sigma": result.residual_floor_effective_total_normalized_robust_sigma,
        "effective_total_reduced_chi2": result.residual_floor_effective_total_reduced_chi2,
        "effective_normalized_robust_sigma": result.residual_floor_effective_normalized_robust_sigma,
        "effective_reduced_chi2": result.residual_floor_effective_reduced_chi2,
        "pairwise_correlation_median": result.residual_floor_pairwise_correlation_median,
        "pairwise_abs_correlation_median": result.residual_floor_pairwise_abs_correlation_median,
        "pairwise_abs_correlation_p90": result.residual_floor_pairwise_abs_correlation_p90,
        "pairwise_correlation_after_common_median": result.residual_floor_pairwise_correlation_after_common_median,
        "pairwise_abs_correlation_after_common_median": result.residual_floor_pairwise_abs_correlation_after_common_median,
        "pairwise_pairs": result.residual_floor_pairwise_pairs,
        "pairwise_correlations_evaluated": (
            result.residual_floor_pairwise_correlations_evaluated
        ),
        "annotation_measurements": result.residual_floor_annotation_measurements,
        "annotation_calibrations": result.residual_floor_annotation_calibrations,
        "magnitude_bins": result.residual_floor_bins,
        "magnitude_bins_after_frame_common": result.residual_floor_after_common_bins,
        "associations": result.residual_floor_associations,
        "objects_csv": str(floor_objects_path),
        "frames_csv": str(floor_frames_path),
        "interpretation": (
            "The lower envelope reduces contamination by variable reference stars. "
            "mag_err_effective_detector_only combines the raw physical detector model "
            "with the floor, while mag_err_effective_total also preserves downstream "
            "random-error terms such as differential aperture-correction uncertainty. "
            "mag_err_effective is a backward-compatible alias of the total quantity. "
            "The frame-common-subtracted floor is called independent only when both "
            "floor and pairwise-correlation improvement criteria pass. All effective "
            "errors remain provenance only; mag_err_random is not replaced."
        ),
        "warnings": result.warnings,
    }
    summary_path.write_text(
        json.dumps(_json_safe(validation_payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    floor_summary_path.write_text(
        json.dumps(_json_safe(floor_payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _write_csv(rows: list[dict[str, Any]], path: Path, fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _fieldnames_for_rows(rows: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _load_frame_context(path: str | Path | None) -> dict[int, dict[str, Any]]:
    if path is None:
        return {}
    context_path = Path(path)
    if not context_path.exists():
        return {}
    out: dict[int, dict[str, Any]] = {}
    try:
        with context_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    frame_id = int(row.get("frame_id") or "")
                except ValueError:
                    continue
                out[frame_id] = dict(row)
    except OSError:
        return {}
    return out


def _frame_pre_qc(flags: Any) -> dict[str, Any]:
    if not isinstance(flags, dict):
        return {}
    for key in ("frame_pre_photometry_qc_v1", "FRAME_PRE_PHOTOMETRY_QC_V1"):
        value = flags.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _frame_detection_qc(flags: Any) -> dict[str, Any]:
    if not isinstance(flags, dict):
        return {}
    groups = flags.get("detection_catalog_qc") or flags.get("DETECTION_CATALOG_QC")
    if not isinstance(groups, dict):
        return {}
    values = [value for value in groups.values() if isinstance(value, dict)]
    return values[0] if values else {}


def _measurement_is_hard_rejected(flags: dict[str, Any]) -> bool:
    for key in (
        "bad_xy",
        "outside_image",
        "empty_aperture",
        "non_positive_flux",
        "saturated",
        "saturated_or_near_saturated",
    ):
        if bool(flags.get(key)):
            return True
    return False


def _magnitude_bins(
    object_rows: list[dict[str, Any]],
    *,
    include_legacy_comparison: bool = True,
) -> list[dict[str, Any]]:
    if not object_rows:
        return []
    mags = [float(row["mag_median"]) for row in object_rows]
    lower = math.floor(min(mags))
    upper = math.ceil(max(mags))
    bins: list[dict[str, Any]] = []
    for left in range(lower, upper + 1):
        right = left + 1
        rows = [
            row
            for row in object_rows
            if left <= float(row["mag_median"]) < right
        ]
        if not rows:
            continue
        bins.append(
            {
                "mag_min": float(left),
                "mag_max": float(right),
                "n_objects": len(rows),
                "physical_normalized_robust_sigma_median": _median_or_none(
                    row.get("physical_normalized_robust_sigma") for row in rows
                ),
                **(
                    {
                        "legacy_normalized_robust_sigma_median": _median_or_none(
                            row.get("legacy_normalized_robust_sigma") for row in rows
                        )
                    }
                    if include_legacy_comparison
                    else {}
                ),
                "physical_mag_err_median": _median_or_none(
                    row.get("physical_mag_err_median") for row in rows
                ),
                **(
                    {
                        "legacy_mag_err_median": _median_or_none(
                            row.get("legacy_mag_err_median") for row in rows
                        )
                    }
                    if include_legacy_comparison
                    else {}
                ),
                "residual_floor_raw_median": _median_or_none(
                    row.get("residual_floor_raw_mag") for row in rows
                ),
                "residual_floor_after_frame_common_median": _median_or_none(
                    row.get("residual_floor_after_frame_common_mag") for row in rows
                ),
            }
        )
    return bins


def _quadrature_excess(observed: Any, detector: Any) -> float | None:
    obs = _finite_or_none(observed)
    det = _positive_or_none(detector)
    if obs is None or det is None:
        return None
    return float(math.sqrt(max(obs * obs - det * det, 0.0)))


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_or_none(value: Any) -> float | None:
    parsed = _finite_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _rms(values: Iterable[float]) -> float | None:
    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if array.size == 0:
        return None
    return float(np.sqrt(np.mean(array**2)))


def _reduced_chi2(
    values: Iterable[float], *, fitted_parameters: int = 0
) -> float | None:
    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    degrees_of_freedom = int(array.size) - max(0, int(fitted_parameters))
    if degrees_of_freedom <= 0:
        return None
    return float(np.sum(array**2) / degrees_of_freedom)


def _mad(values: Iterable[float]) -> float | None:
    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    if array.size == 0:
        return None
    median = float(np.median(array))
    return float(np.median(np.abs(array - median)))


def _robust_sigma(values: Iterable[float]) -> float | None:
    mad = _mad(values)
    return None if mad is None else float(1.4826 * mad)


def _median_or_none(values: Iterable[Any]) -> float | None:
    finite = [value for value in (_finite_or_none(value) for value in values) if value is not None]
    if not finite:
        return None
    return float(np.median(np.asarray(finite, dtype=float)))


def _log_distance_to_unity(value: float | None) -> float | None:
    if value is None or value <= 0 or not math.isfinite(value):
        return None
    return float(abs(math.log(value)))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
