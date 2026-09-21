from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    CalibrationVersion,
    ScienceFlagOverride,
    ScienceFlagOverrideSet,
    ScienceMeasurement,
    SciencePhotometryCache,
)
from grandgousier.photometry.science_flag_overrides import (
    COMPLETE_OVERRIDE_STATUS,
    RETIRED_OVERRIDE_STATUS,
    IMMUTABLE_SCIENCE_STATUSES,
    select_v1_science_calibration_ids,
    verify_science_flag_overrides,
)


@dataclass(slots=True)
class ScienceMeasurementRetirementResult:
    calibration_version_id: int
    calibration_name: str
    lifecycle_status: str
    v1_rows_before: int
    cache_rows: int
    manifest_source_rows: int
    manifest_row_override_count: int
    actual_row_override_count: int
    verified_rows: int
    scalar_mismatches: int
    identity_mismatches: int
    flag_mismatches: int
    deleted_rows: int
    v1_rows_after: int
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_retirable_science_calibration_ids(
    session: Session,
    *,
    calibration_version_ids: Iterable[int] = (),
    field_id: int | None = None,
    all_materialized: bool = False,
    include_superseded: bool = False,
) -> list[int]:
    """Select immutable SCIENCE products that still own V1 rows.

    This deliberately reuses the override selector: retirement only applies to
    physical legacy ``science_measurements`` rows. Compact-only products are
    never candidates and already-retired products disappear from field scope.
    """

    return select_v1_science_calibration_ids(
        session,
        calibration_version_ids=calibration_version_ids,
        field_id=field_id,
        all_materialized=all_materialized,
        include_superseded=include_superseded,
    )


def _inbound_fk_names(session: Session) -> list[str]:
    """Return PostgreSQL foreign keys that reference science_measurements.

    Current production schema has none, but destructive retirement must fail
    closed if a later schema version introduces one. Non-PostgreSQL test
    backends return an empty list; schema-contract tests cover their behavior.
    """

    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return []
    rows = session.execute(
        text(
            """
            SELECT con.conname
            FROM pg_constraint con
            WHERE con.contype = 'f'
              AND con.confrelid = 'science_measurements'::regclass
            ORDER BY con.conname
            """
        )
    ).scalars().all()
    return [str(value) for value in rows]


def retire_science_measurements(
    session: Session,
    *,
    calibration_version_id: int,
    execute: bool = False,
    frame_batch_size: int = 4,
    tolerance: float = 1e-10,
) -> ScienceMeasurementRetirementResult:
    """Prove and optionally retire one legacy wide SCIENCE materialization.

    Retirement is allowed only when all three compact contracts are complete:

    * a row-for-row compact photometry cache;
    * a ``COMPLETE_EXACT`` historical flag-override manifest whose residual
      count still matches storage;
    * a fresh all-row scalar/identity/flag verification against V1.

    The delete and transition to ``RETIRED_EXACT`` happen in the caller's
    transaction. No physical table rewrite or VACUUM is performed here.
    """

    cv = session.get(CalibrationVersion, int(calibration_version_id))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_version_id}")
    if str(cv.product_role or "").upper() != "SCIENCE":
        raise ValueError(
            f"Calibration {calibration_version_id} is not SCIENCE: product_role={cv.product_role!r}"
        )
    lifecycle_status = str(cv.lifecycle_status or "").upper()
    if lifecycle_status not in IMMUTABLE_SCIENCE_STATUSES:
        raise ValueError(
            f"Calibration {calibration_version_id} is not immutable: "
            f"lifecycle_status={lifecycle_status!r}"
        )

    cv_id = int(calibration_version_id)
    v1_rows = int(
        session.scalar(
            select(func.count(ScienceMeasurement.science_measurement_id)).where(
                ScienceMeasurement.calibration_version_id == cv_id
            )
        )
        or 0
    )
    cache_rows = int(
        session.scalar(
            select(func.count()).select_from(SciencePhotometryCache).where(
                SciencePhotometryCache.calibration_version_id == cv_id
            )
        )
        or 0
    )
    manifest = session.get(ScienceFlagOverrideSet, cv_id)
    manifest_source_rows = int(manifest.source_row_count) if manifest is not None else 0
    manifest_row_overrides = int(manifest.row_override_count) if manifest is not None else 0
    actual_row_overrides = int(
        session.scalar(
            select(func.count()).select_from(ScienceFlagOverride).where(
                ScienceFlagOverride.calibration_version_id == cv_id
            )
        )
        or 0
    )

    def result(
        *,
        status: str,
        reason: str | None = None,
        verified_rows: int = 0,
        scalar_mismatches: int = 0,
        identity_mismatches: int = 0,
        flag_mismatches: int = 0,
        deleted_rows: int = 0,
        v1_rows_after: int | None = None,
    ) -> ScienceMeasurementRetirementResult:
        return ScienceMeasurementRetirementResult(
            calibration_version_id=cv_id,
            calibration_name=str(cv.name),
            lifecycle_status=lifecycle_status,
            v1_rows_before=v1_rows,
            cache_rows=cache_rows,
            manifest_source_rows=manifest_source_rows,
            manifest_row_override_count=manifest_row_overrides,
            actual_row_override_count=actual_row_overrides,
            verified_rows=int(verified_rows),
            scalar_mismatches=int(scalar_mismatches),
            identity_mismatches=int(identity_mismatches),
            flag_mismatches=int(flag_mismatches),
            deleted_rows=int(deleted_rows),
            v1_rows_after=v1_rows if v1_rows_after is None else int(v1_rows_after),
            status=status,
            reason=reason,
        )

    if v1_rows <= 0:
        if manifest is not None and str(manifest.status or "").upper() == RETIRED_OVERRIDE_STATUS:
            return result(status="ALREADY_RETIRED", reason="NO_V1_ROWS_RETIRED_MANIFEST", v1_rows_after=0)
        return result(status="BLOCKED", reason="NO_V1_SCIENCE_ROWS", v1_rows_after=0)

    inbound_fks = _inbound_fk_names(session)
    if inbound_fks:
        return result(status="BLOCKED", reason="INBOUND_FOREIGN_KEYS:" + ",".join(inbound_fks))

    if cache_rows != v1_rows:
        return result(status="BLOCKED", reason="CACHE_NOT_COMPLETE")
    if manifest is None:
        return result(status="BLOCKED", reason="MISSING_OVERRIDE_MANIFEST")
    if str(manifest.status or "").upper() != COMPLETE_OVERRIDE_STATUS:
        return result(
            status="BLOCKED",
            reason=f"OVERRIDE_MANIFEST_STATUS_{str(manifest.status or '').upper() or 'EMPTY'}",
        )
    if manifest_source_rows != v1_rows:
        return result(status="BLOCKED", reason="MANIFEST_SOURCE_ROW_COUNT_MISMATCH")
    if actual_row_overrides != manifest_row_overrides:
        return result(status="BLOCKED", reason="ROW_OVERRIDE_COUNT_MISMATCH")

    verification = verify_science_flag_overrides(
        session,
        calibration_version_id=cv_id,
        frame_batch_size=int(frame_batch_size),
        tolerance=float(tolerance),
    )
    if verification.status != "OK":
        return result(
            status="BLOCKED",
            reason=f"VERIFY_{verification.reason or verification.status}",
            verified_rows=verification.checked_rows,
            scalar_mismatches=verification.scalar_mismatches,
            identity_mismatches=verification.identity_mismatches,
            flag_mismatches=verification.flag_mismatches,
        )

    if not execute:
        return result(
            status="READY",
            verified_rows=verification.checked_rows,
            scalar_mismatches=verification.scalar_mismatches,
            identity_mismatches=verification.identity_mismatches,
            flag_mismatches=verification.flag_mismatches,
        )

    delete_result = session.execute(
        text(
            "DELETE FROM science_measurements "
            "WHERE calibration_version_id = :cv"
        ),
        {"cv": cv_id},
    )
    deleted = int(delete_result.rowcount or 0)
    if deleted < 0:
        deleted = v1_rows
    if deleted != v1_rows:
        raise RuntimeError(
            "science_measurements retirement delete count mismatch: "
            f"calibration_version_id={cv_id}, expected={v1_rows}, deleted={deleted}"
        )

    after = int(
        session.scalar(
            select(func.count(ScienceMeasurement.science_measurement_id)).where(
                ScienceMeasurement.calibration_version_id == cv_id
            )
        )
        or 0
    )
    if after != 0:
        raise RuntimeError(
            "science_measurements retirement left residual V1 rows: "
            f"calibration_version_id={cv_id}, remaining={after}"
        )

    manifest.status = RETIRED_OVERRIDE_STATUS
    # ORM onupdate updates updated_at when the manifest is flushed/committed.
    session.flush()

    return result(
        status="RETIRED",
        verified_rows=verification.checked_rows,
        scalar_mismatches=verification.scalar_mismatches,
        identity_mismatches=verification.identity_mismatches,
        flag_mismatches=verification.flag_mismatches,
        deleted_rows=deleted,
        v1_rows_after=after,
    )
