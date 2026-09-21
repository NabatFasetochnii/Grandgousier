from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import (
    _RECONSTRUCTED_FIELDS,
    reconstruct_calibration_measurement_states,
)
from grandgousier.db.models import CalibrationVersion, Measurement, SciencePhotometry
from grandgousier.photometry.science_photometry import (
    SCIENCE_SCALAR_COLUMNS,
    complete_science_calibration_ids,
)


@dataclass(slots=True)
class CalibrationPhotometryRow:
    """Measurement-like scalar row independent of physical storage.

    BASE calibrations are stored in ``measurements``. Derived calibrations are
    reconstructed from immutable base photometry plus normalized calibration
    terms; final SCIENCE scalars may be overlaid from durable ``science_photometry``.
    Calibration algorithms and QC code should
    consume this view rather than assume that every calibration stage owns a
    full row set in ``measurements``.
    """

    measurement_id: int | None
    base_measurement_id: int | None
    object_id: int
    frame_id: int
    x: float | None
    y: float | None
    flux: float | None
    flux_err: float | None
    mag_inst: float | None
    mag_cal: float | None
    mag_err: float | None
    mag_err_random: float | None
    mag_err_systematic: float | None
    aperture_radius_pix: float | None
    local_background: float | None
    local_zp_correction: float | None
    centroid_dx_pix: float | None
    centroid_dy_pix: float | None
    flags: dict[str, Any]


@dataclass(slots=True)
class CalibrationPhotometryView:
    calibration_version_id: int
    calibration_name: str
    product_kind: str | None
    source: str
    rows: list[CalibrationPhotometryRow]



def load_calibration_photometry_view(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    frame_ids: Iterable[int] | None = None,
    object_ids: Iterable[int] | None = None,
    prefer_science: bool = True,
    prefer_cache: bool = False,
) -> CalibrationPhotometryView:
    """Load one calibration through the clean 0.7 storage contract.

    ``measurements`` is BASE-only.  Final SCIENCE products prefer the durable
    narrow ``science_photometry`` layer and inherit identity/extraction geometry
    plus current flags from BASE + normalized reconstruction.  Any derived
    product without durable SCIENCE rows is reconstructed on demand.

    ``prefer_cache`` is retained as a source-API compatibility argument and has
    no effect: the rebuildable cache concept no longer exists in 0.7.
    """

    del prefer_cache
    cv = _resolve_cv(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    cv_id = int(cv.calibration_version_id)
    normalized_frame_ids = sorted({int(v) for v in (frame_ids or [])})
    normalized_object_ids = sorted({int(v) for v in (object_ids or [])})

    if str(cv.product_kind or "").upper() == "BASE_PHOTOMETRY":
        rows = _load_base_rows(
            session,
            cv_id=cv_id,
            frame_ids=normalized_frame_ids,
            object_ids=normalized_object_ids,
        )
        return CalibrationPhotometryView(
            calibration_version_id=cv_id,
            calibration_name=str(cv.name),
            product_kind=cv.product_kind,
            source="MEASUREMENTS_BASE",
            rows=rows,
        )

    if prefer_science and str(cv.product_role or "").upper() == "SCIENCE":
        if cv_id in complete_science_calibration_ids(session, [cv_id]):
            rows = _load_science_photometry_rows(
                session,
                cv_id=cv_id,
                frame_ids=normalized_frame_ids,
                object_ids=normalized_object_ids,
            )
            return CalibrationPhotometryView(
                calibration_version_id=cv_id,
                calibration_name=str(cv.name),
                product_kind=cv.product_kind,
                source="SCIENCE_PHOTOMETRY",
                rows=rows,
            )

    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_version_id=cv_id,
        frame_ids=normalized_frame_ids or None,
        object_ids=normalized_object_ids or None,
        apply_historical_flag_overrides=False,
    )
    rows = [
        _row_from_state(state)
        for _key, state in sorted(
            reconstructed.states.items(), key=lambda item: (int(item[0][0]), int(item[0][1]))
        )
    ]
    return CalibrationPhotometryView(
        calibration_version_id=cv_id,
        calibration_name=str(cv.name),
        product_kind=cv.product_kind,
        source="RECONSTRUCTED_NORMALIZED",
        rows=rows,
    )


def _resolve_cv(
    session: Session,
    *,
    calibration_name: str | None,
    calibration_version_id: int | None,
) -> CalibrationVersion:
    cv = None
    if calibration_version_id is not None:
        cv = session.get(CalibrationVersion, int(calibration_version_id))
    elif calibration_name is not None:
        cv = session.scalar(
            select(CalibrationVersion).where(CalibrationVersion.name == str(calibration_name))
        )
    if cv is None:
        raise ValueError("Calibration version not found")
    return cv



def _load_base_rows(
    session: Session,
    *,
    cv_id: int,
    frame_ids: list[int],
    object_ids: list[int],
) -> list[CalibrationPhotometryRow]:
    columns = [
        Measurement.measurement_id.label("measurement_id"),
        Measurement.object_id.label("object_id"),
        Measurement.frame_id.label("frame_id"),
        *[getattr(Measurement, field_name).label(field_name) for field_name in _RECONSTRUCTED_FIELDS],
        Measurement.flags.label("flags"),
    ]
    stmt = select(*columns).where(Measurement.calibration_version_id == int(cv_id))
    if frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(frame_ids))
    if object_ids:
        stmt = stmt.where(Measurement.object_id.in_(object_ids))
    values = session.execute(
        stmt.order_by(Measurement.object_id, Measurement.frame_id)
    ).mappings()
    return [
        CalibrationPhotometryRow(
            measurement_id=int(row["measurement_id"]),
            base_measurement_id=int(row["measurement_id"]),
            object_id=int(row["object_id"]),
            frame_id=int(row["frame_id"]),
            **{field_name: row[field_name] for field_name in _RECONSTRUCTED_FIELDS},
            flags=dict(row["flags"] or {}),
        )
        for row in values
    ]


def _load_science_photometry_rows(
    session: Session,
    *,
    cv_id: int,
    frame_ids: list[int],
    object_ids: list[int],
) -> list[CalibrationPhotometryRow]:
    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_version_id=int(cv_id),
        frame_ids=frame_ids or None,
        object_ids=object_ids or None,
        apply_historical_flag_overrides=False,
    )
    columns = [
        SciencePhotometry.base_measurement_id.label("base_measurement_id"),
        *[getattr(SciencePhotometry, name).label(name) for name in SCIENCE_SCALAR_COLUMNS],
    ]
    stmt = (
        select(*columns)
        .join(Measurement, Measurement.measurement_id == SciencePhotometry.base_measurement_id)
        .where(SciencePhotometry.calibration_version_id == int(cv_id))
    )
    if frame_ids:
        stmt = stmt.where(Measurement.frame_id.in_(frame_ids))
    if object_ids:
        stmt = stmt.where(Measurement.object_id.in_(object_ids))
    durable = {
        int(row["base_measurement_id"]): row
        for row in session.execute(stmt).mappings()
    }

    rows: list[CalibrationPhotometryRow] = []
    for _key, state in sorted(
        reconstructed.states.items(),
        key=lambda item: (int(item[0][0]), int(item[0][1])),
    ):
        base_measurement_id = int(state["base_measurement_id"])
        stored = durable.get(base_measurement_id)
        if stored is None:
            raise RuntimeError(
                "Durable SCIENCE product is missing a reconstructed BASE row: "
                f"calibration_version_id={cv_id}, base_measurement_id={base_measurement_id}"
            )
        merged = dict(state)
        for name in SCIENCE_SCALAR_COLUMNS:
            merged[name] = stored[name]
        rows.append(_row_from_state(merged))
    return rows


def _row_from_state(state: dict[str, Any]) -> CalibrationPhotometryRow:
    base_measurement_id = (
        int(state["base_measurement_id"])
        if state.get("base_measurement_id") is not None
        else None
    )
    measurement_id = (
        int(state["measurement_id"])
        if state.get("measurement_id") is not None
        else base_measurement_id
    )
    return CalibrationPhotometryRow(
        measurement_id=measurement_id,
        base_measurement_id=base_measurement_id,
        object_id=int(state["object_id"]),
        frame_id=int(state["frame_id"]),
        **{field_name: state.get(field_name) for field_name in _RECONSTRUCTED_FIELDS},
        flags=dict(state.get("flags") or {}),
    )
