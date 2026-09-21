from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
from sqlalchemy import delete, false, func, or_, select
from sqlalchemy.orm import Session

from grandgousier.calibration.aperture_context import (
    analyze_aperture_frame_context,
    build_frame_aperture_context,
    estimate_psf_shape,
    export_aperture_context_diagnostics,
)
from grandgousier.calibration.normalized_terms import FrameTermInput, replace_calibration_iteration
from grandgousier.calibration.provenance import prepare_calibration_version_for_write, set_source_calibration
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import CalibrationVersion, Frame, FrameLocation, Measurement


@dataclass
class ApertureCorrectionFrameStat:
    frame_id: int
    n_candidates: int = 0
    n_used: int = 0
    selected_model: str = "identity"
    selection_reason: str | None = None
    correction_median: float | None = None
    correction_mad: float | None = None
    residual_rms: float | None = None
    residual_mad: float | None = None
    residual_robust_sigma: float | None = None
    constant_bic: float | None = None
    plane_bic: float | None = None
    plane_bic_improvement: float | None = None
    plane_candidate_max_abs_spatial_deviation: float | None = None
    plane_candidate_max_abs_total_correction: float | None = None
    legacy_selected_model: str | None = None
    selection_changed_vs_legacy: bool = False
    spatial_peak_to_peak: float | None = None
    coefficient_0: float | None = None
    coefficient_x: float | None = None
    coefficient_y: float | None = None
    model_error_median: float | None = None
    differential_error_median: float | None = None
    accepted: bool = False
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ReferenceApertureMeasurement:
    """Minimal large-aperture result used by the aperture-correction fit."""

    mag_inst: float | None
    local_background: float | None
    flags: dict[str, object]


@dataclass
class ApertureCorrectionResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_name: str | None = None
    instrument_unit_id: int | None = None
    selected_frames: int = 0
    checked_frames: int = 0
    frames_with_model: int = 0
    frames_with_plane: int = 0
    plane_frame_ids: list[int] = field(default_factory=list)
    legacy_frames_with_plane: int = 0
    legacy_plane_frame_ids: list[int] = field(default_factory=list)
    frames_promoted_to_plane_vs_legacy: int = 0
    promoted_to_plane_frame_ids: list[int] = field(default_factory=list)
    frames_demoted_from_plane_vs_legacy: int = 0
    demoted_from_plane_frame_ids: list[int] = field(default_factory=list)
    frames_with_constant: int = 0
    frames_identity_fallback: int = 0
    selected_measurements: int = 0
    calibrated: int = 0
    copied_identity: int = 0
    inserted_or_updated: int = 0
    deleted_existing: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_missing_file: int = 0
    failed_frames: int = 0
    failed_reference_measurements: int = 0
    reference_radius_pix: float | None = None
    good_frame_fraction: float | None = None
    median_residual_mad: float | None = None
    median_spatial_peak_to_peak: float | None = None
    median_plane_spatial_peak_to_peak: float | None = None
    median_plane_max_abs_spatial_deviation: float | None = None
    quality_gate_passed: bool = False
    quality_gate_failures: list[str] = field(default_factory=list)
    quality_gate_thresholds: dict[str, Any] = field(default_factory=dict)
    context_analysis_status: str | None = None
    context_frames_with_psf_shape: int = 0
    context_plane_frames_with_psf_shape: int = 0
    context_frames_with_header_fwhm: int = 0
    context_plane_gradient_mean_angle_deg: float | None = None
    context_plane_gradient_resultant_length: float | None = None
    context_plane_gradient_sign_consistency: float | None = None
    context_plane_gradient_psf_axis_delta_median_deg: float | None = None
    context_plane_time_span_minutes: float | None = None
    context_plane_candidate_frames: int = 0
    context_plane_candidate_gradient_mean_angle_deg: float | None = None
    context_plane_candidate_gradient_resultant_length: float | None = None
    context_plane_candidate_gradient_sign_consistency: float | None = None
    context_analysis: dict[str, Any] = field(default_factory=dict)
    diagnostics_summary_json: str | None = None
    diagnostics_frames_csv: str | None = None
    diagnostics_context_summary_json: str | None = None
    diagnostics_context_frames_csv: str | None = None
    diagnostics_context_plot_png: str | None = None
    warnings: list[str] = field(default_factory=list)
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    normalized_object_terms: int = 0
    normalized_write_status: str = "NOT_WRITTEN"
    performance_diagnostics_enabled: bool = False
    timings_sec: dict[str, float] = field(default_factory=dict)
    throughput: dict[str, float] = field(default_factory=dict)
    source_read_strategy: str | None = None
    normalized_only_fast_path: bool = False
    output_prepare_strategy: str | None = None
    reference_measure_strategy: str = "SCALAR_MEASURE_ONE_SOURCE_V1"
    reference_measure_batch_sources: int = 0
    reference_measure_scalar_fallback_sources: int = 0
    reference_measure_batch_chunks: int = 0
    dry_run: bool = False


def _measure_reference_apertures_batch(
    data: np.ndarray,
    measurements: list[Any],
    *,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime: float | None,
    saturation_level_adu: float | None,
    performance_timings: dict[str, float] | None = None,
    performance_counters: dict[str, int] | None = None,
    batch_size: int = 64,
) -> list[_ReferenceApertureMeasurement | None]:
    """Measure large reference apertures in vectorized interior batches.

    Aperture correction only consumes ``mag_inst``, local background and a
    handful of quality flags from the large-aperture measurement.  The scalar
    ``measure_one_source`` path also computes a full random-error budget that is
    discarded here.  Interior sources therefore share one NumPy cutout/mask
    kernel per batch; edge and degenerate cases retain the historical scalar
    implementation exactly.
    """

    if not measurements:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    def _timing_start() -> float | None:
        return time.perf_counter() if performance_timings is not None else None

    def _timing_add(key: str, started: float | None) -> None:
        if performance_timings is None or started is None:
            return
        performance_timings[key] = performance_timings.get(key, 0.0) + float(
            time.perf_counter() - started
        )

    def _counter_add(key: str, value: int) -> None:
        if performance_counters is None:
            return
        performance_counters[key] = performance_counters.get(key, 0) + int(value)

    ny, nx = data.shape
    pad = int(math.ceil(float(annulus_outer_pix) + 1.0))
    offsets_i = np.arange(-pad, pad + 1, dtype=np.intp)
    offsets_f = offsets_i.astype(float, copy=False)
    results: list[_ReferenceApertureMeasurement | None] = [None] * len(measurements)
    interior_indices: list[int] = []
    fallback_indices: list[int] = []

    for idx, measurement in enumerate(measurements):
        try:
            x = float(measurement.x)
            y = float(measurement.y)
        except Exception:
            fallback_indices.append(idx)
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            fallback_indices.append(idx)
            continue
        floor_x = int(math.floor(x))
        floor_y = int(math.floor(y))
        if (
            floor_x - pad < 0
            or floor_x + pad >= nx
            or floor_y - pad < 0
            or floor_y + pad >= ny
        ):
            fallback_indices.append(idx)
        else:
            interior_indices.append(idx)

    ap2 = float(aperture_radius_pix) ** 2
    ann_in2 = float(annulus_inner_pix) ** 2
    ann_out2 = float(annulus_outer_pix) ** 2

    for chunk_start in range(0, len(interior_indices), int(batch_size)):
        chunk_indices = interior_indices[chunk_start : chunk_start + int(batch_size)]
        if not chunk_indices:
            continue
        _counter_add("batch_chunks", 1)
        try:
            phase_started = _timing_start()
            xs = np.asarray([float(measurements[idx].x) for idx in chunk_indices], dtype=float)
            ys = np.asarray([float(measurements[idx].y) for idx in chunk_indices], dtype=float)
            floor_x = np.floor(xs).astype(np.intp)
            floor_y = np.floor(ys).astype(np.intp)
            y_index = floor_y[:, None, None] + offsets_i[None, :, None]
            x_index = floor_x[:, None, None] + offsets_i[None, None, :]
            cutouts = data[y_index, x_index]
            dx = offsets_f[None, :] - (xs - floor_x)[:, None]
            dy = offsets_f[None, :] - (ys - floor_y)[:, None]
            rr2 = dy[:, :, None] * dy[:, :, None] + dx[:, None, :] * dx[:, None, :]
            aperture_mask = rr2 <= ap2
            annulus_mask = (rr2 >= ann_in2) & (rr2 <= ann_out2)
            finite = np.isfinite(cutouts)
            aperture_valid = aperture_mask & finite
            annulus_valid = annulus_mask & finite
            n_ap = np.count_nonzero(aperture_valid, axis=(1, 2)).astype(np.int64)
            n_sky = np.count_nonzero(annulus_valid, axis=(1, 2)).astype(np.int64)
            _timing_add("reference_batch_geometry", phase_started)

            good = (n_ap > 0) & (n_sky >= 5)
            for local_idx, source_idx in enumerate(chunk_indices):
                if not bool(good[local_idx]):
                    fallback_indices.append(source_idx)
            good_local = np.flatnonzero(good)
            if good_local.size == 0:
                continue

            phase_started = _timing_start()
            selected_cutouts = cutouts[good_local]
            selected_aperture_valid = aperture_valid[good_local]
            selected_annulus_valid = annulus_valid[good_local]
            selected_n_ap = n_ap[good_local]
            annulus_values = np.where(selected_annulus_valid, selected_cutouts, np.nan)
            aperture_values = np.where(selected_aperture_valid, selected_cutouts, np.nan)
            with np.errstate(invalid="ignore"):
                local_background = np.nanmedian(annulus_values, axis=(1, 2))
                aperture_sum = np.nansum(aperture_values, axis=(1, 2))
                aperture_max = (
                    np.nanmax(aperture_values, axis=(1, 2))
                    if saturation_level_adu is not None
                    else None
                )
            flux = aperture_sum - local_background * selected_n_ap
            _timing_add("reference_batch_statistics", phase_started)

            phase_started = _timing_start()
            for output_local, source_local in enumerate(good_local.tolist()):
                source_idx = chunk_indices[source_local]
                source_flux = float(flux[output_local])
                flags: dict[str, object] = {}
                if saturation_level_adu is not None and aperture_max is not None:
                    source_max = float(aperture_max[output_local])
                    if source_max >= float(saturation_level_adu):
                        flags["saturated_or_near_saturated"] = True
                        flags["aperture_max_adu"] = source_max
                mag_inst = None
                if source_flux > 0:
                    rate = (
                        source_flux / float(exptime)
                        if exptime is not None and float(exptime) > 0
                        else source_flux
                    )
                    if rate > 0:
                        mag_inst = float(-2.5 * math.log10(rate))
                else:
                    flags["non_positive_flux"] = True
                results[source_idx] = _ReferenceApertureMeasurement(
                    mag_inst=mag_inst,
                    local_background=float(local_background[output_local]),
                    flags=flags,
                )
            _counter_add("batch_sources", int(good_local.size))
            _timing_add("reference_batch_finalize", phase_started)
        except Exception:
            # Preserve the historical per-source resilience if a vectorized
            # chunk encounters an unexpected NumPy/data edge case.
            fallback_indices.extend(chunk_indices)

    if fallback_indices:
        from grandgousier.photometry.forced_aperture import measure_one_source

        phase_started = _timing_start()
        fallback_unique = sorted(set(fallback_indices))
        for idx in fallback_unique:
            measurement = measurements[idx]
            try:
                scalar = measure_one_source(
                    data,
                    x=float(measurement.x),
                    y=float(measurement.y),
                    aperture_radius_pix=float(aperture_radius_pix),
                    annulus_inner_pix=float(annulus_inner_pix),
                    annulus_outer_pix=float(annulus_outer_pix),
                    exptime=exptime,
                    zero_point=0.0,
                    saturation_level_adu=saturation_level_adu,
                )
                results[idx] = _ReferenceApertureMeasurement(
                    mag_inst=scalar.mag_inst,
                    local_background=scalar.local_background,
                    flags=dict(scalar.flags),
                )
            except Exception:
                results[idx] = None
        _counter_add("scalar_fallback_sources", len(fallback_unique))
        _timing_add("reference_scalar_fallback", phase_started)

    return results


def apply_aperture_correction(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    frame_ids: list[int] | None = None,
    instrument_unit_id: int | None = None,
    reference_radius_pix: float = 5.0,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    max_calibrator_mag_err: float | None = 0.05,
    min_calibrators_per_frame: int = 20,
    min_plane_calibrators: int = 30,
    clip_sigma: float = 3.5,
    n_iterations: int = 3,
    min_plane_bic_improvement: float = 6.0,
    min_spatial_peak_to_peak: float = 0.005,
    plane_selection_policy: str = "evidence",
    max_residual_mad: float | None = 0.05,
    max_abs_correction: float = 0.5,
    max_abs_spatial_deviation: float = 0.15,
    max_abs_total_correction: float = 0.75,
    min_good_frame_fraction: float = 0.8,
    max_global_median_residual_mad: float | None = 0.05,
    fwhm_header_unit: str = "arcsec",
    psf_shape_radius_pix: float = 6.0,
    out_dir: str | Path | None = None,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    immutable_generation: bool = False,
    materialize_measurements: bool = True,
    diagnose_performance: bool = False,
) -> ApertureCorrectionResult:
    """Correct small-aperture measurements using a per-frame spatial aperture model.

    Bright, unsaturated measurements are remeasured with ``reference_radius_pix``.
    The difference ``m_large - m_small`` is fitted either by a constant or by a
    plane in normalized detector coordinates.  The corrected instrumental value is
    ``m_small + correction(x, y)``.  A later frame zero point absorbs the constant
    term; the scientifically relevant part here is the spatial dependence.
    """

    _function_t0 = time.perf_counter() if diagnose_performance else 0.0
    timings: dict[str, float] = {}

    if reference_radius_pix <= 0:
        raise ValueError("reference_radius_pix must be positive")
    if annulus_inner_pix <= reference_radius_pix:
        raise ValueError("annulus_inner_pix must be greater than reference_radius_pix")
    if annulus_outer_pix <= annulus_inner_pix:
        raise ValueError("annulus_outer_pix must be greater than annulus_inner_pix")
    if min_calibrators_per_frame < 3:
        raise ValueError("min_calibrators_per_frame must be >= 3")
    if min_plane_calibrators < 3:
        raise ValueError("min_plane_calibrators must be >= 3")
    if clip_sigma <= 0 or n_iterations < 1:
        raise ValueError("clip_sigma must be positive and n_iterations must be >= 1")
    if psf_shape_radius_pix < 2.0:
        raise ValueError("psf_shape_radius_pix must be >= 2")
    if max_abs_correction <= 0:
        raise ValueError("max_abs_correction must be positive")
    if max_abs_spatial_deviation <= 0:
        raise ValueError("max_abs_spatial_deviation must be positive")
    if max_abs_total_correction <= 0:
        raise ValueError("max_abs_total_correction must be positive")
    if str(fwhm_header_unit or "arcsec").lower() not in {"arcsec", "pix", "auto"}:
        raise ValueError("fwhm_header_unit must be arcsec, pix, or auto")
    plane_selection_policy = str(plane_selection_policy or "evidence").strip().lower()
    if plane_selection_policy not in {"evidence", "safety_only"}:
        raise ValueError("plane_selection_policy must be evidence or safety_only")

    result = ApertureCorrectionResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        instrument_unit_id=instrument_unit_id,
        reference_radius_pix=float(reference_radius_pix),
        dry_run=bool(dry_run),
        insert_batch_size=int(insert_batch_size),
        performance_diagnostics_enabled=bool(diagnose_performance),
        timings_sec=timings,
    )

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = int(source_cv.calibration_version_id)
    if diagnose_performance:
        timings["source_calibration_lookup"] = time.perf_counter() - _perf_t0

    target_cv = None
    if not dry_run:
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        target_cv = _get_or_create_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            immutable_generation=immutable_generation,
            parameters={
                "reference_radius_pix": reference_radius_pix,
                "annulus_inner_pix": annulus_inner_pix,
                "annulus_outer_pix": annulus_outer_pix,
                "max_calibrator_mag_err": max_calibrator_mag_err,
                "min_calibrators_per_frame": min_calibrators_per_frame,
                "min_plane_calibrators": min_plane_calibrators,
                "clip_sigma": clip_sigma,
                "n_iterations": n_iterations,
                "min_plane_bic_improvement": min_plane_bic_improvement,
                "min_spatial_peak_to_peak": min_spatial_peak_to_peak,
                "plane_selection_policy": plane_selection_policy,
                "max_residual_mad": max_residual_mad,
                "max_abs_correction": max_abs_correction,
                "max_abs_spatial_deviation": max_abs_spatial_deviation,
                "max_abs_total_correction": max_abs_total_correction,
                "min_good_frame_fraction": min_good_frame_fraction,
                "max_global_median_residual_mad": max_global_median_residual_mad,
                "fwhm_header_unit": fwhm_header_unit,
                "psf_shape_radius_pix": psf_shape_radius_pix,
            },
        )
        session.flush()
        result.target_calibration_version_id = int(target_cv.calibration_version_id)
        if diagnose_performance:
            timings["target_calibration_prepare"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    compact_normalized_only = bool(not materialize_measurements)
    source_rows, frames = _load_aperture_source_rows_scalar(
        session,
        source_calibration_version_id=int(source_cv.calibration_version_id),
        frame_ids=frame_ids,
        instrument_unit_id=instrument_unit_id,
        compact_normalized_only=compact_normalized_only,
    )
    result.source_read_strategy = (
        "SCALAR_COMPACT_NORMALIZED_PLUS_FRAME_LOOKUP_V2"
        if compact_normalized_only
        else "SCALAR_MEASUREMENTS_PLUS_FRAME_LOOKUP_V1"
    )
    result.normalized_only_fast_path = bool(compact_normalized_only)
    result.output_prepare_strategy = (
        "COUNTS_ONLY_NORMALIZED_V1"
        if compact_normalized_only
        else "FULL_MEASUREMENT_ROWS_V1"
    )
    if diagnose_performance:
        timings["load_source_measurements"] = time.perf_counter() - _perf_t0
    result.selected_measurements = len(source_rows)

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    grouped: dict[int, list[Any]] = {}
    for measurement in source_rows:
        grouped.setdefault(int(measurement.frame_id), []).append(measurement)
    result.selected_frames = len(frames)
    if diagnose_performance:
        timings["group_source_measurements"] = time.perf_counter() - _perf_t0

    frame_models: dict[int, dict[str, Any]] = {}
    frame_stats: list[ApertureCorrectionFrameStat] = []
    frame_contexts: list[dict[str, Any]] = []

    # Lazy imports keep pure model-selection tests independent of Astropy.
    from astropy.io import fits

    _frame_loop_t0 = time.perf_counter() if diagnose_performance else 0.0
    _fits_read_sec = 0.0
    _reference_measurement_sec = 0.0
    _psf_shape_sec = 0.0
    _frame_model_fit_sec = 0.0
    _frame_context_sec = 0.0
    _reference_measurement_calls = 0
    _psf_shape_calls = 0
    _reference_batch_counters: dict[str, int] = {}
    result.reference_measure_strategy = "VECTORIZED_INTERIOR_BATCH_V1"

    for frame_id in sorted(frames):
        result.checked_frames += 1
        frame = frames[frame_id]
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            model = _identity_model("missing FITS file")
            frame_models[frame_id] = model
            frame_stats.append(_frame_stat_from_model(frame_id, model))
            frame_contexts.append(
                build_frame_aperture_context(
                    frame=frame, header=None, model=model, candidates=[],
                    fwhm_header_unit=fwhm_header_unit, error="missing FITS file",
                )
            )
            continue
        try:
            _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
                header = hdul[0].header
            if data.ndim != 2:
                raise ValueError("FITS primary data are not 2-D")
            if diagnose_performance:
                _fits_read_sec += time.perf_counter() - _perf_t0
        except Exception as exc:
            result.failed_frames += 1
            model = _identity_model(f"failed to read frame: {type(exc).__name__}")
            frame_models[frame_id] = model
            frame_stats.append(_frame_stat_from_model(frame_id, model))
            frame_contexts.append(
                build_frame_aperture_context(
                    frame=frame, header=None, model=model, candidates=[],
                    fwhm_header_unit=fwhm_header_unit,
                    error=f"failed to read frame: {type(exc).__name__}",
                )
            )
            continue

        frame_saturation = _resolve_saturation_level(frame, header)
        exptime = _safe_positive_float(frame.exptime)
        candidates: list[dict[str, float]] = []
        eligible_measurements = [
            measurement
            for measurement in grouped.get(frame_id, [])
            if _eligible_calibrator(measurement, max_mag_err=max_calibrator_mag_err)
        ]
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        large_measurements = _measure_reference_apertures_batch(
            data,
            eligible_measurements,
            aperture_radius_pix=float(reference_radius_pix),
            annulus_inner_pix=float(annulus_inner_pix),
            annulus_outer_pix=float(annulus_outer_pix),
            exptime=exptime,
            saturation_level_adu=frame_saturation,
            performance_timings=(timings if diagnose_performance else None),
            performance_counters=_reference_batch_counters,
        )
        if diagnose_performance:
            _reference_measurement_sec += time.perf_counter() - _perf_t0
            _reference_measurement_calls += len(eligible_measurements)
        for measurement, large in zip(eligible_measurements, large_measurements, strict=True):
            if large is None:
                result.failed_reference_measurements += 1
                continue
            if large.mag_inst is None or not math.isfinite(float(large.mag_inst)):
                result.failed_reference_measurements += 1
                continue
            if any(bool(large.flags.get(key)) for key in (
                "saturated_or_near_saturated",
                "aperture_touches_x_edge",
                "aperture_touches_y_edge",
                "annulus_touches_x_edge",
                "annulus_touches_y_edge",
                "non_positive_flux",
            )):
                continue
            delta = float(large.mag_inst) - float(measurement.mag_inst)
            if not math.isfinite(delta) or abs(delta) > float(max_abs_correction):
                continue
            candidate = {"x": float(measurement.x), "y": float(measurement.y), "delta": delta}
            _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
            shape = estimate_psf_shape(
                data,
                x=float(measurement.x),
                y=float(measurement.y),
                background=large.local_background,
                radius_pix=float(psf_shape_radius_pix),
            )
            if diagnose_performance:
                _psf_shape_sec += time.perf_counter() - _perf_t0
                _psf_shape_calls += 1
            if shape:
                candidate.update(shape)
            candidates.append(candidate)

        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        model = fit_aperture_correction_model(
            candidates,
            image_width=int(data.shape[1]),
            image_height=int(data.shape[0]),
            min_calibrators=min_calibrators_per_frame,
            min_plane_calibrators=min_plane_calibrators,
            clip_sigma=clip_sigma,
            n_iterations=n_iterations,
            min_plane_bic_improvement=min_plane_bic_improvement,
            min_spatial_peak_to_peak=min_spatial_peak_to_peak,
            plane_selection_policy=plane_selection_policy,
            max_residual_mad=max_residual_mad,
            max_abs_correction=max_abs_correction,
            max_abs_spatial_deviation=max_abs_spatial_deviation,
            max_abs_total_correction=max_abs_total_correction,
        )
        if diagnose_performance:
            _frame_model_fit_sec += time.perf_counter() - _perf_t0
        frame_models[frame_id] = model
        frame_stats.append(_frame_stat_from_model(frame_id, model))
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        frame_contexts.append(
            build_frame_aperture_context(
                frame=frame, header=header, model=model, candidates=candidates,
                fwhm_header_unit=fwhm_header_unit,
            )
        )
        if diagnose_performance:
            _frame_context_sec += time.perf_counter() - _perf_t0

    result.reference_measure_batch_sources = int(_reference_batch_counters.get("batch_sources", 0))
    result.reference_measure_scalar_fallback_sources = int(
        _reference_batch_counters.get("scalar_fallback_sources", 0)
    )
    result.reference_measure_batch_chunks = int(_reference_batch_counters.get("batch_chunks", 0))
    if diagnose_performance:
        timings["frame_processing_total"] = time.perf_counter() - _frame_loop_t0
        timings["fits_read"] = _fits_read_sec
        timings["reference_measurement_kernel"] = _reference_measurement_sec
        timings["psf_shape"] = _psf_shape_sec
        timings["frame_model_fit"] = _frame_model_fit_sec
        timings["frame_context_build"] = _frame_context_sec
        result.throughput["reference_measurement_calls"] = float(_reference_measurement_calls)
        result.throughput["psf_shape_calls"] = float(_psf_shape_calls)
        if _reference_measurement_calls:
            result.throughput["reference_measurement_us_per_call"] = 1e6 * _reference_measurement_sec / _reference_measurement_calls

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    context_analysis = analyze_aperture_frame_context(frame_contexts)
    result.context_analysis = context_analysis
    result.context_analysis_status = context_analysis.get("status")
    result.context_frames_with_psf_shape = int(context_analysis.get("n_frames_with_psf_shape") or 0)
    result.context_plane_frames_with_psf_shape = int(
        context_analysis.get("n_plane_frames_with_psf_shape") or 0
    )
    result.context_frames_with_header_fwhm = int(context_analysis.get("n_frames_with_header_fwhm") or 0)
    result.context_plane_gradient_mean_angle_deg = _finite_or_none(
        context_analysis.get("plane_gradient_mean_angle_deg")
    )
    result.context_plane_gradient_resultant_length = _finite_or_none(
        context_analysis.get("plane_gradient_resultant_length")
    )
    result.context_plane_gradient_sign_consistency = _finite_or_none(
        context_analysis.get("plane_gradient_sign_consistency")
    )
    result.context_plane_gradient_psf_axis_delta_median_deg = _finite_or_none(
        context_analysis.get("plane_gradient_psf_axis_delta_median_deg")
    )
    result.context_plane_time_span_minutes = _finite_or_none(
        context_analysis.get("plane_time_span_minutes")
    )
    result.context_plane_candidate_frames = int(context_analysis.get("n_plane_candidate_frames") or 0)
    result.context_plane_candidate_gradient_mean_angle_deg = _finite_or_none(
        context_analysis.get("plane_candidate_gradient_mean_angle_deg")
    )
    result.context_plane_candidate_gradient_resultant_length = _finite_or_none(
        context_analysis.get("plane_candidate_gradient_resultant_length")
    )
    result.context_plane_candidate_gradient_sign_consistency = _finite_or_none(
        context_analysis.get("plane_candidate_gradient_sign_consistency")
    )

    accepted_stats = [stat for stat in frame_stats if stat.accepted]
    result.frames_with_model = len(accepted_stats)
    result.plane_frame_ids = sorted(
        stat.frame_id for stat in accepted_stats if stat.selected_model == "plane"
    )
    result.frames_with_plane = len(result.plane_frame_ids)
    result.legacy_plane_frame_ids = sorted(
        stat.frame_id for stat in accepted_stats if stat.legacy_selected_model == "plane"
    )
    result.legacy_frames_with_plane = len(result.legacy_plane_frame_ids)
    result.promoted_to_plane_frame_ids = sorted(
        stat.frame_id
        for stat in accepted_stats
        if stat.selected_model == "plane" and stat.legacy_selected_model != "plane"
    )
    result.frames_promoted_to_plane_vs_legacy = len(result.promoted_to_plane_frame_ids)
    result.demoted_from_plane_frame_ids = sorted(
        stat.frame_id
        for stat in accepted_stats
        if stat.selected_model != "plane" and stat.legacy_selected_model == "plane"
    )
    result.frames_demoted_from_plane_vs_legacy = len(result.demoted_from_plane_frame_ids)
    result.frames_with_constant = sum(1 for stat in accepted_stats if stat.selected_model == "constant")
    result.frames_identity_fallback = len(frame_stats) - len(accepted_stats)
    result.good_frame_fraction = (len(accepted_stats) / len(frame_stats)) if frame_stats else 0.0
    result.median_residual_mad = _median([stat.residual_mad for stat in accepted_stats])
    result.median_spatial_peak_to_peak = _median([stat.spatial_peak_to_peak for stat in accepted_stats])
    result.median_plane_spatial_peak_to_peak = _median(
        [stat.spatial_peak_to_peak for stat in accepted_stats if stat.selected_model == "plane"]
    )
    result.median_plane_max_abs_spatial_deviation = _median(
        [
            stat.plane_candidate_max_abs_spatial_deviation
            for stat in accepted_stats
            if stat.selected_model == "plane"
        ]
    )
    result.quality_gate_thresholds = {
        "min_good_frame_fraction": min_good_frame_fraction,
        "max_global_median_residual_mad": max_global_median_residual_mad,
        "legacy_max_abs_correction": max_abs_correction,
        "max_abs_spatial_deviation": max_abs_spatial_deviation,
        "max_abs_total_correction": max_abs_total_correction,
    }
    if result.good_frame_fraction < float(min_good_frame_fraction):
        result.quality_gate_failures.append(
            f"good frame fraction {result.good_frame_fraction:.3f} < {float(min_good_frame_fraction):.3f}"
        )
    if (
        max_global_median_residual_mad is not None
        and result.median_residual_mad is not None
        and result.median_residual_mad > float(max_global_median_residual_mad)
    ):
        result.quality_gate_failures.append(
            f"median residual MAD {result.median_residual_mad:.5f} > {float(max_global_median_residual_mad):.5f}"
        )
    result.quality_gate_passed = not result.quality_gate_failures and bool(accepted_stats)
    if diagnose_performance:
        timings["context_analysis_and_quality"] = time.perf_counter() - _perf_t0

    _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
    rows_to_insert: list[dict[str, Any]] = []
    if not materialize_measurements:
        # Normalized-only calibrations persist the fitted frame terms below; no
        # per-measurement aperture-correction payload is written.  The legacy
        # path still built corrected flux/error values and large flags dicts for
        # every source only to discard them.  Preserve the public calibrated /
        # copied-identity counters with the minimum model applicability check.
        for measurement in source_rows:
            model = frame_models.get(int(measurement.frame_id)) or _identity_model("frame model missing")
            correction = predict_aperture_correction(model, measurement.x, measurement.y)
            if bool(model.get("accepted")) and correction is not None:
                result.calibrated += 1
            else:
                result.copied_identity += 1
        if diagnose_performance:
            timings["prepare_output_counts"] = time.perf_counter() - _perf_t0
            timings["prepare_output_rows"] = 0.0
    else:
        for measurement in source_rows:
            model = frame_models.get(int(measurement.frame_id)) or _identity_model("frame model missing")
            correction = predict_aperture_correction(model, measurement.x, measurement.y)
            applied = bool(model.get("accepted")) and correction is not None
            if not applied:
                correction = 0.0
                result.copied_identity += 1
            else:
                result.calibrated += 1
            correction = float(correction or 0.0)
            scale = 10.0 ** (-0.4 * correction)
            corrected_flux = _scaled(measurement.flux, scale)
            corrected_flux_err = _scaled(measurement.flux_err, abs(scale))
            corrected_mag_inst = _add(measurement.mag_inst, correction)
            source_random_err = _first_finite(measurement.mag_err_random, measurement.mag_err)
            model_err_total = aperture_correction_model_error(model, measurement.x, measurement.y) if applied else None
            differential_err = (
                aperture_correction_differential_error(model, measurement.x, measurement.y) if applied else 0.0
            )
            random_err = aperture_corrected_random_error(
                source_random_err, model=model, x=measurement.x, y=measurement.y
            )
            systematic_err = _finite_or_none(measurement.mag_err_systematic)
            flags = dict(measurement.flags or {})
            flags.update(
                {
                    "calibration_stage": "spatial_aperture_correction",
                    "source_calibration_version_id": source_cv.calibration_version_id,
                    "source_calibration_name": source_cv.name,
                    "aperture_correction_applied": applied,
                    "aperture_correction_mag": correction,
                    "aperture_correction_model": model.get("selected_model"),
                    "aperture_correction_reference_radius_pix": reference_radius_pix,
                    "aperture_correction_n_calibrators": model.get("n_used"),
                    "aperture_correction_residual_mad": model.get("residual_mad"),
                    "aperture_correction_residual_robust_sigma": model.get("residual_robust_sigma"),
                    "aperture_correction_spatial_peak_to_peak": model.get("spatial_peak_to_peak"),
                    "aperture_correction_max_abs_spatial_deviation": model.get(
                        "max_abs_spatial_deviation"
                    ),
                    "aperture_correction_max_abs_total_correction": model.get(
                        "max_abs_predicted_correction"
                    ),
                    "aperture_correction_model_selection_semantics": (
                        "separated_spatial_total_safety_v2"
                    ),
                    "aperture_correction_legacy_selected_model": model.get(
                        "legacy_selected_model"
                    ),
                    "aperture_correction_selection_changed_vs_legacy": bool(
                        model.get("selection_changed_vs_legacy")
                    ),
                    "aperture_correction_legacy_selection_reason": model.get(
                        "legacy_selection_reason"
                    ),
                    # Total prediction uncertainty is retained for diagnostics only.
                    # The frame-common intercept term is absorbed by downstream ZP/ensemble offsets.
                    "aperture_correction_model_error": model_err_total,
                    "aperture_correction_model_error_total": model_err_total,
                    "aperture_correction_differential_error": differential_err,
                    "aperture_correction_error_propagation": "differential_spatial_only_v1",
                    "aperture_correction_fallback_reason": model.get("fallback_reason"),
                }
            )
            if dry_run or target_cv is None:
                continue
            rows_to_insert.append(
                {
                    "object_id": measurement.object_id,
                    "frame_id": measurement.frame_id,
                    "calibration_version_id": target_cv.calibration_version_id,
                    "x": measurement.x,
                    "y": measurement.y,
                    "flux": corrected_flux,
                    "flux_err": corrected_flux_err,
                    "mag_inst": corrected_mag_inst,
                    "mag_cal": None,
                    "mag_err": random_err,
                    "mag_err_random": random_err,
                    "mag_err_systematic": systematic_err,
                    "aperture_radius_pix": measurement.aperture_radius_pix,
                    "local_background": measurement.local_background,
                    "local_zp_correction": measurement.local_zp_correction,
                    "centroid_dx_pix": measurement.centroid_dx_pix,
                    "centroid_dy_pix": measurement.centroid_dy_pix,
                    "flags": flags,
                }
            )
        if diagnose_performance:
            timings["prepare_output_rows"] = time.perf_counter() - _perf_t0

    if not dry_run and target_cv is not None:
        if materialize_measurements:
            _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
            result.deleted_existing = _delete_existing_target_rows(
                session,
                target_calibration_version_id=int(target_cv.calibration_version_id),
                frame_ids=frame_ids,
                instrument_unit_id=instrument_unit_id,
            )
            if rows_to_insert:
                result.insert_batches = upsert_measurement_rows(
                    session,
                    rows_to_insert,
                    insert_batch_size=insert_batch_size,
                )
                result.inserted_or_updated = len(rows_to_insert)
            if diagnose_performance:
                timings["materialized_measurement_write"] = time.perf_counter() - _perf_t0
        params = dict(target_cv.parameters or {})
        params.update(
            {
                "quality_gate_passed": result.quality_gate_passed,
                "quality_gate_failures": result.quality_gate_failures,
                "good_frame_fraction": result.good_frame_fraction,
                "frames_with_model": result.frames_with_model,
                "frames_with_plane": result.frames_with_plane,
                "frames_with_constant": result.frames_with_constant,
                "frames_identity_fallback": result.frames_identity_fallback,
                "median_residual_mad": result.median_residual_mad,
                "median_spatial_peak_to_peak": result.median_spatial_peak_to_peak,
                "median_plane_spatial_peak_to_peak": result.median_plane_spatial_peak_to_peak,
                "error_propagation": "differential_spatial_only_v1",
                "context_analysis_algorithm": context_analysis.get("algorithm"),
                "context_analysis_status": result.context_analysis_status,
                "context_frames_with_psf_shape": result.context_frames_with_psf_shape,
                "context_plane_frames_with_psf_shape": result.context_plane_frames_with_psf_shape,
                "context_frames_with_header_fwhm": result.context_frames_with_header_fwhm,
                "context_plane_gradient_mean_angle_deg": result.context_plane_gradient_mean_angle_deg,
                "context_plane_gradient_resultant_length": result.context_plane_gradient_resultant_length,
                "context_plane_gradient_sign_consistency": result.context_plane_gradient_sign_consistency,
                "context_plane_gradient_psf_axis_delta_median_deg": (
                    result.context_plane_gradient_psf_axis_delta_median_deg
                ),
                "context_plane_time_span_minutes": result.context_plane_time_span_minutes,
            }
        )
        target_cv.parameters = params
        target_cv.notes = (
            "Per-frame small-to-large aperture correction. Spatial plane is selected only when supported; "
            "otherwise a constant or identity fallback is used."
        )
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        normalized = replace_calibration_iteration(
            session,
            calibration_version_id=int(target_cv.calibration_version_id),
            source_calibration_version_id=int(source_cv.calibration_version_id),
            iteration_kind="APERTURE_CORRECTION",
            iteration_no=1,
            algorithm="spatial_aperture_correction_v2",
            status="COMPLETE" if result.quality_gate_passed else "COMPLETE_WITH_WARNINGS",
            convergence_status="QUALITY_GATE_PASSED" if result.quality_gate_passed else "QUALITY_GATE_FAILED",
            convergence_reason=(
                None if result.quality_gate_passed else "; ".join(result.quality_gate_failures)
            ),
            parameters={
                "reference_radius_pix": reference_radius_pix,
                "annulus_inner_pix": annulus_inner_pix,
                "annulus_outer_pix": annulus_outer_pix,
                "min_calibrators_per_frame": min_calibrators_per_frame,
                "min_plane_calibrators": min_plane_calibrators,
                "clip_sigma": clip_sigma,
                "n_iterations": n_iterations,
                "min_plane_bic_improvement": min_plane_bic_improvement,
                "min_spatial_peak_to_peak": min_spatial_peak_to_peak,
                "max_residual_mad": max_residual_mad,
                "max_abs_spatial_deviation": max_abs_spatial_deviation,
                "max_abs_total_correction": max_abs_total_correction,
                "application_semantics": "mag_inst_out = mag_inst_in + correction(x,y)",
                "coordinate_normalization": "xnorm=2*x/(image_width-1)-1; ynorm=2*y/(image_height-1)-1",
                "error_semantics": "differential_spatial_only_v1",
            },
            metrics={
                "good_frame_fraction": result.good_frame_fraction,
                "frames_with_model": result.frames_with_model,
                "frames_with_plane": result.frames_with_plane,
                "frames_with_constant": result.frames_with_constant,
                "frames_identity_fallback": result.frames_identity_fallback,
                "median_residual_mad": result.median_residual_mad,
                "median_spatial_peak_to_peak": result.median_spatial_peak_to_peak,
                "quality_gate_passed": result.quality_gate_passed,
                "quality_gate_failures": list(result.quality_gate_failures),
            },
            frame_terms=[
                FrameTermInput(
                    frame_id=stat.frame_id,
                    term_kind="APERTURE_CORRECTION",
                    model_name=stat.selected_model,
                    coefficient_0=stat.coefficient_0,
                    coefficient_x=stat.coefficient_x,
                    coefficient_y=stat.coefficient_y,
                    scatter_mad=stat.residual_mad,
                    scatter_rms=stat.residual_rms,
                    n_candidates=stat.n_candidates,
                    n_used=stat.n_used,
                    accepted=stat.accepted,
                    quality_status="ACCEPTED" if stat.accepted else "IDENTITY_FALLBACK",
                    details={
                        "selection_reason": stat.selection_reason,
                        "fallback_reason": stat.fallback_reason,
                        "image_width": (frame_models.get(int(stat.frame_id)) or {}).get("image_width"),
                        "image_height": (frame_models.get(int(stat.frame_id)) or {}).get("image_height"),
                        "pivot_xnorm": (frame_models.get(int(stat.frame_id)) or {}).get("pivot_xnorm"),
                        "pivot_ynorm": (frame_models.get(int(stat.frame_id)) or {}).get("pivot_ynorm"),
                        "covariance": (frame_models.get(int(stat.frame_id)) or {}).get("covariance"),
                        "plane_candidate_coefficients": (frame_models.get(int(stat.frame_id)) or {}).get("plane_candidate_coefficients"),
                        "plane_candidate_covariance": (frame_models.get(int(stat.frame_id)) or {}).get("plane_candidate_covariance"),
                        "plane_candidate_residual_mad": (frame_models.get(int(stat.frame_id)) or {}).get("plane_candidate_residual_mad"),
                        "legacy_selection_reason": (frame_models.get(int(stat.frame_id)) or {}).get("legacy_selection_reason"),
                        "max_abs_spatial_deviation": (frame_models.get(int(stat.frame_id)) or {}).get("max_abs_spatial_deviation"),
                        "max_abs_predicted_correction": (frame_models.get(int(stat.frame_id)) or {}).get("max_abs_predicted_correction"),
                        "correction_median": stat.correction_median,
                        "correction_mad": stat.correction_mad,
                        "residual_robust_sigma": stat.residual_robust_sigma,
                        "constant_bic": stat.constant_bic,
                        "plane_bic": stat.plane_bic,
                        "plane_bic_improvement": stat.plane_bic_improvement,
                        "spatial_peak_to_peak": stat.spatial_peak_to_peak,
                        "plane_candidate_max_abs_spatial_deviation": stat.plane_candidate_max_abs_spatial_deviation,
                        "plane_candidate_max_abs_total_correction": stat.plane_candidate_max_abs_total_correction,
                        "legacy_selected_model": stat.legacy_selected_model,
                        "selection_changed_vs_legacy": stat.selection_changed_vs_legacy,
                        "model_error_median": stat.model_error_median,
                        "differential_error_median": stat.differential_error_median,
                    },
                )
                for stat in frame_stats
            ],
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

    if out_dir is not None and not dry_run:
        _perf_t0 = time.perf_counter() if diagnose_performance else 0.0
        summary_path, frames_path = export_aperture_correction_diagnostics(
            result,
            frame_stats,
            context_analysis=context_analysis,
            frame_contexts=frame_contexts,
            out_dir=out_dir,
        )
        context_summary_path, context_frames_path, context_plot_path = export_aperture_context_diagnostics(
            rows=frame_contexts,
            analysis=context_analysis,
            out_dir=out_dir,
        )
        result.diagnostics_summary_json = str(summary_path)
        result.diagnostics_frames_csv = str(frames_path)
        result.diagnostics_context_summary_json = str(context_summary_path)
        result.diagnostics_context_frames_csv = str(context_frames_path)
        result.diagnostics_context_plot_png = str(context_plot_path) if context_plot_path else None
        if context_plot_path is None:
            result.warnings.append("aperture context plot was not generated; Matplotlib may be unavailable")
        if diagnose_performance:
            timings["diagnostics_export"] = time.perf_counter() - _perf_t0

    if diagnose_performance:
        timings["function_total"] = time.perf_counter() - _function_t0
        result.throughput["selected_measurements_per_sec_total"] = result.selected_measurements / max(timings["function_total"], 1e-12)
    return result


def fit_aperture_correction_model(
    rows: list[dict[str, float]],
    *,
    image_width: int,
    image_height: int,
    min_calibrators: int = 20,
    min_plane_calibrators: int = 30,
    clip_sigma: float = 3.5,
    n_iterations: int = 3,
    min_plane_bic_improvement: float = 6.0,
    min_spatial_peak_to_peak: float = 0.005,
    plane_selection_policy: str = "evidence",
    max_residual_mad: float | None = 0.05,
    max_abs_correction: float = 0.5,
    max_abs_spatial_deviation: float = 0.15,
    max_abs_total_correction: float = 0.75,
) -> dict[str, Any]:
    plane_selection_policy = str(plane_selection_policy or "evidence").strip().lower()
    if plane_selection_policy not in {"evidence", "safety_only"}:
        raise ValueError("plane_selection_policy must be evidence or safety_only")

    finite = []
    for row in rows:
        try:
            x, y, delta = float(row["x"]), float(row["y"]), float(row["delta"])
        except Exception:
            continue
        if math.isfinite(x) and math.isfinite(y) and math.isfinite(delta) and abs(delta) <= max_abs_correction:
            finite.append((x, y, delta))
    if len(finite) < min_calibrators:
        model = _identity_model(f"only {len(finite)} calibrators; need {min_calibrators}")
        model.update({"n_candidates": len(finite), "n_used": len(finite)})
        return model

    arr = np.asarray(finite, dtype=float)
    xnorm = _normalize_axis(arr[:, 0], image_width)
    ynorm = _normalize_axis(arr[:, 1], image_height)
    delta = arr[:, 2]
    keep = np.ones(delta.size, dtype=bool)
    for _ in range(max(1, int(n_iterations))):
        current = delta[keep]
        if current.size < min_calibrators:
            break
        med = float(np.median(current))
        mad = _np_mad(current)
        if not math.isfinite(mad) or mad <= 0:
            break
        new_keep = np.abs(delta - med) <= float(clip_sigma) * 1.4826 * mad
        if np.array_equal(new_keep, keep):
            break
        if int(new_keep.sum()) < min_calibrators:
            break
        keep = new_keep

    x = xnorm[keep]
    y = ynorm[keep]
    z = delta[keep]
    n = int(z.size)
    if n < min_calibrators:
        model = _identity_model(f"only {n} calibrators after clipping; need {min_calibrators}")
        model.update({"n_candidates": len(finite), "n_used": n})
        return model

    constant_value = float(np.median(z))
    constant_residual = z - constant_value
    constant_metrics = _residual_metrics(constant_residual, n_params=1)
    constant_cov = np.asarray([[max(constant_metrics["residual_rms"] or 0.0, 1e-12) ** 2 / max(n, 1)]])

    plane_coeff = None
    plane_cov = None
    plane_metrics = None
    plane_spatial_ptp = 0.0
    plane_pivot_correction = None
    plane_max_abs_spatial_deviation = None
    plane_max_abs_total_correction = None
    if n >= min_plane_calibrators:
        design = np.column_stack([np.ones(n), x, y])
        try:
            plane_coeff, _, _, _ = np.linalg.lstsq(design, z, rcond=None)
            plane_residual = z - design @ plane_coeff
            plane_metrics = _residual_metrics(plane_residual, n_params=3)
            sigma2 = float(np.sum(plane_residual**2) / max(n - 3, 1))
            plane_cov = sigma2 * np.linalg.pinv(design.T @ design)
            corners = np.asarray([[1.0, -1.0, -1.0], [1.0, -1.0, 1.0], [1.0, 1.0, -1.0], [1.0, 1.0, 1.0]])
            predictions = corners @ plane_coeff
            plane_spatial_ptp = float(np.max(predictions) - np.min(predictions))
            pivot_vec = np.asarray([1.0, float(np.mean(x)), float(np.mean(y))])
            plane_pivot_correction = float(pivot_vec @ plane_coeff)
            plane_max_abs_spatial_deviation = float(
                np.max(np.abs(predictions - plane_pivot_correction))
            )
            plane_max_abs_total_correction = float(np.max(np.abs(predictions)))
        except Exception:
            plane_coeff = None
            plane_cov = None
            plane_metrics = None

    selected_model = "constant"
    reason_parts: list[str] = []
    statistical_rejection_reasons: list[str] = []
    bic_improvement = None
    if plane_metrics is not None and plane_coeff is not None:
        bic_improvement = float(constant_metrics["bic"] - plane_metrics["bic"])
        if bic_improvement < min_plane_bic_improvement:
            statistical_rejection_reasons.append(
                f"BIC improvement {bic_improvement:.3f} < {min_plane_bic_improvement:.3f}"
            )
        if plane_spatial_ptp < min_spatial_peak_to_peak:
            statistical_rejection_reasons.append(
                f"spatial peak-to-peak {plane_spatial_ptp:.5f} < {min_spatial_peak_to_peak:.5f} mag"
            )
        if (plane_metrics["residual_mad"] or math.inf) > (constant_metrics["residual_mad"] or math.inf):
            statistical_rejection_reasons.append("plane residual MAD is worse than constant model")
        safety_rejection_reasons: list[str] = []
        if (
            plane_max_abs_spatial_deviation is not None
            and plane_max_abs_spatial_deviation > max_abs_spatial_deviation
        ):
            safety_rejection_reasons.append(
                "plane max absolute spatial deviation "
                f"{plane_max_abs_spatial_deviation:.5f} exceeds {max_abs_spatial_deviation:.5f} mag"
            )
        if (
            plane_max_abs_total_correction is not None
            and plane_max_abs_total_correction > max_abs_total_correction
        ):
            safety_rejection_reasons.append(
                "plane max absolute total correction "
                f"{plane_max_abs_total_correction:.5f} exceeds {max_abs_total_correction:.5f} mag"
            )
        if plane_selection_policy == "evidence":
            reason_parts.extend(statistical_rejection_reasons)
        else:
            if statistical_rejection_reasons:
                reason_parts.append(
                    "statistical model preference ignored by safety_only policy: "
                    + "; ".join(statistical_rejection_reasons)
                )
        reason_parts.extend(safety_rejection_reasons)
        if not safety_rejection_reasons and (
            plane_selection_policy == "safety_only" or not statistical_rejection_reasons
        ):
            selected_model = "plane"
            if plane_selection_policy == "safety_only":
                reason_parts.append(
                    "plane selected by safety_only policy after spatial/total safety gates"
                )
            else:
                reason_parts.append(
                    "plane selected by statistical evidence and separated spatial/total safety gates"
                )
    else:
        reason_parts.append(f"plane unavailable with {n} usable calibrators")

    legacy_selected_model = "constant"
    legacy_reasons = list(statistical_rejection_reasons)
    if plane_metrics is None or plane_coeff is None:
        legacy_reasons = [f"plane unavailable with {n} usable calibrators"]
    elif (
        plane_max_abs_total_correction is not None
        and plane_max_abs_total_correction > max_abs_correction
    ):
        legacy_reasons.append(
            "legacy plane edge correction "
            f"{plane_max_abs_total_correction:.5f} exceeds {max_abs_correction:.5f} mag"
        )
    if plane_metrics is not None and plane_coeff is not None and not legacy_reasons:
        legacy_selected_model = "plane"
        legacy_reasons.append("plane selected by legacy combined edge-correction gate")

    if selected_model == "plane":
        coeff = np.asarray(plane_coeff, dtype=float)
        cov = np.asarray(plane_cov, dtype=float) if plane_cov is not None else None
        metrics = plane_metrics or constant_metrics
        spatial_ptp = plane_spatial_ptp
    else:
        coeff = np.asarray([constant_value, 0.0, 0.0], dtype=float)
        cov = np.zeros((3, 3), dtype=float)
        cov[0, 0] = constant_cov[0, 0]
        metrics = constant_metrics
        spatial_ptp = 0.0

    residual_mad = metrics["residual_mad"]
    accepted = True
    fallback_reason = None
    if max_residual_mad is not None and residual_mad is not None and residual_mad > max_residual_mad:
        accepted = False
        fallback_reason = f"residual MAD {residual_mad:.5f} > {max_residual_mad:.5f}"
    if selected_model == "constant" and abs(float(coeff[0])) > max_abs_correction:
        accepted = False
        fallback_reason = f"median correction {float(coeff[0]):.5f} exceeds {max_abs_correction:.5f} mag"

    model = {
        "accepted": bool(accepted),
        "selected_model": selected_model if accepted else "identity",
        "selection_reason": "; ".join(reason_parts),
        "fallback_reason": fallback_reason,
        "n_candidates": len(finite),
        "n_used": n,
        "image_width": int(image_width),
        "image_height": int(image_height),
        "pivot_xnorm": float(np.mean(x)),
        "pivot_ynorm": float(np.mean(y)),
        "coefficients": [float(v) for v in coeff],
        "covariance": cov.tolist() if cov is not None else None,
        "correction_median": float(np.median(z)),
        "correction_mad": _np_mad(z),
        "residual_rms": metrics["residual_rms"],
        "residual_mad": metrics["residual_mad"],
        "residual_robust_sigma": metrics["residual_robust_sigma"],
        "constant_bic": constant_metrics["bic"],
        "plane_bic": plane_metrics["bic"] if plane_metrics is not None else None,
        "plane_bic_improvement": bic_improvement,
        "plane_candidate_coefficients": (
            [float(v) for v in plane_coeff] if plane_coeff is not None else None
        ),
        "plane_candidate_covariance": plane_cov.tolist() if plane_cov is not None else None,
        "plane_candidate_residual_rms": (
            plane_metrics["residual_rms"] if plane_metrics is not None else None
        ),
        "plane_candidate_residual_mad": (
            plane_metrics["residual_mad"] if plane_metrics is not None else None
        ),
        "plane_candidate_residual_robust_sigma": (
            plane_metrics["residual_robust_sigma"] if plane_metrics is not None else None
        ),
        "plane_candidate_spatial_peak_to_peak": (
            plane_spatial_ptp if plane_metrics is not None else None
        ),
        "plane_candidate_pivot_correction": plane_pivot_correction,
        "plane_candidate_max_abs_spatial_deviation": plane_max_abs_spatial_deviation,
        "plane_candidate_max_abs_total_correction": plane_max_abs_total_correction,
        "plane_candidate_max_abs_correction": plane_max_abs_total_correction,
        "legacy_max_abs_correction": float(max_abs_correction),
        "max_abs_spatial_deviation_threshold": float(max_abs_spatial_deviation),
        "max_abs_total_correction_threshold": float(max_abs_total_correction),
        "plane_selection_policy": plane_selection_policy,
        "statistical_rejection_reasons": list(statistical_rejection_reasons),
        "legacy_selected_model": legacy_selected_model,
        "legacy_selection_reason": "; ".join(legacy_reasons),
        "selection_changed_vs_legacy": bool(selected_model != legacy_selected_model),
        "spatial_peak_to_peak": spatial_ptp,
        "max_abs_spatial_deviation": (
            plane_max_abs_spatial_deviation if selected_model == "plane" else 0.0
        ),
        "max_abs_predicted_correction": (
            plane_max_abs_total_correction if selected_model == "plane" else abs(float(coeff[0]))
        ),
    }
    prediction_errors = [aperture_correction_model_error(model, xx, yy) for xx, yy, _ in finite]
    differential_errors = [aperture_correction_differential_error(model, xx, yy) for xx, yy, _ in finite]
    model["model_error_median"] = _median(prediction_errors)
    model["differential_error_median"] = _median(differential_errors)
    return model


def predict_aperture_correction(model: dict[str, Any], x: float | None, y: float | None) -> float | None:
    if not model.get("accepted"):
        return None
    if x is None or y is None:
        return None
    try:
        coeff = [float(v) for v in model.get("coefficients", [])]
        xn = float(_normalize_axis(np.asarray([float(x)]), int(model["image_width"]))[0])
        yn = float(_normalize_axis(np.asarray([float(y)]), int(model["image_height"]))[0])
    except Exception:
        return None
    if len(coeff) < 3:
        return None
    value = coeff[0] + coeff[1] * xn + coeff[2] * yn
    return float(value) if math.isfinite(value) else None


def aperture_correction_model_error(model: dict[str, Any], x: float | None, y: float | None) -> float | None:
    if not model.get("accepted") or x is None or y is None:
        return None
    try:
        cov = np.asarray(model.get("covariance"), dtype=float)
        xn = float(_normalize_axis(np.asarray([float(x)]), int(model["image_width"]))[0])
        yn = float(_normalize_axis(np.asarray([float(y)]), int(model["image_height"]))[0])
        vec = np.asarray([1.0, xn, yn], dtype=float)
        prediction_var = max(0.0, float(vec @ cov @ vec)) if cov.shape == (3, 3) else 0.0
        n_used = max(int(model.get("n_used") or 1), 1)
        model_floor = float(model.get("residual_robust_sigma") or 0.0) / math.sqrt(n_used)
        value = math.sqrt(prediction_var + model_floor**2)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def aperture_correction_differential_error(
    model: dict[str, Any], x: float | None, y: float | None
) -> float | None:
    """Return only the position-dependent uncertainty relevant to differential photometry.

    Constant aperture corrections and identity fallbacks contribute no per-object random
    error: their frame-common offset is absorbed by the subsequent zero point and
    ensemble offset.  For a spatial plane, only slope uncertainty relative to the mean
    aperture-calibrator position is propagated; the intercept covariance and the
    frame-common model floor remain diagnostic quantities in
    :func:`aperture_correction_model_error`.
    """

    if not model.get("accepted"):
        return 0.0
    if str(model.get("selected_model") or "identity") != "plane":
        return 0.0
    if x is None or y is None:
        return None
    try:
        cov = np.asarray(model.get("covariance"), dtype=float)
        if cov.shape != (3, 3):
            return None
        xn = float(_normalize_axis(np.asarray([float(x)]), int(model["image_width"]))[0])
        yn = float(_normalize_axis(np.asarray([float(y)]), int(model["image_height"]))[0])
        pivot_x = float(model.get("pivot_xnorm") or 0.0)
        pivot_y = float(model.get("pivot_ynorm") or 0.0)
        vec = np.asarray([0.0, xn - pivot_x, yn - pivot_y], dtype=float)
        differential_var = max(0.0, float(vec @ cov @ vec))
        value = math.sqrt(differential_var)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def aperture_corrected_random_error(
    source_random_error: float | None,
    *,
    model: dict[str, Any],
    x: float | None,
    y: float | None,
) -> float | None:
    """Propagate only aperture uncertainty that survives differential calibration."""

    return _quadrature(source_random_error, aperture_correction_differential_error(model, x, y))


def export_aperture_correction_diagnostics(
    result: ApertureCorrectionResult,
    frame_stats: list[ApertureCorrectionFrameStat],
    *,
    context_analysis: dict[str, Any] | None = None,
    frame_contexts: list[dict[str, Any]] | None = None,
    out_dir: str | Path,
) -> tuple[Path, Path]:
    target = Path(out_dir) / "aperture_correction"
    target.mkdir(parents=True, exist_ok=True)
    summary_path = target / "aperture_correction_summary.json"
    frames_path = target / "aperture_correction_frames.csv"
    summary_path.write_text(
        json.dumps(
            {
                "summary": asdict(result),
                "frames": [asdict(row) for row in frame_stats],
                "context_analysis": context_analysis or {},
                "frame_contexts": frame_contexts or [],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    fieldnames = list(ApertureCorrectionFrameStat.__dataclass_fields__.keys())
    with frames_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in frame_stats:
            writer.writerow(asdict(row))
    return summary_path, frames_path


def _get_or_create_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    parameters: dict[str, Any],
    immutable_generation: bool = False,
) -> CalibrationVersion:
    full_parameters = {
        "source_calibration_name": source_cv.name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        **parameters,
    }
    return prepare_calibration_version_for_write(
        session,
        logical_name=name,
        model_name="spatial_aperture_correction_v2",
        instrument_unit_id=instrument_unit_id,
        instrument_profile={},
        used_catalogs={},
        parameters=full_parameters,
        notes=(
            "Small-to-large aperture correction with optional per-frame spatial plane "
            "and separated spatial/total safety gates."
        ),
        source_cv=source_cv,
        immutable_generation=immutable_generation,
    )


def _delete_existing_target_rows(
    session: Session,
    *,
    target_calibration_version_id: int,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
) -> int:
    stmt = delete(Measurement).where(Measurement.calibration_version_id == target_calibration_version_id)
    if frame_ids is not None:
        stmt = stmt.where(Measurement.frame_id.in_(list(frame_ids)))
    elif instrument_unit_id is not None:
        frame_subquery = select(Frame.frame_id).where(Frame.instrument_unit_id == instrument_unit_id)
        stmt = stmt.where(Measurement.frame_id.in_(frame_subquery))
    res = session.execute(stmt)
    return int(res.rowcount or 0)


def _load_aperture_source_rows_scalar(
    session: Session,
    *,
    source_calibration_version_id: int,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
    compact_normalized_only: bool = False,
) -> tuple[list[Any], dict[int, Any]]:
    """Load aperture-correction inputs without ORM entity hydration.

    The old ``select(Measurement, Frame)`` path materialized two ORM entities for
    every measurement and repeated all frame columns for every source.  The hot
    path only needs measurement scalars plus one compact frame row per frame.
    SQLAlchemy ``Row`` objects retain attribute access, so downstream numerical
    code remains unchanged while avoiding identity-map/state overhead.
    """

    if compact_normalized_only:
        bad_flag_exprs = tuple(
            func.coalesce(Measurement.flags[key].as_boolean(), false())
            for key in (
                "saturated_or_near_saturated",
                "aperture_touches_x_edge",
                "aperture_touches_y_edge",
                "annulus_touches_x_edge",
                "annulus_touches_y_edge",
                "non_positive_flux",
                "bad_xy",
                "empty_aperture",
            )
        )
        measurement_columns = (
            Measurement.frame_id,
            Measurement.x,
            Measurement.y,
            Measurement.mag_inst,
            Measurement.mag_err,
            Measurement.mag_err_random,
            (~or_(*bad_flag_exprs)).label("eligible_flags_ok"),
        )
    else:
        measurement_columns = (
            Measurement.object_id,
            Measurement.frame_id,
            Measurement.x,
            Measurement.y,
            Measurement.flux,
            Measurement.flux_err,
            Measurement.mag_inst,
            Measurement.mag_err,
            Measurement.mag_err_random,
            Measurement.mag_err_systematic,
            Measurement.aperture_radius_pix,
            Measurement.local_background,
            Measurement.local_zp_correction,
            Measurement.centroid_dx_pix,
            Measurement.centroid_dy_pix,
            Measurement.flags,
        )
    stmt = select(*measurement_columns).where(
        Measurement.calibration_version_id == int(source_calibration_version_id)
    )
    if frame_ids is not None:
        stmt = stmt.where(Measurement.frame_id.in_([int(value) for value in frame_ids]))
    if instrument_unit_id is not None:
        stmt = stmt.join(Frame, Frame.frame_id == Measurement.frame_id).where(
            Frame.instrument_unit_id == int(instrument_unit_id)
        )
    measurement_rows = list(
        session.execute(stmt.order_by(Measurement.frame_id, Measurement.object_id)).all()
    )
    selected_frame_ids = sorted({int(row.frame_id) for row in measurement_rows})
    if not selected_frame_ids:
        return measurement_rows, {}

    frame_columns = (
        Frame.frame_id,
        FrameLocation.locator.label("raw_path"),
        Frame.exptime,
        Frame.quality_flags,
        Frame.header_json,
        Frame.date_obs,
        Frame.mjd,
        Frame.airmass,
        Frame.ccd_temp,
    )
    frame_rows = session.execute(
        select(*frame_columns)
        .join(FrameLocation, (FrameLocation.frame_id == Frame.frame_id) & FrameLocation.is_primary)
        .where(Frame.frame_id.in_(selected_frame_ids))
        .order_by(Frame.frame_id)
    ).all()
    frames = {int(row.frame_id): row for row in frame_rows}
    return measurement_rows, frames


def _eligible_calibrator(measurement: Any, *, max_mag_err: float | None) -> bool:
    if measurement.x is None or measurement.y is None or measurement.mag_inst is None:
        return False
    try:
        if not all(math.isfinite(float(value)) for value in (measurement.x, measurement.y, measurement.mag_inst)):
            return False
    except Exception:
        return False
    if max_mag_err is not None:
        err = _first_finite(measurement.mag_err_random, measurement.mag_err)
        if err is None or err > float(max_mag_err):
            return False
    compact_flags_ok = getattr(measurement, "eligible_flags_ok", None)
    if compact_flags_ok is not None:
        return bool(compact_flags_ok)
    flags = measurement.flags or {}
    bad_keys = (
        "saturated_or_near_saturated",
        "aperture_touches_x_edge",
        "aperture_touches_y_edge",
        "annulus_touches_x_edge",
        "annulus_touches_y_edge",
        "non_positive_flux",
        "bad_xy",
        "empty_aperture",
    )
    return not any(bool(flags.get(key)) for key in bad_keys)


def _identity_model(reason: str) -> dict[str, Any]:
    return {
        "accepted": False,
        "selected_model": "identity",
        "selection_reason": None,
        "fallback_reason": reason,
        "n_candidates": 0,
        "n_used": 0,
        "image_width": 1,
        "image_height": 1,
        "pivot_xnorm": 0.0,
        "pivot_ynorm": 0.0,
        "coefficients": [0.0, 0.0, 0.0],
        "covariance": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        "correction_median": None,
        "correction_mad": None,
        "residual_rms": None,
        "residual_mad": None,
        "residual_robust_sigma": None,
        "constant_bic": None,
        "plane_bic": None,
        "plane_bic_improvement": None,
        "plane_candidate_coefficients": None,
        "plane_candidate_covariance": None,
        "plane_candidate_residual_rms": None,
        "plane_candidate_residual_mad": None,
        "plane_candidate_residual_robust_sigma": None,
        "plane_candidate_spatial_peak_to_peak": None,
        "plane_candidate_pivot_correction": None,
        "plane_candidate_max_abs_spatial_deviation": None,
        "plane_candidate_max_abs_total_correction": None,
        "plane_candidate_max_abs_correction": None,
        "legacy_max_abs_correction": None,
        "max_abs_spatial_deviation_threshold": None,
        "max_abs_total_correction_threshold": None,
        "legacy_selected_model": "identity",
        "legacy_selection_reason": reason,
        "selection_changed_vs_legacy": False,
        "spatial_peak_to_peak": None,
        "max_abs_spatial_deviation": None,
        "max_abs_predicted_correction": None,
        "model_error_median": None,
        "differential_error_median": None,
    }


def _frame_stat_from_model(frame_id: int, model: dict[str, Any]) -> ApertureCorrectionFrameStat:
    coeff = list(model.get("coefficients") or [None, None, None]) + [None, None, None]
    return ApertureCorrectionFrameStat(
        frame_id=int(frame_id),
        n_candidates=int(model.get("n_candidates") or 0),
        n_used=int(model.get("n_used") or 0),
        selected_model=str(model.get("selected_model") or "identity"),
        selection_reason=model.get("selection_reason"),
        correction_median=_finite_or_none(model.get("correction_median")),
        correction_mad=_finite_or_none(model.get("correction_mad")),
        residual_rms=_finite_or_none(model.get("residual_rms")),
        residual_mad=_finite_or_none(model.get("residual_mad")),
        residual_robust_sigma=_finite_or_none(model.get("residual_robust_sigma")),
        constant_bic=_finite_or_none(model.get("constant_bic")),
        plane_bic=_finite_or_none(model.get("plane_bic")),
        plane_bic_improvement=_finite_or_none(model.get("plane_bic_improvement")),
        plane_candidate_max_abs_spatial_deviation=_finite_or_none(
            model.get("plane_candidate_max_abs_spatial_deviation")
        ),
        plane_candidate_max_abs_total_correction=_finite_or_none(
            model.get("plane_candidate_max_abs_total_correction")
        ),
        legacy_selected_model=str(model.get("legacy_selected_model") or "identity"),
        selection_changed_vs_legacy=bool(model.get("selection_changed_vs_legacy")),
        spatial_peak_to_peak=_finite_or_none(model.get("spatial_peak_to_peak")),
        coefficient_0=_finite_or_none(coeff[0]),
        coefficient_x=_finite_or_none(coeff[1]),
        coefficient_y=_finite_or_none(coeff[2]),
        model_error_median=_finite_or_none(model.get("model_error_median")),
        differential_error_median=_finite_or_none(model.get("differential_error_median")),
        accepted=bool(model.get("accepted")),
        fallback_reason=model.get("fallback_reason"),
    )


def _normalize_axis(values: np.ndarray, size: int) -> np.ndarray:
    denom = max(int(size) - 1, 1)
    return 2.0 * np.asarray(values, dtype=float) / float(denom) - 1.0


def _residual_metrics(residual: np.ndarray, *, n_params: int) -> dict[str, float | None]:
    residual = np.asarray(residual, dtype=float)
    residual = residual[np.isfinite(residual)]
    if residual.size == 0:
        return {"residual_rms": None, "residual_mad": None, "residual_robust_sigma": None, "bic": math.inf}
    rms = float(math.sqrt(float(np.mean(residual**2))))
    mad = _np_mad(residual)
    robust_sigma = float(1.4826 * mad) if mad is not None else None
    sse = max(float(np.sum(residual**2)), 1e-24)
    bic = float(residual.size * math.log(sse / residual.size) + n_params * math.log(max(residual.size, 1)))
    return {"residual_rms": rms, "residual_mad": mad, "residual_robust_sigma": robust_sigma, "bic": bic}


def _np_mad(values: np.ndarray) -> float | None:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    med = float(np.median(values))
    return float(np.median(np.abs(values - med)))


def _resolve_saturation_level(frame: Frame, header: Any) -> float | None:
    for value in (
        getattr(frame, "saturation_level_adu", None),
        header.get("SATURATE") if header is not None else None,
        header.get("SATLEVEL") if header is not None else None,
        header.get("MAXLIN") if header is not None else None,
    ):
        out = _finite_or_none(value)
        if out is not None and out > 0:
            return out
    return None


def _safe_positive_float(value: Any) -> float | None:
    out = _finite_or_none(value)
    return out if out is not None and out > 0 else None


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _first_finite(*values: Any) -> float | None:
    for value in values:
        out = _finite_or_none(value)
        if out is not None:
            return out
    return None


def _scaled(value: Any, factor: float) -> float | None:
    out = _finite_or_none(value)
    return float(out * factor) if out is not None else None


def _add(value: Any, delta: float) -> float | None:
    out = _finite_or_none(value)
    return float(out + delta) if out is not None else None


def _quadrature(*values: Any) -> float | None:
    finite = [out for value in values if (out := _finite_or_none(value)) is not None and out >= 0]
    if not finite:
        return None
    return float(math.sqrt(sum(value * value for value in finite)))


def _median(values: list[Any]) -> float | None:
    finite = [out for value in values if (out := _finite_or_none(value)) is not None]
    return float(median(finite)) if finite else None
