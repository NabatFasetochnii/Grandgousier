-- A2/B: reference membership and source SCIENCE CV field/series scope.
-- Managed runner owns the SQL + ledger transaction. No live application authorized here.
-- Timeouts apply per statement, not to total lock hold or index-build time.
SET LOCAL statement_timeout = '30s';
SET LOCAL lock_timeout = '5s';
LOCK TABLE public.calibration_versions IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.field_series IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.interseries_calibration_generations IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.interseries_calibration_series IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.interseries_calibration_edges IN ACCESS EXCLUSIVE MODE;
LOCK TABLE public.schema_migrations IN SHARE MODE;

DO $$
DECLARE
    required RECORD;
    expected_fk RECORD;
    actual RECORD;
    bad_rows BIGINT;
BEGIN
    IF current_setting('server_version_num')::integer < 150000 THEN
        RAISE EXCEPTION 'A2/B requires PostgreSQL 15 or newer';
    END IF;
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'A2/B requires READ COMMITTED for locked ledger recheck';
    END IF;

    -- Exact predecessor resource-byte manifest (runner canonicalizes only CRLF to LF).
    IF EXISTS (
        WITH expected(migration_id, filename, checksum_sha256) AS (VALUES
            ('mvp1_1_instrument_units', 'migrate_mvp1_1_instrument_units.sql', 'ed3dbd763b307e9362f574a733f8ce6ef9b12640af43912011d45ad70b22a886'),
            ('mvp3_catalogs', 'migrate_mvp3_catalogs.sql', '728834520708794753e4aecb7a2feaa022d3381afd88bbc9da14fe3f87d6b2ce'),
            ('mvp5_detection_catalog', 'migrate_mvp5_detection_catalog.sql', '3cf2cb4c54ae563cae0affa4f8b669ba9bf8549f965ce092eeb8d4fe7e9efd04'),
            ('mvp5_1_detection_catalog_qc', 'migrate_mvp5_1_detection_catalog_qc.sql', '194e5f75d9ccd1654b6d33c5b7159b4dfb3787a441d016b156b8b2745d1c30c1'),
            ('mvp5_6_object_qc', 'migrate_mvp5_6_object_qc.sql', 'fbf56b70a29546abe7139d1218f6c0c559df08a9ec38103c2f5cc7dda319cc7c'),
            ('mvp5_16_event_registry', 'migrate_mvp5_16_event_registry.sql', '39c2798b0539679a90de9bd838069b407c6a4813489c6436f1c4d40addfc2af4'),
            ('mvp5_20_event_detection_links', 'migrate_mvp5_20_event_detection_links.sql', 'a6b03941805d83c440b41d5872ce52c399a030f59dfb672b8dbc4ab492c5d3ad'),
            ('mvp5_21_pipeline_runs', 'migrate_mvp5_21_pipeline_runs.sql', 'd0650645d2640c35f7d4c6de337bd0bb6f807ab47b3a9e233d0aa42261b42216'),
            ('mvp5_24_detection_idempotency', 'migrate_mvp5_24_detection_idempotency.sql', 'b06f42cf33536c1a09769ed5542151213d8c13126f37cc4500bf850e76cbec28'),
            ('mvp5_25_pipeline_logging', 'migrate_mvp5_25_pipeline_logging.sql', '7bafc344fcfe756513a056aa9829eb75dbadcec2c7e0d85e9be3385f09c86a38'),
            ('mvp5_26_fields_series', 'migrate_mvp5_26_fields_series.sql', '2feeed5c9975139f05c3cf0a3da6a878cb1117f6495c60ebe232419fcc83006a'),
            ('mvp5_28_reference_photometry', 'migrate_mvp5_28_reference_photometry.sql', '052e7a69b2fdc115fc2653976552afa157d94d6f3bc042e41be44c8ffb25a3e5'),
            ('mvp5_30_5_calibration_provenance', 'migrate_mvp5_30_5_calibration_provenance.sql', 'f2c0e619c03592dfb0d90c5f83f1826627da3ccb243dfdb83d28a4909375fff7'),
            ('mvp5_31_0_lightcurve_product', 'migrate_mvp5_31_0_lightcurve_product.sql', '6fa97933aea408ea496dd1115ada355cba32d44ca4a917af8e7c32ad485d9606'),
            ('mvp5_34_2_measurement_error_components', 'migrate_mvp5_34_2_measurement_error_components.sql', '74bb3cb4f47c3eaee968c80029d981a4a459e93742c1bfa490b450099529cec3'),
            ('mvp5_38_3_measurement_calibration_index', 'migrate_mvp5_38_3_measurement_calibration_index.sql', 'd71fc6f587a848eabc7e7248fd6eb24869daf6406de5fb947a27a2ae4b47b7d0'),
            ('mvp5_39_1_schema_migration_ledger', 'migrate_mvp5_39_1_schema_migration_ledger.sql', '0fd4ea8a1c35c1f9d19e36294bf1004cab812d7e57b8b6975eb31b80581e8e56'),
            ('mvp5_39_2_calibration_product_provenance', 'migrate_mvp5_39_2_calibration_product_provenance.sql', '0779535494ac959b0d4c642d1f5515e4faa91ea89de62e13dab1939a0ef13358'),
            ('mvp5_39_5_calibration_immutable_generations', 'migrate_mvp5_39_5_calibration_immutable_generations.sql', 'a87a1af3e39f81ffe5a752f369bc8a3a01c3b5778f3ee0393eba0b9c5347d5c1'),
            ('mvp5_39_6_artifact_registry', 'migrate_mvp5_39_6_artifact_registry.sql', '5565fbc757ed9d1bc8bbb028c8fefceec035d38475ea4027e02c8ff151279f1d'),
            ('mvp5_39_7_batch_orchestration', 'migrate_mvp5_39_7_batch_orchestration.sql', 'adce349efe41d8f7738d1c059409592aa17c429d2cf2e2c22a2c28e2010fe68f'),
            ('mvp5_40_0_normalized_calibration_terms', 'migrate_mvp5_40_0_normalized_calibration_terms.sql', '04e14244a492e5a2e5c0a5b56c2bbd584ba8e03e83a2aee71300687ea80d1576'),
            ('mvp5_40_2_science_measurements', 'migrate_mvp5_40_2_science_measurements.sql', '23fe79b3396eb6828a45fefe1ef710a010868e681645b429be56c4d136934bd2'),
            ('mvp5_40_13_science_measurement_index_cleanup', 'migrate_mvp5_40_13_science_measurement_index_cleanup.sql', '9856c4725f56973123097379031038128c8a65d8c04ec574f3a859a16bc61066'),
            ('mvp5_46_0_interseries_calibration_generations', 'migrate_mvp5_46_0_interseries_calibration_generations.sql', 'd504a27b6200f79af88218de65ecbcd472762a6a8a033693ff053efda85ee529'),
            ('mvp5_52_0_observation_identity', 'migrate_mvp5_52_0_observation_identity.sql', '5497cbad957f36d20523a50b9bfd23cf256cc07cf16333a0a2d83c6a746f6a6c'),
            ('mvp6_4_0_science_photometry_cache', 'migrate_mvp6_4_0_science_photometry_cache.sql', 'd0be844944ddc9f6471c4733a411b4391173317f1f7cd793c88230918ea3bfb7'),
            ('mvp6_4_3_science_flag_overrides', 'migrate_mvp6_4_3_science_flag_overrides.sql', '35a39adc09445ba0391069c60077084dff569a4db7e680737925fa0084f6c84f'),
            ('mvp7_0_0_clean_schema_epoch', 'migrate_mvp7_0_0_clean_schema_epoch.sql', 'f22bfece467118915efe2496d6c528a03d6f620d5a2ceb9e0a5f80e02e79bd7d'),
            ('mvp7_0_1_detection_normalization', 'migrate_mvp7_0_1_detection_normalization.sql', '8c564296df0af88c3355adf17f401c7a4a4faaca89d486930d19aa22cea196fd'),
            ('mvp7_0_2_identity_locations', 'migrate_mvp7_0_2_identity_locations.sql', '7fe028e6030443188b1f0acbd0e5b788633f96f2665c3e1cf6f8f95fdf469144'),
            ('mvp7_0_3_reference_photometry_normalization', 'migrate_mvp7_0_3_reference_photometry_normalization.sql', 'b3c33b4431a6850ca0cca6177c6090ed8f068180b352493c014dff704f5f3380'),
            ('mvp7_0_4_null_identity_constraints', 'migrate_mvp7_0_4_null_identity_constraints.sql', '8317dbfe49097f9258b1b3b43231a37c51beaa915bb1b4e04a53f34e1a19a4a4'),
            ('mvp7_0_5_interseries_membership_field_integrity', 'migrate_mvp7_0_5_interseries_membership_field_integrity.sql', 'a6efc2f2ef07a97b50c93fb57d0bd82aaf9e95d34710815953da1bf4e9a3a878'),
            ('mvp7_0_6_interseries_edge_delete_restrict', 'migrate_mvp7_0_6_interseries_edge_delete_restrict.sql', '1569aecfa02d18455356a11f7e6186ba2fdcb40a475ff98822fbad4bdf3eaea4'),
            ('mvp7_0_7_interseries_edge_membership_integrity', 'migrate_mvp7_0_7_interseries_edge_membership_integrity.sql', '8c987a810d1b93a43dbe9e3b8e295915af1dbd82e9b6dc3210330ba7b7bf13a7')
        )
        SELECT 1 FROM (
            (SELECT migration_id, filename, checksum_sha256 FROM expected
             EXCEPT SELECT migration_id, filename, checksum_sha256 FROM public.schema_migrations)
            UNION ALL
            (SELECT migration_id, filename, checksum_sha256 FROM public.schema_migrations
             EXCEPT SELECT migration_id, filename, checksum_sha256 FROM expected)
        ) AS differences
    ) OR (SELECT COUNT(*) FROM public.schema_migrations) <> 36
      OR EXISTS (SELECT 1 FROM public.schema_migrations
                 WHERE state NOT IN ('APPLIED', 'BASELINED') OR state IS NULL)
      OR EXISTS (SELECT 1 FROM public.schema_migrations
                 WHERE migration_id = 'mvp7_0_8_interseries_reference_cv_scope_integrity') THEN
        RAISE EXCEPTION 'A2/B requires exact 36-entry predecessor ledger and no .8 marker';
    END IF;

    FOR required IN SELECT * FROM (VALUES
        ('public.calibration_versions'::regclass), ('public.field_series'::regclass),
        ('public.interseries_calibration_generations'::regclass),
        ('public.interseries_calibration_series'::regclass),
        ('public.interseries_calibration_edges'::regclass)
    ) AS relations(table_oid) LOOP
        IF pg_catalog.pg_total_relation_size(required.table_oid) > 67108864 THEN
            RAISE EXCEPTION 'A2/B relation exceeds 64 MiB: %', required.table_oid;
        END IF;
    END LOOP;

    FOR required IN SELECT * FROM (VALUES
        ('public.calibration_versions'::regclass, 'calibration_version_id', TRUE),
        ('public.calibration_versions'::regclass, 'field_id', FALSE),
        ('public.calibration_versions'::regclass, 'series_id', FALSE),
        ('public.field_series'::regclass, 'series_id', TRUE),
        ('public.field_series'::regclass, 'field_id', TRUE),
        ('public.interseries_calibration_generations'::regclass, 'interseries_generation_id', TRUE),
        ('public.interseries_calibration_generations'::regclass, 'field_id', TRUE),
        ('public.interseries_calibration_generations'::regclass, 'reference_series_id', TRUE),
        ('public.interseries_calibration_series'::regclass, 'interseries_generation_id', TRUE),
        ('public.interseries_calibration_series'::regclass, 'field_id', TRUE),
        ('public.interseries_calibration_series'::regclass, 'series_id', FALSE),
        ('public.interseries_calibration_series'::regclass, 'science_calibration_version_id', TRUE)
    ) AS columns(table_oid, column_name, expected_not_null) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute AS a
            WHERE a.attrelid = required.table_oid AND a.attname = required.column_name
              AND a.attnum > 0 AND NOT a.attisdropped
              AND a.atttypid = 'pg_catalog.int8'::regtype
              AND a.attnotnull = required.expected_not_null) THEN
            RAISE EXCEPTION 'A2/B column shape mismatch: %.%', required.table_oid, required.column_name;
        END IF;
    END LOOP;
    FOR required IN SELECT * FROM (VALUES
        ('public.interseries_calibration_edges'::regclass, 'interseries_generation_id', 'pg_catalog.int8'::regtype, TRUE),
        ('public.interseries_calibration_edges'::regclass, 'left_series_id', 'pg_catalog.int8'::regtype, TRUE),
        ('public.interseries_calibration_edges'::regclass, 'right_series_id', 'pg_catalog.int8'::regtype, TRUE),
        ('public.interseries_calibration_edges'::regclass, 'pair_status', 'pg_catalog.varchar'::regtype, TRUE)
    ) AS columns(table_oid, column_name, expected_type, expected_not_null) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute AS a
            WHERE a.attrelid = required.table_oid AND a.attname = required.column_name
              AND a.attnum > 0 AND NOT a.attisdropped
              AND a.atttypid = required.expected_type
              AND a.attnotnull = required.expected_not_null) THEN
            RAISE EXCEPTION 'A2/B edge column shape mismatch: %.%', required.table_oid, required.column_name;
        END IF;
    END LOOP;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_constraint AS c
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid = c.connamespace
        WHERE ns.nspname = 'public' AND c.conname IN (
            'uq_calibration_versions_id_field', 'uq_calibration_versions_id_field_series',
            'fk_interseries_generations_reference_member',
            'fk_interseries_series_science_cv_field',
            'fk_interseries_series_science_cv_field_series')) THEN
        RAISE EXCEPTION 'A2/B target constraint name already used in public';
    END IF;

    -- Parent membership unique must already be immediate, valid, and ready.
    SELECT COUNT(*) AS n, BOOL_AND(c.conname = 'uq_interseries_calibration_series_generation_series'
        AND c.convalidated AND NOT c.condeferrable AND NOT c.condeferred
        AND c.conislocal AND c.coninhcount = 0 AND i.indisunique AND i.indisvalid
        AND i.indisready AND i.indimmediate
        AND (current_setting('server_version_num')::integer < 180000
             OR (to_jsonb(c)->>'conenforced')::boolean IS TRUE)) AS valid INTO actual
    FROM pg_catalog.pg_constraint AS c
    JOIN pg_catalog.pg_index AS i ON i.indexrelid = c.conindid
    CROSS JOIN LATERAL (SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
        FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS keys
    WHERE c.conrelid = 'public.interseries_calibration_series'::regclass AND c.contype = 'u'
      AND keys.columns = ARRAY['interseries_generation_id', 'series_id']::text[];
    IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'A2/B membership parent unique unusable';
    END IF;

    -- Existing five edge FKs and existing M4/CV policies must be exact before DDL.
    FOR expected_fk IN SELECT * FROM (VALUES
        ('public.interseries_calibration_generations'::regclass, NULL::text,
         'public.fields'::regclass, ARRAY['field_id']::text[],
         ARRAY['field_id']::text[], 'c', NULL::text[], FALSE),
        ('public.interseries_calibration_generations'::regclass, 'fk_interseries_generations_reference_series_field',
         'public.field_series'::regclass, ARRAY['reference_series_id', 'field_id']::text[],
         ARRAY['series_id', 'field_id']::text[], 'r', NULL::text[], FALSE),
        ('public.interseries_calibration_series'::regclass, 'fk_interseries_series_generation_field',
         'public.interseries_calibration_generations'::regclass,
         ARRAY['interseries_generation_id', 'field_id']::text[],
         ARRAY['interseries_generation_id', 'field_id']::text[], 'c', NULL::text[], FALSE),
        ('public.interseries_calibration_series'::regclass, 'fk_interseries_series_series_field',
         'public.field_series'::regclass, ARRAY['series_id', 'field_id']::text[],
         ARRAY['series_id', 'field_id']::text[], 'n', ARRAY['series_id']::text[], FALSE),
        ('public.interseries_calibration_series'::regclass, NULL::text,
         'public.calibration_versions'::regclass, ARRAY['science_calibration_version_id']::text[],
         ARRAY['calibration_version_id']::text[], 'r', NULL::text[], FALSE),
        ('public.interseries_calibration_edges'::regclass, NULL::text,
         'public.interseries_calibration_generations'::regclass, ARRAY['interseries_generation_id']::text[],
         ARRAY['interseries_generation_id']::text[], 'c', NULL::text[], FALSE),
        ('public.interseries_calibration_edges'::regclass, 'fk_interseries_edges_left_series_restrict',
         'public.field_series'::regclass, ARRAY['left_series_id']::text[], ARRAY['series_id']::text[],
         'r', NULL::text[], FALSE),
        ('public.interseries_calibration_edges'::regclass, 'fk_interseries_edges_right_series_restrict',
         'public.field_series'::regclass, ARRAY['right_series_id']::text[], ARRAY['series_id']::text[],
         'r', NULL::text[], FALSE),
        ('public.interseries_calibration_edges'::regclass, 'fk_interseries_edges_left_generation_member',
         'public.interseries_calibration_series'::regclass,
         ARRAY['interseries_generation_id', 'left_series_id']::text[],
         ARRAY['interseries_generation_id', 'series_id']::text[], 'a', NULL::text[], TRUE),
        ('public.interseries_calibration_edges'::regclass, 'fk_interseries_edges_right_generation_member',
         'public.interseries_calibration_series'::regclass,
         ARRAY['interseries_generation_id', 'right_series_id']::text[],
         ARRAY['interseries_generation_id', 'series_id']::text[], 'a', NULL::text[], TRUE)
    ) AS required_fk(child_table, constraint_name, parent_table, child_columns,
                     parent_columns, delete_action, set_columns, deferred) LOOP
        SELECT COUNT(*) AS n, BOOL_AND(
            (expected_fk.constraint_name IS NULL OR c.conname = expected_fk.constraint_name)
            AND c.confrelid = expected_fk.parent_table
            AND c.confdeltype = expected_fk.delete_action AND c.confupdtype = 'a'
            AND c.confmatchtype = 's' AND c.convalidated AND c.conislocal AND c.coninhcount = 0
            AND c.condeferrable = expected_fk.deferred AND c.condeferred = expected_fk.deferred
            AND parent_key.columns = expected_fk.parent_columns
            AND set_key.columns IS NOT DISTINCT FROM expected_fk.set_columns
            AND (current_setting('server_version_num')::integer < 180000
                 OR (to_jsonb(c)->>'conenforced')::boolean IS TRUE)) AS valid INTO actual
        FROM pg_catalog.pg_constraint AS c
        CROSS JOIN LATERAL (SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
            FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS child_key
        CROSS JOIN LATERAL (SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
            FROM UNNEST(c.confkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.confrelid AND a.attnum = k.attnum) AS parent_key
        CROSS JOIN LATERAL (SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
            FROM UNNEST(c.confdelsetcols) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS set_key
        WHERE c.contype = 'f' AND c.conrelid = expected_fk.child_table
          AND child_key.columns = expected_fk.child_columns;
        IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'A2/B predecessor FK mismatch: %', expected_fk.constraint_name;
        END IF;
    END LOOP;
    IF (SELECT COUNT(*) FROM pg_catalog.pg_constraint AS c WHERE c.contype = 'f'
        AND c.conrelid = 'public.interseries_calibration_edges'::regclass) <> 5
       OR (SELECT COUNT(*) FROM pg_catalog.pg_constraint AS c WHERE c.contype = 'f'
        AND c.conrelid = 'public.interseries_calibration_series'::regclass) <> 3 THEN
        RAISE EXCEPTION 'A2/B unexpected edge or membership FK inventory';
    END IF;

    -- Preserve the .7 edge pair key and active ordering check before inspecting rows.
    SELECT COUNT(*) AS n, BOOL_AND(c.convalidated AND NOT c.condeferrable
        AND c.conislocal AND c.coninhcount = 0 AND i.indisunique AND i.indisvalid
        AND i.indisready AND i.indimmediate
        AND (current_setting('server_version_num')::integer < 180000
             OR (to_jsonb(c)->>'conenforced')::boolean IS TRUE)) AS valid INTO actual
    FROM pg_catalog.pg_constraint AS c
    JOIN pg_catalog.pg_index AS i ON i.indexrelid = c.conindid
    CROSS JOIN LATERAL (SELECT ARRAY_AGG(a.attname::text ORDER BY k.ord) AS columns
        FROM UNNEST(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS keys
    WHERE c.conrelid = 'public.interseries_calibration_edges'::regclass AND c.contype = 'u'
      AND c.conname = 'uq_interseries_calibration_edges_generation_pair'
      AND keys.columns = ARRAY['interseries_generation_id', 'left_series_id', 'right_series_id']::text[];
    IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'A2/B edge pair unique missing or invalid';
    END IF;
    SELECT COUNT(*) AS n, BOOL_AND(c.convalidated AND c.conislocal AND c.coninhcount = 0
        AND (current_setting('server_version_num')::integer < 180000
             OR (to_jsonb(c)->>'conenforced')::boolean IS TRUE)
        AND replace(replace(replace(replace(lower(pg_catalog.pg_get_expr(c.conbin, c.conrelid)),
            ' ', ''), '(', ''), ')', ''), '"', '') = 'left_series_id<right_series_id')
        AS valid INTO actual
    FROM pg_catalog.pg_constraint AS c
    WHERE c.conrelid = 'public.interseries_calibration_edges'::regclass AND c.contype = 'c'
      AND c.conname = 'ck_interseries_calibration_edges_series_order';
    IF actual.n <> 1 OR actual.valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'A2/B edge ordering check missing or invalid';
    END IF;

    FOR required IN SELECT * FROM (VALUES
        ('public.calibration_versions'::regclass, 10001),
        ('public.interseries_calibration_generations'::regclass, 10001),
        ('public.interseries_calibration_series'::regclass, 10001),
        ('public.interseries_calibration_edges'::regclass, 10001)
    ) AS caps(table_oid, limit_rows) LOOP
        EXECUTE format('SELECT COUNT(*) FROM (SELECT 1 FROM %s LIMIT %s) AS bounded',
                       required.table_oid, required.limit_rows) INTO bad_rows;
        IF bad_rows > 10000 THEN
            RAISE EXCEPTION 'A2/B row cap exceeded: %', required.table_oid;
        END IF;
    END LOOP;

    -- All persisted edges, including historical generations and non-OK statuses.
    SELECT COUNT(*) FILTER (WHERE e.interseries_generation_id IS NULL
        OR g.interseries_generation_id IS NULL
        OR e.left_series_id IS NULL OR e.right_series_id IS NULL
        OR left_endpoint.series_id IS NULL OR right_endpoint.series_id IS NULL
        OR left_member.interseries_series_id IS NULL OR right_member.interseries_series_id IS NULL
        OR left_endpoint.field_id IS DISTINCT FROM g.field_id
        OR right_endpoint.field_id IS DISTINCT FROM g.field_id
        OR left_member.field_id IS DISTINCT FROM g.field_id
        OR right_member.field_id IS DISTINCT FROM g.field_id
        OR e.left_series_id >= e.right_series_id) INTO bad_rows
    FROM public.interseries_calibration_edges AS e
    LEFT JOIN public.interseries_calibration_generations AS g
      ON g.interseries_generation_id = e.interseries_generation_id
    LEFT JOIN public.field_series AS left_endpoint ON left_endpoint.series_id = e.left_series_id
    LEFT JOIN public.field_series AS right_endpoint ON right_endpoint.series_id = e.right_series_id
    LEFT JOIN public.interseries_calibration_series AS left_member
      ON left_member.interseries_generation_id = e.interseries_generation_id
     AND left_member.series_id = e.left_series_id
    LEFT JOIN public.interseries_calibration_series AS right_member
      ON right_member.interseries_generation_id = e.interseries_generation_id
     AND right_member.series_id = e.right_series_id;
    IF bad_rows <> 0 THEN RAISE EXCEPTION 'A2/B existing edge scope mismatch'; END IF;
    IF EXISTS (SELECT 1 FROM public.interseries_calibration_edges
        GROUP BY interseries_generation_id, left_series_id, right_series_id HAVING COUNT(*) > 1) THEN
        RAISE EXCEPTION 'A2/B duplicate generation edge pair';
    END IF;

    -- All generations and all members (including historical/non-OK graph state).
    SELECT COUNT(*) INTO bad_rows
    FROM public.interseries_calibration_generations AS g
    LEFT JOIN public.interseries_calibration_series AS m
      ON m.interseries_generation_id = g.interseries_generation_id
     AND m.series_id = g.reference_series_id
    WHERE m.interseries_series_id IS NULL OR m.field_id IS DISTINCT FROM g.field_id;
    IF bad_rows <> 0 THEN RAISE EXCEPTION 'A2/B reference membership mismatch'; END IF;

    SELECT COUNT(*) INTO bad_rows
    FROM public.interseries_calibration_series AS m
    LEFT JOIN public.interseries_calibration_generations AS g
      ON g.interseries_generation_id = m.interseries_generation_id
    LEFT JOIN public.calibration_versions AS cv
      ON cv.calibration_version_id = m.science_calibration_version_id
    LEFT JOIN public.field_series AS fs ON fs.series_id = m.series_id
    WHERE g.interseries_generation_id IS NULL OR m.field_id IS DISTINCT FROM g.field_id
       OR cv.calibration_version_id IS NULL OR cv.field_id IS DISTINCT FROM m.field_id
       OR (m.series_id IS NOT NULL AND (cv.series_id IS DISTINCT FROM m.series_id
                                        OR fs.series_id IS NULL
                                        OR fs.field_id IS DISTINCT FROM m.field_id));
    IF bad_rows <> 0 THEN RAISE EXCEPTION 'A2/B member SCIENCE CV field/series mismatch'; END IF;
END
$$;

-- Two immediate parent keys (CV id is already a PK, but composite FK requires exact keys).
ALTER TABLE public.calibration_versions ADD CONSTRAINT uq_calibration_versions_id_field
    UNIQUE (calibration_version_id, field_id);
ALTER TABLE public.calibration_versions ADD CONSTRAINT uq_calibration_versions_id_field_series
    UNIQUE (calibration_version_id, field_id, series_id);
ALTER TABLE public.interseries_calibration_generations
    ADD CONSTRAINT fk_interseries_generations_reference_member
    FOREIGN KEY (interseries_generation_id, reference_series_id)
    REFERENCES public.interseries_calibration_series (interseries_generation_id, series_id)
    MATCH SIMPLE ON DELETE NO ACTION ON UPDATE NO ACTION DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE public.interseries_calibration_series
    ADD CONSTRAINT fk_interseries_series_science_cv_field
    FOREIGN KEY (science_calibration_version_id, field_id)
    REFERENCES public.calibration_versions (calibration_version_id, field_id)
    MATCH SIMPLE ON DELETE NO ACTION ON UPDATE NO ACTION NOT DEFERRABLE;
ALTER TABLE public.interseries_calibration_series
    ADD CONSTRAINT fk_interseries_series_science_cv_field_series
    FOREIGN KEY (science_calibration_version_id, field_id, series_id)
    REFERENCES public.calibration_versions (calibration_version_id, field_id, series_id)
    MATCH SIMPLE ON DELETE NO ACTION ON UPDATE NO ACTION NOT DEFERRABLE;
