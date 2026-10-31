from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Canonical fresh-baseline schema for Grandgousier 0.7 clean-schema epoch.  Compatibility-only
# 0.6 relations are deliberately excluded: a fresh database must never recreate
# the retired wide SCIENCE layer or legacy Stage-3 offsets.
CURRENT_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "instrument_units",
        "observations",
        "frames",
        "frame_locations",
        "processing_events",
        "catalog_sources",
        "reference_photometry_catalogs",
        "photometric_bands",
        "reference_photometry_sources",
        "reference_photometry_points",
        "frame_catalog_links",
        "detection_runs",
        "detection_run_frames",
        "source_detections",
        "detection_catalog_matches",
        "frame_qc",
        "objects",
        "object_catalog_links",
        "object_qc",
        "measurements",
        "science_photometry",
        "calibration_versions",
        "calibration_product_aliases",
        "calibration_iterations",
        "calibration_frame_terms",
        "calibration_iteration_objects",
        "interseries_calibration_generations",
        "interseries_calibration_series",
        "interseries_calibration_edges",
        "fields",
        "field_series",
        "field_series_frames",
        "pipeline_runs",
        "pipeline_run_steps",
        "pipeline_log_events",
        "artifacts",
        "artifact_publications",
        "batch_runs",
        "batch_inventory_items",
        "batch_jobs",
        "batch_job_attempts",
        "event_registry",
        "event_object_links",
        "event_frame_coverage",
        "event_detection_links",
    }
)

# Their presence identifies a pre-clean-schema database.  0.7 is intentionally
# fresh-baseline-only: neither init-db nor migrate-db may mutate such a database
# into the clean layout in place.
FORBIDDEN_LEGACY_TABLES: frozenset[str] = frozenset(
    {
        "science_measurements",
        "science_photometry_cache",
        "science_flag_override_sets",
        "science_flag_overrides",
        "interseries_calibration_offsets",
        "reference_photometry",
    }
)


def missing_tables(existing_tables: Iterable[str]) -> list[str]:
    """Return canonical Grandgousier tables missing from an inspected schema."""

    return sorted(CURRENT_SCHEMA_TABLES.difference(set(existing_tables)))


def forbidden_tables(existing_tables: Iterable[str]) -> list[str]:
    """Return 0.6 compatibility relations forbidden by the clean 0.7 contract."""

    return sorted(FORBIDDEN_LEGACY_TABLES.intersection(set(existing_tables)))


def assert_clean_schema_epoch(existing_tables: Iterable[str]) -> None:
    """Refuse in-place use of a pre-0.7 compatibility schema."""

    forbidden = forbidden_tables(existing_tables)
    if forbidden:
        raise RuntimeError(
            "Grandgousier 0.7 clean schema is fresh-baseline-only; refusing an "
            "in-place operation on a database containing legacy tables: "
            + ", ".join(forbidden)
        )


def assert_schema_contract(engine: Engine) -> None:
    """Raise unless the database satisfies the clean 0.7 table contract."""

    existing = inspect(engine).get_table_names()
    assert_clean_schema_epoch(existing)
    missing = missing_tables(existing)
    if missing:
        raise RuntimeError(
            "Grandgousier database schema is incomplete; missing tables: "
            + ", ".join(missing)
        )
    assert_m4_schema_contract(engine)


def assert_m4_schema_contract(engine: Engine) -> None:
    """Check only the M4 integrity keys added after the clean 0.7 baseline.

    PostgreSQL's generic FK reflection does not expose the SET NULL column subset;
    pg_constraint.confdelsetcols does. This runs *after* fresh create_all and also
    refuses a complete but pre-migration schema instead of silently baselining it.
    """

    if engine.dialect.name != "postgresql":
        raise RuntimeError("M4 schema contract requires PostgreSQL")
    with engine.connect() as conn:
        columns = conn.execute(text("""
            SELECT tbl.relname AS table_name, a.attname AS column_name,
                   a.attnotnull AS not_null,
                   a.atttypid = 'pg_catalog.int8'::regtype AS is_bigint
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class tbl ON tbl.oid = a.attrelid
            WHERE tbl.oid IN (
                'public.calibration_versions'::regclass,
                'public.field_series'::regclass,
                'public.interseries_calibration_generations'::regclass,
                'public.interseries_calibration_series'::regclass,
                'public.interseries_calibration_edges'::regclass
            ) AND a.attnum > 0 AND NOT a.attisdropped
        """)).mappings().all()
        constraints = conn.execute(text("""
            SELECT c.conname AS name, c.contype AS kind,
                   child.relname AS child_table, child_ns.nspname AS child_schema,
                   parent.relname AS parent_table, parent_ns.nspname AS parent_schema,
                   c.confdeltype AS delete_action, c.confupdtype AS update_action,
                   c.confmatchtype AS match_type, c.convalidated AS validated,
                    c.condeferrable AS deferrable, c.condeferred AS initially_deferred,
                    c.conislocal AS is_local,
                    c.coninhcount AS inherited_count,
                    current_setting('server_version_num')::integer AS server_version_num,
                    -- PG15-17 do not expose conenforced as a catalog column.
                    (to_jsonb(c)->>'conenforced')::boolean AS enforced,
                    i.indisvalid AS index_valid, i.indisready AS index_ready,
                    i.indimmediate AS index_immediate,
                    ARRAY(
                        SELECT a.attname::text
                       FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_catalog.pg_attribute a
                         ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                       ORDER BY k.ord
                   ) AS child_columns,
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_catalog.pg_attribute a
                         ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                       ORDER BY k.ord
                   ) AS parent_columns,
                   ARRAY(
                       SELECT a.attname::text
                       FROM unnest(c.confdelsetcols) WITH ORDINALITY AS k(attnum, ord)
                       JOIN pg_catalog.pg_attribute a
                         ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                        ORDER BY k.ord
                    ) AS delete_columns,
                    CASE WHEN c.contype = 'c'
                         THEN pg_catalog.pg_get_expr(c.conbin, c.conrelid)
                    END AS check_expression
            FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class child ON child.oid = c.conrelid
            JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
            LEFT JOIN pg_catalog.pg_class parent ON parent.oid = c.confrelid
            LEFT JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            LEFT JOIN pg_catalog.pg_index i ON i.indexrelid = c.conindid
            WHERE c.conrelid IN (
                'public.calibration_versions'::regclass,
                'public.field_series'::regclass,
                'public.interseries_calibration_generations'::regclass,
                'public.interseries_calibration_series'::regclass,
                'public.interseries_calibration_edges'::regclass
            ) AND c.contype IN ('p', 'u', 'f', 'c')
        """)).mappings().all()
    _assert_m4_schema_rows(columns, constraints)


def _assert_m4_schema_rows(columns: Iterable[dict], constraints: Iterable[dict]) -> None:
    """Pure validation boundary for offline tests and the PostgreSQL inspector."""

    column_map = {
        (row["table_name"], row["column_name"]):
            (bool(row["not_null"]), bool(row["is_bigint"]))
        for row in columns
    }
    expected_columns = {
        ("calibration_versions", "calibration_version_id"): (True, True),
        ("calibration_versions", "field_id"): (False, True),
        ("calibration_versions", "series_id"): (False, True),
        ("field_series", "series_id"): (True, True),
        ("field_series", "field_id"): (True, True),
        ("interseries_calibration_generations", "interseries_generation_id"): (True, True),
        ("interseries_calibration_generations", "field_id"): (True, True),
        ("interseries_calibration_generations", "reference_series_id"): (True, True),
        ("interseries_calibration_series", "interseries_generation_id"): (True, True),
        ("interseries_calibration_series", "field_id"): (True, True),
        ("interseries_calibration_series", "series_id"): (False, True),
        ("interseries_calibration_series", "science_calibration_version_id"): (True, True),
        ("interseries_calibration_edges", "interseries_generation_id"): (True, True),
        ("interseries_calibration_edges", "left_series_id"): (True, True),
        ("interseries_calibration_edges", "right_series_id"): (True, True),
    }
    for key, expected_shape in expected_columns.items():
        if column_map.get(key) != expected_shape:
            raise RuntimeError(f"M4 schema column missing or has wrong type/nullability: {key}")

    rows = list(constraints)
    unique_keys = {
        ("calibration_versions", "uq_calibration_versions_id_field"):
            ("calibration_version_id", "field_id"),
        ("calibration_versions", "uq_calibration_versions_id_field_series"):
            ("calibration_version_id", "field_id", "series_id"),
        ("field_series", "uq_field_series_series_field"): ("series_id", "field_id"),
        ("interseries_calibration_generations", "uq_interseries_generations_id_field"):
            ("interseries_generation_id", "field_id"),
        ("interseries_calibration_series",
         "uq_interseries_calibration_series_generation_series"):
            ("interseries_generation_id", "series_id"),
        ("interseries_calibration_edges",
         "uq_interseries_calibration_edges_generation_pair"):
            ("interseries_generation_id", "left_series_id", "right_series_id"),
    }
    for (table, name), columns_expected in unique_keys.items():
        matches = [row for row in rows if row["child_schema"] == "public"
                   and row["child_table"] == table and row["name"] == name]
        if (len(matches) != 1 or matches[0]["kind"] != "u"
                or tuple(matches[0]["child_columns"]) != columns_expected
                  or not matches[0]["validated"] or matches[0]["deferrable"]
                  or not matches[0]["is_local"] or matches[0]["inherited_count"] != 0):
            raise RuntimeError(f"M4 composite unique constraint missing or incorrect: {name}")
        if table in {"interseries_calibration_series", "calibration_versions"} and (
            matches[0]["initially_deferred"]
            or matches[0]["index_valid"] is not True
            or matches[0]["index_ready"] is not True
            or matches[0]["index_immediate"] is not True
            or (matches[0]["server_version_num"] >= 180000
                and matches[0]["enforced"] is not True)
        ):
            raise RuntimeError(f"M4 parent unique is not usable: {name}")

    field_series_pks = [
        row for row in rows
        if row["kind"] == "p" and row["child_schema"] == "public"
        and row["child_table"] == "field_series"
        and tuple(row["child_columns"]) == ("series_id",)
    ]
    if (len(field_series_pks) != 1 or not field_series_pks[0]["validated"]
            or field_series_pks[0]["deferrable"] or not field_series_pks[0]["is_local"]
            or field_series_pks[0]["inherited_count"] != 0):
        raise RuntimeError("M4 field_series(series_id) primary key is missing or incorrect")

    edge_order_checks = [
        row for row in rows
        if row["kind"] == "c" and row["child_schema"] == "public"
        and row["child_table"] == "interseries_calibration_edges"
        and row["name"] == "ck_interseries_calibration_edges_series_order"
    ]
    if len(edge_order_checks) != 1:
        raise RuntimeError("M4 edge ordering check is missing or competing")
    order_expression = "".join(
        char for char in str(edge_order_checks[0]["check_expression"]).lower()
        if char not in " ()\t\r\n\""
    )
    if not edge_order_checks[0]["validated"] or order_expression != "left_series_id<right_series_id":
        raise RuntimeError("M4 edge ordering check has incorrect expression or validation")

    foreign_keys = (
        ("interseries_calibration_generations", "reference_series_id",
         "fk_interseries_generations_reference_member", "interseries_calibration_series",
         ("interseries_generation_id", "reference_series_id"),
         ("interseries_generation_id", "series_id"), "a", (), True),
        ("interseries_calibration_generations", "reference_series_id",
         "fk_interseries_generations_reference_series_field", "field_series",
         ("reference_series_id", "field_id"), ("series_id", "field_id"), "r", (), False),
        ("interseries_calibration_series", "interseries_generation_id",
         "fk_interseries_series_generation_field", "interseries_calibration_generations",
         ("interseries_generation_id", "field_id"),
          ("interseries_generation_id", "field_id"), "c", (), False),
        ("interseries_calibration_series", "series_id",
          "fk_interseries_series_series_field", "field_series",
           ("series_id", "field_id"), ("series_id", "field_id"), "n", ("series_id",), False),
        ("interseries_calibration_series", "science_calibration_version_id",
         None, "calibration_versions", ("science_calibration_version_id",),
         ("calibration_version_id",), "r", (), False),
        ("interseries_calibration_series", "science_calibration_version_id",
         "fk_interseries_series_science_cv_field", "calibration_versions",
         ("science_calibration_version_id", "field_id"),
         ("calibration_version_id", "field_id"), "a", (), False),
        ("interseries_calibration_series", "science_calibration_version_id",
         "fk_interseries_series_science_cv_field_series", "calibration_versions",
         ("science_calibration_version_id", "field_id", "series_id"),
         ("calibration_version_id", "field_id", "series_id"), "a", (), False),
        ("interseries_calibration_edges", "interseries_generation_id",
         None, "interseries_calibration_generations",
          ("interseries_generation_id",), ("interseries_generation_id",), "c", (), False),
        ("interseries_calibration_edges", "left_series_id",
         "fk_interseries_edges_left_series_restrict", "field_series",
          ("left_series_id",), ("series_id",), "r", (), False),
        ("interseries_calibration_edges", "right_series_id",
         "fk_interseries_edges_right_series_restrict", "field_series",
          ("right_series_id",), ("series_id",), "r", (), False),
        ("interseries_calibration_edges", "left_series_id",
         "fk_interseries_edges_left_generation_member", "interseries_calibration_series",
         ("interseries_generation_id", "left_series_id"),
         ("interseries_generation_id", "series_id"), "a", (), True),
        ("interseries_calibration_edges", "right_series_id",
         "fk_interseries_edges_right_generation_member", "interseries_calibration_series",
         ("interseries_generation_id", "right_series_id"),
         ("interseries_generation_id", "series_id"), "a", (), True),
    )
    for child, replaced_column, name, parent, child_cols, parent_cols, action, set_cols, deferred in foreign_keys:
        matches = [
            row for row in rows
            if row["kind"] == "f" and row["child_schema"] == "public"
            and row["child_table"] == child
            and tuple(row["child_columns"]) == child_cols
        ]
        if len(matches) != 1:
            raise RuntimeError(f"M4 foreign key missing or competing FK remains: {name}")
        row = matches[0]
        if (
            (name is not None and row["name"] != name) or row["parent_schema"] != "public"
            or row["parent_table"] != parent
            or tuple(row["child_columns"]) != child_cols
            or tuple(row["parent_columns"]) != parent_cols
            or row["delete_action"] != action
            or tuple(row["delete_columns"]) != set_cols
            or row["update_action"] != "a" or row["match_type"] != "s"
            or not row["validated"] or row["deferrable"] is not deferred
            or row["initially_deferred"] is not deferred
            or (row["server_version_num"] >= 180000 and row["enforced"] is not True)
            or not row["is_local"] or row["inherited_count"] != 0
        ):
            raise RuntimeError(f"M4 composite foreign key has incorrect scope or delete action: {name}")

    edge_fks = [
        row for row in rows
        if row["kind"] == "f" and row["child_schema"] == "public"
        and row["child_table"] == "interseries_calibration_edges"
    ]
    if len(edge_fks) != 5:
        raise RuntimeError("M4 edge table has missing or unexpected foreign keys")
