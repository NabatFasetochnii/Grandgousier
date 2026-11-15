from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import median
from typing import Any

GAIA_WCS_COM_ALGORITHM = "gaia_wcs_com_v1"
GAIA_WCS_COM_POLICY: dict[str, Any] = {
    "algorithm": GAIA_WCS_COM_ALGORITHM,
    "local_com_max_shift_pix": 2.0,
    "frame_shift_min_matches": 20,
    "frame_shift_max_robust_scatter_pix": 0.5,
    "frame_shift_max_absolute_pix": 2.0,
    "crowded_arcsec": 10.0,
    "blended_arcsec": 5.0,
    "dynamic_separation": True,
    "dynamic_seeing_factor": 1.5,
    "dynamic_aperture_factor": 1.0,
    "dynamic_crowding_factor": 1.5,
    "fwhm_header_unit": "arcsec",
    "neighbor_completeness_required_for_local_com": True,
}


def build_gaia_wcs_com_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return validated effective centering/QC thresholds with approved defaults."""

    policy = dict(GAIA_WCS_COM_POLICY)
    if overrides is not None:
        policy.update(dict(overrides))

    for key in (
        "local_com_max_shift_pix",
        "frame_shift_max_robust_scatter_pix",
        "frame_shift_max_absolute_pix",
        "crowded_arcsec",
        "blended_arcsec",
        "dynamic_seeing_factor",
        "dynamic_aperture_factor",
        "dynamic_crowding_factor",
    ):
        try:
            value = float(policy[key])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{key} must be a finite non-negative number") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite non-negative number")
        policy[key] = value

    raw_min_matches = policy["frame_shift_min_matches"]
    if isinstance(raw_min_matches, bool):
        raise ValueError("frame_shift_min_matches must be a positive integer")
    try:
        min_matches = int(raw_min_matches)
        if float(raw_min_matches) != float(min_matches) or min_matches < 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("frame_shift_min_matches must be a positive integer") from exc
    policy["frame_shift_min_matches"] = min_matches

    if not isinstance(policy.get("dynamic_separation"), bool):
        raise ValueError("dynamic_separation must be a boolean")
    fwhm_unit = str(policy.get("fwhm_header_unit") or "arcsec").strip().lower()
    if fwhm_unit not in {"arcsec", "pix", "auto"}:
        raise ValueError("fwhm_header_unit must be 'arcsec', 'pix', or 'auto'")
    policy["fwhm_header_unit"] = fwhm_unit
    policy["algorithm"] = GAIA_WCS_COM_ALGORITHM
    return policy


@dataclass(frozen=True, slots=True)
class GaiaWcsComCenter:
    x: float | None
    y: float | None
    frame_dx_pix: float
    frame_dy_pix: float
    local_dx_pix: float
    local_dy_pix: float
    method: str
    provenance: dict[str, Any]


def build_gaia_wcs_com_refinements(
    links_by_frame: Mapping[int, Iterable[Any]],
    matched_detections_by_frame: Mapping[int, Iterable[Mapping[str, Any]]],
    frames_by_id: Mapping[int, Any],
    *,
    candidate_pair_evidence_by_frame: Mapping[int, Mapping[str, Any]] | None = None,
    source_ids_by_catalog_source_id: Mapping[int, str],
    nearest_neighbor_by_source_id: Mapping[str, float | None] | None,
    neighbor_completeness_proven: bool,
    neighbor_query_margin_arcmin: float | None = None,
    source_detection_run_id: int | None,
    catalog_match_run_id: int | None,
    aperture_radius_pix: float,
    fwhm_header_unit: str = "arcsec",
    policy: Mapping[str, Any] | None = None,
) -> dict[int, GaiaWcsComCenter]:
    """Build safe per-link center refinements from one exact detector/match run pair.

    The caller owns the transient catalog and all database reads.  This function
    never removes a link or marks it scientifically unacceptable: a rejected local
    centroid falls back to the accepted frame shift, then to the original WCS link.
    Only pairs proven unique by complete candidate evidence from that exact matcher
    run can support either the local centroid or the frame shift.
    """

    if str(GAIA_WCS_COM_POLICY["algorithm"]) != GAIA_WCS_COM_ALGORITHM:
        raise AssertionError("invalid Gaia WCS/COM policy")
    effective_policy = build_gaia_wcs_com_policy(
        {"fwhm_header_unit": fwhm_header_unit, **dict(policy or {})}
    )
    query_margin_arcmin = (
        None
        if isinstance(neighbor_query_margin_arcmin, bool)
        else _finite(neighbor_query_margin_arcmin)
    )
    neighbor_separations = nearest_neighbor_by_source_id or {}
    output: dict[int, GaiaWcsComCenter] = {}

    all_frame_ids = {int(value) for value in links_by_frame}
    all_frame_ids.update(int(value) for value in matched_detections_by_frame)
    for frame_id in sorted(all_frame_ids):
        links = list(links_by_frame.get(frame_id, ()))
        frame = frames_by_id.get(frame_id)
        link_by_catalog_id: dict[int, list[Any]] = defaultdict(list)
        for link in links:
            try:
                link_by_catalog_id[int(_value(link, "catalog_source_id"))].append(link)
            except (TypeError, ValueError, OverflowError):
                continue

        rows = list(matched_detections_by_frame.get(frame_id, ()))
        detection_by_catalog_id: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        catalog_counts: Counter[int] = Counter()
        detection_counts: Counter[int] = Counter()
        current_run_pair = source_detection_run_id is not None and catalog_match_run_id is not None
        for row in rows:
            if str(row.get("match_status", "MATCHED")).upper() != "MATCHED":
                continue
            if not current_run_pair:
                continue
            if _int_or_none(row.get("source_detection_run_id")) != int(source_detection_run_id):
                continue
            if _int_or_none(row.get("catalog_match_run_id")) != int(catalog_match_run_id):
                continue
            catalog_source_id = _int_or_none(row.get("catalog_source_id"))
            source_detection_id = _int_or_none(row.get("source_detection_id"))
            if catalog_source_id is None or source_detection_id is None:
                continue
            detection_by_catalog_id[catalog_source_id].append(row)
            catalog_counts[catalog_source_id] += 1
            detection_counts[source_detection_id] += 1

        frame_candidate_evidence = (
            candidate_pair_evidence_by_frame.get(frame_id)
            if candidate_pair_evidence_by_frame is not None
            else None
        )
        candidate_status_by_pair, candidate_evidence_reason = _candidate_pair_statuses(
            frame_candidate_evidence,
            frame_id=frame_id,
            source_detection_run_id=source_detection_run_id,
            catalog_match_run_id=catalog_match_run_id,
            accepted_rows=(
                row
                for accepted_rows in detection_by_catalog_id.values()
                for row in accepted_rows
            ),
        )

        ambiguous_catalog_ids = {
            catalog_source_id
            for catalog_source_id, count in catalog_counts.items()
            if count != 1 or len(link_by_catalog_id.get(catalog_source_id, ())) != 1
        }
        ambiguous_detection_ids = {
            detection_id for detection_id, count in detection_counts.items() if count != 1
        }

        candidate_rows: list[tuple[Mapping[str, Any], Any, float, float]] = []
        for catalog_source_id, accepted_rows in detection_by_catalog_id.items():
            if catalog_source_id in ambiguous_catalog_ids:
                continue
            row = accepted_rows[0]
            detection_id = _int_or_none(row.get("source_detection_id"))
            if detection_id is None or detection_id in ambiguous_detection_ids:
                continue
            pair_status = candidate_status_by_pair.get(
                (detection_id, catalog_source_id)
            )
            if pair_status is None or pair_status["status"] != "unique":
                continue
            if _is_saturated(row):
                continue
            link = link_by_catalog_id[catalog_source_id][0]
            delta_x = _finite(row.get("x"))
            delta_y = _finite(row.get("y"))
            link_x = _finite(_value(link, "x"))
            link_y = _finite(_value(link, "y"))
            if None in (delta_x, delta_y, link_x, link_y):
                continue
            candidate_rows.append((row, link, float(delta_x) - float(link_x), float(delta_y) - float(link_y)))

        frame_result = _frame_shift(candidate_rows, policy=effective_policy)
        frame_result["candidate_pair_evidence_status"] = (
            "complete" if candidate_evidence_reason is None else candidate_evidence_reason
        )
        frame_result["candidate_match_radius_arcsec"] = (
            _finite(frame_candidate_evidence.get("radius_arcsec"))
            if isinstance(frame_candidate_evidence, Mapping)
            else None
        )
        frame_result["n_unique_accepted_candidate_pairs"] = sum(
            item["status"] == "unique" for item in candidate_status_by_pair.values()
        )
        frame_result["n_ambiguous_accepted_candidate_pairs"] = sum(
            item["status"] == "ambiguous" for item in candidate_status_by_pair.values()
        )
        apply_frame_shift = frame_result["accepted"]
        frame_dx = float(frame_result["dx_pix"]) if apply_frame_shift else 0.0
        frame_dy = float(frame_result["dy_pix"]) if apply_frame_shift else 0.0
        dynamic_limits = _dynamic_separation_limits(
            frame,
            aperture_radius_pix=aperture_radius_pix,
            fwhm_header_unit=str(effective_policy["fwhm_header_unit"]),
            policy=effective_policy,
        )

        candidate_by_catalog_id = {
            int(row["catalog_source_id"]): (row, dx, dy)
            for row, _link, dx, dy in candidate_rows
        }
        for link in links:
            link_id = _int_or_none(_value(link, "frame_catalog_link_id"))
            if link_id is None:
                continue
            catalog_source_id = _int_or_none(_value(link, "catalog_source_id"))
            source_id = str(
                _value(
                    link,
                    "source_id",
                    source_ids_by_catalog_source_id.get(catalog_source_id, "")
                    if catalog_source_id is not None
                    else "",
                )
                or ""
            )
            original_x = _finite(_value(link, "x"))
            original_y = _finite(_value(link, "y"))
            current_x = (
                float(original_x) + frame_dx if original_x is not None else None
            )
            current_y = (
                float(original_y) + frame_dy if original_y is not None else None
            )
            local_dx = 0.0
            local_dy = 0.0
            method = "frame_shift" if apply_frame_shift else "wcs"
            local_reason: str | None = None
            local_details: dict[str, Any] = {}
            source_detection_id: int | None = None
            candidate_pair_status: Mapping[str, Any] | None = None
            candidate = (
                candidate_by_catalog_id.get(catalog_source_id)
                if catalog_source_id is not None
                else None
            )

            accepted_rows = (
                detection_by_catalog_id.get(catalog_source_id, [])
                if catalog_source_id is not None
                else []
            )
            if accepted_rows:
                source_detection_id = _int_or_none(accepted_rows[0].get("source_detection_id"))
                if source_detection_id is not None and catalog_source_id is not None:
                    candidate_pair_status = candidate_status_by_pair.get(
                        (source_detection_id, catalog_source_id)
                    )
            if catalog_source_id is None or not accepted_rows:
                local_reason = "no_current_accepted_match"
            elif catalog_source_id in ambiguous_catalog_ids or (
                source_detection_id is not None
                and source_detection_id in ambiguous_detection_ids
            ):
                local_reason = "ambiguous_current_match"
            elif candidate_pair_status is None:
                local_reason = candidate_evidence_reason or "candidate_evidence_unavailable"
            elif candidate_pair_status["status"] != "unique":
                local_reason = "ambiguous_candidate_match"
                local_details["candidate_detection_pair_count"] = candidate_pair_status[
                    "detection_candidate_count"
                ]
                local_details["candidate_catalog_pair_count"] = candidate_pair_status[
                    "catalog_candidate_count"
                ]
            elif _is_saturated(accepted_rows[0]):
                local_reason = "saturated_detection"
            elif candidate is None:
                local_reason = "nonfinite_detection_or_wcs_position"
            elif not neighbor_completeness_proven:
                local_reason = "neighbor_completeness_unproven"
            elif source_id not in neighbor_separations:
                local_reason = "neighbor_identity_absent_from_complete_catalog"
            else:
                nearest = _finite(neighbor_separations.get(source_id))
                if nearest is None:
                    local_reason = "neighbor_separation_unavailable"
                else:
                    local_details["nearest_neighbor_arcsec"] = nearest
                    local_details["crowded"] = nearest < float(
                        effective_policy["crowded_arcsec"]
                    )
                    local_details["blended"] = nearest < float(
                        effective_policy["blended_arcsec"]
                    )
                    dynamic_blend = dynamic_limits.get("dynamic_blend_arcsec")
                    dynamic_crowded = _finite(
                        dynamic_limits.get("dynamic_crowded_arcsec")
                    )
                    if dynamic_blend is not None:
                        local_details["dynamic_blend_arcsec"] = dynamic_blend
                        local_details["dynamic_blend_risk"] = nearest < dynamic_blend
                        local_details["separation_index"] = (
                            nearest / dynamic_blend if dynamic_blend > 0 else None
                        )
                    if dynamic_crowded is not None:
                        local_details["dynamic_crowded_arcsec"] = dynamic_crowded
                        local_details["dynamic_crowded"] = nearest < dynamic_crowded
                    local_details["possible_blend"] = bool(
                        local_details.get("dynamic_blend_risk", False)
                        or local_details.get("dynamic_crowded", False)
                    )

                    static_guard_arcsec = max(
                        float(effective_policy["crowded_arcsec"]),
                        float(effective_policy["blended_arcsec"]),
                    )
                    required_guard_arcsec = static_guard_arcsec
                    local_details["neighbor_query_margin_arcmin"] = query_margin_arcmin
                    guard_rejection_reason = None
                    if bool(effective_policy["dynamic_separation"]):
                        local_details["dynamic_neighbor_guard_available"] = (
                            dynamic_crowded is not None
                        )
                        if dynamic_crowded is None:
                            guard_rejection_reason = "dynamic_neighbor_guard_unavailable"
                        else:
                            required_guard_arcsec = max(
                                required_guard_arcsec, dynamic_crowded
                            )
                    margin_sufficient = (
                        guard_rejection_reason is None
                        and query_margin_arcmin is not None
                        and query_margin_arcmin >= required_guard_arcsec / 60.0
                    )
                    local_details[
                        "required_neighbor_guard_arcsec"
                    ] = required_guard_arcsec
                    local_details["neighbor_margin_sufficient"] = margin_sufficient
                    if guard_rejection_reason is None and not margin_sufficient:
                        guard_rejection_reason = "neighbor_query_margin_insufficient"

                    veto_blend = bool(
                        local_details["blended"]
                        or local_details.get("dynamic_blend_risk", False)
                        or local_details.get("dynamic_crowded", False)
                    )
                    veto_crowded = bool(local_details["crowded"])
                    if veto_blend:
                        local_reason = "blend_veto"
                    elif veto_crowded:
                        local_reason = "crowded_neighbor_veto"
                    elif guard_rejection_reason is not None:
                        local_reason = guard_rejection_reason
                    else:
                        row, raw_dx, raw_dy = candidate
                        residual_x = raw_dx - frame_dx
                        residual_y = raw_dy - frame_dy
                        local_details["candidate_dx_pix"] = raw_dx
                        local_details["candidate_dy_pix"] = raw_dy
                        original_link_offset_magnitude = math.hypot(raw_dx, raw_dy)
                        local_details[
                            "original_link_offset_magnitude_pix"
                        ] = original_link_offset_magnitude
                        local_details["residual_dx_pix"] = residual_x
                        local_details["residual_dy_pix"] = residual_y
                        residual_magnitude = math.hypot(residual_x, residual_y)
                        local_details["residual_magnitude_pix"] = residual_magnitude
                        if original_link_offset_magnitude > float(
                            effective_policy["local_com_max_shift_pix"]
                        ):
                            local_reason = "local_com_shift_exceeds_limit"
                        else:
                            local_dx = residual_x
                            local_dy = residual_y
                            method = "local_com"
                            local_reason = None
                            if current_x is not None:
                                current_x += local_dx
                            if current_y is not None:
                                current_y += local_dy

            position_fallback_reason = (
                None
                if method == "local_com"
                else (
                    local_reason
                    if local_reason is not None
                    else frame_result.get("reason")
                )
            )
            provenance = {
                "algorithm": GAIA_WCS_COM_ALGORITHM,
                "method": method,
                "position_method": method,
                "position_fallback_reason": position_fallback_reason,
                "original_link_x": original_x,
                "original_link_y": original_y,
                "frame_shift_dx_pix": frame_dx,
                "frame_shift_dy_pix": frame_dy,
                "frame_shift_applied": bool(apply_frame_shift),
                "frame_shift": dict(frame_result),
                "local_com_dx_pix": local_dx,
                "local_com_dy_pix": local_dy,
                "local_com_rejection_reason": local_reason,
                "local_com_neighbor_diagnostics": local_details,
                "neighbor_completeness_proven": bool(neighbor_completeness_proven),
                "source_detection_run_id": source_detection_run_id,
                "catalog_match_run_id": catalog_match_run_id,
                "source_detection_id": source_detection_id,
                "catalog_source_id": catalog_source_id,
                "source_id": source_id or None,
                "policy": dict(effective_policy),
            }
            output[link_id] = GaiaWcsComCenter(
                x=current_x,
                y=current_y,
                frame_dx_pix=frame_dx,
                frame_dy_pix=frame_dy,
                local_dx_pix=local_dx,
                local_dy_pix=local_dy,
                method=method,
                provenance=provenance,
            )
    return output


def _candidate_pair_statuses(
    evidence: Mapping[str, Any] | None,
    *,
    frame_id: int,
    source_detection_run_id: int | None,
    catalog_match_run_id: int | None,
    accepted_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[int, int], dict[str, Any]], str | None]:
    """Validate exact-run candidate evidence and identify unique accepted pairs."""

    if evidence is None:
        return {}, "candidate_evidence_unavailable"
    if not isinstance(evidence, Mapping) or evidence.get("complete") is not True:
        return {}, "candidate_evidence_invalid"
    if (
        source_detection_run_id is None
        or catalog_match_run_id is None
        or _int_or_none(evidence.get("source_detection_run_id"))
        != int(source_detection_run_id)
        or _int_or_none(evidence.get("catalog_match_run_id"))
        != int(catalog_match_run_id)
        or _int_or_none(evidence.get("frame_id")) != int(frame_id)
    ):
        return {}, "candidate_evidence_scope_mismatch"

    radius = _finite(evidence.get("radius_arcsec"))
    if radius is None or radius <= 0:
        return {}, "candidate_radius_unavailable_or_invalid"

    raw_pairs = evidence.get("accepted_pairs")
    if not isinstance(raw_pairs, (list, tuple)):
        return {}, "candidate_evidence_invalid"

    statuses: dict[tuple[int, int], dict[str, Any]] = {}
    for pair in raw_pairs:
        if not isinstance(pair, Mapping):
            return {}, "candidate_evidence_invalid"
        detection_id = _strict_int(pair.get("source_detection_id"))
        catalog_source_id = _strict_int(pair.get("catalog_source_id"))
        detection_count = _strict_int(pair.get("detection_candidate_count"))
        catalog_count = _strict_int(pair.get("catalog_candidate_count"))
        if (
            detection_id is None
            or catalog_source_id is None
            or detection_count is None
            or catalog_count is None
            or detection_count < 1
            or catalog_count < 1
        ):
            return {}, "candidate_evidence_invalid"
        pair_key = (detection_id, catalog_source_id)
        if pair_key in statuses:
            return {}, "candidate_evidence_invalid"
        statuses[pair_key] = {
            "status": (
                "unique" if detection_count == 1 and catalog_count == 1 else "ambiguous"
            ),
            "detection_candidate_count": detection_count,
            "catalog_candidate_count": catalog_count,
        }

    accepted_pair_counts: Counter[tuple[int, int]] = Counter()
    for row in accepted_rows:
        detection_id = _int_or_none(row.get("source_detection_id"))
        catalog_source_id = _int_or_none(row.get("catalog_source_id"))
        if detection_id is not None and catalog_source_id is not None:
            accepted_pair_counts[(detection_id, catalog_source_id)] += 1
    if accepted_pair_counts != Counter({key: 1 for key in statuses}):
        return {}, "candidate_evidence_scope_mismatch"
    return statuses, None


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        if float(value) != float(result):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return result


def compose_final_center(
    *,
    original_x: float,
    original_y: float,
    base_x: float,
    base_y: float,
    psf_residual_dx_pix: float | None,
    psf_residual_dy_pix: float | None,
    provenance: Mapping[str, Any],
) -> tuple[float, float, float, float, dict[str, Any]]:
    """Compose the optional PSF residual and preserve full link-relative offsets."""

    final_x = float(base_x) + (float(psf_residual_dx_pix) if psf_residual_dx_pix is not None else 0.0)
    final_y = float(base_y) + (float(psf_residual_dy_pix) if psf_residual_dy_pix is not None else 0.0)
    full_dx = final_x - float(original_x)
    full_dy = final_y - float(original_y)
    details = dict(provenance)
    details.update(
        {
            "base_center_x": float(base_x),
            "base_center_y": float(base_y),
            "psf_residual_dx_pix": _finite(psf_residual_dx_pix),
            "psf_residual_dy_pix": _finite(psf_residual_dy_pix),
            "final_x": final_x,
            "final_y": final_y,
            "full_offset_dx_pix": full_dx,
            "full_offset_dy_pix": full_dy,
        }
    )
    return final_x, final_y, full_dx, full_dy, details


def _frame_shift(
    candidate_rows: list[tuple[Mapping[str, Any], Any, float, float]],
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    dxs = [float(row[2]) for row in candidate_rows]
    dys = [float(row[3]) for row in candidate_rows]
    n_matches = len(dxs)
    dx = median(dxs) if dxs else 0.0
    dy = median(dys) if dys else 0.0
    scatter_x = 1.4826 * median([abs(value - dx) for value in dxs]) if dxs else 0.0
    scatter_y = 1.4826 * median([abs(value - dy) for value in dys]) if dys else 0.0
    scatter = max(scatter_x, scatter_y)
    magnitude = math.hypot(dx, dy)
    reason = None
    if n_matches < int(policy["frame_shift_min_matches"]):
        reason = "insufficient_current_unsaturated_matches"
    elif scatter > float(policy["frame_shift_max_robust_scatter_pix"]):
        reason = "robust_scatter_exceeds_limit"
    elif magnitude > float(policy["frame_shift_max_absolute_pix"]):
        reason = "absolute_frame_shift_exceeds_limit"
    return {
        "accepted": reason is None,
        "reason": reason,
        "n_current_unsaturated_accepted_matches": n_matches,
        "dx_pix": float(dx),
        "dy_pix": float(dy),
        "magnitude_pix": float(magnitude),
        "robust_scatter_pix": float(scatter),
        "robust_scatter_dx_pix": float(scatter_x),
        "robust_scatter_dy_pix": float(scatter_y),
    }


def _dynamic_separation_limits(
    frame: Any,
    *,
    aperture_radius_pix: float,
    fwhm_header_unit: str,
    policy: Mapping[str, Any],
) -> dict[str, float]:
    if frame is None or not bool(policy["dynamic_separation"]):
        return {}
    pixel_scale = _pixel_scale_arcsec(frame)
    header = _value(frame, "header_json", {}) or {}
    fwhm_value = None
    if isinstance(header, Mapping):
        for key in (
            "SEXFWHM",
            "SEX_FWHM",
            "FWHM",
            "FWHMARC",
            "FWHM_ARCSEC",
            "SEEING",
            "SEEING1",
            "PSF_FWHM",
        ):
            fwhm_value = _finite(header.get(key))
            if fwhm_value is not None:
                break
    unit = str(fwhm_header_unit or "arcsec").strip().lower()
    fwhm_arcsec = None
    if fwhm_value is not None and fwhm_value > 0:
        if unit == "pix":
            fwhm_arcsec = fwhm_value * pixel_scale if pixel_scale is not None else None
        elif unit == "auto" and pixel_scale is not None and 1.0 <= fwhm_value <= 8.0:
            pixel_value = fwhm_value * pixel_scale
            fwhm_arcsec = pixel_value if 0.3 <= pixel_value <= 20.0 else fwhm_value
        else:
            fwhm_arcsec = fwhm_value
    components: list[float] = []
    if fwhm_arcsec is not None:
        components.append(float(policy["dynamic_seeing_factor"]) * fwhm_arcsec)
    if pixel_scale is not None:
        aperture_arcsec = float(aperture_radius_pix) * pixel_scale
        if math.isfinite(aperture_arcsec) and aperture_arcsec > 0:
            components.append(float(policy["dynamic_aperture_factor"]) * aperture_arcsec)
    if not components:
        return {}
    blend = max(components)
    return {
        "dynamic_blend_arcsec": blend,
        "dynamic_crowded_arcsec": float(policy["dynamic_crowding_factor"]) * blend,
    }


def _pixel_scale_arcsec(frame: Any) -> float | None:
    unit = _value(frame, "instrument_unit")
    value = _finite(_value(unit, "pixel_scale_arcsec")) if unit is not None else None
    if value is not None and value > 0:
        return value
    flags = _value(frame, "quality_flags", {}) or {}
    if not isinstance(flags, Mapping):
        return None
    wcs_check = flags.get("wcs_check")
    candidates = [
        flags.get("pixel_scale_arcsec_mean"),
        wcs_check.get("pixel_scale_arcsec_mean") if isinstance(wcs_check, Mapping) else None,
        (
            (wcs_check.get("details") or {}).get("pixel_scale_arcsec_mean")
            if isinstance(wcs_check, Mapping)
            and isinstance(wcs_check.get("details"), Mapping)
            else None
        ),
    ]
    for candidate in candidates:
        value = _finite(candidate)
        if value is not None and value > 0:
            return value
    return None


def _is_saturated(row: Mapping[str, Any]) -> bool:
    flags = row.get("flags")
    if not isinstance(flags, Mapping):
        return False
    return any(
        bool(flags.get(key))
        for key in (
            "saturated_or_near_saturated",
            "saturated",
            "SATURATED",
            "SATURATED_LIKE",
        )
    )


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _int_or_none(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
