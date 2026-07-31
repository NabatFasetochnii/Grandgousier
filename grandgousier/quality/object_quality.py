from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from grandgousier.calibration.provenance import (
    get_source_calibration_name,
    get_source_calibration_version_id,
    resolve_source_calibration,
)
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks, row_column_count
from grandgousier.db.models import (
    CalibrationVersion,
    CatalogSource,
    DetectionCatalogMatch,
    Frame,
    InstrumentUnit,
    Measurement,
    Object,
    ObjectQc,
    SourceDetection,
)
from grandgousier.quality.lightcurve_quality import ObjectScatterRow, load_object_scatter_rows


@dataclass
class ObjectQcResult:
    calibration_version_id: int | None = None
    calibration_name: str | None = None
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    selected_objects: int = 0
    checked_objects: int = 0
    written: int = 0
    insert_batches: int = 0
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE
    crowded: int = 0
    blended_neighbor: int = 0
    edge: int = 0
    saturated_like: int = 0
    high_rms: int = 0
    faint_noisy: int = 0
    no_detection_support: int = 0
    weak_detection_support: int = 0
    large_catalog_mag_residual: int = 0
    possible_proper_motion_mismatch: int = 0
    dynamic_crowded: int = 0
    dynamic_blend_risk: int = 0
    possible_blend: int = 0
    aperture_unstable: int = 0
    aperture_median_shift: int = 0
    preserved_aperture_stability: int = 0
    multiple_detections_per_frame: int = 0
    dry_run: bool = False


@dataclass
class ApertureStabilityTransferResult:
    source_calibration_version_id: int | None = None
    source_calibration_name: str | None = None
    target_calibration_version_id: int | None = None
    target_calibration_name: str | None = None
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    algorithm: str = "object_qc_v1"
    source_rows: int = 0
    target_rows: int = 0
    updated: int = 0
    created: int = 0
    skipped_no_aperture_stability: int = 0
    skipped_missing_target_qc: int = 0
    dry_run: bool = False


def compute_object_qc(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    algorithm: str = "object_qc_v1",
    crowded_arcsec: float = 5.0,
    blended_arcsec: float = 3.0,
    edge_distance_pix: float = 10.0,
    high_rms: float = 0.10,
    high_mag_err: float = 0.08,
    min_detection_fraction: float = 0.5,
    max_ref_mag_delta: float | None = None,
    proper_motion_sep_arcsec: float = 1.0,
    dynamic_separation: bool = False,
    dynamic_seeing_factor: float = 1.5,
    dynamic_aperture_factor: float = 1.0,
    dynamic_crowding_factor: float = 1.5,
    min_dynamic_blend_arcsec: float | None = None,
    fwhm_header_unit: str = "arcsec",
    dry_run: bool = False,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
    target_object_ids: list[int] | None = None,
) -> ObjectQcResult:
    """Compute object-level quality flags from catalog geometry and measurements."""

    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")

    object_rows = load_object_scatter_rows(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        min_measurements=min_measurements,
    )
    result = ObjectQcResult(
        calibration_version_id=cv.calibration_version_id,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        selected_objects=len(object_rows),
        insert_batch_size=int(insert_batch_size),
        dry_run=dry_run,
    )

    if not object_rows:
        return result

    target_ids = {int(v) for v in (target_object_ids or []) if v is not None}

    catalog_sources_by_id = _load_catalog_sources_by_source_id(session, catalog_name=catalog_name)
    neighbor_stats = _build_neighbor_stats(catalog_sources_by_id)
    measurement_stats = _load_measurement_geometry(
        session,
        calibration_version_id=cv.calibration_version_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        fwhm_header_unit=fwhm_header_unit,
    )
    detection_stats = _load_detection_support_stats(
        session,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
    )
    existing_qc_map = load_object_qc_map(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
    )
    source_aperture_cv = resolve_source_calibration(session, cv)
    source_aperture_calibration_name = source_aperture_cv.name if source_aperture_cv is not None else get_source_calibration_name(cv)
    source_aperture_calibration_id = (
        source_aperture_cv.calibration_version_id
        if source_aperture_cv is not None
        else get_source_calibration_version_id(cv)
    )
    source_aperture_qc_map: dict[int, ObjectQc] = {}
    if source_aperture_calibration_id is not None:
        source_aperture_qc_map = load_object_qc_map(
            session,
            calibration_version_id=source_aperture_calibration_id,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            algorithm=algorithm,
        )
    elif source_aperture_calibration_name:
        source_aperture_qc_map = load_object_qc_map(
            session,
            calibration_name=source_aperture_calibration_name,
            catalog_name=catalog_name,
            instrument_unit_id=instrument_unit_id,
            algorithm=algorithm,
        )

    rows = []
    for row in tqdm(object_rows, desc="Object QC", unit="object"):
        result.checked_objects += 1
        source_id = row.source_id
        cs = catalog_sources_by_id.get(str(source_id)) if source_id is not None else None
        nn = neighbor_stats.get(str(source_id)) if source_id is not None else None
        geom = measurement_stats.get(row.object_id, {})
        dyn = _compute_dynamic_separation_thresholds(
            geom,
            seeing_factor=dynamic_seeing_factor,
            aperture_factor=dynamic_aperture_factor,
            crowding_factor=dynamic_crowding_factor,
            min_blend_arcsec=min_dynamic_blend_arcsec,
        ) if dynamic_separation else {}

        nearest_neighbor_arcsec = nn.get("nearest_neighbor_arcsec") if nn else None
        neighbors_within_3 = int(nn.get("neighbors_within_3arcsec", 0)) if nn else 0
        neighbors_within_5 = int(nn.get("neighbors_within_5arcsec", 0)) if nn else 0
        neighbors_within_10 = int(nn.get("neighbors_within_10arcsec", 0)) if nn else 0

        is_field_target = int(row.object_id) in target_ids
        flags: dict[str, bool] = {}
        if is_field_target:
            flags["FIELD_TARGET"] = True
        if nearest_neighbor_arcsec is not None and nearest_neighbor_arcsec < float(crowded_arcsec):
            flags["CROWDED"] = True
        if nearest_neighbor_arcsec is not None and nearest_neighbor_arcsec < float(blended_arcsec):
            flags["BLENDED_NEIGHBOR"] = True
        if neighbors_within_3 > 0:
            flags["NEIGHBOR_WITHIN_3ARCSEC"] = True
        if neighbors_within_5 > 0:
            flags["NEIGHBOR_WITHIN_5ARCSEC"] = True
        dynamic_blend_arcsec = dyn.get("dynamic_blend_arcsec")
        dynamic_crowded_arcsec = dyn.get("dynamic_crowded_arcsec")
        separation_index = None
        crowding_index = None
        if nearest_neighbor_arcsec is not None and dynamic_blend_arcsec is not None and _is_finite(dynamic_blend_arcsec) and float(dynamic_blend_arcsec) > 0:
            separation_index = float(nearest_neighbor_arcsec) / float(dynamic_blend_arcsec)
            dyn["nearest_neighbor_arcsec"] = float(nearest_neighbor_arcsec)
            dyn["separation_index"] = separation_index
            if separation_index < 1.0:
                dyn["separation_class"] = "inside_dynamic_blend_radius"
            elif separation_index < 1.5:
                dyn["separation_class"] = "marginal_dynamic_separation"
            else:
                dyn["separation_class"] = "separated_by_dynamic_threshold"
            if float(nearest_neighbor_arcsec) < float(dynamic_blend_arcsec):
                flags["DYNAMIC_BLEND_RISK"] = True
                flags["POSSIBLE_BLEND"] = True
        if nearest_neighbor_arcsec is not None and dynamic_crowded_arcsec is not None and _is_finite(dynamic_crowded_arcsec) and float(dynamic_crowded_arcsec) > 0:
            crowding_index = float(nearest_neighbor_arcsec) / float(dynamic_crowded_arcsec)
            dyn["crowding_index"] = crowding_index
            if float(nearest_neighbor_arcsec) < float(dynamic_crowded_arcsec):
                flags["DYNAMIC_CROWDED"] = True
                flags["CROWDED"] = True
                flags["POSSIBLE_BLEND"] = True
        edge_min = geom.get("edge_distance_pix_min")
        if edge_min is not None and edge_min < float(edge_distance_pix):
            flags["EDGE"] = True
        if geom.get("saturated_like"):
            flags["SATURATED_LIKE"] = True
        if row.mag_rms is not None and _is_finite(row.mag_rms) and float(row.mag_rms) >= float(high_rms):
            flags["HIGH_RMS"] = True
        if row.mag_err_median is not None and _is_finite(row.mag_err_median) and float(row.mag_err_median) >= float(high_mag_err):
            flags["FAINT_NOISY"] = True
        if row.n_measurements < int(min_measurements):
            flags["LOW_N"] = True

        det = detection_stats.get(getattr(cs, "catalog_source_id", None), {}) if cs is not None else {}
        # Detection support must be frame-based. A crowded source can receive more than
        # one detection-catalog match in the same image; counting raw matches would make
        # a "fraction" larger than 1.0 and would overstate support.
        n_detection_matches_raw = int(det.get("n_detection_matches_raw", det.get("n_detection_matches", 0)) or 0)
        n_detection_match_frames = int(det.get("n_detection_match_frames", 0) or 0)
        n_detection_matches = n_detection_match_frames  # Backward-compatible alias: unique-frame support.
        n_extra_detection_matches = int(det.get("n_extra_detection_matches", max(0, n_detection_matches_raw - n_detection_match_frames)) or 0)
        n_multiple_detection_frames = int(det.get("n_multiple_detection_frames", 0) or 0)
        detection_frame_fraction = (
            float(n_detection_match_frames) / float(row.n_measurements)
            if row.n_measurements and int(row.n_measurements) > 0
            else None
        )
        detection_fraction = detection_frame_fraction  # Backward-compatible name, now always frame-based.
        multiple_detection_frame_ratio = (
            float(n_detection_matches_raw) / float(n_detection_match_frames)
            if n_detection_match_frames > 0
            else None
        )
        det_sep_median_arcsec = det.get("det_sep_median_arcsec")
        det_snr_median = det.get("det_snr_median")
        det_snr_max = det.get("det_snr_max")

        ref_mag = row.ref_mag
        mag_ref_delta = None
        abs_mag_ref_delta = None
        if row.mag_median is not None and ref_mag is not None and _is_finite(row.mag_median) and _is_finite(ref_mag):
            mag_ref_delta = float(row.mag_median) - float(ref_mag)
            abs_mag_ref_delta = abs(mag_ref_delta)

        if n_detection_match_frames <= 0:
            flags["NO_DETECTION_SUPPORT"] = True
        elif detection_frame_fraction is not None and detection_frame_fraction < float(min_detection_fraction):
            flags["WEAK_DETECTION_SUPPORT"] = True
        if n_extra_detection_matches > 0 or n_multiple_detection_frames > 0:
            flags["MULTIPLE_DETECTIONS_PER_FRAME"] = True

        if max_ref_mag_delta is not None and abs_mag_ref_delta is not None and abs_mag_ref_delta > float(max_ref_mag_delta):
            flags["LARGE_CATALOG_MAG_RESIDUAL"] = True
            if det_sep_median_arcsec is not None and _is_finite(det_sep_median_arcsec) and float(det_sep_median_arcsec) > float(proper_motion_sep_arcsec):
                flags["POSSIBLE_PROPER_MOTION_MISMATCH"] = True
            if flags.get("NO_DETECTION_SUPPORT") or flags.get("WEAK_DETECTION_SUPPORT"):
                flags["POSSIBLE_PROPER_MOTION_MISMATCH"] = True

        existing_qc = existing_qc_map.get(row.object_id)
        source_aperture_qc = source_aperture_qc_map.get(row.object_id)
        preserved_aperture_qc = _select_aperture_stability_qc(existing_qc, source_aperture_qc)
        if _preserve_aperture_stability_flags(preserved_aperture_qc, flags):
            result.preserved_aperture_stability += 1

        result.crowded += int(bool(flags.get("CROWDED")))
        result.blended_neighbor += int(bool(flags.get("BLENDED_NEIGHBOR")))
        result.edge += int(bool(flags.get("EDGE")))
        result.saturated_like += int(bool(flags.get("SATURATED_LIKE")))
        result.high_rms += int(bool(flags.get("HIGH_RMS")))
        result.faint_noisy += int(bool(flags.get("FAINT_NOISY")))
        result.no_detection_support += int(bool(flags.get("NO_DETECTION_SUPPORT")))
        result.weak_detection_support += int(bool(flags.get("WEAK_DETECTION_SUPPORT")))
        result.large_catalog_mag_residual += int(bool(flags.get("LARGE_CATALOG_MAG_RESIDUAL")))
        result.possible_proper_motion_mismatch += int(bool(flags.get("POSSIBLE_PROPER_MOTION_MISMATCH")))
        result.dynamic_crowded += int(bool(flags.get("DYNAMIC_CROWDED")))
        result.dynamic_blend_risk += int(bool(flags.get("DYNAMIC_BLEND_RISK")))
        result.possible_blend += int(bool(flags.get("POSSIBLE_BLEND")))
        result.aperture_unstable += int(bool(flags.get("APERTURE_UNSTABLE")))
        result.aperture_median_shift += int(bool(flags.get("APERTURE_MEDIAN_SHIFT")))
        result.multiple_detections_per_frame += int(bool(flags.get("MULTIPLE_DETECTIONS_PER_FRAME")))

        rows.append(
            {
                "object_id": row.object_id,
                "calibration_version_id": cv.calibration_version_id,
                "instrument_unit_id": instrument_unit_id if instrument_unit_id is not None else row.instrument_unit_id,
                "catalog_name": row.catalog_name or catalog_name,
                "algorithm": algorithm,
                "nearest_neighbor_arcsec": nearest_neighbor_arcsec,
                "neighbors_within_3arcsec": neighbors_within_3,
                "neighbors_within_5arcsec": neighbors_within_5,
                "neighbors_within_10arcsec": neighbors_within_10,
                "median_x": geom.get("median_x"),
                "median_y": geom.get("median_y"),
                "edge_distance_pix_min": edge_min,
                "n_measurements": int(row.n_measurements),
                "mag_median": row.mag_median,
                "mag_rms": row.mag_rms,
                "mag_mad": row.mag_mad,
                "mag_err_median": row.mag_err_median,
                "flags": flags,
                "details": _merge_preserved_qc_details(
                    {
                        "source_id": source_id,
                        "ra_deg": row.ra_deg,
                        "dec_deg": row.dec_deg,
                        "ref_mag": row.ref_mag,
                        "color": row.color,
                        "catalog_source_id": getattr(cs, "catalog_source_id", None),
                        "thresholds": {
                            "crowded_arcsec": crowded_arcsec,
                            "blended_arcsec": blended_arcsec,
                            "edge_distance_pix": edge_distance_pix,
                            "high_rms": high_rms,
                            "high_mag_err": high_mag_err,
                            "min_detection_fraction": min_detection_fraction,
                            "max_ref_mag_delta": max_ref_mag_delta,
                            "proper_motion_sep_arcsec": proper_motion_sep_arcsec,
                            "dynamic_separation": dynamic_separation,
                            "dynamic_seeing_factor": dynamic_seeing_factor,
                            "dynamic_aperture_factor": dynamic_aperture_factor,
                            "dynamic_crowding_factor": dynamic_crowding_factor,
                            "min_dynamic_blend_arcsec": min_dynamic_blend_arcsec,
                            "fwhm_header_unit": fwhm_header_unit,
                        },
                        "dynamic_separation": dyn,
                        "detection_support": {
                            "n_detection_matches": n_detection_match_frames,
                            "n_detection_match_frames": n_detection_match_frames,
                            "n_detection_matches_raw": n_detection_matches_raw,
                            "n_extra_detection_matches": n_extra_detection_matches,
                            "n_multiple_detection_frames": n_multiple_detection_frames,
                            "detection_fraction": detection_fraction,
                            "detection_frame_fraction": detection_frame_fraction,
                            "multiple_detection_frame_ratio": multiple_detection_frame_ratio,
                            "det_sep_median_arcsec": det_sep_median_arcsec,
                            "det_snr_median": det_snr_median,
                            "det_snr_max": det_snr_max,
                        },
                        "target": {
                            "is_field_target": is_field_target,
                            "excluded_from_ensemble_reference": is_field_target,
                            "exclusion_reason": "target_object" if is_field_target else None,
                        },
                        "photometric_consistency": {
                            "ref_mag": ref_mag,
                            "mag_ref_delta": mag_ref_delta,
                            "abs_mag_ref_delta": abs_mag_ref_delta,
                        },
                    },
                    existing_qc,
                    source_aperture_qc=source_aperture_qc,
                    source_aperture_calibration_name=source_aperture_calibration_name,
                ),
            }
        )

    if dry_run or not rows:
        return result

    result.insert_batches = _upsert_object_qc_rows(session, rows, insert_batch_size=insert_batch_size)
    session.commit()
    result.written = len(rows)
    return result


def _upsert_object_qc_rows(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    table = ObjectQc.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        stmt = insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_object_qc_identity",
            set_={
                "nearest_neighbor_arcsec": stmt.excluded.nearest_neighbor_arcsec,
                "neighbors_within_3arcsec": stmt.excluded.neighbors_within_3arcsec,
                "neighbors_within_5arcsec": stmt.excluded.neighbors_within_5arcsec,
                "neighbors_within_10arcsec": stmt.excluded.neighbors_within_10arcsec,
                "median_x": stmt.excluded.median_x,
                "median_y": stmt.excluded.median_y,
                "edge_distance_pix_min": stmt.excluded.edge_distance_pix_min,
                "n_measurements": stmt.excluded.n_measurements,
                "mag_median": stmt.excluded.mag_median,
                "mag_rms": stmt.excluded.mag_rms,
                "mag_mad": stmt.excluded.mag_mad,
                "mag_err_median": stmt.excluded.mag_err_median,
                "flags": stmt.excluded.flags,
                "details": stmt.excluded.details,
            },
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches


def load_object_qc_map(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    algorithm: str = "object_qc_v1",
) -> dict[int, ObjectQc]:
    stmt = select(ObjectQc)
    if calibration_version_id is not None:
        stmt = stmt.where(ObjectQc.calibration_version_id == int(calibration_version_id))
    elif calibration_name:
        stmt = stmt.join(CalibrationVersion, CalibrationVersion.calibration_version_id == ObjectQc.calibration_version_id)
        stmt = stmt.where(CalibrationVersion.name == calibration_name)
    if catalog_name:
        stmt = stmt.where(ObjectQc.catalog_name == catalog_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(ObjectQc.instrument_unit_id == instrument_unit_id)
    if algorithm:
        stmt = stmt.where(ObjectQc.algorithm == algorithm)
    return {qc.object_id: qc for qc in session.scalars(stmt).all()}


def transfer_aperture_stability_qc(
    session: Session,
    *,
    source_calibration_name: str | None = None,
    target_calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    algorithm: str = "object_qc_v1",
    create_missing: bool = False,
    dry_run: bool = False,
) -> ApertureStabilityTransferResult:
    """Copy aperture-stability diagnostics from source object_qc into a target calibration.

    This is a maintenance command for derived calibrations that were produced
    before source-calibration provenance was explicit or before compute-object-qc
    learned to inherit aperture-stability diagnostics.  By default it only updates
    existing target object_qc rows so geometry/scatter statistics remain those of
    the target calibration.  With create_missing=True it creates minimal target
    object_qc rows containing only the inherited aperture-stability layer.
    """

    target_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == target_calibration_name))
    if target_cv is None:
        raise ValueError(f"Target calibration version not found: {target_calibration_name}")

    source_cv = None
    if source_calibration_name:
        source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    else:
        source_cv = resolve_source_calibration(session, target_cv)
    if source_cv is None:
        raise ValueError("Source calibration version not found; pass --source-calibration-name or set target provenance")
    if source_cv.calibration_version_id == target_cv.calibration_version_id:
        raise ValueError("Source and target calibrations must differ")

    result = ApertureStabilityTransferResult(
        source_calibration_version_id=source_cv.calibration_version_id,
        source_calibration_name=source_cv.name,
        target_calibration_version_id=target_cv.calibration_version_id,
        target_calibration_name=target_cv.name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
        dry_run=bool(dry_run),
    )

    source_map = load_object_qc_map(
        session,
        calibration_version_id=source_cv.calibration_version_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
    )
    target_map = load_object_qc_map(
        session,
        calibration_version_id=target_cv.calibration_version_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
    )
    result.source_rows = len(source_map)
    result.target_rows = len(target_map)

    for object_id, source_qc in source_map.items():
        if not _has_aperture_stability(source_qc):
            result.skipped_no_aperture_stability += 1
            continue
        target_qc = target_map.get(object_id)
        if target_qc is None and not create_missing:
            result.skipped_missing_target_qc += 1
            continue

        if dry_run:
            if target_qc is None:
                result.created += 1
            else:
                result.updated += 1
            continue

        if target_qc is None:
            target_qc = ObjectQc(
                object_id=source_qc.object_id,
                calibration_version_id=target_cv.calibration_version_id,
                instrument_unit_id=instrument_unit_id if instrument_unit_id is not None else source_qc.instrument_unit_id,
                catalog_name=catalog_name if catalog_name is not None else source_qc.catalog_name,
                algorithm=algorithm,
                n_measurements=0,
                flags={},
                details={},
            )
            session.add(target_qc)
            result.created += 1
        else:
            result.updated += 1

        flags = dict(target_qc.flags or {})
        _preserve_aperture_stability_flags(source_qc, flags)
        target_qc.flags = flags
        target_qc.details = _merge_preserved_qc_details(
            dict(target_qc.details or {}),
            existing_qc=None,
            source_aperture_qc=source_qc,
            source_aperture_calibration_name=source_cv.name,
        )

    if not dry_run:
        session.commit()
    return result


def filter_object_rows_by_qc(
    object_rows: list[ObjectScatterRow],
    qc_map: dict[int, ObjectQc],
    *,
    exclude_flagged: bool = False,
    exclude_crowded: bool = False,
    exclude_edge: bool = False,
    exclude_saturated: bool = False,
    min_nearest_neighbor_arcsec: float | None = None,
    max_mag_median: float | None = None,
    require_detection_support: bool = False,
    max_ref_mag_delta: float | None = None,
    exclude_catalog_outliers: bool = False,
    exclude_aperture_unstable: bool = False,
) -> list[ObjectScatterRow]:
    out: list[ObjectScatterRow] = []
    for row in object_rows:
        if max_mag_median is not None and row.mag_median is not None and float(row.mag_median) > float(max_mag_median):
            continue
        qc = qc_map.get(row.object_id)
        flags = (qc.flags or {}) if qc is not None else {}
        if exclude_flagged and any(bool(v) for v in flags.values()):
            continue
        if exclude_crowded and (flags.get("CROWDED") or flags.get("BLENDED_NEIGHBOR") or flags.get("NEIGHBOR_WITHIN_3ARCSEC") or flags.get("DYNAMIC_CROWDED") or flags.get("DYNAMIC_BLEND_RISK") or flags.get("POSSIBLE_BLEND")):
            continue
        if exclude_edge and flags.get("EDGE"):
            continue
        if exclude_saturated and flags.get("SATURATED_LIKE"):
            continue
        if min_nearest_neighbor_arcsec is not None:
            nn = qc.nearest_neighbor_arcsec if qc is not None else None
            if nn is None or float(nn) < float(min_nearest_neighbor_arcsec):
                continue
        details = (qc.details or {}) if qc is not None else {}
        det = details.get("detection_support", {}) if isinstance(details, dict) else {}
        phot = details.get("photometric_consistency", {}) if isinstance(details, dict) else {}
        if require_detection_support:
            if qc is None or flags.get("NO_DETECTION_SUPPORT") or flags.get("WEAK_DETECTION_SUPPORT"):
                continue
        if exclude_catalog_outliers and flags.get("LARGE_CATALOG_MAG_RESIDUAL"):
            continue
        if exclude_aperture_unstable and (flags.get("APERTURE_UNSTABLE") or flags.get("APERTURE_MEDIAN_SHIFT")):
            continue
        if max_ref_mag_delta is not None:
            abs_delta = phot.get("abs_mag_ref_delta")
            if abs_delta is None or not _is_finite(abs_delta) or float(abs_delta) > float(max_ref_mag_delta):
                continue
        out.append(row)
    return out


def filter_candidate_dicts_by_qc(
    rows: list[dict], qc_map: dict[int, ObjectQc], **kwargs) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        object_id = row.get("object_id")
        qc = qc_map.get(object_id)
        flags = (qc.flags or {}) if qc is not None else {}
        if kwargs.get("exclude_flagged") and any(bool(v) for v in flags.values()):
            continue
        if kwargs.get("exclude_crowded") and (flags.get("CROWDED") or flags.get("BLENDED_NEIGHBOR") or flags.get("NEIGHBOR_WITHIN_3ARCSEC") or flags.get("DYNAMIC_CROWDED") or flags.get("DYNAMIC_BLEND_RISK") or flags.get("POSSIBLE_BLEND")):
            continue
        if kwargs.get("exclude_edge") and flags.get("EDGE"):
            continue
        if kwargs.get("exclude_saturated") and flags.get("SATURATED_LIKE"):
            continue
        min_nn = kwargs.get("min_nearest_neighbor_arcsec")
        if min_nn is not None:
            nn = qc.nearest_neighbor_arcsec if qc is not None else None
            if nn is None or float(nn) < float(min_nn):
                continue
        max_mag = kwargs.get("max_mag_median")
        if max_mag is not None and row.get("mag_median") is not None and float(row["mag_median"]) > float(max_mag):
            continue
        details = (qc.details or {}) if qc is not None else {}
        det = details.get("detection_support", {}) if isinstance(details, dict) else {}
        phot = details.get("photometric_consistency", {}) if isinstance(details, dict) else {}
        if kwargs.get("require_detection_support"):
            if qc is None or flags.get("NO_DETECTION_SUPPORT") or flags.get("WEAK_DETECTION_SUPPORT"):
                continue
        if kwargs.get("exclude_catalog_outliers") and flags.get("LARGE_CATALOG_MAG_RESIDUAL"):
            continue
        if kwargs.get("exclude_aperture_unstable") and (flags.get("APERTURE_UNSTABLE") or flags.get("APERTURE_MEDIAN_SHIFT")):
            continue
        max_ref_delta = kwargs.get("max_ref_mag_delta")
        if max_ref_delta is not None:
            abs_delta = phot.get("abs_mag_ref_delta")
            if abs_delta is None or not _is_finite(abs_delta) or float(abs_delta) > float(max_ref_delta):
                continue
        row = dict(row)
        row["object_qc_flags"] = ",".join(sorted(k for k, v in flags.items() if v))
        row["nearest_neighbor_arcsec"] = qc.nearest_neighbor_arcsec if qc is not None else None
        row["edge_distance_pix_min"] = qc.edge_distance_pix_min if qc is not None else None
        row["detection_fraction"] = det.get("detection_fraction")
        row["detection_frame_fraction"] = det.get("detection_frame_fraction", det.get("detection_fraction"))
        row["n_detection_matches"] = det.get("n_detection_matches")
        row["n_detection_match_frames"] = det.get("n_detection_match_frames", det.get("n_detection_matches"))
        row["n_detection_matches_raw"] = det.get("n_detection_matches_raw", det.get("n_detection_matches"))
        row["n_extra_detection_matches"] = det.get("n_extra_detection_matches")
        row["n_multiple_detection_frames"] = det.get("n_multiple_detection_frames")
        row["multiple_detection_frame_ratio"] = det.get("multiple_detection_frame_ratio")
        row["det_sep_median_arcsec"] = det.get("det_sep_median_arcsec")
        row["mag_ref_delta"] = phot.get("mag_ref_delta")
        row["abs_mag_ref_delta"] = phot.get("abs_mag_ref_delta")
        out.append(row)
    return out




def _infer_source_aperture_calibration_name(cv: CalibrationVersion, calibration_name: str) -> str | None:
    """Return the source calibration name used by a derived calibration.

    New databases store this relation as calibration_versions.source_calibration_version_id.
    Older databases may only have parameters.source_calibration_name, so this helper
    remains intentionally backward-compatible.
    """

    source_name = get_source_calibration_name(cv)
    if not source_name or source_name == calibration_name:
        return None
    return source_name


def _has_aperture_stability(existing_qc: ObjectQc | None) -> bool:
    if existing_qc is None:
        return False
    existing_details = existing_qc.details or {}
    return isinstance(existing_details, dict) and "aperture_stability" in existing_details


def _select_aperture_stability_qc(existing_qc: ObjectQc | None, source_aperture_qc: ObjectQc | None) -> ObjectQc | None:
    """Prefer target-calibration aperture diagnostics, otherwise inherit source ones."""

    if _has_aperture_stability(existing_qc):
        return existing_qc
    if _has_aperture_stability(source_aperture_qc):
        return source_aperture_qc
    return None


def _preserve_aperture_stability_flags(existing_qc: ObjectQc | None, flags: dict[str, bool]) -> bool:
    """Preserve aperture-stability flags written by compute-aperture-stability-qc.

    compute-object-qc recomputes geometry/detection/photometric flags from scratch.
    Aperture-stability is a separate, more expensive diagnostic layer and should
    survive a later object-QC recomputation.  For derived calibrations, the
    source calibration can provide this layer when the target has not been
    diagnosed separately.
    """

    if not _has_aperture_stability(existing_qc):
        return False
    existing_flags = existing_qc.flags or {}
    for name in ("APERTURE_UNSTABLE", "APERTURE_MEDIAN_SHIFT"):
        if bool(existing_flags.get(name)):
            flags[name] = True
    if bool(existing_flags.get("POSSIBLE_BLEND")):
        flags["POSSIBLE_BLEND"] = True
    return True


def _merge_preserved_qc_details(
    details: dict,
    existing_qc: ObjectQc | None,
    *,
    source_aperture_qc: ObjectQc | None = None,
    source_aperture_calibration_name: str | None = None,
) -> dict:
    """Merge non-primary QC layers that should not be overwritten."""

    preserved_qc = _select_aperture_stability_qc(existing_qc, source_aperture_qc)
    if preserved_qc is None:
        return details
    existing_details = preserved_qc.details or {}
    if not isinstance(existing_details, dict):
        return details
    if "aperture_stability" in existing_details and "aperture_stability" not in details:
        details["aperture_stability"] = existing_details["aperture_stability"]
        details["aperture_stability_provenance"] = {
            "preserved_from_calibration_version_id": preserved_qc.calibration_version_id,
            "preserved_from_source_calibration": bool(source_aperture_qc is not None and preserved_qc is source_aperture_qc),
            "source_calibration_name": source_aperture_calibration_name,
        }
    return details


def _load_detection_support_stats(
    session: Session,
    *,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None = None,
) -> dict[int, dict]:
    """Return detection-match support metrics keyed by catalog_source_id.

    The primary support metric is the number of unique frames with at least one
    matched detection. Raw match counts are retained separately because they are
    useful blend diagnostics, but they must not be used as a fraction denominator:
    a crowded source can have several detection matches in the same frame.
    """

    stmt = (
        select(DetectionCatalogMatch, SourceDetection, Frame)
        .join(SourceDetection, SourceDetection.source_detection_id == DetectionCatalogMatch.source_detection_id)
        .join(Frame, Frame.frame_id == DetectionCatalogMatch.frame_id)
        .where(DetectionCatalogMatch.match_status == "MATCHED")
    )
    if catalog_name:
        stmt = stmt.where(DetectionCatalogMatch.catalog_name == catalog_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))

    grouped: dict[int, list[tuple[DetectionCatalogMatch, SourceDetection, int]]] = defaultdict(list)
    for match, det, frame in session.execute(stmt).all():
        grouped[int(match.catalog_source_id)].append((match, det, int(frame.frame_id)))

    out: dict[int, dict] = {}
    for catalog_source_id, values in grouped.items():
        by_frame: dict[int, list[tuple[DetectionCatalogMatch, SourceDetection]]] = defaultdict(list)
        for match, det, frame_id in values:
            by_frame[frame_id].append((match, det))

        best_per_frame: list[tuple[DetectionCatalogMatch, SourceDetection]] = []
        n_multiple_detection_frames = 0
        for frame_values in by_frame.values():
            if len(frame_values) > 1:
                n_multiple_detection_frames += 1
            best_per_frame.append(min(frame_values, key=lambda item: _sep_sort_value(item[0].sep_arcsec)))

        seps = [
            float(m.sep_arcsec)
            for m, _d in best_per_frame
            if m.sep_arcsec is not None and _is_finite(m.sep_arcsec)
        ]
        snrs = [float(d.snr) for _m, d in best_per_frame if d.snr is not None and _is_finite(d.snr)]
        raw_snrs = [float(d.snr) for _m, d, _frame_id in values if d.snr is not None and _is_finite(d.snr)]
        n_raw = len(values)
        n_frames = len(by_frame)
        out[catalog_source_id] = {
            # Backward-compatible aliases now use unique-frame support.
            "n_detection_matches": n_frames,
            "n_detection_match_frames": n_frames,
            "n_detection_matches_raw": n_raw,
            "n_extra_detection_matches": max(0, n_raw - n_frames),
            "n_multiple_detection_frames": n_multiple_detection_frames,
            "det_sep_median_arcsec": float(median(seps)) if seps else None,
            "det_snr_median": float(median(snrs)) if snrs else None,
            "det_snr_max": max(raw_snrs) if raw_snrs else None,
        }
    return out


def _sep_sort_value(value) -> float:
    if value is None or not _is_finite(value):
        return float("inf")
    return abs(float(value))


def _load_catalog_sources_by_source_id(session: Session, *, catalog_name: str | None) -> dict[str, CatalogSource]:
    stmt = select(CatalogSource)
    if catalog_name:
        stmt = stmt.where(CatalogSource.catalog_name == catalog_name)
    out = {}
    for source in session.scalars(stmt).all():
        out[str(source.source_id)] = source
    return out


def _build_neighbor_stats(sources_by_id: dict[str, CatalogSource]) -> dict[str, dict]:
    sources = list(sources_by_id.values())
    out: dict[str, dict] = {}
    for a in sources:
        seps = []
        for b in sources:
            if a.catalog_source_id == b.catalog_source_id:
                continue
            sep = _angular_sep_arcsec(a.ra_deg, a.dec_deg, b.ra_deg, b.dec_deg)
            if _is_finite(sep):
                seps.append(float(sep))
        if seps:
            nn = min(seps)
            out[str(a.source_id)] = {
                "nearest_neighbor_arcsec": nn,
                "neighbors_within_3arcsec": sum(1 for x in seps if x <= 3.0),
                "neighbors_within_5arcsec": sum(1 for x in seps if x <= 5.0),
                "neighbors_within_10arcsec": sum(1 for x in seps if x <= 10.0),
            }
        else:
            out[str(a.source_id)] = {
                "nearest_neighbor_arcsec": None,
                "neighbors_within_3arcsec": 0,
                "neighbors_within_5arcsec": 0,
                "neighbors_within_10arcsec": 0,
            }
    return out


def _load_measurement_geometry(
    session: Session,
    *,
    calibration_version_id: int,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    frame_ids: list[int] | None = None,
    fwhm_header_unit: str = "arcsec",
) -> dict[int, dict]:
    stmt = (
        select(Measurement, Frame, InstrumentUnit)
        .join(Frame, Frame.frame_id == Measurement.frame_id)
        .outerjoin(InstrumentUnit, InstrumentUnit.instrument_unit_id == Frame.instrument_unit_id)
        .where(Measurement.calibration_version_id == calibration_version_id)
    )
    if instrument_unit_id is not None:
        stmt = stmt.where(Frame.instrument_unit_id == instrument_unit_id)
    if frame_ids:
        stmt = stmt.where(Frame.frame_id.in_(frame_ids))
    grouped: dict[int, list[tuple[Measurement, Frame, InstrumentUnit | None]]] = defaultdict(list)
    for meas, frame, iu in session.execute(stmt).all():
        flags = meas.flags or {}
        if catalog_name and flags.get("catalog_name") != catalog_name:
            continue
        grouped[meas.object_id].append((meas, frame, iu))

    out = {}
    for object_id, values in grouped.items():
        xs = []
        ys = []
        edges = []
        saturated = False
        aperture_radii_pix = []
        aperture_radii_arcsec = []
        pixel_scales = []
        fwhm_arcsec_values = []
        for meas, frame, iu in values:
            if meas.x is not None and _is_finite(meas.x):
                xs.append(float(meas.x))
            if meas.y is not None and _is_finite(meas.y):
                ys.append(float(meas.y))
            pixel_scale = _extract_pixel_scale_arcsec(frame, iu)
            if pixel_scale is not None:
                pixel_scales.append(pixel_scale)
            if meas.aperture_radius_pix is not None and _is_finite(meas.aperture_radius_pix):
                aperture_radii_pix.append(float(meas.aperture_radius_pix))
                if pixel_scale is not None:
                    aperture_radii_arcsec.append(float(meas.aperture_radius_pix) * pixel_scale)
            fwhm_arcsec = _extract_frame_fwhm_arcsec(frame, iu, fwhm_header_unit=fwhm_header_unit)
            if fwhm_arcsec is not None:
                fwhm_arcsec_values.append(fwhm_arcsec)
            flags = meas.flags or {}
            if flags.get("saturated_or_near_saturated"):
                saturated = True
            nx = _to_float((frame.header_json or {}).get("NAXIS1"))
            ny = _to_float((frame.header_json or {}).get("NAXIS2"))
            if meas.x is not None and meas.y is not None and nx is not None and ny is not None:
                x = float(meas.x)
                y = float(meas.y)
                edges.append(min(x, y, nx - 1.0 - x, ny - 1.0 - y))
        out[object_id] = {
            "median_x": float(median(xs)) if xs else None,
            "median_y": float(median(ys)) if ys else None,
            "edge_distance_pix_min": min(edges) if edges else None,
            "saturated_like": saturated,
            "aperture_radius_pix_median": float(median(aperture_radii_pix)) if aperture_radii_pix else None,
            "aperture_radius_arcsec_median": float(median(aperture_radii_arcsec)) if aperture_radii_arcsec else None,
            "pixel_scale_arcsec_median": float(median(pixel_scales)) if pixel_scales else None,
            "fwhm_arcsec_median": float(median(fwhm_arcsec_values)) if fwhm_arcsec_values else None,
        }
    return out



def _compute_dynamic_separation_thresholds(
    geom: dict,
    *,
    seeing_factor: float,
    aperture_factor: float,
    crowding_factor: float,
    min_blend_arcsec: float | None,
) -> dict:
    """Compute frame-aware separation thresholds in arcsec.

    The blend threshold is the maximum of a seeing term and an aperture term:

        blend = max(seeing_factor * FWHM, aperture_factor * r_ap * pixel_scale, min_blend)
        crowded = crowding_factor * blend

    This is deliberately conservative for undersampled/seeing-limited archive data.
    """

    components = {}
    fwhm = geom.get("fwhm_arcsec_median")
    ap = geom.get("aperture_radius_arcsec_median")
    if fwhm is not None and _is_finite(fwhm):
        components["seeing_term_arcsec"] = float(seeing_factor) * float(fwhm)
    if ap is not None and _is_finite(ap):
        components["aperture_term_arcsec"] = float(aperture_factor) * float(ap)
    if min_blend_arcsec is not None:
        components["minimum_term_arcsec"] = float(min_blend_arcsec)
    values = [v for v in components.values() if v is not None and _is_finite(v) and float(v) > 0]
    if not values:
        return {
            "enabled": True,
            "available": False,
            "reason": "no_fwhm_or_aperture_scale",
            "fwhm_arcsec_median": fwhm,
            "aperture_radius_pix_median": geom.get("aperture_radius_pix_median"),
            "aperture_radius_arcsec_median": ap,
            "pixel_scale_arcsec_median": geom.get("pixel_scale_arcsec_median"),
        }
    blend = max(values)
    crowded = float(crowding_factor) * blend
    return {
        "enabled": True,
        "available": True,
        "formula": "blend=max(seeing_factor*FWHM, aperture_factor*r_ap*pixel_scale, min_blend); crowded=crowding_factor*blend",
        "seeing_factor": seeing_factor,
        "aperture_factor": aperture_factor,
        "crowding_factor": crowding_factor,
        "components": components,
        "dynamic_blend_arcsec": blend,
        "dynamic_crowded_arcsec": crowded,
        "fwhm_arcsec_median": fwhm,
        "aperture_radius_pix_median": geom.get("aperture_radius_pix_median"),
        "aperture_radius_arcsec_median": ap,
        "pixel_scale_arcsec_median": geom.get("pixel_scale_arcsec_median"),
    }


def _extract_pixel_scale_arcsec(frame: Frame, iu: InstrumentUnit | None) -> float | None:
    if iu is not None and getattr(iu, "pixel_scale_arcsec", None) is not None:
        try:
            value = float(iu.pixel_scale_arcsec)
            if math.isfinite(value) and value > 0:
                return value
        except Exception:
            pass
    q = frame.quality_flags or {}
    candidates = [
        q.get("pixel_scale_arcsec_mean"),
        (q.get("wcs_check") or {}).get("pixel_scale_arcsec_mean") if isinstance(q.get("wcs_check"), dict) else None,
        ((q.get("wcs_check") or {}).get("details") or {}).get("pixel_scale_arcsec_mean") if isinstance(q.get("wcs_check"), dict) else None,
    ]
    for value in candidates:
        try:
            out = float(value)
            if math.isfinite(out) and out > 0:
                return out
        except Exception:
            continue
    return None


def _extract_frame_fwhm_arcsec(frame: Frame, iu: InstrumentUnit | None, *, fwhm_header_unit: str = "arcsec") -> float | None:
    header = frame.header_json or {}
    keys = (
        "SEXFWHM",
        "SEX_FWHM",
        "FWHM",
        "FWHMARC",
        "FWHM_ARCSEC",
        "SEEING",
        "SEEING1",
        "PSF_FWHM",
    )
    value = None
    for key in keys:
        if key in header:
            value = _to_float(header.get(key))
            if value is not None:
                break
    if value is None:
        return None
    unit = (fwhm_header_unit or "arcsec").lower()
    if unit == "arcsec":
        return value if value > 0 else None
    pixel_scale = _extract_pixel_scale_arcsec(frame, iu)
    if unit == "pix":
        return value * pixel_scale if pixel_scale is not None and value > 0 else None
    if unit == "auto":
        # Conservative heuristic: if a pixel scale is available and the value is a
        # small PSF-like number, treat it as pixels; otherwise keep it as arcsec.
        if pixel_scale is not None and 1.0 <= value <= 8.0:
            pix_value = value * pixel_scale
            if 0.3 <= pix_value <= 20.0:
                return pix_value
        return value if value > 0 else None
    return value if value > 0 else None

def _angular_sep_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    ra1 = math.radians(float(ra1_deg))
    dec1 = math.radians(float(dec1_deg))
    ra2 = math.radians(float(ra2_deg))
    dec2 = math.radians(float(dec2_deg))
    sdec = math.sin((dec2 - dec1) / 2.0)
    sra = math.sin((ra2 - ra1) / 2.0)
    a = sdec * sdec + math.cos(dec1) * math.cos(dec2) * sra * sra
    a = min(1.0, max(0.0, a))
    return math.degrees(2.0 * math.asin(math.sqrt(a))) * 3600.0


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _to_float(value) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None
