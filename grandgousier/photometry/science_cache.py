from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text

from grandgousier.calibration.reconstruction import reconstruct_calibration_measurement_states
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks
from sqlalchemy.orm import Session


IMMUTABLE_SCIENCE_STATUSES = {"COMPLETE_IMMUTABLE", "SUPERSEDED_IMMUTABLE"}


@dataclass(frozen=True, slots=True)
class ScienceCacheBackfillResult:
    calibration_version_id: int
    calibration_name: str
    lifecycle_status: str
    source_rows: int
    source_distinct_base_rows: int
    existing_cache_rows: int
    written_rows: int
    cache_rows_after: int
    scalar_mismatches: int
    status: str
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_version_id": self.calibration_version_id,
            "calibration_name": self.calibration_name,
            "lifecycle_status": self.lifecycle_status,
            "source_rows": self.source_rows,
            "source_distinct_base_rows": self.source_distinct_base_rows,
            "existing_cache_rows": self.existing_cache_rows,
            "written_rows": self.written_rows,
            "cache_rows_after": self.cache_rows_after,
            "scalar_mismatches": self.scalar_mismatches,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ScienceCacheMaterializationResult:
    calibration_version_id: int
    calibration_name: str
    base_calibration_version_id: int
    reconstructed_rows: int
    written_rows: int
    cache_rows_after: int
    write_strategy: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_version_id": self.calibration_version_id,
            "calibration_name": self.calibration_name,
            "base_calibration_version_id": self.base_calibration_version_id,
            "reconstructed_rows": self.reconstructed_rows,
            "written_rows": self.written_rows,
            "cache_rows_after": self.cache_rows_after,
            "write_strategy": self.write_strategy,
            "status": self.status,
        }


def materialize_science_photometry_cache(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    frame_ids: Iterable[int] | None = None,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> ScienceCacheMaterializationResult:
    """Materialize only the rebuildable hot SCIENCE cache.

    Authoritative science state remains immutable BASE photometry plus normalized
    calibration terms.  This writer deliberately does *not* create a wide
    ``science_measurements`` layer.  PostgreSQL reuses the already validated
    server reconstruction query when possible; other/test backends use the
    authoritative Python reconstructor.  The caller's transaction provides
    atomicity: an incomplete cache is never committed as a successful pipeline
    step.
    """

    # Import lazily so the legacy wide materializer can remain independent of
    # the compact cache module.  This also keeps private server-reconstruction
    # helpers localized to the compatibility boundary.
    from grandgousier.photometry import science_materialization as legacy

    target_cv = legacy._resolve_cv(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    target_id = int(target_cv.calibration_version_id)

    session.execute(
        text(
            "DELETE FROM science_photometry_cache "
            "WHERE calibration_version_id = :cv"
        ),
        {"cv": target_id},
    )

    if legacy._postgres_server_reconstruction_capable(session):
        plan = legacy._build_server_reconstruction_plan(
            session,
            target_cv=target_cv,
            frame_ids=frame_ids,
        )
        if plan is not None:
            wide_sql, params = legacy._build_postgres_server_reconstruction_sql(plan)
            reconstruction_select = legacy._server_reconstruction_select_sql(wide_sql)
            column_list = ", ".join(f'"{name}"' for name in legacy._SCIENCE_WRITE_COLUMNS)
            cache_sql = f"""
                WITH reconstructed ({column_list}) AS (
                    {reconstruction_select}
                )
                INSERT INTO science_photometry_cache (
                    calibration_version_id,
                    base_measurement_id,
                    mag_cal,
                    mag_err_random
                )
                SELECT
                    calibration_version_id,
                    base_measurement_id,
                    mag_cal,
                    mag_err_random
                FROM reconstructed
                ORDER BY base_measurement_id
                ON CONFLICT (calibration_version_id, base_measurement_id)
                DO UPDATE SET
                    mag_cal = EXCLUDED.mag_cal,
                    mag_err_random = EXCLUDED.mag_err_random
            """
            execution = session.execute(text(cache_sql), params)
            written = int(execution.rowcount or 0)
            cache_rows = int(
                session.execute(
                    text(
                        "SELECT COUNT(*)::bigint FROM science_photometry_cache "
                        "WHERE calibration_version_id = :cv"
                    ),
                    {"cv": target_id},
                ).scalar_one()
                or 0
            )
            if written < 0:
                written = cache_rows
            legacy._raise_if_empty_science_reconstruction(
                session,
                base_calibration_version_id=int(plan.base_calibration_version_id),
                frame_ids=plan.frame_ids,
                reconstructed_rows=cache_rows,
                calibration_name=str(plan.target_calibration_name),
            )
            if cache_rows != written:
                # For drivers that report an exact rowcount, require exact
                # agreement.  A negative rowcount was normalized above.
                raise RuntimeError(
                    "science cache row-count mismatch after server reconstruction: "
                    f"written={written}, cache_rows={cache_rows}"
                )
            return ScienceCacheMaterializationResult(
                calibration_version_id=target_id,
                calibration_name=str(target_cv.name),
                base_calibration_version_id=int(plan.base_calibration_version_id),
                reconstructed_rows=cache_rows,
                written_rows=written,
                cache_rows_after=cache_rows,
                write_strategy="POSTGRESQL_SERVER_RECONSTRUCTION_CACHE",
                status="OK",
            )

    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
        frame_ids=frame_ids,
    )
    rows = [
        {
            "calibration_version_id": target_id,
            "base_measurement_id": int(state["base_measurement_id"]),
            "mag_cal": state.get("mag_cal"),
            "mag_err_random": state.get("mag_err_random"),
        }
        for _, state in sorted(reconstructed.states.items())
    ]
    written = 0
    statement = text(
        """
        INSERT INTO science_photometry_cache (
            calibration_version_id, base_measurement_id, mag_cal, mag_err_random
        ) VALUES (
            :calibration_version_id, :base_measurement_id, :mag_cal, :mag_err_random
        )
        ON CONFLICT (calibration_version_id, base_measurement_id)
        DO UPDATE SET
            mag_cal = EXCLUDED.mag_cal,
            mag_err_random = EXCLUDED.mag_err_random
        """
    )
    for chunk in iter_sized_chunks(
        rows,
        n_columns=4,
        requested=int(insert_batch_size),
    ):
        session.execute(statement, list(chunk))
        written += len(chunk)

    cache_rows = int(
        session.execute(
            text(
                "SELECT COUNT(*)::bigint FROM science_photometry_cache "
                "WHERE calibration_version_id = :cv"
            ),
            {"cv": target_id},
        ).scalar_one()
        or 0
    )
    legacy._raise_if_empty_science_reconstruction(
        session,
        base_calibration_version_id=int(reconstructed.base_calibration_version_id),
        frame_ids=frame_ids,
        reconstructed_rows=cache_rows,
        calibration_name=str(reconstructed.target_calibration_name),
    )
    if cache_rows != len(rows):
        raise RuntimeError(
            "science cache row-count mismatch after Python reconstruction: "
            f"expected={len(rows)}, cache_rows={cache_rows}"
        )
    return ScienceCacheMaterializationResult(
        calibration_version_id=target_id,
        calibration_name=str(target_cv.name),
        base_calibration_version_id=int(reconstructed.base_calibration_version_id),
        reconstructed_rows=len(rows),
        written_rows=written,
        cache_rows_after=cache_rows,
        write_strategy="PYTHON_RECONSTRUCTION_CACHE",
        status="OK",
    )


def complete_cache_calibration_ids(
    session: Session,
    calibration_version_ids: Iterable[int],
) -> set[int]:
    """Return calibrations whose compact cache has provably complete coverage.

    Completeness is checked against V1 while legacy rows exist.  After controlled
    V1 retirement, the immutable ``RETIRED_EXACT`` override manifest preserves
    the original source row count and becomes the coverage reference.  Only
    compact-only products that never owned V1 use the final non-empty/atomic
    writer contract.  This prevents a partial cache from silently becoming
    authoritative merely because V1 was deleted.
    """

    ids = sorted({int(value) for value in calibration_version_ids})
    if not ids:
        return set()
    cache_rows = session.execute(
        text(
            """
            SELECT calibration_version_id, COUNT(*)::bigint AS n
            FROM science_photometry_cache
            WHERE calibration_version_id = ANY(:ids)
            GROUP BY calibration_version_id
            """
        ),
        {"ids": ids},
    ).mappings().all()
    v1_rows = session.execute(
        text(
            """
            SELECT calibration_version_id, COUNT(*)::bigint AS n
            FROM science_measurements
            WHERE calibration_version_id = ANY(:ids)
            GROUP BY calibration_version_id
            """
        ),
        {"ids": ids},
    ).mappings().all()
    cache_counts = {int(row["calibration_version_id"]): int(row["n"] or 0) for row in cache_rows}
    v1_counts = {int(row["calibration_version_id"]): int(row["n"] or 0) for row in v1_rows}
    no_v1_ids = [cv_id for cv_id in ids if v1_counts.get(cv_id, 0) == 0]
    retired_rows = []
    if no_v1_ids:
        retired_rows = session.execute(
            text(
                """
                SELECT calibration_version_id, source_row_count::bigint AS n
                FROM science_flag_override_sets
                WHERE calibration_version_id = ANY(:ids)
                  AND status = 'RETIRED_EXACT'
                """
            ),
            {"ids": no_v1_ids},
        ).mappings().all()
    retired_counts = {int(row["calibration_version_id"]): int(row["n"] or 0) for row in retired_rows}

    complete: set[int] = set()
    for cv_id in ids:
        cache_count = cache_counts.get(cv_id, 0)
        if cache_count <= 0:
            continue
        v1_count = v1_counts.get(cv_id, 0)
        if v1_count > 0:
            if cache_count == v1_count:
                complete.add(cv_id)
            continue
        retired_count = retired_counts.get(cv_id, 0)
        if retired_count > 0:
            if cache_count == retired_count:
                complete.add(cv_id)
            continue
        # New compact-only product: cache was written atomically by the
        # authoritative reconstruction writer and has no legacy V1 contract.
        complete.add(cv_id)
    return complete


def select_science_calibration_ids(
    session: Session,
    *,
    calibration_version_ids: Iterable[int] = (),
    field_id: int | None = None,
    all_materialized: bool = False,
    include_superseded: bool = False,
) -> list[int]:
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
            ORDER BY cv.calibration_version_id
            """
        )
        params = {"field_id": int(field_id), "statuses": statuses}

    rows = session.execute(statement, params).scalars().all()
    return [int(value) for value in rows]


def backfill_science_photometry_cache(
    session: Session,
    *,
    calibration_version_id: int,
    execute: bool = False,
) -> ScienceCacheBackfillResult:
    cv = session.execute(
        text(
            """
            SELECT calibration_version_id, name, product_role, lifecycle_status
            FROM calibration_versions
            WHERE calibration_version_id = :cv
            """
        ),
        {"cv": int(calibration_version_id)},
    ).mappings().first()
    if cv is None:
        raise ValueError(f"Calibration version not found: {calibration_version_id}")
    if str(cv["product_role"] or "").upper() != "SCIENCE":
        raise ValueError(
            f"Calibration {calibration_version_id} is not SCIENCE: product_role={cv['product_role']!r}"
        )
    lifecycle_status = str(cv["lifecycle_status"] or "").upper()
    if lifecycle_status not in IMMUTABLE_SCIENCE_STATUSES:
        raise ValueError(
            f"Calibration {calibration_version_id} is not immutable: lifecycle_status={lifecycle_status!r}"
        )

    stats = session.execute(
        text(
            """
            SELECT
                COUNT(*)::bigint AS source_rows,
                COUNT(DISTINCT sm.base_measurement_id)::bigint AS distinct_base_rows,
                COUNT(*) FILTER (WHERE sm.base_measurement_id IS NULL)::bigint AS missing_base_id,
                COUNT(*) FILTER (WHERE bm.measurement_id IS NULL)::bigint AS missing_base_row,
                COUNT(*) FILTER (
                    WHERE bm.measurement_id IS NOT NULL
                      AND (bm.object_id IS DISTINCT FROM sm.object_id
                           OR bm.frame_id IS DISTINCT FROM sm.frame_id)
                )::bigint AS identity_mismatches,
                COUNT(*) FILTER (
                    WHERE sm.mag_err IS DISTINCT FROM sm.mag_err_random
                )::bigint AS mag_err_contract_mismatches
            FROM science_measurements sm
            LEFT JOIN measurements bm ON bm.measurement_id = sm.base_measurement_id
            WHERE sm.calibration_version_id = :cv
            """
        ),
        {"cv": int(calibration_version_id)},
    ).mappings().one()
    source_rows = int(stats["source_rows"] or 0)
    distinct_base_rows = int(stats["distinct_base_rows"] or 0)
    existing_cache_rows = int(
        session.execute(
            text(
                "SELECT COUNT(*)::bigint FROM science_photometry_cache "
                "WHERE calibration_version_id = :cv"
            ),
            {"cv": int(calibration_version_id)},
        ).scalar_one()
        or 0
    )

    problems: list[str] = []
    if source_rows <= 0:
        problems.append("NO_V1_SCIENCE_ROWS")
    if int(stats["missing_base_id"] or 0):
        problems.append(f"MISSING_BASE_ID={int(stats['missing_base_id'])}")
    if int(stats["missing_base_row"] or 0):
        problems.append(f"MISSING_BASE_ROW={int(stats['missing_base_row'])}")
    if int(stats["identity_mismatches"] or 0):
        problems.append(f"BASE_IDENTITY_MISMATCH={int(stats['identity_mismatches'])}")
    if int(stats["mag_err_contract_mismatches"] or 0):
        problems.append(
            f"MAG_ERR_NE_RANDOM={int(stats['mag_err_contract_mismatches'])}"
        )
    if distinct_base_rows != source_rows:
        problems.append(
            f"NON_UNIQUE_BASE_MAPPING={source_rows - distinct_base_rows}"
        )
    if problems:
        return ScienceCacheBackfillResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv["name"]),
            lifecycle_status=lifecycle_status,
            source_rows=source_rows,
            source_distinct_base_rows=distinct_base_rows,
            existing_cache_rows=existing_cache_rows,
            written_rows=0,
            cache_rows_after=existing_cache_rows,
            scalar_mismatches=0,
            status="BLOCKED",
            reason=";".join(problems),
        )

    if not execute:
        return ScienceCacheBackfillResult(
            calibration_version_id=int(calibration_version_id),
            calibration_name=str(cv["name"]),
            lifecycle_status=lifecycle_status,
            source_rows=source_rows,
            source_distinct_base_rows=distinct_base_rows,
            existing_cache_rows=existing_cache_rows,
            written_rows=0,
            cache_rows_after=existing_cache_rows,
            scalar_mismatches=0,
            status="READY",
        )

    session.execute(
        text(
            "DELETE FROM science_photometry_cache WHERE calibration_version_id = :cv"
        ),
        {"cv": int(calibration_version_id)},
    )
    written = int(
        session.execute(
            text(
                """
                INSERT INTO science_photometry_cache (
                    calibration_version_id,
                    base_measurement_id,
                    mag_cal,
                    mag_err_random
                )
                SELECT
                    sm.calibration_version_id,
                    sm.base_measurement_id,
                    sm.mag_cal,
                    sm.mag_err_random
                FROM science_measurements sm
                WHERE sm.calibration_version_id = :cv
                ORDER BY sm.base_measurement_id
                """
            ),
            {"cv": int(calibration_version_id)},
        ).rowcount
        or 0
    )
    cache_rows_after = int(
        session.execute(
            text(
                "SELECT COUNT(*)::bigint FROM science_photometry_cache "
                "WHERE calibration_version_id = :cv"
            ),
            {"cv": int(calibration_version_id)},
        ).scalar_one()
        or 0
    )
    scalar_mismatches = int(
        session.execute(
            text(
                """
                SELECT COUNT(*)::bigint
                FROM science_measurements sm
                FULL OUTER JOIN science_photometry_cache c
                  ON c.calibration_version_id = sm.calibration_version_id
                 AND c.base_measurement_id = sm.base_measurement_id
                WHERE COALESCE(sm.calibration_version_id, c.calibration_version_id) = :cv
                  AND (
                    sm.base_measurement_id IS NULL
                    OR c.base_measurement_id IS NULL
                    OR sm.mag_cal IS DISTINCT FROM c.mag_cal
                    OR sm.mag_err_random IS DISTINCT FROM c.mag_err_random
                  )
                """
            ),
            {"cv": int(calibration_version_id)},
        ).scalar_one()
        or 0
    )
    status = "OK" if cache_rows_after == source_rows and scalar_mismatches == 0 else "MISMATCH"
    reason = None
    if status != "OK":
        reason = (
            f"source_rows={source_rows};cache_rows={cache_rows_after};"
            f"scalar_mismatches={scalar_mismatches}"
        )
    return ScienceCacheBackfillResult(
        calibration_version_id=int(calibration_version_id),
        calibration_name=str(cv["name"]),
        lifecycle_status=lifecycle_status,
        source_rows=source_rows,
        source_distinct_base_rows=distinct_base_rows,
        existing_cache_rows=existing_cache_rows,
        written_rows=written,
        cache_rows_after=cache_rows_after,
        scalar_mismatches=scalar_mismatches,
        status=status,
        reason=reason,
    )
