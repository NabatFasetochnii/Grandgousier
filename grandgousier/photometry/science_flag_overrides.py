from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import _RECONSTRUCTED_FIELDS
from grandgousier.db.models import (
    CalibrationVersion,
    Measurement,
    ScienceFlagOverride,
    ScienceFlagOverrideSet,
    ScienceMeasurement,
)
from grandgousier.photometry.science_flags import (
    apply_top_level_flag_patch,
    exact_top_level_flag_patch,
    merge_effective_science_flags,
)


OVERRIDE_ALGORITHM_VERSION = "TOPLEVEL_EXACT_V1"
COMPLETE_OVERRIDE_STATUS = "COMPLETE_EXACT"
RETIRED_OVERRIDE_STATUS = "RETIRED_EXACT"
READABLE_OVERRIDE_STATUSES = frozenset({COMPLETE_OVERRIDE_STATUS, RETIRED_OVERRIDE_STATUS})
IMMUTABLE_SCIENCE_STATUSES = frozenset({"COMPLETE_IMMUTABLE", "SUPERSEDED_IMMUTABLE"})


def select_v1_science_calibration_ids(
    session: Session,
    *,
    calibration_version_ids: Iterable[int] = (),
    field_id: int | None = None,
    all_materialized: bool = False,
    include_superseded: bool = False,
) -> list[int]:
    """Select immutable SCIENCE products that actually have legacy V1 rows.

    Override retirement is meaningful only for historical ``science_measurements``.
    Compact-only products and older calibration records without V1 materialization are
    intentionally excluded from field-wide selection so a retirement batch cannot be
    blocked by products that have nothing to retire.  Explicit IDs are preserved as
    requested and will receive the normal ``NO_V1_SCIENCE_ROWS`` diagnostic.
    """

    explicit = sorted({int(value) for value in calibration_version_ids})
    scope_count = int(bool(explicit)) + int(field_id is not None) + int(bool(all_materialized))
    if scope_count > 1:
        raise ValueError(
            "Use exactly one of calibration_version_ids, field_id, or all_materialized"
        )
    if explicit:
        return explicit
    if field_id is None and not all_materialized:
        raise ValueError(
            "calibration_version_ids, field_id, or all_materialized is required"
        )

    statuses = ["COMPLETE_IMMUTABLE"]
    if include_superseded:
        statuses.append("SUPERSEDED_IMMUTABLE")
    if all_materialized:
        statement = text(
            """
            SELECT cv.calibration_version_id
            FROM calibration_versions cv
            WHERE cv.product_role = 'SCIENCE'
              AND cv.lifecycle_status = ANY(:statuses)
              AND EXISTS (
                  SELECT 1
                  FROM science_measurements sm
                  WHERE sm.calibration_version_id = cv.calibration_version_id
              )
            ORDER BY cv.calibration_version_id
            """
        )
        params = {"statuses": statuses}
    else:
        statement = text(
            """
            SELECT cv.calibration_version_id
            FROM calibration_versions cv
            WHERE cv.field_id = :field_id
              AND cv.product_role = 'SCIENCE'
              AND cv.lifecycle_status = ANY(:statuses)
              AND EXISTS (
                  SELECT 1
                  FROM science_measurements sm
                  WHERE sm.calibration_version_id = cv.calibration_version_id
              )
            ORDER BY cv.calibration_version_id
            """
        )
        params = {"field_id": int(field_id), "statuses": statuses}

    rows = session.execute(statement, params).scalars().all()
    return [int(value) for value in rows]


@dataclass(slots=True)
class ScienceFlagOverrideBackfillResult:
    calibration_version_id: int
    calibration_name: str
    lifecycle_status: str
    source_rows: int
    sampled_or_checked_rows: int
    flags_exact_without_patch: int
    rows_requiring_patch: int
    common_override_keys: int
    common_removed_keys: int
    row_override_rows: int
    scalar_mismatches: int
    identity_mismatches: int
    patch_failures: int
    written_row_overrides: int
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



@dataclass(slots=True)
class ScienceFlagOverrideVerifyResult:
    calibration_version_id: int
    calibration_name: str
    source_rows: int
    checked_rows: int
    scalar_mismatches: int
    identity_mismatches: int
    flag_mismatches: int
    manifest_row_override_count: int
    actual_row_override_count: int
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def apply_complete_science_flag_overrides(
    session: Session,
    *,
    calibration_version_id: int,
    states: dict[tuple[int, int], dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    """Apply a completed exact historical flag patch to reconstructed states.

    Absence of a completed manifest means normalized reconstruction remains the
    public flag representation.  This is the expected state for new compact-only
    SCIENCE products.  Historical compatibility is enabled only after an all-row
    exact backfill has committed ``COMPLETE_EXACT``.
    """

    manifest = session.execute(
        select(
            ScienceFlagOverrideSet.common_override_flags,
            ScienceFlagOverrideSet.common_removed_keys,
            ScienceFlagOverrideSet.status,
        ).where(
            ScienceFlagOverrideSet.calibration_version_id == int(calibration_version_id)
        )
    ).mappings().first()
    if manifest is None or str(manifest["status"] or "").upper() not in READABLE_OVERRIDE_STATUSES:
        return states
    if not states:
        return states

    common_overrides = dict(manifest["common_override_flags"] or {})
    common_removed = [str(value) for value in (manifest["common_removed_keys"] or [])]
    base_ids = sorted(
        {
            int(state["base_measurement_id"])
            for state in states.values()
            if state.get("base_measurement_id") is not None
        }
    )
    residuals: dict[int, tuple[dict[str, Any], list[str]]] = {}
    for chunk_start in range(0, len(base_ids), 10_000):
        chunk = base_ids[chunk_start : chunk_start + 10_000]
        if not chunk:
            continue
        rows = session.execute(
            select(
                ScienceFlagOverride.base_measurement_id,
                ScienceFlagOverride.override_flags,
                ScienceFlagOverride.removed_keys,
            ).where(
                ScienceFlagOverride.calibration_version_id == int(calibration_version_id),
                ScienceFlagOverride.base_measurement_id.in_(chunk),
            )
        ).mappings()
        for row in rows:
            residuals[int(row["base_measurement_id"])] = (
                dict(row["override_flags"] or {}),
                [str(value) for value in (row["removed_keys"] or [])],
            )

    out: dict[tuple[int, int], dict[str, Any]] = {}
    for key, source in states.items():
        row = dict(source)
        flags = apply_top_level_flag_patch(
            row.get("flags"), common_overrides, common_removed
        )
        base_id = row.get("base_measurement_id")
        residual = residuals.get(int(base_id)) if base_id is not None else None
        if residual is not None:
            flags = apply_top_level_flag_patch(flags, residual[0], residual[1])
        row["flags"] = flags
        out[key] = row
    return out


def _finite_equivalent(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return left == right
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= float(tolerance)


def _frame_batches(session: Session, calibration_version_id: int, batch_size: int) -> list[list[int]]:
    frame_ids = [
        int(value)
        for value in session.execute(
            select(ScienceMeasurement.frame_id)
            .where(ScienceMeasurement.calibration_version_id == int(calibration_version_id))
            .distinct()
            .order_by(ScienceMeasurement.frame_id)
        ).scalars()
    ]
    return [frame_ids[i : i + batch_size] for i in range(0, len(frame_ids), batch_size)]


def _stored_rows_for_frames(
    session: Session,
    *,
    calibration_version_id: int,
    frame_ids: list[int],
) -> list[dict[str, Any]]:
    stmt = (
        select(
            ScienceMeasurement.base_measurement_id.label("base_measurement_id"),
            ScienceMeasurement.object_id.label("object_id"),
            ScienceMeasurement.frame_id.label("frame_id"),
            *[
                getattr(ScienceMeasurement, field_name).label(field_name)
                for field_name in _RECONSTRUCTED_FIELDS
            ],
            ScienceMeasurement.flags.label("science_flags"),
            Measurement.flags.label("base_flags"),
        )
        .join(Measurement, Measurement.measurement_id == ScienceMeasurement.base_measurement_id)
        .where(
            ScienceMeasurement.calibration_version_id == int(calibration_version_id),
            ScienceMeasurement.frame_id.in_(frame_ids),
        )
        .order_by(ScienceMeasurement.object_id, ScienceMeasurement.frame_id)
    )
    return [dict(row) for row in session.execute(stmt).mappings().all()]


def _intersect_common_overrides(
    current: dict[str, Any] | None,
    row_overrides: dict[str, Any],
) -> dict[str, Any]:
    if current is None:
        return dict(row_overrides)
    return {
        key: value
        for key, value in current.items()
        if key in row_overrides and row_overrides[key] == value
    }


def _intersect_common_removed(
    current: set[str] | None,
    row_removed: Iterable[str],
) -> set[str]:
    values = {str(value) for value in row_removed}
    if current is None:
        return values
    return current.intersection(values)


def backfill_science_flag_overrides(
    session: Session,
    *,
    calibration_version_id: int,
    execute: bool = False,
    frame_batch_size: int = 4,
    tolerance: float = 1e-10,
) -> ScienceFlagOverrideBackfillResult:
    """Build an all-row exact historical flag patch from V1 SCIENCE storage.

    Two passes are deliberate.  The first proves scalar/identity parity and finds
    the calibration-wide intersection of the historical flag patch.  The second
    stores only residual per-row differences after that common patch.  No V1 row
    is modified or deleted.
    """

    if int(frame_batch_size) < 1:
        raise ValueError("frame_batch_size must be >= 1")

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
            f"Calibration {calibration_version_id} is not immutable: lifecycle_status={lifecycle_status!r}"
        )

    source_rows = int(
        session.scalar(
            select(func.count(ScienceMeasurement.science_measurement_id)).where(
                ScienceMeasurement.calibration_version_id == int(calibration_version_id)
            )
        )
        or 0
    )
    if source_rows <= 0:
        return ScienceFlagOverrideBackfillResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv.name),
            lifecycle_status=lifecycle_status,
            source_rows=0,
            sampled_or_checked_rows=0,
            flags_exact_without_patch=0,
            rows_requiring_patch=0,
            common_override_keys=0,
            common_removed_keys=0,
            row_override_rows=0,
            scalar_mismatches=0,
            identity_mismatches=0,
            patch_failures=0,
            written_row_overrides=0,
            status="BLOCKED",
            reason="NO_V1_SCIENCE_ROWS",
        )

    from grandgousier.calibration.reconstruction import reconstruct_calibration_measurement_states

    batches = _frame_batches(session, int(calibration_version_id), int(frame_batch_size))
    checked_rows = 0
    scalar_mismatches = 0
    identity_mismatches = 0
    exact_without_patch = 0
    rows_requiring_patch = 0
    patch_failures = 0
    common_overrides: dict[str, Any] | None = None
    common_removed: set[str] | None = None

    # Pass 1: prove scalar/identity parity and derive the calibration-common patch.
    for frame_ids in batches:
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
            apply_historical_flag_overrides=False,
        )
        stored_rows = _stored_rows_for_frames(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
        )
        for stored in stored_rows:
            checked_rows += 1
            key = (int(stored["object_id"]), int(stored["frame_id"]))
            state = reconstructed.states.get(key)
            if state is None:
                identity_mismatches += 1
                row_overrides: dict[str, Any] = {}
                row_removed: list[str] = []
            else:
                if int(state.get("base_measurement_id") or -1) != int(
                    stored.get("base_measurement_id") or -2
                ):
                    identity_mismatches += 1
                for field_name in _RECONSTRUCTED_FIELDS:
                    if not _finite_equivalent(
                        state.get(field_name), stored.get(field_name), float(tolerance)
                    ):
                        scalar_mismatches += 1
                historical = merge_effective_science_flags(
                    stored.get("base_flags"), stored.get("science_flags")
                )
                predicted = dict(state.get("flags") or {})
                if predicted == historical:
                    exact_without_patch += 1
                    row_overrides = {}
                    row_removed = []
                else:
                    rows_requiring_patch += 1
                    row_overrides, row_removed = exact_top_level_flag_patch(
                        predicted, historical
                    )
                    if apply_top_level_flag_patch(
                        predicted, row_overrides, row_removed
                    ) != historical:
                        patch_failures += 1
            common_overrides = _intersect_common_overrides(
                common_overrides, row_overrides
            )
            common_removed = _intersect_common_removed(common_removed, row_removed)

    common_overrides = dict(common_overrides or {})
    common_removed_sorted = sorted(common_removed or set())
    if checked_rows != source_rows:
        identity_mismatches += abs(source_rows - checked_rows)

    if scalar_mismatches or identity_mismatches or patch_failures:
        return ScienceFlagOverrideBackfillResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv.name),
            lifecycle_status=lifecycle_status,
            source_rows=source_rows,
            sampled_or_checked_rows=checked_rows,
            flags_exact_without_patch=exact_without_patch,
            rows_requiring_patch=rows_requiring_patch,
            common_override_keys=len(common_overrides),
            common_removed_keys=len(common_removed_sorted),
            row_override_rows=0,
            scalar_mismatches=scalar_mismatches,
            identity_mismatches=identity_mismatches,
            patch_failures=patch_failures,
            written_row_overrides=0,
            status="BLOCKED",
            reason=(
                f"scalar_mismatches={scalar_mismatches};"
                f"identity_mismatches={identity_mismatches};"
                f"patch_failures={patch_failures}"
            ),
        )

    # Pass 2: after applying the common patch, count/store only row residuals.
    residual_count = 0
    residual_failures = 0
    written = 0
    if execute:
        session.execute(
            text("DELETE FROM science_flag_override_sets WHERE calibration_version_id = :cv"),
            {"cv": int(calibration_version_id)},
        )
        session.execute(
            text(
                """
                INSERT INTO science_flag_override_sets (
                    calibration_version_id,
                    source_row_count,
                    row_override_count,
                    common_override_flags,
                    common_removed_keys,
                    algorithm_version,
                    status
                ) VALUES (
                    :cv, :source_rows, 0, CAST(:common_overrides AS jsonb),
                    CAST(:common_removed AS jsonb), :algorithm, 'BUILDING'
                )
                """
            ),
            {
                "cv": int(calibration_version_id),
                "source_rows": source_rows,
                "common_overrides": __import__("json").dumps(
                    common_overrides, ensure_ascii=False, separators=(",", ":")
                ),
                "common_removed": __import__("json").dumps(
                    common_removed_sorted, ensure_ascii=False, separators=(",", ":")
                ),
                "algorithm": OVERRIDE_ALGORITHM_VERSION,
            },
        )

    for frame_ids in batches:
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
            apply_historical_flag_overrides=False,
        )
        stored_rows = _stored_rows_for_frames(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
        )
        payload: list[dict[str, Any]] = []
        for stored in stored_rows:
            key = (int(stored["object_id"]), int(stored["frame_id"]))
            state = reconstructed.states.get(key)
            if state is None:
                residual_failures += 1
                continue
            historical = merge_effective_science_flags(
                stored.get("base_flags"), stored.get("science_flags")
            )
            predicted_common = apply_top_level_flag_patch(
                state.get("flags"), common_overrides, common_removed_sorted
            )
            if predicted_common == historical:
                continue
            residual_overrides, residual_removed = exact_top_level_flag_patch(
                predicted_common, historical
            )
            restored = apply_top_level_flag_patch(
                predicted_common, residual_overrides, residual_removed
            )
            if restored != historical:
                residual_failures += 1
                continue
            residual_count += 1
            if execute:
                payload.append(
                    {
                        "cv": int(calibration_version_id),
                        "base_id": int(stored["base_measurement_id"]),
                        "overrides": __import__("json").dumps(
                            residual_overrides,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        "removed": __import__("json").dumps(
                            residual_removed, ensure_ascii=False, separators=(",", ":")
                        ),
                    }
                )
        if execute and payload:
            result = session.execute(
                text(
                    """
                    INSERT INTO science_flag_overrides (
                        calibration_version_id,
                        base_measurement_id,
                        override_flags,
                        removed_keys
                    ) VALUES (
                        :cv, :base_id, CAST(:overrides AS jsonb), CAST(:removed AS jsonb)
                    )
                    """
                ),
                payload,
            )
            rowcount = getattr(result, "rowcount", None)
            written += (
                len(payload)
                if rowcount is None or int(rowcount) < 0
                else int(rowcount)
            )

    if residual_failures:
        if execute:
            raise RuntimeError(
                "Historical flag residual verification failed after common factoring: "
                f"calibration_version_id={calibration_version_id}, failures={residual_failures}"
            )
        return ScienceFlagOverrideBackfillResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv.name),
            lifecycle_status=lifecycle_status,
            source_rows=source_rows,
            sampled_or_checked_rows=checked_rows,
            flags_exact_without_patch=exact_without_patch,
            rows_requiring_patch=rows_requiring_patch,
            common_override_keys=len(common_overrides),
            common_removed_keys=len(common_removed_sorted),
            row_override_rows=residual_count,
            scalar_mismatches=0,
            identity_mismatches=0,
            patch_failures=residual_failures,
            written_row_overrides=0,
            status="BLOCKED",
            reason=f"residual_patch_failures={residual_failures}",
        )

    status = "READY"
    if execute:
        session.execute(
            text(
                """
                UPDATE science_flag_override_sets
                SET row_override_count = :row_count,
                    status = :status,
                    updated_at = now()
                WHERE calibration_version_id = :cv
                """
            ),
            {
                "cv": int(calibration_version_id),
                "row_count": residual_count,
                "status": COMPLETE_OVERRIDE_STATUS,
            },
        )
        status = "OK"

    return ScienceFlagOverrideBackfillResult(
        calibration_version_id=int(calibration_version_id),
        calibration_name=str(cv.name),
        lifecycle_status=lifecycle_status,
        source_rows=source_rows,
        sampled_or_checked_rows=checked_rows,
        flags_exact_without_patch=exact_without_patch,
        rows_requiring_patch=rows_requiring_patch,
        common_override_keys=len(common_overrides),
        common_removed_keys=len(common_removed_sorted),
        row_override_rows=residual_count,
        scalar_mismatches=0,
        identity_mismatches=0,
        patch_failures=0,
        written_row_overrides=written,
        status=status,
    )


def verify_science_flag_overrides(
    session: Session,
    *,
    calibration_version_id: int,
    frame_batch_size: int = 4,
    tolerance: float = 1e-10,
) -> ScienceFlagOverrideVerifyResult:
    """All-row proof that stored compact overrides exactly reproduce V1."""

    cv = session.get(CalibrationVersion, int(calibration_version_id))
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_version_id}")
    manifest = session.get(ScienceFlagOverrideSet, int(calibration_version_id))
    if manifest is None or str(manifest.status or "").upper() != COMPLETE_OVERRIDE_STATUS:
        return ScienceFlagOverrideVerifyResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv.name),
            source_rows=0,
            checked_rows=0,
            scalar_mismatches=0,
            identity_mismatches=0,
            flag_mismatches=0,
            manifest_row_override_count=0,
            actual_row_override_count=0,
            status="BLOCKED",
            reason="NO_COMPLETE_OVERRIDE_MANIFEST",
        )
    source_rows = int(
        session.scalar(
            select(func.count(ScienceMeasurement.science_measurement_id)).where(
                ScienceMeasurement.calibration_version_id == int(calibration_version_id)
            )
        )
        or 0
    )
    if source_rows <= 0:
        return ScienceFlagOverrideVerifyResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv.name),
            source_rows=0,
            checked_rows=0,
            scalar_mismatches=0,
            identity_mismatches=0,
            flag_mismatches=0,
            manifest_row_override_count=int(manifest.row_override_count or 0),
            actual_row_override_count=0,
            status="BLOCKED",
            reason="NO_V1_SCIENCE_ROWS",
        )
    from grandgousier.calibration.reconstruction import reconstruct_calibration_measurement_states

    checked = 0
    scalar_mismatches = 0
    identity_mismatches = 0
    flag_mismatches = 0
    for frame_ids in _frame_batches(session, int(calibration_version_id), int(frame_batch_size)):
        reconstructed = reconstruct_calibration_measurement_states(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
            apply_historical_flag_overrides=True,
        )
        stored_rows = _stored_rows_for_frames(
            session,
            calibration_version_id=int(calibration_version_id),
            frame_ids=frame_ids,
        )
        for stored in stored_rows:
            checked += 1
            key = (int(stored["object_id"]), int(stored["frame_id"]))
            state = reconstructed.states.get(key)
            if state is None:
                identity_mismatches += 1
                continue
            if int(state.get("base_measurement_id") or -1) != int(
                stored.get("base_measurement_id") or -2
            ):
                identity_mismatches += 1
            for field_name in _RECONSTRUCTED_FIELDS:
                if not _finite_equivalent(
                    state.get(field_name), stored.get(field_name), float(tolerance)
                ):
                    scalar_mismatches += 1
            historical = merge_effective_science_flags(
                stored.get("base_flags"), stored.get("science_flags")
            )
            if dict(state.get("flags") or {}) != historical:
                flag_mismatches += 1

    actual_row_override_count = int(
        session.scalar(
            select(func.count()).select_from(ScienceFlagOverride).where(
                ScienceFlagOverride.calibration_version_id == int(calibration_version_id)
            )
        )
        or 0
    )
    reasons: list[str] = []
    if checked != source_rows:
        reasons.append(f"ROW_COUNT={checked}/{source_rows}")
    if scalar_mismatches:
        reasons.append(f"SCALAR_MISMATCHES={scalar_mismatches}")
    if identity_mismatches:
        reasons.append(f"IDENTITY_MISMATCHES={identity_mismatches}")
    if flag_mismatches:
        reasons.append(f"FLAG_MISMATCHES={flag_mismatches}")
    if actual_row_override_count != int(manifest.row_override_count or 0):
        reasons.append(
            "ROW_OVERRIDE_COUNT="
            f"{actual_row_override_count}/{int(manifest.row_override_count or 0)}"
        )
    return ScienceFlagOverrideVerifyResult(
        calibration_version_id=int(calibration_version_id),
        calibration_name=str(cv.name),
        source_rows=source_rows,
        checked_rows=checked,
        scalar_mismatches=scalar_mismatches,
        identity_mismatches=identity_mismatches,
        flag_mismatches=flag_mismatches,
        manifest_row_override_count=int(manifest.row_override_count or 0),
        actual_row_override_count=actual_row_override_count,
        status="OK" if not reasons else "MISMATCH",
        reason=";".join(reasons) if reasons else None,
    )
