from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.calibration.normalized_terms import FrameTermInput, ObjectTermInput, replace_calibration_iteration
from grandgousier.catalogs.reference_photometry import load_reference_photometry_by_identity
from grandgousier.calibration.provenance import prepare_calibration_version_for_write, set_source_calibration
from grandgousier.db.models import (
    CalibrationFrameTerm,
    CalibrationVersion,
    FieldSeriesFrame,
    Frame,
    Measurement,
    ObjectQc,
)
from grandgousier.photometry.calibration_view import load_calibration_photometry_view


_SUPPORTED_BANDS = {"g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia"}
_REF_MAG_COLUMNS = {band: f"mag_{band}" for band in _SUPPORTED_BANDS}

_DEFAULT_EXCLUDED_OBJECT_QC_FLAGS = (
    "APERTURE_MEDIAN_SHIFT",
    "APERTURE_UNSTABLE",
    "BLENDED_NEIGHBOR",
    "CROWDED",
    "DYNAMIC_BLEND_RISK",
    "DYNAMIC_CROWDED",
    "EDGE",
    "FAINT_NOISY",
    "HIGH_RMS",
    "NO_DETECTION_SUPPORT",
    "POSSIBLE_BLEND",
    "SATURATED_LIKE",
    "WEAK_DETECTION_SUPPORT",
)


@dataclass
class ColorCalibrationDiagnosticsResult:
    calibration_name: str
    series_id: int | None
    identity_catalog_name: str
    reference_catalog_name: str
    reference_band: str
    color_index: str
    out_dir: str | None = None

    n_measurements_input: int = 0
    n_measurements_with_reference: int = 0
    n_measurements_after_basic_filters: int = 0
    n_used_measurements: int = 0
    n_used_objects: int = 0
    n_used_frames: int = 0
    n_frames_with_zp: int = 0
    n_rejected_low_object_measurements: int = 0
    n_rejected_low_frame_stars: int = 0
    n_clipped_measurements: int = 0

    n_reference_objects: int = 0
    n_calibrator_candidate_objects: int = 0
    n_excluded_objects: int = 0
    n_excluded_by_object_id: int = 0
    n_excluded_by_object_qc: int = 0
    n_excluded_by_reference_mag: int = 0
    n_excluded_by_color: int = 0
    n_excluded_by_low_object_measurements: int = 0

    include_flagged_calibrators: bool = False
    excluded_object_qc_flags: list[str] = field(default_factory=list)
    explicit_exclude_object_ids: list[int] = field(default_factory=list)
    min_reference_mag: float | None = None
    max_reference_mag: float | None = None
    min_color: float | None = None
    max_color: float | None = None

    color0: float | None = None
    color_min: float | None = None
    color_max: float | None = None

    current_residual_median: float | None = None
    current_residual_mad: float | None = None
    current_residual_rms: float | None = None
    current_color_intercept: float | None = None
    current_color_slope: float | None = None
    current_residual_after_color_median: float | None = None
    current_residual_after_color_mad: float | None = None
    current_residual_after_color_rms: float | None = None

    ensemble_transform_offset: float | None = None
    ensemble_transform_color_term: float | None = None
    ensemble_transform_offset_err: float | None = None
    ensemble_transform_color_term_err: float | None = None
    ensemble_transform_offset_color_cov: float | None = None
    ensemble_transform_residual_median: float | None = None
    ensemble_transform_residual_mad: float | None = None
    ensemble_transform_residual_rms: float | None = None
    ensemble_transform_residual_robust_sigma: float | None = None
    ensemble_transform_used_objects: int = 0
    ensemble_transform_clipped_objects: int = 0
    ensemble_transform_used_measurements: int = 0
    ensemble_transform_candidate_object_ids: list[int] = field(default_factory=list)
    ensemble_transform_used_object_ids: list[int] = field(default_factory=list)
    ensemble_transform_clipped_object_ids: list[int] = field(default_factory=list)
    calibrator_color_sigma: float | None = None
    ensemble_offset_only_offset: float | None = None
    ensemble_offset_only_offset_err: float | None = None
    ensemble_offset_only_residual_median: float | None = None
    ensemble_offset_only_residual_mad: float | None = None
    ensemble_offset_only_residual_rms: float | None = None
    ensemble_offset_only_residual_robust_sigma: float | None = None
    ensemble_offset_only_bic: float | None = None
    ensemble_color_model_bic: float | None = None
    ensemble_color_model_bic_improvement: float | None = None
    ensemble_color_model_mad_improvement: float | None = None
    ensemble_color_model_rms_improvement: float | None = None
    ensemble_color_term_significance: float | None = None

    frame_model_color_term: float | None = None
    frame_model_residual_median: float | None = None
    frame_model_residual_mad: float | None = None
    frame_model_residual_rms: float | None = None
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None

    summary_json: str | None = None
    stars_csv: str | None = None
    frames_csv: str | None = None
    frame_zp_by_frame: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ColorCalibrationApplyResult:
    source_calibration_name: str
    target_calibration_name: str
    series_id: int | None
    identity_catalog_name: str
    reference_catalog_name: str
    reference_band: str
    color_index: str
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    dry_run: bool = False

    n_measurements_input: int = 0
    n_used_calibrator_measurements: int = 0
    n_used_calibrator_objects: int = 0
    n_clipped_calibrator_objects: int = 0
    n_frames_with_zp: int = 0
    checked_measurements: int = 0
    n_source_frames: int = 0
    calibrated: int = 0
    calibrated_with_reference_color: int = 0
    calibrated_with_pivot_color: int = 0
    copied_without_calibrated_mag: int = 0
    inserted_or_updated: int = 0
    deleted_existing: int = 0
    skipped_no_source_mag: int = 0
    skipped_no_mag_inst: int = 0  # Backward-compatible alias for old reports.
    skipped_no_color: int = 0
    skipped_no_frame_zp: int = 0  # Always zero for ensemble-preserving v2.
    skipped_saturated: int = 0
    skipped_nonfinite: int = 0
    skipped_no_reference: int = 0

    color0: float | None = None
    color_min: float | None = None
    color_max: float | None = None
    color_span: float | None = None
    color_term: float | None = None
    color_term_err: float | None = None
    fitted_color_term: float | None = None
    fitted_color_term_err: float | None = None
    transform_offset: float | None = None
    transform_offset_err: float | None = None
    transform_offset_color_cov: float | None = None
    transform_residual_rms: float | None = None
    transform_residual_mad: float | None = None
    transform_residual_robust_sigma: float | None = None
    calibrator_color_sigma: float | None = None
    frame_model_residual_rms: float | None = None
    frame_model_residual_mad: float | None = None
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None
    pivot_color_fraction: float | None = None
    systematic_error_median: float | None = None
    systematic_error_max: float | None = None
    max_abs_rms_delta: float | None = None
    max_abs_mad_delta: float | None = None
    max_abs_peak_to_peak_delta: float | None = None
    output_mag_median: float | None = None
    output_mag_mad: float | None = None
    selected_transform_model: str | None = None
    model_selection_reason: str | None = None
    model_selection_thresholds: dict[str, Any] = field(default_factory=dict)
    color_term_significance: float | None = None
    offset_only_residual_mad: float | None = None
    offset_only_residual_rms: float | None = None
    color_model_residual_mad: float | None = None
    color_model_residual_rms: float | None = None
    offset_only_bic: float | None = None
    color_model_bic: float | None = None
    color_model_bic_improvement: float | None = None
    color_model_mad_improvement: float | None = None
    color_model_rms_improvement: float | None = None

    quality_gate_passed: bool = False
    quality_gate_failures: list[str] = field(default_factory=list)
    quality_gate_thresholds: dict[str, Any] = field(default_factory=dict)
    calibrator_candidate_object_ids: list[int] = field(default_factory=list)
    calibrator_used_object_ids: list[int] = field(default_factory=list)
    calibrator_clipped_object_ids: list[int] = field(default_factory=list)
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    normalized_object_terms: int = 0
    normalized_write_status: str = "NOT_WRITTEN"
    performance_diagnostics_enabled: bool = False
    timings_sec: dict[str, float] = field(default_factory=dict)
    throughput: dict[str, float] = field(default_factory=dict)
    measurement_row_read_strategy: str | None = None
    shared_measurement_rows: int = 0

    diagnostics_summary_json: str | None = None
    diagnostics_stars_csv: str | None = None
    diagnostics_frames_csv: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_color_calibration_diagnostics(
    session: Session,
    *,
    calibration_name: str,
    identity_catalog_name: str = "gaia_dr3_master",
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    series_id: int | None = None,
    out_dir: str | Path | None = None,
    min_measurements_per_object: int = 5,
    min_stars_per_frame: int = 20,
    max_measurement_mag_err: float | None = None,
    max_reference_mag_err: float | None = None,
    min_reference_mag: float | None = None,
    max_reference_mag: float | None = None,
    min_color: float | None = None,
    max_color: float | None = None,
    include_flagged_calibrators: bool = False,
    excluded_object_qc_flags: Iterable[str] | None = None,
    exclude_object_ids: Iterable[int] | None = None,
    clip_sigma: float = 4.0,
    n_iterations: int = 5,
    exclude_saturated: bool = True,
    limit: int | None = None,
    _performance_timings: dict[str, float] | None = None,
    _preloaded_rows: list[dict[str, Any]] | None = None,
) -> ColorCalibrationDiagnosticsResult:
    """Diagnose color-dependent photometric residuals without writing measurements.

    The command intentionally produces diagnostics only. It does not modify
    calibration_versions or measurements. Two complementary diagnostics are
    computed:

    1. Current calibrated magnitude residuals: median(mag_cal) - reference_mag
       per object, fitted as a linear function of color.
    2. A frame-zero-point + color-term model built from mag_inst:
       reference_mag - mag_inst = ZP_frame + C * (color - color0).
    """

    ref_band = normalize_reference_band(reference_band)
    color_key, color_bands = parse_color_index(color_index)
    if min_measurements_per_object < 1:
        raise ValueError("min_measurements_per_object must be >= 1")
    if min_stars_per_frame < 1:
        raise ValueError("min_stars_per_frame must be >= 1")
    if clip_sigma <= 0:
        raise ValueError("clip_sigma must be positive")
    if n_iterations < 1:
        raise ValueError("n_iterations must be >= 1")

    excluded_flags = tuple(
        sorted({_normalize_flag_name(f) for f in (excluded_object_qc_flags or _DEFAULT_EXCLUDED_OBJECT_QC_FLAGS) if str(f).strip()})
    )
    explicit_exclude_ids = tuple(sorted({int(v) for v in (exclude_object_ids or [])}))

    out_dir_path = Path(out_dir) if out_dir else None
    result = ColorCalibrationDiagnosticsResult(
        calibration_name=calibration_name,
        series_id=series_id,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        out_dir=str(out_dir_path) if out_dir_path else None,
        include_flagged_calibrators=bool(include_flagged_calibrators),
        excluded_object_qc_flags=list(excluded_flags),
        explicit_exclude_object_ids=list(explicit_exclude_ids),
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
    )

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    if _preloaded_rows is None:
        rows = _load_color_measurement_rows(
            session,
            calibration_name=calibration_name,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=reference_catalog_name,
            reference_band=ref_band,
            series_id=series_id,
            limit=limit,
            require_mag_inst=False,
        )
        if _performance_timings is not None:
            _performance_timings["diagnostics_load_rows"] = time.perf_counter() - _perf_t0
            _performance_timings["diagnostics_reuse_rows"] = 0.0
    else:
        rows = _select_color_measurement_rows(
            _preloaded_rows,
            require_reference=True,
            require_mag_inst=False,
            limit=limit,
        )
        if _performance_timings is not None:
            _performance_timings["diagnostics_load_rows"] = 0.0
            _performance_timings["diagnostics_reuse_rows"] = time.perf_counter() - _perf_t0
    result.n_measurements_input = len(rows)
    result.n_measurements_with_reference = len(rows)
    if not rows:
        result.warnings.append("No measurements matched the requested calibration/reference catalogs.")
        _write_outputs(result, [], [], out_dir_path)
        return result

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    prepared = []
    for row in rows:
        flags = row.get("measurement_flags") if isinstance(row.get("measurement_flags"), dict) else {}
        if exclude_saturated and _is_saturated(flags):
            continue
        mag_inst = _to_float(row.get("mag_inst"))
        mag_cal = _to_float(row.get("mag_cal"))
        ref_mag = _to_float(row.get("reference_mag"))
        color = _extract_color(row, color_key=color_key, color_bands=color_bands)
        if not _is_finite(mag_cal) or not _is_finite(ref_mag) or not _is_finite(color):
            continue
        mag_err = _to_float(row.get("mag_err"))
        if max_measurement_mag_err is not None and _is_finite(mag_err) and float(mag_err) > float(max_measurement_mag_err):
            continue
        ref_mag_err = _reference_mag_error(row, ref_band)
        if max_reference_mag_err is not None and _is_finite(ref_mag_err) and float(ref_mag_err) > float(max_reference_mag_err):
            continue
        d = dict(row)
        d["mag_inst"] = float(mag_inst) if _is_finite(mag_inst) else None
        d["reference_mag"] = float(ref_mag)
        d["color_value"] = float(color)
        d["mag_cal"] = float(mag_cal)
        d["mag_err"] = mag_err
        d["reference_mag_err"] = ref_mag_err
        prepared.append(d)

    if _performance_timings is not None:
        _performance_timings["diagnostics_basic_filters"] = time.perf_counter() - _perf_t0
    result.n_measurements_after_basic_filters = len(prepared)
    if not prepared:
        result.warnings.append("No measurements survived basic finite/error/saturation filters.")
        _write_outputs(result, [], [], out_dir_path)
        return result

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    object_info = _build_object_filter_info(
        prepared,
        min_measurements_per_object=min_measurements_per_object,
        include_flagged_calibrators=include_flagged_calibrators,
        excluded_object_qc_flags=excluded_flags,
        explicit_exclude_object_ids=explicit_exclude_ids,
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
    )
    if _performance_timings is not None:
        _performance_timings["diagnostics_object_filter"] = time.perf_counter() - _perf_t0
    result.n_reference_objects = object_info["n_reference_objects"]
    result.n_excluded_objects = object_info["n_excluded_objects"]
    result.n_excluded_by_object_id = object_info["n_excluded_by_object_id"]
    result.n_excluded_by_object_qc = object_info["n_excluded_by_object_qc"]
    result.n_excluded_by_reference_mag = object_info["n_excluded_by_reference_mag"]
    result.n_excluded_by_color = object_info["n_excluded_by_color"]
    result.n_excluded_by_low_object_measurements = object_info["n_excluded_by_low_object_measurements"]

    candidate_object_ids = object_info["candidate_object_ids"]
    exclusion_reasons_by_object = object_info["exclusion_reasons_by_object"]
    used = [row for row in prepared if int(row["object_id"]) in candidate_object_ids]
    result.n_calibrator_candidate_objects = len(candidate_object_ids)
    result.n_rejected_low_object_measurements = object_info["n_measurements_excluded_by_low_object_measurements"]
    if not used:
        result.warnings.append("No objects survived calibrator QC/magnitude/color/min-measurement filters.")
        star_rows = _build_star_rows(prepared, color0=0.0, exclusion_reasons_by_object=exclusion_reasons_by_object)
        _write_outputs(result, star_rows, [], out_dir_path)
        return result

    result.color0 = _median(_unique_object_values(used, "object_id", "color_value"))
    colors = [float(r["color_value"]) for r in used]
    result.color_min = min(colors) if colors else None
    result.color_max = max(colors) if colors else None
    if result.color0 is None:
        result.warnings.append("Could not compute color0.")
        star_rows = _build_star_rows(prepared, color0=0.0, exclusion_reasons_by_object=exclusion_reasons_by_object)
        _write_outputs(result, star_rows, [], out_dir_path)
        return result

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    star_rows = _build_star_rows(prepared, color0=float(result.color0), exclusion_reasons_by_object=exclusion_reasons_by_object)
    _compute_current_color_fit(star_rows, result, clip_sigma=clip_sigma, n_iterations=n_iterations)
    if _performance_timings is not None:
        _performance_timings["diagnostics_current_color_fit"] = time.perf_counter() - _perf_t0

    frame_model_input = [row for row in used if _is_finite(row.get("mag_inst"))]
    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    model = _fit_frame_color_model(
        frame_model_input,
        color0=float(result.color0),
        min_stars_per_frame=min_stars_per_frame,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
    )
    if _performance_timings is not None:
        _performance_timings["diagnostics_frame_color_model"] = time.perf_counter() - _perf_t0
    result.frame_model_color_term = model["color_term"]
    result.n_used_measurements = model["n_used_measurements"]
    result.n_used_objects = model["n_used_objects"]
    result.n_used_frames = model["n_used_frames"]
    result.n_frames_with_zp = model["n_frames_with_zp"]
    result.n_rejected_low_frame_stars = model["n_rejected_low_frame_stars"]
    result.n_clipped_measurements = model["n_clipped_measurements"]
    result.frame_model_residual_median = model["residual_median"]
    result.frame_model_residual_mad = model["residual_mad"]
    result.frame_model_residual_rms = model["residual_rms"]
    result.frame_zp_median = model["frame_zp_median"]
    result.frame_zp_mad = model["frame_zp_mad"]

    frame_rows = model["frame_rows"]
    result.frame_zp_by_frame = {
        str(int(row["frame_id"])): float(row["frame_zp"])
        for row in frame_rows
        if row.get("frame_zp") is not None and _is_finite(row.get("frame_zp"))
    }
    # Add frame model residual summaries back to star rows.
    residuals_by_object: dict[int, list[float]] = {}
    for row in model["measurement_rows"]:
        if row.get("model_used") and _is_finite(row.get("frame_model_residual")):
            residuals_by_object.setdefault(int(row["object_id"]), []).append(float(row["frame_model_residual"]))
    for srow in star_rows:
        vals = residuals_by_object.get(int(srow["object_id"]), [])
        srow["frame_model_residual_median"] = _median(vals)
        srow["frame_model_residual_mad"] = _mad(vals)
        srow["frame_model_n_measurements"] = len(vals)
        if vals:
            srow["used_as_calibrator"] = True
        elif srow.get("calibrator_candidate") and not srow.get("exclusion_reason"):
            srow["exclusion_reason"] = "MODEL_CLIPPED_OR_NO_FRAME_ZP"
    if result.n_used_measurements == 0:
        result.warnings.append("Frame/color model has no usable measurements after frame-count/clipping filters.")
    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    _write_outputs(result, star_rows, frame_rows, out_dir_path)
    if _performance_timings is not None:
        _performance_timings["diagnostics_write_outputs"] = time.perf_counter() - _perf_t0
    return result



def apply_color_calibration(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    identity_catalog_name: str = "gaia_dr3_master",
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    series_id: int | None = None,
    out_dir: str | Path | None = None,
    min_measurements_per_object: int = 5,
    min_stars_per_frame: int = 20,
    max_measurement_mag_err: float | None = None,
    max_reference_mag_err: float | None = None,
    min_reference_mag: float | None = None,
    max_reference_mag: float | None = None,
    min_color: float | None = None,
    max_color: float | None = None,
    include_flagged_calibrators: bool = False,
    excluded_object_qc_flags: Iterable[str] | None = None,
    exclude_object_ids: Iterable[int] | None = None,
    clip_sigma: float = 4.0,
    n_iterations: int = 5,
    exclude_saturated: bool = True,
    limit: int | None = None,
    min_calibrator_objects: int = 20,
    min_color_span: float = 0.3,
    max_transform_residual_mad: float | None = 0.10,
    max_pivot_color_fraction: float | None = 0.25,
    max_shape_metric_delta: float = 1e-10,
    min_color_term_significance: float = 2.0,
    min_color_model_bic_improvement: float = 6.0,
    min_color_model_mad_improvement: float = 0.0,
    dry_run: bool = False,
    immutable_generation: bool = False,
    materialize_measurements: bool = True,
    diagnose_performance: bool = False,
    _preloaded_analysis_snapshot: Any | None = None,
) -> ColorCalibrationApplyResult:
    """Apply an absolute reference-band transform without altering time-series shape.

    A robust object-level fit is derived from median source ensemble magnitudes:

        reference_mag - median(source_mag_cal) = A + C * (color - color0)

    Every epoch of one object receives the same additive correction. Thus the
    source ensemble RMS, MAD and robust peak-to-peak are preserved. The older
    frame-ZP model is still computed as a diagnostic but is not used to create
    the science calibration.
    """

    _function_t0 = time.perf_counter() if diagnose_performance else 0.0
    timings: dict[str, float] = {}

    if not target_calibration_name:
        raise ValueError("target_calibration_name is required")
    if target_calibration_name == source_calibration_name:
        raise ValueError("target_calibration_name must differ from source_calibration_name")
    if min_calibrator_objects < 3:
        raise ValueError("min_calibrator_objects must be >= 3")
    if min_color_span < 0:
        raise ValueError("min_color_span must be non-negative")
    if max_shape_metric_delta < 0:
        raise ValueError("max_shape_metric_delta must be non-negative")
    if min_color_term_significance < 0:
        raise ValueError("min_color_term_significance must be non-negative")
    if min_color_model_bic_improvement < 0:
        raise ValueError("min_color_model_bic_improvement must be non-negative")

    ref_band = normalize_reference_band(reference_band)
    color_key, color_bands = parse_color_index(color_index)
    result = ColorCalibrationApplyResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        series_id=series_id,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        dry_run=bool(dry_run),
        quality_gate_thresholds={
            "min_calibrator_objects": int(min_calibrator_objects),
            "min_color_span": float(min_color_span),
            "max_transform_residual_mad": max_transform_residual_mad,
            "max_pivot_color_fraction": max_pivot_color_fraction,
            "max_shape_metric_delta": float(max_shape_metric_delta),
        },
        model_selection_thresholds={
            "min_color_term_significance": float(min_color_term_significance),
            "min_color_model_bic_improvement": float(min_color_model_bic_improvement),
            "min_color_model_mad_improvement": float(min_color_model_mad_improvement),
        },
    )

    result.performance_diagnostics_enabled = bool(diagnose_performance)
    result.timings_sec = timings

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id
    if diagnose_performance:
        timings["source_calibration_lookup"] = time.perf_counter() - _perf_t0

    # Diagnostics and application need the same reconstructed source calibration,
    # frame metadata, ObjectQc rows and reference photometry.  Load the superset
    # once and derive both views in memory.  This is especially important for
    # normalized-only ENSEMBLE products, where reconstructing ~50k rows twice
    # dominated the entire color-calibration stage.
    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    _color_read_metadata: dict[str, Any] = {}
    shared_rows = _load_color_measurement_rows(
        session,
        calibration_name=source_calibration_name,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        series_id=series_id,
        limit=None,
        require_reference=False,
        require_mag_inst=False,
        # Normalized-only pipeline generations do not need the full reconstructed
        # per-epoch provenance flags here.  PostgreSQL can reconstruct the scalar
        # source state and decorate it with the immutable BASE flags in one query.
        # Keep the legacy/full materialization path unchanged because callers that
        # explicitly write intermediate Measurement rows still require those flags.
        prefer_server_normalized=not bool(materialize_measurements),
        _performance_timings=timings if diagnose_performance else None,
        _read_metadata=_color_read_metadata,
        _preloaded_analysis_snapshot=_preloaded_analysis_snapshot,
    )
    result.measurement_row_read_strategy = str(
        _color_read_metadata.get("strategy") or "SHARED_SUPERSET_SINGLE_LOAD_V1"
    )
    result.shared_measurement_rows = len(shared_rows)
    if diagnose_performance:
        timings["load_shared_color_rows"] = time.perf_counter() - _perf_t0
        result.throughput["shared_measurement_rows"] = float(len(shared_rows))

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    diag = compute_color_calibration_diagnostics(
        session,
        calibration_name=source_calibration_name,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        series_id=series_id,
        out_dir=out_dir,
        min_measurements_per_object=min_measurements_per_object,
        min_stars_per_frame=min_stars_per_frame,
        max_measurement_mag_err=max_measurement_mag_err,
        max_reference_mag_err=max_reference_mag_err,
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
        include_flagged_calibrators=include_flagged_calibrators,
        excluded_object_qc_flags=excluded_object_qc_flags,
        exclude_object_ids=exclude_object_ids,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
        exclude_saturated=exclude_saturated,
        limit=limit,
        _performance_timings=timings if diagnose_performance else None,
        _preloaded_rows=shared_rows,
    )
    if diagnose_performance:
        timings["compute_diagnostics_total"] = time.perf_counter() - _perf_t0
    result.n_measurements_input = diag.n_measurements_input
    result.n_used_calibrator_measurements = diag.ensemble_transform_used_measurements
    result.n_used_calibrator_objects = diag.ensemble_transform_used_objects
    result.n_clipped_calibrator_objects = diag.ensemble_transform_clipped_objects
    result.calibrator_candidate_object_ids = list(diag.ensemble_transform_candidate_object_ids)
    result.calibrator_used_object_ids = list(diag.ensemble_transform_used_object_ids)
    result.calibrator_clipped_object_ids = list(diag.ensemble_transform_clipped_object_ids)
    result.n_frames_with_zp = diag.n_frames_with_zp  # Secondary diagnostic only.
    result.color0 = diag.color0
    result.color_min = diag.color_min
    result.color_max = diag.color_max
    if diag.color_min is not None and diag.color_max is not None:
        result.color_span = float(diag.color_max) - float(diag.color_min)
    result.fitted_color_term = diag.ensemble_transform_color_term
    result.fitted_color_term_err = diag.ensemble_transform_color_term_err
    result.color_term_significance = diag.ensemble_color_term_significance
    result.offset_only_residual_mad = diag.ensemble_offset_only_residual_mad
    result.offset_only_residual_rms = diag.ensemble_offset_only_residual_rms
    result.color_model_residual_mad = diag.ensemble_transform_residual_mad
    result.color_model_residual_rms = diag.ensemble_transform_residual_rms
    result.offset_only_bic = diag.ensemble_offset_only_bic
    result.color_model_bic = diag.ensemble_color_model_bic
    result.color_model_bic_improvement = diag.ensemble_color_model_bic_improvement
    result.color_model_mad_improvement = diag.ensemble_color_model_mad_improvement
    result.color_model_rms_improvement = diag.ensemble_color_model_rms_improvement

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    selection = _select_ensemble_transform_model(
        diag,
        min_color_term_significance=min_color_term_significance,
        min_color_model_bic_improvement=min_color_model_bic_improvement,
        min_color_model_mad_improvement=min_color_model_mad_improvement,
    )
    if diagnose_performance:
        timings["model_selection"] = time.perf_counter() - _perf_t0
    result.selected_transform_model = selection["model"]
    result.model_selection_reason = selection["reason"]
    result.color_term = selection["color_term"]
    result.color_term_err = selection["color_term_err"]
    result.transform_offset = selection["offset"]
    result.transform_offset_err = selection["offset_err"]
    result.transform_offset_color_cov = selection["offset_color_cov"]
    result.transform_residual_rms = selection["residual_rms"]
    result.transform_residual_mad = selection["residual_mad"]
    result.transform_residual_robust_sigma = selection["residual_robust_sigma"]
    result.calibrator_color_sigma = diag.calibrator_color_sigma
    result.frame_model_residual_rms = diag.frame_model_residual_rms
    result.frame_model_residual_mad = diag.frame_model_residual_mad
    result.frame_zp_median = diag.frame_zp_median
    result.frame_zp_mad = diag.frame_zp_mad
    result.diagnostics_summary_json = diag.summary_json
    result.diagnostics_stars_csv = diag.stars_csv
    result.diagnostics_frames_csv = diag.frames_csv
    result.warnings.extend(diag.warnings)

    if diag.color0 is None or result.transform_offset is None or result.color_term is None:
        result.quality_gate_failures.append("INCOMPLETE_ENSEMBLE_TRANSFORM")
        result.warnings.append("Ensemble-preserving color transform is incomplete; no target calibration was written.")
        if diagnose_performance:
            timings["function_total"] = time.perf_counter() - _function_t0
        return result

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    rows = _select_color_measurement_rows(
        shared_rows,
        require_reference=False,
        require_mag_inst=False,
        limit=limit,
    )
    if diagnose_performance:
        timings["load_application_rows"] = 0.0
        timings["reuse_application_rows"] = time.perf_counter() - _perf_t0
    result.checked_measurements = len(rows)
    result.n_source_frames = len({int(row["frame_id"]) for row in rows})

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    prepared_rows: list[dict[str, Any]] = []
    output_mags: list[float] = []
    systematic_errors: list[float] = []
    source_by_object: dict[int, list[float]] = {}
    output_by_object: dict[int, list[float]] = {}
    offset = float(result.transform_offset)
    color_term = float(result.color_term)
    color0 = float(diag.color0)
    systematic_floor = float(result.transform_residual_robust_sigma or result.transform_residual_mad or 0.0)
    fallback_color_sigma = float(diag.calibrator_color_sigma or 0.0)

    for row in rows:
        flags0 = row.get("measurement_flags") if isinstance(row.get("measurement_flags"), dict) else {}
        source_mag = _to_float(row.get("mag_cal"))
        color_value = _extract_color(row, color_key=color_key, color_bands=color_bands)
        color_missing = not _is_finite(color_value)
        applied_color_value = color0 if color_missing else float(color_value)
        x_color = applied_color_value - color0
        correction = offset + color_term * x_color
        output_mag: float | None = None
        calibration_skip_reason = None
        if not _is_finite(source_mag):
            result.skipped_no_source_mag += 1
            result.skipped_no_mag_inst += 1
            calibration_skip_reason = "NO_SOURCE_MAG_CAL"
        else:
            candidate_output_mag = float(source_mag) + correction
            if _is_finite(candidate_output_mag):
                output_mag = candidate_output_mag
            else:
                result.skipped_nonfinite += 1
                calibration_skip_reason = "NONFINITE_OUTPUT_MAG"

        color_outside_fit_range = False
        if not color_missing and diag.color_min is not None and float(color_value) < float(diag.color_min):
            color_outside_fit_range = True
        if not color_missing and diag.color_max is not None and float(color_value) > float(diag.color_max):
            color_outside_fit_range = True

        random_err = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        color_err = _reference_color_error(row, color_bands=color_bands)
        if color_missing:
            color_err = fallback_color_sigma if fallback_color_sigma > 0 else None
        systematic_err = _color_systematic_error(
            x_color=x_color,
            color_term=color_term,
            color_error=color_err,
            offset_error=result.transform_offset_err,
            color_term_error=result.color_term_err,
            offset_color_cov=result.transform_offset_color_cov,
            model_floor=systematic_floor,
        )

        flags = _build_reference_color_measurement_flags(
            source_flags=flags0,
            source_measurement_id=row.get("measurement_id"),
            source_calibration_version_id=source_cv.calibration_version_id,
            source_calibration_name=source_cv.name,
            identity_catalog_name=identity_catalog_name,
            source_id=row.get("source_id"),
            reference_catalog_name=reference_catalog_name,
            reference_catalog_source_id=row.get("reference_catalog_source_id"),
            reference_band=ref_band,
            reference_mag=row.get("reference_mag"),
            color_index=color_key,
            color_value=None if color_missing else float(color_value),
            color0=color0,
            color_term=color_term,
            fitted_color_term=result.fitted_color_term,
            fitted_color_term_error=result.fitted_color_term_err,
            transform_model=result.selected_transform_model,
            model_selection_reason=result.model_selection_reason,
            transform_offset=offset,
            correction=correction,
            color_outside_fit_range=color_outside_fit_range,
            applied_color_value=applied_color_value,
            color_missing=color_missing,
            calibration_applied=output_mag is not None,
            calibration_skip_reason=calibration_skip_reason,
            random_error=random_err,
            systematic_error=systematic_err,
            transform_offset_error=result.transform_offset_err,
            color_term_error=result.color_term_err,
            color_error=color_err,
            model_systematic_floor=systematic_floor,
        )
        if output_mag is None:
            result.copied_without_calibrated_mag += 1
        else:
            result.calibrated += 1
            if color_missing:
                result.calibrated_with_pivot_color += 1
            else:
                result.calibrated_with_reference_color += 1
            output_mags.append(output_mag)
            if systematic_err is not None:
                systematic_errors.append(systematic_err)
            oid = int(row["object_id"])
            source_by_object.setdefault(oid, []).append(float(source_mag))
            output_by_object.setdefault(oid, []).append(float(output_mag))

        prepared_rows.append(
            {
                "object_id": row["object_id"],
                "frame_id": row["frame_id"],
                "x": row.get("x"),
                "y": row.get("y"),
                "flux": row.get("flux"),
                "flux_err": row.get("flux_err"),
                "mag_inst": row.get("mag_inst"),
                "mag_cal": output_mag,
                "mag_err": random_err,
                "mag_err_random": random_err,
                "mag_err_systematic": systematic_err,
                "aperture_radius_pix": row.get("aperture_radius_pix"),
                "local_background": row.get("local_background"),
                # Constant object transform; do not masquerade as a local/frame ZP.
                "local_zp_correction": row.get("local_zp_correction"),
                "centroid_dx_pix": row.get("centroid_dx_pix"),
                "centroid_dy_pix": row.get("centroid_dy_pix"),
                "flags": flags,
            }
        )

    if diagnose_performance:
        timings["apply_measurement_rows"] = time.perf_counter() - _perf_t0
        if rows:
            result.throughput["application_rows_per_sec"] = len(rows) / max(timings["apply_measurement_rows"], 1e-12)
    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    result.output_mag_median = _median(output_mags)
    result.output_mag_mad = _mad(output_mags)
    denominator = result.calibrated_with_reference_color + result.calibrated_with_pivot_color
    result.pivot_color_fraction = (
        float(result.calibrated_with_pivot_color) / float(denominator) if denominator > 0 else None
    )
    result.systematic_error_median = _median(systematic_errors)
    result.systematic_error_max = max(systematic_errors) if systematic_errors else None
    shape = _compare_shape_metrics(source_by_object, output_by_object)
    result.max_abs_rms_delta = shape["max_abs_rms_delta"]
    result.max_abs_mad_delta = shape["max_abs_mad_delta"]
    result.max_abs_peak_to_peak_delta = shape["max_abs_peak_to_peak_delta"]

    if result.n_used_calibrator_objects < int(min_calibrator_objects):
        result.quality_gate_failures.append(
            f"TOO_FEW_CALIBRATORS:{result.n_used_calibrator_objects}<{int(min_calibrator_objects)}"
        )
    if (
        result.selected_transform_model == "offset_plus_color"
        and (result.color_span is None or float(result.color_span) < float(min_color_span))
    ):
        result.quality_gate_failures.append(
            f"COLOR_SPAN_TOO_NARROW:{result.color_span}<{float(min_color_span)}"
        )
    if (
        max_transform_residual_mad is not None
        and (result.transform_residual_mad is None or float(result.transform_residual_mad) > float(max_transform_residual_mad))
    ):
        result.quality_gate_failures.append(
            f"TRANSFORM_RESIDUAL_MAD_TOO_HIGH:{result.transform_residual_mad}>{float(max_transform_residual_mad)}"
        )
    if (
        max_pivot_color_fraction is not None
        and (result.pivot_color_fraction is None or float(result.pivot_color_fraction) > float(max_pivot_color_fraction))
    ):
        result.quality_gate_failures.append(
            f"PIVOT_COLOR_FRACTION_TOO_HIGH:{result.pivot_color_fraction}>{float(max_pivot_color_fraction)}"
        )
    for metric_name, metric_value in (
        ("RMS", result.max_abs_rms_delta),
        ("MAD", result.max_abs_mad_delta),
        ("PEAK_TO_PEAK", result.max_abs_peak_to_peak_delta),
    ):
        if metric_value is None or float(metric_value) > float(max_shape_metric_delta):
            result.quality_gate_failures.append(
                f"SHAPE_NOT_PRESERVED_{metric_name}:{metric_value}>{float(max_shape_metric_delta)}"
            )
    if result.calibrated <= 0:
        result.quality_gate_failures.append("NO_CALIBRATED_MEASUREMENTS")

    result.quality_gate_passed = not result.quality_gate_failures
    if diagnose_performance:
        timings["shape_metrics_and_quality_gates"] = time.perf_counter() - _perf_t0
    if not result.quality_gate_passed:
        result.warnings.append(
            "Color transform failed science quality gates; source ensemble calibration remains active: "
            + "; ".join(result.quality_gate_failures)
        )
        if diagnose_performance:
            timings["function_total"] = time.perf_counter() - _function_t0
        return result

    if dry_run:
        if diagnose_performance:
            timings["function_total"] = time.perf_counter() - _function_t0
        return result

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    target_cv = _get_or_create_color_calibration_version(
        session,
        name=target_calibration_name,
        source_cv=source_cv,
        instrument_unit_id=_infer_instrument_unit_id(session, source_cv.calibration_version_id, series_id),
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        color0=color0,
        color_term=color_term,
        transform_offset=offset,
        transform_offset_err=result.transform_offset_err,
        color_term_err=result.color_term_err,
        transform_offset_color_cov=result.transform_offset_color_cov,
        transform_residual_mad=result.transform_residual_mad,
        transform_residual_rms=result.transform_residual_rms,
        transform_residual_robust_sigma=result.transform_residual_robust_sigma,
        calibrator_color_sigma=diag.calibrator_color_sigma,
        selected_transform_model=result.selected_transform_model,
        model_selection_reason=result.model_selection_reason,
        model_selection_metrics={
            "fitted_color_term": result.fitted_color_term,
            "fitted_color_term_err": result.fitted_color_term_err,
            "color_term_significance": result.color_term_significance,
            "offset_only_residual_mad": result.offset_only_residual_mad,
            "offset_only_residual_rms": result.offset_only_residual_rms,
            "color_model_residual_mad": result.color_model_residual_mad,
            "color_model_residual_rms": result.color_model_residual_rms,
            "offset_only_bic": result.offset_only_bic,
            "color_model_bic": result.color_model_bic,
            "color_model_bic_improvement": result.color_model_bic_improvement,
            "color_model_mad_improvement": result.color_model_mad_improvement,
            "color_model_rms_improvement": result.color_model_rms_improvement,
            "thresholds": result.model_selection_thresholds,
        },
        diagnostics=diag,
        quality_gates={
            "passed": result.quality_gate_passed,
            "failures": result.quality_gate_failures,
            "thresholds": result.quality_gate_thresholds,
            "pivot_color_fraction": result.pivot_color_fraction,
            "max_abs_rms_delta": result.max_abs_rms_delta,
            "max_abs_mad_delta": result.max_abs_mad_delta,
            "max_abs_peak_to_peak_delta": result.max_abs_peak_to_peak_delta,
        },
        filters={
            "min_measurements_per_object": min_measurements_per_object,
            "min_stars_per_frame": min_stars_per_frame,
            "max_measurement_mag_err": max_measurement_mag_err,
            "max_reference_mag_err": max_reference_mag_err,
            "min_reference_mag": min_reference_mag,
            "max_reference_mag": max_reference_mag,
            "min_color": min_color,
            "max_color": max_color,
            "include_flagged_calibrators": include_flagged_calibrators,
            "excluded_object_qc_flags": list(excluded_object_qc_flags or _DEFAULT_EXCLUDED_OBJECT_QC_FLAGS),
            "exclude_object_ids": list(exclude_object_ids or []),
            "clip_sigma": clip_sigma,
            "n_iterations": n_iterations,
            "exclude_saturated": exclude_saturated,
            "missing_color_policy": "use_fitted_color0_with_systematic_uncertainty",
        },
        immutable_generation=immutable_generation,
    )
    session.flush()
    result.target_calibration_version_id = target_cv.calibration_version_id
    if diagnose_performance:
        timings["target_calibration_prepare"] = time.perf_counter() - _perf_t0
    if materialize_measurements:
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        result.deleted_existing = _delete_existing_target_measurements(
            session,
            target_calibration_version_id=target_cv.calibration_version_id,
            series_id=series_id,
        )
        for row in prepared_rows:
            row["calibration_version_id"] = target_cv.calibration_version_id
        result.inserted_or_updated = _upsert_measurement_rows(session, prepared_rows)
        if diagnose_performance:
            timings["materialized_measurement_write"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    candidate_ids = set(int(v) for v in result.calibrator_candidate_object_ids)
    used_ids = set(int(v) for v in result.calibrator_used_object_ids)
    clipped_ids = set(int(v) for v in result.calibrator_clipped_object_ids)
    explicit_excluded_ids = set(int(v) for v in (exclude_object_ids or []))

    # Color application state is object-level, not epoch-level.  Persist one
    # compact row for every object in the science product so the final
    # magnitudes and correlated systematic uncertainty can be reconstructed
    # without querying mutable reference-photometry rows or legacy flags.
    application_by_object: dict[int, dict[str, Any]] = {}
    for prepared in prepared_rows:
        object_id = int(prepared["object_id"])
        flags = prepared.get("flags") if isinstance(prepared.get("flags"), dict) else {}
        candidate = {
            "reference_mag": flags.get("reference_mag"),
            "reference_color_value": flags.get("reference_color_value"),
            "applied_color_value": flags.get("reference_color_applied_value"),
            "color_missing": bool(flags.get("reference_color_missing")),
            "color_error": flags.get("reference_color_error"),
            "systematic_error": prepared.get("mag_err_systematic"),
            "reference_catalog_source_id": flags.get("reference_catalog_source_id"),
            "color_outside_fit_range": bool(flags.get("reference_color_outside_fit_range")),
            "application_basis": "per_object_reference_color_or_fitted_pivot",
        }
        existing = application_by_object.get(object_id)
        if existing is None:
            application_by_object[object_id] = candidate
        elif existing != candidate:
            raise RuntimeError(
                f"Color application state is not object-constant for object_id={object_id}"
            )

    normalized_objects: list[ObjectTermInput] = []
    all_color_object_ids = set(application_by_object) | candidate_ids | explicit_excluded_ids
    for object_id in sorted(all_color_object_ids):
        if object_id in used_ids:
            role = "COLOR_CALIBRATOR"
            status = "USED"
        elif object_id in clipped_ids:
            role = "COLOR_CALIBRATOR_CLIPPED"
            status = "CLIPPED"
        elif object_id in explicit_excluded_ids:
            role = "REQUESTED_EXCLUSION"
            status = "EXCLUDED"
        elif object_id in candidate_ids:
            role = "COLOR_CALIBRATOR_CANDIDATE"
            status = "NOT_USED"
        else:
            role = "COLOR_APPLICATION"
            status = "APPLIED"
        details = dict(application_by_object.get(object_id) or {})
        details["membership_basis"] = "ensemble_reference_transform"
        normalized_objects.append(
            ObjectTermInput(
                object_id=object_id,
                role=role,
                is_reference=object_id in used_ids,
                requested_exclusion=object_id in explicit_excluded_ids,
                effective_exclusion=object_id in explicit_excluded_ids or object_id in clipped_ids,
                status=status,
                details=details,
            )
        )

    if diagnose_performance:
        timings["build_normalized_object_terms"] = time.perf_counter() - _perf_t0
    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    normalized = replace_calibration_iteration(
        session,
        calibration_version_id=int(target_cv.calibration_version_id),
        source_calibration_version_id=int(source_cv.calibration_version_id),
        iteration_kind="COLOR_CALIBRATION",
        iteration_no=1,
        algorithm="ensemble_preserving_reference_transform_v3",
        status="COMPLETE",
        convergence_status="QUALITY_GATE_PASSED",
        parameters={
            "identity_catalog_name": identity_catalog_name,
            "reference_catalog_name": reference_catalog_name,
            "reference_band": ref_band,
            "color_index": color_key,
            "clip_sigma": clip_sigma,
            "n_iterations": n_iterations,
            "selected_transform_model": result.selected_transform_model,
            "model_selection_reason": result.model_selection_reason,
            "model_selection_thresholds": result.model_selection_thresholds,
            "quality_gate_thresholds": result.quality_gate_thresholds,
            "application_semantics": "mag_out = mag_source + transform_offset + color_term*(color-color0)",
            "missing_color_policy": "use_color0_with_systematic_uncertainty",
        },
        metrics={
            "color0": result.color0,
            "color_min": result.color_min,
            "color_max": result.color_max,
            "color_term": result.color_term,
            "color_term_err": result.color_term_err,
            "fitted_color_term": result.fitted_color_term,
            "fitted_color_term_err": result.fitted_color_term_err,
            "transform_offset": result.transform_offset,
            "transform_offset_err": result.transform_offset_err,
            "transform_offset_color_cov": result.transform_offset_color_cov,
            "transform_residual_mad": result.transform_residual_mad,
            "transform_residual_rms": result.transform_residual_rms,
            "transform_residual_robust_sigma": result.transform_residual_robust_sigma,
            "calibrator_color_sigma": result.calibrator_color_sigma,
            "n_used_calibrator_objects": result.n_used_calibrator_objects,
            "n_clipped_calibrator_objects": result.n_clipped_calibrator_objects,
            "pivot_color_fraction": result.pivot_color_fraction,
            "quality_gate_passed": result.quality_gate_passed,
        },
        frame_terms=[
            FrameTermInput(
                frame_id=int(frame_id),
                term_kind="COLOR_DIAGNOSTIC_ZP",
                model_name="diagnostic_frame_color_model",
                offset_mag=frame_zp,
                accepted=True,
                quality_status="DIAGNOSTIC_ONLY",
                details={"applied_to_science_product": False},
            )
            for frame_id, frame_zp in sorted(
                ((int(k), float(v)) for k, v in diag.frame_zp_by_frame.items()),
                key=lambda item: item[0],
            )
        ],
        object_terms=normalized_objects,
    )
    result.normalized_calibration_iteration_id = normalized.calibration_iteration_id
    result.normalized_frame_terms = normalized.frame_terms
    result.normalized_object_terms = normalized.object_terms
    result.normalized_write_status = (
        "DUAL_WRITTEN" if materialize_measurements else "NORMALIZED_ONLY"
    )
    if diagnose_performance:
        timings["normalized_iteration_write"] = time.perf_counter() - _perf_t0
    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    session.commit()
    if diagnose_performance:
        timings["commit"] = time.perf_counter() - _perf_t0
        timings["function_total"] = time.perf_counter() - _function_t0
        result.throughput["checked_measurements_per_sec_total"] = result.checked_measurements / max(timings["function_total"], 1e-12)
    return result


def _build_reference_color_measurement_flags(
    *,
    source_flags: dict[str, Any],
    source_measurement_id: Any,
    source_calibration_version_id: int,
    source_calibration_name: str,
    identity_catalog_name: str,
    source_id: Any,
    reference_catalog_name: str,
    reference_catalog_source_id: Any,
    reference_band: str,
    reference_mag: Any,
    color_index: str,
    color_value: float | None,
    color0: float,
    color_term: float,
    fitted_color_term: float | None = None,
    fitted_color_term_error: float | None = None,
    transform_model: str | None = None,
    model_selection_reason: str | None = None,
    correction: float,
    color_outside_fit_range: bool,
    transform_offset: float = 0.0,
    applied_color_value: float | None = None,
    color_missing: bool = False,
    calibration_applied: bool = True,
    calibration_skip_reason: str | None = None,
    random_error: float | None = None,
    systematic_error: float | None = None,
    transform_offset_error: float | None = None,
    color_term_error: float | None = None,
    color_error: float | None = None,
    model_systematic_floor: float | None = None,
    # Deprecated compatibility input from v1 callers/tests.
    frame_zp: float | None = None,
) -> dict[str, Any]:
    """Build provenance for an ensemble-preserving absolute color transform."""

    src = source_flags if isinstance(source_flags, dict) else {}
    source_id_text = str(source_id) if source_id is not None else src.get("source_id")
    catalog_name = str(identity_catalog_name or src.get("catalog_name") or "")
    applied_value = color_value if applied_color_value is None else applied_color_value

    flags: dict[str, Any] = {
        "catalog_name": catalog_name,
        "source_id": source_id_text,
        "calibration_stage": "reference_color_calibration",
        "calibration_method": "ensemble_preserving_reference_color_transform",
        "color_calibration_algorithm": "ensemble_preserving_reference_transform_v3",
        "reference_transform_model": transform_model,
        "reference_transform_model_selection_reason": model_selection_reason,
        "source_measurement_id": int(source_measurement_id) if source_measurement_id is not None else None,
        "source_calibration_version_id": int(source_calibration_version_id),
        "source_calibration_name": source_calibration_name,
        "reference_catalog_name": reference_catalog_name,
        "reference_catalog_source_id": str(reference_catalog_source_id) if reference_catalog_source_id is not None else None,
        "reference_band": reference_band,
        "reference_mag": _json_number_or_none(reference_mag),
        "reference_color_index": color_index,
        "reference_color_value": _json_number_or_none(color_value),
        "reference_color_applied_value": _json_number_or_none(applied_value),
        "reference_color_missing": bool(color_missing),
        "reference_color_assumed_pivot": bool(color_missing),
        "reference_color0": _json_number_or_none(color0),
        "reference_color_term": _json_number_or_none(color_term),
        "reference_fitted_color_term": _json_number_or_none(fitted_color_term),
        "reference_fitted_color_term_error": _json_number_or_none(fitted_color_term_error),
        "reference_transform_offset": _json_number_or_none(transform_offset),
        "reference_frame_zp": _json_number_or_none(frame_zp),
        "reference_color_correction": _json_number_or_none(correction),
        "reference_color_outside_fit_range": bool(color_outside_fit_range),
        "reference_color_calibration_applied": bool(calibration_applied),
        "reference_color_calibration_skip_reason": calibration_skip_reason,
        "mag_err_random": _json_number_or_none(random_error),
        "mag_err_systematic": _json_number_or_none(systematic_error),
        "reference_transform_offset_error": _json_number_or_none(transform_offset_error),
        "reference_color_term_error": _json_number_or_none(color_term_error),
        "reference_color_error": _json_number_or_none(color_error),
        "reference_transform_systematic_floor": _json_number_or_none(model_systematic_floor),
        "calibration_warning": (
            f"reference-color calibration not applied: {calibration_skip_reason}"
            if not calibration_applied
            else (
                "reference color unavailable; fitted pivot color and enlarged systematic uncertainty were used"
                if color_missing
                else None
            )
        ),
    }

    for key in (
        "bad_xy", "outside_image", "aperture_touches_x_edge", "aperture_touches_y_edge",
        "annulus_touches_x_edge", "annulus_touches_y_edge", "empty_aperture",
        "too_few_annulus_pixels", "saturated_or_near_saturated", "aperture_max_adu",
        "non_positive_flux", "saturated", "saturation_warning", "edge", "edge_warning",
        "photometry_warning", "aperture_warning", "centroid_warning",
    ):
        if key in src:
            flags[key] = src[key]

    # Aperture-correction provenance is measurement-specific upstream state, not
    # stale reference/color-calibration metadata. Preserve the complete namespace
    # so science-level products can audit both the applied correction and the
    # distinction between total diagnostic and differential random uncertainty.
    for key, value in src.items():
        if str(key).startswith("aperture_correction_"):
            flags[key] = value
        if str(key).startswith("random_error_"):
            flags[key] = value
    return flags


def _json_number_or_none(value: Any) -> float | None:
    value_float = _to_float(value)
    if not _is_finite(value_float):
        return None
    return float(value_float)



def _first_finite(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if _is_finite(parsed):
            return float(parsed)
    return None


def _reference_color_error(row: dict[str, Any], *, color_bands: tuple[str, str] | None) -> float | None:
    if color_bands is None:
        return None
    ea = _reference_mag_error(row, color_bands[0])
    eb = _reference_mag_error(row, color_bands[1])
    if not _is_finite(ea) or not _is_finite(eb):
        return None
    return math.sqrt(float(ea) ** 2 + float(eb) ** 2)


def _color_systematic_error(
    *,
    x_color: float,
    color_term: float,
    color_error: float | None,
    offset_error: float | None,
    color_term_error: float | None,
    offset_color_cov: float | None,
    model_floor: float,
) -> float | None:
    variance = max(0.0, float(model_floor) ** 2)
    if _is_finite(offset_error):
        variance += float(offset_error) ** 2
    if _is_finite(color_term_error):
        variance += float(x_color) ** 2 * float(color_term_error) ** 2
    if _is_finite(offset_color_cov):
        variance += 2.0 * float(x_color) * float(offset_color_cov)
    if _is_finite(color_error):
        variance += float(color_term) ** 2 * float(color_error) ** 2
    return math.sqrt(max(0.0, variance))


def _compare_shape_metrics(
    source_by_object: dict[int, list[float]],
    output_by_object: dict[int, list[float]],
) -> dict[str, float | None]:
    rms_deltas: list[float] = []
    mad_deltas: list[float] = []
    ptp_deltas: list[float] = []
    for object_id, source_values in source_by_object.items():
        output_values = output_by_object.get(object_id) or []
        if len(source_values) != len(output_values) or len(source_values) < 2:
            continue
        source_rms = _sample_rms(source_values)
        output_rms = _sample_rms(output_values)
        source_mad = _mad(source_values)
        output_mad = _mad(output_values)
        source_ptp = _robust_peak_to_peak(source_values)
        output_ptp = _robust_peak_to_peak(output_values)
        if _is_finite(source_rms) and _is_finite(output_rms):
            rms_deltas.append(abs(float(output_rms) - float(source_rms)))
        if _is_finite(source_mad) and _is_finite(output_mad):
            mad_deltas.append(abs(float(output_mad) - float(source_mad)))
        if _is_finite(source_ptp) and _is_finite(output_ptp):
            ptp_deltas.append(abs(float(output_ptp) - float(source_ptp)))
    return {
        "max_abs_rms_delta": max(rms_deltas) if rms_deltas else None,
        "max_abs_mad_delta": max(mad_deltas) if mad_deltas else None,
        "max_abs_peak_to_peak_delta": max(ptp_deltas) if ptp_deltas else None,
    }

def normalize_reference_band(value: str | None) -> str:
    if value is None:
        raise ValueError("reference_band is required")
    band = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"ps1_g": "g", "ps1_r": "r", "ps1_i": "i", "ps1_z": "z", "ps1_y": "y", "gaia_g": "g_gaia"}
    band = aliases.get(band, band)
    if band not in _SUPPORTED_BANDS:
        raise ValueError(f"Unsupported reference band: {value!r}")
    return band


def parse_color_index(value: str | None) -> tuple[str, tuple[str, str] | None]:
    if value is None:
        raise ValueError("color_index is required")
    text_value = str(value).strip().lower().replace(" ", "")
    if not text_value:
        raise ValueError("color_index is empty")
    if "-" in text_value:
        a, b = text_value.split("-", 1)
    elif "_" in text_value:
        a, b = text_value.split("_", 1)
    else:
        key = text_value
        aliases = {"bp_rp": "bp_rp", "bprp": "bp_rp", "g_r": "g_r", "gr": "g_r", "r_i": "r_i", "ri": "r_i", "i_z": "i_z", "iz": "i_z"}
        return aliases.get(key, key), None
    a = normalize_reference_band(a)
    b = normalize_reference_band(b)
    return f"{a}_{b}", (a, b)



def _get_or_create_color_calibration_version(
    session: Session,
    *,
    name: str,
    source_cv: CalibrationVersion,
    instrument_unit_id: int | None,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    color0: float,
    color_term: float,
    transform_offset: float,
    transform_offset_err: float | None,
    color_term_err: float | None,
    transform_offset_color_cov: float | None,
    transform_residual_mad: float | None,
    transform_residual_rms: float | None,
    transform_residual_robust_sigma: float | None,
    calibrator_color_sigma: float | None,
    selected_transform_model: str | None,
    model_selection_reason: str | None,
    model_selection_metrics: dict[str, Any],
    diagnostics: ColorCalibrationDiagnosticsResult,
    quality_gates: dict[str, Any],
    filters: dict[str, Any],
    immutable_generation: bool = False,
) -> CalibrationVersion:
    params = {
        "source_calibration_name": source_cv.name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "algorithm": "ensemble_preserving_reference_transform_v3",
        "formula": "mag_cal = source_mag_cal + transform_offset + color_term * (color - color0)",
        "shape_preserving": True,
        "selected_transform_model": selected_transform_model,
        "model_selection_reason": model_selection_reason,
        "model_selection": model_selection_metrics,
        "identity_catalog_name": identity_catalog_name,
        "reference_catalog_name": reference_catalog_name,
        "reference_band": reference_band,
        "color_index": color_index,
        "color0": color0,
        "transform_offset": transform_offset,
        "transform_offset_err": transform_offset_err,
        "color_term": color_term,
        "color_term_err": color_term_err,
        "transform_offset_color_cov": transform_offset_color_cov,
        "transform_residual_mad": transform_residual_mad,
        "transform_residual_rms": transform_residual_rms,
        "transform_residual_robust_sigma": transform_residual_robust_sigma,
        "calibrator_color_sigma": calibrator_color_sigma,
        "error_semantics": {
            "mag_err": "backward-compatible random per-epoch uncertainty",
            "mag_err_random": "random per-epoch uncertainty",
            "mag_err_systematic": "object-level absolute transform uncertainty; correlated across epochs",
        },
        "diagnostics": {
            "n_used_objects": diagnostics.ensemble_transform_used_objects,
            "n_clipped_objects": diagnostics.ensemble_transform_clipped_objects,
            "n_used_measurements": diagnostics.ensemble_transform_used_measurements,
            "secondary_frame_model_color_term": diagnostics.frame_model_color_term,
            "secondary_frame_model_residual_rms": diagnostics.frame_model_residual_rms,
            "secondary_frame_model_residual_mad": diagnostics.frame_model_residual_mad,
            "summary_json": diagnostics.summary_json,
            "stars_csv": diagnostics.stars_csv,
            "frames_csv": diagnostics.frames_csv,
        },
        "quality_gates": quality_gates,
        "filters": filters,
    }
    return prepare_calibration_version_for_write(
        session,
        logical_name=name,
        model_name="ensemble_preserving_reference_transform_v3",
        instrument_unit_id=instrument_unit_id,
        used_catalogs={
            "identity_catalog_name": identity_catalog_name,
            "reference_catalog_name": reference_catalog_name,
            "reference_band": reference_band,
            "color_index": color_index,
        },
        parameters=params,
        notes="Absolute reference-band transform selected between offset-only and offset+color models; ensemble light-curve shape is preserved.",
        source_cv=source_cv,
        immutable_generation=immutable_generation,
    )


def _infer_instrument_unit_id(session: Session, source_calibration_version_id: int | None, series_id: int | None) -> int | None:
    if source_calibration_version_id is None:
        return None
    cv = session.get(CalibrationVersion, int(source_calibration_version_id))
    if cv is not None and cv.instrument_unit_id is not None:
        return int(cv.instrument_unit_id)
    frame_scope = None
    if series_id is not None:
        frame_scope = list(
            session.scalars(
                select(FieldSeriesFrame.frame_id).where(
                    FieldSeriesFrame.series_id == int(series_id)
                )
            ).all()
        )
    view = load_calibration_photometry_view(
        session,
        calibration_version_id=int(source_calibration_version_id),
        frame_ids=frame_scope,
        prefer_science=False,
    )
    frame_ids = sorted({int(row.frame_id) for row in view.rows})
    if not frame_ids:
        return None
    values = sorted(
        {
            int(value)
            for value in session.scalars(
                select(Frame.instrument_unit_id).where(
                    Frame.frame_id.in_(frame_ids),
                    Frame.instrument_unit_id.is_not(None),
                )
            ).all()
            if value is not None
        }
    )
    return values[0] if len(values) == 1 else None



def _delete_existing_target_measurements(
    session: Session,
    *,
    target_calibration_version_id: int,
    series_id: int | None,
) -> int:
    if series_id is None:
        sql = "DELETE FROM measurements WHERE calibration_version_id = :cv_id"
        result = session.execute(text(sql), {"cv_id": int(target_calibration_version_id)})
        return int(result.rowcount or 0)
    sql = """
        DELETE FROM measurements m
        USING field_series_frames fsf
        WHERE m.calibration_version_id = :cv_id
          AND fsf.series_id = :series_id
          AND fsf.frame_id = m.frame_id
    """
    result = session.execute(text(sql), {"cv_id": int(target_calibration_version_id), "series_id": int(series_id)})
    return int(result.rowcount or 0)


def _upsert_measurement_rows(session: Session, rows: list[dict[str, Any]], *, batch_size: int = 500) -> int:
    if not rows:
        return 0
    table = Measurement.__table__
    total = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        stmt_ins = insert(table).values(chunk)
        stmt_ins = stmt_ins.on_conflict_do_update(
            constraint="uq_measurement_version",
            set_={
                "x": stmt_ins.excluded.x,
                "y": stmt_ins.excluded.y,
                "flux": stmt_ins.excluded.flux,
                "flux_err": stmt_ins.excluded.flux_err,
                "mag_inst": stmt_ins.excluded.mag_inst,
                "mag_cal": stmt_ins.excluded.mag_cal,
                "mag_err": stmt_ins.excluded.mag_err,
                "mag_err_random": stmt_ins.excluded.mag_err_random,
                "mag_err_systematic": stmt_ins.excluded.mag_err_systematic,
                "aperture_radius_pix": stmt_ins.excluded.aperture_radius_pix,
                "local_background": stmt_ins.excluded.local_background,
                "local_zp_correction": stmt_ins.excluded.local_zp_correction,
                "centroid_dx_pix": stmt_ins.excluded.centroid_dx_pix,
                "centroid_dy_pix": stmt_ins.excluded.centroid_dy_pix,
                "flags": stmt_ins.excluded.flags,
            },
        )
        try:
            session.execute(stmt_ins)
        except Exception as exc:
            raise RuntimeError(f"Failed to upsert {len(chunk)} color-calibrated measurements at batch offset {start}: {exc.__class__.__name__}") from exc
        total += len(chunk)
    return total


def _select_color_measurement_rows(
    rows: Iterable[dict[str, Any]],
    *,
    require_reference: bool,
    require_mag_inst: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Derive one legacy-compatible color-calibration row view in memory.

    The shared loader returns the ordered superset used by application.  Filtering
    that superset reproduces the old SQL/view semantics for diagnostics, including
    applying ``limit`` *after* reference/mag-inst eligibility.
    """

    normalized_limit = None if limit is None else int(limit)
    if normalized_limit is not None and normalized_limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    for row in rows:
        if require_mag_inst and row.get("mag_inst") is None:
            continue
        if require_reference and (
            row.get("reference_catalog_source_id") is None
            or row.get("reference_mag") is None
        ):
            continue
        selected.append(row)
        if normalized_limit is not None and len(selected) >= normalized_limit:
            break
    return selected



def _session_dialect_name(session: Session) -> str | None:
    try:
        bind = session.get_bind()
    except Exception:
        return None
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return str(name).lower() if name else None


def _build_color_server_core_sql(
    reconstruction_select_sql: str,
    *,
    require_mag_inst: bool,
) -> str:
    """Return the narrow PostgreSQL half of the normalized color reader.

    Reference photometry and ObjectQc are deliberately *not* joined here.  The
    production MASTER profile showed that decorating every reconstructed epoch
    inside the same server query made the supposedly faster reader slower than
    the Python view.  Reconstruction is kept server-side, while the small
    object/reference tables are loaded once and joined in Python by object/source
    id.  Only BASE flag keys that affect color-calibration semantics are moved
    across the DB boundary.
    """

    from grandgousier.calibration.reconstruction import _RECONSTRUCTED_FIELDS

    alias_columns = [
        "reconstruction_calibration_version_id",
        "reconstruction_source_measurement_id",
        "base_measurement_id",
        "object_id",
        "frame_id",
        *_RECONSTRUCTED_FIELDS,
        "reconstruction_flags",
        "reconstruction_materialization_source",
    ]
    alias_sql = ", ".join(alias_columns)
    where = ["(bm.flags ->> 'catalog_name') = :identity_catalog_name"]
    if require_mag_inst:
        where.append("r.mag_inst IS NOT NULL")

    return f"""
        SELECT
            r.base_measurement_id AS measurement_id,
            r.object_id,
            r.frame_id,
            r.mag_inst,
            r.mag_cal,
            r.mag_err,
            r.mag_err_random,
            jsonb_strip_nulls(jsonb_build_object(
                'catalog_name', bm.flags -> 'catalog_name',
                'source_id', bm.flags -> 'source_id',
                'saturated', bm.flags -> 'saturated',
                'near_saturated', bm.flags -> 'near_saturated',
                'saturated_or_near_saturated', bm.flags -> 'saturated_or_near_saturated'
            )) AS measurement_flags,
            fr.mjd,
            fr.date_obs,
            fr.instrument_unit_id,
            fr.filter_name,
            (bm.flags ->> 'catalog_name') AS measurement_catalog_name,
            (bm.flags ->> 'source_id') AS source_id
        FROM (
            {reconstruction_select_sql}
        ) AS r ({alias_sql})
        JOIN measurements bm
          ON bm.measurement_id = r.base_measurement_id
        JOIN frames fr
          ON fr.frame_id = r.frame_id
        WHERE {' AND '.join(where)}
    """.strip()



def _fuse_compact_color_ensemble_rows(
    rows: Iterable[dict[str, Any]],
    *,
    aperture_terms: dict[int, CalibrationFrameTerm],
    aperture_zp_terms: dict[int, CalibrationFrameTerm],
    ensemble_terms: dict[int, CalibrationFrameTerm],
    aperture_zp_catalog_name: str | None,
    ensemble_catalog_name: str | None,
    ensemble_prefer_mag_cal: bool,
) -> list[dict[str, Any]]:
    """Reconstruct the scalar ENSEMBLE state needed by normalized color calibration.

    The output intentionally carries only color-stage inputs.  Full upstream
    provenance flags are not required when the color product itself is stored as
    NORMALIZED_TERMS; science materialization reconstructs that provenance from
    the authoritative normalized chain later.
    """

    from grandgousier.calibration.reconstruction import (
        _aperture_differential_error,
        _aperture_prediction,
    )

    output: list[dict[str, Any]] = []
    for base in rows:
        object_id = int(base["object_id"])
        frame_id = int(base["frame_id"])
        catalog_name = base.get("measurement_catalog_name")

        mag_inst = _to_float(base.get("mag_inst"))
        random_err = _first_finite(base.get("mag_err_random"), base.get("mag_err"))

        aperture_term = aperture_terms.get(frame_id)
        if aperture_term is not None and bool(aperture_term.accepted) and _is_finite(mag_inst):
            correction = float(_aperture_prediction(aperture_term, base.get("x"), base.get("y")))
            corrected = float(mag_inst) + correction
            mag_inst = corrected if math.isfinite(corrected) else None
            differential_error = float(
                _aperture_differential_error(aperture_term, base.get("x"), base.get("y"))
            )
            if random_err is not None:
                random_err = math.hypot(float(random_err), differential_error)
            elif differential_error > 0:
                random_err = differential_error

        if aperture_zp_catalog_name and str(catalog_name) != str(aperture_zp_catalog_name):
            continue
        zp_term = aperture_zp_terms.get(frame_id)
        if zp_term is None or not bool(zp_term.accepted) or not _is_finite(zp_term.offset_mag):
            continue
        if not _is_finite(mag_inst):
            continue
        mag_cal = float(mag_inst) + float(zp_term.offset_mag)

        if ensemble_catalog_name and str(catalog_name) != str(ensemble_catalog_name):
            continue
        ensemble_term = ensemble_terms.get(frame_id)
        if (
            ensemble_term is None
            or not bool(ensemble_term.accepted)
            or not _is_finite(ensemble_term.offset_mag)
        ):
            continue
        value: float | None = None
        if ensemble_prefer_mag_cal and _is_finite(mag_cal):
            value = float(mag_cal)
        elif _is_finite(mag_inst):
            value = float(mag_inst)
        elif _is_finite(mag_cal):
            value = float(mag_cal)
        if value is None:
            continue
        mag_cal = value - float(ensemble_term.offset_mag)
        if not math.isfinite(mag_cal):
            continue

        measurement_flags: dict[str, Any] = {}
        if catalog_name is not None:
            measurement_flags["catalog_name"] = catalog_name
        for key in (
            "source_id",
            "saturated",
            "near_saturated",
            "saturated_or_near_saturated",
        ):
            value0 = base.get(key)
            if value0 is not None:
                measurement_flags[key] = value0

        output.append(
            {
                "measurement_id": int(base["measurement_id"]),
                "object_id": object_id,
                "frame_id": frame_id,
                "mag_inst": float(mag_inst),
                "mag_cal": float(mag_cal),
                "mag_err": random_err,
                "mag_err_random": random_err,
                "measurement_flags": measurement_flags,
                "mjd": base.get("mjd"),
                "date_obs": base.get("date_obs"),
                "instrument_unit_id": base.get("instrument_unit_id"),
                "filter_name": base.get("filter_name"),
                "measurement_catalog_name": catalog_name,
                "source_id": base.get("source_id"),
            }
        )
    return output


def _load_color_measurement_rows_from_compact_reconstruction(
    session: Session,
    *,
    calibration_version: CalibrationVersion,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
    require_reference: bool,
    require_mag_inst: bool,
    _performance_timings: dict[str, float] | None = None,
) -> list[dict[str, Any]] | None:
    """Compact PostgreSQL + fused-Python reader for normalized ENSEMBLE color input.

    This is deliberately narrower than the generic server reconstruction: only
    BASE scalars required to reconstruct APERTURE_CORRECTION -> APERTURE_ZP ->
    ENSEMBLE are read, then low-cardinality reference/QC decoration is reused.
    Unsupported chains return ``None`` and retain the V3/server/view fallbacks.
    """

    if _session_dialect_name(session) != "postgresql":
        return None
    if str(calibration_version.product_kind or "").upper() != "ENSEMBLE":
        return None
    if str(calibration_version.materialization_kind or "").upper() != "NORMALIZED_TERMS":
        return None
    if limit is not None and int(limit) <= 0:
        return []

    from grandgousier.calibration.reconstruction import _chain_from_base, _latest_iteration

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    chain = _chain_from_base(session, calibration_version)
    kinds = [str(cv.product_kind or "").upper() for cv in chain]
    if kinds != ["BASE_PHOTOMETRY", "APERTURE_CORRECTION", "APERTURE_ZP", "ENSEMBLE"]:
        return None
    base_cv, aperture_cv, aperture_zp_cv, ensemble_cv = chain
    if str(base_cv.materialization_kind or "").upper() != "FULL_MEASUREMENTS":
        return None
    aperture_iteration = _latest_iteration(session, int(aperture_cv.calibration_version_id))
    aperture_zp_iteration = _latest_iteration(session, int(aperture_zp_cv.calibration_version_id))
    ensemble_iteration = _latest_iteration(session, int(ensemble_cv.calibration_version_id))
    if aperture_iteration is None or aperture_zp_iteration is None or ensemble_iteration is None:
        return None
    if _performance_timings is not None:
        _performance_timings["color_compact_plan"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    aperture_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(aperture_iteration.calibration_iteration_id)
            )
        ).all()
    }
    aperture_zp_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(aperture_zp_iteration.calibration_iteration_id)
            )
        ).all()
        if bool(term.accepted) and _is_finite(term.offset_mag)
    }
    ensemble_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(ensemble_iteration.calibration_iteration_id)
            )
        ).all()
        if bool(term.accepted) and _is_finite(term.offset_mag)
    }
    if _performance_timings is not None:
        _performance_timings["color_compact_term_load"] = time.perf_counter() - _perf_t0

    frame_scope: list[int] | None = None
    if series_id is not None:
        frame_scope = [
            int(value)
            for value in session.scalars(
                select(FieldSeriesFrame.frame_id).where(
                    FieldSeriesFrame.series_id == int(series_id)
                )
            ).all()
        ]
        if not frame_scope:
            return []

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    stmt = (
        select(
            Measurement.measurement_id.label("measurement_id"),
            Measurement.object_id.label("object_id"),
            Measurement.frame_id.label("frame_id"),
            Measurement.x.label("x"),
            Measurement.y.label("y"),
            Measurement.mag_inst.label("mag_inst"),
            Measurement.mag_err.label("mag_err"),
            Measurement.mag_err_random.label("mag_err_random"),
            Measurement.flags["catalog_name"].as_string().label("measurement_catalog_name"),
            Measurement.flags["source_id"].as_string().label("source_id"),
            Measurement.flags["saturated"].as_boolean().label("saturated"),
            Measurement.flags["near_saturated"].as_boolean().label("near_saturated"),
            Measurement.flags["saturated_or_near_saturated"].as_boolean().label(
                "saturated_or_near_saturated"
            ),
            Frame.mjd.label("mjd"),
            Frame.date_obs.label("date_obs"),
            Frame.instrument_unit_id.label("instrument_unit_id"),
            Frame.filter_name.label("filter_name"),
        )
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .where(
            Measurement.calibration_version_id == int(base_cv.calibration_version_id),
            Measurement.flags["catalog_name"].as_string() == str(identity_catalog_name),
        )
    )
    if frame_scope:
        stmt = stmt.where(Measurement.frame_id.in_(frame_scope))
    raw_rows = session.execute(
        stmt.order_by(Measurement.object_id, Frame.mjd, Frame.date_obs, Measurement.frame_id)
    ).mappings().all()
    if _performance_timings is not None:
        _performance_timings["color_compact_base_query"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    rows = _fuse_compact_color_ensemble_rows(
        raw_rows,
        aperture_terms=aperture_terms,
        aperture_zp_terms=aperture_zp_terms,
        ensemble_terms=ensemble_terms,
        aperture_zp_catalog_name=dict(aperture_zp_iteration.parameters or {}).get("catalog_name"),
        ensemble_catalog_name=dict(ensemble_iteration.parameters or {}).get("catalog_name"),
        ensemble_prefer_mag_cal=bool(
            dict(ensemble_iteration.parameters or {}).get("prefer_mag_cal", True)
        ),
    )
    if require_mag_inst:
        rows = [row for row in rows if row.get("mag_inst") is not None]
    if _performance_timings is not None:
        _performance_timings["color_compact_fused_reconstruction"] = (
            time.perf_counter() - _perf_t0
        )

    return _decorate_color_server_core_rows(
        session,
        rows,
        calibration_version=calibration_version,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=reference_band,
        require_reference=require_reference,
        limit=limit,
        _performance_timings=_performance_timings,
    )


def _best_color_object_qc(
    candidates: list[ObjectQc],
    *,
    calibration_version_id: int,
    instrument_unit_id: int | None,
) -> ObjectQc | None:
    eligible = [
        qc
        for qc in candidates
        if qc.instrument_unit_id is None
        or instrument_unit_id is None
        or int(qc.instrument_unit_id) == int(instrument_unit_id)
    ]
    if not eligible:
        return None

    def rank(qc: ObjectQc) -> tuple[int, int, float, int]:
        exact_calibration = int(
            qc.calibration_version_id is not None
            and int(qc.calibration_version_id) == int(calibration_version_id)
        )
        exact_instrument = int(
            qc.instrument_unit_id is not None
            and instrument_unit_id is not None
            and int(qc.instrument_unit_id) == int(instrument_unit_id)
        )
        updated = qc.updated_at.timestamp() if qc.updated_at is not None else float("-inf")
        return exact_calibration, exact_instrument, updated, int(qc.object_qc_id)

    return max(eligible, key=rank)


def _decorate_color_server_core_rows(
    session: Session,
    rows: list[dict[str, Any]],
    *,
    calibration_version: CalibrationVersion,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    require_reference: bool,
    limit: int | None,
    _performance_timings: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Bulk-decorate narrow reconstructed rows with reference and ObjectQc data."""

    normalized_limit = None if limit is None else int(limit)
    if normalized_limit is not None and normalized_limit <= 0:
        return []
    if not rows:
        return []

    object_ids = sorted({int(row["object_id"]) for row in rows})
    source_ids = sorted(
        {
            str(row["source_id"])
            for row in rows
            if row.get("source_id") is not None
        }
    )

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    ref_by_source = load_reference_photometry_by_identity(
        session,
        reference_catalog_name=reference_catalog_name,
        identity_catalog_name=identity_catalog_name,
        source_ids=source_ids,
    )
    if _performance_timings is not None:
        _performance_timings["color_reference_load"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    qcs_by_object: dict[int, list[ObjectQc]] = {}
    if object_ids:
        qcs = session.scalars(
            select(ObjectQc).where(
                ObjectQc.object_id.in_(object_ids),
                (ObjectQc.calibration_version_id == int(calibration_version.calibration_version_id))
                | ObjectQc.calibration_version_id.is_(None),
                (ObjectQc.catalog_name == identity_catalog_name) | ObjectQc.catalog_name.is_(None),
            )
        ).all()
        for qc in qcs:
            qcs_by_object.setdefault(int(qc.object_id), []).append(qc)
    if _performance_timings is not None:
        _performance_timings["color_object_qc_load"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    mag_col = _REF_MAG_COLUMNS[reference_band]
    ref_payload_by_source: dict[str, dict[str, Any]] = {}
    for source_id, ref in ref_by_source.items():
        ref_payload_by_source[source_id] = {
            "reference_catalog_source_id": ref.get("catalog_source_id"),
            "distance_to_gaia_arcsec": ref.get("distance_to_gaia_arcsec"),
            "reference_mag": ref.get(mag_col),
            **{
                f"mag_{band}": ref.get(f"mag_{band}")
                for band in ("g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia")
            },
            # These mappings are immutable inputs during color fitting/application.
            # Reusing them avoids rebuilding the same small dict for every epoch.
            "reference_mag_errors": dict(ref.get("reference_mag_errors") or {}),
            "reference_colors": dict(ref.get("colors") or {}),
            "reference_quality_flags": dict(ref.get("quality_flags") or {}),
        }

    qc_payload_cache: dict[tuple[int, int | None], dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        source_id = str(row["source_id"]) if row.get("source_id") is not None else None
        ref_payload = ref_payload_by_source.get(source_id or "")
        reference_mag = ref_payload.get("reference_mag") if ref_payload is not None else None
        if require_reference and (ref_payload is None or reference_mag is None):
            continue

        object_id = int(row["object_id"])
        instrument_unit_id = (
            int(row["instrument_unit_id"])
            if row.get("instrument_unit_id") is not None
            else None
        )
        qc_key = (object_id, instrument_unit_id)
        if qc_key not in qc_payload_cache:
            qc = _best_color_object_qc(
                qcs_by_object.get(object_id, []),
                calibration_version_id=int(calibration_version.calibration_version_id),
                instrument_unit_id=instrument_unit_id,
            )
            qc_payload_cache[qc_key] = {
                "object_qc_flags": dict(qc.flags or {}) if qc is not None else {},
                "object_qc_details": dict(qc.details or {}) if qc is not None else {},
                "object_qc_algorithm": qc.algorithm if qc is not None else None,
            }

        decorated = dict(row)
        # The server fast path is used only for normalized-only color products,
        # so materialized-only scalar fields are intentionally omitted from the
        # wire payload.  Keep the legacy row shape for downstream code.
        for field_name in (
            "x",
            "y",
            "flux",
            "flux_err",
            "mag_err_systematic",
            "aperture_radius_pix",
            "local_background",
            "local_zp_correction",
            "centroid_dx_pix",
            "centroid_dy_pix",
        ):
            decorated.setdefault(field_name, None)
        if ref_payload is not None:
            decorated.update(ref_payload)
        else:
            decorated.update(
                {
                    "reference_catalog_source_id": None,
                    "distance_to_gaia_arcsec": None,
                    "reference_mag": None,
                    **{
                        f"mag_{band}": None
                        for band in ("g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia")
                    },
                    "reference_mag_errors": {},
                    "reference_colors": {},
                    "reference_quality_flags": {},
                }
            )
        decorated.update(qc_payload_cache[qc_key])
        output.append(decorated)

    output.sort(
        key=lambda row: (
            int(row["object_id"]),
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
        )
    )
    if normalized_limit is not None:
        output = output[:normalized_limit]
    if _performance_timings is not None:
        _performance_timings["color_server_decorate"] = time.perf_counter() - _perf_t0
    return output


def _load_color_measurement_rows_from_server_reconstruction(
    session: Session,
    *,
    calibration_version: CalibrationVersion,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
    require_reference: bool,
    require_mag_inst: bool,
    _performance_timings: dict[str, float] | None = None,
) -> list[dict[str, Any]] | None:
    """Hybrid PostgreSQL/Python reader for normalized-only color application.

    PostgreSQL performs only normalized scalar reconstruction plus narrow BASE/
    frame decoration.  Normalized reference source/point data and ObjectQc are low-cardinality
    object/source data and are loaded once in bulk, avoiding per-epoch lateral
    joins and wide JSONB row transfer.

    ``None`` means that the calibration chain is unsupported and the caller must
    fall back to the authoritative Python reconstruction view.  An empty list is
    a valid successfully executed query result and must not trigger fallback.
    """

    from grandgousier.photometry.science_materialization import (
        _build_postgres_server_reconstruction_sql,
        _build_server_reconstruction_plan,
        _server_reconstruction_select_sql,
    )

    if limit is not None and int(limit) <= 0:
        return []

    frame_scope: list[int] | None = None
    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    if series_id is not None:
        frame_scope = [
            int(value)
            for value in session.scalars(
                select(FieldSeriesFrame.frame_id).where(
                    FieldSeriesFrame.series_id == int(series_id)
                )
            ).all()
        ]
    plan = _build_server_reconstruction_plan(
        session,
        target_cv=calibration_version,
        frame_ids=frame_scope,
    )
    if _performance_timings is not None:
        _performance_timings["color_server_plan"] = time.perf_counter() - _perf_t0
    if plan is None:
        return None

    insert_sql, params = _build_postgres_server_reconstruction_sql(plan)
    reconstruction_select_sql = _server_reconstruction_select_sql(insert_sql)
    sql = _build_color_server_core_sql(
        reconstruction_select_sql,
        require_mag_inst=require_mag_inst,
    )
    params.update({"identity_catalog_name": identity_catalog_name})

    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    raw_rows = session.execute(text(sql), params).mappings().all()
    if _performance_timings is not None:
        _performance_timings["color_server_query"] = time.perf_counter() - _perf_t0
    _perf_t0 = time.perf_counter() if _performance_timings is not None else 0.0
    rows = [dict(row) for row in raw_rows]
    if _performance_timings is not None:
        _performance_timings["color_server_decode"] = time.perf_counter() - _perf_t0

    return _decorate_color_server_core_rows(
        session,
        rows,
        calibration_version=calibration_version,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=reference_band,
        require_reference=require_reference,
        limit=limit,
        _performance_timings=_performance_timings,
    )


def _load_color_measurement_rows_from_analysis_snapshot(
    session: Session,
    *,
    analysis_snapshot: Any,
    calibration_version: CalibrationVersion,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
    require_reference: bool,
    require_mag_inst: bool,
    _performance_timings: dict[str, float] | None = None,
) -> list[dict[str, Any]] | None:
    """Reuse the runner's already reconstructed ENSEMBLE analysis snapshot.

    The primary Object-QC pass immediately precedes COLOR_CALIBRATION in the
    field pipeline and already owns the final ENSEMBLE scalar state for every
    epoch. Re-querying BASE and re-applying APERTURE/ZP/ENSEMBLE terms therefore
    duplicated ~2 seconds of work on the MASTER control field.  The analysis
    snapshot now retains only two extra transient values needed by color fitting:
    the BASE measurement identity and the post-aperture instrumental magnitude.

    This helper deliberately rebuilds the *same narrow V4 color-core contract*;
    it does not forward the richer QC flags from the snapshot. Low-cardinality
    reference photometry and ObjectQc are still refreshed from the database so
    the color stage sees the Object-QC rows written immediately before it.
    """

    if analysis_snapshot is None:
        return None
    try:
        if int(analysis_snapshot.calibration_version_id) != int(
            calibration_version.calibration_version_id
        ):
            return None
        if str(analysis_snapshot.calibration_name) != str(calibration_version.name):
            return None
        if str(getattr(analysis_snapshot, "y_key", "")) != "mag_cal":
            return None
        measurement_rows = tuple(analysis_snapshot.measurement_rows)
        frames_by_id = analysis_snapshot.frames_by_id
    except Exception:
        return None

    frame_scope: set[int] | None = None
    if series_id is not None:
        frame_scope = {
            int(value)
            for value in session.scalars(
                select(FieldSeriesFrame.frame_id).where(
                    FieldSeriesFrame.series_id == int(series_id)
                )
            ).all()
        }
        if not frame_scope:
            return []

    normalized_limit = None if limit is None else int(limit)
    if normalized_limit is not None and normalized_limit <= 0:
        return []

    phase_started = time.perf_counter() if _performance_timings is not None else 0.0
    core_rows: list[dict[str, Any]] = []
    for source in measurement_rows:
        frame_id = int(source.frame_id)
        if frame_scope is not None and frame_id not in frame_scope:
            continue
        flags = source.flags if isinstance(source.flags, dict) else {}
        catalog_name = flags.get("catalog_name")
        if str(catalog_name) != str(identity_catalog_name):
            continue
        source_measurement_id = getattr(source, "source_measurement_id", None)
        if source_measurement_id is None:
            # Older/caller-constructed snapshots lack the transient identity;
            # falling back is safer than changing clipping/provenance semantics.
            return None
        mag_inst = _to_float(getattr(source, "mag_inst", None))
        if require_mag_inst and mag_inst is None:
            continue
        mag_cal = _to_float(source.mag_cal)
        if mag_cal is None:
            continue
        frame = frames_by_id.get(frame_id)
        if frame is None:
            return None

        measurement_flags: dict[str, Any] = {"catalog_name": catalog_name}
        source_id = flags.get("source_id")
        if source_id is not None:
            measurement_flags["source_id"] = source_id
        for key in ("saturated", "near_saturated", "saturated_or_near_saturated"):
            value = flags.get(key)
            if value is not None:
                measurement_flags[key] = bool(value)

        core_rows.append(
            {
                "measurement_id": int(source_measurement_id),
                "object_id": int(source.object_id),
                "frame_id": frame_id,
                "mag_inst": mag_inst,
                "mag_cal": mag_cal,
                "mag_err": _first_finite(source.mag_err_random, source.mag_err),
                "mag_err_random": _first_finite(source.mag_err_random, source.mag_err),
                "measurement_flags": measurement_flags,
                "mjd": frame.mjd,
                "date_obs": getattr(frame, "date_obs", None),
                "instrument_unit_id": frame.instrument_unit_id,
                "filter_name": frame.filter_name,
                "measurement_catalog_name": catalog_name,
                "source_id": source_id,
            }
        )

    core_rows.sort(
        key=lambda row: (
            int(row["object_id"]),
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
        )
    )
    if _performance_timings is not None:
        _performance_timings["color_analysis_snapshot_convert"] = (
            time.perf_counter() - phase_started
        )

    return _decorate_color_server_core_rows(
        session,
        core_rows,
        calibration_version=calibration_version,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=reference_band,
        require_reference=require_reference,
        limit=normalized_limit,
        _performance_timings=_performance_timings,
    )


def _load_color_measurement_rows(
    session: Session,
    *,
    calibration_name: str,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
    require_reference: bool = True,
    require_mag_inst: bool = True,
    prefer_server_normalized: bool = False,
    _performance_timings: dict[str, float] | None = None,
    _read_metadata: dict[str, Any] | None = None,
    _preloaded_analysis_snapshot: Any | None = None,
) -> list[dict[str, Any]]:
    cv = session.scalar(
        select(CalibrationVersion).where(CalibrationVersion.name == str(calibration_name))
    )
    if cv is None:
        return []

    snapshot_rows = _load_color_measurement_rows_from_analysis_snapshot(
        session,
        analysis_snapshot=_preloaded_analysis_snapshot,
        calibration_version=cv,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=reference_band,
        series_id=series_id,
        limit=limit,
        require_reference=require_reference,
        require_mag_inst=require_mag_inst,
        _performance_timings=_performance_timings,
    )
    if snapshot_rows is not None:
        if _read_metadata is not None:
            _read_metadata["strategy"] = (
                "SHARED_SUPERSET_RUNNER_ANALYSIS_SNAPSHOT_V5"
            )
        return snapshot_rows

    legacy_count = int(
        session.scalar(
            select(func.count(Measurement.measurement_id)).where(
                Measurement.calibration_version_id == int(cv.calibration_version_id)
            )
        )
        or 0
    )
    if legacy_count == 0:
        if prefer_server_normalized and _session_dialect_name(session) == "postgresql":
            compact_rows = _load_color_measurement_rows_from_compact_reconstruction(
                session,
                calibration_version=cv,
                identity_catalog_name=identity_catalog_name,
                reference_catalog_name=reference_catalog_name,
                reference_band=reference_band,
                series_id=series_id,
                limit=limit,
                require_reference=require_reference,
                require_mag_inst=require_mag_inst,
                _performance_timings=_performance_timings,
            )
            if compact_rows is not None:
                if _read_metadata is not None:
                    _read_metadata["strategy"] = (
                        "SHARED_SUPERSET_POSTGRES_FUSED_COMPACT_RECONSTRUCTION_V4"
                    )
                return compact_rows

            server_rows = _load_color_measurement_rows_from_server_reconstruction(
                session,
                calibration_version=cv,
                identity_catalog_name=identity_catalog_name,
                reference_catalog_name=reference_catalog_name,
                reference_band=reference_band,
                series_id=series_id,
                limit=limit,
                require_reference=require_reference,
                require_mag_inst=require_mag_inst,
                _performance_timings=_performance_timings,
            )
            if server_rows is not None:
                if _read_metadata is not None:
                    _read_metadata["strategy"] = "SHARED_SUPERSET_POSTGRES_HYBRID_RECONSTRUCTION_V3"
                return server_rows
        if _read_metadata is not None:
            _read_metadata["strategy"] = "SHARED_SUPERSET_PYTHON_RECONSTRUCTION_V1"
        return _load_color_measurement_rows_from_view(
            session,
            calibration_version=cv,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=reference_catalog_name,
            reference_band=reference_band,
            series_id=series_id,
            limit=limit,
            require_reference=require_reference,
            require_mag_inst=require_mag_inst,
        )

    if _read_metadata is not None:
        _read_metadata["strategy"] = "SHARED_SUPERSET_BASE_VIEW_V1"
    # Grandgousier 0.7 stores only BASE full measurements.  The authoritative
    # calibration view handles BASE as well as normalized descendants, so the
    # old raw-SQL wide-reference join is no longer part of the clean schema.
    return _load_color_measurement_rows_from_view(
        session,
        calibration_version=cv,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=reference_band,
        series_id=series_id,
        limit=limit,
        require_reference=require_reference,
        require_mag_inst=require_mag_inst,
    )


def _load_color_measurement_rows_from_view(
    session: Session,
    *,
    calibration_version: CalibrationVersion,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
    require_reference: bool,
    require_mag_inst: bool,
) -> list[dict[str, Any]]:
    frame_scope: list[int] | None = None
    if series_id is not None:
        frame_scope = [
            int(value)
            for value in session.scalars(
                select(FieldSeriesFrame.frame_id).where(
                    FieldSeriesFrame.series_id == int(series_id)
                )
            ).all()
        ]
    view = load_calibration_photometry_view(
        session,
        calibration_version_id=int(calibration_version.calibration_version_id),
        frame_ids=frame_scope,
        prefer_science=False,
    )
    measurements = [
        row
        for row in view.rows
        if (row.flags or {}).get("catalog_name") == identity_catalog_name
        and (not require_mag_inst or row.mag_inst is not None)
    ]
    if not measurements:
        return []

    frame_ids = sorted({int(row.frame_id) for row in measurements})
    object_ids = sorted({int(row.object_id) for row in measurements})
    frames = {
        int(frame.frame_id): frame
        for frame in session.scalars(select(Frame).where(Frame.frame_id.in_(frame_ids))).all()
    }

    # Reproduce the legacy lateral ObjectQc preference in Python.
    qcs_by_object: dict[int, list[ObjectQc]] = {}
    if object_ids:
        qcs = session.scalars(
            select(ObjectQc).where(
                ObjectQc.object_id.in_(object_ids),
                (ObjectQc.calibration_version_id == int(calibration_version.calibration_version_id))
                | ObjectQc.calibration_version_id.is_(None),
                (ObjectQc.catalog_name == identity_catalog_name) | ObjectQc.catalog_name.is_(None),
            )
        ).all()
        for qc in qcs:
            qcs_by_object.setdefault(int(qc.object_id), []).append(qc)

    source_ids = sorted(
        {
            str((row.flags or {}).get("source_id"))
            for row in measurements
            if (row.flags or {}).get("source_id") is not None
        }
    )
    ref_by_source = load_reference_photometry_by_identity(
        session,
        reference_catalog_name=reference_catalog_name,
        identity_catalog_name=identity_catalog_name,
        source_ids=source_ids,
    )

    mag_col = _REF_MAG_COLUMNS[reference_band]
    output: list[dict[str, Any]] = []
    for measurement in measurements:
        frame = frames.get(int(measurement.frame_id))
        if frame is None:
            continue
        flags = dict(measurement.flags or {})
        source_id = str(flags.get("source_id")) if flags.get("source_id") is not None else None
        ref = ref_by_source.get(source_id or "")
        reference_mag = ref.get(mag_col) if ref is not None else None
        if require_reference and (ref is None or reference_mag is None):
            continue

        candidates = [
            qc
            for qc in qcs_by_object.get(int(measurement.object_id), [])
            if qc.instrument_unit_id is None
            or frame.instrument_unit_id is None
            or int(qc.instrument_unit_id) == int(frame.instrument_unit_id)
        ]
        candidates.sort(
            key=lambda qc: (
                1 if qc.calibration_version_id == int(calibration_version.calibration_version_id) else 0,
                1
                if qc.instrument_unit_id is not None
                and frame.instrument_unit_id is not None
                and int(qc.instrument_unit_id) == int(frame.instrument_unit_id)
                else 0,
                qc.updated_at.timestamp() if qc.updated_at is not None else float("-inf"),
                int(qc.object_qc_id),
            ),
            reverse=True,
        )
        qc = candidates[0] if candidates else None
        output.append(
            {
                "measurement_id": measurement.measurement_id,
                "object_id": int(measurement.object_id),
                "frame_id": int(measurement.frame_id),
                "x": measurement.x,
                "y": measurement.y,
                "flux": measurement.flux,
                "flux_err": measurement.flux_err,
                "mag_inst": measurement.mag_inst,
                "mag_cal": measurement.mag_cal,
                "mag_err": measurement.mag_err,
                "mag_err_random": measurement.mag_err_random,
                "mag_err_systematic": measurement.mag_err_systematic,
                "aperture_radius_pix": measurement.aperture_radius_pix,
                "local_background": measurement.local_background,
                "local_zp_correction": measurement.local_zp_correction,
                "centroid_dx_pix": measurement.centroid_dx_pix,
                "centroid_dy_pix": measurement.centroid_dy_pix,
                "measurement_flags": flags,
                "mjd": frame.mjd,
                "date_obs": frame.date_obs,
                "instrument_unit_id": frame.instrument_unit_id,
                "filter_name": frame.filter_name,
                "measurement_catalog_name": flags.get("catalog_name"),
                "source_id": source_id,
                "reference_catalog_source_id": ref.get("catalog_source_id") if ref is not None else None,
                "distance_to_gaia_arcsec": ref.get("distance_to_gaia_arcsec") if ref is not None else None,
                "reference_mag": reference_mag,
                **{
                    f"mag_{band}": ref.get(f"mag_{band}") if ref is not None else None
                    for band in ("g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia")
                },
                "reference_mag_errors": dict(ref.get("reference_mag_errors") or {}) if ref is not None else {},
                "reference_colors": dict(ref.get("colors") or {}) if ref is not None else {},
                "reference_quality_flags": dict(ref.get("quality_flags") or {}) if ref is not None else {},
                "object_qc_flags": dict(qc.flags or {}) if qc is not None else {},
                "object_qc_details": dict(qc.details or {}) if qc is not None else {},
                "object_qc_algorithm": qc.algorithm if qc is not None else None,
            }
        )

    output.sort(
        key=lambda row: (
            int(row["object_id"]),
            float(row["mjd"]) if row.get("mjd") is not None else float("inf"),
            str(row.get("date_obs") or ""),
            int(row["frame_id"]),
        )
    )
    if limit is not None:
        output = output[: int(limit)]
    return output

def _build_object_filter_info(
    rows: list[dict[str, Any]],
    *,
    min_measurements_per_object: int,
    include_flagged_calibrators: bool,
    excluded_object_qc_flags: tuple[str, ...],
    explicit_exclude_object_ids: tuple[int, ...],
    min_reference_mag: float | None,
    max_reference_mag: float | None,
    min_color: float | None,
    max_color: float | None,
) -> dict[str, Any]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["object_id"]), []).append(row)

    exclusion_reasons: dict[int, list[str]] = {}
    counters = {
        "n_excluded_by_object_id": 0,
        "n_excluded_by_object_qc": 0,
        "n_excluded_by_reference_mag": 0,
        "n_excluded_by_color": 0,
        "n_excluded_by_low_object_measurements": 0,
    }
    explicit_ids = set(explicit_exclude_object_ids)
    excluded_flags_set = {_normalize_flag_name(flag) for flag in excluded_object_qc_flags}

    for object_id, values in grouped.items():
        first = values[0]
        reasons: list[str] = []
        if object_id in explicit_ids:
            reasons.append("EXPLICIT_OBJECT_ID")
            counters["n_excluded_by_object_id"] += 1

        object_flags = set(_truthy_flag_names(first.get("object_qc_flags") if isinstance(first.get("object_qc_flags"), dict) else {}))
        bad_flags = sorted(object_flags & excluded_flags_set)
        if bad_flags and not include_flagged_calibrators:
            reasons.append("OBJECT_QC:" + ",".join(bad_flags))
            counters["n_excluded_by_object_qc"] += 1

        ref_mag = _to_float(first.get("reference_mag"))
        if min_reference_mag is not None and _is_finite(ref_mag) and float(ref_mag) < float(min_reference_mag):
            reasons.append(f"REFERENCE_MAG_LT:{float(min_reference_mag):.6g}")
            counters["n_excluded_by_reference_mag"] += 1
        if max_reference_mag is not None and _is_finite(ref_mag) and float(ref_mag) > float(max_reference_mag):
            reasons.append(f"REFERENCE_MAG_GT:{float(max_reference_mag):.6g}")
            if "REFERENCE_MAG_LT" not in ";".join(reasons[:-1]):
                counters["n_excluded_by_reference_mag"] += 1

        color = _to_float(first.get("color_value"))
        excluded_by_color = False
        if min_color is not None and _is_finite(color) and float(color) < float(min_color):
            reasons.append(f"COLOR_LT:{float(min_color):.6g}")
            excluded_by_color = True
        if max_color is not None and _is_finite(color) and float(color) > float(max_color):
            reasons.append(f"COLOR_GT:{float(max_color):.6g}")
            excluded_by_color = True
        if excluded_by_color:
            counters["n_excluded_by_color"] += 1

        if len(values) < min_measurements_per_object:
            reasons.append(f"LOW_OBJECT_MEASUREMENTS:{len(values)}<{min_measurements_per_object}")
            counters["n_excluded_by_low_object_measurements"] += 1

        if reasons:
            exclusion_reasons[object_id] = reasons

    candidate_object_ids = {object_id for object_id in grouped if object_id not in exclusion_reasons}
    low_meas_ids = {
        object_id
        for object_id, reasons in exclusion_reasons.items()
        if any(reason.startswith("LOW_OBJECT_MEASUREMENTS") for reason in reasons)
    }
    n_measurements_excluded_by_low_object_measurements = sum(len(grouped[object_id]) for object_id in low_meas_ids)
    out = {
        "n_reference_objects": len(grouped),
        "n_excluded_objects": len(exclusion_reasons),
        "candidate_object_ids": candidate_object_ids,
        "exclusion_reasons_by_object": exclusion_reasons,
        "n_measurements_excluded_by_low_object_measurements": n_measurements_excluded_by_low_object_measurements,
    }
    out.update(counters)
    return out


def _normalize_flag_name(value: Any) -> str:
    return str(value).strip().upper().replace(" ", "_").replace("-", "_")


def _truthy_flag_names(flags: dict[str, Any]) -> list[str]:
    out = []
    for key, value in (flags or {}).items():
        if bool(value):
            out.append(_normalize_flag_name(key))
    return sorted(out)


def _extract_color(row: dict[str, Any], *, color_key: str, color_bands: tuple[str, str] | None) -> float | None:
    colors = row.get("reference_colors") if isinstance(row.get("reference_colors"), dict) else {}
    if color_key in colors:
        value = _to_float(colors.get(color_key))
        if _is_finite(value):
            return float(value)
    if color_bands is None:
        return None
    a, b = color_bands
    ma = _to_float(row.get(_REF_MAG_COLUMNS[a]))
    mb = _to_float(row.get(_REF_MAG_COLUMNS[b]))
    if _is_finite(ma) and _is_finite(mb):
        return float(ma) - float(mb)
    return None


def _reference_mag_error(row: dict[str, Any], band: str) -> float | None:
    errors = row.get("reference_mag_errors") if isinstance(row.get("reference_mag_errors"), dict) else {}
    candidates = [band, f"err_{band}", f"mag_{band}_err"]
    for key in candidates:
        value = _to_float(errors.get(key))
        if _is_finite(value):
            return float(value)
    return None


def _build_star_rows(
    rows: list[dict[str, Any]],
    *,
    color0: float,
    exclusion_reasons_by_object: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["object_id"]), []).append(row)
    star_rows: list[dict[str, Any]] = []
    for object_id, values in sorted(grouped.items()):
        first = values[0]
        mag_cal_values = [float(v["mag_cal"]) for v in values if _is_finite(v.get("mag_cal"))]
        mag_inst_values = [float(v["mag_inst"]) for v in values if _is_finite(v.get("mag_inst"))]
        ref_mag = float(first["reference_mag"])
        color = float(first["color_value"])
        current_mag = _median(mag_cal_values)
        current_residual = current_mag - ref_mag if _is_finite(current_mag) else None
        reasons = list((exclusion_reasons_by_object or {}).get(int(object_id), []))
        object_qc_flags = _truthy_flag_names(first.get("object_qc_flags") if isinstance(first.get("object_qc_flags"), dict) else {})
        star_rows.append(
            {
                "object_id": object_id,
                "source_id": first.get("source_id"),
                "reference_catalog_source_id": first.get("reference_catalog_source_id"),
                "reference_mag": ref_mag,
                "reference_mag_err": first.get("reference_mag_err"),
                "color": color,
                "color_minus_color0": color - color0,
                "n_measurements": len(values),
                "n_mag_cal": len(mag_cal_values),
                "mag_cal_median": current_mag,
                "mag_inst_median": _median(mag_inst_values),
                "current_residual": current_residual,
                "current_residual_after_color": None,
                "distance_to_gaia_arcsec": first.get("distance_to_gaia_arcsec"),
                "object_qc_flags": ";".join(object_qc_flags),
                "calibrator_candidate": not bool(reasons),
                "used_as_calibrator": False,
                "exclusion_reason": ";".join(reasons),
            }
        )
    return star_rows


def _compute_current_color_fit(
    star_rows: list[dict[str, Any]],
    result: ColorCalibrationDiagnosticsResult,
    *,
    clip_sigma: float = 4.0,
    n_iterations: int = 5,
) -> None:
    """Fit an absolute transform on object medians without changing light-curve shape.

    The fitted quantity is ``reference_mag - median(source mag_cal)``. Therefore
    the returned offset and color term can be added directly to every source
    ``mag_cal`` value. Sigma clipping is performed per object, not per epoch.
    """

    rows = [
        r
        for r in star_rows
        if r.get("calibrator_candidate")
        and _is_finite(r.get("current_residual"))
        and _is_finite(r.get("color_minus_color0"))
    ]
    result.ensemble_transform_candidate_object_ids = sorted({int(r["object_id"]) for r in rows})
    residuals = [float(r["current_residual"]) for r in rows]
    result.current_residual_median = _median(residuals)
    result.current_residual_mad = _mad(residuals)
    result.current_residual_rms = _rms(residuals)
    if len(rows) < 3:
        result.warnings.append("Too few stars with mag_cal to fit ensemble-preserving color transform.")
        return

    fit = _fit_ensemble_color_transform(rows, clip_sigma=clip_sigma, n_iterations=n_iterations)
    if fit.get("offset_only_offset") is None:
        result.warnings.append("Could not fit ensemble-preserving absolute transform.")
        return

    result.ensemble_offset_only_offset = fit.get("offset_only_offset")
    result.ensemble_offset_only_offset_err = fit.get("offset_only_offset_err")
    result.ensemble_offset_only_residual_median = fit.get("offset_only_residual_median")
    result.ensemble_offset_only_residual_mad = fit.get("offset_only_residual_mad")
    result.ensemble_offset_only_residual_rms = fit.get("offset_only_residual_rms")
    result.ensemble_offset_only_residual_robust_sigma = fit.get("offset_only_residual_robust_sigma")
    result.ensemble_offset_only_bic = fit.get("offset_only_bic")
    result.ensemble_color_model_bic = fit.get("color_model_bic")
    result.ensemble_color_model_bic_improvement = fit.get("color_model_bic_improvement")
    result.ensemble_color_model_mad_improvement = fit.get("color_model_mad_improvement")
    result.ensemble_color_model_rms_improvement = fit.get("color_model_rms_improvement")
    result.ensemble_color_term_significance = fit.get("color_term_significance")

    if fit.get("offset") is None or fit.get("color_term") is None:
        result.warnings.append(
            "Color-dependent transform could not be fitted; offset-only absolute transform remains available."
        )
        result.ensemble_transform_offset = fit.get("offset_only_offset")
        result.ensemble_transform_offset_err = fit.get("offset_only_offset_err")
        result.ensemble_transform_color_term = 0.0
        result.ensemble_transform_color_term_err = None
        result.ensemble_transform_offset_color_cov = None
        result.ensemble_transform_residual_median = fit.get("offset_only_residual_median")
        result.ensemble_transform_residual_mad = fit.get("offset_only_residual_mad")
        result.ensemble_transform_residual_rms = fit.get("offset_only_residual_rms")
        result.ensemble_transform_residual_robust_sigma = fit.get("offset_only_residual_robust_sigma")
        result.ensemble_transform_used_objects = int(fit.get("n_used_objects") or 0)
        result.ensemble_transform_clipped_objects = int(fit.get("n_clipped_objects") or 0)
        result.ensemble_transform_used_measurements = int(fit.get("n_used_measurements") or 0)
        result.ensemble_transform_used_object_ids = sorted(int(v) for v in (fit.get("used_object_ids") or []))
        result.ensemble_transform_clipped_object_ids = sorted(
            set(result.ensemble_transform_candidate_object_ids) - set(result.ensemble_transform_used_object_ids)
        )
        result.calibrator_color_sigma = fit.get("color_sigma")
        return

    # Preserve historical current_* semantics: source-reference residual fit.
    result.current_color_intercept = -float(fit["offset"])
    result.current_color_slope = -float(fit["color_term"])
    result.ensemble_transform_offset = float(fit["offset"])
    result.ensemble_transform_color_term = float(fit["color_term"])
    result.ensemble_transform_offset_err = fit.get("offset_err")
    result.ensemble_transform_color_term_err = fit.get("color_term_err")
    result.ensemble_transform_offset_color_cov = fit.get("offset_color_cov")
    result.ensemble_transform_residual_median = fit.get("residual_median")
    result.ensemble_transform_residual_mad = fit.get("residual_mad")
    result.ensemble_transform_residual_rms = fit.get("residual_rms")
    result.ensemble_transform_residual_robust_sigma = fit.get("residual_robust_sigma")
    result.ensemble_transform_used_objects = int(fit.get("n_used_objects") or 0)
    result.ensemble_transform_clipped_objects = int(fit.get("n_clipped_objects") or 0)
    result.ensemble_transform_used_measurements = int(fit.get("n_used_measurements") or 0)
    result.ensemble_transform_used_object_ids = sorted(int(v) for v in (fit.get("used_object_ids") or []))
    result.ensemble_transform_clipped_object_ids = sorted(
        set(result.ensemble_transform_candidate_object_ids) - set(result.ensemble_transform_used_object_ids)
    )
    result.calibrator_color_sigma = fit.get("color_sigma")
    # The publish gate and extrapolation flags must use the color range of the
    # objects that survived transform clipping, not rejected candidates.
    if _is_finite(fit.get("x_min")) and _is_finite(fit.get("x_max")):
        result.color_min = float(result.color0) + float(fit["x_min"])
        result.color_max = float(result.color0) + float(fit["x_max"])

    after = []
    used_ids = set(fit.get("used_object_ids") or [])
    for row in rows:
        x = float(row["color_minus_color0"])
        source_minus_ref = float(row["current_residual"])
        transformed_residual = source_minus_ref + float(fit["offset"]) + float(fit["color_term"]) * x
        row["current_residual_after_color"] = transformed_residual
        row["ensemble_transform_used"] = int(row["object_id"]) in used_ids
        row["ensemble_transform_residual"] = transformed_residual
        if int(row["object_id"]) in used_ids:
            row["used_as_calibrator"] = True
            after.append(transformed_residual)
        elif not row.get("exclusion_reason"):
            row["exclusion_reason"] = "ENSEMBLE_TRANSFORM_CLIPPED"
    result.current_residual_after_color_median = _median(after)
    result.current_residual_after_color_mad = _mad(after)
    result.current_residual_after_color_rms = _rms(after)


def _fit_ensemble_color_transform(
    star_rows: list[dict[str, Any]],
    *,
    clip_sigma: float,
    n_iterations: int,
) -> dict[str, Any]:
    prepared = []
    for row in star_rows:
        if not _is_finite(row.get("color_minus_color0")) or not _is_finite(row.get("current_residual")):
            continue
        prepared.append(
            {
                "object_id": int(row["object_id"]),
                "x": float(row["color_minus_color0"]),
                # Additive correction required to map source onto reference.
                "y": -float(row["current_residual"]),
                "n_measurements": int(row.get("n_measurements") or 0),
            }
        )
    if len(prepared) < 3:
        return {
            "offset": None,
            "color_term": None,
            "offset_only_offset": None,
            "n_used_objects": 0,
            "n_clipped_objects": 0,
        }

    used = list(prepared)
    previous_ids: set[int] | None = None
    offset = color_term = None
    for _ in range(max(1, int(n_iterations))):
        offset, color_term = _linear_fit_with_intercept(
            [r["x"] for r in used],
            [r["y"] for r in used],
        )
        if offset is None or color_term is None:
            break
        residuals = [r["y"] - (float(offset) + float(color_term) * r["x"]) for r in used]
        center = _median(residuals) or 0.0
        scale = _robust_sigma(residuals)
        if scale is None or scale <= 0:
            kept = list(used)
        else:
            kept = [
                r for r, residual in zip(used, residuals)
                if abs(float(residual) - float(center)) <= float(clip_sigma) * float(scale)
            ]
        if len(kept) < 3:
            break
        kept_ids = {r["object_id"] for r in kept}
        if kept_ids == previous_ids or len(kept) == len(used):
            used = kept
            break
        previous_ids = kept_ids
        used = kept

    if len(used) < 3:
        offset_only = _fit_offset_only_transform(used)
        return {
            "offset": None,
            "color_term": None,
            **offset_only,
            "n_used_objects": len(used),
            "n_clipped_objects": len(prepared) - len(used),
            "n_used_measurements": sum(int(r.get("n_measurements") or 0) for r in used),
            "used_object_ids": sorted(r["object_id"] for r in used),
        }
    offset, color_term = _linear_fit_with_intercept([r["x"] for r in used], [r["y"] for r in used])
    if offset is None or color_term is None:
        offset_only = _fit_offset_only_transform(used)
        return {
            "offset": None,
            "color_term": None,
            **offset_only,
            "n_used_objects": len(used),
            "n_clipped_objects": len(prepared) - len(used),
            "n_used_measurements": sum(int(r.get("n_measurements") or 0) for r in used),
            "used_object_ids": sorted(r["object_id"] for r in used),
        }
    residuals = [r["y"] - (float(offset) + float(color_term) * r["x"]) for r in used]
    uncertainty = _linear_fit_uncertainties([r["x"] for r in used], residuals)
    xs = [r["x"] for r in used]
    offset_only = _fit_offset_only_transform(used)
    color_bic = _bic_from_residuals(residuals, n_parameters=2)
    offset_only_bic = offset_only.get("offset_only_bic")
    color_term_significance = None
    if uncertainty[1] is not None and float(uncertainty[1]) > 0:
        color_term_significance = abs(float(color_term)) / float(uncertainty[1])
    elif float(color_term) != 0.0:
        color_term_significance = math.inf
    color_mad = _mad(residuals)
    color_rms = _rms(residuals)
    return {
        "offset": float(offset),
        "color_term": float(color_term),
        "offset_err": uncertainty[0],
        "color_term_err": uncertainty[1],
        "offset_color_cov": uncertainty[2],
        "residual_median": _median(residuals),
        "residual_mad": color_mad,
        "residual_rms": color_rms,
        "residual_robust_sigma": _robust_sigma(residuals),
        **offset_only,
        "color_model_bic": color_bic,
        "color_model_bic_improvement": (
            float(offset_only_bic) - float(color_bic)
            if _is_finite(offset_only_bic) and _is_finite(color_bic)
            else None
        ),
        "color_model_mad_improvement": (
            float(offset_only.get("offset_only_residual_mad")) - float(color_mad)
            if _is_finite(offset_only.get("offset_only_residual_mad")) and _is_finite(color_mad)
            else None
        ),
        "color_model_rms_improvement": (
            float(offset_only.get("offset_only_residual_rms")) - float(color_rms)
            if _is_finite(offset_only.get("offset_only_residual_rms")) and _is_finite(color_rms)
            else None
        ),
        "color_term_significance": color_term_significance,
        "n_used_objects": len(used),
        "n_clipped_objects": len(prepared) - len(used),
        "n_used_measurements": sum(int(r.get("n_measurements") or 0) for r in used),
        "used_object_ids": sorted(r["object_id"] for r in used),
        "color_sigma": _robust_sigma(xs),
        "x_min": min(xs) if xs else None,
        "x_max": max(xs) if xs else None,
    }


def _fit_offset_only_transform(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["y"]) for row in rows if _is_finite(row.get("y"))]
    if not values:
        return {"offset_only_offset": None}
    offset = sum(values) / float(len(values))
    residuals = [value - offset for value in values]
    rms = _rms(residuals)
    offset_err = None
    if len(values) > 1 and _is_finite(rms):
        offset_err = float(rms) / math.sqrt(float(len(values)))
    return {
        "offset_only_offset": offset,
        "offset_only_offset_err": offset_err,
        "offset_only_residual_median": _median(residuals),
        "offset_only_residual_mad": _mad(residuals),
        "offset_only_residual_rms": rms,
        "offset_only_residual_robust_sigma": _robust_sigma(residuals),
        "offset_only_bic": _bic_from_residuals(residuals, n_parameters=1),
    }


def _select_ensemble_transform_model(
    diagnostics: ColorCalibrationDiagnosticsResult,
    *,
    min_color_term_significance: float,
    min_color_model_bic_improvement: float,
    min_color_model_mad_improvement: float,
) -> dict[str, Any]:
    """Select a conservative absolute transform model from comparable fits."""

    offset = diagnostics.ensemble_offset_only_offset
    if offset is None:
        return {
            "model": None,
            "reason": "offset-only fit unavailable",
            "offset": None,
            "offset_err": None,
            "color_term": None,
            "color_term_err": None,
            "offset_color_cov": None,
            "residual_mad": None,
            "residual_rms": None,
            "residual_robust_sigma": None,
        }

    color_available = (
        diagnostics.ensemble_transform_offset is not None
        and diagnostics.ensemble_transform_color_term is not None
    )
    significance = diagnostics.ensemble_color_term_significance
    bic_improvement = diagnostics.ensemble_color_model_bic_improvement
    mad_improvement = diagnostics.ensemble_color_model_mad_improvement
    rejection_reasons: list[str] = []
    if not color_available:
        rejection_reasons.append("color fit unavailable")
    if significance is None or float(significance) < float(min_color_term_significance):
        rejection_reasons.append(
            f"color-term significance {significance} < {float(min_color_term_significance)}"
        )
    if bic_improvement is None or float(bic_improvement) < float(min_color_model_bic_improvement):
        rejection_reasons.append(
            f"BIC improvement {bic_improvement} < {float(min_color_model_bic_improvement)}"
        )
    if mad_improvement is None or float(mad_improvement) < float(min_color_model_mad_improvement):
        rejection_reasons.append(
            f"MAD improvement {mad_improvement} < {float(min_color_model_mad_improvement)}"
        )

    if not rejection_reasons:
        return {
            "model": "offset_plus_color",
            "reason": "color term accepted by significance, BIC, and residual-MAD criteria",
            "offset": diagnostics.ensemble_transform_offset,
            "offset_err": diagnostics.ensemble_transform_offset_err,
            "color_term": diagnostics.ensemble_transform_color_term,
            "color_term_err": diagnostics.ensemble_transform_color_term_err,
            "offset_color_cov": diagnostics.ensemble_transform_offset_color_cov,
            "residual_mad": diagnostics.ensemble_transform_residual_mad,
            "residual_rms": diagnostics.ensemble_transform_residual_rms,
            "residual_robust_sigma": diagnostics.ensemble_transform_residual_robust_sigma,
        }

    return {
        "model": "offset_only",
        "reason": "offset-only selected: " + "; ".join(rejection_reasons),
        "offset": diagnostics.ensemble_offset_only_offset,
        "offset_err": diagnostics.ensemble_offset_only_offset_err,
        "color_term": 0.0,
        "color_term_err": None,
        "offset_color_cov": None,
        "residual_mad": diagnostics.ensemble_offset_only_residual_mad,
        "residual_rms": diagnostics.ensemble_offset_only_residual_rms,
        "residual_robust_sigma": diagnostics.ensemble_offset_only_residual_robust_sigma,
    }


def _bic_from_residuals(residuals: list[float], *, n_parameters: int) -> float | None:
    values = [float(value) for value in residuals if _is_finite(value)]
    n = len(values)
    if n <= int(n_parameters):
        return None
    rss = sum(value * value for value in values)
    mean_square = max(rss / float(n), 1e-24)
    return float(n) * math.log(mean_square) + float(n_parameters) * math.log(float(n))


def _linear_fit_uncertainties(xs: list[float], residuals: list[float]) -> tuple[float | None, float | None, float | None]:
    n = len(xs)
    if n < 3 or len(residuals) != n:
        return None, None, None
    sxx = sum(float(x) ** 2 for x in xs)
    sx = sum(float(x) for x in xs)
    det = float(n) * sxx - sx * sx
    if det <= 0:
        return None, None, None
    rss = sum(float(r) ** 2 for r in residuals)
    variance = rss / float(max(1, n - 2))
    var_offset = variance * sxx / det
    var_slope = variance * float(n) / det
    covariance = -variance * sx / det
    return math.sqrt(max(0.0, var_offset)), math.sqrt(max(0.0, var_slope)), covariance


def _fit_frame_color_model(
    rows: list[dict[str, Any]],
    *,
    color0: float,
    min_stars_per_frame: int,
    clip_sigma: float,
    n_iterations: int,
) -> dict[str, Any]:
    model_rows = [dict(r) for r in rows]
    for r in model_rows:
        r["x_color"] = float(r["color_value"]) - color0
        r["y_zp"] = float(r["reference_mag"]) - float(r["mag_inst"])
        r["model_used"] = True
        r["frame_zp"] = None
        r["frame_model_residual"] = None

    color_term = 0.0
    previous_used_ids: set[int] | None = None
    frame_zp: dict[int, float] = {}
    rejected_low_frame = 0

    for _iteration in range(n_iterations):
        used_now = [r for r in model_rows if r.get("model_used")]
        frame_zp = _compute_frame_zp(used_now, color_term=color_term, min_stars_per_frame=min_stars_per_frame)
        frame_counts_all = _frame_counts(used_now)
        rejected_low_frame = sum(n for fid, n in frame_counts_all.items() if fid not in frame_zp)
        for r in model_rows:
            fid = int(r["frame_id"])
            r["frame_zp"] = frame_zp.get(fid)
            if fid not in frame_zp:
                r["model_used"] = False
                r["frame_model_residual"] = None
                continue
            pred_mag = float(r["mag_inst"]) + frame_zp[fid] + color_term * float(r["x_color"])
            r["frame_model_residual"] = pred_mag - float(r["reference_mag"])
        residuals = [float(r["frame_model_residual"]) for r in model_rows if r.get("model_used") and _is_finite(r.get("frame_model_residual"))]
        if not residuals:
            break
        center = _median(residuals) or 0.0
        scale = _robust_sigma(residuals)
        if scale is None or scale <= 0:
            clipped_ids = {int(r["measurement_id"]) for r in model_rows if r.get("model_used")}
        else:
            clipped_ids = {
                int(r["measurement_id"])
                for r in model_rows
                if r.get("model_used")
                and _is_finite(r.get("frame_model_residual"))
                and abs(float(r["frame_model_residual"]) - center) <= clip_sigma * scale
            }
        for r in model_rows:
            if int(r["frame_id"]) not in frame_zp:
                r["model_used"] = False
            else:
                r["model_used"] = int(r["measurement_id"]) in clipped_ids
        fit_rows = [r for r in model_rows if r.get("model_used") and int(r["frame_id"]) in frame_zp]
        denom = sum(float(r["x_color"]) ** 2 for r in fit_rows)
        if denom > 0:
            color_term = sum(float(r["x_color"]) * (float(r["y_zp"]) - frame_zp[int(r["frame_id"])]) for r in fit_rows) / denom
        if previous_used_ids == clipped_ids:
            break
        previous_used_ids = clipped_ids

    # Final pass with final color_term and final used mask.
    used_final = [r for r in model_rows if r.get("model_used")]
    frame_zp = _compute_frame_zp(used_final, color_term=color_term, min_stars_per_frame=min_stars_per_frame)
    for r in model_rows:
        fid = int(r["frame_id"])
        if r.get("model_used") and fid in frame_zp:
            r["frame_zp"] = frame_zp[fid]
            r["frame_model_residual"] = float(r["mag_inst"]) + frame_zp[fid] + color_term * float(r["x_color"]) - float(r["reference_mag"])
        else:
            r["model_used"] = False
            r["frame_zp"] = frame_zp.get(fid)
            r["frame_model_residual"] = None

    residuals_final = [float(r["frame_model_residual"]) for r in model_rows if r.get("model_used") and _is_finite(r.get("frame_model_residual"))]
    used_object_ids = {int(r["object_id"]) for r in model_rows if r.get("model_used")}
    used_frame_ids = {int(r["frame_id"]) for r in model_rows if r.get("model_used")}
    frame_rows = _build_frame_rows(model_rows, frame_zp)
    return {
        "color_term": float(color_term) if _is_finite(color_term) else None,
        "n_used_measurements": len(residuals_final),
        "n_used_objects": len(used_object_ids),
        "n_used_frames": len(used_frame_ids),
        "n_frames_with_zp": len(frame_zp),
        "n_rejected_low_frame_stars": int(rejected_low_frame),
        "n_clipped_measurements": max(0, len(rows) - len(residuals_final) - int(rejected_low_frame)),
        "residual_median": _median(residuals_final),
        "residual_mad": _mad(residuals_final),
        "residual_rms": _rms(residuals_final),
        "frame_zp_median": _median(list(frame_zp.values())),
        "frame_zp_mad": _mad(list(frame_zp.values())),
        "frame_rows": frame_rows,
        "measurement_rows": model_rows,
    }


def _compute_frame_zp(rows: list[dict[str, Any]], *, color_term: float, min_stars_per_frame: int) -> dict[int, float]:
    grouped: dict[int, list[float]] = {}
    for r in rows:
        fid = int(r["frame_id"])
        value = float(r["y_zp"]) - color_term * float(r["x_color"])
        if _is_finite(value):
            grouped.setdefault(fid, []).append(float(value))
    return {fid: float(_median(vals)) for fid, vals in grouped.items() if len(vals) >= min_stars_per_frame and _median(vals) is not None}


def _frame_counts(rows: list[dict[str, Any]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in rows:
        fid = int(r["frame_id"])
        out[fid] = out.get(fid, 0) + 1
    return out


def _build_frame_rows(rows: list[dict[str, Any]], frame_zp: dict[int, float]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(int(r["frame_id"]), []).append(r)
    out = []
    for fid, values in sorted(grouped.items()):
        used = [v for v in values if v.get("model_used") and _is_finite(v.get("frame_model_residual"))]
        residuals = [float(v["frame_model_residual"]) for v in used]
        first = values[0]
        out.append(
            {
                "frame_id": fid,
                "mjd": first.get("mjd"),
                "date_obs": first.get("date_obs"),
                "instrument_unit_id": first.get("instrument_unit_id"),
                "filter_name": first.get("filter_name"),
                "n_measurements": len(values),
                "n_used_measurements": len(used),
                "frame_zp": frame_zp.get(fid),
                "residual_median": _median(residuals),
                "residual_mad": _mad(residuals),
                "residual_rms": _rms(residuals),
            }
        )
    return out


def _write_outputs(
    result: ColorCalibrationDiagnosticsResult,
    star_rows: list[dict[str, Any]],
    frame_rows: list[dict[str, Any]],
    out_dir: Path | None,
) -> None:
    if out_dir is None:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "color_calibration_summary.json"
    stars_path = out_dir / "color_calibration_stars.csv"
    frames_path = out_dir / "color_residuals_by_frame.csv"
    result.summary_json = str(summary_path)
    result.stars_csv = str(stars_path)
    result.frames_csv = str(frames_path)

    _write_csv(stars_path, star_rows, _STAR_COLUMNS)
    _write_csv(frames_path, frame_rows, _FRAME_COLUMNS)
    summary_path.write_text(json.dumps(result.summary_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


_STAR_COLUMNS = [
    "object_id",
    "source_id",
    "calibrator_candidate",
    "used_as_calibrator",
    "exclusion_reason",
    "object_qc_flags",
    "reference_catalog_source_id",
    "reference_mag",
    "reference_mag_err",
    "color",
    "color_minus_color0",
    "n_measurements",
    "n_mag_cal",
    "mag_cal_median",
    "mag_inst_median",
    "current_residual",
    "current_residual_after_color",
    "ensemble_transform_used",
    "ensemble_transform_residual",
    "frame_model_residual_median",
    "frame_model_residual_mad",
    "frame_model_n_measurements",
    "distance_to_gaia_arcsec",
]

_FRAME_COLUMNS = [
    "frame_id",
    "mjd",
    "date_obs",
    "instrument_unit_id",
    "filter_name",
    "n_measurements",
    "n_used_measurements",
    "frame_zp",
    "residual_median",
    "residual_mad",
    "residual_rms",
]


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _csv_value(row.get(col)) for col in columns})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.10g}"
    return value


def _unique_object_values(rows: list[dict[str, Any]], object_key: str, value_key: str) -> list[float]:
    seen = set()
    values = []
    for r in rows:
        oid = r.get(object_key)
        if oid in seen:
            continue
        seen.add(oid)
        value = _to_float(r.get(value_key))
        if _is_finite(value):
            values.append(float(value))
    return values


def _linear_fit_with_intercept(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if _is_finite(x) and _is_finite(y)]
    if len(pairs) < 2:
        return None, None
    xmean = sum(x for x, _ in pairs) / len(pairs)
    ymean = sum(y for _, y in pairs) / len(pairs)
    denom = sum((x - xmean) ** 2 for x, _ in pairs)
    if denom <= 0:
        return ymean, 0.0
    slope = sum((x - xmean) * (y - ymean) for x, y in pairs) / denom
    intercept = ymean - slope * xmean
    return float(intercept), float(slope)



def _sample_rms(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return 0.0 if len(vals) == 1 else None
    center = sum(vals) / len(vals)
    return math.sqrt(sum((v - center) ** 2 for v in vals) / (len(vals) - 1))


def _percentile(values: Iterable[float], q: float) -> float | None:
    vals = sorted(float(v) for v in values if _is_finite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    position = (len(vals) - 1) * float(q) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return vals[lower]
    fraction = position - lower
    return vals[lower] * (1.0 - fraction) + vals[upper] * fraction


def _robust_peak_to_peak(values: Iterable[float]) -> float | None:
    p10 = _percentile(values, 10.0)
    p90 = _percentile(values, 90.0)
    if p10 is None or p90 is None:
        return None
    return float(p90) - float(p10)

def _median(values: Iterable[float | None]) -> float | None:
    vals = sorted(float(v) for v in values if _is_finite(v))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mad(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = _median(vals)
    if med is None:
        return None
    return _median([abs(v - med) for v in vals])


def _robust_sigma(values: Iterable[float | None]) -> float | None:
    mad = _mad(values)
    if mad is None:
        return None
    return 1.4826 * mad


def _rms(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = _median(vals) or 0.0
    return math.sqrt(sum((v - med) ** 2 for v in vals) / len(vals))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _is_saturated(flags: dict[str, Any]) -> bool:
    for key in ("saturated", "near_saturated", "saturated_or_near_saturated"):
        if bool(flags.get(key)):
            return True
    return False
