from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json
import warnings

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier.pipeline.recipe_contracts import (
    MISSING_PROVENANCE,
    READY_STAGE3_RECIPE,
    RECIPE_MISMATCH,
    assess_stage2_recipe_json,
)


SERIES_COLUMNS: tuple[str, ...] = (
    "series_id",
    "instrument_unit_id",
    "filter_name",
    "catalog_name",
    "n_frames",
    "calibration_version_id",
    "calibration_name",
    "n_science_points",
    "n_science_frames",
    "n_objects",
    "n_usable_objects",
    "status",
    "pipeline_run_id",
    "pipeline_status",
    "recipe_compatibility",
    "recipe_mismatch_reasons",
    "recipe_compatibility_notes",
    "provenance_source",
)

PAIR_COLUMNS: tuple[str, ...] = (
    "left_series_id",
    "right_series_id",
    "n_common_objects",
    "same_instrument_unit",
    "same_filter",
    "same_catalog",
    "eligible",
    "reason",
)


@dataclass(frozen=True)
class InterseriesSeriesCandidate:
    series_id: int
    instrument_unit_id: int | None
    filter_name: str | None
    catalog_name: str | None
    n_frames: int
    calibration_version_id: int | None
    calibration_name: str | None
    n_science_points: int
    n_science_frames: int
    n_objects: int
    n_usable_objects: int
    status: str
    pipeline_run_id: int | None = None
    pipeline_status: str | None = None
    recipe_compatibility: str | None = None
    recipe_mismatch_reasons: tuple[str, ...] = ()
    recipe_compatibility_notes: tuple[str, ...] = ()
    provenance_source: str | None = None
    time_start: str | None = None
    time_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InterseriesPairCandidate:
    left_series_id: int
    right_series_id: int
    n_common_objects: int
    same_instrument_unit: bool
    same_filter: bool
    same_catalog: bool
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InterseriesPlan:
    field_id: int
    field_name: str | None
    min_points_per_object: int
    min_common_objects: int
    series: list[InterseriesSeriesCandidate]
    pairs: list[InterseriesPairCandidate]
    connected_components: list[list[int]]
    recommended_reference_series_id: int | None
    status: str

    @property
    def n_series(self) -> int:
        return len(self.series)

    @property
    def n_ready_series(self) -> int:
        return sum(1 for row in self.series if row.status == "READY")

    @property
    def n_eligible_pairs(self) -> int:
        return sum(1 for row in self.pairs if row.eligible)

    @property
    def largest_component_size(self) -> int:
        return max((len(component) for component in self.connected_components), default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "field_name": self.field_name,
            "min_points_per_object": self.min_points_per_object,
            "min_common_objects": self.min_common_objects,
            "n_series": self.n_series,
            "n_ready_series": self.n_ready_series,
            "n_eligible_pairs": self.n_eligible_pairs,
            "largest_component_size": self.largest_component_size,
            "recommended_reference_series_id": self.recommended_reference_series_id,
            "status": self.status,
            "series": [row.to_dict() for row in self.series],
            "pairs": [row.to_dict() for row in self.pairs],
            "connected_components": self.connected_components,
        }


def interseries_series_columns() -> list[str]:
    return list(SERIES_COLUMNS)


def interseries_pair_columns() -> list[str]:
    return list(PAIR_COLUMNS)


def export_interseries_plan_json(plan: InterseriesPlan, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def build_interseries_plan(
    session: Session,
    *,
    field_id: int | None = None,
    field_name: str | None = None,
    instrument_unit_id: int | None = None,
    filter_name: str | None = None,
    catalog_name: str | None = None,
    min_points_per_object: int = 5,
    min_common_objects: int = 20,
    required_stage2_recipe: str | None = None,
    science_calibration_version_ids: list[int] | None = None,
) -> InterseriesPlan:
    """Inspect whether one field is ready for Stage-3 inter-series calibration.

    This is deliberately a read-only Stage-3 foundation. It selects current
    SCIENCE calibration-product alias heads (or validates an explicit head set),
    reports missing materialization explicitly, counts objects with enough epochs
    inside each series, and constructs a pairwise overlap graph. No offset is
    estimated or persisted here.

    Direct object-id overlap is safe when series resolve catalog sources through
    the same internal object identity.  Since 0.7.0.2 this identity is catalog-
    independent and catalog provenance is represented explicitly by
    ``object_catalog_links``.
    """

    min_points_per_object = max(1, int(min_points_per_object))
    min_common_objects = max(1, int(min_common_objects))
    resolved_field_id, resolved_field_name = _resolve_field(
        session,
        field_id=field_id,
        field_name=field_name,
    )
    raw_series = _load_series_candidates(
        session,
        field_id=resolved_field_id,
        instrument_unit_id=instrument_unit_id,
        filter_name=filter_name,
        catalog_name=catalog_name,
        science_calibration_version_ids=science_calibration_version_ids,
    )

    calibration_ids = sorted(
        {
            int(row["calibration_version_id"])
            for row in raw_series
            if row.get("calibration_version_id") is not None
        }
    )
    science_stats = _load_science_stats(session, calibration_ids)
    object_point_counts = _load_object_point_counts(session, calibration_ids)

    usable_by_calibration: dict[int, set[int]] = {}
    for calibration_version_id, counts in object_point_counts.items():
        usable_by_calibration[calibration_version_id] = {
            object_id
            for object_id, n_points in counts.items()
            if n_points >= min_points_per_object
        }

    series: list[InterseriesSeriesCandidate] = []
    for row in raw_series:
        cv_id_value = row.get("calibration_version_id")
        cv_id = int(cv_id_value) if cv_id_value is not None else None
        stats = science_stats.get(cv_id or -1, {})
        usable = usable_by_calibration.get(cv_id or -1, set())
        recipe_compatibility: str | None = None
        recipe_mismatch_reasons: tuple[str, ...] = ()
        recipe_compatibility_notes: tuple[str, ...] = ()
        provenance_source: str | None = None
        selection_status = row.get("_selection_status")
        if selection_status is not None:
            status = str(selection_status)
        elif cv_id is None:
            status = (
                "NO_MATCHING_SCIENCE_CALIBRATION"
                if catalog_name is not None
                else "NO_SCIENCE_CALIBRATION"
            )
        elif int(stats.get("n_science_points") or 0) <= 0:
            status = "NO_SCIENCE_ROWS"
        elif not usable:
            status = "NO_USABLE_OBJECTS"
        else:
            status = "READY"
        if cv_id is not None and required_stage2_recipe is not None:
            compatibility = assess_stage2_recipe_json(
                row.get("recipe_json"),
                required_recipe=required_stage2_recipe,
            )
            recipe_compatibility = compatibility.status
            recipe_mismatch_reasons = compatibility.mismatch_reasons
            recipe_compatibility_notes = compatibility.compatibility_notes
            provenance_source = compatibility.provenance_source
            if status == "READY":
                if compatibility.status == RECIPE_MISMATCH:
                    status = RECIPE_MISMATCH
                elif compatibility.status == MISSING_PROVENANCE:
                    status = MISSING_PROVENANCE
                elif compatibility.status != READY_STAGE3_RECIPE:
                    raise ValueError(
                        f"Unsupported Stage-2 recipe compatibility status: {compatibility.status}"
                    )
        series.append(
            InterseriesSeriesCandidate(
                series_id=int(row["series_id"]),
                instrument_unit_id=_int_or_none(row.get("instrument_unit_id")),
                filter_name=_str_or_none(row.get("filter_name")),
                catalog_name=_str_or_none(row.get("catalog_name")),
                n_frames=int(row.get("n_frames") or 0),
                calibration_version_id=cv_id,
                calibration_name=_str_or_none(row.get("calibration_name")),
                n_science_points=int(stats.get("n_science_points") or 0),
                n_science_frames=int(stats.get("n_science_frames") or 0),
                n_objects=int(stats.get("n_objects") or 0),
                n_usable_objects=len(usable),
                status=status,
                pipeline_run_id=_int_or_none(row.get("pipeline_run_id")),
                pipeline_status=_str_or_none(row.get("pipeline_status")),
                recipe_compatibility=recipe_compatibility,
                recipe_mismatch_reasons=recipe_mismatch_reasons,
                recipe_compatibility_notes=recipe_compatibility_notes,
                provenance_source=provenance_source,
                time_start=_iso_or_none(row.get("time_start")),
                time_end=_iso_or_none(row.get("time_end")),
            )
        )

    pairs = _build_pair_candidates(
        series,
        usable_by_calibration=usable_by_calibration,
        min_common_objects=min_common_objects,
    )
    components = _eligible_connected_components(series, pairs)
    reference_series_id = _choose_reference_series(series, components)
    status = _plan_status(series, pairs, components)
    return InterseriesPlan(
        field_id=resolved_field_id,
        field_name=resolved_field_name,
        min_points_per_object=min_points_per_object,
        min_common_objects=min_common_objects,
        series=series,
        pairs=pairs,
        connected_components=components,
        recommended_reference_series_id=reference_series_id,
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
        if field_name and str(row.get("field_name") or "") != str(field_name):
            raise ValueError(
                f"field_id={field_id} resolves to {row.get('field_name')!r}, not {field_name!r}"
            )
        return int(row["field_id"]), _str_or_none(row.get("field_name"))

    name = str(field_name).strip()
    rows = session.execute(
        text(
            """
            SELECT field_id, field_name
            FROM fields
            WHERE lower(field_name) = lower(:field_name)
            ORDER BY field_id
            """
        ),
        {"field_name": name},
    ).mappings().all()
    if not rows:
        raise ValueError(f"No field found with field_name={name!r}")
    if len(rows) > 1:
        ids = ", ".join(str(int(row["field_id"])) for row in rows)
        raise ValueError(f"Ambiguous field_name={name!r}; matching field_id values: {ids}")
    row = rows[0]
    return int(row["field_id"]), _str_or_none(row.get("field_name"))


def _load_series_candidates(
    session: Session,
    *,
    field_id: int,
    instrument_unit_id: int | None,
    filter_name: str | None,
    catalog_name: str | None,
    science_calibration_version_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    explicit_ids = _normalize_science_calibration_version_ids(science_calibration_version_ids)
    clauses = ["fs.field_id = :field_id"]
    params: dict[str, Any] = {"field_id": int(field_id)}
    if filter_name is not None:
        clauses.append("lower(COALESCE(fs.filter_name, '')) = lower(:filter_name)")
        params["filter_name"] = str(filter_name)
    if instrument_unit_id is not None:
        params["instrument_unit_id"] = int(instrument_unit_id)
        clauses.append(
            """
            (
                fs.instrument_unit_id = :instrument_unit_id
                OR (
                    fs.instrument_unit_id IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM calibration_product_aliases scoped_alias
                        JOIN calibration_versions scoped_cv
                          ON scoped_cv.calibration_version_id = scoped_alias.head_calibration_version_id
                        WHERE scoped_cv.series_id = fs.series_id
                          AND scoped_cv.product_role = 'SCIENCE'
                          AND scoped_cv.instrument_unit_id = :instrument_unit_id
                    )
                )
                OR (
                    fs.instrument_unit_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM calibration_product_aliases live_alias
                        JOIN calibration_versions live_cv
                          ON live_cv.calibration_version_id = live_alias.head_calibration_version_id
                        WHERE live_cv.series_id = fs.series_id
                          AND live_cv.product_role = 'SCIENCE'
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM calibration_versions legacy_cv
                        WHERE legacy_cv.series_id = fs.series_id
                          AND legacy_cv.product_role = 'SCIENCE'
                          AND legacy_cv.instrument_unit_id = :instrument_unit_id
                    )
                )
            )
            """
        )

    sql = f"""
        SELECT
            fs.series_id,
            fs.field_id AS series_field_id,
            fs.instrument_unit_id AS series_instrument_unit_id,
            fs.filter_name,
            fs.n_frames,
            fs.time_start,
            fs.time_end,
            science_head.alias_id,
            science_head.logical_name AS alias_logical_name,
            science_head.calibration_version_id,
            science_head.calibration_name,
            science_head.calibration_field_id,
            science_head.calibration_series_id,
            science_head.calibration_product_role,
            science_head.calibration_instrument_unit_id,
            science_head.catalog_name,
            science_head.pipeline_run_id,
            science_head.pipeline_status,
            science_head.recipe_json,
            EXISTS (
                SELECT 1
                FROM calibration_product_aliases live_alias
                JOIN calibration_versions live_cv
                  ON live_cv.calibration_version_id = live_alias.head_calibration_version_id
                WHERE live_cv.series_id = fs.series_id
                  AND live_cv.product_role = 'SCIENCE'
            ) AS has_science_alias_head,
            (
                NOT EXISTS (
                    SELECT 1
                    FROM calibration_product_aliases live_alias
                    JOIN calibration_versions live_cv
                      ON live_cv.calibration_version_id = live_alias.head_calibration_version_id
                    WHERE live_cv.series_id = fs.series_id
                      AND live_cv.product_role = 'SCIENCE'
                )
                AND EXISTS (
                    SELECT 1
                    FROM calibration_versions legacy_cv
                    WHERE legacy_cv.series_id = fs.series_id
                      AND legacy_cv.product_role = 'SCIENCE'
                )
            ) AS has_legacy_science_products
        FROM field_series fs
        LEFT JOIN LATERAL (
            SELECT
                cpa.alias_id,
                cpa.logical_name,
                cv.calibration_version_id,
                cv.name AS calibration_name,
                cv.field_id AS calibration_field_id,
                cv.series_id AS calibration_series_id,
                cv.product_role AS calibration_product_role,
                cv.instrument_unit_id AS calibration_instrument_unit_id,
                cv.pipeline_run_id,
                pr.status AS pipeline_status,
                pr.recipe_json,
                COALESCE(pr.catalog_name, cv.used_catalogs ->> 'catalog_name') AS catalog_name
            FROM calibration_product_aliases cpa
            JOIN calibration_versions cv
              ON cv.calibration_version_id = cpa.head_calibration_version_id
            LEFT JOIN pipeline_runs pr ON pr.pipeline_run_id = cv.pipeline_run_id
            WHERE cv.series_id = fs.series_id
              AND cv.product_role = 'SCIENCE'
        ) AS science_head ON TRUE
        WHERE {' AND '.join(clauses)}
        ORDER BY fs.time_start NULLS LAST, fs.series_id,
                 science_head.logical_name, science_head.calibration_version_id
    """
    raw_rows = [dict(row) for row in session.execute(text(sql), params).mappings().all()]
    grouped: dict[int, dict[str, Any]] = {}
    heads_by_series: dict[int, list[dict[str, Any]]] = {}
    for raw in raw_rows:
        series_id = int(raw["series_id"])
        if series_id not in grouped:
            grouped[series_id] = {
                key: raw.get(key)
                for key in (
                    "series_id",
                    "series_field_id",
                    "series_instrument_unit_id",
                    "filter_name",
                    "n_frames",
                    "time_start",
                    "time_end",
                    "has_science_alias_head",
                    "has_legacy_science_products",
                )
            }
            heads_by_series[series_id] = []
        if raw.get("calibration_version_id") is not None:
            heads_by_series[series_id].append(raw)

    if explicit_ids is not None:
        by_id: dict[int, list[dict[str, Any]]] = {}
        for series_id, heads in heads_by_series.items():
            for head in heads:
                by_id.setdefault(int(head["calibration_version_id"]), []).append(
                    {**grouped[series_id], **head}
                )
        selected: dict[int, dict[str, Any]] = {}
        for calibration_version_id in explicit_ids:
            matching_heads = by_id.get(calibration_version_id, [])
            if not matching_heads:
                raise ValueError(
                    f"science_calibration_version_id={calibration_version_id} is not a current "
                    "SCIENCE alias head in the requested field/instrument/filter/catalog scope"
                )
            head = matching_heads[0]
            mismatches = _head_scope_mismatches(
                head,
                field_id=field_id,
                instrument_unit_id=instrument_unit_id,
                filter_name=filter_name,
                catalog_name=catalog_name,
            )
            if mismatches:
                raise ValueError(
                    f"science_calibration_version_id={calibration_version_id} is not a valid "
                    f"current SCIENCE alias head in the requested scope: {', '.join(mismatches)}"
                )
            series_id = int(head["series_id"])
            if series_id in selected:
                previous_id = int(selected[series_id]["calibration_version_id"])
                raise ValueError(
                    f"At most one explicit SCIENCE calibration version may be selected per series; "
                    f"series {series_id} has IDs {previous_id} and {calibration_version_id}"
                )
            selected[series_id] = head

        result: list[dict[str, Any]] = []
        for series_id, base in grouped.items():
            head = selected.get(series_id)
            if head is None:
                if (
                    not bool(base.get("has_science_alias_head"))
                    and bool(base.get("has_legacy_science_products"))
                ):
                    warnings.warn(
                        f"Series {series_id} has legacy SCIENCE products but no current alias head; "
                        "skipping it rather than selecting an unaliased calibration version.",
                        UserWarning,
                        stacklevel=2,
                    )
                    selection_status = "LEGACY_SCIENCE_SKIPPED"
                else:
                    selection_status = "UNSELECTED"
                result.append(_unselected_candidate_row(base, selection_status=selection_status))
            else:
                result.append(_candidate_row(head))
        return result

    result = []
    for series_id, base in grouped.items():
        heads = heads_by_series[series_id]
        for head in heads:
            mismatches = _head_integrity_mismatches(head, field_id=field_id)
            if mismatches:
                raise ValueError(
                    f"Current SCIENCE alias head {head['calibration_version_id']} for series "
                    f"{series_id} violates field/series/instrument scope: {', '.join(mismatches)}"
                )
        scoped_heads = [
            head
            for head in heads
            if not _head_scope_mismatches(
                {**base, **head},
                field_id=field_id,
                instrument_unit_id=instrument_unit_id,
                filter_name=filter_name,
                catalog_name=catalog_name,
            )
        ]
        if len(scoped_heads) > 1:
            rendered = "; ".join(
                f"{head['calibration_version_id']} (logical_name={head.get('alias_logical_name')!r})"
                for head in sorted(
                    scoped_heads,
                    key=lambda item: (
                        int(item["calibration_version_id"]),
                        str(item.get("alias_logical_name") or ""),
                    ),
                )
            )
            raise ValueError(
                f"Series {series_id} has multiple current SCIENCE alias heads in scope: {rendered}. "
                "Supply --science-calibration-version-id explicitly."
            )
        if scoped_heads:
            result.append(_candidate_row({**base, **scoped_heads[0]}))
            continue

        if heads or bool(base.get("has_science_alias_head")):
            selection_status = "NO_MATCHING_SCIENCE_CALIBRATION"
        elif bool(base.get("has_legacy_science_products")):
            selection_status = "LEGACY_SCIENCE_SKIPPED"
            warnings.warn(
                f"Series {series_id} has legacy SCIENCE products but no current alias head; "
                "skipping it rather than selecting an unaliased calibration version.",
                UserWarning,
                stacklevel=2,
            )
        elif catalog_name is not None:
            selection_status = "NO_MATCHING_SCIENCE_CALIBRATION"
        else:
            selection_status = "NO_SCIENCE_CALIBRATION"
        result.append(_unselected_candidate_row(base, selection_status=selection_status))
    return result


def _normalize_science_calibration_version_ids(
    values: list[int] | None,
) -> list[int] | None:
    if values is None:
        return None
    try:
        ids = [int(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise ValueError("science_calibration_version_ids must contain integer IDs") from exc
    if not ids:
        raise ValueError("science_calibration_version_ids must not be empty when explicitly provided")
    if any(value <= 0 for value in ids):
        raise ValueError("science_calibration_version_ids must contain positive IDs")
    if len(set(ids)) != len(ids):
        raise ValueError("science_calibration_version_ids must not contain duplicate IDs")
    return ids


def _head_integrity_mismatches(head: dict[str, Any], *, field_id: int) -> list[str]:
    mismatches = []
    if _int_or_none(head.get("calibration_field_id")) != int(field_id):
        mismatches.append(f"field_id={head.get('calibration_field_id')!r}")
    if _int_or_none(head.get("calibration_series_id")) != int(head["series_id"]):
        mismatches.append(f"series_id={head.get('calibration_series_id')!r}")
    if head.get("calibration_product_role") != "SCIENCE":
        mismatches.append(f"product_role={head.get('calibration_product_role')!r}")
    series_unit = _int_or_none(head.get("series_instrument_unit_id"))
    calibration_unit = _int_or_none(head.get("calibration_instrument_unit_id"))
    if series_unit is not None and calibration_unit is not None and series_unit != calibration_unit:
        mismatches.append(
            "instrument_unit_id differs between field_series "
            f"({series_unit}) and calibration head ({calibration_unit})"
        )
    return mismatches


def _head_scope_mismatches(
    head: dict[str, Any],
    *,
    field_id: int,
    instrument_unit_id: int | None,
    filter_name: str | None,
    catalog_name: str | None,
) -> list[str]:
    mismatches = _head_integrity_mismatches(head, field_id=field_id)
    series_filter = _str_or_none(head.get("filter_name"))
    if filter_name is not None and not _same_optional_text(series_filter, str(filter_name)):
        mismatches.append(f"filter_name={series_filter!r}")
    series_unit = _int_or_none(head.get("series_instrument_unit_id"))
    head_unit = _int_or_none(head.get("calibration_instrument_unit_id"))
    if instrument_unit_id is not None:
        requested_unit = int(instrument_unit_id)
        if series_unit is not None and series_unit != requested_unit:
            mismatches.append(f"series_instrument_unit_id={series_unit!r}")
        if head_unit is not None and head_unit != requested_unit:
            mismatches.append(f"calibration_instrument_unit_id={head_unit!r}")
        if series_unit is None and head_unit is None:
            mismatches.append("instrument_unit_id=None")
    actual_catalog = _str_or_none(head.get("catalog_name"))
    if catalog_name is not None and not _same_optional_text(actual_catalog, str(catalog_name)):
        mismatches.append(f"catalog_name={actual_catalog!r}")
    return mismatches


def _candidate_row(head: dict[str, Any]) -> dict[str, Any]:
    return {
        "series_id": head["series_id"],
        "instrument_unit_id": (
            head.get("series_instrument_unit_id")
            if head.get("series_instrument_unit_id") is not None
            else head.get("calibration_instrument_unit_id")
        ),
        "filter_name": head.get("filter_name"),
        "n_frames": head.get("n_frames"),
        "time_start": head.get("time_start"),
        "time_end": head.get("time_end"),
        "calibration_version_id": head.get("calibration_version_id"),
        "calibration_name": head.get("calibration_name"),
        "catalog_name": head.get("catalog_name"),
        "pipeline_run_id": head.get("pipeline_run_id"),
        "pipeline_status": head.get("pipeline_status"),
        "recipe_json": head.get("recipe_json"),
    }


def _unselected_candidate_row(
    series: dict[str, Any],
    *,
    selection_status: str,
) -> dict[str, Any]:
    return {
        "series_id": series["series_id"],
        "instrument_unit_id": series.get("series_instrument_unit_id"),
        "filter_name": series.get("filter_name"),
        "n_frames": series.get("n_frames"),
        "time_start": series.get("time_start"),
        "time_end": series.get("time_end"),
        "calibration_version_id": None,
        "calibration_name": None,
        "catalog_name": None,
        "pipeline_run_id": None,
        "pipeline_status": None,
        "recipe_json": None,
        "_selection_status": selection_status,
    }


def _load_science_stats(session: Session, calibration_ids: list[int]) -> dict[int, dict[str, int]]:
    if not calibration_ids:
        return {}
    ids = sorted({int(value) for value in calibration_ids})
    rows = session.execute(
        text(
            """
            SELECT
                s.calibration_version_id,
                COUNT(*)::bigint AS n_science_points,
                COUNT(DISTINCT bm.frame_id)::bigint AS n_science_frames,
                COUNT(DISTINCT bm.object_id)::bigint AS n_objects
            FROM science_photometry s
            JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
            WHERE s.calibration_version_id = ANY(:calibration_ids)
            GROUP BY s.calibration_version_id
            """
        ),
        {"calibration_ids": ids},
    ).mappings().all()
    return {
        int(row["calibration_version_id"]): {
            "n_science_points": int(row["n_science_points"] or 0),
            "n_science_frames": int(row["n_science_frames"] or 0),
            "n_objects": int(row["n_objects"] or 0),
        }
        for row in rows
    }


def _load_object_point_counts(session: Session, calibration_ids: list[int]) -> dict[int, dict[int, int]]:
    if not calibration_ids:
        return {}
    ids = sorted({int(value) for value in calibration_ids})
    rows = session.execute(
        text(
            """
            SELECT s.calibration_version_id, bm.object_id, COUNT(*)::bigint AS n_points
            FROM science_photometry s
            JOIN measurements bm ON bm.measurement_id = s.base_measurement_id
            WHERE s.calibration_version_id = ANY(:calibration_ids)
            GROUP BY s.calibration_version_id, bm.object_id
            """
        ),
        {"calibration_ids": ids},
    ).mappings().all()
    result: dict[int, dict[int, int]] = {}
    for row in rows:
        result.setdefault(int(row["calibration_version_id"]), {})[int(row["object_id"])] = int(row["n_points"] or 0)
    return result


def _build_pair_candidates(
    series: list[InterseriesSeriesCandidate],
    *,
    usable_by_calibration: dict[int, set[int]],
    min_common_objects: int,
) -> list[InterseriesPairCandidate]:
    pairs: list[InterseriesPairCandidate] = []
    for i, left in enumerate(series):
        for right in series[i + 1 :]:
            left_objects = usable_by_calibration.get(left.calibration_version_id or -1, set())
            right_objects = usable_by_calibration.get(right.calibration_version_id or -1, set())
            common = len(left_objects & right_objects)
            same_unit = (
                left.instrument_unit_id is not None
                and right.instrument_unit_id is not None
                and left.instrument_unit_id == right.instrument_unit_id
            )
            same_filter = _same_optional_text(left.filter_name, right.filter_name)
            same_catalog = _same_optional_text(left.catalog_name, right.catalog_name)
            reasons: list[str] = []
            if left.status != "READY":
                reasons.append(f"LEFT_{left.status}")
            if right.status != "READY":
                reasons.append(f"RIGHT_{right.status}")
            if not same_unit:
                reasons.append("INSTRUMENT_UNIT_MISMATCH")
            if not same_filter:
                reasons.append("FILTER_MISMATCH")
            if not same_catalog:
                reasons.append("CATALOG_MISMATCH")
            if common < min_common_objects:
                reasons.append("INSUFFICIENT_COMMON_OBJECTS")
            eligible = not reasons
            pairs.append(
                InterseriesPairCandidate(
                    left_series_id=left.series_id,
                    right_series_id=right.series_id,
                    n_common_objects=common,
                    same_instrument_unit=same_unit,
                    same_filter=same_filter,
                    same_catalog=same_catalog,
                    eligible=eligible,
                    reason="OK" if eligible else ",".join(reasons),
                )
            )
    return pairs


def _eligible_connected_components(
    series: list[InterseriesSeriesCandidate],
    pairs: list[InterseriesPairCandidate],
) -> list[list[int]]:
    ready_ids = {row.series_id for row in series if row.status == "READY"}
    adjacency: dict[int, set[int]] = {series_id: set() for series_id in ready_ids}
    for pair in pairs:
        if not pair.eligible:
            continue
        adjacency.setdefault(pair.left_series_id, set()).add(pair.right_series_id)
        adjacency.setdefault(pair.right_series_id, set()).add(pair.left_series_id)
    components: list[list[int]] = []
    remaining = set(adjacency)
    while remaining:
        root = min(remaining)
        stack = [root]
        component: set[int] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency.get(node, ()))
        remaining.difference_update(component)
        components.append(sorted(component))
    components.sort(key=lambda values: (-len(values), values))
    return components


def _choose_reference_series(
    series: list[InterseriesSeriesCandidate],
    components: list[list[int]],
) -> int | None:
    if not components or len(components[0]) < 2:
        return None
    largest = set(components[0])
    candidates = [row for row in series if row.series_id in largest]
    candidates.sort(
        key=lambda row: (
            -row.n_usable_objects,
            -row.n_science_points,
            -row.n_frames,
            row.series_id,
        )
    )
    return candidates[0].series_id if candidates else None


def _plan_status(
    series: list[InterseriesSeriesCandidate],
    pairs: list[InterseriesPairCandidate],
    components: list[list[int]],
) -> str:
    n_ready = sum(1 for row in series if row.status == "READY")
    n_eligible = sum(1 for row in pairs if row.eligible)
    largest = max((len(component) for component in components), default=0)
    if n_ready < 2:
        return "NEED_MORE_READY_SERIES"
    if n_eligible == 0 or largest < 2:
        return "NO_ELIGIBLE_SERIES_OVERLAP"
    if largest >= 3:
        return "READY_FOR_GRAPH_VALIDATION"
    return "READY_FOR_PAIRWISE_MVP"


def _same_optional_text(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return left.strip().casefold() == right.strip().casefold()


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value)
    return text_value if text_value else None


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)
