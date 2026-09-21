from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from time import perf_counter
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE
from grandgousier.db.measurement_io import upsert_measurement_rows
from grandgousier.db.models import (
    CalibrationFrameTerm,
    CalibrationVersion,
    Frame,
    InstrumentUnit,
    Measurement,
    Object,
)
from grandgousier.calibration.normalized_terms import FrameTermInput, replace_calibration_iteration
from grandgousier.calibration.provenance import prepare_calibration_version_for_write, set_source_calibration
from grandgousier.photometry.calibration_view import load_calibration_photometry_view


@dataclass
class ApertureZpFrameStat:
    frame_id: int
    n_candidates: int = 0
    n_used: int = 0
    zp_median: float | None = None
    zp_mad: float | None = None
    zp_std: float | None = None
    accepted: bool = False
    rejection_reason: str | None = None


@dataclass
class ApertureZpCalibrationResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_name: str | None = None
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    selected_measurements: int = 0
    checked_measurements: int = 0
    selected_frames: int = 0
    frames_with_zp: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_other_catalog: int = 0
    skipped_no_ref_mag: int = 0
    skipped_no_mag_inst: int = 0
    skipped_bad_mag_err: int = 0
    skipped_saturated: int = 0
    skipped_no_frame_zp: int = 0
    skipped_nonfinite: int = 0
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    normalized_object_terms: int = 0
    normalized_write_status: str = "NOT_WRITTEN"
    source_read_strategy: str = "CALIBRATION_VIEW_V1"
    normalized_only_fast_path: bool = False
    downstream_seed_prepared: bool = False
    downstream_seed_rows: int = 0
    analysis_snapshot_seed_prepared: bool = False
    analysis_snapshot_seed_rows: int = 0
    timings_sec: dict[str, float] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(frozen=True)
class _CompactApertureZpMeasurement:
    object_id: int
    frame_id: int
    mag_inst: float | None
    mag_err: float | None
    flags: dict[str, Any]


def _fuse_compact_aperture_correction_rows(
    base_rows: list[Mapping[str, Any]],
    *,
    aperture_terms: Mapping[int, CalibrationFrameTerm],
) -> list[_CompactApertureZpMeasurement]:
    """Reconstruct only APERTURE_CORRECTION fields needed by aperture-ZP fitting."""

    from grandgousier.calibration.reconstruction import (
        _aperture_differential_error,
        _aperture_prediction,
        _first_finite,
        _quadrature,
    )

    rows: list[_CompactApertureZpMeasurement] = []
    for base in base_rows:
        frame_id = int(base["frame_id"])
        object_id = int(base["object_id"])
        x = base.get("x")
        y = base.get("y")
        mag_inst_raw = base.get("mag_inst")

        try:
            mag_inst = float(mag_inst_raw) if mag_inst_raw is not None else None
        except (TypeError, ValueError):
            mag_inst = None
        if mag_inst is not None and not math.isfinite(mag_inst):
            mag_inst = None

        differential_error = 0.0
        term = aperture_terms.get(frame_id)
        if term is not None and bool(term.accepted):
            correction = float(_aperture_prediction(term, x, y))
            differential_error = float(_aperture_differential_error(term, x, y))
            if mag_inst is not None:
                corrected = mag_inst + correction
                mag_inst = corrected if math.isfinite(corrected) else None

        random_err = _quadrature(
            _first_finite(base.get("mag_err_random"), base.get("mag_err")),
            differential_error,
        )
        flags = {
            "catalog_name": base.get("catalog_name"),
            "saturated_or_near_saturated": bool(base.get("saturated_or_near_saturated")),
        }
        rows.append(
            _CompactApertureZpMeasurement(
                object_id=object_id,
                frame_id=frame_id,
                mag_inst=mag_inst,
                mag_err=random_err,
                flags=flags,
            )
        )
    return rows


def _load_compact_normalized_aperture_correction_source(
    session: Session,
    *,
    source_cv: CalibrationVersion,
    frame_ids: list[int] | None,
    instrument_unit_id: int | None,
    result: ApertureZpCalibrationResult,
    prepare_analysis_snapshot_seed: bool = False,
) -> list[_CompactApertureZpMeasurement] | None:
    """Compact PostgreSQL reader for normalized APERTURE_CORRECTION input.

    Unsupported chains return ``None`` so the authoritative calibration-view path
    remains the fallback.
    """

    bind = session.get_bind()
    if bind is None or str(bind.dialect.name).lower() != "postgresql":
        return None
    if str(source_cv.product_kind or "").upper() != "APERTURE_CORRECTION":
        return None
    if str(source_cv.materialization_kind or "").upper() != "NORMALIZED_TERMS":
        return None

    legacy_count = int(
        session.scalar(
            select(func.count(Measurement.measurement_id)).where(
                Measurement.calibration_version_id == int(source_cv.calibration_version_id)
            )
        )
        or 0
    )
    if legacy_count > 0:
        return None

    from grandgousier.calibration.reconstruction import _chain_from_base, _latest_iteration

    phase_started = perf_counter()
    chain = _chain_from_base(session, source_cv)
    kinds = [str(cv.product_kind or "").upper() for cv in chain]
    if kinds != ["BASE_PHOTOMETRY", "APERTURE_CORRECTION"]:
        return None
    base_cv, aperture_cv = chain
    if str(base_cv.materialization_kind or "").upper() != "FULL_MEASUREMENTS":
        return None
    aperture_iteration = _latest_iteration(session, int(aperture_cv.calibration_version_id))
    if aperture_iteration is None:
        return None
    result.timings_sec["compact_plan"] = perf_counter() - phase_started

    phase_started = perf_counter()
    aperture_terms = {
        int(term.frame_id): term
        for term in session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id
                == int(aperture_iteration.calibration_iteration_id)
            )
        ).all()
    }
    result.timings_sec["compact_term_load"] = perf_counter() - phase_started

    phase_started = perf_counter()
    if prepare_analysis_snapshot_seed:
        columns = [
            Measurement.measurement_id.label("measurement_id"),
            Measurement.object_id.label("object_id"),
            Measurement.frame_id.label("frame_id"),
            Measurement.x.label("x"),
            Measurement.y.label("y"),
            Measurement.flux.label("flux"),
            Measurement.mag_inst.label("mag_inst"),
            Measurement.mag_err.label("mag_err"),
            Measurement.mag_err_random.label("mag_err_random"),
            Measurement.mag_err_systematic.label("mag_err_systematic"),
            Measurement.aperture_radius_pix.label("aperture_radius_pix"),
            Measurement.flags["catalog_name"].as_string().label("catalog_name"),
            Measurement.flags["source_id"].as_string().label("source_id"),
            Measurement.flags["bad_xy"].as_boolean().label("bad_xy"),
            Measurement.flags["outside_image"].as_boolean().label("outside_image"),
            Measurement.flags["empty_aperture"].as_boolean().label("empty_aperture"),
            Measurement.flags["non_positive_flux"].as_boolean().label("non_positive_flux"),
            Measurement.flags["saturated"].as_boolean().label("saturated"),
            Measurement.flags["near_saturated"].as_boolean().label("near_saturated"),
            Measurement.flags["saturated_or_near_saturated"].as_boolean().label(
                "saturated_or_near_saturated"
            ),
            Measurement.flags["random_error_physical_mag_err"].as_float().label(
                "random_error_physical_mag_err"
            ),
            Measurement.flags["random_error_legacy_mag_err"].as_float().label(
                "random_error_legacy_mag_err"
            ),
            Measurement.flags["reference_mag"].as_float().label("reference_mag"),
            Measurement.flags["reference_color_value"].as_float().label(
                "reference_color_value"
            ),
            Measurement.flags["reference_catalog_name"].as_string().label(
                "reference_catalog_name"
            ),
            Measurement.flags["reference_band"].as_string().label("reference_band"),
        ]
    else:
        columns = [
            Measurement.object_id.label("object_id"),
            Measurement.frame_id.label("frame_id"),
            Measurement.x.label("x"),
            Measurement.y.label("y"),
            Measurement.mag_inst.label("mag_inst"),
            Measurement.mag_err.label("mag_err"),
            Measurement.mag_err_random.label("mag_err_random"),
            Measurement.flags["catalog_name"].as_string().label("catalog_name"),
            Measurement.flags["saturated_or_near_saturated"].as_boolean().label(
                "saturated_or_near_saturated"
            ),
        ]
    stmt = select(*columns).where(
        Measurement.calibration_version_id == int(base_cv.calibration_version_id)
    )
    normalized_frame_ids = sorted({int(value) for value in (frame_ids or [])})
    if normalized_frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(normalized_frame_ids))
    if instrument_unit_id is not None:
        stmt = stmt.join(Frame, Frame.frame_id == Measurement.frame_id).where(
            Frame.instrument_unit_id == int(instrument_unit_id)
        )
    raw_rows = session.execute(
        stmt.order_by(Measurement.frame_id, Measurement.object_id)
    ).mappings().all()
    result.timings_sec["compact_base_query"] = perf_counter() - phase_started
    if prepare_analysis_snapshot_seed:
        seed_started = perf_counter()
        seed_rows = tuple(
            sorted(
                (dict(row) for row in raw_rows),
                key=lambda row: (int(row["object_id"]), int(row["frame_id"])),
            )
        )
        result._analysis_snapshot_base_rows = seed_rows
        result.analysis_snapshot_seed_prepared = True
        result.analysis_snapshot_seed_rows = len(seed_rows)
        result.timings_sec["compact_analysis_seed_prepare"] = perf_counter() - seed_started

    phase_started = perf_counter()
    rows = _fuse_compact_aperture_correction_rows(
        raw_rows,
        aperture_terms=aperture_terms,
    )
    result.timings_sec["compact_fused_reconstruction"] = perf_counter() - phase_started
    return rows


def aperture_zp_calibrate(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    catalog_name: str,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    mag_key: str = "G",
    min_ref_stars_per_frame: int = 20,
    max_mag_err: float | None = 0.08,
    max_zp_mad: float | None = None,
    clip_sigma: float = 3.0,
    n_iterations: int = 3,
    exclude_saturated: bool = True,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    immutable_generation: bool = False,
    materialize_measurements: bool = True,
    prepare_downstream_seed: bool = False,
    prepare_analysis_snapshot_seed: bool = False,
) -> ApertureZpCalibrationResult:
    """Calibrate forced-aperture measurements with a zero point derived from the same measurements.

    This fixes the mismatch of using a source-detection zero point for forced-aperture
    photometry. For each frame:

        zp_i = catalog_mag - forced_aperture_mag_inst

    A robust median zero point is computed from the forced-aperture measurements
    themselves. The target calibration is stored as a new CalibrationVersion; source
    measurements are copied, not overwritten.
    """

    total_started = perf_counter()
    result = ApertureZpCalibrationResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        dry_run=dry_run,
        insert_batch_size=int(insert_batch_size),
    )

    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            source_calibration_name=source_calibration_name,
            catalog_name=catalog_name,
            mag_key=mag_key,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
            max_mag_err=max_mag_err,
            max_zp_mad=max_zp_mad,
            clip_sigma=clip_sigma,
            n_iterations=n_iterations,
            exclude_saturated=exclude_saturated,
            immutable_generation=immutable_generation,
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id

    load_started = perf_counter()
    compact_rows = None
    if not materialize_measurements:
        compact_rows = _load_compact_normalized_aperture_correction_source(
            session,
            source_cv=source_cv,
            frame_ids=frame_ids,
            instrument_unit_id=instrument_unit_id,
            result=result,
            prepare_analysis_snapshot_seed=prepare_analysis_snapshot_seed,
        )
    if compact_rows is not None:
        source_measurements = compact_rows
        result.source_read_strategy = (
            "POSTGRESQL_COMPACT_APERTURE_CORRECTION_SHARED_SEED_V2"
            if prepare_analysis_snapshot_seed
            else "POSTGRESQL_COMPACT_APERTURE_CORRECTION_V1"
        )
        result.normalized_only_fast_path = True
    else:
        view = load_calibration_photometry_view(
            session,
            calibration_version_id=int(source_cv.calibration_version_id),
            frame_ids=frame_ids,
            prefer_science=False,
        )
        source_measurements = list(view.rows)

    frame_ids_needed = sorted({int(row.frame_id) for row in source_measurements})
    object_ids_needed = sorted({int(row.object_id) for row in source_measurements})
    frames = {
        int(row.frame_id): row
        for row in session.scalars(select(Frame).where(Frame.frame_id.in_(frame_ids_needed))).all()
    } if frame_ids_needed else {}
    objects = {
        int(row.object_id): row
        for row in session.scalars(select(Object).where(Object.object_id.in_(object_ids_needed))).all()
    } if object_ids_needed else {}
    instrument_ids = sorted(
        {
            int(frame.instrument_unit_id)
            for frame in frames.values()
            if frame.instrument_unit_id is not None
        }
    )
    instrument_units = {
        int(row.instrument_unit_id): row
        for row in session.scalars(
            select(InstrumentUnit).where(InstrumentUnit.instrument_unit_id.in_(instrument_ids))
        ).all()
    } if instrument_ids else {}
    rows = []
    for measurement in source_measurements:
        frame = frames.get(int(measurement.frame_id))
        obj = objects.get(int(measurement.object_id))
        if frame is None or obj is None:
            continue
        if instrument_unit_id is not None and frame.instrument_unit_id != int(instrument_unit_id):
            continue
        iu = (
            instrument_units.get(int(frame.instrument_unit_id))
            if frame.instrument_unit_id is not None
            else None
        )
        rows.append((measurement, frame, obj, iu))
    rows.sort(key=lambda item: (int(item[1].frame_id), int(item[0].object_id)))
    result.selected_measurements = len(rows)
    result.timings_sec["load_source_measurements"] = perf_counter() - load_started

    candidate_started = perf_counter()
    frame_candidates: dict[int, list[tuple[float, object, Frame, Object]]] = defaultdict(list)
    source_rows: list[tuple[object, Frame, Object, InstrumentUnit | None]] = []
    for measurement, frame, obj, iu in rows:
        result.checked_measurements += 1
        flags = measurement.flags or {}
        if flags.get("catalog_name") != catalog_name:
            result.skipped_other_catalog += 1
            continue
        source_rows.append((measurement, frame, obj, iu))

        if measurement.mag_inst is None:
            result.skipped_no_mag_inst += 1
            continue
        try:
            mag_inst = float(measurement.mag_inst)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(mag_inst):
            result.skipped_nonfinite += 1
            continue

        if exclude_saturated and bool(flags.get("saturated_or_near_saturated")):
            result.skipped_saturated += 1
            continue

        if max_mag_err is not None and measurement.mag_err is not None:
            try:
                if float(measurement.mag_err) > float(max_mag_err):
                    result.skipped_bad_mag_err += 1
                    continue
            except Exception:
                pass

        ref_mag = _get_ref_mag(obj, mag_key)
        if ref_mag is None:
            result.skipped_no_ref_mag += 1
            continue

        zp = float(ref_mag) - mag_inst
        if not math.isfinite(zp):
            result.skipped_nonfinite += 1
            continue
        frame_candidates[int(frame.frame_id)].append((zp, measurement, frame, obj))

    result.selected_frames = len(frame_candidates)
    result.timings_sec["candidate_selection"] = perf_counter() - candidate_started

    fit_started = perf_counter()
    frame_stats: dict[int, ApertureZpFrameStat] = {}
    all_frame_stats: dict[int, ApertureZpFrameStat] = {}
    for frame_id, values in sorted(frame_candidates.items()):
        zps = [v[0] for v in values]
        clipped = _sigma_clip(zps, sigma=clip_sigma, n_iterations=n_iterations)
        stat = ApertureZpFrameStat(frame_id=frame_id, n_candidates=len(zps), n_used=len(clipped))
        if len(clipped) < int(min_ref_stars_per_frame):
            stat.rejection_reason = "TOO_FEW_REFERENCE_STARS"
        else:
            stat.zp_median = _median(clipped)
            stat.zp_mad = _mad(clipped)
            stat.zp_std = _std(clipped)
            if max_zp_mad is not None and (stat.zp_mad is None or stat.zp_mad > float(max_zp_mad)):
                stat.rejection_reason = "ZP_MAD_TOO_HIGH"
            else:
                stat.accepted = True
                frame_stats[frame_id] = stat
        all_frame_stats[frame_id] = stat

    result.frames_with_zp = len(frame_stats)
    frame_zps = [s.zp_median for s in frame_stats.values() if s.zp_median is not None and math.isfinite(s.zp_median)]
    result.frame_zp_median = _median(frame_zps) if frame_zps else None
    result.frame_zp_mad = _mad(frame_zps) if frame_zps else None
    result.timings_sec["frame_zp_fit"] = perf_counter() - fit_started

    finalize_started = perf_counter()
    rows_to_insert = []
    downstream_seed_rows: list[dict[str, Any]] = []
    for measurement, frame, obj, iu in tqdm(source_rows, desc="Aperture ZP calibration", unit="meas"):
        stat = frame_stats.get(int(frame.frame_id))
        if stat is None or stat.zp_median is None:
            result.skipped_no_frame_zp += 1
            continue
        if measurement.mag_inst is None:
            result.skipped_no_mag_inst += 1
            continue
        try:
            mag_inst = float(measurement.mag_inst)
            zp = float(stat.zp_median)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(mag_inst) or not math.isfinite(zp):
            result.skipped_nonfinite += 1
            continue

        mag_cal = mag_inst + zp
        result.calibrated += 1
        if prepare_downstream_seed:
            downstream_seed_rows.append(
                {
                    "object_id": int(measurement.object_id),
                    "frame_id": int(measurement.frame_id),
                    "mag_inst": float(mag_inst),
                    "mag_cal": float(mag_cal),
                    "catalog_name": catalog_name,
                }
            )
        if dry_run or target_cv is None or not materialize_measurements:
            continue

        flags = dict(measurement.flags or {})
        flags.update(
            {
                "calibration_stage": "aperture_frame_zp",
                "source_calibration_version_id": source_cv.calibration_version_id,
                "source_calibration_name": source_cv.name,
                "catalog_name": catalog_name,
                "mag_key": mag_key,
                "frame_aperture_zp": stat.zp_median,
                "frame_aperture_zp_mad": stat.zp_mad,
                "frame_aperture_zp_std": stat.zp_std,
                "frame_aperture_zp_n": stat.n_used,
                "frame_aperture_zp_candidates": stat.n_candidates,
                "warning": "diagnostic aperture zero point; color term is not applied",
            }
        )
        rows_to_insert.append(
            {
                "object_id": measurement.object_id,
                "frame_id": measurement.frame_id,
                "calibration_version_id": target_cv.calibration_version_id,
                "x": measurement.x,
                "y": measurement.y,
                "flux": measurement.flux,
                "flux_err": measurement.flux_err,
                "mag_inst": measurement.mag_inst,
                "mag_cal": mag_cal,
                "mag_err": measurement.mag_err_random if measurement.mag_err_random is not None else measurement.mag_err,
                "mag_err_random": measurement.mag_err_random if measurement.mag_err_random is not None else measurement.mag_err,
                "mag_err_systematic": measurement.mag_err_systematic,
                "aperture_radius_pix": measurement.aperture_radius_pix,
                "local_background": measurement.local_background,
                "local_zp_correction": stat.zp_median,
                "centroid_dx_pix": measurement.centroid_dx_pix,
                "centroid_dy_pix": measurement.centroid_dy_pix,
                "flags": flags,
            }
        )

    result.timings_sec["finalize_measurements"] = perf_counter() - finalize_started
    if prepare_downstream_seed:
        seed_started = perf_counter()
        result._ensemble_input_rows = tuple(
            sorted(
                downstream_seed_rows,
                key=lambda row: (int(row["object_id"]), int(row["frame_id"])),
            )
        )
        result.downstream_seed_prepared = True
        result.downstream_seed_rows = len(downstream_seed_rows)
        result.timings_sec["downstream_seed_prepare"] = perf_counter() - seed_started

    if rows_to_insert and materialize_measurements:
        result.insert_batches = upsert_measurement_rows(
            session,
            rows_to_insert,
            insert_batch_size=insert_batch_size,
        )
        result.inserted_or_updated = len(rows_to_insert)

    normalized_started = perf_counter()
    if not dry_run and target_cv is not None:
        normalized = replace_calibration_iteration(
            session,
            calibration_version_id=int(target_cv.calibration_version_id),
            source_calibration_version_id=int(source_cv.calibration_version_id),
            iteration_kind="APERTURE_ZP",
            iteration_no=1,
            algorithm="aperture_zp_calibration_v1",
            status="COMPLETE",
            convergence_status="FRAME_ZP_FIT_COMPLETE",
            parameters={
                "catalog_name": catalog_name,
                "mag_key": mag_key,
                "min_ref_stars_per_frame": min_ref_stars_per_frame,
                "max_mag_err": max_mag_err,
                "max_zp_mad": max_zp_mad,
                "clip_sigma": clip_sigma,
                "n_iterations": n_iterations,
                "exclude_saturated": exclude_saturated,
                "application_semantics": "mag_cal = mag_inst + frame_zp",
            },
            metrics={
                "selected_frames": result.selected_frames,
                "frames_with_zp": result.frames_with_zp,
                "frame_zp_median": result.frame_zp_median,
                "frame_zp_mad": result.frame_zp_mad,
                "calibrated": result.calibrated,
            },
            frame_terms=[
                FrameTermInput(
                    frame_id=stat.frame_id,
                    term_kind="APERTURE_ZP",
                    model_name="robust_frame_median",
                    offset_mag=stat.zp_median,
                    scatter_mad=stat.zp_mad,
                    scatter_rms=stat.zp_std,
                    n_candidates=stat.n_candidates,
                    n_used=stat.n_used,
                    n_reference=stat.n_used,
                    accepted=stat.accepted,
                    quality_status="ACCEPTED" if stat.accepted else "REJECTED",
                    details={"rejection_reason": stat.rejection_reason},
                )
                for stat in all_frame_stats.values()
            ],
        )
        result.normalized_calibration_iteration_id = normalized.calibration_iteration_id
        result.normalized_frame_terms = normalized.frame_terms
        result.normalized_object_terms = normalized.object_terms
        result.normalized_write_status = (
            "DUAL_WRITTEN" if materialize_measurements else "NORMALIZED_ONLY"
        )
        session.commit()
    elif not dry_run:
        session.commit()
    result.timings_sec["normalized_write_and_commit"] = perf_counter() - normalized_started
    result.timings_sec["function_total"] = perf_counter() - total_started
    return result


def _get_or_create_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    source_calibration_name: str,
    catalog_name: str,
    mag_key: str,
    min_ref_stars_per_frame: int,
    max_mag_err: float | None,
    max_zp_mad: float | None,
    clip_sigma: float,
    n_iterations: int,
    exclude_saturated: bool,
    immutable_generation: bool = False,
) -> CalibrationVersion:
    params = {
        "source_calibration_name": source_calibration_name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "catalog_name": catalog_name,
        "mag_key": mag_key,
        "min_ref_stars_per_frame": min_ref_stars_per_frame,
        "max_mag_err": max_mag_err,
        "max_zp_mad": max_zp_mad,
        "clip_sigma": clip_sigma,
        "n_iterations": n_iterations,
        "exclude_saturated": exclude_saturated,
        "warning": "zero point derived from forced-aperture mag_inst, not from source detections",
    }
    return prepare_calibration_version_for_write(
        session,
        logical_name=name,
        model_name="aperture_zp_calibration_v1",
        instrument_unit_id=instrument_unit_id,
        instrument_profile={},
        used_catalogs={"catalog_name": catalog_name, "mag_key": mag_key},
        parameters=params,
        notes=None,
        source_cv=source_cv,
        immutable_generation=immutable_generation,
    )


def _get_ref_mag(obj: Object, mag_key: str) -> float | None:
    ref_mags = obj.ref_mags or {}
    value = ref_mags.get(mag_key)
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _sigma_clip(values: list[float], *, sigma: float, n_iterations: int) -> list[float]:
    current = [float(v) for v in values if math.isfinite(float(v))]
    if not current:
        return []
    for _ in range(max(0, int(n_iterations))):
        if len(current) < 3:
            break
        med = _median(current)
        scatter = _mad(current)
        if scatter is None or scatter <= 0:
            break
        limit = float(sigma) * 1.4826 * scatter
        new = [v for v in current if abs(v - med) <= limit]
        if len(new) == len(current):
            break
        current = new
    return current


def _median(values: list[float]) -> float | None:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return None
    n = len(vals)
    if n % 2:
        return float(vals[n // 2])
    return float((vals[n // 2 - 1] + vals[n // 2]) / 2.0)


def _mad(values: list[float]) -> float | None:
    med = _median(values)
    if med is None:
        return None
    return _median([abs(float(v) - med) for v in values])


def _std(values: list[float]) -> float | None:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
