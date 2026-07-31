from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grandgousier.db.bulk import iter_sized_chunks, row_column_count
from grandgousier.db.models import Measurement


def upsert_measurement_rows(session: Session, rows: list[dict], *, insert_batch_size: int | None = None) -> int:
    """Upsert measurement rows in PostgreSQL-safe chunks.

    Returns the number of executed INSERT/UPSERT batches.
    """

    if not rows:
        return 0
    table = Measurement.__table__
    n_batches = 0
    for chunk in iter_sized_chunks(rows, n_columns=row_column_count(rows), requested=insert_batch_size):
        stmt = insert(table).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_measurement_version",
            set_={
                "x": stmt.excluded.x,
                "y": stmt.excluded.y,
                "flux": stmt.excluded.flux,
                "flux_err": stmt.excluded.flux_err,
                "mag_inst": stmt.excluded.mag_inst,
                "mag_cal": stmt.excluded.mag_cal,
                "mag_err": stmt.excluded.mag_err,
                "aperture_radius_pix": stmt.excluded.aperture_radius_pix,
                "local_background": stmt.excluded.local_background,
                "local_zp_correction": stmt.excluded.local_zp_correction,
                "centroid_dx_pix": stmt.excluded.centroid_dx_pix,
                "centroid_dy_pix": stmt.excluded.centroid_dy_pix,
                "flags": stmt.excluded.flags,
            },
        )
        session.execute(stmt)
        n_batches += 1
    return n_batches
