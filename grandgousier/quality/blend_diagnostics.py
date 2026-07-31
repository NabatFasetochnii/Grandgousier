from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import CalibrationVersion, InstrumentUnit, ObjectQc


def build_blend_diagnostic_rows(
    session: Session,
    *,
    calibration_name: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    frame_ids: list[int] | None = None,
    algorithm: str = "object_qc_v1",
    only_flagged: bool = False,
    only_possible_blends: bool = False,
    max_separation_index: float | None = None,
    min_mag_rms: float | None = None,
    sort: str = "risk",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Build a field-level blend/separation diagnostics table from object_qc."""

    stmt = select(ObjectQc, CalibrationVersion, InstrumentUnit).outerjoin(
        CalibrationVersion, CalibrationVersion.calibration_version_id == ObjectQc.calibration_version_id
    ).outerjoin(
        InstrumentUnit, InstrumentUnit.instrument_unit_id == ObjectQc.instrument_unit_id
    )
    if calibration_name:
        stmt = stmt.where(CalibrationVersion.name == calibration_name)
    if catalog_name:
        stmt = stmt.where(ObjectQc.catalog_name == catalog_name)
    if instrument_unit_id is not None:
        stmt = stmt.where(ObjectQc.instrument_unit_id == instrument_unit_id)
    if algorithm:
        stmt = stmt.where(ObjectQc.algorithm == algorithm)

    rows: list[dict[str, Any]] = []
    for qc, cv, iu in session.execute(stmt).all():
        row = _row_from_qc(qc, cv, iu)
        flags = row.get("object_qc_flags_set", set())
        if only_flagged and not flags:
            continue
        if only_possible_blends and not (
            row.get("possible_blend")
            or row.get("dynamic_crowded")
            or row.get("dynamic_blend_risk")
            or row.get("aperture_unstable")
            or row.get("aperture_median_shift")
        ):
            continue
        sep_index = row.get("separation_index")
        if max_separation_index is not None:
            if sep_index is None or float(sep_index) > float(max_separation_index):
                continue
        mag_rms = row.get("mag_rms")
        if min_mag_rms is not None:
            if mag_rms is None or float(mag_rms) < float(min_mag_rms):
                continue
        row.pop("object_qc_flags_set", None)
        rows.append(row)

    rows.sort(key=_sort_key(sort))
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def blend_diagnostic_columns() -> list[str]:
    return [
        "object_id",
        "catalog_name",
        "source_id",
        "calibration_name",
        "instrument_unit_id",
        "instrument",
        "filter_name",
        "ra_deg",
        "dec_deg",
        "mag_median",
        "mag_rms",
        "mag_mad",
        "nearest_neighbor_arcsec",
        "dynamic_blend_arcsec",
        "dynamic_crowded_arcsec",
        "separation_index",
        "crowding_index",
        "separation_class",
        "fwhm_arcsec_used",
        "aperture_radius_pix_used",
        "aperture_radius_arcsec_used",
        "pixel_scale_arcsec_used",
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
        "aperture_min_corr_with_ref",
        "aperture_max_rms_delta_vs_ref",
        "aperture_max_abs_median_delta_vs_ref",
        "dynamic_blend_risk",
        "dynamic_crowded",
        "possible_blend",
        "aperture_unstable",
        "aperture_median_shift",
        "edge",
        "saturated_like",
        "weak_detection_support",
        "object_qc_flags",
    ]


def export_blend_diagnostic_csv(rows: list[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = blend_diagnostic_columns()
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def summarize_blend_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    if not rows:
        return {
            "total": 0,
            "possible_blend": 0,
            "dynamic_crowded": 0,
            "dynamic_blend_risk": 0,
            "aperture_unstable": 0,
            "aperture_median_shift": 0,
            "multiple_detections_per_frame": 0,
        }
    return {
        "total": total,
        "possible_blend": sum(1 for r in rows if r.get("possible_blend")),
        "dynamic_crowded": sum(1 for r in rows if r.get("dynamic_crowded")),
        "dynamic_blend_risk": sum(1 for r in rows if r.get("dynamic_blend_risk")),
        "aperture_unstable": sum(1 for r in rows if r.get("aperture_unstable")),
        "aperture_median_shift": sum(1 for r in rows if r.get("aperture_median_shift")),
        "edge": sum(1 for r in rows if r.get("edge")),
        "weak_detection_support": sum(1 for r in rows if r.get("weak_detection_support")),
        "multiple_detections_per_frame": sum(1 for r in rows if _gt(r.get("n_extra_detection_matches"), 0)),
        "separation_index_lt_1": sum(1 for r in rows if _lt(r.get("separation_index"), 1.0)),
        "separation_index_lt_1p5": sum(1 for r in rows if _lt(r.get("separation_index"), 1.5)),
    }


def _row_from_qc(qc: ObjectQc, cv: CalibrationVersion | None, iu: InstrumentUnit | None) -> dict[str, Any]:
    flags = qc.flags or {}
    details = qc.details or {}
    dyn = details.get("dynamic_separation", {}) if isinstance(details, dict) else {}
    det = details.get("detection_support", {}) if isinstance(details, dict) else {}
    ap = details.get("aperture_stability", {}) if isinstance(details, dict) else {}

    dynamic_blend = dyn.get("dynamic_blend_arcsec")
    dynamic_crowded = dyn.get("dynamic_crowded_arcsec")
    nearest = qc.nearest_neighbor_arcsec
    separation_index = dyn.get("separation_index")
    crowding_index = dyn.get("crowding_index")
    if separation_index is None and _positive(dynamic_blend) and nearest is not None:
        separation_index = float(nearest) / float(dynamic_blend)
    if crowding_index is None and _positive(dynamic_crowded) and nearest is not None:
        crowding_index = float(nearest) / float(dynamic_crowded)

    source_id = details.get("source_id") if isinstance(details, dict) else None
    active_flags = sorted(k for k, v in flags.items() if v)
    return {
        "object_id": qc.object_id,
        "catalog_name": qc.catalog_name,
        "source_id": source_id,
        "calibration_name": getattr(cv, "name", None),
        "instrument_unit_id": qc.instrument_unit_id,
        "instrument": getattr(iu, "instrument", None),
        "filter_name": getattr(iu, "filter_name", None),
        "ra_deg": details.get("ra_deg") if isinstance(details, dict) else None,
        "dec_deg": details.get("dec_deg") if isinstance(details, dict) else None,
        "mag_median": qc.mag_median,
        "mag_rms": qc.mag_rms,
        "mag_mad": qc.mag_mad,
        "nearest_neighbor_arcsec": nearest,
        "dynamic_blend_arcsec": dynamic_blend,
        "dynamic_crowded_arcsec": dynamic_crowded,
        "separation_index": separation_index,
        "crowding_index": crowding_index,
        "separation_class": dyn.get("separation_class"),
        "fwhm_arcsec_used": dyn.get("fwhm_arcsec_median"),
        "aperture_radius_pix_used": dyn.get("aperture_radius_pix_median"),
        "aperture_radius_arcsec_used": dyn.get("aperture_radius_arcsec_median"),
        "pixel_scale_arcsec_used": dyn.get("pixel_scale_arcsec_median"),
        "detection_fraction": det.get("detection_fraction"),
        "detection_frame_fraction": det.get("detection_frame_fraction", det.get("detection_fraction")),
        "n_detection_matches": det.get("n_detection_matches"),
        "n_detection_match_frames": det.get("n_detection_match_frames", det.get("n_detection_matches")),
        "n_detection_matches_raw": det.get("n_detection_matches_raw", det.get("n_detection_matches")),
        "n_extra_detection_matches": det.get("n_extra_detection_matches"),
        "n_multiple_detection_frames": det.get("n_multiple_detection_frames"),
        "multiple_detection_frame_ratio": det.get("multiple_detection_frame_ratio"),
        "det_sep_median_arcsec": det.get("det_sep_median_arcsec"),
        "det_snr_median": det.get("det_snr_median"),
        "aperture_min_corr_with_ref": ap.get("min_corr_with_ref"),
        "aperture_max_rms_delta_vs_ref": ap.get("max_rms_delta_vs_ref"),
        "aperture_max_abs_median_delta_vs_ref": ap.get("max_abs_median_delta_vs_ref"),
        "dynamic_blend_risk": bool(flags.get("DYNAMIC_BLEND_RISK")),
        "dynamic_crowded": bool(flags.get("DYNAMIC_CROWDED")),
        "possible_blend": bool(flags.get("POSSIBLE_BLEND")),
        "aperture_unstable": bool(flags.get("APERTURE_UNSTABLE")),
        "aperture_median_shift": bool(flags.get("APERTURE_MEDIAN_SHIFT")),
        "edge": bool(flags.get("EDGE")),
        "saturated_like": bool(flags.get("SATURATED_LIKE")),
        "weak_detection_support": bool(flags.get("WEAK_DETECTION_SUPPORT") or flags.get("NO_DETECTION_SUPPORT")),
        "object_qc_flags": ",".join(active_flags),
        "object_qc_flags_set": set(active_flags),
    }


def _sort_key(sort: str):
    if sort == "separation_index":
        return lambda r: (r.get("separation_index") is None, r.get("separation_index") if r.get("separation_index") is not None else 999.0, -(r.get("mag_rms") or 0.0))
    if sort == "mag_rms":
        return lambda r: (-(r.get("mag_rms") or 0.0), r.get("separation_index") is None, r.get("separation_index") or 999.0)
    if sort == "source_id":
        return lambda r: (str(r.get("source_id") or ""), r.get("object_id") or 0)
    # risk: most suspicious blend/systematic candidates first.
    return lambda r: (
        0 if (r.get("possible_blend") or r.get("aperture_unstable") or r.get("dynamic_crowded") or r.get("dynamic_blend_risk")) else 1,
        r.get("separation_index") is None,
        r.get("separation_index") if r.get("separation_index") is not None else 999.0,
        -(r.get("mag_rms") or 0.0),
    )


def _positive(value) -> bool:
    try:
        return float(value) > 0
    except Exception:
        return False


def _lt(value, threshold: float) -> bool:
    try:
        return float(value) < float(threshold)
    except Exception:
        return False


def _gt(value, threshold: float) -> bool:
    try:
        return float(value) > float(threshold)
    except Exception:
        return False
