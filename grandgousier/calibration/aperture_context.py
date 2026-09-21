from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import numpy as np


APERTURE_CONTEXT_ALGORITHM = "aperture_correction_context_v2"


def estimate_psf_shape(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    background: float | None,
    radius_pix: float = 6.0,
) -> dict[str, float] | None:
    """Estimate PSF second-moment shape in a small circular cutout.

    The estimate is intentionally diagnostic rather than a replacement for a PSF
    model. Negative background-subtracted pixels are clipped to zero, and the
    returned position angle follows detector coordinates: +x to the right and +y
    toward increasing image row.
    """

    if data.ndim != 2 or not all(math.isfinite(float(v)) for v in (x, y, radius_pix)):
        return None
    radius = max(float(radius_pix), 2.0)
    x0 = max(0, int(math.floor(float(x) - radius)))
    x1 = min(data.shape[1], int(math.ceil(float(x) + radius)) + 1)
    y0 = max(0, int(math.floor(float(y) - radius)))
    y1 = min(data.shape[0], int(math.ceil(float(y) + radius)) + 1)
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None

    cut = np.asarray(data[y0:y1, x0:x1], dtype=float)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    circular = (xx - float(x)) ** 2 + (yy - float(y)) ** 2 <= radius**2
    finite = circular & np.isfinite(cut)
    if int(np.count_nonzero(finite)) < 12:
        return None

    bg = _finite_or_none(background)
    if bg is None:
        bg = float(np.nanmedian(cut[finite]))
    weights = np.where(finite, np.maximum(cut - bg, 0.0), 0.0)
    total = float(np.sum(weights))
    if not math.isfinite(total) or total <= 0:
        return None

    xc = float(np.sum(weights * xx) / total)
    yc = float(np.sum(weights * yy) / total)
    dx = xx - xc
    dy = yy - yc
    var_x = float(np.sum(weights * dx * dx) / total)
    var_y = float(np.sum(weights * dy * dy) / total)
    cov_xy = float(np.sum(weights * dx * dy) / total)
    cov = np.asarray([[var_x, cov_xy], [cov_xy, var_y]], dtype=float)
    if not np.isfinite(cov).all():
        return None
    values, vectors = np.linalg.eigh(cov)
    values = np.maximum(values, 0.0)
    order = np.argsort(values)[::-1]
    major_var = float(values[order[0]])
    minor_var = float(values[order[1]])
    if major_var <= 0 or minor_var < 0:
        return None
    major_sigma = math.sqrt(major_var)
    minor_sigma = math.sqrt(minor_var)
    fwhm_major = 2.354820045 * major_sigma
    fwhm_minor = 2.354820045 * minor_sigma
    fwhm_circular = math.sqrt(max(fwhm_major * fwhm_minor, 0.0))
    if not 0.3 <= fwhm_circular <= 30.0:
        return None
    vec = vectors[:, order[0]]
    angle = math.degrees(math.atan2(float(vec[1]), float(vec[0]))) % 180.0
    ellipticity = 1.0 - (minor_sigma / major_sigma if major_sigma > 0 else 1.0)
    return {
        "psf_fwhm_pix": float(fwhm_circular),
        "psf_fwhm_major_pix": float(fwhm_major),
        "psf_fwhm_minor_pix": float(fwhm_minor),
        "psf_ellipticity": float(max(0.0, min(1.0, ellipticity))),
        "psf_position_angle_deg": float(angle),
    }


def build_frame_aperture_context(
    *,
    frame: Any,
    header: Any,
    model: dict[str, Any],
    candidates: list[dict[str, Any]],
    fwhm_header_unit: str = "arcsec",
    error: str | None = None,
) -> dict[str, Any]:
    quality_flags = getattr(frame, "quality_flags", None) or {}
    pre_qc = quality_flags.get("frame_pre_photometry_qc_v1") if isinstance(quality_flags, dict) else None
    pre_qc = pre_qc if isinstance(pre_qc, dict) else {}
    detection_qc = _best_detection_qc(quality_flags)
    pixel_scale = _extract_pixel_scale_arcsec(frame, header, quality_flags)
    header_fwhm_arcsec = _extract_header_fwhm_arcsec(
        frame,
        header,
        pixel_scale_arcsec=pixel_scale,
        fwhm_header_unit=fwhm_header_unit,
    )

    shape_rows = [row for row in candidates if _finite_or_none(row.get("psf_fwhm_pix")) is not None]
    psf_fwhm_pix = _median(row.get("psf_fwhm_pix") for row in shape_rows)
    psf_fwhm_major_pix = _median(row.get("psf_fwhm_major_pix") for row in shape_rows)
    psf_fwhm_minor_pix = _median(row.get("psf_fwhm_minor_pix") for row in shape_rows)
    psf_ellipticity = _median(row.get("psf_ellipticity") for row in shape_rows)
    psf_angle = _axial_mean_deg(row.get("psf_position_angle_deg") for row in shape_rows)

    coeff = _coefficients(model.get("coefficients"))
    plane_candidate_coeff = _coefficients(model.get("plane_candidate_coefficients"))
    gradient = _gradient_metrics(coeff if model.get("selected_model") == "plane" else None)
    plane_candidate_gradient = _gradient_metrics(plane_candidate_coeff)
    corner = _corner_metrics(model)
    alignment = _axis_delta_deg(gradient.get("angle_deg"), psf_angle)

    date_obs = getattr(frame, "date_obs", None)
    row = {
        "algorithm": APERTURE_CONTEXT_ALGORITHM,
        "frame_id": int(getattr(frame, "frame_id")),
        "date_obs": date_obs.isoformat() if hasattr(date_obs, "isoformat") else None,
        "mjd": _finite_or_none(getattr(frame, "mjd", None)),
        "airmass": _finite_or_none(getattr(frame, "airmass", None)),
        "ccd_temp": _finite_or_none(getattr(frame, "ccd_temp", None)),
        "selected_model": str(model.get("selected_model") or "identity"),
        "accepted": bool(model.get("accepted")),
        "n_candidates": int(model.get("n_candidates") or 0),
        "n_used": int(model.get("n_used") or 0),
        "error": error,
        "background_median": _finite_or_none(pre_qc.get("background_median")),
        "background_mad_sigma": _finite_or_none(pre_qc.get("background_mad_sigma")),
        "bright_fraction": _finite_or_none(pre_qc.get("bright_fraction")),
        "n_local_maxima": _int_or_none(pre_qc.get("n_local_maxima_before_cap")),
        "astrom_rms_arcsec": _finite_or_none(detection_qc.get("astrom_rms_arcsec")),
        "detection_zp_mad": _finite_or_none(detection_qc.get("zp_mad")),
        "n_detection_matches": _int_or_none(detection_qc.get("n_matched")),
        "pixel_scale_arcsec": pixel_scale,
        "header_fwhm_arcsec": header_fwhm_arcsec,
        "psf_shape_sources": len(shape_rows),
        "psf_fwhm_pix_median": psf_fwhm_pix,
        "psf_fwhm_arcsec_median": (
            float(psf_fwhm_pix * pixel_scale)
            if psf_fwhm_pix is not None and pixel_scale is not None
            else None
        ),
        "psf_fwhm_major_pix_median": psf_fwhm_major_pix,
        "psf_fwhm_minor_pix_median": psf_fwhm_minor_pix,
        "psf_ellipticity_median": psf_ellipticity,
        "psf_position_angle_deg": psf_angle,
        "gradient_magnitude": gradient.get("magnitude"),
        "gradient_angle_deg": gradient.get("angle_deg"),
        "gradient_dominant_axis": gradient.get("dominant_axis"),
        "gradient_psf_axis_delta_deg": alignment,
        "plane_candidate_gradient_magnitude": plane_candidate_gradient.get("magnitude"),
        "plane_candidate_gradient_angle_deg": plane_candidate_gradient.get("angle_deg"),
        "plane_candidate_coefficient_x": (
            plane_candidate_coeff[1] if plane_candidate_coeff is not None else None
        ),
        "plane_candidate_coefficient_y": (
            plane_candidate_coeff[2] if plane_candidate_coeff is not None else None
        ),
        "plane_candidate_spatial_peak_to_peak": _finite_or_none(
            model.get("plane_candidate_spatial_peak_to_peak")
        ),
        "plane_candidate_pivot_correction": _finite_or_none(
            model.get("plane_candidate_pivot_correction")
        ),
        "plane_candidate_max_abs_spatial_deviation": _finite_or_none(
            model.get("plane_candidate_max_abs_spatial_deviation")
        ),
        "plane_candidate_max_abs_total_correction": _finite_or_none(
            model.get("plane_candidate_max_abs_total_correction")
        ),
        "plane_candidate_residual_mad": _finite_or_none(model.get("plane_candidate_residual_mad")),
        "plane_bic_improvement": _finite_or_none(model.get("plane_bic_improvement")),
        "legacy_selected_model": str(model.get("legacy_selected_model") or "identity"),
        "selection_changed_vs_legacy": bool(model.get("selection_changed_vs_legacy")),
        "legacy_selection_reason": model.get("legacy_selection_reason"),
        "legacy_max_abs_correction_threshold": _finite_or_none(
            model.get("legacy_max_abs_correction")
        ),
        "max_abs_spatial_deviation_threshold": _finite_or_none(
            model.get("max_abs_spatial_deviation_threshold")
        ),
        "max_abs_total_correction_threshold": _finite_or_none(
            model.get("max_abs_total_correction_threshold")
        ),
        "pivot_xnorm": _finite_or_none(model.get("pivot_xnorm")),
        "pivot_ynorm": _finite_or_none(model.get("pivot_ynorm")),
        **corner,
    }
    return row


def analyze_aperture_frame_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    plane_rows = [row for row in rows if row.get("selected_model") == "plane" and row.get("accepted")]
    angles = [_finite_or_none(row.get("gradient_angle_deg")) for row in plane_rows]
    angles = [value for value in angles if value is not None]
    mean_angle, resultant = _directional_mean_deg(angles)
    sign_consistency = _gradient_sign_consistency(plane_rows)
    alignments = [_finite_or_none(row.get("gradient_psf_axis_delta_deg")) for row in plane_rows]
    alignments = [value for value in alignments if value is not None]
    mjd_values = [_finite_or_none(row.get("mjd")) for row in plane_rows]
    mjd_values = [value for value in mjd_values if value is not None]

    candidate_rows = [
        row
        for row in rows
        if _finite_or_none(row.get("plane_candidate_gradient_angle_deg")) is not None
    ]
    candidate_angles = [
        float(row["plane_candidate_gradient_angle_deg"])
        for row in candidate_rows
    ]
    candidate_mean_angle, candidate_resultant = _directional_mean_deg(candidate_angles)
    candidate_sign_consistency = _gradient_sign_consistency(
        candidate_rows, angle_key="plane_candidate_gradient_angle_deg"
    )
    promoted_rows = [row for row in rows if bool(row.get("selection_changed_vs_legacy"))]
    promoted_frame_ids = sorted(
        int(row["frame_id"])
        for row in promoted_rows
        if row.get("frame_id") is not None and row.get("selected_model") == "plane"
    )
    demoted_frame_ids = sorted(
        int(row["frame_id"])
        for row in promoted_rows
        if row.get("frame_id") is not None and row.get("selected_model") != "plane"
    )

    predictors = (
        "mjd",
        "airmass",
        "ccd_temp",
        "background_median",
        "background_mad_sigma",
        "n_local_maxima",
        "astrom_rms_arcsec",
        "detection_zp_mad",
        "psf_fwhm_pix_median",
        "psf_fwhm_arcsec_median",
        "psf_ellipticity_median",
    )
    associations: list[dict[str, Any]] = []
    for predictor in predictors:
        associations.append(
            _association(
                rows,
                x_key=predictor,
                y_key="plane_selected",
                y_transform=lambda row: 1.0 if row.get("selected_model") == "plane" else 0.0,
                scope="all_frames",
            )
        )
        associations.append(
            _association(
                rows,
                x_key=predictor,
                y_key="plane_candidate_spatial_peak_to_peak",
                scope="frames_with_plane_candidate",
            )
        )
    associations = [item for item in associations if item.get("n", 0) >= 5]
    strongest = sorted(
        (item for item in associations if item.get("spearman_rho") is not None),
        key=lambda item: abs(float(item["spearman_rho"])),
        reverse=True,
    )[:8]

    model_groups = {}
    for model_name in ("plane", "constant", "identity"):
        subset = [row for row in rows if row.get("selected_model") == model_name]
        model_groups[model_name] = {
            "n_frames": len(subset),
            "median_mjd": _median(row.get("mjd") for row in subset),
            "median_background": _median(row.get("background_median") for row in subset),
            "median_background_sigma": _median(row.get("background_mad_sigma") for row in subset),
            "median_psf_fwhm_pix": _median(row.get("psf_fwhm_pix_median") for row in subset),
            "median_psf_ellipticity": _median(row.get("psf_ellipticity_median") for row in subset),
            "median_astrom_rms_arcsec": _median(row.get("astrom_rms_arcsec") for row in subset),
            "median_n_used": _median(row.get("n_used") for row in subset),
        }

    hints: list[str] = []
    if resultant is not None and resultant >= 0.8 and len(angles) >= 5:
        hints.append("coherent_detector_gradient_direction")
    if candidate_resultant is not None and candidate_resultant >= 0.8 and len(candidate_angles) >= 5:
        hints.append("coherent_plane_candidate_gradient_direction")
    if alignments and float(median(alignments)) <= 20.0:
        hints.append("gradient_aligned_with_psf_major_axis")
    if len(plane_rows) < 5:
        evidence = "insufficient_plane_frames"
    elif len(plane_rows) < 10:
        evidence = "exploratory_small_sample"
    else:
        evidence = "descriptive"

    return {
        "algorithm": APERTURE_CONTEXT_ALGORITHM,
        "coordinate_convention": "+x right, +y increasing detector row; gradient angle uses atan2(c_y, c_x)",
        "status": "OK" if rows else "NO_FRAMES",
        "evidence_level": evidence,
        "causal_inference_allowed": False,
        "n_frames": len(rows),
        "n_plane_frames": len(plane_rows),
        "n_plane_candidate_frames": len(candidate_rows),
        "n_frames_changed_vs_legacy": len(promoted_rows),
        "n_frames_promoted_to_plane": len(promoted_frame_ids),
        "n_frames_demoted_from_plane": len(demoted_frame_ids),
        "promoted_to_plane_frame_ids": promoted_frame_ids,
        "demoted_from_plane_frame_ids": demoted_frame_ids,
        "n_frames_with_psf_shape": sum(
            1 for row in rows if _finite_or_none(row.get("psf_fwhm_pix_median")) is not None
        ),
        "n_plane_frames_with_psf_shape": sum(
            1 for row in plane_rows if _finite_or_none(row.get("psf_fwhm_pix_median")) is not None
        ),
        "n_frames_with_header_fwhm": sum(
            1 for row in rows if _finite_or_none(row.get("header_fwhm_arcsec")) is not None
        ),
        "plane_gradient_mean_angle_deg": mean_angle,
        "plane_gradient_resultant_length": resultant,
        "plane_gradient_sign_consistency": sign_consistency,
        "plane_candidate_gradient_mean_angle_deg": candidate_mean_angle,
        "plane_candidate_gradient_resultant_length": candidate_resultant,
        "plane_candidate_gradient_sign_consistency": candidate_sign_consistency,
        "plane_gradient_psf_axis_delta_median_deg": float(median(alignments)) if alignments else None,
        "plane_time_span_minutes": (
            float((max(mjd_values) - min(mjd_values)) * 1440.0) if len(mjd_values) >= 2 else None
        ),
        "model_groups": model_groups,
        "strongest_associations": strongest,
        "associations": associations,
        "interpretation_hints": hints,
    }


def export_aperture_context_diagnostics(
    *,
    rows: list[dict[str, Any]],
    analysis: dict[str, Any],
    out_dir: str | Path,
) -> tuple[Path, Path, Path | None]:
    target = Path(out_dir) / "aperture_correction"
    target.mkdir(parents=True, exist_ok=True)
    summary_path = target / "aperture_correction_context_summary.json"
    csv_path = target / "aperture_correction_context_frames.csv"
    plot_path = target / "aperture_correction_context.png"
    summary_path.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    fieldnames = _fieldnames(rows)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    try:
        _plot_context(rows, plot_path)
    except Exception:
        plot_path = None
    return summary_path, csv_path, plot_path


def _plot_context(rows: list[dict[str, Any]], path: Path) -> None:
    import matplotlib.pyplot as plt

    ordered = sorted(rows, key=lambda row: (_finite_or_none(row.get("mjd")) or math.inf, row.get("frame_id") or 0))
    x = np.asarray([
        ((_finite_or_none(row.get("mjd")) or 0.0) - (_finite_or_none(ordered[0].get("mjd")) or 0.0)) * 1440.0
        for row in ordered
    ])
    amplitude = np.asarray([
        _finite_or_none(row.get("plane_candidate_spatial_peak_to_peak")) or np.nan for row in ordered
    ])
    fwhm = np.asarray([_finite_or_none(row.get("psf_fwhm_pix_median")) or np.nan for row in ordered])
    ellipticity = np.asarray([_finite_or_none(row.get("psf_ellipticity_median")) or np.nan for row in ordered])
    selected = np.asarray([1.0 if row.get("selected_model") == "plane" else 0.0 for row in ordered])
    legacy_selected = np.asarray([
        1.0 if row.get("legacy_selected_model") == "plane" else 0.0 for row in ordered
    ])
    promoted = (selected > 0) & (legacy_selected == 0)

    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    axes[0].plot(x, amplitude, marker="o", linestyle="-")
    axes[0].scatter(
        x[legacy_selected > 0], amplitude[legacy_selected > 0], marker="s", label="legacy plane"
    )
    axes[0].scatter(x[promoted], amplitude[promoted], marker="x", label="promoted by v2 safety")
    axes[0].set_ylabel("Plane candidate p2p, mag")
    axes[0].legend(loc="best")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(x, fwhm, marker="o", linestyle="-")
    axes[1].set_ylabel("PSF FWHM, pix")
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(x, ellipticity, marker="o", linestyle="-")
    axes[2].set_ylabel("PSF ellipticity")
    axes[2].set_xlabel("Minutes from first frame")
    axes[2].grid(True, alpha=0.3)
    fig.suptitle("Aperture-correction detector/PSF context and safety A/B")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _best_detection_qc(quality_flags: dict[str, Any]) -> dict[str, Any]:
    container = quality_flags.get("detection_catalog_qc") if isinstance(quality_flags, dict) else None
    if not isinstance(container, dict):
        return {}
    candidates = [value for value in container.values() if isinstance(value, dict)]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: int(item.get("n_matched") or 0))


def _extract_pixel_scale_arcsec(frame: Any, header: Any, quality_flags: dict[str, Any]) -> float | None:
    wcs_check = quality_flags.get("wcs_check") if isinstance(quality_flags, dict) else None
    candidates = []
    if isinstance(wcs_check, dict):
        candidates.extend([
            wcs_check.get("pixel_scale_arcsec_mean"),
            (wcs_check.get("details") or {}).get("pixel_scale_arcsec_mean")
            if isinstance(wcs_check.get("details"), dict)
            else None,
        ])
    for key in ("PIXSCALE", "SECPIX", "PIXSCAL1"):
        try:
            candidates.append(header.get(key) if header is not None else None)
        except Exception:
            pass
    for value in candidates:
        out = _finite_or_none(value)
        if out is not None and out > 0:
            return out
    try:
        cd11 = _finite_or_none(header.get("CD1_1"))
        cd12 = _finite_or_none(header.get("CD1_2"))
        cd21 = _finite_or_none(header.get("CD2_1"))
        cd22 = _finite_or_none(header.get("CD2_2"))
        if None not in (cd11, cd12, cd21, cd22):
            scale = math.sqrt(abs(cd11 * cd22 - cd12 * cd21)) * 3600.0
            if math.isfinite(scale) and scale > 0:
                return scale
        cdelt1 = abs(float(header.get("CDELT1"))) * 3600.0
        cdelt2 = abs(float(header.get("CDELT2"))) * 3600.0
        scale = math.sqrt(cdelt1 * cdelt2)
        return scale if math.isfinite(scale) and scale > 0 else None
    except Exception:
        return None


def _extract_header_fwhm_arcsec(
    frame: Any,
    header: Any,
    *,
    pixel_scale_arcsec: float | None,
    fwhm_header_unit: str,
) -> float | None:
    frame_header = getattr(frame, "header_json", None) or {}
    for key in ("SEXFWHM", "SEX_FWHM", "FWHM", "FWHMARC", "FWHM_ARCSEC", "SEEING", "SEEING1", "PSF_FWHM"):
        value = _finite_or_none(frame_header.get(key))
        if value is None and header is not None:
            try:
                value = _finite_or_none(header.get(key))
            except Exception:
                value = None
        if value is None or value <= 0:
            continue
        unit = str(fwhm_header_unit or "arcsec").lower()
        if unit == "pix":
            return value * pixel_scale_arcsec if pixel_scale_arcsec is not None else None
        if unit == "auto" and pixel_scale_arcsec is not None and 1.0 <= value <= 8.0:
            candidate = value * pixel_scale_arcsec
            if 0.3 <= candidate <= 20.0:
                return candidate
        return value
    return None


def _corner_metrics(model: dict[str, Any]) -> dict[str, float | None]:
    if not model.get("accepted"):
        return {
            "correction_center": None,
            "correction_corner_min": None,
            "correction_corner_max": None,
            "correction_corner_peak_to_peak": None,
        }
    coeff = _coefficients(model.get("coefficients"))
    if coeff is None:
        return {
            "correction_center": None,
            "correction_corner_min": None,
            "correction_corner_max": None,
            "correction_corner_peak_to_peak": None,
        }
    center = coeff[0]
    corners = [coeff[0] + coeff[1] * x + coeff[2] * y for x in (-1.0, 1.0) for y in (-1.0, 1.0)]
    return {
        "correction_center": float(center),
        "correction_corner_min": float(min(corners)),
        "correction_corner_max": float(max(corners)),
        "correction_corner_peak_to_peak": float(max(corners) - min(corners)),
    }


def _gradient_metrics(coeff: tuple[float, float, float] | None) -> dict[str, Any]:
    if coeff is None:
        return {"magnitude": None, "angle_deg": None, "dominant_axis": None}
    cx, cy = coeff[1], coeff[2]
    magnitude = math.hypot(cx, cy)
    if magnitude <= 0:
        return {"magnitude": 0.0, "angle_deg": None, "dominant_axis": None}
    return {
        "magnitude": float(magnitude),
        "angle_deg": float(math.degrees(math.atan2(cy, cx)) % 360.0),
        "dominant_axis": "x" if abs(cx) >= abs(cy) else "y",
    }


def _gradient_sign_consistency(
    rows: list[dict[str, Any]],
    *,
    angle_key: str = "gradient_angle_deg",
) -> float | None:
    pairs = []
    for row in rows:
        angle = _finite_or_none(row.get(angle_key))
        if angle is None:
            continue
        rad = math.radians(angle)
        pairs.append((1 if math.cos(rad) >= 0 else -1, 1 if math.sin(rad) >= 0 else -1))
    if not pairs:
        return None
    counts = {pair: pairs.count(pair) for pair in set(pairs)}
    return float(max(counts.values()) / len(pairs))


def _association(
    rows: list[dict[str, Any]],
    *,
    x_key: str,
    y_key: str,
    scope: str,
    y_transform: Any | None = None,
) -> dict[str, Any]:
    x_values = []
    y_values = []
    for row in rows:
        x = _finite_or_none(row.get(x_key))
        y = _finite_or_none(y_transform(row) if y_transform is not None else row.get(y_key))
        if x is None or y is None:
            continue
        x_values.append(x)
        y_values.append(y)
    rho = _spearman_rho(x_values, y_values)
    return {
        "scope": scope,
        "x": x_key,
        "y": y_key,
        "n": len(x_values),
        "spearman_rho": rho,
    }


def _spearman_rho(x_values: list[float], y_values: list[float]) -> float | None:
    if len(x_values) < 3 or len(x_values) != len(y_values):
        return None
    x_rank = _rankdata(x_values)
    y_rank = _rankdata(y_values)
    x_centered = x_rank - np.mean(x_rank)
    y_centered = y_rank - np.mean(y_rank)
    denom = float(math.sqrt(float(np.sum(x_centered**2) * np.sum(y_centered**2))))
    if denom <= 0:
        return None
    return float(np.sum(x_centered * y_centered) / denom)


def _rankdata(values: list[float]) -> np.ndarray:
    order = sorted(range(len(values)), key=lambda idx: values[idx])
    ranks = np.zeros(len(values), dtype=float)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = 0.5 * ((i + 1) + j)
        for idx in order[i:j]:
            ranks[idx] = rank
        i = j
    return ranks


def _directional_mean_deg(values: Iterable[Any]) -> tuple[float | None, float | None]:
    angles = [_finite_or_none(value) for value in values]
    angles = [value for value in angles if value is not None]
    if not angles:
        return None, None
    cos_mean = float(np.mean([math.cos(math.radians(value)) for value in angles]))
    sin_mean = float(np.mean([math.sin(math.radians(value)) for value in angles]))
    resultant = math.hypot(cos_mean, sin_mean)
    angle = math.degrees(math.atan2(sin_mean, cos_mean)) % 360.0
    return float(angle), float(resultant)


def _axial_mean_deg(values: Iterable[Any]) -> float | None:
    angles = [_finite_or_none(value) for value in values]
    angles = [value for value in angles if value is not None]
    if not angles:
        return None
    cos_mean = float(np.mean([math.cos(math.radians(2.0 * value)) for value in angles]))
    sin_mean = float(np.mean([math.sin(math.radians(2.0 * value)) for value in angles]))
    return float((0.5 * math.degrees(math.atan2(sin_mean, cos_mean))) % 180.0)


def _axis_delta_deg(direction_deg: Any, axis_deg: Any) -> float | None:
    direction = _finite_or_none(direction_deg)
    axis = _finite_or_none(axis_deg)
    if direction is None or axis is None:
        return None
    delta = abs((direction % 180.0) - (axis % 180.0))
    return float(min(delta, 180.0 - delta))


def _coefficients(value: Any) -> tuple[float, float, float] | None:
    try:
        values = [float(item) for item in value]
    except Exception:
        return None
    if len(values) < 3 or not all(math.isfinite(item) for item in values[:3]):
        return None
    return float(values[0]), float(values[1]), float(values[2])


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    return names


def _finite_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _int_or_none(value: Any) -> int | None:
    out = _finite_or_none(value)
    return int(out) if out is not None else None


def _median(values: Iterable[Any]) -> float | None:
    finite = [_finite_or_none(value) for value in values]
    finite = [value for value in finite if value is not None]
    return float(median(finite)) if finite else None
