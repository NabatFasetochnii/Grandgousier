-- M4: keep every persisted edge endpoint's field_series parent from deletion.
-- The managed runner applies this SQL and its ledger row in one transaction.
DO $$
BEGIN
    IF current_setting('server_version_num')::integer < 150000 THEN
        RAISE EXCEPTION 'mvp7_0_6_interseries_edge_delete_restrict requires PostgreSQL 15 or newer';
    END IF;
END
$$;

SET LOCAL lock_timeout = '5s';
-- Keep the field-series parent and edge child stable while catalog and all-row
-- preflight run. The generation lock also stabilizes the field-scope check.
LOCK TABLE public.field_series, public.interseries_calibration_generations,
    public.interseries_calibration_edges IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    required_column RECORD;
    expected_fk RECORD;
    expected_key RECORD;
    actual RECORD;
    old_left_fk TEXT;
    old_right_fk TEXT;
    old_generation_fk TEXT;
    edge_cap BIGINT;
    invalid_edge_rows BIGINT;
BEGIN
    -- Validate the exact source columns before interpreting existing rows.
    FOR required_column IN
        SELECT * FROM (VALUES
            ('public.field_series'::regclass, 'series_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.field_series'::regclass, 'field_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_generations'::regclass,
                'interseries_generation_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_generations'::regclass,
                'field_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_edges'::regclass,
                'interseries_generation_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_edges'::regclass,
                'left_series_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_edges'::regclass,
                'right_series_id', 'pg_catalog.int8'::regtype, TRUE),
            ('public.interseries_calibration_edges'::regclass,
                'pair_status', 'pg_catalog.varchar'::regtype, TRUE)
        ) AS required(table_oid, column_name, expected_type, expected_not_null)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS a
            WHERE a.attrelid = required_column.table_oid
              AND a.attname = required_column.column_name
              AND a.attnum > 0 AND NOT a.attisdropped
              AND a.atttypid = required_column.expected_type
              AND a.attnotnull = required_column.expected_not_null
        ) THEN
            RAISE EXCEPTION 'Unexpected M4 column shape: %.%',
                required_column.table_oid, required_column.column_name;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint AS c
        WHERE c.conrelid = 'public.interseries_calibration_edges'::regclass
          AND c.conname IN (
              'fk_interseries_edges_left_series_restrict',
              'fk_interseries_edges_right_series_restrict'
          )
    ) THEN
        RAISE EXCEPTION 'M4 endpoint target constraint name already exists';
    END IF;

    -- Check the series primary key, existing M4 composite keys, and edge-pair key
    -- structurally. Ordered child columns are material to the contract.
    SELECT COUNT(*) AS n, BOOL_AND(c.convalidated AND NOT c.condeferrable
                                   AND c.conislocal AND c.coninhcount = 0
                                   AND i.indisvalid AND i.indisready) AS valid
    INTO actual
    FROM pg_catalog.pg_constraint AS c
    JOIN pg_catalog.pg_index AS i ON i.indexrelid = c.conindid
    CROSS JOIN LATERAL (
        SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
        FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_catalog.pg_attribute AS a
          ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    ) AS key_columns
    WHERE c.contype = 'p'
      AND c.conrelid = 'public.field_series'::regclass
      AND key_columns.columns = ARRAY['series_id']::text[];
    IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Expected field_series(series_id) primary key is missing or invalid';
    END IF;

    FOR expected_key IN
        SELECT * FROM (VALUES
            ('public.field_series'::regclass, 'uq_field_series_series_field',
                ARRAY['series_id', 'field_id']::text[]),
            ('public.interseries_calibration_generations'::regclass,
                'uq_interseries_generations_id_field',
                ARRAY['interseries_generation_id', 'field_id']::text[]),
            ('public.interseries_calibration_edges'::regclass,
                'uq_interseries_calibration_edges_generation_pair',
                ARRAY['interseries_generation_id', 'left_series_id',
                      'right_series_id']::text[])
        ) AS expected(table_oid, constraint_name, columns)
    LOOP
        SELECT COUNT(*) AS n, MIN(c.conname::text) AS name,
               BOOL_AND(c.convalidated AND NOT c.condeferrable
                        AND c.conislocal AND c.coninhcount = 0
                        AND i.indisvalid AND i.indisready) AS valid
        INTO actual
        FROM pg_catalog.pg_constraint AS c
        JOIN pg_catalog.pg_index AS i ON i.indexrelid = c.conindid
        CROSS JOIN LATERAL (
            SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
            FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        ) AS key_columns
        WHERE c.contype = 'u'
          AND c.conrelid = expected_key.table_oid
          AND key_columns.columns = expected_key.columns;
        IF actual.n <> 1 OR actual.name <> expected_key.constraint_name
           OR actual.valid IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'Expected M4 unique key is missing, duplicated, or invalid: %',
                expected_key.constraint_name;
        END IF;
    END LOOP;

    SELECT COUNT(*) AS n, BOOL_AND(
               c.convalidated
               AND replace(replace(replace(replace(
                   lower(pg_catalog.pg_get_expr(c.conbin, c.conrelid)),
                   ' ', ''), '(', ''), ')', ''), '"', '')
                   = 'left_series_id<right_series_id'
           ) AS valid
    INTO actual
    FROM pg_catalog.pg_constraint AS c
    WHERE c.contype = 'c'
      AND c.conrelid = 'public.interseries_calibration_edges'::regclass
      AND c.conname = 'ck_interseries_calibration_edges_series_order';
    IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Expected validated M4 edge ordering check is missing or incorrect';
    END IF;

    -- Discover the existing names by ordered FK shape, never by generated-name guess.
    FOR expected_fk IN
        SELECT * FROM (VALUES
            ('left', 'left_series_id', 'public.field_series'::regclass,
                ARRAY['left_series_id']::text[], ARRAY['series_id']::text[], 'c'),
            ('right', 'right_series_id', 'public.field_series'::regclass,
                ARRAY['right_series_id']::text[], ARRAY['series_id']::text[], 'c'),
            ('generation', 'interseries_generation_id',
                'public.interseries_calibration_generations'::regclass,
                ARRAY['interseries_generation_id']::text[],
                ARRAY['interseries_generation_id']::text[], 'c')
        ) AS expected(kind, touched_column, parent_table,
                      child_columns, parent_columns, delete_action)
    LOOP
        SELECT COUNT(*) AS n, MIN(fk.conname::text) AS name,
               BOOL_AND(fk.matches_contract) AS matches_contract
        INTO actual
        FROM (
            SELECT c.conname,
                   (c.confrelid = expected_fk.parent_table
                    AND c.confdeltype = expected_fk.delete_action
                    AND c.confupdtype = 'a' AND c.confmatchtype = 's'
                    AND c.convalidated AND NOT c.condeferrable AND NOT c.condeferred
                    AND c.conislocal AND c.coninhcount = 0
                    AND c.confdelsetcols IS NULL
                    AND child_key.columns = expected_fk.child_columns
                    AND parent_key.columns = expected_fk.parent_columns) AS matches_contract
            FROM pg_catalog.pg_constraint AS c
            CROSS JOIN LATERAL (
                SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
                FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute AS a
                  ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            ) AS child_key
            CROSS JOIN LATERAL (
                SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
                FROM UNNEST(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_catalog.pg_attribute AS a
                  ON a.attrelid = c.confrelid AND a.attnum = k.attnum
            ) AS parent_key
            WHERE c.contype = 'f'
              AND c.conrelid = 'public.interseries_calibration_edges'::regclass
              AND EXISTS (
                  SELECT 1
                  FROM UNNEST(c.conkey) AS k(attnum)
                  JOIN pg_catalog.pg_attribute AS a
                    ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                  WHERE a.attname::text = expected_fk.touched_column
              )
        ) AS fk;
        IF actual.n <> 1 OR actual.matches_contract IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'Unexpected or competing M4 edge % FK', expected_fk.kind;
        END IF;
        CASE expected_fk.kind
            WHEN 'left' THEN old_left_fk := actual.name;
            WHEN 'right' THEN old_right_fk := actual.name;
            WHEN 'generation' THEN old_generation_fk := actual.name;
        END CASE;
    END LOOP;

    IF old_left_fk = old_right_fk OR old_left_fk IS NULL OR old_right_fk IS NULL
       OR old_generation_fk IS NULL THEN
        RAISE EXCEPTION 'M4 edge FK names are missing or overlap';
    END IF;
    IF (SELECT COUNT(*) FROM pg_catalog.pg_constraint AS c
        WHERE c.contype = 'f'
          AND c.conrelid = 'public.interseries_calibration_edges'::regclass) <> 3 THEN
        RAISE EXCEPTION 'Unexpected additional M4 edge foreign key';
    END IF;

    -- Bound the all-status, all-generation integrity scan; never filter pair_status.
    SELECT COUNT(*) INTO edge_cap
    FROM (SELECT 1 FROM public.interseries_calibration_edges LIMIT 10001) AS bounded_edges;
    IF edge_cap > 10000 THEN
        RAISE EXCEPTION 'M4 edge row cap exceeded; re-scope before migration';
    END IF;

    SELECT COUNT(*) FILTER (
        WHERE e.interseries_generation_id IS NULL
           OR g.interseries_generation_id IS NULL
           OR e.left_series_id IS NULL OR e.right_series_id IS NULL
           OR left_series.series_id IS NULL OR right_series.series_id IS NULL
           OR left_series.field_id IS DISTINCT FROM g.field_id
           OR right_series.field_id IS DISTINCT FROM g.field_id
           OR e.left_series_id >= e.right_series_id
    ) INTO invalid_edge_rows
    FROM public.interseries_calibration_edges AS e
    LEFT JOIN public.interseries_calibration_generations AS g
      ON g.interseries_generation_id = e.interseries_generation_id
    LEFT JOIN public.field_series AS left_series
      ON left_series.series_id = e.left_series_id
    LEFT JOIN public.field_series AS right_series
      ON right_series.series_id = e.right_series_id;
    IF invalid_edge_rows <> 0 THEN
        RAISE EXCEPTION 'M4 edge endpoint or field-scope preflight failed';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.interseries_calibration_edges
        GROUP BY interseries_generation_id, left_series_id, right_series_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'Duplicate M4 generation edge pair found';
    END IF;

    -- Drop only the two catalog-discovered endpoint constraints.
    EXECUTE format(
        'ALTER TABLE public.interseries_calibration_edges DROP CONSTRAINT %I',
        old_left_fk
    );
    EXECUTE format(
        'ALTER TABLE public.interseries_calibration_edges DROP CONSTRAINT %I',
        old_right_fk
    );

    ALTER TABLE public.interseries_calibration_edges
        ADD CONSTRAINT fk_interseries_edges_left_series_restrict
        FOREIGN KEY (left_series_id)
        REFERENCES public.field_series (series_id)
        ON DELETE RESTRICT NOT DEFERRABLE;
    ALTER TABLE public.interseries_calibration_edges
        ADD CONSTRAINT fk_interseries_edges_right_series_restrict
        FOREIGN KEY (right_series_id)
        REFERENCES public.field_series (series_id)
        ON DELETE RESTRICT NOT DEFERRABLE;
END
$$;
