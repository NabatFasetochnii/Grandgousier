from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import CalibrationVersion


def get_source_calibration_version_id(cv: CalibrationVersion | None) -> int | None:
    """Return the explicit source calibration id, falling back to legacy parameters."""

    if cv is None:
        return None
    direct = getattr(cv, "source_calibration_version_id", None)
    if direct is not None:
        try:
            return int(direct)
        except Exception:
            return None
    params = cv.parameters or {}
    if isinstance(params, dict):
        value = params.get("source_calibration_version_id")
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
    return None


def get_source_calibration_name(cv: CalibrationVersion | None) -> str | None:
    """Return the source calibration name from relationship or legacy parameters."""

    if cv is None:
        return None
    source = getattr(cv, "source_calibration", None)
    name = getattr(source, "name", None)
    if name:
        return str(name)
    params = cv.parameters or {}
    if isinstance(params, dict):
        name = params.get("source_calibration_name")
        if name:
            return str(name)
    return None


def set_source_calibration(cv: CalibrationVersion, source_cv: CalibrationVersion | None) -> None:
    """Persist source-calibration provenance in both the FK column and JSON parameters.

    The column is the authoritative relation for new databases. The JSON keys are
    kept for compatibility with older patch code and exported diagnostics.
    """

    if source_cv is None:
        cv.source_calibration_version_id = None
        return
    cv.source_calibration_version_id = int(source_cv.calibration_version_id)
    params: dict[str, Any] = dict(cv.parameters or {}) if isinstance(cv.parameters, dict) else {}
    params["source_calibration_version_id"] = int(source_cv.calibration_version_id)
    params["source_calibration_name"] = source_cv.name
    cv.parameters = params


def resolve_source_calibration(session: Session, cv: CalibrationVersion | None) -> CalibrationVersion | None:
    """Resolve the source CalibrationVersion for a derived calibration.

    New rows use calibration_versions.source_calibration_version_id. Legacy rows
    produced before MVP5.30.5 may only have parameters.source_calibration_name.
    """

    if cv is None:
        return None
    source_id = get_source_calibration_version_id(cv)
    if source_id is not None:
        source = session.get(CalibrationVersion, source_id)
        if source is not None:
            return source
    source_name = get_source_calibration_name(cv)
    if source_name and source_name != cv.name:
        return session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_name))
    return None
