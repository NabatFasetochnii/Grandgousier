from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from grandgousier.quality.field_report import _calibration_chain_names, _resolve_field_series_filter
from grandgousier.db.models import CalibrationVersion


DEFAULT_DETECTION_ALGORITHM = "numpy_localmax_v1"
DEFAULT_DETECTION_MATCH_ALGORITHM = "nearest_sky_v1"
DEFAULT_FRAME_QC_ALGORITHM = "detection_catalog_qc_v1"
DEFAULT_OBJECT_QC_ALGORITHM = "object_qc_v1"


@dataclass
class IdempotencyCheckResult:
    name: str
    table_name: str
    identity: list[str]
    total_rows: int | None = None
    duplicate_groups: int = 0
    duplicate_extra_rows: int = 0
    status: str = "OK"
    error_message: str | None = None
    examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PipelineIdempotencyReport:
    status: str
    calibration_name: str | None
    calibration_version_id: int | None
    catalog_name: str | None
    instrument_unit_id: int | None
    frame_selection_mode: str
    selected_frame_ids_count: int | None
    series_id: int | None
    field_name: str | None
    checks: list[IdempotencyCheckResult]

    @property
    def violations(self) -> int:
        return sum(1 for check in self.checks if check.status == "VIOLATION")

    @property
    def missing_checks(self) -> int:
        return sum(1 for check in self.checks if check.status == "MISSING")

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == "WARNING")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["violations"] = self.violations
        data["missing_checks"] = self.missing_checks
        data["warnings"] = self.warnings
        return data


def build_pipeline_idempotency_report(
    session: Session,
    *,
    calibration_name: str | None = None,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    series_id: int | None = None,
    field_series_name: str | None = None,
    frame_ids: list[int] | None = None,
    detection_algorithm: str = DEFAULT_DETECTION_ALGORITHM,
    detection_match_algorithm: str = DEFAULT_DETECTION_MATCH_ALGORITHM,
    frame_qc_algorithm: str = DEFAULT_FRAME_QC_ALGORITHM,
    object_qc_algorithm: str = DEFAULT_OBJECT_QC_ALGORITHM,
    example_limit: int = 10,
) -> PipelineIdempotencyReport:
    """Audit logical duplicate groups that would break pipeline idempotency.

    This is a read-only pre/post-flight diagnostic. It does not prove that two
    arbitrary pipeline runs are byte-identical; it checks the database identities
    that must stay unique after repeated ``run-field-pipeline`` executions.
    """

    cv_id = None
    calibration_names: list[str] = []
    if calibration_name:
        cv = _load_calibration_version(session, calibration_name)
        cv_id = int(cv.calibration_version_id)
        calibration_names = _calibration_chain_names(session, cv)
        if calibration_name not in calibration_names:
            calibration_names.insert(0, calibration_name)

    series_selection = _resolve_field_series_filter(
        session,
        series_id=series_id,
        field_series_name=field_series_name,
        frame_ids=frame_ids,
        calibration_version_id=cv_id,
        calibration_names=calibration_names or ([calibration_name] if calibration_name else None),
    )
    if series_selection is not None:
        selected_frame_ids = list(series_selection["frame_ids"])
        selected_series_id = int(series_selection["series_id"])
        field_name = series_selection.get("field_name")
        frame_selection_mode = "field_series"
        if instrument_unit_id is None and series_selection.get("instrument_unit_id") is not None:
            instrument_unit_id = int(series_selection["instrument_unit_id"])
    else:
        selected_frame_ids = list(frame_ids or [])
        selected_series_id = None
        field_name = None
        frame_selection_mode = "explicit_frame_ids" if selected_frame_ids else "calibration_scope"

    scope = _Scope(
        calibration_version_id=cv_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_ids=selected_frame_ids,
        object_qc_algorithm=object_qc_algorithm,
        detection_algorithm=detection_algorithm,
        detection_match_algorithm=detection_match_algorithm,
        frame_qc_algorithm=frame_qc_algorithm,
        example_limit=int(example_limit),
    )

    checks = [
        _check_measurements(session, scope),
        _check_object_qc(session, scope),
        _check_source_detections(session, scope),
        _check_detection_catalog_matches(session, scope),
        _check_frame_qc(session, scope),
        _check_event_detection_links(session, scope),
    ]
    status = "OK"
    if any(check.status == "VIOLATION" for check in checks):
        status = "VIOLATION"
    elif any(check.status in {"WARNING", "MISSING"} for check in checks):
        status = "WARNING"

    return PipelineIdempotencyReport(
        status=status,
        calibration_name=calibration_name,
        calibration_version_id=cv_id,
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        frame_selection_mode=frame_selection_mode,
        selected_frame_ids_count=len(selected_frame_ids) if selected_frame_ids else None,
        series_id=selected_series_id,
        field_name=str(field_name) if field_name is not None else None,
        checks=checks,
    )


@dataclass(frozen=True)
class _Scope:
    calibration_version_id: int | None
    catalog_name: str | None
    instrument_unit_id: int | None
    frame_ids: list[int]
    object_qc_algorithm: str
    detection_algorithm: str
    detection_match_algorithm: str
    frame_qc_algorithm: str
    example_limit: int


def idempotency_summary_rows(report: PipelineIdempotencyReport) -> list[dict[str, Any]]:
    rows = [
        {"section": "scope", "metric": "status", "value": report.status},
        {"section": "scope", "metric": "calibration_name", "value": report.calibration_name},
        {"section": "scope", "metric": "calibration_version_id", "value": report.calibration_version_id},
        {"section": "scope", "metric": "catalog_name", "value": report.catalog_name},
        {"section": "scope", "metric": "frame_selection_mode", "value": report.frame_selection_mode},
        {"section": "scope", "metric": "series_id", "value": report.series_id},
        {"section": "scope", "metric": "field_name", "value": report.field_name},
        {"section": "scope", "metric": "selected_frame_ids_count", "value": report.selected_frame_ids_count},
    ]
    for check in report.checks:
        rows.append({"section": check.name, "metric": "status", "value": check.status})
        rows.append({"section": check.name, "metric": "total_rows", "value": check.total_rows})
        rows.append({"section": check.name, "metric": "duplicate_groups", "value": check.duplicate_groups})
        rows.append({"section": check.name, "metric": "duplicate_extra_rows", "value": check.duplicate_extra_rows})
        if check.error_message:
            rows.append({"section": check.name, "metric": "error", "value": check.error_message})
    return rows


def idempotency_summary_columns() -> list[str]:
    return ["section", "metric", "value"]


def export_pipeline_idempotency_json(report: PipelineIdempotencyReport, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_calibration_version(session: Session, calibration_name: str) -> CalibrationVersion:
    cv = session.query(CalibrationVersion).filter(CalibrationVersion.name == calibration_name).one_or_none()
    if cv is None:
        raise ValueError(f"calibration version not found: {calibration_name!r}")
    return cv


def _check_measurements(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["TRUE"]
    params = _base_params(scope)
    if scope.calibration_version_id is not None:
        where.append("m.calibration_version_id = :calibration_version_id")
    if scope.frame_ids:
        where.append("m.frame_id = ANY(:frame_ids)")
    if scope.instrument_unit_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM frames f WHERE f.frame_id = m.frame_id AND f.instrument_unit_id = :instrument_unit_id)"
        )
    return _duplicate_check(
        session,
        name="measurements",
        table_name="measurements",
        identity=["object_id", "frame_id", "calibration_version_id"],
        from_sql="measurements m",
        where_sql=" AND ".join(where),
        group_sql="m.object_id, m.frame_id, m.calibration_version_id",
        select_sql="m.object_id, m.frame_id, m.calibration_version_id",
        params=params,
        example_limit=scope.example_limit,
    )


def _check_object_qc(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["oq.algorithm = :object_qc_algorithm"]
    params = _base_params(scope)
    if scope.calibration_version_id is not None:
        where.append("oq.calibration_version_id = :calibration_version_id")
    if scope.catalog_name is not None:
        where.append("oq.catalog_name = :catalog_name")
    if scope.instrument_unit_id is not None:
        where.append("(oq.instrument_unit_id = :instrument_unit_id OR oq.instrument_unit_id IS NULL)")
    if scope.frame_ids:
        where.append(
            "EXISTS (SELECT 1 FROM measurements m WHERE m.object_id = oq.object_id AND m.frame_id = ANY(:frame_ids)"
            " AND (:calibration_version_id IS NULL OR m.calibration_version_id = :calibration_version_id))"
        )
    return _duplicate_check(
        session,
        name="object_qc",
        table_name="object_qc",
        identity=["object_id", "catalog_name", "instrument_unit_id", "calibration_version_id", "algorithm"],
        from_sql="object_qc oq",
        where_sql=" AND ".join(where),
        group_sql="oq.object_id, oq.catalog_name, oq.instrument_unit_id, oq.calibration_version_id, oq.algorithm",
        select_sql="oq.object_id, oq.catalog_name, oq.instrument_unit_id, oq.calibration_version_id, oq.algorithm",
        params=params,
        example_limit=scope.example_limit,
    )


def _check_source_detections(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["sd.algorithm = :detection_algorithm"]
    params = _base_params(scope)
    if scope.frame_ids:
        where.append("sd.frame_id = ANY(:frame_ids)")
    if scope.instrument_unit_id is not None:
        where.append("f.instrument_unit_id = :instrument_unit_id")
    return _duplicate_check(
        session,
        name="source_detections",
        table_name="source_detections",
        identity=["frame_id", "algorithm", "x_round_1mpix", "y_round_1mpix"],
        from_sql="source_detections sd JOIN frames f ON f.frame_id = sd.frame_id",
        where_sql=" AND ".join(where),
        group_sql="sd.frame_id, sd.algorithm, ROUND(sd.x::numeric, 3), ROUND(sd.y::numeric, 3)",
        select_sql="sd.frame_id, sd.algorithm, ROUND(sd.x::numeric, 3) AS x_round_1mpix, ROUND(sd.y::numeric, 3) AS y_round_1mpix",
        params=params,
        example_limit=scope.example_limit,
    )


def _check_detection_catalog_matches(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["dcm.algorithm = :detection_match_algorithm"]
    params = _base_params(scope)
    if scope.catalog_name is not None:
        where.append("dcm.catalog_name = :catalog_name")
    if scope.frame_ids:
        where.append("dcm.frame_id = ANY(:frame_ids)")
    if scope.instrument_unit_id is not None:
        where.append("EXISTS (SELECT 1 FROM frames f WHERE f.frame_id = dcm.frame_id AND f.instrument_unit_id = :instrument_unit_id)")
    return _duplicate_check(
        session,
        name="detection_catalog_matches",
        table_name="detection_catalog_matches",
        identity=["frame_id", "catalog_source_id", "catalog_name", "algorithm"],
        from_sql="detection_catalog_matches dcm",
        where_sql=" AND ".join(where),
        group_sql="dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm",
        select_sql="dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm",
        params=params,
        example_limit=scope.example_limit,
    )


def _check_frame_qc(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["fq.algorithm = :frame_qc_algorithm"]
    params = _base_params(scope)
    if scope.catalog_name is not None:
        where.append("fq.catalog_name = :catalog_name")
    if scope.frame_ids:
        where.append("fq.frame_id = ANY(:frame_ids)")
    if scope.instrument_unit_id is not None:
        where.append("EXISTS (SELECT 1 FROM frames f WHERE f.frame_id = fq.frame_id AND f.instrument_unit_id = :instrument_unit_id)")
    return _duplicate_check(
        session,
        name="frame_qc",
        table_name="frame_qc",
        identity=["frame_id", "catalog_name", "algorithm"],
        from_sql="frame_qc fq",
        where_sql=" AND ".join(where),
        group_sql="fq.frame_id, fq.catalog_name, fq.algorithm",
        select_sql="fq.frame_id, fq.catalog_name, fq.algorithm",
        params=params,
        example_limit=scope.example_limit,
    )


def _check_event_detection_links(session: Session, scope: _Scope) -> IdempotencyCheckResult:
    where = ["TRUE"]
    params = _base_params(scope)
    if scope.frame_ids:
        where.append("edl.frame_id = ANY(:frame_ids)")
    if scope.instrument_unit_id is not None:
        where.append("edl.instrument_unit_id = :instrument_unit_id")
    return _duplicate_check(
        session,
        name="event_detection_links",
        table_name="event_detection_links",
        identity=["event_id", "source_detection_id"],
        from_sql="event_detection_links edl",
        where_sql=" AND ".join(where),
        group_sql="edl.event_id, edl.source_detection_id",
        select_sql="edl.event_id, edl.source_detection_id",
        params=params,
        example_limit=scope.example_limit,
        missing_ok=True,
    )


def _base_params(scope: _Scope) -> dict[str, Any]:
    return {
        "calibration_version_id": scope.calibration_version_id,
        "catalog_name": scope.catalog_name,
        "instrument_unit_id": scope.instrument_unit_id,
        "frame_ids": list(scope.frame_ids),
        "object_qc_algorithm": scope.object_qc_algorithm,
        "detection_algorithm": scope.detection_algorithm,
        "detection_match_algorithm": scope.detection_match_algorithm,
        "frame_qc_algorithm": scope.frame_qc_algorithm,
    }


def _duplicate_check(
    session: Session,
    *,
    name: str,
    table_name: str,
    identity: list[str],
    from_sql: str,
    where_sql: str,
    group_sql: str,
    select_sql: str,
    params: dict[str, Any],
    example_limit: int,
    missing_ok: bool = False,
) -> IdempotencyCheckResult:
    try:
        total_rows = session.execute(
            text(f"SELECT COUNT(*) AS n FROM {from_sql} WHERE {where_sql}"),
            params,
        ).scalar_one()
        metrics = session.execute(
            text(
                f"""
                WITH duplicate_groups AS (
                    SELECT COUNT(*) AS n
                    FROM {from_sql}
                    WHERE {where_sql}
                    GROUP BY {group_sql}
                    HAVING COUNT(*) > 1
                )
                SELECT
                    COUNT(*) AS duplicate_groups,
                    COALESCE(SUM(n - 1), 0) AS duplicate_extra_rows
                FROM duplicate_groups
                """
            ),
            params,
        ).mappings().first()
        examples = session.execute(
            text(
                f"""
                SELECT {select_sql}, COUNT(*) AS n
                FROM {from_sql}
                WHERE {where_sql}
                GROUP BY {group_sql}
                HAVING COUNT(*) > 1
                ORDER BY n DESC
                LIMIT :example_limit
                """
            ),
            {**params, "example_limit": int(example_limit)},
        ).mappings().all()
    except ProgrammingError as exc:
        session.rollback()
        if missing_ok:
            return IdempotencyCheckResult(
                name=name,
                table_name=table_name,
                identity=identity,
                status="MISSING",
                error_message=_short_error(exc),
            )
        return IdempotencyCheckResult(
            name=name,
            table_name=table_name,
            identity=identity,
            status="WARNING",
            error_message=_short_error(exc),
        )

    duplicate_groups = int((metrics or {}).get("duplicate_groups") or 0)
    duplicate_extra_rows = int((metrics or {}).get("duplicate_extra_rows") or 0)
    status = "VIOLATION" if duplicate_groups else "OK"
    return IdempotencyCheckResult(
        name=name,
        table_name=table_name,
        identity=identity,
        total_rows=int(total_rows or 0),
        duplicate_groups=duplicate_groups,
        duplicate_extra_rows=duplicate_extra_rows,
        status=status,
        examples=[dict(row) for row in examples],
    )


def _short_error(exc: Exception) -> str:
    text_value = str(exc).replace("\n", " ").strip()
    return text_value[:500] + ("..." if len(text_value) > 500 else "")
