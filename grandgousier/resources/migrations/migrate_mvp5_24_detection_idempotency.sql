-- MVP5.24: make detection-catalog matching idempotent.
--
-- Earlier versions allowed multiple rows for the same
-- (frame_id, catalog_source_id, catalog_name, algorithm) when source detections
-- were regenerated and matched again. Keep the best row by separation and add a
-- unique constraint at the correct grain.

WITH ranked AS (
    SELECT
        detection_catalog_match_id,
        ROW_NUMBER() OVER (
            PARTITION BY frame_id, catalog_source_id, catalog_name, algorithm
            ORDER BY sep_arcsec ASC NULLS LAST,
                     updated_at DESC NULLS LAST,
                     detection_catalog_match_id DESC
        ) AS rn
    FROM detection_catalog_matches
)
DELETE FROM detection_catalog_matches dcm
USING ranked r
WHERE dcm.detection_catalog_match_id = r.detection_catalog_match_id
  AND r.rn > 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_detection_catalog_matches_frame_source_catalog_algorithm'
    ) THEN
        ALTER TABLE detection_catalog_matches
            ADD CONSTRAINT uq_detection_catalog_matches_frame_source_catalog_algorithm
            UNIQUE (frame_id, catalog_source_id, catalog_name, algorithm);
    END IF;
END $$;
