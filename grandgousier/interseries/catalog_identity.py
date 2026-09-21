from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


SERIES_IDENTITY_COLUMNS: tuple[str, ...] = (
    "series_id",
    "instrument_unit_id",
    "filter_name",
    "n_frames",
    "n_linked_frames",
    "n_catalog_sources",
    "catalog_names",
    "status",
)

GROUP_IDENTITY_COLUMNS: tuple[str, ...] = (
    "field_id",
    "field_name",
    "instrument_unit_id",
    "filter_name",
    "series_ids",
    "common_catalog_names",
    "all_catalog_names",
    "status",
)


@dataclass(frozen=True)
class SeriesCatalogIdentity:
    series_id: int
    instrument_unit_id: int | None
    filter_name: str | None
    n_frames: int
    n_linked_frames: int
    n_catalog_sources: int
    catalog_names: list[str]
    status: str

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["catalog_names"] = ",".join(self.catalog_names)
        return row


@dataclass(frozen=True)
class CatalogIdentityGroup:
    field_id: int
    field_name: str | None
    instrument_unit_id: int | None
    filter_name: str | None
    series_ids: list[int]
    common_catalog_names: list[str]
    all_catalog_names: list[str]
    status: str

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["series_ids"] = ",".join(str(v) for v in self.series_ids)
        row["common_catalog_names"] = ",".join(self.common_catalog_names)
        row["all_catalog_names"] = ",".join(self.all_catalog_names)
        return row


@dataclass(frozen=True)
class CatalogIdentityAudit:
    field_id: int
    field_name: str | None
    series: list[SeriesCatalogIdentity]
    groups: list[CatalogIdentityGroup]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "field_name": self.field_name,
            "status": self.status,
            "series": [asdict(row) for row in self.series],
            "groups": [asdict(row) for row in self.groups],
        }


def series_identity_columns() -> list[str]:
    return list(SERIES_IDENTITY_COLUMNS)


def group_identity_columns() -> list[str]:
    return list(GROUP_IDENTITY_COLUMNS)


def audit_field_catalog_identity(
    session: Session,
    *,
    field_id: int | None = None,
    field_name: str | None = None,
) -> CatalogIdentityAudit:
    resolved_id, resolved_name = _resolve_field(session, field_id=field_id, field_name=field_name)
    rows = session.execute(
        text(
            """
            SELECT
                fs.series_id,
                fs.instrument_unit_id,
                fs.filter_name,
                fs.n_frames,
                COUNT(DISTINCT fcl.frame_id)::int AS n_linked_frames,
                COUNT(DISTINCT fcl.catalog_source_id)::int AS n_catalog_sources,
                COALESCE(
                    array_agg(DISTINCT cs.catalog_name ORDER BY cs.catalog_name)
                        FILTER (WHERE cs.catalog_name IS NOT NULL),
                    ARRAY[]::text[]
                ) AS catalog_names
            FROM field_series fs
            LEFT JOIN field_series_frames fsf
                   ON fsf.series_id = fs.series_id
            LEFT JOIN frame_catalog_links fcl
                   ON fcl.frame_id = fsf.frame_id
            LEFT JOIN catalog_sources cs
                   ON cs.catalog_source_id = fcl.catalog_source_id
            WHERE fs.field_id = :field_id
            GROUP BY
                fs.series_id,
                fs.instrument_unit_id,
                fs.filter_name,
                fs.n_frames
            ORDER BY fs.time_start NULLS LAST, fs.series_id
            """
        ),
        {"field_id": resolved_id},
    ).mappings().all()

    series: list[SeriesCatalogIdentity] = []
    catalog_sets: dict[int, set[str]] = {}
    for raw in rows:
        names = sorted(str(v) for v in (raw.get("catalog_names") or []) if v)
        linked_frames = int(raw.get("n_linked_frames") or 0)
        n_frames = int(raw.get("n_frames") or 0)
        if not names or linked_frames <= 0:
            status = "NO_CURRENT_LINKS"
        elif linked_frames < n_frames:
            status = "PARTIAL_LINK_COVERAGE"
        elif len(names) == 1:
            status = "SINGLE_NAMESPACE"
        else:
            status = "MULTIPLE_NAMESPACES"
        series_id = int(raw["series_id"])
        catalog_sets[series_id] = set(names)
        series.append(
            SeriesCatalogIdentity(
                series_id=series_id,
                instrument_unit_id=_int_or_none(raw.get("instrument_unit_id")),
                filter_name=_str_or_none(raw.get("filter_name")),
                n_frames=n_frames,
                n_linked_frames=linked_frames,
                n_catalog_sources=int(raw.get("n_catalog_sources") or 0),
                catalog_names=names,
                status=status,
            )
        )

    grouped: dict[tuple[int | None, str | None], list[SeriesCatalogIdentity]] = {}
    for row in series:
        grouped.setdefault((row.instrument_unit_id, row.filter_name), []).append(row)

    groups: list[CatalogIdentityGroup] = []
    for (instrument_unit_id, filter_name), members in sorted(
        grouped.items(), key=lambda item: ((item[0][0] or -1), item[0][1] or "")
    ):
        if len(members) < 2:
            continue
        sets = [catalog_sets[row.series_id] for row in members]
        union = set().union(*sets) if sets else set()
        common = set.intersection(*sets) if sets and all(sets) else set()
        if any(not item for item in sets):
            status = "MISSING_LINKS"
        elif common and union == common and len(common) == 1:
            status = "READY_SHARED_NAMESPACE"
        elif common:
            status = "SHARED_WITH_EXTRA_NAMESPACES"
        else:
            status = "NEEDS_RECONCILIATION"
        groups.append(
            CatalogIdentityGroup(
                field_id=resolved_id,
                field_name=resolved_name,
                instrument_unit_id=instrument_unit_id,
                filter_name=filter_name,
                series_ids=sorted(row.series_id for row in members),
                common_catalog_names=sorted(common),
                all_catalog_names=sorted(union),
                status=status,
            )
        )

    if not groups:
        status = "NO_REPEATED_COMPATIBILITY_GROUPS"
    elif all(group.status == "READY_SHARED_NAMESPACE" for group in groups):
        status = "READY_SHARED_IDENTITY"
    elif any(group.status == "NEEDS_RECONCILIATION" for group in groups):
        status = "NEEDS_RECONCILIATION"
    elif any(group.status == "MISSING_LINKS" for group in groups):
        status = "INCOMPLETE"
    else:
        status = "REVIEW_EXTRA_NAMESPACES"

    return CatalogIdentityAudit(
        field_id=resolved_id,
        field_name=resolved_name,
        series=series,
        groups=groups,
        status=status,
    )


def _resolve_field(
    session: Session,
    *,
    field_id: int | None,
    field_name: str | None,
) -> tuple[int, str | None]:
    if field_id is None and not (field_name or "").strip():
        raise ValueError("Provide field_id or field_name")
    if field_id is not None:
        row = session.execute(
            text("SELECT field_id, field_name FROM fields WHERE field_id = :field_id"),
            {"field_id": int(field_id)},
        ).mappings().first()
        if row is None:
            raise ValueError(f"No field found with field_id={field_id}")
        return int(row["field_id"]), _str_or_none(row.get("field_name"))

    row = session.execute(
        text(
            """
            SELECT field_id, field_name
            FROM fields
            WHERE LOWER(field_name) = LOWER(:field_name)
            ORDER BY field_id
            LIMIT 2
            """
        ),
        {"field_name": str(field_name).strip()},
    ).mappings().all()
    if not row:
        raise ValueError(f"No field found with field_name={field_name!r}")
    if len(row) > 1:
        raise ValueError(f"Multiple fields found with field_name={field_name!r}; use --field-id")
    return int(row[0]["field_id"]), _str_or_none(row[0].get("field_name"))


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value)
    return text_value if text_value else None
