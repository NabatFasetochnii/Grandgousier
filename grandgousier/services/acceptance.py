from __future__ import annotations

import json
import math
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text

from grandgousier.config import field_archive_mapping, read_config_file
from grandgousier.db.session import make_session_factory
from grandgousier.lightcurves.global_object import export_global_object_light_curve
from grandgousier.resources import resource_path
from grandgousier.services.operations import FieldStatusResult, build_field_status, run_doctor


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    status: str
    dataset: str
    checks: tuple[AcceptanceCheck, ...]
    actual: dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "dataset": self.dataset,
            "checks": [row.to_dict() for row in self.checks],
            "actual": self.actual,
        }


def load_acceptance_spec(dataset: str | Path = "gpx_tf17_2") -> dict[str, Any]:
    candidate = Path(dataset)
    if candidate.is_file():
        path = candidate
    else:
        token = str(dataset).strip()
        filename = token if token.casefold().endswith(".json") else f"{token}.json"
        path = resource_path("acceptance", filename)
    if not path.is_file():
        raise ValueError(f"Acceptance dataset not found: {dataset}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping) or payload.get("format") != "grandgousier_acceptance_v1":
        raise ValueError(f"Unsupported acceptance dataset format: {path}")
    return dict(payload)


def database_snapshot(session, *, field_id: int) -> dict[str, int]:
    queries = {
        "frames": "SELECT COUNT(*) FROM frames",
        "observations": "SELECT COUNT(*) FROM observations",
        "field_series_frames": "SELECT COUNT(*) FROM field_series_frames",
        "field_series_for_field": "SELECT COUNT(*) FROM field_series WHERE field_id = :field_id",
        "multiple_observation_representatives": """
            SELECT COUNT(*) FROM (
                SELECT fr.observation_id
                FROM field_series_frames fsf
                JOIN frames fr ON fr.frame_id = fsf.frame_id
                WHERE fr.observation_id IS NOT NULL
                GROUP BY fr.observation_id
                HAVING COUNT(*) > 1
            ) q
        """,
        "interseries_generations_for_field": """
            SELECT COUNT(*) FROM interseries_calibration_generations WHERE field_id = :field_id
        """,
        "max_generation_id": """
            SELECT COALESCE(MAX(interseries_generation_id), 0)
            FROM interseries_calibration_generations WHERE field_id = :field_id
        """,
    }
    params = {"field_id": int(field_id)}
    return {
        name: int(session.execute(text(sql), params).scalar_one())
        for name, sql in queries.items()
    }


def _normalized_components(value: Any) -> list[list[int]]:
    components = [sorted(int(v) for v in component) for component in (value or [])]
    return sorted(components, key=lambda row: (row[0] if row else -1, row))


def _compact_field_status(result: FieldStatusResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "field_id": result.field.get("field_id"),
        "field_name": result.field.get("field_name"),
        "stage2": {
            "plan_status": result.stage2.get("plan_status"),
            "n_series": result.stage2.get("n_series"),
            "n_ready_series": result.stage2.get("n_ready_series"),
            "n_eligible_pairs": result.stage2.get("n_eligible_pairs"),
            "largest_component_size": result.stage2.get("largest_component_size"),
            "recipe_compatibility_counts": result.stage2.get("recipe_compatibility_counts"),
            "connected_components": result.stage2.get("connected_components"),
        },
        "stage3": list(result.stage3),
    }


def evaluate_acceptance(
    spec: Mapping[str, Any],
    *,
    doctor_status: str,
    db_before: Mapping[str, Any],
    field_status: Mapping[str, Any],
    controls: Mapping[int, Mapping[str, Any]],
    db_after: Mapping[str, Any],
) -> tuple[AcceptanceCheck, ...]:
    checks: list[AcceptanceCheck] = []

    checks.append(AcceptanceCheck(
        "full_science_environment",
        "OK" if doctor_status == "READY" else "FAIL",
        f"doctor={doctor_status}",
    ))

    expected_db = dict(spec.get("database") or {})
    db_ok = dict(db_before) == expected_db
    checks.append(AcceptanceCheck(
        "database_baseline",
        "OK" if db_ok else "FAIL",
        "exact frozen counters match" if db_ok else f"expected={expected_db}; actual={dict(db_before)}",
    ))

    stage2_expected = dict(spec.get("stage2") or {})
    actual_stage2 = dict(field_status.get("stage2") or {})
    stage2_pairs = {
        "status": field_status.get("status"),
        "plan_status": actual_stage2.get("plan_status"),
        "n_series": actual_stage2.get("n_series"),
        "n_ready_series": actual_stage2.get("n_ready_series"),
        "n_eligible_pairs": actual_stage2.get("n_eligible_pairs"),
        "largest_component_size": actual_stage2.get("largest_component_size"),
        "recipe_compatibility_counts": actual_stage2.get("recipe_compatibility_counts"),
        "connected_components": _normalized_components(actual_stage2.get("connected_components")),
    }
    expected_stage2_pairs = {
        "status": stage2_expected.get("status"),
        "plan_status": stage2_expected.get("plan_status"),
        "n_series": stage2_expected.get("n_series"),
        "n_ready_series": stage2_expected.get("n_ready_series"),
        "n_eligible_pairs": stage2_expected.get("n_eligible_pairs"),
        "largest_component_size": stage2_expected.get("largest_component_size"),
        "recipe_compatibility_counts": stage2_expected.get("recipe_compatibility_counts"),
        "connected_components": _normalized_components(stage2_expected.get("connected_components")),
    }
    stage2_ok = stage2_pairs == expected_stage2_pairs
    checks.append(AcceptanceCheck(
        "stage2_readiness",
        "OK" if stage2_ok else "FAIL",
        "frozen readiness/components match" if stage2_ok else f"expected={expected_stage2_pairs}; actual={stage2_pairs}",
    ))

    actual_stage3 = {
        (str(row.get("filter_name")), row.get("instrument_unit_id")): row
        for row in field_status.get("stage3", [])
    }
    for expected in spec.get("stage3", []):
        scope = (str(expected.get("filter_name")), expected.get("instrument_unit_id"))
        actual = actual_stage3.get(scope)
        name = f"stage3_{scope[0]}_unit{scope[1]}"
        if actual is None:
            checks.append(AcceptanceCheck(name, "FAIL", "scope missing"))
            continue
        keys = (
            "interseries_generation_id", "reference_series_id", "verification_status",
            "reliability_status", "n_series", "n_edges", "graph_dof", "source_scope_matches",
        )
        mismatches = [
            f"{key}:expected={expected.get(key)!r},actual={actual.get(key)!r}"
            for key in keys if actual.get(key) != expected.get(key)
        ]
        if sorted(int(v) for v in actual.get("source_series_ids", [])) != sorted(int(v) for v in expected.get("source_series_ids", [])):
            mismatches.append("source_series_ids")
        target_chi2 = float(expected.get("reduced_chi2"))
        actual_chi2 = actual.get("reduced_chi2")
        tol = float(expected.get("reduced_chi2_abs_tol", 1e-10))
        if actual_chi2 is None or not math.isclose(float(actual_chi2), target_chi2, rel_tol=0.0, abs_tol=tol):
            mismatches.append(f"reduced_chi2:expected={target_chi2},actual={actual_chi2},tol={tol}")
        checks.append(AcceptanceCheck(
            name,
            "OK" if not mismatches else "FAIL",
            "generation/scope/graph match" if not mismatches else "; ".join(mismatches),
        ))

    for expected in spec.get("m5", []):
        object_id = int(expected["object_id"])
        actual = controls.get(object_id)
        name = f"m5_object_{object_id}"
        if actual is None:
            checks.append(AcceptanceCheck(name, "FAIL", "control result missing"))
            continue
        keys = ("status", "rows_source_exact_m4", "accepted_rows", "rejected_rows")
        mismatches = [
            f"{key}:expected={expected.get(key)!r},actual={actual.get(key)!r}"
            for key in keys if actual.get(key) != expected.get(key)
        ]
        if [int(v) for v in actual.get("generation_ids", [])] != [int(v) for v in expected.get("generation_ids", [])]:
            mismatches.append("generation_ids")
        if dict(actual.get("rejection_reasons") or {}) != dict(expected.get("rejection_reasons") or {}):
            mismatches.append(
                f"rejection_reasons:expected={expected.get('rejection_reasons')},actual={actual.get('rejection_reasons')}"
            )
        checks.append(AcceptanceCheck(
            name,
            "OK" if not mismatches else "FAIL",
            "exact M4-linked M5 counts match" if not mismatches else "; ".join(mismatches),
        ))

    unchanged = dict(db_before) == dict(db_after)
    checks.append(AcceptanceCheck(
        "no_unexpected_db_writes",
        "OK" if unchanged else "FAIL",
        "database counters unchanged" if unchanged else f"before={dict(db_before)}; after={dict(db_after)}",
    ))
    return tuple(checks)


def run_acceptance(
    *,
    database_url: str | None,
    dataset: str | Path = "gpx_tf17_2",
    config_file: str | Path | None = None,
    out_root: str | Path | None = None,
) -> AcceptanceResult:
    spec = load_acceptance_spec(dataset)
    field_id = int(spec["field_id"])

    config_payload: dict[str, Any] = {}
    if config_file is not None:
        config_payload = read_config_file(config_file)
    field_cfg = field_archive_mapping(config_payload) if config_payload else {}
    catalog_name = str(spec.get("catalog_name") or field_cfg.get("catalog_name") or "") or None

    doctor = run_doctor(
        database_url=database_url,
        config_file=config_file,
        strict_optional=True,
    )
    if doctor.status != "READY":
        checks = (AcceptanceCheck("full_science_environment", "FAIL", f"doctor={doctor.status}"),)
        return AcceptanceResult(
            status="FAILED",
            dataset=str(spec.get("dataset") or dataset),
            checks=checks,
            actual={"doctor": doctor.to_dict()},
        )

    Session = make_session_factory(database_url)
    controls: dict[int, dict[str, Any]] = {}
    with Session() as session:
        db_before = database_snapshot(session, field_id=field_id)
        field_result = build_field_status(
            session,
            field_id=field_id,
            catalog_name=catalog_name,
            required_stage2_recipe=str(spec.get("required_stage2_recipe") or "stage3_scale_v1"),
            min_points_per_object=int(spec.get("min_points_per_object") or 5),
            min_common_objects=int(spec.get("min_common_objects") or 20),
        )
        compact_status = _compact_field_status(field_result)

        if out_root is None:
            temp_ctx = tempfile.TemporaryDirectory(prefix="gg_acceptance_")
            output_dir = Path(temp_ctx.name)
        else:
            temp_ctx = None
            output_dir = Path(out_root)
            output_dir.mkdir(parents=True, exist_ok=True)
        try:
            for expected in spec.get("m5", []):
                object_id = int(expected["object_id"])
                result = export_global_object_light_curve(
                    session,
                    object_id=object_id,
                    field_id=field_id,
                    generation_ids=[int(v) for v in expected.get("generation_ids", [])],
                    out_root=output_dir / f"object_{object_id}",
                    include_rejected=False,
                    include_rejected_debug=False,
                    plots=False,
                )
                controls[object_id] = {
                    "object_id": object_id,
                    "status": result.status,
                    "generation_ids": list(result.generation_ids),
                    "rows_source_exact_m4": result.rows_source_exact_m4,
                    "accepted_rows": result.accepted_rows,
                    "rejected_rows": result.rejected_rows,
                    "rejection_reasons": dict(result.rejection_reasons),
                }
        finally:
            if temp_ctx is not None:
                temp_ctx.cleanup()

        db_after = database_snapshot(session, field_id=field_id)

    checks = evaluate_acceptance(
        spec,
        doctor_status=doctor.status,
        db_before=db_before,
        field_status=compact_status,
        controls=controls,
        db_after=db_after,
    )
    status = "READY" if all(row.status == "OK" for row in checks) else "FAILED"
    actual = {
        "doctor_status": doctor.status,
        "database_before": db_before,
        "field_status": compact_status,
        "controls": {str(key): value for key, value in sorted(controls.items())},
        "database_after": db_after,
    }
    return AcceptanceResult(
        status=status,
        dataset=str(spec.get("dataset") or dataset),
        checks=checks,
        actual=actual,
    )
