from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json

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
) -> InterseriesPlan:
    """Inspect whether one field is ready for Stage-3 inter-series calibration.

    This is deliberately a read-only Stage-3 foundation.  It selects the newest
    published SCIENCE generation by publication chronology for every field series,
    reports missing materialization explicitly, counts objects with enough epochs
    inside each series, and constructs
    a pairwise overlap graph.  No offset is estimated or persisted here.

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
        if cv_id is None:
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
) -> list[dict[str, Any]]:
    clauses = ["fs.field_id = :field_id"]
    science_clauses = [
        "cv.series_id = fs.series_id",
        "cv.product_role = 'SCIENCE'",
    ]
    params: dict[str, Any] = {"field_id": int(field_id)}
    if instrument_unit_id is not None:
        clauses.append("COALESCE(fs.instrument_unit_id, science_cv.instrument_unit_id) = :instrument_unit_id")
        params["instrument_unit_id"] = int(instrument_unit_id)
    if filter_name is not None:
        clauses.append("lower(COALESCE(fs.filter_name, '')) = lower(:filter_name)")
        params["filter_name"] = str(filter_name)
    if catalog_name is not None:
        science_clauses.append(
            "lower(COALESCE(pr.catalog_name, cv.used_catalogs ->> 'catalog_name', '')) = lower(:catalog_name)"
        )
        params["catalog_name"] = str(catalog_name)

    sql = f"""
        SELECT
            fs.series_id,
            COALESCE(fs.instrument_unit_id, science_cv.instrument_unit_id) AS instrument_unit_id,
            fs.filter_name,
            fs.n_frames,
            fs.time_start,
            fs.time_end,
            science_cv.calibration_version_id,
            science_cv.calibration_name,
            science_cv.catalog_name,
            science_cv.pipeline_run_id,
            science_cv.pipeline_status,
            science_cv.recipe_json
        FROM field_series fs
        LEFT JOIN LATERAL (
            SELECT
                cv.calibration_version_id,
                cv.name AS calibration_name,
                cv.instrument_unit_id,
                cv.pipeline_run_id,
                pr.status AS pipeline_status,
                pr.recipe_json,
                COALESCE(pr.catalog_name, cv.used_catalogs ->> 'catalog_name') AS catalog_name
            FROM calibration_versions cv
            LEFT JOIN pipeline_runs pr ON pr.pipeline_run_id = cv.pipeline_run_id
            WHERE {' AND '.join(science_clauses)}
            ORDER BY
                cv.finalized_at DESC NULLS LAST,
                cv.materialized_at DESC NULLS LAST,
                cv.pipeline_run_id DESC NULLS LAST,
                cv.calibration_version_id DESC
            LIMIT 1
        ) AS science_cv ON TRUE
        WHERE {' AND '.join(clauses)}
        ORDER BY fs.time_start NULLS LAST, fs.series_id
    """
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


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
