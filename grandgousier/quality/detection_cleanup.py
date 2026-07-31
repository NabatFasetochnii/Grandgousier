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
    """Remove stale duplicate detection-catalog matches.

    The canonical grain of `detection_catalog_matches` is now one row per
    (frame_id, catalog_source_id, catalog_name, algorithm). Repeated historical
    runs could leave multiple rows for the same frame/source when source
    detections were regenerated with new IDs. This cleanup keeps the best row
    per group, preferring the smallest separation and then the newest row.
    """

    result = DetectionMatchCleanupResult(
        catalog_name=catalog_name,
        instrument_unit_id=instrument_unit_id,
        algorithm=algorithm,
        dry_run=dry_run,
    )

    filters = []
    params: dict[str, object] = {}
    if catalog_name:
        filters.append("dcm.catalog_name = :catalog_name")
        params["catalog_name"] = catalog_name
    if algorithm:
        filters.append("dcm.algorithm = :algorithm")
        params["algorithm"] = algorithm
    if instrument_unit_id is not None:
        filters.append("f.instrument_unit_id = :instrument_unit_id")
        params["instrument_unit_id"] = instrument_unit_id

    where_sql = ""
    if filters:
        where_sql = "WHERE " + " AND ".join(filters)

    duplicate_groups_sql = f"""
        WITH grouped AS (
            SELECT dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm, COUNT(*) AS n
            FROM detection_catalog_matches dcm
            JOIN frames f ON f.frame_id = dcm.frame_id
            {where_sql}
            GROUP BY dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm
            HAVING COUNT(*) > 1
        )
        SELECT COUNT(*) FROM grouped
    """
    result.duplicate_groups = int(session.execute(text(duplicate_groups_sql), params).scalar() or 0)

    orphan_filter_sql = ""
    orphan_params = dict(params)
    # The base alias is different in the orphan query.
    orphan_filters = []
    if catalog_name:
        orphan_filters.append("dcm.catalog_name = :catalog_name")
    if algorithm:
        orphan_filters.append("dcm.algorithm = :algorithm")
    if instrument_unit_id is not None:
        orphan_filters.append("f.instrument_unit_id = :instrument_unit_id")
    if orphan_filters:
        orphan_filter_sql = " AND " + " AND ".join(orphan_filters)

    if dry_run:
        if delete_orphans:
            orphan_count_sql = f"""
                SELECT COUNT(*)
                FROM detection_catalog_matches dcm
                JOIN frames f ON f.frame_id = dcm.frame_id
                LEFT JOIN source_detections sd ON sd.source_detection_id = dcm.source_detection_id
                WHERE sd.source_detection_id IS NULL
                {orphan_filter_sql}
            """
            result.orphan_matches_deleted = int(session.execute(text(orphan_count_sql), orphan_params).scalar() or 0)
        duplicate_rows_sql = f"""
            WITH ranked AS (
                SELECT dcm.detection_catalog_match_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm
                           ORDER BY dcm.sep_arcsec ASC NULLS LAST,
                                    dcm.updated_at DESC NULLS LAST,
                                    dcm.detection_catalog_match_id DESC
                       ) AS rn
                FROM detection_catalog_matches dcm
                JOIN frames f ON f.frame_id = dcm.frame_id
                {where_sql}
            )
            SELECT COUNT(*) FROM ranked WHERE rn > 1
        """
        result.duplicate_rows_deleted = int(session.execute(text(duplicate_rows_sql), params).scalar() or 0)
        return result

    if delete_orphans:
        orphan_delete_sql = f"""
            DELETE FROM detection_catalog_matches dcm
            USING frames f
            WHERE f.frame_id = dcm.frame_id
              AND NOT EXISTS (
                  SELECT 1 FROM source_detections sd WHERE sd.source_detection_id = dcm.source_detection_id
              )
              {orphan_filter_sql}
            RETURNING dcm.detection_catalog_match_id
        """
        result.orphan_matches_deleted = len(session.execute(text(orphan_delete_sql), orphan_params).all())

    duplicate_delete_sql = f"""
        WITH ranked AS (
            SELECT dcm.detection_catalog_match_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY dcm.frame_id, dcm.catalog_source_id, dcm.catalog_name, dcm.algorithm
                       ORDER BY dcm.sep_arcsec ASC NULLS LAST,
                                dcm.updated_at DESC NULLS LAST,
                                dcm.detection_catalog_match_id DESC
                   ) AS rn
            FROM detection_catalog_matches dcm
            JOIN frames f ON f.frame_id = dcm.frame_id
            {where_sql}
        )
        DELETE FROM detection_catalog_matches dcm
        USING ranked r
        WHERE dcm.detection_catalog_match_id = r.detection_catalog_match_id
          AND r.rn > 1
        RETURNING dcm.detection_catalog_match_id
    """
    result.duplicate_rows_deleted = len(session.execute(text(duplicate_delete_sql), params).all())
    session.commit()
    return result
