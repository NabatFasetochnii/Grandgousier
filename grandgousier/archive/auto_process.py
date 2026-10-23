from __future__ import annotations

import csv
import json
import math
import re
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
from grandgousier.catalogs.reference_photometry import (
    create_reference_photometry_csv,
    import_reference_photometry,
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
    effective_catalog_name = catalog_name or _default_auto_catalog_name(groups, root=root)
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

    detected_by_profile: dict[str, list[DetectedArchiveFile]] = {}
    for item in detected:
        detected_by_profile.setdefault(item.profile_name, []).append(item)

    indexed_paths: list[str] = []
    for profile_name, items in sorted(detected_by_profile.items()):
        profile = profiles[profile_name]
        paths = [Path(item.path) for item in items]
        idx = index_archive_paths(
            session=session,
            paths=paths,
            profile=profile,
            dry_run=False,
            progress_desc=f"Indexing {profile_name}",
        )
        indexed_paths.extend(str(p) for p in paths)
        result.index_results.append(
            {
                "profile_name": profile_name,
                "discovered": idx.discovered,
                "inserted": idx.inserted,
                "skipped_existing": idx.skipped_existing,
                "failed": idx.failed,
            }
        )

    indexed_frame_ids = _frame_ids_for_paths(session, indexed_paths)
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

    if catalog_name is None:
        effective_auto_catalog_mag_limit = _effective_auto_catalog_mag_limit(
            explicit=auto_catalog_mag_limit, working=working_catalog_mag_limit
        )
        effective_auto_catalog_max_radius_deg = _effective_auto_catalog_max_radius_deg(
            groups, explicit=auto_catalog_max_radius_deg
        )
        catalog_row = _prepare_auto_catalog(
            session,
            catalog_name=effective_catalog_name,
            frame_ids=indexed_frame_ids,
            enabled=auto_catalog_query,
            margin_arcmin=auto_catalog_margin_arcmin,
            mag_limit=effective_auto_catalog_mag_limit,
            max_rows_per_frame=auto_catalog_max_rows_per_frame,
            query_mode=auto_catalog_query_mode,
            max_radius_deg=effective_auto_catalog_max_radius_deg,
            timeout_sec=auto_catalog_timeout_sec,
            warnings=result.warnings,
        )
        result.catalog_results.append(catalog_row)
        if not _catalog_precondition_ok(catalog_row, allow_empty_catalog=allow_empty_catalog):
            result.status = "FAILED_PRECONDITION_CATALOG"
            result.warnings.append(
                "auto catalog did not produce usable sources; install/fix astroquery, retry later, pass --catalog-name, or use --allow-empty-catalog for debugging"
            )
            _write_manifest_if_requested(result, manifest_out, session=session)
            return result
    else:
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
                dry_run=False,
            )
            created = [int(row["series_id"]) for row in build_result.rows if row.get("series_id") is not None]
            result.created_series_ids.extend(created)
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
            created_series_ids.extend(created)

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

    for series in _load_series_for_pipeline(session, pipeline_series_ids):
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
            reference_result = _ensure_reference_photometry_for_series(
                session,
                series=series,
                policy=color_policy,
                identity_catalog_name=effective_catalog_name,
                out_dir=series_out_dir,
                allow_remote_fetch=auto_reference_photometry,
            )
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
        config = FieldPipelineConfig(
            catalog_name=effective_catalog_name,
            calibration_prefix=prefix,
            series_id=int(series["series_id"]),
            frame_ids=frame_ids_by_series_id.get(int(series["series_id"]), []),
            out_dir=series_out_dir,
            aperture_radius_pix=aperture_radius_pix,
            photometry_estimator=photometry_estimator,
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
                enable_color_calibration and color_policy and color_policy.enabled and color_reference_ready
            ),
            color_calibration_requested=bool(enable_color_calibration),
            color_reference_ready=(bool(color_reference_ready) if enable_color_calibration else None),
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
            pipeline_result = run_field_pipeline(session, config)
            pipe_row = _pipeline_row(series, prefix, pipeline_result)
            result.processed_series_ids.append(int(series["series_id"]))
            if reference_result is not None and reference_result.get("csv_path") and series_out_dir:
                try:
                    _snapshot_post_pipeline_artifact(
                        session,
                        pipeline_result=pipeline_result,
                        series_out_dir=series_out_dir,
                        source_path=str(reference_result["csv_path"]),
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
            elif pipeline_result.status == "DONE":
                pipe_row["science_status"] = "NO_LIGHT_CURVE_SUMMARY"
                result.warnings.append(f"series_id={series['series_id']} science_status=NO_LIGHT_CURVE_SUMMARY")
            result.pipeline_results.append(pipe_row)
        except Exception as exc:  # noqa: BLE001 - batch command reports per-series failures.
            # A failed SQL statement leaves PostgreSQL transactions aborted until
            # rollback.  Isolate series failures so the next series can still run.
            session.rollback()
            failure_status = (
                FAILED_PRECONDITION_ENSEMBLE_STATUS
                if isinstance(exc, EnsemblePreconditionError)
                else "FAILED"
            )
            result.pipeline_results.append(
                {
                    "series_id": int(series["series_id"]),
                    "field_name": series.get("field_name"),
                    "calibration_prefix": prefix,
                    "status": failure_status,
                    "error_message": f"{type(exc).__name__}: {exc}",
                    **(getattr(exc, "diagnostics", {}) if isinstance(getattr(exc, "diagnostics", None), dict) else {}),
                }
            )

    failed_rows = [
        row for row in result.pipeline_results
        if row.get("status") not in ("DONE", "DONE_NO_SCIENCE_FRAMES")
    ]
    failed = len(failed_rows)
    science_warnings = sum(1 for row in result.pipeline_results if row.get("science_status") not in (None, "OK"))
    if failed:
        if len(failed_rows) == len(result.pipeline_results) and all(
            row.get("status") == FAILED_PRECONDITION_ENSEMBLE_STATUS for row in failed_rows
        ):
            result.status = FAILED_PRECONDITION_ENSEMBLE_STATUS
        else:
            result.status = "FAILED"
    elif science_warnings or result.warnings:
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
                {"section": "catalog", "metric": "queried_frames", "value": item.get("queried_frames")},
                {"section": "catalog", "metric": "queried_cones", "value": item.get("queried_cones")},
                {"section": "catalog", "metric": "row_limit_reached_cones", "value": item.get("row_limit_reached_cones")},
                {"section": "catalog", "metric": "read_rows", "value": item.get("read_rows")},
                {"section": "catalog", "metric": "inserted_or_updated", "value": item.get("inserted_or_updated")},
                {"section": "catalog", "metric": "n_sources", "value": item.get("n_sources")},
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
        file_size_bytes=item.file_size_bytes,
        naxis1=item.naxis1,
        naxis2=item.naxis2,
        object_name=item.object_name,
        identity_status=item.observation_identity_status,
        identity_detail=item.observation_identity_detail,
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



def _ensure_reference_photometry_for_series(
    session: Session,
    *,
    series: dict[str, Any],
    policy: ColorCalibrationPolicy,
    identity_catalog_name: str,
    out_dir: str | Path | None,
    allow_remote_fetch: bool,
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
    }

    try:
        before_counts = _reference_band_counts_for_series(
            session,
            series=series,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=provider_catalog_name,
        )
    except Exception as exc:  # noqa: BLE001 - optional reference layer must not destroy ensemble output.
        session.rollback()
        return {
            **base,
            "status": "REFERENCE_COVERAGE_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    base["band_counts_before"] = before_counts
    if _reference_band_coverage_ready(before_counts, required_bands, minimum_sources):
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
            series=series,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=scoped_catalog_name,
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {
            **base,
            "status": "SCOPED_REFERENCE_COVERAGE_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    base["scoped_band_counts_before"] = scoped_counts
    if _reference_band_coverage_ready(scoped_counts, required_bands, minimum_sources):
        return {
            **base,
            "status": "READY_EXISTING_SCOPED",
            "ready": True,
            "effective_reference_catalog_name": scoped_catalog_name,
            "reference_namespace_mode": "identity_scoped",
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

    target_dir = Path(out_dir) / "reference_photometry" if out_dir else Path("exports") / "reference_photometry"
    csv_path = target_dir / f"series_{series_id}_{provider_catalog_name}.csv"
    max_mag_err = None
    if policy.max_reference_mag_err is not None:
        max_mag_err = {band: float(policy.max_reference_mag_err) for band in required_bands}
    min_mag = {str(policy.reference_band): float(policy.min_reference_mag)} if policy.min_reference_mag is not None else None
    max_mag = {str(policy.reference_band): float(policy.max_reference_mag)} if policy.max_reference_mag is not None else None

    try:
        fetched = create_reference_photometry_csv(
            session,
            out_path=csv_path,
            provider=provider_catalog_name,
            series_id=series_id,
            overwrite=True,
            require_bands=required_bands,
            min_mag=min_mag,
            max_mag=max_mag,
            max_mag_err=max_mag_err,
        )
        imported = import_reference_photometry(
            session,
            path=csv_path,
            catalog_name=scoped_catalog_name,
            catalog_version="dr2",
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
            max_match_arcsec=1.0,
        )
    except Exception as exc:  # noqa: BLE001 - reference acquisition is an optional science refinement.
        session.rollback()
        return {
            **base,
            "status": "REFERENCE_ACQUISITION_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
            "csv_path": str(csv_path),
        }

    try:
        after_counts = _reference_band_counts_for_series(
            session,
            series=series,
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=scoped_catalog_name,
        )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        return {
            **base,
            "status": "REFERENCE_POST_IMPORT_CHECK_FAILED",
            "reason": f"{type(exc).__name__}: {exc}",
            "csv_path": str(csv_path),
            "remote_rows": fetched.raw_rows,
            "written_rows": fetched.written_rows,
            "imported_or_updated": imported.inserted_or_updated,
            "matched_to_identity": imported.matched_to_gaia,
        }
    ready = _reference_band_coverage_ready(after_counts, required_bands, minimum_sources)
    return {
        **base,
        "status": "FETCHED_IMPORTED" if ready else "INSUFFICIENT_REFERENCE_COVERAGE",
        "ready": ready,
        "reason": None if ready else "reference import completed but too few matched sources remain",
        "csv_path": str(csv_path),
        "remote_rows": fetched.raw_rows,
        "written_rows": fetched.written_rows,
        "imported_or_updated": imported.inserted_or_updated,
        "matched_to_identity": imported.matched_to_gaia,
        "unmatched_to_identity": imported.unmatched_to_gaia,
        "band_counts_after": after_counts,
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
    series: dict[str, Any],
    identity_catalog_name: str,
    reference_catalog_name: str,
) -> dict[str, int]:
    """Count matched reference bands inside the series field before frame links exist."""

    ra = series.get("ra_center_deg")
    dec = series.get("dec_center_deg")
    radius_arcmin = series.get("radius_arcmin") or 30.0
    if ra is None or dec is None:
        return {}
    radius_deg = max(float(radius_arcmin) / 60.0, 1.0 / 3600.0)
    cos_dec = max(abs(math.cos(math.radians(float(dec)))), 0.01)
    ra_half_width = min(180.0, radius_deg / cos_dec)
    rows = session.execute(
        text(
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
              AND rps.identity_source_id IS NOT NULL
              AND rps.dec_deg BETWEEN :dec_min AND :dec_max
              AND ABS(DEGREES(ATAN2(SIN(RADIANS(rps.ra_deg - :ra_center)), COS(RADIANS(rps.ra_deg - :ra_center))))) <= :ra_half_width
            GROUP BY pb.canonical_name
            """
        ),
        {
            "reference_catalog_name": reference_catalog_name,
            "identity_catalog_name": identity_catalog_name,
            "dec_min": max(-90.0, float(dec) - radius_deg),
            "dec_max": min(90.0, float(dec) + radius_deg),
            "ra_center": float(ra),
            "ra_half_width": ra_half_width,
        },
    ).mappings().all()
    return {str(row["band"]): int(row["n_sources"] or 0) for row in rows}


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

def _prepare_auto_catalog(
    session: Session,
    *,
    catalog_name: str,
    frame_ids: list[int],
    enabled: bool,
    margin_arcmin: float,
    mag_limit: float | None,
    max_rows_per_frame: int,
    query_mode: str,
    max_radius_deg: float | None,
    timeout_sec: float | None,
    warnings: list[str],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mode": "auto_gaia_dr3",
        "catalog_name": catalog_name,
        "enabled": bool(enabled),
        "frame_ids_count": len(frame_ids),
        "query_mode": query_mode,
        "max_radius_deg": max_radius_deg,
        "mag_limit": mag_limit,
        "timeout_sec": timeout_sec,
        "status": "SKIPPED_DISABLED" if not enabled else "RUNNING",
    }

    # Auto-generated catalog names are deterministic for the same archive group.
    # A clean Stage-2 rebuild deliberately preserves CatalogSource identity, so
    # querying Gaia again here would be both unnecessary and harmful: it makes
    # a rebuild depend on network availability and can replace a previously
    # established catalog namespace.  Reuse a populated exact-name catalog
    # before considering any remote query.
    existing = _inspect_existing_catalog(session, catalog_name=catalog_name)
    existing_sources = int(existing.get("n_sources") or 0)
    if existing_sources > 0:
        row.update(
            {
                "mode": "auto_existing_reuse",
                "status": "DONE",
                "reused_existing": True,
                "n_sources": existing_sources,
                "inserted_or_updated": existing_sources,
                "selected_frames": 0,
                "queried_frames": 0,
                "queried_cones": 0,
                "effective_query_mode": "local_reuse",
                "row_limit_reached_cones": 0,
                "query_provider": "local_preserved_catalog",
                "query_provider_failures": {},
                "read_rows": 0,
                "failed_frames": 0,
                "failed_footprint_reasons": {},
                "sample_failed_footprints": [],
                "skipped_missing_file": 0,
                "skipped_oversized_cones": 0,
            }
        )
        return row

    if not enabled:
        warnings.append(f"auto catalog name generated but Gaia query disabled: {catalog_name}")
        return row
    if not frame_ids:
        row["status"] = "SKIPPED_NO_FRAMES"
        warnings.append(f"auto catalog query skipped because no indexed frames were found: {catalog_name}")
        return row
    try:
        from grandgousier.catalogs.gaia_query import query_gaia_for_frames

        res = query_gaia_for_frames(
            session,
            catalog_name=catalog_name,
            frame_ids=frame_ids,
            only_wcs_ok=False,
            margin_arcmin=margin_arcmin,
            mag_limit=mag_limit,
            max_rows_per_frame=max_rows_per_frame,
            query_mode=query_mode,
            max_radius_deg=max_radius_deg,
            gaia_timeout_sec=timeout_sec,
            match=False,
            dry_run=False,
        )
        row_limit_reached_cones = int(getattr(res, "row_limit_reached_cones", 0) or 0)
        if res.failed_frames > 0:
            status = "DONE_WITH_FAILED_FRAMES"
        elif row_limit_reached_cones > 0:
            status = "DONE_WITH_WARNINGS"
        else:
            status = "DONE"
        row.update(
            {
                "status": status,
                "selected_frames": res.selected_frames,
                "queried_frames": res.queried_frames,
                "queried_cones": getattr(res, "queried_cones", None),
                "query_mode": getattr(res, "query_mode", query_mode),
                "effective_query_mode": getattr(res, "effective_query_mode", query_mode),
                "row_limit_reached_cones": row_limit_reached_cones,
                "query_provider": getattr(res, "query_provider", None),
                "query_provider_failures": getattr(res, "query_provider_failures", {}),
                "read_rows": res.read_rows,
                "inserted_or_updated": res.inserted_or_updated,
                "failed_frames": res.failed_frames,
                "failed_footprint_reasons": getattr(res, "failed_footprint_reasons", {}),
                "sample_failed_footprints": getattr(res, "sample_failed_footprints", []),
                "skipped_missing_file": res.skipped_missing_file,
                "skipped_oversized_cones": getattr(res, "skipped_oversized_cones", 0),
            }
        )
        if res.inserted_or_updated == 0:
            warnings.append(f"auto catalog query inserted zero sources: {catalog_name}")
        if row_limit_reached_cones > 0:
            warnings.append(
                f"auto Gaia query reached MAXREC={max_rows_per_frame} in {row_limit_reached_cones} cone(s); "
                "catalog may be truncated; use a brighter --auto-catalog-mag-limit or raise --auto-catalog-max-rows-per-frame"
            )
        return row
    except Exception as exc:  # noqa: BLE001 - one-command processing should continue and report the catalog failure.
        session.rollback()
        row.update({"status": "FAILED", "error_message": f"{type(exc).__name__}: {exc}"})
        warnings.append(f"auto catalog query failed for {catalog_name}: {type(exc).__name__}: {exc}")
        return row



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
            s.n_frames
        FROM field_series s
        JOIN fields f ON f.field_id = s.field_id
        LEFT JOIN instrument_units iu ON iu.instrument_unit_id = s.instrument_unit_id
        WHERE s.series_id IN :series_ids
        ORDER BY s.time_start NULLS LAST, s.series_id
        """
    ).bindparams(bindparam("series_ids", expanding=True))
    return [_json_safe(dict(row)) for row in session.execute(stmt, {"series_ids": unique_ids}).mappings().all()]


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
