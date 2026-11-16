from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from astropy.io import fits
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from grandgousier.artifacts.registry import run_artifact_directory, snapshot_file_artifact
from grandgousier.archive.fields_series import build_field_series
from grandgousier.archive.observation_selection import (
    ScienceFileIdentityCandidate,
    plan_file_science_selection,
    select_persisted_science_frames,
)
from grandgousier.identity.observation import (
    ObservationIdentityError,
    ObservationIdentityMissing,
    build_frame_observation_identity,
)
from grandgousier.astrometry.gaia_wcs_com import GAIA_WCS_COM_POLICY
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.archive_indexer import IndexResult, index_archive_paths
from grandgousier.io.fits_header import header_to_jsonable
from grandgousier.io.frame_uid import make_frame_uid
from grandgousier.io.parsing import parse_datetime, to_float
from grandgousier.lightcurves.product import (
    build_light_curve_summary,
    export_light_curve_summary_json,
    light_curve_reject_flags,
)
from grandgousier.photometry.error_model import (
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
)
from grandgousier.resources import instrument_profile_path
from grandgousier.pipeline.runner import (
    FAILED_PRECONDITION_ENSEMBLE_STATUS,
    EnsemblePreconditionError,
    FieldPipelineConfig,
    PipelineRunResult,
    run_field_pipeline,
)
from grandgousier.calibration.photometric_policy import (
    ColorCalibrationPolicy,
    identity_scoped_reference_catalog_name,
    resolve_color_calibration_policy,
)
from grandgousier.catalogs import reference_photometry as reference_photometry_module
from grandgousier.catalogs.reference_photometry import (
    create_reference_photometry_csv,
    import_reference_photometry,
    load_persisted_identity_sources,
)


DEFAULT_EXTENSIONS: tuple[str, ...] = (".fits", ".fit", ".fts")
PROFILE_CONFIGS: dict[str, str] = {
    "MASTER": "master.yaml",
    "RoboPhot": "robophot.yaml",
}


@dataclass(frozen=True)
class DetectedArchiveFile:
    path: str
    profile_name: str
    instrument: str | None
    site: str | None
    tube: str | None
    camera: str | None
    filter_name: str | None
    object_name: str | None
    date_obs: Any | None
    mjd: float | None
    image_type: str | None
    frame_uid: str | None = None
    observation_uid: str | None = None
    identity_scheme: str | None = None
    native_identity: str | None = None
    file_size_bytes: int | None = None
    naxis1: int | None = None
    naxis2: int | None = None
    observation_identity_status: str = "UNSUPPORTED"
    observation_identity_detail: str | None = None


@dataclass
class ArchiveGroupPlan:
    profile_name: str
    profile_path: str
    instrument: str | None
    site: str | None
    tube: str | None
    camera: str | None
    filter_name: str | None
    object_name: str | None
    n_files: int
    date_min: Any | None = None
    date_max: Any | None = None
    mjd_min: float | None = None
    mjd_max: float | None = None
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.profile_name,
            "profile_path": self.profile_path,
            "instrument": self.instrument,
            "site": self.site,
            "tube": self.tube,
            "camera": self.camera,
            "filter_name": self.filter_name,
            "object_name": self.object_name,
            "n_files": self.n_files,
            "date_min": _json_safe(self.date_min),
            "date_max": _json_safe(self.date_max),
            "mjd_min": self.mjd_min,
            "mjd_max": self.mjd_max,
            "examples": self.examples,
        }


@dataclass
class ProcessArchiveResult:
    status: str
    path: str
    catalog_name: str
    execute: bool
    discovered_files: int
    detected_files: int
    failed_files: int
    indexed_frame_ids: list[int] = field(default_factory=list)
    created_series_ids: list[int] = field(default_factory=list)
    open_series_ids: list[int] = field(default_factory=list)
    processed_series_ids: list[int] = field(default_factory=list)
    groups: list[ArchiveGroupPlan] = field(default_factory=list)
    index_results: list[dict[str, Any]] = field(default_factory=list)
    catalog_results: list[dict[str, Any]] = field(default_factory=list)
    series_results: list[dict[str, Any]] = field(default_factory=list)
    pipeline_results: list[dict[str, Any]] = field(default_factory=list)
    light_curve_summaries: list[dict[str, Any]] = field(default_factory=list)
    reference_photometry_results: list[dict[str, Any]] = field(default_factory=list)
    observation_selection_results: list[dict[str, Any]] = field(default_factory=list)
    observation_selection_csv_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    manifest_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "catalog_name": self.catalog_name,
            "execute": self.execute,
            "discovered_files": self.discovered_files,
            "detected_files": self.detected_files,
            "failed_files": self.failed_files,
            "indexed_frame_ids": self.indexed_frame_ids,
            "created_series_ids": self.created_series_ids,
            "open_series_ids": self.open_series_ids,
            "processed_series_ids": self.processed_series_ids,
            "groups": [g.to_dict() for g in self.groups],
            "index_results": self.index_results,
            "catalog_results": self.catalog_results,
            "series_results": self.series_results,
            "pipeline_results": self.pipeline_results,
            "light_curve_summaries": self.light_curve_summaries,
            "reference_photometry_results": self.reference_photometry_results,
            "observation_selection_results": self.observation_selection_results,
            "observation_selection_csv_path": self.observation_selection_csv_path,
            "warnings": self.warnings,
            "manifest_path": self.manifest_path,
        }


@dataclass(frozen=True)
class DetectedArchiveIndexResult:
    index_results: tuple[dict[str, Any], ...]
    indexed_paths: tuple[str, ...]
    indexed_frame_ids: tuple[int, ...]
    failed_files: int


def default_profile_paths() -> dict[str, Path]:
    return {name: instrument_profile_path(filename) for name, filename in PROFILE_CONFIGS.items()}


def load_auto_profiles(
    *,
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
) -> dict[str, InstrumentProfile]:
    paths = default_profile_paths()
    if master_profile:
        paths["MASTER"] = Path(master_profile)
    if robophot_profile:
        paths["RoboPhot"] = Path(robophot_profile)
    return {name: InstrumentProfile.from_yaml(path) for name, path in paths.items()}


def index_detected_archive_files(
    session: Session,
    detected: Iterable[DetectedArchiveFile],
    *,
    profiles: dict[str, InstrumentProfile] | None = None,
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
) -> DetectedArchiveIndexResult:
    """Persist exactly the supplied detected FITS files through the canonical indexer."""

    active_profiles = profiles or load_auto_profiles(
        master_profile=master_profile,
        robophot_profile=robophot_profile,
    )
    detected_by_profile: dict[str, list[DetectedArchiveFile]] = {}
    for item in detected:
        detected_by_profile.setdefault(item.profile_name, []).append(item)

    index_results: list[dict[str, Any]] = []
    indexed_paths: list[str] = []
    failed_files = 0
    for profile_name, items in sorted(detected_by_profile.items()):
        paths = [Path(item.path) for item in items]
        result = index_archive_paths(
            session=session,
            paths=paths,
            profile=active_profiles[profile_name],
            dry_run=False,
            progress_desc=f"Indexing {profile_name}",
        )
        indexed_paths.extend(str(path) for path in paths)
        failed_files += int(result.failed)
        index_results.append(
            {
                "profile_name": profile_name,
                "discovered": result.discovered,
                "inserted": result.inserted,
                "skipped_existing": result.skipped_existing,
                "failed": result.failed,
            }
        )
        if result.failed:
            break

    indexed_frame_ids = (
        _frame_ids_for_paths(session, indexed_paths) if failed_files == 0 else []
    )
    return DetectedArchiveIndexResult(
        index_results=tuple(index_results),
        indexed_paths=tuple(indexed_paths),
        indexed_frame_ids=tuple(indexed_frame_ids),
        failed_files=failed_files,
    )


def discover_fits_files(
    root: str | Path,
    *,
    recursive: bool = True,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    limit: int | None = None,
) -> list[Path]:
    root_path = Path(root)
    suffixes = {e.lower() if str(e).startswith(".") else f".{str(e).lower()}" for e in extensions}
    iterator = root_path.rglob("*") if recursive else root_path.glob("*")
    paths: list[Path] = []
    for path in iterator:
        if not path.is_file():
            continue
        if path.suffix.lower() not in suffixes:
            continue
        paths.append(path)
        if limit is not None and len(paths) >= limit:
            break
    paths.sort(key=lambda p: str(p).lower())
    return paths


def scan_archive_paths(
    paths: Iterable[str | Path],
    *,
    instrument_profile: str = "auto",
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
    limit: int | None = None,
    example_limit: int = 3,
) -> tuple[list[DetectedArchiveFile], list[ArchiveGroupPlan], list[str]]:
    """Scan exactly the supplied FITS paths without directory discovery.

    This is the explicit-list sibling of :func:`scan_archive_path`. It exists so
    Stage-1 handoff can preserve the real ProductStore path/Frame UID while
    guaranteeing that neighboring or historical products are not discovered.
    """

    profiles = load_auto_profiles(master_profile=master_profile, robophot_profile=robophot_profile)
    normalized = [Path(value) for value in paths]
    if limit is not None:
        normalized = normalized[: max(int(limit), 0)]
    detected: list[DetectedArchiveFile] = []
    warnings: list[str] = []
    profile_mode = _normalize_profile_mode(instrument_profile)

    for path in normalized:
        try:
            header = fits.getheader(path, 0)
            profile = _select_profile(header, profiles, profile_mode=profile_mode)
            detected.append(_detected_file_from_header(path, header, profile))
        except Exception as exc:  # noqa: BLE001 - scan must continue over bad files.
            warnings.append(f"failed_to_read_header: {path}: {type(exc).__name__}: {exc}")

    groups = _build_group_plans(detected, profiles=profiles, example_limit=example_limit)
    return detected, groups, warnings


def scan_archive_path(
    root: str | Path,
    *,
    instrument_profile: str = "auto",
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
    recursive: bool = True,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    limit: int | None = None,
    example_limit: int = 3,
) -> tuple[list[DetectedArchiveFile], list[ArchiveGroupPlan], list[str]]:
    paths = discover_fits_files(root, recursive=recursive, extensions=extensions, limit=limit)
    return scan_archive_paths(
        paths,
        instrument_profile=instrument_profile,
        master_profile=master_profile,
        robophot_profile=robophot_profile,
        limit=None,
        example_limit=example_limit,
    )


def process_archive_path(
    session: Session,
    *,
    root: str | Path,
    catalog_name: str | None = None,
    execute: bool = False,
    instrument_profile: str = "auto",
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
    recursive: bool = True,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    exact_paths: Iterable[str | Path] | None = None,
    native_observation_ids: Iterable[str] | None = None,
    limit: int | None = None,
    series_ids: Iterable[int] | None = None,
    min_frames_per_series: int = 10,
    finalize_tail: bool = True,
    max_field_separation_arcmin: float = 30.0,
    max_series_gap_minutes: float = 60.0,
    include_assigned: bool = False,
    include_non_science: bool = False,
    calibration_prefix_template: str | None = None,
    out_root: str | Path = "exports",
    log_dir: str | Path = "logs",
    file_log_level: str = "INFO",
    no_file_log: bool = False,
    aperture_radius_pix: float = 3.0,
    photometry_estimator: str = "adaptive_ellipse",
    local_com_max_shift_pix: float = float(GAIA_WCS_COM_POLICY["local_com_max_shift_pix"]),
    frame_shift_min_matches: int = int(GAIA_WCS_COM_POLICY["frame_shift_min_matches"]),
    frame_shift_max_robust_scatter_pix: float = float(
        GAIA_WCS_COM_POLICY["frame_shift_max_robust_scatter_pix"]
    ),
    frame_shift_max_absolute_pix: float = float(
        GAIA_WCS_COM_POLICY["frame_shift_max_absolute_pix"]
    ),
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
    aperture_correction_reference_radius_pix: float = 5.0,
    aperture_correction_max_calibrator_mag_err: float | None = 0.05,
    aperture_correction_min_calibrators_per_frame: int = 20,
    aperture_correction_min_plane_calibrators: int = 30,
    aperture_correction_min_plane_bic_improvement: float = 6.0,
    aperture_correction_min_spatial_peak_to_peak: float = 0.005,
    aperture_correction_plane_policy: str = "evidence",
    aperture_correction_max_residual_mad: float | None = 0.05,
    aperture_correction_max_abs_correction: float = 0.5,
    aperture_correction_max_abs_spatial_deviation: float = 0.15,
    aperture_correction_max_abs_total_correction: float = 0.75,
    aperture_correction_min_good_frame_fraction: float = 0.8,
    skip_aperture_correction: bool = False,
    bulk_insert_batch_size: int = 1000,
    make_light_curve_summary: bool = True,
    enable_detection_branch: bool = False,
    enable_psf_field_diagnostics: bool = False,
    psf_diagnostics_max_sources_per_frame: int = 500,
    psf_diagnostics_min_snr: float = 20.0,
    psf_diagnostics_cutout_radius_pix: int = 7,
    psf_diagnostics_min_isolation_pix: float = 12.0,
    psf_diagnostics_clip_sigma: float = 4.0,
    psf_diagnostics_n_iterations: int = 3,
    psf_diagnostics_render_plots: bool = True,
    enable_aperture_stability_qc: bool = False,
    require_detection_support: bool | None = None,
    target_source_id: str | None = None,
    target_object_id: int | None = None,
    target_ra_deg: float | None = None,
    target_dec_deg: float | None = None,
    target_max_sep_arcsec: float | None = 120.0,
    export_target_light_curve: bool = True,
    export_candidate_light_curves: bool = True,
    export_candidate_plots: bool = True,
    candidate_light_curve_limit: int | None = None,
    exclude_candidates_from_ensemble: bool = False,
    candidate_ensemble_max_iterations: int = 2,
    candidate_ensemble_until_converged: bool = False,
    candidate_ensemble_convergence_max_iterations: int = 10,
    candidate_ensemble_growth_policy: str = "initial_only",
    ensemble_min_ref_stars_per_frame: int = 30,
    crowded_arcsec: float = float(GAIA_WCS_COM_POLICY["crowded_arcsec"]),
    blended_arcsec: float = float(GAIA_WCS_COM_POLICY["blended_arcsec"]),
    dynamic_separation: bool = bool(GAIA_WCS_COM_POLICY["dynamic_separation"]),
    dynamic_seeing_factor: float = float(GAIA_WCS_COM_POLICY["dynamic_seeing_factor"]),
    dynamic_aperture_factor: float = float(GAIA_WCS_COM_POLICY["dynamic_aperture_factor"]),
    dynamic_crowding_factor: float = float(GAIA_WCS_COM_POLICY["dynamic_crowding_factor"]),
    fwhm_header_unit: str = str(GAIA_WCS_COM_POLICY["fwhm_header_unit"]),
    enable_spatial_ensemble: bool = False,
    spatial_ensemble_max_order: int = 2,
    spatial_ensemble_min_ref_stars_per_frame: int = 30,
    spatial_ensemble_clip_sigma: float = 3.5,
    spatial_ensemble_n_iterations: int = 3,
    spatial_ensemble_min_bic_improvement: float = 6.0,
    spatial_ensemble_min_spatial_peak_to_peak: float = 0.003,
    spatial_ensemble_max_residual_mad: float | None = 0.05,
    spatial_ensemble_max_residual_mad_degradation: float | None = 0.0,
    spatial_ensemble_max_abs_spatial_deviation: float = 0.15,
    spatial_ensemble_min_good_frame_fraction: float = 0.8,
    enable_photometric_frame_qc: bool = False,
    enable_intrinsic_variability_candidates: bool = True,
    field_report_min_intrinsic_rms: float | None = 0.03,
    field_report_min_rms_over_mag_err: float | None = 4.0,
    field_report_min_mad_over_mag_err: float | None = 2.5,
    field_report_intrinsic_mag_err_floor: float | None = 0.01,
    field_report_min_robust_peak_to_peak: float | None = 0.08,
    enable_low_amplitude_intrinsic_candidates: bool = True,
    field_report_low_amplitude_min_measurements: int = 30,
    field_report_low_amplitude_min_intrinsic_rms: float | None = 0.03,
    field_report_low_amplitude_min_rms_over_mag_err: float | None = 2.3,
    field_report_low_amplitude_min_mad_over_mag_err: float | None = 2.0,
    field_report_low_amplitude_min_robust_peak_to_peak: float | None = 0.08,
    exclude_bad_pre_qc_frames: bool = False,
    working_catalog_mag_limit: float | None = 19.0,
    max_working_sources_per_frame: int | None = 2000,
    catalog_edge_margin_pix: float = 20.0,
    force_recompute_photometry: bool = False,
    materialize_intermediate_calibrations: bool = False,
    science_materialization_diagnostics: bool = False,
    forced_photometry_diagnostics: bool = False,
    calibration_performance_diagnostics: bool = False,
    random_error_model: str = PHYSICAL_APERTURE_ERROR_MODEL,
    photometry_gain_e_per_adu: float | None = None,
    photometry_read_noise_e: float | None = None,
    background_estimator_variance_factor: float = MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    empirical_background_floor: bool = True,
    photometry_fractional_flux_floor: float | None = None,
    photometry_scintillation_enabled: bool | None = None,
    photometry_scintillation_coefficient: float | None = None,
    photometry_telescope_aperture_m: float | None = None,
    photometry_site_altitude_m: float | None = None,
    enable_color_calibration: bool = False,
    auto_reference_photometry: bool = True,
    color_reference_catalog_name: str | None = None,
    color_reference_band: str | None = None,
    color_index: str | None = None,
    auto_catalog_query: bool = True,
    auto_catalog_margin_arcmin: float = 2.0,
    auto_catalog_mag_limit: float | None = None,
    auto_catalog_max_rows_per_frame: int = 50000,
    auto_catalog_query_mode: str = "frame_union",
    auto_catalog_max_radius_deg: float | None = None,
    auto_catalog_timeout_sec: float | None = 120.0,
    allow_empty_catalog: bool = False,
    manifest_out: str | Path | None = None,
) -> ProcessArchiveResult:
    from grandgousier.lightcurves.target import validate_target_coordinates

    validate_target_coordinates(target_ra_deg, target_dec_deg)
    exact_path_list = [Path(value) for value in exact_paths] if exact_paths is not None else None
    if exact_path_list is None:
        detected, groups, warnings = scan_archive_path(
            root,
            instrument_profile=instrument_profile,
            master_profile=master_profile,
            robophot_profile=robophot_profile,
            recursive=recursive,
            extensions=extensions,
            limit=limit,
        )
        discovered_count = len(
            discover_fits_files(root, recursive=recursive, extensions=extensions, limit=limit)
        )
    else:
        detected, groups, warnings = scan_archive_paths(
            exact_path_list,
            instrument_profile=instrument_profile,
            master_profile=master_profile,
            robophot_profile=robophot_profile,
            limit=limit,
        )
        discovered_count = len(exact_path_list if limit is None else exact_path_list[: max(int(limit), 0)])
    profiles = load_auto_profiles(master_profile=master_profile, robophot_profile=robophot_profile)
    effective_catalog_name = catalog_name if catalog_name is not None else "gaia_dr3"
    result = ProcessArchiveResult(
        status="PLAN" if not execute else "RUNNING",
        path=str(root),
        catalog_name=effective_catalog_name,
        execute=execute,
        discovered_files=discovered_count,
        detected_files=len(detected),
        failed_files=len(warnings),
        groups=groups,
        warnings=warnings,
    )

    requested_native_ids = (
        tuple(str(value) for value in native_observation_ids)
        if native_observation_ids is not None else None
    )
    if requested_native_ids is not None:
        result.observation_selection_results.append({
            "mode": "EXACT_NATIVE_OBSERVATION_IDS",
            "requested_native_observation_ids": list(requested_native_ids),
            "selected_native_observation_ids": [],
        })
        selector_row = result.observation_selection_results[-1]
        detected_native_ids = [item.native_identity for item in detected]
        detected_native_counts = Counter(detected_native_ids)
        errors = []
        if len(set(requested_native_ids)) != len(requested_native_ids):
            errors.append("requested native observation IDs contain duplicates")
        if not requested_native_ids:
            errors.append("at least one native observation ID is required")
        if any(item.profile_name != "MASTER" for item in detected):
            errors.append("--native-observation-id is supported only for MASTER sources")
        selected_candidates = [
            item for item in detected if item.native_identity in requested_native_ids
        ]
        if any(item.observation_identity_status != "READY" for item in selected_candidates):
            errors.append("requested observations must all have identity READY")
        if any(not native for native in detected_native_ids):
            errors.append("detected file is missing native observation identity")
        found = [native for native in detected_native_ids if native in requested_native_ids]
        if found != list(requested_native_ids):
            errors.append("requested IDs were not found exactly once in requested scanner order")
        if any(detected_native_counts[native] != 1 for native in requested_native_ids):
            errors.append("each requested native observation ID must be detected exactly once")
        if errors:
            result.status = "FAILED_PRECONDITION_NATIVE_OBSERVATION_SELECTION"
            result.warnings.extend(errors)
            selector_row["errors"] = errors
            _write_manifest_if_requested(result, manifest_out, session=session)
            return result
        detected = [item for item in detected if item.native_identity in requested_native_ids]
        selector_row["selected_native_observation_ids"] = [item.native_identity for item in detected]
        result.detected_files = len(detected)
        result.groups = _build_group_plans(detected, profiles=profiles, example_limit=5)

    if (
        target_source_id is None
        and target_object_id is None
        and target_ra_deg is None
        and target_max_sep_arcsec is not None
        and float(target_max_sep_arcsec) < 0
    ):
        result.warnings.append(
            "target provenance disabled: --target-max-sep-arcsec is negative and neither "
            "--target-source-id, --target-object-id nor paired sky coordinates were supplied"
        )

    if not execute:
        observation_plan = plan_file_science_selection(
            session,
            [_science_identity_candidate(item) for item in detected],
            allow_reassign_existing_frame=include_assigned,
        )
        result.observation_selection_results.append(
            {
                "mode": "PLAN_INPUT_FILES",
                **observation_plan.summary,
                "rows": observation_plan.rows,
            }
        )
        warning = _observation_selection_warning(observation_plan.summary, mode="plan")
        if warning:
            result.warnings.append(warning)
        requested_series_ids = sorted({int(v) for v in (series_ids or [])})
        if requested_series_ids:
            scoped_paths = [item.path for item in detected]
            represented_ids = set(_series_ids_for_paths(session, scoped_paths))
            by_series = _frame_ids_by_series_for_paths(session, scoped_paths)
            membership = _requested_series_membership(session, requested_series_ids)
            identities_by_path = {item.path: item.observation_uid for item in detected}
            scoped_identity_rows = _series_frame_identities_for_paths(session, scoped_paths)
            selection_rows = []
            failed = False
            for series_id in requested_series_ids:
                selected = set(by_series.get(series_id, []))
                all_frames = membership.get(series_id, {})
                selected_identities = {
                    frame_id: (path, uid)
                    for sid, frame_id, path, uid in scoped_identity_rows
                    if sid == series_id and frame_id in selected
                }
                mismatched = sorted(
                    frame_id for frame_id, (path, uid) in selected_identities.items()
                    if not uid or not identities_by_path.get(path)
                    or identities_by_path[path] != uid or all_frames.get(frame_id) != uid
                )
                missing = sorted(set(all_frames) - selected)
                invalid = (
                    series_id not in represented_ids
                    or not selected
                    or not all_frames
                    or not selected.issubset(all_frames)
                    or set(selected_identities) != selected
                    or bool(mismatched)
                )
                failed |= invalid
                selection_rows.append({
                    "series_id": series_id,
                    "status": "FAILED_PRECONDITION_SERIES_SELECTION" if invalid else "REPRESENTED",
                    "selected_frame_count": len(selected),
                    "total_series_frame_count": len(all_frames),
                    "selected_frame_ids": sorted(selected),
                    "missing_series_frame_ids": missing,
                    "coverage": "SUBSET" if missing else "COMPLETE",
                    "matched_observation_identity_count": len(selected_identities) - len(mismatched),
                    "selected_observation_uids": sorted(
                        uid for fid, (_path, uid) in selected_identities.items()
                        if fid not in mismatched and uid is not None
                    ),
                    "identity_mismatch_frame_ids": mismatched,
                })
            result.observation_selection_results.append({
                "mode": "PLAN_REQUESTED_SERIES",
                "requested_series_ids": requested_series_ids,
                "selected_series_ids": [] if failed else requested_series_ids,
                "rows": selection_rows,
            })
            if failed:
                result.warnings.append(
                    "requested --series-id selection has missing paths, series membership, "
                    "or mismatched physical observation identities; no series selected"
                )
        result.status = (
            "FAILED_PRECONDITION_SERIES_SELECTION"
            if requested_series_ids and failed else "PLAN"
        )
        _write_manifest_if_requested(result, manifest_out, session=session)
        return result

    indexing = index_detected_archive_files(session, detected, profiles=profiles)
    indexed_paths = list(indexing.indexed_paths)
    result.index_results.extend(indexing.index_results)
    result.failed_files += indexing.failed_files
    if indexing.failed_files > 0:
        result.status = "FAILED_INDEX_FILES"
        result.warnings.append(
            f"archive indexing failed for {indexing.failed_files} file(s)"
        )
        if manifest_out is not None:
            _write_manifest_if_requested(result, manifest_out, session=session)
        return result

    indexed_frame_ids = list(indexing.indexed_frame_ids)
    result.indexed_frame_ids = indexed_frame_ids
    requested_series_ids = sorted({int(v) for v in (series_ids or [])})
    existing_input_series_ids = _series_ids_for_paths(session, indexed_paths)

    science_input_frame_ids = list(indexed_frame_ids)
    if not requested_series_ids:
        observation_selection = select_persisted_science_frames(
            session,
            indexed_frame_ids,
            allow_reassign_existing_frame=include_assigned,
        )
        science_input_frame_ids = list(observation_selection.selected_frame_ids)
        result.observation_selection_results.append(
            {
                "mode": "EXECUTE_PERSISTED_FRAMES",
                **observation_selection.summary,
                "rows": observation_selection.rows,
            }
        )
        warning = _observation_selection_warning(observation_selection.summary, mode="execute")
        if warning:
            result.warnings.append(warning)

        # In ordinary process-by-path mode, only Frames selected by the P4c
        # observation guard are science work for this invocation. Existing
        # series reached through blocked/previously represented input products
        # are historical context, not an implicit request to rerun them.
        # Explicit --series-id remains the supported path for intentional
        # reprocessing of an existing series.
        if not science_input_frame_ids:
            result.status = "DONE_WITH_WARNINGS" if result.warnings else "DONE"
            _write_manifest_if_requested(result, manifest_out, session=session)
            return result

    if catalog_name is not None:
        catalog_row = _inspect_existing_catalog(session, catalog_name=effective_catalog_name)
        result.catalog_results.append(catalog_row)
        if not _catalog_precondition_ok(catalog_row, allow_empty_catalog=allow_empty_catalog):
            result.status = "FAILED_PRECONDITION_CATALOG"
            result.warnings.append(
                f"explicit catalog contains no sources: {effective_catalog_name}; import/populate it, omit --catalog-name for automatic Gaia DR3 creation, or use --allow-empty-catalog for debugging"
            )
            _write_manifest_if_requested(result, manifest_out, session=session)
            return result
    created_series_ids: list[int] = []
    if not requested_series_ids:
        frame_ids_by_unit = _frame_ids_by_instrument_unit(session, science_input_frame_ids)
        for instrument_unit_id, unit_frame_ids in frame_ids_by_unit.items():
            build_result = build_field_series(
                session,
                instrument_unit_id=instrument_unit_id,
                frame_ids=unit_frame_ids,
                max_field_separation_arcmin=max_field_separation_arcmin,
                max_series_gap_minutes=max_series_gap_minutes,
                only_unassigned=not include_assigned,
                only_science=not include_non_science,
                min_frames_per_series=min_frames_per_series,
                finalize_tail=finalize_tail,
                dry_run=False,
            )
            created = [int(row["series_id"]) for row in build_result.rows if row.get("series_id") is not None]
            ready = [
                int(row["series_id"])
                for row in build_result.rows
                if row.get("series_id") is not None and row.get("status") == "READY"
            ]
            open_ids = [
                int(row["series_id"])
                for row in build_result.rows
                if row.get("series_id") is not None and row.get("status") == "OPEN"
            ]
            result.created_series_ids.extend(created)
            result.open_series_ids.extend(open_ids)
            result.series_results.append(
                {
                    "instrument_unit_id": instrument_unit_id,
                    "frame_ids_count": len(unit_frame_ids),
                    "frame_ids_sample": unit_frame_ids[:20],
                    **build_result.summary(),
                    "observation_selection_rows": [
                        _json_safe(row) for row in build_result.observation_selection_rows
                    ],
                    "rows": [_json_safe(row) for row in build_result.rows],
                }
            )
            created_series_ids.extend(ready)

    # Ordinary process-by-path mode must run only series created (or explicitly
    # rebuilt via --include-assigned) from Frames selected for this invocation.
    # Existing series merely encountered through input paths are not pipeline
    # work: otherwise blocked historical representatives can resurrect old
    # series and break replay idempotency. In selective --series-id mode no new
    # series are built; the command intentionally reruns only requested series
    # that are represented by --path.
    if requested_series_ids:
        current_series_ids = _merge_current_series_ids([], existing_input_series_ids)
        missing_series_ids = sorted(set(requested_series_ids) - set(current_series_ids))
        if missing_series_ids:
            result.status = "FAILED_PRECONDITION_SERIES_SELECTION"
            result.warnings.append(
                "requested --series-id values are not associated with FITS frames under --path: "
                + ", ".join(str(v) for v in missing_series_ids)
            )
            _write_manifest_if_requested(result, manifest_out, session=session)
            return result
        pipeline_series_ids = requested_series_ids
    else:
        pipeline_series_ids = sorted(set(created_series_ids))
    frame_ids_by_series_id = _frame_ids_by_series_for_paths(session, indexed_paths)

    pipeline_series_rows = _load_series_for_pipeline(session, pipeline_series_ids)
    finalized_series_rows, open_ids = _separate_open_series(pipeline_series_rows)
    result.open_series_ids = sorted(set(result.open_series_ids).union(open_ids))
    if open_ids:
        result.warnings.append(
            "OPEN field_series were not submitted to Stage 2; explicitly finalize eligible tails first: "
            + ", ".join(str(series_id) for series_id in open_ids)
        )
    if catalog_name is None:
        # The pipeline and Gaia acquisition must share exactly the selected input frames.
        selected_frame_scope = {int(frame_id) for frame_id in science_input_frame_ids}
        frame_ids_by_series_id = {
            int(series_id): [
                int(frame_id)
                for frame_id in frame_ids
                if int(frame_id) in selected_frame_scope
            ]
            for series_id, frame_ids in frame_ids_by_series_id.items()
        }
    field_frame_ids: dict[int, set[int]] = {}
    if catalog_name is None:
        finalized_series_rows.sort(
            key=lambda row: (
                row.get("field_id") is None,
                int(row["field_id"]) if row.get("field_id") is not None else 0,
                int(row["series_id"]),
            )
        )
        for series in finalized_series_rows:
            if series.get("field_id") is None:
                continue
            field_id = int(series["field_id"])
            field_frame_ids.setdefault(field_id, set()).update(
                frame_ids_by_series_id.get(int(series["series_id"]), [])
            )

    if catalog_name is None:
        effective_auto_catalog_mag_limit = _effective_auto_catalog_mag_limit(
            explicit=auto_catalog_mag_limit, working=working_catalog_mag_limit
        )
        effective_auto_catalog_max_radius_deg = _effective_auto_catalog_max_radius_deg(
            groups, explicit=auto_catalog_max_radius_deg
        )
    else:
        effective_auto_catalog_mag_limit = None
        effective_auto_catalog_max_radius_deg = None
    active_field_id: int | None = None
    working_catalog = None
    working_catalog_provenance: dict[str, Any] | None = None
    field_promoted_source_ids: set[str] = set()
    field_series_selections: list[dict[str, Any]] = []
    current_series_promoted_source_ids: set[str] = set()
    field_pipeline_run_ids_by_series: dict[int, int] = {}
    field_provenance_finalized = False
    reference_cache_by_fingerprint: dict[str, _ValidatedReferenceCacheEntry] = {}

    def finalize_active_working_catalog_field() -> None:
        nonlocal field_provenance_finalized, working_catalog_provenance
        if field_provenance_finalized:
            return
        field_provenance_finalized = True
        provenance = working_catalog_provenance
        try:
            _finalize_auto_working_catalog_provenance(
                catalog=working_catalog,
                provenance=provenance,
                promoted_source_ids=field_promoted_source_ids,
                series_selections=field_series_selections,
            )
            if provenance is not None:
                _persist_finalized_auto_working_catalog_provenance(
                    session,
                    field_id=active_field_id,
                    run_ids_by_series=field_pipeline_run_ids_by_series,
                    provenance=provenance,
                )
        finally:
            field_pipeline_run_ids_by_series.clear()
            working_catalog_provenance = None

    try:
        for series in finalized_series_rows:
            if catalog_name is None:
                if series.get("field_id") is None:
                    result.status = "FAILED_PRECONDITION_CATALOG"
                    result.warnings.append(
                        "automatic Gaia acquisition requires a persistent field_id"
                    )
                    finalize_active_working_catalog_field()
                    _write_manifest_if_requested(result, manifest_out, session=session)
                    return result
                field_id = int(series["field_id"])
                if field_id != active_field_id:
                    try:
                        finalize_active_working_catalog_field()
                    finally:
                        _close_working_catalog(working_catalog)
                        working_catalog = None
                    active_field_id = field_id
                    field_provenance_finalized = False
                    catalog_frame_ids = sorted(field_frame_ids.get(field_id, set()))
                    if auto_catalog_query:
                        working_catalog, catalog_row = _prepare_auto_working_catalog(
                            session,
                            field_id=field_id,
                            frame_ids=catalog_frame_ids,
                            enabled=True,
                            margin_arcmin=auto_catalog_margin_arcmin,
                            mag_limit=effective_auto_catalog_mag_limit,
                            maxrec=auto_catalog_max_rows_per_frame,
                            max_radius_deg=effective_auto_catalog_max_radius_deg,
                            timeout_sec=auto_catalog_timeout_sec,
                        )
                    else:
                        working_catalog, catalog_row = _reuse_auto_working_catalog(
                            session,
                            field_id=field_id,
                            frame_ids=catalog_frame_ids,
                            margin_arcmin=auto_catalog_margin_arcmin,
                            mag_limit=effective_auto_catalog_mag_limit,
                            maxrec=auto_catalog_max_rows_per_frame,
                            max_radius_deg=effective_auto_catalog_max_radius_deg,
                            target_source_id=target_source_id,
                            working_catalog_mag_limit=working_catalog_mag_limit,
                            max_working_sources_per_frame=max_working_sources_per_frame,
                            catalog_edge_margin_pix=catalog_edge_margin_pix,
                            aperture_radius_pix=aperture_radius_pix,
                            annulus_inner_pix=annulus_inner_pix,
                            annulus_outer_pix=annulus_outer_pix,
                        )
                    if (
                        not catalog_row.get("reuse_compatible", True)
                        and allow_empty_catalog
                        and not auto_catalog_query
                    ):
                        from grandgousier.catalogs.gaia_query import GaiaWorkingCatalog

                        refusal_reason = str(
                            catalog_row.get("refusal_reason")
                            or "OFFLINE_EVIDENCE_INCOMPATIBLE"
                        )
                        working_catalog = GaiaWorkingCatalog()
                        working_catalog_provenance = {
                            "provenance_version": 1,
                            "field_id": field_id,
                            "field_frame_count": len(set(catalog_frame_ids)),
                            "reuse_mode": "empty_catalog_debug_fallback",
                            "empty_catalog_debug_fallback": True,
                            "empty_catalog_debug_fallback_reason": refusal_reason,
                            "new_source_search_performed": False,
                            "coverage_complete": False,
                            "per_frame_match_coverage_complete": False,
                            "fetch_status": "NOT_ATTEMPTED",
                            "fetch_complete": False,
                            "working_catalog_sources": 0,
                        }
                        catalog_row.update(
                            {
                                "offline_reuse_status": catalog_row.get("status"),
                                "status": "DONE_WITH_WARNINGS",
                                "empty_catalog_debug_fallback": True,
                                "empty_catalog_debug_fallback_reason": refusal_reason,
                                "effective_query_mode": "no_search_empty_debug_fallback",
                                "working_catalog_sources": 0,
                                "new_source_search_performed": False,
                                "coverage_complete": False,
                                "per_frame_match_coverage_complete": False,
                                "working_catalog_provenance": working_catalog_provenance,
                            }
                        )
                        result.warnings.append(
                            "offline Gaia evidence was absent or incompatible; "
                            "explicit empty-catalog debugging fallback used "
                            f"(reason={refusal_reason})"
                        )
                    else:
                        provenance = catalog_row.get("working_catalog_provenance")
                        working_catalog_provenance = (
                            provenance if isinstance(provenance, dict) else None
                        )
                    field_promoted_source_ids.clear()
                    field_series_selections.clear()
                    result.catalog_results.append(catalog_row)
                    if catalog_row.get("status") not in {"DONE", "DONE_WITH_WARNINGS"}:
                        result.warnings.append(
                            f"auto Gaia working catalog field_id={field_id} "
                            f"status={catalog_row.get('status')}"
                        )
                    elif catalog_row.get("status") == "DONE_WITH_WARNINGS":
                        result.warnings.append(
                            f"auto Gaia working catalog field_id={field_id} completed with warnings"
                        )
                    if not catalog_row.get("reuse_compatible", True) and not catalog_row.get(
                        "empty_catalog_debug_fallback", False
                    ):
                        result.status = "FAILED_PRECONDITION_CATALOG"
                        finalize_active_working_catalog_field()
                        _write_manifest_if_requested(result, manifest_out, session=session)
                        return result
                    if not _working_catalog_precondition_ok(
                        catalog_row, allow_empty_catalog=allow_empty_catalog
                    ):
                        result.status = "FAILED_PRECONDITION_CATALOG"
                        finalize_active_working_catalog_field()
                        _write_manifest_if_requested(result, manifest_out, session=session)
                        return result
                    if working_catalog is None:
                        from grandgousier.catalogs.gaia_query import GaiaWorkingCatalog

                        working_catalog = GaiaWorkingCatalog()
            current_series_promoted_source_ids = set()
            prefix = _format_calibration_prefix(calibration_prefix_template, series)
            series_out_dir = str(Path(out_root) / prefix) if out_root else None
            profile = profiles.get(str(series.get("instrument") or ""))
            error_policy = {}
            if profile is not None and isinstance(profile.raw_config, dict):
                configured = profile.raw_config.get("random_error_model")
                if isinstance(configured, dict):
                    error_policy = configured
            resolved_fractional_flux_floor = (
                float(photometry_fractional_flux_floor)
                if photometry_fractional_flux_floor is not None
                else float(error_policy.get("fractional_flux_floor", 0.0) or 0.0)
            )
            resolved_scintillation_enabled = (
                bool(photometry_scintillation_enabled)
                if photometry_scintillation_enabled is not None
                else bool(error_policy.get("scintillation_enabled", False))
            )
            resolved_scintillation_coefficient = (
                float(photometry_scintillation_coefficient)
                if photometry_scintillation_coefficient is not None
                else float(error_policy.get("scintillation_coefficient", 0.09) or 0.09)
            )
            resolved_telescope_aperture_m = (
                float(photometry_telescope_aperture_m)
                if photometry_telescope_aperture_m is not None
                else _optional_float(error_policy.get("telescope_aperture_m"))
            )
            resolved_site_altitude_m = (
                float(photometry_site_altitude_m)
                if photometry_site_altitude_m is not None
                else _optional_float(error_policy.get("site_altitude_m"))
            )
            color_policy = None
            reference_result: dict[str, Any] | None = None
            if profile is not None:
                color_policy = resolve_color_calibration_policy(
                    profile,
                    series.get("filter_name"),
                    enabled_override=True if enable_color_calibration else None,
                    reference_catalog_name=color_reference_catalog_name,
                    reference_band=color_reference_band,
                    color_index=color_index,
                )
            color_reference_ready = False
            color_calibration_skip_reason: str | None = None
            if enable_color_calibration and (color_policy is None or not color_policy.enabled):
                reason = color_policy.skip_reason if color_policy is not None else "instrument profile was not resolved"
                color_calibration_skip_reason = str(reason)
                result.warnings.append(
                    f"series_id={series['series_id']}: color calibration skipped: {reason}"
                )
            elif enable_color_calibration and color_policy is not None:
                if catalog_name is not None:
                    color_calibration_skip_reason = (
                        "exact selected identity sources are unavailable before this Stage-2 run"
                    )
                    reference_result = {
                        "series_id": int(series["series_id"]),
                        "field_id": int(series["field_id"]),
                        "status": "SKIPPED_SELECTED_IDENTITY_SET_UNAVAILABLE",
                        "ready": False,
                        "reason": color_calibration_skip_reason,
                    }
                    result.warnings.append(
                        f"series_id={series['series_id']}: color calibration skipped: "
                        f"{color_calibration_skip_reason}"
                    )
                    result.reference_photometry_results.append(reference_result)
            config = FieldPipelineConfig(
                catalog_name=effective_catalog_name,
                calibration_prefix=prefix,
                series_id=int(series["series_id"]),
                frame_ids=frame_ids_by_series_id.get(int(series["series_id"]), []),
                out_dir=series_out_dir,
                aperture_radius_pix=aperture_radius_pix,
                photometry_estimator=photometry_estimator,
                local_com_max_shift_pix=local_com_max_shift_pix,
                frame_shift_min_matches=frame_shift_min_matches,
                frame_shift_max_robust_scatter_pix=frame_shift_max_robust_scatter_pix,
                frame_shift_max_absolute_pix=frame_shift_max_absolute_pix,
                adaptive_detection_algorithm=adaptive_detection_algorithm,
                adaptive_min_psf_snr=adaptive_min_psf_snr,
                adaptive_max_psf_sources_per_frame=adaptive_max_psf_sources_per_frame,
                adaptive_psf_cutout_radius_pix=adaptive_psf_cutout_radius_pix,
                adaptive_psf_min_isolation_pix=adaptive_psf_min_isolation_pix,
                adaptive_interpolation_neighbors=adaptive_interpolation_neighbors,
                adaptive_min_psf_references=adaptive_min_psf_references,
                adaptive_aperture_fwhm_factor=adaptive_aperture_fwhm_factor,
                adaptive_max_axis_ratio=adaptive_max_axis_ratio,
                adaptive_max_radius_scale=adaptive_max_radius_scale,
                adaptive_fallback_to_circular=adaptive_fallback_to_circular,
                psf_profile=psf_profile,
                psf_moffat_beta=psf_moffat_beta,
                psf_fit_radius_fwhm=psf_fit_radius_fwhm,
                psf_min_fit_radius_pix=psf_min_fit_radius_pix,
                psf_max_fit_radius_pix=psf_max_fit_radius_pix,
                psf_centroid_search_radius_pix=psf_centroid_search_radius_pix,
                psf_centroid_grid_steps=psf_centroid_grid_steps,
                psf_residual_clip_sigma=psf_residual_clip_sigma,
                psf_residual_clip_iterations=psf_residual_clip_iterations,
                psf_fallback_to_adaptive=psf_fallback_to_adaptive,
                empirical_psf_grid_size=empirical_psf_grid_size,
                empirical_psf_stamp_radius_pix=empirical_psf_stamp_radius_pix,
                empirical_psf_reference_radius_pix=empirical_psf_reference_radius_pix,
                empirical_psf_references_per_node=empirical_psf_references_per_node,
                empirical_psf_min_references_per_node=empirical_psf_min_references_per_node,
                empirical_psf_fallback_to_adaptive=empirical_psf_fallback_to_adaptive,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                aperture_correction_reference_radius_pix=aperture_correction_reference_radius_pix,
                aperture_correction_max_calibrator_mag_err=aperture_correction_max_calibrator_mag_err,
                aperture_correction_min_calibrators_per_frame=aperture_correction_min_calibrators_per_frame,
                aperture_correction_min_plane_calibrators=aperture_correction_min_plane_calibrators,
                aperture_correction_min_plane_bic_improvement=aperture_correction_min_plane_bic_improvement,
                aperture_correction_min_spatial_peak_to_peak=aperture_correction_min_spatial_peak_to_peak,
                aperture_correction_plane_policy=aperture_correction_plane_policy,
                aperture_correction_max_residual_mad=aperture_correction_max_residual_mad,
                aperture_correction_max_abs_correction=aperture_correction_max_abs_correction,
                aperture_correction_max_abs_spatial_deviation=(
                    aperture_correction_max_abs_spatial_deviation
                ),
                aperture_correction_max_abs_total_correction=(
                    aperture_correction_max_abs_total_correction
                ),
                aperture_correction_min_good_frame_fraction=aperture_correction_min_good_frame_fraction,
                skip_aperture_correction=skip_aperture_correction,
                bulk_insert_batch_size=bulk_insert_batch_size,
                skip_detection=not (enable_detection_branch or enable_psf_field_diagnostics or photometry_estimator in {"adaptive_ellipse", "spatial_psf", "empirical_psf"}),
                enable_psf_field_diagnostics=enable_psf_field_diagnostics,
                psf_diagnostics_max_sources_per_frame=psf_diagnostics_max_sources_per_frame,
                psf_diagnostics_min_snr=psf_diagnostics_min_snr,
                psf_diagnostics_cutout_radius_pix=psf_diagnostics_cutout_radius_pix,
                psf_diagnostics_min_isolation_pix=psf_diagnostics_min_isolation_pix,
                psf_diagnostics_clip_sigma=psf_diagnostics_clip_sigma,
                psf_diagnostics_n_iterations=psf_diagnostics_n_iterations,
                psf_diagnostics_render_plots=psf_diagnostics_render_plots,
                skip_detection_catalog_match=not enable_detection_branch,
                require_detection_support=require_detection_support,
                target_source_id=target_source_id,
                target_object_id=target_object_id,
                target_ra_deg=target_ra_deg,
                target_dec_deg=target_dec_deg,
                target_max_sep_arcsec=target_max_sep_arcsec,
                export_target_light_curve=export_target_light_curve,
                export_candidate_light_curves=export_candidate_light_curves,
                export_candidate_plots=export_candidate_plots,
                candidate_light_curve_limit=candidate_light_curve_limit,
                exclude_candidates_from_ensemble=exclude_candidates_from_ensemble,
                candidate_ensemble_max_iterations=candidate_ensemble_max_iterations,
                candidate_ensemble_until_converged=candidate_ensemble_until_converged,
                candidate_ensemble_convergence_max_iterations=candidate_ensemble_convergence_max_iterations,
                candidate_ensemble_growth_policy=candidate_ensemble_growth_policy,
                ensemble_min_ref_stars_per_frame=ensemble_min_ref_stars_per_frame,
                crowded_arcsec=crowded_arcsec,
                blended_arcsec=blended_arcsec,
                dynamic_separation=dynamic_separation,
                dynamic_seeing_factor=dynamic_seeing_factor,
                dynamic_aperture_factor=dynamic_aperture_factor,
                dynamic_crowding_factor=dynamic_crowding_factor,
                fwhm_header_unit=fwhm_header_unit,
                enable_spatial_ensemble=enable_spatial_ensemble,
                spatial_ensemble_max_order=spatial_ensemble_max_order,
                spatial_ensemble_min_ref_stars_per_frame=spatial_ensemble_min_ref_stars_per_frame,
                spatial_ensemble_clip_sigma=spatial_ensemble_clip_sigma,
                spatial_ensemble_n_iterations=spatial_ensemble_n_iterations,
                spatial_ensemble_min_bic_improvement=spatial_ensemble_min_bic_improvement,
                spatial_ensemble_min_spatial_peak_to_peak=spatial_ensemble_min_spatial_peak_to_peak,
                spatial_ensemble_max_residual_mad=spatial_ensemble_max_residual_mad,
                spatial_ensemble_max_residual_mad_degradation=spatial_ensemble_max_residual_mad_degradation,
                spatial_ensemble_max_abs_spatial_deviation=spatial_ensemble_max_abs_spatial_deviation,
                spatial_ensemble_min_good_frame_fraction=spatial_ensemble_min_good_frame_fraction,
                enable_photometric_frame_qc=enable_photometric_frame_qc,
                enable_color_calibration=bool(
                    enable_color_calibration
                    and color_policy
                    and color_policy.enabled
                    and catalog_name is None
                ),
                color_calibration_requested=bool(enable_color_calibration),
                color_reference_ready=(
                    None
                    if catalog_name is None
                    and enable_color_calibration
                    and color_policy is not None
                    and color_policy.enabled
                    else (bool(color_reference_ready) if enable_color_calibration else None)
                ),
                color_calibration_skip_reason=color_calibration_skip_reason,
                color_identity_catalog_name=effective_catalog_name,
                color_reference_catalog_name=(
                    reference_result.get("effective_reference_catalog_name")
                    if reference_result is not None
                    else (color_policy.reference_catalog_name if color_policy else color_reference_catalog_name)
                ),
                color_reference_band=color_policy.reference_band if color_policy else color_reference_band,
                color_index=color_policy.color_index if color_policy else color_index,
                color_min_measurements_per_object=color_policy.min_measurements_per_object if color_policy else 5,
                color_min_stars_per_frame=color_policy.min_stars_per_frame if color_policy else 20,
                color_max_measurement_mag_err=color_policy.max_measurement_mag_err if color_policy else None,
                color_max_reference_mag_err=color_policy.max_reference_mag_err if color_policy else None,
                color_min_reference_mag=color_policy.min_reference_mag if color_policy else None,
                color_max_reference_mag=color_policy.max_reference_mag if color_policy else None,
                color_min_value=color_policy.min_color if color_policy else None,
                color_max_value=color_policy.max_color if color_policy else None,
                color_clip_sigma=color_policy.clip_sigma if color_policy else 4.0,
                color_n_iterations=color_policy.n_iterations if color_policy else 5,
                color_min_calibrator_objects=color_policy.min_calibrator_objects if color_policy else 20,
                color_min_span=color_policy.min_color_span if color_policy else 0.3,
                color_max_transform_residual_mad=color_policy.max_transform_residual_mad if color_policy else 0.10,
                color_max_pivot_fraction=color_policy.max_pivot_color_fraction if color_policy else 0.25,
                color_max_shape_metric_delta=color_policy.max_shape_metric_delta if color_policy else 1e-10,
                color_min_term_significance=color_policy.min_color_term_significance if color_policy else 2.0,
                color_min_bic_improvement=color_policy.min_color_model_bic_improvement if color_policy else 6.0,
                color_min_mad_improvement=color_policy.min_color_model_mad_improvement if color_policy else 0.0,
                enable_intrinsic_variability_candidates=enable_intrinsic_variability_candidates,
                field_report_min_intrinsic_rms=field_report_min_intrinsic_rms,
                field_report_min_rms_over_mag_err=field_report_min_rms_over_mag_err,
                field_report_min_mad_over_mag_err=field_report_min_mad_over_mag_err,
                field_report_intrinsic_mag_err_floor=field_report_intrinsic_mag_err_floor,
                field_report_min_robust_peak_to_peak=field_report_min_robust_peak_to_peak,
                enable_low_amplitude_intrinsic_candidates=enable_low_amplitude_intrinsic_candidates,
                field_report_low_amplitude_min_measurements=field_report_low_amplitude_min_measurements,
                field_report_low_amplitude_min_intrinsic_rms=field_report_low_amplitude_min_intrinsic_rms,
                field_report_low_amplitude_min_rms_over_mag_err=field_report_low_amplitude_min_rms_over_mag_err,
                field_report_low_amplitude_min_mad_over_mag_err=field_report_low_amplitude_min_mad_over_mag_err,
                field_report_low_amplitude_min_robust_peak_to_peak=field_report_low_amplitude_min_robust_peak_to_peak,
                skip_aperture_stability_qc=not enable_aperture_stability_qc,
                exclude_bad_pre_qc_frames=exclude_bad_pre_qc_frames,
                working_catalog_mag_limit=working_catalog_mag_limit,
                max_working_sources_per_frame=max_working_sources_per_frame,
                catalog_edge_margin_pix=catalog_edge_margin_pix,
                force_recompute_photometry=force_recompute_photometry,
                materialize_intermediate_calibrations=materialize_intermediate_calibrations,
                science_materialization_diagnostics=science_materialization_diagnostics,
                forced_photometry_diagnostics=forced_photometry_diagnostics,
                calibration_performance_diagnostics=calibration_performance_diagnostics,
                random_error_model=random_error_model,
                photometry_gain_e_per_adu=photometry_gain_e_per_adu,
                photometry_read_noise_e=photometry_read_noise_e,
                background_estimator_variance_factor=(
                    background_estimator_variance_factor
                ),
                empirical_background_floor=empirical_background_floor,
                photometry_fractional_flux_floor=resolved_fractional_flux_floor,
                photometry_scintillation_enabled=resolved_scintillation_enabled,
                photometry_scintillation_coefficient=resolved_scintillation_coefficient,
                photometry_telescope_aperture_m=resolved_telescope_aperture_m,
                photometry_site_altitude_m=resolved_site_altitude_m,
                record_run=True,
                log_dir=str(log_dir) if log_dir else None,
                file_log_level=file_log_level,
                no_file_log=no_file_log,
                command_json={
                    "command": "process-archive-path",
                    "root": str(root),
                    "catalog_name": effective_catalog_name,
                    "working_catalog_mag_limit": working_catalog_mag_limit,
                    "max_working_sources_per_frame": max_working_sources_per_frame,
                    "catalog_edge_margin_pix": catalog_edge_margin_pix,
                    "enable_detection_branch": enable_detection_branch,
                    "enable_psf_field_diagnostics": enable_psf_field_diagnostics,
                    "psf_diagnostics_max_sources_per_frame": psf_diagnostics_max_sources_per_frame,
                    "psf_diagnostics_min_snr": psf_diagnostics_min_snr,
                    "psf_diagnostics_cutout_radius_pix": psf_diagnostics_cutout_radius_pix,
                    "psf_diagnostics_min_isolation_pix": psf_diagnostics_min_isolation_pix,
                    "enable_aperture_stability_qc": enable_aperture_stability_qc,
                    "skip_aperture_correction": skip_aperture_correction,
                    "calibration_performance_diagnostics": calibration_performance_diagnostics,
                    "aperture_correction_reference_radius_pix": aperture_correction_reference_radius_pix,
                    "aperture_correction_max_calibrator_mag_err": aperture_correction_max_calibrator_mag_err,
                    "aperture_correction_min_calibrators_per_frame": aperture_correction_min_calibrators_per_frame,
                    "aperture_correction_min_plane_calibrators": aperture_correction_min_plane_calibrators,
                    "aperture_correction_min_plane_bic_improvement": aperture_correction_min_plane_bic_improvement,
                    "aperture_correction_min_spatial_peak_to_peak": aperture_correction_min_spatial_peak_to_peak,
                    "aperture_correction_plane_policy": aperture_correction_plane_policy,
                    "aperture_correction_max_residual_mad": aperture_correction_max_residual_mad,
                    "aperture_correction_max_abs_correction": aperture_correction_max_abs_correction,
                    "aperture_correction_max_abs_spatial_deviation": (
                        aperture_correction_max_abs_spatial_deviation
                    ),
                    "aperture_correction_max_abs_total_correction": (
                        aperture_correction_max_abs_total_correction
                    ),
                    "aperture_correction_min_good_frame_fraction": aperture_correction_min_good_frame_fraction,
                    "require_detection_support": require_detection_support,
                    "target_source_id": target_source_id,
                    "target_object_id": target_object_id,
                    "target_ra_deg": target_ra_deg,
                    "target_dec_deg": target_dec_deg,
                    "target_max_sep_arcsec": target_max_sep_arcsec,
                    "export_target_light_curve": export_target_light_curve,
                    "export_candidate_light_curves": export_candidate_light_curves,
                    "export_candidate_plots": export_candidate_plots,
                    "candidate_light_curve_limit": candidate_light_curve_limit,
                    "exclude_candidates_from_ensemble": exclude_candidates_from_ensemble,
                    "candidate_ensemble_max_iterations": candidate_ensemble_max_iterations,
                    "candidate_ensemble_until_converged": candidate_ensemble_until_converged,
                    "candidate_ensemble_convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
                    "candidate_ensemble_growth_policy": candidate_ensemble_growth_policy,
                    "ensemble_min_ref_stars_per_frame": int(ensemble_min_ref_stars_per_frame),
                    "astrometric_centering": {
                        "local_com_max_shift_pix": local_com_max_shift_pix,
                        "frame_shift_min_matches": frame_shift_min_matches,
                        "frame_shift_max_robust_scatter_pix": frame_shift_max_robust_scatter_pix,
                        "frame_shift_max_absolute_pix": frame_shift_max_absolute_pix,
                        "crowded_arcsec": crowded_arcsec,
                        "blended_arcsec": blended_arcsec,
                        "dynamic_separation": dynamic_separation,
                        "dynamic_seeing_factor": dynamic_seeing_factor,
                        "dynamic_aperture_factor": dynamic_aperture_factor,
                        "dynamic_crowding_factor": dynamic_crowding_factor,
                        "fwhm_header_unit": fwhm_header_unit,
                    },
                    "selected_series_ids": requested_series_ids or None,
                    "enable_spatial_ensemble": bool(enable_spatial_ensemble),
                    "enable_photometric_frame_qc": bool(enable_photometric_frame_qc),
                    "enable_color_calibration": bool(enable_color_calibration),
                    "auto_reference_photometry": bool(auto_reference_photometry),
                    "color_reference_ready": bool(color_reference_ready),
                    "color_policy": color_policy.to_dict() if color_policy else None,
                    "enable_intrinsic_variability_candidates": enable_intrinsic_variability_candidates,
                    "field_report_min_intrinsic_rms": field_report_min_intrinsic_rms,
                    "field_report_min_rms_over_mag_err": field_report_min_rms_over_mag_err,
                    "field_report_min_mad_over_mag_err": field_report_min_mad_over_mag_err,
                    "field_report_intrinsic_mag_err_floor": field_report_intrinsic_mag_err_floor,
                    "field_report_min_robust_peak_to_peak": field_report_min_robust_peak_to_peak,
                    "enable_low_amplitude_intrinsic_candidates": enable_low_amplitude_intrinsic_candidates,
                    "field_report_low_amplitude_min_measurements": field_report_low_amplitude_min_measurements,
                    "field_report_low_amplitude_min_intrinsic_rms": field_report_low_amplitude_min_intrinsic_rms,
                    "field_report_low_amplitude_min_rms_over_mag_err": field_report_low_amplitude_min_rms_over_mag_err,
                    "field_report_low_amplitude_min_mad_over_mag_err": field_report_low_amplitude_min_mad_over_mag_err,
                    "field_report_low_amplitude_min_robust_peak_to_peak": field_report_low_amplitude_min_robust_peak_to_peak,
                },
                environment_json={"generated_by": "process-archive-path"},
            )
            try:
                if catalog_name is None:
                    reference_callback = None
                    if enable_color_calibration and color_policy is not None and color_policy.enabled:
                        def _selected_reference_callback(
                            selected_source_ids: set[str],
                        ) -> dict[str, Any]:
                            nonlocal reference_result
                            nonlocal color_reference_ready
                            nonlocal color_calibration_skip_reason
                            try:
                                reference_result = _ensure_reference_photometry_for_series(
                                    session,
                                    series=series,
                                    policy=color_policy,
                                    identity_catalog_name=effective_catalog_name,
                                    selected_identity_source_ids=selected_source_ids,
                                    out_dir=out_root,
                                    allow_remote_fetch=auto_reference_photometry,
                                    process_local_cache=reference_cache_by_fingerprint,
                                )
                            except Exception as exc:  # noqa: BLE001 - preserve ensemble calibration fallback.
                                session.rollback()
                                reference_result = {
                                    "series_id": int(series["series_id"]),
                                    "field_id": int(series["field_id"]),
                                    "status": "REFERENCE_ACQUISITION_FAILED",
                                    "ready": False,
                                    "reason": f"{type(exc).__name__}: optional reference acquisition failed",
                                }
                            result.reference_photometry_results.append(reference_result)
                            color_reference_ready = bool(reference_result.get("ready"))
                            if not color_reference_ready:
                                color_calibration_skip_reason = str(
                                    reference_result.get("reason") or reference_result.get("status")
                                )
                                result.warnings.append(
                                    f"series_id={series['series_id']}: color calibration skipped: "
                                    f"{color_calibration_skip_reason}"
                                )
                            return reference_result

                        reference_callback = _selected_reference_callback
                    pipeline_result = run_field_pipeline(
                        session,
                        config,
                        working_catalog=working_catalog,
                        working_catalog_provenance=working_catalog_provenance,
                        working_catalog_promoted_source_ids=current_series_promoted_source_ids,
                        working_catalog_run_ids_by_series=field_pipeline_run_ids_by_series,
                        **(
                            {"reference_photometry_callback": reference_callback}
                            if reference_callback is not None
                            else {}
                        ),
                    )
                else:
                    pipeline_result = run_field_pipeline(session, config)
                pipeline_run_id = getattr(pipeline_result, "pipeline_run_id", None)
                if pipeline_run_id is not None:
                    field_pipeline_run_ids_by_series[int(series["series_id"])] = int(
                        pipeline_run_id
                    )
                field_promoted_source_ids.update(current_series_promoted_source_ids)
                selection_record = _auto_working_catalog_series_selection(
                    series_id=int(series["series_id"]),
                    pipeline_result=pipeline_result,
                    promoted_source_ids=current_series_promoted_source_ids,
                    config=config,
                )
                if working_catalog_provenance is not None:
                    field_series_selections.append(selection_record)
                pipe_row = _pipeline_row(series, prefix, pipeline_result)
                result.processed_series_ids.append(int(series["series_id"]))
                if pipeline_result.status == "DONE_WITH_WARNINGS":
                    warning_summary = pipeline_result.quality_policy.get(
                        "pipeline_warning_summary"
                    )
                    result.warnings.append(
                        _pipeline_warning_message(
                            int(series["series_id"]), warning_summary
                        )
                    )
                if (
                    reference_result is not None
                    and reference_result.get("cache_provenance")
                    and series_out_dir
                ):
                    for reference_artifact_path in (
                        reference_result.get("csv_path"),
                        reference_result.get("manifest_path"),
                    ):
                        if not reference_artifact_path:
                            continue
                        try:
                            _snapshot_post_pipeline_artifact(
                                session,
                                pipeline_result=pipeline_result,
                                series_out_dir=series_out_dir,
                                source_path=str(reference_artifact_path),
                                series_id=int(series["series_id"]),
                                artifact_type="REFERENCE_PHOTOMETRY_CACHE",
                                artifact_role="INPUT_CACHE",
                            )
                        except Exception as artifact_exc:  # noqa: BLE001 - science run already completed.
                            session.rollback()
                            result.warnings.append(
                                f"series_id={series['series_id']}: reference artifact registration failed: "
                                f"{type(artifact_exc).__name__}: {artifact_exc}"
                            )
                if pipeline_result.status == "DONE_NO_SCIENCE_FRAMES":
                    pipe_row.update(
                        {
                            "science_status": "BAD_FRAME_QC",
                            "total_points": 0,
                            "accepted_points": 0,
                            "rejected_points": 0,
                            "n_objects": 0,
                        }
                    )
                    result.warnings.append(
                        f"series_id={series['series_id']} science_status=BAD_FRAME_QC all selected frames rejected by pre-QC"
                    )
                elif make_light_curve_summary and (
                    pipeline_result.calibration_names.get("science") or pipeline_result.calibration_names.get("ensemble")
                ):
                    summary_path = None
                    if series_out_dir:
                        summary_path = str(Path(series_out_dir) / f"{prefix}_light_curve_summary.json")
                    require_detection_support_effective = bool(
                        pipeline_result.quality_policy.get("require_detection_support_effective", enable_detection_branch)
                    )
                    science_calibration = (
                        pipeline_result.calibration_names.get("science")
                        or pipeline_result.calibration_names["ensemble"]
                    )
                    summary = build_light_curve_summary(
                        session,
                        calibration_names=[science_calibration],
                        catalog_name=effective_catalog_name,
                        series_id=int(series["series_id"]),
                        reject_flags=light_curve_reject_flags(
                            require_detection_support=require_detection_support_effective,
                            extra_flags=("PHOTOMETRIC_FRAME_BAD" if enable_photometric_frame_qc else None),
                        ),
                    )
                    if summary_path:
                        export_light_curve_summary_json(summary, summary_path)
                        try:
                            _snapshot_post_pipeline_artifact(
                                session,
                                pipeline_result=pipeline_result,
                                series_out_dir=series_out_dir,
                                source_path=summary_path,
                                series_id=int(series["series_id"]),
                                artifact_type="LIGHT_CURVE_SUMMARY",
                                artifact_role="SCIENCE",
                            )
                        except Exception as artifact_exc:  # noqa: BLE001 - retain the science product and report provenance warning.
                            session.rollback()
                            result.warnings.append(
                                f"series_id={series['series_id']}: light-curve summary artifact registration failed: "
                                f"{type(artifact_exc).__name__}: {artifact_exc}"
                            )
                    science_status = _classify_science_status(
                        summary.total_points,
                        summary.accepted_points,
                        summary.n_objects,
                        pipeline_result=pipeline_result,
                    )
                    summary_row = {
                        "series_id": int(series["series_id"]),
                        "calibration_name": science_calibration,
                        "path": summary_path,
                        "total_points": summary.total_points,
                        "accepted_points": summary.accepted_points,
                        "rejected_points": summary.rejected_points,
                        "n_objects": summary.n_objects,
                        "science_status": science_status,
                        "quality_policy": dict(pipeline_result.quality_policy),
                    }
                    result.light_curve_summaries.append(summary_row)
                    pipe_row.update(
                        {
                            "science_status": science_status,
                            "quality_policy": dict(pipeline_result.quality_policy),
                            "total_points": summary.total_points,
                            "accepted_points": summary.accepted_points,
                            "rejected_points": summary.rejected_points,
                            "n_objects": summary.n_objects,
                            "light_curve_summary_json": summary_path,
                            "target_light_curve_csv": pipeline_result.outputs.get("target_light_curve_csv"),
                            "target_light_curve_json": pipeline_result.outputs.get("target_light_curve_json"),
                            "accepted_candidate_light_curves_dir": pipeline_result.outputs.get("accepted_candidate_light_curves_dir"),
                            "accepted_candidates_summary_csv": pipeline_result.outputs.get("accepted_candidates_summary_csv"),
                            "accepted_candidates_summary_json": pipeline_result.outputs.get("accepted_candidates_summary_json"),
                            "accepted_candidates_index_html": pipeline_result.outputs.get("accepted_candidates_index_html"),
                            "accepted_candidate_count": _step_metric(pipeline_result, "export_candidate_light_curves", "accepted_candidates"),
                            "exported_candidate_light_curves": _step_metric(pipeline_result, "export_candidate_light_curves", "exported_light_curves"),
                            "candidate_plot_count": _step_metric(pipeline_result, "export_candidate_light_curves", "candidate_plot_count"),
                            "candidate_plot_warnings_count": _step_metric(pipeline_result, "export_candidate_light_curves", "candidate_plot_warnings_count"),
                            "candidate_plot_backend": _step_metric(pipeline_result, "export_candidate_light_curves", "plot_backend"),
                            "ensemble_initial_calibration": pipeline_result.outputs.get("ensemble_initial_calibration"),
                            "ensemble_candidate_excluded_calibration": pipeline_result.outputs.get("ensemble_candidate_excluded_calibration"),
                        }
                    )
                    if science_status != "OK":
                        result.warnings.append(
                            f"series_id={series['series_id']} science_status={science_status} total_points={summary.total_points} accepted_points={summary.accepted_points}"
                        )
                elif pipeline_result.status in {"DONE", "DONE_WITH_WARNINGS"}:
                    pipe_row["science_status"] = "NO_LIGHT_CURVE_SUMMARY"
                    result.warnings.append(f"series_id={series['series_id']} science_status=NO_LIGHT_CURVE_SUMMARY")
                result.pipeline_results.append(pipe_row)
            except Exception as exc:  # noqa: BLE001 - batch command reports per-series failures.
                field_promoted_source_ids.update(current_series_promoted_source_ids)
                if working_catalog_provenance is not None and not any(
                    int(item["series_id"]) == int(series["series_id"])
                    for item in field_series_selections
                ):
                    field_series_selections.append(
                        _auto_working_catalog_series_selection(
                            series_id=int(series["series_id"]),
                            pipeline_result=None,
                            promoted_source_ids=current_series_promoted_source_ids,
                            config=config,
                        )
                    )
                # A failed SQL statement leaves PostgreSQL transactions aborted until
                # rollback.  Isolate series failures so the next series can still run.
                session.rollback()
                failure_status = (
                    FAILED_PRECONDITION_ENSEMBLE_STATUS
                    if isinstance(exc, EnsemblePreconditionError)
                    else "FAILED"
                )
                if catalog_name is None:
                    error_message = f"{type(exc).__name__}: automatic field pipeline failed"
                    failure_diagnostics = {}
                else:
                    error_message = f"{type(exc).__name__}: {exc}"
                    diagnostics = getattr(exc, "diagnostics", None)
                    failure_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                result.pipeline_results.append(
                    {
                        "series_id": int(series["series_id"]),
                        "field_name": series.get("field_name"),
                        "calibration_prefix": prefix,
                        "status": failure_status,
                        "error_message": error_message,
                        **failure_diagnostics,
                    }
                )

    finally:
        try:
            finalize_active_working_catalog_field()
        finally:
            _close_working_catalog(working_catalog)
    failed_rows = [
        row for row in result.pipeline_results
        if row.get("status") not in (
            "DONE",
            "DONE_WITH_WARNINGS",
            "DONE_NO_SCIENCE_FRAMES",
        )
    ]
    failed = len(failed_rows)
    science_warnings = sum(1 for row in result.pipeline_results if row.get("science_status") not in (None, "OK"))
    pipeline_warnings = any(
        row.get("status") == "DONE_WITH_WARNINGS" for row in result.pipeline_results
    )
    if failed:
        if len(failed_rows) == len(result.pipeline_results) and all(
            row.get("status") == FAILED_PRECONDITION_ENSEMBLE_STATUS for row in failed_rows
        ):
            result.status = FAILED_PRECONDITION_ENSEMBLE_STATUS
        else:
            result.status = "FAILED"
    elif pipeline_warnings or science_warnings or result.warnings:
        result.status = "DONE_WITH_WARNINGS"
    else:
        result.status = "DONE"
    _write_manifest_if_requested(result, manifest_out, session=session)
    return result


def process_archive_summary_rows(result: ProcessArchiveResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"section": "scope", "metric": "status", "value": result.status},
        {"section": "scope", "metric": "path", "value": result.path},
        {"section": "files", "metric": "discovered", "value": result.discovered_files},
        {"section": "files", "metric": "detected", "value": result.detected_files},
        {"section": "files", "metric": "failed", "value": result.failed_files},
        {"section": "frames", "metric": "indexed_frame_ids", "value": len(result.indexed_frame_ids)},
        {"section": "catalog", "metric": "catalog_name", "value": result.catalog_name},
        {"section": "groups", "metric": "n_groups", "value": len(result.groups)},
        {"section": "series", "metric": "open_series_ids", "value": result.open_series_ids},
    ]
    for item in result.index_results:
        rows.extend(
            [
                {"section": f"index:{item.get('profile_name')}", "metric": "inserted", "value": item.get("inserted")},
                {"section": f"index:{item.get('profile_name')}", "metric": "skipped_existing", "value": item.get("skipped_existing")},
                {"section": f"index:{item.get('profile_name')}", "metric": "failed", "value": item.get("failed")},
            ]
        )
    for i, item in enumerate(result.observation_selection_results, start=1):
        rows.extend(
            [
                {"section": f"observation_selection:{i}", "metric": "mode", "value": item.get("mode")},
                {"section": f"observation_selection:{i}", "metric": "input_frames", "value": item.get("input_frames")},
                {"section": f"observation_selection:{i}", "metric": "selected_frames", "value": item.get("selected_frames")},
                {"section": f"observation_selection:{i}", "metric": "blocked_frames", "value": item.get("blocked_frames")},
                {"section": f"observation_selection:{i}", "metric": "actions", "value": item.get("actions")},
            ]
        )
    for item in result.catalog_results:
        rows.extend(
            [
                {"section": "catalog", "metric": "mode", "value": item.get("mode")},
                {"section": "catalog", "metric": "query_mode", "value": item.get("query_mode")},
                {"section": "catalog", "metric": "effective_query_mode", "value": item.get("effective_query_mode")},
                {"section": "catalog", "metric": "mag_limit", "value": item.get("mag_limit")},
                {"section": "catalog", "metric": "max_radius_deg", "value": item.get("max_radius_deg")},
                {"section": "catalog", "metric": "selected_frames", "value": item.get("selected_frames")},
                {"section": "catalog", "metric": "raw_cones", "value": item.get("raw_cones")},
                {
                    "section": "catalog",
                    "metric": "unique_footprints",
                    "value": item.get("unique_footprints", item.get("cone_count")),
                },
                {"section": "catalog", "metric": "row_limit_reached_cones", "value": item.get("row_limit_reached_cones")},
                {"section": "catalog", "metric": "logical_queries", "value": item.get("logical_queries")},
                {"section": "catalog", "metric": "physical_parts", "value": item.get("physical_parts")},
                {"section": "catalog", "metric": "provider_attempts", "value": item.get("provider_attempts")},
                {
                    "section": "catalog",
                    "metric": "rows_received",
                    "value": item.get("rows_received", item.get("read_rows")),
                },
                {
                    "section": "catalog",
                    "metric": "unique_sources",
                    "value": item.get(
                        "unique_sources",
                        item.get("working_catalog_sources", item.get("n_sources")),
                    ),
                },
                {"section": "catalog", "metric": "provider_failures", "value": item.get("provider_failures")},
                {"section": "catalog", "metric": "inserted_or_updated", "value": item.get("inserted_or_updated")},
                {"section": "catalog", "metric": "failed_frames", "value": item.get("failed_frames")},
                {"section": "catalog", "metric": "failed_footprint_reasons", "value": item.get("failed_footprint_reasons")},
                {"section": "catalog", "metric": "skipped_missing_file", "value": item.get("skipped_missing_file")},
                {"section": "catalog", "metric": "skipped_oversized_cones", "value": item.get("skipped_oversized_cones")},
                {"section": "catalog", "metric": "status", "value": item.get("status")},
            ]
        )
    for i, item in enumerate(result.reference_photometry_results, start=1):
        rows.extend(
            [
                {"section": f"reference:{i}", "metric": "series_id", "value": item.get("series_id")},
                {"section": f"reference:{i}", "metric": "catalog", "value": item.get("reference_catalog_name")},
                {"section": f"reference:{i}", "metric": "status", "value": item.get("status")},
                {"section": f"reference:{i}", "metric": "ready", "value": item.get("ready")},
                {"section": f"reference:{i}", "metric": "required_bands", "value": item.get("required_bands")},
                {"section": f"reference:{i}", "metric": "matched_to_identity", "value": item.get("matched_to_identity")},
            ]
        )
        for metric in (
            "selected_identity_count",
            "already_complete_identity_count",
            "missing_identity_count",
            "cache_hit",
            "cache_miss",
            "cache_invalid",
            "logical_query_groups",
            "provider_requests",
            "provider_response_bytes_received",
            "provider_rows_received",
            "provider_rows_retained",
            "provider_exact_duplicates_collapsed",
            "provider_conflicting_source_ids",
            "provider_conflicting_rows",
            "matched_rows",
            "ambiguous_rows",
            "ambiguous_identities",
            "unmatched_rows",
            "unmatched_identities",
            "imported_or_updated",
            "band_counts_before",
            "band_counts_after",
        ):
            rows.append(
                {
                    "section": f"reference:{i}",
                    "metric": metric,
                    "value": item.get(metric),
                }
            )
    for i, pipe in enumerate(result.pipeline_results, start=1):
        rows.extend(
            [
                {"section": f"pipeline:{i}", "metric": "series_id", "value": pipe.get("series_id")},
                {"section": f"pipeline:{i}", "metric": "field_name", "value": pipe.get("field_name")},
                {"section": f"pipeline:{i}", "metric": "calibration_prefix", "value": pipe.get("calibration_prefix")},
                {"section": f"pipeline:{i}", "metric": "photometry_estimator", "value": (pipe.get("quality_policy") or {}).get("photometry_estimator")},
                {"section": f"pipeline:{i}", "metric": "spatial_ensemble", "value": (pipe.get("quality_policy") or {}).get("spatial_ensemble_enabled")},
                {"section": f"pipeline:{i}", "metric": "spatial_ensemble_gate", "value": (pipe.get("quality_policy") or {}).get("spatial_ensemble_quality_gate_passed")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_enabled")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_series_status", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_series_status")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_frames", "value": {
                    "GOOD": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_good_frames"),
                    "SUSPECT": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_suspect_frames"),
                    "BAD": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_bad_frames"),
                }},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_sigma_frame_diag_median", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_median_sigma_frame_systematic_mag")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_sigma_frame_diag_max", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_max_sigma_frame_systematic_mag")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_absolute_bad_limit", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_absolute_bad_residual_limit_mag")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_spatial_flags", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_frames_with_spatial_systematic")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_psf_summaries", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_frame_psf_summaries")},
                {"section": f"pipeline:{i}", "metric": "photometric_frame_qc_error_policy", "value": (pipe.get("quality_policy") or {}).get("photometric_frame_qc_error_policy")},
                {"section": f"pipeline:{i}", "metric": "candidate_good_frame_confirmation", "value": (pipe.get("quality_policy") or {}).get("candidate_good_frame_confirmation")},
                {"section": f"pipeline:{i}", "metric": "candidate_good_frame_confirmed", "value": (((pipe.get("quality_policy") or {}).get("candidate_good_frame_confirmation") or {}).get("status_counts") or {}).get("GOOD_FRAME_CONFIRMED")},
                {"section": f"pipeline:{i}", "metric": "candidate_systematics_sensitive", "value": (((pipe.get("quality_policy") or {}).get("candidate_good_frame_confirmation") or {}).get("status_counts") or {}).get("SYSTEMATICS_SENSITIVE")},
                {"section": f"pipeline:{i}", "metric": "candidate_insufficient_good_support", "value": (((pipe.get("quality_policy") or {}).get("candidate_good_frame_confirmation") or {}).get("status_counts") or {}).get("INSUFFICIENT_GOOD_FRAME_SUPPORT")},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlation", "value": (pipe.get("quality_policy") or {}).get("candidate_axis_correlation")},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlated_any", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidates_correlated_any"))},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlated_x", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidates_correlated_x"))},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlated_y", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidates_correlated_y"))},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlated_spearman", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidates_correlated_spearman"))},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlated_pearson", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidates_correlated_pearson"))},
                {"section": f"pipeline:{i}", "metric": "candidate_axis_correlation_modes", "value": (((pipe.get("quality_policy") or {}).get("candidate_axis_correlation") or {}).get("candidate_mode_counts"))},
                {"section": f"pipeline:{i}", "metric": "status", "value": pipe.get("status")},
                {"section": f"pipeline:{i}", "metric": "error_message", "value": pipe.get("error_message")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_reference_candidates", "value": pipe.get("base_ensemble_reference_candidates")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_usable_reference_objects", "value": pipe.get("base_ensemble_usable_reference_objects")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_required_references_per_frame", "value": pipe.get("base_ensemble_required_references_per_frame")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_selected_frames", "value": pipe.get("base_ensemble_selected_frames")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_frames_with_offsets", "value": pipe.get("base_ensemble_frames_with_offsets")},
                {"section": f"pipeline:{i}", "metric": "base_ensemble_calibrated", "value": pipe.get("base_ensemble_calibrated")},
                {"section": f"pipeline:{i}", "metric": "science_status", "value": pipe.get("science_status")},
                {"section": f"pipeline:{i}", "metric": "total_points", "value": pipe.get("total_points")},
                {"section": f"pipeline:{i}", "metric": "accepted_points", "value": pipe.get("accepted_points")},
                {"section": f"pipeline:{i}", "metric": "aperture_correction", "value": pipe.get("aperture_correction_calibration")},
                {"section": f"pipeline:{i}", "metric": "psf_diagnostics", "value": pipe.get("psf_summary_json")},
                {"section": f"pipeline:{i}", "metric": "ensemble", "value": pipe.get("ensemble_calibration")},
                {"section": f"pipeline:{i}", "metric": "color_calibration", "value": pipe.get("color_calibration")},
                {"section": f"pipeline:{i}", "metric": "science_calibration", "value": pipe.get("science_calibration")},
                {"section": f"pipeline:{i}", "metric": "target_object_id", "value": (pipe.get("quality_policy") or {}).get("target_object_id")},
                {"section": f"pipeline:{i}", "metric": "target_light_curve", "value": pipe.get("target_light_curve_csv")},
                {"section": f"pipeline:{i}", "metric": "accepted_candidate_count", "value": pipe.get("accepted_candidate_count")},
                {"section": f"pipeline:{i}", "metric": "candidate_light_curves", "value": pipe.get("accepted_candidate_light_curves_dir")},
            ]
        )
    if result.manifest_path:
        rows.append({"section": "outputs", "metric": "manifest", "value": result.manifest_path})
    return rows


def process_archive_group_columns() -> list[str]:
    return [
        "profile_name",
        "instrument",
        "site",
        "tube",
        "camera",
        "filter_name",
        "object_name",
        "n_files",
        "date_min",
        "date_max",
        "mjd_min",
        "mjd_max",
    ]


def process_archive_summary_columns() -> list[str]:
    return ["section", "metric", "value"]


def _normalize_profile_mode(value: str | None) -> str:
    text = (value or "auto").strip().lower()
    if text in {"auto", "master", "robophot"}:
        return "RoboPhot" if text == "robophot" else ("MASTER" if text == "master" else "auto")
    raise ValueError("instrument_profile must be one of: auto, MASTER, RoboPhot")


def _select_profile(header: fits.Header, profiles: dict[str, InstrumentProfile], *, profile_mode: str) -> InstrumentProfile:
    if profile_mode != "auto":
        return profiles[profile_mode]
    scores = {name: _profile_score(header, profile) for name, profile in profiles.items()}
    if _has_any_key(header, {"SERNUM", "OBJRA", "OBJDEC"}) or "ROBO" in _header_text(header, "TELESCOP", "INSTRUME"):
        scores["RoboPhot"] = scores.get("RoboPhot", 0) + 10
    if _has_any_key(header, {"ID_CAM", "AXIS", "MASTERID"}) or "MASTER" in _header_text(header, "TELESCOP", "MASTERID"):
        scores["MASTER"] = scores.get("MASTER", 0) + 10
    best_name = max(scores, key=lambda name: (scores[name], name))
    if scores[best_name] <= 0:
        raise ValueError("cannot detect instrument profile from FITS header")
    return profiles[best_name]


def _profile_score(header: fits.Header, profile: InstrumentProfile) -> int:
    fields = ("date_obs", "filter", "object", "exptime", "ra_center", "dec_center", "site", "tube", "camera")
    score = 0
    for field in fields:
        if profile.get_header_value(header, field) not in (None, ""):
            score += 1
    return score


def _detected_file_from_header(path: Path, header: fits.Header, profile: InstrumentProfile) -> DetectedArchiveFile:
    jd = to_float(profile.get_header_value(header, "jd"))
    mjd = to_float(profile.get_header_value(header, "mjd"))
    if mjd is None and jd is not None:
        mjd = jd - 2400000.5
    date_obs = parse_datetime(profile.get_header_value(header, "date_obs"))
    camera = _str_or_none(profile.get_header_value(header, "camera"))
    stat = path.stat()
    frame_uid = make_frame_uid(path, stat.st_size, stat.st_mtime_ns)

    observation_uid = identity_scheme = native_identity = None
    identity_status = "READY"
    identity_detail = None
    try:
        identity = build_frame_observation_identity(
            SimpleNamespace(
                instrument=profile.name,
                camera=camera,
                date_obs=date_obs,
                header_json=header_to_jsonable(header),
            )
        )
        observation_uid = identity.observation_uid
        identity_scheme = identity.identity_scheme
        native_identity = identity.native_identity
    except ObservationIdentityMissing as exc:
        identity_status = "MISSING"
        identity_detail = str(exc)
    except ObservationIdentityError as exc:
        identity_status = "INVALID"
        identity_detail = str(exc)

    return DetectedArchiveFile(
        path=str(path),
        profile_name=profile.name,
        instrument=profile.name,
        site=_str_or_none(profile.get_header_value(header, "site")),
        tube=_str_or_none(profile.get_header_value(header, "tube")),
        camera=camera,
        filter_name=profile.normalize_filter(profile.get_header_value(header, "filter")),
        object_name=_str_or_none(profile.get_header_value(header, "object")),
        date_obs=date_obs,
        mjd=mjd,
        image_type=profile.normalize_image_type(profile.get_header_value(header, "image_type")),
        frame_uid=frame_uid,
        observation_uid=observation_uid,
        identity_scheme=identity_scheme,
        native_identity=native_identity,
        file_size_bytes=int(stat.st_size),
        naxis1=int(header.get("NAXIS1")) if header.get("NAXIS1") is not None else None,
        naxis2=int(header.get("NAXIS2")) if header.get("NAXIS2") is not None else None,
        observation_identity_status=identity_status,
        observation_identity_detail=identity_detail,
    )


def _science_identity_candidate(item: DetectedArchiveFile) -> ScienceFileIdentityCandidate:
    return ScienceFileIdentityCandidate(
        path=item.path,
        instrument=item.instrument,
        frame_uid=item.frame_uid,
        observation_uid=item.observation_uid,
        identity_scheme=item.identity_scheme,
        native_identity=item.native_identity,
        date_obs=item.date_obs,
        file_size_bytes=item.file_size_bytes,
        naxis1=item.naxis1,
        naxis2=item.naxis2,
        object_name=item.object_name,
        identity_status=item.observation_identity_status,
        identity_detail=item.observation_identity_detail,
        camera=item.camera,
    )


def _observation_selection_warning(summary: dict[str, Any], *, mode: str) -> str | None:
    blocked = int(summary.get("blocked_frames") or 0)
    if blocked <= 0:
        return None
    actions = summary.get("actions") or {}
    return (
        f"observation science-input guard ({mode}): blocked={blocked}, "
        f"already_represented={int(actions.get('ALREADY_REPRESENTED') or 0)}, "
        f"multiple_existing_representatives={int(actions.get('EXISTING_MULTIPLE_REPRESENTATIVES') or 0)}, "
        f"ambiguous_products={int(actions.get('AMBIGUOUS_MULTIPLE_PRODUCTS') or 0)}, "
        f"missing_identity={int(actions.get('MISSING_OBSERVATION_ID') or 0)}, "
        f"identity_conflict={int(actions.get('IDENTITY_CONFLICT') or 0)}"
    )


def _build_group_plans(
    detected: list[DetectedArchiveFile],
    *,
    profiles: dict[str, InstrumentProfile],
    example_limit: int,
) -> list[ArchiveGroupPlan]:
    profile_paths = {profile.name: str(default_profile_paths().get(profile.name, "")) for profile in profiles.values()}
    buckets: dict[tuple[Any, ...], list[DetectedArchiveFile]] = {}
    for item in detected:
        key = (
            item.profile_name,
            item.instrument,
            item.site,
            item.tube,
            item.camera,
            item.filter_name,
            item.object_name,
        )
        buckets.setdefault(key, []).append(item)
    groups: list[ArchiveGroupPlan] = []
    for key, items in sorted(buckets.items(), key=lambda kv: (str(kv[0]), len(kv[1]))):
        dates = [item.date_obs for item in items if item.date_obs is not None]
        mjds = [float(item.mjd) for item in items if item.mjd is not None]
        groups.append(
            ArchiveGroupPlan(
                profile_name=str(key[0]),
                profile_path=profile_paths.get(str(key[0]), ""),
                instrument=key[1],
                site=key[2],
                tube=key[3],
                camera=key[4],
                filter_name=key[5],
                object_name=key[6],
                n_files=len(items),
                date_min=min(dates) if dates else None,
                date_max=max(dates) if dates else None,
                mjd_min=min(mjds) if mjds else None,
                mjd_max=max(mjds) if mjds else None,
                examples=[item.path for item in items[:example_limit]],
            )
        )
    return groups



def _frame_ids_for_paths(session: Session, raw_paths: list[str]) -> list[int]:
    if not raw_paths:
        return []
    stmt = text(
        """
        SELECT fr.frame_id
        FROM frames fr
        JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
        WHERE fl.locator IN :raw_paths
        ORDER BY fr.frame_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    rows = session.execute(stmt, {"raw_paths": raw_paths}).all()
    return [int(row[0]) for row in rows]


def _frame_ids_by_instrument_unit(session: Session, frame_ids: list[int]) -> dict[int, list[int]]:
    if not frame_ids:
        return {}
    stmt = text(
        """
        SELECT instrument_unit_id, frame_id
        FROM frames
        WHERE frame_id IN :frame_ids AND instrument_unit_id IS NOT NULL
        ORDER BY instrument_unit_id, frame_id
        """
    ).bindparams(bindparam("frame_ids", expanding=True))
    out: dict[int, list[int]] = {}
    for row in session.execute(stmt, {"frame_ids": frame_ids}).all():
        out.setdefault(int(row[0]), []).append(int(row[1]))
    return out



_REFERENCE_PHOTOMETRY_CACHE_SCHEMA_VERSION = 3
_REFERENCE_PHOTOMETRY_PROVIDER_RELEASE = "dr2"
_REFERENCE_PHOTOMETRY_MATCH_RADIUS_ARCSEC = 1.0
_REFERENCE_PROVIDER_DIAGNOSTIC_KEYS = (
    "logical_query_groups",
    "provider_requests",
    "provider_response_bytes_received",
    "provider_rows_received",
    "provider_rows_retained",
    "provider_exact_duplicates_collapsed",
    "provider_conflicting_source_ids",
    "provider_conflicting_rows",
    "matched_rows",
    "ambiguous_rows",
    "ambiguous_identities",
    "unmatched_rows",
    "unmatched_identities",
)


@dataclass(frozen=True)
class _ValidatedReferenceCacheEntry:
    csv_path: Path
    manifest_path: Path
    manifest: dict[str, Any]


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selected_reference_grouping_algorithm() -> dict[str, Any]:
    provider_module = reference_photometry_module
    return {
        "name": "greedy_capped_spherical_cones",
        "version": 1,
        "limits": {
            "max_selected_identities": int(provider_module._MAX_SELECTED_IDENTITIES),
            "max_selected_groups": int(provider_module._MAX_SELECTED_GROUPS),
            "max_request_radius_deg": float(provider_module._MAX_SELECTED_REQUEST_RADIUS_DEG),
            "max_response_bytes_per_request": int(
                provider_module._MAX_SELECTED_RESPONSE_BYTES_PER_REQUEST
            ),
            "max_response_bytes_total": int(
                provider_module._MAX_SELECTED_RESPONSE_BYTES_TOTAL
            ),
            "max_response_rows": int(provider_module._MAX_SELECTED_RESPONSE_ROWS),
            "max_candidate_comparisons": int(
                provider_module._MAX_SELECTED_CANDIDATE_COMPARISONS
            ),
            "radius_decimal_places": int(provider_module._PANSTARRS_RADIUS_DECIMAL_PLACES),
        },
    }


def _selected_reference_cache_fingerprint(
    *,
    provider: str,
    provider_release: str,
    identity_catalog_name: str,
    missing_identity_source_ids: Iterable[str],
    persisted_identity_sources: Iterable[dict[str, Any]],
    required_bands: Iterable[str],
    filters: dict[str, Any],
    match_radius_arcsec: float,
    grouping_algorithm: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    identity_ids = sorted(
        {str(source_id).strip() for source_id in missing_identity_source_ids if str(source_id).strip()}
    )
    position_by_id: dict[str, dict[str, Any]] = {}
    for source in persisted_identity_sources:
        source_id = str(source.get("source_id", "")).strip()
        if not source_id:
            continue
        ra_deg = float(source["ra_deg"])
        dec_deg = float(source["dec_deg"])
        if (
            not math.isfinite(ra_deg)
            or not math.isfinite(dec_deg)
            or not (0.0 <= ra_deg < 360.0)
            or not (-90.0 <= dec_deg <= 90.0)
        ):
            raise ValueError("Persisted selected reference identity positions are invalid")
        if source_id in position_by_id:
            raise ValueError("Persisted selected reference identity positions are not unique")
        position_by_id[source_id] = {
            "source_id": source_id,
            "ra_deg": ra_deg,
            "dec_deg": dec_deg,
        }
    if set(position_by_id) != set(identity_ids):
        raise ValueError("Persisted positions do not match the selected missing reference identities")

    identity_positions = [position_by_id[source_id] for source_id in identity_ids]
    identity_set_sha256 = _canonical_json_sha256(identity_ids)
    identity_positions_sha256 = _canonical_json_sha256(identity_positions)
    provenance = {
        "schema_version": _REFERENCE_PHOTOMETRY_CACHE_SCHEMA_VERSION,
        "cache_schema_version": _REFERENCE_PHOTOMETRY_CACHE_SCHEMA_VERSION,
        "provider": str(provider),
        "provider_release": str(provider_release),
        "identity_catalog_name": str(identity_catalog_name),
        "identity_count": len(identity_ids),
        "identity_set_sha256": identity_set_sha256,
        "identity_positions_sha256": identity_positions_sha256,
        "required_bands": sorted(
            {str(band).strip().lower() for band in required_bands if str(band).strip()}
        ),
        "filters": filters,
        "match_radius_arcsec": float(match_radius_arcsec),
        "grouping_algorithm": grouping_algorithm or _selected_reference_grouping_algorithm(),
    }
    return _canonical_json_sha256(provenance), provenance


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reference_cache_csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream)
        try:
            next(rows)
        except StopIteration as exc:
            raise ValueError("cached reference CSV is empty") from exc
        return sum(1 for _ in rows)


def _validate_selected_reference_cache(
    *,
    csv_path: Path,
    manifest_path: Path,
    fingerprint: str,
    expected_provenance: dict[str, Any],
) -> dict[str, Any]:
    if not csv_path.is_file() or csv_path.is_symlink():
        raise ValueError("cached reference CSV is not a regular file")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("cached reference manifest is not a regular file")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("cached reference manifest is not a JSON object")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != _REFERENCE_PHOTOMETRY_CACHE_SCHEMA_VERSION
    ):
        raise ValueError("cached reference manifest schema version mismatch")
    if manifest.get("fingerprint") != fingerprint:
        raise ValueError("cached reference manifest fingerprint mismatch")
    if manifest.get("complete") is not True:
        raise ValueError("cached reference manifest is incomplete")
    for key, expected_value in expected_provenance.items():
        if key == "identity_count" and type(manifest.get(key)) is not int:
            raise ValueError("cached reference manifest has invalid identity_count")
        if manifest.get(key) != expected_value:
            raise ValueError(f"cached reference manifest provenance mismatch for {key}")
    provider_diagnostics = manifest.get("selected_reference_diagnostics")
    if (
        not isinstance(provider_diagnostics, dict)
        or set(provider_diagnostics) != set(_REFERENCE_PROVIDER_DIAGNOSTIC_KEYS)
    ):
        raise ValueError("cached reference manifest has an invalid selected-reference diagnostic contract")
    for key in _REFERENCE_PROVIDER_DIAGNOSTIC_KEYS:
        value = provider_diagnostics.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"cached reference manifest has invalid {key}")
    for manifest_key, diagnostic_key in (
        ("logical_query_groups", "logical_query_groups"),
        ("provider_requests", "provider_requests"),
        ("rows_received", "provider_rows_received"),
        ("retained_rows", "provider_rows_retained"),
    ):
        value = manifest.get(manifest_key)
        if type(value) is not int or value < 0:
            raise ValueError(f"cached reference manifest has invalid {manifest_key}")
        if value != provider_diagnostics[diagnostic_key]:
            raise ValueError(
                f"cached reference manifest {manifest_key} does not match selected-reference diagnostics"
            )
    if provider_diagnostics["provider_requests"] != provider_diagnostics["logical_query_groups"]:
        raise ValueError("cached reference manifest provider/group counts do not match")
    if provider_diagnostics["provider_rows_retained"] > provider_diagnostics["provider_rows_received"]:
        raise ValueError("cached reference manifest retained-row count exceeds rows received")
    if provider_diagnostics["provider_rows_retained"] > provider_diagnostics["matched_rows"]:
        raise ValueError("cached reference manifest retained-row count exceeds matched rows")
    if provider_diagnostics["matched_rows"] > provider_diagnostics["provider_rows_received"]:
        raise ValueError("cached reference manifest matched-row count exceeds rows received")
    if provider_diagnostics["provider_exact_duplicates_collapsed"] > provider_diagnostics[
        "provider_rows_received"
    ]:
        raise ValueError("cached reference manifest duplicate-row count exceeds rows received")
    if provider_diagnostics["provider_conflicting_rows"] > provider_diagnostics[
        "provider_rows_received"
    ]:
        raise ValueError("cached reference manifest conflicting-row count exceeds rows received")
    manifest_csv_sha256 = manifest.get("csv_sha256")
    if not isinstance(manifest_csv_sha256, str) or len(manifest_csv_sha256) != 64:
        raise ValueError("cached reference manifest has invalid CSV SHA-256")
    if _file_sha256(csv_path) != manifest_csv_sha256:
        raise ValueError("cached reference CSV SHA-256 mismatch")
    if _reference_cache_csv_row_count(csv_path) != provider_diagnostics["provider_rows_retained"]:
        raise ValueError("cached reference CSV row count mismatch")
    return manifest


def _new_reference_cache_temp_path(cache_dir: Path, *, fingerprint: str, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".panstarrs_dr2_{fingerprint}.",
        suffix=suffix,
        dir=cache_dir,
    )
    os.close(descriptor)
    return Path(name)


def _fsync_reference_cache_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _publish_reference_cache_file_no_overwrite(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise FileExistsError(f"Reference cache artifact already exists: {destination}")
    # On Windows os.rename is an atomic same-volume move and fails if dst exists.
    # That no-replace behavior prevents concurrent runs from overwriting a cache.
    os.rename(source, destination)


def _ensure_reference_photometry_for_series(
    session: Session,
    *,
    series: dict[str, Any],
    policy: ColorCalibrationPolicy,
    identity_catalog_name: str,
    selected_identity_source_ids: Iterable[str] | None,
    out_dir: str | Path | None,
    allow_remote_fetch: bool,
    process_local_cache: dict[str, _ValidatedReferenceCacheEntry] | None = None,
) -> dict[str, Any]:
    """Ensure that a series has enough matched reference-band photometry.

    Existing imported reference rows are reused.  When coverage is insufficient
    and remote acquisition is allowed, the provider CSV is fetched, normalized,
    imported idempotently, and coverage is checked again.  Any provider/network
    failure is returned as a non-fatal result so the archive pipeline can retain
    its internal ensemble calibration as the science fallback.
    """

    series_id = int(series["series_id"])
    field_id = int(series["field_id"]) if series.get("field_id") is not None else None
    required_bands = _required_reference_bands(policy)
    minimum_sources = max(1, int(policy.min_stars_per_frame))
    provider_catalog_name = str(policy.reference_catalog_name)
    scoped_catalog_name = identity_scoped_reference_catalog_name(
        provider_catalog_name,
        identity_catalog_name,
    )
    base: dict[str, Any] = {
        "series_id": series_id,
        "field_id": field_id,
        "identity_catalog_name": identity_catalog_name,
        "reference_provider_name": provider_catalog_name,
        "reference_catalog_name": provider_catalog_name,
        "scoped_reference_catalog_name": scoped_catalog_name,
        "photometric_system": policy.photometric_system,
        "required_bands": required_bands,
        "minimum_sources_per_band": minimum_sources,
        "allow_remote_fetch": bool(allow_remote_fetch),
        "ready": False,
        "selected_identity_count": 0,
        "already_complete_identity_count": 0,
        "missing_identity_count": 0,
        "cache_hit": False,
        "cache_miss": False,
        "cache_invalid": False,
        "logical_query_groups": None,
        "provider_requests": None,
        "provider_response_bytes_received": None,
        "provider_rows_received": None,
        "provider_rows_retained": None,
        "provider_exact_duplicates_collapsed": None,
        "provider_conflicting_source_ids": None,
        "provider_conflicting_rows": None,
        "matched_rows": None,
        "ambiguous_rows": None,
        "ambiguous_identities": None,
        "unmatched_rows": None,
        "unmatched_identities": None,
        "imported_or_updated": None,
        "band_counts_before": {},
        "band_counts_after": {},
    }

    if selected_identity_source_ids is None:
        return {
            **base,
            "status": "SKIPPED_SELECTED_IDENTITY_SET_UNAVAILABLE",
            "reason": "reference acquisition requires identities selected and persisted by this Stage-2 run",
        }
    selected_ids = sorted({str(source_id).strip() for source_id in selected_identity_source_ids if str(source_id).strip()})
    base["selected_identity_count"] = len(selected_ids)
    if not selected_ids:
        return {
            **base,
            "status": "SKIPPED_NO_SELECTED_IDENTITIES",
            "reason": "Stage-2 selected no persisted identity sources for reference acquisition",
        }
    try:
        persisted_identity_sources = load_persisted_identity_sources(
            session,
            identity_catalog_name=identity_catalog_name,
            source_ids=selected_ids,
        )
    except Exception as exc:  # noqa: BLE001 - optional reference refinement fails closed.
        session.rollback()
        return {
            **base,
            "status": "SKIPPED_SELECTED_IDENTITIES_NOT_PERSISTED",
            "reason": f"{type(exc).__name__}: selected identities are not fully persisted",
        }

    try:
        before_counts = _reference_band_counts_for_series(
            session,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=provider_catalog_name,
            selected_identity_source_ids=selected_ids,
        )
    except Exception as exc:  # noqa: BLE001 - optional reference layer must not destroy ensemble output.
        session.rollback()
        return {
            **base,
            "status": "REFERENCE_COVERAGE_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    base["band_counts_before"] = before_counts
    base["band_counts_after"] = before_counts
    if _reference_band_coverage_ready(before_counts, required_bands, minimum_sources):
        try:
            global_coverage_by_identity = _reference_band_coverage_by_identity(
                session,
                identity_catalog_name=identity_catalog_name,
                reference_catalog_name=provider_catalog_name,
                selected_identity_source_ids=selected_ids,
                required_bands=required_bands,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the existing global-readiness gate.
            session.rollback()
            return {
                **base,
                "status": "REFERENCE_COVERAGE_CHECK_FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        _set_reference_identity_coverage_telemetry(
            base, global_coverage_by_identity, selected_ids, required_bands
        )
        return {
            **base,
            "status": "READY_EXISTING",
            "ready": True,
            "effective_reference_catalog_name": provider_catalog_name,
            "reference_namespace_mode": "existing_global",
        }

    try:
        scoped_counts = _reference_band_counts_for_series(
            session,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=scoped_catalog_name,
            selected_identity_source_ids=selected_ids,
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {
            **base,
            "status": "SCOPED_REFERENCE_COVERAGE_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    base["band_counts_before"] = scoped_counts
    base["band_counts_after"] = scoped_counts
    try:
        scoped_coverage_by_identity = _reference_band_coverage_by_identity(
            session,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=scoped_catalog_name,
            selected_identity_source_ids=selected_ids,
            required_bands=required_bands,
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {
            **base,
            "status": "SCOPED_REFERENCE_COVERAGE_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    missing_identity_source_ids = _set_reference_identity_coverage_telemetry(
        base, scoped_coverage_by_identity, selected_ids, required_bands
    )
    if _reference_band_coverage_ready(scoped_counts, required_bands, minimum_sources):
        return {
            **base,
            "status": "READY_EXISTING_SCOPED",
            "ready": True,
            "effective_reference_catalog_name": scoped_catalog_name,
            "reference_namespace_mode": "identity_scoped",
        }

    if not missing_identity_source_ids:
        return {
            **base,
            "status": "INSUFFICIENT_REFERENCE_COVERAGE",
            "reason": (
                "all selected identities have the required bands, but the scoped catalog "
                "still has fewer sources than the configured minimum"
            ),
        }

    if not allow_remote_fetch:
        return {
            **base,
            "status": "MISSING_REFERENCE_PHOTOMETRY",
            "reason": "required reference photometry is not imported and automatic fetch is disabled",
        }
    if provider_catalog_name != "panstarrs_dr2":
        return {
            **base,
            "status": "UNSUPPORTED_AUTO_PROVIDER",
            "reason": f"automatic acquisition is not implemented for {provider_catalog_name!r}",
        }

    target_dir = (
        Path(out_dir) / "reference_photometry"
        if out_dir
        else Path("exports") / "reference_photometry"
    )
    max_mag_err = None
    if policy.max_reference_mag_err is not None:
        max_mag_err = {band: float(policy.max_reference_mag_err) for band in required_bands}
    min_mag = {str(policy.reference_band): float(policy.min_reference_mag)} if policy.min_reference_mag is not None else None
    max_mag = {str(policy.reference_band): float(policy.max_reference_mag)} if policy.max_reference_mag is not None else None
    match_radius_arcsec = _REFERENCE_PHOTOMETRY_MATCH_RADIUS_ARCSEC
    filters = {
        "require_bands": sorted(set(required_bands)),
        "min_mag": min_mag,
        "max_mag": max_mag,
        "max_mag_err": max_mag_err,
        "min_detections": None,
        "use_remote_filters": True,
        "limit": None,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    missing_identity_ids = set(missing_identity_source_ids)
    fingerprint, cache_provenance = _selected_reference_cache_fingerprint(
        provider=provider_catalog_name,
        provider_release=_REFERENCE_PHOTOMETRY_PROVIDER_RELEASE,
        identity_catalog_name=identity_catalog_name,
        missing_identity_source_ids=missing_identity_source_ids,
        persisted_identity_sources=(
            source
            for source in persisted_identity_sources
            if str(source.get("source_id", "")).strip()
            in missing_identity_ids
        ),
        required_bands=required_bands,
        filters=filters,
        match_radius_arcsec=match_radius_arcsec,
    )
    csv_path = target_dir / f"panstarrs_dr2_{fingerprint}.csv"
    manifest_path = target_dir / f"panstarrs_dr2_{fingerprint}.json"
    cache_manifest: dict[str, Any] | None = None
    cache_hit = False
    local_cache_entry_reused = False
    local_entry = (
        process_local_cache.get(fingerprint)
        if process_local_cache is not None
        else None
    )
    if (
        isinstance(local_entry, _ValidatedReferenceCacheEntry)
        and local_entry.csv_path == csv_path
        and local_entry.manifest_path == manifest_path
    ):
        cache_manifest = json.loads(json.dumps(local_entry.manifest))
        cache_hit = True
        local_cache_entry_reused = True
    else:
        cache_exists = (os.path.lexists(csv_path), os.path.lexists(manifest_path))
    if not cache_hit:
        if any(cache_exists):
            try:
                if not all(cache_exists):
                    raise ValueError("fingerprinted CSV/manifest pair is incomplete")
                cache_manifest = _validate_selected_reference_cache(
                    csv_path=csv_path,
                    manifest_path=manifest_path,
                    fingerprint=fingerprint,
                    expected_provenance=cache_provenance,
                )
                cache_hit = True
            except Exception as exc:  # noqa: BLE001 - invalid deterministic artifacts must fail closed.
                base["cache_invalid"] = True
                return {
                    **base,
                    "status": "REFERENCE_CACHE_INVALID",
                    "reason": f"invalid existing reference cache: {type(exc).__name__}: {exc}",
                    "csv_path": str(csv_path),
                    "manifest_path": str(manifest_path),
                    "cache_fingerprint": fingerprint,
                }

    base["cache_hit"] = cache_hit
    base["cache_miss"] = not cache_hit
    cache_details: dict[str, Any] = {
        "csv_path": str(csv_path),
        "manifest_path": str(manifest_path),
        "cache_fingerprint": fingerprint,
    }
    if cache_manifest is not None:
        base.update(cache_manifest["selected_reference_diagnostics"])
        if process_local_cache is not None and not local_cache_entry_reused:
            process_local_cache[fingerprint] = _ValidatedReferenceCacheEntry(
                csv_path=csv_path,
                manifest_path=manifest_path,
                manifest=json.loads(json.dumps(cache_manifest)),
            )

    try:
        if not cache_hit:
            csv_temp_path = _new_reference_cache_temp_path(
                target_dir,
                fingerprint=fingerprint,
                suffix=".csv.tmp",
            )
            fetched = create_reference_photometry_csv(
                session,
                out_path=csv_temp_path,
                provider=provider_catalog_name,
                # The exclusively-created temporary file is ours; published cache paths
                # are always moved with no-replace semantics below.
                overwrite=True,
                require_bands=required_bands,
                min_mag=min_mag,
                max_mag=max_mag,
                max_mag_err=max_mag_err,
                min_detections=None,
                use_remote_filters=True,
                selected_identity_catalog_name=identity_catalog_name,
                selected_identity_source_ids=missing_identity_source_ids,
                selected_identity_match_radius_arcsec=match_radius_arcsec,
            )
            if not csv_temp_path.is_file() or csv_temp_path.is_symlink():
                raise RuntimeError("reference provider did not finish its temporary CSV")
            provider_diagnostics = (getattr(fetched, "filter_summary", None) or {}).get(
                "selected_reference_diagnostics"
            )
            if (
                not isinstance(provider_diagnostics, dict)
                or set(provider_diagnostics) != set(_REFERENCE_PROVIDER_DIAGNOSTIC_KEYS)
            ):
                raise RuntimeError("reference provider omitted selected-acquisition diagnostics")
            if any(
                type(value) is not int or value < 0
                for value in provider_diagnostics.values()
            ):
                raise RuntimeError("reference provider returned invalid selected-reference counters")
            if provider_diagnostics["provider_requests"] != provider_diagnostics[
                "logical_query_groups"
            ]:
                raise RuntimeError("reference provider request/group counts do not match")
            provider_raw_rows = getattr(fetched, "raw_rows", None)
            if (
                type(provider_raw_rows) is not int
                or provider_raw_rows != provider_diagnostics["provider_rows_received"]
            ):
                raise RuntimeError("reference provider row diagnostics do not match rows received")
            provider_rows_retained = getattr(fetched, "written_rows", None)
            if (
                type(provider_rows_retained) is not int
                or provider_rows_retained != provider_diagnostics["provider_rows_retained"]
            ):
                raise RuntimeError("reference provider returned an invalid retained-row count")
            if _reference_cache_csv_row_count(csv_temp_path) != provider_rows_retained:
                raise RuntimeError("completed reference CSV row count does not match provider diagnostics")
            _fsync_reference_cache_file(csv_temp_path)
            cache_manifest = {
                **cache_provenance,
                "fingerprint": fingerprint,
                "logical_query_groups": provider_diagnostics["logical_query_groups"],
                "provider_requests": provider_diagnostics["provider_requests"],
                "rows_received": provider_diagnostics["provider_rows_received"],
                "retained_rows": provider_diagnostics["provider_rows_retained"],
                "selected_reference_diagnostics": dict(provider_diagnostics),
                "csv_sha256": _file_sha256(csv_temp_path),
                "complete": True,
            }
            manifest_temp_path = _new_reference_cache_temp_path(
                target_dir,
                fingerprint=fingerprint,
                suffix=".json.tmp",
            )
            manifest_payload = json.dumps(
                cache_manifest,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ) + "\n"
            with manifest_temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(manifest_payload)
                stream.flush()
                os.fsync(stream.fileno())
            _publish_reference_cache_file_no_overwrite(csv_temp_path, csv_path)
            _publish_reference_cache_file_no_overwrite(manifest_temp_path, manifest_path)
            cache_manifest = _validate_selected_reference_cache(
                csv_path=csv_path,
                manifest_path=manifest_path,
                fingerprint=fingerprint,
                expected_provenance=cache_provenance,
            )
            base.update(cache_manifest["selected_reference_diagnostics"])
            if process_local_cache is not None:
                process_local_cache[fingerprint] = _ValidatedReferenceCacheEntry(
                    csv_path=csv_path,
                    manifest_path=manifest_path,
                    manifest=json.loads(json.dumps(cache_manifest)),
                )

        if cache_manifest is None:
            raise RuntimeError("reference cache manifest was not available after acquisition")
        cache_details["cache_provenance"] = cache_manifest
        imported = import_reference_photometry(
            session,
            path=csv_path,
            catalog_name=scoped_catalog_name,
            catalog_version=_REFERENCE_PHOTOMETRY_PROVIDER_RELEASE,
            photometric_system=policy.photometric_system or "ps1_ab",
            source_priority=10,
            ra_col="ra",
            dec_col="dec",
            source_id_col="catalog_source_id",
            mag_cols={
                "g": "mag_g",
                "r": "mag_r",
                "b": "mag_b",
                "v": "mag_v",
                "i": "mag_i",
                "z": "mag_z",
                "y": "mag_y",
            },
            mag_err_cols={
                "g": "err_g",
                "r": "err_r",
                "b": "err_b",
                "v": "err_v",
                "i": "err_i",
                "z": "err_z",
                "y": "err_y",
            },
            color_cols={"g_r": "color_g_r", "r_i": "color_r_i", "i_z": "color_i_z"},
            quality_cols={
                "n_detections": "n_detections",
                "quality_flag": "quality_flag",
                "obj_info_flag": "obj_info_flag",
                "synthetic_bv_transform": "synthetic_bv_transform",
            },
            match_to_catalog=identity_catalog_name,
            max_match_arcsec=match_radius_arcsec,
            selected_identity_source_ids=missing_identity_source_ids,
            requested_identity_source_id_col="selected_identity_source_id",
            commit=False,
        )
        base["imported_or_updated"] = int(imported.inserted_or_updated)
    except Exception as exc:  # noqa: BLE001 - reference acquisition is an optional science refinement.
        session.rollback()
        return {
            **base,
            **cache_details,
            "status": "REFERENCE_ACQUISITION_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    try:
        after_counts = _reference_band_counts_for_series(
            session,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=scoped_catalog_name,
            selected_identity_source_ids=selected_ids,
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {
            **base,
            **cache_details,
            "status": "REFERENCE_POST_IMPORT_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
            "matched_to_identity": imported.matched_to_gaia,
        }
    ready = _reference_band_coverage_ready(after_counts, required_bands, minimum_sources)
    base["band_counts_after"] = after_counts
    return {
        **base,
        **cache_details,
        "status": "FETCHED_IMPORTED" if ready else "INSUFFICIENT_REFERENCE_COVERAGE",
        "ready": ready,
        "reason": None if ready else "reference import completed but too few matched sources remain",
        "matched_to_identity": imported.matched_to_gaia,
        "unmatched_to_identity": imported.unmatched_to_gaia,
        "effective_reference_catalog_name": scoped_catalog_name,
        "reference_namespace_mode": "identity_scoped",
    }


def _required_reference_bands(policy: ColorCalibrationPolicy) -> list[str]:
    bands: list[str] = []
    for value in policy.require_bands:
        band = str(value).strip().lower()
        if band and band not in bands:
            bands.append(band)
    if policy.reference_band:
        band = str(policy.reference_band).strip().lower()
        if band and band not in bands:
            bands.append(band)
    if policy.color_index:
        for part in re.split(r"\s*[-_/]\s*", str(policy.color_index).strip().lower()):
            if part and part not in bands:
                bands.append(part)
    return bands


def _reference_band_counts_for_series(
    session: Session,
    *,
    identity_catalog_name: str,
    reference_catalog_name: str,
    selected_identity_source_ids: Iterable[str],
) -> dict[str, int]:
    """Count reference bands only for the exact Stage-2 selected identity set."""

    selected_ids = sorted({str(source_id).strip() for source_id in selected_identity_source_ids if str(source_id).strip()})
    if not selected_ids:
        return {}
    stmt = text(
        """
        SELECT
            pb.canonical_name AS band,
            COUNT(DISTINCT rps.refphot_source_id) AS n_sources
        FROM reference_photometry_sources rps
        JOIN reference_photometry_catalogs rpc ON rpc.refcat_id = rps.refcat_id
        JOIN reference_photometry_points rpp ON rpp.refphot_source_id = rps.refphot_source_id
        JOIN photometric_bands pb ON pb.band_id = rpp.band_id
        WHERE rpc.catalog_name = :reference_catalog_name
          AND rps.identity_catalog_name = :identity_catalog_name
          AND rps.identity_source_id IN :selected_identity_source_ids
        GROUP BY pb.canonical_name
        """
    ).bindparams(bindparam("selected_identity_source_ids", expanding=True))
    rows = session.execute(
        stmt,
        {
            "reference_catalog_name": reference_catalog_name,
            "identity_catalog_name": identity_catalog_name,
            "selected_identity_source_ids": selected_ids,
        },
    ).mappings().all()
    return {str(row["band"]): int(row["n_sources"] or 0) for row in rows}


_REFERENCE_COVERAGE_QUERY_CHUNK_SIZE = 500


def _reference_band_coverage_by_identity(
    session: Session,
    *,
    identity_catalog_name: str,
    reference_catalog_name: str,
    selected_identity_source_ids: Iterable[str],
    required_bands: list[str],
) -> dict[str, set[str]]:
    """Return normalized required-band coverage for each selected identity.

    The selected identity set is queried in bounded chunks. Identities without
    any matching reference points remain present with an empty band set.
    """

    selected_ids = sorted(
        {str(source_id).strip() for source_id in selected_identity_source_ids if str(source_id).strip()}
    )
    required = sorted({str(band).strip().lower() for band in required_bands if str(band).strip()})
    coverage = {source_id: set() for source_id in selected_ids}
    if not selected_ids or not required:
        return coverage

    stmt = text(
        """
        SELECT DISTINCT
            rps.identity_source_id AS identity_source_id,
            pb.canonical_name AS band
        FROM reference_photometry_sources rps
        JOIN reference_photometry_catalogs rpc ON rpc.refcat_id = rps.refcat_id
        JOIN reference_photometry_points rpp ON rpp.refphot_source_id = rps.refphot_source_id
        JOIN photometric_bands pb ON pb.band_id = rpp.band_id
        WHERE rpc.catalog_name = :reference_catalog_name
          AND rps.identity_catalog_name = :identity_catalog_name
          AND rps.identity_source_id IN :selected_identity_source_ids
          AND pb.canonical_name IN :required_bands
        """
    ).bindparams(
        bindparam("selected_identity_source_ids", expanding=True),
        bindparam("required_bands", expanding=True),
    )
    chunk_size = max(1, int(_REFERENCE_COVERAGE_QUERY_CHUNK_SIZE))
    for offset in range(0, len(selected_ids), chunk_size):
        chunk = selected_ids[offset : offset + chunk_size]
        result = session.execute(
            stmt,
            {
                "reference_catalog_name": reference_catalog_name,
                "identity_catalog_name": identity_catalog_name,
                "selected_identity_source_ids": chunk,
                "required_bands": required,
            },
        )
        rows = result.mappings().all() if hasattr(result, "mappings") else result.all()
        for row in rows:
            if hasattr(row, "_mapping"):
                row = row._mapping
            if hasattr(row, "keys"):
                source_id = str(row["identity_source_id"])
                band = str(row["band"]).strip().lower()
            else:
                source_id = str(row[0])
                band = str(row[1]).strip().lower()
            if source_id in coverage and band in required:
                coverage[source_id].add(band)
    return coverage


def _set_reference_identity_coverage_telemetry(
    result: dict[str, Any],
    coverage_by_identity: dict[str, set[str]],
    selected_identity_source_ids: list[str],
    required_bands: list[str],
) -> list[str]:
    """Record bounded selected-identity completeness counts and return missing IDs."""

    required = set(required_bands)
    if required:
        missing_ids = [
            source_id
            for source_id in selected_identity_source_ids
            if not required.issubset(coverage_by_identity.get(source_id, set()))
        ]
    else:
        missing_ids = list(selected_identity_source_ids)
    result["already_complete_identity_count"] = len(selected_identity_source_ids) - len(missing_ids)
    result["missing_identity_count"] = len(missing_ids)
    return missing_ids


def _reference_band_coverage_ready(counts: dict[str, int], required_bands: list[str], minimum_sources: int) -> bool:
    return bool(required_bands) and all(int(counts.get(band, 0)) >= int(minimum_sources) for band in required_bands)


def _merge_current_series_ids(created_series_ids: list[int], existing_series_ids: list[int]) -> list[int]:
    return sorted({int(v) for v in [*created_series_ids, *existing_series_ids] if v is not None})



def _effective_auto_catalog_mag_limit(*, explicit: float | None, working: float | None) -> float | None:
    return explicit if explicit is not None else working


def _effective_auto_catalog_max_radius_deg(
    groups: list[ArchiveGroupPlan], *, explicit: float | None
) -> float:
    if explicit is not None:
        return float(explicit)
    # Full-frame MASTER images are ~2.1 deg across, requiring a ~1.52 deg
    # corner-covering cone including the default margin. Smaller instruments
    # retain the historical 1 deg guard.
    if any((group.profile_name or group.instrument or "").strip().upper() == "MASTER" for group in groups):
        return 1.6
    return 1.0

def _default_auto_catalog_name(groups: list[ArchiveGroupPlan], *, root: str | Path) -> str:
    if groups:
        first = sorted(groups, key=lambda g: (g.date_min or "", g.profile_name, g.object_name or ""))[0]
        date = _date_token(first.date_min, first.mjd_min)
        instrument = _slug(first.profile_name or first.instrument or "archive").lower()
        object_name = _slug(first.object_name or Path(root).name or "field")
        filters = sorted({_slug(g.filter_name or "filter") for g in groups})
        filter_token = filters[0] if len(filters) == 1 else "mixed"
        return f"auto_catalog_{instrument}_{date}_{object_name}_{filter_token}"
    return f"auto_catalog_archive_{_slug(Path(root).name or 'field')}"



def _inspect_existing_catalog(session: Session, *, catalog_name: str) -> dict[str, Any]:
    from sqlalchemy import func, select
    from grandgousier.db.models import CatalogSource

    n_sources = int(
        session.execute(
            select(func.count())
            .select_from(CatalogSource)
            .where(CatalogSource.catalog_name == catalog_name)
        ).scalar_one()
        or 0
    )
    return {
        "mode": "explicit_existing",
        "catalog_name": catalog_name,
        "status": "DONE" if n_sources > 0 else "EMPTY",
        "inserted_or_updated": n_sources,
        "n_sources": n_sources,
    }


def _working_catalog_hash_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _working_catalog_frame_scope_sha256(frame_ids: Iterable[int]) -> str:
    return _working_catalog_hash_json(sorted({int(frame_id) for frame_id in frame_ids}))


def _working_catalog_footprint_sha256(gaia_query: Any, cones: Iterable[Any]) -> str:
    """Hash the exact normalized WCS-cone union sent to the transient Gaia fetch."""

    normalized_cones = gaia_query._normalise_working_cones(cones)
    exact_float_payload = [
        [float(value).hex() for value in cone]
        for cone in normalized_cones
    ]
    return _working_catalog_hash_json(exact_float_payload)


def _auto_working_catalog_fetch_provenance(
    *,
    gaia_query: Any,
    field_id: int,
    frame_ids: list[int],
    cones: list[tuple[float, float, float]],
    margin_arcmin: float,
    mag_limit: float | None,
    max_radius_deg: float,
    maxrec: int,
    diagnostics: dict[str, Any],
    fetch_status: str,
    fetch_complete: bool,
    unique_rows: int | None,
    elapsed_seconds: float | None,
) -> dict[str, Any]:
    provider_failures = diagnostics.get("provider_failures") or []
    providers_used = []
    providers_attempted = []
    for provider in diagnostics.get("providers_used") or []:
        if isinstance(provider, str) and provider and provider not in providers_used:
            providers_used.append(provider)
            if provider not in providers_attempted:
                providers_attempted.append(provider)
    for failure in provider_failures:
        if not isinstance(failure, dict):
            continue
        provider = failure.get("provider")
        if isinstance(provider, str) and provider and provider not in providers_attempted:
            providers_attempted.append(provider)
    overflow_events = diagnostics.get("overflow_events") or []
    overflow_statuses = sorted(
        {
            str(event["query_status"]).strip().upper()
            for event in overflow_events
            if isinstance(event, dict) and event.get("query_status")
        }
    )
    rows_received = int(diagnostics.get("rows_received") or 0)
    duplicate_rows = None
    if unique_rows is not None:
        unique_rows = int(unique_rows)
        if unique_rows > rows_received:
            raise ValueError("Gaia fetch unique rows exceed rows received")
        duplicate_rows = rows_received - unique_rows
    partial_rows_discarded = bool(diagnostics.get("partial_rows_discarded", False))
    complete = bool(fetch_complete and not partial_rows_discarded)
    raw_cones = int(diagnostics.get("raw_cones", len(cones)))
    unique_footprints = diagnostics.get("unique_footprints")
    if unique_footprints is None:
        unique_footprints = diagnostics.get("cone_count")
    if unique_footprints is None:
        unique_footprints = len(gaia_query._normalise_working_cones(cones))
    unique_footprints = int(unique_footprints)
    return {
        "provenance_version": 1,
        "field_id": int(field_id),
        "field_frame_count": len(set(int(value) for value in frame_ids)),
        "query_mode": "field_union",
        "new_source_search_performed": True,
        "coverage_complete": complete,
        "frame_scope_sha256": _working_catalog_frame_scope_sha256(frame_ids),
        "footprint_sha256": _working_catalog_footprint_sha256(gaia_query, cones),
        "catalog_name": "gaia_dr3",
        "gaia_release": gaia_query.GAIA_DR3_RELEASE,
        "gaia_table": gaia_query.GAIA_DR3_TABLE,
        "requested_columns": list(gaia_query.GAIA_WORKING_CATALOG_COLUMNS),
        "requested_columns_sha256": _working_catalog_hash_json(
            list(gaia_query.GAIA_WORKING_CATALOG_COLUMNS)
        ),
        "margin_arcmin": float(margin_arcmin),
        "magnitude_limit": float(mag_limit) if mag_limit is not None else None,
        "max_radius_deg": float(max_radius_deg),
        "maxrec": int(maxrec),
        "fetch_status": str(fetch_status),
        "fetch_complete": complete,
        "elapsed_seconds": (
            float(elapsed_seconds) if elapsed_seconds is not None else None
        ),
        "logical_acquisitions": 1,
        "raw_cones": raw_cones,
        "cone_count": unique_footprints,
        "unique_footprints": unique_footprints,
        "logical_queries": int(diagnostics.get("logical_queries") or 0),
        "physical_parts": int(diagnostics.get("physical_parts") or 0),
        "provider_attempts": int(diagnostics.get("provider_attempts") or 0),
        "providers_used": providers_used,
        "providers_attempted": providers_attempted,
        "provider_failures": len(provider_failures),
        "rows_received": rows_received,
        "unique_rows": unique_rows,
        "unique_sources": unique_rows,
        "field_acquisition_unique_identity_count": unique_rows,
        "working_catalog_sources": unique_rows,
        "duplicate_rows": duplicate_rows,
        "overflow_splits": int(diagnostics.get("overflow_splits") or 0),
        "overflow_occurred": bool(overflow_events or diagnostics.get("overflow_splits")),
        "overflow_event_count": len(overflow_events),
        "overflow_statuses": overflow_statuses,
        "partial_rows_discarded": partial_rows_discarded,
        "position_policy": {
            "coordinate_frame": "ICRS",
            "catalog_position_columns": ["ra", "dec"],
            "catalog_position_epoch_year": 2016.0,
            "proper_motion_correction_applied": False,
            "query_footprints": "union_of_frame_wcs_corner_cones",
            "query_margin_arcmin": float(margin_arcmin),
        },
    }


def _auto_working_catalog_matching_policy(
    config: FieldPipelineConfig,
) -> dict[str, Any]:
    return {
        "algorithm": "per_frame_greedy_one_to_one_haversine",
        "detection_positions": "source_detection_ra_dec_from_frame_wcs",
        "radius_arcsec": float(config.detection_match_radius_arcsec),
        "working_catalog_mag_limit": (
            float(config.working_catalog_mag_limit)
            if config.working_catalog_mag_limit is not None
            else None
        ),
        "max_sources_per_frame": (
            int(config.max_working_sources_per_frame)
            if config.max_working_sources_per_frame is not None
            else None
        ),
        "catalog_edge_margin_pix": float(config.catalog_edge_margin_pix),
        "include_exact_requested_target": bool(config.target_source_id is not None),
    }


def _auto_working_catalog_series_selection(
    *,
    series_id: int,
    pipeline_result: Any,
    promoted_source_ids: set[str],
    config: FieldPipelineConfig,
) -> dict[str, Any]:
    step = next(
        (
            item
            for item in getattr(pipeline_result, "steps", ())
            if getattr(item, "step_name", None) == "select_and_promote_working_gaia_sources"
        ),
        None,
    )
    payload = getattr(step, "result", None) if step is not None else None
    payload = payload if isinstance(payload, dict) else {}
    selected_count = payload.get("selected_gaia_source_count")
    promoted_count = payload.get("canonical_catalog_source_count")
    selection_status = getattr(step, "status", None) if step is not None else None
    promotion_status = payload.get("promotion_status")
    if step is not None and selection_status != "FAILED":
        if (
            isinstance(selected_count, bool)
            or not isinstance(selected_count, int)
            or isinstance(promoted_count, bool)
            or not isinstance(promoted_count, int)
            or selected_count != len(promoted_source_ids)
            or promoted_count != len(promoted_source_ids)
        ):
            raise RuntimeError(
                "working Gaia selection counts disagree with the non-serialized identity capture"
            )
    elif promoted_source_ids:
        selected_count = len(promoted_source_ids)
        promoted_count = len(promoted_source_ids)
        selection_status = "COMPLETED_BEFORE_PIPELINE_FAILURE"
        promotion_status = "PROMOTED"
    else:
        selected_count = None
        promoted_count = None
        selection_status = str(selection_status or "NOT_REPORTED")
    return {
        "series_id": int(series_id),
        "selection_step_status": str(selection_status),
        "promotion_status": (
            str(promotion_status) if promotion_status is not None else None
        ),
        "selected_identity_count": selected_count,
        "promoted_identity_count": promoted_count,
        "matching_policy": _auto_working_catalog_matching_policy(config),
    }


def _finalize_auto_working_catalog_provenance(
    *,
    catalog: Any | None,
    provenance: dict[str, Any] | None,
    promoted_source_ids: set[str],
    series_selections: list[dict[str, Any]],
) -> None:
    if not isinstance(provenance, dict):
        promoted_source_ids.clear()
        series_selections.clear()
        return
    provenance["series_selection"] = [dict(item) for item in series_selections]
    provenance["series_selection_count"] = len(series_selections)
    series_accounting_complete = bool(series_selections) and all(
        type(item.get("selected_identity_count")) is int
        and item["selected_identity_count"] >= 0
        and type(item.get("promoted_identity_count")) is int
        and item["promoted_identity_count"] >= 0
        for item in series_selections
    )
    provenance["field_promoted_unique_identity_count"] = None
    provenance["field_promotion_accounting_scope"] = "unique_source_id_union_across_series"
    provenance["field_promotion_accounting_complete"] = False
    provenance["field_discarded_transient_identity_count"] = None
    provenance["field_acquisition_unique_identity_count"] = None
    if catalog is not None:
        catalog_ids = {str(value) for value in catalog.source_ids}
        if not promoted_source_ids.issubset(catalog_ids):
            raise RuntimeError(
                "working Gaia promotion identity capture exceeds its field catalog scope"
            )
        if provenance.get("new_source_search_performed") is True:
            if provenance.get("fetch_complete") is not True:
                raise RuntimeError(
                    "working Gaia field promotion accounting requires a complete acquisition"
                )
            provenance["field_acquisition_unique_identity_count"] = len(catalog_ids)
            if series_accounting_complete:
                provenance["field_promoted_unique_identity_count"] = len(
                    promoted_source_ids
                )
                provenance["field_discarded_transient_identity_count"] = len(
                    catalog_ids.difference(promoted_source_ids)
                )
                provenance["field_promotion_accounting_complete"] = True
        elif provenance.get("new_source_search_performed") is False:
            provenance["field_reused_catalog_unique_identity_count"] = len(catalog_ids)
            if series_accounting_complete:
                provenance["field_promoted_unique_identity_count"] = len(
                    promoted_source_ids
                )
                provenance["field_promotion_accounting_complete"] = True
    promoted_source_ids.clear()
    series_selections.clear()


def _persist_finalized_auto_working_catalog_provenance(
    session: Session,
    *,
    field_id: int | None,
    run_ids_by_series: dict[int, int],
    provenance: dict[str, Any],
) -> None:
    """Replace the acquisition snapshot on each exact field pipeline run."""

    if not run_ids_by_series:
        return
    if field_id is None or isinstance(field_id, bool) or not isinstance(field_id, int):
        raise RuntimeError("finalized working Gaia provenance requires an exact field_id")
    if not isinstance(provenance, dict) or provenance.get("field_id") != field_id:
        raise RuntimeError("finalized working Gaia provenance does not match its field scope")

    try:
        provenance_json = json.dumps(
            provenance,
            ensure_ascii=False,
            allow_nan=False,
        )
        safe_provenance = json.loads(provenance_json)
    except (TypeError, ValueError):
        raise RuntimeError("finalized working Gaia provenance must contain JSON values only") from None

    def validate_compact_payload(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise RuntimeError("working Gaia provenance keys must be strings")
                if key.casefold() in {
                    "source_id",
                    "source_ids",
                    "catalog_source_id",
                    "catalog_source_ids",
                    "catalog_row",
                    "catalog_rows",
                }:
                    raise RuntimeError("working Gaia provenance must not contain source identities")
                validate_compact_payload(child)
        elif isinstance(value, list):
            for child in value:
                validate_compact_payload(child)
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise RuntimeError("working Gaia provenance must contain scalar/list JSON values")

    validate_compact_payload(safe_provenance)
    update_statement = text(
        """
        UPDATE pipeline_runs
        SET recipe_json = jsonb_set(
                jsonb_set(
                    recipe_json,
                    '{working_catalog_provenance}',
                    CAST(:provenance_json AS JSONB),
                    true
                ),
                '{quality_policy}',
                jsonb_set(
                    recipe_json->'quality_policy',
                    '{working_catalog_provenance}',
                    CAST(:provenance_json AS JSONB),
                    true
                ),
                true
            ),
            updated_at = now()
        WHERE pipeline_run_id = :pipeline_run_id
          AND field_id = :field_id
          AND series_id = :series_id
          AND catalog_name = 'gaia_dr3'
          AND jsonb_typeof(recipe_json) = 'object'
          AND jsonb_typeof(recipe_json->'quality_policy') = 'object'
        """
    )
    normalized_run_ids: list[tuple[int, int]] = []
    for series_id, pipeline_run_id in run_ids_by_series.items():
        if (
            isinstance(series_id, bool)
            or not isinstance(series_id, int)
            or isinstance(pipeline_run_id, bool)
            or not isinstance(pipeline_run_id, int)
        ):
            raise RuntimeError("working Gaia run scope requires integer series/run IDs")
        normalized_run_ids.append((series_id, pipeline_run_id))
    if len({run_id for _series_id, run_id in normalized_run_ids}) != len(
        normalized_run_ids
    ):
        raise RuntimeError("working Gaia run scope contains a duplicate pipeline_run_id")

    try:
        for series_id, pipeline_run_id in sorted(normalized_run_ids):
            update = session.execute(
                update_statement,
                {
                    "provenance_json": provenance_json,
                    "pipeline_run_id": pipeline_run_id,
                    "field_id": field_id,
                    "series_id": series_id,
                },
            )
            if getattr(update, "rowcount", None) != 1:
                raise RuntimeError(
                    "finalized working Gaia provenance did not match one exact pipeline run"
                )
        session.commit()
    except Exception:
        session.rollback()
        raise


def _prepare_auto_working_catalog(
    session: Session,
    *,
    field_id: int,
    frame_ids: list[int],
    enabled: bool,
    margin_arcmin: float,
    mag_limit: float | None,
    maxrec: int,
    max_radius_deg: float,
    timeout_sec: float | None,
) -> tuple[Any | None, dict[str, Any]]:
    """Acquire one bounded, transient Gaia DR3 catalog for a persistent field."""

    from grandgousier.catalogs import gaia_query

    selected_frame_ids = sorted({int(frame_id) for frame_id in frame_ids})
    row: dict[str, Any] = {
        "mode": "auto_gaia_dr3_working",
        "catalog_name": "gaia_dr3",
        "field_id": int(field_id),
        "enabled": bool(enabled),
        "frame_ids_count": len(selected_frame_ids),
        "raw_cones": 0,
        "unique_footprints": 0,
        "query_mode": "field_union",
        "max_radius_deg": max_radius_deg,
        "mag_limit": mag_limit,
        "maxrec": maxrec,
        "timeout_sec": timeout_sec,
        "status": "RUNNING",
    }
    if not enabled:
        row["status"] = "SKIPPED_DISABLED"
        return None, row
    if not selected_frame_ids:
        row["status"] = "SKIPPED_NO_FRAMES"
        return None, row

    catalog = None
    fetch_elapsed_seconds: float | None = None
    try:
        margin_arcmin = float(margin_arcmin)
        if not math.isfinite(margin_arcmin) or margin_arcmin < 0.0:
            raise ValueError("margin_arcmin must be finite and non-negative")
        frames = gaia_query._select_frames(
            session,
            frame_ids=selected_frame_ids,
            instrument=None,
            instrument_unit_id=None,
            only_wcs_ok=False,
            limit=None,
        )
        frames_by_id = {int(frame.frame_id): frame for frame in frames}
        missing_frame_count = len(set(selected_frame_ids) - set(frames_by_id))
        row["selected_frames"] = len(frames_by_id)
        row["missing_frame_count"] = missing_frame_count
        if missing_frame_count:
            row["status"] = "FAILED_FRAME_SELECTION"
            return None, row

        cones: list[tuple[float, float, float]] = []
        failed_footprint_reasons: Counter[str] = Counter()
        for frame_id in selected_frame_ids:
            frame = frames_by_id[frame_id]
            try:
                cone = gaia_query._frame_cone(
                    Path(frame.raw_path), margin_arcmin=margin_arcmin
                )
            except Exception as exc:  # noqa: BLE001 - report footprint failure without response data.
                failed_footprint_reasons[
                    f"wcs_footprint_failed:{type(exc).__name__}"
                ] += 1
            else:
                if cone is None:
                    failed_footprint_reasons["invalid_wcs_footprint"] += 1
                    continue
                cones.append(cone)
        row["raw_cones"] = len(cones)
        row["unique_footprints"] = len(gaia_query._normalise_working_cones(cones))
        row["failed_footprints"] = sum(failed_footprint_reasons.values())
        row["failed_footprint_reasons"] = dict(sorted(failed_footprint_reasons.items()))
        if failed_footprint_reasons:
            row["status"] = "FAILED_FOOTPRINTS"
            return None, row

        timeout = 120.0 if timeout_sec is None else float(timeout_sec)
        fetch_started = time.perf_counter()
        try:
            catalog, diagnostics = asyncio.run(
                gaia_query.fetch_gaia_working_catalog(
                    cones,
                    max_radius_deg=max_radius_deg,
                    mag_limit=mag_limit,
                    maxrec=maxrec,
                    gaia_timeout_sec=timeout,
                )
            )
        finally:
            fetch_elapsed_seconds = float(time.perf_counter() - fetch_started)
        provider_failures = diagnostics.get("provider_failures") or []
        overflow_splits = int(diagnostics.get("overflow_splits") or 0)
        unique_rows = len(catalog)
        rows_received = int(diagnostics.get("rows_received") or 0)
        fetch_diagnostics = {
            "raw_cones": int(diagnostics.get("raw_cones", len(cones))),
            "unique_footprints": int(
                diagnostics.get("unique_footprints", row["unique_footprints"])
            ),
            "logical_queries": int(diagnostics.get("logical_queries") or 0),
            "physical_parts": int(diagnostics.get("physical_parts") or 0),
            "provider_attempts": int(diagnostics.get("provider_attempts") or 0),
            "provider_failures": len(provider_failures),
            "providers_used": list(diagnostics.get("providers_used") or []),
            "overflow_splits": overflow_splits,
            "rows_received": rows_received,
            "unique_sources": unique_rows,
            "duplicate_rows": rows_received - unique_rows,
            "working_catalog_sources": unique_rows,
            "partial_rows_discarded": bool(
                diagnostics.get("partial_rows_discarded", False)
            ),
        }
        row["working_catalog_provenance"] = _auto_working_catalog_fetch_provenance(
            gaia_query=gaia_query,
            field_id=field_id,
            frame_ids=selected_frame_ids,
            cones=cones,
            margin_arcmin=margin_arcmin,
            mag_limit=mag_limit,
            max_radius_deg=max_radius_deg,
            maxrec=maxrec,
            diagnostics=diagnostics,
            fetch_status="SUCCEEDED_COMPLETE",
            fetch_complete=True,
            unique_rows=unique_rows,
            elapsed_seconds=fetch_elapsed_seconds,
        )
        row.update(
            {
                "status": "DONE_WITH_WARNINGS" if provider_failures else "DONE",
                "raw_cones": fetch_diagnostics["raw_cones"],
                "unique_footprints": fetch_diagnostics["unique_footprints"],
                "logical_queries": fetch_diagnostics["logical_queries"],
                "physical_parts": fetch_diagnostics["physical_parts"],
                "provider_attempts": fetch_diagnostics["provider_attempts"],
                "provider_failures": fetch_diagnostics["provider_failures"],
                "overflow_splits": fetch_diagnostics["overflow_splits"],
                "rows_received": fetch_diagnostics["rows_received"],
                "duplicate_rows": fetch_diagnostics["duplicate_rows"],
                "providers_used": fetch_diagnostics["providers_used"],
                "elapsed_seconds": fetch_elapsed_seconds,
                "working_catalog_sources": unique_rows,
                "partial_rows_discarded": fetch_diagnostics["partial_rows_discarded"],
            }
        )
        return catalog, row
    except Exception as exc:  # noqa: BLE001 - acquisition failures are reported without response data.
        if catalog is not None:
            _close_working_catalog(catalog)
        diagnostics = getattr(exc, "diagnostics", {})
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        row.update(
            {
                "status": "FAILED",
                "failure_type": str(diagnostics.get("failure_type") or type(exc).__name__),
                "logical_queries": int(diagnostics.get("logical_queries") or 0),
                "physical_parts": int(diagnostics.get("physical_parts") or 0),
                "provider_attempts": int(diagnostics.get("provider_attempts") or 0),
                "provider_failures": len(diagnostics.get("provider_failures") or []),
                "overflow_splits": int(diagnostics.get("overflow_splits") or 0),
                "rows_received": int(diagnostics.get("rows_received") or 0),
                "providers_used": list(diagnostics.get("providers_used") or []),
                "elapsed_seconds": fetch_elapsed_seconds,
                "partial_rows_discarded": bool(
                    diagnostics.get("partial_rows_discarded", False)
                ),
            }
        )
        if fetch_elapsed_seconds is not None:
            row["working_catalog_provenance"] = _auto_working_catalog_fetch_provenance(
                gaia_query=gaia_query,
                field_id=field_id,
                frame_ids=selected_frame_ids,
                cones=cones,
                margin_arcmin=margin_arcmin,
                mag_limit=mag_limit,
                max_radius_deg=max_radius_deg,
                maxrec=maxrec,
                diagnostics=diagnostics,
                fetch_status="FAILED",
                fetch_complete=False,
                unique_rows=None,
                elapsed_seconds=fetch_elapsed_seconds,
            )
        return None, row


class _OfflineWorkingCatalogReuseRejected(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _reuse_auto_working_catalog(
    session: Session,
    *,
    field_id: int,
    frame_ids: list[int],
    margin_arcmin: float,
    mag_limit: float | None,
    maxrec: int,
    max_radius_deg: float,
    target_source_id: str | None,
    working_catalog_mag_limit: float | None,
    max_working_sources_per_frame: int | None,
    catalog_edge_margin_pix: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
) -> tuple[Any | None, dict[str, Any]]:
    """Reuse exact-scope Gaia identities backed by completed match evidence.

    Identity and link reads are restricted to IDs from successful detection matches
    (plus one explicitly requested target). This path never scans ``gaia_dr3``.
    """

    row: dict[str, Any] = {
        "mode": "offline_gaia_dr3_reuse",
        "catalog_name": "gaia_dr3",
        "field_id": int(field_id),
        "frame_ids_count": len(set(int(value) for value in frame_ids)),
        "query_mode": "field_union",
        "new_source_search_performed": False,
        "coverage_complete": False,
        "reuse_compatible": False,
        "status": "FAILED_OFFLINE_REUSE_PRECONDITION",
    }
    try:
        catalog, provenance, diagnostics = _load_offline_working_catalog_reuse(
            session,
            field_id=field_id,
            frame_ids=frame_ids,
            margin_arcmin=margin_arcmin,
            mag_limit=mag_limit,
            maxrec=maxrec,
            max_radius_deg=max_radius_deg,
            target_source_id=target_source_id,
            working_catalog_mag_limit=working_catalog_mag_limit,
            max_working_sources_per_frame=max_working_sources_per_frame,
            catalog_edge_margin_pix=catalog_edge_margin_pix,
            aperture_radius_pix=aperture_radius_pix,
            annulus_inner_pix=annulus_inner_pix,
            annulus_outer_pix=annulus_outer_pix,
        )
    except _OfflineWorkingCatalogReuseRejected as exc:
        row["refusal_reason"] = exc.reason
        return None, row
    except Exception:  # noqa: BLE001 - proof failures must not reach the pipeline.
        row["refusal_reason"] = "OFFLINE_EVIDENCE_READ_FAILED"
        return None, row

    offline_provenance = dict(provenance)
    offline_provenance.update(
        {
            "reuse_mode": "completed_pipeline_catalog_match",
            "new_source_search_performed": False,
            "coverage_complete": False,
            "per_frame_match_coverage_complete": True,
            "reused_catalog_source_count": len(catalog),
            "explicit_target_included": bool(diagnostics["target_source_included"]),
        }
    )
    row.update(diagnostics)
    row.update(
        {
            "status": "DONE",
            "reuse_compatible": True,
            "working_catalog_sources": len(catalog),
            "working_catalog_provenance": offline_provenance,
        }
    )
    return catalog, row


def _load_offline_working_catalog_reuse(
    session: Session,
    *,
    field_id: int,
    frame_ids: list[int],
    margin_arcmin: float,
    mag_limit: float | None,
    maxrec: int,
    max_radius_deg: float,
    target_source_id: str | None,
    working_catalog_mag_limit: float | None,
    max_working_sources_per_frame: int | None,
    catalog_edge_margin_pix: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from grandgousier.catalogs import gaia_query
    from grandgousier.db.detection_runs import (
        CATALOG_MATCH_RUN,
        SOURCE_DETECTION_RUN,
        algorithm_version,
        detection_run_fingerprint,
    )
    from grandgousier.db.models import (
        CatalogSource,
        DetectionCatalogMatch,
        DetectionRun,
        DetectionRunFrame,
        FrameCatalogLink,
        PipelineRun,
        PipelineRunStep,
        SourceDetection,
    )
    from grandgousier.photometry.source_detection import DEFAULT_ALGORITHM
    from grandgousier.quality.detection_catalog_qc import MATCH_ALGORITHM

    selected_frame_ids = sorted({int(value) for value in frame_ids})
    if not selected_frame_ids:
        raise _OfflineWorkingCatalogReuseRejected("NO_SELECTED_FRAMES")
    if len(selected_frame_ids) != len(frame_ids):
        raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_FRAME_SCOPE")
    if target_source_id is not None and not isinstance(target_source_id, str):
        raise _OfflineWorkingCatalogReuseRejected("INVALID_EXPLICIT_TARGET_ID")

    try:
        if isinstance(maxrec, bool):
            raise ValueError
        margin_arcmin = float(margin_arcmin)
        max_radius_deg = float(max_radius_deg)
        maxrec = int(maxrec)
        if (
            not math.isfinite(margin_arcmin)
            or margin_arcmin < 0.0
            or not math.isfinite(max_radius_deg)
            or max_radius_deg <= 0.0
            or maxrec <= 0
        ):
            raise ValueError
        if mag_limit is not None:
            mag_limit = float(mag_limit)
            if not math.isfinite(mag_limit):
                raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise _OfflineWorkingCatalogReuseRejected("INVALID_CURRENT_QUERY_CONFIGURATION") from None

    frames = gaia_query._select_frames(
        session,
        frame_ids=selected_frame_ids,
        instrument=None,
        instrument_unit_id=None,
        only_wcs_ok=False,
        limit=None,
    )
    frames_by_id = {int(frame.frame_id): frame for frame in frames}
    if len(frames_by_id) != len(selected_frame_ids) or set(frames_by_id) != set(
        selected_frame_ids
    ):
        raise _OfflineWorkingCatalogReuseRejected("CURRENT_FRAME_SELECTION_INCOMPLETE")

    cones: list[tuple[float, float, float]] = []
    for frame_id in selected_frame_ids:
        try:
            cone = gaia_query._frame_cone(
                Path(frames_by_id[frame_id].raw_path), margin_arcmin=margin_arcmin
            )
        except Exception:
            raise _OfflineWorkingCatalogReuseRejected("CURRENT_WCS_FOOTPRINT_UNAVAILABLE") from None
        if cone is None:
            raise _OfflineWorkingCatalogReuseRejected("CURRENT_WCS_FOOTPRINT_UNAVAILABLE")
        cones.append(cone)

    defaults = FieldPipelineConfig(catalog_name="gaia_dr3", calibration_prefix="offline-reuse")
    expected_provenance = {
        "provenance_version": 1,
        "field_id": int(field_id),
        "field_frame_count": len(selected_frame_ids),
        "query_mode": "field_union",
        "frame_scope_sha256": _working_catalog_frame_scope_sha256(selected_frame_ids),
        "footprint_sha256": _working_catalog_footprint_sha256(gaia_query, cones),
        "catalog_name": "gaia_dr3",
        "gaia_release": gaia_query.GAIA_DR3_RELEASE,
        "gaia_table": gaia_query.GAIA_DR3_TABLE,
        "requested_columns_sha256": _working_catalog_hash_json(
            list(gaia_query.GAIA_WORKING_CATALOG_COLUMNS)
        ),
        "margin_arcmin": margin_arcmin,
        "magnitude_limit": mag_limit,
        "max_radius_deg": max_radius_deg,
        "maxrec": maxrec,
    }

    pipeline_rows = session.execute(
        select(
            PipelineRun.pipeline_run_id.label("pipeline_run_id"),
            PipelineRun.field_id.label("field_id"),
            PipelineRun.series_id.label("series_id"),
            PipelineRun.instrument_unit_id.label("instrument_unit_id"),
            PipelineRun.catalog_name.label("catalog_name"),
            PipelineRun.status.label("status"),
            PipelineRun.recipe_json.label("recipe_json"),
        ).where(
            PipelineRun.field_id == int(field_id),
            PipelineRun.catalog_name == "gaia_dr3",
            PipelineRun.status.in_(("DONE", "DONE_WITH_WARNINGS")),
        ).order_by(PipelineRun.pipeline_run_id.desc()).limit(1001)
    ).mappings().all()
    if not pipeline_rows:
        raise _OfflineWorkingCatalogReuseRejected("NO_COMPLETED_PIPELINE_EVIDENCE")
    if len(pipeline_rows) > 1000:
        raise _OfflineWorkingCatalogReuseRejected("TOO_MANY_PIPELINE_EVIDENCE_ROWS")

    scope_rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for run in pipeline_rows:
        recipe = run.get("recipe_json")
        recipe = recipe if isinstance(recipe, dict) else {}
        provenance = recipe.get("working_catalog_provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        if (
            provenance.get("field_id") == expected_provenance["field_id"]
            and provenance.get("field_frame_count") == expected_provenance["field_frame_count"]
            and provenance.get("frame_scope_sha256")
            == expected_provenance["frame_scope_sha256"]
        ):
            scope_rows.append((dict(run), recipe, provenance))
    if not scope_rows:
        raise _OfflineWorkingCatalogReuseRejected("FIELD_FRAME_SCOPE_MISMATCH")

    footprint_rows = [
        candidate
        for candidate in scope_rows
        if candidate[2].get("footprint_sha256") == expected_provenance["footprint_sha256"]
    ]
    if not footprint_rows:
        raise _OfflineWorkingCatalogReuseRejected("WCS_FOOTPRINT_MISMATCH")

    query_rows = [
        candidate
        for candidate in footprint_rows
        if all(
            _offline_json_value_matches(candidate[2].get(key), value)
            for key, value in expected_provenance.items()
        )
    ]
    if not query_rows:
        raise _OfflineWorkingCatalogReuseRejected("QUERY_PROVENANCE_MISMATCH")

    complete_query_rows = [
        candidate
        for candidate in query_rows
        if candidate[2].get("fetch_status") == "SUCCEEDED_COMPLETE"
        and candidate[2].get("fetch_complete") is True
        and candidate[2].get("partial_rows_discarded") is False
        and _offline_nonnegative_int(candidate[2].get("unique_sources"))
        == _offline_nonnegative_int(candidate[2].get("working_catalog_sources"))
    ]
    if not complete_query_rows:
        raise _OfflineWorkingCatalogReuseRejected("FETCH_PROVENANCE_INCOMPLETE")

    expected_detector_parameters = {
        "threshold_sigma": defaults.detection_threshold_sigma,
        "max_sources_per_frame": defaults.detection_max_sources_per_frame,
        "min_separation_pix": defaults.detection_min_separation_pix,
        "aperture_radius_pix": aperture_radius_pix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
        "centroid_box_radius_pix": 3,
    }
    expected_match_parameters = {
        "detection_algorithm": DEFAULT_ALGORITHM,
        "radius_arcsec": defaults.detection_match_radius_arcsec,
        "mag_key": defaults.mag_key,
        "min_snr": None,
    }
    expected_detector_recipe = {
        "detection_threshold_sigma": defaults.detection_threshold_sigma,
        "detection_max_sources_per_frame": defaults.detection_max_sources_per_frame,
        "detection_min_separation_pix": defaults.detection_min_separation_pix,
        "aperture_radius_pix": aperture_radius_pix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
    }
    expected_projection_recipe = {
        "margin_pix": defaults.margin_pix,
        "mag_key": defaults.mag_key,
        "working_catalog_mag_limit": working_catalog_mag_limit,
        "max_working_sources_per_frame": max_working_sources_per_frame,
        "catalog_edge_margin_pix": catalog_edge_margin_pix,
        "replace_frame_catalog_links": True,
    }

    def pipeline_recipe_compatible(
        run: dict[str, Any], recipe: dict[str, Any], provenance: dict[str, Any]
    ) -> bool:
        if run.get("field_id") != int(field_id):
            return False
        if run.get("status") not in {"DONE", "DONE_WITH_WARNINGS"}:
            return False
        if recipe.get("field_id") != int(field_id):
            return False
        if recipe.get("series_id") != run.get("series_id") or run.get("series_id") is None:
            return False
        if recipe.get("instrument_unit_id") != run.get("instrument_unit_id"):
            return False
        if recipe.get("catalog_name") != "gaia_dr3":
            return False
        if recipe.get("dry_run") is not False:
            return False
        if recipe.get("skip_detection") is not False:
            return False
        if recipe.get("skip_detection_catalog_match") is not False:
            return False
        if recipe.get("skip_catalog_frame_match") is not False:
            return False
        if recipe.get("working_catalog_provenance") != provenance:
            return False
        quality_policy = recipe.get("quality_policy")
        if not isinstance(quality_policy, dict) or quality_policy.get(
            "working_catalog_provenance"
        ) != provenance:
            return False
        if not all(
            _offline_json_value_matches(recipe.get(key), value)
            for key, value in expected_projection_recipe.items()
        ):
            return False
        if not all(
            _offline_json_value_matches(recipe.get(key), value)
            for key, value in expected_detector_recipe.items()
        ):
            return False
        return _offline_json_value_matches(
            recipe.get("detection_match_radius_arcsec"),
            defaults.detection_match_radius_arcsec,
        )

    compatible_pipeline_rows = [
        candidate
        for candidate in complete_query_rows
        if pipeline_recipe_compatible(*candidate)
    ]
    if not compatible_pipeline_rows:
        raise _OfflineWorkingCatalogReuseRejected("PIPELINE_CONFIGURATION_MISMATCH")

    pipeline_ids = sorted(
        {int(candidate[0]["pipeline_run_id"]) for candidate in compatible_pipeline_rows}
    )
    step_names = (
        "detect_sources",
        "select_and_promote_working_gaia_sources",
        "project_selected_working_gaia_sources",
        "match_detections_to_catalog",
    )
    steps = session.execute(
        select(
            PipelineRunStep.pipeline_run_id.label("pipeline_run_id"),
            PipelineRunStep.step_name.label("step_name"),
            PipelineRunStep.status.label("status"),
            PipelineRunStep.result_json.label("result_json"),
        ).where(
            PipelineRunStep.pipeline_run_id.in_(pipeline_ids),
            PipelineRunStep.step_name.in_(step_names),
        )
    ).mappings().all()
    steps_by_pipeline: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for step in steps:
        steps_by_pipeline.setdefault(int(step["pipeline_run_id"]), {}).setdefault(
            str(step["step_name"]), []
        ).append(dict(step))

    detection_runs = session.execute(
        select(
            DetectionRun.detection_run_id.label("detection_run_id"),
            DetectionRun.pipeline_run_id.label("pipeline_run_id"),
            DetectionRun.run_kind.label("run_kind"),
            DetectionRun.catalog_name.label("catalog_name"),
            DetectionRun.algorithm.label("algorithm"),
            DetectionRun.algorithm_version.label("algorithm_version"),
            DetectionRun.parameters.label("parameters"),
            DetectionRun.fingerprint.label("fingerprint"),
        ).where(
            DetectionRun.pipeline_run_id.in_(pipeline_ids),
            DetectionRun.run_kind.in_((SOURCE_DETECTION_RUN, CATALOG_MATCH_RUN)),
        )
    ).mappings().all()
    runs_by_pipeline: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for detection_run in detection_runs:
        runs_by_pipeline.setdefault(int(detection_run["pipeline_run_id"]), {}).setdefault(
            str(detection_run["run_kind"]), []
        ).append(dict(detection_run))

    candidate_records: list[dict[str, Any]] = []
    for run, recipe, provenance in compatible_pipeline_rows:
        pipeline_run_id = int(run["pipeline_run_id"])
        raw_frame_ids = recipe.get("frame_ids")
        if not isinstance(raw_frame_ids, list) or not raw_frame_ids:
            continue
        try:
            pipeline_frame_ids = [int(value) for value in raw_frame_ids]
        except (TypeError, ValueError, OverflowError):
            continue
        if len(set(pipeline_frame_ids)) != len(pipeline_frame_ids):
            continue
        pipeline_frame_set = set(pipeline_frame_ids)
        if not pipeline_frame_set.issubset(set(selected_frame_ids)):
            continue
        if any(
            getattr(frames_by_id[frame_id], "instrument_unit_id", None)
            != run.get("instrument_unit_id")
            for frame_id in pipeline_frame_set
        ):
            continue

        pipeline_steps = steps_by_pipeline.get(pipeline_run_id, {})
        required_steps: dict[str, dict[str, Any]] = {}
        for step_name in step_names:
            rows_for_step = pipeline_steps.get(step_name, [])
            if len(rows_for_step) != 1 or rows_for_step[0].get("status") != "DONE":
                break
            result_json = rows_for_step[0].get("result_json")
            if not isinstance(result_json, dict):
                break
            required_steps[step_name] = result_json
        if len(required_steps) != len(step_names):
            continue

        source_runs = runs_by_pipeline.get(pipeline_run_id, {}).get(SOURCE_DETECTION_RUN, [])
        match_runs = runs_by_pipeline.get(pipeline_run_id, {}).get(CATALOG_MATCH_RUN, [])
        if len(source_runs) != 1 or len(match_runs) != 1:
            continue
        source_run, match_run = source_runs[0], match_runs[0]
        source_parameters = source_run.get("parameters")
        match_parameters = match_run.get("parameters")
        if not isinstance(source_parameters, dict) or not isinstance(match_parameters, dict):
            continue
        if (
            source_run.get("algorithm") != DEFAULT_ALGORITHM
            or source_run.get("algorithm_version") != algorithm_version(DEFAULT_ALGORITHM)
            or source_parameters != expected_detector_parameters
            or source_run.get("fingerprint")
            != detection_run_fingerprint(
                run_kind=SOURCE_DETECTION_RUN,
                algorithm=DEFAULT_ALGORITHM,
                parameters=expected_detector_parameters,
            )
        ):
            continue
        if (
            match_run.get("catalog_name") != "gaia_dr3"
            or match_run.get("algorithm") != MATCH_ALGORITHM
            or match_run.get("algorithm_version") != algorithm_version(MATCH_ALGORITHM)
            or match_parameters != expected_match_parameters
            or match_run.get("fingerprint")
            != detection_run_fingerprint(
                run_kind=CATALOG_MATCH_RUN,
                algorithm=MATCH_ALGORITHM,
                parameters=expected_match_parameters,
                catalog_name="gaia_dr3",
            )
        ):
            continue

        source_step = required_steps["detect_sources"]
        select_step = required_steps["select_and_promote_working_gaia_sources"]
        projection_step = required_steps["project_selected_working_gaia_sources"]
        match_step = required_steps["match_detections_to_catalog"]
        if not _offline_step_covers_frames(source_step, len(pipeline_frame_set), skipped=True):
            continue
        selected_source_count = _offline_nonnegative_int(
            select_step.get("selected_gaia_source_count")
        )
        canonical_source_count = _offline_nonnegative_int(
            select_step.get("canonical_catalog_source_count")
        )
        if (
            source_step.get("detection_run_id") != int(source_run["detection_run_id"])
            or selected_source_count is None
            or selected_source_count <= 0
            or canonical_source_count != selected_source_count
        ):
            continue
        if not _offline_step_covers_frames(
            projection_step, len(pipeline_frame_set), skipped=True
        ):
            continue
        if not _offline_step_covers_frames(match_step, len(pipeline_frame_set), skipped=False):
            continue
        if match_step.get("detection_run_id") != int(match_run["detection_run_id"]):
            continue
        candidate_records.append(
            {
                "pipeline_run_id": pipeline_run_id,
                "frame_ids": pipeline_frame_set,
                "provenance": provenance,
                "source_run_id": int(source_run["detection_run_id"]),
                "match_run_id": int(match_run["detection_run_id"]),
            }
        )

    if not candidate_records:
        raise _OfflineWorkingCatalogReuseRejected("NO_COMPATIBLE_COMPLETED_MATCH_RUN")

    detection_run_ids = sorted(
        {
            run_id
            for candidate in candidate_records
            for run_id in (candidate["source_run_id"], candidate["match_run_id"])
        }
    )
    run_frames = session.execute(
        select(
            DetectionRunFrame.detection_run_frame_id.label("detection_run_frame_id"),
            DetectionRunFrame.detection_run_id.label("detection_run_id"),
            DetectionRunFrame.frame_id.label("frame_id"),
            DetectionRunFrame.status.label("status"),
            DetectionRunFrame.diagnostics.label("diagnostics"),
        ).where(
            DetectionRunFrame.detection_run_id.in_(detection_run_ids),
            DetectionRunFrame.frame_id.in_(selected_frame_ids),
        )
    ).mappings().all()
    run_frames_by_run: dict[int, list[dict[str, Any]]] = {}
    for run_frame in run_frames:
        run_frames_by_run.setdefault(int(run_frame["detection_run_id"]), []).append(
            dict(run_frame)
        )

    frame_evidence: dict[int, list[dict[str, Any]]] = {}
    matched_run_frame_ids: set[int] = set()
    valid_candidate_records: list[dict[str, Any]] = []
    for candidate in candidate_records:
        expected_frames = set(candidate["frame_ids"])
        per_run_frames: dict[int, dict[int, dict[str, Any]]] = {}
        valid = True
        for run_id in (candidate["source_run_id"], candidate["match_run_id"]):
            selected_rows = run_frames_by_run.get(run_id, [])
            row_by_frame = {int(item["frame_id"]): item for item in selected_rows}
            if (
                len(row_by_frame) != len(selected_rows)
                or set(row_by_frame) != expected_frames
                or any(item.get("status") != "DONE" for item in selected_rows)
            ):
                valid = False
                break
            per_run_frames[run_id] = row_by_frame
        if not valid:
            continue
        match_frames = per_run_frames[candidate["match_run_id"]]
        source_frames = per_run_frames[candidate["source_run_id"]]
        candidate["match_frame_by_id"] = match_frames
        candidate["source_frame_by_id"] = source_frames
        valid_candidate_records.append(candidate)
        matched_run_frame_ids.update(
            int(item["detection_run_frame_id"]) for item in match_frames.values()
        )
        for frame_id in expected_frames:
            frame_evidence.setdefault(frame_id, []).append(candidate)

    if set(frame_evidence) != set(selected_frame_ids):
        raise _OfflineWorkingCatalogReuseRejected("INCOMPLETE_SUCCESSFUL_FRAME_COVERAGE")

    reuse_only_provenance_keys = {
        "reuse_mode",
        "new_source_search_performed",
        "coverage_complete",
        "per_frame_match_coverage_complete",
        "reused_catalog_source_count",
        "explicit_target_included",
    }
    expected_reuse_provenance = {
        "reuse_mode": "completed_pipeline_catalog_match",
        "new_source_search_performed": False,
        "coverage_complete": False,
        "per_frame_match_coverage_complete": True,
    }
    for candidate in valid_candidate_records:
        candidate_provenance = candidate["provenance"]
        if any(
            key in candidate_provenance
            and not _offline_json_value_matches(candidate_provenance[key], value)
            for key, value in expected_reuse_provenance.items()
        ):
            raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_QUERY_PROVENANCE")
        if (
            "reused_catalog_source_count" in candidate_provenance
            and _offline_nonnegative_int(
                candidate_provenance["reused_catalog_source_count"]
            )
            is None
        ):
            raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_QUERY_PROVENANCE")
        if (
            "explicit_target_included" in candidate_provenance
            and not isinstance(candidate_provenance["explicit_target_included"], bool)
        ):
            raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_QUERY_PROVENANCE")
    provenance_values = [
        {
            key: value
            for key, value in candidate["provenance"].items()
            if key not in reuse_only_provenance_keys
        }
        for candidate in valid_candidate_records
    ]
    if not provenance_values or any(value != provenance_values[0] for value in provenance_values[1:]):
        raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_QUERY_PROVENANCE")

    expected_match_frames: dict[int, dict[str, Any]] = {}
    for candidate in valid_candidate_records:
        for frame_id in candidate["frame_ids"]:
            match_frame = candidate["match_frame_by_id"][frame_id]
            source_frame = candidate["source_frame_by_id"][frame_id]
            diagnostics = match_frame.get("diagnostics")
            expected_match_frames[int(match_frame["detection_run_frame_id"])] = {
                "frame_id": int(frame_id),
                "n_matched": (
                    _offline_nonnegative_int(diagnostics.get("n_matched"))
                    if isinstance(diagnostics, dict)
                    else None
                ),
                "source_detection_run_id": int(candidate["source_run_id"]),
                "source_detection_run_frame_id": int(
                    source_frame["detection_run_frame_id"]
                ),
                "pipeline_run_id": int(candidate["pipeline_run_id"]),
            }

    source_run_frame = aliased(DetectionRunFrame)
    match_rows = session.execute(
        select(
            DetectionRunFrame.frame_id.label("frame_id"),
            DetectionCatalogMatch.detection_run_frame_id.label("detection_run_frame_id"),
            DetectionCatalogMatch.source_detection_id.label("source_detection_id"),
            DetectionCatalogMatch.catalog_source_id.label("catalog_source_id"),
            DetectionCatalogMatch.match_status.label("match_status"),
            SourceDetection.detection_run_frame_id.label(
                "source_detection_run_frame_id"
            ),
            source_run_frame.detection_run_frame_id.label(
                "joined_source_detection_run_frame_id"
            ),
            source_run_frame.detection_run_id.label("source_detection_run_id"),
            source_run_frame.frame_id.label("source_frame_id"),
        )
        .join(
            DetectionRunFrame,
            DetectionRunFrame.detection_run_frame_id
            == DetectionCatalogMatch.detection_run_frame_id,
        )
        .outerjoin(
            SourceDetection,
            SourceDetection.source_detection_id
            == DetectionCatalogMatch.source_detection_id,
        )
        .outerjoin(
            source_run_frame,
            source_run_frame.detection_run_frame_id
            == SourceDetection.detection_run_frame_id,
        )
        .where(
            DetectionCatalogMatch.detection_run_frame_id.in_(
                sorted(matched_run_frame_ids)
            )
        )
    ).mappings().all()
    actual_match_counts = {run_frame_id: 0 for run_frame_id in expected_match_frames}
    match_source_ids_by_pipeline: dict[int, dict[int, set[int]]] = {
        candidate["pipeline_run_id"]: {
            frame_id: set() for frame_id in candidate["frame_ids"]
        }
        for candidate in valid_candidate_records
    }
    matched_pairs: set[tuple[int, int]] = set()
    matched_catalog_source_ids: set[int] = set()
    for match in match_rows:
        match_run_frame_id = _offline_nonnegative_int(
            match.get("detection_run_frame_id")
        )
        expected_frame = expected_match_frames.get(match_run_frame_id)
        if expected_frame is None:
            raise _OfflineWorkingCatalogReuseRejected(
                "MATCH_ROW_OUTSIDE_CURRENT_FRAME_SCOPE"
            )
        actual_match_counts[match_run_frame_id] += 1
        if (
            match.get("frame_id") != expected_frame["frame_id"]
            or _offline_nonnegative_int(match.get("source_detection_run_frame_id"))
            != expected_frame["source_detection_run_frame_id"]
            or _offline_nonnegative_int(
                match.get("joined_source_detection_run_frame_id")
            )
            != expected_frame["source_detection_run_frame_id"]
            or _offline_nonnegative_int(match.get("source_detection_run_id"))
            != expected_frame["source_detection_run_id"]
            or match.get("source_frame_id") != expected_frame["frame_id"]
        ):
            raise _OfflineWorkingCatalogReuseRejected(
                "MATCH_SOURCE_DETECTION_SCOPE_MISMATCH"
            )
        if match.get("match_status") != "MATCHED":
            raise _OfflineWorkingCatalogReuseRejected("NONMATCHED_ROW_IN_COMPLETED_MATCH_RUN")
        frame_id = int(match["frame_id"])
        source_id = int(match["catalog_source_id"])
        if frame_id not in frame_evidence:
            raise _OfflineWorkingCatalogReuseRejected("MATCH_ROW_OUTSIDE_CURRENT_FRAME_SCOPE")
        match_source_ids_by_pipeline[expected_frame["pipeline_run_id"]][frame_id].add(
            source_id
        )
    for run_frame_id, expected in expected_match_frames.items():
        if (
            expected["n_matched"] is None
            or actual_match_counts[run_frame_id] != expected["n_matched"]
        ):
            raise _OfflineWorkingCatalogReuseRejected("MATCH_ROW_COUNT_MISMATCH")

    selected_pipeline_ids: set[int] = set()
    for frame_id, evidence in frame_evidence.items():
        ordered_evidence = sorted(
            evidence, key=lambda item: int(item["pipeline_run_id"]), reverse=True
        )
        identity_sets = [
            match_source_ids_by_pipeline[int(candidate["pipeline_run_id"])][frame_id]
            for candidate in ordered_evidence
        ]
        if any(identities != identity_sets[0] for identities in identity_sets[1:]):
            raise _OfflineWorkingCatalogReuseRejected("AMBIGUOUS_COMPLETED_FRAME_EVIDENCE")
        selected_candidate = ordered_evidence[0]
        selected_pipeline_ids.add(int(selected_candidate["pipeline_run_id"]))
        for source_id in identity_sets[0]:
            matched_pairs.add((frame_id, source_id))
            matched_catalog_source_ids.add(source_id)

    catalog_rows: list[dict[str, Any]] = []
    if matched_catalog_source_ids:
        identity_rows = session.execute(
            select(
                CatalogSource.catalog_source_id.label("catalog_source_id"),
                CatalogSource.catalog_name.label("catalog_name"),
                CatalogSource.source_id.label("source_id"),
                CatalogSource.ra_deg.label("ra_deg"),
                CatalogSource.dec_deg.label("dec_deg"),
                CatalogSource.ref_mags.label("ref_mags"),
            ).where(
                CatalogSource.catalog_name == "gaia_dr3",
                CatalogSource.catalog_source_id.in_(sorted(matched_catalog_source_ids)),
            )
        ).mappings().all()
        if len(identity_rows) != len(matched_catalog_source_ids):
            raise _OfflineWorkingCatalogReuseRejected("MATCHED_GAIA_IDENTITY_MISSING")
        seen_source_ids: dict[str, int] = {}
        for identity in identity_rows:
            canonical_id = int(identity["catalog_source_id"])
            if canonical_id not in matched_catalog_source_ids or identity.get(
                "catalog_name"
            ) != "gaia_dr3":
                raise _OfflineWorkingCatalogReuseRejected("MATCHED_GAIA_IDENTITY_AMBIGUOUS")
            source_id = str(identity.get("source_id") or "")
            previous_id = seen_source_ids.get(source_id)
            if previous_id is not None and previous_id != canonical_id:
                raise _OfflineWorkingCatalogReuseRejected("MATCHED_GAIA_IDENTITY_AMBIGUOUS")
            seen_source_ids[source_id] = canonical_id
            try:
                ra_deg = float(identity["ra_deg"])
                dec_deg = float(identity["dec_deg"])
            except (TypeError, ValueError, OverflowError):
                raise _OfflineWorkingCatalogReuseRejected("MATCHED_GAIA_IDENTITY_INVALID") from None
            if (
                not source_id.isdigit()
                or not math.isfinite(ra_deg)
                or not math.isfinite(dec_deg)
                or not 0.0 <= ra_deg < 360.0
                or not -90.0 <= dec_deg <= 90.0
            ):
                raise _OfflineWorkingCatalogReuseRejected("MATCHED_GAIA_IDENTITY_INVALID")
            ref_mags = identity.get("ref_mags")
            ref_mags = ref_mags if isinstance(ref_mags, dict) else {}
            try:
                g_mag = float(ref_mags["G"]) if ref_mags.get("G") is not None else math.nan
            except (TypeError, ValueError, OverflowError):
                g_mag = math.nan
            if not math.isfinite(g_mag):
                g_mag = math.nan
            catalog_rows.append(
                {
                    "catalog_source_id": canonical_id,
                    "source_id": source_id,
                    "ra": ra_deg,
                    "dec": dec_deg,
                    "phot_g_mean_mag": g_mag,
                }
            )

    target_catalog_source_id: int | None = None
    target_source_row: dict[str, Any] | None = None
    if target_source_id is not None:
        target_rows = session.execute(
            select(
                CatalogSource.catalog_source_id.label("catalog_source_id"),
                CatalogSource.catalog_name.label("catalog_name"),
                CatalogSource.source_id.label("source_id"),
                CatalogSource.ra_deg.label("ra_deg"),
                CatalogSource.dec_deg.label("dec_deg"),
                CatalogSource.ref_mags.label("ref_mags"),
            ).where(
                CatalogSource.catalog_name == "gaia_dr3",
                CatalogSource.source_id == target_source_id,
            )
        ).mappings().all()
        if len(target_rows) != 1:
            raise _OfflineWorkingCatalogReuseRejected("EXPLICIT_TARGET_NOT_CANONICAL")
        target_source_row = dict(target_rows[0])
        target_catalog_source_id = int(target_source_row["catalog_source_id"])
        if target_source_row.get("source_id") != target_source_id:
            raise _OfflineWorkingCatalogReuseRejected("EXPLICIT_TARGET_IDENTITY_MISMATCH")
        target_link_source_ids = sorted(
            matched_catalog_source_ids | {target_catalog_source_id}
        )
    else:
        target_link_source_ids = sorted(matched_catalog_source_ids)

    if target_link_source_ids:
        link_rows = session.execute(
            select(
                FrameCatalogLink.frame_id.label("frame_id"),
                FrameCatalogLink.catalog_source_id.label("catalog_source_id"),
                FrameCatalogLink.in_footprint.label("in_footprint"),
                FrameCatalogLink.margin_pix.label("margin_pix"),
                FrameCatalogLink.match_status.label("match_status"),
                FrameCatalogLink.details.label("details"),
            ).where(
                FrameCatalogLink.frame_id.in_(selected_frame_ids),
                FrameCatalogLink.catalog_source_id.in_(target_link_source_ids),
            )
        ).mappings().all()
    else:
        link_rows = []

    expected_link_details = {
        "mag_key": defaults.mag_key,
        "working_mag_limit": working_catalog_mag_limit,
        "max_sources_per_frame": max_working_sources_per_frame,
        "edge_margin_pix": catalog_edge_margin_pix,
    }
    scoped_frame_set = set(selected_frame_ids)
    scoped_source_set = set(target_link_source_ids)
    links_by_pair: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for link in link_rows:
        pair = (int(link["frame_id"]), int(link["catalog_source_id"]))
        if pair[0] not in scoped_frame_set or pair[1] not in scoped_source_set:
            raise _OfflineWorkingCatalogReuseRejected("FRAME_LINK_OUTSIDE_CURRENT_SCOPE")
        links_by_pair.setdefault(pair, []).append(dict(link))

    def link_is_compatible(link: dict[str, Any]) -> bool:
        details = link.get("details")
        return (
            link.get("in_footprint") is True
            and link.get("match_status") == "IN_FOOTPRINT"
            and _offline_json_value_matches(link.get("margin_pix"), defaults.margin_pix)
            and isinstance(details, dict)
            and all(
                _offline_json_value_matches(details.get(key), value)
                for key, value in expected_link_details.items()
            )
        )

    for pair in matched_pairs:
        candidate_links = links_by_pair.get(pair, [])
        if len(candidate_links) != 1 or not link_is_compatible(candidate_links[0]):
            raise _OfflineWorkingCatalogReuseRejected(
                "MATCHED_FRAME_LINK_MISSING_OR_INCOMPATIBLE"
            )

    target_source_included = False
    if target_catalog_source_id is not None:
        target_links = [
            link
            for (frame_id, source_id), values in links_by_pair.items()
            if source_id == target_catalog_source_id and frame_id in scoped_frame_set
            for link in values
            if link_is_compatible(link)
        ]
        if not target_links:
            raise _OfflineWorkingCatalogReuseRejected(
                "EXPLICIT_TARGET_HAS_NO_EXACT_SCOPE_LINK"
            )
        if not any(
            identity["catalog_source_id"] == target_catalog_source_id
            for identity in catalog_rows
        ):
            try:
                ra_deg = float(target_source_row["ra_deg"])
                dec_deg = float(target_source_row["dec_deg"])
            except (TypeError, ValueError, OverflowError):
                raise _OfflineWorkingCatalogReuseRejected(
                    "EXPLICIT_TARGET_IDENTITY_INVALID"
                ) from None
            if (
                not target_source_id.isdigit()
                or not math.isfinite(ra_deg)
                or not math.isfinite(dec_deg)
                or not 0.0 <= ra_deg < 360.0
                or not -90.0 <= dec_deg <= 90.0
            ):
                raise _OfflineWorkingCatalogReuseRejected(
                    "EXPLICIT_TARGET_IDENTITY_INVALID"
                )
            ref_mags = target_source_row.get("ref_mags")
            ref_mags = ref_mags if isinstance(ref_mags, dict) else {}
            try:
                g_mag = float(ref_mags["G"]) if ref_mags.get("G") is not None else math.nan
            except (TypeError, ValueError, OverflowError):
                g_mag = math.nan
            catalog_rows.append(
                {
                    "catalog_source_id": target_catalog_source_id,
                    "source_id": target_source_id,
                    "ra": ra_deg,
                    "dec": dec_deg,
                    "phot_g_mean_mag": g_mag if math.isfinite(g_mag) else math.nan,
                }
            )
        target_source_included = True

    source_id_to_canonical: dict[str, int] = {}
    unique_rows: dict[str, tuple[float, float, float]] = {}
    for identity in catalog_rows:
        source_id = str(identity["source_id"])
        canonical_id = int(identity["catalog_source_id"])
        previous_canonical_id = source_id_to_canonical.get(source_id)
        if previous_canonical_id is not None and previous_canonical_id != canonical_id:
            raise _OfflineWorkingCatalogReuseRejected("GAIA_SOURCE_ID_AMBIGUOUS")
        source_id_to_canonical[source_id] = canonical_id
        unique_rows[source_id] = (
            float(identity["ra"]),
            float(identity["dec"]),
            float(identity["phot_g_mean_mag"]),
        )

    expected_canonical_ids = matched_catalog_source_ids | (
        {target_catalog_source_id} if target_source_included else set()
    )
    if set(source_id_to_canonical.values()) != expected_canonical_ids:
        raise _OfflineWorkingCatalogReuseRejected("GAIA_IDENTITY_SET_INCOMPLETE")

    provenance = dict(provenance_values[0])
    catalog = gaia_query.GaiaWorkingCatalog._from_unique_rows(unique_rows)
    return catalog, provenance, {
        "reused_pipeline_count": len(selected_pipeline_ids),
        "per_frame_match_coverage_complete": True,
        "matched_catalog_source_count": len(matched_catalog_source_ids),
        "target_source_included": target_source_included,
    }


def _offline_json_value_matches(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        try:
            return math.isfinite(float(actual)) and float(actual) == float(expected)
        except (TypeError, ValueError, OverflowError):
            return False
    return type(actual) is type(expected) and actual == expected


def _offline_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _offline_step_covers_frames(
    result: dict[str, Any], frame_count: int, *, skipped: bool
) -> bool:
    for key in ("selected_frames", "checked_frames"):
        if _offline_nonnegative_int(result.get(key)) != int(frame_count):
            return False
    if _offline_nonnegative_int(result.get("failed_frames")) != 0:
        return False
    if skipped and _offline_nonnegative_int(result.get("skipped_missing_file")) != 0:
        return False
    return True


def _close_working_catalog(catalog: Any | None) -> None:
    if catalog is None:
        return
    try:
        catalog.clear()
    finally:
        catalog.close()


def _working_catalog_precondition_ok(
    row: dict[str, Any], *, allow_empty_catalog: bool
) -> bool:
    if allow_empty_catalog:
        return True
    return (
        row.get("status") in {"DONE", "DONE_WITH_WARNINGS"}
        and int(row.get("working_catalog_sources") or 0) > 0
    )


def _catalog_precondition_ok(row: dict[str, Any], *, allow_empty_catalog: bool) -> bool:
    if allow_empty_catalog:
        return True
    status = row.get("status")
    if status not in {"DONE", "DONE_WITH_WARNINGS", "DONE_WITH_FAILED_FRAMES"}:
        return False
    if int(row.get("inserted_or_updated") or 0) <= 0:
        return False
    return True

def _classify_science_status(
    total_points: int,
    accepted_points: int,
    n_objects: int,
    *,
    pipeline_result: PipelineRunResult | None = None,
) -> str:
    if pipeline_result is not None:
        qp = pipeline_result.quality_policy or {}
        if qp.get("detection_branch_enabled") and qp.get("detection_matching_executed") and int(qp.get("detection_matches_count") or 0) <= 0:
            return "WARNING_NO_DETECTION_MATCHES"
    if int(total_points or 0) <= 0:
        return "WARNING_NO_MEASUREMENTS"
    if int(n_objects or 0) <= 0:
        return "WARNING_NO_OBJECTS"
    if int(accepted_points or 0) <= 0:
        return "WARNING_NO_ACCEPTED_POINTS"
    return "OK"


def _instrument_units_for_paths(session: Session, raw_paths: list[str]) -> list[int]:
    if not raw_paths:
        return []
    stmt = text(
        """
        SELECT DISTINCT fr.instrument_unit_id
        FROM frames fr
        JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
        WHERE fl.locator IN :raw_paths AND fr.instrument_unit_id IS NOT NULL
        ORDER BY instrument_unit_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    rows = session.execute(stmt, {"raw_paths": raw_paths}).all()
    return [int(row[0]) for row in rows]


def _series_ids_for_paths(session: Session, raw_paths: list[str]) -> list[int]:
    if not raw_paths:
        return []
    stmt = text(
        """
        SELECT DISTINCT sf.series_id
        FROM field_series_frames sf
        JOIN frames fr ON fr.frame_id = sf.frame_id
        JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
        WHERE fl.locator IN :raw_paths
        ORDER BY sf.series_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    rows = session.execute(stmt, {"raw_paths": raw_paths}).all()
    return [int(row[0]) for row in rows]


def _frame_ids_by_series_for_paths(session: Session, raw_paths: list[str]) -> dict[int, list[int]]:
    """Return current input-path frame IDs grouped by series_id."""

    if not raw_paths:
        return {}
    stmt = text(
        """
        SELECT sf.series_id, fr.frame_id
        FROM field_series_frames sf
        JOIN frames fr ON fr.frame_id = sf.frame_id
        JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
        WHERE fl.locator IN :raw_paths
        ORDER BY sf.series_id, fr.frame_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    out: dict[int, list[int]] = {}
    for row in session.execute(stmt, {"raw_paths": raw_paths}).all():
        out.setdefault(int(row[0]), []).append(int(row[1]))
    return out


def _requested_series_membership(session: Session, series_ids: list[int]) -> dict[int, dict[int, str | None]]:
    stmt = text(
        """
        SELECT sf.series_id, sf.frame_id, o.observation_uid
        FROM field_series_frames sf
        JOIN field_series s ON s.series_id = sf.series_id
        JOIN frames fr ON fr.frame_id = sf.frame_id
        LEFT JOIN observations o ON o.observation_id = fr.observation_id
        WHERE sf.series_id IN :series_ids
        ORDER BY sf.series_id, sf.frame_id
        """
    ).bindparams(bindparam("series_ids", expanding=True))
    out: dict[int, dict[int, str | None]] = {}
    for sid, fid, uid in session.execute(stmt, {"series_ids": series_ids}).all():
        out.setdefault(int(sid), {})[int(fid)] = uid
    return out


def _series_frame_identities_for_paths(
    session: Session, raw_paths: list[str]
) -> list[tuple[int, int, str, str | None]]:
    if not raw_paths:
        return []
    stmt = text(
        """
        SELECT sf.series_id, fr.frame_id, fl.locator, o.observation_uid
        FROM field_series_frames sf
        JOIN frames fr ON fr.frame_id = sf.frame_id
        JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
        LEFT JOIN observations o ON o.observation_id = fr.observation_id
        WHERE fl.locator IN :raw_paths
        ORDER BY sf.series_id, fr.frame_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    return [(int(sid), int(fid), path, uid) for sid, fid, path, uid in
            session.execute(stmt, {"raw_paths": raw_paths}).all()]


def _load_series_for_pipeline(session: Session, series_ids: list[int]) -> list[dict[str, Any]]:
    if not series_ids:
        return []
    unique_ids = sorted(set(int(v) for v in series_ids))
    stmt = text(
        """
        SELECT
            s.series_id,
            s.field_id,
            f.field_name,
            s.instrument_unit_id,
            iu.instrument,
            iu.site,
            iu.tube,
            iu.camera,
            s.filter_name,
            COALESCE(s.ra_center_deg, f.ra_center_deg) AS ra_center_deg,
            COALESCE(s.dec_center_deg, f.dec_center_deg) AS dec_center_deg,
            f.radius_arcmin,
            s.time_start,
            s.time_end,
            s.mjd_start,
            s.mjd_end,
            s.n_frames,
            s.status
        FROM field_series s
        JOIN fields f ON f.field_id = s.field_id
        LEFT JOIN instrument_units iu ON iu.instrument_unit_id = s.instrument_unit_id
        WHERE s.series_id IN :series_ids
        ORDER BY s.time_start NULLS LAST, s.series_id
        """
    ).bindparams(bindparam("series_ids", expanding=True))
    return [_json_safe(dict(row)) for row in session.execute(stmt, {"series_ids": unique_ids}).mappings().all()]


def _separate_open_series(
    series_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Keep OPEN tails out of the pipeline queue while reporting their IDs."""

    finalized = []
    open_ids = []
    for series in series_rows:
        series_id = int(series["series_id"])
        if str(series.get("status", "")).upper() == "OPEN":
            open_ids.append(series_id)
        else:
            finalized.append(series)
    return finalized, sorted(set(open_ids))


def _format_calibration_prefix(template: str | None, series: dict[str, Any]) -> str:
    values = dict(series)
    values["date_yyyymmdd"] = _date_token(series.get("time_start"), series.get("mjd_start"))
    values["instrument_lower"] = _slug(series.get("instrument") or "instrument").lower()
    values["instrument"] = _slug(series.get("instrument") or "instrument")
    values["field_name"] = _slug(series.get("field_name") or f"field{series.get('field_id')}")
    values["tube"] = _slug(series.get("tube") or "tube")
    values["camera"] = _slug(series.get("camera") or "camera")
    values["filter_name"] = _slug(series.get("filter_name") or "filter")
    values["series_id"] = int(series.get("series_id"))
    fmt = template or "auto_{instrument_lower}_{date_yyyymmdd}_{field_name}_{tube}_{camera}_{filter_name}_s{series_id}_v1"
    return _slug(fmt.format(**values))


def _date_token(value: Any, mjd: Any = None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if value:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed.strftime("%Y%m%d")
        text = str(value)
        m = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", text)
        if m:
            return "".join(m.groups())
    return "nodate" if mjd is None else f"mjd{int(float(mjd))}"


def _pipeline_warning_message(
    series_id: int, summary: Any, *, sample_limit: int = 3
) -> str:
    if not isinstance(summary, dict):
        return f"series_id={series_id} pipeline_status=DONE_WITH_WARNINGS"

    def count(key: str) -> int:
        try:
            return max(0, int(summary.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    sample_descriptions: list[str] = []
    samples = summary.get("failure_samples")
    if isinstance(samples, list):
        sample_limit = max(0, int(sample_limit))
        for sample in samples:
            if len(sample_descriptions) >= sample_limit:
                break
            if not isinstance(sample, dict):
                continue
            try:
                frame_id = int(sample["frame_id"])
            except (KeyError, TypeError, ValueError):
                continue
            description = f"frame_id={frame_id}"
            stage = sample.get("stage")
            if isinstance(stage, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", stage):
                description += f" stage={stage}"
            sample_descriptions.append(description)

    message = (
        f"series_id={series_id} pipeline_status=DONE_WITH_WARNINGS "
        f"failed_frames={count('failed_frames')} "
        f"failed_measurements={count('failed_measurements')}"
    )
    if sample_descriptions:
        message += " samples=[" + "; ".join(sample_descriptions) + "]"
    samples_truncated = count("failure_samples_truncated")
    if samples_truncated:
        message += f" samples_truncated={samples_truncated}"
    return message


def _pipeline_row(series: dict[str, Any], prefix: str, result: PipelineRunResult) -> dict[str, Any]:
    return {
        "series_id": int(series["series_id"]),
        "field_id": series.get("field_id"),
        "field_name": series.get("field_name"),
        "instrument_unit_id": series.get("instrument_unit_id"),
        "filter_name": series.get("filter_name"),
        "calibration_prefix": prefix,
        "pipeline_run_id": result.pipeline_run_id,
        "status": result.status,
        "raw_calibration": result.calibration_names.get("raw"),
        "aperture_correction_calibration": result.calibration_names.get("aperture_correction"),
        "aperture_zp_calibration": result.calibration_names.get("aperture_zp"),
        "ensemble_calibration": result.calibration_names.get("ensemble"),
        "color_calibration": result.calibration_names.get("color"),
        "science_calibration": result.calibration_names.get("science") or result.calibration_names.get("ensemble"),
        "field_qc_report_json": result.outputs.get("field_qc_report_json"),
        "aperture_correction_summary_json": result.outputs.get("aperture_correction_summary_json"),
        "aperture_correction_frames_csv": result.outputs.get("aperture_correction_frames_csv"),
        "aperture_correction_context_summary_json": result.outputs.get(
            "aperture_correction_context_summary_json"
        ),
        "aperture_correction_context_frames_csv": result.outputs.get(
            "aperture_correction_context_frames_csv"
        ),
        "aperture_correction_context_plot_png": result.outputs.get(
            "aperture_correction_context_plot_png"
        ),
        "psf_sources_csv": result.outputs.get("psf_sources_csv"),
        "psf_frames_csv": result.outputs.get("psf_frames_csv"),
        "psf_summary_json": result.outputs.get("psf_summary_json"),
        "psf_field_map_png": result.outputs.get("psf_field_map_png"),
        "psf_time_series_png": result.outputs.get("psf_time_series_png"),
        "target_light_curve_csv": result.outputs.get("target_light_curve_csv"),
        "target_light_curve_json": result.outputs.get("target_light_curve_json"),
        "accepted_candidate_light_curves_dir": result.outputs.get("accepted_candidate_light_curves_dir"),
        "accepted_candidates_summary_csv": result.outputs.get("accepted_candidates_summary_csv"),
        "accepted_candidates_summary_json": result.outputs.get("accepted_candidates_summary_json"),
        "accepted_candidates_index_html": result.outputs.get("accepted_candidates_index_html"),
        "candidate_plot_count": result.outputs.get("candidate_plot_count"),
        "candidate_plot_warnings_count": result.outputs.get("candidate_plot_warnings_count"),
        "candidate_plot_backend": result.outputs.get("candidate_plot_backend"),
        "base_ensemble_reference_candidates": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_reference_candidates"
        ),
        "base_ensemble_usable_reference_objects": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_usable_reference_objects"
        ),
        "base_ensemble_required_references_per_frame": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_required_references_per_frame"
        ),
        "base_ensemble_selected_frames": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_selected_frames"
        ),
        "base_ensemble_frames_with_offsets": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_frames_with_offsets"
        ),
        "base_ensemble_calibrated": _step_metric(
            result, "ensemble_calibrate", "base_ensemble_calibrated"
        ),
        "log_file": result.log_files.get("log_file"),
        "error_log_file": result.log_files.get("error_log_file"),
        "artifact_run_dir": result.outputs.get("artifact_run_dir"),
        "artifact_registry_status": result.outputs.get("artifact_registry_status"),
        "artifact_count": result.outputs.get("artifact_count"),
        "quality_policy": dict(result.quality_policy),
        "steps": [
            {
                "step_name": step.step_name,
                "status": step.status,
                "elapsed_sec": step.elapsed_sec,
                "result": step.result,
                "error_message": step.error_message,
            }
            for step in result.steps
        ],
        "error_message": None,
    }


def _step_metric(result: PipelineRunResult, step_name: str, metric: str) -> Any:
    for step in result.steps:
        if step.step_name == step_name:
            return (step.result or {}).get(metric)
    return None



def _write_observation_selection_csv(
    result: ProcessArchiveResult,
    manifest_path: Path,
) -> Path | None:
    flattened: list[dict[str, Any]] = []
    for selection_index, item in enumerate(result.observation_selection_results, start=1):
        mode = item.get("mode")
        for row in item.get("rows") or []:
            flattened.append(
                {
                    "selection_index": selection_index,
                    "mode": mode,
                    **dict(row),
                }
            )
    if not flattened:
        return None

    csv_path = manifest_path.with_name(
        f"{manifest_path.stem}_observation_selection.csv"
    )
    columns = [
        "selection_index",
        "mode",
        "frame_id",
        "frame_uid",
        "raw_path",
        "instrument",
        "observation_id",
        "observation_uid",
        "identity_scheme",
        "native_identity",
        "identity_status",
        "action",
        "selected_frame_id",
        "selected_frame_uid",
        "product_frame_ids",
        "existing_series_ids",
        "reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for raw in flattened:
            row = dict(raw)
            for key in ("product_frame_ids", "existing_series_ids"):
                value = row.get(key)
                if isinstance(value, (list, dict)):
                    row[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            writer.writerow({key: row.get(key) for key in columns})
    return csv_path

def _write_manifest_if_requested(
    result: ProcessArchiveResult,
    manifest_out: str | Path | None,
    *,
    session: Session | None = None,
) -> None:
    if not manifest_out:
        return
    path = Path(manifest_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    result.manifest_path = str(path)
    selection_csv = _write_observation_selection_csv(result, path)
    result.observation_selection_csv_path = str(selection_csv) if selection_csv is not None else None
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # A process-archive manifest may span several pipeline runs. During B2 we
    # attach and snapshot it when there is exactly one recorded run; multi-run
    # manifests become batch-owned artifacts in B3.
    if session is None:
        return
    recorded = [row for row in result.pipeline_results if row.get("pipeline_run_id") is not None]
    if len(recorded) != 1:
        return
    row = recorded[0]
    run_dir = row.get("artifact_run_dir")
    if not run_dir:
        return
    try:
        snapshot_file_artifact(
            session,
            source_path=path,
            run_dir=Path(run_dir),
            relative_path=Path("manifests") / path.name,
            pipeline_run_id=int(row["pipeline_run_id"]),
            series_id=int(row["series_id"]) if row.get("series_id") is not None else None,
            artifact_type="PROCESS_ARCHIVE_MANIFEST",
            artifact_role="PROVENANCE",
            metadata={"process_status": result.status},
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - manifest itself remains available at the requested path.
        session.rollback()
        result.warnings.append(
            f"manifest artifact registration failed: {type(exc).__name__}: {exc}"
        )


def _snapshot_post_pipeline_artifact(
    session: Session,
    *,
    pipeline_result: PipelineRunResult,
    series_out_dir: str | Path,
    source_path: str | Path,
    series_id: int,
    artifact_type: str,
    artifact_role: str,
) -> None:
    if pipeline_result.pipeline_run_id is None:
        return
    source = Path(source_path)
    if not source.is_file():
        return
    publication_root = Path(series_out_dir)
    run_dir_value = pipeline_result.outputs.get("artifact_run_dir")
    run_dir = (
        Path(run_dir_value)
        if run_dir_value
        else run_artifact_directory(
            publication_root,
            run_uid=pipeline_result.run_uid,
            pipeline_run_id=pipeline_result.pipeline_run_id,
        )
    )
    try:
        relative_path = source.relative_to(publication_root)
    except ValueError:
        relative_path = Path("external") / source.name

    science_name = (
        pipeline_result.calibration_names.get("science")
        or pipeline_result.calibration_names.get("ensemble")
    )
    calibration_version_id = None
    if science_name:
        calibration_version_id = session.execute(
            text(
                "SELECT calibration_version_id FROM calibration_versions "
                "WHERE name = :name ORDER BY calibration_version_id DESC LIMIT 1"
            ),
            {"name": science_name},
        ).scalar_one_or_none()
    snapshot_file_artifact(
        session,
        source_path=source,
        run_dir=run_dir,
        relative_path=relative_path,
        pipeline_run_id=int(pipeline_result.pipeline_run_id),
        calibration_version_id=(int(calibration_version_id) if calibration_version_id is not None else None),
        series_id=int(series_id),
        artifact_type=artifact_type,
        artifact_role=artifact_role,
    )
    session.commit()


def _has_any_key(header: fits.Header, keys: set[str]) -> bool:
    return any(k in header for k in keys)


def _header_text(header: fits.Header, *keys: str) -> str:
    return " ".join(str(header.get(k, "")) for k in keys).upper()


def _str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _slug(value: Any) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"[^0-9A-Za-zА-Яа-я_+-]+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
