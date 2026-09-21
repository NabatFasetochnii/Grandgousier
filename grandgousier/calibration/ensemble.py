from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from time import perf_counter
from typing import Any, Iterable, Mapping

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
from grandgousier.calibration.normalized_terms import FrameTermInput, ObjectTermInput, replace_calibration_iteration
from grandgousier.calibration.provenance import prepare_calibration_version_for_write, set_source_calibration
from grandgousier.photometry.calibration_view import (
    CalibrationPhotometryRow,
    load_calibration_photometry_view,
)


@dataclass
class EnsembleCalibrationResult:
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    target_calibration_name: str | None = None
    selected_measurements: int = 0
    checked_measurements: int = 0
    selected_objects: int = 0
    reference_candidates: int = 0
    reference_object_ids: list[int] = field(default_factory=list)
    usable_reference_objects: int = 0
    selected_frames: int = 0
    frames_with_offsets: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    skipped_other_catalog: int = 0
    skipped_no_input_mag: int = 0
    skipped_no_frame_offset: int = 0
    skipped_nonfinite: int = 0
    dry_run: bool = False
    frame_offset_median: float | None = None
    frame_offset_mad: float | None = None
    requested_excluded_reference_objects: int = 0
    requested_excluded_reference_object_ids: list[int] = field(default_factory=list)
    actually_excluded_reference_objects: int = 0
    actually_excluded_reference_object_ids: list[int] = field(default_factory=list)
    excluded_reference_objects: int = 0
    excluded_reference_object_ids: list[int] = field(default_factory=list)
    calibration_iteration_no: int = 1
    normalized_calibration_iteration_id: int | None = None
    normalized_frame_terms: int = 0
    normalized_object_terms: int = 0
    normalized_write_status: str = "NOT_WRITTEN"
    source_read_strategy: str = "CALIBRATION_VIEW_V1"
    normalized_only_fast_path: bool = False
    analysis_snapshot_seed_prepared: bool = False
    analysis_snapshot_seed_rows: int = 0
    downstream_seed_reused: bool = False
    downstream_seed_rows: int = 0
    timings_sec: dict[str, float] = field(default_factory=dict)


@dataclass
class _MeasRow:
    measurement: CalibrationPhotometryRow | None
    frame: Frame | "_CompactFrameRef"
    obj: Object | "_CompactObjectRef"
    value: float
    is_reference: bool = False


@dataclass(frozen=True)
class _CompactFrameRef:
    frame_id: int


@dataclass(frozen=True)
class _CompactObjectRef:
    object_id: int


@dataclass
class _FrameOffset:
    frame_id: int
    offset: float
    n_ref: int
    scatter_mad: float | None


@dataclass
class _ObjectStats:
    object_id: int
    n: int
    median_mag: float
    rms: float | None
    mad: float | None


def ensemble_calibrate_measurements(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    min_ref_measurements: int = 5,
    min_ref_stars_per_frame: int = 20,
    max_ref_rms: float | None = 0.08,
    max_ref_mad: float | None = 0.05,
    min_mag: float | None = None,
    max_mag: float | None = None,
    n_iterations: int = 2,
    prefer_mag_cal: bool = True,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    exclude_reference_object_ids: list[int] | None = None,
    calibration_iteration_no: int = 1,
    immutable_generation: bool = False,
    materialize_measurements: bool = True,
    prepare_analysis_snapshot_seed: bool = False,
    _preloaded_aperture_zp_rows: Iterable[Mapping[str, Any]] | None = None,
    _preloaded_analysis_snapshot_base_rows: Iterable[Mapping[str, Any]] | None = None,
) -> EnsembleCalibrationResult:
    """Create an ensemble-differential calibration from an existing measurement set.

    This is still a local-series calibration, not an absolute standard-system solution. It uses
    stable objects in the selected measurements as an ensemble, estimates a robust per-frame
    residual offset, then copies measurements into a new CalibrationVersion with:

        output_mag = input_mag - frame_ensemble_offset

    If the source calibration has mag_cal, that is used by default. Otherwise mag_inst is used.
    """

    total_started = perf_counter()
    if min_ref_stars_per_frame < 1:
        raise ValueError("min_ref_stars_per_frame must be >= 1")
    if min_measurements < 1 or min_ref_measurements < 1:
        raise ValueError("min_measurements/min_ref_measurements must be >= 1")
    if n_iterations < 1:
        raise ValueError("n_iterations must be >= 1")

    if int(calibration_iteration_no) < 1:
        raise ValueError("calibration_iteration_no must be >= 1")
    result = EnsembleCalibrationResult(
        dry_run=dry_run,
        target_calibration_name=target_calibration_name,
        calibration_iteration_no=int(calibration_iteration_no),
    )
    requested_excluded_reference_ids = {int(v) for v in (exclude_reference_object_ids or []) if v is not None}
    result.requested_excluded_reference_object_ids = sorted(requested_excluded_reference_ids)
    result.requested_excluded_reference_objects = len(requested_excluded_reference_ids)
    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_ensemble_calibration_version(
            session,
            name=target_calibration_name,
            instrument_unit_id=instrument_unit_id,
            source_cv=source_cv,
            source_calibration_name=source_calibration_name,
            catalog_name=catalog_name,
            min_measurements=min_measurements,
            min_ref_measurements=min_ref_measurements,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
            max_ref_rms=max_ref_rms,
            max_ref_mad=max_ref_mad,
            min_mag=min_mag,
            max_mag=max_mag,
            n_iterations=n_iterations,
            prefer_mag_cal=prefer_mag_cal,
            exclude_reference_object_ids=sorted(requested_excluded_reference_ids),
            immutable_generation=immutable_generation,
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id

    phase_started = perf_counter()
    rows = None
    if not materialize_measurements:
        rows = _load_compact_normalized_aperture_zp_source_measurements(
            session,
            source_cv=source_cv,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            frame_ids=frame_ids,
            prefer_mag_cal=prefer_mag_cal,
            result=result,
            prepare_analysis_snapshot_seed=prepare_analysis_snapshot_seed,
            preloaded_aperture_zp_rows=_preloaded_aperture_zp_rows,
            preloaded_analysis_snapshot_base_rows=_preloaded_analysis_snapshot_base_rows,
        )
        if rows is not None:
            result.source_read_strategy = (
                "PIPELINE_SHARED_APERTURE_ZP_SEED_V2"
                if _preloaded_aperture_zp_rows is not None
                else "POSTGRESQL_COMPACT_APERTURE_ZP_V1"
            )
            result.normalized_only_fast_path = True
    if rows is None:
        rows = _load_source_measurements(
            session,
            source_cv=source_cv,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            frame_ids=frame_ids,
            prefer_mag_cal=prefer_mag_cal,
            result=result,
        )
    result.timings_sec["load_source_measurements"] = perf_counter() - phase_started
    result.selected_measurements = len(rows)

    phase_started = perf_counter()
    object_values: dict[int, list[float]] = {}
    frame_ids_seen: set[int] = set()
    for row in rows:
        object_values.setdefault(row.obj.object_id, []).append(row.value)
        frame_ids_seen.add(row.frame.frame_id)
    result.selected_objects = len(object_values)
    result.selected_frames = len(frame_ids_seen)

    object_stats = _build_object_stats(object_values)
    reference_object_ids = _select_reference_objects(
        object_stats,
        min_ref_measurements=min_ref_measurements,
        max_ref_rms=max_ref_rms,
        max_ref_mad=max_ref_mad,
        min_mag=min_mag,
        max_mag=max_mag,
    )
    before_exclusion = set(reference_object_ids)
    actually_excluded_reference_ids = before_exclusion & requested_excluded_reference_ids
    if requested_excluded_reference_ids:
        reference_object_ids = [obj_id for obj_id in reference_object_ids if int(obj_id) not in requested_excluded_reference_ids]
    result.reference_candidates = len(reference_object_ids)
    result.reference_object_ids = sorted(int(obj_id) for obj_id in reference_object_ids)
    result.actually_excluded_reference_object_ids = sorted(actually_excluded_reference_ids)
    result.actually_excluded_reference_objects = len(actually_excluded_reference_ids)
    # Backwards-compatible aliases: in 0.5.33.1 these refer to objects
    # that were actually removed from the reference ensemble, not merely
    # requested as exclusions. The requested set is recorded separately above.
    result.excluded_reference_object_ids = list(result.actually_excluded_reference_object_ids)
    result.excluded_reference_objects = int(result.actually_excluded_reference_objects)
    if materialize_measurements:
        for row in rows:
            row.is_reference = row.obj.object_id in reference_object_ids
    result.timings_sec["reference_selection"] = perf_counter() - phase_started

    if not reference_object_ids:
        phase_started = perf_counter()
        if not dry_run and target_cv is not None:
            _persist_normalized_ensemble_terms(
                session,
                result=result,
                target_cv=target_cv,
                source_cv=source_cv,
                object_stats=object_stats,
                reference_object_ids=set(),
                requested_excluded_reference_ids=requested_excluded_reference_ids,
                actually_excluded_reference_ids=actually_excluded_reference_ids,
                frame_offsets={},
                catalog_name=catalog_name,
                min_measurements=min_measurements,
                min_ref_measurements=min_ref_measurements,
                min_ref_stars_per_frame=min_ref_stars_per_frame,
                max_ref_rms=max_ref_rms,
                max_ref_mad=max_ref_mad,
                min_mag=min_mag,
                max_mag=max_mag,
                n_iterations=n_iterations,
                prefer_mag_cal=prefer_mag_cal,
            )
            result.normalized_write_status = (
                "DUAL_WRITTEN" if materialize_measurements else "NORMALIZED_ONLY"
            )
            session.commit()
        elif not dry_run:
            session.commit()
        result.timings_sec["normalized_write_and_commit"] = perf_counter() - phase_started
        result.timings_sec["total"] = perf_counter() - total_started
        return result

    phase_started = perf_counter()
    object_medians = {st.object_id: st.median_mag for st in object_stats.values() if st.object_id in reference_object_ids}
    frame_offsets: dict[int, _FrameOffset] = {}
    for _iteration in range(max(1, n_iterations)):
        frame_offsets = _estimate_frame_offsets(
            rows,
            object_medians=object_medians,
            reference_object_ids=reference_object_ids,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
        )
        corrected_ref_values: dict[int, list[float]] = {}
        for row in rows:
            if row.obj.object_id not in reference_object_ids:
                continue
            fo = frame_offsets.get(row.frame.frame_id)
            if fo is None:
                continue
            corrected_ref_values.setdefault(row.obj.object_id, []).append(row.value - fo.offset)
        new_stats = _build_object_stats(corrected_ref_values)
        if new_stats:
            object_medians = {obj_id: st.median_mag for obj_id, st in new_stats.items()}
    result.timings_sec["frame_offset_iterations"] = perf_counter() - phase_started

    result.frames_with_offsets = len(frame_offsets)
    result.usable_reference_objects = len(object_medians)
    offsets = [fo.offset for fo in frame_offsets.values() if _is_finite(fo.offset)]
    result.frame_offset_median = _median(offsets)
    result.frame_offset_mad = _mad(offsets)

    phase_started = perf_counter()
    rows_to_insert = []
    for row in tqdm(rows, desc="Ensemble calibration", unit="meas"):
        result.checked_measurements += 1
        fo = frame_offsets.get(row.frame.frame_id)
        if fo is None:
            result.skipped_no_frame_offset += 1
            continue
        if not _is_finite(row.value) or not _is_finite(fo.offset):
            result.skipped_nonfinite += 1
            continue
        result.calibrated += 1
        if dry_run or target_cv is None or not materialize_measurements:
            continue

        # The normalized-only pipeline stores only frame/object terms.  Avoid
        # constructing the full output measurement payload in that mode; this
        # is one of the largest avoidable per-row costs in the ensemble stage.
        if row.measurement is None:
            raise RuntimeError("compact ensemble row cannot be materialized")
        output_mag = float(row.value) - float(fo.offset)
        flags = dict(row.measurement.flags or {})
        old_local_zp = row.measurement.local_zp_correction
        if old_local_zp is not None and _is_finite(old_local_zp):
            new_local_zp = float(old_local_zp) - float(fo.offset)
        else:
            new_local_zp = -float(fo.offset)
        flags.update(
            {
                "calibration_stage": "ensemble_differential",
                "source_calibration_version_id": source_cv.calibration_version_id,
                "source_calibration_name": source_cv.name,
                "ensemble_algorithm": "robust_frame_median_v1",
                "ensemble_frame_offset": fo.offset,
                "ensemble_frame_n_ref": fo.n_ref,
                "ensemble_frame_scatter_mad": fo.scatter_mad,
                "ensemble_object_is_reference": row.is_reference,
                "ensemble_object_requested_reference_exclusion": row.obj.object_id in requested_excluded_reference_ids,
                "ensemble_object_excluded_from_reference": row.obj.object_id in actually_excluded_reference_ids,
                "ensemble_reference_exclusion_reason": "candidate_or_target_object" if row.obj.object_id in requested_excluded_reference_ids else None,
                "ensemble_min_ref_stars_per_frame": min_ref_stars_per_frame,
                "ensemble_n_iterations": n_iterations,
                "warning": "ensemble-differential internal calibration; not final color-corrected standard photometry",
            }
        )
        rows_to_insert.append(
            {
                "object_id": row.measurement.object_id,
                "frame_id": row.measurement.frame_id,
                "calibration_version_id": target_cv.calibration_version_id,
                "x": row.measurement.x,
                "y": row.measurement.y,
                "flux": row.measurement.flux,
                "flux_err": row.measurement.flux_err,
                "mag_inst": row.measurement.mag_inst,
                "mag_cal": output_mag,
                "mag_err": row.measurement.mag_err_random if row.measurement.mag_err_random is not None else row.measurement.mag_err,
                "mag_err_random": row.measurement.mag_err_random if row.measurement.mag_err_random is not None else row.measurement.mag_err,
                "mag_err_systematic": row.measurement.mag_err_systematic,
                "aperture_radius_pix": row.measurement.aperture_radius_pix,
                "local_background": row.measurement.local_background,
                "local_zp_correction": new_local_zp,
                "centroid_dx_pix": row.measurement.centroid_dx_pix,
                "centroid_dy_pix": row.measurement.centroid_dy_pix,
                "flags": flags,
            }
        )
    result.timings_sec["finalize_measurements"] = perf_counter() - phase_started

    if rows_to_insert and materialize_measurements:
        result.insert_batches = upsert_measurement_rows(
            session,
            rows_to_insert,
            insert_batch_size=insert_batch_size,
        )
        result.inserted_or_updated = len(rows_to_insert)

    phase_started = perf_counter()
    if not dry_run and target_cv is not None:
        _persist_normalized_ensemble_terms(
            session,
            result=result,
            target_cv=target_cv,
            source_cv=source_cv,
            object_stats=object_stats,
            reference_object_ids=set(int(v) for v in reference_object_ids),
            requested_excluded_reference_ids=requested_excluded_reference_ids,
            actually_excluded_reference_ids=actually_excluded_reference_ids,
            frame_offsets=frame_offsets,
            catalog_name=catalog_name,
            min_measurements=min_measurements,
            min_ref_measurements=min_ref_measurements,
            min_ref_stars_per_frame=min_ref_stars_per_frame,
            max_ref_rms=max_ref_rms,
            max_ref_mad=max_ref_mad,
            min_mag=min_mag,
            max_mag=max_mag,
            n_iterations=n_iterations,
            prefer_mag_cal=prefer_mag_cal,
        )
        result.normalized_write_status = (
            "DUAL_WRITTEN" if materialize_measurements else "NORMALIZED_ONLY"
        )
        session.commit()
    elif not dry_run:
        session.commit()
    result.timings_sec["normalized_write_and_commit"] = perf_counter() - phase_started
    result.timings_sec["total"] = perf_counter() - total_started
    return result


def _persist_normalized_ensemble_terms(
    session: Session,
    *,
    result: EnsembleCalibrationResult,
    target_cv: CalibrationVersion,
    source_cv: CalibrationVersion,
    object_stats: dict[int, _ObjectStats],
    reference_object_ids: set[int],
    requested_excluded_reference_ids: set[int],
    actually_excluded_reference_ids: set[int],
    frame_offsets: dict[int, _FrameOffset],
    catalog_name: str | None,
    min_measurements: int,
    min_ref_measurements: int,
    min_ref_stars_per_frame: int,
    max_ref_rms: float | None,
    max_ref_mad: float | None,
    min_mag: float | None,
    max_mag: float | None,
    n_iterations: int,
    prefer_mag_cal: bool,
) -> None:
    object_terms: list[ObjectTermInput] = []
    all_object_ids = set(int(v) for v in object_stats) | set(int(v) for v in requested_excluded_reference_ids)
    for object_id in sorted(all_object_ids):
        stat = object_stats.get(int(object_id))
        requested = int(object_id) in requested_excluded_reference_ids
        effective = int(object_id) in actually_excluded_reference_ids
        is_reference = int(object_id) in reference_object_ids
        if effective:
            role = "EXCLUDED_REFERENCE"
        elif is_reference:
            role = "REFERENCE"
        elif requested:
            role = "REQUESTED_EXCLUSION"
        else:
            role = "NON_REFERENCE"
        object_terms.append(
            ObjectTermInput(
                object_id=int(object_id),
                role=role,
                is_reference=is_reference,
                requested_exclusion=requested,
                effective_exclusion=effective,
                n_measurements=stat.n if stat is not None else None,
                median_mag=stat.median_mag if stat is not None else None,
                rms=stat.rms if stat is not None else None,
                mad=stat.mad if stat is not None else None,
                status="SELECTED_REFERENCE" if is_reference else ("EXCLUDED_REFERENCE" if effective else "NOT_REFERENCE"),
                details={"stats_basis": "source_measurements"},
            )
        )

    normalized = replace_calibration_iteration(
        session,
        calibration_version_id=int(target_cv.calibration_version_id),
        source_calibration_version_id=int(source_cv.calibration_version_id),
        iteration_kind="ENSEMBLE",
        iteration_no=int(result.calibration_iteration_no),
        algorithm="robust_frame_median_v1",
        status="COMPLETE" if reference_object_ids else "COMPLETE_WITH_WARNINGS",
        convergence_status="FIXED_INNER_ITERATIONS_COMPLETE" if reference_object_ids else "NO_REFERENCE_OBJECTS",
        convergence_reason=None if reference_object_ids else "reference selection produced no usable objects",
        parameters={
            "catalog_name": catalog_name,
            "min_measurements": min_measurements,
            "min_ref_measurements": min_ref_measurements,
            "min_ref_stars_per_frame": min_ref_stars_per_frame,
            "max_ref_rms": max_ref_rms,
            "max_ref_mad": max_ref_mad,
            "min_mag": min_mag,
            "max_mag": max_mag,
            "inner_n_iterations": n_iterations,
            "prefer_mag_cal": bool(prefer_mag_cal),
            "requested_excluded_reference_object_ids": sorted(requested_excluded_reference_ids),
            "application_semantics": "mag_out = mag_in - ensemble_frame_offset",
        },
        metrics={
            "selected_measurements": result.selected_measurements,
            "selected_objects": result.selected_objects,
            "selected_frames": result.selected_frames,
            "reference_candidates": result.reference_candidates,
            "usable_reference_objects": result.usable_reference_objects,
            "frames_with_offsets": result.frames_with_offsets,
            "frame_offset_median": result.frame_offset_median,
            "frame_offset_mad": result.frame_offset_mad,
            "actually_excluded_reference_object_ids": sorted(actually_excluded_reference_ids),
        },
        frame_terms=[
            FrameTermInput(
                frame_id=int(fo.frame_id),
                term_kind="ENSEMBLE_OFFSET",
                model_name="robust_frame_median",
                offset_mag=float(fo.offset),
                scatter_mad=fo.scatter_mad,
                n_used=int(fo.n_ref),
                n_reference=int(fo.n_ref),
                accepted=True,
                quality_status="ACCEPTED",
            )
            for fo in sorted(frame_offsets.values(), key=lambda item: int(item.frame_id))
        ],
        object_terms=object_terms,
    )
    result.normalized_calibration_iteration_id = normalized.calibration_iteration_id
    result.normalized_frame_terms = normalized.frame_terms
    result.normalized_object_terms = normalized.object_terms
    result.normalized_write_status = "DUAL_WRITTEN"


def build_ensemble_frame_summary(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    limit: int | None = 50,
) -> list[dict]:
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")
    stmt = (
        select(Measurement, Frame, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(Measurement.calibration_version_id == cv.calibration_version_id)
        .order_by(Frame.frame_id, Measurement.measurement_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)

    grouped: dict[int, list[tuple[Measurement, Frame, InstrumentUnit | None]]] = {}
    for meas, frame, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        grouped.setdefault(frame.frame_id, []).append((meas, frame, iu))

    rows: list[dict] = []
    for frame_id, values in grouped.items():
        meas0, frame, iu = values[0]
        offsets = []
        n_ref_values = []
        scatter_values = []
        mag_values = []
        for meas, _frame, _iu in values:
            flags = meas.flags or {}
            off = flags.get("ensemble_frame_offset")
            nref = flags.get("ensemble_frame_n_ref")
            sc = flags.get("ensemble_frame_scatter_mad")
            if _is_finite(off):
                offsets.append(float(off))
            if nref is not None:
                try:
                    n_ref_values.append(int(nref))
                except Exception:
                    pass
            if _is_finite(sc):
                scatter_values.append(float(sc))
            if meas.mag_cal is not None and _is_finite(meas.mag_cal):
                mag_values.append(float(meas.mag_cal))
        rows.append(
            {
                "frame_id": frame_id,
                "instrument_unit_id": getattr(iu, "instrument_unit_id", None),
                "instrument": getattr(iu, "instrument", None),
                "filter_name": getattr(iu, "filter_name", None),
                "date_obs": frame.date_obs.isoformat() if frame.date_obs is not None else None,
                "mjd": frame.mjd,
                "n_measurements": len(values),
                "ensemble_offset": _median(offsets),
                "ensemble_n_ref": max(n_ref_values) if n_ref_values else None,
                "ensemble_scatter_mad": _median(scatter_values),
                "mag_cal_median": _median(mag_values),
                "mag_cal_mad": _mad(mag_values),
            }
        )
    rows.sort(key=lambda d: d.get("frame_id") or 0)
    if limit is not None:
        rows = rows[:limit]
    return rows


def _fuse_compact_aperture_zp_source_rows(
    base_rows: list[Mapping[str, Any]],
    *,
    aperture_terms: Mapping[int, CalibrationFrameTerm],
    aperture_zp_terms: Mapping[int, CalibrationFrameTerm],
    aperture_zp_catalog_name: str | None,
    requested_catalog_name: str | None,
    prefer_mag_cal: bool,
    result: EnsembleCalibrationResult,
) -> list[_MeasRow]:
    """Reconstruct only the APERTURE_ZP scalar state needed by ensemble fitting.

    The generic photometry view preserves the complete measurement payload and
    provenance flags after every normalized stage.  Ensemble fitting only needs
    ``object_id``, ``frame_id`` and one finite input magnitude.  For the normal
    BASE -> APERTURE_CORRECTION -> APERTURE_ZP chain we can therefore apply the
    two scalar magnitude transforms directly without hydrating/copying the full
    ~54k-row state dictionary.
    """

    from grandgousier.calibration.reconstruction import _aperture_prediction

    rows: list[_MeasRow] = []
    effective_catalog_name = aperture_zp_catalog_name
    for base in base_rows:
        frame_id = int(base["frame_id"])
        object_id = int(base["object_id"])
        base_catalog_name = base.get("catalog_name")

        # APERTURE_ZP itself drops rows outside its configured catalog.  These
        # are not counted as ensemble-stage skips by the authoritative view.
        if aperture_zp_catalog_name and str(base_catalog_name) != str(aperture_zp_catalog_name):
            continue

        mag_inst = base.get("mag_inst")
        if not _is_finite(mag_inst):
            continue
        mag_inst_value = float(mag_inst)

        aperture_term = aperture_terms.get(frame_id)
        if aperture_term is not None and bool(aperture_term.accepted):
            mag_inst_value += float(
                _aperture_prediction(aperture_term, base.get("x"), base.get("y"))
            )
        if not math.isfinite(mag_inst_value):
            continue

        zp_term = aperture_zp_terms.get(frame_id)
        if zp_term is None or not bool(zp_term.accepted) or not _is_finite(zp_term.offset_mag):
            continue
        mag_cal_value = mag_inst_value + float(zp_term.offset_mag)

        row_catalog_name = effective_catalog_name or base_catalog_name
        if requested_catalog_name and str(row_catalog_name) != str(requested_catalog_name):
            result.skipped_other_catalog += 1
            continue

        value = mag_cal_value if prefer_mag_cal else mag_inst_value
        if not math.isfinite(value):
            result.skipped_nonfinite += 1
            continue

        rows.append(
            _MeasRow(
                measurement=None,
                frame=_CompactFrameRef(frame_id=frame_id),
                obj=_CompactObjectRef(object_id=object_id),
                value=float(value),
            )
        )
    return rows


def _load_compact_normalized_aperture_zp_source_measurements(
    session: Session,
    *,
    source_cv: CalibrationVersion,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
    prefer_mag_cal: bool,
    result: EnsembleCalibrationResult,
    prepare_analysis_snapshot_seed: bool = False,
    preloaded_aperture_zp_rows: Iterable[Mapping[str, Any]] | None = None,
    preloaded_analysis_snapshot_base_rows: Iterable[Mapping[str, Any]] | None = None,
) -> list[_MeasRow] | None:
    """PostgreSQL compact reader for normalized APERTURE_ZP ensemble input.

    Unsupported storage/chains deliberately return ``None`` so callers fall
    back to ``load_calibration_photometry_view`` unchanged.
    """

    bind = session.get_bind()
    if bind is None or str(bind.dialect.name).lower() != "postgresql":
        return None
    if str(source_cv.product_kind or "").upper() != "APERTURE_ZP":
        return None
    if str(source_cv.materialization_kind or "").upper() != "NORMALIZED_TERMS":
        return None

    # Preserve calibration_view precedence if legacy rows unexpectedly coexist
    # with a nominally normalized-only generation.
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

    if preloaded_aperture_zp_rows is not None:
        phase_started = perf_counter()
        preloaded_rows = tuple(preloaded_aperture_zp_rows)
        result.downstream_seed_reused = True
        result.downstream_seed_rows = len(preloaded_rows)
        result.timings_sec["compact_preloaded_aperture_zp_rows"] = float(len(preloaded_rows))
        result.timings_sec["compact_base_query"] = 0.0
        result.timings_sec["compact_term_load"] = 0.0
        result.timings_sec["compact_plan"] = 0.0

        rows: list[_MeasRow] = []
        for base in preloaded_rows:
            row_catalog_name = base.get("catalog_name")
            if catalog_name and str(row_catalog_name) != str(catalog_name):
                result.skipped_other_catalog += 1
                continue
            value = base.get("mag_cal") if prefer_mag_cal else base.get("mag_inst")
            if not _is_finite(value):
                result.skipped_nonfinite += 1
                continue
            rows.append(
                _MeasRow(
                    measurement=None,
                    frame=_CompactFrameRef(frame_id=int(base["frame_id"])),
                    obj=_CompactObjectRef(object_id=int(base["object_id"])),
                    value=float(value),
                )
            )

        if prepare_analysis_snapshot_seed and preloaded_analysis_snapshot_base_rows is not None:
            seed_rows = tuple(preloaded_analysis_snapshot_base_rows)
            result._analysis_snapshot_base_rows = seed_rows
            result.analysis_snapshot_seed_prepared = True
            result.analysis_snapshot_seed_rows = len(seed_rows)
            result.timings_sec["compact_analysis_seed_reuse"] = 0.0
        result.timings_sec["compact_fused_reconstruction"] = perf_counter() - phase_started
        return rows

    from grandgousier.calibration.reconstruction import _chain_from_base, _latest_iteration

    phase_started = perf_counter()
    chain = _chain_from_base(session, source_cv)
    kinds = [str(cv.product_kind or "").upper() for cv in chain]
    if kinds != ["BASE_PHOTOMETRY", "APERTURE_CORRECTION", "APERTURE_ZP"]:
        return None
    base_cv, aperture_cv, aperture_zp_cv = chain
    if str(base_cv.materialization_kind or "").upper() != "FULL_MEASUREMENTS":
        return None

    aperture_iteration = _latest_iteration(session, int(aperture_cv.calibration_version_id))
    aperture_zp_iteration = _latest_iteration(session, int(aperture_zp_cv.calibration_version_id))
    if aperture_iteration is None or aperture_zp_iteration is None:
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
            Measurement.flags["catalog_name"].as_string().label("catalog_name"),
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
    if preloaded_analysis_snapshot_base_rows is None:
        raw_rows = session.execute(
            stmt.order_by(Measurement.object_id, Measurement.frame_id)
        ).mappings().all()
        result.timings_sec["compact_base_query"] = perf_counter() - phase_started
    else:
        raw_rows = tuple(preloaded_analysis_snapshot_base_rows)
        result.timings_sec["compact_base_query"] = 0.0
        result.timings_sec["compact_analysis_seed_reuse"] = perf_counter() - phase_started
    if prepare_analysis_snapshot_seed:
        # Transient handoff only: dataclass serialization ignores dynamic attrs,
        # so the ~54k seed rows never enter pipeline_run_steps.result_json.
        result._analysis_snapshot_base_rows = tuple(raw_rows)
        result.analysis_snapshot_seed_prepared = True
        result.analysis_snapshot_seed_rows = len(raw_rows)

    phase_started = perf_counter()
    rows = _fuse_compact_aperture_zp_source_rows(
        raw_rows,
        aperture_terms=aperture_terms,
        aperture_zp_terms=aperture_zp_terms,
        aperture_zp_catalog_name=dict(aperture_zp_iteration.parameters or {}).get(
            "catalog_name"
        ),
        requested_catalog_name=catalog_name,
        prefer_mag_cal=prefer_mag_cal,
        result=result,
    )
    result.timings_sec["compact_fused_reconstruction"] = perf_counter() - phase_started
    return rows


def _load_source_measurements(
    session: Session,
    *,
    source_cv: CalibrationVersion,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None,
    prefer_mag_cal: bool,
    result: EnsembleCalibrationResult,
) -> list[_MeasRow]:
    view = load_calibration_photometry_view(
        session,
        calibration_version_id=int(source_cv.calibration_version_id),
        frame_ids=frame_ids,
        prefer_science=False,
    )
    frame_ids_needed = sorted({int(row.frame_id) for row in view.rows})
    object_ids_needed = sorted({int(row.object_id) for row in view.rows})
    frames = {
        int(row.frame_id): row
        for row in session.scalars(select(Frame).where(Frame.frame_id.in_(frame_ids_needed))).all()
    } if frame_ids_needed else {}
    objects = {
        int(row.object_id): row
        for row in session.scalars(select(Object).where(Object.object_id.in_(object_ids_needed))).all()
    } if object_ids_needed else {}

    rows: list[_MeasRow] = []
    sorted_measurements = sorted(
        view.rows,
        key=lambda meas: (
            int(meas.object_id),
            float(frames[int(meas.frame_id)].mjd)
            if int(meas.frame_id) in frames and frames[int(meas.frame_id)].mjd is not None
            else float("inf"),
            int(meas.frame_id),
        ),
    )
    for meas in sorted_measurements:
        frame = frames.get(int(meas.frame_id))
        obj = objects.get(int(meas.object_id))
        if frame is None or obj is None:
            continue
        if instrument_unit_id is not None and frame.instrument_unit_id != int(instrument_unit_id):
            continue
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            result.skipped_other_catalog += 1
            continue
        value = None
        if prefer_mag_cal and meas.mag_cal is not None:
            value = meas.mag_cal
        elif meas.mag_inst is not None:
            value = meas.mag_inst
        elif meas.mag_cal is not None:
            value = meas.mag_cal
        if value is None:
            result.skipped_no_input_mag += 1
            continue
        try:
            value_f = float(value)
        except Exception:
            result.skipped_nonfinite += 1
            continue
        if not math.isfinite(value_f):
            result.skipped_nonfinite += 1
            continue
        rows.append(_MeasRow(measurement=meas, frame=frame, obj=obj, value=value_f))
    # selected_measurements is reset to len(rows) by caller; checked_measurements is useful as raw scan count.
    return rows


def _build_object_stats(object_values: dict[int, list[float]]) -> dict[int, _ObjectStats]:
    out: dict[int, _ObjectStats] = {}
    for object_id, values0 in object_values.items():
        values = [float(v) for v in values0 if _is_finite(v)]
        if not values:
            continue
        out[object_id] = _ObjectStats(
            object_id=object_id,
            n=len(values),
            median_mag=float(median(values)),
            rms=_sample_rms(values),
            mad=_mad(values),
        )
    return out


def _select_reference_objects(
    object_stats: dict[int, _ObjectStats],
    *,
    min_ref_measurements: int,
    max_ref_rms: float | None,
    max_ref_mad: float | None,
    min_mag: float | None,
    max_mag: float | None,
) -> set[int]:
    out: set[int] = set()
    for object_id, st in object_stats.items():
        if st.n < min_ref_measurements:
            continue
        if min_mag is not None and st.median_mag < min_mag:
            continue
        if max_mag is not None and st.median_mag > max_mag:
            continue
        if max_ref_rms is not None:
            if st.rms is None or not _is_finite(st.rms) or st.rms > max_ref_rms:
                continue
        if max_ref_mad is not None:
            if st.mad is None or not _is_finite(st.mad) or st.mad > max_ref_mad:
                continue
        out.add(object_id)
    return out


def _estimate_frame_offsets(
    rows: list[_MeasRow],
    *,
    object_medians: dict[int, float],
    reference_object_ids: set[int],
    min_ref_stars_per_frame: int,
) -> dict[int, _FrameOffset]:
    residuals_by_frame: dict[int, list[float]] = {}
    for row in rows:
        if row.obj.object_id not in reference_object_ids:
            continue
        object_med = object_medians.get(row.obj.object_id)
        if object_med is None or not _is_finite(object_med):
            continue
        residuals_by_frame.setdefault(row.frame.frame_id, []).append(row.value - object_med)
    offsets: dict[int, _FrameOffset] = {}
    for frame_id, residuals0 in residuals_by_frame.items():
        residuals = [float(r) for r in residuals0 if _is_finite(r)]
        if len(residuals) < min_ref_stars_per_frame:
            continue
        offsets[frame_id] = _FrameOffset(
            frame_id=frame_id,
            offset=float(median(residuals)),
            n_ref=len(residuals),
            scatter_mad=_mad(residuals),
        )
    return offsets


def _get_or_create_ensemble_calibration_version(
    session: Session,
    *,
    name: str,
    instrument_unit_id: int | None,
    source_cv: CalibrationVersion,
    source_calibration_name: str,
    catalog_name: str | None,
    min_measurements: int,
    min_ref_measurements: int,
    min_ref_stars_per_frame: int,
    max_ref_rms: float | None,
    max_ref_mad: float | None,
    min_mag: float | None,
    max_mag: float | None,
    n_iterations: int,
    prefer_mag_cal: bool,
    exclude_reference_object_ids: list[int] | None = None,
    immutable_generation: bool = False,
) -> CalibrationVersion:
    params = {
        "source_calibration_name": source_calibration_name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "catalog_name": catalog_name,
        "algorithm": "robust_frame_median_v1",
        "formula": "mag_cal = input_mag - frame_ensemble_offset",
        "input_mag": "mag_cal preferred, fallback mag_inst" if prefer_mag_cal else "mag_inst preferred, fallback mag_cal",
        "min_measurements": min_measurements,
        "min_ref_measurements": min_ref_measurements,
        "min_ref_stars_per_frame": min_ref_stars_per_frame,
        "max_ref_rms": max_ref_rms,
        "max_ref_mad": max_ref_mad,
        "min_mag": min_mag,
        "max_mag": max_mag,
        "n_iterations": n_iterations,
        "warning": "internal ensemble-differential calibration; no color term or absolute standard-system transform",
        "exclude_reference_object_ids": list(exclude_reference_object_ids or []),
    }
    return prepare_calibration_version_for_write(
        session,
        logical_name=name,
        model_name="ensemble_differential_v1",
        instrument_unit_id=instrument_unit_id,
        used_catalogs={"catalog_name": catalog_name} if catalog_name else {},
        parameters=params,
        notes="MVP5.4 robust ensemble differential calibration. Not final color-corrected photometry.",
        source_cv=source_cv,
        immutable_generation=immutable_generation,
    )


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    return float(median(vals)) if vals else None


def _mad(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = median(vals)
    return float(median([abs(v - med) for v in vals]))


def _sample_rms(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return float(math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)))
