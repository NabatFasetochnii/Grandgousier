-- Grandgousier 0.7.0-C identities/locations clean-schema marker.
--
-- Phase C is fresh-baseline-only.  Fresh init-db creates the normalized layout
-- directly from ORM metadata and baselines this marker.  Existing 0.7.0/0.7.0.1
-- databases are intentionally not rewritten in place.
DO $$
BEGIN
    IF to_regclass('public.object_catalog_links') IS NULL
       OR to_regclass('public.frame_locations') IS NULL THEN
        RAISE EXCEPTION
            'mvp7_0_2_identity_locations requires a fresh 0.7.0-C schema; run init-db on a separate empty database';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'frames'
          AND column_name IN ('raw_path', 'file_size_bytes', 'file_mtime_utc')
    ) OR EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'objects'
          AND column_name IN ('gaia_dr3_id', 'external_ids')
    ) THEN
        RAISE EXCEPTION
            'mvp7_0_2_identity_locations refuses in-place conversion of pre-C identity/location columns; use a fresh database';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'objects' AND column_name = 'identity_scheme'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'objects' AND column_name = 'reference_epoch'
    ) THEN
        RAISE EXCEPTION
            'mvp7_0_2_identity_locations object identity columns are incomplete';
    END IF;
END
$$;

SELECT 1;
