from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class DetectionMatchCleanupResult:
    duplicate_groups: int = 0
    duplicate_rows_deleted: int = 0
    orphan_matches_deleted: int = 0
    catalog_name: str | None = None
    instrument_unit_id: int | None = None
    algorithm: str | None = None
    dry_run: bool = False


def cleanup_duplicate_detection_matches(
    session: Session,
    *,
    catalog_name: str | None = None,
    instrument_unit_id: int | None = None,
    algorithm: str | None = "nearest_sky_v1",
    delete_orphans: bool = True,
    dry_run: bool = False,
) -> DetectionMatchCleanupResult:
    """Remove duplicate match evidence across repeated match runs.

    Clean 0.7 storage derives frame/catalog/algorithm identity through the run and
    catalog foreign keys.  The canonical logical grain remains one accepted catalog
    source per frame/catalog/matching algorithm.  Source-detection FK orphans are
    impossible in a valid clean schema (the FK cascades), but the defensive audit is
    retained for diagnostics.
    """

    result = DetectionMatchCleanupResult(
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
        dry_run=dry_run,
    )
    filters = ["dr.run_kind = 'CATALOG_MATCH'"]
    params: dict[str, object] = {}
    if catalog_name:
        filters.append("cs.catalog_name = :catalog_name")
        params["catalog_name"] = catalog_name
    if algorithm:
        filters.append("dr.algorithm = :algorithm")
        params["algorithm"] = algorithm
    if instrument_unit_id is not None:
        filters.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = instrument_unit_id
    where_sql = " AND ".join(filters)

    joins = """
        JOIN detection_run_frames drf ON drf.detection_run_frame_id = dcm.detection_run_frame_id
        JOIN detection_runs dr ON dr.detection_run_id = drf.detection_run_id
        JOIN frames f ON f.frame_id = drf.frame_id
        JOIN catalog_sources cs ON cs.catalog_source_id = dcm.catalog_source_id
    """

    duplicate_groups_sql = f"""
        WITH grouped AS (
            SELECT drf.frame_id, dcm.catalog_source_id, cs.catalog_name, dr.algorithm, COUNT(*) AS n
            FROM detection_catalog_matches dcm
            {joins}
            WHERE {where_sql}
            GROUP BY drf.frame_id, dcm.catalog_source_id, cs.catalog_name, dr.algorithm
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*) FROM grouped
    """
    result.duplicate_groups = int(session.execute(text(duplicate_groups_sql), params).scalar() or 0)

    ranked_cte = f"""
        WITH ranked AS (
            SELECT dcm.detection_catalog_match_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY drf.frame_id, dcm.catalog_source_id, cs.catalog_name, dr.algorithm
                       ORDER BY dcm.sep_arcsec ASC NULLS LAST,
                                dcm.updated_at DESC NULLS LAST,
                                dcm.detection_catalog_match_id DESC
                   ) AS rn
            FROM detection_catalog_matches dcm
            {joins}
            WHERE {where_sql}
        )
    """
    result.duplicate_rows_deleted = int(
        session.execute(
            text(ranked_cte + " SELECT COUNT(*) FROM ranked WHERE rn > 1"), params
        ).scalar()
        or 0
    )

    if delete_orphans:
        orphan_sql = f"""
            SELECT COUNT(*)
            FROM detection_catalog_matches dcm
            {joins}
            LEFT JOIN source_detections sd ON sd.source_detection_id = dcm.source_detection_id
            WHERE {where_sql} AND sd.source_detection_id IS NULL
        """
        result.orphan_matches_deleted = int(session.execute(text(orphan_sql), params).scalar() or 0)

    if dry_run:
        return result

    if delete_orphans and result.orphan_matches_deleted:
        orphan_delete_sql = f"""
            DELETE FROM detection_catalog_matches dcm
            WHERE dcm.detection_catalog_match_id IN (
                SELECT dcm2.detection_catalog_match_id
                FROM detection_catalog_matches dcm2
                JOIN detection_run_frames drf ON drf.detection_run_frame_id = dcm2.detection_run_frame_id
                JOIN detection_runs dr ON dr.detection_run_id = drf.detection_run_id
                JOIN frames f ON f.frame_id = drf.frame_id
                JOIN catalog_sources cs ON cs.catalog_source_id = dcm2.catalog_source_id
                LEFT JOIN source_detections sd ON sd.source_detection_id = dcm2.source_detection_id
                WHERE {where_sql.replace('dcm.', 'dcm2.')} AND sd.source_detection_id IS NULL
            )
        """
        session.execute(text(orphan_delete_sql), params)

    if result.duplicate_rows_deleted:
        duplicate_delete_sql = ranked_cte + """
            DELETE FROM detection_catalog_matches dcm
            USING ranked r
            WHERE dcm.detection_catalog_match_id = r.detection_catalog_match_id
              AND r.rn > 1
        """
        session.execute(text(duplicate_delete_sql), params)

    session.commit()
    return result
