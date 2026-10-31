-- Grandgousier 0.7.0.4: make the approved nullable identity keys NULLS NOT DISTINCT.
-- Duplicate NULL-inclusive identity groups must be resolved manually before this migration.
DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 150000 THEN
        RAISE EXCEPTION
            'mvp7_0_4_null_identity_constraints requires PostgreSQL 15 or newer';
    END IF;
END
$$;

LOCK TABLE
    public.event_frame_coverage,
    public.instrument_units,
    public.object_qc
IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    duplicate_instrument_units BOOLEAN;
    duplicate_object_qc BOOLEAN;
    duplicate_event_frame_coverage BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM public.instrument_units
        GROUP BY instrument, site, tube, camera, filter_name, observing_mode
        HAVING COUNT(*) > 1
    ) INTO duplicate_instrument_units;

    SELECT EXISTS (
        SELECT 1
        FROM public.object_qc
        GROUP BY object_id, catalog_name, instrument_unit_id, calibration_version_id, algorithm
        HAVING COUNT(*) > 1
    ) INTO duplicate_object_qc;

    SELECT EXISTS (
        SELECT 1
        FROM public.event_frame_coverage
        GROUP BY event_id, frame_id, object_id, calibration_version_id
        HAVING COUNT(*) > 1
    ) INTO duplicate_event_frame_coverage;

    IF duplicate_instrument_units OR duplicate_object_qc OR duplicate_event_frame_coverage THEN
        RAISE EXCEPTION
            'mvp7_0_4_null_identity_constraints found duplicate NULL-inclusive identity groups '
            '(instrument_units=%, object_qc=%, event_frame_coverage=%); resolve duplicate rows '
            'manually and rerun; this migration does not modify data',
            duplicate_instrument_units,
            duplicate_object_qc,
            duplicate_event_frame_coverage;
    END IF;
END
$$;

ALTER TABLE public.instrument_units
    DROP CONSTRAINT uq_instrument_units_identity,
    ADD CONSTRAINT uq_instrument_units_identity
        UNIQUE NULLS NOT DISTINCT (instrument, site, tube, camera, filter_name, observing_mode);

ALTER TABLE public.object_qc
    DROP CONSTRAINT uq_object_qc_identity,
    ADD CONSTRAINT uq_object_qc_identity
        UNIQUE NULLS NOT DISTINCT
        (object_id, catalog_name, instrument_unit_id, calibration_version_id, algorithm);

ALTER TABLE public.event_frame_coverage
    DROP CONSTRAINT uq_event_frame_coverage,
    ADD CONSTRAINT uq_event_frame_coverage
        UNIQUE NULLS NOT DISTINCT (event_id, frame_id, object_id, calibration_version_id);
