from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.calibration.reconstruction import reconstruct_calibration_measurement_states
from grandgousier.db.bulk import DEFAULT_BULK_INSERT_BATCH_SIZE, iter_sized_chunks


SCIENCE_SCALAR_COLUMNS: tuple[str, ...] = (
    "flux",
    "flux_err",
    "mag_inst",
    "mag_cal",
    "mag_err",
    "mag_err_random",
    "mag_err_systematic",
    "local_zp_correction",
)


@dataclass(frozen=True, slots=True)
class SciencePhotometryMaterializationResult:
    calibration_version_id: int
    calibration_name: str
    base_calibration_version_id: int
    reconstructed_rows: int
    written_rows: int
    science_rows_after: int
    pruned_non_science_rows: int
    write_strategy: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_version_id": self.calibration_version_id,
            "calibration_name": self.calibration_name,
            "base_calibration_version_id": self.base_calibration_version_id,
            "reconstructed_rows": self.reconstructed_rows,
            "written_rows": self.written_rows,
            "science_rows_after": self.science_rows_after,
            "pruned_non_science_rows": self.pruned_non_science_rows,
            "write_strategy": self.write_strategy,
            "status": self.status,
        }


def materialize_science_photometry(
    session: Session,
    *,
    calibration_name: str | None = None,
    calibration_version_id: int | None = None,
    frame_ids: Iterable[int] | None = None,
    insert_batch_size: int = DEFAULT_BULK_INSERT_BATCH_SIZE,
) -> SciencePhotometryMaterializationResult:
    """Persist the narrow durable SCIENCE scalar state for one immutable product.

    The table owns no extraction geometry or full flag blobs: those are inherited
    from BASE and current normalized reconstruction.  The caller transaction is
    the publication boundary, so readers never intentionally observe a partially
    materialized product.
    """

    # Reuse the already validated PostgreSQL reconstruction compiler.  It does
    # not read science_measurements while constructing normalized SCIENCE rows;
    # the compatibility module remains available only as frozen-reference code.
    from grandgousier.photometry import science_materialization as reconstruction_sql

    target_cv = reconstruction_sql._resolve_cv(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
    )
    target_id = int(target_cv.calibration_version_id)
    _assert_durable_science_target(target_cv)
    session.execute(
        text("DELETE FROM science_photometry WHERE calibration_version_id = :cv"),
        {"cv": target_id},
    )

    if reconstruction_sql._postgres_server_reconstruction_capable(session):
        plan = reconstruction_sql._build_server_reconstruction_plan(
            session,
            target_cv=target_cv,
            frame_ids=frame_ids,
        )
        if plan is not None:
            wide_sql, params = reconstruction_sql._build_postgres_server_reconstruction_sql(plan)
            reconstruction_select = reconstruction_sql._server_reconstruction_select_sql(wide_sql)
            reconstructed_columns = ", ".join(
                f'"{name}"' for name in reconstruction_sql._SCIENCE_WRITE_COLUMNS
            )
            scalar_columns = ", ".join(SCIENCE_SCALAR_COLUMNS)
            excluded_update = ",\n                    ".join(
                f"{name} = EXCLUDED.{name}" for name in SCIENCE_SCALAR_COLUMNS
            )
            statement = f"""
                WITH reconstructed ({reconstructed_columns}) AS (
                    {reconstruction_select}
                )
                INSERT INTO science_photometry (
                    calibration_version_id,
                    base_measurement_id,
                    {scalar_columns}
                )
                SELECT
                    calibration_version_id,
                    base_measurement_id,
                    {scalar_columns}
                FROM reconstructed
                ORDER BY base_measurement_id
                ON CONFLICT (calibration_version_id, base_measurement_id)
                DO UPDATE SET
                    {excluded_update}
            """
            execution = session.execute(text(statement), params)
            science_rows = _science_row_count(session, target_id)
            written = int(execution.rowcount or 0)
            if written < 0:
                written = science_rows
            reconstruction_sql._raise_if_empty_science_reconstruction(
                session,
                base_calibration_version_id=int(plan.base_calibration_version_id),
                frame_ids=plan.frame_ids,
                reconstructed_rows=science_rows,
                calibration_name=str(plan.target_calibration_name),
            )
            if science_rows != written:
                raise RuntimeError(
                    "science_photometry row-count mismatch after server reconstruction: "
                    f"written={written}, science_rows={science_rows}"
                )
            pruned = _prune_non_science_rows_for_series(session, target_cv)
            return SciencePhotometryMaterializationResult(
                calibration_version_id=target_id,
                calibration_name=str(target_cv.name),
                base_calibration_version_id=int(plan.base_calibration_version_id),
                reconstructed_rows=science_rows,
                written_rows=written,
                science_rows_after=science_rows,
                pruned_non_science_rows=pruned,
                write_strategy="POSTGRESQL_SERVER_RECONSTRUCTION_SCIENCE",
                status="OK",
            )

    reconstructed = reconstruct_calibration_measurement_states(
        session,
        calibration_name=calibration_name,
        calibration_version_id=calibration_version_id,
        frame_ids=frame_ids,
        apply_historical_flag_overrides=False,
    )
    rows = [
        {
            "calibration_version_id": target_id,
            "base_measurement_id": int(state["base_measurement_id"]),
            **{name: state.get(name) for name in SCIENCE_SCALAR_COLUMNS},
        }
        for _, state in sorted(reconstructed.states.items())
    ]
    bind_columns = ("calibration_version_id", "base_measurement_id", *SCIENCE_SCALAR_COLUMNS)
    values_sql = ", ".join(f":{name}" for name in bind_columns)
    columns_sql = ", ".join(bind_columns)
    update_sql = ", ".join(f"{name} = EXCLUDED.{name}" for name in SCIENCE_SCALAR_COLUMNS)
    statement = text(
        f"""
        INSERT INTO science_photometry ({columns_sql})
        VALUES ({values_sql})
        ON CONFLICT (calibration_version_id, base_measurement_id)
        DO UPDATE SET {update_sql}
        """
    )
    written = 0
    for chunk in iter_sized_chunks(
        rows,
        n_columns=len(bind_columns),
        requested=int(insert_batch_size),
    ):
        session.execute(statement, list(chunk))
        written += len(chunk)

    science_rows = _science_row_count(session, target_id)
    reconstruction_sql._raise_if_empty_science_reconstruction(
        session,
        base_calibration_version_id=int(reconstructed.base_calibration_version_id),
        frame_ids=frame_ids,
        reconstructed_rows=science_rows,
        calibration_name=str(reconstructed.target_calibration_name),
    )
    if science_rows != len(rows):
        raise RuntimeError(
            "science_photometry row-count mismatch after Python reconstruction: "
            f"expected={len(rows)}, science_rows={science_rows}"
        )
    pruned = _prune_non_science_rows_for_series(session, target_cv)
    return SciencePhotometryMaterializationResult(
        calibration_version_id=target_id,
        calibration_name=str(target_cv.name),
        base_calibration_version_id=int(reconstructed.base_calibration_version_id),
        reconstructed_rows=len(rows),
        written_rows=written,
        science_rows_after=science_rows,
        pruned_non_science_rows=pruned,
        write_strategy="PYTHON_RECONSTRUCTION_SCIENCE",
        status="OK",
    )


def _assert_durable_science_target(target_cv: object) -> None:
    role = str(getattr(target_cv, "product_role", "") or "").upper()
    lifecycle = str(getattr(target_cv, "lifecycle_status", "") or "").upper()
    if role != "SCIENCE" or lifecycle != "COMPLETE_IMMUTABLE":
        raise ValueError(
            "science_photometry is durable final-state storage and may only be "
            "materialized for COMPLETE_IMMUTABLE SCIENCE products: "
            f"calibration_version_id={getattr(target_cv, 'calibration_version_id', None)}, "
            f"product_role={role or None}, lifecycle_status={lifecycle or None}"
        )


def _prune_non_science_rows_for_series(session: Session, target_cv: object) -> int:
    """Remove stale durable rows from non-SCIENCE products in this series.

    A failed older pipeline may have published durable rows before its final
    science role was established.  Those rows are always reconstructable from
    BASE + normalized terms and are not part of the 0.7 durable SCIENCE
    contract.  Scope the cleanup to the current series so unrelated products
    are never touched.
    """

    series_id = getattr(target_cv, "series_id", None)
    if series_id is None:
        return 0
    result = session.execute(
        text(
            """
            DELETE FROM science_photometry sp
            USING calibration_versions cv
            WHERE sp.calibration_version_id = cv.calibration_version_id
              AND cv.series_id = :series_id
              AND cv.calibration_version_id <> :target_cv
              AND COALESCE(UPPER(cv.product_role), '') <> 'SCIENCE'
            """
        ),
        {
            "series_id": int(series_id),
            "target_cv": int(getattr(target_cv, "calibration_version_id")),
        },
    )
    return max(0, int(result.rowcount or 0))


def complete_science_calibration_ids(
    session: Session,
    calibration_version_ids: Iterable[int],
) -> set[int]:
    """Return immutable SCIENCE products with a non-empty durable row set.

    New 0.7 products are published transactionally.  There is no legacy V1 row
    count or historical flag manifest in the clean schema; a product with no
    durable rows is therefore incomplete and falls back to normalized on-demand
    reconstruction rather than mixing representations.
    """

    ids = sorted({int(value) for value in calibration_version_ids})
    if not ids:
        return set()
    rows = session.execute(
        text(
            """
            SELECT calibration_version_id, COUNT(*)::bigint AS n
            FROM science_photometry
            WHERE calibration_version_id = ANY(:ids)
            GROUP BY calibration_version_id
            """
        ),
        {"ids": ids},
    ).mappings().all()
    return {
        int(row["calibration_version_id"])
        for row in rows
        if int(row["n"] or 0) > 0
    }


def _science_row_count(session: Session, calibration_version_id: int) -> int:
    return int(
        session.execute(
            text(
                "SELECT COUNT(*)::bigint FROM science_photometry "
                "WHERE calibration_version_id = :cv"
            ),
            {"cv": int(calibration_version_id)},
        ).scalar_one()
        or 0
    )
