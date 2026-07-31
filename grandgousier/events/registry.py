from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session


EVENT_COLUMNS = [
    "event_id",
    "event_uid",
    "event_type",
    "event_subtype",
    "source_catalog",
    "source_event_id",
    "external_catalog",
    "external_id",
    "target_name",
    "ra_deg",
    "dec_deg",
    "position_error_arcsec",
    "object_id",
    "t_start_bjd_tdb",
    "t_peak_bjd_tdb",
    "t_end_bjd_tdb",
    "t0_bjd_tdb",
    "period_days",
    "duration_hours",
    "expected_sign",
    "expected_depth_ppt",
    "expected_amplitude_mag",
    "priority",
    "status",
]

EVENT_SHOW_COLUMNS = EVENT_COLUMNS + ["n_object_links"]
EVENT_LINK_COLUMNS = [
    "event_id",
    "event_uid",
    "event_type",
    "target_name",
    "object_id",
    "match_type",
    "separation_arcsec",
    "confidence",
]
EVENT_COVERAGE_COLUMNS = [
    "event_id",
    "event_uid",
    "event_type",
    "event_subtype",
    "target_name",
    "object_id",
    "source_id",
    "ra_deg",
    "dec_deg",
    "calibration_name",
    "instrument_unit_id",
    "n_points",
    "n_before",
    "n_in_window",
    "n_after",
    "n_outside",
    "n_pre_baseline",
    "n_event",
    "n_post_baseline",
    "event_window_source",
    "baseline_status",
    "event_observability_status",
    "event_observability_reasons",
    "window_start_bjd_tdb",
    "window_end_bjd_tdb",
    "window_duration_hours",
    "pre_baseline_start_bjd_tdb",
    "post_baseline_end_bjd_tdb",
    "pre_margin_hours",
    "post_margin_hours",
    "mjd_min",
    "mjd_max",
    "mag_median",
    "mag_rms",
    "mag_mad",
    "median_mag_err",
    "expected_sign",
    "expected_depth_ppt",
    "expected_amplitude_mag",
    "depth_per_point_snr_est",
    "depth_total_snr_est",
    "object_qc_flags",
    "coverage_status",
]

EVENT_FRAME_COVERAGE_COLUMNS = [
    "event_frame_coverage_id",
    "event_id",
    "event_uid",
    "event_type",
    "event_subtype",
    "target_name",
    "frame_id",
    "object_id",
    "source_id",
    "calibration_name",
    "instrument_unit_id",
    "time_relation",
    "frame_time_jd_approx",
    "mjd",
    "date_obs",
    "measurement_id",
    "mag_cal",
    "mag_err",
    "event_window_source",
    "baseline_status",
    "event_observability_status",
    "event_observability_reasons",
    "raw_path",
]

EVENT_DETECTION_LINK_COLUMNS = [
    "event_detection_link_id",
    "event_id",
    "event_uid",
    "event_type",
    "event_subtype",
    "target_name",
    "frame_id",
    "source_detection_id",
    "object_id",
    "instrument_unit_id",
    "time_relation",
    "frame_time_jd_approx",
    "mjd",
    "date_obs",
    "separation_arcsec",
    "detection_snr",
    "detection_flux",
    "peak_adu",
    "catalog_matched",
    "nearest_catalog_name",
    "nearest_catalog_source_id",
    "nearest_catalog_sep_arcsec",
    "confidence",
    "raw_path",
]

# QC flags that usually make an event/object unsuitable for science-grade
# interpretation even when the time coverage is geometrically good.
DEFAULT_BAD_EVENT_OBJECT_QC_FLAGS = {
    "EDGE",
    "SATURATED_LIKE",
    "CROWDED",
    "BLENDED_NEIGHBOR",
    "DYNAMIC_CROWDED",
    "DYNAMIC_BLEND_RISK",
    "POSSIBLE_BLEND",
    "APERTURE_UNSTABLE",
    "APERTURE_MEDIAN_SHIFT",
    "NO_DETECTION_SUPPORT",
    "WEAK_DETECTION_SUPPORT",
    "LARGE_CATALOG_MAG_RESIDUAL",
    "POSSIBLE_PROPER_MOTION_MISMATCH",
}


@dataclass
class ImportEventsResult:
    read_rows: int = 0
    valid_rows: int = 0
    inserted_or_updated: int = 0
    skipped: int = 0
    dry_run: bool = False


@dataclass
class MatchEventsResult:
    selected_events: int = 0
    matched_links: int = 0
    unmatched_events: int = 0
    inserted_or_updated: int = 0
    dry_run: bool = False


@dataclass
class EventCoverageResult:
    rows: list[dict[str, Any]]
    summary: dict[str, Any]


@dataclass
class MatchEventDetectionsResult:
    selected_events: int = 0
    candidate_detections: int = 0
    linked_detections: int = 0
    catalog_matched_detections: int = 0
    catalog_unmatched_detections: int = 0
    inserted_or_updated: int = 0
    skipped_no_position: int = 0
    skipped_no_time_window: int = 0
    dry_run: bool = False


def import_events_csv(
    session: Session,
    *,
    path: str | Path,
    default_event_type: str | None = None,
    default_source_catalog: str | None = None,
    dry_run: bool = False,
) -> ImportEventsResult:
    """Import a generic event registry CSV.

    The CSV may contain any subset of the fields documented in
    README_MVP5_16_EVENT_REGISTRY_PATCH.md. Unknown columns are preserved in raw_json.
    """

    result = ImportEventsResult(dry_run=dry_run)
    path = Path(path)
    rows_to_upsert: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            result.read_rows += 1
            row = _normalize_event_row(raw, default_event_type=default_event_type, default_source_catalog=default_source_catalog)
            if not row.get("event_type"):
                result.skipped += 1
                continue
            row["event_uid"] = row.get("event_uid") or _make_event_uid(row)
            row["raw_json"] = dict(raw)
            result.valid_rows += 1
            rows_to_upsert.append(row)

    if dry_run:
        result.inserted_or_updated = len(rows_to_upsert)
        return result

    for row in rows_to_upsert:
        _upsert_event(session, row)
        result.inserted_or_updated += 1
    session.commit()
    return result


def list_events(
    session: Session,
    *,
    event_type: str | None = None,
    source_catalog: str | None = None,
    status: str | None = None,
    target: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    wheres = ["1=1"]
    params: dict[str, Any] = {}
    if event_type:
        wheres.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if source_catalog:
        wheres.append("e.source_catalog = :source_catalog")
        params["source_catalog"] = source_catalog
    if status:
        wheres.append("e.status = :status")
        params["status"] = status
    if target:
        wheres.append("lower(coalesce(e.target_name,'')) LIKE lower(:target)")
        params["target"] = f"%{target}%"
    sql = f"""
        SELECT e.*, count(l.event_object_link_id) AS n_object_links
        FROM event_registry e
        LEFT JOIN event_object_links l ON l.event_id = e.event_id
        WHERE {' AND '.join(wheres)}
        GROUP BY e.event_id
        ORDER BY coalesce(e.t_peak_bjd_tdb, e.t0_bjd_tdb, e.t_start_bjd_tdb), e.event_id
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
    return [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]


def match_events_to_objects(
    session: Session,
    *,
    event_type: str | None = None,
    event_uid: str | None = None,
    source_catalog: str | None = None,
    external_catalog: str | None = None,
    external_id_column_preference: str | None = None,
    coordinate_radius_arcsec: float = 5.0,
    dry_run: bool = False,
) -> MatchEventsResult:
    """Create event-object links by explicit object_id, external_id, or coordinates."""

    result = MatchEventsResult(dry_run=dry_run)
    events = list_events(session, event_type=event_type, source_catalog=source_catalog, limit=None)
    if event_uid:
        events = [e for e in events if e.get("event_uid") == event_uid]
    result.selected_events = len(events)

    for event in events:
        matches = _match_one_event_to_objects(
            session,
            event,
            external_catalog=external_catalog,
            external_id_column_preference=external_id_column_preference,
            coordinate_radius_arcsec=coordinate_radius_arcsec,
        )
        if not matches:
            result.unmatched_events += 1
            continue
        for m in matches:
            result.matched_links += 1
            if dry_run:
                continue
            _upsert_event_object_link(session, event, m)
            result.inserted_or_updated += 1
    if not dry_run:
        session.commit()
    return result


def list_event_object_links(
    session: Session,
    *,
    event_type: str | None = None,
    event_uid: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    wheres = ["1=1"]
    params: dict[str, Any] = {}
    if event_type:
        wheres.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if event_uid:
        wheres.append("e.event_uid = :event_uid")
        params["event_uid"] = event_uid
    sql = f"""
        SELECT e.event_id, e.event_uid, e.event_type, e.target_name,
               l.object_id, l.match_type, l.separation_arcsec, l.confidence
        FROM event_object_links l
        JOIN event_registry e ON e.event_id = l.event_id
        WHERE {' AND '.join(wheres)}
        ORDER BY e.event_id, l.confidence DESC NULLS LAST, l.separation_arcsec NULLS LAST
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
    return [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]


def build_event_coverage_summary(
    session: Session,
    *,
    calibration_name: str,
    event_type: str | None = None,
    event_uid: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    window_padding_factor: float = 1.0,
    pre_margin_hours: float | None = None,
    post_margin_hours: float | None = None,
    event_window_source: str = "auto",
    min_depth_total_snr: float = 3.0,
    min_event_points: int = 1,
    min_pre_baseline_points: int = 1,
    min_post_baseline_points: int = 1,
    bad_object_qc_flags: set[str] | None = None,
    ignore_object_qc: bool = False,
    write_frame_coverage: bool = False,
    limit: int | None = None,
) -> EventCoverageResult:
    """Summarize whether the calibrated light-curve database covers registered events.

    This is intentionally an infrastructure coverage layer, not a transit/nova model fit.
    Frame times are compared to event BJD_TDB windows using frame.mjd + 2400000.5 as
    an approximate JD-like time. A future layer can replace this with cached BJD_TDB.

    The baseline window can be expressed either as a multiple of the event duration
    (`window_padding_factor`) or as explicit pre/post margins in hours. The explicit
    margins are preferable for scheduled follow-up observations where the baseline
    requirement is instrument- or science-case dependent.
    """

    effective_bad_qc_flags = set(bad_object_qc_flags or DEFAULT_BAD_EVENT_OBJECT_QC_FLAGS)

    events = _load_events_for_coverage(session, event_type=event_type, event_uid=event_uid, limit=limit)
    rows: list[dict[str, Any]] = []
    for event in events:
        linked_objects = _load_event_linked_objects(session, event["event_id"])
        if event.get("object_id") and event["object_id"] not in linked_objects:
            linked_objects.append(int(event["object_id"]))
        if not linked_objects:
            rows.append(
                _coverage_no_match_row(
                    event,
                    calibration_name,
                    instrument_unit_id,
                    pre_margin_hours=pre_margin_hours,
                    post_margin_hours=post_margin_hours,
                    event_window_source=event_window_source,
                    window_padding_factor=window_padding_factor,
                    min_depth_total_snr=min_depth_total_snr,
                    min_event_points=min_event_points,
                    min_pre_baseline_points=min_pre_baseline_points,
                    min_post_baseline_points=min_post_baseline_points,
                )
            )
            continue
        for object_id in linked_objects:
            row, frame_rows = _coverage_for_object(
                session,
                event=event,
                object_id=object_id,
                calibration_name=calibration_name,
                catalog_name=catalog_name,
                instrument_unit_id=instrument_unit_id,
                window_padding_factor=window_padding_factor,
                pre_margin_hours=pre_margin_hours,
                post_margin_hours=post_margin_hours,
                event_window_source=event_window_source,
                min_depth_total_snr=min_depth_total_snr,
                min_event_points=min_event_points,
                min_pre_baseline_points=min_pre_baseline_points,
                min_post_baseline_points=min_post_baseline_points,
                bad_object_qc_flags=effective_bad_qc_flags,
                ignore_object_qc=ignore_object_qc,
            )
            rows.append(row)
            if write_frame_coverage:
                _write_frame_coverage(session, event=event, row=row, frame_rows=frame_rows)
    if write_frame_coverage:
        session.commit()

    summary = {
        "calibration_name": calibration_name,
        "event_type": event_type,
        "event_uid": event_uid,
        "catalog_name": catalog_name,
        "instrument_unit_id": instrument_unit_id,
        "window_padding_factor": window_padding_factor,
        "pre_margin_hours": pre_margin_hours,
        "post_margin_hours": post_margin_hours,
        "event_window_source": event_window_source,
        "min_depth_total_snr": min_depth_total_snr,
        "min_event_points": min_event_points,
        "min_pre_baseline_points": min_pre_baseline_points,
        "min_post_baseline_points": min_post_baseline_points,
        "ignore_object_qc": ignore_object_qc,
        "events_selected": len(events),
        "coverage_rows": len(rows),
        "covered_with_baseline": sum(1 for r in rows if r.get("coverage_status") == "COVERED_WITH_BASELINE"),
        "full_baseline": sum(1 for r in rows if r.get("baseline_status") == "FULL_BASELINE"),
        "no_baseline": sum(1 for r in rows if r.get("baseline_status") == "NO_BASELINE"),
        "partial_coverage": sum(1 for r in rows if str(r.get("coverage_status", "")).startswith("PARTIAL")),
        "no_match": sum(1 for r in rows if r.get("coverage_status") == "NO_MATCHED_OBJECT"),
        "no_points": sum(1 for r in rows if r.get("coverage_status") == "NO_POINTS"),
        "good_coverage_good_precision": sum(1 for r in rows if r.get("event_observability_status") == "GOOD_COVERAGE_GOOD_PRECISION"),
        "good_coverage_low_snr": sum(1 for r in rows if r.get("event_observability_status") == "GOOD_COVERAGE_LOW_SNR"),
        "good_coverage_bad_object_qc": sum(1 for r in rows if r.get("event_observability_status") == "GOOD_COVERAGE_BAD_OBJECT_QC"),
        "partial_or_no_coverage": sum(1 for r in rows if str(r.get("event_observability_status", "")).startswith(("PARTIAL", "NO_"))),
    }
    return EventCoverageResult(rows=rows, summary=summary)


def export_event_coverage_csv(rows: Iterable[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_COVERAGE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def list_event_frame_coverage(
    session: Session,
    *,
    event_type: str | None = None,
    event_uid: str | None = None,
    calibration_name: str | None = None,
    instrument_unit_id: int | None = None,
    object_id: int | None = None,
    time_relation: str | None = None,
    limit: int | None = 200,
) -> list[dict[str, Any]]:
    """Return stored per-frame event coverage rows from event_frame_coverage.

    This is a read-only inspection helper for rows previously written by
    `event-coverage-summary --write-frame-coverage`. It intentionally does not
    recompute windows or object QC; use it to audit what was persisted for each
    frame after a coverage run.
    """

    wheres = ["1=1"]
    params: dict[str, Any] = {}
    if event_type:
        wheres.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if event_uid:
        wheres.append("e.event_uid = :event_uid")
        params["event_uid"] = event_uid
    if calibration_name:
        wheres.append("cv.name = :calibration_name")
        params["calibration_name"] = calibration_name
    if instrument_unit_id is not None:
        wheres.append("efc.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if object_id is not None:
        wheres.append("efc.object_id = :object_id")
        params["object_id"] = int(object_id)
    if time_relation:
        wheres.append("efc.time_relation = :time_relation")
        params["time_relation"] = time_relation

    sql = f"""
        SELECT
            efc.event_frame_coverage_id,
            e.event_id,
            e.event_uid,
            e.event_type,
            e.event_subtype,
            e.target_name,
            efc.frame_id,
            efc.object_id,
            m.flags->>'source_id' AS source_id,
            cv.name AS calibration_name,
            efc.instrument_unit_id,
            efc.time_relation,
            efc.frame_time_jd_approx,
            f.mjd,
            f.date_obs,
            efc.measurement_id,
            efc.mag_cal,
            efc.mag_err,
            efc.details->>'event_window_source' AS event_window_source,
            efc.details->>'baseline_status' AS baseline_status,
            efc.details->>'event_observability_status' AS event_observability_status,
            efc.details->>'event_observability_reasons' AS event_observability_reasons,
            f.raw_path
        FROM event_frame_coverage efc
        JOIN event_registry e ON e.event_id = efc.event_id
        JOIN frames f ON f.frame_id = efc.frame_id
        LEFT JOIN calibration_versions cv ON cv.calibration_version_id = efc.calibration_version_id
        LEFT JOIN measurements m ON m.measurement_id = efc.measurement_id
        WHERE {' AND '.join(wheres)}
        ORDER BY e.event_id, efc.object_id, efc.frame_time_jd_approx NULLS LAST, efc.frame_id
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
    return [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]


def export_event_frame_coverage_csv(rows: Iterable[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_FRAME_COVERAGE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
    return count



def match_events_to_detections(
    session: Session,
    *,
    event_type: str | None = None,
    event_uid: str | None = None,
    instrument_unit_id: int | None = None,
    radius_arcsec: float = 3.0,
    min_snr: float | None = None,
    event_window_source: str = "auto",
    pre_margin_hours: float | None = None,
    post_margin_hours: float | None = None,
    window_padding_factor: float = 1.0,
    include_outside_window: bool = False,
    limit_events: int | None = None,
    dry_run: bool = False,
) -> MatchEventDetectionsResult:
    """Link registered events to source detections near their sky position.

    This is the detection-side complement to event-object coverage. It supports
    transient-style cases such as novae or single-frame optical counterparts where
    the alert may not yet have a stable catalog object in the forced-photometry
    object table. The function matches source_detections by sky position and,
    when an event time window is available, classifies each detection as event,
    pre_baseline, post_baseline, or outside.
    """

    if radius_arcsec <= 0:
        raise ValueError("radius_arcsec must be positive")
    if event_window_source not in {"auto", "explicit", "t0_duration"}:
        raise ValueError("event_window_source must be one of: auto, explicit, t0_duration")

    result = MatchEventDetectionsResult(dry_run=dry_run)
    events = _load_events_for_coverage(session, event_type=event_type, event_uid=event_uid, limit=limit_events)
    result.selected_events = len(events)

    for event in events:
        positions = _event_detection_positions(session, event)
        if not positions:
            result.skipped_no_position += 1
            continue

        start, end, _duration_hours, resolved_window_source = _event_window(
            event,
            measurements=[],
            event_window_source=event_window_source,
        )
        pre_start = post_end = None
        resolved_pre_margin = pre_margin_hours
        resolved_post_margin = post_margin_hours
        if start is not None and end is not None:
            width_days = max(0.0, end - start)
            if resolved_pre_margin is None:
                resolved_pre_margin = width_days * float(window_padding_factor) * 24.0
            if resolved_post_margin is None:
                resolved_post_margin = width_days * float(window_padding_factor) * 24.0
            pre_start = start - float(resolved_pre_margin) / 24.0
            post_end = end + float(resolved_post_margin) / 24.0
        elif not include_outside_window:
            # Detection-only matching without a temporal window can be very broad
            # over a large archive. Keep it explicit unless the caller asks for it.
            result.skipped_no_time_window += 1
            continue

        best_by_detection: dict[int, dict[str, Any]] = {}
        for pos in positions:
            candidates = _candidate_detections_near_position(
                session,
                ra=float(pos["ra_deg"]),
                dec=float(pos["dec_deg"]),
                radius_arcsec=float(radius_arcsec),
                instrument_unit_id=instrument_unit_id,
                min_snr=min_snr,
            )
            result.candidate_detections += len(candidates)
            for det in candidates:
                sep = _angular_sep_arcsec(float(pos["ra_deg"]), float(pos["dec_deg"]), float(det["ra_deg"]), float(det["dec_deg"]))
                if sep > float(radius_arcsec):
                    continue
                t_jd = float(det["mjd"]) + 2400000.5 if _is_finite(det.get("mjd")) else None
                if start is None or end is None:
                    rel = "no_event_window"
                else:
                    rel = _time_relation(t_jd, start, end, pre_start, post_end)
                if rel == "outside" and not include_outside_window:
                    continue

                link = {
                    "event_id": int(event["event_id"]),
                    "source_detection_id": int(det["source_detection_id"]),
                    "frame_id": int(det["frame_id"]),
                    "object_id": pos.get("object_id"),
                    "instrument_unit_id": det.get("instrument_unit_id"),
                    "time_relation": rel,
                    "frame_time_jd_approx": t_jd,
                    "separation_arcsec": sep,
                    "detection_snr": det.get("snr"),
                    "detection_flux": det.get("flux"),
                    "peak_adu": det.get("peak_adu"),
                    "catalog_matched": bool(det.get("catalog_matched")),
                    "nearest_catalog_name": det.get("nearest_catalog_name"),
                    "nearest_catalog_source_id": det.get("nearest_catalog_source_id"),
                    "nearest_catalog_sep_arcsec": det.get("nearest_catalog_sep_arcsec"),
                    "confidence": max(0.05, 1.0 - sep / max(float(radius_arcsec), 1e-9)),
                    "details": {
                        "position_source": pos.get("position_source"),
                        "event_window_source": resolved_window_source,
                        "pre_margin_hours": resolved_pre_margin,
                        "post_margin_hours": resolved_post_margin,
                        "matching_radius_arcsec": radius_arcsec,
                        "note": "frame_time_jd_approx = frame.mjd + 2400000.5",
                    },
                }
                old = best_by_detection.get(int(det["source_detection_id"]))
                if old is None or (link["confidence"] or 0.0) > (old.get("confidence") or 0.0):
                    best_by_detection[int(det["source_detection_id"])] = link

        for link in best_by_detection.values():
            result.linked_detections += 1
            if link.get("catalog_matched"):
                result.catalog_matched_detections += 1
            else:
                result.catalog_unmatched_detections += 1
            if dry_run:
                continue
            _upsert_event_detection_link(session, link)
            result.inserted_or_updated += 1

    if not dry_run:
        session.commit()
    return result


def list_event_detection_links(
    session: Session,
    *,
    event_type: str | None = None,
    event_uid: str | None = None,
    instrument_unit_id: int | None = None,
    time_relation: str | None = None,
    catalog_matched: bool | None = None,
    min_snr: float | None = None,
    limit: int | None = 200,
) -> list[dict[str, Any]]:
    """Return stored event-detection links created by match-events-to-detections."""

    wheres = ["1=1"]
    params: dict[str, Any] = {}
    if event_type:
        wheres.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if event_uid:
        wheres.append("e.event_uid = :event_uid")
        params["event_uid"] = event_uid
    if instrument_unit_id is not None:
        wheres.append("edl.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if time_relation:
        wheres.append("edl.time_relation = :time_relation")
        params["time_relation"] = time_relation
    if catalog_matched is not None:
        wheres.append("edl.catalog_matched = :catalog_matched")
        params["catalog_matched"] = bool(catalog_matched)
    if min_snr is not None:
        wheres.append("edl.detection_snr >= :min_snr")
        params["min_snr"] = float(min_snr)

    sql = f"""
        SELECT
            edl.event_detection_link_id,
            e.event_id,
            e.event_uid,
            e.event_type,
            e.event_subtype,
            e.target_name,
            edl.frame_id,
            edl.source_detection_id,
            edl.object_id,
            edl.instrument_unit_id,
            edl.time_relation,
            edl.frame_time_jd_approx,
            f.mjd,
            f.date_obs,
            edl.separation_arcsec,
            edl.detection_snr,
            edl.detection_flux,
            edl.peak_adu,
            edl.catalog_matched,
            edl.nearest_catalog_name,
            edl.nearest_catalog_source_id,
            edl.nearest_catalog_sep_arcsec,
            edl.confidence,
            f.raw_path
        FROM event_detection_links edl
        JOIN event_registry e ON e.event_id = edl.event_id
        JOIN frames f ON f.frame_id = edl.frame_id
        WHERE {' AND '.join(wheres)}
        ORDER BY e.event_id, edl.time_relation, edl.frame_time_jd_approx NULLS LAST, edl.detection_snr DESC NULLS LAST
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)
    return [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]


def export_event_detection_links_csv(rows: Iterable[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_DETECTION_LINK_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _event_detection_positions(session: Session, event: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    if event.get("ra_deg") is not None and event.get("dec_deg") is not None:
        positions.append({
            "ra_deg": float(event["ra_deg"]),
            "dec_deg": float(event["dec_deg"]),
            "object_id": event.get("object_id"),
            "position_source": "event_coordinates",
        })

    object_ids = _load_event_linked_objects(session, int(event["event_id"]))
    if event.get("object_id") and int(event["object_id"]) not in object_ids:
        object_ids.append(int(event["object_id"]))
    if object_ids:
        rows = session.execute(
            text("SELECT object_id, ra_deg, dec_deg FROM objects WHERE object_id = ANY(:object_ids)"),
            {"object_ids": object_ids},
        ).mappings().all()
        for r in rows:
            if r.get("ra_deg") is None or r.get("dec_deg") is None:
                continue
            positions.append({
                "ra_deg": float(r["ra_deg"]),
                "dec_deg": float(r["dec_deg"]),
                "object_id": int(r["object_id"]),
                "position_source": "linked_object",
            })

    # Deduplicate very close positions, preferring linked object coordinates over
    # raw event coordinates when they are essentially identical.
    unique: list[dict[str, Any]] = []
    for pos in positions:
        duplicate_index = None
        for i, old in enumerate(unique):
            if _angular_sep_arcsec(float(pos["ra_deg"]), float(pos["dec_deg"]), float(old["ra_deg"]), float(old["dec_deg"])) < 0.05:
                duplicate_index = i
                break
        if duplicate_index is None:
            unique.append(pos)
        elif pos.get("position_source") == "linked_object":
            unique[duplicate_index] = pos
    return unique


def _candidate_detections_near_position(
    session: Session,
    *,
    ra: float,
    dec: float,
    radius_arcsec: float,
    instrument_unit_id: int | None,
    min_snr: float | None,
) -> list[dict[str, Any]]:
    dec_factor = max(0.05, abs(math.cos(math.radians(dec))))
    dra_deg = radius_arcsec / 3600.0 / dec_factor
    ddec_deg = radius_arcsec / 3600.0
    wheres = [
        "sd.ra_deg IS NOT NULL",
        "sd.dec_deg IS NOT NULL",
        "sd.ra_deg BETWEEN :ra_min AND :ra_max",
        "sd.dec_deg BETWEEN :dec_min AND :dec_max",
    ]
    params: dict[str, Any] = {
        "ra_min": ra - dra_deg,
        "ra_max": ra + dra_deg,
        "dec_min": dec - ddec_deg,
        "dec_max": dec + ddec_deg,
    }
    if instrument_unit_id is not None:
        wheres.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if min_snr is not None:
        wheres.append("sd.snr >= :min_snr")
        params["min_snr"] = float(min_snr)

    sql = f"""
        SELECT
            sd.source_detection_id,
            sd.frame_id,
            sd.ra_deg,
            sd.dec_deg,
            sd.snr,
            sd.flux,
            sd.peak_adu,
            f.instrument_unit_id,
            f.mjd,
            f.date_obs,
            EXISTS (
                SELECT 1 FROM detection_catalog_matches dcm
                WHERE dcm.source_detection_id = sd.source_detection_id
                  AND dcm.match_status = 'MATCHED'
            ) AS catalog_matched,
            (
                SELECT dcm.catalog_name FROM detection_catalog_matches dcm
                WHERE dcm.source_detection_id = sd.source_detection_id
                  AND dcm.match_status = 'MATCHED'
                ORDER BY dcm.sep_arcsec NULLS LAST
                LIMIT 1
            ) AS nearest_catalog_name,
            (
                SELECT cs.source_id FROM detection_catalog_matches dcm
                JOIN catalog_sources cs ON cs.catalog_source_id = dcm.catalog_source_id
                WHERE dcm.source_detection_id = sd.source_detection_id
                  AND dcm.match_status = 'MATCHED'
                ORDER BY dcm.sep_arcsec NULLS LAST
                LIMIT 1
            ) AS nearest_catalog_source_id,
            (
                SELECT dcm.sep_arcsec FROM detection_catalog_matches dcm
                WHERE dcm.source_detection_id = sd.source_detection_id
                  AND dcm.match_status = 'MATCHED'
                ORDER BY dcm.sep_arcsec NULLS LAST
                LIMIT 1
            ) AS nearest_catalog_sep_arcsec
        FROM source_detections sd
        JOIN frames f ON f.frame_id = sd.frame_id
        WHERE {' AND '.join(wheres)}
        ORDER BY f.mjd NULLS LAST, sd.snr DESC NULLS LAST
    """
    return [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]


def _upsert_event_detection_link(session: Session, link: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO event_detection_links (
                event_id, source_detection_id, frame_id, object_id, instrument_unit_id,
                time_relation, frame_time_jd_approx, separation_arcsec, detection_snr,
                detection_flux, peak_adu, catalog_matched, nearest_catalog_name,
                nearest_catalog_source_id, nearest_catalog_sep_arcsec, confidence, details
            ) VALUES (
                :event_id, :source_detection_id, :frame_id, :object_id, :instrument_unit_id,
                :time_relation, :frame_time_jd_approx, :separation_arcsec, :detection_snr,
                :detection_flux, :peak_adu, :catalog_matched, :nearest_catalog_name,
                :nearest_catalog_source_id, :nearest_catalog_sep_arcsec, :confidence, CAST(:details AS jsonb)
            )
            ON CONFLICT (event_id, source_detection_id) DO UPDATE SET
                frame_id = EXCLUDED.frame_id,
                object_id = EXCLUDED.object_id,
                instrument_unit_id = EXCLUDED.instrument_unit_id,
                time_relation = EXCLUDED.time_relation,
                frame_time_jd_approx = EXCLUDED.frame_time_jd_approx,
                separation_arcsec = EXCLUDED.separation_arcsec,
                detection_snr = EXCLUDED.detection_snr,
                detection_flux = EXCLUDED.detection_flux,
                peak_adu = EXCLUDED.peak_adu,
                catalog_matched = EXCLUDED.catalog_matched,
                nearest_catalog_name = EXCLUDED.nearest_catalog_name,
                nearest_catalog_source_id = EXCLUDED.nearest_catalog_source_id,
                nearest_catalog_sep_arcsec = EXCLUDED.nearest_catalog_sep_arcsec,
                confidence = EXCLUDED.confidence,
                details = EXCLUDED.details,
                updated_at = now()
            """
        ),
        {
            **{k: link.get(k) for k in (
                "event_id", "source_detection_id", "frame_id", "object_id", "instrument_unit_id",
                "time_relation", "frame_time_jd_approx", "separation_arcsec", "detection_snr",
                "detection_flux", "peak_adu", "catalog_matched", "nearest_catalog_name",
                "nearest_catalog_source_id", "nearest_catalog_sep_arcsec", "confidence",
            )},
            "details": json.dumps(link.get("details") or {}, ensure_ascii=False),
        },
    )

def _normalize_event_row(raw: dict[str, Any], *, default_event_type: str | None, default_source_catalog: str | None) -> dict[str, Any]:
    def s(*names: str) -> str | None:
        for name in names:
            if name in raw and str(raw[name]).strip() != "":
                return str(raw[name]).strip()
        return None

    row = {
        "event_uid": s("event_uid", "uid"),
        "event_type": s("event_type", "type") or default_event_type,
        "event_subtype": s("event_subtype", "subtype"),
        "source_catalog": s("source_catalog", "event_source") or default_source_catalog,
        "source_event_id": s("source_event_id", "event_id", "alert_id"),
        "external_catalog": s("external_catalog", "target_catalog", "id_catalog"),
        "external_id": s("external_id", "target_id", "source_id"),
        "target_name": s("target_name", "target", "object_name"),
        "ra_deg": _float_or_none(s("ra_deg", "ra", "RA")),
        "dec_deg": _float_or_none(s("dec_deg", "dec", "DEC")),
        "position_error_arcsec": _float_or_none(s("position_error_arcsec", "pos_err_arcsec", "error_arcsec")),
        "object_id": _int_or_none(s("object_id")),
        "t_start_bjd_tdb": _float_or_none(s("t_start_bjd_tdb", "start_bjd_tdb", "t_start")),
        "t_peak_bjd_tdb": _float_or_none(s("t_peak_bjd_tdb", "peak_bjd_tdb", "t_peak")),
        "t_end_bjd_tdb": _float_or_none(s("t_end_bjd_tdb", "end_bjd_tdb", "t_end")),
        "t0_bjd_tdb": _float_or_none(s("t0_bjd_tdb", "epoch_bjd_tdb", "t0")),
        "period_days": _float_or_none(s("period_days", "period")),
        "duration_hours": _float_or_none(s("duration_hours", "duration_hr", "duration")),
        "expected_sign": s("expected_sign", "sign"),
        "expected_depth_ppt": _float_or_none(s("expected_depth_ppt", "depth_ppt")),
        "expected_amplitude_mag": _float_or_none(s("expected_amplitude_mag", "amplitude_mag")),
        "priority": _float_or_none(s("priority")),
        "status": s("status") or "NEW",
        "event_model_json": _json_or_empty(s("event_model_json", "model_json")),
    }
    if row["external_catalog"] is None and row["external_id"] and row["source_catalog"] in {"gaia_dr3_master", "gaia_dr3", "tic"}:
        row["external_catalog"] = row["source_catalog"]
    return row


def _make_event_uid(row: dict[str, Any]) -> str:
    if row.get("source_catalog") and row.get("source_event_id"):
        return f"{row['source_catalog']}:{row['source_event_id']}"
    if row.get("external_catalog") and row.get("external_id") and row.get("event_type"):
        center = row.get("t_peak_bjd_tdb") or row.get("t0_bjd_tdb") or row.get("t_start_bjd_tdb") or "na"
        return f"{row['event_type']}:{row['external_catalog']}:{row['external_id']}:{center}"
    payload = json.dumps(row, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"event:{digest}"


def _upsert_event(session: Session, row: dict[str, Any]) -> None:
    params = dict(row)
    params["event_model_json"] = json.dumps(params.get("event_model_json") or {}, ensure_ascii=False)
    params["raw_json"] = json.dumps(params.get("raw_json") or {}, ensure_ascii=False)
    sql = text(
        """
        INSERT INTO event_registry (
            event_uid, event_type, event_subtype, source_catalog, source_event_id,
            external_catalog, external_id, target_name, ra_deg, dec_deg, position_error_arcsec,
            object_id, t_start_bjd_tdb, t_peak_bjd_tdb, t_end_bjd_tdb, t0_bjd_tdb,
            period_days, duration_hours, expected_sign, expected_depth_ppt, expected_amplitude_mag,
            priority, status, event_model_json, raw_json
        ) VALUES (
            :event_uid, :event_type, :event_subtype, :source_catalog, :source_event_id,
            :external_catalog, :external_id, :target_name, :ra_deg, :dec_deg, :position_error_arcsec,
            :object_id, :t_start_bjd_tdb, :t_peak_bjd_tdb, :t_end_bjd_tdb, :t0_bjd_tdb,
            :period_days, :duration_hours, :expected_sign, :expected_depth_ppt, :expected_amplitude_mag,
            :priority, :status, CAST(:event_model_json AS jsonb), CAST(:raw_json AS jsonb)
        )
        ON CONFLICT (event_uid) DO UPDATE SET
            event_type = EXCLUDED.event_type,
            event_subtype = EXCLUDED.event_subtype,
            source_catalog = EXCLUDED.source_catalog,
            source_event_id = EXCLUDED.source_event_id,
            external_catalog = EXCLUDED.external_catalog,
            external_id = EXCLUDED.external_id,
            target_name = EXCLUDED.target_name,
            ra_deg = EXCLUDED.ra_deg,
            dec_deg = EXCLUDED.dec_deg,
            position_error_arcsec = EXCLUDED.position_error_arcsec,
            object_id = EXCLUDED.object_id,
            t_start_bjd_tdb = EXCLUDED.t_start_bjd_tdb,
            t_peak_bjd_tdb = EXCLUDED.t_peak_bjd_tdb,
            t_end_bjd_tdb = EXCLUDED.t_end_bjd_tdb,
            t0_bjd_tdb = EXCLUDED.t0_bjd_tdb,
            period_days = EXCLUDED.period_days,
            duration_hours = EXCLUDED.duration_hours,
            expected_sign = EXCLUDED.expected_sign,
            expected_depth_ppt = EXCLUDED.expected_depth_ppt,
            expected_amplitude_mag = EXCLUDED.expected_amplitude_mag,
            priority = EXCLUDED.priority,
            status = EXCLUDED.status,
            event_model_json = EXCLUDED.event_model_json,
            raw_json = EXCLUDED.raw_json,
            updated_at = now()
        """
    )
    session.execute(sql, params)


def _match_one_event_to_objects(
    session: Session,
    event: dict[str, Any],
    *,
    external_catalog: str | None,
    external_id_column_preference: str | None,
    coordinate_radius_arcsec: float,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if event.get("object_id"):
        matches.append({"object_id": int(event["object_id"]), "match_type": "explicit_object_id", "separation_arcsec": None, "confidence": 1.0})

    ext_cat = external_catalog or event.get("external_catalog")
    ext_id = str(event.get("external_id") or "").strip()
    if ext_cat and ext_id:
        matches.extend(_match_by_external_id(session, ext_cat=ext_cat, ext_id=ext_id, catalog_name=external_id_column_preference))

    if event.get("ra_deg") is not None and event.get("dec_deg") is not None:
        radius = max(float(coordinate_radius_arcsec), float(event.get("position_error_arcsec") or 0.0))
        matches.extend(_match_by_coordinates(session, ra=float(event["ra_deg"]), dec=float(event["dec_deg"]), radius_arcsec=radius))

    # Deduplicate, keep highest confidence / smallest separation.
    by_object: dict[int, dict[str, Any]] = {}
    for m in matches:
        object_id = int(m["object_id"])
        old = by_object.get(object_id)
        if old is None:
            by_object[object_id] = m
            continue
        old_score = (old.get("confidence") or 0.0, -1.0 * (old.get("separation_arcsec") or 0.0))
        new_score = (m.get("confidence") or 0.0, -1.0 * (m.get("separation_arcsec") or 0.0))
        if new_score > old_score:
            by_object[object_id] = m
    return list(by_object.values())


def _match_by_external_id(session: Session, *, ext_cat: str, ext_id: str, catalog_name: str | None) -> list[dict[str, Any]]:
    ext_cat_lower = ext_cat.lower()
    matches: list[dict[str, Any]] = []
    if ext_cat_lower in {"gaia", "gaia_dr3", "gaiaedr3"}:
        rows = session.execute(text("SELECT object_id FROM objects WHERE gaia_dr3_id = :ext_id"), {"ext_id": ext_id}).mappings().all()
        for r in rows:
            matches.append({"object_id": int(r["object_id"]), "match_type": "gaia_dr3_id", "separation_arcsec": None, "confidence": 1.0})

    # Match through measurement flags, useful for gaia_dr3_master source_id values.
    flag_catalog = catalog_name or ext_cat
    rows = session.execute(
        text(
            """
            SELECT DISTINCT object_id
            FROM measurements
            WHERE flags->>'catalog_name' = :catalog_name
              AND flags->>'source_id' = :source_id
            """
        ),
        {"catalog_name": flag_catalog, "source_id": ext_id},
    ).mappings().all()
    for r in rows:
        matches.append({"object_id": int(r["object_id"]), "match_type": "measurement_flag_source_id", "separation_arcsec": None, "confidence": 0.98})

    # Generic JSON external_ids fallback: supports keys such as TIC, tic, VSX, etc.
    rows = session.execute(text("SELECT object_id, external_ids FROM objects WHERE external_ids IS NOT NULL")).mappings().all()
    for r in rows:
        external_ids = r.get("external_ids") or {}
        for key, value in external_ids.items():
            if str(key).lower() == ext_cat_lower and str(value) == ext_id:
                matches.append({"object_id": int(r["object_id"]), "match_type": "object_external_ids", "separation_arcsec": None, "confidence": 0.95})
                break
    return matches


def _match_by_coordinates(session: Session, *, ra: float, dec: float, radius_arcsec: float) -> list[dict[str, Any]]:
    if radius_arcsec <= 0:
        return []
    dec_factor = max(0.05, abs(math.cos(math.radians(dec))))
    dra_deg = radius_arcsec / 3600.0 / dec_factor
    ddec_deg = radius_arcsec / 3600.0
    rows = session.execute(
        text(
            """
            SELECT object_id, ra_deg, dec_deg
            FROM objects
            WHERE ra_deg BETWEEN :ra_min AND :ra_max
              AND dec_deg BETWEEN :dec_min AND :dec_max
            """
        ),
        {"ra_min": ra - dra_deg, "ra_max": ra + dra_deg, "dec_min": dec - ddec_deg, "dec_max": dec + ddec_deg},
    ).mappings().all()
    matches: list[dict[str, Any]] = []
    for r in rows:
        sep = _angular_sep_arcsec(ra, dec, float(r["ra_deg"]), float(r["dec_deg"]))
        if sep <= radius_arcsec:
            confidence = max(0.1, 1.0 - sep / max(radius_arcsec, 1e-6))
            matches.append({"object_id": int(r["object_id"]), "match_type": "coordinate", "separation_arcsec": sep, "confidence": confidence})
    matches.sort(key=lambda m: m.get("separation_arcsec") or 0.0)
    return matches


def _upsert_event_object_link(session: Session, event: dict[str, Any], match: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO event_object_links (event_id, object_id, match_type, separation_arcsec, confidence, details)
            VALUES (:event_id, :object_id, :match_type, :separation_arcsec, :confidence, CAST(:details AS jsonb))
            ON CONFLICT (event_id, object_id, match_type) DO UPDATE SET
                separation_arcsec = EXCLUDED.separation_arcsec,
                confidence = EXCLUDED.confidence,
                details = EXCLUDED.details,
                updated_at = now()
            """
        ),
        {
            "event_id": int(event["event_id"]),
            "object_id": int(match["object_id"]),
            "match_type": match.get("match_type"),
            "separation_arcsec": match.get("separation_arcsec"),
            "confidence": match.get("confidence"),
            "details": json.dumps({k: v for k, v in match.items() if k not in {"object_id", "match_type", "separation_arcsec", "confidence"}}, ensure_ascii=False),
        },
    )


def _load_events_for_coverage(session: Session, *, event_type: str | None, event_uid: str | None, limit: int | None) -> list[dict[str, Any]]:
    rows = list_events(session, event_type=event_type, limit=limit, status=None)
    if event_uid:
        rows = [r for r in rows if r.get("event_uid") == event_uid]
    return rows


def _load_event_linked_objects(session: Session, event_id: int) -> list[int]:
    rows = session.execute(
        text("SELECT object_id FROM event_object_links WHERE event_id = :event_id ORDER BY confidence DESC NULLS LAST"),
        {"event_id": int(event_id)},
    ).mappings().all()
    return [int(r["object_id"]) for r in rows]


def _coverage_for_object(
    session: Session,
    *,
    event: dict[str, Any],
    object_id: int,
    calibration_name: str,
    catalog_name: str | None,
    instrument_unit_id: int | None,
    window_padding_factor: float,
    pre_margin_hours: float | None,
    post_margin_hours: float | None,
    event_window_source: str,
    min_depth_total_snr: float = 3.0,
    min_event_points: int = 1,
    min_pre_baseline_points: int = 1,
    min_post_baseline_points: int = 1,
    bad_object_qc_flags: set[str] | None = None,
    ignore_object_qc: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cv = session.execute(text("SELECT calibration_version_id FROM calibration_versions WHERE name = :name"), {"name": calibration_name}).scalar_one_or_none()
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_name}")

    params = {"object_id": object_id, "cv": int(cv)}
    wheres = ["m.object_id = :object_id", "m.calibration_version_id = :cv"]
    if instrument_unit_id is not None:
        wheres.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if catalog_name is not None:
        wheres.append("m.flags->>'catalog_name' = :catalog_name")
        params["catalog_name"] = catalog_name
    sql = f"""
        SELECT m.measurement_id, m.object_id, m.frame_id, m.mag_cal, m.mag_err, m.flags,
               f.mjd, f.date_obs, f.instrument_unit_id,
               o.ra_deg, o.dec_deg,
               oq.flags AS object_qc_flags
        FROM measurements m
        JOIN frames f ON f.frame_id = m.frame_id
        JOIN objects o ON o.object_id = m.object_id
        LEFT JOIN object_qc oq ON oq.object_id = m.object_id
            AND oq.calibration_version_id = m.calibration_version_id
            AND (oq.instrument_unit_id IS NOT DISTINCT FROM f.instrument_unit_id)
            AND (oq.catalog_name IS NOT DISTINCT FROM (m.flags->>'catalog_name'))
        WHERE {' AND '.join(wheres)}
        ORDER BY f.mjd, f.frame_id
    """
    meas_rows = [_row_to_dict(r) for r in session.execute(text(sql), params).mappings().all()]
    start, end, duration_hours, resolved_window_source = _event_window(
        event, measurements=meas_rows, event_window_source=event_window_source
    )

    mags = [float(r["mag_cal"]) for r in meas_rows if _is_finite(r.get("mag_cal"))]
    mag_errs = [float(r["mag_err"]) for r in meas_rows if _is_finite(r.get("mag_err"))]
    frame_rows: list[dict[str, Any]] = []
    n_before = n_in = n_after = n_outside = 0
    pre_start = post_end = None
    resolved_pre_margin_hours = pre_margin_hours
    resolved_post_margin_hours = post_margin_hours
    if start is not None and end is not None:
        # If the caller did not provide explicit baseline margins, fall back to the
        # original duration-scaled padding. This keeps old CLI behavior intact while
        # making scheduled-event coverage checks more explicit.
        width_days = max(0.0, end - start)
        if resolved_pre_margin_hours is None:
            resolved_pre_margin_hours = width_days * float(window_padding_factor) * 24.0
        if resolved_post_margin_hours is None:
            resolved_post_margin_hours = width_days * float(window_padding_factor) * 24.0
        pre_start = start - float(resolved_pre_margin_hours) / 24.0
        post_end = end + float(resolved_post_margin_hours) / 24.0
    for r in meas_rows:
        t_jd = (float(r["mjd"]) + 2400000.5) if _is_finite(r.get("mjd")) else None
        rel = _time_relation(t_jd, start, end, pre_start, post_end)
        if rel == "pre_baseline":
            n_before += 1
        elif rel == "event":
            n_in += 1
        elif rel == "post_baseline":
            n_after += 1
        else:
            n_outside += 1
        frame_rows.append({**r, "frame_time_jd_approx": t_jd, "time_relation": rel})

    median_mag_err = _median(mag_errs)
    expected_depth_ppt = event.get("expected_depth_ppt")
    depth_per_point = None
    depth_total = None
    if expected_depth_ppt is not None and median_mag_err is not None and median_mag_err > 0:
        noise_ppt = 921.034 * median_mag_err
        depth_per_point = float(expected_depth_ppt) / noise_ppt if noise_ppt > 0 else None
        depth_total = depth_per_point * math.sqrt(n_in) if depth_per_point is not None and n_in > 0 else None

    object_qc_flags = ""
    if meas_rows:
        flags = meas_rows[0].get("object_qc_flags") or {}
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except Exception:
                flags = {}
        object_qc_flags = ",".join(sorted(k for k, v in (flags or {}).items() if v))

    active_object_qc_flags = [flag for flag in object_qc_flags.split(",") if flag]
    status = _coverage_status(len(meas_rows), start, end, n_before, n_in, n_after)
    baseline_status = _baseline_status(len(meas_rows), start, end, n_before, n_in, n_after)
    observability_status, observability_reasons = _event_observability_status(
        coverage_status=status,
        baseline_status=baseline_status,
        n_event=n_in,
        n_pre_baseline=n_before,
        n_post_baseline=n_after,
        depth_total_snr_est=depth_total,
        expected_depth_ppt=expected_depth_ppt,
        object_qc_flags=active_object_qc_flags,
        min_depth_total_snr=min_depth_total_snr,
        min_event_points=min_event_points,
        min_pre_baseline_points=min_pre_baseline_points,
        min_post_baseline_points=min_post_baseline_points,
        bad_object_qc_flags=bad_object_qc_flags or DEFAULT_BAD_EVENT_OBJECT_QC_FLAGS,
        ignore_object_qc=ignore_object_qc,
    )
    row = {
        "event_id": event.get("event_id"),
        "event_uid": event.get("event_uid"),
        "event_type": event.get("event_type"),
        "event_subtype": event.get("event_subtype"),
        "target_name": event.get("target_name"),
        "object_id": object_id,
        "source_id": (meas_rows[0].get("flags") or {}).get("source_id") if meas_rows else None,
        "ra_deg": meas_rows[0].get("ra_deg") if meas_rows else event.get("ra_deg"),
        "dec_deg": meas_rows[0].get("dec_deg") if meas_rows else event.get("dec_deg"),
        "calibration_name": calibration_name,
        "instrument_unit_id": instrument_unit_id,
        "n_points": len(meas_rows),
        "n_before": n_before,
        "n_in_window": n_in,
        "n_after": n_after,
        "n_outside": n_outside,
        "n_pre_baseline": n_before,
        "n_event": n_in,
        "n_post_baseline": n_after,
        "event_window_source": resolved_window_source,
        "baseline_status": baseline_status,
        "event_observability_status": observability_status,
        "event_observability_reasons": observability_reasons,
        "window_start_bjd_tdb": start,
        "window_end_bjd_tdb": end,
        "window_duration_hours": duration_hours,
        "pre_baseline_start_bjd_tdb": pre_start,
        "post_baseline_end_bjd_tdb": post_end,
        "pre_margin_hours": resolved_pre_margin_hours,
        "post_margin_hours": resolved_post_margin_hours,
        "mjd_min": min([r["mjd"] for r in meas_rows if _is_finite(r.get("mjd"))], default=None),
        "mjd_max": max([r["mjd"] for r in meas_rows if _is_finite(r.get("mjd"))], default=None),
        "mag_median": _median(mags),
        "mag_rms": _sample_rms(mags),
        "mag_mad": _mad(mags),
        "median_mag_err": median_mag_err,
        "expected_sign": event.get("expected_sign"),
        "expected_depth_ppt": expected_depth_ppt,
        "expected_amplitude_mag": event.get("expected_amplitude_mag"),
        "depth_per_point_snr_est": depth_per_point,
        "depth_total_snr_est": depth_total,
        "object_qc_flags": object_qc_flags,
        "coverage_status": status,
    }
    return row, frame_rows


def _coverage_no_match_row(
    event: dict[str, Any],
    calibration_name: str,
    instrument_unit_id: int | None,
    *,
    pre_margin_hours: float | None = None,
    post_margin_hours: float | None = None,
    event_window_source: str = "auto",
    window_padding_factor: float = 1.0,
    min_depth_total_snr: float = 3.0,
    min_event_points: int = 1,
    min_pre_baseline_points: int = 1,
    min_post_baseline_points: int = 1,
) -> dict[str, Any]:
    start, end, duration_hours, resolved_window_source = _event_window(
        event, measurements=[], event_window_source=event_window_source
    )
    pre_start = post_end = None
    resolved_pre_margin_hours = pre_margin_hours
    resolved_post_margin_hours = post_margin_hours
    if start is not None and end is not None:
        width_days = max(0.0, end - start)
        if resolved_pre_margin_hours is None:
            resolved_pre_margin_hours = width_days * float(window_padding_factor) * 24.0
        if resolved_post_margin_hours is None:
            resolved_post_margin_hours = width_days * float(window_padding_factor) * 24.0
        pre_start = start - float(resolved_pre_margin_hours) / 24.0
        post_end = end + float(resolved_post_margin_hours) / 24.0
    observability_status, observability_reasons = _event_observability_status(
        coverage_status="NO_MATCHED_OBJECT",
        baseline_status="NO_MATCHED_OBJECT",
        n_event=0,
        n_pre_baseline=0,
        n_post_baseline=0,
        depth_total_snr_est=None,
        expected_depth_ppt=event.get("expected_depth_ppt"),
        object_qc_flags=[],
        min_depth_total_snr=min_depth_total_snr,
        min_event_points=min_event_points,
        min_pre_baseline_points=min_pre_baseline_points,
        min_post_baseline_points=min_post_baseline_points,
        bad_object_qc_flags=DEFAULT_BAD_EVENT_OBJECT_QC_FLAGS,
        ignore_object_qc=True,
    )
    return {
        "event_id": event.get("event_id"),
        "event_uid": event.get("event_uid"),
        "event_type": event.get("event_type"),
        "event_subtype": event.get("event_subtype"),
        "target_name": event.get("target_name"),
        "object_id": event.get("object_id"),
        "source_id": event.get("external_id"),
        "ra_deg": event.get("ra_deg"),
        "dec_deg": event.get("dec_deg"),
        "calibration_name": calibration_name,
        "instrument_unit_id": instrument_unit_id,
        "n_points": 0,
        "n_before": 0,
        "n_in_window": 0,
        "n_after": 0,
        "n_outside": 0,
        "n_pre_baseline": 0,
        "n_event": 0,
        "n_post_baseline": 0,
        "event_window_source": resolved_window_source,
        "baseline_status": "NO_MATCHED_OBJECT",
        "event_observability_status": observability_status,
        "event_observability_reasons": observability_reasons,
        "window_start_bjd_tdb": start,
        "window_end_bjd_tdb": end,
        "window_duration_hours": duration_hours,
        "pre_baseline_start_bjd_tdb": pre_start,
        "post_baseline_end_bjd_tdb": post_end,
        "pre_margin_hours": resolved_pre_margin_hours,
        "post_margin_hours": resolved_post_margin_hours,
        "expected_sign": event.get("expected_sign"),
        "expected_depth_ppt": event.get("expected_depth_ppt"),
        "expected_amplitude_mag": event.get("expected_amplitude_mag"),
        "coverage_status": "NO_MATCHED_OBJECT",
    }


def _write_frame_coverage(session: Session, *, event: dict[str, Any], row: dict[str, Any], frame_rows: list[dict[str, Any]]) -> None:
    cv = session.execute(text("SELECT calibration_version_id FROM calibration_versions WHERE name = :name"), {"name": row["calibration_name"]}).scalar_one_or_none()
    for fr in frame_rows:
        session.execute(
            text(
                """
                INSERT INTO event_frame_coverage (
                    event_id, frame_id, object_id, calibration_version_id, instrument_unit_id,
                    time_relation, frame_time_jd_approx, measurement_id, mag_cal, mag_err, details
                ) VALUES (
                    :event_id, :frame_id, :object_id, :calibration_version_id, :instrument_unit_id,
                    :time_relation, :frame_time_jd_approx, :measurement_id, :mag_cal, :mag_err, CAST(:details AS jsonb)
                )
                ON CONFLICT (event_id, frame_id, object_id, calibration_version_id) DO UPDATE SET
                    instrument_unit_id = EXCLUDED.instrument_unit_id,
                    time_relation = EXCLUDED.time_relation,
                    frame_time_jd_approx = EXCLUDED.frame_time_jd_approx,
                    measurement_id = EXCLUDED.measurement_id,
                    mag_cal = EXCLUDED.mag_cal,
                    mag_err = EXCLUDED.mag_err,
                    details = EXCLUDED.details,
                    updated_at = now()
                """
            ),
            {
                "event_id": int(event["event_id"]),
                "frame_id": int(fr["frame_id"]),
                "object_id": int(row["object_id"]),
                "calibration_version_id": int(cv) if cv is not None else None,
                "instrument_unit_id": int(fr["instrument_unit_id"]) if fr.get("instrument_unit_id") is not None else None,
                "time_relation": fr.get("time_relation"),
                "frame_time_jd_approx": fr.get("frame_time_jd_approx"),
                "measurement_id": int(fr["measurement_id"]),
                "mag_cal": fr.get("mag_cal"),
                "mag_err": fr.get("mag_err"),
                "details": json.dumps({
                    "note": "frame_time_jd_approx = frame.mjd + 2400000.5",
                    "event_window_source": row.get("event_window_source"),
                    "baseline_status": row.get("baseline_status"),
                    "event_observability_status": row.get("event_observability_status"),
                    "event_observability_reasons": row.get("event_observability_reasons"),
                    "pre_baseline_start_bjd_tdb": row.get("pre_baseline_start_bjd_tdb"),
                    "post_baseline_end_bjd_tdb": row.get("post_baseline_end_bjd_tdb"),
                }, ensure_ascii=False),
            },
        )


def _event_window(
    event: dict[str, Any],
    *,
    measurements: list[dict[str, Any]],
    event_window_source: str = "auto",
) -> tuple[float | None, float | None, float | None, str | None]:
    """Resolve the event window used for coverage classification.

    `auto` prefers explicitly supplied start/end contacts. `explicit` uses only
    t_start/t_end and reports no window if they are absent. `t0_duration` uses
    t_peak/t0 plus duration and, when period is available, folds the epoch near
    the median observed time.
    """

    if event_window_source not in {"auto", "explicit", "t0_duration"}:
        raise ValueError("event_window_source must be one of: auto, explicit, t0_duration")

    has_explicit = event.get("t_start_bjd_tdb") is not None and event.get("t_end_bjd_tdb") is not None
    if event_window_source in {"auto", "explicit"} and has_explicit:
        start = float(event["t_start_bjd_tdb"])
        end = float(event["t_end_bjd_tdb"])
        return start, end, (end - start) * 24.0, "explicit"
    if event_window_source == "explicit":
        return None, None, None, None

    center = event.get("t_peak_bjd_tdb") if event.get("t_peak_bjd_tdb") is not None else event.get("t0_bjd_tdb")
    period = event.get("period_days")
    if center is not None and period is not None and measurements:
        times = [float(r["mjd"]) + 2400000.5 for r in measurements if _is_finite(r.get("mjd"))]
        if times:
            median_time = _median(times)
            if median_time is not None:
                k = round((median_time - float(center)) / float(period))
                center = float(center) + k * float(period)

    if center is None:
        return None, None, None, None
    duration_hours = event.get("duration_hours")
    if duration_hours is None:
        # Default infrastructure window: one hour around a peak/epoch if duration is unknown.
        duration_hours = 1.0
    half_days = float(duration_hours) / 48.0
    return float(center) - half_days, float(center) + half_days, float(duration_hours), "t0_duration"


def _time_relation(t: float | None, start: float | None, end: float | None, pre_start: float | None, post_end: float | None) -> str:
    if t is None or start is None or end is None:
        return "outside"
    if start <= t <= end:
        return "event"
    if pre_start is not None and pre_start <= t < start:
        return "pre_baseline"
    if post_end is not None and end < t <= post_end:
        return "post_baseline"
    return "outside"


def _coverage_status(n_points: int, start: float | None, end: float | None, n_before: int, n_in: int, n_after: int) -> str:
    if n_points == 0:
        return "NO_POINTS"
    if start is None or end is None:
        return "NO_EVENT_WINDOW"
    if n_in > 0 and n_before > 0 and n_after > 0:
        return "COVERED_WITH_BASELINE"
    if n_in > 0:
        return "PARTIAL_EVENT_ONLY"
    if n_before > 0 or n_after > 0:
        return "PARTIAL_BASELINE_ONLY"
    return "OUTSIDE_WINDOW"


def _baseline_status(n_points: int, start: float | None, end: float | None, n_before: int, n_in: int, n_after: int) -> str:
    if n_points == 0:
        return "NO_POINTS"
    if start is None or end is None:
        return "NO_EVENT_WINDOW"
    if n_in <= 0:
        if n_before > 0 or n_after > 0:
            return "BASELINE_ONLY"
        return "NO_EVENT_POINTS"
    if n_before > 0 and n_after > 0:
        return "FULL_BASELINE"
    if n_before > 0:
        return "PRE_BASELINE_ONLY"
    if n_after > 0:
        return "POST_BASELINE_ONLY"
    return "NO_BASELINE"


def _event_observability_status(
    *,
    coverage_status: str,
    baseline_status: str,
    n_event: int,
    n_pre_baseline: int,
    n_post_baseline: int,
    depth_total_snr_est: float | None,
    expected_depth_ppt: float | None,
    object_qc_flags: list[str],
    min_depth_total_snr: float,
    min_event_points: int,
    min_pre_baseline_points: int,
    min_post_baseline_points: int,
    bad_object_qc_flags: set[str],
    ignore_object_qc: bool,
) -> tuple[str, str]:
    """Classify whether an event is practically usable after coverage is known.

    Coverage status answers a geometric question: did we observe the event window?
    Observability adds two operational checks used in archive triage: whether the
    expected signal is large enough for the measured scatter and whether the object
    itself is contaminated by QC flags such as crowding, edge placement, or unstable
    apertures. This is intentionally a triage label, not a physical model fit.
    """

    reasons: list[str] = []
    if coverage_status == "NO_MATCHED_OBJECT":
        return "NO_MATCH", "NO_MATCHED_OBJECT"
    if coverage_status == "NO_POINTS":
        return "NO_POINTS", "NO_POINTS"
    if coverage_status == "NO_EVENT_WINDOW":
        return "NO_EVENT_WINDOW", "NO_EVENT_WINDOW"
    if n_event <= 0:
        return "NO_EVENT_POINTS", "NO_EVENT_POINTS"

    if n_event < int(min_event_points):
        reasons.append(f"LOW_EVENT_POINTS={n_event}<{int(min_event_points)}")
    if n_pre_baseline < int(min_pre_baseline_points):
        reasons.append(f"LOW_PRE_BASELINE={n_pre_baseline}<{int(min_pre_baseline_points)}")
    if n_post_baseline < int(min_post_baseline_points):
        reasons.append(f"LOW_POST_BASELINE={n_post_baseline}<{int(min_post_baseline_points)}")

    if baseline_status == "NO_BASELINE":
        reasons.append("NO_BASELINE")
    elif baseline_status in {"PRE_BASELINE_ONLY", "POST_BASELINE_ONLY", "BASELINE_ONLY"}:
        reasons.append(baseline_status)

    if coverage_status != "COVERED_WITH_BASELINE" or reasons:
        return "PARTIAL_COVERAGE", ";".join(reasons or [coverage_status])

    bad_flags = sorted(set(object_qc_flags) & set(bad_object_qc_flags))
    low_snr = (
        expected_depth_ppt is not None
        and depth_total_snr_est is not None
        and float(depth_total_snr_est) < float(min_depth_total_snr)
    )
    if low_snr:
        reasons.append(f"LOW_DEPTH_SNR={float(depth_total_snr_est):.3g}<{float(min_depth_total_snr):.3g}")

    if bad_flags and not ignore_object_qc:
        reasons.append("BAD_OBJECT_QC=" + ",".join(bad_flags))
        return "GOOD_COVERAGE_BAD_OBJECT_QC", ";".join(reasons)
    if low_snr:
        return "GOOD_COVERAGE_LOW_SNR", ";".join(reasons)
    if expected_depth_ppt is not None and depth_total_snr_est is None:
        return "GOOD_COVERAGE_UNKNOWN_PRECISION", "EXPECTED_SIGNAL_BUT_NO_SNR_ESTIMATE"
    if expected_depth_ppt is None:
        return "GOOD_COVERAGE_UNKNOWN_SIGNAL", "NO_EXPECTED_SIGNAL"
    return "GOOD_COVERAGE_GOOD_PRECISION", "OK"


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    for key in ("raw_json", "event_model_json", "flags", "object_qc_flags"):
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    return d


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        x = float(str(value).strip())
    except Exception:
        return None
    return x if math.isfinite(x) else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _json_or_empty(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _angular_sep_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    sd = math.sin((d2 - d1) / 2.0)
    sr = math.sin((r2 - r1) / 2.0)
    a = sd * sd + math.cos(d1) * math.cos(d2) * sr * sr
    return math.degrees(2.0 * math.asin(min(1.0, math.sqrt(max(0.0, a))))) * 3600.0


def _median(values: list[float]) -> float | None:
    vals = sorted(float(v) for v in values if _is_finite(v))
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


def _sample_rms(values: list[float]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False
