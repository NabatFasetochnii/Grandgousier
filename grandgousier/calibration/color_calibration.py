from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.calibration.provenance import set_source_calibration
from grandgousier.db.models import CalibrationVersion, Measurement


_SUPPORTED_BANDS = {"g", "r", "i", "z", "y", "b", "v", "j", "h", "k", "bp", "rp", "g_gaia"}
_REF_MAG_COLUMNS = {band: f"mag_{band}" for band in _SUPPORTED_BANDS}

_DEFAULT_EXCLUDED_OBJECT_QC_FLAGS = (
    "APERTURE_MEDIAN_SHIFT",
    "APERTURE_UNSTABLE",
    "BLENDED_NEIGHBOR",
    "CROWDED",
    "DYNAMIC_BLEND_RISK",
    "DYNAMIC_CROWDED",
    "EDGE",
    "FAINT_NOISY",
    "HIGH_RMS",
    "NO_DETECTION_SUPPORT",
    "POSSIBLE_BLEND",
    "SATURATED_LIKE",
    "WEAK_DETECTION_SUPPORT",
)


@dataclass
class ColorCalibrationDiagnosticsResult:
    calibration_name: str
    series_id: int | None
    identity_catalog_name: str
    reference_catalog_name: str
    reference_band: str
    color_index: str
    out_dir: str | None = None

    n_measurements_input: int = 0
    n_measurements_with_reference: int = 0
    n_measurements_after_basic_filters: int = 0
    n_used_measurements: int = 0
    n_used_objects: int = 0
    n_used_frames: int = 0
    n_frames_with_zp: int = 0
    n_rejected_low_object_measurements: int = 0
    n_rejected_low_frame_stars: int = 0
    n_clipped_measurements: int = 0

    n_reference_objects: int = 0
    n_calibrator_candidate_objects: int = 0
    n_excluded_objects: int = 0
    n_excluded_by_object_id: int = 0
    n_excluded_by_object_qc: int = 0
    n_excluded_by_reference_mag: int = 0
    n_excluded_by_color: int = 0
    n_excluded_by_low_object_measurements: int = 0

    include_flagged_calibrators: bool = False
    excluded_object_qc_flags: list[str] = field(default_factory=list)
    explicit_exclude_object_ids: list[int] = field(default_factory=list)
    min_reference_mag: float | None = None
    max_reference_mag: float | None = None
    min_color: float | None = None
    max_color: float | None = None

    color0: float | None = None
    color_min: float | None = None
    color_max: float | None = None

    current_residual_median: float | None = None
    current_residual_mad: float | None = None
    current_residual_rms: float | None = None
    current_color_intercept: float | None = None
    current_color_slope: float | None = None
    current_residual_after_color_median: float | None = None
    current_residual_after_color_mad: float | None = None
    current_residual_after_color_rms: float | None = None

    frame_model_color_term: float | None = None
    frame_model_residual_median: float | None = None
    frame_model_residual_mad: float | None = None
    frame_model_residual_rms: float | None = None
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None

    summary_json: str | None = None
    stars_csv: str | None = None
    frames_csv: str | None = None
    frame_zp_by_frame: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ColorCalibrationApplyResult:
    source_calibration_name: str
    target_calibration_name: str
    series_id: int | None
    identity_catalog_name: str
    reference_catalog_name: str
    reference_band: str
    color_index: str
    source_calibration_version_id: int | None = None
    target_calibration_version_id: int | None = None
    dry_run: bool = False

    n_measurements_input: int = 0
    n_used_calibrator_measurements: int = 0
    n_used_calibrator_objects: int = 0
    n_frames_with_zp: int = 0
    checked_measurements: int = 0
    calibrated: int = 0
    inserted_or_updated: int = 0
    deleted_existing: int = 0
    skipped_no_mag_inst: int = 0
    skipped_no_color: int = 0
    skipped_no_frame_zp: int = 0
    skipped_saturated: int = 0
    skipped_nonfinite: int = 0
    skipped_no_reference: int = 0

    color0: float | None = None
    color_min: float | None = None
    color_max: float | None = None
    color_term: float | None = None
    frame_model_residual_rms: float | None = None
    frame_model_residual_mad: float | None = None
    frame_zp_median: float | None = None
    frame_zp_mad: float | None = None
    output_mag_median: float | None = None
    output_mag_mad: float | None = None

    diagnostics_summary_json: str | None = None
    diagnostics_stars_csv: str | None = None
    diagnostics_frames_csv: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_color_calibration_diagnostics(
    session: Session,
    *,
    calibration_name: str,
    identity_catalog_name: str = "gaia_dr3_master",
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    series_id: int | None = None,
    out_dir: str | Path | None = None,
    min_measurements_per_object: int = 5,
    min_stars_per_frame: int = 20,
    max_measurement_mag_err: float | None = None,
    max_reference_mag_err: float | None = None,
    min_reference_mag: float | None = None,
    max_reference_mag: float | None = None,
    min_color: float | None = None,
    max_color: float | None = None,
    include_flagged_calibrators: bool = False,
    excluded_object_qc_flags: Iterable[str] | None = None,
    exclude_object_ids: Iterable[int] | None = None,
    clip_sigma: float = 4.0,
    n_iterations: int = 5,
    exclude_saturated: bool = True,
    limit: int | None = None,
) -> ColorCalibrationDiagnosticsResult:
    """Diagnose color-dependent photometric residuals without writing measurements.

    The command intentionally produces diagnostics only. It does not modify
    calibration_versions or measurements. Two complementary diagnostics are
    computed:

    1. Current calibrated magnitude residuals: median(mag_cal) - reference_mag
       per object, fitted as a linear function of color.
    2. A frame-zero-point + color-term model built from mag_inst:
       reference_mag - mag_inst = ZP_frame + C * (color - color0).
    """

    ref_band = normalize_reference_band(reference_band)
    color_key, color_bands = parse_color_index(color_index)
    if min_measurements_per_object < 1:
        raise ValueError("min_measurements_per_object must be >= 1")
    if min_stars_per_frame < 1:
        raise ValueError("min_stars_per_frame must be >= 1")
    if clip_sigma <= 0:
        raise ValueError("clip_sigma must be positive")
    if n_iterations < 1:
        raise ValueError("n_iterations must be >= 1")

    excluded_flags = tuple(
        sorted({_normalize_flag_name(f) for f in (excluded_object_qc_flags or _DEFAULT_EXCLUDED_OBJECT_QC_FLAGS) if str(f).strip()})
    )
    explicit_exclude_ids = tuple(sorted({int(v) for v in (exclude_object_ids or [])}))

    out_dir_path = Path(out_dir) if out_dir else None
    result = ColorCalibrationDiagnosticsResult(
        calibration_name=calibration_name,
        series_id=series_id,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        out_dir=str(out_dir_path) if out_dir_path else None,
        include_flagged_calibrators=bool(include_flagged_calibrators),
        excluded_object_qc_flags=list(excluded_flags),
        explicit_exclude_object_ids=list(explicit_exclude_ids),
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
    )

    rows = _load_color_measurement_rows(
        session,
        calibration_name=calibration_name,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        series_id=series_id,
        limit=limit,
    )
    result.n_measurements_input = len(rows)
    result.n_measurements_with_reference = len(rows)
    if not rows:
        result.warnings.append("No measurements matched the requested calibration/reference catalogs.")
        _write_outputs(result, [], [], out_dir_path)
        return result

    prepared = []
    for row in rows:
        flags = row.get("measurement_flags") if isinstance(row.get("measurement_flags"), dict) else {}
        if exclude_saturated and _is_saturated(flags):
            continue
        mag_inst = _to_float(row.get("mag_inst"))
        ref_mag = _to_float(row.get("reference_mag"))
        color = _extract_color(row, color_key=color_key, color_bands=color_bands)
        if not _is_finite(mag_inst) or not _is_finite(ref_mag) or not _is_finite(color):
            continue
        mag_err = _to_float(row.get("mag_err"))
        if max_measurement_mag_err is not None and _is_finite(mag_err) and float(mag_err) > float(max_measurement_mag_err):
            continue
        ref_mag_err = _reference_mag_error(row, ref_band)
        if max_reference_mag_err is not None and _is_finite(ref_mag_err) and float(ref_mag_err) > float(max_reference_mag_err):
            continue
        d = dict(row)
        d["mag_inst"] = float(mag_inst)
        d["reference_mag"] = float(ref_mag)
        d["color_value"] = float(color)
        d["mag_cal"] = _to_float(row.get("mag_cal"))
        d["mag_err"] = mag_err
        d["reference_mag_err"] = ref_mag_err
        prepared.append(d)

    result.n_measurements_after_basic_filters = len(prepared)
    if not prepared:
        result.warnings.append("No measurements survived basic finite/error/saturation filters.")
        _write_outputs(result, [], [], out_dir_path)
        return result

    object_info = _build_object_filter_info(
        prepared,
        min_measurements_per_object=min_measurements_per_object,
        include_flagged_calibrators=include_flagged_calibrators,
        excluded_object_qc_flags=excluded_flags,
        explicit_exclude_object_ids=explicit_exclude_ids,
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
    )
    result.n_reference_objects = object_info["n_reference_objects"]
    result.n_excluded_objects = object_info["n_excluded_objects"]
    result.n_excluded_by_object_id = object_info["n_excluded_by_object_id"]
    result.n_excluded_by_object_qc = object_info["n_excluded_by_object_qc"]
    result.n_excluded_by_reference_mag = object_info["n_excluded_by_reference_mag"]
    result.n_excluded_by_color = object_info["n_excluded_by_color"]
    result.n_excluded_by_low_object_measurements = object_info["n_excluded_by_low_object_measurements"]

    candidate_object_ids = object_info["candidate_object_ids"]
    exclusion_reasons_by_object = object_info["exclusion_reasons_by_object"]
    used = [row for row in prepared if int(row["object_id"]) in candidate_object_ids]
    result.n_calibrator_candidate_objects = len(candidate_object_ids)
    result.n_rejected_low_object_measurements = object_info["n_measurements_excluded_by_low_object_measurements"]
    if not used:
        result.warnings.append("No objects survived calibrator QC/magnitude/color/min-measurement filters.")
        star_rows = _build_star_rows(prepared, color0=0.0, exclusion_reasons_by_object=exclusion_reasons_by_object)
        _write_outputs(result, star_rows, [], out_dir_path)
        return result

    result.color0 = _median(_unique_object_values(used, "object_id", "color_value"))
    colors = [float(r["color_value"]) for r in used]
    result.color_min = min(colors) if colors else None
    result.color_max = max(colors) if colors else None
    if result.color0 is None:
        result.warnings.append("Could not compute color0.")
        star_rows = _build_star_rows(prepared, color0=0.0, exclusion_reasons_by_object=exclusion_reasons_by_object)
        _write_outputs(result, star_rows, [], out_dir_path)
        return result

    star_rows = _build_star_rows(prepared, color0=float(result.color0), exclusion_reasons_by_object=exclusion_reasons_by_object)
    _compute_current_color_fit(star_rows, result)

    model = _fit_frame_color_model(
        used,
        color0=float(result.color0),
        min_stars_per_frame=min_stars_per_frame,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
    )
    result.frame_model_color_term = model["color_term"]
    result.n_used_measurements = model["n_used_measurements"]
    result.n_used_objects = model["n_used_objects"]
    result.n_used_frames = model["n_used_frames"]
    result.n_frames_with_zp = model["n_frames_with_zp"]
    result.n_rejected_low_frame_stars = model["n_rejected_low_frame_stars"]
    result.n_clipped_measurements = model["n_clipped_measurements"]
    result.frame_model_residual_median = model["residual_median"]
    result.frame_model_residual_mad = model["residual_mad"]
    result.frame_model_residual_rms = model["residual_rms"]
    result.frame_zp_median = model["frame_zp_median"]
    result.frame_zp_mad = model["frame_zp_mad"]

    frame_rows = model["frame_rows"]
    result.frame_zp_by_frame = {
        str(int(row["frame_id"])): float(row["frame_zp"])
        for row in frame_rows
        if row.get("frame_zp") is not None and _is_finite(row.get("frame_zp"))
    }
    # Add frame model residual summaries back to star rows.
    residuals_by_object: dict[int, list[float]] = {}
    for row in model["measurement_rows"]:
        if row.get("model_used") and _is_finite(row.get("frame_model_residual")):
            residuals_by_object.setdefault(int(row["object_id"]), []).append(float(row["frame_model_residual"]))
    for srow in star_rows:
        vals = residuals_by_object.get(int(srow["object_id"]), [])
        srow["frame_model_residual_median"] = _median(vals)
        srow["frame_model_residual_mad"] = _mad(vals)
        srow["frame_model_n_measurements"] = len(vals)
        if vals:
            srow["used_as_calibrator"] = True
        elif srow.get("calibrator_candidate") and not srow.get("exclusion_reason"):
            srow["exclusion_reason"] = "MODEL_CLIPPED_OR_NO_FRAME_ZP"
    if result.n_used_measurements == 0:
        result.warnings.append("Frame/color model has no usable measurements after frame-count/clipping filters.")
    _write_outputs(result, star_rows, frame_rows, out_dir_path)
    return result



def apply_color_calibration(
    session: Session,
    *,
    source_calibration_name: str,
    target_calibration_name: str,
    identity_catalog_name: str = "gaia_dr3_master",
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    series_id: int | None = None,
    out_dir: str | Path | None = None,
    min_measurements_per_object: int = 5,
    min_stars_per_frame: int = 20,
    max_measurement_mag_err: float | None = None,
    max_reference_mag_err: float | None = None,
    min_reference_mag: float | None = None,
    max_reference_mag: float | None = None,
    min_color: float | None = None,
    max_color: float | None = None,
    include_flagged_calibrators: bool = False,
    excluded_object_qc_flags: Iterable[str] | None = None,
    exclude_object_ids: Iterable[int] | None = None,
    clip_sigma: float = 4.0,
    n_iterations: int = 5,
    exclude_saturated: bool = True,
    limit: int | None = None,
    dry_run: bool = False,
) -> ColorCalibrationApplyResult:
    """Create a new CalibrationVersion with reference-band color calibration.

    The fit is derived with the same robust frame-ZP + color-term model used by
    compute_color_calibration_diagnostics. The source measurements are copied to
    a new calibration version and mag_cal is replaced by:

        mag_cal = mag_inst + frame_zp + C * (color - color0)

    Calibrator QC filters are used only to fit the model. The resulting model is
    applied to every selected source measurement that has finite mag_inst,
    reference color, and a frame ZP. Measurements that cannot be calibrated are
    skipped; the source calibration is left unchanged.
    """

    if not target_calibration_name:
        raise ValueError("target_calibration_name is required")
    if target_calibration_name == source_calibration_name:
        raise ValueError("target_calibration_name must differ from source_calibration_name")

    ref_band = normalize_reference_band(reference_band)
    color_key, color_bands = parse_color_index(color_index)
    result = ColorCalibrationApplyResult(
        source_calibration_name=source_calibration_name,
        target_calibration_name=target_calibration_name,
        series_id=series_id,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        dry_run=bool(dry_run),
    )

    source_cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_calibration_name))
    if source_cv is None:
        raise ValueError(f"Source calibration version not found: {source_calibration_name}")
    result.source_calibration_version_id = source_cv.calibration_version_id

    diag = compute_color_calibration_diagnostics(
        session,
        calibration_name=source_calibration_name,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        color_index=color_key,
        series_id=series_id,
        out_dir=out_dir,
        min_measurements_per_object=min_measurements_per_object,
        min_stars_per_frame=min_stars_per_frame,
        max_measurement_mag_err=max_measurement_mag_err,
        max_reference_mag_err=max_reference_mag_err,
        min_reference_mag=min_reference_mag,
        max_reference_mag=max_reference_mag,
        min_color=min_color,
        max_color=max_color,
        include_flagged_calibrators=include_flagged_calibrators,
        excluded_object_qc_flags=excluded_object_qc_flags,
        exclude_object_ids=exclude_object_ids,
        clip_sigma=clip_sigma,
        n_iterations=n_iterations,
        exclude_saturated=exclude_saturated,
        limit=limit,
    )
    result.n_measurements_input = diag.n_measurements_input
    result.n_used_calibrator_measurements = diag.n_used_measurements
    result.n_used_calibrator_objects = diag.n_used_objects
    result.n_frames_with_zp = diag.n_frames_with_zp
    result.color0 = diag.color0
    result.color_min = diag.color_min
    result.color_max = diag.color_max
    result.color_term = diag.frame_model_color_term
    result.frame_model_residual_rms = diag.frame_model_residual_rms
    result.frame_model_residual_mad = diag.frame_model_residual_mad
    result.frame_zp_median = diag.frame_zp_median
    result.frame_zp_mad = diag.frame_zp_mad
    result.diagnostics_summary_json = diag.summary_json
    result.diagnostics_stars_csv = diag.stars_csv
    result.diagnostics_frames_csv = diag.frames_csv
    result.warnings.extend(diag.warnings)

    if diag.color0 is None or diag.frame_model_color_term is None or not diag.frame_zp_by_frame:
        result.warnings.append("Color model is incomplete; no target calibration was written.")
        return result

    rows = _load_color_measurement_rows(
        session,
        calibration_name=source_calibration_name,
        identity_catalog_name=identity_catalog_name,
        reference_catalog_name=reference_catalog_name,
        reference_band=ref_band,
        series_id=series_id,
        limit=limit,
    )
    result.checked_measurements = len(rows)
    frame_zp = {int(k): float(v) for k, v in diag.frame_zp_by_frame.items() if _is_finite(v)}
    if not frame_zp:
        result.warnings.append("No frame zero-points are available; no target calibration was written.")
        return result

    target_cv = None
    if not dry_run:
        target_cv = _get_or_create_color_calibration_version(
            session,
            name=target_calibration_name,
            source_cv=source_cv,
            instrument_unit_id=_infer_instrument_unit_id(session, source_cv.calibration_version_id, series_id),
            identity_catalog_name=identity_catalog_name,
            reference_catalog_name=reference_catalog_name,
            reference_band=ref_band,
            color_index=color_key,
            color0=float(diag.color0),
            color_term=float(diag.frame_model_color_term),
            frame_zp=frame_zp,
            diagnostics=diag,
            filters={
                "min_measurements_per_object": min_measurements_per_object,
                "min_stars_per_frame": min_stars_per_frame,
                "max_measurement_mag_err": max_measurement_mag_err,
                "max_reference_mag_err": max_reference_mag_err,
                "min_reference_mag": min_reference_mag,
                "max_reference_mag": max_reference_mag,
                "min_color": min_color,
                "max_color": max_color,
                "include_flagged_calibrators": include_flagged_calibrators,
                "excluded_object_qc_flags": list(excluded_object_qc_flags or _DEFAULT_EXCLUDED_OBJECT_QC_FLAGS),
                "exclude_object_ids": list(exclude_object_ids or []),
                "clip_sigma": clip_sigma,
                "n_iterations": n_iterations,
                "exclude_saturated": exclude_saturated,
            },
        )
        session.flush()
        result.target_calibration_version_id = target_cv.calibration_version_id
        result.deleted_existing = _delete_existing_target_measurements(
            session,
            target_calibration_version_id=target_cv.calibration_version_id,
            series_id=series_id,
        )

    rows_to_insert: list[dict[str, Any]] = []
    output_mags: list[float] = []
    for row in rows:
        result.checked_measurements += 0  # kept explicit for readability of per-row counters
        flags0 = row.get("measurement_flags") if isinstance(row.get("measurement_flags"), dict) else {}
        if exclude_saturated and _is_saturated(flags0):
            result.skipped_saturated += 1
            continue
        mag_inst = _to_float(row.get("mag_inst"))
        if not _is_finite(mag_inst):
            result.skipped_no_mag_inst += 1
            continue
        color_value = _extract_color(row, color_key=color_key, color_bands=color_bands)
        if not _is_finite(color_value):
            result.skipped_no_color += 1
            continue
        fid = int(row["frame_id"])
        zp = frame_zp.get(fid)
        if zp is None or not _is_finite(zp):
            result.skipped_no_frame_zp += 1
            continue
        correction = float(zp) + float(diag.frame_model_color_term) * (float(color_value) - float(diag.color0))
        output_mag = float(mag_inst) + correction
        if not _is_finite(output_mag):
            result.skipped_nonfinite += 1
            continue
        color_outside_fit_range = False
        if diag.color_min is not None and float(color_value) < float(diag.color_min):
            color_outside_fit_range = True
        if diag.color_max is not None and float(color_value) > float(diag.color_max):
            color_outside_fit_range = True
        flags = _build_reference_color_measurement_flags(
            source_flags=flags0,
            source_measurement_id=row.get("measurement_id"),
            source_calibration_version_id=source_cv.calibration_version_id,
            source_calibration_name=source_cv.name,
            identity_catalog_name=identity_catalog_name,
            source_id=row.get("source_id"),
            reference_catalog_name=reference_catalog_name,
            reference_catalog_source_id=row.get("reference_catalog_source_id"),
            reference_band=ref_band,
            reference_mag=row.get("reference_mag"),
            color_index=color_key,
            color_value=float(color_value),
            color0=float(diag.color0),
            color_term=float(diag.frame_model_color_term),
            frame_zp=float(zp),
            correction=float(correction),
            color_outside_fit_range=color_outside_fit_range,
        )
        result.calibrated += 1
        output_mags.append(output_mag)
        if dry_run or target_cv is None:
            continue
        rows_to_insert.append(
            {
                "object_id": row["object_id"],
                "frame_id": row["frame_id"],
                "calibration_version_id": target_cv.calibration_version_id,
                "x": row.get("x"),
                "y": row.get("y"),
                "flux": row.get("flux"),
                "flux_err": row.get("flux_err"),
                "mag_inst": row.get("mag_inst"),
                "mag_cal": output_mag,
                "mag_err": row.get("mag_err"),
                "aperture_radius_pix": row.get("aperture_radius_pix"),
                "local_background": row.get("local_background"),
                "local_zp_correction": correction,
                "centroid_dx_pix": row.get("centroid_dx_pix"),
                "centroid_dy_pix": row.get("centroid_dy_pix"),
                "flags": flags,
            }
        )

    result.output_mag_median = _median(output_mags)
    result.output_mag_mad = _mad(output_mags)
    if rows_to_insert:
        result.inserted_or_updated = _upsert_measurement_rows(session, rows_to_insert)

    if not dry_run:
        session.commit()
    return result


def _build_reference_color_measurement_flags(
    *,
    source_flags: dict[str, Any],
    source_measurement_id: Any,
    source_calibration_version_id: int,
    source_calibration_name: str,
    identity_catalog_name: str,
    source_id: Any,
    reference_catalog_name: str,
    reference_catalog_source_id: Any,
    reference_band: str,
    reference_mag: Any,
    color_index: str,
    color_value: float,
    color0: float,
    color_term: float,
    frame_zp: float,
    correction: float,
    color_outside_fit_range: bool,
) -> dict[str, Any]:
    """Build clean flags for a derived reference-color calibration measurement.

    Source measurement flags may contain calibration-specific provenance from an
    older Gaia-based scale, for example mag_key=G or warning strings. Those
    keys must not be copied to the new PS1/color calibration version because
    downstream tables would then display stale provenance. We only preserve the
    object identity keys needed by existing Grandgousier commands and write a
    fresh, explicit provenance block for the new calibration.
    """

    src = source_flags if isinstance(source_flags, dict) else {}
    source_id_text = str(source_id) if source_id is not None else src.get("source_id")
    catalog_name = str(identity_catalog_name or src.get("catalog_name") or "")

    flags: dict[str, Any] = {
        "catalog_name": catalog_name,
        "source_id": source_id_text,
        "calibration_stage": "reference_color_calibration",
        "calibration_method": "reference_color_frame_zp",
        "color_calibration_algorithm": "frame_zp_plus_linear_color_v1",
        "source_measurement_id": int(source_measurement_id) if source_measurement_id is not None else None,
        "source_calibration_version_id": int(source_calibration_version_id),
        "source_calibration_name": source_calibration_name,
        "reference_catalog_name": reference_catalog_name,
        "reference_catalog_source_id": str(reference_catalog_source_id) if reference_catalog_source_id is not None else None,
        "reference_band": reference_band,
        "reference_mag": _json_number_or_none(reference_mag),
        "reference_color_index": color_index,
        "reference_color_value": _json_number_or_none(color_value),
        "reference_color0": _json_number_or_none(color0),
        "reference_color_term": _json_number_or_none(color_term),
        "reference_frame_zp": _json_number_or_none(frame_zp),
        "reference_color_correction": _json_number_or_none(correction),
        "reference_color_outside_fit_range": bool(color_outside_fit_range),
        "calibration_warning": "reference-color calibrated photometry; compare against old calibration before science use",
    }

    # Preserve only raw measurement quality flags that are still relevant after
    # recalibration. Do not preserve old calibration provenance keys such as
    # mag_key, calibration_stage, reference_* or warning.
    for key in (
        "saturated",
        "saturation_warning",
        "edge",
        "edge_warning",
        "photometry_warning",
        "aperture_warning",
        "centroid_warning",
    ):
        if key in src:
            flags[key] = src[key]

    return flags


def _json_number_or_none(value: Any) -> float | None:
    value_float = _to_float(value)
    if not _is_finite(value_float):
        return None
    return float(value_float)


def normalize_reference_band(value: str | None) -> str:
    if value is None:
        raise ValueError("reference_band is required")
    band = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"ps1_g": "g", "ps1_r": "r", "ps1_i": "i", "ps1_z": "z", "ps1_y": "y", "gaia_g": "g_gaia"}
    band = aliases.get(band, band)
    if band not in _SUPPORTED_BANDS:
        raise ValueError(f"Unsupported reference band: {value!r}")
    return band


def parse_color_index(value: str | None) -> tuple[str, tuple[str, str] | None]:
    if value is None:
        raise ValueError("color_index is required")
    text_value = str(value).strip().lower().replace(" ", "")
    if not text_value:
        raise ValueError("color_index is empty")
    if "-" in text_value:
        a, b = text_value.split("-", 1)
    elif "_" in text_value:
        a, b = text_value.split("_", 1)
    else:
        key = text_value
        aliases = {"bp_rp": "bp_rp", "bprp": "bp_rp", "g_r": "g_r", "gr": "g_r", "r_i": "r_i", "ri": "r_i", "i_z": "i_z", "iz": "i_z"}
        return aliases.get(key, key), None
    a = normalize_reference_band(a)
    b = normalize_reference_band(b)
    return f"{a}_{b}", (a, b)



def _get_or_create_color_calibration_version(
    session: Session,
    *,
    name: str,
    source_cv: CalibrationVersion,
    instrument_unit_id: int | None,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    color_index: str,
    color0: float,
    color_term: float,
    frame_zp: dict[int, float],
    diagnostics: ColorCalibrationDiagnosticsResult,
    filters: dict[str, Any],
) -> CalibrationVersion:
    params = {
        "source_calibration_name": source_cv.name,
        "source_calibration_version_id": source_cv.calibration_version_id,
        "algorithm": "frame_zp_plus_linear_color_v1",
        "formula": "mag_cal = mag_inst + frame_zp + color_term * (color - color0)",
        "identity_catalog_name": identity_catalog_name,
        "reference_catalog_name": reference_catalog_name,
        "reference_band": reference_band,
        "color_index": color_index,
        "color0": color0,
        "color_term": color_term,
        "n_frames_with_zp": len(frame_zp),
        "frame_zp_by_frame": {str(k): v for k, v in sorted(frame_zp.items())},
        "diagnostics": {
            "n_used_measurements": diagnostics.n_used_measurements,
            "n_used_objects": diagnostics.n_used_objects,
            "n_used_frames": diagnostics.n_used_frames,
            "frame_model_residual_rms": diagnostics.frame_model_residual_rms,
            "frame_model_residual_mad": diagnostics.frame_model_residual_mad,
            "frame_zp_median": diagnostics.frame_zp_median,
            "frame_zp_mad": diagnostics.frame_zp_mad,
            "summary_json": diagnostics.summary_json,
            "stars_csv": diagnostics.stars_csv,
            "frames_csv": diagnostics.frames_csv,
        },
        "filters": filters,
    }
    cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == name))
    if cv is None:
        cv = CalibrationVersion(
            name=name,
            model_name="reference_color_frame_zp_v1",
            instrument_unit_id=instrument_unit_id,
            used_catalogs={
                "identity_catalog_name": identity_catalog_name,
                "reference_catalog_name": reference_catalog_name,
                "reference_band": reference_band,
                "color_index": color_index,
            },
            parameters=params,
            notes="MVP5.30 reference-band color calibration. Source calibration is preserved.",
        )
        set_source_calibration(cv, source_cv)
        session.add(cv)
    else:
        cv.model_name = "reference_color_frame_zp_v1"
        cv.instrument_unit_id = instrument_unit_id
        cv.used_catalogs = {
            "identity_catalog_name": identity_catalog_name,
            "reference_catalog_name": reference_catalog_name,
            "reference_band": reference_band,
            "color_index": color_index,
        }
        cv.parameters = params
        set_source_calibration(cv, source_cv)
        cv.notes = "MVP5.30 reference-band color calibration. Source calibration is preserved."
    return cv


def _infer_instrument_unit_id(session: Session, source_calibration_version_id: int | None, series_id: int | None) -> int | None:
    if source_calibration_version_id is None:
        return None
    where = ["m.calibration_version_id = :cv_id", "fr.instrument_unit_id IS NOT NULL"]
    params: dict[str, Any] = {"cv_id": int(source_calibration_version_id)}
    if series_id is not None:
        where.append("EXISTS (SELECT 1 FROM field_series_frames fsf WHERE fsf.series_id = :series_id AND fsf.frame_id = m.frame_id)")
        params["series_id"] = int(series_id)
    sql = f"""
        SELECT DISTINCT fr.instrument_unit_id
        FROM measurements m
        JOIN frames fr ON fr.frame_id = m.frame_id
        WHERE {' AND '.join(where)}
        ORDER BY fr.instrument_unit_id
    """
    values = [row[0] for row in session.execute(text(sql), params).all()]
    if len(values) == 1:
        return int(values[0])
    return None



def _delete_existing_target_measurements(
    session: Session,
    *,
    target_calibration_version_id: int,
    series_id: int | None,
) -> int:
    if series_id is None:
        sql = "DELETE FROM measurements WHERE calibration_version_id = :cv_id"
        result = session.execute(text(sql), {"cv_id": int(target_calibration_version_id)})
        return int(result.rowcount or 0)
    sql = """
        DELETE FROM measurements m
        USING field_series_frames fsf
        WHERE m.calibration_version_id = :cv_id
          AND fsf.series_id = :series_id
          AND fsf.frame_id = m.frame_id
    """
    result = session.execute(text(sql), {"cv_id": int(target_calibration_version_id), "series_id": int(series_id)})
    return int(result.rowcount or 0)


def _upsert_measurement_rows(session: Session, rows: list[dict[str, Any]], *, batch_size: int = 500) -> int:
    if not rows:
        return 0
    table = Measurement.__table__
    total = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        stmt_ins = insert(table).values(chunk)
        stmt_ins = stmt_ins.on_conflict_do_update(
            constraint="uq_measurement_version",
            set_={
                "x": stmt_ins.excluded.x,
                "y": stmt_ins.excluded.y,
                "flux": stmt_ins.excluded.flux,
                "flux_err": stmt_ins.excluded.flux_err,
                "mag_inst": stmt_ins.excluded.mag_inst,
                "mag_cal": stmt_ins.excluded.mag_cal,
                "mag_err": stmt_ins.excluded.mag_err,
                "aperture_radius_pix": stmt_ins.excluded.aperture_radius_pix,
                "local_background": stmt_ins.excluded.local_background,
                "local_zp_correction": stmt_ins.excluded.local_zp_correction,
                "centroid_dx_pix": stmt_ins.excluded.centroid_dx_pix,
                "centroid_dy_pix": stmt_ins.excluded.centroid_dy_pix,
                "flags": stmt_ins.excluded.flags,
            },
        )
        try:
            session.execute(stmt_ins)
        except Exception as exc:
            raise RuntimeError(f"Failed to upsert {len(chunk)} color-calibrated measurements at batch offset {start}: {exc.__class__.__name__}") from exc
        total += len(chunk)
    return total


def _load_color_measurement_rows(
    session: Session,
    *,
    calibration_name: str,
    identity_catalog_name: str,
    reference_catalog_name: str,
    reference_band: str,
    series_id: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    mag_col = _REF_MAG_COLUMNS[reference_band]
    where = [
        "cv.name = :calibration_name",
        "(m.flags ->> 'catalog_name') = :identity_catalog_name",
        "rp.catalog_name = :reference_catalog_name",
        "rp.gaia_catalog_name = :identity_catalog_name",
        "rp.gaia_source_id = (m.flags ->> 'source_id')",
        "m.mag_inst IS NOT NULL",
        f"rp.{mag_col} IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "calibration_name": calibration_name,
        "identity_catalog_name": identity_catalog_name,
        "reference_catalog_name": reference_catalog_name,
    }
    if series_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM field_series_frames fsf WHERE fsf.series_id = :series_id AND fsf.frame_id = m.frame_id)"
        )
        params["series_id"] = int(series_id)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT :limit"
        params["limit"] = int(limit)

    sql = f"""
        SELECT
            m.measurement_id,
            m.object_id,
            m.frame_id,
            m.x,
            m.y,
            m.flux,
            m.flux_err,
            m.mag_inst,
            m.mag_cal,
            m.mag_err,
            m.aperture_radius_pix,
            m.local_background,
            m.local_zp_correction,
            m.centroid_dx_pix,
            m.centroid_dy_pix,
            m.flags AS measurement_flags,
            fr.mjd,
            fr.date_obs,
            fr.instrument_unit_id,
            fr.filter_name,
            (m.flags ->> 'catalog_name') AS measurement_catalog_name,
            (m.flags ->> 'source_id') AS source_id,
            rp.catalog_source_id AS reference_catalog_source_id,
            rp.distance_to_gaia_arcsec,
            rp.{mag_col} AS reference_mag,
            rp.mag_g,
            rp.mag_r,
            rp.mag_i,
            rp.mag_z,
            rp.mag_y,
            rp.mag_b,
            rp.mag_v,
            rp.mag_j,
            rp.mag_h,
            rp.mag_k,
            rp.mag_bp,
            rp.mag_rp,
            rp.mag_g_gaia,
            rp.mag_errors AS reference_mag_errors,
            rp.colors AS reference_colors,
            rp.quality_flags AS reference_quality_flags,
            oqc.flags AS object_qc_flags,
            oqc.details AS object_qc_details,
            oqc.algorithm AS object_qc_algorithm
        FROM measurements m
        JOIN calibration_versions cv ON cv.calibration_version_id = m.calibration_version_id
        JOIN frames fr ON fr.frame_id = m.frame_id
        LEFT JOIN LATERAL (
            SELECT oqc.flags, oqc.details, oqc.algorithm
            FROM object_qc oqc
            WHERE oqc.object_id = m.object_id
              AND (oqc.calibration_version_id = cv.calibration_version_id OR oqc.calibration_version_id IS NULL)
              AND (oqc.catalog_name IS NULL OR oqc.catalog_name = :identity_catalog_name)
              AND (oqc.instrument_unit_id IS NULL OR oqc.instrument_unit_id = fr.instrument_unit_id)
            ORDER BY
              (oqc.calibration_version_id = cv.calibration_version_id) DESC,
              (oqc.instrument_unit_id = fr.instrument_unit_id) DESC,
              oqc.updated_at DESC NULLS LAST,
              oqc.object_qc_id DESC
            LIMIT 1
        ) oqc ON TRUE
        JOIN (
            SELECT DISTINCT ON (gaia_catalog_name, gaia_source_id) *
            FROM reference_photometry
            WHERE catalog_name = :reference_catalog_name
              AND gaia_catalog_name = :identity_catalog_name
              AND gaia_source_id IS NOT NULL
            ORDER BY gaia_catalog_name, gaia_source_id, distance_to_gaia_arcsec NULLS LAST, refphot_id
        ) rp ON rp.gaia_source_id = (m.flags ->> 'source_id')
        WHERE {' AND '.join(where)}
        ORDER BY m.object_id, fr.mjd, fr.date_obs, m.measurement_id
        {limit_sql}
    """
    rows = session.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]



def _build_object_filter_info(
    rows: list[dict[str, Any]],
    *,
    min_measurements_per_object: int,
    include_flagged_calibrators: bool,
    excluded_object_qc_flags: tuple[str, ...],
    explicit_exclude_object_ids: tuple[int, ...],
    min_reference_mag: float | None,
    max_reference_mag: float | None,
    min_color: float | None,
    max_color: float | None,
) -> dict[str, Any]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["object_id"]), []).append(row)

    exclusion_reasons: dict[int, list[str]] = {}
    counters = {
        "n_excluded_by_object_id": 0,
        "n_excluded_by_object_qc": 0,
        "n_excluded_by_reference_mag": 0,
        "n_excluded_by_color": 0,
        "n_excluded_by_low_object_measurements": 0,
    }
    explicit_ids = set(explicit_exclude_object_ids)
    excluded_flags_set = {_normalize_flag_name(flag) for flag in excluded_object_qc_flags}

    for object_id, values in grouped.items():
        first = values[0]
        reasons: list[str] = []
        if object_id in explicit_ids:
            reasons.append("EXPLICIT_OBJECT_ID")
            counters["n_excluded_by_object_id"] += 1

        object_flags = set(_truthy_flag_names(first.get("object_qc_flags") if isinstance(first.get("object_qc_flags"), dict) else {}))
        bad_flags = sorted(object_flags & excluded_flags_set)
        if bad_flags and not include_flagged_calibrators:
            reasons.append("OBJECT_QC:" + ",".join(bad_flags))
            counters["n_excluded_by_object_qc"] += 1

        ref_mag = _to_float(first.get("reference_mag"))
        if min_reference_mag is not None and _is_finite(ref_mag) and float(ref_mag) < float(min_reference_mag):
            reasons.append(f"REFERENCE_MAG_LT:{float(min_reference_mag):.6g}")
            counters["n_excluded_by_reference_mag"] += 1
        if max_reference_mag is not None and _is_finite(ref_mag) and float(ref_mag) > float(max_reference_mag):
            reasons.append(f"REFERENCE_MAG_GT:{float(max_reference_mag):.6g}")
            if "REFERENCE_MAG_LT" not in ";".join(reasons[:-1]):
                counters["n_excluded_by_reference_mag"] += 1

        color = _to_float(first.get("color_value"))
        excluded_by_color = False
        if min_color is not None and _is_finite(color) and float(color) < float(min_color):
            reasons.append(f"COLOR_LT:{float(min_color):.6g}")
            excluded_by_color = True
        if max_color is not None and _is_finite(color) and float(color) > float(max_color):
            reasons.append(f"COLOR_GT:{float(max_color):.6g}")
            excluded_by_color = True
        if excluded_by_color:
            counters["n_excluded_by_color"] += 1

        if len(values) < min_measurements_per_object:
            reasons.append(f"LOW_OBJECT_MEASUREMENTS:{len(values)}<{min_measurements_per_object}")
            counters["n_excluded_by_low_object_measurements"] += 1

        if reasons:
            exclusion_reasons[object_id] = reasons

    candidate_object_ids = {object_id for object_id in grouped if object_id not in exclusion_reasons}
    low_meas_ids = {
        object_id
        for object_id, reasons in exclusion_reasons.items()
        if any(reason.startswith("LOW_OBJECT_MEASUREMENTS") for reason in reasons)
    }
    n_measurements_excluded_by_low_object_measurements = sum(len(grouped[object_id]) for object_id in low_meas_ids)
    out = {
        "n_reference_objects": len(grouped),
        "n_excluded_objects": len(exclusion_reasons),
        "candidate_object_ids": candidate_object_ids,
        "exclusion_reasons_by_object": exclusion_reasons,
        "n_measurements_excluded_by_low_object_measurements": n_measurements_excluded_by_low_object_measurements,
    }
    out.update(counters)
    return out


def _normalize_flag_name(value: Any) -> str:
    return str(value).strip().upper().replace(" ", "_").replace("-", "_")


def _truthy_flag_names(flags: dict[str, Any]) -> list[str]:
    out = []
    for key, value in (flags or {}).items():
        if bool(value):
            out.append(_normalize_flag_name(key))
    return sorted(out)


def _extract_color(row: dict[str, Any], *, color_key: str, color_bands: tuple[str, str] | None) -> float | None:
    colors = row.get("reference_colors") if isinstance(row.get("reference_colors"), dict) else {}
    if color_key in colors:
        value = _to_float(colors.get(color_key))
        if _is_finite(value):
            return float(value)
    if color_bands is None:
        return None
    a, b = color_bands
    ma = _to_float(row.get(_REF_MAG_COLUMNS[a]))
    mb = _to_float(row.get(_REF_MAG_COLUMNS[b]))
    if _is_finite(ma) and _is_finite(mb):
        return float(ma) - float(mb)
    return None


def _reference_mag_error(row: dict[str, Any], band: str) -> float | None:
    errors = row.get("reference_mag_errors") if isinstance(row.get("reference_mag_errors"), dict) else {}
    candidates = [band, f"err_{band}", f"mag_{band}_err"]
    for key in candidates:
        value = _to_float(errors.get(key))
        if _is_finite(value):
            return float(value)
    return None


def _build_star_rows(
    rows: list[dict[str, Any]],
    *,
    color0: float,
    exclusion_reasons_by_object: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["object_id"]), []).append(row)
    star_rows: list[dict[str, Any]] = []
    for object_id, values in sorted(grouped.items()):
        first = values[0]
        mag_cal_values = [float(v["mag_cal"]) for v in values if _is_finite(v.get("mag_cal"))]
        mag_inst_values = [float(v["mag_inst"]) for v in values if _is_finite(v.get("mag_inst"))]
        ref_mag = float(first["reference_mag"])
        color = float(first["color_value"])
        current_mag = _median(mag_cal_values)
        current_residual = current_mag - ref_mag if _is_finite(current_mag) else None
        reasons = list((exclusion_reasons_by_object or {}).get(int(object_id), []))
        object_qc_flags = _truthy_flag_names(first.get("object_qc_flags") if isinstance(first.get("object_qc_flags"), dict) else {})
        star_rows.append(
            {
                "object_id": object_id,
                "source_id": first.get("source_id"),
                "reference_catalog_source_id": first.get("reference_catalog_source_id"),
                "reference_mag": ref_mag,
                "reference_mag_err": first.get("reference_mag_err"),
                "color": color,
                "color_minus_color0": color - color0,
                "n_measurements": len(values),
                "n_mag_cal": len(mag_cal_values),
                "mag_cal_median": current_mag,
                "mag_inst_median": _median(mag_inst_values),
                "current_residual": current_residual,
                "current_residual_after_color": None,
                "distance_to_gaia_arcsec": first.get("distance_to_gaia_arcsec"),
                "object_qc_flags": ";".join(object_qc_flags),
                "calibrator_candidate": not bool(reasons),
                "used_as_calibrator": False,
                "exclusion_reason": ";".join(reasons),
            }
        )
    return star_rows


def _compute_current_color_fit(star_rows: list[dict[str, Any]], result: ColorCalibrationDiagnosticsResult) -> None:
    rows = [
        r
        for r in star_rows
        if r.get("calibrator_candidate")
        and _is_finite(r.get("current_residual"))
        and _is_finite(r.get("color_minus_color0"))
    ]
    residuals = [float(r["current_residual"]) for r in rows]
    result.current_residual_median = _median(residuals)
    result.current_residual_mad = _mad(residuals)
    result.current_residual_rms = _rms(residuals)
    if len(rows) < 3:
        result.warnings.append("Too few stars with mag_cal to fit current residual color slope.")
        return
    xs = [float(r["color_minus_color0"]) for r in rows]
    ys = [float(r["current_residual"]) for r in rows]
    intercept, slope = _linear_fit_with_intercept(xs, ys)
    result.current_color_intercept = intercept
    result.current_color_slope = slope
    after = []
    for row, x, y in zip(rows, xs, ys):
        corrected = y - (intercept + slope * x)
        row["current_residual_after_color"] = corrected
        after.append(corrected)
    result.current_residual_after_color_median = _median(after)
    result.current_residual_after_color_mad = _mad(after)
    result.current_residual_after_color_rms = _rms(after)


def _fit_frame_color_model(
    rows: list[dict[str, Any]],
    *,
    color0: float,
    min_stars_per_frame: int,
    clip_sigma: float,
    n_iterations: int,
) -> dict[str, Any]:
    model_rows = [dict(r) for r in rows]
    for r in model_rows:
        r["x_color"] = float(r["color_value"]) - color0
        r["y_zp"] = float(r["reference_mag"]) - float(r["mag_inst"])
        r["model_used"] = True
        r["frame_zp"] = None
        r["frame_model_residual"] = None

    color_term = 0.0
    previous_used_ids: set[int] | None = None
    frame_zp: dict[int, float] = {}
    rejected_low_frame = 0

    for _iteration in range(n_iterations):
        used_now = [r for r in model_rows if r.get("model_used")]
        frame_zp = _compute_frame_zp(used_now, color_term=color_term, min_stars_per_frame=min_stars_per_frame)
        frame_counts_all = _frame_counts(used_now)
        rejected_low_frame = sum(n for fid, n in frame_counts_all.items() if fid not in frame_zp)
        for r in model_rows:
            fid = int(r["frame_id"])
            r["frame_zp"] = frame_zp.get(fid)
            if fid not in frame_zp:
                r["model_used"] = False
                r["frame_model_residual"] = None
                continue
            pred_mag = float(r["mag_inst"]) + frame_zp[fid] + color_term * float(r["x_color"])
            r["frame_model_residual"] = pred_mag - float(r["reference_mag"])
        residuals = [float(r["frame_model_residual"]) for r in model_rows if r.get("model_used") and _is_finite(r.get("frame_model_residual"))]
        if not residuals:
            break
        center = _median(residuals) or 0.0
        scale = _robust_sigma(residuals)
        if scale is None or scale <= 0:
            clipped_ids = {int(r["measurement_id"]) for r in model_rows if r.get("model_used")}
        else:
            clipped_ids = {
                int(r["measurement_id"])
                for r in model_rows
                if r.get("model_used")
                and _is_finite(r.get("frame_model_residual"))
                and abs(float(r["frame_model_residual"]) - center) <= clip_sigma * scale
            }
        for r in model_rows:
            if int(r["frame_id"]) not in frame_zp:
                r["model_used"] = False
            else:
                r["model_used"] = int(r["measurement_id"]) in clipped_ids
        fit_rows = [r for r in model_rows if r.get("model_used") and int(r["frame_id"]) in frame_zp]
        denom = sum(float(r["x_color"]) ** 2 for r in fit_rows)
        if denom > 0:
            color_term = sum(float(r["x_color"]) * (float(r["y_zp"]) - frame_zp[int(r["frame_id"])]) for r in fit_rows) / denom
        if previous_used_ids == clipped_ids:
            break
        previous_used_ids = clipped_ids

    # Final pass with final color_term and final used mask.
    used_final = [r for r in model_rows if r.get("model_used")]
    frame_zp = _compute_frame_zp(used_final, color_term=color_term, min_stars_per_frame=min_stars_per_frame)
    for r in model_rows:
        fid = int(r["frame_id"])
        if r.get("model_used") and fid in frame_zp:
            r["frame_zp"] = frame_zp[fid]
            r["frame_model_residual"] = float(r["mag_inst"]) + frame_zp[fid] + color_term * float(r["x_color"]) - float(r["reference_mag"])
        else:
            r["model_used"] = False
            r["frame_zp"] = frame_zp.get(fid)
            r["frame_model_residual"] = None

    residuals_final = [float(r["frame_model_residual"]) for r in model_rows if r.get("model_used") and _is_finite(r.get("frame_model_residual"))]
    used_object_ids = {int(r["object_id"]) for r in model_rows if r.get("model_used")}
    used_frame_ids = {int(r["frame_id"]) for r in model_rows if r.get("model_used")}
    frame_rows = _build_frame_rows(model_rows, frame_zp)
    return {
        "color_term": float(color_term) if _is_finite(color_term) else None,
        "n_used_measurements": len(residuals_final),
        "n_used_objects": len(used_object_ids),
        "n_used_frames": len(used_frame_ids),
        "n_frames_with_zp": len(frame_zp),
        "n_rejected_low_frame_stars": int(rejected_low_frame),
        "n_clipped_measurements": max(0, len(rows) - len(residuals_final) - int(rejected_low_frame)),
        "residual_median": _median(residuals_final),
        "residual_mad": _mad(residuals_final),
        "residual_rms": _rms(residuals_final),
        "frame_zp_median": _median(list(frame_zp.values())),
        "frame_zp_mad": _mad(list(frame_zp.values())),
        "frame_rows": frame_rows,
        "measurement_rows": model_rows,
    }


def _compute_frame_zp(rows: list[dict[str, Any]], *, color_term: float, min_stars_per_frame: int) -> dict[int, float]:
    grouped: dict[int, list[float]] = {}
    for r in rows:
        fid = int(r["frame_id"])
        value = float(r["y_zp"]) - color_term * float(r["x_color"])
        if _is_finite(value):
            grouped.setdefault(fid, []).append(float(value))
    return {fid: float(_median(vals)) for fid, vals in grouped.items() if len(vals) >= min_stars_per_frame and _median(vals) is not None}


def _frame_counts(rows: list[dict[str, Any]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in rows:
        fid = int(r["frame_id"])
        out[fid] = out.get(fid, 0) + 1
    return out


def _build_frame_rows(rows: list[dict[str, Any]], frame_zp: dict[int, float]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(int(r["frame_id"]), []).append(r)
    out = []
    for fid, values in sorted(grouped.items()):
        used = [v for v in values if v.get("model_used") and _is_finite(v.get("frame_model_residual"))]
        residuals = [float(v["frame_model_residual"]) for v in used]
        first = values[0]
        out.append(
            {
                "frame_id": fid,
                "mjd": first.get("mjd"),
                "date_obs": first.get("date_obs"),
                "instrument_unit_id": first.get("instrument_unit_id"),
                "filter_name": first.get("filter_name"),
                "n_measurements": len(values),
                "n_used_measurements": len(used),
                "frame_zp": frame_zp.get(fid),
                "residual_median": _median(residuals),
                "residual_mad": _mad(residuals),
                "residual_rms": _rms(residuals),
            }
        )
    return out


def _write_outputs(
    result: ColorCalibrationDiagnosticsResult,
    star_rows: list[dict[str, Any]],
    frame_rows: list[dict[str, Any]],
    out_dir: Path | None,
) -> None:
    if out_dir is None:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "color_calibration_summary.json"
    stars_path = out_dir / "color_calibration_stars.csv"
    frames_path = out_dir / "color_residuals_by_frame.csv"
    result.summary_json = str(summary_path)
    result.stars_csv = str(stars_path)
    result.frames_csv = str(frames_path)

    _write_csv(stars_path, star_rows, _STAR_COLUMNS)
    _write_csv(frames_path, frame_rows, _FRAME_COLUMNS)
    summary_path.write_text(json.dumps(result.summary_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")


_STAR_COLUMNS = [
    "object_id",
    "source_id",
    "calibrator_candidate",
    "used_as_calibrator",
    "exclusion_reason",
    "object_qc_flags",
    "reference_catalog_source_id",
    "reference_mag",
    "reference_mag_err",
    "color",
    "color_minus_color0",
    "n_measurements",
    "n_mag_cal",
    "mag_cal_median",
    "mag_inst_median",
    "current_residual",
    "current_residual_after_color",
    "frame_model_residual_median",
    "frame_model_residual_mad",
    "frame_model_n_measurements",
    "distance_to_gaia_arcsec",
]

_FRAME_COLUMNS = [
    "frame_id",
    "mjd",
    "date_obs",
    "instrument_unit_id",
    "filter_name",
    "n_measurements",
    "n_used_measurements",
    "frame_zp",
    "residual_median",
    "residual_mad",
    "residual_rms",
]


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _csv_value(row.get(col)) for col in columns})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.10g}"
    return value


def _unique_object_values(rows: list[dict[str, Any]], object_key: str, value_key: str) -> list[float]:
    seen = set()
    values = []
    for r in rows:
        oid = r.get(object_key)
        if oid in seen:
            continue
        seen.add(oid)
        value = _to_float(r.get(value_key))
        if _is_finite(value):
            values.append(float(value))
    return values


def _linear_fit_with_intercept(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if _is_finite(x) and _is_finite(y)]
    if len(pairs) < 2:
        return None, None
    xmean = sum(x for x, _ in pairs) / len(pairs)
    ymean = sum(y for _, y in pairs) / len(pairs)
    denom = sum((x - xmean) ** 2 for x, _ in pairs)
    if denom <= 0:
        return ymean, 0.0
    slope = sum((x - xmean) * (y - ymean) for x, y in pairs) / denom
    intercept = ymean - slope * xmean
    return float(intercept), float(slope)


def _median(values: Iterable[float | None]) -> float | None:
    vals = sorted(float(v) for v in values if _is_finite(v))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mad(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = _median(vals)
    if med is None:
        return None
    return _median([abs(v - med) for v in vals])


def _robust_sigma(values: Iterable[float | None]) -> float | None:
    mad = _mad(values)
    if mad is None:
        return None
    return 1.4826 * mad


def _rms(values: Iterable[float | None]) -> float | None:
    vals = [float(v) for v in values if _is_finite(v)]
    if not vals:
        return None
    med = _median(vals) or 0.0
    return math.sqrt(sum((v - med) ** 2 for v in vals) / len(vals))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _is_finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def _is_saturated(flags: dict[str, Any]) -> bool:
    for key in ("saturated", "near_saturated", "saturated_or_near_saturated"):
        if bool(flags.get(key)):
            return True
    return False
