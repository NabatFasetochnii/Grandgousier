from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from grandgousier.db.models import (
    CalibrationFrameTerm,
    CalibrationIteration,
    CalibrationIterationObject,
    CalibrationVersion,
    Measurement,
    ScienceMeasurement,
)
from grandgousier.photometry.science_flags import merge_effective_science_flags


_RECONSTRUCTED_FIELDS = (
    "x",
    "y",
    "flux",
    "flux_err",
    "mag_inst",
    "mag_cal",
    "mag_err",
    "mag_err_random",
    "mag_err_systematic",
    "aperture_radius_pix",
    "local_background",
    "local_zp_correction",
    "centroid_dx_pix",
    "centroid_dy_pix",
)


@dataclass(slots=True)
class ReconstructionStageResult:
    calibration_version_id: int
    product_kind: str
    iteration_id: int | None
    input_rows: int
    output_rows: int
    status: str = "OK"
    reason: str | None = None


@dataclass(slots=True)
class CalibrationReconstructionState:
    target_calibration_version_id: int
    target_calibration_name: str
    base_calibration_version_id: int
    base_calibration_name: str
    chain: tuple[CalibrationVersion, ...]
    states: dict[tuple[int, int], dict[str, Any]]
    stages: tuple[ReconstructionStageResult, ...]


def reconstruct_calibration_measurement_states(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    frame_ids: Iterable[int] | None = None,
    object_ids: Iterable[int] | None = None,
    apply_historical_flag_overrides: bool = False,
) -> CalibrationReconstructionState:
    """Authoritatively reconstruct one calibration from base + normalized terms.

    Unlike :func:`calibration_reconstruction_parity_report`, this function does
    not read the target materialized layer.  It is therefore suitable for the
    D2/D3 reader cutover and runs where intermediate measurement layers no
    longer exist.  Clean 0.7 reconstruction uses current normalized flag
    semantics.  Frozen 0.6 forensic callers may explicitly request historical
    compatibility flag overrides.
    """

    target = _resolve_calibration_version(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    chain = _chain_from_base(session, target)
    if not chain or str(chain[0].product_kind or "").upper() != "BASE_PHOTOMETRY":
        raise ReconstructionStateError("calibration chain does not terminate at BASE_PHOTOMETRY")
    base = chain[0]
    states = _load_measurement_states(
        session,
        int(base.calibration_version_id),
        frame_ids=frame_ids,
        object_ids=object_ids,
    )
    stages: list[ReconstructionStageResult] = []
    for cv in chain[1:]:
        before = len(states)
        iteration = _latest_iteration(session, int(cv.calibration_version_id))
        if iteration is None:
            raise ReconstructionStateError(
                f"calibration {cv.calibration_version_id} ({cv.name}) has no normalized iteration"
            )
        frame_terms = list(
            session.scalars(
                select(CalibrationFrameTerm).where(
                    CalibrationFrameTerm.calibration_iteration_id
                    == int(iteration.calibration_iteration_id)
                )
            ).all()
        )
        object_terms = list(
            session.scalars(
                select(CalibrationIterationObject).where(
                    CalibrationIterationObject.calibration_iteration_id
                    == int(iteration.calibration_iteration_id)
                )
            ).all()
        )
        states = _apply_stage(
            states,
            product_kind=str(cv.product_kind or ""),
            iteration=iteration,
            frame_terms=frame_terms,
            object_terms=object_terms,
            calibration_version=cv,
        )
        stages.append(
            ReconstructionStageResult(
                calibration_version_id=int(cv.calibration_version_id),
                product_kind=str(cv.product_kind or ""),
                iteration_id=int(iteration.calibration_iteration_id),
                input_rows=before,
                output_rows=len(states),
            )
        )
    if apply_historical_flag_overrides:
        # Local import avoids a module cycle: the backfill implementation uses
        # this reconstruction function with historical overrides explicitly
        # disabled while deriving the exact patch from V1.
        from grandgousier.photometry.science_flag_overrides import (
            apply_complete_science_flag_overrides,
        )

        states = apply_complete_science_flag_overrides(
            session,
            calibration_version_id=int(target.calibration_version_id),
            states=states,
        )
    return CalibrationReconstructionState(
        target_calibration_version_id=int(target.calibration_version_id),
        target_calibration_name=str(target.name),
        base_calibration_version_id=int(base.calibration_version_id),
        base_calibration_name=str(base.name),
        chain=tuple(chain),
        states=states,
        stages=tuple(stages),
    )


def calibration_reconstruction_parity_report(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    tolerance: float = 1e-10,
    max_details: int = 100,
) -> dict[str, Any]:
    """Reconstruct a materialized calibration from base photometry + normalized terms.

    Measurement flags are deliberately excluded from parity: the cutover target is
    to stop repeating calibration provenance in every measurement row.  All scalar
    photometric state required by downstream science readers is checked.
    """

    target = _resolve_calibration_version(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    chain = _chain_from_base(session, target)
    base = chain[0]
    states = _load_measurement_states(session, int(base.calibration_version_id))
    stages: list[ReconstructionStageResult] = []
    errors: list[dict[str, Any]] = []

    for cv in chain[1:]:
        before = len(states)
        iteration = _latest_iteration(session, int(cv.calibration_version_id))
        if iteration is None:
            stages.append(
                ReconstructionStageResult(
                    calibration_version_id=int(cv.calibration_version_id),
                    product_kind=str(cv.product_kind or ""),
                    iteration_id=None,
                    input_rows=before,
                    output_rows=before,
                    status="NO_NORMALIZED_ITERATION",
                    reason="calibration stage has no normalized iteration",
                )
            )
            errors.append(
                {
                    "scope": "stage",
                    "id": int(cv.calibration_version_id),
                    "field": "normalized_iteration",
                    "reconstructed": None,
                    "materialized": "required",
                }
            )
            break
        frame_terms = list(
            session.scalars(
                select(CalibrationFrameTerm).where(
                    CalibrationFrameTerm.calibration_iteration_id
                    == int(iteration.calibration_iteration_id)
                )
            ).all()
        )
        object_terms = list(
            session.scalars(
                select(CalibrationIterationObject).where(
                    CalibrationIterationObject.calibration_iteration_id
                    == int(iteration.calibration_iteration_id)
                )
            ).all()
        )
        try:
            states = _apply_stage(
                states,
                product_kind=str(cv.product_kind or ""),
                iteration=iteration,
                frame_terms=frame_terms,
                object_terms=object_terms,
                calibration_version=cv,
            )
            stages.append(
                ReconstructionStageResult(
                    calibration_version_id=int(cv.calibration_version_id),
                    product_kind=str(cv.product_kind or ""),
                    iteration_id=int(iteration.calibration_iteration_id),
                    input_rows=before,
                    output_rows=len(states),
                )
            )
        except ReconstructionStateError as exc:
            stages.append(
                ReconstructionStageResult(
                    calibration_version_id=int(cv.calibration_version_id),
                    product_kind=str(cv.product_kind or ""),
                    iteration_id=int(iteration.calibration_iteration_id),
                    input_rows=before,
                    output_rows=len(states),
                    status="INSUFFICIENT_NORMALIZED_STATE",
                    reason=str(exc),
                )
            )
            errors.append(
                {
                    "scope": "stage",
                    "id": int(cv.calibration_version_id),
                    "field": "normalized_state",
                    "reconstructed": str(exc),
                    "materialized": "required",
                }
            )
            break

    materialized = _load_measurement_states(session, int(target.calibration_version_id))
    comparison_source = "LEGACY_MEASUREMENTS"
    if not materialized:
        materialized = _load_science_measurement_states(
            session, int(target.calibration_version_id)
        )
        comparison_source = (
            "SCIENCE_MEASUREMENTS" if materialized else "NONE"
        )
    compared = 0
    field_checks = 0
    max_abs_delta: dict[str, float] = {}
    all_keys = sorted(set(states) | set(materialized))
    for key in all_keys:
        predicted = states.get(key)
        actual = materialized.get(key)
        if predicted is None:
            if len(errors) < max_details:
                errors.append(
                    {
                        "scope": "measurement",
                        "id": f"{key[0]}:{key[1]}",
                        "field": "row",
                        "reconstructed": None,
                        "materialized": "present",
                    }
                )
            continue
        if actual is None:
            if len(errors) < max_details:
                errors.append(
                    {
                        "scope": "measurement",
                        "id": f"{key[0]}:{key[1]}",
                        "field": "row",
                        "reconstructed": "present",
                        "materialized": None,
                    }
                )
            continue
        compared += 1
        for field_name in _RECONSTRUCTED_FIELDS:
            field_checks += 1
            left = predicted.get(field_name)
            right = actual.get(field_name)
            delta = _absolute_delta(left, right)
            if delta is not None:
                max_abs_delta[field_name] = max(max_abs_delta.get(field_name, 0.0), delta)
            if not _equivalent(left, right, tolerance):
                if len(errors) < max_details:
                    errors.append(
                        {
                            "scope": "measurement",
                            "id": f"{key[0]}:{key[1]}",
                            "field": field_name,
                            "reconstructed": left,
                            "materialized": right,
                        }
                    )

    structural_mismatches = abs(len(states) - len(materialized))
    mismatch_count = max(structural_mismatches, 0)
    # Details are capped, so count scalar mismatches separately in a second pass.
    scalar_mismatches = 0
    common_keys = set(states) & set(materialized)
    for key in common_keys:
        predicted = states[key]
        actual = materialized[key]
        scalar_mismatches += sum(
            1
            for field_name in _RECONSTRUCTED_FIELDS
            if not _equivalent(predicted.get(field_name), actual.get(field_name), tolerance)
        )
    missing_rows = len(set(materialized) - set(states))
    extra_rows = len(set(states) - set(materialized))
    mismatch_count = missing_rows + extra_rows + scalar_mismatches

    stage_ok = all(stage.status == "OK" for stage in stages)
    status = "OK" if stage_ok and mismatch_count == 0 else "MISMATCH"
    return {
        "status": status,
        "calibration_version_id": int(target.calibration_version_id),
        "calibration_name": target.name,
        "product_kind": target.product_kind,
        "base_calibration_version_id": int(base.calibration_version_id),
        "base_calibration_name": base.name,
        "chain_length": len(chain),
        "chain": [
            {
                "calibration_version_id": int(cv.calibration_version_id),
                "name": cv.name,
                "product_kind": cv.product_kind,
            }
            for cv in chain
        ],
        "base_measurements": _count_measurements(session, int(base.calibration_version_id)),
        "reconstructed_measurements": len(states),
        "materialized_measurements": len(materialized),
        "comparison_source": comparison_source,
        "compared_measurements": compared,
        "field_checks": field_checks,
        "missing_rows": missing_rows,
        "extra_rows": extra_rows,
        "scalar_mismatches": scalar_mismatches,
        "mismatches": mismatch_count,
        "max_abs_delta": max_abs_delta,
        "stages": [
            {
                "calibration_version_id": stage.calibration_version_id,
                "product_kind": stage.product_kind,
                "calibration_iteration_id": stage.iteration_id,
                "input_rows": stage.input_rows,
                "output_rows": stage.output_rows,
                "status": stage.status,
                "reason": stage.reason,
            }
            for stage in stages
        ],
        "details": errors[:max_details],
    }


class ReconstructionStateError(RuntimeError):
    pass


def _apply_stage(
    states: dict[tuple[int, int], dict[str, Any]],
    *,
    product_kind: str,
    iteration: CalibrationIteration,
    frame_terms: Iterable[CalibrationFrameTerm],
    object_terms: Iterable[CalibrationIterationObject],
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    kind = str(product_kind or "").upper()
    iteration_kind = str(getattr(iteration, "iteration_kind", "") or "").upper()
    if iteration_kind == "PHOTOMETRIC_FRAME_QC":
        out = _apply_photometric_frame_qc(
            states,
            frame_terms,
            dict(iteration.parameters or {}),
            calibration_version=calibration_version,
        )
    elif iteration_kind == "SPATIAL_ENSEMBLE":
        out = _apply_spatial_ensemble(
            states,
            frame_terms,
            dict(iteration.parameters or {}),
            calibration_version=calibration_version,
        )
    elif kind == "APERTURE_CORRECTION":
        out = _apply_aperture_correction(
            states,
            frame_terms,
            calibration_version=calibration_version,
        )
    elif kind == "APERTURE_ZP":
        out = _apply_aperture_zp(
            states,
            frame_terms,
            dict(iteration.parameters or {}),
            calibration_version=calibration_version,
        )
    elif kind == "ENSEMBLE":
        out = _apply_ensemble(
            states,
            frame_terms,
            dict(iteration.parameters or {}),
            object_terms=object_terms,
            calibration_version=calibration_version,
        )
    elif kind == "COLOR_CALIBRATION":
        out = _apply_color_calibration(
            states,
            object_terms,
            dict(iteration.metrics or {}),
            dict(iteration.parameters or {}),
            calibration_version=calibration_version,
        )
    else:
        raise ReconstructionStateError(f"unsupported product_kind={product_kind!r}")

    return _apply_residual_floor_provenance(
        out, dict(iteration.metrics or {})
    )


def _apply_photometric_frame_qc(
    states: dict[tuple[int, int], dict[str, Any]],
    frame_terms: Iterable[CalibrationFrameTerm],
    parameters: dict[str, Any],
    *,
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Apply conservative post-ensemble frame QC without changing good/suspect errors.

    GOOD and SUSPECT frames preserve ``mag_cal``, ``mag_err`` and
    ``mag_err_random`` exactly as reconstructed from the source calibration.  The
    estimated excess frame scatter is retained only in flags as a diagnostic. BAD
    frames preserve source provenance but expose ``mag_cal=None`` in this derived
    calibration so they cannot contribute to downstream science products.
    """

    terms = {int(term.frame_id): term for term in frame_terms}
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        row = dict(source)
        term = terms.get(int(row["frame_id"]))
        details = dict(term.details or {}) if term is not None else {}
        status = str(
            details.get("status")
            or (getattr(term, "quality_status", None) if term is not None else None)
            or "GOOD"
        ).upper()
        sigma_frame = _first_finite(
            details.get("sigma_frame_systematic_mag"),
            details.get("sigma_systematic_mag"),
        )
        sigma_frame = max(float(sigma_frame or 0.0), 0.0)

        detector_random_err = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        prior_mag_err = _first_finite(row.get("mag_err"), detector_random_err)
        prior_systematic = _first_finite(row.get("mag_err_systematic"))
        # v3 policy: the frame-scatter estimate is not a calibrated per-star error.
        # Preserve all source uncertainty columns for GOOD/SUSPECT epochs.
        row["mag_err_random"] = detector_random_err
        row["mag_err"] = prior_mag_err
        row["mag_err_systematic"] = prior_systematic

        flags = dict(row.get("flags") or {})
        psf_summary = details.get("psf_summary")
        flags.update(
            {
                "calibration_stage": "photometric_frame_qc",
                "photometric_frame_qc_algorithm": parameters.get("algorithm"),
                "photometric_frame_qc_status": status,
                "photometric_frame_qc_action": details.get("science_action"),
                "photometric_frame_qc_reasons": list(details.get("reasons") or []),
                "photometric_frame_qc_sigma_frame_diagnostic_mag": sigma_frame,
                "photometric_frame_qc_error_policy": "diagnostic_only_v3",
                "mag_err_random_before_frame_qc": detector_random_err,
                "photometric_frame_qc_residual_robust_sigma_mag": details.get(
                    "residual_robust_sigma_mag"
                ),
                "photometric_frame_qc_spatial_peak_to_peak_mag": details.get(
                    "spatial_peak_to_peak_mag"
                ),
                "frame_psf_summary_v1": (
                    dict(psf_summary) if isinstance(psf_summary, dict) else {}
                ),
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
            }
        )

        is_bad = status == "BAD"
        flags["PHOTOMETRIC_FRAME_BAD"] = is_bad
        flags["PHOTOMETRIC_FRAME_SUSPECT"] = status == "SUSPECT"
        if is_bad:
            flags["photometric_frame_qc_reject"] = True
            row["mag_cal"] = None
        else:
            flags["photometric_frame_qc_reject"] = False
        row["flags"] = flags
        out[key] = row
    return out


def _apply_spatial_ensemble(
    states: dict[tuple[int, int], dict[str, Any]],
    frame_terms: Iterable[CalibrationFrameTerm],
    parameters: dict[str, Any],
    *,
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Reconstruct a zero-mean per-frame spatial ensemble correction."""

    terms = {int(term.frame_id): term for term in frame_terms}
    default_bounds = parameters.get("detector_bounds")
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        row = dict(source)
        term = terms.get(int(row["frame_id"]))
        correction = 0.0
        details = dict(term.details or {}) if term is not None else {}
        if term is not None and bool(term.accepted):
            bounds = details.get("detector_bounds") or default_bounds
            correction = _spatial_ensemble_prediction(
                term,
                x=row.get("x"),
                y=row.get("y"),
                bounds=bounds,
            )
        row["mag_cal"] = _add(row.get("mag_cal"), -correction)
        row["local_zp_correction"] = _add(
            row.get("local_zp_correction"), -correction
        )
        flags = dict(row.get("flags") or {})
        flags.update(
            {
                "calibration_stage": "spatial_ensemble_residual_correction",
                "spatial_ensemble_algorithm": parameters.get("algorithm"),
                "spatial_ensemble_model": (
                    str(term.model_name) if term is not None and term.model_name else "identity"
                ),
                "spatial_ensemble_model_accepted": bool(term.accepted) if term is not None else False,
                "spatial_ensemble_correction_mag": float(correction),
                "spatial_ensemble_zero_mean_basis": True,
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
            }
        )
        if details.get("selection_reason") is not None:
            flags["spatial_ensemble_selection_reason"] = details.get("selection_reason")
        if details.get("fallback_reason") is not None:
            flags["spatial_ensemble_fallback_reason"] = details.get("fallback_reason")
        row["flags"] = flags
        out[key] = row
    return out


def _spatial_ensemble_prediction(
    term: CalibrationFrameTerm,
    *,
    x: Any,
    y: Any,
    bounds: Any,
) -> float:
    if not _finite(x) or not _finite(y):
        return 0.0
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise ReconstructionStateError(
            f"frame {term.frame_id} lacks spatial detector bounds"
        )
    xmin, xmax, ymin, ymax = [float(value) for value in bounds]
    if not all(math.isfinite(value) for value in (xmin, xmax, ymin, ymax)):
        raise ReconstructionStateError(
            f"frame {term.frame_id} has non-finite spatial detector bounds"
        )
    xn = 2.0 * (float(x) - xmin) / max(xmax - xmin, 1e-12) - 1.0
    yn = 2.0 * (float(y) - ymin) / max(ymax - ymin, 1e-12) - 1.0
    details = dict(term.details or {})
    coefficient_0 = float(term.coefficient_0 or 0.0)
    coefficient_x = float(term.coefficient_x or 0.0)
    coefficient_y = float(term.coefficient_y or 0.0)
    coefficient_xx = float(details.get("coefficient_xx") or 0.0)
    coefficient_xy = float(details.get("coefficient_xy") or 0.0)
    coefficient_yy = float(details.get("coefficient_yy") or 0.0)
    correction = (
        coefficient_0
        + coefficient_x * xn
        + coefficient_y * yn
        + coefficient_xx * xn * xn
        + coefficient_xy * xn * yn
        + coefficient_yy * yn * yn
    )
    if not math.isfinite(correction):
        raise ReconstructionStateError(
            f"frame {term.frame_id} produced non-finite spatial correction"
        )
    return float(correction)


def _apply_aperture_correction(
    states: dict[tuple[int, int], dict[str, Any]],
    frame_terms: Iterable[CalibrationFrameTerm],
    *,
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    terms = {int(term.frame_id): term for term in frame_terms}
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        row = dict(source)
        term = terms.get(int(row["frame_id"]))
        correction = 0.0
        differential_error = 0.0
        if term is not None and bool(term.accepted):
            correction = _aperture_prediction(term, row.get("x"), row.get("y"))
            differential_error = _aperture_differential_error(term, row.get("x"), row.get("y"))
        scale = 10.0 ** (-0.4 * correction)
        row["flux"] = _scaled(row.get("flux"), scale)
        row["flux_err"] = _scaled(row.get("flux_err"), abs(scale))
        row["mag_inst"] = _add(row.get("mag_inst"), correction)
        row["mag_cal"] = None
        random_err = _quadrature(_first_finite(row.get("mag_err_random"), row.get("mag_err")), differential_error)
        row["mag_err"] = random_err
        row["mag_err_random"] = random_err
        flags = dict(row.get("flags") or {})
        details = dict(term.details or {}) if term is not None else {}
        flags.update(
            {
                "calibration_stage": "spatial_aperture_correction",
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
                "aperture_correction_applied": bool(term is not None and term.accepted),
                "aperture_correction_mag": correction,
                "aperture_correction_model": getattr(term, "model_name", None),
                "aperture_correction_reference_radius_pix": _iteration_parameter_from_cv(
                    calibration_version, "reference_radius_pix"
                ),
                "aperture_correction_n_calibrators": getattr(term, "n_used", None),
                "aperture_correction_residual_mad": getattr(term, "scatter_mad", None),
                "aperture_correction_residual_robust_sigma": details.get(
                    "residual_robust_sigma"
                ),
                "aperture_correction_spatial_peak_to_peak": details.get(
                    "spatial_peak_to_peak"
                ),
                "aperture_correction_max_abs_spatial_deviation": details.get(
                    "max_abs_spatial_deviation"
                ),
                "aperture_correction_max_abs_total_correction": details.get(
                    "max_abs_predicted_correction"
                ),
                "aperture_correction_model_selection_semantics": (
                    "separated_spatial_total_safety_v2"
                ),
                "aperture_correction_legacy_selected_model": details.get(
                    "legacy_selected_model"
                ),
                "aperture_correction_selection_changed_vs_legacy": bool(
                    details.get("selection_changed_vs_legacy")
                ),
                "aperture_correction_legacy_selection_reason": details.get(
                    "legacy_selection_reason"
                ),
                "aperture_correction_differential_error": differential_error,
                "aperture_correction_error_propagation": "differential_spatial_only_v1",
                "aperture_correction_fallback_reason": details.get("fallback_reason"),
            }
        )
        row["flags"] = flags
        out[key] = row
    return out


def _apply_aperture_zp(
    states: dict[tuple[int, int], dict[str, Any]],
    frame_terms: Iterable[CalibrationFrameTerm],
    parameters: dict[str, Any],
    *,
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    terms = {
        int(term.frame_id): term
        for term in frame_terms
        if bool(term.accepted) and _finite(term.offset_mag)
    }
    catalog_name = parameters.get("catalog_name")
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        if catalog_name and (source.get("flags") or {}).get("catalog_name") != catalog_name:
            continue
        term = terms.get(int(source["frame_id"]))
        if term is None or not _finite(source.get("mag_inst")):
            continue
        row = dict(source)
        zp = float(term.offset_mag)
        row["mag_cal"] = float(row["mag_inst"]) + zp
        random_err = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        row["mag_err"] = random_err
        row["mag_err_random"] = random_err
        row["local_zp_correction"] = zp
        flags = dict(row.get("flags") or {})
        flags.update(
            {
                "calibration_stage": "aperture_frame_zp",
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
                "catalog_name": catalog_name or flags.get("catalog_name"),
                "mag_key": parameters.get("mag_key"),
                "frame_aperture_zp": term.offset_mag,
                "frame_aperture_zp_mad": getattr(term, "scatter_mad", None),
                "frame_aperture_zp_std": getattr(term, "scatter_rms", None),
                "frame_aperture_zp_n": getattr(term, "n_reference", None),
                "frame_aperture_zp_candidates": getattr(term, "n_candidates", None),
            }
        )
        row["flags"] = flags
        out[key] = row
    return out


def _apply_ensemble(
    states: dict[tuple[int, int], dict[str, Any]],
    frame_terms: Iterable[CalibrationFrameTerm],
    parameters: dict[str, Any],
    *,
    object_terms: Iterable[CalibrationIterationObject] = (),
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    terms = {
        int(term.frame_id): term
        for term in frame_terms
        if bool(term.accepted) and _finite(term.offset_mag)
    }
    catalog_name = parameters.get("catalog_name")
    prefer_mag_cal = bool(parameters.get("prefer_mag_cal", True))
    object_map = {int(term.object_id): term for term in object_terms}
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        if catalog_name and (source.get("flags") or {}).get("catalog_name") != catalog_name:
            continue
        term = terms.get(int(source["frame_id"]))
        if term is None:
            continue
        value = None
        if prefer_mag_cal and _finite(source.get("mag_cal")):
            value = float(source["mag_cal"])
        elif _finite(source.get("mag_inst")):
            value = float(source["mag_inst"])
        elif _finite(source.get("mag_cal")):
            value = float(source["mag_cal"])
        if value is None:
            continue
        row = dict(source)
        offset = float(term.offset_mag)
        row["mag_cal"] = value - offset
        old_local_zp = row.get("local_zp_correction")
        row["local_zp_correction"] = (
            float(old_local_zp) - offset if _finite(old_local_zp) else -offset
        )
        random_err = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        row["mag_err"] = random_err
        row["mag_err_random"] = random_err
        object_term = object_map.get(int(row["object_id"]))
        flags = dict(row.get("flags") or {})
        flags.update(
            {
                "calibration_stage": "ensemble_differential",
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
                "ensemble_algorithm": "robust_frame_median_v1",
                "ensemble_frame_offset": term.offset_mag,
                "ensemble_frame_n_ref": getattr(term, "n_reference", None),
                "ensemble_frame_scatter_mad": getattr(term, "scatter_mad", None),
                "ensemble_object_is_reference": bool(
                    object_term.is_reference if object_term is not None else False
                ),
                "ensemble_object_requested_reference_exclusion": bool(
                    object_term.requested_exclusion if object_term is not None else False
                ),
                "ensemble_object_excluded_from_reference": bool(
                    object_term.effective_exclusion if object_term is not None else False
                ),
                "ensemble_reference_exclusion_reason": (
                    "candidate_or_target_object"
                    if object_term is not None and object_term.requested_exclusion
                    else None
                ),
                "ensemble_min_ref_stars_per_frame": parameters.get(
                    "min_ref_stars_per_frame"
                ),
                "ensemble_n_iterations": parameters.get("inner_n_iterations"),
            }
        )
        row["flags"] = flags
        out[key] = row
    return out


def _apply_color_calibration(
    states: dict[tuple[int, int], dict[str, Any]],
    object_terms: Iterable[CalibrationIterationObject],
    metrics: dict[str, Any],
    parameters: dict[str, Any] | None = None,
    *,
    calibration_version: CalibrationVersion | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    if not all(_finite(metrics.get(key)) for key in ("transform_offset", "color_term", "color0")):
        raise ReconstructionStateError("color transform coefficients are missing")
    object_map = {int(term.object_id): term for term in object_terms}
    parameters = dict(parameters or {})
    identity_catalog_name = parameters.get("identity_catalog_name")
    eligible_states = {
        key: row
        for key, row in states.items()
        if not identity_catalog_name
        or (row.get("flags") or {}).get("catalog_name") == identity_catalog_name
    }
    source_object_ids = {int(row["object_id"]) for row in eligible_states.values()}
    missing = sorted(source_object_ids - set(object_map))
    if missing:
        preview = ",".join(str(v) for v in missing[:10])
        raise ReconstructionStateError(
            f"color application terms missing for {len(missing)} objects; first={preview}"
        )
    offset = float(metrics["transform_offset"])
    color_term = float(metrics["color_term"])
    color0 = float(metrics["color0"])
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in eligible_states.items():
        term = object_map[int(source["object_id"])]
        details = dict(term.details or {})
        applied_color = details.get("applied_color_value")
        if not _finite(applied_color):
            if bool(details.get("color_missing")):
                applied_color = color0
            else:
                raise ReconstructionStateError(
                    f"object {source['object_id']} lacks applied_color_value"
                )
        correction = offset + color_term * (float(applied_color) - color0)
        row = dict(source)
        source_mag = row.get("mag_cal")
        row["mag_cal"] = float(source_mag) + correction if _finite(source_mag) else None
        random_err = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        row["mag_err"] = random_err
        row["mag_err_random"] = random_err
        row["mag_err_systematic"] = _finite_or_none(details.get("systematic_error"))
        flags = dict(row.get("flags") or {})
        flags.update(
            {
                "calibration_stage": "reference_color_calibration",
                "calibration_method": "ensemble_preserving_reference_color_transform",
                "color_calibration_algorithm": "ensemble_preserving_reference_transform_v3",
                "reference_transform_model": parameters.get("selected_transform_model"),
                "reference_transform_model_selection_reason": parameters.get("model_selection_reason"),
                "source_calibration_version_id": (
                    int(calibration_version.source_calibration_version_id)
                    if calibration_version is not None
                    and calibration_version.source_calibration_version_id is not None
                    else None
                ),
                "source_calibration_name": _source_name(calibration_version),
                "identity_catalog_name": parameters.get("identity_catalog_name"),
                "reference_catalog_name": parameters.get("reference_catalog_name"),
                "reference_catalog_source_id": details.get(
                    "reference_catalog_source_id"
                ),
                "reference_band": parameters.get("reference_band"),
                "reference_mag": details.get("reference_mag"),
                "reference_color_index": parameters.get("color_index"),
                "reference_color_value": details.get("reference_color_value"),
                "reference_color_applied_value": applied_color,
                "reference_color_missing": bool(details.get("color_missing")),
                "reference_color_assumed_pivot": bool(details.get("color_missing")),
                "reference_color0": color0,
                "reference_color_term": color_term,
                "reference_fitted_color_term": metrics.get("fitted_color_term"),
                "reference_fitted_color_term_error": metrics.get("fitted_color_term_err"),
                "reference_transform_offset": offset,
                "reference_transform_offset_error": metrics.get("transform_offset_err"),
                "reference_color_term_error": metrics.get("color_term_err"),
                "reference_transform_systematic_floor": metrics.get("transform_residual_robust_sigma"),
                "reference_color_correction": correction,
                "reference_color_outside_fit_range": bool(
                    details.get("color_outside_fit_range")
                ),
                "reference_color_calibration_applied": bool(_finite(row.get("mag_cal"))),
                "mag_err_systematic": row.get("mag_err_systematic"),
                "reference_color_error": details.get("color_error"),
            }
        )
        row["flags"] = flags
        out[key] = row
    return out


def _apply_residual_floor_provenance(
    states: dict[tuple[int, int], dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[tuple[int, int], dict[str, Any]]:
    floor = _first_finite(metrics.get("residual_noise_floor_mag"))
    if floor is None or floor <= 0:
        return states

    for row in states.values():
        flags = dict(row.get("flags") or {})
        detector = _first_finite(
            flags.get("random_error_physical_mag_err"),
            row.get("mag_err_random"),
            row.get("mag_err"),
        )
        total_random = _first_finite(row.get("mag_err_random"), row.get("mag_err"))
        if detector is None:
            continue
        if total_random is None:
            total_random = detector
        effective_detector_only = math.hypot(float(detector), float(floor))
        effective_total = math.hypot(float(total_random), float(floor))
        flags.update(
            {
                "residual_noise_floor_algorithm": metrics.get(
                    "residual_noise_floor_algorithm"
                ),
                "residual_noise_floor_error_basis": metrics.get(
                    "residual_noise_floor_error_basis"
                ),
                "residual_noise_floor_applied": False,
                "mag_err_detector": detector,
                "mag_err_epoch_floor": floor,
                "mag_err_effective_detector_only": effective_detector_only,
                "mag_err_effective_total": effective_total,
                "mag_err_effective": effective_total,
                "residual_noise_frame_common_sigma_mag": metrics.get(
                    "residual_noise_frame_common_sigma_mag"
                ),
                "residual_noise_frame_common_supported": metrics.get(
                    "residual_noise_frame_common_supported"
                ),
                "residual_noise_frame_common_support_status": metrics.get(
                    "residual_noise_frame_common_support_status"
                ),
                "residual_noise_floor_after_frame_common_mag": metrics.get(
                    "residual_noise_floor_after_frame_common_mag"
                ),
                "residual_noise_independent_floor_mag": metrics.get(
                    "residual_noise_independent_floor_mag"
                ),
            }
        )
        row["flags"] = flags
    return states


def _source_name(calibration_version: CalibrationVersion | None) -> str | None:
    if calibration_version is None:
        return None
    source = getattr(calibration_version, "source_calibration", None)
    name = getattr(source, "name", None)
    if name:
        return str(name)
    parameters = calibration_version.parameters if isinstance(calibration_version.parameters, dict) else {}
    value = parameters.get("source_calibration_name")
    return str(value) if value else None


def _iteration_parameter_from_cv(
    calibration_version: CalibrationVersion | None,
    key: str,
) -> Any:
    if calibration_version is None:
        return None
    parameters = calibration_version.parameters if isinstance(calibration_version.parameters, dict) else {}
    return parameters.get(key)


def _aperture_prediction(term: CalibrationFrameTerm, x: Any, y: Any) -> float:
    if x is None or y is None:
        return 0.0
    details = dict(term.details or {})
    width = _positive_int(details.get("image_width"))
    height = _positive_int(details.get("image_height"))
    if width is None or height is None:
        raise ReconstructionStateError(f"frame {term.frame_id} lacks aperture image dimensions")
    coeff0 = _finite_or_zero(term.coefficient_0)
    coeffx = _finite_or_zero(term.coefficient_x)
    coeffy = _finite_or_zero(term.coefficient_y)
    xn = _normalize_axis(float(x), width)
    yn = _normalize_axis(float(y), height)
    value = coeff0 + coeffx * xn + coeffy * yn
    if not math.isfinite(value):
        raise ReconstructionStateError(f"frame {term.frame_id} produced non-finite aperture correction")
    return float(value)


def _aperture_differential_error(term: CalibrationFrameTerm, x: Any, y: Any) -> float:
    if str(term.model_name or "identity") != "plane" or x is None or y is None:
        return 0.0
    details = dict(term.details or {})
    covariance = details.get("covariance")
    width = _positive_int(details.get("image_width"))
    height = _positive_int(details.get("image_height"))
    if width is None or height is None or not _matrix3(covariance):
        raise ReconstructionStateError(f"frame {term.frame_id} lacks aperture covariance state")
    xn = _normalize_axis(float(x), width)
    yn = _normalize_axis(float(y), height)
    vector = [
        0.0,
        xn - float(details.get("pivot_xnorm") or 0.0),
        yn - float(details.get("pivot_ynorm") or 0.0),
    ]
    variance = 0.0
    for i in range(3):
        for j in range(3):
            variance += vector[i] * float(covariance[i][j]) * vector[j]
    return math.sqrt(max(0.0, variance))


def _chain_from_base(session: Session, target: CalibrationVersion) -> list[CalibrationVersion]:
    reverse_chain: list[CalibrationVersion] = []
    seen: set[int] = set()
    current: CalibrationVersion | None = target
    while current is not None:
        current_id = int(current.calibration_version_id)
        if current_id in seen:
            raise ReconstructionStateError("cycle in calibration source chain")
        seen.add(current_id)
        reverse_chain.append(current)
        if str(current.product_kind or "").upper() == "BASE_PHOTOMETRY":
            break
        source_id = current.source_calibration_version_id
        current = session.get(CalibrationVersion, int(source_id)) if source_id is not None else None
    return list(reversed(reverse_chain))


def _latest_iteration(session: Session, calibration_version_id: int) -> CalibrationIteration | None:
    return session.scalar(
        select(CalibrationIteration)
        .where(CalibrationIteration.calibration_version_id == int(calibration_version_id))
        .order_by(CalibrationIteration.iteration_no.desc(), CalibrationIteration.calibration_iteration_id.desc())
        .limit(1)
    )


def _load_measurement_states(
    session: Session,
    calibration_version_id: int,
    *,
    frame_ids: Iterable[int] | None = None,
    object_ids: Iterable[int] | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    columns = [
        Measurement.measurement_id.label("measurement_id"),
        Measurement.object_id.label("object_id"),
        Measurement.frame_id.label("frame_id"),
        *[getattr(Measurement, field_name).label(field_name) for field_name in _RECONSTRUCTED_FIELDS],
        Measurement.flags.label("flags"),
    ]
    stmt = select(*columns).where(
        Measurement.calibration_version_id == int(calibration_version_id)
    )
    normalized_frame_ids = sorted({int(value) for value in (frame_ids or [])})
    normalized_object_ids = sorted({int(value) for value in (object_ids or [])})
    if normalized_frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(normalized_frame_ids))
    if normalized_object_ids:
        stmt = stmt.where(Measurement.object_id.in_(normalized_object_ids))
    rows = session.execute(
        stmt.order_by(Measurement.object_id, Measurement.frame_id)
    ).mappings()
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for measurement in rows:
        measurement_id = int(measurement["measurement_id"])
        base_flags = dict(measurement["flags"] or {})
        row = {
            "measurement_id": measurement_id,
            "base_measurement_id": measurement_id,
            "object_id": int(measurement["object_id"]),
            "frame_id": int(measurement["frame_id"]),
            **{field_name: measurement[field_name] for field_name in _RECONSTRUCTED_FIELDS},
            # ``flags`` and ``_base_flags`` intentionally share the same initial
            # object.  Calibration stages replace ``flags`` with a copied mapping
            # on first mutation, while ``_base_flags`` keeps the immutable BASE
            # snapshot without doubling the large JSON payload in memory.
            "flags": base_flags,
            "_base_flags": base_flags,
            "_base_calibration_version_id": int(calibration_version_id),
        }
        out[(row["object_id"], row["frame_id"])] = row
    return out


def _load_science_measurement_states(
    session: Session,
    calibration_version_id: int,
) -> dict[tuple[int, int], dict[str, Any]]:
    base_measurement = aliased(Measurement, name="science_base_measurement")
    columns = [
        ScienceMeasurement.science_measurement_id.label("science_measurement_id"),
        ScienceMeasurement.base_measurement_id.label("base_measurement_id"),
        ScienceMeasurement.object_id.label("object_id"),
        ScienceMeasurement.frame_id.label("frame_id"),
        *[getattr(ScienceMeasurement, field_name).label(field_name) for field_name in _RECONSTRUCTED_FIELDS],
        base_measurement.flags.label("base_flags"),
        ScienceMeasurement.flags.label("science_flags"),
    ]
    stmt = (
        select(*columns)
        .outerjoin(
            base_measurement,
            base_measurement.measurement_id == ScienceMeasurement.base_measurement_id,
        )
        .where(ScienceMeasurement.calibration_version_id == int(calibration_version_id))
        .order_by(ScienceMeasurement.object_id, ScienceMeasurement.frame_id)
    )
    rows = session.execute(stmt).mappings()
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for measurement in rows:
        row = {
            "measurement_id": (
                int(measurement["science_measurement_id"])
                if measurement["science_measurement_id"] is not None
                else None
            ),
            "base_measurement_id": (
                int(measurement["base_measurement_id"])
                if measurement["base_measurement_id"] is not None
                else None
            ),
            "object_id": int(measurement["object_id"]),
            "frame_id": int(measurement["frame_id"]),
            **{field_name: measurement[field_name] for field_name in _RECONSTRUCTED_FIELDS},
            "flags": merge_effective_science_flags(
                measurement["base_flags"], measurement["science_flags"]
            ),
            "_base_flags": dict(measurement["base_flags"] or {}),
        }
        out[(row["object_id"], row["frame_id"])] = row
    return out


def _count_measurements(session: Session, calibration_version_id: int) -> int:
    from sqlalchemy import func

    return int(
        session.scalar(
            select(func.count(Measurement.measurement_id)).where(
                Measurement.calibration_version_id == int(calibration_version_id)
            )
        )
        or 0
    )


def _resolve_calibration_version(
    session: Session,
    *,
    calibration_name: str | None,
    calibration_version_id: int | None,
) -> CalibrationVersion:
    cv = None
    if calibration_version_id is not None:
        cv = session.get(CalibrationVersion, int(calibration_version_id))
    elif calibration_name is not None:
        cv = session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == str(calibration_name)))
    if cv is None:
        raise ValueError("Calibration version not found")
    return cv


def _normalize_axis(value: float, size: int) -> float:
    return 2.0 * float(value) / float(max(int(size) - 1, 1)) - 1.0


def _matrix3(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(row, (list, tuple)) and len(row) == 3 for row in value)
    )


def _scaled(value: Any, factor: float) -> float | None:
    if not _finite(value):
        return None
    result = float(value) * float(factor)
    return result if math.isfinite(result) else None


def _add(value: Any, delta: float) -> float | None:
    if not _finite(value):
        return None
    result = float(value) + float(delta)
    return result if math.isfinite(result) else None


def _quadrature(left: Any, right: Any) -> float | None:
    values = [float(v) for v in (left, right) if _finite(v)]
    if not values:
        return None
    return math.sqrt(sum(v * v for v in values))


def _first_finite(*values: Any) -> float | None:
    for value in values:
        if _finite(value):
            return float(value)
    return None


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _finite_or_none(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _finite_or_zero(value: Any) -> float:
    return float(value) if _finite(value) else 0.0


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _equivalent(left: Any, right: Any, tolerance: float) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    try:
        lf = float(left)
        rf = float(right)
    except (TypeError, ValueError):
        return left == right
    if not math.isfinite(lf) or not math.isfinite(rf):
        return lf == rf
    return math.isclose(lf, rf, rel_tol=float(tolerance), abs_tol=float(tolerance))


def _absolute_delta(left: Any, right: Any) -> float | None:
    if not _finite(left) or not _finite(right):
        return None
    return abs(float(left) - float(right))
