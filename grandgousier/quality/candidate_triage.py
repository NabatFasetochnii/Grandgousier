from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from grandgousier.db.models import ObjectQc
from grandgousier.quality.lightcurve_quality import build_rms_mag_summary, build_variable_candidates, load_object_scatter_rows
from grandgousier.quality.object_quality import load_object_qc_map


@dataclass
class CandidateTriageResult:
    rows: list[dict[str, Any]]
    summary: dict[str, Any]


def build_candidate_triage(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    min_measurements: int = 5,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    min_rms: float | None = None,
    min_excess: float = 2.0,
    exclude_flagged: bool = False,
    exclude_crowded: bool = True,
    exclude_edge: bool = True,
    exclude_saturated: bool = True,
    exclude_catalog_outliers: bool = True,
    reject_aperture_unstable: bool = True,
    require_detection_support: bool = True,
    min_nearest_neighbor_arcsec: float | None = None,
    max_mag_median: float | None = None,
    max_ref_mag_delta: float | None = None,
    qc_algorithm: str = "object_qc_v1",
    limit: int | None = None,
    target_object_ids: list[int] | None = None,
    enable_intrinsic_variability_candidates: bool = True,
    min_intrinsic_rms: float | None = 0.03,
    min_rms_over_mag_err: float | None = 4.0,
    min_mad_over_mag_err: float | None = 2.5,
    intrinsic_mag_err_floor: float | None = 0.01,
    min_robust_peak_to_peak: float | None = 0.08,
    enable_low_amplitude_intrinsic_candidates: bool = True,
    low_amplitude_min_measurements: int = 30,
    low_amplitude_min_intrinsic_rms: float | None = 0.03,
    low_amplitude_min_rms_over_mag_err: float | None = 2.3,
    low_amplitude_min_mad_over_mag_err: float | None = 2.0,
    low_amplitude_min_robust_peak_to_peak: float | None = 0.08,
) -> CandidateTriageResult:
    """Return candidate variables with explicit rejection reasons.

    Unlike `variable-candidate-summary`, this command keeps rejected candidates in
    the output so a strict filter producing "No rows" can be diagnosed.
    """

    object_rows = load_object_scatter_rows(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        min_measurements=min_measurements,
    )
    rms_candidates = build_variable_candidates(
        object_rows,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
        min_measurements=min_measurements,
        min_rms=min_rms,
        min_excess=min_excess,
        limit=None,
    )

    candidate_map: dict[int, dict[str, Any]] = {}
    origin_map: dict[int, set[str]] = {}

    def _merge_candidate(candidate: dict[str, Any], origin: str) -> None:
        object_id_value = candidate.get("object_id")
        if object_id_value is None:
            return
        object_id = int(object_id_value)
        if object_id not in candidate_map:
            candidate_map[object_id] = dict(candidate)
            origin_map[object_id] = set()
        else:
            # Preserve the object scatter fields already present, but add any
            # diagnostics computed by additional candidate channels.
            for key, value in candidate.items():
                if value is not None or key not in candidate_map[object_id]:
                    candidate_map[object_id][key] = value
        origin_map[object_id].add(origin)

    for candidate in rms_candidates:
        _merge_candidate(candidate, "rms_excess")

    intrinsic_candidates: list[dict[str, Any]] = []
    low_amplitude_intrinsic_candidates: list[dict[str, Any]] = []
    if enable_intrinsic_variability_candidates:
        intrinsic_candidates = _intrinsic_variability_candidate_rows(
            object_rows,
            min_measurements=min_measurements,
            min_intrinsic_rms=min_intrinsic_rms,
            min_rms_over_mag_err=min_rms_over_mag_err,
            min_mad_over_mag_err=min_mad_over_mag_err,
            intrinsic_mag_err_floor=intrinsic_mag_err_floor,
            min_robust_peak_to_peak=min_robust_peak_to_peak,
            bin_width=bin_width,
            min_objects_per_bin=min_objects_per_bin,
        )
        strong_intrinsic_ids = {int(candidate["object_id"]) for candidate in intrinsic_candidates if candidate.get("object_id") is not None}
        for candidate in intrinsic_candidates:
            _merge_candidate(candidate, "intrinsic_variability")

        if enable_low_amplitude_intrinsic_candidates:
            low_amplitude_intrinsic_candidates = _intrinsic_variability_candidate_rows(
                object_rows,
                min_measurements=max(int(min_measurements), int(low_amplitude_min_measurements)),
                min_intrinsic_rms=low_amplitude_min_intrinsic_rms,
                min_rms_over_mag_err=low_amplitude_min_rms_over_mag_err,
                min_mad_over_mag_err=low_amplitude_min_mad_over_mag_err,
                intrinsic_mag_err_floor=intrinsic_mag_err_floor,
                min_robust_peak_to_peak=low_amplitude_min_robust_peak_to_peak,
                bin_width=bin_width,
                min_objects_per_bin=min_objects_per_bin,
            )
            for candidate in low_amplitude_intrinsic_candidates:
                object_id_value = candidate.get("object_id")
                if object_id_value is not None and int(object_id_value) in strong_intrinsic_ids:
                    continue
                _merge_candidate(candidate, "low_amplitude_intrinsic")

    target_ids = {int(v) for v in (target_object_ids or []) if v is not None}
    if target_ids:
        forced_targets = _forced_target_candidate_rows(
            object_rows,
            target_ids=target_ids - set(candidate_map),
            bin_width=bin_width,
            min_objects_per_bin=min_objects_per_bin,
        )
        for candidate in forced_targets:
            _merge_candidate(candidate, "target")

    raw_candidates = list(candidate_map.values())
    for candidate in raw_candidates:
        object_id = int(candidate["object_id"])
        origins = origin_map.get(object_id, set())
        if object_id in target_ids:
            origins.add("target")
        ordered_origins = [
            name
            for name in ("rms_excess", "intrinsic_variability", "low_amplitude_intrinsic", "target")
            if name in origins
        ]
        candidate["candidate_origin"] = ",".join(ordered_origins)
        candidate["is_target"] = object_id in target_ids
        if candidate["is_target"]:
            if len(ordered_origins) == 1 and ordered_origins[0] == "target":
                candidate["target_reason"] = "forced_field_target"
            else:
                candidate["target_reason"] = "target_object_also_auto_candidate"
        else:
            candidate["target_reason"] = None

    qc_map = load_object_qc_map(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=qc_algorithm,
    )

    rows: list[dict[str, Any]] = []
    reason_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()

    for candidate in raw_candidates:
        object_id = candidate.get("object_id")
        qc = qc_map.get(object_id)
        enriched = _enrich_candidate(candidate, qc)
        reasons = _candidate_rejection_reasons(
            enriched,
            qc,
            exclude_flagged=exclude_flagged,
            exclude_crowded=exclude_crowded,
            exclude_edge=exclude_edge,
            exclude_saturated=exclude_saturated,
            exclude_catalog_outliers=exclude_catalog_outliers,
            reject_aperture_unstable=reject_aperture_unstable,
            require_detection_support=require_detection_support,
            min_nearest_neighbor_arcsec=min_nearest_neighbor_arcsec,
            max_mag_median=max_mag_median,
            max_ref_mag_delta=max_ref_mag_delta,
        )
        status = "ACCEPTED" if not reasons else "REJECTED"
        enriched["candidate_status"] = status
        enriched["rejection_reasons"] = ",".join(reasons)
        enriched["n_rejection_reasons"] = len(reasons)
        rows.append(enriched)
        status_counter[status] += 1
        for reason in reasons:
            reason_counter[reason] += 1

    rows.sort(
        key=lambda d: (
            0 if d.get("candidate_status") == "ACCEPTED" else 1,
            0 if d.get("is_target") else 1,
            -(float(d.get("rms_excess") or 0.0)),
            -(float(d.get("mag_rms") or 0.0)),
        )
    )
    if limit is not None:
        rows = rows[: int(limit)]

    summary = {
        "calibration_name": calibration_name,
        "catalog_name": catalog_name,
        "instrument_unit_id": instrument_unit_id,
        "objects_used": len(object_rows),
        "raw_candidates": sum(1 for row in raw_candidates if _has_auto_origin(row)),
        "rms_excess_candidates": sum(1 for row in raw_candidates if _has_origin(row, "rms_excess")),
        "intrinsic_variability_candidates": sum(1 for row in raw_candidates if _has_origin(row, "intrinsic_variability")),
        "low_amplitude_intrinsic_candidates": sum(1 for row in raw_candidates if _has_origin(row, "low_amplitude_intrinsic")),
        "intrinsic_only_candidates": sum(1 for row in raw_candidates if _has_any_intrinsic_origin(row) and not _has_origin(row, "rms_excess")),
        "low_amplitude_only_candidates": sum(1 for row in raw_candidates if _has_origin(row, "low_amplitude_intrinsic") and not _has_origin(row, "rms_excess") and not _has_origin(row, "intrinsic_variability")),
        "target_candidates": sum(1 for row in raw_candidates if row.get("is_target")),
        "forced_target_candidates": sum(1 for row in raw_candidates if row.get("candidate_origin") == "target"),
        "accepted": int(status_counter.get("ACCEPTED", 0)),
        "rejected": int(status_counter.get("REJECTED", 0)),
        "rejection_counts": dict(sorted(reason_counter.items())),
        "filters": {
            "min_measurements": min_measurements,
            "bin_width": bin_width,
            "min_objects_per_bin": min_objects_per_bin,
            "min_rms": min_rms,
            "min_excess": min_excess,
            "exclude_flagged": exclude_flagged,
            "exclude_crowded": exclude_crowded,
            "exclude_edge": exclude_edge,
            "exclude_saturated": exclude_saturated,
            "exclude_catalog_outliers": exclude_catalog_outliers,
            "reject_aperture_unstable": reject_aperture_unstable,
            "require_detection_support": require_detection_support,
            "min_nearest_neighbor_arcsec": min_nearest_neighbor_arcsec,
            "max_mag_median": max_mag_median,
            "max_ref_mag_delta": max_ref_mag_delta,
            "qc_algorithm": qc_algorithm,
            "enable_intrinsic_variability_candidates": enable_intrinsic_variability_candidates,
            "min_intrinsic_rms": min_intrinsic_rms,
            "min_rms_over_mag_err": min_rms_over_mag_err,
            "min_mad_over_mag_err": min_mad_over_mag_err,
            "intrinsic_mag_err_floor": intrinsic_mag_err_floor,
            "min_robust_peak_to_peak": min_robust_peak_to_peak,
            "enable_low_amplitude_intrinsic_candidates": enable_low_amplitude_intrinsic_candidates,
            "low_amplitude_min_measurements": low_amplitude_min_measurements,
            "low_amplitude_min_intrinsic_rms": low_amplitude_min_intrinsic_rms,
            "low_amplitude_min_rms_over_mag_err": low_amplitude_min_rms_over_mag_err,
            "low_amplitude_min_mad_over_mag_err": low_amplitude_min_mad_over_mag_err,
            "low_amplitude_min_robust_peak_to_peak": low_amplitude_min_robust_peak_to_peak,
        },
    }
    return CandidateTriageResult(rows=rows, summary=summary)



def _has_origin(row: dict[str, Any], origin: str) -> bool:
    value = row.get("candidate_origin")
    if not value:
        return False
    return origin in {part.strip() for part in str(value).split(",") if part.strip()}


def _has_any_intrinsic_origin(row: dict[str, Any]) -> bool:
    return _has_origin(row, "intrinsic_variability") or _has_origin(row, "low_amplitude_intrinsic")


def _has_auto_origin(row: dict[str, Any]) -> bool:
    return _has_origin(row, "rms_excess") or _has_any_intrinsic_origin(row)


def _intrinsic_variability_candidate_rows(
    object_rows,
    *,
    min_measurements: int,
    min_intrinsic_rms: float | None,
    min_rms_over_mag_err: float | None,
    min_mad_over_mag_err: float | None,
    intrinsic_mag_err_floor: float | None,
    min_robust_peak_to_peak: float | None,
    bin_width: float,
    min_objects_per_bin: int,
) -> list[dict[str, Any]]:
    """Return objects significant relative to their own formal errors.

    The RMS-excess channel finds field outliers relative to neighboring objects in
    the RMS--magnitude diagram. This companion channel catches moderate-amplitude
    but high-S/N variables: objects whose scatter is not exceptional for the field
    but is clearly larger than their per-point uncertainties.
    """

    rows = [row for row in object_rows if int(row.n_measurements) >= int(min_measurements)]
    rms_bins = build_rms_mag_summary(
        rows,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
    )
    med_by_bin = {
        int(math.floor(float(s["mag_bin_center"]) / float(bin_width))): s.get("rms_median")
        for s in rms_bins
    }

    out: list[dict[str, Any]] = []
    for row in rows:
        if row.mag_rms is None or row.mag_err_median is None:
            continue
        if not _is_finite_number(row.mag_rms) or not _is_finite_number(row.mag_err_median):
            continue
        mag_rms = float(row.mag_rms)
        mag_mad = float(row.mag_mad) if row.mag_mad is not None and _is_finite_number(row.mag_mad) else None
        mag_err = float(row.mag_err_median)
        if mag_err <= 0:
            continue
        effective_mag_err = mag_err
        if intrinsic_mag_err_floor is not None and _is_finite_number(intrinsic_mag_err_floor):
            effective_mag_err = max(effective_mag_err, float(intrinsic_mag_err_floor))
        if effective_mag_err <= 0:
            continue
        if min_intrinsic_rms is not None and mag_rms < float(min_intrinsic_rms):
            continue

        robust_peak_to_peak = (
            float(row.robust_peak_to_peak)
            if getattr(row, "robust_peak_to_peak", None) is not None
            and _is_finite_number(getattr(row, "robust_peak_to_peak", None))
            else None
        )
        if min_robust_peak_to_peak is not None:
            if robust_peak_to_peak is None or robust_peak_to_peak < float(min_robust_peak_to_peak):
                continue

        rms_over_err = mag_rms / effective_mag_err
        mad_over_err = (mag_mad / effective_mag_err) if mag_mad is not None else None
        if min_rms_over_mag_err is not None and rms_over_err < float(min_rms_over_mag_err):
            continue
        if min_mad_over_mag_err is not None:
            if mad_over_err is None or mad_over_err < float(min_mad_over_mag_err):
                continue

        d = row.to_dict()
        baseline = None
        rms_excess = None
        if row.mag_median is not None and _is_finite_number(row.mag_median):
            idx = int(math.floor(float(row.mag_median) / float(bin_width)))
            baseline = med_by_bin.get(idx)
            if baseline is not None and _is_finite_number(baseline) and float(baseline) > 0:
                rms_excess = mag_rms / float(baseline)
        d["baseline_rms"] = baseline
        d["rms_excess"] = rms_excess
        d["rms_over_mag_err"] = rms_over_err
        d["mad_over_mag_err"] = mad_over_err
        d["effective_mag_err_median"] = effective_mag_err
        d["robust_peak_to_peak"] = robust_peak_to_peak
        d["intrinsic_variability_score"] = min(
            rms_over_err,
            mad_over_err if mad_over_err is not None else rms_over_err,
        )
        out.append(d)
    out.sort(
        key=lambda d: (
            -(float(d.get("intrinsic_variability_score") or 0.0)),
            -(float(d.get("rms_over_mag_err") or 0.0)),
            -(float(d.get("mag_rms") or 0.0)),
        )
    )
    return out


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False

def _forced_target_candidate_rows(
    object_rows,
    *,
    target_ids: set[int],
    bin_width: float,
    min_objects_per_bin: int,
) -> list[dict[str, Any]]:
    if not target_ids:
        return []
    rms_bins = build_rms_mag_summary(
        object_rows,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
    )
    med_by_bin = {
        int(math.floor(float(s["mag_bin_center"]) / float(bin_width))): s.get("rms_median")
        for s in rms_bins
    }
    rows_by_id = {int(row.object_id): row for row in object_rows}
    out: list[dict[str, Any]] = []
    for obj_id in sorted(target_ids):
        row = rows_by_id.get(int(obj_id))
        if row is None:
            continue
        d = row.to_dict()
        baseline = None
        rms_excess = None
        if row.mag_median is not None and row.mag_rms is not None:
            try:
                idx = int(math.floor(float(row.mag_median) / float(bin_width)))
                baseline = med_by_bin.get(idx)
                if baseline is not None and float(baseline) > 0:
                    rms_excess = float(row.mag_rms) / float(baseline)
            except Exception:
                pass
        d["baseline_rms"] = baseline
        d["rms_excess"] = rms_excess
        d["is_target"] = True
        d["candidate_origin"] = "target"
        d["target_reason"] = "forced_field_target"
        out.append(d)
    return out


def export_candidate_triage_csv(rows: list[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = candidate_triage_columns()
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def candidate_triage_columns() -> list[str]:
    return [
        "candidate_status",
        "rejection_reasons",
        "candidate_origin",
        "is_target",
        "target_reason",
        "object_id",
        "catalog_name",
        "source_id",
        "ra_deg",
        "dec_deg",
        "n_measurements",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "mag_p10",
        "mag_p90",
        "robust_peak_to_peak",
        "baseline_rms",
        "rms_excess",
        "rms_over_mag_err",
        "mad_over_mag_err",
        "effective_mag_err_median",
        "intrinsic_variability_score",
        "mag_err_median",
        "ref_mag",
        "color",
        "nearest_neighbor_arcsec",
        "dynamic_blend_arcsec",
        "dynamic_crowded_arcsec",
        "separation_index",
        "crowding_index",
        "separation_class",
        "edge_distance_pix_min",
        "detection_fraction",
        "detection_frame_fraction",
        "n_detection_matches",
        "n_detection_match_frames",
        "n_detection_matches_raw",
        "n_extra_detection_matches",
        "n_multiple_detection_frames",
        "multiple_detection_frame_ratio",
        "det_sep_median_arcsec",
        "det_snr_median",
        "det_snr_max",
        "mag_ref_delta",
        "abs_mag_ref_delta",
        "aperture_min_corr_with_ref",
        "aperture_max_rms_delta_vs_ref",
        "aperture_max_abs_median_delta_vs_ref",
        "object_qc_flags",
    ]


def _enrich_candidate(candidate: dict[str, Any], qc: ObjectQc | None) -> dict[str, Any]:
    row = dict(candidate)
    flags = (qc.flags or {}) if qc is not None else {}
    details = (qc.details or {}) if qc is not None else {}
    det = details.get("detection_support", {}) if isinstance(details, dict) else {}
    phot = details.get("photometric_consistency", {}) if isinstance(details, dict) else {}
    ap = details.get("aperture_stability", {}) if isinstance(details, dict) else {}
    dyn = details.get("dynamic_separation", {}) if isinstance(details, dict) else {}

    row["object_qc_flags"] = ",".join(sorted(k for k, v in flags.items() if v))
    row["nearest_neighbor_arcsec"] = getattr(qc, "nearest_neighbor_arcsec", None) if qc is not None else None
    row["dynamic_blend_arcsec"] = dyn.get("dynamic_blend_arcsec")
    row["dynamic_crowded_arcsec"] = dyn.get("dynamic_crowded_arcsec")
    row["separation_index"] = dyn.get("separation_index")
    row["crowding_index"] = dyn.get("crowding_index")
    row["separation_class"] = dyn.get("separation_class")
    if row["separation_index"] is None and row["nearest_neighbor_arcsec"] is not None and row["dynamic_blend_arcsec"]:
        try:
            row["separation_index"] = float(row["nearest_neighbor_arcsec"]) / float(row["dynamic_blend_arcsec"])
        except Exception:
            pass
    if row["crowding_index"] is None and row["nearest_neighbor_arcsec"] is not None and row["dynamic_crowded_arcsec"]:
        try:
            row["crowding_index"] = float(row["nearest_neighbor_arcsec"]) / float(row["dynamic_crowded_arcsec"])
        except Exception:
            pass
    row["edge_distance_pix_min"] = getattr(qc, "edge_distance_pix_min", None) if qc is not None else None
    row["detection_fraction"] = det.get("detection_fraction")
    row["detection_frame_fraction"] = det.get("detection_frame_fraction", det.get("detection_fraction"))
    row["n_detection_matches"] = det.get("n_detection_matches")
    row["n_detection_match_frames"] = det.get("n_detection_match_frames", det.get("n_detection_matches"))
    row["n_detection_matches_raw"] = det.get("n_detection_matches_raw", det.get("n_detection_matches"))
    row["n_extra_detection_matches"] = det.get("n_extra_detection_matches")
    row["n_multiple_detection_frames"] = det.get("n_multiple_detection_frames")
    row["multiple_detection_frame_ratio"] = det.get("multiple_detection_frame_ratio")
    row["det_sep_median_arcsec"] = det.get("det_sep_median_arcsec")
    row["det_snr_median"] = det.get("det_snr_median")
    row["det_snr_max"] = det.get("det_snr_max")
    row["mag_ref_delta"] = phot.get("mag_ref_delta")
    row["aperture_min_corr_with_ref"] = ap.get("min_corr_with_ref")
    row["aperture_max_rms_delta_vs_ref"] = ap.get("max_rms_delta_vs_ref")
    row["aperture_max_abs_median_delta_vs_ref"] = ap.get("max_abs_median_delta_vs_ref")
    row["abs_mag_ref_delta"] = phot.get("abs_mag_ref_delta")
    return row


def _candidate_rejection_reasons(
    row: dict[str, Any],
    qc: ObjectQc | None,
    *,
    exclude_flagged: bool,
    exclude_crowded: bool,
    exclude_edge: bool,
    exclude_saturated: bool,
    exclude_catalog_outliers: bool,
    reject_aperture_unstable: bool,
    require_detection_support: bool,
    min_nearest_neighbor_arcsec: float | None,
    max_mag_median: float | None,
    max_ref_mag_delta: float | None,
) -> list[str]:
    reasons: list[str] = []
    flags = (qc.flags or {}) if qc is not None else {}

    if qc is None:
        reasons.append("NO_OBJECT_QC")
        if require_detection_support:
            reasons.append("NO_DETECTION_SUPPORT")
    else:
        if exclude_flagged and any(bool(v) for v in flags.values()):
            reasons.append("ANY_OBJECT_QC_FLAG")
        if exclude_crowded and (
            flags.get("CROWDED")
            or flags.get("BLENDED_NEIGHBOR")
            or flags.get("NEIGHBOR_WITHIN_3ARCSEC")
            or flags.get("NEIGHBOR_WITHIN_5ARCSEC")
            or flags.get("DYNAMIC_CROWDED")
            or flags.get("DYNAMIC_BLEND_RISK")
            or flags.get("POSSIBLE_BLEND")
        ):
            reasons.append("CROWDED_OR_BLEND")
        if exclude_edge and flags.get("EDGE"):
            reasons.append("EDGE")
        if exclude_saturated and flags.get("SATURATED_LIKE"):
            reasons.append("SATURATED_LIKE")
        if exclude_catalog_outliers and flags.get("LARGE_CATALOG_MAG_RESIDUAL"):
            reasons.append("CATALOG_MAG_RESIDUAL")
        if reject_aperture_unstable and flags.get("APERTURE_UNSTABLE"):
            reasons.append("APERTURE_UNSTABLE")
        if reject_aperture_unstable and flags.get("APERTURE_MEDIAN_SHIFT"):
            reasons.append("APERTURE_MEDIAN_SHIFT")
        if require_detection_support and (flags.get("NO_DETECTION_SUPPORT") or flags.get("WEAK_DETECTION_SUPPORT")):
            reasons.append("WEAK_OR_NO_DETECTION_SUPPORT")

    if min_nearest_neighbor_arcsec is not None:
        nn = row.get("nearest_neighbor_arcsec")
        if nn is None or float(nn) < float(min_nearest_neighbor_arcsec):
            reasons.append("NEAREST_NEIGHBOR_TOO_CLOSE")

    if max_mag_median is not None:
        mag = row.get("mag_median")
        if mag is not None and float(mag) > float(max_mag_median):
            reasons.append("TOO_FAINT")

    if max_ref_mag_delta is not None:
        abs_delta = row.get("abs_mag_ref_delta")
        if abs_delta is None:
            reasons.append("NO_REF_MAG_DELTA")
        elif float(abs_delta) > float(max_ref_mag_delta):
            reasons.append("REF_MAG_DELTA_TOO_LARGE")

    # Remove duplicates while preserving order.
    out: list[str] = []
    seen = set()
    for reason in reasons:
        if reason not in seen:
            out.append(reason)
            seen.add(reason)
    return out
