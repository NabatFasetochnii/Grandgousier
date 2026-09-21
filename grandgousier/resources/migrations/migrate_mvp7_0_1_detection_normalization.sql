-- Grandgousier 0.7.0-B clean-schema marker.
--
-- Detection normalization is part of the 0.7 fresh-baseline epoch.  This file
-- deliberately does not transform a 0.7.0-A (or 0.6) database in place.  Fresh
-- init-db creates the ORM schema first and baselines this marker.  If migrate-db
-- encounters the marker on a database whose clean B layout is not already
-- present, fail before recording a false APPLIED ledger row.
DO $$
BEGIN
    IF to_regclass('public.detection_runs') IS NULL
       OR to_regclass('public.detection_run_frames') IS NULL THEN
        RAISE EXCEPTION
            'mvp7_0_1_detection_normalization requires a fresh 0.7.0-B schema; run init-db on a separate empty database';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'source_detections'
          AND column_name IN ('frame_id', 'algorithm', 'parameters')
    ) OR EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'detection_catalog_matches'
          AND column_name IN ('frame_id', 'catalog_name', 'algorithm', 'details')
    ) THEN
        RAISE EXCEPTION
            'mvp7_0_1_detection_normalization refuses in-place conversion of legacy detection rows; use a fresh database';
    END IF;
END
$$;

SELECT 1;
