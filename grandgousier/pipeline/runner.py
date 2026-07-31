from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE

from grandgousier.astrometry.wcs_check import check_frames_wcs
from grandgousier.calibration.aperture_zp import aperture_zp_calibrate
from grandgousier.calibration.ensemble import ensemble_calibrate_measurements
from grandgousier.catalogs.frame_match import match_catalog_to_frames
from grandgousier.photometry.forced_aperture import run_forced_aperture_photometry
from grandgousier.photometry.source_detection import detect_sources_in_frames
from grandgousier.quality.detection_catalog_qc import match_detections_to_catalog
from grandgousier.quality.candidate_aperture_stability import (
    compute_candidate_aperture_stability_qc,
    export_candidate_aperture_stability_csv,
)
from grandgousier.quality.field_report import (
    build_field_qc_report,
    export_field_qc_json,
    export_field_qc_tables,
)
from grandgousier.quality.object_quality import compute_object_qc
from grandgousier.quality.frame_pre_qc import filter_bad_pre_qc_frame_ids, run_frame_pre_qc
from grandgousier.lightcurves.product import light_curve_reject_flags
from grandgousier.lightcurves.candidates import export_accepted_candidate_light_curves
from grandgousier.lightcurves.target import TargetResolution, export_target_light_curve_product, resolve_target_object
from grandgousier.logging_utils import PipelineFileLogger
from grandgousier.archive.fields_series import (
    load_series_selection,
    mark_series_pipeline_result,
    mark_series_pipeline_started,
)


PIPELINE_RECIPE_VERSION = "field_pipeline_v2"
NO_USABLE_FRAMES_STATUS = "DONE_NO_SCIENCE_FRAMES"


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
    outputs: dict[str, str] = field(default_factory=dict)
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
    margin_pix: float = 2.0
    aperture_radius_pix: float = 3.0
    annulus_inner_pix: float = 8.0
    annulus_outer_pix: float = 12.0
    detection_threshold_sigma: float = 5.0
    detection_max_sources_per_frame: int = 2000
    detection_min_separation_pix: float = 5.0
    detection_match_radius_arcsec: float = 2.0
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
    require_detection_support: bool | None = None
    target_source_id: str | None = None
    target_object_id: int | None = None
    target_max_sep_arcsec: float | None = 120.0
    export_target_light_curve: bool = True
    export_candidate_light_curves: bool = True
    export_candidate_plots: bool = True
    candidate_light_curve_limit: int | None = None
    exclude_candidates_from_ensemble: bool = False
    candidate_ensemble_max_iterations: int = 2
    candidate_ensemble_until_converged: bool = False
    candidate_ensemble_convergence_max_iterations: int = 10
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
    log_dir: str | None = "logs"
    file_log_level: str = "INFO"
    no_file_log: bool = False
    command_json: dict[str, Any] = field(default_factory=dict)
    environment_json: dict[str, Any] = field(default_factory=dict)


def run_field_pipeline(session: Session, config: FieldPipelineConfig) -> PipelineRunResult:
    """Run the main field-processing chain and optionally record its provenance.

    The runner intentionally orchestrates the already-tested low-level steps instead of
    duplicating their logic. It is the reproducibility layer: it records the input recipe,
    the generated calibration names, and per-step result counters in `pipeline_runs` and
    `pipeline_run_steps`.
    """

    run_uid = config.run_uid or _make_run_uid(config.calibration_prefix)
    calibration_names = _calibration_names(config)
    outputs: dict[str, str] = {}
    pipeline_run_id: int | None = None

    selected_frame_ids = list(config.frame_ids or [])
    has_explicit_frame_selection = config.frame_ids is not None
    stop_due_to_no_usable_frames = False
    target_resolution: dict[str, Any] = {"resolved": False, "method": "not_run"}
    target_object_ids: list[int] = []
    latest_candidate_rows: list[dict[str, Any]] = []
    current_ensemble_name = calibration_names["ensemble"]
    initial_ensemble_name = calibration_names["ensemble"]
    candidate_exclusion_object_ids: list[int] = []
    candidate_exclusion_new_object_ids: list[int] = []
    candidate_ensemble_fixed_max_iterations = max(1, int(config.candidate_ensemble_max_iterations or 1))
    candidate_ensemble_convergence_max_iterations = max(2, int(config.candidate_ensemble_convergence_max_iterations or 10))
    candidate_ensemble_iteration_limit = (
        candidate_ensemble_convergence_max_iterations
        if config.candidate_ensemble_until_converged
        else candidate_ensemble_fixed_max_iterations
    )
    field_id = config.field_id
    series_id = config.series_id
    if series_id is not None:
        series = load_series_selection(session, int(series_id))
        series_frame_ids = [int(fid) for fid in series["frame_ids"]]
        if has_explicit_frame_selection:
            series_frame_id_set = set(series_frame_ids)
            selected_frame_ids = [int(fid) for fid in selected_frame_ids if int(fid) in series_frame_id_set]
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
    recipe["recipe_version"] = PIPELINE_RECIPE_VERSION
    recipe["calibration_names"] = calibration_names
    recipe["field_id"] = field_id
    recipe["series_id"] = series_id
    recipe["frame_ids"] = selected_frame_ids
    recipe["quality_policy"] = {
        "detection_branch_enabled": _detection_branch_enabled(config),
        "require_detection_support_requested": config.require_detection_support,
        "require_detection_support_effective": require_detection_support_effective,
        "target_source_id": config.target_source_id,
        "target_object_id": config.target_object_id,
        "export_target_light_curve": config.export_target_light_curve,
        "exclude_candidates_from_ensemble": config.exclude_candidates_from_ensemble,
        "candidate_ensemble_max_iterations": candidate_ensemble_fixed_max_iterations,
        "candidate_ensemble_until_converged": config.candidate_ensemble_until_converged,
        "candidate_ensemble_convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
        "candidate_ensemble_iteration_limit": candidate_ensemble_iteration_limit,
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
    )

    def add_step(step_name: str, fn: Callable[[], Any]) -> None:
        order_index = len(result.steps) + 1
        step_id = _insert_pipeline_step(session, pipeline_run_id, step_name, order_index) if pipeline_run_id else None
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
        )
        started = time.perf_counter()
        try:
            payload = fn()
            elapsed_sec = float(time.perf_counter() - started)
            payload_dict = _result_to_dict(payload)
            payload_dict["elapsed_sec"] = elapsed_sec
            _finish_pipeline_step(session, step_id, "DONE", payload_dict, None)
            result.steps.append(PipelineStepRecord(step_name=step_name, status="DONE", result=payload_dict, elapsed_sec=elapsed_sec))
            _emit_pipeline_log(
                session,
                file_logger,
                pipeline_run_id=pipeline_run_id,
                pipeline_run_step_id=step_id,
                step_name=step_name,
                level="INFO",
                event_type="step_done",
                message=f"step {step_name} finished",
                context=_compact_log_context(payload_dict),
            )
        except Exception as exc:  # noqa: BLE001 - runner must persist failure context before returning.
            elapsed_sec = float(time.perf_counter() - started)
            error_message = f"{type(exc).__name__}: {exc}"
            _finish_pipeline_step(session, step_id, "FAILED", {"elapsed_sec": elapsed_sec}, error_message)
            result.steps.append(
                PipelineStepRecord(step_name=step_name, status="FAILED", result={"elapsed_sec": elapsed_sec}, error_message=error_message, elapsed_sec=elapsed_sec)
            )
            if file_logger is not None:
                file_logger.exception(
                    exc=exc,
                    event_type="step_failed",
                    message=f"step {step_name} failed",
                    pipeline_run_id=pipeline_run_id,
                    pipeline_run_step_id=step_id,
                    step_name=step_name,
                    context={"order_index": order_index},
                )
            _insert_pipeline_log_event(
                session,
                pipeline_run_id=pipeline_run_id,
                pipeline_run_step_id=step_id,
                level="ERROR",
                event_type="step_failed",
                message=error_message,
                context_json={"step_name": step_name, "order_index": order_index},
            )
            if not config.continue_on_error:
                raise

    try:
        if not config.skip_wcs:
            add_step(
                "check_wcs",
                lambda: check_frames_wcs(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    current_status=None,
                    dry_run=config.dry_run,
                    update_center=True,
                    record_events=False,
                ),
            )

        if config.run_pre_photometry_qc:
            def _pre_qc_step() -> dict[str, Any]:
                nonlocal selected_frame_ids, stop_due_to_no_usable_frames
                res = run_frame_pre_qc(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    threshold_sigma=config.detection_threshold_sigma,
                    detection_cap=config.detection_max_sources_per_frame,
                    max_local_maxima=config.pre_qc_max_local_maxima,
                    max_bright_fraction=config.pre_qc_max_bright_fraction,
                    max_saturated_fraction=config.pre_qc_max_saturated_fraction,
                    dry_run=config.dry_run,
                )
                payload = res.summary()
                if config.exclude_bad_pre_qc_frames and not config.dry_run:
                    before = len(selected_frame_ids)
                    selected_frame_ids = filter_bad_pre_qc_frame_ids(session, selected_frame_ids)
                    payload["excluded_bad_frames"] = before - len(selected_frame_ids)
                    payload["remaining_frames"] = len(selected_frame_ids)
                    if before > 0 and not selected_frame_ids:
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

        if not stop_due_to_no_usable_frames and not config.skip_catalog_frame_match:
            add_step(
                "match_catalog_to_frames",
                lambda: match_catalog_to_frames(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    margin_pix=config.margin_pix,
                    mag_key=config.mag_key,
                    working_mag_limit=config.working_catalog_mag_limit,
                    max_sources_per_frame=config.max_working_sources_per_frame,
                    edge_margin_pix=config.catalog_edge_margin_pix,
                    replace_existing=config.replace_frame_catalog_links,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_detection:
            add_step(
                "detect_sources",
                lambda: detect_sources_in_frames(
                    session,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    threshold_sigma=config.detection_threshold_sigma,
                    max_sources_per_frame=config.detection_max_sources_per_frame,
                    min_separation_pix=config.detection_min_separation_pix,
                    aperture_radius_pix=config.aperture_radius_pix,
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    replace_existing=config.replace_detections,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_detection_catalog_match:
            add_step(
                "match_detections_to_catalog",
                lambda: match_detections_to_catalog(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    radius_arcsec=config.detection_match_radius_arcsec,
                    mag_key=config.mag_key,
                    replace_existing=config.replace_detection_matches,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_forced_photometry:
            add_step(
                "run_forced_photometry",
                lambda: run_forced_aperture_photometry(
                    session,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    aperture_radius_pix=config.aperture_radius_pix,
                    annulus_inner_pix=config.annulus_inner_pix,
                    annulus_outer_pix=config.annulus_outer_pix,
                    calibration_name=calibration_names["raw"],
                    update_processing_status=True,
                    reuse_existing=config.reuse_existing_photometry,
                    force_recompute=config.force_recompute_photometry,
                    dry_run=config.dry_run,
                    insert_batch_size=config.bulk_insert_batch_size,
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_aperture_zp:
            add_step(
                "aperture_zp_calibrate",
                lambda: aperture_zp_calibrate(
                    session,
                    source_calibration_name=calibration_names["raw"],
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
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_ensemble:
            def _resolve_target_step() -> dict[str, Any]:
                nonlocal target_resolution, target_object_ids
                source_name = calibration_names["aperture_zp"] if not config.skip_aperture_zp else calibration_names["raw"]
                target = resolve_target_object(
                    session,
                    calibration_name=source_name,
                    catalog_name=config.catalog_name,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    target_source_id=config.target_source_id,
                    target_object_id=config.target_object_id,
                    max_sep_arcsec=config.target_max_sep_arcsec,
                )
                target_resolution = target.to_dict()
                target_object_ids = [int(target.object_id)] if target.resolved and target.object_id is not None else []
                return dict(target_resolution)

            add_step("resolve_target_object", _resolve_target_step)

        if not stop_due_to_no_usable_frames and not config.skip_ensemble:
            add_step(
                "ensemble_calibrate",
                lambda: ensemble_calibrate_measurements(
                    session,
                    source_calibration_name=calibration_names["aperture_zp"],
                    target_calibration_name=calibration_names["ensemble"],
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
                    exclude_reference_object_ids=target_object_ids,
                ),
            )

        if not stop_due_to_no_usable_frames and not config.skip_object_qc:
            add_step(
                "compute_object_qc",
                lambda: compute_object_qc(
                    session,
                    calibration_name=current_ensemble_name,
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
                ),
            )

        if not stop_due_to_no_usable_frames and config.export_target_light_curve and not config.exclude_candidates_from_ensemble:
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
                    reject_flags=light_curve_reject_flags(require_detection_support=require_detection_support_effective),
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

        if not stop_due_to_no_usable_frames and not config.skip_field_report:
            def _field_report_step() -> dict[str, Any]:
                nonlocal latest_candidate_rows
                report = build_field_qc_report(
                    session,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    instrument_unit_id=config.instrument_unit_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
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
                )
                latest_candidate_rows = list(report.candidate_rows)
                payload: dict[str, Any] = {
                    "summary_rows": report.summary_rows,
                    "warnings": report.report.get("warnings", []),
                    "candidate_triage": report.report.get("candidate_triage", {}),
                    "blend_diagnostics": report.report.get("blend_diagnostics", {}),
                }
                if config.out_dir:
                    out_dir = Path(config.out_dir)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    json_path = out_dir / f"{config.calibration_prefix}_field_qc_report.json"
                    tables_prefix = out_dir / f"{config.calibration_prefix}_field_qc"
                    export_field_qc_json(report.report, json_path)
                    table_outputs = export_field_qc_tables(
                        prefix=tables_prefix,
                        rms_bins=report.rms_bins,
                        candidate_rows=report.candidate_rows,
                        blend_rows=report.blend_rows,
                    )
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
                    nonlocal candidate_exclusion_object_ids, candidate_exclusion_new_object_ids
                    previous_ids = set(candidate_exclusion_object_ids)
                    discovered_ids = _candidate_object_ids_for_ensemble_exclusion(
                        latest_candidate_rows,
                        target_object_ids=target_object_ids,
                    )
                    candidate_exclusion_object_ids = sorted(previous_ids | set(discovered_ids))
                    candidate_exclusion_new_object_ids = sorted(set(candidate_exclusion_object_ids) - previous_ids)
                    candidate_origin_counts: dict[str, int] = {}
                    for row in latest_candidate_rows:
                        if str(row.get("candidate_status") or "").upper() != "ACCEPTED":
                            continue
                        for origin in _candidate_origin_parts(row.get("candidate_origin")):
                            candidate_origin_counts[origin] = candidate_origin_counts.get(origin, 0) + 1
                    will_run_iteration = bool(candidate_exclusion_object_ids) and (iteration == 2 or bool(candidate_exclusion_new_object_ids))
                    stop_reason = None
                    if not candidate_exclusion_object_ids:
                        stop_reason = "no accepted candidates or target objects to exclude"
                    elif iteration > 2 and not candidate_exclusion_new_object_ids:
                        stop_reason = "converged: no new accepted candidate reference exclusions"
                    return {
                        "enabled": True,
                        "source": "field_qc_report",
                        "iteration": iteration,
                        "max_iterations": candidate_ensemble_iteration_limit,
                        "fixed_max_iterations": candidate_ensemble_fixed_max_iterations,
                        "convergence_max_iterations": candidate_ensemble_convergence_max_iterations,
                        "until_converged": bool(config.candidate_ensemble_until_converged),
                        "iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
                        "input_ensemble_calibration": current_ensemble_name,
                        "field_report_candidate_rows": len(latest_candidate_rows),
                        "target_object_ids": list(target_object_ids),
                        "candidate_object_ids": list(discovered_ids),
                        "new_requested_excluded_object_ids": list(candidate_exclusion_new_object_ids),
                        "new_requested_excluded_objects": len(candidate_exclusion_new_object_ids),
                        "requested_excluded_object_ids": list(candidate_exclusion_object_ids),
                        "requested_excluded_objects": len(candidate_exclusion_object_ids),
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
                    nonlocal current_ensemble_name
                    current_ensemble_name = _candidate_excluded_calibration_name(config, iteration=iteration)
                    if "ensemble_initial" not in calibration_names:
                        calibration_names["ensemble_initial"] = initial_ensemble_name
                    calibration_names[f"ensemble_candidates_excluded_iter{iteration}"] = current_ensemble_name
                    if iteration == 2:
                        calibration_names["ensemble_candidates_excluded"] = current_ensemble_name
                    calibration_names["ensemble"] = current_ensemble_name
                    outputs["ensemble_initial_calibration"] = calibration_names["ensemble_initial"]
                    outputs["ensemble_candidate_excluded_calibration"] = current_ensemble_name
                    outputs[f"ensemble_candidate_excluded_iter{iteration}_calibration"] = current_ensemble_name
                    payload = _result_to_dict(
                        ensemble_calibrate_measurements(
                            session,
                            source_calibration_name=calibration_names["aperture_zp"] if not config.skip_aperture_zp else calibration_names["raw"],
                            target_calibration_name=current_ensemble_name,
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
                            exclude_reference_object_ids=candidate_exclusion_object_ids,
                        )
                    )
                    payload["candidate_ensemble_iteration"] = iteration
                    payload["candidate_ensemble_max_iterations"] = candidate_ensemble_iteration_limit
                    payload["candidate_ensemble_fixed_max_iterations"] = candidate_ensemble_fixed_max_iterations
                    payload["candidate_ensemble_convergence_max_iterations"] = candidate_ensemble_convergence_max_iterations
                    payload["candidate_ensemble_until_converged"] = bool(config.candidate_ensemble_until_converged)
                    payload["candidate_ensemble_iteration_mode"] = "until_converged" if config.candidate_ensemble_until_converged else "fixed"
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
                    lambda: compute_object_qc(
                        session,
                        calibration_name=current_ensemble_name,
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
                    ),
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
                if not final_candidate_object_ids and not candidate_exclusion_object_ids:
                    stop_reason = "no accepted candidates or target objects to exclude"
                elif converged:
                    stop_reason = "converged"
                elif config.candidate_ensemble_until_converged:
                    stop_reason = "max_iterations_reached_with_new_candidates"
                else:
                    stop_reason = "fixed_iteration_limit_reached_with_new_candidates"
                return {
                    "enabled": True,
                    "iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
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
                    "converged": bool(converged),
                    "stop_reason": stop_reason,
                }

            add_step("candidate_ensemble_convergence", _candidate_ensemble_convergence_step)

        if not stop_due_to_no_usable_frames and config.export_target_light_curve and config.exclude_candidates_from_ensemble:
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
                    reject_flags=light_curve_reject_flags(require_detection_support=require_detection_support_effective),
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
                if not latest_candidate_rows:
                    return {
                        "rows_total": 0,
                        "accepted_candidates": 0,
                        "exported_light_curves": 0,
                        "warnings": ["field report produced no candidate rows"],
                    }
                if not config.out_dir:
                    return {
                        "rows_total": len(latest_candidate_rows),
                        "accepted_candidates": 0,
                        "exported_light_curves": 0,
                        "warnings": ["candidate light-curve export skipped because out_dir is not set"],
                    }
                candidates_dir = Path(config.out_dir) / "candidates"
                product = export_accepted_candidate_light_curves(
                    session,
                    candidate_rows=latest_candidate_rows,
                    calibration_name=current_ensemble_name,
                    catalog_name=config.catalog_name,
                    series_id=series_id,
                    frame_ids=_frame_ids_for_step(selected_frame_ids),
                    reject_flags=light_curve_reject_flags(require_detection_support=require_detection_support_effective),
                    out_dir=candidates_dir,
                    limit=config.candidate_light_curve_limit,
                    export_plots=config.export_candidate_plots,
                )
                payload = product.to_dict()
                if product.candidates_dir:
                    outputs["accepted_candidate_light_curves_dir"] = product.candidates_dir
                if product.candidates_summary_csv:
                    outputs["accepted_candidates_summary_csv"] = product.candidates_summary_csv
                if product.candidates_summary_json:
                    outputs["accepted_candidates_summary_json"] = product.candidates_summary_json
                if product.candidates_index_html:
                    outputs["accepted_candidates_index_html"] = product.candidates_index_html
                outputs["candidate_plot_count"] = product.candidate_plot_count
                outputs["candidate_plot_warnings_count"] = product.candidate_plot_warnings_count
                outputs["candidate_plot_backend"] = product.plot_backend
                return payload

            add_step("export_candidate_light_curves", _candidate_light_curve_export_step)

        result.quality_policy = _quality_policy_summary(config, result)
        failed = [s for s in result.steps if s.status == "FAILED"]
        if failed:
            result.status = "FAILED"
        elif stop_due_to_no_usable_frames:
            result.status = NO_USABLE_FRAMES_STATUS
        else:
            result.status = "DONE"
    except Exception as exc:
        result.status = "FAILED"
        if file_logger is not None:
            file_logger.exception(
                exc=exc,
                event_type="run_failed",
                message="field pipeline run failed",
                pipeline_run_id=pipeline_run_id,
            )
        _insert_pipeline_log_event(
            session,
            pipeline_run_id=pipeline_run_id,
            pipeline_run_step_id=None,
            level="ERROR",
            event_type="run_failed",
            message=f"{type(exc).__name__}: {exc}",
            context_json={"run_uid": run_uid},
        )
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
    )
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


def _calibration_names(config: FieldPipelineConfig) -> dict[str, str]:
    r = _fmt_float(config.aperture_radius_pix)
    a1 = _fmt_float(config.annulus_inner_pix)
    a2 = _fmt_float(config.annulus_outer_pix)
    work_tag = _working_catalog_tag(config)
    ensemble_name = config.ensemble_calibration_name or f"{config.calibration_prefix}{work_tag}_ensemble_{config.mag_key.lower()}_v1"
    ensemble_candidate_excluded_name = _candidate_excluded_calibration_name(config, iteration=2)
    return {
        "raw": config.raw_calibration_name or f"{config.calibration_prefix}{work_tag}_raw_aperture_r{r}_ann{a1}_{a2}_v1",
        "aperture_zp": config.aperture_zp_calibration_name or f"{config.calibration_prefix}{work_tag}_aperture_{config.mag_key.lower()}_zp_v1",
        "ensemble": ensemble_name,
        "ensemble_candidates_excluded": ensemble_candidate_excluded_name,
    }


def _candidate_excluded_calibration_name(config: FieldPipelineConfig, *, iteration: int) -> str:
    """Return the ensemble calibration name for a candidate-exclusion pass.

    Iteration is the total ensemble-pass number: iteration=2 is the first
    candidate-excluded pass and keeps the historical 0.5.33.0 name.
    Later iterations get an explicit iterN suffix.
    """

    if iteration < 2:
        raise ValueError("candidate-excluded ensemble iteration must be >= 2")
    if config.ensemble_calibration_name:
        if iteration == 2:
            return f"{config.ensemble_calibration_name}_candidate_excluded_v1"
        return f"{config.ensemble_calibration_name}_candidate_excluded_iter{iteration}_v1"
    work_tag = _working_catalog_tag(config)
    if iteration == 2:
        return f"{config.calibration_prefix}{work_tag}_ensemble_{config.mag_key.lower()}_candidate_excluded_v1"
    return f"{config.calibration_prefix}{work_tag}_ensemble_{config.mag_key.lower()}_candidate_excluded_iter{iteration}_v1"


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
                    (run_uid, instrument_unit_id, field_id, series_id, catalog_name, calibration_prefix, recipe_json, status, started_at, updated_at)
                VALUES
                    (:run_uid, :instrument_unit_id, :field_id, :series_id, :catalog_name, :calibration_prefix, CAST(:recipe_json AS JSONB), 'RUNNING', now(), now())
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
                "message": message,
                "context_json": json.dumps(_to_jsonable(context_json or {}), ensure_ascii=False),
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
                (pipeline_run_id, order_index, step_name, status, started_at, updated_at)
            VALUES
                (:pipeline_run_id, :order_index, :step_name, 'RUNNING', now(), now())
            RETURNING pipeline_run_step_id
            """
        ),
        {"pipeline_run_id": pipeline_run_id, "order_index": order_index, "step_name": step_name},
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


def _detection_branch_enabled(config: FieldPipelineConfig) -> bool:
    return not config.skip_detection and not config.skip_detection_catalog_match


def _effective_require_detection_support(config: FieldPipelineConfig) -> bool:
    if config.require_detection_support is not None:
        return bool(config.require_detection_support)
    return _detection_branch_enabled(config)


def _step_result(result: PipelineRunResult, step_name: str) -> dict[str, Any] | None:
    for step in result.steps:
        if step.step_name == step_name and step.status == "DONE":
            return step.result
    return None


def _step_results_with_prefix(result: PipelineRunResult, step_name_prefix: str) -> list[dict[str, Any]]:
    return [step.result for step in result.steps if step.status == "DONE" and step.step_name.startswith(step_name_prefix)]


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
        history.append(
            {
                "iteration": iteration,
                "will_run_iteration": bool(selection.get("will_run_iteration")),
                "executed": bool(ensemble),
                "input_ensemble_calibration": selection.get("input_ensemble_calibration"),
                "output_ensemble_calibration": ensemble.get("target_calibration_name"),
                "new_requested_excluded_object_ids": list(selection.get("new_requested_excluded_object_ids") or []),
                "new_requested_excluded_objects": int(selection.get("new_requested_excluded_objects") or 0),
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
    requested_exclusion_ids = exclusion_history[-1].get("requested_excluded_object_ids", []) if exclusion_history else []
    actual_exclusion_ids = exclusion_history[-1].get("actually_excluded_reference_object_ids", []) if exclusion_history else []
    convergence_result = _step_result(result, "candidate_ensemble_convergence") or {}
    iteration_limit = (
        max(2, int(config.candidate_ensemble_convergence_max_iterations or 10))
        if config.candidate_ensemble_until_converged
        else max(1, int(config.candidate_ensemble_max_iterations or 1))
    )
    return {
        "detection_branch_enabled": _detection_branch_enabled(config),
        "detection_matching_executed": bool(match_result),
        "detection_matches_count": int(match_result.get("matched") or 0),
        "require_detection_support_requested": config.require_detection_support,
        "require_detection_support_effective": _effective_require_detection_support(config),
        "target_source_id": config.target_source_id,
        "target_object_id_requested": config.target_object_id,
        "target_resolved": target_resolved,
        "target_object_id": target_result.get("object_id") if target_resolved else None,
        "target_catalog_source_id": target_result.get("catalog_source_id") if target_resolved else None,
        "target_sep_arcsec": target_result.get("sep_arcsec") if target_resolved else None,
        "target_resolution_method": target_result.get("method"),
        "nearest_target_candidate_object_id": target_result.get("nearest_object_id"),
        "nearest_target_candidate_source_id": target_result.get("nearest_source_id"),
        "nearest_target_candidate_catalog_source_id": target_result.get("nearest_catalog_source_id"),
        "nearest_target_candidate_sep_arcsec": target_result.get("nearest_sep_arcsec"),
        "candidate_ensemble_exclusion_enabled": bool(config.exclude_candidates_from_ensemble),
        "candidate_ensemble_iteration_mode": "until_converged" if config.candidate_ensemble_until_converged else "fixed",
        "candidate_ensemble_until_converged": bool(config.candidate_ensemble_until_converged),
        "candidate_ensemble_max_iterations": max(1, int(config.candidate_ensemble_max_iterations or 1)),
        "candidate_ensemble_convergence_max_iterations": max(2, int(config.candidate_ensemble_convergence_max_iterations or 10)),
        "candidate_ensemble_iteration_limit": iteration_limit,
        "candidate_ensemble_exclusion_executed": bool(_step_results_with_prefix(result, "ensemble_calibrate_candidate_excluded")),
        "candidate_ensemble_exclusion_iterations_executed": len(_step_results_with_prefix(result, "ensemble_calibrate_candidate_excluded")),
        "candidate_ensemble_requested_excluded_object_ids": requested_exclusion_ids,
        "candidate_ensemble_requested_excluded_objects": len(requested_exclusion_ids),
        "candidate_ensemble_actual_excluded_reference_object_ids": actual_exclusion_ids,
        "candidate_ensemble_actual_excluded_reference_objects": len(actual_exclusion_ids),
        # Backwards-compatible aliases from 0.5.33.0. These are the requested
        # exclusions; the actual reference exclusions are recorded above.
        "candidate_ensemble_excluded_object_ids": requested_exclusion_ids,
        "candidate_ensemble_excluded_objects": len(requested_exclusion_ids),
        "candidate_ensemble_exclusion_history": exclusion_history,
        "candidate_ensemble_converged": convergence_result.get("converged"),
        "candidate_ensemble_stop_reason": convergence_result.get("stop_reason"),
        "candidate_ensemble_final_candidate_object_ids": list(convergence_result.get("final_candidate_object_ids") or []),
        "candidate_ensemble_final_candidate_objects": int(convergence_result.get("final_candidate_objects") or 0),
        "candidate_ensemble_new_final_candidate_object_ids": list(convergence_result.get("new_final_candidate_object_ids") or []),
        "candidate_ensemble_new_final_candidate_objects": int(convergence_result.get("new_final_candidate_objects") or 0),
        "candidate_ensemble_convergence_diagnostics": convergence_result or None,
        "initial_ensemble_calibration": result.outputs.get("ensemble_initial_calibration"),
        "final_ensemble_calibration": result.calibration_names.get("ensemble"),
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
