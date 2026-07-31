from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from astropy.io import fits
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from grandgousier.archive.fields_series import build_field_series
from grandgousier.instruments.base import InstrumentProfile
from grandgousier.io.archive_indexer import IndexResult, index_archive_paths
from grandgousier.io.parsing import parse_datetime, to_float
from grandgousier.lightcurves.product import (
    build_light_curve_summary,
    export_light_curve_summary_json,
    light_curve_reject_flags,
)
from grandgousier.pipeline.runner import FieldPipelineConfig, PipelineRunResult, run_field_pipeline


DEFAULT_EXTENSIONS: tuple[str, ...] = (".fits", ".fit", ".fts")
PROFILE_CONFIGS: dict[str, str] = {
    "MASTER": "configs/master.yaml",
    "RoboPhot": "configs/robophot.yaml",
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
            "warnings": self.warnings,
            "manifest_path": self.manifest_path,
        }


def default_profile_paths() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[2]
    return {name: root / rel for name, rel in PROFILE_CONFIGS.items()}


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
    profiles = load_auto_profiles(master_profile=master_profile, robophot_profile=robophot_profile)
    paths = discover_fits_files(root, recursive=recursive, extensions=extensions, limit=limit)
    detected: list[DetectedArchiveFile] = []
    warnings: list[str] = []
    profile_mode = _normalize_profile_mode(instrument_profile)

    for path in paths:
        try:
            header = fits.getheader(path, 0)
            profile = _select_profile(header, profiles, profile_mode=profile_mode)
            detected.append(_detected_file_from_header(path, header, profile))
        except Exception as exc:  # noqa: BLE001 - scan must continue over bad files.
            warnings.append(f"failed_to_read_header: {path}: {type(exc).__name__}: {exc}")

    groups = _build_group_plans(detected, profiles=profiles, example_limit=example_limit)
    return detected, groups, warnings


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
    limit: int | None = None,
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
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    bulk_insert_batch_size: int = 1000,
    make_light_curve_summary: bool = True,
    enable_detection_branch: bool = False,
    enable_aperture_stability_qc: bool = False,
    require_detection_support: bool | None = None,
    target_source_id: str | None = None,
    target_object_id: int | None = None,
    target_max_sep_arcsec: float | None = 120.0,
    export_target_light_curve: bool = True,
    export_candidate_light_curves: bool = True,
    export_candidate_plots: bool = True,
    candidate_light_curve_limit: int | None = None,
    exclude_candidates_from_ensemble: bool = False,
    candidate_ensemble_max_iterations: int = 2,
    candidate_ensemble_until_converged: bool = False,
    candidate_ensemble_convergence_max_iterations: int = 10,
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
    auto_catalog_query: bool = True,
    auto_catalog_margin_arcmin: float = 2.0,
    auto_catalog_mag_limit: float | None = None,
    auto_catalog_max_rows_per_frame: int = 50000,
    auto_catalog_query_mode: str = "frame_union",
    auto_catalog_max_radius_deg: float | None = 1.0,
    auto_catalog_timeout_sec: float | None = 120.0,
    allow_empty_catalog: bool = False,
    manifest_out: str | Path | None = None,
) -> ProcessArchiveResult:
    detected, groups, warnings = scan_archive_path(
        root,
        instrument_profile=instrument_profile,
        master_profile=master_profile,
        robophot_profile=robophot_profile,
        recursive=recursive,
        extensions=extensions,
        limit=limit,
    )
    profiles = load_auto_profiles(master_profile=master_profile, robophot_profile=robophot_profile)
    effective_catalog_name = catalog_name or _default_auto_catalog_name(groups, root=root)
    result = ProcessArchiveResult(
        status="PLAN" if not execute else "RUNNING",
        path=str(root),
        catalog_name=effective_catalog_name,
        execute=execute,
        discovered_files=len(discover_fits_files(root, recursive=recursive, extensions=extensions, limit=limit)),
        detected_files=len(detected),
        failed_files=len(warnings),
        groups=groups,
        warnings=warnings,
    )

    if not execute:
        result.status = "PLAN"
        _write_manifest_if_requested(result, manifest_out)
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

    if catalog_name is None:
        catalog_row = _prepare_auto_catalog(
            session,
            catalog_name=effective_catalog_name,
            frame_ids=indexed_frame_ids,
            enabled=auto_catalog_query,
            margin_arcmin=auto_catalog_margin_arcmin,
            mag_limit=auto_catalog_mag_limit,
            max_rows_per_frame=auto_catalog_max_rows_per_frame,
            query_mode=auto_catalog_query_mode,
            max_radius_deg=auto_catalog_max_radius_deg,
            timeout_sec=auto_catalog_timeout_sec,
            warnings=result.warnings,
        )
        result.catalog_results.append(catalog_row)
        if not _catalog_precondition_ok(catalog_row, allow_empty_catalog=allow_empty_catalog):
            result.status = "FAILED_PRECONDITION_CATALOG"
            result.warnings.append(
                "auto catalog did not produce usable sources; install/fix astroquery, retry later, pass --catalog-name, or use --allow-empty-catalog for debugging"
            )
            _write_manifest_if_requested(result, manifest_out)
            return result

    frame_ids_by_unit = _frame_ids_by_instrument_unit(session, indexed_frame_ids)
    series_ids: list[int] = []
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
                "rows": [_json_safe(row) for row in build_result.rows],
            }
        )
        series_ids.extend(created)

    # Also include existing series that already contain input-path frames.  This keeps
    # reruns idempotent while avoiding unrelated historical series from the same
    # instrument unit.
    series_ids = _merge_current_series_ids(series_ids, _series_ids_for_paths(session, indexed_paths))
    frame_ids_by_series_id = _frame_ids_by_series_for_paths(session, indexed_paths)

    for series in _load_series_for_pipeline(session, series_ids):
        prefix = _format_calibration_prefix(calibration_prefix_template, series)
        series_out_dir = str(Path(out_root) / prefix) if out_root else None
        config = FieldPipelineConfig(
            catalog_name=effective_catalog_name,
            calibration_prefix=prefix,
            series_id=int(series["series_id"]),
            frame_ids=frame_ids_by_series_id.get(int(series["series_id"]), []),
            out_dir=series_out_dir,
            aperture_radius_pix=aperture_radius_pix,
            annulus_inner_pix=annulus_inner_pix,
            annulus_outer_pix=annulus_outer_pix,
            bulk_insert_batch_size=bulk_insert_batch_size,
            skip_detection=not enable_detection_branch,
            skip_detection_catalog_match=not enable_detection_branch,
            require_detection_support=require_detection_support,
            target_source_id=target_source_id,
            target_object_id=target_object_id,
            target_max_sep_arcsec=target_max_sep_arcsec,
            export_target_light_curve=export_target_light_curve,
            export_candidate_light_curves=export_candidate_light_curves,
            export_candidate_plots=export_candidate_plots,
            candidate_light_curve_limit=candidate_light_curve_limit,
            exclude_candidates_from_ensemble=exclude_candidates_from_ensemble,
            candidate_ensemble_max_iterations=candidate_ensemble_max_iterations,
            candidate_ensemble_until_converged=candidate_ensemble_until_converged,
            candidate_ensemble_convergence_max_iterations=candidate_ensemble_convergence_max_iterations,
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
                "enable_aperture_stability_qc": enable_aperture_stability_qc,
                "require_detection_support": require_detection_support,
                "target_source_id": target_source_id,
                "target_object_id": target_object_id,
                "target_max_sep_arcsec": target_max_sep_arcsec,
                "export_target_light_curve": export_target_light_curve,
                "export_candidate_light_curves": export_candidate_light_curves,
                "export_candidate_plots": export_candidate_plots,
                "candidate_light_curve_limit": candidate_light_curve_limit,
                "exclude_candidates_from_ensemble": exclude_candidates_from_ensemble,
                "candidate_ensemble_max_iterations": candidate_ensemble_max_iterations,
                "candidate_ensemble_until_converged": candidate_ensemble_until_converged,
                "candidate_ensemble_convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
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
            elif make_light_curve_summary and pipeline_result.calibration_names.get("ensemble"):
                summary_path = None
                if series_out_dir:
                    summary_path = str(Path(series_out_dir) / f"{prefix}_light_curve_summary.json")
                require_detection_support_effective = bool(
                    pipeline_result.quality_policy.get("require_detection_support_effective", enable_detection_branch)
                )
                summary = build_light_curve_summary(
                    session,
                    calibration_names=[pipeline_result.calibration_names["ensemble"]],
                    catalog_name=effective_catalog_name,
                    series_id=int(series["series_id"]),
                    reject_flags=light_curve_reject_flags(require_detection_support=require_detection_support_effective),
                )
                if summary_path:
                    export_light_curve_summary_json(summary, summary_path)
                science_status = _classify_science_status(
                    summary.total_points,
                    summary.accepted_points,
                    summary.n_objects,
                    pipeline_result=pipeline_result,
                )
                summary_row = {
                    "series_id": int(series["series_id"]),
                    "calibration_name": pipeline_result.calibration_names["ensemble"],
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
            result.pipeline_results.append(
                {
                    "series_id": int(series["series_id"]),
                    "field_name": series.get("field_name"),
                    "calibration_prefix": prefix,
                    "status": "FAILED",
                    "error_message": f"{type(exc).__name__}: {exc}",
                }
            )

    failed = sum(1 for row in result.pipeline_results if row.get("status") not in ("DONE", "DONE_NO_SCIENCE_FRAMES"))
    science_warnings = sum(1 for row in result.pipeline_results if row.get("science_status") not in (None, "OK"))
    if failed:
        result.status = "FAILED"
    elif science_warnings or result.warnings:
        result.status = "DONE_WITH_WARNINGS"
    else:
        result.status = "DONE"
    _write_manifest_if_requested(result, manifest_out)
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
    for item in result.catalog_results:
        rows.extend(
            [
                {"section": "catalog", "metric": "mode", "value": item.get("mode")},
                {"section": "catalog", "metric": "query_mode", "value": item.get("query_mode")},
                {"section": "catalog", "metric": "queried_frames", "value": item.get("queried_frames")},
                {"section": "catalog", "metric": "queried_cones", "value": item.get("queried_cones")},
                {"section": "catalog", "metric": "read_rows", "value": item.get("read_rows")},
                {"section": "catalog", "metric": "inserted_or_updated", "value": item.get("inserted_or_updated")},
                {"section": "catalog", "metric": "failed_frames", "value": item.get("failed_frames")},
                {"section": "catalog", "metric": "failed_footprint_reasons", "value": item.get("failed_footprint_reasons")},
                {"section": "catalog", "metric": "skipped_missing_file", "value": item.get("skipped_missing_file")},
                {"section": "catalog", "metric": "skipped_oversized_cones", "value": item.get("skipped_oversized_cones")},
                {"section": "catalog", "metric": "status", "value": item.get("status")},
            ]
        )
    for i, pipe in enumerate(result.pipeline_results, start=1):
        rows.extend(
            [
                {"section": f"pipeline:{i}", "metric": "series_id", "value": pipe.get("series_id")},
                {"section": f"pipeline:{i}", "metric": "field_name", "value": pipe.get("field_name")},
                {"section": f"pipeline:{i}", "metric": "calibration_prefix", "value": pipe.get("calibration_prefix")},
                {"section": f"pipeline:{i}", "metric": "status", "value": pipe.get("status")},
                {"section": f"pipeline:{i}", "metric": "science_status", "value": pipe.get("science_status")},
                {"section": f"pipeline:{i}", "metric": "total_points", "value": pipe.get("total_points")},
                {"section": f"pipeline:{i}", "metric": "accepted_points", "value": pipe.get("accepted_points")},
                {"section": f"pipeline:{i}", "metric": "ensemble", "value": pipe.get("ensemble_calibration")},
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
    return DetectedArchiveFile(
        path=str(path),
        profile_name=profile.name,
        instrument=profile.name,
        site=_str_or_none(profile.get_header_value(header, "site")),
        tube=_str_or_none(profile.get_header_value(header, "tube")),
        camera=_str_or_none(profile.get_header_value(header, "camera")),
        filter_name=profile.normalize_filter(profile.get_header_value(header, "filter")),
        object_name=_str_or_none(profile.get_header_value(header, "object")),
        date_obs=parse_datetime(profile.get_header_value(header, "date_obs")),
        mjd=mjd,
        image_type=profile.normalize_image_type(profile.get_header_value(header, "image_type")),
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
        SELECT frame_id
        FROM frames
        WHERE raw_path IN :raw_paths
        ORDER BY frame_id
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


def _merge_current_series_ids(created_series_ids: list[int], existing_series_ids: list[int]) -> list[int]:
    return sorted({int(v) for v in [*created_series_ids, *existing_series_ids] if v is not None})


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
        "timeout_sec": timeout_sec,
        "status": "SKIPPED_DISABLED" if not enabled else "RUNNING",
    }
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
        row.update(
            {
                "status": "DONE" if res.failed_frames == 0 else "DONE_WITH_FAILED_FRAMES",
                "selected_frames": res.selected_frames,
                "queried_frames": res.queried_frames,
                "queried_cones": getattr(res, "queried_cones", None),
                "query_mode": getattr(res, "query_mode", query_mode),
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
    if status not in {"DONE", "DONE_WITH_FAILED_FRAMES"}:
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
        SELECT DISTINCT instrument_unit_id
        FROM frames
        WHERE raw_path IN :raw_paths AND instrument_unit_id IS NOT NULL
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
        WHERE fr.raw_path IN :raw_paths
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
        WHERE fr.raw_path IN :raw_paths
        ORDER BY sf.series_id, fr.frame_id
        """
    ).bindparams(bindparam("raw_paths", expanding=True))
    out: dict[int, list[int]] = {}
    for row in session.execute(stmt, {"raw_paths": raw_paths}).all():
        out.setdefault(int(row[0]), []).append(int(row[1]))
    return out


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
        "aperture_zp_calibration": result.calibration_names.get("aperture_zp"),
        "ensemble_calibration": result.calibration_names.get("ensemble"),
        "field_qc_report_json": result.outputs.get("field_qc_report_json"),
        "target_light_curve_csv": result.outputs.get("target_light_curve_csv"),
        "target_light_curve_json": result.outputs.get("target_light_curve_json"),
        "accepted_candidate_light_curves_dir": result.outputs.get("accepted_candidate_light_curves_dir"),
        "accepted_candidates_summary_csv": result.outputs.get("accepted_candidates_summary_csv"),
        "accepted_candidates_summary_json": result.outputs.get("accepted_candidates_summary_json"),
        "accepted_candidates_index_html": result.outputs.get("accepted_candidates_index_html"),
        "candidate_plot_count": result.outputs.get("candidate_plot_count"),
        "candidate_plot_warnings_count": result.outputs.get("candidate_plot_warnings_count"),
        "candidate_plot_backend": result.outputs.get("candidate_plot_backend"),
        "log_file": result.log_files.get("log_file"),
        "error_log_file": result.log_files.get("error_log_file"),
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


def _write_manifest_if_requested(result: ProcessArchiveResult, manifest_out: str | Path | None) -> None:
    if not manifest_out:
        return
    path = Path(manifest_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    result.manifest_path = str(path)


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
