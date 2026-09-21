from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationFrameTerm,
    CalibrationIteration,
    CalibrationIterationObject,
    CalibrationVersion,
    Measurement,
)


@dataclass(slots=True)
class FrameTermInput:
    frame_id: int
    term_kind: str
    model_name: str | None = None
    coefficient_0: float | None = None
    coefficient_x: float | None = None
    coefficient_y: float | None = None
    offset_mag: float | None = None
    offset_err: float | None = None
    scatter_mad: float | None = None
    scatter_rms: float | None = None
    n_candidates: int | None = None
    n_used: int | None = None
    n_reference: int | None = None
    accepted: bool = True
    quality_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ObjectTermInput:
    object_id: int
    role: str = "NON_REFERENCE"
    is_reference: bool = False
    requested_exclusion: bool = False
    effective_exclusion: bool = False
    n_measurements: int | None = None
    median_mag: float | None = None
    rms: float | None = None
    mad: float | None = None
    status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedCalibrationWriteResult:
    calibration_iteration_id: int
    frame_terms: int
    object_terms: int


def replace_calibration_iteration(
    session: Session,
    *,
    calibration_version_id: int,
    source_calibration_version_id: int | None,
    iteration_kind: str,
    iteration_no: int = 1,
    algorithm: str,
    status: str = "COMPLETE",
    convergence_status: str | None = None,
    convergence_reason: str | None = None,
    is_final: bool = True,
    parameters: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    frame_terms: Iterable[FrameTermInput] = (),
    object_terms: Iterable[ObjectTermInput] = (),
) -> NormalizedCalibrationWriteResult:
    """Replace compact model state for one significant calibration pass.

    This is intentionally a dual-write helper: callers still materialize the
    compatibility Measurement layer, then store only the model state required
    to reproduce/audit that layer.  The operation does not commit; the caller's
    existing calibration transaction remains authoritative.
    """

    cv = session.get(CalibrationVersion, int(calibration_version_id))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_version_id}")

    iteration_no = int(iteration_no)
    if iteration_no < 1:
        raise ValueError("iteration_no must be >= 1")
    iteration_kind = str(iteration_kind).strip().upper()
    if not iteration_kind:
        raise ValueError("iteration_kind is required")

    existing_ids = list(
        session.scalars(
            select(CalibrationIteration.calibration_iteration_id).where(
                CalibrationIteration.calibration_version_id == int(calibration_version_id),
                CalibrationIteration.iteration_kind == iteration_kind,
                CalibrationIteration.iteration_no == iteration_no,
            )
        ).all()
    )
    if existing_ids:
        session.execute(
            delete(CalibrationIteration).where(
                CalibrationIteration.calibration_iteration_id.in_([int(v) for v in existing_ids])
            )
        )
        session.flush()

    iteration = CalibrationIteration(
        calibration_version_id=int(calibration_version_id),
        source_calibration_version_id=(
            int(source_calibration_version_id) if source_calibration_version_id is not None else None
        ),
        pipeline_run_id=_optional_int(getattr(cv, "pipeline_run_id", None)),
        field_id=_optional_int(getattr(cv, "field_id", None)),
        series_id=_optional_int(getattr(cv, "series_id", None)),
        iteration_kind=iteration_kind,
        iteration_no=iteration_no,
        algorithm=str(algorithm),
        status=str(status),
        convergence_status=str(convergence_status) if convergence_status is not None else None,
        convergence_reason=str(convergence_reason) if convergence_reason is not None else None,
        is_final=bool(is_final),
        parameters=_json_safe(parameters or {}),
        metrics=_json_safe(metrics or {}),
    )
    session.add(iteration)
    session.flush()
    iteration_id = int(iteration.calibration_iteration_id)

    frame_rows: list[CalibrationFrameTerm] = []
    seen_frames: set[tuple[int, str]] = set()
    for item in frame_terms:
        key = (int(item.frame_id), str(item.term_kind).upper())
        if key in seen_frames:
            raise ValueError(f"Duplicate normalized frame term: frame_id={key[0]}, kind={key[1]}")
        seen_frames.add(key)
        frame_rows.append(
            CalibrationFrameTerm(
                calibration_iteration_id=iteration_id,
                frame_id=int(item.frame_id),
                term_kind=key[1],
                model_name=_optional_str(item.model_name),
                coefficient_0=_finite_or_none(item.coefficient_0),
                coefficient_x=_finite_or_none(item.coefficient_x),
                coefficient_y=_finite_or_none(item.coefficient_y),
                offset_mag=_finite_or_none(item.offset_mag),
                offset_err=_finite_or_none(item.offset_err),
                scatter_mad=_finite_or_none(item.scatter_mad),
                scatter_rms=_finite_or_none(item.scatter_rms),
                n_candidates=_optional_int(item.n_candidates),
                n_used=_optional_int(item.n_used),
                n_reference=_optional_int(item.n_reference),
                accepted=bool(item.accepted),
                quality_status=_optional_str(item.quality_status),
                details=_json_safe(item.details or {}),
            )
        )
    if frame_rows:
        session.add_all(frame_rows)

    object_rows: list[CalibrationIterationObject] = []
    seen_objects: set[int] = set()
    for item in object_terms:
        object_id = int(item.object_id)
        if object_id in seen_objects:
            raise ValueError(f"Duplicate normalized object term: object_id={object_id}")
        seen_objects.add(object_id)
        object_rows.append(
            CalibrationIterationObject(
                calibration_iteration_id=iteration_id,
                object_id=object_id,
                role=str(item.role or "NON_REFERENCE"),
                is_reference=bool(item.is_reference),
                requested_exclusion=bool(item.requested_exclusion),
                effective_exclusion=bool(item.effective_exclusion),
                n_measurements=_optional_int(item.n_measurements),
                median_mag=_finite_or_none(item.median_mag),
                rms=_finite_or_none(item.rms),
                mad=_finite_or_none(item.mad),
                status=_optional_str(item.status),
                details=_json_safe(item.details or {}),
            )
        )
    if object_rows:
        session.add_all(object_rows)
    session.flush()

    return NormalizedCalibrationWriteResult(
        calibration_iteration_id=iteration_id,
        frame_terms=len(frame_rows),
        object_terms=len(object_rows),
    )


def synchronize_calibration_iteration_scope(
    session: Session,
    *,
    calibration_version_id: int,
    pipeline_run_id: int | None,
    field_id: int | None,
    series_id: int | None,
) -> int:
    """Propagate product scope stamped after materialization into compact terms."""

    if not hasattr(session, "scalars"):
        # Lightweight/fake sessions used by pure provenance callers do not
        # necessarily expose the full ORM query API. There are no normalized
        # rows to synchronize in those contexts.
        return 0

    rows = list(
        session.scalars(
            select(CalibrationIteration).where(
                CalibrationIteration.calibration_version_id == int(calibration_version_id)
            )
        ).all()
    )
    for row in rows:
        row.pipeline_run_id = _optional_int(pipeline_run_id)
        row.field_id = _optional_int(field_id)
        row.series_id = _optional_int(series_id)
    if rows:
        session.flush()
    return len(rows)


def list_calibration_iterations(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
) -> list[dict[str, Any]]:
    if calibration_name is None and calibration_version_id is None:
        raise ValueError("calibration_name or calibration_version_id is required")
    cv = _resolve_calibration_version(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    rows = session.scalars(
        select(CalibrationIteration)
        .where(CalibrationIteration.calibration_version_id == int(cv.calibration_version_id))
        .order_by(CalibrationIteration.iteration_no, CalibrationIteration.calibration_iteration_id)
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        n_frames = session.scalar(
            select_count(CalibrationFrameTerm.calibration_frame_term_id).where(
                CalibrationFrameTerm.calibration_iteration_id == int(row.calibration_iteration_id)
            )
        )
        n_objects = session.scalar(
            select_count(CalibrationIterationObject.calibration_iteration_object_id).where(
                CalibrationIterationObject.calibration_iteration_id == int(row.calibration_iteration_id)
            )
        )
        out.append(
            {
                "calibration_version_id": int(cv.calibration_version_id),
                "calibration_name": cv.name,
                "product_kind": cv.product_kind,
                "calibration_iteration_id": int(row.calibration_iteration_id),
                "iteration_kind": row.iteration_kind,
                "iteration_no": int(row.iteration_no),
                "algorithm": row.algorithm,
                "status": row.status,
                "convergence_status": row.convergence_status,
                "frame_terms": int(n_frames or 0),
                "object_terms": int(n_objects or 0),
                "pipeline_run_id": row.pipeline_run_id,
                "series_id": row.series_id,
            }
        )
    return out


def calibration_term_parity_report(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    tolerance: float = 1e-10,
) -> dict[str, Any]:
    """Compare dual-written normalized state with legacy materialized flags.

    The report intentionally validates only information that exists in both
    representations.  Aperture plane coefficients, for example, were never
    stored in measurement flags, so they cannot be checked against legacy rows.
    """

    cv = _resolve_calibration_version(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    iterations = list(
        session.scalars(
            select(CalibrationIteration)
            .where(CalibrationIteration.calibration_version_id == int(cv.calibration_version_id))
            .order_by(CalibrationIteration.iteration_no.desc(), CalibrationIteration.calibration_iteration_id.desc())
        ).all()
    )
    if not iterations:
        return {
            "status": "NO_NORMALIZED_ITERATION",
            "calibration_version_id": int(cv.calibration_version_id),
            "calibration_name": cv.name,
            "product_kind": cv.product_kind,
            "checked": 0,
            "mismatches": 0,
            "details": [],
        }
    iteration = iterations[0]
    frame_terms = list(
        session.scalars(
            select(CalibrationFrameTerm).where(
                CalibrationFrameTerm.calibration_iteration_id == int(iteration.calibration_iteration_id)
            )
        ).all()
    )
    object_terms = list(
        session.scalars(
            select(CalibrationIterationObject).where(
                CalibrationIterationObject.calibration_iteration_id == int(iteration.calibration_iteration_id)
            )
        ).all()
    )

    product_kind = str(cv.product_kind or "")
    legacy_measurements = int(
        session.scalar(
            select_count(Measurement.measurement_id).where(
                Measurement.calibration_version_id == int(cv.calibration_version_id)
            )
        )
        or 0
    )
    if legacy_measurements == 0:
        return {
            "status": "NORMALIZED_ONLY",
            "calibration_version_id": int(cv.calibration_version_id),
            "calibration_name": cv.name,
            "product_kind": product_kind,
            "calibration_iteration_id": int(iteration.calibration_iteration_id),
            "iteration_no": int(iteration.iteration_no),
            "frame_terms": len(frame_terms),
            "object_terms": len(object_terms),
            "legacy_measurements": 0,
            "checked": 0,
            "mismatches": 0,
            "details": [],
        }
    frame_flag_rows = session.execute(
        select(Measurement.frame_id, Measurement.flags)
        .where(Measurement.calibration_version_id == int(cv.calibration_version_id))
        .distinct(Measurement.frame_id)
        .order_by(Measurement.frame_id, Measurement.measurement_id)
    ).all()
    first_flags_by_frame: dict[int, dict[str, Any]] = {
        int(frame_id): dict(flags or {}) for frame_id, flags in frame_flag_rows
    }
    first_flags_by_object: dict[int, dict[str, Any]] = {}
    if product_kind in {"ENSEMBLE", "COLOR_CALIBRATION"}:
        object_flag_rows = session.execute(
            select(Measurement.object_id, Measurement.flags)
            .where(Measurement.calibration_version_id == int(cv.calibration_version_id))
            .distinct(Measurement.object_id)
            .order_by(Measurement.object_id, Measurement.measurement_id)
        ).all()
        first_flags_by_object = {
            int(object_id): dict(flags or {}) for object_id, flags in object_flag_rows
        }

    checked = 0
    mismatches: list[dict[str, Any]] = []
    for term in frame_terms:
        flags = first_flags_by_frame.get(int(term.frame_id))
        if flags is None:
            # Rejected frame terms are intentionally retained in the normalized
            # model even when the compatibility materializer emitted no target
            # measurements for that frame.
            if not bool(term.accepted):
                checked += 1
                continue
            mismatches.append({"scope": "frame", "id": int(term.frame_id), "field": "legacy_row", "expected": "present", "actual": None})
            continue
        if product_kind == "APERTURE_ZP":
            checked += _compare_field(mismatches, "frame", term.frame_id, "offset_mag", term.offset_mag, flags.get("frame_aperture_zp"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "scatter_mad", term.scatter_mad, flags.get("frame_aperture_zp_mad"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "n_reference", term.n_reference, flags.get("frame_aperture_zp_n"), tolerance)
        elif product_kind == "ENSEMBLE":
            checked += _compare_field(mismatches, "frame", term.frame_id, "offset_mag", term.offset_mag, flags.get("ensemble_frame_offset"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "scatter_mad", term.scatter_mad, flags.get("ensemble_frame_scatter_mad"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "n_reference", term.n_reference, flags.get("ensemble_frame_n_ref"), tolerance)
        elif product_kind == "APERTURE_CORRECTION":
            checked += _compare_field(mismatches, "frame", term.frame_id, "model_name", term.model_name, flags.get("aperture_correction_model"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "scatter_mad", term.scatter_mad, flags.get("aperture_correction_residual_mad"), tolerance)
            checked += _compare_field(mismatches, "frame", term.frame_id, "n_used", term.n_used, flags.get("aperture_correction_n_calibrators"), tolerance)

    if product_kind == "ENSEMBLE":
        for term in object_terms:
            flags = first_flags_by_object.get(int(term.object_id))
            if flags is None:
                continue
            checked += _compare_field(mismatches, "object", term.object_id, "is_reference", term.is_reference, flags.get("ensemble_object_is_reference"), tolerance)
            checked += _compare_field(mismatches, "object", term.object_id, "requested_exclusion", term.requested_exclusion, flags.get("ensemble_object_requested_reference_exclusion"), tolerance)
            checked += _compare_field(mismatches, "object", term.object_id, "effective_exclusion", term.effective_exclusion, flags.get("ensemble_object_excluded_from_reference"), tolerance)

    if product_kind == "COLOR_CALIBRATION":
        metrics = dict(iteration.metrics or {})
        any_flags = next(iter(first_flags_by_frame.values()), {})
        for field_name, normalized, legacy_key in (
            ("transform_offset", metrics.get("transform_offset"), "reference_transform_offset"),
            ("color_term", metrics.get("color_term"), "reference_color_term"),
            ("color0", metrics.get("color0"), "reference_color0"),
        ):
            checked += _compare_field(mismatches, "global", 0, field_name, normalized, any_flags.get(legacy_key), tolerance)
        for term in object_terms:
            flags = first_flags_by_object.get(int(term.object_id))
            if flags is None:
                continue
            details = dict(term.details or {})
            if "applied_color_value" in details:
                checked += _compare_field(
                    mismatches, "object", term.object_id, "applied_color_value",
                    details.get("applied_color_value"), flags.get("reference_color_applied_value"), tolerance,
                )
            if "color_missing" in details:
                checked += _compare_field(
                    mismatches, "object", term.object_id, "color_missing",
                    details.get("color_missing"), flags.get("reference_color_missing"), tolerance,
                )
            if "systematic_error" in details:
                checked += _compare_field(
                    mismatches, "object", term.object_id, "systematic_error",
                    details.get("systematic_error"), flags.get("mag_err_systematic"), tolerance,
                )

    return {
        "status": "OK" if not mismatches else "MISMATCH",
        "calibration_version_id": int(cv.calibration_version_id),
        "calibration_name": cv.name,
        "product_kind": product_kind,
        "calibration_iteration_id": int(iteration.calibration_iteration_id),
        "iteration_no": int(iteration.iteration_no),
        "frame_terms": len(frame_terms),
        "object_terms": len(object_terms),
        "legacy_measurements": legacy_measurements,
        "checked": checked,
        "mismatches": len(mismatches),
        "details": mismatches[:100],
    }


def select_count(column):
    # Local import keeps the public module surface small and avoids a second
    # SQLAlchemy import list solely for func.count.
    from sqlalchemy import func

    return select(func.count(column))


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


def _compare_field(
    mismatches: list[dict[str, Any]],
    scope: str,
    entity_id: int,
    field_name: str,
    normalized: Any,
    legacy: Any,
    tolerance: float,
) -> int:
    if _equivalent(normalized, legacy, tolerance):
        return 1
    mismatches.append(
        {
            "scope": scope,
            "id": int(entity_id),
            "field": field_name,
            "normalized": _json_safe(normalized),
            "legacy": _json_safe(legacy),
        }
    )
    return 1


def _equivalent(left: Any, right: Any, tolerance: float) -> bool:
    if left is None and right is None:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    try:
        lf = float(left)
        rf = float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)
    if not math.isfinite(lf) or not math.isfinite(rf):
        return lf == rf
    return math.isclose(lf, rf, rel_tol=float(tolerance), abs_tol=float(tolerance))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)
