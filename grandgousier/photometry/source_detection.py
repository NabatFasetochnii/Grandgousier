from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.detection_io import PostgresSourceDetectionCopyStage, postgres_copy_capable
from grandgousier.db.detection_runs import (
    SOURCE_DETECTION_RUN,
    create_detection_run,
    create_detection_run_frame,
)
from grandgousier.db.models import DetectionRun, DetectionRunFrame, Frame, SourceDetection
from grandgousier.photometry.detection_algorithms import select_separated_candidates as _select_separated_candidates
from grandgousier.photometry.error_model import MEDIAN_BACKGROUND_VARIANCE_FACTOR
from grandgousier.photometry.forced_aperture import measure_one_source


@dataclass
class SourceDetectionResult:
    detection_run_id: int | None = None
    selected_frames: int = 0
    checked_frames: int = 0
    detected: int = 0
    inserted: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    deleted_existing: int = 0
    delete_batches: int = 0
    skipped_missing_file: int = 0
    failed_frames: int = 0
    detection_cap_reached_frames: int = 0
    raw_candidates_total: int = 0
    dry_run: bool = False
    write_strategy: str = "SQLALCHEMY_PER_FRAME_INSERT"
    min_separation_strategy: str = "SPATIAL_HASH_GREEDY_V1"
    candidate_seed_strategy: str = "RECOMPUTE_IMAGE_STATS_V1"
    pre_qc_seed_frames: int = 0
    pre_qc_seed_candidates: int = 0
    pre_qc_seed_invalid_frames: int = 0
    wcs_transform_strategy: str = "BATCH_VECTOR_WITH_SCALAR_FALLBACK_V1"
    aperture_measure_strategy: str = "VECTORIZED_INTERIOR_BATCH_V1"
    aperture_batch_sources: int = 0
    aperture_scalar_fallback_sources: int = 0
    aperture_batch_chunks: int = 0
    kernel_timings_sec: dict[str, float] = field(default_factory=dict)
    staging_rows: int = 0
    staging_copy_batches: int = 0
    frame_load_sec: float = 0.0
    fits_read_sec: float = 0.0
    wcs_setup_sec: float = 0.0
    detection_kernel_sec: float = 0.0
    row_build_sec: float = 0.0
    delete_existing_sec: float = 0.0
    staging_copy_sec: float = 0.0
    insert_select_sec: float = 0.0
    write_finalize_sec: float = 0.0


@dataclass(frozen=True)
class DetectedSource:
    x: float
    y: float
    ra_deg: float | None
    dec_deg: float | None
    peak_adu: float
    snr: float
    flux: float | None
    flux_err: float | None
    local_background: float | None
    npix: int | None
    flags: dict


DEFAULT_ALGORITHM = "numpy_localmax_v1"


def detect_sources_in_frames(
    session: Session,
    *,
    frame_ids: list[int] | None = None,
    instrument: str | None = None,
    instrument_unit_id: int | None = None,
    only_wcs_ok: bool = True,
    threshold_sigma: float = 5.0,
    max_sources_per_frame: int = 2000,
    min_separation_pix: float = 5.0,
    aperture_radius_pix: float = 3.0,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    centroid_box_radius_pix: int = 3,
    replace_existing: bool = False,
    limit_frames: int | None = None,
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    pre_qc_detection_seeds: dict[int, dict] | None = None,
    pipeline_run_id: int | None = None,
) -> SourceDetectionResult:
    if threshold_sigma <= 0:
        raise ValueError("threshold_sigma must be positive")
    if max_sources_per_frame <= 0:
        raise ValueError("max_sources_per_frame must be positive")
    if min_separation_pix < 0:
        raise ValueError("min_separation_pix must be non-negative")

    result = SourceDetectionResult(dry_run=dry_run, insert_batch_size=int(insert_batch_size))
    started = time.perf_counter()
    frames = _select_frames(
        session,
        frame_ids=frame_ids,
        instrument=instrument,
        instrument_unit_id=instrument_unit_id,
        only_wcs_ok=only_wcs_ok,
        limit=limit_frames,
    )
    result.frame_load_sec = float(time.perf_counter() - started)
    result.selected_frames = len(frames)

    parameters = {
        "threshold_sigma": threshold_sigma,
        "max_sources_per_frame": max_sources_per_frame,
        "min_separation_pix": min_separation_pix,
        "aperture_radius_pix": aperture_radius_pix,
        "annulus_inner_pix": annulus_inner_pix,
        "annulus_outer_pix": annulus_outer_pix,
        "centroid_box_radius_pix": centroid_box_radius_pix,
    }

    detection_run = None
    if not dry_run:
        detection_run = create_detection_run(
            session,
            run_kind=SOURCE_DETECTION_RUN,
            algorithm=DEFAULT_ALGORITHM,
            parameters=parameters,
            pipeline_run_id=pipeline_run_id,
        )
        result.detection_run_id = int(detection_run.detection_run_id)

    copy_stage: PostgresSourceDetectionCopyStage | None = None
    copy_buffer: list[dict] = []
    kernel_timings: dict[str, float] = {}
    kernel_counters: dict[str, int] = {}
    successful_frame_ids: list[int] = []
    copy_buffer_rows = 8192
    if dry_run:
        result.write_strategy = "DRY_RUN"
    elif postgres_copy_capable(session):
        copy_stage = PostgresSourceDetectionCopyStage.create(session)
        result.write_strategy = "POSTGRESQL_COPY_STAGING"
    else:
        result.write_strategy = "SQLALCHEMY_PER_FRAME_INSERT"

    try:
        for frame in tqdm(frames, desc="Detecting sources", unit="frame"):
            result.checked_frames += 1
            path = Path(frame.raw_path)
            if not path.exists():
                result.skipped_missing_file += 1
                if detection_run is not None:
                    create_detection_run_frame(
                        session,
                        detection_run_id=int(detection_run.detection_run_id),
                        frame_id=int(frame.frame_id),
                        status="SKIPPED_MISSING_FILE",
                        diagnostics={"reason": "missing_file"},
                    )
                continue

            frame_diagnostics: dict[str, object] = {}
            seed_used = False
            try:
                started = time.perf_counter()
                with fits.open(path, memmap=False) as hdul:
                    data = np.asarray(hdul[0].data, dtype=float)
                    header = hdul[0].header
                result.fits_read_sec += float(time.perf_counter() - started)
                if data.ndim != 2:
                    result.failed_frames += 1
                    if detection_run is not None:
                        create_detection_run_frame(
                            session,
                            detection_run_id=int(detection_run.detection_run_id),
                            frame_id=int(frame.frame_id),
                            status="FAILED",
                            diagnostics={"reason": "non_2d_image", "ndim": int(data.ndim)},
                        )
                    continue
                started = time.perf_counter()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FITSFixedWarning)
                    wcs = WCS(header)
                if not wcs.has_celestial:
                    wcs = None
                result.wcs_setup_sec += float(time.perf_counter() - started)

                detection_seed = None
                if pre_qc_detection_seeds:
                    candidate = pre_qc_detection_seeds.get(int(frame.frame_id))
                    if candidate is not None:
                        if _pre_qc_detection_seed_is_valid(
                            candidate,
                            path=path,
                            threshold_sigma=threshold_sigma,
                        ):
                            detection_seed = candidate
                            seed_used = True
                            result.pre_qc_seed_frames += 1
                            result.pre_qc_seed_candidates += len(candidate.get("candidates") or ())
                            result.candidate_seed_strategy = "PRE_QC_TRANSIENT_HANDOFF_V1"
                        else:
                            result.pre_qc_seed_invalid_frames += 1

                started = time.perf_counter()
                detections = detect_sources_in_image(
                    data,
                    wcs=wcs,
                    threshold_sigma=threshold_sigma,
                    max_sources=max_sources_per_frame,
                    min_separation_pix=min_separation_pix,
                    aperture_radius_pix=aperture_radius_pix,
                    annulus_inner_pix=annulus_inner_pix,
                    annulus_outer_pix=annulus_outer_pix,
                    centroid_box_radius_pix=centroid_box_radius_pix,
                    exptime=_safe_positive_float(frame.exptime),
                    saturation_level_adu=_resolve_saturation(frame, header),
                    candidate_seed=detection_seed,
                    performance_timings=kernel_timings,
                    performance_counters=kernel_counters,
                    diagnostics_out=frame_diagnostics,
                )
                result.detection_kernel_sec += float(time.perf_counter() - started)
                result.detected += len(detections)
                frame_raw_candidates = int(frame_diagnostics.get("n_candidates_raw_before_cap") or 0)
                result.raw_candidates_total += frame_raw_candidates
                if bool(frame_diagnostics.get("detection_cap_reached")):
                    result.detection_cap_reached_frames += 1

                started = time.perf_counter()
                row_payloads = [
                    {
                        "x": det.x,
                        "y": det.y,
                        "ra_deg": det.ra_deg,
                        "dec_deg": det.dec_deg,
                        "peak_adu": det.peak_adu,
                        "flux": det.flux,
                        "flux_err": det.flux_err,
                        "local_background": det.local_background,
                        "snr": det.snr,
                        "npix": det.npix,
                        "detection_status": "DETECTED",
                        "flags": det.flags,
                    }
                    for det in detections
                ]
                result.row_build_sec += float(time.perf_counter() - started)
            except Exception as exc:  # noqa: BLE001 - preserve per-frame isolation.
                result.failed_frames += 1
                if detection_run is not None:
                    create_detection_run_frame(
                        session,
                        detection_run_id=int(detection_run.detection_run_id),
                        frame_id=int(frame.frame_id),
                        status="FAILED",
                        diagnostics={
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:1000],
                        },
                    )
                continue

            if dry_run:
                continue

            assert detection_run is not None
            if replace_existing and copy_stage is None:
                started = time.perf_counter()
                deleted, _ = _delete_prior_source_detection_run_frames(
                    session,
                    frame_ids=[int(frame.frame_id)],
                    algorithm=DEFAULT_ALGORITHM,
                    keep_detection_run_id=int(detection_run.detection_run_id),
                )
                result.delete_existing_sec += float(time.perf_counter() - started)
                result.delete_batches += 1
                result.deleted_existing += deleted

            frame_diagnostics.update(
                {
                    "n_detections": int(len(row_payloads)),
                    "candidate_seed_used": bool(seed_used),
                }
            )
            run_frame = create_detection_run_frame(
                session,
                detection_run_id=int(detection_run.detection_run_id),
                frame_id=int(frame.frame_id),
                status="DONE",
                diagnostics=frame_diagnostics,
            )
            rows = [
                {"detection_run_frame_id": int(run_frame.detection_run_frame_id), **payload}
                for payload in row_payloads
            ]

            if copy_stage is not None:
                successful_frame_ids.append(int(frame.frame_id))
                if rows:
                    copy_buffer.extend(rows)
                    result.inserted += len(rows)
                    if len(copy_buffer) >= copy_buffer_rows:
                        copy_stage.append_rows(copy_buffer)
                        copy_buffer.clear()
                continue

            if rows:
                started = time.perf_counter()
                result.insert_batches += _insert_source_detections(
                    session,
                    rows,
                    insert_batch_size=insert_batch_size,
                )
                result.insert_select_sec += float(time.perf_counter() - started)
                result.inserted += len(rows)

        if not dry_run and copy_stage is not None:
            if copy_buffer:
                copy_stage.append_rows(copy_buffer)
                copy_buffer.clear()

            if replace_existing and successful_frame_ids:
                assert detection_run is not None
                started = time.perf_counter()
                deleted, _ = _delete_prior_source_detection_run_frames(
                    session,
                    frame_ids=successful_frame_ids,
                    algorithm=DEFAULT_ALGORITHM,
                    keep_detection_run_id=int(detection_run.detection_run_id),
                )
                result.delete_existing_sec += float(time.perf_counter() - started)
                result.delete_batches = 1
                result.deleted_existing += deleted

            copy_stage.merge()
            result.staging_rows = int(copy_stage.rows_staged)
            result.staging_copy_batches = int(copy_stage.copy_batches)
            result.staging_copy_sec = float(copy_stage.staging_copy_sec)
            result.insert_select_sec = float(copy_stage.insert_select_sec)
            result.insert_batches = 1 if copy_stage.rows_staged else 0

        if not dry_run:
            started = time.perf_counter()
            if copy_stage is not None:
                copy_stage.close()
            session.commit()
            result.write_finalize_sec = float(time.perf_counter() - started)
    finally:
        if copy_stage is not None and not copy_stage.closed:
            try:
                copy_stage.close()
            except Exception:
                pass

    result.kernel_timings_sec = {key: float(value) for key, value in sorted(kernel_timings.items())}
    result.aperture_batch_sources = int(kernel_counters.get("aperture_batch_sources", 0))
    result.aperture_scalar_fallback_sources = int(
        kernel_counters.get("aperture_scalar_fallback_sources", 0)
    )
    result.aperture_batch_chunks = int(kernel_counters.get("aperture_batch_chunks", 0))
    return result



def _delete_prior_source_detection_run_frames(
    session: Session,
    *,
    frame_ids: list[int],
    algorithm: str,
    keep_detection_run_id: int,
) -> tuple[int, int]:
    """Delete prior source-detection frame scopes and cascade their derived rows."""

    if not frame_ids:
        return 0, 0
    old_run_frame_ids = (
        select(DetectionRunFrame.detection_run_frame_id)
        .join(DetectionRun, DetectionRun.detection_run_id == DetectionRunFrame.detection_run_id)
        .where(
            DetectionRunFrame.frame_id.in_(frame_ids),
            DetectionRun.run_kind == SOURCE_DETECTION_RUN,
            DetectionRun.algorithm == algorithm,
            DetectionRun.detection_run_id != int(keep_detection_run_id),
        )
    )
    n_detections = int(
        session.scalar(
            select(func.count(SourceDetection.source_detection_id)).where(
                SourceDetection.detection_run_frame_id.in_(old_run_frame_ids)
            )
        )
        or 0
    )
    deleted_run_frames = int(
        session.execute(
            delete(DetectionRunFrame).where(
                DetectionRunFrame.detection_run_frame_id.in_(old_run_frame_ids)
            )
        ).rowcount
        or 0
    )
    return n_detections, deleted_run_frames


def _insert_source_detections(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    if not rows:
        return 0
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        session.execute(insert(SourceDetection.__table__).values(list(chunk)))
        n_batches += 1
    return n_batches


def detect_sources_in_image(
    data: np.ndarray,
    *,
    wcs: WCS | None,
    threshold_sigma: float,
    max_sources: int,
    min_separation_pix: float,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    centroid_box_radius_pix: int,
    exptime: float | None,
    saturation_level_adu: float | None,
    candidate_seed: dict | None = None,
    performance_timings: dict[str, float] | None = None,
    performance_counters: dict[str, int] | None = None,
    diagnostics_out: dict[str, object] | None = None,
) -> list[DetectedSource]:
    if diagnostics_out is not None:
        diagnostics_out.clear()

    def _add_timing(key: str, started: float) -> None:
        if performance_timings is not None:
            performance_timings[key] = performance_timings.get(key, 0.0) + float(
                time.perf_counter() - started
            )

    phase_started = time.perf_counter()
    seeded = _candidate_seed_values(candidate_seed, threshold_sigma=threshold_sigma)
    if seeded is None:
        finite = np.isfinite(data)
        if finite.sum() < 100:
            if diagnostics_out is not None:
                diagnostics_out.update({
                    "empty_reason": "insufficient_finite_pixels",
                    "finite_pixels": int(finite.sum()),
                })
            _add_timing("background_stats", phase_started)
            return []
        values = data[finite]
        background = float(np.nanmedian(values))
        mad = float(np.nanmedian(np.abs(values - background)))
        sigma = 1.4826 * mad
        if not math.isfinite(sigma) or sigma <= 0:
            sigma = float(np.nanstd(values))
        if not math.isfinite(sigma) or sigma <= 0:
            if diagnostics_out is not None:
                diagnostics_out.update({
                    "empty_reason": "invalid_background_sigma",
                    "background_global": float(background),
                })
            _add_timing("background_stats", phase_started)
            return []
        threshold = background + threshold_sigma * sigma
        _add_timing("background_stats", phase_started)

        phase_started = time.perf_counter()
        candidates = _local_maxima(data, threshold=threshold)
        _add_timing("local_maxima", phase_started)
    else:
        background, sigma, threshold, candidates = seeded
        _add_timing("pre_qc_seed_decode", phase_started)

    n_candidates_raw = len(candidates)
    if diagnostics_out is not None:
        diagnostics_out.update({
            "background_global": float(background),
            "sigma_global": float(sigma),
            "threshold_adu": float(threshold),
            "centroid_method": "positive_com",
            "n_candidates_raw_before_cap": int(n_candidates_raw),
            "max_sources_per_frame": int(max_sources),
            "detection_cap_reached": False,
        })
    if not candidates:
        return []

    phase_started = time.perf_counter()
    scored = []
    for y_int, x_int in candidates:
        peak = float(data[y_int, x_int])
        snr = (peak - background) / sigma
        if math.isfinite(snr):
            scored.append((snr, y_int, x_int, peak))
    scored.sort(reverse=True)
    _add_timing("score_sort", phase_started)

    phase_started = time.perf_counter()
    chosen = _select_separated_candidates(
        scored,
        max_sources=max_sources,
        min_separation_pix=min_separation_pix,
    )
    _add_timing("min_separation", phase_started)

    phase_started = time.perf_counter()
    centered: list[tuple[float, float, float, float]] = []
    for snr, y0, x0, peak in chosen:
        x_cen, y_cen = _centroid_com(
            data, x0, y0, background, radius=centroid_box_radius_pix
        )
        if x_cen is None or y_cen is None:
            x_cen, y_cen = x0, y0
        centered.append((float(snr), float(x_cen), float(y_cen), float(peak)))
    _add_timing("centroid", phase_started)

    phase_started = time.perf_counter()
    world = _pixel_to_world_batch(wcs, centered)
    _add_timing("wcs_transform", phase_started)

    phase_started = time.perf_counter()
    aperture_measurements = _measure_detection_apertures_batch(
        data,
        centered,
        aperture_radius_pix=aperture_radius_pix,
        annulus_inner_pix=annulus_inner_pix,
        annulus_outer_pix=annulus_outer_pix,
        exptime=exptime,
        saturation_level_adu=saturation_level_adu,
        performance_timings=performance_timings,
        performance_counters=performance_counters,
    )
    detections: list[DetectedSource] = []
    cap_reached = bool(len(chosen) >= max_sources and n_candidates_raw >= max_sources)
    for (snr, x_cen, y_cen, peak), (ra_deg, dec_deg), measurement in zip(
        centered, world, aperture_measurements, strict=True
    ):
        flags = dict(measurement.flags)
        detections.append(
            DetectedSource(
                x=x_cen,
                y=y_cen,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                peak_adu=peak,
                snr=snr,
                flux=measurement.flux,
                flux_err=measurement.flux_err,
                local_background=measurement.local_background,
                npix=None,
                flags=flags,
            )
        )
    _add_timing("aperture_measure_and_finalize", phase_started)
    if diagnostics_out is not None:
        diagnostics_out.update({
            "background_global": float(background),
            "sigma_global": float(sigma),
            "threshold_adu": float(threshold),
            "centroid_method": "positive_com",
            "n_candidates_raw_before_cap": int(n_candidates_raw),
            "max_sources_per_frame": int(max_sources),
            "detection_cap_reached": bool(cap_reached),
        })
    return detections



@dataclass(frozen=True, slots=True)
class _DetectionApertureMeasurement:
    flux: float | None
    flux_err: float | None
    local_background: float | None
    flags: dict


def _measure_detection_apertures_batch(
    data: np.ndarray,
    centered: list[tuple[float, float, float, float]],
    *,
    aperture_radius_pix: float,
    annulus_inner_pix: float,
    annulus_outer_pix: float,
    exptime: float | None,
    saturation_level_adu: float | None,
    performance_timings: dict[str, float] | None = None,
    performance_counters: dict[str, int] | None = None,
    batch_size: int = 128,
) -> list[_DetectionApertureMeasurement]:
    """Measure detection apertures in vectorized interior batches.

    Source detection needs only flux, flux uncertainty, local background and the
    small set of aperture flags.  Calling :func:`measure_one_source` separately
    for every local maximum rebuilt a ~27x27 coordinate grid and two masks for
    each source.  Here sources whose complete outer-annulus cutout is inside the
    image are gathered in batches and share NumPy broadcasting for geometry and
    robust background statistics.  Edge/degenerate cases retain the historical
    scalar implementation exactly.

    The selected random-error formula is algebraically identical to the default
    ``physical_aperture_v1`` path used historically by detection: gain fallback
    1 e-/ADU, read-noise fallback 0, empirical-annulus floor enabled, no
    fractional floor, and unavailable scintillation because telescope/airmass
    metadata are not supplied by the detection branch.
    """

    if not centered:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    def _timing_add(key: str, started: float) -> None:
        if performance_timings is not None:
            performance_timings[key] = performance_timings.get(key, 0.0) + float(
                time.perf_counter() - started
            )

    def _counter_add(key: str, value: int) -> None:
        if performance_counters is not None:
            performance_counters[key] = performance_counters.get(key, 0) + int(value)

    ny, nx = data.shape
    pad = int(math.ceil(float(annulus_outer_pix) + 1.0))
    offsets_i = np.arange(-pad, pad + 1, dtype=np.intp)
    offsets_f = offsets_i.astype(float, copy=False)

    results: list[_DetectionApertureMeasurement | None] = [None] * len(centered)
    interior_indices: list[int] = []
    fallback_indices: list[int] = []
    for idx, (_, x, y, _) in enumerate(centered):
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
    estimator_factor = float(MEDIAN_BACKGROUND_VARIANCE_FACTOR)

    for chunk_start in range(0, len(interior_indices), int(batch_size)):
        chunk_indices = interior_indices[chunk_start : chunk_start + int(batch_size)]
        if not chunk_indices:
            continue
        _counter_add("aperture_batch_chunks", 1)

        phase_started = time.perf_counter()
        xs = np.asarray([centered[idx][1] for idx in chunk_indices], dtype=float)
        ys = np.asarray([centered[idx][2] for idx in chunk_indices], dtype=float)
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
        _timing_add("aperture_batch_geometry", phase_started)

        # Degenerate finite-pixel patterns are rare and are deliberately sent
        # through the scalar implementation to preserve every historical edge
        # flag/fallback semantic.
        good = (n_ap > 0) & (n_sky >= 5)
        for local_idx, source_idx in enumerate(chunk_indices):
            if not bool(good[local_idx]):
                fallback_indices.append(source_idx)

        good_local = np.flatnonzero(good)
        if good_local.size == 0:
            continue

        phase_started = time.perf_counter()
        selected_cutouts = cutouts[good_local]
        selected_aperture_valid = aperture_valid[good_local]
        selected_annulus_valid = annulus_valid[good_local]
        selected_n_ap = n_ap[good_local]
        selected_n_sky = n_sky[good_local]

        annulus_values = np.where(selected_annulus_valid, selected_cutouts, np.nan)
        aperture_values = np.where(selected_aperture_valid, selected_cutouts, np.nan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            local_background = np.nanmedian(annulus_values, axis=(1, 2))
            annulus_mad = np.nanmedian(
                np.abs(annulus_values - local_background[:, None, None]),
                axis=(1, 2),
            )
            annulus_sigma = 1.4826 * annulus_mad
            bad_sigma = (~np.isfinite(annulus_sigma)) | (annulus_sigma <= 0)
            if np.any(bad_sigma):
                annulus_sigma[bad_sigma] = np.nanstd(
                    annulus_values[bad_sigma], axis=(1, 2)
                )
            aperture_sum = np.nansum(aperture_values, axis=(1, 2))
            aperture_max = (
                np.nanmax(aperture_values, axis=(1, 2))
                if saturation_level_adu is not None
                else None
            )
        flux = aperture_sum - local_background * selected_n_ap
        _timing_add("aperture_batch_statistics", phase_started)

        phase_started = time.perf_counter()
        sigma_for_error = np.where(
            np.isfinite(annulus_sigma) & (annulus_sigma > 0), annulus_sigma, 0.0
        )
        source_poisson_variance = np.maximum(flux, 0.0)
        empirical_background_variance = sigma_for_error * sigma_for_error
        detector_background_variance = np.maximum(local_background, 0.0)
        selected_background_variance = np.maximum(
            empirical_background_variance, detector_background_variance
        )
        physical_variance = (
            source_poisson_variance
            + selected_n_ap * selected_background_variance
            + estimator_factor
            * (selected_n_ap * selected_n_ap)
            * selected_background_variance
            / selected_n_sky
        )
        flux_err = np.where(
            np.isfinite(physical_variance) & (physical_variance > 0),
            np.sqrt(np.maximum(physical_variance, 0.0)),
            np.nan,
        )

        for output_local, source_local in enumerate(good_local.tolist()):
            source_idx = chunk_indices[source_local]
            source_flux = float(flux[output_local])
            source_flux_err = float(flux_err[output_local])
            flags: dict[str, object] = {}
            if saturation_level_adu is not None and aperture_max is not None:
                source_max = float(aperture_max[output_local])
                if source_max >= float(saturation_level_adu):
                    flags["saturated_or_near_saturated"] = True
                    flags["aperture_max_adu"] = source_max
            if source_flux <= 0:
                flags["non_positive_flux"] = True
            results[source_idx] = _DetectionApertureMeasurement(
                flux=source_flux,
                flux_err=(source_flux_err if math.isfinite(source_flux_err) else None),
                local_background=float(local_background[output_local]),
                flags=flags,
            )
        _counter_add("aperture_batch_sources", int(good_local.size))
        _timing_add("aperture_batch_finalize", phase_started)

    # Preserve the old implementation for edges, empty apertures, and sparse
    # annuli.  Sorting makes the fallback deterministic and prevents duplicate
    # work when an interior source becomes degenerate after finite-pixel tests.
    if fallback_indices:
        phase_started = time.perf_counter()
        fallback_unique = sorted(set(fallback_indices))
        for idx in fallback_unique:
            _, x, y, _ = centered[idx]
            measurement = measure_one_source(
                data,
                x=x,
                y=y,
                aperture_radius_pix=aperture_radius_pix,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                exptime=exptime,
                zero_point=0.0,
                saturation_level_adu=saturation_level_adu,
            )
            results[idx] = _DetectionApertureMeasurement(
                flux=measurement.flux,
                flux_err=measurement.flux_err,
                local_background=measurement.local_background,
                flags=dict(measurement.flags),
            )
        _counter_add("aperture_scalar_fallback_sources", len(fallback_unique))
        _timing_add("aperture_scalar_fallback", phase_started)

    if any(row is None for row in results):
        raise RuntimeError("detection aperture batch left an unmeasured source")
    return [row for row in results if row is not None]

def _candidate_seed_values(
    candidate_seed: dict | None,
    *,
    threshold_sigma: float,
) -> tuple[float, float, float, tuple[tuple[int, int], ...]] | None:
    if not isinstance(candidate_seed, dict):
        return None
    try:
        seed_threshold_sigma = float(candidate_seed.get("threshold_sigma"))
        background = float(candidate_seed.get("background"))
        sigma = float(candidate_seed.get("sigma"))
        threshold = float(candidate_seed.get("threshold"))
    except (TypeError, ValueError):
        return None
    if not math.isclose(seed_threshold_sigma, float(threshold_sigma), rel_tol=0.0, abs_tol=1e-12):
        return None
    if not all(math.isfinite(value) for value in (background, sigma, threshold)) or sigma <= 0:
        return None
    raw_candidates = candidate_seed.get("candidates")
    if not isinstance(raw_candidates, (tuple, list)):
        return None
    try:
        candidates = tuple((int(row[0]), int(row[1])) for row in raw_candidates)
    except (TypeError, ValueError, IndexError):
        return None
    return background, sigma, threshold, candidates


def _pre_qc_detection_seed_is_valid(
    seed: dict,
    *,
    path: Path,
    threshold_sigma: float,
) -> bool:
    if _candidate_seed_values(seed, threshold_sigma=threshold_sigma) is None:
        return False
    raw_path = seed.get("raw_path")
    if raw_path is not None and str(path) != str(raw_path):
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if seed.get("file_size") is not None and int(seed["file_size"]) != int(stat.st_size):
        return False
    if seed.get("file_mtime_ns") is not None and int(seed["file_mtime_ns"]) != int(stat.st_mtime_ns):
        return False
    return True


def _pixel_to_world_batch(
    wcs: WCS | None,
    centered: list[tuple[float, float, float, float]],
) -> list[tuple[float | None, float | None]]:
    if not centered:
        return []
    if wcs is None:
        return [(None, None)] * len(centered)

    xs = np.asarray([row[1] for row in centered], dtype=float)
    ys = np.asarray([row[2] for row in centered], dtype=float)
    try:
        ras, decs = wcs.pixel_to_world_values(xs, ys)
        ras = np.asarray(ras, dtype=float).reshape(-1)
        decs = np.asarray(decs, dtype=float).reshape(-1)
        if ras.size != len(centered) or decs.size != len(centered):
            raise ValueError("unexpected vector WCS result shape")
        result: list[tuple[float | None, float | None]] = []
        for ra, dec in zip(ras, decs, strict=True):
            if math.isfinite(float(ra)) and math.isfinite(float(dec)):
                result.append((float(ra) % 360.0, float(dec)))
            else:
                result.append((None, None))
        return result
    except Exception:
        # Preserve the historical per-source exception semantics when a WCS
        # implementation cannot process vector inputs.
        result = []
        for row in centered:
            try:
                ra, dec = wcs.pixel_to_world_values(float(row[1]), float(row[2]))
                if math.isfinite(float(ra)) and math.isfinite(float(dec)):
                    result.append((float(ra) % 360.0, float(dec)))
                else:
                    result.append((None, None))
            except Exception:
                result.append((None, None))
        return result


def _local_maxima(data: np.ndarray, *, threshold: float) -> list[tuple[int, int]]:
    finite = np.isfinite(data)
    mask = finite & (data > threshold)
    if not np.any(mask):
        return []
    padded = np.pad(data, 1, mode="constant", constant_values=-np.inf)
    center = padded[1:-1, 1:-1]
    local = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neigh = padded[1 + dy : 1 + dy + data.shape[0], 1 + dx : 1 + dx + data.shape[1]]
            local &= center >= neigh
    ys, xs = np.nonzero(local)
    return list(zip(ys.tolist(), xs.tolist()))


def _centroid_com(data: np.ndarray, x: float, y: float, background: float, *, radius: int) -> tuple[float | None, float | None]:
    x_int = int(round(x))
    y_int = int(round(y))
    y0 = max(0, y_int - radius)
    y1 = min(data.shape[0], y_int + radius + 1)
    x0 = max(0, x_int - radius)
    x1 = min(data.shape[1], x_int + radius + 1)
    cut = data[y0:y1, x0:x1] - background
    cut = np.where(np.isfinite(cut) & (cut > 0), cut, 0.0)
    total = float(cut.sum())
    if total <= 0 or not math.isfinite(total):
        return None, None
    yy, xx = np.indices(cut.shape)
    x_cen = float((cut * (xx + x0)).sum() / total)
    y_cen = float((cut * (yy + y0)).sum() / total)
    return x_cen, y_cen


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


def _safe_positive_float(value) -> float | None:
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    return x


def _resolve_saturation(frame: Frame, header) -> float | None:
    for key in ("SATURATE", "SATLEVEL", "SATURATI", "SATURATE_LEVEL"):
        try:
            value = float(header.get(key))
        except Exception:
            value = None
        if value is not None and math.isfinite(value) and value > 0:
            return value
    return None
