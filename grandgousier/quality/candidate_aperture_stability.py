from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from grandgousier.quality.aperture_stability import compute_aperture_stability_qc
from grandgousier.quality.aperture_sweep import run_aperture_radius_sweep
from grandgousier.quality.candidate_triage import build_candidate_triage


@dataclass
class CandidateApertureStabilityResult:
    calibration_name: str
    catalog_name: str | None
    instrument_unit_id: int | None
    rows: list[dict[str, Any]]
    summary: dict[str, Any]


def compute_candidate_aperture_stability_qc(
    session: Session,
    *,
    calibration_name: str,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    radii: list[float],
    frame_ids: list[int] | None = None,
    sweep_calibration_prefix: str | None = None,
    annulus_inner_pix: float = 8.0,
    annulus_outer_pix: float = 12.0,
    mag_key: str = "G",
    min_measurements: int = 10,
    bin_width: float = 0.5,
    min_objects_per_bin: int = 3,
    min_rms: float | None = None,
    min_excess: float = 2.0,
    max_mag_median: float | None = 22.5,
    max_ref_mag_delta: float | None = 2.0,
    qc_algorithm: str = "object_qc_v1",
    max_candidates: int | None = None,
    min_stable_corr: float = 0.7,
    max_stable_rms_delta: float = 0.03,
    max_median_shift: float = 0.2,
    possible_blend_neighbor_arcsec: float | None = None,
    min_ref_stars_per_frame: int = 30,
    max_mag_err: float | None = 0.08,
    max_zp_mad: float | None = None,
    clip_sigma: float = 3.0,
    n_iterations: int = 3,
    min_ref_measurements: int = 10,
    ensemble_min_ref_stars_per_frame: int = 30,
    max_ref_rms: float | None = 0.08,
    max_ref_mad: float | None = 0.05,
    skip_sweep_preparation: bool = False,
    continue_on_error: bool = True,
    dry_run: bool = False,
) -> CandidateApertureStabilityResult:
    """Run aperture-stability QC for all raw high-scatter candidates.

    The candidate list is produced by the same RMS-excess logic used by the field
    triage report. Aperture-radius photometry/calibration is prepared once for the
    full field and then each raw candidate is evaluated against those per-radius
    calibration versions. This keeps the batch step usable inside the field pipeline
    without repeating a full photometry/calibration cycle for every candidate.
    """

    if not radii:
        raise ValueError("At least one aperture radius is required")

    radii = [float(r) for r in radii]
    if sweep_calibration_prefix is None:
        safe = str(calibration_name).replace(" ", "_").replace("/", "_").replace("\\", "_")
        sweep_calibration_prefix = f"{safe}_apstab"

    triage = build_candidate_triage(
        session,
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=frame_ids,
        min_measurements=min_measurements,
        bin_width=bin_width,
        min_objects_per_bin=min_objects_per_bin,
        min_rms=min_rms,
        min_excess=min_excess,
        exclude_crowded=False,
        exclude_edge=False,
        exclude_saturated=False,
        exclude_catalog_outliers=False,
        reject_aperture_unstable=False,
        require_detection_support=False,
        max_mag_median=max_mag_median,
        max_ref_mag_delta=max_ref_mag_delta,
        qc_algorithm=qc_algorithm,
        limit=None,
    )
    candidates = list(triage.rows)
    if max_candidates is not None:
        candidates = candidates[: int(max_candidates)]

    rows: list[dict[str, Any]] = []
    prepared = False
    preparation_error: str | None = None

    if candidates and not skip_sweep_preparation:
        first = candidates[0]
        try:
            run_aperture_radius_sweep(
                session,
                catalog_name=catalog_name or str(first.get("catalog_name") or ""),
                source_id=str(first.get("source_id")) if first.get("source_id") is not None else None,
                object_id=int(first["object_id"]) if first.get("source_id") is None and first.get("object_id") is not None else None,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
                radii=radii,
                calibration_prefix=sweep_calibration_prefix,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                mag_key=mag_key,
                min_ref_stars_per_frame=min_ref_stars_per_frame,
                max_mag_err=max_mag_err,
                max_zp_mad=max_zp_mad,
                clip_sigma=clip_sigma,
                n_iterations=n_iterations,
                min_ref_measurements=min_ref_measurements,
                ensemble_min_ref_stars_per_frame=ensemble_min_ref_stars_per_frame,
                max_ref_rms=max_ref_rms,
                max_ref_mad=max_ref_mad,
                skip_photometry=False,
                skip_calibration=False,
                dry_run=dry_run,
            )
            prepared = True
        except Exception as exc:  # noqa: BLE001 - batch result should carry the failed candidate context.
            preparation_error = f"{type(exc).__name__}: {exc}"
            if not continue_on_error:
                raise

    updated = 0
    aperture_unstable = 0
    aperture_median_shift = 0
    possible_blend = 0
    failed = 0

    for cand in candidates:
        row = {
            "object_id": cand.get("object_id"),
            "source_id": cand.get("source_id"),
            "candidate_status_before_aperture_qc": cand.get("candidate_status"),
            "rejection_reasons_before_aperture_qc": cand.get("rejection_reasons"),
            "mag_median": cand.get("mag_median"),
            "mag_rms": cand.get("mag_rms"),
            "rms_excess": cand.get("rms_excess"),
            "status": "NOT_RUN",
        }
        try:
            res = compute_aperture_stability_qc(
                session,
                calibration_name=calibration_name,
                catalog_name=catalog_name or cand.get("catalog_name"),
                source_id=str(cand.get("source_id")) if cand.get("source_id") is not None else None,
                object_id=int(cand["object_id"]) if cand.get("source_id") is None and cand.get("object_id") is not None else None,
                instrument_unit_id=instrument_unit_id,
                frame_ids=frame_ids,
                radii=radii,
                sweep_calibration_prefix=sweep_calibration_prefix,
                annulus_inner_pix=annulus_inner_pix,
                annulus_outer_pix=annulus_outer_pix,
                mag_key=mag_key,
                algorithm=qc_algorithm,
                min_stable_corr=min_stable_corr,
                max_stable_rms_delta=max_stable_rms_delta,
                max_median_shift=max_median_shift,
                possible_blend_neighbor_arcsec=possible_blend_neighbor_arcsec,
                dry_run=dry_run,
            )
            m = res.metrics or {}
            row.update(
                {
                    "status": "DONE",
                    "updated": res.updated,
                    "aperture_unstable": res.aperture_unstable,
                    "aperture_median_shift": res.aperture_median_shift,
                    "possible_blend": res.possible_blend,
                    "min_corr_with_ref": m.get("min_corr_with_ref"),
                    "max_rms_delta_vs_ref": m.get("max_rms_delta_vs_ref"),
                    "max_abs_median_delta_vs_ref": m.get("max_abs_median_delta_vs_ref"),
                }
            )
            updated += int(res.updated)
            aperture_unstable += int(res.aperture_unstable)
            aperture_median_shift += int(res.aperture_median_shift)
            possible_blend += int(res.possible_blend)
        except Exception as exc:  # noqa: BLE001 - continue to diagnose all candidates when requested.
            failed += 1
            row.update({"status": "FAILED", "error_message": f"{type(exc).__name__}: {exc}"})
            if not continue_on_error:
                rows.append(row)
                raise
        rows.append(row)

    summary = {
        "calibration_name": calibration_name,
        "catalog_name": catalog_name,
        "instrument_unit_id": instrument_unit_id,
        "raw_candidates": int(triage.summary.get("raw_candidates", len(candidates))),
        "selected_candidates": len(candidates),
        "checked_candidates": len(rows),
        "updated": updated,
        "aperture_unstable": aperture_unstable,
        "aperture_median_shift": aperture_median_shift,
        "possible_blend": possible_blend,
        "failed": failed,
        "prepared_sweep_calibrations": int(bool(prepared)),
        "preparation_error": preparation_error,
        "sweep_calibration_prefix": sweep_calibration_prefix,
        "radii": radii,
        "dry_run": dry_run,
    }
    return CandidateApertureStabilityResult(
        calibration_name=calibration_name,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        rows=rows,
        summary=summary,
    )


def export_candidate_aperture_stability_csv(rows: list[dict[str, Any]], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = candidate_aperture_stability_columns()
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def candidate_aperture_stability_columns() -> list[str]:
    return [
        "status",
        "object_id",
        "source_id",
        "candidate_status_before_aperture_qc",
        "rejection_reasons_before_aperture_qc",
        "mag_median",
        "mag_rms",
        "rms_excess",
        "updated",
        "aperture_unstable",
        "aperture_median_shift",
        "possible_blend",
        "min_corr_with_ref",
        "max_rms_delta_vs_ref",
        "max_abs_median_delta_vs_ref",
        "error_message",
    ]
