"""Dry-run-first audit and narrowly scoped cleanup of legacy field Gaia catalogs.

The default command path is protected by a PostgreSQL READ ONLY transaction.  The
optional apply path only removes ``auto_catalog_field_`` frame projections and
source rows after exact database confirmation, target protection checks, and a
complete incoming-FK inventory.  It never targets canonical ``gaia_dr3`` or any
object, measurement, or science-product table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import text

LEGACY_CATALOG_PREFIX = "auto_catalog_field_"
CANONICAL_CATALOG_NAME = "gaia_dr3"
REPORT_SCHEMA = "grandgousier.legacy-gaia-catalog-cleanup.v1"

_IDENTITY_SQL = """
SELECT
    d.datname AS database_name,
    d.oid::bigint AS database_oid,
    current_user AS db_user,
    inet_server_addr()::text AS server_address,
    inet_server_port() AS server_port,
    current_setting('server_version') AS server_version,
    current_setting('server_version_num')::integer AS server_version_num,
    current_schema() AS current_schema,
    shobj_description(d.oid, 'pg_database') AS database_comment,
    d.datallowconn AS database_allows_connections,
    d.datistemplate AS database_is_template
FROM pg_catalog.pg_database AS d
WHERE d.datname = current_database()
"""

_RELATION_SQL = """
SELECT c.oid::bigint AS relation_oid, n.nspname AS schema_name,
       c.relname AS relation_name, c.relkind::text AS relation_kind
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(:relation_name)
"""

_INCOMING_FKS_SQL = """
WITH target_relations AS (
    SELECT 'catalog_sources'::text AS target_table,
           to_regclass('catalog_sources')::oid AS target_oid
    UNION ALL
    SELECT 'frame_catalog_links'::text AS target_table,
           to_regclass('frame_catalog_links')::oid AS target_oid
)
SELECT
    target_relations.target_table,
    target_ns.nspname AS target_schema,
    child_ns.nspname AS child_schema,
    child.relname AS child_table,
    constraint_row.conname AS constraint_name,
    ARRAY(
        SELECT child_attribute.attname
        FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, position)
        JOIN pg_catalog.pg_attribute AS child_attribute
          ON child_attribute.attrelid = child.oid
         AND child_attribute.attnum = key_column.attnum
        ORDER BY key_column.position
    ) AS child_columns,
    ARRAY(
        SELECT target_attribute.attname
        FROM unnest(constraint_row.confkey) WITH ORDINALITY AS key_column(attnum, position)
        JOIN pg_catalog.pg_attribute AS target_attribute
          ON target_attribute.attrelid = target.oid
         AND target_attribute.attnum = key_column.attnum
        ORDER BY key_column.position
    ) AS target_columns,
    CASE constraint_row.confdeltype
        WHEN 'a' THEN 'NO ACTION'
        WHEN 'r' THEN 'RESTRICT'
        WHEN 'c' THEN 'CASCADE'
        WHEN 'n' THEN 'SET NULL'
        WHEN 'd' THEN 'SET DEFAULT'
        ELSE 'UNKNOWN'
    END AS on_delete,
    constraint_row.convalidated AS validated
FROM pg_catalog.pg_constraint AS constraint_row
JOIN target_relations ON target_relations.target_oid = constraint_row.confrelid
JOIN pg_catalog.pg_class AS target ON target.oid = constraint_row.confrelid
JOIN pg_catalog.pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
JOIN pg_catalog.pg_class AS child ON child.oid = constraint_row.conrelid
JOIN pg_catalog.pg_namespace AS child_ns ON child_ns.oid = child.relnamespace
WHERE constraint_row.contype = 'f'
ORDER BY target_relations.target_table, child_ns.nspname, child.relname,
         constraint_row.conname
"""

_KNOWN_FK_ROLES: dict[tuple[str, str, tuple[str, ...], tuple[str, ...], str], str] = {
    (
        "catalog_sources",
        "frame_catalog_links",
        ("catalog_source_id",),
        ("catalog_source_id",),
        "CASCADE",
    ): "FRAME_PROJECTION",
    (
        "catalog_sources",
        "detection_catalog_matches",
        ("catalog_source_id",),
        ("catalog_source_id",),
        "CASCADE",
    ): "DETECTION_MATCH",
    (
        "catalog_sources",
        "object_catalog_links",
        ("catalog_source_id",),
        ("catalog_source_id",),
        "RESTRICT",
    ): "OBJECT_CATALOG_LINK",
    (
        "catalog_sources",
        "reference_photometry_sources",
        ("identity_catalog_source_id",),
        ("catalog_source_id",),
        "RESTRICT",
    ): "REFERENCE_IDENTITY",
}

_EXPECTED_FK_SHAPES = frozenset(_KNOWN_FK_ROLES)
_PROTECTED_MARKERS = (
    "production",
    "prod",
    "frozen",
    "reference",
    "protected",
    "readonly",
    "read-only",
)
_PROTECTED_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:production|prod|frozen|reference|protected|readonly|read[-_ ]only)"
    r"(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class LegacyGaiaCatalogCleanupError(RuntimeError):
    """A safety or schema contract required by the cleanup was not met."""


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    database_name: str
    database_oid: int | None
    db_user: str | None
    server_address: str | None
    server_port: int | None
    server_version: str | None
    server_version_num: int | None
    current_schema: str | None
    configured_host: str | None
    database_allows_connections: bool
    database_is_template: bool
    protected_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "database_name": self.database_name,
            "database_oid": self.database_oid,
            "db_user": self.db_user,
            "server_address": self.server_address,
            "server_port": self.server_port,
            "server_version": self.server_version,
            "server_version_num": self.server_version_num,
            "current_schema": self.current_schema,
            "configured_host": self.configured_host,
            "database_allows_connections": self.database_allows_connections,
            "database_is_template": self.database_is_template,
            "protected": bool(self.protected_reasons),
            "protected_reasons": list(self.protected_reasons),
        }


@dataclass(frozen=True, slots=True)
class Relation:
    oid: int
    schema: str
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class IncomingForeignKey:
    target_table: str
    target_schema: str
    child_schema: str
    child_table: str
    constraint_name: str
    child_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    on_delete: str
    validated: bool

    @property
    def shape(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...], str]:
        return (
            self.target_table,
            self.child_table,
            self.child_columns,
            self.target_columns,
            self.on_delete,
        )

    def role(self, *, expected_schema: str) -> str | None:
        if self.target_schema != expected_schema or self.child_schema != expected_schema:
            return None
        if not self.validated:
            return None
        return _KNOWN_FK_ROLES.get(self.shape)

    def to_dict(self, *, expected_schema: str, blocker_count: int | None) -> dict[str, Any]:
        role = self.role(expected_schema=expected_schema)
        return {
            "target_table": self.target_table,
            "target_schema": self.target_schema,
            "child_table": self.child_table,
            "child_schema": self.child_schema,
            "constraint_name": self.constraint_name,
            "child_columns": list(self.child_columns),
            "target_columns": list(self.target_columns),
            "on_delete": self.on_delete,
            "validated": self.validated,
            "recognized_role": role,
            "unknown_or_unvalidated": role is None,
            "references_legacy_rows": blocker_count,
            "blocks_source_delete": (
                self.target_table == "catalog_sources"
                and (role is None or blocker_count not in (None, 0))
            ),
            "blocks_frame_link_delete": (
                self.target_table == "frame_catalog_links"
                and (role is None or blocker_count not in (None, 0))
            ),
        }


@dataclass(frozen=True, slots=True)
class SourceDisposition:
    durable_keep: bool
    removable_frame_projection: bool
    removable_source: bool
    blockers: tuple[str, ...]


def is_legacy_catalog_name(catalog_name: str) -> bool:
    """Match the literal legacy prefix (underscores are not SQL wildcards)."""

    return isinstance(catalog_name, str) and catalog_name.startswith(LEGACY_CATALOG_PREFIX)


def classify_source_disposition(
    *,
    durable_keep_reasons: Sequence[str],
    incoming_fk_counts: Mapping[str, int],
    unknown_fk_present: bool = False,
) -> SourceDisposition:
    """Purely classify a source while treating frame projections as removable caches."""

    keep_reasons = tuple(sorted(set(durable_keep_reasons)))
    durable_keep = bool(keep_reasons)
    blockers = tuple(
        sorted(
            role
            for role, count in incoming_fk_counts.items()
            if int(count) > 0 and role != "FRAME_PROJECTION"
        )
    )
    if unknown_fk_present:
        blockers = tuple(sorted(set(blockers) | {"UNKNOWN_FOREIGN_KEY"}))
    return SourceDisposition(
        durable_keep=durable_keep,
        removable_frame_projection=not durable_keep,
        removable_source=not durable_keep and not blockers,
        blockers=blockers,
    )


def validate_apply_acknowledgements(
    *, apply: bool, confirm_database: str | None, confirm_catalog_cleanup: bool
) -> None:
    """Validate the three CLI gates without opening a connection."""

    if not apply:
        return
    if not confirm_database:
        raise LegacyGaiaCatalogCleanupError(
            "--apply requires --confirm-database with the exact current database name"
        )
    if not confirm_catalog_cleanup:
        raise LegacyGaiaCatalogCleanupError(
            "--apply requires --confirm-catalog-cleanup"
        )


def _has_protected_marker(value: str | None) -> bool:
    if not value:
        return False
    if _PROTECTED_TOKEN_RE.search(value):
        return True
    # A few deployments use compact names such as ``prod2`` or ``frozenref``.
    lowered = value.casefold()
    return any(marker in lowered for marker in _PROTECTED_MARKERS)


def _database_identity(connection) -> DatabaseIdentity:
    row = connection.execute(text(_IDENTITY_SQL)).mappings().first()
    if row is None or not row.get("database_name"):
        raise LegacyGaiaCatalogCleanupError("could not establish PostgreSQL database identity")

    url = getattr(getattr(connection, "engine", None), "url", None)
    configured_host = getattr(url, "host", None) if url is not None else None
    candidates = (
        ("database_name", row.get("database_name")),
        ("db_user", row.get("db_user")),
        ("configured_host", configured_host),
        ("server_address", row.get("server_address")),
        ("database_comment", row.get("database_comment")),
        ("current_schema", row.get("current_schema")),
    )
    protected_reasons = tuple(
        f"{label}_protected_marker"
        for label, value in candidates
        if _has_protected_marker(str(value) if value is not None else None)
    )
    if not bool(row.get("database_allows_connections", True)):
        protected_reasons += ("database_disallows_connections",)
    if bool(row.get("database_is_template", False)):
        protected_reasons += ("template_database",)

    return DatabaseIdentity(
        database_name=str(row["database_name"]),
        database_oid=int(row["database_oid"]) if row.get("database_oid") is not None else None,
        db_user=str(row["db_user"]) if row.get("db_user") is not None else None,
        server_address=(
            str(row["server_address"]) if row.get("server_address") is not None else None
        ),
        server_port=int(row["server_port"]) if row.get("server_port") is not None else None,
        server_version=(
            str(row["server_version"]) if row.get("server_version") is not None else None
        ),
        server_version_num=(
            int(row["server_version_num"])
            if row.get("server_version_num") is not None
            else None
        ),
        current_schema=(
            str(row["current_schema"]) if row.get("current_schema") is not None else None
        ),
        configured_host=str(configured_host) if configured_host is not None else None,
        database_allows_connections=bool(row.get("database_allows_connections", True)),
        database_is_template=bool(row.get("database_is_template", False)),
        protected_reasons=protected_reasons,
    )


def _assert_identity_is_complete(identity: DatabaseIdentity) -> None:
    if (
        not identity.database_name
        or identity.database_oid is None
        or identity.database_oid <= 0
        or not identity.db_user
        or not identity.server_version
    ):
        raise LegacyGaiaCatalogCleanupError(
            "database identity is incomplete; audit/apply is blocked"
        )


def _assert_apply_transaction_isolation(connection) -> None:
    isolation_level = connection.execute(text("SHOW transaction_isolation")).scalar_one()
    if str(isolation_level).casefold() != "read committed":
        raise LegacyGaiaCatalogCleanupError(
            "apply requires READ COMMITTED transaction isolation so the locked "
            "pipeline_runs protection inventory sees writers that finish before lock acquisition"
        )


def _assert_apply_target(identity: DatabaseIdentity, confirm_database: str | None) -> None:
    _assert_identity_is_complete(identity)
    if confirm_database != identity.database_name:
        raise LegacyGaiaCatalogCleanupError(
            "--confirm-database does not exactly match current_database()"
        )
    if identity.protected_reasons:
        raise LegacyGaiaCatalogCleanupError(
            "apply is forbidden for a protected, frozen, production, or template database"
        )


def _resolve_relation(connection, relation_name: str) -> Relation:
    row = connection.execute(
        text(_RELATION_SQL), {"relation_name": relation_name}
    ).mappings().first()
    if row is None or row.get("relation_oid") is None:
        raise LegacyGaiaCatalogCleanupError(
            f"required PostgreSQL relation is missing: {relation_name}"
        )
    relation = Relation(
        oid=int(row["relation_oid"]),
        schema=str(row["schema_name"]),
        name=str(row["relation_name"]),
        kind=str(row["relation_kind"]),
    )
    if relation.kind not in {"r", "p"}:
        raise LegacyGaiaCatalogCleanupError(
            f"required relation is not a PostgreSQL table: {relation_name}"
        )
    return relation


def _q(identifier: str) -> str:
    """Quote an identifier obtained from PostgreSQL catalogs."""

    return '"' + identifier.replace('"', '""') + '"'


def _qualified(relation: Relation) -> str:
    return f"{_q(relation.schema)}.{_q(relation.name)}"


def _read_incoming_fks(connection) -> list[IncomingForeignKey]:
    rows = connection.execute(text(_INCOMING_FKS_SQL)).mappings().all()
    return [
        IncomingForeignKey(
            target_table=str(row["target_table"]),
            target_schema=str(row["target_schema"]),
            child_schema=str(row["child_schema"]),
            child_table=str(row["child_table"]),
            constraint_name=str(row["constraint_name"]),
            child_columns=tuple(str(value) for value in (row.get("child_columns") or ())),
            target_columns=tuple(str(value) for value in (row.get("target_columns") or ())),
            on_delete=str(row["on_delete"]),
            validated=bool(row["validated"]),
        )
        for row in rows
    ]


def _expected_fk_shapes_present(
    foreign_keys: Sequence[IncomingForeignKey], *, expected_schema: str
) -> tuple[bool, list[str]]:
    present = {
        fk.shape
        for fk in foreign_keys
        if fk.target_schema == expected_schema and fk.child_schema == expected_schema
    }
    missing = sorted(_EXPECTED_FK_SHAPES.difference(present))
    labels = [f"{target}<-{child}" for target, child, *_ in missing]
    return not missing, labels


def _foreign_key_reference_count(
    connection,
    fk: IncomingForeignKey,
    relations: Mapping[str, Relation],
) -> int | None:
    if not fk.child_columns or len(fk.child_columns) != len(fk.target_columns):
        return None
    target = relations.get(fk.target_table)
    catalog_sources = relations["catalog_sources"]
    if target is None:
        return None

    join = " AND ".join(
        f"child.{_q(child_column)} = parent.{_q(target_column)}"
        for child_column, target_column in zip(fk.child_columns, fk.target_columns, strict=True)
    )
    sql = (
        f"SELECT count(*)::bigint AS reference_count "
        f"FROM {_q(fk.child_schema)}.{_q(fk.child_table)} AS child "
        f"JOIN {_qualified(target)} AS parent ON {join} "
    )
    if fk.target_table == "catalog_sources":
        sql += (
            "WHERE left(parent.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix"
        )
    elif fk.target_table == "frame_catalog_links":
        sql += (
            f"JOIN {_qualified(catalog_sources)} AS source "
            "ON parent.catalog_source_id = source.catalog_source_id "
            "WHERE left(source.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix"
        )
    else:
        return None
    value = connection.execute(
        text(sql), {"legacy_prefix": LEGACY_CATALOG_PREFIX}
    ).scalar_one()
    return int(value)


def _foreign_key_source_references(
    connection,
    fk: IncomingForeignKey,
    relations: Mapping[str, Relation],
) -> dict[int, int] | None:
    """Map unknown incoming-FK references back to legacy source IDs when possible."""

    if not fk.child_columns or len(fk.child_columns) != len(fk.target_columns):
        return None
    target = relations.get(fk.target_table)
    catalog_sources = relations["catalog_sources"]
    if target is None:
        return None
    join = " AND ".join(
        f"child.{_q(child_column)} = parent.{_q(target_column)}"
        for child_column, target_column in zip(fk.child_columns, fk.target_columns, strict=True)
    )
    sql = (
        "SELECT source.catalog_source_id AS source_id, count(*)::bigint AS reference_count "
        f"FROM {_q(fk.child_schema)}.{_q(fk.child_table)} AS child "
        f"JOIN {_qualified(target)} AS parent ON {join} "
    )
    if fk.target_table == "catalog_sources":
        sql += (
            "JOIN "
            f"{_qualified(catalog_sources)} AS source "
            "ON source.catalog_source_id = parent.catalog_source_id "
            "WHERE left(source.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix"
        )
    elif fk.target_table == "frame_catalog_links":
        sql += (
            f"JOIN {_qualified(catalog_sources)} AS source "
            "ON source.catalog_source_id = parent.catalog_source_id "
            "WHERE left(source.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix"
        )
    else:
        return None
    sql += " GROUP BY source.catalog_source_id"
    rows = connection.execute(
        text(sql), {"legacy_prefix": LEGACY_CATALOG_PREFIX}
    ).mappings().all()
    return {int(row["source_id"]): int(row["reference_count"]) for row in rows}


def _pipeline_run_target_condition(
    schema: str,
    alias: str = "cs",
    *,
    pipeline_runs_schema: str | None = None,
) -> str:
    pipeline_runs = _q(pipeline_runs_schema or schema) + "." + _q("pipeline_runs")
    return (
        f"EXISTS (SELECT 1 FROM {pipeline_runs} AS pipeline_run "
        f"WHERE pipeline_run.catalog_name = {alias}.catalog_name "
        "AND jsonb_typeof(pipeline_run.recipe_json #> "
        "'{quality_policy,target_source_id}') = 'string' "
        "AND pipeline_run.recipe_json #>> "
        "'{quality_policy,target_source_id}' = "
        f"{alias}.source_id)"
    )


def _legacy_cte(schema: str, *, pipeline_runs_schema: str | None = None) -> str:
    catalog_sources = _q(schema) + "." + _q("catalog_sources")
    object_links = _q(schema) + "." + _q("object_catalog_links")
    detection_matches = _q(schema) + "." + _q("detection_catalog_matches")
    reference_sources = _q(schema) + "." + _q("reference_photometry_sources")
    pipeline_runs = pipeline_runs_schema or schema
    return f"""
legacy_sources AS (
    SELECT cs.catalog_source_id, cs.catalog_name, cs.source_id,
           EXISTS (
               SELECT 1 FROM {object_links} AS object_link
               WHERE object_link.catalog_source_id = cs.catalog_source_id
           ) AS has_object_link,
           EXISTS (
               SELECT 1 FROM {detection_matches} AS detection_match
               WHERE detection_match.catalog_source_id = cs.catalog_source_id
                 AND detection_match.match_status = 'MATCHED'
           ) AS has_accepted_match,
           EXISTS (
               SELECT 1 FROM {detection_matches} AS detection_match
               WHERE detection_match.catalog_source_id = cs.catalog_source_id
           ) AS has_any_detection_match,
           EXISTS (
               SELECT 1 FROM {reference_sources} AS reference_source
               WHERE reference_source.identity_catalog_source_id = cs.catalog_source_id
           ) AS has_reference_identity,
            (
                cs.catalog_source_id = ANY(CAST(:keep_catalog_source_ids AS bigint[]))
             OR cs.source_id = ANY(CAST(:keep_source_ids AS text[]))
             ) AS is_explicit_target,
             {_pipeline_run_target_condition(
                 schema, "cs", pipeline_runs_schema=pipeline_runs
             )} AS has_pipeline_run_target,
             cs.catalog_source_id = ANY(CAST(:foreign_key_source_ids AS bigint[]))
                 AS has_incoming_fk_reference
    FROM {catalog_sources} AS cs
    WHERE left(cs.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix
      AND cs.catalog_name <> :canonical_catalog_name
),
source_facts AS (
    SELECT legacy_sources.*,
           (has_object_link OR has_accepted_match OR has_reference_identity
            OR is_explicit_target OR has_pipeline_run_target
            OR has_incoming_fk_reference) AS is_durable_keep
    FROM legacy_sources
)
"""


def _plan_parameters(
    *,
    keep_source_ids: Sequence[str],
    keep_catalog_source_ids: Sequence[int],
    foreign_key_source_ids: Sequence[int],
) -> dict[str, Any]:
    return {
        "legacy_prefix": LEGACY_CATALOG_PREFIX,
        "canonical_catalog_name": CANONICAL_CATALOG_NAME,
        "keep_source_ids": list(keep_source_ids),
        "keep_catalog_source_ids": [int(value) for value in keep_catalog_source_ids],
        "foreign_key_source_ids": [int(value) for value in foreign_key_source_ids],
    }


def _source_summary(
    connection,
    schema: str,
    parameters: Mapping[str, Any],
    *,
    pipeline_runs_schema: str | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
WITH {_legacy_cte(schema, pipeline_runs_schema=pipeline_runs_schema)}
SELECT catalog_name,
       count(*)::bigint AS source_count,
       count(*) FILTER (WHERE has_object_link)::bigint AS object_link_sources,
       count(*) FILTER (WHERE has_accepted_match)::bigint AS accepted_match_sources,
       count(*) FILTER (WHERE has_reference_identity)::bigint AS reference_identity_sources,
       count(*) FILTER (WHERE is_explicit_target)::bigint AS explicit_target_sources,
       count(*) FILTER (WHERE has_pipeline_run_target)::bigint AS pipeline_run_target_sources,
       count(*) FILTER (WHERE has_incoming_fk_reference)::bigint AS other_fk_protected_sources,
       count(*) FILTER (WHERE is_durable_keep)::bigint AS durable_keep_sources,
       count(*) FILTER (
           WHERE NOT is_durable_keep AND NOT has_any_detection_match
       )::bigint AS known_removable_sources,
       count(*) FILTER (
           WHERE NOT is_durable_keep AND has_any_detection_match
       )::bigint AS blocked_by_detection_match_sources
FROM source_facts
GROUP BY catalog_name
ORDER BY catalog_name
"""
    rows = connection.execute(text(sql), dict(parameters)).mappings().all()
    return [dict(row) for row in rows]


def _frame_link_summary(
    connection,
    schema: str,
    parameters: Mapping[str, Any],
    *,
    pipeline_runs_schema: str | None = None,
) -> list[dict[str, Any]]:
    frame_links = _q(schema) + "." + _q("frame_catalog_links")
    sql = f"""
WITH {_legacy_cte(schema, pipeline_runs_schema=pipeline_runs_schema)}
SELECT source_facts.catalog_name,
       count(frame_link.frame_catalog_link_id)::bigint AS frame_link_count,
       count(frame_link.frame_catalog_link_id) FILTER (
           WHERE NOT source_facts.is_durable_keep
       )::bigint AS removable_frame_links
FROM source_facts
LEFT JOIN {frame_links} AS frame_link
  ON frame_link.catalog_source_id = source_facts.catalog_source_id
GROUP BY source_facts.catalog_name
ORDER BY source_facts.catalog_name
"""
    rows = connection.execute(text(sql), dict(parameters)).mappings().all()
    return [dict(row) for row in rows]


def _durable_keep_set(
    connection,
    schema: str,
    parameters: Mapping[str, Any],
    foreign_key_reasons: Mapping[int, Sequence[str]],
    *,
    pipeline_runs_schema: str | None = None,
) -> list[dict[str, Any]]:
    sql = f"""
WITH {_legacy_cte(schema, pipeline_runs_schema=pipeline_runs_schema)}
SELECT catalog_source_id, catalog_name, source_id,
       array_remove(ARRAY[
           CASE WHEN has_object_link THEN 'OBJECT_CATALOG_LINK' END,
           CASE WHEN has_accepted_match THEN 'ACCEPTED_DETECTION_CATALOG_MATCH' END,
            CASE WHEN has_reference_identity THEN 'REFERENCE_IDENTITY_LINK' END,
            CASE WHEN is_explicit_target THEN 'EXPLICIT_TARGET' END,
            CASE WHEN has_pipeline_run_target THEN 'PIPELINE_RUN_TARGET_SOURCE_ID' END,
            CASE WHEN has_incoming_fk_reference THEN 'INCOMING_FOREIGN_KEY_REFERENCE' END
       ], NULL) AS keep_reasons
FROM source_facts
WHERE is_durable_keep
ORDER BY catalog_name, source_id, catalog_source_id
"""
    rows = connection.execute(text(sql), dict(parameters)).mappings().all()
    result = []
    for row in rows:
        source_id = int(row["catalog_source_id"])
        reasons = [str(reason) for reason in (row.get("keep_reasons") or ())]
        reasons.extend(str(reason) for reason in foreign_key_reasons.get(source_id, ()))
        result.append(
            {
                "catalog_source_id": source_id,
                "catalog_name": str(row["catalog_name"]),
                "source_id": str(row["source_id"]),
                "reasons": sorted(set(reasons)),
            }
        )
    return result


def _count(connection, sql: str, parameters: Mapping[str, Any] | None = None) -> int:
    return int(connection.execute(text(sql), dict(parameters or {})).scalar_one())


def _relation_size(connection, relation: Relation) -> dict[str, Any]:
    qualified_name = f"{relation.schema}.{relation.name}"
    size_row = connection.execute(
        text(
            """
SELECT pg_relation_size(to_regclass(:qualified_name))::bigint AS table_bytes,
       pg_indexes_size(to_regclass(:qualified_name))::bigint AS index_bytes,
       pg_total_relation_size(to_regclass(:qualified_name))::bigint AS total_bytes
"""
        ),
        {"qualified_name": qualified_name},
    ).mappings().one()
    if size_row.get("total_bytes") is None:
        raise LegacyGaiaCatalogCleanupError(
            f"could not inspect relation size for {relation.schema}.{relation.name}"
        )
    index_rows = connection.execute(
        text(
            """
SELECT index_class.relname AS index_name,
       pg_relation_size(index_class.oid)::bigint AS bytes
FROM pg_catalog.pg_index AS index_row
JOIN pg_catalog.pg_class AS index_class ON index_class.oid = index_row.indexrelid
WHERE index_row.indrelid = to_regclass(:qualified_name)
ORDER BY index_class.relname
"""
        ),
        {"qualified_name": qualified_name},
    ).mappings().all()
    table_bytes = int(size_row["table_bytes"])
    index_bytes = int(size_row["index_bytes"])
    total_bytes = int(size_row["total_bytes"])
    return {
        "schema": relation.schema,
        "table": relation.name,
        "row_storage_bytes": table_bytes,
        "indexes_bytes": index_bytes,
        "total_bytes_including_toast": total_bytes,
        "toast_and_other_bytes": max(0, total_bytes - table_bytes - index_bytes),
        "indexes": [
            {"index_name": str(row["index_name"]), "bytes": int(row["bytes"])}
            for row in index_rows
        ],
    }


def _legacy_query_coverage(connection, pipeline_runs: Relation) -> dict[str, int]:
    table = _qualified(pipeline_runs)
    row = connection.execute(
        text(
            f"""
SELECT count(*) FILTER (
           WHERE recipe_json ? 'gaia_query_coverage_v1'
       )::bigint AS runs_with_legacy_coverage_key,
       count(*) FILTER (
           WHERE recipe_json ? 'gaia_query_coverage_v1'
             AND left(coalesce(catalog_name, ''), char_length(:legacy_prefix)) = :legacy_prefix
       )::bigint AS legacy_catalog_runs_with_key,
       count(*) FILTER (
           WHERE recipe_json ? 'gaia_query_coverage_v1'
             AND left(coalesce(catalog_name, ''), char_length(:legacy_prefix)) = :legacy_prefix
             AND recipe_json #>> '{{gaia_query_coverage_v1,coverage_complete}}' = 'true'
       )::bigint AS legacy_catalog_runs_complete,
       count(*) FILTER (
           WHERE recipe_json ? 'gaia_query_coverage_v1'
             AND left(coalesce(catalog_name, ''), char_length(:legacy_prefix)) = :legacy_prefix
             AND recipe_json #>> '{{gaia_query_coverage_v1,coverage_complete}}' = 'false'
       )::bigint AS legacy_catalog_runs_incomplete
FROM {table}
"""
        ),
        {"legacy_prefix": LEGACY_CATALOG_PREFIX},
    ).mappings().one()
    total = int(row["legacy_catalog_runs_with_key"])
    complete = int(row["legacy_catalog_runs_complete"])
    incomplete = int(row["legacy_catalog_runs_incomplete"])
    return {
        "pipeline_runs_with_key": int(row["runs_with_legacy_coverage_key"]),
        "legacy_catalog_runs_with_key": total,
        "legacy_catalog_runs_complete": complete,
        "legacy_catalog_runs_incomplete": incomplete,
        "legacy_catalog_runs_unknown_completeness": total - complete - incomplete,
    }


def _size_estimate(
    *,
    relation_sizes: Mapping[str, Mapping[str, Any]],
    total_rows: Mapping[str, int],
    eligible_rows: Mapping[str, int],
) -> dict[str, Any]:
    per_relation: dict[str, Any] = {}
    sum_estimate = 0
    for table in ("frame_catalog_links", "catalog_sources"):
        size = relation_sizes[table]
        denominator = int(total_rows[table])
        numerator = min(int(eligible_rows[table]), denominator)
        fraction = (numerator / denominator) if denominator else 0.0
        table_estimate = int(int(size["row_storage_bytes"]) * fraction)
        index_estimate = int(int(size["indexes_bytes"]) * fraction)
        total_estimate = int(int(size["total_bytes_including_toast"]) * fraction)
        sum_estimate += total_estimate
        per_relation[table] = {
            "eligible_rows": numerator,
            "all_rows": denominator,
            "estimated_reusable_row_storage_bytes": table_estimate,
            "estimated_reusable_index_bytes": index_estimate,
            "estimated_reusable_total_bytes": total_estimate,
        }
    return {
        "method": "current relation bytes multiplied by eligible-row fraction",
        "immediately_freed_bytes_by_delete": 0,
        "estimated_reusable_bytes": sum_estimate,
        "by_relation": per_relation,
        "caveat": (
            "This is a proportional estimate, not a PostgreSQL guarantee. DELETE normally "
            "makes space reusable inside relations but does not return it to the OS; no VACUUM "
            "FULL, REINDEX, or other DDL is part of this command."
        ),
    }


def _legacy_filter(alias: str = "cs") -> str:
    return (
        f"left({alias}.catalog_name, char_length(:legacy_prefix)) = :legacy_prefix "
        f"AND {alias}.catalog_name <> :canonical_catalog_name"
    )


def _explicit_target_condition(alias: str = "cs") -> str:
    return (
        f"({alias}.catalog_source_id = ANY(CAST(:keep_catalog_source_ids AS bigint[])) "
        f"OR {alias}.source_id = ANY(CAST(:keep_source_ids AS text[])))"
    )


def _durable_keep_condition(
    schema: str,
    alias: str = "cs",
    *,
    pipeline_runs_schema: str | None = None,
) -> str:
    object_links = _q(schema) + "." + _q("object_catalog_links")
    detection_matches = _q(schema) + "." + _q("detection_catalog_matches")
    reference_sources = _q(schema) + "." + _q("reference_photometry_sources")
    return " OR ".join(
        (
            f"EXISTS (SELECT 1 FROM {object_links} AS object_link "
            f"WHERE object_link.catalog_source_id = {alias}.catalog_source_id)",
            f"EXISTS (SELECT 1 FROM {detection_matches} AS detection_match "
            f"WHERE detection_match.catalog_source_id = {alias}.catalog_source_id "
            "AND detection_match.match_status = 'MATCHED')",
            f"EXISTS (SELECT 1 FROM {reference_sources} AS reference_source "
            f"WHERE reference_source.identity_catalog_source_id = {alias}.catalog_source_id)",
            _explicit_target_condition(alias),
            _pipeline_run_target_condition(
                schema, alias, pipeline_runs_schema=pipeline_runs_schema
            ),
        )
    )


def _delete_frame_links_sql(
    schema: str, *, pipeline_runs_schema: str | None = None
) -> str:
    frame_links = _q(schema) + "." + _q("frame_catalog_links")
    catalog_sources = _q(schema) + "." + _q("catalog_sources")
    return f"""
DELETE FROM {frame_links} AS frame_link
USING {catalog_sources} AS cs
WHERE frame_link.catalog_source_id = cs.catalog_source_id
  AND {_legacy_filter('cs')}
  AND NOT ({_durable_keep_condition(
      schema, 'cs', pipeline_runs_schema=pipeline_runs_schema
  )})
"""


def _delete_sources_sql(
    schema: str, *, pipeline_runs_schema: str | None = None
) -> str:
    frame_links = _q(schema) + "." + _q("frame_catalog_links")
    detection_matches = _q(schema) + "." + _q("detection_catalog_matches")
    object_links = _q(schema) + "." + _q("object_catalog_links")
    reference_sources = _q(schema) + "." + _q("reference_photometry_sources")
    catalog_sources = _q(schema) + "." + _q("catalog_sources")
    return f"""
DELETE FROM {catalog_sources} AS cs
WHERE {_legacy_filter('cs')}
  AND NOT {_explicit_target_condition('cs')}
  AND NOT ({_pipeline_run_target_condition(
      schema, 'cs', pipeline_runs_schema=pipeline_runs_schema
  )})
  AND NOT EXISTS (
      SELECT 1 FROM {frame_links} AS frame_link
      WHERE frame_link.catalog_source_id = cs.catalog_source_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM {detection_matches} AS detection_match
      WHERE detection_match.catalog_source_id = cs.catalog_source_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM {object_links} AS object_link
      WHERE object_link.catalog_source_id = cs.catalog_source_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM {reference_sources} AS reference_source
      WHERE reference_source.identity_catalog_source_id = cs.catalog_source_id
  )
"""


def audit_legacy_gaia_catalogs(
    connection,
    *,
    apply: bool = False,
    confirm_database: str | None = None,
    confirm_catalog_cleanup: bool = False,
    keep_source_ids: Sequence[str] = (),
    keep_catalog_source_ids: Sequence[int] = (),
) -> dict[str, Any]:
    """Return a read-only audit or run the explicitly gated legacy-row cleanup.

    The caller owns and commits/rolls back the transaction.  In apply mode this
    function takes exclusive table locks on the two target tables and
    ``pipeline_runs``, re-reads FK metadata and protection inventory under those
    locks, then executes only the two narrow DELETE statements built below. Apply
    requires READ COMMITTED isolation so its post-lock inventory sees writers that
    committed while lock acquisition was waiting.
    """

    validate_apply_acknowledgements(
        apply=apply,
        confirm_database=confirm_database,
        confirm_catalog_cleanup=confirm_catalog_cleanup,
    )
    if apply:
        _assert_apply_transaction_isolation(connection)
    else:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    identity = _database_identity(connection)
    _assert_identity_is_complete(identity)
    if apply:
        _assert_apply_target(identity, confirm_database)

    relations = {
        name: _resolve_relation(connection, name)
        for name in ("catalog_sources", "frame_catalog_links", "pipeline_runs")
    }
    if relations["catalog_sources"].schema != relations["frame_catalog_links"].schema:
        raise LegacyGaiaCatalogCleanupError(
            "catalog_sources and frame_catalog_links resolve to different schemas"
        )
    schema = relations["catalog_sources"].schema
    if apply and _has_protected_marker(schema):
        raise LegacyGaiaCatalogCleanupError(
            "apply is forbidden for a protected, frozen, production, or reference schema"
        )

    if apply:
        connection.execute(
            text(
                "LOCK TABLE "
                f"{_qualified(relations['catalog_sources'])}, "
                f"{_qualified(relations['frame_catalog_links'])}, "
                f"{_qualified(relations['pipeline_runs'])} "
                "IN EXCLUSIVE MODE"
            )
        )

    foreign_keys = _read_incoming_fks(connection)
    expected_fks_present, missing_expected_fks = _expected_fk_shapes_present(
        foreign_keys, expected_schema=schema
    )
    fk_counts: list[int | None] = [
        _foreign_key_reference_count(connection, fk, relations) for fk in foreign_keys
    ]
    unknown_fks = [
        fk
        for fk in foreign_keys
        if fk.role(expected_schema=schema) is None
    ]
    if apply:
        _assert_apply_target(identity, confirm_database)
        if unknown_fks:
            names = ", ".join(fk.constraint_name for fk in unknown_fks)
            raise LegacyGaiaCatalogCleanupError(
                f"apply blocked by unknown or unvalidated incoming FK(s): {names}"
            )
        if not expected_fks_present:
            raise LegacyGaiaCatalogCleanupError(
                "apply blocked because expected model FK relationships are missing"
            )

    foreign_key_reasons: dict[int, set[str]] = {}
    for fk, count in zip(foreign_keys, fk_counts, strict=True):
        if fk.role(expected_schema=schema) is not None or count in (None, 0):
            continue
        source_references = _foreign_key_source_references(connection, fk, relations)
        if source_references is None:
            continue
        reason = f"INCOMING_FK:{fk.child_schema}.{fk.child_table}.{fk.constraint_name}"
        for source_id, reference_count in source_references.items():
            if reference_count > 0:
                foreign_key_reasons.setdefault(source_id, set()).add(reason)

    parameters = _plan_parameters(
        keep_source_ids=keep_source_ids,
        keep_catalog_source_ids=keep_catalog_source_ids,
        foreign_key_source_ids=sorted(foreign_key_reasons),
    )
    pipeline_runs_schema = relations["pipeline_runs"].schema
    source_rows = _source_summary(
        connection, schema, parameters, pipeline_runs_schema=pipeline_runs_schema
    )
    link_rows = _frame_link_summary(
        connection, schema, parameters, pipeline_runs_schema=pipeline_runs_schema
    )
    keep_set = _durable_keep_set(
        connection,
        schema,
        parameters,
        foreign_key_reasons,
        pipeline_runs_schema=pipeline_runs_schema,
    )
    link_by_catalog = {str(row["catalog_name"]): row for row in link_rows}
    fk_cleanup_blocked = bool(unknown_fks) or not expected_fks_present

    catalogs: list[dict[str, Any]] = []
    known_removable_source_count = 0
    eligible_source_count = 0
    candidate_frame_link_count = 0
    eligible_frame_link_count = 0
    for row in source_rows:
        name = str(row["catalog_name"])
        link_row = link_by_catalog.get(name, {})
        known_removable = int(row["known_removable_sources"])
        removable_links = int(link_row.get("removable_frame_links", 0))
        known_removable_source_count += known_removable
        candidate_frame_link_count += removable_links
        eligible_sources = 0 if fk_cleanup_blocked else known_removable
        eligible_links = 0 if fk_cleanup_blocked else removable_links
        eligible_source_count += eligible_sources
        eligible_frame_link_count += eligible_links
        catalogs.append(
            {
                "catalog_name": name,
                "catalog_source_count": int(row["source_count"]),
                "frame_catalog_link_count": int(link_row.get("frame_link_count", 0)),
                "durable_keep_source_count": int(row["durable_keep_sources"]),
                "durable_keep_source_counts_by_reason": {
                    "object_catalog_link": int(row["object_link_sources"]),
                    "accepted_detection_catalog_match": int(row["accepted_match_sources"]),
                    "reference_identity_link": int(row["reference_identity_sources"]),
                    "explicit_target": int(row["explicit_target_sources"]),
                    "pipeline_run_target_source_id": int(
                        row["pipeline_run_target_sources"]
                    ),
                    "other_incoming_foreign_key": int(row["other_fk_protected_sources"]),
                },
                "removable_frame_link_count": eligible_links,
                "candidate_frame_link_count_before_unknown_fk_gate": removable_links,
                "removable_catalog_source_count": eligible_sources,
                "candidate_catalog_source_count_before_unknown_fk_gate": known_removable,
                "source_rows_blocked_by_detection_match": int(
                    row["blocked_by_detection_match_sources"]
                ),
            }
        )

    total_catalog_sources = _count(
        connection,
        f"SELECT count(*)::bigint FROM {_qualified(relations['catalog_sources'])}",
    )
    total_frame_links = _count(
        connection,
        f"SELECT count(*)::bigint FROM {_qualified(relations['frame_catalog_links'])}",
    )
    canonical_source_count = _count(
        connection,
        f"SELECT count(*)::bigint FROM {_qualified(relations['catalog_sources'])} "
        "WHERE catalog_name = :canonical_catalog_name",
        {"canonical_catalog_name": CANONICAL_CATALOG_NAME},
    )

    relation_specs: dict[str, Relation] = {
        name: relations[name] for name in ("catalog_sources", "frame_catalog_links", "pipeline_runs")
    }
    for fk in foreign_keys:
        relation_specs.setdefault(
            f"{fk.child_schema}.{fk.child_table}",
            Relation(
                oid=0,
                schema=fk.child_schema,
                name=fk.child_table,
                kind="r",
            ),
        )
    relation_sizes = {
        key: _relation_size(connection, relation)
        for key, relation in relation_specs.items()
    }
    # The two estimates only use the two target relations, not dependent-table bytes.
    target_relation_sizes = {
        table: relation_sizes[table]
        for table in ("catalog_sources", "frame_catalog_links")
    }
    estimated_reclaimable = _size_estimate(
        relation_sizes=target_relation_sizes,
        total_rows={
            "catalog_sources": total_catalog_sources,
            "frame_catalog_links": total_frame_links,
        },
        eligible_rows={
            "catalog_sources": eligible_source_count,
            "frame_catalog_links": eligible_frame_link_count,
        },
    )

    fk_report = [
        fk.to_dict(expected_schema=schema, blocker_count=count)
        for fk, count in zip(foreign_keys, fk_counts, strict=True)
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "mode": "APPLY" if apply else "DRY_RUN_READ_ONLY",
        "database": identity.to_dict(),
        "scope": {
            "legacy_catalog_prefix": LEGACY_CATALOG_PREFIX,
            "canonical_catalog_excluded": CANONICAL_CATALOG_NAME,
            "explicit_keep_source_ids": [str(value) for value in keep_source_ids],
            "explicit_keep_catalog_source_ids": [int(value) for value in keep_catalog_source_ids],
        },
        "catalogs": catalogs,
        "catalog_source_count_all_catalogs": total_catalog_sources,
        "frame_catalog_link_count_all_catalogs": total_frame_links,
        "canonical_gaia_dr3_source_count": canonical_source_count,
        "durable_keep_set": {
            "source_count": len(keep_set),
            "sources": keep_set,
            "definition": [
                "ObjectCatalogLink",
                "DetectionCatalogMatch with match_status=MATCHED",
                "ReferencePhotometrySource.identity_catalog_source_id",
                "explicit source/catalog_source_id targets",
                (
                    "PipelineRun.recipe_json.quality_policy.target_source_id as a string, "
                    "matched within the exact same catalog_name"
                ),
                "other actual incoming FKs discovered in PostgreSQL catalogs (conservatively kept)",
            ],
            "ordinary_frame_catalog_links_are_not_keep_reasons": True,
        },
        "removable_frame_links": {
            "count": eligible_frame_link_count,
            "candidate_count_before_unknown_fk_gate": candidate_frame_link_count,
            "definition": "legacy frame projections for sources outside the durable keep-set",
        },
        "removable_catalog_sources": {
            "count": eligible_source_count,
            "candidate_count_before_unknown_fk_gate": known_removable_source_count,
            "definition": "legacy sources with no durable keep reason or remaining known FK reference",
        },
        "incoming_foreign_keys": fk_report,
        "foreign_key_schema": {
            "expected_model_relationships_present": expected_fks_present,
            "missing_expected_relationships": missing_expected_fks,
            "unknown_or_unvalidated_count": len(unknown_fks),
            "apply_blocked": bool(unknown_fks) or not expected_fks_present,
        },
        "table_and_index_sizes": relation_sizes,
        "estimated_reclaimable_bytes": estimated_reclaimable,
        "legacy_gaia_query_coverage_v1": _legacy_query_coverage(
            connection, relations["pipeline_runs"]
        ),
        "apply_safety": {
            "requires_all_three_flags": True,
            "unknown_or_unvalidated_fk_fails_closed": True,
            "protected_frozen_production_target_fails_closed": True,
            "canonical_gaia_dr3_modified": False,
            "objects_measurements_science_products_modified": False,
        },
    }

    if apply:
        if fk_cleanup_blocked:
            raise LegacyGaiaCatalogCleanupError(
                "apply blocked because the incoming-FK inventory is incomplete"
            )
        expected_link_deletes = eligible_frame_link_count
        expected_source_deletes = eligible_source_count
        frame_result = connection.execute(
            text(
                _delete_frame_links_sql(
                    schema, pipeline_runs_schema=pipeline_runs_schema
                )
            ),
            parameters,
        )
        source_result = connection.execute(
            text(
                _delete_sources_sql(
                    schema, pipeline_runs_schema=pipeline_runs_schema
                )
            ),
            parameters,
        )
        deleted_links = int(frame_result.rowcount)
        deleted_sources = int(source_result.rowcount)
        if deleted_links != expected_link_deletes or deleted_sources != expected_source_deletes:
            raise LegacyGaiaCatalogCleanupError(
                "delete row counts differ from the locked dry-run plan; transaction must roll back"
            )
        report["applied"] = {
            "deleted_frame_catalog_links": deleted_links,
            "deleted_catalog_sources": deleted_sources,
            "deleted_rows_total": deleted_links + deleted_sources,
            "canonical_gaia_dr3_deleted": False,
            "objects_measurements_science_products_deleted": False,
            "transaction_note": "caller must commit this transaction; any error must roll it back",
        }
    return report


__all__ = [
    "CANONICAL_CATALOG_NAME",
    "IncomingForeignKey",
    "LEGACY_CATALOG_PREFIX",
    "LegacyGaiaCatalogCleanupError",
    "SourceDisposition",
    "audit_legacy_gaia_catalogs",
    "classify_source_disposition",
    "is_legacy_catalog_name",
    "validate_apply_acknowledgements",
]
