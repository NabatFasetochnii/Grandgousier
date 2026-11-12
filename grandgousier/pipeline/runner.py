from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import bindparam, text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.pipeline.provenance import summarize_candidate_ensemble_exclusions
from grandgousier.pipeline.profiles import (
    normalize_stage2_profile,
    resolve_stage2_profile_settings,
)
from grandgousier.pipeline.candidate_ensemble_policy import (
    apply_candidate_ensemble_growth_policy,
    normalize_candidate_ensemble_growth_policy,
)

from grandgousier.artifacts.registry import (
    capture_tree_state,
    register_run_log_artifacts,
    run_artifact_directory,
    snapshot_changed_export_tree,
)

from grandgousier.astrometry.wcs_check import check_frames_wcs
from grandgousier.calibration.aperture_correction import apply_aperture_correction
from grandgousier.calibration.aperture_zp import aperture_zp_calibrate
from grandgousier.calibration.color_calibration import apply_color_calibration
from grandgousier.calibration.naming import (
    candidate_excluded_calibration_name,
    compact_generated_calibration_name,
)
from grandgousier.calibration.ensemble import ensemble_calibrate_measurements
from grandgousier.calibration.spatial_ensemble import apply_spatial_ensemble_correction
from grandgousier.calibration.photometric_frame_qc import (
    BAD_FRAME_REJECT_FLAG,
    PHOTOMETRIC_FRAME_QC_ALGORITHM,
    apply_photometric_frame_qc,
)
from grandgousier.calibration.provenance import stamp_calibration_product
from grandgousier.catalogs.frame_match import match_catalog_to_frames
from grandgousier.photometry.error_model import (
    LEGACY_APERTURE_ERROR_MODEL,
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
)
from grandgousier.photometry.forced_aperture import run_forced_aperture_photometry
from grandgousier.photometry.science_photometry import materialize_science_photometry
from grandgousier.photometry.source_detection import detect_sources_in_frames
from grandgousier.quality.detection_catalog_qc import (
    aggregate_detection_catalog_support,
    match_detections_to_catalog,
)
from grandgousier.quality.candidate_aperture_stability import (
    compute_candidate_aperture_stability_qc,
    export_candidate_aperture_stability_csv,
)
from grandgousier.quality.field_report import (
    build_field_qc_report,
    export_field_qc_json,
    export_field_qc_tables,
)
from grandgousier.quality.lightcurve_quality import (
    CalibrationAnalysisSnapshot,
    derive_color_calibration_analysis_snapshot,
    load_calibration_analysis_snapshot,
)
from grandgousier.quality.object_quality import (
    ObjectQcSupportSnapshot,
    compute_object_qc,
    load_object_qc_support_snapshot,
)
from grandgousier.quality.random_error_validation import validate_random_error_model
from grandgousier.quality.psf_field import analyze_psf_field
from grandgousier.quality.candidate_triage import LOW_AMPLITUDE_DIAGNOSTIC_COLUMNS
from grandgousier.quality.candidate_axis_correlation import (
    annotate_candidate_axis_correlations,
    export_candidate_axis_correlation,
)
from grandgousier.quality.frame_pre_qc import filter_bad_pre_qc_frame_ids, run_frame_pre_qc
from grandgousier.lightcurves.product import light_curve_reject_flags
from grandgousier.lightcurves.candidates import export_accepted_candidate_light_curves
from grandgousier.lightcurves.target import (
    TargetResolution,
    export_target_light_curve_product,
    resolve_explicit_target_catalog_source,
    resolve_target_object,
    validate_target_coordinates,
)
from grandgousier.logging_utils import (
    PipelineFileLogger,
    sanitize_diagnostic_text,
    sanitize_jsonable_context,
)
from grandgousier.archive.fields_series import (
    load_series_selection,
    mark_series_pipeline_result,
    mark_series_pipeline_started,
)


PIPELINE_RECIPE_VERSION = "field_pipeline_v9"
NO_USABLE_FRAMES_STATUS = "DONE_NO_SCIENCE_FRAMES"
FAILED_PRECONDITION_ENSEMBLE_STATUS = "FAILED_PRECONDITION_ENSEMBLE"


class EnsemblePreconditionError(RuntimeError):
    """Raised when conventional ensemble calibration cannot produce usable frame offsets."""

    status = FAILED_PRECONDITION_ENSEMBLE_STATUS

    def __init__(self, message: str, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.diagnostics = dict(diagnostics)


class FramePreQcExclusionPreconditionError(RuntimeError):
    """Raised when pre-QC exclusion cannot establish a safe frame scope."""

    status = "FAILED_PRECONDITION"


def _ensemble_precondition_diagnostics(base_result: Any, *, min_ref_stars_per_frame: int) -> dict[str, Any]:
    return {
        "base_ensemble_reference_candidates": int(getattr(base_result, "reference_candidates", 0) or 0),
        "base_ensemble_usable_reference_objects": int(getattr(base_result, "usable_reference_objects", 0) or 0),
        "base_ensemble_required_references_per_frame": int(min_ref_stars_per_frame),
        "base_ensemble_selected_frames": int(getattr(base_result, "selected_frames", 0) or 0),
        "base_ensemble_frames_with_offsets": int(getattr(base_result, "frames_with_offsets", 0) or 0),
        "base_ensemble_calibrated": int(getattr(base_result, "calibrated", 0) or 0),
        "base_ensemble_target_calibration_version_id": getattr(base_result, "target_calibration_version_id", None),
    }


def _raise_if_ensemble_unusable(base_result: Any, *, min_ref_stars_per_frame: int) -> None:
    diagnostics = _ensemble_precondition_diagnostics(
        base_result, min_ref_stars_per_frame=min_ref_stars_per_frame
    )
    if (
        diagnostics["base_ensemble_usable_reference_objects"] < int(min_ref_stars_per_frame)
        or diagnostics["base_ensemble_selected_frames"] <= 0
        or diagnostics["base_ensemble_frames_with_offsets"] <= 0
        or diagnostics["base_ensemble_calibrated"] <= 0
    ):
        raise EnsemblePreconditionError(
            "ensemble calibration produced no usable complete frame solution: "
            f"usable_reference_objects={diagnostics['base_ensemble_usable_reference_objects']}, "
            f"required_per_frame={diagnostics['base_ensemble_required_references_per_frame']}, "
            f"selected_frames={diagnostics['base_ensemble_selected_frames']}, "
            f"frames_with_offsets={diagnostics['base_ensemble_frames_with_offsets']}, "
            f"calibrated={diagnostics['base_ensemble_calibrated']}",
            diagnostics,
        )


def _frame_ids_for_step(frame_ids: list[int]) -> list[int]:
    """Return an explicit frame selection for low-level pipeline steps.

    Several low-level functions interpret ``None`` as "all frames for this
    instrument unit".  After pre-photometry QC, however, an empty list is a
    real selection meaning "process no frames".  The runner must therefore
    never convert an explicit empty selection to ``None``.
    """

    return list(frame_ids)


@dataclass
class PipelineStepRecord:
    step_name: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    elapsed_sec: float | None = None


@dataclass
class PipelineRunResult:
    run_uid: str
    pipeline_run_id: int | None
    status: str
    calibration_names: dict[str, str]
    outputs: dict[str, Any] = field(default_factory=dict)
    log_files: dict[str, str] = field(default_factory=dict)
    steps: list[PipelineStepRecord] = field(default_factory=list)
    quality_policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldPipelineConfig:
    catalog_name: str
    calibration_prefix: str
    instrument_unit_id: int | None = None
    field_id: int | None = None
    series_id: int | None = None
    frame_ids: list[int] | None = None
    run_uid: str | None = None
    out_dir: str | None = None
    artifact_root: str | None = None
    margin_pix: float = 2.0
    aperture_radius_pix: float = 3.0
    photometry_estimator: str = "adaptive_ellipse"
    adaptive_detection_algorithm: str = "numpy_localmax_v1"
    adaptive_min_psf_snr: float = 20.0
    adaptive_max_psf_sources_per_frame: int = 500
    adaptive_psf_cutout_radius_pix: int = 7
    adaptive_psf_min_isolation_pix: float = 12.0
    adaptive_interpolation_neighbors: int = 24
    adaptive_min_psf_references: int = 20
    adaptive_aperture_fwhm_factor: float = 1.0
    adaptive_max_axis_ratio: float = 3.0
    adaptive_max_radius_scale: float = 2.5
    adaptive_fallback_to_circular: bool = True
    psf_profile: str = "moffat"
    psf_moffat_beta: float = 2.5
    psf_fit_radius_fwhm: float = 2.5
    psf_min_fit_radius_pix: float = 5.0
    psf_max_fit_radius_pix: float = 14.0
    psf_centroid_search_radius_pix: float = 0.75
    psf_centroid_grid_steps: int = 3
    psf_residual_clip_sigma: float = 5.0
    psf_residual_clip_iterations: int = 2
    psf_fallback_to_adaptive: bool = True
    empirical_psf_grid_size: int = 3
    empirical_psf_stamp_radius_pix: int = 8
    empirical_psf_reference_radius_pix: float = 5.0
    empirical_psf_references_per_node: int = 48
    empirical_psf_min_references_per_node: int = 12
    empirical_psf_fallback_to_adaptive: bool = True
    annulus_inner_pix: float = 8.0
    annulus_outer_pix: float = 12.0
    detection_threshold_sigma: float = 5.0
    detection_max_sources_per_frame: int = 2000
    detection_min_separation_pix: float = 5.0
    detection_match_radius_arcsec: float = 2.0
    enable_psf_field_diagnostics: bool = False
    psf_diagnostics_max_sources_per_frame: int = 500
    psf_diagnostics_min_snr: float = 20.0
    psf_diagnostics_cutout_radius_pix: int = 7
    psf_diagnostics_min_isolation_pix: float = 12.0
    psf_diagnostics_clip_sigma: float = 4.0
    psf_diagnostics_n_iterations: int = 3
    psf_diagnostics_render_plots: bool = True
    bulk_insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    mag_key: str = "G"
    run_pre_photometry_qc: bool = True
    exclude_bad_pre_qc_frames: bool = False
    pre_qc_max_local_maxima: int | None = 5000
    pre_qc_max_bright_fraction: float | None = 0.08
    pre_qc_max_saturated_fraction: float | None = 0.001
    working_catalog_mag_limit: float | None = None
    max_working_sources_per_frame: int | None = None
    catalog_edge_margin_pix: float = 0.0
    replace_frame_catalog_links: bool = True
    reuse_existing_photometry: bool = True
    force_recompute_photometry: bool = False
    materialize_intermediate_calibrations: bool = False
    science_materialization_diagnostics: bool = False
    forced_photometry_diagnostics: bool = False
    calibration_performance_diagnostics: bool = False
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL
    photometry_gain_e_per_adu: float | None = None
    photometry_read_noise_e: float | None = None
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR
    empirical_background_floor: bool = True
    photometry_fractional_flux_floor: float = 0.0
    photometry_scintillation_enabled: bool = False
    photometry_scintillation_coefficient: float = 0.09
    photometry_telescope_aperture_m: float | None = None
    photometry_site_altitude_m: float | None = None
    aperture_correction_reference_radius_pix: float = 5.0
    aperture_correction_max_calibrator_mag_err: float | None = 0.05
    aperture_correction_min_calibrators_per_frame: int = 20
    aperture_correction_min_plane_calibrators: int = 30
    aperture_correction_clip_sigma: float = 3.5
    aperture_correction_n_iterations: int = 3
    aperture_correction_min_plane_bic_improvement: float = 6.0
    aperture_correction_min_spatial_peak_to_peak: float = 0.005
    aperture_correction_plane_policy: str = "evidence"
    aperture_correction_max_residual_mad: float | None = 0.05
    aperture_correction_max_abs_correction: float = 0.5
    aperture_correction_max_abs_spatial_deviation: float = 0.15
    aperture_correction_max_abs_total_correction: float = 0.75
    aperture_correction_min_good_frame_fraction: float = 0.8
    aperture_correction_max_global_median_residual_mad: float | None = 0.05
    aperture_zp_min_ref_stars_per_frame: int = 30
    aperture_zp_max_mag_err: float | None = 0.08
    aperture_zp_clip_sigma: float = 3.0
    aperture_zp_n_iterations: int = 3
    ensemble_min_measurements: int = 10
    ensemble_min_ref_measurements: int = 10
    ensemble_min_ref_stars_per_frame: int = 30
    ensemble_max_ref_rms: float | None = 0.08
    ensemble_max_ref_mad: float | None = 0.05
    ensemble_n_iterations: int = 3
    enable_spatial_ensemble: bool = False
    spatial_ensemble_max_order: int = 2
    spatial_ensemble_min_ref_stars_per_frame: int = 30
    spatial_ensemble_clip_sigma: float = 3.5
    spatial_ensemble_n_iterations: int = 3
    spatial_ensemble_min_bic_improvement: float = 6.0
    spatial_ensemble_min_spatial_peak_to_peak: float = 0.003
    spatial_ensemble_max_residual_mad: float | None = 0.05
    spatial_ensemble_max_residual_mad_degradation: float | None = 0.0
    spatial_ensemble_max_abs_spatial_deviation: float = 0.15
    spatial_ensemble_min_good_frame_fraction: float = 0.8
    enable_photometric_frame_qc: bool = False
    photometric_frame_qc_min_reference_stars_per_frame: int = 20
    photometric_frame_qc_suspect_sigma: float = 3.0
    photometric_frame_qc_bad_sigma: float = 6.0
    photometric_frame_qc_suspect_ratio: float = 1.5
    photometric_frame_qc_bad_ratio: float = 2.5
    photometric_frame_qc_absolute_suspect_residual_sigma_mag: float = 0.05
    photometric_frame_qc_absolute_bad_residual_sigma_mag: float = 0.10
    photometric_frame_qc_spatial_suspect_peak_to_peak_mag: float = 0.03
    photometric_frame_qc_spatial_bad_peak_to_peak_mag: float = 0.10
    enable_color_calibration: bool = False
    color_calibration_requested: bool = False
    color_reference_ready: bool | None = None
    color_calibration_skip_reason: str | None = None
    color_identity_catalog_name: str | None = None
    color_reference_catalog_name: str | None = None
    color_reference_band: str | None = None
    color_index: str | None = None
    color_target_calibration_name: str | None = None
    color_min_measurements_per_object: int = 5
    color_min_stars_per_frame: int = 20
    color_max_measurement_mag_err: float | None = None
    color_max_reference_mag_err: float | None = None
    color_min_reference_mag: float | None = None
    color_max_reference_mag: float | None = None
    color_min_value: float | None = None
    color_max_value: float | None = None
    color_clip_sigma: float = 4.0
    color_n_iterations: int = 5
    color_min_calibrator_objects: int = 20
    color_min_span: float = 0.3
    color_max_transform_residual_mad: float | None = 0.10
    color_max_pivot_fraction: float | None = 0.25
    color_max_shape_metric_delta: float = 1e-10
    color_min_term_significance: float = 2.0
    color_min_bic_improvement: float = 6.0
    color_min_mad_improvement: float = 0.0
    object_qc_min_measurements: int = 10
    crowded_arcsec: float = 10.0
    blended_arcsec: float = 5.0
    edge_distance_pix: float = 10.0
    high_rms: float = 0.10
    high_mag_err: float = 0.08
    min_detection_fraction: float = 0.5
    max_ref_mag_delta: float | None = 2.0
    dynamic_separation: bool = True
    dynamic_aperture_factor: float = 1.0
    dynamic_crowding_factor: float = 1.5
    fwhm_header_unit: str = "arcsec"
    field_report_min_excess: float = 2.0
    field_report_max_mag_median: float | None = 22.5
    field_report_top_limit: int = 20
    enable_intrinsic_variability_candidates: bool = True
    field_report_min_intrinsic_rms: float | None = 0.03
    field_report_min_rms_over_mag_err: float | None = 4.0
    field_report_min_mad_over_mag_err: float | None = 2.5
    field_report_intrinsic_mag_err_floor: float | None = 0.01
    field_report_min_robust_peak_to_peak: float | None = 0.08
    enable_low_amplitude_intrinsic_candidates: bool = True
    field_report_low_amplitude_min_measurements: int = 30
    field_report_low_amplitude_min_intrinsic_rms: float | None = 0.03
    field_report_low_amplitude_min_rms_over_mag_err: float | None = 2.3
    field_report_low_amplitude_min_mad_over_mag_err: float | None = 2.0
    field_report_low_amplitude_min_robust_peak_to_peak: float | None = 0.08
    field_report_borderline_threshold_ratio: float = 0.9
    field_report_diagnostic_max_failed_criteria: int = 2
    residual_floor_bin_width: float = 1.0
    residual_floor_lower_quantile: float = 0.25
    residual_floor_min_objects_per_bin: int = 3
    require_detection_support: bool | None = None
    target_source_id: str | None = None
    target_object_id: int | None = None
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None
    target_max_sep_arcsec: float | None = 120.0
    export_target_light_curve: bool = True
    export_candidate_light_curves: bool = True
    export_candidate_plots: bool = True
    interseries_generation_ids: list[int] | None = None
    candidate_light_curve_limit: int | None = None
    exclude_candidates_from_ensemble: bool = False
    candidate_ensemble_max_iterations: int = 2
    candidate_ensemble_until_converged: bool = False
    candidate_ensemble_convergence_max_iterations: int = 10
    candidate_ensemble_growth_policy: str = "initial_only"
    aperture_stability_radii: list[float] = field(default_factory=lambda: [2.0, 3.0, 4.0, 5.0])
    aperture_stability_max_candidates: int | None = None
    aperture_stability_min_corr: float = 0.7
    aperture_stability_max_rms_delta: float = 0.03
    aperture_stability_max_median_shift: float = 0.2
    aperture_stability_sweep_prefix: str | None = None
    raw_calibration_name: str | None = None
    aperture_zp_calibration_name: str | None = None
    ensemble_calibration_name: str | None = None
    skip_wcs: bool = False
    skip_catalog_frame_match: bool = False
    skip_detection: bool = False
    skip_detection_catalog_match: bool = False
    skip_forced_photometry: bool = False
    skip_aperture_correction: bool = False
    skip_aperture_zp: bool = False
    skip_ensemble: bool = False
    skip_object_qc: bool = False
    skip_aperture_stability_qc: bool = False
    skip_field_report: bool = False
    replace_detections: bool = True
    replace_detection_matches: bool = True
    dry_run: bool = False
    record_run: bool = True
    continue_on_error: bool = False
    stage2_profile: str = "diagnostic"
    log_dir: str | None = "logs"
    file_log_level: str = "INFO"
    no_file_log: bool = False
    command_json: dict[str, Any] = field(default_factory=dict)
    environment_json: dict[str, Any] = field(default_factory=dict)


def _check_open_series_admission(session: Session, config: FieldPipelineConfig) -> None:
    """Reject requested invocation scopes that include an OPEN field series."""

    if config.series_id is not None:
        return

    with session.no_autoflush:
        if config.frame_ids is not None:
            frame_ids = sorted({int(frame_id) for frame_id in config.frame_ids})
            if not frame_ids:
                raise ValueError("An explicit frame_ids selection must not be empty")
            statement = text(
                """
                SELECT fs.series_id
                FROM field_series_frames AS fsf
                JOIN field_series AS fs ON fs.series_id = fsf.series_id
                WHERE fsf.frame_id IN :frame_ids
                  AND UPPER(fs.status) = 'OPEN'
                LIMIT 1
                """
            ).bindparams(bindparam("frame_ids", expanding=True))
            open_series_id = session.execute(
                statement, {"frame_ids": frame_ids}
            ).scalar_one_or_none()
            if open_series_id is not None:
                raise ValueError(
                    "one or more requested frame_ids belong to an OPEN field_series; "
                    "explicitly finalize before Stage 2: "
                    f"series_id={int(open_series_id)}, frame_ids={frame_ids}"
                )
            return

        raise ValueError(
            "run_field_pipeline requires either series_id or explicit frame_ids; "
            "field_id and instrument_unit_id alone are not an executable scope"
        )


def run_field_pipeline(session: Session, config: FieldPipelineConfig) -> PipelineRunResult:
    """Run the field pipeline, applying pre-QC exclusion safety gates first."""

    config, _ = _apply_stage2_profile(config)

    validate_target_coordinates(config.target_ra_deg, config.target_dec_deg)

    pre_qc_exclusion_target = bool(
        config.run_pre_photometry_qc and config.exclude_bad_pre_qc_frames
    )
    if (
        pre_qc_exclusion_target
        and not config.dry_run
        and config.series_id is None
        and config.frame_ids is None
    ):
        raise FramePreQcExclusionPreconditionError(
            "pre-photometry QC frame exclusion requires an explicit series or frame_ids "
            "scope for ordinary runs"
        )

    if pre_qc_exclusion_target and config.dry_run:
        if config.record_run:
            raise FramePreQcExclusionPreconditionError(
                "pre-photometry QC exclusion dry-run requires record_run=False"
            )
        if session.new or session.dirty or session.deleted:
            raise FramePreQcExclusionPreconditionError(
                "pre-photometry QC exclusion dry-run requires a Session with no pending ORM state"
            )
        # Accepted target dry-runs may read through the Session, but must never
        # autoflush caller state. Keep this context active through the complete
        # runner call, including every downstream diagnostic and exception path.
        with session.no_autoflush:
            return _run_field_pipeline(session, config)

    return _run_field_pipeline(session, config)


def _stage2_profile_settings_for_config(config: FieldPipelineConfig) -> dict[str, Any]:
    return resolve_stage2_profile_settings(
        config.stage2_profile,
        requested={
            "enable_psf_field_diagnostics": config.enable_psf_field_diagnostics,
            "enable_aperture_stability_qc": not config.skip_aperture_stability_qc,
            "export_candidate_light_curves": config.export_candidate_light_curves,
            "export_candidate_plots": config.export_candidate_plots,
            "forced_photometry_diagnostics": config.forced_photometry_diagnostics,
            "calibration_performance_diagnostics": config.calibration_performance_diagnostics,
            "make_light_curve_summary": True,
            "light_curve_summary_json_requested": bool(config.out_dir),
            "aperture_correction_diagnostic_exports_requested": bool(config.out_dir),
            "random_error_validation": not config.skip_ensemble,
            "random_error_diagnostic_exports_requested": bool(config.out_dir),
            "exclude_candidates_from_ensemble": config.exclude_candidates_from_ensemble,
            "enable_photometric_frame_qc": config.enable_photometric_frame_qc,
            "field_report_enabled": not config.skip_field_report,
        },
    )


def _apply_stage2_profile(
    config: FieldPipelineConfig,
) -> tuple[FieldPipelineConfig, dict[str, Any]]:
    profile = normalize_stage2_profile(config.stage2_profile)
    if config.exclude_candidates_from_ensemble:
        if config.skip_field_report or config.skip_object_qc or config.skip_ensemble:
            raise ValueError(
                "exclude_candidates_from_ensemble requires field-QC candidate input, "
                "object QC, and ensemble calibration; refusing to run with a required path disabled"
            )
        iteration_limit = (
            max(2, int(config.candidate_ensemble_convergence_max_iterations or 10))
            if config.candidate_ensemble_until_converged
            else max(1, int(config.candidate_ensemble_max_iterations or 1))
        )
        if iteration_limit < 2:
            raise ValueError(
                "exclude_candidates_from_ensemble requires at least two ensemble iterations"
            )

    selected = replace(config, stage2_profile=profile)
    settings = _stage2_profile_settings_for_config(selected)
    if profile == "bulk":
        selected = replace(
            selected,
            enable_psf_field_diagnostics=settings["psf_field_diagnostics"],
            skip_aperture_stability_qc=not settings["aperture_stability_qc"],
            export_candidate_light_curves=settings["candidate_light_curve_exports"],
            export_candidate_plots=settings["candidate_plot_exports"],
            forced_photometry_diagnostics=settings[
                "forced_photometry_performance_diagnostics"
            ],
            calibration_performance_diagnostics=settings[
                "calibration_performance_diagnostics"
            ],
            # Candidate triage is mandatory input to exclusion, but otherwise
            # the full field-QC report is diagnostic-only.
            skip_field_report=not bool(config.exclude_candidates_from_ensemble),
        )
        settings = _stage2_profile_settings_for_config(selected)
    return selected, settings


def _run_field_pipeline(
    session: Session,
    config: FieldPipelineConfig,
) -> PipelineRunResult:
    """Run the main field-processing chain and optionally record its provenance.

    The runner intentionally orchestrates the already-tested low-level steps instead of
    duplicating their logic. It is the reproducibility layer: it records the input recipe,
    the generated calibration names, and per-step result counters in `pipeline_runs` and
    `pipeline_run_steps`.
    """

    profile_settings = _stage2_profile_settings_for_config(config)
    if config.frame_ids is not None and not config.frame_ids:
        raise ValueError("An explicit frame_ids selection must not be empty")

    pre_qc_exclusion_zero_write_dry_run = bool(
        config.run_pre_photometry_qc
        and config.exclude_bad_pre_qc_frames
        and config.dry_run
    )

    if config.materialize_intermediate_calibrations:
        raise ValueError(
            "Grandgousier 0.7 clean storage is BASE-only in measurements; "
            "full intermediate/science measurement materialization is no longer supported "
            "by the normal pipeline"
        )

    if config.enable_spatial_ensemble and config.enable_photometric_frame_qc:
        raise ValueError(
            "enable_spatial_ensemble and enable_photometric_frame_qc are mutually exclusive: "
            "spatial ensemble changes magnitudes, while photometric frame QC is the conservative production path"
        )

    series = None
    if config.series_id is not None:
        series = load_series_selection(session, int(config.series_id))
        if str(series.get("status", "")).upper() == "OPEN":
            raise ValueError(
                f"field_series is OPEN and must be explicitly finalized before Stage 2: "
                f"series_id={config.series_id}"
            )

    _check_open_series_admission(session, config)

    run_uid = config.run_uid or _make_run_uid(config.calibration_prefix)
    calibration_names = _calibration_names(config)
    outputs: dict[str, str] = {}
    pipeline_run_id: int | None = None
    artifact_publication_root = Path(config.out_dir) if config.out_dir else None
    artifact_publication_baseline = capture_tree_state(artifact_publication_root)

    has_explicit_frame_selection = config.frame_ids is not None
    selected_frame_ids = (
        [int(frame_id) for frame_id in config.frame_ids]
        if has_explicit_frame_selection
        else []
    )
    stop_due_to_no_usable_frames = False
    pre_qc_resolved_runtime_scope = False
    pre_qc_detection_seeds: dict[int, dict[str, Any]] = {}
    target_resolution: dict[str, Any] = {"resolved": False, "method": "not_run"}
    target_object_ids: list[int] = []
    eligible_catalog_source_ids: list[int] | None = None
    explicit_target_catalog_source_ids: list[int] = []
    explicit_target_catalog_selection: dict[str, Any] | None = None
    detection_support_selection: dict[str, Any] = {"status": "NOT_RUN"}
    latest_candidate_rows: list[dict[str, Any]] = []
    candidate_selection_rows: list[dict[str, Any]] = []
    candidate_membership_history: list[dict[str, Any]] = []
    latest_photometric_qc_good_frame_ids: list[int] = []
    latest_photometric_qc_suspect_frame_ids: list[int] = []
    latest_photometric_qc_bad_frame_ids: list[int] = []
    aperture_photometry_source_name = calibration_names["raw"]
    aperture_correction_activated = False
    current_ensemble_name = calibration_names["ensemble"]
    color_calibration_activated = False
    initial_ensemble_name = calibration_names["ensemble"]
    candidate_exclusion_object_ids: list[int] = []
    candidate_exclusion_new_object_ids: list[int] = []
    candidate_exclusion_effective_object_ids: list[int] = []
    current_ensemble_reference_object_ids: set[int] | None = None
    aperture_zp_downstream_seed_rows: tuple[Any, ...] | None = None
    aperture_zp_analysis_base_seed_rows: tuple[Any, ...] | None = None
    candidate_ensemble_terminal_reason: str | None = None
    candidate_ensemble_fixed_max_iterations = max(1, int(config.candidate_ensemble_max_iterations or 1))
    candidate_ensemble_convergence_max_iterations = max(2, int(config.candidate_ensemble_convergence_max_iterations or 10))
    candidate_ensemble_iteration_limit = (
        candidate_ensemble_convergence_max_iterations
        if config.candidate_ensemble_until_converged
        else candidate_ensemble_fixed_max_iterations
    )
    candidate_ensemble_growth_policy = normalize_candidate_ensemble_growth_policy(
        config.candidate_ensemble_growth_policy
    )
    field_id = config.field_id
    series_id = config.series_id
    if series_id is not None:
        assert series is not None
        series_frame_ids = [int(fid) for fid in series["frame_ids"]]
        if not series_frame_ids:
            raise ValueError(f"field_series has no selected frames: series_id={series_id}")
        if has_explicit_frame_selection:
            series_frame_id_set = set(series_frame_ids)
            selected_frame_ids = [int(fid) for fid in selected_frame_ids if int(fid) in series_frame_id_set]
            if not selected_frame_ids:
                raise ValueError(
                    "The explicit frame_ids selection has no frames in "
                    f"series_id={series_id}"
                )
        else:
            selected_frame_ids = series_frame_ids
        field_id = int(series["field_id"])
        if config.instrument_unit_id is None:
            config.instrument_unit_id = int(series["instrument_unit_id"]) if series.get("instrument_unit_id") is not None else None
        elif series.get("instrument_unit_id") is not None and int(config.instrument_unit_id) != int(series["instrument_unit_id"]):
            raise ValueError(
                f"--instrument-unit-id={config.instrument_unit_id} does not match series_id={series_id} "
                f"instrument_unit_id={series['instrument_unit_id']}"
            )

    require_detection_support_effective = _effective_require_detection_support(config)
    recipe = _to_jsonable(asdict(config))
    recipe["effective_stage2_settings"] = _to_jsonable(profile_settings)
    recipe["recipe_version"] = PIPELINE_RECIPE_VERSION
    recipe["calibration_names"] = calibration_names
    recipe["field_id"] = field_id
    recipe["series_id"] = series_id
    recipe["frame_ids"] = selected_frame_ids
    recipe["quality_policy"] = {
        "stage2_profile": config.stage2_profile,
        "effective_stage2_settings": _to_jsonable(profile_settings),
        "random_error_validation_status": profile_settings[
            "random_error_validation_status"
        ],
        "random_error_model": config.random_error_model,
        "random_error_physical_model": PHYSICAL_APERTURE_ERROR_MODEL,
        "random_error_legacy_comparison_model": LEGACY_APERTURE_ERROR_MODEL,
        "random_error_legacy_comparison_enabled": profile_settings[
            "random_error_legacy_comparison"
        ],
        "random_error_gain_e_per_adu_override": config.photometry_gain_e_per_adu,
        "random_error_read_noise_e_override": config.photometry_read_noise_e,
        "random_error_background_estimator_variance_factor": (
            config.background_estimator_variance_factor
        ),
        "random_error_empirical_background_floor": config.empirical_background_floor,
        "random_error_fractional_flux_floor": config.photometry_fractional_flux_floor,
        "random_error_scintillation_enabled": config.photometry_scintillation_enabled,
        "random_error_scintillation_coefficient": (
            config.photometry_scintillation_coefficient
        ),
        "random_error_telescope_aperture_m": config.photometry_telescope_aperture_m,
        "random_error_site_altitude_m": config.photometry_site_altitude_m,
        "detection_branch_enabled": _detection_branch_enabled(config),
        "psf_field_diagnostics_enabled": config.enable_psf_field_diagnostics,
        "psf_field_diagnostics_algorithm": "second_moments_spatial_v1",
        "require_detection_support_requested": config.require_detection_support,
        "require_detection_support_effective": require_detection_support_effective,
        "target_source_id": config.target_source_id,
        "target_object_id": config.target_object_id,
        "target_ra_deg": config.target_ra_deg,
        "target_dec_deg": config.target_dec_deg,
        "target_provenance_status": _target_provenance_status(config),
        "target_requested_provenance_status": _target_provenance_status(config),
        "export_target_light_curve": config.export_target_light_curve,
        "exclude_candidates_from_ensemble": config.exclude_candidates_from_ensemble,
        "intermediate_measurement_materialization": "NORMALIZED_ONLY",
        "candidate_ensemble_max_iterations": candidate_ensemble_fixed_max_iterations,
        "candidate_ensemble_until_converged": config.candidate_ensemble_until_converged,
        "candidate_ensemble_convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
        "candidate_ensemble_iteration_limit": candidate_ensemble_iteration_limit,
        "candidate_ensemble_growth_policy": candidate_ensemble_growth_policy,
    }

    if config.record_run:
        pipeline_run_id = _insert_pipeline_run(
            session,
            run_uid=run_uid,
            instrument_unit_id=config.instrument_unit_id,
            field_id=field_id,
            series_id=series_id,
            catalog_name=config.catalog_name,
            calibration_prefix=config.calibration_prefix,
            recipe_json=recipe,
        )

    if series_id is not None and pipeline_run_id is not None and not config.dry_run:
        mark_series_pipeline_started(
            session,
            series_id=int(series_id),
            pipeline_run_id=int(pipeline_run_id),
            run_uid=run_uid,
        )

    file_logger: PipelineFileLogger | None = None
    log_files: dict[str, str] = {}
    if not config.no_file_log and config.log_dir:
        file_logger = PipelineFileLogger.create(
            root_dir=config.log_dir,
            pipeline_run_id=pipeline_run_id,
            run_uid=run_uid,
            command_json=config.command_json,
            environment_json=config.environment_json,
            min_level=config.file_log_level,
        )
        log_files = file_logger.paths
        _update_pipeline_run_log_paths(session, pipeline_run_id, log_files)

    result = PipelineRunResult(
        run_uid=run_uid,
        pipeline_run_id=pipeline_run_id,
        status="RUNNING",
        calibration_names=calibration_names,
        outputs=outputs,
        log_files=log_files,
        quality_policy=dict(recipe["quality_policy"]),
    )

    def _snapshot_run_artifacts() -> None:
        if config.dry_run or not config.record_run or pipeline_run_id is None:
            return
        def _resolve_calibration_version_id(name: str | None) -> int | None:
            if not name:
                return None
            value = session.execute(
                text(
                    "SELECT calibration_version_id FROM calibration_versions "
                    "WHERE name = :name ORDER BY calibration_version_id DESC LIMIT 1"
                ),
                {"name": name},
            ).scalar_one_or_none()
            return int(value) if value is not None else None

        science_calibration_name = calibration_names.get("science") or calibration_names.get("ensemble")
        science_calibration_version_id = _resolve_calibration_version_id(science_calibration_name)
        aperture_correction_version_id = _resolve_calibration_version_id(
            calibration_names.get("aperture_correction") or calibration_names.get("aperture_corrected")
        )
        color_version_id = _resolve_calibration_version_id(calibration_names.get("color"))
        artifact_calibration_ids = {
            "APERTURE_CORRECTION_DIAGNOSTIC": aperture_correction_version_id,
            "COLOR_CALIBRATION_DIAGNOSTIC": color_version_id or science_calibration_version_id,
            "REFERENCE_PHOTOMETRY_CACHE": None,
        }

        registered = []
        if artifact_publication_root is not None:
            run_dir = run_artifact_directory(
                artifact_publication_root,
                run_uid=run_uid,
                pipeline_run_id=pipeline_run_id,
                artifact_root=config.artifact_root,
            )
            if config.stage2_profile == "diagnostic":
                registered.extend(
                    snapshot_changed_export_tree(
                        session,
                        publication_root=artifact_publication_root,
                        run_dir=run_dir,
                        baseline_state=artifact_publication_baseline,
                        pipeline_run_id=pipeline_run_id,
                        series_id=series_id,
                        calibration_version_id=science_calibration_version_id,
                        calibration_version_ids_by_type=artifact_calibration_ids,
                    )
                )
            outputs["artifact_run_dir"] = str(run_dir)

        registered.extend(
            register_run_log_artifacts(
                session,
                log_files=log_files,
                pipeline_run_id=pipeline_run_id,
                series_id=series_id,
            )
        )
        session.commit()
        outputs["artifact_registry_status"] = "OK"
        outputs["artifact_count"] = len(registered)

    analysis_snapshot_cache: OrderedDict[tuple[Any, ...], CalibrationAnalysisSnapshot] = OrderedDict()
    analysis_snapshot_seed_cache: OrderedDict[tuple[Any, ...], tuple[Any, ...]] = OrderedDict()
    object_qc_support_cache: OrderedDict[tuple[Any, ...], ObjectQcSupportSnapshot] = OrderedDict()

    def _derived_materialization_kind() -> str:
        return "NORMALIZED_TERMS"

    def _stamp_product(
        calibration_name: str,
        *,
        product_kind: str | None = None,
        product_role: str | None = None,
        materialization_kind: str | None = None,
        calibration_version_id: int | None = None,
    ) -> None:
        # Product provenance belongs to persisted pipeline-run provenance.
        # record_run=False is used by lightweight/library runs and must not
        # mutate calibration_versions as an incidental side effect.
        if config.dry_run or not config.record_run:
            return
        stamp_calibration_product(
            session,
            calibration_name=calibration_name,
            pipeline_run_id=pipeline_run_id,
            field_id=field_id,
            series_id=series_id,
            frame_ids=_frame_ids_for_step(selected_frame_ids),
            product_kind=product_kind,
            product_role=product_role,
            materialization_kind=materialization_kind,
            calibration_version_id=calibration_version_id,
            finalize_immutable_generation=True,
        )

    def _analysis_snapshot_key(calibration_name: str) -> tuple[Any, ...]:
        frame_scope = tuple(sorted({int(value) for value in selected_frame_ids})) or None
        return (
            str(calibration_name),
            config.catalog_name,
            config.instrument_unit_id,
            frame_scope,
            int(config.object_qc_min_measurements),
            str(config.fwhm_header_unit or "arcsec"),
        )

    def _cached_analysis_snapshot_for(
        calibration_name: str,
    ) -> CalibrationAnalysisSnapshot | None:
        key = _analysis_snapshot_key(calibration_name)
        cached = analysis_snapshot_cache.get(key)
        if cached is not None:
            analysis_snapshot_cache.move_to_end(key)
        return cached

    def _remember_analysis_snapshot_seed(calibration_name: str, calibration_result: Any) -> None:
        seed_rows = getattr(calibration_result, "_analysis_snapshot_base_rows", None)
        if not seed_rows:
            return
        key = _analysis_snapshot_key(calibration_name)
        analysis_snapshot_seed_cache[key] = tuple(seed_rows)
        analysis_snapshot_seed_cache.move_to_end(key)
        while len(analysis_snapshot_seed_cache) > 2:
            analysis_snapshot_seed_cache.popitem(last=False)

    def _analysis_snapshot_for(calibration_name: str) -> CalibrationAnalysisSnapshot:
        key = _analysis_snapshot_key(calibration_name)
        frame_scope = key[3]
        cached = _cached_analysis_snapshot_for(calibration_name)
        if cached is not None:
            cached.timings_sec["_runner_cache_hit"] = 1.0
            return cached

        # The compact ENSEMBLE stage may already have read the richer BASE scalar
        # subset needed by Object-QC. Reuse those transient rows so the first QC
        # pass does not issue another ~54k-row BASE query.
        snapshot = None
        seed_rows = analysis_snapshot_seed_cache.pop(key, None)
        if seed_rows is not None:
            snapshot = load_calibration_analysis_snapshot(
                session,
                calibration_name=calibration_name,
                catalog_name=config.catalog_name,
                instrument_unit_id=config.instrument_unit_id,
                frame_ids=_frame_ids_for_step(selected_frame_ids),
                min_measurements=config.object_qc_min_measurements,
                fwhm_header_unit=config.fwhm_header_unit,
                _preloaded_compact_base_rows=seed_rows,
            )

        # A color-calibrated QC pass immediately follows its ensemble parent.
        # Reuse that parent snapshot and apply only the compact normalized color
        # terms instead of reconstructing/re-reading ~50k rows from PostgreSQL.
        if snapshot is None:
            for parent_snapshot in reversed(tuple(analysis_snapshot_cache.values())):
                if (
                    parent_snapshot.catalog_name != config.catalog_name
                    or parent_snapshot.instrument_unit_id != config.instrument_unit_id
                    or parent_snapshot.frame_ids != frame_scope
                    or parent_snapshot.min_measurements
                    != int(config.object_qc_min_measurements)
                    or parent_snapshot.fwhm_header_unit
                    != str(config.fwhm_header_unit or "arcsec")
                ):
                    continue
                snapshot = derive_color_calibration_analysis_snapshot(
                    session,
                    source_snapshot=parent_snapshot,
                    target_calibration_name=calibration_name,
                )
                if snapshot is not None:
                    break

        if snapshot is None:
            snapshot = load_calibration_analysis_snapshot(
                session,
                calibration_name=calibration_name,
                catalog_name=config.catalog_name,
                instrument_unit_id=config.instrument_unit_id,
                frame_ids=_frame_ids_for_step(selected_frame_ids),
                min_measurements=config.object_qc_min_measurements,
                fwhm_header_unit=config.fwhm_header_unit,
            )
        snapshot.timings_sec["_runner_cache_hit"] = 0.0
        analysis_snapshot_cache[key] = snapshot
        analysis_snapshot_cache.move_to_end(key)
        # One snapshot can contain tens of thousands of measurement rows. Two
        # entries retain the current science branch and its source calibration
        # while bounding memory during iterative ensemble passes.
        while len(analysis_snapshot_cache) > 2:
            analysis_snapshot_cache.popitem(last=False)
        return snapshot

    def _object_qc_support_for(
        analysis_snapshot: CalibrationAnalysisSnapshot,
    ) -> ObjectQcSupportSnapshot | None:
        object_rows = getattr(analysis_snapshot, "object_rows", None)
        if object_rows is None:
            return None
        frame_scope = tuple(sorted({int(value) for value in selected_frame_ids})) or None
        measured_source_ids = tuple(
            sorted(
                {
                    str(row.source_id)
                    for row in object_rows
                    if row.source_id is not None and str(row.source_id).strip()
                }
            )
        )
        key = (
            config.catalog_name,
            config.instrument_unit_id,
            frame_scope,
            measured_source_ids,
        )
        cached = object_qc_support_cache.get(key)
        if cached is not None:
            object_qc_support_cache.move_to_end(key)
            cached.timings_sec["_runner_cache_hit"] = 1.0
            return cached
        snapshot = load_object_qc_support_snapshot(
            session,
            catalog_name=config.catalog_name,
            instrument_unit_id=config.instrument_unit_id,
            frame_ids=_frame_ids_for_step(selected_frame_ids),
            target_source_ids=measured_source_ids,
        )
        snapshot.timings_sec["_runner_cache_hit"] = 0.0
        object_qc_support_cache[key] = snapshot
        object_qc_support_cache.move_to_end(key)
        while len(object_qc_support_cache) > 4:
            object_qc_support_cache.popitem(last=False)
        return snapshot

    def _compute_object_qc_for(calibration_name: str):
        analysis_snapshot = _analysis_snapshot_for(calibration_name)
        support_snapshot = _object_qc_support_for(analysis_snapshot)
        return compute_object_qc(
            session,
            calibration_name=calibration_name,
            catalog_name=config.catalog_name,
            instrument_unit_id=config.instrument_unit_id,
            frame_ids=_frame_ids_for_step(selected_frame_ids),
            min_measurements=config.object_qc_min_measurements,
            crowded_arcsec=config.crowded_arcsec,
            blended_arcsec=config.blended_arcsec,
            edge_distance_pix=config.edge_distance_pix,
            high_rms=config.high_rms,
            high_mag_err=config.high_mag_err,
            min_detection_fraction=config.min_detection_fraction,
            max_ref_mag_delta=config.max_ref_mag_delta,
            dynamic_separation=config.dynamic_separation,
            dynamic_aperture_factor=config.dynamic_aperture_factor,
            dynamic_crowding_factor=config.dynamic_crowding_factor,
            fwhm_header_unit=config.fwhm_header_unit,
            insert_batch_size=config.bulk_insert_batch_size,
            dry_run=config.dry_run,
            target_object_ids=target_object_ids,
            analysis_snapshot=analysis_snapshot,
            support_snapshot=support_snapshot,
        )

    _emit_pipeline_log(
        session,
        file_logger,
        pipeline_run_id=pipeline_run_id,
        pipeline_run_step_id=None,
        step_name=None,
        level="INFO",
        event_type="run_started",
        message="field pipeline run started",
        context={"run_uid": run_uid, "field_id": field_id, "series_id": series_id, "n_selected_frames": len(selected_frame_ids) if selected_frame_ids else None, "calibration_names": calibration_names},
        write_db_event=not pre_qc_exclusion_zero_write_dry_run,
    )

    def add_step(step_name: str, fn: Callable[[], Any]) -> None:
        order_index = len(result.steps) + 1
        step_id = (
            _insert_pipeline_step(session, pipeline_run_id, step_name, order_index)
            if pipeline_run_id and not pre_qc_exclusion_zero_write_dry_run
            else None
        )
        _emit_pipeline_log(
            session,
            file_logger,
            pipeline_run_id=pipeline_run_id,
            pipeline_run_step_id=step_id,
            step_name=step_name,
            level="INFO",
            event_type="step_started",
            message=f"step {step_name} started",
            context={"order_index": order_index},
            write_db_event=not pre_qc_exclusion_zero_write_dry_run,
        )
        started = time.perf_counter()
        try:
            payload = fn()
            elapsed_sec = float(time.perf_counter() - started)
            payload_dict = _result_to_dict(payload)
            payload_dict["elapsed_sec"] = elapsed_sec
            step_status = _completed_step_status(step_name, payload_dict)
            if not pre_qc_exclusion_zero_write_dry_run:
                _finish_pipeline_step(session, step_id, step_status, payload_dict, None)
            result.steps.append(PipelineStepRecord(step_name=step_name, status=step_status, result=payload_dict, elapsed_sec=elapsed_sec))
            log_level, event_type, message = _completed_step_log(
                step_name, step_status, payload_dict
            )
            _emit_pipeline_log(
                session,
                file_logger,
                pipeline_run_id=pipeline_run_id,
                pipeline_run_step_id=step_id,
                step_name=step_name,
                level=log_level,
                event_type=event_type,
                message=message,
                context=_compact_log_context(payload_dict),
                write_db_event=not pre_qc_exclusion_zero_write_dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - runner must persist failure context before returning.
            elapsed_sec = float(time.perf_counter() - started)
            error_message = f"{type(exc).__name__}: {exc}"
            failure_payload = {"elapsed_sec": elapsed_sec}
            diagnostics = getattr(exc, "diagnostics", None)
            if isinstance(diagnostics, dict):
                failure_payload.update(_to_jsonable(diagnostics))
            # A flush/execute error leaves SQLAlchemy in a failed transaction.
            # Roll back the failed step before trying to persist its diagnostics;
            # the step-start record and all earlier successful steps were already
            # committed independently.
            if not pre_qc_exclusion_zero_write_dry_run:
                session.rollback()
                _finish_pipeline_step(session, step_id, "FAILED", failure_payload, error_message)
            result.steps.append(
                PipelineStepRecord(step_name=step_name, status="FAILED", result=failure_payload, error_message=error_message, elapsed_sec=elapsed_sec)
            )
            if file_logger is not None:
                file_logger.exception(
                    exc=exc,
                    event_type="step_failed",
                    message=f"step {step_name} failed",
                    pipeline_run_id=pipeline_run_id,
                    pipeline_run_step_id=step_id,
                    step_name=step_name,
                    context={"order_index": order_index, **failure_payload},
                )
            if not pre_qc_exclusion_zero_write_dry_run:
                _insert_pipeline_log_event(
                    session,
                    pipeline_run_id=pipeline_run_id,
                    pipeline_run_step_id=step_id,
                    level="ERROR",
                    event_type="step_failed",
                    message=error_message,
                    context_json={"step_name": step_name, "order_index": order_index, **failure_payload},
                )
            if getattr(exc, "status", None) == "FAILED_PRECONDITION":
                raise
            if not config.continue_on_error:
                raise

    def _ensure_explicit_target_catalog_selection() -> dict[str, Any]:
        nonlocal explicit_target_catalog_selection, explicit_target_catalog_source_ids
        if explicit_target_catalog_selection is None:
            explicit_target_catalog_selection = resolve_explicit_target_catalog_source(
                session,
                catalog_name=config.catalog_name,
                target_source_id=config.target_source_id,
                target_object_id=config.target_object_id,
                target_ra_deg=config.target_ra_deg,
                target_dec_deg=config.target_dec_deg,
            )
        source_id = explicit_target_catalog_selection.get("catalog_source_id")
        explicit_target_catalog_source_ids = (
            [int(source_id)]
            if explicit_target_catalog_selection.get("eligible") and source_id is not None
            else []
        )
        return explicit_target_catalog_selection

    def _catalog_frame_scope() -> list[int] | None:
        if (
            series_id is not None
            or has_explicit_frame_selection
            or pre_qc_resolved_runtime_scope
        ):
            return _frame_ids_for_step(selected_frame_ids)
        return None

    try:
        if not config.skip_wcs:
            add_step(
                "check_wcs",
                lambda: check_frames_wcs(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if series_id is not None or has_explicit_frame_selection
                        else None
                    ),
                    current_status=None,
                    dry_run=config.dry_run,
                    update_center=True,
                    record_events=False,
                ),
            )

        if config.run_pre_photometry_qc:
            def _pre_qc_step() -> dict[str, Any]:
                nonlocal selected_frame_ids, stop_due_to_no_usable_frames, pre_qc_detection_seeds
                nonlocal pre_qc_resolved_runtime_scope
                res = run_frame_pre_qc(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if series_id is not None or has_explicit_frame_selection
                        else None
                    ),
                    threshold_sigma=config.detection_threshold_sigma,
                    detection_cap=config.detection_max_sources_per_frame,
                    max_local_maxima=config.pre_qc_max_local_maxima,
                    max_bright_fraction=config.pre_qc_max_bright_fraction,
                    max_saturated_fraction=config.pre_qc_max_saturated_fraction,
                    dry_run=config.dry_run,
                    capture_detection_seeds=(not config.skip_detection or config.photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"}),
                )
                pre_qc_detection_seeds = dict(res.detection_seeds)
                payload = res.summary()
                payload["transient_detection_seed_frames"] = len(pre_qc_detection_seeds)
                payload["transient_detection_seed_candidates"] = sum(
                    len(seed.get("candidates") or ())
                    for seed in pre_qc_detection_seeds.values()
                )
                if config.exclude_bad_pre_qc_frames:
                    resolved_frame_ids = [
                        int(frame_id) for frame_id in res.selected_frame_ids
                    ]
                    if not resolved_frame_ids:
                        raise FramePreQcExclusionPreconditionError(
                            "pre-photometry QC exclusion resolved zero eligible input frames"
                        )
                    if pre_qc_exclusion_zero_write_dry_run:
                        bad_frame_ids = {int(frame_id) for frame_id in res.bad_frame_ids}
                        selected_frame_ids = [
                            frame_id
                            for frame_id in resolved_frame_ids
                            if frame_id not in bad_frame_ids
                        ]
                        pre_qc_resolved_runtime_scope = True
                        payload["excluded_bad_frames"] = (
                            len(resolved_frame_ids) - len(selected_frame_ids)
                        )
                        payload["remaining_frames"] = len(selected_frame_ids)
                    else:
                        before = len(selected_frame_ids)
                        selected_frame_ids = filter_bad_pre_qc_frame_ids(
                            session, selected_frame_ids
                        )
                        payload["excluded_bad_frames"] = before - len(selected_frame_ids)
                        payload["remaining_frames"] = len(selected_frame_ids)
                    if not selected_frame_ids:
                        stop_due_to_no_usable_frames = True
                        payload["stop_reason"] = "all selected frames rejected by pre-photometry QC"
                return payload

            add_step("pre_photometry_frame_qc", _pre_qc_step)

        if stop_due_to_no_usable_frames:
            add_step(
                "stop_no_usable_frames",
                lambda: {
                    "selected_frames_after_pre_qc": 0,
                    "science_status": "BAD_FRAME_QC",
                    "reason": "all selected frames were rejected by pre-photometry QC",
                },
            )

        if not stop_due_to_no_usable_frames and (not config.skip_detection or config.photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"}):
            add_step(
                "detect_sources",
                lambda: detect_sources_in_frames(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if selected_frame_ids
                        else None
                    ),
                    threshold_sigma=config.detection_threshold_sigma,
                    max_sources_per_frame=config.detection_max_sources_per_frame,
                    min_separation_pix=config.detection_min_separation_pix,
                    aperture_radius_pix=config.aperture_radius_pix,
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    replace_existing=config.replace_detections,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                    pre_qc_detection_seeds=pre_qc_detection_seeds or None,
                    pipeline_run_id=pipeline_run_id,
                ),
            )

        if (
            not stop_due_to_no_usable_frames
            and not config.skip_catalog_frame_match
            and not config.skip_detection_catalog_match
        ):
            def _bootstrap_catalog_to_frames_step():
                _ensure_explicit_target_catalog_selection()
                return match_catalog_to_frames(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_catalog_frame_scope(),
                    margin_pix=config.margin_pix,
                    mag_key=config.mag_key,
                    working_mag_limit=config.working_catalog_mag_limit,
                    include_catalog_source_ids=explicit_target_catalog_source_ids,
                    eligible_catalog_source_ids=None,
                    priority_catalog_source_ids=explicit_target_catalog_source_ids,
                    max_sources_per_frame=None,
                    edge_margin_pix=config.catalog_edge_margin_pix,
                    replace_existing=True,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                )

            add_step("bootstrap_catalog_to_frames", _bootstrap_catalog_to_frames_step)

        if not stop_due_to_no_usable_frames and not config.skip_detection_catalog_match:
            add_step(
                "match_detections_to_catalog",
                lambda: match_detections_to_catalog(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if selected_frame_ids
                        else None
                    ),
                    radius_arcsec=config.detection_match_radius_arcsec,
                    mag_key=config.mag_key,
                    replace_existing=config.replace_detection_matches,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                    pipeline_run_id=pipeline_run_id,
                ),
            )

            def _aggregate_detection_support_step() -> dict[str, Any]:
                nonlocal eligible_catalog_source_ids
                nonlocal detection_support_selection
                # Distinguish an enabled-but-unavailable/failed support pass from
                # the disabled branch, whose None value means unfiltered projection.
                eligible_catalog_source_ids = []
                match_result = _step_result(result, "match_detections_to_catalog") or {}
                detection_run_id = match_result.get("detection_run_id")
                if detection_run_id is None:
                    support_counts: dict[int, int] = {}
                    support_status = "UNAVAILABLE_NO_PERSISTED_MATCH_RUN"
                else:
                    support_counts = aggregate_detection_catalog_support(
                        session,
                        catalog_name=config.catalog_name,
                        detection_run_id=int(detection_run_id),
                        frame_ids=(
                            _frame_ids_for_step(selected_frame_ids)
                            if selected_frame_ids
                            else None
                        ),
                    )
                    support_status = "DONE"

                explicit_target = _ensure_explicit_target_catalog_selection()

                eligible_catalog_source_ids = _eligible_catalog_sources_for_forced_photometry(
                    support_counts, explicit_target
                )
                support_values = list(support_counts.values())
                detection_support_selection = {
                    "status": support_status,
                    "minimum_distinct_physical_frames": 1,
                    "supported_catalog_sources": len(support_counts),
                    "sources_supported_by_one_frame": sum(
                        1 for count in support_values if count == 1
                    ),
                    "sources_supported_by_multiple_frames": sum(
                        1 for count in support_values if count > 1
                    ),
                    "max_distinct_physical_frame_support": max(support_values, default=0),
                    "explicit_target": explicit_target,
                    "explicit_target_source_included": bool(
                        explicit_target_catalog_source_ids
                    ),
                    "eligible_catalog_sources": len(eligible_catalog_source_ids),
                    "detection_run_id": int(detection_run_id)
                    if detection_run_id is not None
                    else None,
                }
                result.quality_policy["detection_support_selection"] = dict(
                    detection_support_selection
                )
                return dict(detection_support_selection)

            add_step("aggregate_detection_support", _aggregate_detection_support_step)

        if not stop_due_to_no_usable_frames and not config.skip_catalog_frame_match:
            def _match_catalog_to_frames_step():
                _ensure_explicit_target_catalog_selection()
                return match_catalog_to_frames(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_catalog_frame_scope(),
                    margin_pix=config.margin_pix,
                    mag_key=config.mag_key,
                    working_mag_limit=config.working_catalog_mag_limit,
                    include_catalog_source_ids=explicit_target_catalog_source_ids,
                    eligible_catalog_source_ids=eligible_catalog_source_ids,
                    priority_catalog_source_ids=explicit_target_catalog_source_ids,
                    max_sources_per_frame=config.max_working_sources_per_frame,
                    edge_margin_pix=config.catalog_edge_margin_pix,
                    replace_existing=(
                        True
                        if not config.skip_detection_catalog_match
                        else config.replace_frame_catalog_links
                    ),
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                )

            add_step("match_catalog_to_frames", _match_catalog_to_frames_step)

        if not stop_due_to_no_usable_frames and config.enable_psf_field_diagnostics:
            def _psf_field_diagnostics_step() -> dict[str, Any]:
                diagnostics_dir = None
                if config.out_dir:
                    diagnostics_dir = Path(config.out_dir) / "psf_field"
                res = analyze_psf_field(
                    session,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    instrument_unit_id=config.instrument_unit_id,
                    out_dir=diagnostics_dir,
                    max_sources_per_frame=config.psf_diagnostics_max_sources_per_frame,
                    min_snr=config.psf_diagnostics_min_snr,
                    cutout_radius_pix=config.psf_diagnostics_cutout_radius_pix,
                    min_isolation_pix=config.psf_diagnostics_min_isolation_pix,
                    clip_sigma=config.psf_diagnostics_clip_sigma,
                    n_iterations=config.psf_diagnostics_n_iterations,
                    render_plots=config.psf_diagnostics_render_plots,
                )
                if res.sources_csv:
                    outputs["psf_sources_csv"] = res.sources_csv
                if res.frames_csv:
                    outputs["psf_frames_csv"] = res.frames_csv
                if res.summary_json:
                    outputs["psf_summary_json"] = res.summary_json
                if res.field_map_png:
                    outputs["psf_field_map_png"] = res.field_map_png
                if res.time_series_png:
                    outputs["psf_time_series_png"] = res.time_series_png
                return res.summary()

            add_step("psf_field_diagnostics", _psf_field_diagnostics_step)


        if not stop_due_to_no_usable_frames and not config.skip_forced_photometry:
            def _forced_photometry_step():
                diagnostics_dir = None
                if config.out_dir and config.stage2_profile == "diagnostic":
                    diagnostics_dir = Path(config.out_dir) / "random_error_model"
                _ensure_explicit_target_catalog_selection()
                res = run_forced_aperture_photometry(
                    session,
                    catalog_name=config.catalog_name,
                    eligible_catalog_source_ids=eligible_catalog_source_ids,
                    priority_catalog_source_ids=explicit_target_catalog_source_ids,
                    limit_links=config.max_working_sources_per_frame,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if selected_frame_ids
                        else None
                    ),
                    aperture_radius_pix=config.aperture_radius_pix,
                    photometry_estimator=config.photometry_estimator,
                    adaptive_detection_algorithm=config.adaptive_detection_algorithm,
                    adaptive_min_psf_snr=config.adaptive_min_psf_snr,
                    adaptive_max_psf_sources_per_frame=config.adaptive_max_psf_sources_per_frame,
                    adaptive_psf_cutout_radius_pix=config.adaptive_psf_cutout_radius_pix,
                    adaptive_psf_min_isolation_pix=config.adaptive_psf_min_isolation_pix,
                    adaptive_interpolation_neighbors=config.adaptive_interpolation_neighbors,
                    adaptive_min_psf_references=config.adaptive_min_psf_references,
                    adaptive_aperture_fwhm_factor=config.adaptive_aperture_fwhm_factor,
                    adaptive_max_axis_ratio=config.adaptive_max_axis_ratio,
                    adaptive_max_radius_scale=config.adaptive_max_radius_scale,
                    adaptive_fallback_to_circular=config.adaptive_fallback_to_circular,
                    psf_profile=config.psf_profile,
                    psf_moffat_beta=config.psf_moffat_beta,
                    psf_fit_radius_fwhm=config.psf_fit_radius_fwhm,
                    psf_min_fit_radius_pix=config.psf_min_fit_radius_pix,
                    psf_max_fit_radius_pix=config.psf_max_fit_radius_pix,
                    psf_centroid_search_radius_pix=config.psf_centroid_search_radius_pix,
                    psf_centroid_grid_steps=config.psf_centroid_grid_steps,
                    psf_residual_clip_sigma=config.psf_residual_clip_sigma,
                    psf_residual_clip_iterations=config.psf_residual_clip_iterations,
                    psf_fallback_to_adaptive=config.psf_fallback_to_adaptive,
                    empirical_psf_grid_size=config.empirical_psf_grid_size,
                    empirical_psf_stamp_radius_pix=config.empirical_psf_stamp_radius_pix,
                    empirical_psf_reference_radius_pix=config.empirical_psf_reference_radius_pix,
                    empirical_psf_references_per_node=config.empirical_psf_references_per_node,
                    empirical_psf_min_references_per_node=config.empirical_psf_min_references_per_node,
                    empirical_psf_fallback_to_adaptive=config.empirical_psf_fallback_to_adaptive,
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    calibration_name=calibration_names["raw"],
                    update_processing_status=True,
                    reuse_existing=config.reuse_existing_photometry,
                    force_recompute=config.force_recompute_photometry,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                    random_error_model=config.random_error_model,
                    include_legacy_comparison=profile_settings[
                        "random_error_legacy_comparison"
                    ],
                    gain_e_per_adu=config.photometry_gain_e_per_adu,
                    read_noise_e=config.photometry_read_noise_e,
                    background_estimator_variance_factor=(
                        config.background_estimator_variance_factor
                    ),
                    empirical_background_floor=config.empirical_background_floor,
                    fractional_flux_floor=config.photometry_fractional_flux_floor,
                    scintillation_enabled=config.photometry_scintillation_enabled,
                    scintillation_coefficient=(
                        config.photometry_scintillation_coefficient
                    ),
                    telescope_aperture_m=config.photometry_telescope_aperture_m,
                    site_altitude_m=config.photometry_site_altitude_m,
                    diagnostics_out_dir=diagnostics_dir,
                    diagnose_performance=config.forced_photometry_diagnostics,
                    immutable_generation=bool(config.record_run and not config.dry_run),
                )
                if res.diagnostics_summary_json:
                    outputs["random_error_model_summary_json"] = (
                        res.diagnostics_summary_json
                    )
                if res.diagnostics_frames_csv:
                    outputs["random_error_model_frames_csv"] = res.diagnostics_frames_csv
                result.quality_policy["forced_photometry_selection"] = {
                    **detection_support_selection,
                    "eligible_links_before_cap": int(
                        getattr(res, "eligible_links_before_cap", 0) or 0
                    ),
                    "selected_links_after_cap": int(
                        getattr(res, "selected_links_after_cap", 0) or 0
                    ),
                    "cap_dropped_links": int(getattr(res, "cap_dropped_links", 0) or 0),
                    "source_cap_per_frame": config.max_working_sources_per_frame,
                }
                _stamp_product(
                    calibration_names["raw"],
                    product_kind="BASE_PHOTOMETRY",
                    product_role="BASE",
                    materialization_kind="FULL_MEASUREMENTS",
                    calibration_version_id=res.calibration_version_id,
                )
                return res

            add_step("run_forced_photometry", _forced_photometry_step)

        if not stop_due_to_no_usable_frames and not config.skip_aperture_correction:
            def _aperture_correction_step() -> dict[str, Any]:
                nonlocal aperture_photometry_source_name, aperture_correction_activated, current_ensemble_name, initial_ensemble_name
                res = apply_aperture_correction(
                    session,
                    source_calibration_name=calibration_names["raw"],
                    target_calibration_name=calibration_names["aperture_corrected"],
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    instrument_unit_id=config.instrument_unit_id,
                    reference_radius_pix=config.aperture_correction_reference_radius_pix,
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    max_calibrator_mag_err=config.aperture_correction_max_calibrator_mag_err,
                    min_calibrators_per_frame=config.aperture_correction_min_calibrators_per_frame,
                    min_plane_calibrators=config.aperture_correction_min_plane_calibrators,
                    clip_sigma=config.aperture_correction_clip_sigma,
                    n_iterations=config.aperture_correction_n_iterations,
                    min_plane_bic_improvement=config.aperture_correction_min_plane_bic_improvement,
                    min_spatial_peak_to_peak=config.aperture_correction_min_spatial_peak_to_peak,
                    plane_selection_policy=config.aperture_correction_plane_policy,
                    max_residual_mad=config.aperture_correction_max_residual_mad,
                    max_abs_correction=config.aperture_correction_max_abs_correction,
                    max_abs_spatial_deviation=(
                        config.aperture_correction_max_abs_spatial_deviation
                    ),
                    max_abs_total_correction=(
                        config.aperture_correction_max_abs_total_correction
                    ),
                    min_good_frame_fraction=config.aperture_correction_min_good_frame_fraction,
                    max_global_median_residual_mad=config.aperture_correction_max_global_median_residual_mad,
                    fwhm_header_unit=config.fwhm_header_unit,
                    out_dir=(
                        config.out_dir
                        if profile_settings["aperture_correction_diagnostic_exports"]
                        else None
                    ),
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                    materialize_measurements=False,
                    immutable_generation=bool(config.record_run and not config.dry_run),
                    diagnose_performance=config.calibration_performance_diagnostics,
                )
                aperture_correction_activated = bool(res.quality_gate_passed)
                if aperture_correction_activated:
                    aperture_photometry_source_name = calibration_names["aperture_corrected"]
                    calibration_names["aperture_correction"] = calibration_names["aperture_corrected"]
                    outputs["aperture_correction_calibration"] = calibration_names["aperture_corrected"]
                else:
                    fallback_names = _aperture_correction_fallback_names(config)
                    calibration_names.update(fallback_names)
                    current_ensemble_name = calibration_names["ensemble"]
                    initial_ensemble_name = calibration_names["ensemble"]
                if res.diagnostics_summary_json:
                    outputs["aperture_correction_summary_json"] = res.diagnostics_summary_json
                if res.diagnostics_frames_csv:
                    outputs["aperture_correction_frames_csv"] = res.diagnostics_frames_csv
                if res.diagnostics_context_summary_json:
                    outputs["aperture_correction_context_summary_json"] = res.diagnostics_context_summary_json
                if res.diagnostics_context_frames_csv:
                    outputs["aperture_correction_context_frames_csv"] = res.diagnostics_context_frames_csv
                if res.diagnostics_context_plot_png:
                    outputs["aperture_correction_context_plot_png"] = res.diagnostics_context_plot_png
                result.quality_policy.update(
                    {
                        "aperture_correction_requested": True,
                        "aperture_correction_activated": aperture_correction_activated,
                        "aperture_correction_source_calibration": calibration_names["raw"],
                        "aperture_correction_target_calibration": calibration_names["aperture_corrected"],
                        "aperture_correction_effective_calibration": aperture_photometry_source_name,
                        "aperture_correction_reference_radius_pix": res.reference_radius_pix,
                        "aperture_correction_good_frame_fraction": res.good_frame_fraction,
                        "aperture_correction_median_residual_mad": res.median_residual_mad,
                        "aperture_correction_median_spatial_peak_to_peak": res.median_spatial_peak_to_peak,
                        "aperture_correction_median_plane_spatial_peak_to_peak": (
                            res.median_plane_spatial_peak_to_peak
                        ),
                        "aperture_correction_median_plane_max_abs_spatial_deviation": (
                            res.median_plane_max_abs_spatial_deviation
                        ),
                        "aperture_correction_legacy_frames_with_plane": (
                            res.legacy_frames_with_plane
                        ),
                        "aperture_correction_legacy_plane_frame_ids": list(
                            res.legacy_plane_frame_ids
                        ),
                        "aperture_correction_frames_promoted_to_plane_vs_legacy": (
                            res.frames_promoted_to_plane_vs_legacy
                        ),
                        "aperture_correction_promoted_to_plane_frame_ids": list(
                            res.promoted_to_plane_frame_ids
                        ),
                        "aperture_correction_frames_demoted_from_plane_vs_legacy": (
                            res.frames_demoted_from_plane_vs_legacy
                        ),
                        "aperture_correction_demoted_from_plane_frame_ids": list(
                            res.demoted_from_plane_frame_ids
                        ),
                        "aperture_correction_legacy_max_abs_correction": (
                            config.aperture_correction_max_abs_correction
                        ),
                        "aperture_correction_max_abs_spatial_deviation": (
                            config.aperture_correction_max_abs_spatial_deviation
                        ),
                        "aperture_correction_max_abs_total_correction": (
                            config.aperture_correction_max_abs_total_correction
                        ),
                        "aperture_correction_model_selection_semantics": (
                            "separated_spatial_total_safety_v2"
                        ),
                        "aperture_correction_error_propagation": "differential_spatial_only_v1",
                        "aperture_correction_frames_with_plane": res.frames_with_plane,
                        "aperture_correction_plane_frame_ids": list(res.plane_frame_ids),
                        "aperture_correction_frames_with_constant": res.frames_with_constant,
                        "aperture_correction_frames_identity_fallback": res.frames_identity_fallback,
                        "aperture_correction_quality_gate_failures": list(res.quality_gate_failures),
                        "aperture_context_analysis_status": res.context_analysis_status,
                        "aperture_context_frames_with_psf_shape": res.context_frames_with_psf_shape,
                        "aperture_context_plane_frames_with_psf_shape": (
                            res.context_plane_frames_with_psf_shape
                        ),
                        "aperture_context_frames_with_header_fwhm": res.context_frames_with_header_fwhm,
                        "aperture_context_plane_gradient_mean_angle_deg": (
                            res.context_plane_gradient_mean_angle_deg
                        ),
                        "aperture_context_plane_gradient_resultant_length": (
                            res.context_plane_gradient_resultant_length
                        ),
                        "aperture_context_plane_gradient_sign_consistency": (
                            res.context_plane_gradient_sign_consistency
                        ),
                        "aperture_context_plane_gradient_psf_axis_delta_median_deg": (
                            res.context_plane_gradient_psf_axis_delta_median_deg
                        ),
                        "aperture_context_plane_time_span_minutes": res.context_plane_time_span_minutes,
                        "aperture_context_plane_candidate_frames": (
                            res.context_plane_candidate_frames
                        ),
                        "aperture_context_plane_candidate_gradient_mean_angle_deg": (
                            res.context_plane_candidate_gradient_mean_angle_deg
                        ),
                        "aperture_context_plane_candidate_gradient_resultant_length": (
                            res.context_plane_candidate_gradient_resultant_length
                        ),
                        "aperture_context_plane_candidate_gradient_sign_consistency": (
                            res.context_plane_candidate_gradient_sign_consistency
                        ),
                    }
                )
                _stamp_product(
                    calibration_names["aperture_corrected"],
                    product_kind="APERTURE_CORRECTION",
                    product_role="INTERMEDIATE",
                    materialization_kind=_derived_materialization_kind(),
                    calibration_version_id=res.target_calibration_version_id,
                )
                return _result_to_dict(res)

            add_step("apply_aperture_correction", _aperture_correction_step)
        else:
            result.quality_policy.update(
                {
                    "aperture_correction_requested": not config.skip_aperture_correction,
                    "aperture_correction_activated": False,
                    "aperture_correction_effective_calibration": aperture_photometry_source_name,
                }
            )

        if not stop_due_to_no_usable_frames and not config.skip_aperture_zp:
            def _aperture_zp_step():
                nonlocal aperture_zp_downstream_seed_rows, aperture_zp_analysis_base_seed_rows
                prepare_downstream_seed = bool(
                    not config.skip_ensemble
                    and not config.materialize_intermediate_calibrations
                    and not config.dry_run
                )
                prepare_analysis_seed = bool(
                    prepare_downstream_seed and not config.skip_object_qc
                )
                res = aperture_zp_calibrate(
                    session,
                    source_calibration_name=aperture_photometry_source_name,
                    target_calibration_name=calibration_names["aperture_zp"],
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    mag_key=config.mag_key,
                    min_ref_stars_per_frame=config.aperture_zp_min_ref_stars_per_frame,
                    max_mag_err=config.aperture_zp_max_mag_err,
                    clip_sigma=config.aperture_zp_clip_sigma,
                    n_iterations=config.aperture_zp_n_iterations,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                    materialize_measurements=False,
                    immutable_generation=bool(config.record_run and not config.dry_run),
                    prepare_downstream_seed=prepare_downstream_seed,
                    prepare_analysis_snapshot_seed=prepare_analysis_seed,
                )
                aperture_zp_downstream_seed_rows = getattr(
                    res, "_ensemble_input_rows", None
                )
                aperture_zp_analysis_base_seed_rows = getattr(
                    res, "_analysis_snapshot_base_rows", None
                )
                _stamp_product(
                    calibration_names["aperture_zp"],
                    product_kind="APERTURE_ZP",
                    product_role="INTERMEDIATE",
                    materialization_kind=_derived_materialization_kind(),
                    calibration_version_id=res.target_calibration_version_id,
                )
                return res

            add_step("aperture_zp_calibrate", _aperture_zp_step)

        if not stop_due_to_no_usable_frames and not config.skip_ensemble:
            def _resolve_target_step() -> dict[str, Any]:
                nonlocal target_resolution, target_object_ids
                nonlocal explicit_target_catalog_selection
                has_explicit_selector = bool(
                    config.target_source_id
                    or config.target_object_id is not None
                    or config.target_ra_deg is not None
                    or config.target_dec_deg is not None
                )
                if has_explicit_selector and explicit_target_catalog_selection is None:
                    explicit_target_catalog_selection = resolve_explicit_target_catalog_source(
                        session,
                        catalog_name=config.catalog_name,
                        target_source_id=config.target_source_id,
                        target_object_id=config.target_object_id,
                        target_ra_deg=config.target_ra_deg,
                        target_dec_deg=config.target_dec_deg,
                    )
                target = _unresolved_explicit_target_resolution(
                    config, explicit_target_catalog_selection
                )
                if target is None:
                    source_name = calibration_names["aperture_zp"] if not config.skip_aperture_zp else aperture_photometry_source_name
                    target = resolve_target_object(
                        session,
                        calibration_name=source_name,
                        catalog_name=config.catalog_name,
                        frame_ids=_frame_ids_for_step(selected_frame_ids),
                        target_source_id=config.target_source_id,
                        target_object_id=config.target_object_id,
                        target_ra_deg=config.target_ra_deg,
                        target_dec_deg=config.target_dec_deg,
                        max_sep_arcsec=config.target_max_sep_arcsec,
                    )
                target_resolution = target.to_dict()
                target_object_ids = [int(target.object_id)] if target.resolved and target.object_id is not None else []
                return dict(target_resolution)

            add_step("resolve_target_object", _resolve_target_step)

        def _publish_photometric_qc_ensemble_base(
            _session: Session,
            *,
            logical_name: str,
            calibration_version_id: int | None,
            materialization_kind: str,
        ) -> None:
            # Kept separate from the spatial handoff so the experimental spatial
            # contract and its regression test remain unchanged.
            _stamp_product(
                logical_name,
                product_kind="ENSEMBLE",
                product_role="INTERMEDIATE",
                materialization_kind=materialization_kind,
                calibration_version_id=calibration_version_id,
            )

        def _run_ensemble_with_optional_spatial(
            *,
            final_name: str,
            base_name: str,
            exclude_reference_object_ids: list[int] | None,
            calibration_iteration_no: int = 1,
        ):
            nonlocal latest_photometric_qc_good_frame_ids
            nonlocal latest_photometric_qc_suspect_frame_ids
            nonlocal latest_photometric_qc_bad_frame_ids
            base_result = ensemble_calibrate_measurements(
                session,
                source_calibration_name=(
                    calibration_names["aperture_zp"]
                    if not config.skip_aperture_zp
                    else aperture_photometry_source_name
                ),
                target_calibration_name=base_name,
                catalog_name=config.catalog_name,
                instrument_unit_id=config.instrument_unit_id,
                frame_ids=_frame_ids_for_step(selected_frame_ids),
                min_measurements=config.ensemble_min_measurements,
                min_ref_measurements=config.ensemble_min_ref_measurements,
                min_ref_stars_per_frame=config.ensemble_min_ref_stars_per_frame,
                max_ref_rms=config.ensemble_max_ref_rms,
                max_ref_mad=config.ensemble_max_ref_mad,
                n_iterations=config.ensemble_n_iterations,
                dry_run=config.dry_run,
                insert_batch_size=config.bulk_insert_batch_size,
                exclude_reference_object_ids=exclude_reference_object_ids,
                calibration_iteration_no=calibration_iteration_no,
                materialize_measurements=False,
                immutable_generation=bool(config.record_run and not config.dry_run),
                prepare_analysis_snapshot_seed=bool(
                    not config.enable_spatial_ensemble
                    and not config.enable_photometric_frame_qc
                    and not config.skip_object_qc
                    and not config.materialize_intermediate_calibrations
                    and not config.dry_run
                ),
                _preloaded_aperture_zp_rows=aperture_zp_downstream_seed_rows,
                _preloaded_analysis_snapshot_base_rows=aperture_zp_analysis_base_seed_rows,
            )
            _raise_if_ensemble_unusable(
                base_result,
                min_ref_stars_per_frame=config.ensemble_min_ref_stars_per_frame,
            )
            if config.enable_photometric_frame_qc:
                # Publish the newly created normalized conventional ensemble before
                # the QC layer resolves it by logical name.  This mirrors the
                # immutable-generation handoff required by the experimental spatial
                # branch, but keeps the intermediate product normalized-only.
                _publish_photometric_qc_ensemble_base(
                    session,
                    logical_name=base_name,
                    calibration_version_id=base_result.target_calibration_version_id,
                    materialization_kind=_derived_materialization_kind(),
                )
                qc_result = apply_photometric_frame_qc(
                    session,
                    source_calibration_name=base_name,
                    target_calibration_name=final_name,
                    reference_object_ids=base_result.reference_object_ids,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    instrument_unit_id=config.instrument_unit_id,
                    min_reference_stars_per_frame=config.photometric_frame_qc_min_reference_stars_per_frame,
                    suspect_sigma=config.photometric_frame_qc_suspect_sigma,
                    bad_sigma=config.photometric_frame_qc_bad_sigma,
                    suspect_ratio=config.photometric_frame_qc_suspect_ratio,
                    bad_ratio=config.photometric_frame_qc_bad_ratio,
                    absolute_suspect_residual_sigma_mag=config.photometric_frame_qc_absolute_suspect_residual_sigma_mag,
                    absolute_bad_residual_sigma_mag=config.photometric_frame_qc_absolute_bad_residual_sigma_mag,
                    spatial_suspect_peak_to_peak_mag=config.photometric_frame_qc_spatial_suspect_peak_to_peak_mag,
                    spatial_bad_peak_to_peak_mag=config.photometric_frame_qc_spatial_bad_peak_to_peak_mag,
                    aperture_context_frames_csv=outputs.get("aperture_correction_context_frames_csv"),
                    psf_frames_csv=outputs.get("psf_frames_csv"),
                    out_dir=config.out_dir,
                    diagnostics_label=f"iter{int(calibration_iteration_no)}",
                    dry_run=config.dry_run,
                    immutable_generation=bool(config.record_run and not config.dry_run),
                )
                qc_result.excluded_reference_object_ids = list(
                    getattr(base_result, "excluded_reference_object_ids", []) or []
                )
                qc_result.actually_excluded_reference_object_ids = list(
                    getattr(base_result, "actually_excluded_reference_object_ids", [])
                    or qc_result.excluded_reference_object_ids
                )
                qc_result.excluded_reference_objects = len(qc_result.excluded_reference_object_ids)
                qc_result.actually_excluded_reference_objects = len(
                    qc_result.actually_excluded_reference_object_ids
                )
                qc_result.base_ensemble_reference_candidates = int(base_result.reference_candidates)
                qc_result.base_ensemble_usable_reference_objects = int(base_result.usable_reference_objects)
                qc_result.base_ensemble_required_references_per_frame = int(config.ensemble_min_ref_stars_per_frame)
                qc_result.base_ensemble_selected_frames = int(base_result.selected_frames)
                qc_result.base_ensemble_frames_with_offsets = int(base_result.frames_with_offsets)
                qc_result.base_ensemble_calibrated = int(base_result.calibrated)
                qc_result.base_ensemble_target_calibration_version_id = base_result.target_calibration_version_id
                latest_photometric_qc_good_frame_ids = list(qc_result.good_frame_ids)
                latest_photometric_qc_suspect_frame_ids = list(qc_result.suspect_frame_ids)
                latest_photometric_qc_bad_frame_ids = list(qc_result.bad_frame_ids)
                if qc_result.diagnostics_summary_json:
                    outputs["photometric_frame_qc_summary_json"] = qc_result.diagnostics_summary_json
                if qc_result.diagnostics_frames_csv:
                    outputs["photometric_frame_qc_frames_csv"] = qc_result.diagnostics_frames_csv
                return qc_result

            if not config.enable_spatial_ensemble:
                return base_result

            # Publish the newly normalized conventional-ensemble generation before
            # resolving it by logical name below.  The spatial stage consumes the
            # calibration view and therefore does not require a full measurement copy.
            _stamp_product(
                base_name,
                product_kind="ENSEMBLE",
                product_role="INTERMEDIATE",
                materialization_kind="NORMALIZED_TERMS",
                calibration_version_id=base_result.target_calibration_version_id,
            )

            spatial_result = apply_spatial_ensemble_correction(
                session,
                source_calibration_name=base_name,
                target_calibration_name=final_name,
                reference_object_ids=base_result.reference_object_ids,
                frame_ids=_frame_ids_for_step(selected_frame_ids),
                instrument_unit_id=config.instrument_unit_id,
                max_order=config.spatial_ensemble_max_order,
                min_ref_stars_per_frame=config.spatial_ensemble_min_ref_stars_per_frame,
                clip_sigma=config.spatial_ensemble_clip_sigma,
                n_iterations=config.spatial_ensemble_n_iterations,
                min_bic_improvement=config.spatial_ensemble_min_bic_improvement,
                min_spatial_peak_to_peak=config.spatial_ensemble_min_spatial_peak_to_peak,
                max_residual_mad=config.spatial_ensemble_max_residual_mad,
                max_residual_mad_degradation=config.spatial_ensemble_max_residual_mad_degradation,
                max_abs_spatial_deviation=config.spatial_ensemble_max_abs_spatial_deviation,
                min_good_frame_fraction=config.spatial_ensemble_min_good_frame_fraction,
                out_dir=config.out_dir,
                diagnostics_label=f"iter{int(calibration_iteration_no)}",
                dry_run=config.dry_run,
                insert_batch_size=config.bulk_insert_batch_size,
                immutable_generation=bool(config.record_run and not config.dry_run),
            )
            spatial_result.excluded_reference_object_ids = list(
                getattr(base_result, "excluded_reference_object_ids", []) or []
            )
            spatial_result.excluded_reference_objects = len(
                spatial_result.excluded_reference_object_ids
            )
            spatial_result.actually_excluded_reference_object_ids = list(
                getattr(base_result, "actually_excluded_reference_object_ids", [])
                or spatial_result.excluded_reference_object_ids
            )
            spatial_result.actually_excluded_reference_objects = len(
                spatial_result.actually_excluded_reference_object_ids
            )
            return spatial_result

        if not stop_due_to_no_usable_frames and not config.skip_ensemble:
            def _ensemble_step():
                nonlocal current_ensemble_reference_object_ids
                calibration_result = _run_ensemble_with_optional_spatial(
                    final_name=calibration_names["ensemble"],
                    base_name=calibration_names.get("ensemble_base", calibration_names["ensemble"]),
                    exclude_reference_object_ids=target_object_ids,
                )
                _remember_analysis_snapshot_seed(
                    calibration_names["ensemble"], calibration_result
                )
                current_ensemble_reference_object_ids = {
                    int(object_id) for object_id in calibration_result.reference_object_ids
                }
                _stamp_product(
                    calibration_names["ensemble"],
                    product_kind="ENSEMBLE",
                    product_role="INTERMEDIATE",
                    materialization_kind=_derived_materialization_kind(),
                    calibration_version_id=calibration_result.target_calibration_version_id,
                )
                return calibration_result

            add_step("ensemble_calibrate", _ensemble_step)

        if not stop_due_to_no_usable_frames and not config.skip_object_qc:
            add_step(
                "compute_object_qc",
                lambda: _compute_object_qc_for(current_ensemble_name),
            )

        if (
            not stop_due_to_no_usable_frames
            and config.export_target_light_curve
            and target_resolution.get("resolved")
            and not config.exclude_candidates_from_ensemble
        ):
            def _target_light_curve_step() -> dict[str, Any]:
                target = TargetResolution(**{k: v for k, v in target_resolution.items() if k in TargetResolution.__dataclass_fields__})
                csv_path = None
                json_path = None
                if config.out_dir:
                    out_dir = Path(config.out_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    csv_path = out_dir / f"{config.calibration_prefix}_target_light_curve.csv"
                    json_path = out_dir / f"{config.calibration_prefix}_target_light_curve.json"
                product = export_target_light_curve_product(
                    session,
                    target=target,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    series_id=series_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    interseries_generation_ids=config.interseries_generation_ids,
                    reject_flags=_science_reject_flags(config, require_detection_support_effective),
                    out_csv=csv_path,
                    out_json=json_path,
                )
                payload = product.to_dict()
                if product.csv_path:
                    outputs["target_light_curve_csv"] = product.csv_path
                if product.json_path:
                    outputs["target_light_curve_json"] = product.json_path
                return payload

            add_step("export_target_light_curve", _target_light_curve_step)

        if not stop_due_to_no_usable_frames and not config.skip_aperture_stability_qc:
            def _aperture_stability_step() -> dict[str, Any]:
                res = compute_candidate_aperture_stability_qc(
                    session,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    radii=config.aperture_stability_radii,
                    sweep_calibration_prefix=config.aperture_stability_sweep_prefix
                    or f"{config.calibration_prefix}_apstab",
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    mag_key=config.mag_key,
                    min_measurements=config.object_qc_min_measurements,
                    min_excess=config.field_report_min_excess,
                    max_mag_median=config.field_report_max_mag_median,
                    max_ref_mag_delta=config.max_ref_mag_delta,
                    max_candidates=config.aperture_stability_max_candidates,
                    min_stable_corr=config.aperture_stability_min_corr,
                    max_stable_rms_delta=config.aperture_stability_max_rms_delta,
                    max_median_shift=config.aperture_stability_max_median_shift,
                    min_ref_stars_per_frame=config.aperture_zp_min_ref_stars_per_frame,
                    max_mag_err=config.aperture_zp_max_mag_err,
                    clip_sigma=config.aperture_zp_clip_sigma,
                    n_iterations=config.aperture_zp_n_iterations,
                    min_ref_measurements=config.ensemble_min_ref_measurements,
                    ensemble_min_ref_stars_per_frame=config.ensemble_min_ref_stars_per_frame,
                    max_ref_rms=config.ensemble_max_ref_rms,
                    max_ref_mad=config.ensemble_max_ref_mad,
                    dry_run=config.dry_run,
                )
                payload = dict(res.summary)
                payload["rows_total"] = len(res.rows)
                if config.out_dir:
                    out_dir = Path(config.out_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    csv_path = out_dir / f"{config.calibration_prefix}_candidate_aperture_stability.csv"
                    rows_written = export_candidate_aperture_stability_csv(res.rows, csv_path)
                    outputs["candidate_aperture_stability"] = str(csv_path)
                    payload["candidate_aperture_stability_csv"] = str(csv_path)
                    payload["candidate_aperture_stability_rows_written"] = int(rows_written)
                return payload

            add_step("aperture_stability_qc", _aperture_stability_step)

        def _build_field_report_for_frames(
            frame_scope: list[int],
            *,
            analysis_snapshot: CalibrationAnalysisSnapshot | None = None,
        ):
            kwargs = dict(
                calibration_name=current_ensemble_name,
                catalog_name=config.catalog_name,
                instrument_unit_id=config.instrument_unit_id,
                frame_ids=_frame_ids_for_step(frame_scope),
                min_measurements=config.object_qc_min_measurements,
                min_excess=config.field_report_min_excess,
                max_mag_median=config.field_report_max_mag_median,
                top_limit=config.field_report_top_limit,
                require_detection_support=require_detection_support_effective,
                target_object_ids=target_object_ids,
                enable_intrinsic_variability_candidates=config.enable_intrinsic_variability_candidates,
                min_intrinsic_rms=config.field_report_min_intrinsic_rms,
                min_rms_over_mag_err=config.field_report_min_rms_over_mag_err,
                min_mad_over_mag_err=config.field_report_min_mad_over_mag_err,
                intrinsic_mag_err_floor=config.field_report_intrinsic_mag_err_floor,
                min_robust_peak_to_peak=config.field_report_min_robust_peak_to_peak,
                enable_low_amplitude_intrinsic_candidates=config.enable_low_amplitude_intrinsic_candidates,
                low_amplitude_min_measurements=config.field_report_low_amplitude_min_measurements,
                low_amplitude_min_intrinsic_rms=config.field_report_low_amplitude_min_intrinsic_rms,
                low_amplitude_min_rms_over_mag_err=config.field_report_low_amplitude_min_rms_over_mag_err,
                low_amplitude_min_mad_over_mag_err=config.field_report_low_amplitude_min_mad_over_mag_err,
                low_amplitude_min_robust_peak_to_peak=config.field_report_low_amplitude_min_robust_peak_to_peak,
                borderline_threshold_ratio=config.field_report_borderline_threshold_ratio,
                diagnostic_max_failed_criteria=config.field_report_diagnostic_max_failed_criteria,
                include_diagnostics=config.stage2_profile == "diagnostic",
            )
            if analysis_snapshot is not None:
                kwargs["analysis_snapshot"] = analysis_snapshot
            return build_field_qc_report(session, **kwargs)

        def _good_frame_confirmed_candidate_rows(
            selection_rows: list[dict[str, Any]],
        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            if not config.enable_photometric_frame_qc:
                return [dict(row) for row in selection_rows], {"enabled": False}
            good_ids = list(latest_photometric_qc_good_frame_ids)
            min_good_frames = max(5, int(config.object_qc_min_measurements))
            if len(good_ids) < min_good_frames:
                merged = _apply_good_frame_candidate_confirmation(
                    selection_rows,
                    [],
                    n_good_frames=len(good_ids),
                    min_good_frames=min_good_frames,
                )
                return merged, _good_frame_confirmation_summary(merged, len(good_ids), min_good_frames)
            good_report = _build_field_report_for_frames(good_ids)
            merged = _apply_good_frame_candidate_confirmation(
                selection_rows,
                list(good_report.candidate_rows),
                n_good_frames=len(good_ids),
                min_good_frames=min_good_frames,
            )
            summary = _good_frame_confirmation_summary(merged, len(good_ids), min_good_frames)
            summary["candidate_triage_good_frames"] = good_report.report.get("candidate_triage", {})
            if config.out_dir and config.stage2_profile == "diagnostic":
                out_dir = Path(config.out_dir) / "candidate_good_frame_confirmation"
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "summary.json").write_text(
                    json.dumps(
                        {"summary": summary, "candidates": merged},
                        ensure_ascii=False, indent=2, default=str,
                    ),
                    encoding="utf-8",
                )
                outputs["candidate_good_frame_confirmation_json"] = str(out_dir / "summary.json")
            return merged, summary

        if not stop_due_to_no_usable_frames and not config.skip_field_report:
            def _field_report_step() -> dict[str, Any]:
                nonlocal latest_candidate_rows, candidate_selection_rows, candidate_membership_history
                report = _build_field_report_for_frames(
                    selected_frame_ids,
                    analysis_snapshot=_analysis_snapshot_for(current_ensemble_name),
                )
                confirmed_rows, confirmation_summary = _good_frame_confirmed_candidate_rows(
                    list(report.candidate_rows)
                )
                correlation_result = annotate_candidate_axis_correlations(
                    session,
                    candidate_rows=confirmed_rows,
                    calibration_name=current_ensemble_name,
                    good_frame_ids=latest_photometric_qc_good_frame_ids,
                ) if (
                    config.enable_photometric_frame_qc
                    and config.stage2_profile == "diagnostic"
                ) else None
                correlation_rows = (
                    correlation_result.candidate_rows if correlation_result is not None else confirmed_rows
                )
                correlation_summary = (
                    correlation_result.summary if correlation_result is not None else {"enabled": False}
                )
                latest_candidate_rows = list(correlation_rows)
                if config.stage2_profile == "diagnostic":
                    membership_snapshot = _candidate_membership_snapshot(
                        correlation_rows,
                        calibration_name=current_ensemble_name,
                        stage_index=len(candidate_membership_history) + 1,
                    )
                    if candidate_membership_history:
                        membership_snapshot.update(
                            _candidate_membership_transition(
                                candidate_membership_history[-1],
                                membership_snapshot,
                            )
                        )
                    candidate_membership_history.append(membership_snapshot)
                else:
                    membership_snapshot = {
                        "stage_index": len(candidate_membership_history) + 1,
                        "candidate_rows": len(correlation_rows),
                        "accepted_candidates": sum(
                            str(row.get("candidate_status") or "").upper() == "ACCEPTED"
                            for row in correlation_rows
                        ),
                        "candidate_input_retained_for_ensemble_exclusion": bool(
                            config.exclude_candidates_from_ensemble
                        ),
                    }
                if not color_calibration_activated:
                    candidate_selection_rows = list(correlation_rows)
                report_metadata = report.report.get("metadata", {})
                if config.stage2_profile == "diagnostic":
                    payload: dict[str, Any] = {
                        "summary_rows": report.summary_rows,
                        "warnings": report.report.get("warnings", []),
                        "candidate_triage": report.report.get("candidate_triage", {}),
                        "candidate_triage_raw": report.report.get("candidate_triage", {}),
                        "candidate_good_frame_confirmation": confirmation_summary,
                        "candidate_axis_correlation": correlation_summary,
                        "blend_diagnostics": report.report.get("blend_diagnostics", {}),
                        "candidate_membership": membership_snapshot,
                        "analysis_snapshot_algorithm": report_metadata.get(
                            "analysis_snapshot_algorithm"
                        ),
                        "analysis_snapshot_cache_hit": report_metadata.get(
                            "analysis_snapshot_cache_hit"
                        ),
                        "analysis_snapshot_measurements": report_metadata.get(
                            "analysis_snapshot_measurements"
                        ),
                    }
                else:
                    payload = {
                        "warnings": report.report.get("warnings", []),
                        "candidate_triage": report.report.get("candidate_triage", {}),
                        "candidate_good_frame_confirmation": confirmation_summary,
                        "candidate_membership": membership_snapshot,
                        "candidate_input_retained_for_ensemble_exclusion": bool(
                            config.exclude_candidates_from_ensemble
                        ),
                        "analysis_snapshot_algorithm": report_metadata.get(
                            "analysis_snapshot_algorithm"
                        ),
                        "analysis_snapshot_cache_hit": report_metadata.get(
                            "analysis_snapshot_cache_hit"
                        ),
                        "analysis_snapshot_measurements": report_metadata.get(
                            "analysis_snapshot_measurements"
                        ),
                    }
                if config.out_dir and config.stage2_profile == "diagnostic":
                    out_dir = Path(config.out_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    json_path = out_dir / f"{config.calibration_prefix}_field_qc_report.json"
                    tables_prefix = out_dir / f"{config.calibration_prefix}_field_qc"
                    report.report["candidate_good_frame_confirmation"] = confirmation_summary
                    report.report["candidate_axis_correlation"] = correlation_summary
                    report.report["candidate_triage_raw"] = report.report.get("candidate_triage", {})
                    export_field_qc_json(report.report, json_path)
                    table_outputs = export_field_qc_tables(
                        prefix=tables_prefix,
                        rms_bins=report.rms_bins,
                        candidate_rows=correlation_rows,
                        blend_rows=report.blend_rows,
                    )
                    if correlation_result is not None:
                        correlation_dir = out_dir / "candidate_axis_correlation"
                        correlation_outputs = export_candidate_axis_correlation(
                            correlation_result, correlation_dir
                        )
                        outputs["candidate_axis_correlation_pairs_csv"] = correlation_outputs["pairs_csv"]
                        outputs["candidate_axis_correlation_json"] = correlation_outputs["summary_json"]
                    outputs["field_qc_report_json"] = str(json_path)
                    outputs.update({f"field_qc_{key}": value for key, value in table_outputs.items()})
                    payload["outputs"] = dict(outputs)
                return payload

            add_step("field_qc_report", _field_report_step)

        if (
            not stop_due_to_no_usable_frames
            and config.exclude_candidates_from_ensemble
            and not config.skip_ensemble
            and not config.skip_object_qc
            and not config.skip_field_report
            and candidate_ensemble_iteration_limit >= 2
        ):
            for candidate_ensemble_iteration in range(2, candidate_ensemble_iteration_limit + 1):
                select_step_name = (
                    "select_candidate_reference_exclusions"
                    if candidate_ensemble_iteration == 2
                    else f"select_candidate_reference_exclusions_iter{candidate_ensemble_iteration}"
                )

                def _select_candidate_reference_exclusions_step(iteration: int = candidate_ensemble_iteration) -> dict[str, Any]:
                    nonlocal candidate_exclusion_object_ids
                    nonlocal candidate_exclusion_new_object_ids
                    nonlocal candidate_exclusion_effective_object_ids
                    nonlocal candidate_ensemble_terminal_reason
                    previous_ids = set(candidate_exclusion_object_ids)
                    discovered_ids = _candidate_object_ids_for_ensemble_exclusion(
                        latest_candidate_rows,
                        target_object_ids=target_object_ids,
                    )
                    growth = apply_candidate_ensemble_growth_policy(
                        previous_ids=previous_ids,
                        discovered_ids=set(discovered_ids),
                        iteration=iteration,
                        policy=candidate_ensemble_growth_policy,
                        target_object_ids=set(target_object_ids),
                    )
                    candidate_exclusion_object_ids = list(growth["requested_ids"])
                    candidate_exclusion_new_object_ids = list(growth["new_ids"])
                    candidate_exclusion_effective_object_ids = _effective_reference_exclusion_ids(
                        candidate_exclusion_object_ids,
                        current_ensemble_reference_object_ids,
                    ) or []
                    candidate_origin_counts: dict[str, int] = {}
                    for row in latest_candidate_rows:
                        if str(row.get("candidate_status") or "").upper() != "ACCEPTED":
                            continue
                        for origin in _candidate_origin_parts(row.get("candidate_origin")):
                            candidate_origin_counts[origin] = candidate_origin_counts.get(origin, 0) + 1
                    reference_precheck_available = current_ensemble_reference_object_ids is not None
                    growth_allows_iteration = iteration == 2 or bool(candidate_exclusion_new_object_ids)
                    has_effective_reference_exclusions = (
                        bool(candidate_exclusion_effective_object_ids)
                        if reference_precheck_available
                        else True
                    )
                    will_run_iteration = bool(
                        candidate_exclusion_object_ids
                        and growth_allows_iteration
                        and has_effective_reference_exclusions
                    )
                    stop_reason = None
                    if not candidate_exclusion_object_ids:
                        stop_reason = "no accepted candidates or target objects to exclude"
                    elif iteration > 2 and not candidate_exclusion_new_object_ids:
                        stop_reason = (
                            "initial_only_growth_policy_suppressed_new_candidates"
                            if growth["suppressed_ids"]
                            else "converged: no new accepted candidate reference exclusions"
                        )
                    elif reference_precheck_available and not candidate_exclusion_effective_object_ids:
                        stop_reason = "no_usable_reference_exclusions"
                        candidate_ensemble_terminal_reason = stop_reason
                    return {
                        "enabled": True,
                        "source": "field_qc_report",
                        "iteration": iteration,
                        "max_iterations": candidate_ensemble_iteration_limit,
                        "fixed_max_iterations": candidate_ensemble_fixed_max_iterations,
                        "convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
                        "until_converged": bool(config.candidate_ensemble_until_converged),
                        "iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
                        "growth_policy": candidate_ensemble_growth_policy,
                        "input_ensemble_calibration": current_ensemble_name,
                        "field_report_candidate_rows": len(latest_candidate_rows),
                        "target_object_ids": list(target_object_ids),
                        "candidate_object_ids": list(discovered_ids),
                        "suppressed_new_candidate_object_ids": list(growth["suppressed_ids"]),
                        "suppressed_new_candidate_objects": len(growth["suppressed_ids"]),
                        "new_requested_excluded_object_ids": list(candidate_exclusion_new_object_ids),
                        "new_requested_excluded_objects": len(candidate_exclusion_new_object_ids),
                        "requested_excluded_object_ids": list(candidate_exclusion_object_ids),
                        "requested_excluded_objects": len(candidate_exclusion_object_ids),
                        "reference_precheck_available": reference_precheck_available,
                        "input_reference_objects": (
                            len(current_ensemble_reference_object_ids)
                            if current_ensemble_reference_object_ids is not None
                            else None
                        ),
                        "effective_reference_exclusion_object_ids": list(
                            candidate_exclusion_effective_object_ids
                        ),
                        "effective_reference_exclusion_objects": len(
                            candidate_exclusion_effective_object_ids
                        ),
                        # Backwards-compatible aliases from 0.5.33.0.
                        "excluded_object_ids": list(candidate_exclusion_object_ids),
                        "excluded_objects": len(candidate_exclusion_object_ids),
                        "candidate_origin_counts": candidate_origin_counts,
                        "will_run_iteration": will_run_iteration,
                        "will_run_second_pass": will_run_iteration if iteration == 2 else None,
                        "stop_reason": stop_reason,
                    }

                add_step(select_step_name, _select_candidate_reference_exclusions_step)

                if not candidate_exclusion_object_ids:
                    break
                if candidate_ensemble_iteration > 2 and not candidate_exclusion_new_object_ids:
                    break
                if (
                    current_ensemble_reference_object_ids is not None
                    and not candidate_exclusion_effective_object_ids
                ):
                    break

                ensemble_step_name = (
                    "ensemble_calibrate_candidate_excluded"
                    if candidate_ensemble_iteration == 2
                    else f"ensemble_calibrate_candidate_excluded_iter{candidate_ensemble_iteration}"
                )
                object_qc_step_name = (
                    "compute_object_qc_candidate_excluded"
                    if candidate_ensemble_iteration == 2
                    else f"compute_object_qc_candidate_excluded_iter{candidate_ensemble_iteration}"
                )
                field_report_step_name = (
                    "field_qc_report_candidate_excluded"
                    if candidate_ensemble_iteration == 2
                    else f"field_qc_report_candidate_excluded_iter{candidate_ensemble_iteration}"
                )

                def _candidate_excluded_ensemble_step(iteration: int = candidate_ensemble_iteration) -> dict[str, Any]:
                    nonlocal current_ensemble_name, current_ensemble_reference_object_ids
                    current_ensemble_name = _candidate_excluded_calibration_name(
                        config,
                        iteration=iteration,
                        base_ensemble_name=initial_ensemble_name,
                    )
                    if "ensemble_initial" not in calibration_names:
                        calibration_names["ensemble_initial"] = initial_ensemble_name
                    calibration_names[f"ensemble_candidates_excluded_iter{iteration}"] = current_ensemble_name
                    if iteration == 2:
                        calibration_names["ensemble_candidates_excluded"] = current_ensemble_name
                    calibration_names["ensemble"] = current_ensemble_name
                    calibration_names["science"] = current_ensemble_name
                    outputs["ensemble_initial_calibration"] = calibration_names["ensemble_initial"]
                    outputs["science_calibration"] = current_ensemble_name
                    outputs["ensemble_candidate_excluded_calibration"] = current_ensemble_name
                    outputs[f"ensemble_candidate_excluded_iter{iteration}_calibration"] = current_ensemble_name
                    if config.enable_spatial_ensemble:
                        candidate_base_name = compact_generated_calibration_name(
                            f"{current_ensemble_name}_pre_spatial_v1",
                            semantic_suffix="_pre_spatial_v1",
                        )
                    elif config.enable_photometric_frame_qc:
                        candidate_base_name = compact_generated_calibration_name(
                            f"{current_ensemble_name}_pre_photqc_v3",
                            semantic_suffix="_pre_photqc_v3",
                        )
                    else:
                        candidate_base_name = current_ensemble_name
                    calibration_result = _run_ensemble_with_optional_spatial(
                        final_name=current_ensemble_name,
                        base_name=candidate_base_name,
                        exclude_reference_object_ids=candidate_exclusion_object_ids,
                        calibration_iteration_no=iteration,
                    )
                    _remember_analysis_snapshot_seed(current_ensemble_name, calibration_result)
                    current_ensemble_reference_object_ids = {
                        int(object_id) for object_id in calibration_result.reference_object_ids
                    }
                    _stamp_product(
                        current_ensemble_name,
                        product_kind="ENSEMBLE",
                        product_role="INTERMEDIATE",
                        materialization_kind=_derived_materialization_kind(),
                        calibration_version_id=calibration_result.target_calibration_version_id,
                    )
                    payload = _result_to_dict(calibration_result)
                    payload["candidate_ensemble_iteration"] = iteration
                    payload["candidate_ensemble_max_iterations"] = candidate_ensemble_iteration_limit
                    payload["candidate_ensemble_fixed_max_iterations"] = candidate_ensemble_fixed_max_iterations
                    payload["candidate_ensemble_convergence_max_iterations"] = candidate_ensemble_convergence_max_iterations
                    payload["candidate_ensemble_until_converged"] = bool(config.candidate_ensemble_until_converged)
                    payload["candidate_ensemble_iteration_mode"] = "until_converged" if config.candidate_ensemble_until_converged else "fixed"
                    payload["candidate_ensemble_growth_policy"] = candidate_ensemble_growth_policy
                    payload["requested_excluded_object_ids"] = list(candidate_exclusion_object_ids)
                    payload["requested_excluded_objects"] = len(candidate_exclusion_object_ids)
                    payload["actually_excluded_reference_object_ids"] = list(
                        payload.get("actually_excluded_reference_object_ids")
                        or payload.get("excluded_reference_object_ids")
                        or []
                    )
                    payload["actually_excluded_reference_objects"] = int(
                        payload.get("actually_excluded_reference_objects")
                        or payload.get("excluded_reference_objects")
                        or 0
                    )
                    return payload

                add_step(ensemble_step_name, _candidate_excluded_ensemble_step)

                add_step(
                    object_qc_step_name,
                    lambda: _compute_object_qc_for(current_ensemble_name),
                )

                add_step(field_report_step_name, _field_report_step)

            def _candidate_ensemble_convergence_step() -> dict[str, Any]:
                final_candidate_object_ids = _candidate_object_ids_for_ensemble_exclusion(
                    latest_candidate_rows,
                    target_object_ids=target_object_ids,
                )
                requested_set = set(candidate_exclusion_object_ids)
                new_final_candidate_object_ids = sorted(set(final_candidate_object_ids) - requested_set)
                ensemble_passes = _step_results_with_prefix(result, "ensemble_calibrate_candidate_excluded")
                converged = not new_final_candidate_object_ids
                growth_suppressed = bool(
                    candidate_ensemble_growth_policy == "initial_only"
                    and new_final_candidate_object_ids
                )
                if candidate_ensemble_terminal_reason == "no_usable_reference_exclusions":
                    stop_reason = "no_usable_reference_exclusions"
                    stop_status = "NO_USABLE_REFERENCE_EXCLUSIONS"
                    converged = True
                elif not final_candidate_object_ids and not candidate_exclusion_object_ids:
                    stop_reason = "no accepted candidates or target objects to exclude"
                    stop_status = "NO_EXCLUSIONS"
                elif converged:
                    stop_reason = "converged"
                    stop_status = "CONVERGED"
                elif candidate_ensemble_growth_policy == "initial_only":
                    stop_reason = "initial_only_growth_policy_suppressed_new_candidates"
                    stop_status = "POLICY_STOPPED"
                elif config.candidate_ensemble_until_converged:
                    stop_reason = "max_iterations_reached_with_new_candidates"
                    stop_status = "MAX_ITERATIONS_REACHED"
                else:
                    stop_reason = "fixed_iteration_limit_reached_with_new_candidates"
                    stop_status = "FIXED_ITERATION_LIMIT_REACHED"
                policy_satisfied = bool(
                    converged
                    or growth_suppressed
                    or not config.candidate_ensemble_until_converged
                )
                return {
                    "enabled": True,
                    "iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
                    "growth_policy": candidate_ensemble_growth_policy,
                    "until_converged": bool(config.candidate_ensemble_until_converged),
                    "fixed_max_iterations": candidate_ensemble_fixed_max_iterations,
                    "convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
                    "iteration_limit": candidate_ensemble_iteration_limit,
                    "iterations_executed": len(ensemble_passes),
                    "final_candidate_rows": len(latest_candidate_rows),
                    "final_candidate_object_ids": list(final_candidate_object_ids),
                    "final_candidate_objects": len(final_candidate_object_ids),
                    "requested_excluded_object_ids": list(candidate_exclusion_object_ids),
                    "requested_excluded_objects": len(candidate_exclusion_object_ids),
                    "new_final_candidate_object_ids": list(new_final_candidate_object_ids),
                    "new_final_candidate_objects": len(new_final_candidate_object_ids),
                    "growth_suppressed": growth_suppressed,
                    "growth_suppressed_candidate_object_ids": (
                        list(new_final_candidate_object_ids)
                        if candidate_ensemble_growth_policy == "initial_only"
                        else []
                    ),
                    "growth_suppressed_candidate_objects": (
                        len(new_final_candidate_object_ids)
                        if candidate_ensemble_growth_policy == "initial_only"
                        else 0
                    ),
                    "converged": bool(converged),
                    "policy_satisfied": policy_satisfied,
                    "no_op_iteration_skipped": bool(
                        candidate_ensemble_terminal_reason == "no_usable_reference_exclusions"
                    ),
                    "stop_status": stop_status,
                    "stop_reason": stop_reason,
                }

            add_step("candidate_ensemble_convergence", _candidate_ensemble_convergence_step)

        if not stop_due_to_no_usable_frames and config.enable_color_calibration:
            _validate_color_calibration_config(config)

            def _color_calibration_step() -> dict[str, Any]:
                nonlocal current_ensemble_name, color_calibration_activated
                source_name = current_ensemble_name
                target_name = config.color_target_calibration_name or _color_calibration_target_name(
                    source_name,
                    reference_catalog_name=str(config.color_reference_catalog_name),
                    reference_band=str(config.color_reference_band),
                )
                diag_dir = None
                if config.out_dir:
                    diag_dir = Path(config.out_dir) / "color_calibration"
                color_result = apply_color_calibration(
                    session,
                    source_calibration_name=source_name,
                    target_calibration_name=target_name,
                    identity_catalog_name=config.color_identity_catalog_name or config.catalog_name,
                    reference_catalog_name=str(config.color_reference_catalog_name),
                    reference_band=str(config.color_reference_band),
                    color_index=str(config.color_index),
                    series_id=series_id,
                    out_dir=diag_dir,
                    min_measurements_per_object=config.color_min_measurements_per_object,
                    min_stars_per_frame=config.color_min_stars_per_frame,
                    max_measurement_mag_err=config.color_max_measurement_mag_err,
                    max_reference_mag_err=config.color_max_reference_mag_err,
                    min_reference_mag=config.color_min_reference_mag,
                    max_reference_mag=config.color_max_reference_mag,
                    min_color=config.color_min_value,
                    max_color=config.color_max_value,
                    clip_sigma=config.color_clip_sigma,
                    n_iterations=config.color_n_iterations,
                    min_calibrator_objects=config.color_min_calibrator_objects,
                    min_color_span=config.color_min_span,
                    max_transform_residual_mad=config.color_max_transform_residual_mad,
                    max_pivot_color_fraction=config.color_max_pivot_fraction,
                    max_shape_metric_delta=config.color_max_shape_metric_delta,
                    min_color_term_significance=config.color_min_term_significance,
                    min_color_model_bic_improvement=config.color_min_bic_improvement,
                    min_color_model_mad_improvement=config.color_min_mad_improvement,
                    dry_run=config.dry_run,
                    materialize_measurements=False,
                    immutable_generation=bool(config.record_run and not config.dry_run),
                    diagnose_performance=config.calibration_performance_diagnostics,
                    _preloaded_analysis_snapshot=_cached_analysis_snapshot_for(source_name),
                )
                payload = _result_to_dict(color_result)
                activated = bool(
                    not config.dry_run
                    and color_result.target_calibration_version_id is not None
                    and color_result.calibrated > 0
                )
                payload["activated_as_science_calibration"] = activated
                payload["source_science_calibration"] = source_name
                payload["target_science_calibration"] = target_name
                if color_result.target_calibration_version_id is not None:
                    _stamp_product(
                        target_name,
                        product_kind="COLOR_CALIBRATION",
                        product_role="SCIENCE" if activated else "INTERMEDIATE",
                        materialization_kind=_derived_materialization_kind(),
                        calibration_version_id=color_result.target_calibration_version_id,
                    )
                if activated:
                    color_calibration_activated = True
                    current_ensemble_name = target_name
                    calibration_names["ensemble_internal"] = source_name
                    calibration_names["color"] = target_name
                    calibration_names["science"] = target_name
                    outputs["ensemble_internal_calibration"] = source_name
                    outputs["color_calibration"] = target_name
                    outputs["science_calibration"] = target_name
                else:
                    calibration_names["science"] = source_name
                    outputs["science_calibration"] = source_name
                return payload

            add_step("apply_color_calibration", _color_calibration_step)

            if color_calibration_activated and not config.skip_object_qc:
                add_step(
                    "compute_object_qc_color_calibrated",
                    lambda: _compute_object_qc_for(current_ensemble_name),
                )

            if (
                color_calibration_activated
                and not config.skip_field_report
                and config.stage2_profile == "diagnostic"
            ):
                add_step("field_qc_report_color_calibrated", _field_report_step)

            if (
                color_calibration_activated
                and config.export_target_light_curve
                and target_resolution.get("resolved")
                and not config.exclude_candidates_from_ensemble
            ):
                def _target_light_curve_color_step() -> dict[str, Any]:
                    target = TargetResolution(**{k: v for k, v in target_resolution.items() if k in TargetResolution.__dataclass_fields__})
                    csv_path = None
                    json_path = None
                    if config.out_dir:
                        out_dir = Path(config.out_dir)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        csv_path = out_dir / f"{config.calibration_prefix}_target_light_curve.csv"
                        json_path = out_dir / f"{config.calibration_prefix}_target_light_curve.json"
                    product = export_target_light_curve_product(
                        session,
                        target=target,
                        calibration_name=current_ensemble_name,
                        catalog_name=config.catalog_name,
                        series_id=series_id,
                        frame_ids=_frame_ids_for_step(selected_frame_ids),
                        interseries_generation_ids=config.interseries_generation_ids,
                        reject_flags=_science_reject_flags(config, require_detection_support_effective),
                        out_csv=csv_path,
                        out_json=json_path,
                    )
                    payload = product.to_dict()
                    payload["calibration_name"] = current_ensemble_name
                    if product.csv_path:
                        outputs["target_light_curve_csv"] = product.csv_path
                    if product.json_path:
                        outputs["target_light_curve_json"] = product.json_path
                    return payload

                add_step("export_target_light_curve_color_calibrated", _target_light_curve_color_step)

        if (
            not stop_due_to_no_usable_frames
            and profile_settings["random_error_validation"]
        ):
            def _random_error_validation_step():
                validation_calibration = (
                    calibration_names.get("ensemble_internal") or current_ensemble_name
                )
                validation_out_dir = None
                if (
                    config.out_dir
                    and profile_settings.get("random_error_diagnostic_exports", False)
                ):
                    validation_out_dir = Path(config.out_dir) / "random_error_model"
                validation = validate_random_error_model(
                    session,
                    calibration_name=validation_calibration,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    min_measurements_per_object=config.object_qc_min_measurements,
                    out_dir=validation_out_dir,
                    residual_floor_bin_width=config.residual_floor_bin_width,
                    residual_floor_lower_quantile=config.residual_floor_lower_quantile,
                    residual_floor_min_objects_per_bin=config.residual_floor_min_objects_per_bin,
                    aperture_context_frames_csv=outputs.get("aperture_correction_context_frames_csv"),
                    diagnostic_calibration_names=[validation_calibration, current_ensemble_name],
                    analysis_snapshot=_analysis_snapshot_for(validation_calibration),
                    annotate_residual_floor=not pre_qc_exclusion_zero_write_dry_run,
                    include_legacy_comparison=profile_settings[
                        "random_error_legacy_comparison"
                    ],
                    include_pairwise_residual_correlations=profile_settings[
                        "random_error_pairwise_residual_correlations"
                    ],
                )
                if validation.summary_json:
                    outputs["random_error_validation_summary_json"] = (
                        validation.summary_json
                    )
                if validation.objects_csv:
                    outputs["random_error_validation_objects_csv"] = validation.objects_csv
                if validation.residual_floor_summary_json:
                    outputs["residual_noise_floor_summary_json"] = validation.residual_floor_summary_json
                if validation.residual_floor_objects_csv:
                    outputs["residual_noise_floor_objects_csv"] = validation.residual_floor_objects_csv
                if validation.residual_floor_frames_csv:
                    outputs["residual_noise_floor_frames_csv"] = validation.residual_floor_frames_csv
                return validation

            add_step("validate_random_error_model", _random_error_validation_step)

        if (
            not stop_due_to_no_usable_frames
            and config.export_target_light_curve
            and target_resolution.get("resolved")
            and config.exclude_candidates_from_ensemble
        ):
            def _target_light_curve_candidate_excluded_step() -> dict[str, Any]:
                target = TargetResolution(**{k: v for k, v in target_resolution.items() if k in TargetResolution.__dataclass_fields__})
                csv_path = None
                json_path = None
                if config.out_dir:
                    out_dir = Path(config.out_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    csv_path = out_dir / f"{config.calibration_prefix}_target_light_curve.csv"
                    json_path = out_dir / f"{config.calibration_prefix}_target_light_curve.json"
                product = export_target_light_curve_product(
                    session,
                    target=target,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    series_id=series_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    interseries_generation_ids=config.interseries_generation_ids,
                    reject_flags=_science_reject_flags(config, require_detection_support_effective),
                    out_csv=csv_path,
                    out_json=json_path,
                )
                payload = product.to_dict()
                payload["calibration_name"] = current_ensemble_name
                if product.csv_path:
                    outputs["target_light_curve_csv"] = product.csv_path
                if product.json_path:
                    outputs["target_light_curve_json"] = product.json_path
                return payload

            add_step("export_target_light_curve_candidate_excluded", _target_light_curve_candidate_excluded_step)

        if not stop_due_to_no_usable_frames and config.export_candidate_light_curves and not config.skip_field_report:
            def _candidate_light_curve_export_step() -> dict[str, Any]:
                selected_candidate_rows = _candidate_rows_for_science_export(
                    candidate_selection_rows or latest_candidate_rows,
                    latest_candidate_rows,
                    use_science_diagnostics=bool(color_calibration_activated),
                )
                if not selected_candidate_rows:
                    return {
                        "rows_total": 0,
                        "accepted_candidates": 0,
                        "exported_light_curves": 0,
                        "warnings": ["field report produced no candidate rows"],
                    }
                if not config.out_dir:
                    return {
                        "rows_total": len(selected_candidate_rows),
                        "accepted_candidates": 0,
                        "exported_light_curves": 0,
                        "warnings": ["candidate light-curve export skipped because out_dir is not set"],
                    }
                candidates_dir = Path(config.out_dir) / "candidates"
                product = export_accepted_candidate_light_curves(
                    session,
                    candidate_rows=selected_candidate_rows,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    series_id=series_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    interseries_generation_ids=config.interseries_generation_ids,
                    reject_flags=_science_reject_flags(config, require_detection_support_effective),
                    out_dir=candidates_dir,
                    limit=config.candidate_light_curve_limit,
                    export_plots=config.export_candidate_plots,
                    membership_history=candidate_membership_history,
                )
                payload = product.to_dict()
                payload["candidate_selection_calibration"] = (
                    calibration_names.get("ensemble_internal")
                    or calibration_names.get("ensemble")
                )
                payload["candidate_photometry_calibration"] = current_ensemble_name
                if product.candidates_dir:
                    outputs["accepted_candidate_light_curves_dir"] = product.candidates_dir
                if product.candidates_summary_csv:
                    outputs["accepted_candidates_summary_csv"] = product.candidates_summary_csv
                if product.candidates_summary_json:
                    outputs["accepted_candidates_summary_json"] = product.candidates_summary_json
                if product.candidates_index_html:
                    outputs["accepted_candidates_index_html"] = product.candidates_index_html
                if product.borderline_candidates_csv:
                    outputs["borderline_candidates_csv"] = product.borderline_candidates_csv
                if product.borderline_candidates_json:
                    outputs["borderline_candidates_json"] = product.borderline_candidates_json
                if product.near_threshold_candidates_csv:
                    outputs["near_threshold_candidates_csv"] = product.near_threshold_candidates_csv
                if product.near_threshold_candidates_json:
                    outputs["near_threshold_candidates_json"] = product.near_threshold_candidates_json
                if product.residual_floor_candidate_ab_csv:
                    outputs["residual_floor_candidate_ab_csv"] = product.residual_floor_candidate_ab_csv
                if product.residual_floor_candidate_ab_json:
                    outputs["residual_floor_candidate_ab_json"] = product.residual_floor_candidate_ab_json
                if product.target_diagnostics_csv:
                    outputs["target_diagnostics_csv"] = product.target_diagnostics_csv
                if product.target_diagnostics_json:
                    outputs["target_diagnostics_json"] = product.target_diagnostics_json
                if product.membership_history_json:
                    outputs["candidate_membership_history_json"] = product.membership_history_json
                outputs["candidate_plot_count"] = product.candidate_plot_count
                outputs["candidate_plot_warnings_count"] = product.candidate_plot_warnings_count
                outputs["candidate_plot_backend"] = product.plot_backend
                return payload

            add_step("export_candidate_light_curves", _candidate_light_curve_export_step)

        # Durable science publication is deliberately the final fallible science
        # step. Earlier pipeline versions materialized science_photometry before
        # candidate export; a later failure therefore left reconstructable rows
        # attached to INTERMEDIATE/SUPERSEDED products. Publish the SCIENCE role
        # and durable rows atomically only after every preceding science/export
        # step has succeeded.
        prior_failed = any(step.status == "FAILED" for step in result.steps)
        if (
            not prior_failed
            and not stop_due_to_no_usable_frames
            and not config.skip_ensemble
            and not config.dry_run
            and config.record_run
        ):
            def _science_materialization_step():
                science_name = calibration_names.get("science") or current_ensemble_name
                _stamp_product(science_name, product_role="SCIENCE")
                science_result = materialize_science_photometry(
                    session,
                    calibration_name=science_name,
                    frame_ids=(
                        _frame_ids_for_step(selected_frame_ids)
                        if series_id is not None or has_explicit_frame_selection
                        else None
                    ),
                    insert_batch_size=config.bulk_insert_batch_size,
                )
                outputs["science_photometry_calibration"] = science_name
                outputs["science_photometry_rows"] = science_result.science_rows_after
                outputs["science_photometry_pruned_non_science_rows"] = (
                    science_result.pruned_non_science_rows
                )
                outputs["science_photometry_status"] = science_result.status
                outputs["science_storage_mode"] = "DURABLE_NARROW"
                outputs["intermediate_measurement_materialization"] = "NORMALIZED_ONLY"
                return {
                    "status": science_result.status,
                    "science_storage_mode": outputs["science_storage_mode"],
                    "science_photometry": science_result.to_dict(),
                }

            add_step("materialize_science_photometry", _science_materialization_step)

        result.quality_policy = _quality_policy_summary(config, result)
        result.status = _pipeline_status_from_steps(
            result.steps,
            stop_due_to_no_usable_frames=stop_due_to_no_usable_frames,
        )

        _attach_pipeline_warning_summary(result)

    except Exception as exc:
        result.status = getattr(exc, "status", "FAILED")
        # Also recover failures raised outside add_step(), so logging the
        # original exception never degrades into PendingRollbackError.
        if not pre_qc_exclusion_zero_write_dry_run:
            session.rollback()
        if file_logger is not None:
            file_logger.exception(
                exc=exc,
                event_type="run_failed",
                message="field pipeline run failed",
                pipeline_run_id=pipeline_run_id,
            )
        if not pre_qc_exclusion_zero_write_dry_run:
            _insert_pipeline_log_event(
                session,
                pipeline_run_id=pipeline_run_id,
                pipeline_run_step_id=None,
                level="ERROR",
                event_type="run_failed",
                message=f"{type(exc).__name__}: {exc}",
                context_json={
                    "run_uid": run_uid,
                    **(getattr(exc, "diagnostics", {}) if isinstance(getattr(exc, "diagnostics", None), dict) else {}),
                },
            )
            try:
                _snapshot_run_artifacts()
            except Exception as artifact_exc:  # noqa: BLE001 - never mask the original pipeline failure.
                session.rollback()
                result.outputs["artifact_registry_status"] = "FAILED"
                result.outputs["artifact_registry_error"] = f"{type(artifact_exc).__name__}: {artifact_exc}"
            _finish_pipeline_run(session, pipeline_run_id, result.status, _compact_run_summary(result))
        if series_id is not None and pipeline_run_id is not None and not config.dry_run:
            mark_series_pipeline_result(
                session,
                series_id=int(series_id),
                pipeline_run_id=int(pipeline_run_id),
                pipeline_status=result.status,
                run_uid=run_uid,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        raise

    _emit_pipeline_log(
        session,
        file_logger,
        pipeline_run_id=pipeline_run_id,
        pipeline_run_step_id=None,
        step_name=None,
        level="INFO" if result.status == "DONE" else "WARNING",
        event_type="run_done",
        message=f"field pipeline run finished with status={result.status}",
        context=_compact_log_context(_run_summary(result)),
        write_db_event=not pre_qc_exclusion_zero_write_dry_run,
    )
    _snapshot_run_artifacts()
    _finish_pipeline_run(session, pipeline_run_id, result.status, _compact_run_summary(result))
    if series_id is not None and pipeline_run_id is not None and not config.dry_run:
        mark_series_pipeline_result(
            session,
            series_id=int(series_id),
            pipeline_run_id=int(pipeline_run_id),
            pipeline_status=result.status,
            run_uid=run_uid,
        )
    return result


def _make_run_uid(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"field:{prefix}:{stamp}"


def _fmt_float(value: float) -> str:
    text = f"{float(value):g}"
    return text.replace(".", "p")


def _photometry_estimator_tag(config: FieldPipelineConfig) -> str:
    estimator = str(config.photometry_estimator or "circular").strip().lower()
    if estimator == "circular":
        return ""
    if estimator == "adaptive_ellipse":
        f = _fmt_float(config.adaptive_aperture_fwhm_factor)
        q = _fmt_float(config.adaptive_max_axis_ratio)
        rs = _fmt_float(config.adaptive_max_radius_scale)
        return f"_adaptive_ellipse_f{f}_q{q}_rs{rs}"
    if estimator == "spatial_psf":
        beta = _fmt_float(config.psf_moffat_beta)
        rf = _fmt_float(config.psf_fit_radius_fwhm)
        cr = _fmt_float(config.psf_centroid_search_radius_pix)
        return f"_spatial_psf_{config.psf_profile}_b{beta}_rf{rf}_c{cr}"
    if estimator == "empirical_psf":
        g = int(config.empirical_psf_grid_size)
        sr = int(config.empirical_psf_stamp_radius_pix)
        rr = _fmt_float(config.empirical_psf_reference_radius_pix)
        n = int(config.empirical_psf_references_per_node)
        m = int(config.empirical_psf_min_references_per_node)
        return f"_empirical_psf_g{g}_sr{sr}_rr{rr}_n{n}_m{m}"
    raise ValueError(f"Unsupported photometry_estimator={config.photometry_estimator!r}")


def _science_reject_flags(
    config: FieldPipelineConfig,
    require_detection_support: bool,
) -> set[str]:
    return light_curve_reject_flags(
        require_detection_support=require_detection_support,
        extra_flags=(BAD_FRAME_REJECT_FLAG if config.enable_photometric_frame_qc else None),
    )


def _calibration_names(config: FieldPipelineConfig) -> dict[str, str]:
    r = _fmt_float(config.aperture_radius_pix)
    r_ref = _fmt_float(config.aperture_correction_reference_radius_pix)
    a1 = _fmt_float(config.annulus_inner_pix)
    a2 = _fmt_float(config.annulus_outer_pix)
    work_tag = _working_catalog_tag(config)
    error_tag = _random_error_model_tag(config.random_error_model)
    estimator_tag = _photometry_estimator_tag(config)
    raw_estimator_tag = "_aperture" if not estimator_tag else estimator_tag
    aperture_tag = (
        f"_{error_tag}"
        if config.skip_aperture_correction
        else f"_{error_tag}_apcorr_r{r}_to_r{r_ref}_sv2"
    )

    raw_suffix = f"_raw{raw_estimator_tag}_r{r}_ann{a1}_{a2}_{error_tag}"
    apcorr_suffix = f"{estimator_tag}_{error_tag}_apcorr_r{r}_to_r{r_ref}_spatial_v2"
    aperture_zp_suffix = f"{estimator_tag}{aperture_tag}_aperture_{config.mag_key.lower()}_zp_v1"
    ensemble_base_suffix = f"{estimator_tag}{aperture_tag}_ensemble_{config.mag_key.lower()}_v1"
    ensemble_suffix = (
        f"{estimator_tag}{aperture_tag}_ensemble_{config.mag_key.lower()}_spatial_v3"
        if config.enable_spatial_ensemble
        else (
            f"{estimator_tag}{aperture_tag}_ensemble_{config.mag_key.lower()}_photqc_v3"
            if config.enable_photometric_frame_qc
            else ensemble_base_suffix
        )
    )

    raw_name = config.raw_calibration_name or compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{raw_suffix}",
        semantic_suffix=raw_suffix,
    )
    aperture_corrected_name = compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{apcorr_suffix}",
        semantic_suffix=apcorr_suffix,
    )
    aperture_zp_name = config.aperture_zp_calibration_name or compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{aperture_zp_suffix}",
        semantic_suffix=aperture_zp_suffix,
    )
    ensemble_base_name = compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{ensemble_base_suffix}",
        semantic_suffix=ensemble_base_suffix,
    )
    ensemble_name = config.ensemble_calibration_name or compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{ensemble_suffix}",
        semantic_suffix=ensemble_suffix,
    )
    if not config.enable_spatial_ensemble and not config.enable_photometric_frame_qc:
        ensemble_base_name = ensemble_name
    ensemble_candidate_excluded_name = _candidate_excluded_calibration_name(
        config,
        iteration=2,
        base_ensemble_name=ensemble_name,
    )
    return {
        "raw": raw_name,
        "aperture_corrected": aperture_corrected_name,
        "aperture_zp": aperture_zp_name,
        "ensemble_base": ensemble_base_name,
        "ensemble": ensemble_name,
        "science": ensemble_name,
        "ensemble_candidates_excluded": ensemble_candidate_excluded_name,
    }


def _aperture_correction_fallback_names(config: FieldPipelineConfig) -> dict[str, str]:
    """Return distinct downstream names when the aperture quality gate falls back to raw."""

    work_tag = _working_catalog_tag(config)
    estimator_tag = _photometry_estimator_tag(config)
    fallback_tag = f"{estimator_tag}_{_random_error_model_tag(config.random_error_model)}_apcorr_fallback_raw"
    aperture_zp_suffix = f"{fallback_tag}_aperture_{config.mag_key.lower()}_zp_v1"
    ensemble_base_suffix = f"{fallback_tag}_ensemble_{config.mag_key.lower()}_v1"
    ensemble_suffix = (
        f"{fallback_tag}_ensemble_{config.mag_key.lower()}_spatial_v3"
        if config.enable_spatial_ensemble
        else (
            f"{fallback_tag}_ensemble_{config.mag_key.lower()}_photqc_v3"
            if config.enable_photometric_frame_qc
            else ensemble_base_suffix
        )
    )
    aperture_zp = config.aperture_zp_calibration_name or compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{aperture_zp_suffix}",
        semantic_suffix=aperture_zp_suffix,
    )
    ensemble_base = compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{ensemble_base_suffix}",
        semantic_suffix=ensemble_base_suffix,
    )
    ensemble = config.ensemble_calibration_name or compact_generated_calibration_name(
        f"{config.calibration_prefix}{work_tag}{ensemble_suffix}",
        semantic_suffix=ensemble_suffix,
    )
    if not config.enable_spatial_ensemble and not config.enable_photometric_frame_qc:
        ensemble_base = ensemble
    return {
        "aperture_zp": aperture_zp,
        "ensemble_base": ensemble_base,
        "ensemble": ensemble,
        "science": ensemble,
        "ensemble_candidates_excluded": _candidate_excluded_calibration_name(
            config,
            iteration=2,
            base_ensemble_name=ensemble,
        ),
    }


def _random_error_model_tag(model: str) -> str:
    if model == PHYSICAL_APERTURE_ERROR_MODEL:
        return "errphys_v1"
    if model == LEGACY_APERTURE_ERROR_MODEL:
        return "errlegacy_v1"
    raise ValueError(f"Unsupported aperture random-error model: {model!r}")


def _validate_color_calibration_config(config: FieldPipelineConfig) -> None:
    missing = []
    if not config.color_reference_catalog_name:
        missing.append("color_reference_catalog_name")
    if not config.color_reference_band:
        missing.append("color_reference_band")
    if not config.color_index:
        missing.append("color_index")
    if missing:
        raise ValueError(
            "Color calibration is enabled but required settings are missing: " + ", ".join(missing)
        )
    if config.color_min_measurements_per_object < 1:
        raise ValueError("color_min_measurements_per_object must be >= 1")
    if config.color_min_stars_per_frame < 1:
        raise ValueError("color_min_stars_per_frame must be >= 1")
    if config.color_clip_sigma <= 0:
        raise ValueError("color_clip_sigma must be positive")
    if config.color_n_iterations < 1:
        raise ValueError("color_n_iterations must be >= 1")
    if config.color_min_calibrator_objects < 3:
        raise ValueError("color_min_calibrator_objects must be >= 3")
    if config.color_min_span < 0:
        raise ValueError("color_min_span must be non-negative")
    if config.color_max_shape_metric_delta < 0:
        raise ValueError("color_max_shape_metric_delta must be non-negative")
    if config.color_min_term_significance < 0:
        raise ValueError("color_min_term_significance must be non-negative")
    if config.color_min_bic_improvement < 0:
        raise ValueError("color_min_bic_improvement must be non-negative")


def _color_calibration_target_name(
    source_calibration_name: str,
    *,
    reference_catalog_name: str,
    reference_band: str,
) -> str:
    provider_name = reference_catalog_name.split("__", 1)[0]
    catalog_token = (re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").lower() or "reference")[:32]
    band_token = (re.sub(r"[^A-Za-z0-9]+", "_", reference_band).strip("_").lower() or "band")[:16]
    full_name = f"{source_calibration_name}_{catalog_token}_{band_token}_color_v3"
    if len(full_name) <= 128:
        return full_name
    digest = hashlib.sha256(full_name.encode("utf-8")).hexdigest()[:10]
    suffix = f"_{catalog_token}_{band_token}_color_{digest}_v3"
    prefix_length = max(1, 128 - len(suffix))
    prefix = source_calibration_name[:prefix_length].rstrip("_") or "calibration"
    return f"{prefix}{suffix}"


def _candidate_excluded_calibration_name(
    config: FieldPipelineConfig,
    *,
    iteration: int,
    base_ensemble_name: str | None = None,
) -> str:
    """Return a deterministic DB-safe name for a candidate-exclusion pass."""

    if base_ensemble_name:
        base_name = str(base_ensemble_name)
    elif config.ensemble_calibration_name:
        base_name = str(config.ensemble_calibration_name)
    else:
        work_tag = _working_catalog_tag(config)
        r = _fmt_float(config.aperture_radius_pix)
        r_ref = _fmt_float(config.aperture_correction_reference_radius_pix)
        aperture_tag = "" if config.skip_aperture_correction else f"_apcorr_r{r}_to_r{r_ref}"
        base_name = f"{config.calibration_prefix}{work_tag}{aperture_tag}_ensemble_{config.mag_key.lower()}_v1"
    return candidate_excluded_calibration_name(base_name, iteration=iteration)


def _working_catalog_tag(config: FieldPipelineConfig) -> str:
    parts: list[str] = []
    if config.working_catalog_mag_limit is not None:
        parts.append(f"g{_fmt_float(float(config.working_catalog_mag_limit))}")
    if config.max_working_sources_per_frame is not None:
        parts.append(f"max{int(config.max_working_sources_per_frame)}")
    if config.catalog_edge_margin_pix and config.catalog_edge_margin_pix > 0:
        parts.append(f"edge{_fmt_float(float(config.catalog_edge_margin_pix))}")
    return "_work_" + "_".join(parts) if parts else ""


def _insert_pipeline_run(
    session: Session,
    *,
    run_uid: str,
    instrument_unit_id: int | None,
    field_id: int | None,
    series_id: int | None,
    catalog_name: str,
    calibration_prefix: str,
    recipe_json: dict[str, Any],
) -> int:
    try:
        run_id = session.execute(
            text(
                """
                INSERT INTO pipeline_runs
                    (run_uid, instrument_unit_id, field_id, series_id, catalog_name, calibration_prefix, recipe_json, status, summary_json, started_at, updated_at)
                VALUES
                    (:run_uid, :instrument_unit_id, :field_id, :series_id, :catalog_name, :calibration_prefix, CAST(:recipe_json AS JSONB), 'RUNNING', CAST(:summary_json AS JSONB), now(), now())
                RETURNING pipeline_run_id
                """
            ),
            {
                "run_uid": run_uid,
                "instrument_unit_id": instrument_unit_id,
                "field_id": field_id,
                "series_id": series_id,
                "catalog_name": catalog_name,
                "calibration_prefix": calibration_prefix,
                "recipe_json": json.dumps(recipe_json, ensure_ascii=False),
                "summary_json": json.dumps({}, ensure_ascii=False),
            },
        ).scalar_one()
        session.commit()
        return int(run_id)
    except ProgrammingError as exc:
        session.rollback()
        raise RuntimeError(
            "pipeline_runs table is not available or lacks fields/series columns. Apply scripts/migrate_mvp5_21_pipeline_runs.sql and scripts/migrate_mvp5_26_fields_series.sql first, "
            "or run with --no-record."
        ) from exc
    except SQLAlchemyError:
        session.rollback()
        raise


def _update_pipeline_run_log_paths(session: Session, pipeline_run_id: int | None, log_files: dict[str, str]) -> None:
    if pipeline_run_id is None or not log_files:
        return
    try:
        session.execute(
            text(
                """
                UPDATE pipeline_runs
                SET log_dir = :log_dir,
                    log_file = :log_file,
                    jsonl_log_file = :jsonl_log_file,
                    error_log_file = :error_log_file,
                    command_json_file = :command_json_file,
                    environment_json_file = :environment_json_file,
                    updated_at = now()
                WHERE pipeline_run_id = :pipeline_run_id
                """
            ),
            {
                "pipeline_run_id": pipeline_run_id,
                "log_dir": log_files.get("log_dir"),
                "log_file": log_files.get("log_file"),
                "jsonl_log_file": log_files.get("jsonl_log_file"),
                "error_log_file": log_files.get("error_log_file"),
                "command_json_file": log_files.get("command_json_file"),
                "environment_json_file": log_files.get("environment_json_file"),
            },
        )
        session.commit()
    except ProgrammingError as exc:
        session.rollback()
        raise RuntimeError(
            "pipeline logging columns are not available. Apply scripts/migrate_mvp5_25_pipeline_logging.sql first, "
            "or run with --no-file-log."
        ) from exc


def _emit_pipeline_log(
    session: Session,
    file_logger: PipelineFileLogger | None,
    *,
    pipeline_run_id: int | None,
    pipeline_run_step_id: int | None,
    step_name: str | None,
    level: str,
    event_type: str,
    message: str,
    context: dict[str, Any] | None = None,
    write_db_event: bool = True,
) -> None:
    if file_logger is not None:
        file_logger.log(
            level=level,
            event_type=event_type,
            message=message,
            pipeline_run_id=pipeline_run_id,
            pipeline_run_step_id=pipeline_run_step_id,
            step_name=step_name,
            context=_compact_log_context(context or {}),
        )
    if write_db_event:
        _insert_pipeline_log_event(
            session,
            pipeline_run_id=pipeline_run_id,
            pipeline_run_step_id=pipeline_run_step_id,
            level=level,
            event_type=event_type,
            message=message,
            context_json=_compact_log_context(context or {}),
        )


def _insert_pipeline_log_event(
    session: Session,
    *,
    pipeline_run_id: int | None,
    pipeline_run_step_id: int | None,
    level: str,
    event_type: str,
    message: str,
    context_json: dict[str, Any] | None = None,
) -> None:
    if pipeline_run_id is None:
        return
    safe_message = sanitize_diagnostic_text(message)
    safe_context = sanitize_jsonable_context(_to_jsonable(context_json or {}))
    try:
        session.execute(
            text(
                """
                INSERT INTO pipeline_log_events
                    (pipeline_run_id, pipeline_run_step_id, level, event_type, message, context_json, created_at)
                VALUES
                    (:pipeline_run_id, :pipeline_run_step_id, :level, :event_type, :message, CAST(:context_json AS JSONB), now())
                """
            ),
            {
                "pipeline_run_id": pipeline_run_id,
                "pipeline_run_step_id": pipeline_run_step_id,
                "level": level,
                "event_type": event_type,
                "message": safe_message,
                "context_json": json.dumps(safe_context, ensure_ascii=False),
            },
        )
        session.commit()
    except ProgrammingError as exc:
        session.rollback()
        raise RuntimeError(
            "pipeline_log_events table is not available. Apply scripts/migrate_mvp5_25_pipeline_logging.sql first, "
            "or run with --no-record."
        ) from exc


def _insert_pipeline_step(session: Session, pipeline_run_id: int | None, step_name: str, order_index: int) -> int | None:
    if pipeline_run_id is None:
        return None
    step_id = session.execute(
        text(
            """
            INSERT INTO pipeline_run_steps
                (pipeline_run_id, order_index, step_name, status, result_json, started_at, updated_at)
            VALUES
                (:pipeline_run_id, :order_index, :step_name, 'RUNNING', CAST(:result_json AS JSONB), now(), now())
            RETURNING pipeline_run_step_id
            """
        ),
        {
            "pipeline_run_id": pipeline_run_id,
            "order_index": order_index,
            "step_name": step_name,
            "result_json": json.dumps({}, ensure_ascii=False),
        },
    ).scalar_one()
    session.commit()
    return int(step_id)


def _finish_pipeline_step(
    session: Session,
    step_id: int | None,
    status: str,
    result_json: dict[str, Any],
    error_message: str | None,
) -> None:
    if step_id is None:
        return
    session.execute(
        text(
            """
            UPDATE pipeline_run_steps
            SET status = :status,
                finished_at = now(),
                updated_at = now(),
                result_json = CAST(:result_json AS JSONB),
                error_message = :error_message
            WHERE pipeline_run_step_id = :step_id
            """
        ),
        {
            "step_id": step_id,
            "status": status,
            "result_json": json.dumps(_to_jsonable(result_json), ensure_ascii=False),
            "error_message": error_message,
        },
    )
    session.commit()


def _finish_pipeline_run(
    session: Session,
    pipeline_run_id: int | None,
    status: str,
    summary_json: dict[str, Any],
) -> None:
    if pipeline_run_id is None:
        return
    session.execute(
        text(
            """
            UPDATE pipeline_runs
            SET status = :status,
                finished_at = now(),
                updated_at = now(),
                summary_json = CAST(:summary_json AS JSONB)
            WHERE pipeline_run_id = :pipeline_run_id
            """
        ),
        {
            "pipeline_run_id": pipeline_run_id,
            "status": status,
            "summary_json": json.dumps(_to_jsonable(summary_json), ensure_ascii=False),
        },
    )
    session.commit()


def _result_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return _to_jsonable(value)
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    return _to_jsonable({"value": str(value)})


def _candidate_origin_parts(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            raw.extend(_candidate_origin_parts(item))
        return raw
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _apply_good_frame_candidate_confirmation(
    selection_rows: list[dict[str, Any]],
    good_frame_rows: list[dict[str, Any]],
    *,
    n_good_frames: int,
    min_good_frames: int,
) -> list[dict[str, Any]]:
    """Confirm accepted candidates using the existing evaluator on GOOD epochs only.

    No new variability statistic is introduced.  An all-usable-epoch ACCEPTED row
    remains ACCEPTED only when the same object is ACCEPTED by the ordinary field-QC
    evaluator restricted to GOOD frames.  Otherwise it is demoted to BORDERLINE so
    it is retained diagnostically but cannot drive automatic candidate acceptance or
    candidate-reference exclusion.
    """

    good_by_object: dict[int, dict[str, Any]] = {}
    for row in good_frame_rows:
        object_id = row.get("object_id")
        if object_id is None:
            continue
        try:
            good_by_object[int(object_id)] = row
        except Exception:
            continue

    merged: list[dict[str, Any]] = []
    for original in selection_rows:
        row = dict(original)
        status = str(row.get("candidate_status") or "").upper()
        row["all_usable_candidate_status"] = original.get("candidate_status")
        row["all_usable_candidate_origin"] = original.get("candidate_origin")
        row["good_frame_count"] = int(n_good_frames)
        row["good_frame_min_required"] = int(min_good_frames)
        if status != "ACCEPTED":
            row["good_frame_confirmation_status"] = "NOT_REQUIRED"
            merged.append(row)
            continue
        if row.get("is_target") or "target" in _candidate_origin_parts(row.get("candidate_origin")):
            row["good_frame_confirmation_status"] = "TARGET_PRESERVED"
            merged.append(row)
            continue
        if int(n_good_frames) < int(min_good_frames):
            row["candidate_status"] = "BORDERLINE"
            row["candidate_diagnostic_class"] = "INSUFFICIENT_GOOD_FRAME_SUPPORT"
            row["good_frame_confirmation_status"] = "INSUFFICIENT_GOOD_FRAME_SUPPORT"
            merged.append(row)
            continue
        object_id = row.get("object_id")
        try:
            object_id_int = int(object_id) if object_id is not None else None
        except Exception:
            object_id_int = None
        good_row = good_by_object.get(object_id_int) if object_id_int is not None else None
        good_status = str((good_row or {}).get("candidate_status") or "").upper()
        row["good_frame_candidate_status"] = (good_row or {}).get("candidate_status")
        row["good_frame_candidate_origin"] = (good_row or {}).get("candidate_origin")
        for field_name in (
            "n_measurements", "n_points", "n_accepted", "n_rejected",
            "mag_median", "mag_rms", "mag_mad", "robust_peak_to_peak",
            "mag_err_median", "baseline_rms", "rms_excess",
            "rms_over_mag_err", "mad_over_mag_err", "intrinsic_variability_score",
            "candidate_diagnostic_class", "rejection_reasons",
        ):
            row[f"good_frame_{field_name}"] = (good_row or {}).get(field_name)
        if good_status == "ACCEPTED":
            row["good_frame_confirmation_status"] = "GOOD_FRAME_CONFIRMED"
        else:
            row["candidate_status"] = "BORDERLINE"
            row["candidate_diagnostic_class"] = "SYSTEMATICS_SENSITIVE"
            row["good_frame_confirmation_status"] = "SYSTEMATICS_SENSITIVE"
        merged.append(row)
    return merged


def _good_frame_confirmation_summary(
    rows: list[dict[str, Any]],
    n_good_frames: int,
    min_good_frames: int,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    accepted = 0
    for row in rows:
        key = str(row.get("good_frame_confirmation_status") or "UNSET")
        counts[key] = counts.get(key, 0) + 1
        if str(row.get("candidate_status") or "").upper() == "ACCEPTED":
            accepted += 1
    return {
        "enabled": True,
        "policy": "existing_candidate_evaluator_on_good_frames_v1",
        "n_good_frames": int(n_good_frames),
        "min_good_frames": int(min_good_frames),
        "status_counts": counts,
        "accepted_candidates_after_confirmation": int(accepted),
    }


def _candidate_object_ids_for_ensemble_exclusion(
    candidate_rows: list[dict[str, Any]],
    *,
    target_object_ids: list[int] | None = None,
) -> list[int]:
    ids = {int(v) for v in (target_object_ids or []) if v is not None}
    for row in candidate_rows:
        if str(row.get("candidate_status") or "").upper() != "ACCEPTED":
            continue
        obj_id = row.get("object_id")
        if obj_id is None:
            continue
        try:
            ids.add(int(obj_id))
        except Exception:
            continue
    return sorted(ids)


def _target_provenance_status(config: FieldPipelineConfig) -> str:
    if config.target_object_id is not None:
        return "EXPLICIT_OBJECT_ID"
    if config.target_source_id:
        return "EXPLICIT_SOURCE_ID"
    if config.target_ra_deg is not None or config.target_dec_deg is not None:
        return "EXPLICIT_OBJECT_COORDINATES"
    if config.target_max_sep_arcsec is not None and float(config.target_max_sep_arcsec) < 0:
        return "DISABLED_UNRESOLVED"
    return "UNRESOLVED_NO_TRUSTED_TARGET_INPUT"


def _unresolved_explicit_target_resolution(
    config: FieldPipelineConfig,
    catalog_selection: dict[str, Any] | None,
) -> TargetResolution | None:
    """Gate ambiguous catalog selectors while preserving opaque-object fallback."""

    has_explicit_selector = bool(
        config.target_source_id
        or config.target_object_id is not None
        or config.target_ra_deg is not None
        or config.target_dec_deg is not None
    )
    # An opaque Object ID is resolved against measured Objects by the normal
    # post-measurement resolver. A missing catalog link only prevents using it
    # to expand the pre-photometry CatalogSource working set.
    if config.target_object_id is not None and not config.target_source_id:
        return None
    if not has_explicit_selector or catalog_selection is None or catalog_selection.get("eligible"):
        return None
    return TargetResolution(
        resolved=False,
        object_id=config.target_object_id,
        source_id=config.target_source_id,
        catalog_name=config.catalog_name,
        target_ra_deg=config.target_ra_deg,
        target_dec_deg=config.target_dec_deg,
        coordinate_provenance=(
            "explicit_object_coordinates" if config.target_ra_deg is not None else None
        ),
        method="explicit_catalog_source",
        reason=catalog_selection.get("reason")
        or "explicit target did not map to one unique catalog source",
    )


def _target_resolution_provenance_status(
    config: FieldPipelineConfig, target_result: dict[str, Any]
) -> str:
    return _target_provenance_status(config)


def _detection_branch_enabled(config: FieldPipelineConfig) -> bool:
    return not config.skip_detection and not config.skip_detection_catalog_match


def _eligible_catalog_sources_for_forced_photometry(
    support_counts: dict[int, int], explicit_target: dict[str, Any]
) -> list[int]:
    """Combine positive detection support with a uniquely selected explicit source."""

    eligible = {int(source_id) for source_id, count in support_counts.items() if int(count) > 0}
    source_id = explicit_target.get("catalog_source_id")
    if explicit_target.get("eligible") and source_id is not None:
        eligible.add(int(source_id))
    return sorted(eligible)


def _effective_require_detection_support(config: FieldPipelineConfig) -> bool:
    if config.require_detection_support is not None:
        return bool(config.require_detection_support)
    return _detection_branch_enabled(config)


def _forced_photometry_failure_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("failed_frames", "failed_measurements"):
        try:
            counts[key] = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            counts[key] = 0
    return counts


def _projection_limited_link_count(payload: dict[str, Any]) -> int:
    try:
        return max(0, int(payload.get("limited_links") or 0))
    except (TypeError, ValueError):
        return 0


def _completed_step_status(step_name: str, payload: dict[str, Any]) -> str:
    if step_name == "match_catalog_to_frames":
        return (
            "DONE_WITH_WARNINGS"
            if _projection_limited_link_count(payload)
            else "DONE"
        )
    if step_name != "run_forced_photometry":
        return "DONE"
    counts = _forced_photometry_failure_counts(payload)
    cap_dropped_links = max(0, int(payload.get("cap_dropped_links") or 0))
    return (
        "DONE_WITH_WARNINGS"
        if counts["failed_frames"] or counts["failed_measurements"] or cap_dropped_links
        else "DONE"
    )


def _completed_step_log(
    step_name: str, status: str, payload: dict[str, Any]
) -> tuple[str, str, str]:
    if status != "DONE_WITH_WARNINGS":
        return "INFO", "step_done", f"step {step_name} finished"
    if step_name == "match_catalog_to_frames":
        projection_limited_links = _projection_limited_link_count(payload)
        return (
            "WARNING",
            "step_done_with_warnings",
            "frame-catalog projection applied the per-frame link cap: "
            f"projection_limited_links={projection_limited_links}",
        )
    counts = _forced_photometry_failure_counts(payload)
    cap_dropped_links = max(0, int(payload.get("cap_dropped_links") or 0))
    if cap_dropped_links and not counts["failed_frames"] and not counts["failed_measurements"]:
        return (
            "WARNING",
            "step_done_with_warnings",
            "working-source cap applied after detection-support eligibility: "
            f"eligible_links_before_cap={payload.get('eligible_links_before_cap', 0)}, "
            f"selected_links_after_cap={payload.get('selected_links_after_cap', 0)}, "
            f"cap_dropped_links={cap_dropped_links}",
        )
    return (
        "WARNING",
        "step_done_with_warnings",
        (
            f"step {step_name} finished with failures: "
            f"failed_frames={counts['failed_frames']}, "
            f"failed_measurements={counts['failed_measurements']}"
        ),
    )


def _pipeline_status_from_steps(
    steps: list[PipelineStepRecord], *, stop_due_to_no_usable_frames: bool
) -> str:
    if any(step.status == "FAILED" for step in steps):
        return "FAILED"
    if stop_due_to_no_usable_frames:
        return NO_USABLE_FRAMES_STATUS
    if any(step.status == "DONE_WITH_WARNINGS" for step in steps):
        return "DONE_WITH_WARNINGS"
    return "DONE"


def _pipeline_warning_summary(steps: list[PipelineStepRecord]) -> dict[str, Any] | None:
    warning_steps = [step for step in steps if step.status == "DONE_WITH_WARNINGS"]
    if not warning_steps:
        return None

    sample_limit = 20
    failure_samples: list[dict[str, Any]] = []
    failure_samples_truncated = 0
    warning_step_rows: list[dict[str, Any]] = []
    total_failed_frames = 0
    total_failed_measurements = 0
    total_cap_dropped_links = 0
    total_projection_limited_links = 0
    for step in warning_steps:
        counts = _forced_photometry_failure_counts(step.result)
        total_failed_frames += counts["failed_frames"]
        total_failed_measurements += counts["failed_measurements"]
        cap_dropped_links = max(0, int(step.result.get("cap_dropped_links") or 0))
        total_cap_dropped_links += cap_dropped_links
        projection_limited_links = _projection_limited_link_count(step.result)
        total_projection_limited_links += projection_limited_links
        raw_samples = step.result.get("failure_samples") or []
        samples = [item for item in raw_samples if isinstance(item, dict)]
        remaining = max(0, sample_limit - len(failure_samples))
        failure_samples.extend(samples[:remaining])
        failure_samples_truncated += max(
            0, int(step.result.get("failure_samples_truncated") or 0)
        )
        failure_samples_truncated += max(0, len(samples) - remaining)
        warning_step_rows.append(
            {
                "step_name": step.step_name,
                "status": step.status,
                "failed_frames": counts["failed_frames"],
                "failed_measurements": counts["failed_measurements"],
                "cap_dropped_links": cap_dropped_links,
                "projection_limited_links": projection_limited_links,
                "failure_samples_count": len(samples),
            }
        )

    return {
        "requested_pipeline_status": "DONE_WITH_WARNINGS",
        "warning_step_count": len(warning_steps),
        "warning_steps": warning_step_rows,
        "failed_frames": total_failed_frames,
        "failed_measurements": total_failed_measurements,
        "cap_dropped_links": total_cap_dropped_links,
        "projection_limited_links": total_projection_limited_links,
        "failure_samples": failure_samples,
        "failure_samples_truncated": failure_samples_truncated,
    }


def _attach_pipeline_warning_summary(result: PipelineRunResult) -> dict[str, Any] | None:
    summary = _pipeline_warning_summary(result.steps)
    if summary is None:
        return None
    summary["pipeline_run_status"] = result.status
    summary["pipeline_status_rollup_applied"] = result.status == "DONE_WITH_WARNINGS"
    result.quality_policy["pipeline_warning_summary"] = summary
    return summary


def _step_result(result: PipelineRunResult, step_name: str) -> dict[str, Any] | None:
    for step in result.steps:
        if step.step_name == step_name and step.status in {"DONE", "DONE_WITH_WARNINGS"}:
            return step.result
    return None


def _step_results_with_prefix(result: PipelineRunResult, step_name_prefix: str) -> list[dict[str, Any]]:
    return [
        step.result
        for step in result.steps
        if step.status in {"DONE", "DONE_WITH_WARNINGS"}
        and step.step_name.startswith(step_name_prefix)
    ]


def _effective_reference_exclusion_ids(
    requested_object_ids: list[int] | set[int],
    current_reference_object_ids: set[int] | None,
) -> list[int] | None:
    """Return requested objects that can actually change the current ensemble.

    ``None`` means the current reference set is unavailable and the caller must
    preserve the legacy behaviour. An empty list means the precheck is
    authoritative and a candidate-excluded recalibration would be a no-op.
    """

    if current_reference_object_ids is None:
        return None
    requested = {int(object_id) for object_id in requested_object_ids}
    return sorted(requested & {int(object_id) for object_id in current_reference_object_ids})


def _candidate_ensemble_exclusion_history(result: PipelineRunResult) -> list[dict[str, Any]]:
    selection_by_iteration: dict[int, dict[str, Any]] = {}
    ensemble_by_iteration: dict[int, dict[str, Any]] = {}
    for step in result.steps:
        if step.status != "DONE":
            continue
        if step.step_name.startswith("select_candidate_reference_exclusions"):
            iteration = int(step.result.get("iteration") or 2)
            selection_by_iteration[iteration] = step.result
        elif step.step_name.startswith("ensemble_calibrate_candidate_excluded"):
            iteration = int(step.result.get("candidate_ensemble_iteration") or 2)
            ensemble_by_iteration[iteration] = step.result

    history: list[dict[str, Any]] = []
    for iteration in sorted(set(selection_by_iteration) | set(ensemble_by_iteration)):
        selection = selection_by_iteration.get(iteration, {})
        ensemble = ensemble_by_iteration.get(iteration, {})
        requested_ids = list(
            selection.get("requested_excluded_object_ids")
            or ensemble.get("requested_excluded_object_ids")
            or []
        )
        actual_ids = list(
            ensemble.get("actually_excluded_reference_object_ids")
            or ensemble.get("excluded_reference_object_ids")
            or []
        )
        new_requested_ids = list(selection.get("new_requested_excluded_object_ids") or [])
        suppressed_ids = list(selection.get("suppressed_new_candidate_object_ids") or [])
        history.append(
            {
                "iteration": iteration,
                "growth_policy": selection.get("growth_policy") or ensemble.get("candidate_ensemble_growth_policy"),
                "will_run_iteration": bool(selection.get("will_run_iteration")),
                "executed": bool(ensemble),
                "input_ensemble_calibration": selection.get("input_ensemble_calibration"),
                "output_ensemble_calibration": ensemble.get("target_calibration_name"),
                "new_requested_excluded_object_ids": new_requested_ids,
                "new_requested_excluded_objects": len(new_requested_ids),
                "suppressed_new_candidate_object_ids": suppressed_ids,
                "suppressed_new_candidate_objects": len(suppressed_ids),
                "requested_excluded_object_ids": requested_ids,
                "requested_excluded_objects": len(requested_ids),
                "actually_excluded_reference_object_ids": actual_ids,
                "actually_excluded_reference_objects": len(actual_ids),
                "reference_candidates_after_exclusion": ensemble.get("reference_candidates"),
                "usable_reference_objects": ensemble.get("usable_reference_objects"),
                "stop_reason": selection.get("stop_reason"),
            }
        )
    return history


def _quality_policy_summary(config: FieldPipelineConfig, result: PipelineRunResult) -> dict[str, Any]:
    match_result = _step_result(result, "match_detections_to_catalog") or {}
    target_result = _step_result(result, "resolve_target_object") or {}
    target_resolved = bool(target_result.get("resolved"))
    exclusion_history = _candidate_ensemble_exclusion_history(result)
    convergence_result = _step_result(result, "candidate_ensemble_convergence") or {}
    color_result = _step_result(result, "apply_color_calibration") or {}
    forced_result = _step_result(result, "run_forced_photometry") or {}
    detection_support_result = _step_result(result, "aggregate_detection_support") or {
        "status": "NOT_RUN"
    }
    error_validation_result = _step_result(result, "validate_random_error_model") or {}
    aperture_result = _step_result(result, "apply_aperture_correction") or {}
    candidate_export_result = _step_result(result, "export_candidate_light_curves") or {}
    ensemble_step_results = _step_results_with_prefix(result, "ensemble_calibrate")
    final_ensemble_result = ensemble_step_results[-1] if ensemble_step_results else {}
    effective_stage2_settings = _stage2_profile_settings_for_config(config)
    validation_step = next(
        (
            step
            for step in result.steps
            if step.step_name == "validate_random_error_model"
        ),
        None,
    )
    if validation_step is None:
        validation_status = (
            "SKIPPED_NO_USABLE_FRAMES"
            if _step_result(result, "stop_no_usable_frames") is not None
            else effective_stage2_settings["random_error_validation_status"]
        )
    elif validation_step.status == "FAILED":
        validation_status = "FAILED"
    else:
        validation_status = error_validation_result.get("status") or validation_step.status
    aperture_requested = not bool(config.skip_aperture_correction)
    aperture_activated = bool(aperture_result.get("quality_gate_passed"))
    aperture_effective_calibration = (
        aperture_result.get("target_calibration_name")
        if aperture_activated
        else result.calibration_names.get("raw")
    )
    exclusion_summary = summarize_candidate_ensemble_exclusions(
        exclusion_history,
        convergence_result,
    )
    requested_exclusion_ids = exclusion_summary["requested_excluded_object_ids"]
    actual_exclusion_ids = exclusion_summary["actually_excluded_reference_object_ids"]
    actual_exclusion_iteration = exclusion_summary["actual_exclusions_iteration"]
    iteration_limit = (
        max(2, int(config.candidate_ensemble_convergence_max_iterations or 10))
        if config.candidate_ensemble_until_converged
        else max(1, int(config.candidate_ensemble_max_iterations or 1))
    )
    return {
        "stage2_profile": config.stage2_profile,
        "effective_stage2_settings": effective_stage2_settings,
        "random_error_model": forced_result.get("random_error_model") or config.random_error_model,
        "random_error_physical_model": PHYSICAL_APERTURE_ERROR_MODEL,
        "random_error_legacy_comparison_model": LEGACY_APERTURE_ERROR_MODEL,
        "random_error_legacy_comparison_enabled": effective_stage2_settings[
            "random_error_legacy_comparison"
        ],
        "random_error_gain_e_per_adu_median": forced_result.get("gain_e_per_adu_median"),
        "random_error_read_noise_e_median": forced_result.get("read_noise_e_median"),
        "random_error_measurements_with_selected_error": forced_result.get(
            "measurements_with_selected_error"
        ),
        "random_error_measurements_gain_fallback": forced_result.get(
            "measurements_gain_fallback"
        ),
        "random_error_measurements_read_noise_fallback": forced_result.get(
            "measurements_read_noise_fallback"
        ),
        "random_error_measurements_background_empirical": forced_result.get(
            "measurements_background_empirical"
        ),
        "random_error_measurements_background_detector_prediction": forced_result.get(
            "measurements_background_detector_prediction"
        ),
        "random_error_measurements_scintillation_available": forced_result.get(
            "measurements_scintillation_available"
        ),
        "random_error_frames_gain_fallback": forced_result.get("frames_gain_fallback"),
        "random_error_frames_read_noise_fallback": forced_result.get(
            "frames_read_noise_fallback"
        ),
        "random_error_frames_scintillation_available": forced_result.get(
            "frames_scintillation_available"
        ),
        "random_error_selected_mag_err_median": forced_result.get(
            "selected_mag_err_median"
        ),
        "random_error_physical_mag_err_median": forced_result.get(
            "physical_mag_err_median"
        ),
        "random_error_legacy_mag_err_median": forced_result.get(
            "legacy_mag_err_median"
        ),
        "random_error_physical_to_legacy_ratio_median": forced_result.get(
            "physical_to_legacy_mag_err_ratio_median"
        ),
        "random_error_physical_to_legacy_ratio_p10": forced_result.get(
            "physical_to_legacy_mag_err_ratio_p10"
        ),
        "random_error_physical_to_legacy_ratio_p90": forced_result.get(
            "physical_to_legacy_mag_err_ratio_p90"
        ),
        "random_error_source_poisson_fraction_median": forced_result.get(
            "source_poisson_fraction_median"
        ),
        "random_error_aperture_background_fraction_median": forced_result.get(
            "aperture_background_fraction_median"
        ),
        "random_error_background_estimate_fraction_median": forced_result.get(
            "background_estimate_fraction_median"
        ),
        "random_error_background_estimator_variance_factor": (
            config.background_estimator_variance_factor
        ),
        "random_error_empirical_background_floor": config.empirical_background_floor,
        "random_error_fractional_flux_floor": forced_result.get(
            "fractional_flux_floor"
        ),
        "random_error_scintillation_enabled": forced_result.get(
            "scintillation_enabled"
        ),
        "random_error_telescope_aperture_m": forced_result.get(
            "telescope_aperture_m"
        ),
        "random_error_site_altitude_m": forced_result.get("site_altitude_m"),
        "random_error_scintillation_fractional_sigma_median": forced_result.get(
            "scintillation_fractional_sigma_median"
        ),
        "random_error_fractional_floor_fraction_median": forced_result.get(
            "fractional_floor_fraction_median"
        ),
        "random_error_scintillation_fraction_median": forced_result.get(
            "scintillation_fraction_median"
        ),
        "random_error_model_summary_json": forced_result.get("diagnostics_summary_json"),
        "random_error_model_frames_csv": forced_result.get("diagnostics_frames_csv"),
        "random_error_validation_status": validation_status,
        "random_error_validation_calibration": error_validation_result.get(
            "calibration_name"
        ),
        "random_error_validation_reference_objects": error_validation_result.get(
            "n_reference_objects"
        ),
        "random_error_validation_measurements": error_validation_result.get(
            "n_measurements"
        ),
        "random_error_validation_degrees_of_freedom": error_validation_result.get(
            "degrees_of_freedom"
        ),
        "random_error_physical_normalized_robust_sigma": error_validation_result.get(
            "physical_normalized_robust_sigma"
        ),
        "random_error_legacy_normalized_robust_sigma": error_validation_result.get(
            "legacy_normalized_robust_sigma"
        ),
        "random_error_physical_reduced_chi2": error_validation_result.get(
            "physical_reduced_chi2"
        ),
        "random_error_legacy_reduced_chi2": error_validation_result.get(
            "legacy_reduced_chi2"
        ),
        "random_error_physical_error_scale_recommendation": error_validation_result.get(
            "physical_error_scale_recommendation"
        ),
        "random_error_legacy_error_scale_recommendation": error_validation_result.get(
            "legacy_error_scale_recommendation"
        ),
        "random_error_physical_closer_to_unity": error_validation_result.get(
            "physical_closer_to_unity"
        ),
        "random_error_validation_summary_json": error_validation_result.get(
            "summary_json"
        ),
        "random_error_validation_objects_csv": error_validation_result.get(
            "objects_csv"
        ),
        "residual_noise_floor_status": error_validation_result.get("residual_floor_status"),
        "residual_noise_floor_algorithm": error_validation_result.get("residual_floor_algorithm"),
        "residual_noise_floor_error_basis": error_validation_result.get("residual_floor_error_basis"),
        "residual_noise_floor_suggested_mag": error_validation_result.get("residual_floor_suggested_mag"),
        "residual_noise_floor_after_frame_common_mag": error_validation_result.get("residual_floor_after_frame_common_mag"),
        "residual_noise_floor_independent_mag": error_validation_result.get("residual_floor_independent_mag"),
        "residual_noise_floor_frame_common_sigma_mag": error_validation_result.get("residual_floor_frame_common_sigma_mag"),
        "residual_noise_floor_frame_common_supported": error_validation_result.get("residual_floor_frame_common_supported"),
        "residual_noise_floor_frame_common_support_status": error_validation_result.get("residual_floor_frame_common_support_status"),
        "residual_noise_floor_frame_common_support_reason": error_validation_result.get("residual_floor_frame_common_support_reason"),
        "residual_noise_floor_frame_common_min_floor_reduction_fraction": error_validation_result.get("residual_floor_frame_common_min_floor_reduction_fraction"),
        "residual_noise_floor_frame_common_min_abs_correlation_reduction": error_validation_result.get("residual_floor_frame_common_min_abs_correlation_reduction"),
        "residual_noise_floor_frame_common_floor_reduction_mag": error_validation_result.get("residual_floor_frame_common_floor_reduction_mag"),
        "residual_noise_floor_frame_common_floor_reduction_fraction": error_validation_result.get("residual_floor_frame_common_floor_reduction_fraction"),
        "residual_noise_floor_frame_common_abs_correlation_reduction": error_validation_result.get("residual_floor_frame_common_abs_correlation_reduction"),
        "residual_noise_floor_calibrator_objects": error_validation_result.get("residual_floor_calibrator_objects"),
        "residual_noise_floor_frames_with_common_residual": error_validation_result.get("residual_floor_frames_with_common_residual"),
        "residual_noise_floor_effective_detector_only_normalized_robust_sigma": error_validation_result.get("residual_floor_effective_detector_only_normalized_robust_sigma"),
        "residual_noise_floor_effective_detector_only_reduced_chi2": error_validation_result.get("residual_floor_effective_detector_only_reduced_chi2"),
        "residual_noise_floor_effective_total_normalized_robust_sigma": error_validation_result.get("residual_floor_effective_total_normalized_robust_sigma"),
        "residual_noise_floor_effective_total_reduced_chi2": error_validation_result.get("residual_floor_effective_total_reduced_chi2"),
        "residual_noise_floor_effective_normalized_robust_sigma": error_validation_result.get("residual_floor_effective_normalized_robust_sigma"),
        "residual_noise_floor_effective_reduced_chi2": error_validation_result.get("residual_floor_effective_reduced_chi2"),
        "residual_noise_floor_pairwise_abs_correlation_median": error_validation_result.get("residual_floor_pairwise_abs_correlation_median"),
        "residual_noise_floor_pairwise_abs_correlation_after_common_median": error_validation_result.get("residual_floor_pairwise_abs_correlation_after_common_median"),
        "residual_noise_floor_annotation_measurements": error_validation_result.get("residual_floor_annotation_measurements"),
        "residual_noise_floor_annotation_calibrations": error_validation_result.get("residual_floor_annotation_calibrations"),
        "residual_noise_floor_summary_json": error_validation_result.get("residual_floor_summary_json"),
        "residual_noise_floor_objects_csv": error_validation_result.get("residual_floor_objects_csv"),
        "residual_noise_floor_frames_csv": error_validation_result.get("residual_floor_frames_csv"),
        "detection_branch_enabled": _detection_branch_enabled(config),
        "psf_field_diagnostics_enabled": config.enable_psf_field_diagnostics,
        "psf_field_diagnostics_algorithm": "second_moments_spatial_v1",
        "detection_matching_executed": bool(match_result),
        "detection_matches_count": int(match_result.get("matched") or 0),
        "detection_support_selection": detection_support_result,
        "forced_photometry_selection": (
            {
                **detection_support_result,
                "eligible_links_before_cap": int(
                    forced_result.get("eligible_links_before_cap") or 0
                ),
                "selected_links_after_cap": int(
                    forced_result.get("selected_links_after_cap") or 0
                ),
                "cap_dropped_links": int(forced_result.get("cap_dropped_links") or 0),
                "source_cap_per_frame": config.max_working_sources_per_frame,
            }
            if _step_result(result, "run_forced_photometry") is not None
            else None
        ),
        "require_detection_support_requested": config.require_detection_support,
        "require_detection_support_effective": _effective_require_detection_support(config),
        "target_source_id": config.target_source_id,
        "target_object_id_requested": config.target_object_id,
        "target_ra_deg": config.target_ra_deg,
        "target_dec_deg": config.target_dec_deg,
        "target_requested_ra_deg": config.target_ra_deg,
        "target_requested_dec_deg": config.target_dec_deg,
        "target_requested_provenance_status": _target_provenance_status(config),
        "target_provenance_status": _target_resolution_provenance_status(
            config, target_result
        ),
        "target_resolved": target_resolved,
        "target_effective_ra_deg": (
            target_result.get("target_ra_deg") if target_resolved else None
        ),
        "target_effective_dec_deg": (
            target_result.get("target_dec_deg") if target_resolved else None
        ),
        "target_resolution_ra_deg": target_result.get("target_ra_deg"),
        "target_resolution_dec_deg": target_result.get("target_dec_deg"),
        "target_object_id": target_result.get("object_id") if target_resolved else None,
        "target_catalog_source_id": target_result.get("catalog_source_id") if target_resolved else None,
        "target_sep_arcsec": target_result.get("sep_arcsec") if target_resolved else None,
        "target_resolution_method": target_result.get("method"),
        "target_coordinate_provenance": target_result.get("coordinate_provenance"),
        "target_resolution_reason": target_result.get("reason"),
        "nearest_target_candidate_object_id": target_result.get("nearest_object_id"),
        "nearest_target_candidate_source_id": target_result.get("nearest_source_id"),
        "nearest_target_candidate_catalog_source_id": target_result.get("nearest_catalog_source_id"),
        "nearest_target_candidate_sep_arcsec": target_result.get("nearest_sep_arcsec"),
        "photometry_estimator": config.photometry_estimator,
        "ensemble_min_ref_stars_per_frame": int(config.ensemble_min_ref_stars_per_frame),
        "spatial_ensemble_enabled": bool(config.enable_spatial_ensemble),
        "spatial_ensemble_algorithm": (
            final_ensemble_result.get("model_name")
            or ("robust_zero_mean_spatial_residual_v1" if config.enable_spatial_ensemble else None)
        ),
        "spatial_ensemble_quality_gate_passed": final_ensemble_result.get("quality_gate_passed"),
        "spatial_ensemble_global_identity_fallback": final_ensemble_result.get("global_identity_fallback"),
        "spatial_ensemble_frames_with_plane": final_ensemble_result.get("frames_with_plane"),
        "spatial_ensemble_frames_with_quadratic": final_ensemble_result.get("frames_with_quadratic"),
        "spatial_ensemble_frames_with_identity": final_ensemble_result.get("frames_with_identity"),
        "spatial_ensemble_median_residual_mad_before": final_ensemble_result.get("median_residual_mad_before"),
        "spatial_ensemble_median_residual_mad_after": final_ensemble_result.get("median_residual_mad_after"),
        "photometric_frame_qc_enabled": bool(config.enable_photometric_frame_qc),
        "photometric_frame_qc_algorithm": (
            final_ensemble_result.get("algorithm")
            or (PHOTOMETRIC_FRAME_QC_ALGORITHM if config.enable_photometric_frame_qc else None)
        ),
        "photometric_frame_qc_series_status": final_ensemble_result.get("series_status"),
        "photometric_frame_qc_good_frames": final_ensemble_result.get("good_frames"),
        "photometric_frame_qc_suspect_frames": final_ensemble_result.get("suspect_frames"),
        "photometric_frame_qc_bad_frames": final_ensemble_result.get("bad_frames"),
        "photometric_frame_qc_median_residual_robust_sigma_mag": final_ensemble_result.get("median_residual_robust_sigma_mag"),
        "photometric_frame_qc_suspect_residual_limit_mag": final_ensemble_result.get("suspect_residual_limit_mag"),
        "photometric_frame_qc_bad_residual_limit_mag": final_ensemble_result.get("bad_residual_limit_mag"),
        "photometric_frame_qc_absolute_bad_residual_limit_mag": final_ensemble_result.get("absolute_bad_residual_limit_mag"),
        "photometric_frame_qc_median_sigma_systematic_mag": final_ensemble_result.get("median_sigma_systematic_mag"),
        "photometric_frame_qc_max_sigma_systematic_mag": final_ensemble_result.get("max_sigma_systematic_mag"),
        "photometric_frame_qc_median_sigma_frame_systematic_mag": final_ensemble_result.get("median_sigma_frame_systematic_mag"),
        "photometric_frame_qc_max_sigma_frame_systematic_mag": final_ensemble_result.get("max_sigma_frame_systematic_mag"),
        "photometric_frame_qc_rejection_policy": (
            "hard_failures_only_v2" if config.enable_photometric_frame_qc else None
        ),
        "photometric_frame_qc_error_policy": (
            "diagnostic_only_no_error_inflation_v3"
            if config.enable_photometric_frame_qc else None
        ),
        "photometric_frame_qc_sigma_frame_semantics": (
            "diagnostic_only_not_a_per_star_uncertainty_v3"
            if config.enable_photometric_frame_qc else None
        ),
        "photometric_frame_qc_frames_with_spatial_systematic": final_ensemble_result.get("frames_with_spatial_systematic"),
        "photometric_frame_qc_frame_psf_summaries": final_ensemble_result.get("frame_psf_summaries"),
        "candidate_good_frame_confirmation": (
            (_step_results_with_prefix(result, "field_qc_report")[-1].get("candidate_good_frame_confirmation")
             if _step_results_with_prefix(result, "field_qc_report") else None)
            if config.enable_photometric_frame_qc else None
        ),
        "candidate_axis_correlation": (
            (_step_results_with_prefix(result, "field_qc_report")[-1].get("candidate_axis_correlation")
             if _step_results_with_prefix(result, "field_qc_report") else None)
            if config.enable_photometric_frame_qc else None
        ),
        "adaptive_aperture_fwhm_factor": config.adaptive_aperture_fwhm_factor,
        "adaptive_max_axis_ratio": config.adaptive_max_axis_ratio,
        "adaptive_max_radius_scale": config.adaptive_max_radius_scale,
        "psf_profile": config.psf_profile,
        "psf_moffat_beta": config.psf_moffat_beta,
        "psf_fit_radius_fwhm": config.psf_fit_radius_fwhm,
        "psf_centroid_search_radius_pix": config.psf_centroid_search_radius_pix,
        "empirical_psf_grid_size": config.empirical_psf_grid_size,
        "empirical_psf_stamp_radius_pix": config.empirical_psf_stamp_radius_pix,
        "empirical_psf_reference_radius_pix": config.empirical_psf_reference_radius_pix,
        "empirical_psf_references_per_node": config.empirical_psf_references_per_node,
        "empirical_psf_min_references_per_node": config.empirical_psf_min_references_per_node,
        "aperture_correction_requested": aperture_requested,
        "aperture_correction_activated": aperture_activated,
        "aperture_correction_source_calibration": aperture_result.get("source_calibration_name"),
        "aperture_correction_target_calibration": aperture_result.get("target_calibration_name"),
        "aperture_correction_effective_calibration": aperture_effective_calibration,
        "aperture_correction_reference_radius_pix": aperture_result.get("reference_radius_pix"),
        "aperture_correction_good_frame_fraction": aperture_result.get("good_frame_fraction"),
        "aperture_correction_median_residual_mad": aperture_result.get("median_residual_mad"),
        "aperture_correction_median_spatial_peak_to_peak": aperture_result.get(
            "median_spatial_peak_to_peak"
        ),
        "aperture_correction_median_plane_spatial_peak_to_peak": aperture_result.get(
            "median_plane_spatial_peak_to_peak"
        ),
        "aperture_correction_median_plane_max_abs_spatial_deviation": aperture_result.get(
            "median_plane_max_abs_spatial_deviation"
        ),
        "aperture_correction_legacy_frames_with_plane": aperture_result.get(
            "legacy_frames_with_plane"
        ),
        "aperture_correction_legacy_plane_frame_ids": aperture_result.get(
            "legacy_plane_frame_ids"
        ),
        "aperture_correction_frames_promoted_to_plane_vs_legacy": aperture_result.get(
            "frames_promoted_to_plane_vs_legacy"
        ),
        "aperture_correction_promoted_to_plane_frame_ids": aperture_result.get(
            "promoted_to_plane_frame_ids"
        ),
        "aperture_correction_frames_demoted_from_plane_vs_legacy": aperture_result.get(
            "frames_demoted_from_plane_vs_legacy"
        ),
        "aperture_correction_demoted_from_plane_frame_ids": aperture_result.get(
            "demoted_from_plane_frame_ids"
        ),
        "aperture_correction_legacy_max_abs_correction": (
            config.aperture_correction_max_abs_correction
        ),
        "aperture_correction_max_abs_spatial_deviation": (
            config.aperture_correction_max_abs_spatial_deviation
        ),
        "aperture_correction_max_abs_total_correction": (
            config.aperture_correction_max_abs_total_correction
        ),
        "aperture_correction_model_selection_semantics": (
            "separated_spatial_total_safety_v2" if aperture_result else None
        ),
        "aperture_correction_error_propagation": (
            "differential_spatial_only_v1" if aperture_result else None
        ),
        "aperture_correction_frames_with_plane": aperture_result.get("frames_with_plane"),
        "aperture_correction_plane_frame_ids": aperture_result.get("plane_frame_ids"),
        "aperture_correction_frames_with_constant": aperture_result.get("frames_with_constant"),
        "aperture_correction_frames_identity_fallback": aperture_result.get(
            "frames_identity_fallback"
        ),
        "aperture_correction_quality_gate_failures": aperture_result.get(
            "quality_gate_failures"
        ),
        "aperture_context_analysis_status": aperture_result.get("context_analysis_status"),
        "aperture_context_frames_with_psf_shape": aperture_result.get(
            "context_frames_with_psf_shape"
        ),
        "aperture_context_plane_frames_with_psf_shape": aperture_result.get(
            "context_plane_frames_with_psf_shape"
        ),
        "aperture_context_frames_with_header_fwhm": aperture_result.get(
            "context_frames_with_header_fwhm"
        ),
        "aperture_context_plane_gradient_mean_angle_deg": aperture_result.get(
            "context_plane_gradient_mean_angle_deg"
        ),
        "aperture_context_plane_gradient_resultant_length": aperture_result.get(
            "context_plane_gradient_resultant_length"
        ),
        "aperture_context_plane_gradient_sign_consistency": aperture_result.get(
            "context_plane_gradient_sign_consistency"
        ),
        "aperture_context_plane_gradient_psf_axis_delta_median_deg": aperture_result.get(
            "context_plane_gradient_psf_axis_delta_median_deg"
        ),
        "aperture_context_plane_time_span_minutes": aperture_result.get(
            "context_plane_time_span_minutes"
        ),
        "aperture_context_plane_candidate_frames": aperture_result.get(
            "context_plane_candidate_frames"
        ),
        "aperture_context_plane_candidate_gradient_mean_angle_deg": aperture_result.get(
            "context_plane_candidate_gradient_mean_angle_deg"
        ),
        "aperture_context_plane_candidate_gradient_resultant_length": aperture_result.get(
            "context_plane_candidate_gradient_resultant_length"
        ),
        "aperture_context_plane_candidate_gradient_sign_consistency": aperture_result.get(
            "context_plane_candidate_gradient_sign_consistency"
        ),
        "candidate_borderline_diagnostics_enabled": True,
        "candidate_borderline_threshold_ratio": config.field_report_borderline_threshold_ratio,
        "candidate_diagnostic_max_failed_criteria": config.field_report_diagnostic_max_failed_criteria,
        "candidate_borderline_objects": int(candidate_export_result.get("borderline_candidates") or 0),
        "candidate_near_threshold_objects": int(candidate_export_result.get("near_threshold_candidates") or 0),
        "candidate_target_diagnostic_objects": int(candidate_export_result.get("target_diagnostic_candidates") or 0),
        "candidate_borderline_candidates_csv": candidate_export_result.get("borderline_candidates_csv"),
        "candidate_borderline_candidates_json": candidate_export_result.get("borderline_candidates_json"),
        "candidate_near_threshold_candidates_csv": candidate_export_result.get("near_threshold_candidates_csv"),
        "candidate_near_threshold_candidates_json": candidate_export_result.get("near_threshold_candidates_json"),
        "candidate_residual_floor_ab_csv": candidate_export_result.get("residual_floor_candidate_ab_csv"),
        "candidate_residual_floor_ab_json": candidate_export_result.get("residual_floor_candidate_ab_json"),
        "candidate_membership_history_json": candidate_export_result.get("membership_history_json"),
        "candidate_ensemble_exclusion_enabled": bool(config.exclude_candidates_from_ensemble),
        "candidate_ensemble_iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
        "candidate_ensemble_until_converged": bool(config.candidate_ensemble_until_converged),
        "candidate_ensemble_max_iterations": max(1, int(config.candidate_ensemble_max_iterations or 1)),
        "candidate_ensemble_convergence_max_iterations": max(2, int(config.candidate_ensemble_convergence_max_iterations or 10)),
        "candidate_ensemble_iteration_limit": iteration_limit,
        "candidate_ensemble_growth_policy": normalize_candidate_ensemble_growth_policy(config.candidate_ensemble_growth_policy),
        "candidate_ensemble_exclusion_executed": bool(_step_results_with_prefix(result, "ensemble_calibrate_candidate_excluded")),
        "candidate_ensemble_exclusion_iterations_executed": len(_step_results_with_prefix(result, "ensemble_calibrate_candidate_excluded")),
        "candidate_ensemble_requested_excluded_object_ids": requested_exclusion_ids,
        "candidate_ensemble_requested_excluded_objects": len(requested_exclusion_ids),
        "candidate_ensemble_actual_excluded_reference_object_ids": actual_exclusion_ids,
        "candidate_ensemble_actual_excluded_reference_objects": len(actual_exclusion_ids),
        "candidate_ensemble_actual_exclusions_iteration": actual_exclusion_iteration,
        # Backwards-compatible aliases from 0.5.33.0. These are the requested
        # exclusions; the actual reference exclusions are recorded above.
        "candidate_ensemble_excluded_object_ids": requested_exclusion_ids,
        "candidate_ensemble_excluded_objects": len(requested_exclusion_ids),
        "candidate_ensemble_exclusion_history": exclusion_history,
        "candidate_ensemble_converged": convergence_result.get("converged"),
        "candidate_ensemble_policy_satisfied": convergence_result.get("policy_satisfied"),
        "candidate_ensemble_stop_status": convergence_result.get("stop_status"),
        "candidate_ensemble_stop_reason": convergence_result.get("stop_reason"),
        "candidate_ensemble_final_candidate_object_ids": list(convergence_result.get("final_candidate_object_ids") or []),
        "candidate_ensemble_final_candidate_objects": int(convergence_result.get("final_candidate_objects") or 0),
        "candidate_ensemble_new_final_candidate_object_ids": list(convergence_result.get("new_final_candidate_object_ids") or []),
        "candidate_ensemble_new_final_candidate_objects": int(convergence_result.get("new_final_candidate_objects") or 0),
        "candidate_ensemble_suppressed_new_candidate_object_ids": list(
            convergence_result.get("growth_suppressed_candidate_object_ids") or []
        ),
        "candidate_ensemble_suppressed_new_candidate_objects": int(
            convergence_result.get("growth_suppressed_candidate_objects") or 0
        ),
        "candidate_ensemble_convergence_diagnostics": convergence_result or None,
        "initial_ensemble_calibration": result.outputs.get("ensemble_initial_calibration"),
        "final_ensemble_calibration": result.calibration_names.get("ensemble"),
        "color_calibration_requested": bool(
            config.color_calibration_requested or config.enable_color_calibration
        ),
        "color_calibration_enabled": bool(config.enable_color_calibration),
        "color_calibration_enabled_effective": bool(config.enable_color_calibration),
        "color_reference_ready": config.color_reference_ready,
        "color_calibration_skip_reason": config.color_calibration_skip_reason,
        "color_calibration_executed": bool(color_result),
        "color_calibration_activated": bool(color_result.get("activated_as_science_calibration")),
        "color_reference_catalog_name": config.color_reference_catalog_name,
        "color_reference_band": config.color_reference_band,
        "color_index": config.color_index,
        "color_term": color_result.get("color_term"),
        "color_term_err": color_result.get("color_term_err"),
        "color_fitted_term": color_result.get("fitted_color_term"),
        "color_fitted_term_err": color_result.get("fitted_color_term_err"),
        "color_term_significance": color_result.get("color_term_significance"),
        "color_transform_model": color_result.get("selected_transform_model"),
        "color_transform_model_selection_reason": color_result.get("model_selection_reason"),
        "color_transform_offset_only_bic": color_result.get("offset_only_bic"),
        "color_transform_color_model_bic": color_result.get("color_model_bic"),
        "color_transform_bic_improvement": color_result.get("color_model_bic_improvement"),
        "color_transform_mad_improvement": color_result.get("color_model_mad_improvement"),
        "color_transform_rms_improvement": color_result.get("color_model_rms_improvement"),
        "color_transform_offset": color_result.get("transform_offset"),
        "color_transform_offset_err": color_result.get("transform_offset_err"),
        "color_transform_residual_mad": color_result.get("transform_residual_mad"),
        "color_transform_residual_rms": color_result.get("transform_residual_rms"),
        "color_transform_quality_gate_passed": color_result.get("quality_gate_passed"),
        "color_transform_quality_gate_failures": color_result.get("quality_gate_failures"),
        "color_transform_pivot_fraction": color_result.get("pivot_color_fraction"),
        "color_transform_systematic_error_median": color_result.get("systematic_error_median"),
        "color_transform_max_abs_rms_delta": color_result.get("max_abs_rms_delta"),
        "color_transform_max_abs_mad_delta": color_result.get("max_abs_mad_delta"),
        "color_transform_max_abs_peak_to_peak_delta": color_result.get("max_abs_peak_to_peak_delta"),
        "color_calibration_used_objects": color_result.get("n_used_calibrator_objects"),
        "color_calibration_used_frames": color_result.get("n_frames_with_zp"),
        "color_calibration_source_frames": color_result.get("n_source_frames"),
        "color_calibration_frames_missing_zp": color_result.get("n_frames_missing_zp"),
        "color_calibrated_with_reference_color": color_result.get("calibrated_with_reference_color"),
        "color_calibrated_with_pivot_color": color_result.get("calibrated_with_pivot_color"),
        "color_rows_copied_without_calibrated_mag": color_result.get("copied_without_calibrated_mag"),
        "color_calibration_residual_rms": color_result.get("frame_model_residual_rms"),
        "candidate_selection_calibration": (
            result.calibration_names.get("ensemble_internal")
            or result.calibration_names.get("ensemble")
        ),
        "candidate_photometry_calibration": result.calibration_names.get("science"),
        "science_calibration": result.calibration_names.get("science") or result.calibration_names.get("ensemble"),
    }


def _candidate_rows_for_science_export(
    selection_rows: list[dict[str, Any]],
    science_rows: list[dict[str, Any]],
    *,
    use_science_diagnostics: bool,
) -> list[dict[str, Any]]:
    """Keep selection decisions while exposing science-scale diagnostics and membership changes."""

    if not use_science_diagnostics:
        return [dict(row) for row in selection_rows]
    science_by_object = {
        int(row["object_id"]): row
        for row in science_rows
        if row.get("object_id") is not None
    }
    science_fields = (
        "mag_median", "mag_rms", "mag_mad", "mag_p10", "mag_p90",
        "robust_peak_to_peak", "mag_err_median", "mag_err_systematic_median",
        "ref_mag", "color", "reference_catalog_name", "reference_band",
        "reference_residual_status", "mag_ref_delta", "abs_mag_ref_delta",
        *LOW_AMPLITUDE_DIAGNOSTIC_COLUMNS,
    )
    merged: list[dict[str, Any]] = []
    selection_ids: set[int] = set()
    for selection_row in selection_rows:
        row = dict(selection_row)
        object_id = row.get("object_id")
        object_id_int = int(object_id) if object_id is not None else None
        if object_id_int is not None:
            selection_ids.add(object_id_int)
        science_row = science_by_object.get(object_id_int) if object_id_int is not None else None
        row["selection_candidate_status"] = selection_row.get("candidate_status")
        row["selection_candidate_origin"] = selection_row.get("candidate_origin")
        row["science_candidate_status"] = science_row.get("candidate_status") if science_row else None
        row["science_candidate_origin"] = science_row.get("candidate_origin") if science_row else None
        if science_row is not None:
            for field_name in science_fields:
                row[field_name] = science_row.get(field_name)
            row["membership_change"] = _candidate_membership_change(selection_row, science_row)
        else:
            row["membership_change"] = "NOT_EVALUATED_ON_SCIENCE_CALIBRATION"
            row["ref_mag"] = None
            row["reference_catalog_name"] = None
            row["reference_band"] = None
            row["reference_residual_status"] = "NOT_EVALUATED_ON_SCIENCE_CALIBRATION"
            row["mag_ref_delta"] = None
            row["abs_mag_ref_delta"] = None
        merged.append(row)

    for object_id, science_row in science_by_object.items():
        if object_id in selection_ids:
            continue
        science_status = str(science_row.get("candidate_status") or "").upper()
        if science_status not in {"ACCEPTED", "BORDERLINE", "DIAGNOSTIC"} and not science_row.get("is_target"):
            continue
        row = dict(science_row)
        row["selection_candidate_status"] = None
        row["selection_candidate_origin"] = None
        row["science_candidate_status"] = science_row.get("candidate_status")
        row["science_candidate_origin"] = science_row.get("candidate_origin")
        row["membership_change"] = "ADDED_ON_SCIENCE_CALIBRATION"
        if science_status == "ACCEPTED":
            row["candidate_status"] = "BORDERLINE"
            row["candidate_diagnostic_class"] = "SCIENCE_ONLY_CANDIDATE"
        merged.append(row)
    return merged


def _candidate_membership_change(selection_row: dict[str, Any], science_row: dict[str, Any]) -> str:
    selection_status = str(selection_row.get("candidate_status") or "").upper()
    science_status = str(science_row.get("candidate_status") or "").upper()
    selection_origin = str(selection_row.get("candidate_origin") or "")
    science_origin = str(science_row.get("candidate_origin") or "")
    if selection_status == science_status and selection_origin == science_origin:
        return "UNCHANGED"
    if selection_status == "ACCEPTED" and science_status != "ACCEPTED":
        return "DROPPED_AFTER_SCIENCE_CALIBRATION"
    if selection_status != "ACCEPTED" and science_status == "ACCEPTED":
        return "PROMOTED_ON_SCIENCE_CALIBRATION"
    return "DIAGNOSTICS_CHANGED"


def _candidate_membership_snapshot(
    rows: list[dict[str, Any]],
    *,
    calibration_name: str,
    stage_index: int,
) -> dict[str, Any]:
    def ids_for(status: str) -> list[int]:
        return sorted(
            int(row["object_id"])
            for row in rows
            if row.get("object_id") is not None
            and str(row.get("candidate_status") or "").upper() == status
        )
    return {
        "stage_index": int(stage_index),
        "calibration_name": calibration_name,
        "accepted_object_ids": ids_for("ACCEPTED"),
        "borderline_object_ids": ids_for("BORDERLINE"),
        "diagnostic_object_ids": ids_for("DIAGNOSTIC"),
        "rejected_object_ids": ids_for("REJECTED"),
        "accepted_objects": len(ids_for("ACCEPTED")),
        "borderline_objects": len(ids_for("BORDERLINE")),
        "diagnostic_objects": len(ids_for("DIAGNOSTIC")),
        "rejected_objects": len(ids_for("REJECTED")),
    }


def _candidate_membership_transition(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    previous_accepted = {int(v) for v in previous.get("accepted_object_ids") or []}
    current_accepted = {int(v) for v in current.get("accepted_object_ids") or []}
    previous_borderline = {int(v) for v in previous.get("borderline_object_ids") or []}
    current_borderline = {int(v) for v in current.get("borderline_object_ids") or []}
    previous_diagnostic = {int(v) for v in previous.get("diagnostic_object_ids") or []}
    current_diagnostic = {int(v) for v in current.get("diagnostic_object_ids") or []}
    return {
        "accepted_added_object_ids": sorted(current_accepted - previous_accepted),
        "accepted_dropped_object_ids": sorted(previous_accepted - current_accepted),
        "borderline_added_object_ids": sorted(current_borderline - previous_borderline),
        "borderline_dropped_object_ids": sorted(previous_borderline - current_borderline),
        "accepted_to_borderline_object_ids": sorted(previous_accepted & current_borderline),
        "borderline_to_accepted_object_ids": sorted(previous_borderline & current_accepted),
        "diagnostic_added_object_ids": sorted(current_diagnostic - previous_diagnostic),
        "diagnostic_dropped_object_ids": sorted(previous_diagnostic - current_diagnostic),
        "accepted_to_diagnostic_object_ids": sorted(previous_accepted & current_diagnostic),
        "borderline_to_diagnostic_object_ids": sorted(previous_borderline & current_diagnostic),
        "diagnostic_to_borderline_object_ids": sorted(previous_diagnostic & current_borderline),
        "diagnostic_to_accepted_object_ids": sorted(previous_diagnostic & current_accepted),
    }


def _run_summary(result: PipelineRunResult) -> dict[str, Any]:
    return {
        "run_uid": result.run_uid,
        "pipeline_run_id": result.pipeline_run_id,
        "status": result.status,
        "calibration_names": result.calibration_names,
        "outputs": result.outputs,
        "log_files": result.log_files,
        "quality_policy": result.quality_policy,
        "steps": [asdict(step) for step in result.steps],
    }


def _compact_run_summary(result: PipelineRunResult) -> dict[str, Any]:
    """Return a DB/log-safe run summary without large diagnostic row payloads."""

    return {
        "run_uid": result.run_uid,
        "pipeline_run_id": result.pipeline_run_id,
        "status": result.status,
        "calibration_names": result.calibration_names,
        "outputs": result.outputs,
        "log_files": result.log_files,
        "quality_policy": result.quality_policy,
        "steps": [
            {
                "step_name": step.step_name,
                "status": step.status,
                "result": _compact_log_context(step.result),
                "error_message": step.error_message,
                "elapsed_sec": step.elapsed_sec,
            }
            for step in result.steps
        ],
    }


_LARGE_LOG_KEYS = {
    "rows",
    "candidate_rows",
    "blend_rows",
    "top_candidates",
    "top_blend_risk",
}
_SCALAR_PRESERVE_KEYS = {
    "accepted",
    "rejected",
    "raw_candidates",
    "selected_candidates",
    "checked_candidates",
    "objects_used",
    "total",
    "failed",
    "updated",
    "written",
    "calibrated",
    "matched",
    "measured",
    "inserted_or_updated",
}
_MAX_LOG_LIST_ITEMS = 8
_MAX_LOG_STRING_LENGTH = 1200


def _compact_log_context(value: Any) -> Any:
    """Make pipeline log contexts compact enough for JSONL and pipeline_log_events.

    Full diagnostic products should live in exported JSON/CSV artifacts and in the
    step-specific result tables where intentionally stored. Log events should carry
    counters, statuses, warning strings, and artifact paths, not candidate/object rows.
    """

    value = _to_jsonable(value)
    return _compact_jsonable(value)


def _compact_jsonable(value: Any, *, key: str | None = None) -> Any:
    if _is_scalar(value):
        if isinstance(value, str) and len(value) > _MAX_LOG_STRING_LENGTH:
            return value[:_MAX_LOG_STRING_LENGTH] + f"...<truncated {len(value) - _MAX_LOG_STRING_LENGTH} chars>"
        return value

    if key == "steps" and isinstance(value, list):
        return _compact_steps_context(value)

    if key in _LARGE_LOG_KEYS:
        if isinstance(value, list):
            return {"omitted": True, "count": len(value), "reason": "large_diagnostic_rows"}
        if isinstance(value, dict):
            return {"omitted": True, "keys": sorted(value.keys()), "reason": "large_diagnostic_rows"}

    # Keys such as accepted/rejected are scalar counters in candidate_triage, but
    # can also be diagnostic row containers in top_candidates. Preserve scalar
    # values and compact only their list/dict forms.
    if key in _SCALAR_PRESERVE_KEYS and isinstance(value, (list, dict)):
        if isinstance(value, list):
            return {"omitted": True, "count": len(value), "reason": "large_diagnostic_rows"}
        return {"omitted": True, "keys": sorted(value.keys()), "reason": "large_diagnostic_rows"}

    if isinstance(value, dict):
        return {str(k): _compact_jsonable(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _MAX_LOG_LIST_ITEMS:
            return {
                "omitted": True,
                "count": len(value),
                "sample": [_compact_jsonable(v) for v in value[: min(3, _MAX_LOG_LIST_ITEMS)]],
                "reason": "long_list",
            }
        return [_compact_jsonable(v) for v in value]
    return value


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _compact_steps_context(steps: list[Any]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    step_names: list[str] = []
    failed_steps: list[str] = []
    for item in steps:
        if isinstance(item, dict):
            name = str(item.get("step_name") or item.get("name") or "")
            status = str(item.get("status") or "UNKNOWN")
        else:
            name = str(getattr(item, "step_name", "") or "")
            status = str(getattr(item, "status", "UNKNOWN") or "UNKNOWN")
        if name:
            step_names.append(name)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "FAILED" and name:
            failed_steps.append(name)
    return {
        "count": len(steps),
        "done": status_counts.get("DONE", 0),
        "failed": status_counts.get("FAILED", 0),
        "skipped": status_counts.get("SKIPPED", 0),
        "running": status_counts.get("RUNNING", 0),
        "status_counts": status_counts,
        "step_names": step_names,
        "failed_steps": failed_steps,
    }


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)
