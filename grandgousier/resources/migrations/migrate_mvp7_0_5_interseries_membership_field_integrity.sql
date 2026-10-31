-- M4 field identity: preserve orphaned membership field_id and protect reference series.
-- Managed migration: the runner keeps this SQL and its ledger row in one transaction.
DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 150000 THEN
        RAISE EXCEPTION 'mvp7_0_5_interseries_membership_field_integrity requires PostgreSQL 15 or newer';
    END IF;
END
$$;

SET LOCAL lock_timeout = '5s';
LOCK TABLE public.field_series, public.interseries_calibration_generations,
    public.interseries_calibration_series IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    expected RECORD;
    required_column RECORD;
    actual RECORD;
    old_reference_fk TEXT;
    old_generation_fk TEXT;
    old_series_fk TEXT;
BEGIN
    -- Nothing is changed until the schema and every existing row have passed preflight.
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid = 'public.interseries_calibration_series'::regclass
          AND attname = 'field_id' AND attnum > 0 AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION 'M4 membership columns differ from the expected pre-0.7.0.5 schema';
    END IF;

    FOR required_column IN
        SELECT * FROM (VALUES
            ('public.field_series'::regclass, 'series_id'),
            ('public.field_series'::regclass, 'field_id'),
            ('public.interseries_calibration_generations'::regclass, 'interseries_generation_id'),
            ('public.interseries_calibration_generations'::regclass, 'field_id'),
            ('public.interseries_calibration_generations'::regclass, 'reference_series_id'),
            ('public.interseries_calibration_series'::regclass, 'interseries_generation_id'),
            ('public.interseries_calibration_series'::regclass, 'series_id'),
            ('public.interseries_calibration_series'::regclass, 'science_calibration_version_id')
        ) AS required(table_oid, column_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_attribute a
            WHERE a.attrelid = required_column.table_oid
              AND a.attname = required_column.column_name
              AND a.attnum > 0 AND NOT a.attisdropped
              AND a.atttypid = 'pg_catalog.int8'::regtype AND a.attnotnull
        ) THEN
            RAISE EXCEPTION 'Expected non-null bigint M4 column is missing or incompatible: %.%',
                required_column.table_oid, required_column.column_name;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.conrelid IN (
            'public.field_series'::regclass,
            'public.interseries_calibration_generations'::regclass,
            'public.interseries_calibration_series'::regclass
        )
          AND c.conname IN (
              'uq_field_series_series_field', 'uq_interseries_generations_id_field',
              'fk_interseries_generations_reference_series_field',
              'fk_interseries_series_generation_field', 'fk_interseries_series_series_field'
          )
    ) THEN
        RAISE EXCEPTION 'M4 composite constraints already exist; refusing a partial schema';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.contype = 'u' AND (
            (c.conrelid = 'public.field_series'::regclass AND ARRAY(
                SELECT a.attname::text
                FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                ORDER BY k.ord
            ) = ARRAY['series_id', 'field_id']::text[])
            OR (c.conrelid = 'public.interseries_calibration_generations'::regclass
                AND ARRAY(
                    SELECT a.attname::text
                    FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                    ORDER BY k.ord
                ) = ARRAY['interseries_generation_id', 'field_id']::text[])
        )
    ) THEN
        RAISE EXCEPTION 'M4 composite parent keys already exist under another name';
    END IF;

    -- pg_constraint plus ordered pg_attribute columns, not assumed generated FK names.
    -- Count every FK touching the replaced column to reject overlapping extra FKs.
    FOR expected IN
        SELECT * FROM (VALUES
            ('reference', 'public.interseries_calibration_generations'::regclass,
                'public.field_series'::regclass, ARRAY['reference_series_id']::text[],
                ARRAY['series_id']::text[]),
            ('generation', 'public.interseries_calibration_series'::regclass,
                'public.interseries_calibration_generations'::regclass,
                ARRAY['interseries_generation_id']::text[],
                ARRAY['interseries_generation_id']::text[]),
            ('series', 'public.interseries_calibration_series'::regclass,
                'public.field_series'::regclass, ARRAY['series_id']::text[],
                ARRAY['series_id']::text[])
        ) AS targets(kind, child_table, parent_table, child_columns, parent_columns)
    LOOP
        SELECT COUNT(*) AS n, MIN(fk.conname::text) AS name,
               BOOL_AND(fk.matches_contract) AS matches_contract
        INTO actual
        FROM (
            SELECT c.conname,
                   (c.confrelid = expected.parent_table AND c.confdeltype = 'c'
                    AND c.confupdtype = 'a' AND c.confmatchtype = 's'
                    AND c.convalidated AND NOT c.condeferrable
                    AND c.conislocal AND c.coninhcount = 0
                    AND c.confdelsetcols IS NULL
                    AND child_key.columns = expected.child_columns
                    AND parent_key.columns = expected.parent_columns) AS matches_contract
            FROM pg_catalog.pg_constraint c
            CROSS JOIN LATERAL (
                SELECT array_agg(a.attname::text ORDER BY k.ord) AS columns
                FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            ) child_key
            CROSS JOIN LATERAL (
                SELECT array_agg(a.attname::text ORDER BY k.ord) AS columns
                FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = c.confrelid AND a.attnum = k.attnum
            ) parent_key
            WHERE c.contype = 'f' AND c.conrelid = expected.child_table
              AND EXISTS (
                  SELECT 1 FROM unnest(c.conkey) AS k(attnum)
                  JOIN pg_catalog.pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                  WHERE a.attname::text = ANY(expected.child_columns)
              )
        ) fk;
        IF actual.n <> 1 OR actual.matches_contract IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'Unexpected old M4 % FK; refusing to drop or replace it', expected.kind;
        END IF;
        CASE expected.kind
            WHEN 'reference' THEN old_reference_fk := actual.name;
            WHEN 'generation' THEN old_generation_fk := actual.name;
            WHEN 'series' THEN old_series_fk := actual.name;
        END CASE;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM public.interseries_calibration_generations g
        LEFT JOIN public.field_series fs ON fs.series_id = g.reference_series_id
        WHERE fs.series_id IS NULL OR fs.field_id IS DISTINCT FROM g.field_id
    ) THEN
        RAISE EXCEPTION 'M4 reference series is missing or belongs to another field';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.interseries_calibration_series s
        LEFT JOIN public.interseries_calibration_generations g
          ON g.interseries_generation_id = s.interseries_generation_id
        LEFT JOIN public.field_series fs ON fs.series_id = s.series_id
        LEFT JOIN public.calibration_versions cv
          ON cv.calibration_version_id = s.science_calibration_version_id
        WHERE g.interseries_generation_id IS NULL OR s.series_id IS NULL
           OR fs.series_id IS NULL OR cv.calibration_version_id IS NULL
           OR fs.field_id IS DISTINCT FROM g.field_id
           OR cv.field_id IS DISTINCT FROM g.field_id
           OR cv.series_id IS DISTINCT FROM s.series_id
    ) THEN
        RAISE EXCEPTION 'M4 membership has a missing parent, field mismatch, or SCIENCE CV scope mismatch';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.interseries_calibration_generations g
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS n_reference,
                   COUNT(*) FILTER (WHERE s.series_id = g.reference_series_id) AS n_matching
            FROM public.interseries_calibration_series s
            WHERE s.interseries_generation_id = g.interseries_generation_id
              AND s.is_reference_series
        ) refs ON TRUE
        WHERE refs.n_reference <> 1 OR refs.n_matching <> 1
    ) THEN
        RAISE EXCEPTION 'M4 generation does not have exactly one matching reference membership';
    END IF;

    ALTER TABLE public.interseries_calibration_series ADD COLUMN field_id BIGINT;
    UPDATE public.interseries_calibration_series AS s
    SET field_id = g.field_id
    FROM public.interseries_calibration_generations AS g
    WHERE s.interseries_generation_id = g.interseries_generation_id;
    IF EXISTS (SELECT 1 FROM public.interseries_calibration_series WHERE field_id IS NULL) THEN
        RAISE EXCEPTION 'M4 membership field_id backfill was incomplete';
    END IF;
    ALTER TABLE public.interseries_calibration_series ALTER COLUMN field_id SET NOT NULL;

    ALTER TABLE public.field_series
        ADD CONSTRAINT uq_field_series_series_field UNIQUE (series_id, field_id);
    ALTER TABLE public.interseries_calibration_generations
        ADD CONSTRAINT uq_interseries_generations_id_field
            UNIQUE (interseries_generation_id, field_id);

    EXECUTE format('ALTER TABLE public.interseries_calibration_generations DROP CONSTRAINT %I',
                   old_reference_fk);
    EXECUTE format('ALTER TABLE public.interseries_calibration_series DROP CONSTRAINT %I',
                   old_generation_fk);
    EXECUTE format('ALTER TABLE public.interseries_calibration_series DROP CONSTRAINT %I',
                   old_series_fk);
    ALTER TABLE public.interseries_calibration_series ALTER COLUMN series_id DROP NOT NULL;

    ALTER TABLE public.interseries_calibration_generations
        ADD CONSTRAINT fk_interseries_generations_reference_series_field
        FOREIGN KEY (reference_series_id, field_id)
        REFERENCES public.field_series (series_id, field_id) ON DELETE RESTRICT;
    ALTER TABLE public.interseries_calibration_series
        ADD CONSTRAINT fk_interseries_series_generation_field
        FOREIGN KEY (interseries_generation_id, field_id)
        REFERENCES public.interseries_calibration_generations (interseries_generation_id, field_id)
        ON DELETE CASCADE;
    ALTER TABLE public.interseries_calibration_series
        ADD CONSTRAINT fk_interseries_series_series_field
        FOREIGN KEY (series_id, field_id)
        REFERENCES public.field_series (series_id, field_id) ON DELETE SET NULL (series_id);
END
$$;
