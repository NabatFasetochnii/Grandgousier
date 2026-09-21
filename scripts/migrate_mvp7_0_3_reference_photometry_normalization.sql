-- Grandgousier 0.7.0-D normalized reference-photometry marker.
--
-- Phase D is fresh-baseline-only. Fresh init-db creates the normalized layout
-- directly from ORM metadata and baselines this marker. Existing databases with
-- the wide reference_photometry table are intentionally not rewritten in place.
DO $$
BEGIN
    IF to_regclass('public.reference_photometry') IS NOT NULL THEN
        RAISE EXCEPTION
            'mvp7_0_3_reference_photometry_normalization refuses in-place conversion of wide reference_photometry; use a fresh database';
    END IF;

    IF to_regclass('public.photometric_bands') IS NULL
       OR to_regclass('public.reference_photometry_sources') IS NULL
       OR to_regclass('public.reference_photometry_points') IS NULL THEN
        RAISE EXCEPTION
            'mvp7_0_3_reference_photometry_normalization requires a fresh 0.7.0-D schema; run init-db on a separate empty database';
    END IF;
END
$$;

SELECT 1;
