from __future__ import annotations

import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from astropy.io import fits
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.calibration.provenance import (
    IMMUTABLE_CALIBRATION_STATUSES,
    prepare_calibration_version_for_write,
)
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import (
    PostgresMeasurementCopyStage,
    PostgresMeasurementDirectCopyWriter,
    postgres_copy_capable,
    upsert_measurement_rows,
)
from grandgousier.photometry.adaptive_aperture import (
    ADAPTIVE_APERTURE_ALGORITHM,
    _build_psf_references,
    _make_reference_field,
    interpolate_adaptive_geometry,
    measure_elliptical_source,
)
from grandgousier.quality.psf_field import _load_detection_rows
from grandgousier.photometry.spatial_psf import SPATIAL_PSF_ALGORITHM, measure_spatial_psf_source
from grandgousier.photometry.empirical_psf import (
    EMPIRICAL_PSF_ALGORITHM,
    build_empirical_psf_field,
    measure_empirical_psf_source,
)

from grandgousier.photometry.error_model import (
    LEGACY_APERTURE_ERROR_MODEL,
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    SUPPORTED_APERTURE_ERROR_MODELS,
    aperture_error_budget,
)
from grandgousier.db.models import (
    CalibrationVersion,
    CatalogSource,
    Frame,
    FrameCatalogLink,
    FrameStatus,
    InstrumentUnit,
    Measurement,
)
from grandgousier.identity.object import get_or_create_object_for_catalog_source


FORCED_PHOTOMETRY_FAILURE_SAMPLE_LIMIT = 20


@dataclass
class ForcedPhotometryResult:
    selected_frames: int = 0
    checked_frames: int = 0
    selected_links: int = 0
    measured: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    write_strategy: str = "PENDING"
    measurement_write_strategy: str = "PENDING"
    link_read_strategy: str = "PER_FRAME_ORM"
    prefetched_link_rows: int = 0
    direct_copy_rows: int = 0
    direct_copy_batches: int = 0
    direct_copy_sec: float = 0.0
    staging_rows: int = 0
    staging_copy_batches: int = 0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    write_finalize_sec: float = 0.0
    object_identity_cache_hits: int = 0
    object_identity_cache_misses: int = 0
    preexisting_measurements: int = 0
    reuse_preflight_strategy: str = "OFF"
    existing_measurement_check_strategy: str = "OFF"
    reuse_fast_path: bool = False
    diagnostics_read_strategy: str = "IN_MEMORY"
    persisted_diagnostics_reloaded: bool = False
    reused_frames: int = 0
    reused_measurements: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    failed_measurements: int = 0
    failure_samples: list[dict[str, Any]] = field(default_factory=list)
    failure_samples_truncated: int = 0
    calibration_version_id: int | None = None
    calibration_name: str | None = None
    dry_run: bool = False
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL
    photometry_estimator: str = "circular"
    adaptive_frames_with_psf_model: int = 0
    adaptive_frames_fallback: int = 0
    adaptive_measurements: int = 0
    adaptive_measurement_fallbacks: int = 0
    psf_frames_with_psf_model: int = 0
    psf_frames_fallback: int = 0
    psf_measurements: int = 0
    psf_measurement_fallbacks: int = 0
    empirical_psf_frames_with_model: int = 0
    empirical_psf_frames_fallback: int = 0
    empirical_psf_measurements: int = 0
    empirical_psf_measurement_fallbacks: int = 0
    measurements_with_selected_error: int = 0
    measurements_with_physical_error: int = 0
    measurements_with_legacy_comparison: int = 0
    measurements_gain_fallback: int = 0
    measurements_read_noise_fallback: int = 0
    measurements_background_empirical: int = 0
    measurements_background_detector_prediction: int = 0
    measurements_scintillation_available: int = 0
    frames_gain_fallback: int = 0
    frames_read_noise_fallback: int = 0
    frames_scintillation_available: int = 0
    gain_e_per_adu_median: float | None = None
    read_noise_e_median: float | None = None
    selected_mag_err_median: float | None = None
    physical_mag_err_median: float | None = None
    legacy_mag_err_median: float | None = None
    physical_to_legacy_mag_err_ratio_median: float | None = None
    physical_to_legacy_mag_err_ratio_p10: float | None = None
    physical_to_legacy_mag_err_ratio_p90: float | None = None
    source_poisson_fraction_median: float | None = None
    aperture_background_fraction_median: float | None = None
    background_estimate_fraction_median: float | None = None
    fractional_flux_floor: float = 0.0
    scintillation_enabled: bool = True
    telescope_aperture_m: float | None = None
    site_altitude_m: float | None = None
    scintillation_fractional_sigma_median: float | None = None
    fractional_floor_fraction_median: float | None = None
    scintillation_fraction_median: float | None = None
    diagnostics_summary_json: str | None = None
    diagnostics_frames_csv: str | None = None
    performance_diagnostics_enabled: bool = False
    performance_timing_algorithm: str = "OFF"
    timings_sec: dict[str, float] = field(default_factory=dict)
    frame_timing_summary: dict[str, dict[str, float]] = field(default_factory=dict)
    throughput: dict[str, float] = field(default_factory=dict)


def _record_failure_sample(
    result: ForcedPhotometryResult,
    *,
    frame_id: int,
    stage: str,
    reason: str,
    exception: Exception | None = None,
    frame_catalog_link_id: int | None = None,
    catalog_source_id: int | None = None,
) -> None:
    """Keep a bounded, path-free sample of failures for pipeline diagnostics."""

    if len(result.failure_samples) >= FORCED_PHOTOMETRY_FAILURE_SAMPLE_LIMIT:
        result.failure_samples_truncated += 1
        return

    sample: dict[str, Any] = {
        "frame_id": int(frame_id),
        "stage": str(stage),
        "reason": str(reason),
        "exception_class": type(exception).__name__ if exception is not None else None,
    }
    if frame_catalog_link_id is not None:
        sample["frame_catalog_link_id"] = int(frame_catalog_link_id)
    if catalog_source_id is not None:
        sample["catalog_source_id"] = int(catalog_source_id)
    result.failure_samples.append(sample)


@dataclass(frozen=True, slots=True)
class ForcedFrameLink:
    frame_catalog_link_id: int
    frame_id: int
    catalog_source_id: int
    x: float | None
    y: float | None


@dataclass(frozen=True, slots=True)
class ForcedCatalogSource:
    catalog_source_id: int
    catalog_name: str
    source_id: str
    ra_deg: float
    dec_deg: float
    epoch: float | None
    pmra_masyr: float | None
    pmdec_masyr: float | None
    ref_mags: dict
    colors: dict


@dataclass(frozen=True)
class ApertureMeasurement:
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_err: float | None
    local_background: float | None
    aperture_radius_pix: float
    flags: dict
    error_budget: dict[str, Any] | None = None


def _accumulate_optional_timing(
    timings: dict[str, float] | None,
    key: str,
    started: float | None,
) -> float | None:
    """Accumulate one optional high-resolution profiling interval.

    Returns a fresh phase start so callers can chain adjacent sub-phases without
    taking an additional timestamp when profiling is disabled.
    """

    if timings is None or started is None:
        return None
    now = time.perf_counter()
    timings[key] = timings.get(key, 0.0) + float(now - started)
    return now


def _timing_distribution(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {}
    return {
        "n": int(arr.size),
        "sum": float(np.sum(arr)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "max": float(np.max(arr)),
    }


def run_forced_aperture_photometry(
    session: Session,
    *,
    catalog_name: str | None = None,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    aperture_radius_pix: float = 3.0,
    photometry_estimator: str = "adaptive_ellipse",
    adaptive_detection_algorithm: str = "numpy_localmax_v1",
    adaptive_min_psf_snr: float = 20.0,
    adaptive_max_psf_sources_per_frame: int = 500,
    adaptive_psf_cutout_radius_pix: int = 7,
    adaptive_psf_min_isolation_pix: float = 12.0,
    adaptive_interpolation_neighbors: int = 24,
    adaptive_min_psf_references: int = 20,
    adaptive_aperture_fwhm_factor: float = 1.0,
    adaptive_max_axis_ratio: float = 3.0,
    adaptive_max_radius_scale: float = 2.5,
    adaptive_fallback_to_circular: bool = True,
    psf_profile: str = "moffat",
    psf_moffat_beta: float = 2.5,
    psf_fit_radius_fwhm: float = 2.5,
    psf_min_fit_radius_pix: float = 5.0,
    psf_max_fit_radius_pix: float = 14.0,
    psf_centroid_search_radius_pix: float = 0.75,
    psf_centroid_grid_steps: int = 3,
    psf_residual_clip_sigma: float = 5.0,
    psf_residual_clip_iterations: int = 2,
    psf_fallback_to_adaptive: bool = True,
    empirical_psf_grid_size: int = 3,
    empirical_psf_stamp_radius_pix: int = 8,
    empirical_psf_reference_radius_pix: float = 5.0,
    empirical_psf_references_per_node: int = 48,
    empirical_psf_min_references_per_node: int = 12,
    empirical_psf_fallback_to_adaptive: bool = True,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    zero_point: float = 0.0,
    saturation_level_adu: float | None = None,
    calibration_name: str | None = None,
    limit_frames: int | None = None,
    limit_links: int | None = None,
    update_processing_status: bool = False,
    reuse_existing: bool = True,
    force_recompute: bool = False,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL,
    include_legacy_comparison: bool = True,
    gain_e_per_adu: float | None = None,
    read_noise_e: float | None = None,
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    empirical_background_floor: bool = True,
    fractional_flux_floor: float = 0.0,
    scintillation_enabled: bool = True,
    scintillation_coefficient: float = 0.09,
    telescope_aperture_m: float | None = None,
    site_altitude_m: float | None = None,
    diagnostics_out_dir: str | Path | None = None,
    diagnose_performance: bool = False,
    immutable_generation: bool = False,
) -> ForcedPhotometryResult:
    """Run forced photometry for catalog-frame links.

    ``photometry_estimator='circular'`` preserves the historical fixed circular
    aperture exactly. ``adaptive_ellipse`` builds a local PSF covariance field and
    measures a PSF-aligned ellipse. ``spatial_psf`` fits a local elliptical Moffat
    (or Gaussian) profile with spatially interpolated shape. ``empirical_psf`` builds
    a grid of sub-pixel registered stellar templates and interpolates the full 2-D PSF.
    Coordinates are taken from frame_catalog_links.x/y and are interpreted as 0-based
    FITS array pixel coordinates produced by astropy.wcs.world_to_pixel_values().
    """

    if frame_ids is not None and not frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

    photometry_estimator = str(photometry_estimator or "circular").strip().lower()
    if photometry_estimator not in {"circular", "adaptive_ellipse", "spatial_psf", "empirical_psf"}:
        raise ValueError("photometry_estimator must be 'circular', 'adaptive_ellipse', 'spatial_psf', or 'empirical_psf'")
    if adaptive_interpolation_neighbors < 3:
        raise ValueError("adaptive_interpolation_neighbors must be >= 3")
    if adaptive_min_psf_references < 6:
        raise ValueError("adaptive_min_psf_references must be >= 6")
    if adaptive_aperture_fwhm_factor <= 0:
        raise ValueError("adaptive_aperture_fwhm_factor must be positive")
    if adaptive_max_axis_ratio < 1:
        raise ValueError("adaptive_max_axis_ratio must be >= 1")
    if adaptive_max_radius_scale < 1:
        raise ValueError("adaptive_max_radius_scale must be >= 1")
    psf_profile = str(psf_profile or "moffat").strip().lower()
    if psf_profile not in {"moffat", "gaussian"}:
        raise ValueError("psf_profile must be 'moffat' or 'gaussian'")
    if psf_moffat_beta <= 1.0:
        raise ValueError("psf_moffat_beta must be > 1")
    if psf_fit_radius_fwhm <= 0 or psf_min_fit_radius_pix <= 0 or psf_max_fit_radius_pix <= 0:
        raise ValueError("PSF fit radii must be positive")
    if psf_max_fit_radius_pix < psf_min_fit_radius_pix:
        raise ValueError("psf_max_fit_radius_pix must be >= psf_min_fit_radius_pix")
    if psf_centroid_search_radius_pix < 0 or psf_centroid_grid_steps < 1:
        raise ValueError("invalid PSF centroid search configuration")
    if psf_residual_clip_sigma <= 0 or psf_residual_clip_iterations < 0:
        raise ValueError("invalid PSF residual clipping configuration")
    if empirical_psf_grid_size < 2:
        raise ValueError("empirical_psf_grid_size must be >= 2")
    if empirical_psf_stamp_radius_pix < 3:
        raise ValueError("empirical_psf_stamp_radius_pix must be >= 3")
    if empirical_psf_reference_radius_pix <= 0 or empirical_psf_reference_radius_pix >= empirical_psf_stamp_radius_pix:
        raise ValueError("empirical_psf_reference_radius_pix must be > 0 and < stamp radius")
    if empirical_psf_references_per_node < 3:
        raise ValueError("empirical_psf_references_per_node must be >= 3")
    if empirical_psf_min_references_per_node < 3 or empirical_psf_min_references_per_node > empirical_psf_references_per_node:
        raise ValueError("invalid empirical_psf_min_references_per_node")

    if random_error_model not in SUPPORTED_APERTURE_ERROR_MODELS:
        raise ValueError(
            f"Unsupported random_error_model={random_error_model!r}; "
            f"expected one of {sorted(SUPPORTED_APERTURE_ERROR_MODELS)}"
        )
    if background_estimator_variance_factor < 0:
        raise ValueError("background_estimator_variance_factor must be non-negative")
    if fractional_flux_floor < 0:
        raise ValueError("fractional_flux_floor must be non-negative")
    if scintillation_coefficient < 0:
        raise ValueError("scintillation_coefficient must be non-negative")
    if aperture_radius_pix <= 0:
        raise ValueError("aperture_radius_pix must be positive")
    if annulus_inner_pix <= aperture_radius_pix:
        raise ValueError("annulus_inner_pix must be greater than aperture_radius_pix")
    if annulus_outer_pix <= annulus_inner_pix:
        raise ValueError("annulus_outer_pix must be greater than annulus_inner_pix")

    result = ForcedPhotometryResult(
        dry_run=dry_run,
        insert_batch_size=int(insert_batch_size),
        random_error_model=random_error_model,
        photometry_estimator=photometry_estimator,
        fractional_flux_floor=float(fractional_flux_floor),
        scintillation_enabled=bool(scintillation_enabled),
        telescope_aperture_m=_safe_positive_float(telescope_aperture_m),
        site_altitude_m=_finite_float(site_altitude_m),
        performance_diagnostics_enabled=bool(diagnose_performance),
        performance_timing_algorithm=(
            "FORCED_APERTURE_PHASE_TIMING_V1" if diagnose_performance else "OFF"
        ),
    )
    performance_started = time.perf_counter() if diagnose_performance else None
    timing_totals: dict[str, float] = {}
    frame_timing_samples: dict[str, list[float]] = {}
    estimated_fits_bytes = 0

    def _timing_start() -> float | None:
        return time.perf_counter() if diagnose_performance else None

    def _timing_add(key: str, started: float | None) -> float:
        if started is None:
            return 0.0
        elapsed = float(time.perf_counter() - started)
        timing_totals[key] = timing_totals.get(key, 0.0) + elapsed
        return elapsed

    def _frame_timing_add(key: str, elapsed: float) -> None:
        if diagnose_performance:
            frame_timing_samples.setdefault(key, []).append(float(elapsed))
    if calibration_name is None:
        estimator_tag = (
            "aperture"
            if photometry_estimator == "circular"
            else (
                f"adaptive_ellipse_f{_fmt_float(adaptive_aperture_fwhm_factor)}"
                if photometry_estimator == "adaptive_ellipse"
                else (
                    f"spatial_psf_{psf_profile}_b{_fmt_float(psf_moffat_beta)}"
                    if photometry_estimator == "spatial_psf"
                    else f"empirical_psf_g{int(empirical_psf_grid_size)}_sr{int(empirical_psf_stamp_radius_pix)}_rr{_fmt_float(empirical_psf_reference_radius_pix)}_n{int(empirical_psf_references_per_node)}_m{int(empirical_psf_min_references_per_node)}"
                )
            )
        )
        calibration_name = (
            f"raw_{estimator_tag}_r{_fmt_float(aperture_radius_pix)}_"
            f"ann{_fmt_float(annulus_inner_pix)}_{_fmt_float(annulus_outer_pix)}_"
            f"{_random_error_model_tag(random_error_model)}"
        )
    result.calibration_name = calibration_name

    phase_started = _timing_start()
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    _timing_add("select_frames", phase_started)
    result.selected_frames = len(frames)

    reuse_completed_immutable = False
    reuse_published_calibration_version_id: int | None = None
    reuse_scope_link_counts: dict[int, int] | None = None
    reuse_scope_existing_counts: dict[int, int] | None = None
    if not dry_run and immutable_generation and reuse_existing and not force_recompute:
        published = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
        if (
            published is not None
            and str(getattr(published, "lifecycle_status", "") or "") in IMMUTABLE_CALIBRATION_STATUSES
        ):
            reuse_published_calibration_version_id = int(published.calibration_version_id)
            result.reuse_preflight_strategy = "BULK_GROUPED_COUNTS_V1"
            reuse_scope_link_counts = _select_frame_link_counts_bulk(
                session,
                frame_ids=[int(frame.frame_id) for frame in frames],
                catalog_name=catalog_name,
                limit_per_frame=limit_links,
            )
            reuse_scope_existing_counts = _existing_measurement_counts_bulk(
                session,
                calibration_version_id=int(published.calibration_version_id),
                frame_ids=[int(frame.frame_id) for frame in frames],
            )
            reuse_completed_immutable = _measurement_scope_counts_are_complete(
                frame_ids=[int(frame.frame_id) for frame in frames],
                required_link_counts=reuse_scope_link_counts,
                existing_measurement_counts=reuse_scope_existing_counts,
            )

    phase_started = _timing_start()
    if not dry_run:
        calibration_version = _get_or_create_calibration_version(
            session,
            name=calibration_name,
            instrument_unit_id=instrument_unit_id,
            parameters={
                "aperture_radius_pix": aperture_radius_pix,
                "photometry_estimator": photometry_estimator,
                "adaptive_algorithm": (ADAPTIVE_APERTURE_ALGORITHM if photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"} else None),
                "adaptive_detection_algorithm": adaptive_detection_algorithm,
                "adaptive_min_psf_snr": adaptive_min_psf_snr,
                "adaptive_max_psf_sources_per_frame": adaptive_max_psf_sources_per_frame,
                "adaptive_psf_cutout_radius_pix": adaptive_psf_cutout_radius_pix,
                "adaptive_psf_min_isolation_pix": adaptive_psf_min_isolation_pix,
                "adaptive_interpolation_neighbors": adaptive_interpolation_neighbors,
                "adaptive_min_psf_references": adaptive_min_psf_references,
                "adaptive_aperture_fwhm_factor": adaptive_aperture_fwhm_factor,
                "adaptive_max_axis_ratio": adaptive_max_axis_ratio,
                "adaptive_max_radius_scale": adaptive_max_radius_scale,
                "adaptive_fallback_to_circular": adaptive_fallback_to_circular,
                "psf_algorithm": (SPATIAL_PSF_ALGORITHM if photometry_estimator == "spatial_psf" else None),
                "psf_profile": psf_profile,
                "psf_moffat_beta": psf_moffat_beta,
                "psf_fit_radius_fwhm": psf_fit_radius_fwhm,
                "psf_min_fit_radius_pix": psf_min_fit_radius_pix,
                "psf_max_fit_radius_pix": psf_max_fit_radius_pix,
                "psf_centroid_search_radius_pix": psf_centroid_search_radius_pix,
                "psf_centroid_grid_steps": psf_centroid_grid_steps,
                "psf_residual_clip_sigma": psf_residual_clip_sigma,
                "psf_residual_clip_iterations": psf_residual_clip_iterations,
                "psf_fallback_to_adaptive": psf_fallback_to_adaptive,
                "empirical_psf_algorithm": (EMPIRICAL_PSF_ALGORITHM if photometry_estimator == "empirical_psf" else None),
                "empirical_psf_grid_size": empirical_psf_grid_size,
                "empirical_psf_stamp_radius_pix": empirical_psf_stamp_radius_pix,
                "empirical_psf_reference_radius_pix": empirical_psf_reference_radius_pix,
                "empirical_psf_references_per_node": empirical_psf_references_per_node,
                "empirical_psf_min_references_per_node": empirical_psf_min_references_per_node,
                "empirical_psf_fallback_to_adaptive": empirical_psf_fallback_to_adaptive,
                "annulus_inner_pix": annulus_inner_pix,
                "annulus_outer_pix": annulus_outer_pix,
                "zero_point": zero_point,
                "saturation_level_adu": saturation_level_adu,
                "catalog_name": catalog_name,
                "reuse_existing": reuse_existing,
                "force_recompute": force_recompute,
                "random_error_model": random_error_model,
                "gain_e_per_adu_override": gain_e_per_adu,
                "read_noise_e_override": read_noise_e,
                "background_estimator_variance_factor": background_estimator_variance_factor,
                "empirical_background_floor": empirical_background_floor,
                "fractional_flux_floor": fractional_flux_floor,
                "scintillation_enabled": scintillation_enabled,
                "scintillation_coefficient": scintillation_coefficient,
                "telescope_aperture_m": telescope_aperture_m,
                "site_altitude_m": site_altitude_m,
            },
            immutable_generation=immutable_generation,
            reuse_completed_immutable=reuse_completed_immutable,
        )
        session.flush()
        result.calibration_version_id = calibration_version.calibration_version_id
        result.preexisting_measurements = _existing_calibration_measurement_count(
            session, calibration_version_id=int(result.calibration_version_id)
        )
    else:
        result.calibration_version_id = None
    _timing_add("prepare_calibration", phase_started)

    selected_mag_errors: list[float] = []
    physical_mag_errors: list[float] = []
    legacy_mag_errors: list[float] = []
    physical_to_legacy_ratios: list[float] = []
    gain_values: list[float] = []
    read_noise_values: list[float] = []
    source_poisson_fractions: list[float] = []
    aperture_background_fractions: list[float] = []
    background_estimate_fractions: list[float] = []
    fractional_floor_fractions: list[float] = []
    scintillation_fractions: list[float] = []
    scintillation_fractional_sigmas: list[float] = []
    frame_diagnostics: list[dict[str, Any]] = []

    # Catalog sources repeat on every frame. Keep their resolved archive object IDs
    # for the whole run rather than issuing one objects lookup per source per frame.
    object_cache: dict[int, int] = {}

    # A completed immutable BASE generation does not need link geometry, source
    # JSON payloads, FITS reads, or per-frame existence queries merely to prove
    # that every row can be reused. The preflight already has exact per-frame
    # required/existing counts, so keep the no-op reuse path count-only.
    fast_complete_reuse = bool(
        reuse_completed_immutable
        and reuse_scope_link_counts is not None
        and reuse_scope_existing_counts is not None
        and result.calibration_version_id is not None
        and reuse_published_calibration_version_id is not None
        and int(result.calibration_version_id) == reuse_published_calibration_version_id
    )
    prefetched_links: dict[int, list[tuple[ForcedFrameLink, ForcedCatalogSource]]] = {}
    existing_measurement_counts: dict[int, int] = {}
    if fast_complete_reuse:
        result.reuse_fast_path = True
        result.link_read_strategy = "BULK_COUNT_REUSE_V1"
        result.existing_measurement_check_strategy = "BULK_GROUPED_COUNTS_V1"
        result.prefetched_link_rows = int(sum(reuse_scope_link_counts.values()))
        existing_measurement_counts = reuse_scope_existing_counts
    else:
        # Read the complete catalog-link scope in two compact scalar queries: one for
        # frame-link geometry and one for the unique catalog sources. The historical
        # path issued one ORM join query per frame, which dominated BASE setup time.
        phase_started = _timing_start()
        prefetched_links = _select_frame_links_bulk(
            session,
            frame_ids=[int(frame.frame_id) for frame in frames],
            catalog_name=catalog_name,
            limit_per_frame=limit_links,
        )
        _timing_add("select_frame_links", phase_started)
        result.link_read_strategy = "BULK_SCALAR_2QUERY_V1"
        result.prefetched_link_rows = int(sum(len(items) for items in prefetched_links.values()))
        if (
            (not dry_run)
            and reuse_existing
            and not force_recompute
            and result.calibration_version_id is not None
            and result.preexisting_measurements > 0
        ):
            phase_started = _timing_start()
            existing_measurement_counts = _existing_measurement_counts_bulk(
                session,
                calibration_version_id=int(result.calibration_version_id),
                frame_ids=[int(frame.frame_id) for frame in frames],
            )
            _timing_add("prefetch_existing_measurement_counts", phase_started)
            result.existing_measurement_check_strategy = "BULK_GROUPED_COUNTS_V1"

    adaptive_detections_by_frame: dict[int, list[dict[str, Any]]] = {}
    if photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"} and not fast_complete_reuse:
        adaptive_detections_by_frame = _load_detection_rows(
            session,
            frame_ids=[int(frame.frame_id) for frame in frames],
            algorithm=adaptive_detection_algorithm,
        )

    # PostgreSQL fresh immutable BASE generations can be streamed directly into
    # measurements. Existing/legacy targets retain the staging + UPSERT path.
    copy_stage: PostgresMeasurementCopyStage | None = None
    direct_copy_writer: PostgresMeasurementDirectCopyWriter | None = None
    copy_buffer: list[dict[str, Any]] = []
    copy_buffer_rows = max(4096, min(16384, int(insert_batch_size) * 8))
    use_postgres_copy = bool(not dry_run and postgres_copy_capable(session))
    use_direct_copy = bool(
        use_postgres_copy
        and immutable_generation
        and result.preexisting_measurements == 0
    )
    result.write_strategy = (
        "DRY_RUN"
        if dry_run
        else (
            "REUSE_EXISTING_IMMUTABLE"
            if fast_complete_reuse
            else (
                "POSTGRESQL_COPY_DIRECT_FRESH_BASE"
                if use_direct_copy
                else ("POSTGRESQL_COPY_STAGING" if use_postgres_copy else "SQLALCHEMY_BATCH_UPSERT")
            )
        )
    )
    result.measurement_write_strategy = result.write_strategy

    photometry_desc = {
        "circular": "Forced aperture photometry",
        "adaptive_ellipse": "Forced adaptive aperture photometry",
        "spatial_psf": "Forced spatial PSF photometry",
        "empirical_psf": "Forced empirical spatial PSF photometry",
    }[photometry_estimator]
    for frame in tqdm(frames, desc=photometry_desc, unit="frame"):
        frame_started = _timing_start()
        result.checked_frames += 1
        path = Path(frame.raw_path)
        if not path.exists():
            result.skipped_missing_file += 1
            continue

        frame_id = int(frame.frame_id)
        if fast_complete_reuse:
            link_count = int((reuse_scope_link_counts or {}).get(frame_id, 0))
            if link_count <= 0:
                continue
            result.selected_links += link_count
            existing_n = int(existing_measurement_counts.get(frame_id, 0))
            if existing_n < link_count:
                raise RuntimeError(
                    "Immutable forced-photometry reuse scope changed after preflight: "
                    f"frame_id={frame_id}, required={link_count}, existing={existing_n}"
                )
            result.reused_frames += 1
            result.reused_measurements += existing_n
            if update_processing_status:
                frame.processing_status = FrameStatus.PHOTOMETRY_DONE
            continue

        links = prefetched_links.get(frame_id, [])
        if not links:
            continue
        result.selected_links += len(links)

        if (not dry_run) and reuse_existing and not force_recompute and result.calibration_version_id is not None:
            existing_n = int(existing_measurement_counts.get(frame_id, 0))
            if existing_n >= len(links):
                result.reused_frames += 1
                result.reused_measurements += int(existing_n)
                if update_processing_status:
                    frame.processing_status = FrameStatus.PHOTOMETRY_DONE
                continue

        try:
            phase_started = _timing_start()
            with fits.open(path, memmap=False) as hdul:
                data = np.asarray(hdul[0].data, dtype=float)
                header = hdul[0].header
            fits_elapsed = _timing_add("fits_read", phase_started)
            _frame_timing_add("fits_read", fits_elapsed)
            if diagnose_performance:
                try:
                    estimated_fits_bytes += int(path.stat().st_size)
                except OSError:
                    pass
            if data.ndim != 2:
                result.failed_frames += 1
                _record_failure_sample(
                    result,
                    frame_id=frame_id,
                    stage="image_geometry",
                    reason="expected_two_dimensional_image",
                )
                continue
        except Exception as exc:  # noqa: BLE001 - FITS failures are counted and safely sampled.
            result.failed_frames += 1
            _record_failure_sample(
                result,
                frame_id=frame_id,
                stage="fits_read",
                reason="fits_read_failed",
                exception=exc,
            )
            continue

        phase_started = _timing_start()
        frame_saturation = _resolve_saturation_level(
            explicit=saturation_level_adu,
            frame=frame,
            header=header,
        )
        exptime = _safe_positive_float(frame.exptime)
        resolved_gain, gain_source, gain_fallback = _resolve_gain_e_per_adu(
            explicit=gain_e_per_adu,
            frame=frame,
        )
        resolved_read_noise, read_noise_source, read_noise_fallback = _resolve_read_noise_e(
            explicit=read_noise_e,
            frame=frame,
        )
        if gain_fallback:
            result.frames_gain_fallback += 1
        if read_noise_fallback:
            result.frames_read_noise_fallback += 1
        _frame_timing_add("frame_metadata", _timing_add("frame_metadata", phase_started))

        adaptive_psf_field = None
        empirical_psf_field = None
        adaptive_frame_fallback_reason: str | None = None
        if photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"}:
            psf_refs = _build_psf_references(
                data,
                adaptive_detections_by_frame.get(frame_id, []),
                min_snr=float(adaptive_min_psf_snr),
                max_sources=int(adaptive_max_psf_sources_per_frame),
                cutout_radius_pix=int(adaptive_psf_cutout_radius_pix),
                min_isolation_pix=float(adaptive_psf_min_isolation_pix),
            )
            if len(psf_refs) >= int(adaptive_min_psf_references):
                adaptive_psf_field = _make_reference_field(psf_refs)
                if photometry_estimator == "adaptive_ellipse":
                    result.adaptive_frames_with_psf_model += 1
                elif photometry_estimator == "spatial_psf":
                    result.psf_frames_with_psf_model += 1
            else:
                adaptive_frame_fallback_reason = f"insufficient_psf_references:{len(psf_refs)}"
                if photometry_estimator == "adaptive_ellipse":
                    result.adaptive_frames_fallback += 1
                    if not adaptive_fallback_to_circular:
                        result.failed_frames += 1
                        _record_failure_sample(
                            result,
                            frame_id=frame_id,
                            stage="psf_model",
                            reason="adaptive_psf_model_unavailable_fallback_disabled",
                        )
                        continue
                elif photometry_estimator == "spatial_psf":
                    result.psf_frames_fallback += 1
                    if not adaptive_fallback_to_circular:
                        result.failed_frames += 1
                        _record_failure_sample(
                            result,
                            frame_id=frame_id,
                            stage="psf_model",
                            reason="spatial_psf_model_unavailable_fallback_disabled",
                        )
                        continue

        if photometry_estimator == "empirical_psf":
            empirical_psf_field = build_empirical_psf_field(
                data,
                adaptive_detections_by_frame.get(frame_id, []),
                min_snr=float(adaptive_min_psf_snr),
                max_sources=int(adaptive_max_psf_sources_per_frame),
                min_isolation_pix=float(adaptive_psf_min_isolation_pix),
                grid_size=int(empirical_psf_grid_size),
                stamp_radius_pix=int(empirical_psf_stamp_radius_pix),
                reference_radius_pix=float(empirical_psf_reference_radius_pix),
                references_per_node=int(empirical_psf_references_per_node),
                min_references_per_node=int(empirical_psf_min_references_per_node),
                background_annulus_inner_pix=max(float(annulus_inner_pix), float(empirical_psf_reference_radius_pix) + 2.0),
                background_annulus_outer_pix=max(float(annulus_outer_pix), float(empirical_psf_reference_radius_pix) + 6.0),
            )
            if empirical_psf_field is not None:
                result.empirical_psf_frames_with_model += 1
            else:
                result.empirical_psf_frames_fallback += 1
                adaptive_frame_fallback_reason = adaptive_frame_fallback_reason or "empirical_psf_model_unavailable"
                if adaptive_psf_field is None and not adaptive_fallback_to_circular:
                    result.failed_frames += 1
                    _record_failure_sample(
                        result,
                        frame_id=frame_id,
                        stage="psf_model",
                        reason="empirical_psf_model_unavailable_fallback_disabled",
                    )
                    continue

        frame_selected_errors: list[float] = []
        frame_physical_errors: list[float] = []
        frame_legacy_errors: list[float] = []
        frame_ratios: list[float] = []
        frame_source_fractions: list[float] = []
        frame_aperture_fractions: list[float] = []
        frame_estimate_fractions: list[float] = []
        frame_floor_fractions: list[float] = []
        frame_scintillation_fractions: list[float] = []
        frame_scintillation_sigmas: list[float] = []
        frame_background_sources: list[str] = []
        frame_scintillation_available: list[bool] = []

        rows = []
        frame_object_identity_sec = 0.0
        frame_measurement_kernel_sec = 0.0
        frame_diagnostics_row_build_sec = 0.0
        source_loop_started = _timing_start()
        for link, source in links:
            failure_stage = "object_identity"
            try:
                phase_started = _timing_start()
                object_id = object_cache.get(source.catalog_source_id)
                if object_id is None:
                    result.object_identity_cache_misses += 1
                    object_id = _get_or_create_object_id(session, source, dry_run=dry_run)
                    if object_id is not None:
                        object_cache[source.catalog_source_id] = object_id
                else:
                    result.object_identity_cache_hits += 1
                object_identity_elapsed = _timing_add("object_identity", phase_started)
                frame_object_identity_sec += object_identity_elapsed

                source_kernel_timings: dict[str, float] | None = {} if diagnose_performance else None
                failure_stage = "source_measurement"
                phase_started = _timing_start()
                if photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"} and (adaptive_psf_field is not None or empirical_psf_field is not None):
                    geometry = (
                        interpolate_adaptive_geometry(
                            adaptive_psf_field,
                            x=float(link.x),
                            y=float(link.y),
                            base_radius_pix=float(aperture_radius_pix),
                            aperture_fwhm_factor=float(adaptive_aperture_fwhm_factor),
                            interpolation_neighbors=int(adaptive_interpolation_neighbors),
                            max_axis_ratio=float(adaptive_max_axis_ratio),
                            max_radius_scale=float(adaptive_max_radius_scale),
                        )
                        if adaptive_psf_field is not None
                        else None
                    )
                    if photometry_estimator == "empirical_psf" and empirical_psf_field is not None:
                        measurement = measure_empirical_psf_source(
                            data, x=float(link.x), y=float(link.y), field=empirical_psf_field,
                            annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                            exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                            gain_e_per_adu=(None if gain_fallback else resolved_gain),
                            read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                            random_error_model=random_error_model,
                            include_legacy_comparison=include_legacy_comparison,
                            background_estimator_variance_factor=background_estimator_variance_factor,
                            empirical_background_floor=empirical_background_floor,
                            fractional_flux_floor=fractional_flux_floor,
                            scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                            telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                            centroid_search_radius_pix=psf_centroid_search_radius_pix,
                            centroid_grid_steps=psf_centroid_grid_steps,
                            residual_clip_sigma=psf_residual_clip_sigma,
                            residual_clip_iterations=psf_residual_clip_iterations,
                        )
                        result.empirical_psf_measurements += 1
                        if measurement.flux is None:
                            empirical_failure_flags = dict(measurement.flags or {})
                            if empirical_psf_fallback_to_adaptive and geometry is not None:
                                result.empirical_psf_measurement_fallbacks += 1
                                measurement = measure_elliptical_source(
                                    data, x=float(link.x), y=float(link.y), geometry=geometry,
                                    annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                                    exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                                    gain_e_per_adu=(None if gain_fallback else resolved_gain),
                                    read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                                    random_error_model=random_error_model,
                                    include_legacy_comparison=include_legacy_comparison,
                                    background_estimator_variance_factor=background_estimator_variance_factor,
                                    empirical_background_floor=empirical_background_floor,
                                    fractional_flux_floor=fractional_flux_floor,
                                    scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                                    telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                                )
                                measurement.flags["photometry_estimator"] = "adaptive_fallback_from_empirical_psf"
                                measurement.flags["empirical_psf_failure_flags"] = empirical_failure_flags
                            elif adaptive_fallback_to_circular:
                                result.empirical_psf_measurement_fallbacks += 1
                                measurement = measure_one_source(
                                    data, x=float(link.x), y=float(link.y), aperture_radius_pix=aperture_radius_pix,
                                    annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                                    exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                                    gain_e_per_adu=(None if gain_fallback else resolved_gain), read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                                    random_error_model=random_error_model, include_legacy_comparison=include_legacy_comparison,
                                    background_estimator_variance_factor=background_estimator_variance_factor,
                                    empirical_background_floor=empirical_background_floor, fractional_flux_floor=fractional_flux_floor,
                                    scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                                    telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                                    performance_timings=source_kernel_timings,
                                )
                                measurement.flags["photometry_estimator"] = "circular_fallback_from_empirical_psf"
                                measurement.flags["empirical_psf_failure_flags"] = empirical_failure_flags
                            else:
                                result.failed_measurements += 1
                                _record_failure_sample(
                                    result,
                                    frame_id=frame_id,
                                    stage="source_measurement",
                                    reason="empirical_psf_measurement_failed_fallback_disabled",
                                    frame_catalog_link_id=link.frame_catalog_link_id,
                                    catalog_source_id=source.catalog_source_id,
                                )
                                continue
                    elif geometry is not None and photometry_estimator == "spatial_psf":
                        measurement = measure_spatial_psf_source(
                            data, x=float(link.x), y=float(link.y), geometry=geometry,
                            annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                            exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                            gain_e_per_adu=(None if gain_fallback else resolved_gain),
                            read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                            random_error_model=random_error_model,
                            include_legacy_comparison=include_legacy_comparison,
                            background_estimator_variance_factor=background_estimator_variance_factor,
                            empirical_background_floor=empirical_background_floor,
                            fractional_flux_floor=fractional_flux_floor,
                            scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                            telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m,
                            airmass=frame.airmass, profile=psf_profile, moffat_beta=psf_moffat_beta,
                            fit_radius_fwhm=psf_fit_radius_fwhm, min_fit_radius_pix=psf_min_fit_radius_pix,
                            max_fit_radius_pix=psf_max_fit_radius_pix,
                            centroid_search_radius_pix=psf_centroid_search_radius_pix,
                            centroid_grid_steps=psf_centroid_grid_steps,
                            residual_clip_sigma=psf_residual_clip_sigma,
                            residual_clip_iterations=psf_residual_clip_iterations,
                        )
                        result.psf_measurements += 1
                        if measurement.flux is None and psf_fallback_to_adaptive:
                            result.psf_measurement_fallbacks += 1
                            psf_failure_flags = dict(measurement.flags or {})
                            measurement = measure_elliptical_source(
                                data, x=float(link.x), y=float(link.y), geometry=geometry,
                                annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                                exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                                gain_e_per_adu=(None if gain_fallback else resolved_gain),
                                read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                                random_error_model=random_error_model,
                                include_legacy_comparison=include_legacy_comparison,
                                background_estimator_variance_factor=background_estimator_variance_factor,
                                empirical_background_floor=empirical_background_floor,
                                fractional_flux_floor=fractional_flux_floor,
                                scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                                telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                            )
                            measurement.flags["photometry_estimator"] = "adaptive_fallback_from_spatial_psf"
                            measurement.flags["spatial_psf_failure_flags"] = psf_failure_flags
                    elif geometry is not None and photometry_estimator == "empirical_psf" and empirical_psf_fallback_to_adaptive:
                        result.empirical_psf_measurement_fallbacks += 1
                        measurement = measure_elliptical_source(
                            data, x=float(link.x), y=float(link.y), geometry=geometry,
                            annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                            exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                            gain_e_per_adu=(None if gain_fallback else resolved_gain),
                            read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                            random_error_model=random_error_model,
                            include_legacy_comparison=include_legacy_comparison,
                            background_estimator_variance_factor=background_estimator_variance_factor,
                            empirical_background_floor=empirical_background_floor,
                            fractional_flux_floor=fractional_flux_floor,
                            scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                            telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                        )
                        measurement.flags["photometry_estimator"] = "adaptive_fallback_from_empirical_psf"
                        measurement.flags["empirical_psf_fallback_reason"] = adaptive_frame_fallback_reason or "empirical_psf_model_unavailable"
                    elif geometry is not None and photometry_estimator == "adaptive_ellipse":
                        measurement = measure_elliptical_source(
                            data, x=float(link.x), y=float(link.y), geometry=geometry,
                            annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                            exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                            gain_e_per_adu=(None if gain_fallback else resolved_gain),
                            read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                            random_error_model=random_error_model,
                            include_legacy_comparison=include_legacy_comparison,
                            background_estimator_variance_factor=background_estimator_variance_factor,
                            empirical_background_floor=empirical_background_floor,
                            fractional_flux_floor=fractional_flux_floor,
                            scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                            telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m,
                            airmass=frame.airmass,
                        )
                        result.adaptive_measurements += 1
                    elif adaptive_fallback_to_circular:
                        if photometry_estimator == "adaptive_ellipse":
                            result.adaptive_measurement_fallbacks += 1
                        elif photometry_estimator == "spatial_psf":
                            result.psf_measurement_fallbacks += 1
                        else:
                            result.empirical_psf_measurement_fallbacks += 1
                        measurement = measure_one_source(
                            data, x=float(link.x), y=float(link.y), aperture_radius_pix=aperture_radius_pix,
                            annulus_inner_pix=annulus_inner_pix, annulus_outer_pix=annulus_outer_pix,
                            exptime=exptime, zero_point=zero_point, saturation_level_adu=frame_saturation,
                            gain_e_per_adu=(None if gain_fallback else resolved_gain), read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                            random_error_model=random_error_model, include_legacy_comparison=include_legacy_comparison,
                            background_estimator_variance_factor=background_estimator_variance_factor,
                            empirical_background_floor=empirical_background_floor, fractional_flux_floor=fractional_flux_floor,
                            scintillation_enabled=scintillation_enabled, scintillation_coefficient=scintillation_coefficient,
                            telescope_aperture_m=telescope_aperture_m, site_altitude_m=site_altitude_m, airmass=frame.airmass,
                            performance_timings=source_kernel_timings,
                        )
                        measurement.flags["photometry_estimator"] = f"circular_fallback_from_{photometry_estimator}"
                    else:
                        result.failed_measurements += 1
                        _record_failure_sample(
                            result,
                            frame_id=frame_id,
                            stage=(
                                "psf_model"
                                if photometry_estimator != "circular"
                                else "source_measurement"
                            ),
                            reason=(
                                f"{photometry_estimator}_model_unavailable_fallback_disabled"
                                if photometry_estimator != "circular"
                                else "measurement_unavailable_fallback_disabled"
                            ),
                            frame_catalog_link_id=link.frame_catalog_link_id,
                            catalog_source_id=source.catalog_source_id,
                        )
                        continue
                else:
                    measurement = measure_one_source(
                        data,
                        x=float(link.x),
                        y=float(link.y),
                        aperture_radius_pix=aperture_radius_pix,
                        annulus_inner_pix=annulus_inner_pix,
                        annulus_outer_pix=annulus_outer_pix,
                        exptime=exptime,
                        zero_point=zero_point,
                        saturation_level_adu=frame_saturation,
                        gain_e_per_adu=(None if gain_fallback else resolved_gain),
                        read_noise_e=(None if read_noise_fallback else resolved_read_noise),
                        random_error_model=random_error_model,
                        include_legacy_comparison=include_legacy_comparison,
                        background_estimator_variance_factor=background_estimator_variance_factor,
                        empirical_background_floor=empirical_background_floor,
                        fractional_flux_floor=fractional_flux_floor,
                        scintillation_enabled=scintillation_enabled,
                        scintillation_coefficient=scintillation_coefficient,
                        telescope_aperture_m=telescope_aperture_m,
                        site_altitude_m=site_altitude_m,
                        airmass=frame.airmass,
                        performance_timings=source_kernel_timings,
                    )
                    if photometry_estimator == "adaptive_ellipse":
                        result.adaptive_measurement_fallbacks += 1
                        measurement.flags["photometry_estimator"] = "circular_fallback_from_adaptive"
                        measurement.flags["adaptive_fallback_reason"] = adaptive_frame_fallback_reason or "psf_model_unavailable"
                    elif photometry_estimator == "spatial_psf":
                        result.psf_measurement_fallbacks += 1
                        measurement.flags["photometry_estimator"] = "circular_fallback_from_spatial_psf"
                        measurement.flags["psf_fallback_reason"] = adaptive_frame_fallback_reason or "psf_model_unavailable"
                    elif photometry_estimator == "empirical_psf":
                        result.empirical_psf_measurement_fallbacks += 1
                        measurement.flags["photometry_estimator"] = "circular_fallback_from_empirical_psf"
                        measurement.flags["empirical_psf_fallback_reason"] = adaptive_frame_fallback_reason or "empirical_psf_model_unavailable"
                measurement_kernel_elapsed = _timing_add("measurement_kernel", phase_started)
                frame_measurement_kernel_sec += measurement_kernel_elapsed
                if source_kernel_timings:
                    for key, value in source_kernel_timings.items():
                        timing_totals[key] = timing_totals.get(key, 0.0) + float(value)

                phase_started = _timing_start()
                result.measured += 1
                # Persisted diagnostics historically weight detector parameters
                # by measurement rows, not by frames. Keep the in-memory fresh-
                # generation path numerically equivalent so no DB readback is needed.
                gain_values.append(float(resolved_gain))
                read_noise_values.append(float(resolved_read_noise))
                budget = measurement.error_budget or {}
                selected_err = _finite_float(budget.get("selected_mag_err"))
                physical_err = _finite_float(budget.get("physical_mag_err"))
                legacy_err = _finite_float(budget.get("legacy_mag_err"))
                ratio = _finite_float(budget.get("physical_to_legacy_mag_err_ratio"))
                source_fraction = _finite_float(budget.get("source_poisson_fraction"))
                aperture_fraction = _finite_float(budget.get("aperture_background_fraction"))
                estimate_fraction = _finite_float(budget.get("background_estimate_fraction"))
                floor_fraction = _finite_float(budget.get("fractional_floor_fraction"))
                scintillation_fraction = _finite_float(budget.get("scintillation_fraction"))
                scintillation_sigma = _finite_float(
                    budget.get("scintillation_fractional_sigma")
                )
                background_source = str(
                    budget.get("selected_background_variance_source") or "unknown"
                )
                scintillation_available = bool(budget.get("scintillation_available"))
                if selected_err is not None:
                    result.measurements_with_selected_error += 1
                    selected_mag_errors.append(selected_err)
                    frame_selected_errors.append(selected_err)
                if physical_err is not None:
                    result.measurements_with_physical_error += 1
                    physical_mag_errors.append(physical_err)
                    frame_physical_errors.append(physical_err)
                if legacy_err is not None:
                    result.measurements_with_legacy_comparison += 1
                    legacy_mag_errors.append(legacy_err)
                    frame_legacy_errors.append(legacy_err)
                if ratio is not None:
                    physical_to_legacy_ratios.append(ratio)
                    frame_ratios.append(ratio)
                if source_fraction is not None:
                    source_poisson_fractions.append(source_fraction)
                    frame_source_fractions.append(source_fraction)
                if aperture_fraction is not None:
                    aperture_background_fractions.append(aperture_fraction)
                    frame_aperture_fractions.append(aperture_fraction)
                if estimate_fraction is not None:
                    background_estimate_fractions.append(estimate_fraction)
                    frame_estimate_fractions.append(estimate_fraction)
                if floor_fraction is not None:
                    fractional_floor_fractions.append(floor_fraction)
                    frame_floor_fractions.append(floor_fraction)
                if scintillation_fraction is not None:
                    scintillation_fractions.append(scintillation_fraction)
                    frame_scintillation_fractions.append(scintillation_fraction)
                if scintillation_sigma is not None:
                    scintillation_fractional_sigmas.append(scintillation_sigma)
                    frame_scintillation_sigmas.append(scintillation_sigma)
                if bool(budget.get("gain_fallback_used")):
                    result.measurements_gain_fallback += 1
                if bool(budget.get("read_noise_fallback_used")):
                    result.measurements_read_noise_fallback += 1
                if background_source == "empirical_annulus":
                    result.measurements_background_empirical += 1
                elif background_source == "detector_prediction":
                    result.measurements_background_detector_prediction += 1
                if scintillation_available:
                    result.measurements_scintillation_available += 1
                frame_background_sources.append(background_source)
                frame_scintillation_available.append(scintillation_available)
                if dry_run or object_id is None:
                    continue
                failure_stage = "measurement_row_build"
                rows.append(
                    {
                        "object_id": object_id,
                        "frame_id": frame.frame_id,
                        "calibration_version_id": result.calibration_version_id,
                        "x": float(link.x),
                        "y": float(link.y),
                        "flux": measurement.flux,
                        "flux_err": measurement.flux_err,
                        "mag_inst": measurement.mag_inst,
                        "mag_cal": None,
                        "mag_err": measurement.mag_err,
                        "mag_err_random": measurement.mag_err,
                        "mag_err_systematic": None,
                        "aperture_radius_pix": measurement.aperture_radius_pix,
                        "local_background": measurement.local_background,
                        "local_zp_correction": None,
                        "centroid_dx_pix": _finite_float(measurement.flags.get("psf_fit_centroid_dx_pix")),
                        "centroid_dy_pix": _finite_float(measurement.flags.get("psf_fit_centroid_dy_pix")),
                        "flags": {
                            **measurement.flags,
                            "catalog_name": source.catalog_name,
                            "catalog_source_id": source.catalog_source_id,
                            "source_id": source.source_id,
                            "annulus_inner_pix": annulus_inner_pix,
                            "annulus_outer_pix": annulus_outer_pix,
                            "zero_point": zero_point,
                            "photometry_stage": (
                                "forced_adaptive_ellipse_physical_error_v1" if photometry_estimator == "adaptive_ellipse"
                                else ("forced_spatial_psf_physical_error_v1" if photometry_estimator == "spatial_psf" else ("forced_empirical_psf_physical_error_v1" if photometry_estimator == "empirical_psf" else "forced_aperture_physical_error_v1"))
                            ),
                            "photometry_estimator_requested": photometry_estimator,
                            "random_error_model": random_error_model,
                            "random_error_model_semantics": (
                                "source_poisson_plus_measured_or_detector_background_plus_background_estimator"
                                if random_error_model == PHYSICAL_APERTURE_ERROR_MODEL
                                else "legacy_positive_flux_plus_aperture_annulus_scatter"
                            ),
                            "random_error_gain_source": gain_source,
                            "random_error_read_noise_source": read_noise_source,
                            **{f"random_error_{key}": value for key, value in (measurement.error_budget or {}).items()},
                        },
                    }
                )
                diagnostics_row_elapsed = _timing_add("diagnostics_and_row_build", phase_started)
                frame_diagnostics_row_build_sec += diagnostics_row_elapsed
            except Exception as exc:  # noqa: BLE001 - a bad source row must not abort the frame.
                result.failed_measurements += 1
                failure_reason = {
                    "object_identity": "object_identity_failed",
                    "source_measurement": "source_measurement_failed",
                    "measurement_row_build": "measurement_row_build_failed",
                }.get(failure_stage, "measurement_processing_failed")
                _record_failure_sample(
                    result,
                    frame_id=frame_id,
                    stage=failure_stage,
                    reason=failure_reason,
                    exception=exc,
                    frame_catalog_link_id=link.frame_catalog_link_id,
                    catalog_source_id=source.catalog_source_id,
                )
                continue

        source_loop_elapsed = _timing_add("source_loop", source_loop_started)
        _frame_timing_add("source_loop", source_loop_elapsed)
        _frame_timing_add("object_identity", frame_object_identity_sec)
        _frame_timing_add("measurement_kernel", frame_measurement_kernel_sec)
        _frame_timing_add("diagnostics_and_row_build", frame_diagnostics_row_build_sec)

        if rows:
            phase_started = _timing_start()
            if use_postgres_copy:
                copy_buffer.extend(rows)
                if len(copy_buffer) >= copy_buffer_rows:
                    if use_direct_copy:
                        if direct_copy_writer is None:
                            direct_copy_writer = PostgresMeasurementDirectCopyWriter.create(session)
                        direct_copy_writer.append_rows(copy_buffer)
                    else:
                        if copy_stage is None:
                            copy_stage = PostgresMeasurementCopyStage.create(session)
                        copy_stage.append_rows(copy_buffer)
                    copy_buffer.clear()
                result.inserted_or_updated += len(rows)
            else:
                result.insert_batches += upsert_measurement_rows(
                    session, rows, insert_batch_size=insert_batch_size
                )
                result.inserted_or_updated += len(rows)
            _frame_timing_add("stage_rows", _timing_add("stage_rows", phase_started))

        if any(frame_scintillation_available):
            result.frames_scintillation_available += 1

        phase_started = _timing_start()
        frame_diagnostics.append(
            {
                "frame_id": int(frame.frame_id),
                "gain_e_per_adu": float(resolved_gain),
                "gain_source": gain_source,
                "gain_fallback_used": bool(gain_fallback),
                "read_noise_e": float(resolved_read_noise),
                "read_noise_source": read_noise_source,
                "read_noise_fallback_used": bool(read_noise_fallback),
                "n_measurements": len(frame_selected_errors),
                "selected_mag_err_median": _percentile_or_none(frame_selected_errors, 50),
                "physical_mag_err_median": _percentile_or_none(frame_physical_errors, 50),
                "legacy_mag_err_median": _percentile_or_none(frame_legacy_errors, 50),
                "physical_to_legacy_mag_err_ratio_median": _percentile_or_none(frame_ratios, 50),
                "physical_to_legacy_mag_err_ratio_p10": _percentile_or_none(frame_ratios, 10),
                "physical_to_legacy_mag_err_ratio_p90": _percentile_or_none(frame_ratios, 90),
                "source_poisson_fraction_median": _percentile_or_none(frame_source_fractions, 50),
                "aperture_background_fraction_median": _percentile_or_none(frame_aperture_fractions, 50),
                "background_estimate_fraction_median": _percentile_or_none(frame_estimate_fractions, 50),
                "fractional_floor_fraction_median": _percentile_or_none(frame_floor_fractions, 50),
                "scintillation_fraction_median": _percentile_or_none(frame_scintillation_fractions, 50),
                "scintillation_fractional_sigma_median": _percentile_or_none(frame_scintillation_sigmas, 50),
                "background_variance_source": _mode_or_none(frame_background_sources),
                "background_empirical_fraction": _fraction_equal(
                    frame_background_sources, "empirical_annulus"
                ),
                "scintillation_available": any(frame_scintillation_available),
            }
        )

        if update_processing_status and not dry_run:
            frame.processing_status = FrameStatus.PHOTOMETRY_DONE
        _frame_timing_add("frame_summary", _timing_add("frame_summary", phase_started))
        if frame_started is not None:
            _frame_timing_add("frame_total", float(time.perf_counter() - frame_started))

    if use_postgres_copy and copy_buffer:
        phase_started = _timing_start()
        if use_direct_copy:
            if direct_copy_writer is None:
                direct_copy_writer = PostgresMeasurementDirectCopyWriter.create(session)
            direct_copy_writer.append_rows(copy_buffer)
        else:
            if copy_stage is None:
                copy_stage = PostgresMeasurementCopyStage.create(session)
            copy_stage.append_rows(copy_buffer)
        copy_buffer.clear()
        _timing_add("final_stage_flush", phase_started)

    if direct_copy_writer is not None:
        result.direct_copy_rows = int(direct_copy_writer.rows_written)
        result.direct_copy_batches = int(direct_copy_writer.copy_batches)
        result.direct_copy_sec = float(direct_copy_writer.copy_sec)
        result.insert_batches = int(direct_copy_writer.copy_batches)
    elif copy_stage is not None:
        phase_started = _timing_start()
        copy_stage.merge(upsert=bool(result.preexisting_measurements))
        _timing_add("stage_merge", phase_started)
        phase_started = _timing_start()
        copy_stage.close()
        _timing_add("stage_close", phase_started)
        result.staging_rows = int(copy_stage.rows_staged)
        result.staging_copy_batches = int(copy_stage.copy_batches)
        result.staging_copy_sec = float(copy_stage.staging_copy_sec)
        result.insert_select_sec = float(copy_stage.insert_select_sec)
        result.write_finalize_sec = float(copy_stage.cleanup_sec)
        # One server-side INSERT .. SELECT publishes the whole staged batch.
        result.insert_batches = 1 if copy_stage.rows_staged else 0

    # A fresh immutable BASE generation has no pre-existing rows, so the
    # diagnostics accumulated during measurement are already complete. Avoid a
    # second 50k-row JSONB readback in that common pipeline path. Legacy/partial
    # targets still reload persisted diagnostics to preserve historical semantics.
    if (
        not dry_run
        and result.calibration_version_id is not None
        and result.preexisting_measurements > 0
    ):
        phase_started = _timing_start()
        result.persisted_diagnostics_reloaded = True
        result.diagnostics_read_strategy = "SCALAR_FLAGS_V1"
        persisted = _load_persisted_error_diagnostics(
            session,
            calibration_version_id=int(result.calibration_version_id),
            frame_ids=[int(frame.frame_id) for frame in frames],
            include_legacy_comparison=include_legacy_comparison,
        )
        if persisted["n_rows"]:
            selected_mag_errors = persisted["selected_mag_errors"]
            physical_mag_errors = persisted["physical_mag_errors"]
            legacy_mag_errors = persisted["legacy_mag_errors"]
            physical_to_legacy_ratios = persisted["physical_to_legacy_ratios"]
            gain_values = persisted["gain_values"]
            read_noise_values = persisted["read_noise_values"]
            source_poisson_fractions = persisted["source_poisson_fractions"]
            aperture_background_fractions = persisted["aperture_background_fractions"]
            background_estimate_fractions = persisted["background_estimate_fractions"]
            fractional_floor_fractions = persisted["fractional_floor_fractions"]
            scintillation_fractions = persisted["scintillation_fractions"]
            scintillation_fractional_sigmas = persisted["scintillation_fractional_sigmas"]
            frame_diagnostics = persisted["frame_rows"]
            result.measurements_with_selected_error = len(selected_mag_errors)
            result.measurements_with_physical_error = len(physical_mag_errors)
            result.measurements_with_legacy_comparison = len(legacy_mag_errors)
            result.measurements_gain_fallback = int(persisted["measurements_gain_fallback"])
            result.measurements_read_noise_fallback = int(
                persisted["measurements_read_noise_fallback"]
            )
            result.frames_gain_fallback = int(persisted["frames_gain_fallback"])
            result.frames_read_noise_fallback = int(persisted["frames_read_noise_fallback"])
            result.measurements_background_empirical = int(
                persisted["measurements_background_empirical"]
            )
            result.measurements_background_detector_prediction = int(
                persisted["measurements_background_detector_prediction"]
            )
            result.measurements_scintillation_available = int(
                persisted["measurements_scintillation_available"]
            )
            result.frames_scintillation_available = int(
                persisted["frames_scintillation_available"]
            )
        _timing_add("reload_persisted_diagnostics", phase_started)

    phase_started = _timing_start()
    result.gain_e_per_adu_median = _percentile_or_none(gain_values, 50)
    result.read_noise_e_median = _percentile_or_none(read_noise_values, 50)
    result.selected_mag_err_median = _percentile_or_none(selected_mag_errors, 50)
    result.physical_mag_err_median = _percentile_or_none(physical_mag_errors, 50)
    result.legacy_mag_err_median = _percentile_or_none(legacy_mag_errors, 50)
    result.physical_to_legacy_mag_err_ratio_median = _percentile_or_none(
        physical_to_legacy_ratios, 50
    )
    result.physical_to_legacy_mag_err_ratio_p10 = _percentile_or_none(
        physical_to_legacy_ratios, 10
    )
    result.physical_to_legacy_mag_err_ratio_p90 = _percentile_or_none(
        physical_to_legacy_ratios, 90
    )
    result.source_poisson_fraction_median = _percentile_or_none(
        source_poisson_fractions, 50
    )
    result.aperture_background_fraction_median = _percentile_or_none(
        aperture_background_fractions, 50
    )
    result.background_estimate_fraction_median = _percentile_or_none(
        background_estimate_fractions, 50
    )
    result.fractional_floor_fraction_median = _percentile_or_none(
        fractional_floor_fractions, 50
    )
    result.scintillation_fraction_median = _percentile_or_none(
        scintillation_fractions, 50
    )
    result.scintillation_fractional_sigma_median = _percentile_or_none(
        scintillation_fractional_sigmas, 50
    )
    _timing_add("final_statistics", phase_started)
    if diagnostics_out_dir is not None:
        phase_started = _timing_start()
        _write_error_model_diagnostics(
            result=result,
            frame_rows=frame_diagnostics,
            out_dir=Path(diagnostics_out_dir),
            random_error_model=random_error_model,
            background_estimator_variance_factor=background_estimator_variance_factor,
            empirical_background_floor=empirical_background_floor,
            fractional_flux_floor=fractional_flux_floor,
            scintillation_enabled=scintillation_enabled,
            scintillation_coefficient=scintillation_coefficient,
            telescope_aperture_m=telescope_aperture_m,
            site_altitude_m=site_altitude_m,
        )
        _timing_add("write_error_diagnostics", phase_started)

    if not dry_run:
        phase_started = _timing_start()
        session.commit()
        _timing_add("commit", phase_started)

    if diagnose_performance:
        timing_totals["postgres_copy_staging"] = float(result.staging_copy_sec)
        timing_totals["postgres_insert_select"] = float(result.insert_select_sec)
        timing_totals["postgres_direct_copy"] = float(result.direct_copy_sec)
        timing_totals["postgres_write_finalize"] = float(result.write_finalize_sec)
        if performance_started is not None:
            timing_totals["function_total"] = float(time.perf_counter() - performance_started)
        result.timings_sec = {key: float(value) for key, value in sorted(timing_totals.items())}
        result.frame_timing_summary = {
            key: _timing_distribution(values)
            for key, values in sorted(frame_timing_samples.items())
            if values
        }
        total_sec = max(float(timing_totals.get("function_total", 0.0)), 1e-12)
        kernel_sec = max(float(timing_totals.get("measurement_kernel", 0.0)), 1e-12)
        fits_sec = max(float(timing_totals.get("fits_read", 0.0)), 1e-12)
        result.throughput = {
            "measured_sources_per_sec_total": float(result.measured) / total_sec,
            "measured_sources_per_sec_kernel": float(result.measured) / kernel_sec,
            "measurement_kernel_us_per_source": kernel_sec * 1e6 / max(int(result.measured), 1),
            "estimated_fits_mib": float(estimated_fits_bytes) / (1024.0 * 1024.0),
            "estimated_fits_mib_per_sec": (float(estimated_fits_bytes) / (1024.0 * 1024.0)) / fits_sec,
        }
    return result


def measure_one_source(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime: float | None,
    zero_point: float,
    saturation_level_adu: float | None,
    gain_e_per_adu: float | None = None,
    read_noise_e: float | None = None,
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL,
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    empirical_background_floor: bool = True,
    fractional_flux_floor: float = 0.0,
    scintillation_enabled: bool = True,
    scintillation_coefficient: float = 0.09,
    telescope_aperture_m: float | None = None,
    site_altitude_m: float | None = None,
    airmass: float | None = None,
    performance_timings: dict[str, float] | None = None,
    include_legacy_comparison: bool = True,
) -> ApertureMeasurement:
    phase_started = time.perf_counter() if performance_timings is not None else None
    flags: dict[str, object] = {}
    ny, nx = data.shape
    if not math.isfinite(x) or not math.isfinite(y):
        flags["bad_xy"] = True
        _accumulate_optional_timing(performance_timings, "source_geometry", phase_started)
        return _empty_measurement(aperture_radius_pix, flags)

    # Use a cutout covering the outer annulus. This keeps the MVP fast enough even
    # for many sources per frame and avoids allocating full-frame distance arrays.
    pad = int(math.ceil(annulus_outer_pix + 1.0))
    x0 = max(0, int(math.floor(x)) - pad)
    x1 = min(nx, int(math.floor(x)) + pad + 1)
    y0 = max(0, int(math.floor(y)) - pad)
    y1 = min(ny, int(math.floor(y)) + pad + 1)

    if x0 >= x1 or y0 >= y1:
        flags["outside_image"] = True
        _accumulate_optional_timing(performance_timings, "source_geometry", phase_started)
        return _empty_measurement(aperture_radius_pix, flags)

    if x - aperture_radius_pix < 0 or x + aperture_radius_pix > nx - 1:
        flags["aperture_touches_x_edge"] = True
    if y - aperture_radius_pix < 0 or y + aperture_radius_pix > ny - 1:
        flags["aperture_touches_y_edge"] = True
    if x - annulus_outer_pix < 0 or x + annulus_outer_pix > nx - 1:
        flags["annulus_touches_x_edge"] = True
    if y - annulus_outer_pix < 0 or y + annulus_outer_pix > ny - 1:
        flags["annulus_touches_y_edge"] = True

    cutout = data[y0:y1, x0:x1]
    yy, xx = np.indices(cutout.shape, dtype=float)
    xx = xx + x0
    yy = yy + y0
    rr2 = (xx - x) ** 2 + (yy - y) ** 2

    aperture_mask = rr2 <= aperture_radius_pix**2
    annulus_mask = (rr2 >= annulus_inner_pix**2) & (rr2 <= annulus_outer_pix**2)

    aperture_values = cutout[aperture_mask]
    annulus_values = cutout[annulus_mask]
    aperture_values = aperture_values[np.isfinite(aperture_values)]
    annulus_values = annulus_values[np.isfinite(annulus_values)]

    phase_started = _accumulate_optional_timing(
        performance_timings, "source_geometry", phase_started
    )

    if aperture_values.size == 0:
        flags["empty_aperture"] = True
        _accumulate_optional_timing(performance_timings, "source_background_flux", phase_started)
        return _empty_measurement(aperture_radius_pix, flags)
    if annulus_values.size < 5:
        flags["too_few_annulus_pixels"] = True
        local_background = float(np.nanmedian(cutout)) if np.isfinite(cutout).any() else 0.0
        annulus_std = float(np.nanstd(cutout)) if np.isfinite(cutout).any() else 0.0
    else:
        local_background = float(np.nanmedian(annulus_values))
        annulus_std = float(1.4826 * np.nanmedian(np.abs(annulus_values - local_background)))
        if not math.isfinite(annulus_std) or annulus_std <= 0:
            annulus_std = float(np.nanstd(annulus_values))

    aperture_sum = float(np.nansum(aperture_values))
    n_ap = int(aperture_values.size)
    flux = aperture_sum - local_background * n_ap

    if saturation_level_adu is not None and np.nanmax(aperture_values) >= saturation_level_adu:
        flags["saturated_or_near_saturated"] = True
        flags["aperture_max_adu"] = float(np.nanmax(aperture_values))

    phase_started = _accumulate_optional_timing(
        performance_timings, "source_background_flux", phase_started
    )

    error_budget = aperture_error_budget(
        net_flux_adu=flux,
        local_background_adu=local_background,
        annulus_sigma_adu=annulus_std,
        n_aperture_pixels=n_ap,
        n_annulus_pixels=max(1, int(annulus_values.size)),
        gain_e_per_adu=gain_e_per_adu,
        read_noise_e=read_noise_e,
        model=random_error_model,
        background_estimator_variance_factor=background_estimator_variance_factor,
        empirical_background_floor=empirical_background_floor,
        fractional_flux_floor=fractional_flux_floor,
        scintillation_enabled=scintillation_enabled,
        scintillation_coefficient=scintillation_coefficient,
        telescope_aperture_m=telescope_aperture_m,
        site_altitude_m=site_altitude_m,
        airmass=airmass,
        exptime_s=exptime,
        include_legacy_comparison=include_legacy_comparison,
    )
    flux_err = error_budget.selected_flux_err_adu

    phase_started = _accumulate_optional_timing(
        performance_timings, "source_error_model", phase_started
    )

    mag_inst = None
    mag_err = None
    if flux > 0:
        rate = flux / exptime if exptime and exptime > 0 else flux
        if rate > 0:
            mag_inst = float(-2.5 * math.log10(rate) + zero_point)
            mag_err = error_budget.selected_mag_err
    else:
        flags["non_positive_flux"] = True

    _accumulate_optional_timing(
        performance_timings, "source_magnitude_finalize", phase_started
    )

    return ApertureMeasurement(
        flux=float(flux),
        flux_err=flux_err,
        mag_inst=mag_inst,
        mag_err=mag_err,
        local_background=local_background,
        aperture_radius_pix=float(aperture_radius_pix),
        flags=flags,
        error_budget=error_budget.to_dict(),
    )


def _select_frames(
    session: Session,
    *,
    frame_ids: list[int] | None,
    instrument: str | None,
    instrument_unit_id: int | None,
    only_wcs_ok: bool,
    limit: int | None,
) -> list[Frame]:
    stmt = select(Frame).order_by(Frame.frame_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    if instrument:
        stmt = stmt.where(Frame.instrument == instrument)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if only_wcs_ok:
        stmt = stmt.where(Frame.wcs_status == "HEADER_WCS_OK")
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _select_frame_links(
    session: Session,
    *,
    frame_id: int,
    catalog_name: str | None,
    limit: int | None,
) -> list[tuple[FrameCatalogLink, CatalogSource]]:
    stmt = (
        select(FrameCatalogLink, CatalogSource)
        .join(CatalogSource, CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id)
        .where(FrameCatalogLink.frame_id == frame_id)
        .where(FrameCatalogLink.in_footprint.is_(True))
        .order_by(FrameCatalogLink.frame_catalog_link_id)
    )
    if catalog_name:
        stmt = stmt.where(CatalogSource.catalog_name == catalog_name)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(link, source) for link, source in session.execute(stmt).all()]


def _select_frame_links_bulk(
    session: Session,
    *,
    frame_ids: list[int],
    catalog_name: str | None,
    limit_per_frame: int | None,
) -> dict[int, list[tuple[ForcedFrameLink, ForcedCatalogSource]]]:
    """Prefetch the forced-photometry link scope without ORM hydration.

    The hot path historically executed one ``FrameCatalogLink + CatalogSource``
    ORM join per frame. Here link geometry is fetched once for the full frame
    scope, then the much smaller unique catalog-source set is fetched once.
    Per-frame ordering and ``limit_per_frame`` semantics are preserved exactly.
    """

    if not frame_ids:
        return {}

    stmt = (
        select(
            FrameCatalogLink.frame_catalog_link_id.label("frame_catalog_link_id"),
            FrameCatalogLink.frame_id.label("frame_id"),
            FrameCatalogLink.catalog_source_id.label("catalog_source_id"),
            FrameCatalogLink.x.label("x"),
            FrameCatalogLink.y.label("y"),
        )
        .where(FrameCatalogLink.frame_id.in_([int(value) for value in frame_ids]))
        .where(FrameCatalogLink.in_footprint.is_(True))
        .order_by(FrameCatalogLink.frame_id, FrameCatalogLink.frame_catalog_link_id)
    )
    if catalog_name:
        stmt = stmt.join(
            CatalogSource,
            CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id,
        ).where(CatalogSource.catalog_name == catalog_name)

    raw_links = list(session.execute(stmt).mappings().all())
    link_rows_by_frame: dict[int, list[ForcedFrameLink]] = {}
    source_ids: set[int] = set()
    per_frame_limit = None if limit_per_frame is None else max(0, int(limit_per_frame))
    for row in raw_links:
        frame_id = int(row["frame_id"])
        bucket = link_rows_by_frame.setdefault(frame_id, [])
        if per_frame_limit is not None and len(bucket) >= per_frame_limit:
            continue
        link = ForcedFrameLink(
            frame_catalog_link_id=int(row["frame_catalog_link_id"]),
            frame_id=frame_id,
            catalog_source_id=int(row["catalog_source_id"]),
            x=_finite_float(row["x"]),
            y=_finite_float(row["y"]),
        )
        bucket.append(link)
        source_ids.add(link.catalog_source_id)

    if not source_ids:
        return {frame_id: [] for frame_id in link_rows_by_frame}

    source_stmt = (
        select(
            CatalogSource.catalog_source_id.label("catalog_source_id"),
            CatalogSource.catalog_name.label("catalog_name"),
            CatalogSource.source_id.label("source_id"),
            CatalogSource.ra_deg.label("ra_deg"),
            CatalogSource.dec_deg.label("dec_deg"),
            CatalogSource.epoch.label("epoch"),
            CatalogSource.pmra_masyr.label("pmra_masyr"),
            CatalogSource.pmdec_masyr.label("pmdec_masyr"),
            CatalogSource.ref_mags.label("ref_mags"),
            CatalogSource.colors.label("colors"),
        )
        .where(CatalogSource.catalog_source_id.in_(sorted(source_ids)))
        .order_by(CatalogSource.catalog_source_id)
    )
    source_rows = list(session.execute(source_stmt).mappings().all())
    sources = {
        int(row["catalog_source_id"]): ForcedCatalogSource(
            catalog_source_id=int(row["catalog_source_id"]),
            catalog_name=str(row["catalog_name"]),
            source_id=str(row["source_id"]),
            ra_deg=float(row["ra_deg"]),
            dec_deg=float(row["dec_deg"]),
            epoch=_finite_float(row["epoch"]),
            pmra_masyr=_finite_float(row["pmra_masyr"]),
            pmdec_masyr=_finite_float(row["pmdec_masyr"]),
            ref_mags=dict(row["ref_mags"] or {}),
            colors=dict(row["colors"] or {}),
        )
        for row in source_rows
    }

    grouped: dict[int, list[tuple[ForcedFrameLink, ForcedCatalogSource]]] = {}
    for frame_id, links in link_rows_by_frame.items():
        items: list[tuple[ForcedFrameLink, ForcedCatalogSource]] = []
        for link in links:
            source = sources.get(link.catalog_source_id)
            if source is not None:
                items.append((link, source))
        grouped[frame_id] = items
    return grouped


def _forced_measurement_scope_is_complete(
    session: Session,
    *,
    calibration_version_id: int,
    frames: list[Frame],
    catalog_name: str | None,
    limit_links: int | None,
) -> bool:
    """Return True only when every requested catalog link is already materialized.

    This preflight is required before reusing an immutable base-photometry head.
    Without it, a partially populated historical generation could be selected and
    the normal per-frame repair path would append rows to an immutable product.
    """

    frame_ids = [int(frame.frame_id) for frame in frames]
    required_counts = _select_frame_link_counts_bulk(
        session,
        frame_ids=frame_ids,
        catalog_name=catalog_name,
        limit_per_frame=limit_links,
    )
    existing_counts = _existing_measurement_counts_bulk(
        session,
        calibration_version_id=int(calibration_version_id),
        frame_ids=frame_ids,
    )
    return _measurement_scope_counts_are_complete(
        frame_ids=frame_ids,
        required_link_counts=required_counts,
        existing_measurement_counts=existing_counts,
    )


def _select_frame_link_counts_bulk(
    session: Session,
    *,
    frame_ids: list[int],
    catalog_name: str | None,
    limit_per_frame: int | None,
) -> dict[int, int]:
    """Return required forced-photometry link counts with one grouped query.

    Only counts are needed when validating/reusing an immutable BASE generation.
    Applying ``min(count, limit_per_frame)`` is exactly equivalent to the historic
    per-frame ``ORDER BY ... LIMIT`` for the completeness predicate because that
    predicate compares row cardinality only, not specific link identities.
    """

    if not frame_ids:
        return {}

    stmt = (
        select(
            FrameCatalogLink.frame_id.label("frame_id"),
            func.count().label("n_links"),
        )
        .where(FrameCatalogLink.frame_id.in_([int(value) for value in frame_ids]))
        .where(FrameCatalogLink.in_footprint.is_(True))
        .group_by(FrameCatalogLink.frame_id)
    )
    if catalog_name:
        stmt = stmt.join(
            CatalogSource,
            CatalogSource.catalog_source_id == FrameCatalogLink.catalog_source_id,
        ).where(CatalogSource.catalog_name == catalog_name)

    rows = session.execute(stmt).mappings().all()
    per_frame_limit = None if limit_per_frame is None else max(0, int(limit_per_frame))
    counts: dict[int, int] = {}
    for row in rows:
        value = int(row["n_links"] or 0)
        if per_frame_limit is not None:
            value = min(value, per_frame_limit)
        counts[int(row["frame_id"])] = value
    return counts


def _existing_measurement_counts_bulk(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: list[int],
) -> dict[int, int]:
    """Return existing measurement cardinality for every frame in one query."""

    if not frame_ids:
        return {}
    stmt = (
        select(
            Measurement.frame_id.label("frame_id"),
            func.count().label("n_measurements"),
        )
        .where(Measurement.calibration_version_id == int(calibration_version_id))
        .where(Measurement.frame_id.in_([int(value) for value in frame_ids]))
        .group_by(Measurement.frame_id)
    )
    return {
        int(row["frame_id"]): int(row["n_measurements"] or 0)
        for row in session.execute(stmt).mappings().all()
    }


def _measurement_scope_counts_are_complete(
    *,
    frame_ids: list[int],
    required_link_counts: dict[int, int],
    existing_measurement_counts: dict[int, int],
) -> bool:
    for frame_id in frame_ids:
        required = int(required_link_counts.get(int(frame_id), 0))
        if required <= 0:
            continue
        if int(existing_measurement_counts.get(int(frame_id), 0)) < required:
            return False
    return True


def _existing_calibration_measurement_count(
    session: Session, *, calibration_version_id: int
) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(Measurement)
        .where(Measurement.calibration_version_id == calibration_version_id)
    )
    return int(value or 0)


def _existing_measurement_count(session: Session, *, frame_id: int, calibration_version_id: int) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(Measurement)
        .where(Measurement.frame_id == frame_id)
        .where(Measurement.calibration_version_id == calibration_version_id)
    )
    return int(value or 0)


def _get_or_create_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    parameters: dict,
    immutable_generation: bool = False,
    reuse_completed_immutable: bool = False,
) -> CalibrationVersion:
    existing = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    if existing is not None and str(getattr(existing, "lifecycle_status", "") or "") not in {
        "COMPLETE_IMMUTABLE",
        "SUPERSEDED_IMMUTABLE",
    }:
        existing_parameters = existing.parameters if isinstance(existing.parameters, dict) else {}
        existing_model = existing_parameters.get("random_error_model") or LEGACY_APERTURE_ERROR_MODEL
        requested_model = parameters.get("random_error_model") or LEGACY_APERTURE_ERROR_MODEL
        if existing_model != requested_model:
            raise ValueError(
                "Calibration name already exists with a different random-error model: "
                f"name={name!r}, existing={existing_model!r}, requested={requested_model!r}. "
                "Use a distinct calibration name instead of reusing incompatible rows."
            )
    cv = prepare_calibration_version_for_write(
        session,
        logical_name=name,
        model_name=f"forced_aperture_{parameters.get('random_error_model') or LEGACY_APERTURE_ERROR_MODEL}",
        instrument_unit_id=instrument_unit_id,
        instrument_profile={},
        used_catalogs={"catalog_name": parameters.get("catalog_name")},
        parameters=parameters,
        notes=(
            "Forced circular aperture photometry with local annulus-median background "
            "and explicit random-error budget provenance."
        ),
        immutable_generation=immutable_generation,
        reuse_completed_immutable=reuse_completed_immutable,
    )
    expected = {
        "product_kind": "BASE_PHOTOMETRY",
        "product_role": "BASE",
        "materialization_kind": "FULL_MEASUREMENTS",
    }
    immutable = str(getattr(cv, "lifecycle_status", "") or "") in {
        "COMPLETE_IMMUTABLE",
        "SUPERSEDED_IMMUTABLE",
    }
    mismatches = [
        key for key, value in expected.items()
        if immutable and str(getattr(cv, key, "") or "") != value
    ]
    if mismatches:
        raise ValueError(
            "Existing immutable forced-photometry calibration is not a BASE measurement product: "
            f"calibration_version_id={cv.calibration_version_id}, fields={mismatches}"
        )
    if not immutable:
        for key, value in expected.items():
            setattr(cv, key, value)
        session.flush()
    return cv


def _get_or_create_object_id(
    session: Session, source: CatalogSource | ForcedCatalogSource, *, dry_run: bool
) -> int | None:
    result = get_or_create_object_for_catalog_source(session, source, dry_run=dry_run)
    return int(result.object_id) if result.object_id is not None else None


def _resolve_saturation_level(*, explicit: float | None, frame: Frame, header) -> float | None:
    if explicit is not None:
        return explicit
    if frame.instrument_unit is not None and frame.instrument_unit.saturation_level_adu is not None:
        return float(frame.instrument_unit.saturation_level_adu)
    for key in ("SATURATE", "SATLEVEL", "SATUR", "MAXADU"):
        value = header.get(key)
        try:
            if value is not None:
                return float(value)
        except Exception:
            pass
    return None


def _safe_positive_float(value) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _empty_measurement(aperture_radius_pix: float, flags: dict) -> ApertureMeasurement:
    return ApertureMeasurement(
        flux=None,
        flux_err=None,
        mag_inst=None,
        mag_err=None,
        local_background=None,
        aperture_radius_pix=float(aperture_radius_pix),
        flags=flags,
        error_budget=None,
    )


def _load_persisted_error_diagnostics(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: list[int],
    include_legacy_comparison: bool = True,
) -> dict[str, Any]:
    stmt = (
        select(
            Measurement.frame_id.label("frame_id"),
            Measurement.mag_err_random.label("mag_err_random"),
            Measurement.mag_err.label("mag_err"),
            Measurement.flags.label("flags"),
        )
        .where(Measurement.calibration_version_id == int(calibration_version_id))
        .order_by(Measurement.frame_id, Measurement.measurement_id)
    )
    if frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(frame_ids))
    measurements = list(session.execute(stmt).mappings().all())

    selected_mag_errors: list[float] = []
    physical_mag_errors: list[float] = []
    legacy_mag_errors: list[float] = []
    physical_to_legacy_ratios: list[float] = []
    gain_values: list[float] = []
    read_noise_values: list[float] = []
    source_poisson_fractions: list[float] = []
    aperture_background_fractions: list[float] = []
    background_estimate_fractions: list[float] = []
    fractional_floor_fractions: list[float] = []
    scintillation_fractions: list[float] = []
    scintillation_fractional_sigmas: list[float] = []
    measurements_gain_fallback = 0
    measurements_read_noise_fallback = 0
    measurements_background_empirical = 0
    measurements_background_detector_prediction = 0
    measurements_scintillation_available = 0
    by_frame: dict[int, dict[str, Any]] = {}

    for measurement in measurements:
        flags_value = measurement["flags"]
        flags = flags_value if isinstance(flags_value, dict) else {}
        frame_id = int(measurement["frame_id"])
        frame = by_frame.setdefault(
            frame_id,
            {
                "gain_values": [],
                "read_noise_values": [],
                "gain_sources": [],
                "read_noise_sources": [],
                "gain_fallback": [],
                "read_noise_fallback": [],
                "selected": [],
                "physical": [],
                "legacy": [],
                "ratio": [],
                "source_fraction": [],
                "aperture_fraction": [],
                "estimate_fraction": [],
                "floor_fraction": [],
                "scintillation_fraction": [],
                "scintillation_sigma": [],
                "background_sources": [],
                "scintillation_available": [],
            },
        )

        selected = _finite_float(measurement["mag_err_random"] or measurement["mag_err"])
        physical = _finite_float(flags.get("random_error_physical_mag_err"))
        legacy = (
            _finite_float(flags.get("random_error_legacy_mag_err"))
            if include_legacy_comparison
            else None
        )
        ratio = (
            _finite_float(
                flags.get("random_error_physical_to_legacy_mag_err_ratio")
            )
            if include_legacy_comparison
            else None
        )
        gain = _finite_float(flags.get("random_error_gain_e_per_adu"))
        read_noise = _finite_float(flags.get("random_error_read_noise_e"))
        source_fraction = _finite_float(flags.get("random_error_source_poisson_fraction"))
        aperture_fraction = _finite_float(
            flags.get("random_error_aperture_background_fraction")
        )
        estimate_fraction = _finite_float(
            flags.get("random_error_background_estimate_fraction")
        )
        floor_fraction = _finite_float(flags.get("random_error_fractional_floor_fraction"))
        scintillation_fraction = _finite_float(
            flags.get("random_error_scintillation_fraction")
        )
        scintillation_sigma = _finite_float(
            flags.get("random_error_scintillation_fractional_sigma")
        )
        gain_fallback = bool(flags.get("random_error_gain_fallback_used"))
        read_noise_fallback = bool(flags.get("random_error_read_noise_fallback_used"))
        background_source = str(
            flags.get("random_error_selected_background_variance_source") or "unknown"
        )
        scintillation_available = bool(flags.get("random_error_scintillation_available"))

        for value, global_values, frame_key in (
            (selected, selected_mag_errors, "selected"),
            (physical, physical_mag_errors, "physical"),
            (legacy, legacy_mag_errors, "legacy"),
            (ratio, physical_to_legacy_ratios, "ratio"),
            (source_fraction, source_poisson_fractions, "source_fraction"),
            (aperture_fraction, aperture_background_fractions, "aperture_fraction"),
            (estimate_fraction, background_estimate_fractions, "estimate_fraction"),
            (floor_fraction, fractional_floor_fractions, "floor_fraction"),
            (scintillation_fraction, scintillation_fractions, "scintillation_fraction"),
            (scintillation_sigma, scintillation_fractional_sigmas, "scintillation_sigma"),
        ):
            if value is not None:
                global_values.append(value)
                frame[frame_key].append(value)
        if gain is not None:
            gain_values.append(gain)
            frame["gain_values"].append(gain)
        if read_noise is not None:
            read_noise_values.append(read_noise)
            frame["read_noise_values"].append(read_noise)
        if flags.get("random_error_gain_source") is not None:
            frame["gain_sources"].append(str(flags.get("random_error_gain_source")))
        if flags.get("random_error_read_noise_source") is not None:
            frame["read_noise_sources"].append(
                str(flags.get("random_error_read_noise_source"))
            )
        frame["gain_fallback"].append(gain_fallback)
        frame["read_noise_fallback"].append(read_noise_fallback)
        frame["background_sources"].append(background_source)
        frame["scintillation_available"].append(scintillation_available)
        measurements_gain_fallback += int(gain_fallback)
        measurements_read_noise_fallback += int(read_noise_fallback)
        measurements_background_empirical += int(background_source == "empirical_annulus")
        measurements_background_detector_prediction += int(
            background_source == "detector_prediction"
        )
        measurements_scintillation_available += int(scintillation_available)

    frame_rows: list[dict[str, Any]] = []
    for frame_id, values in sorted(by_frame.items()):
        frame_rows.append(
            {
                "frame_id": frame_id,
                "gain_e_per_adu": _percentile_or_none(values["gain_values"], 50),
                "gain_source": _mode_or_none(values["gain_sources"]),
                "gain_fallback_used": any(values["gain_fallback"]),
                "read_noise_e": _percentile_or_none(values["read_noise_values"], 50),
                "read_noise_source": _mode_or_none(values["read_noise_sources"]),
                "read_noise_fallback_used": any(values["read_noise_fallback"]),
                "n_measurements": len(values["selected"]),
                "selected_mag_err_median": _percentile_or_none(values["selected"], 50),
                "physical_mag_err_median": _percentile_or_none(values["physical"], 50),
                "legacy_mag_err_median": _percentile_or_none(values["legacy"], 50),
                "physical_to_legacy_mag_err_ratio_median": _percentile_or_none(
                    values["ratio"], 50
                ),
                "physical_to_legacy_mag_err_ratio_p10": _percentile_or_none(
                    values["ratio"], 10
                ),
                "physical_to_legacy_mag_err_ratio_p90": _percentile_or_none(
                    values["ratio"], 90
                ),
                "source_poisson_fraction_median": _percentile_or_none(
                    values["source_fraction"], 50
                ),
                "aperture_background_fraction_median": _percentile_or_none(
                    values["aperture_fraction"], 50
                ),
                "background_estimate_fraction_median": _percentile_or_none(
                    values["estimate_fraction"], 50
                ),
                "fractional_floor_fraction_median": _percentile_or_none(
                    values["floor_fraction"], 50
                ),
                "scintillation_fraction_median": _percentile_or_none(
                    values["scintillation_fraction"], 50
                ),
                "scintillation_fractional_sigma_median": _percentile_or_none(
                    values["scintillation_sigma"], 50
                ),
                "background_variance_source": _mode_or_none(values["background_sources"]),
                "background_empirical_fraction": _fraction_equal(
                    values["background_sources"], "empirical_annulus"
                ),
                "scintillation_available": any(values["scintillation_available"]),
            }
        )

    return {
        "n_rows": len(measurements),
        "selected_mag_errors": selected_mag_errors,
        "physical_mag_errors": physical_mag_errors,
        "legacy_mag_errors": legacy_mag_errors,
        "physical_to_legacy_ratios": physical_to_legacy_ratios,
        "gain_values": gain_values,
        "read_noise_values": read_noise_values,
        "source_poisson_fractions": source_poisson_fractions,
        "aperture_background_fractions": aperture_background_fractions,
        "background_estimate_fractions": background_estimate_fractions,
        "fractional_floor_fractions": fractional_floor_fractions,
        "scintillation_fractions": scintillation_fractions,
        "scintillation_fractional_sigmas": scintillation_fractional_sigmas,
        "measurements_gain_fallback": measurements_gain_fallback,
        "measurements_read_noise_fallback": measurements_read_noise_fallback,
        "measurements_background_empirical": measurements_background_empirical,
        "measurements_background_detector_prediction": (
            measurements_background_detector_prediction
        ),
        "measurements_scintillation_available": measurements_scintillation_available,
        "frames_gain_fallback": sum(
            1 for values in by_frame.values() if any(values["gain_fallback"])
        ),
        "frames_read_noise_fallback": sum(
            1 for values in by_frame.values() if any(values["read_noise_fallback"])
        ),
        "frames_scintillation_available": sum(
            1 for values in by_frame.values() if any(values["scintillation_available"])
        ),
        "frame_rows": frame_rows,
    }


def _mode_or_none(values: Iterable[str]) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    if not counts:
        return None
    return sorted(counts, key=lambda value: (-counts[value], value))[0]


def _fraction_equal(values: Iterable[str], expected: str) -> float | None:
    items = [str(value) for value in values]
    if not items:
        return None
    return sum(value == expected for value in items) / len(items)


def _resolve_gain_e_per_adu(*, explicit: float | None, frame: Frame) -> tuple[float, str, bool]:
    value = _safe_positive_float(explicit)
    if value is not None:
        return float(value), "explicit_override", False
    value = _safe_positive_float(frame.gain)
    if value is not None:
        return float(value), "frame_metadata", False
    if frame.instrument_unit is not None:
        value = _safe_positive_float(frame.instrument_unit.nominal_gain_e_per_adu)
        if value is not None:
            return float(value), "instrument_unit_nominal", False
    return 1.0, "fallback_unity", True


def _resolve_read_noise_e(*, explicit: float | None, frame: Frame) -> tuple[float, str, bool]:
    value = _safe_nonnegative_float(explicit)
    if value is not None:
        return float(value), "explicit_override", False
    value = _safe_nonnegative_float(frame.read_noise)
    if value is not None:
        return float(value), "frame_metadata", False
    if frame.instrument_unit is not None:
        value = _safe_nonnegative_float(frame.instrument_unit.nominal_read_noise_e)
        if value is not None:
            return float(value), "instrument_unit_nominal", False
    return 0.0, "fallback_zero", True


def _safe_nonnegative_float(value) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _percentile_or_none(values: Iterable[float], percentile: float) -> float | None:
    finite = [float(value) for value in values if _finite_float(value) is not None]
    if not finite:
        return None
    return float(np.percentile(np.asarray(finite, dtype=float), float(percentile)))


def _random_error_model_tag(model: str) -> str:
    if model == PHYSICAL_APERTURE_ERROR_MODEL:
        return "errphys_v1"
    if model == LEGACY_APERTURE_ERROR_MODEL:
        return "errlegacy_v1"
    raise ValueError(f"Unsupported aperture random-error model: {model!r}")


def _write_error_model_diagnostics(
    *,
    result: ForcedPhotometryResult,
    frame_rows: list[dict[str, Any]],
    out_dir: Path,
    random_error_model: str,
    background_estimator_variance_factor: float,
    empirical_background_floor: bool,
    fractional_flux_floor: float,
    scintillation_enabled: bool,
    scintillation_coefficient: float,
    telescope_aperture_m: float | None,
    site_altitude_m: float | None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_path = out_dir / "random_error_model_frames.csv"
    summary_path = out_dir / "random_error_model_summary.json"

    fieldnames = [
        "frame_id",
        "gain_e_per_adu",
        "gain_source",
        "gain_fallback_used",
        "read_noise_e",
        "read_noise_source",
        "read_noise_fallback_used",
        "n_measurements",
        "selected_mag_err_median",
        "physical_mag_err_median",
        "legacy_mag_err_median",
        "physical_to_legacy_mag_err_ratio_median",
        "physical_to_legacy_mag_err_ratio_p10",
        "physical_to_legacy_mag_err_ratio_p90",
        "source_poisson_fraction_median",
        "aperture_background_fraction_median",
        "background_estimate_fraction_median",
        "fractional_floor_fraction_median",
        "scintillation_fraction_median",
        "scintillation_fractional_sigma_median",
        "background_variance_source",
        "background_empirical_fraction",
        "scintillation_available",
    ]
    with frames_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(frame_rows)

    result.diagnostics_frames_csv = str(frames_path)
    result.diagnostics_summary_json = str(summary_path)
    summary = {
        "algorithm": "physical_aperture_random_error_v1",
        "selected_model": random_error_model,
        "legacy_comparison_model": LEGACY_APERTURE_ERROR_MODEL,
        "physical_model": PHYSICAL_APERTURE_ERROR_MODEL,
        "variance_units": "ADU^2",
        "magnitude_error_factor": 2.5 / math.log(10.0),
        "background_estimator": "annulus_median",
        "background_estimator_variance_factor": float(
            background_estimator_variance_factor
        ),
        "empirical_background_floor": bool(empirical_background_floor),
        "fractional_flux_floor": float(fractional_flux_floor),
        "scintillation_enabled": bool(scintillation_enabled),
        "scintillation_coefficient": float(scintillation_coefficient),
        "telescope_aperture_m": _safe_positive_float(telescope_aperture_m),
        "site_altitude_m": _finite_float(site_altitude_m),
        "selected_variance_formula": (
            "source_poisson + n_ap*max(empirical_annulus_variance, detector_sky_plus_read_variance) "
            "+ estimator_factor*n_ap^2/n_sky*selected_background_variance "
            "+ (net_flux*fractional_floor)^2 + (net_flux*scintillation_fraction)^2"
            if random_error_model == PHYSICAL_APERTURE_ERROR_MODEL
            else "max(net_flux,0) + n_ap*annulus_sigma^2"
        ),
        "result": {
            key: value
            for key, value in result.__dict__.items()
            if key not in {"diagnostics_summary_json", "diagnostics_frames_csv"}
        },
        "diagnostics_summary_json": str(summary_path),
        "diagnostics_frames_csv": str(frames_path),
        "frames": frame_rows,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _fmt_float(value: float) -> str:
    text = f"{value:g}"
    return text.replace(".", "p").replace("-", "m")
