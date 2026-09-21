from __future__ import annotations

import importlib.util
import platform
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from grandgousier import __version__
from grandgousier.config import field_archive_mapping, public_config_summary, read_config_file
from grandgousier.db.migrations import status as migration_status, validate_manifest
from grandgousier.db.session import make_engine
from grandgousier.interseries.persistence import (
    load_interseries_generation_series,
    verify_interseries_generation,
)
from grandgousier.interseries.planning import build_interseries_plan
from grandgousier.pipeline.recipe_contracts import STAGE2_RECIPE_STAGE3_SCALE_V1
from grandgousier.resources import instrument_profile_path


CORE_IMPORTS = (
    "astropy",
    "numpy",
    "pandas",
    "sqlalchemy",
    "psycopg",
    "pydantic",
    "yaml",
    "matplotlib",
    "requests",
)
OPTIONAL_IMPORTS = (
    "photutils",
    "ccdproc",
    "astroscrappy",
    "sep",
    "astroquery",
)


@dataclass(frozen=True, slots=True)
class OperationCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DoctorResult:
    status: str
    checks: tuple[OperationCheck, ...]
    config: dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.status in {"READY", "READY_WITH_WARNINGS"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "version": __version__,
            "checks": [row.to_dict() for row in self.checks],
            "config": self.config,
        }


@dataclass(frozen=True, slots=True)
class FieldStatusResult:
    status: str
    field: dict[str, Any]
    stage2: dict[str, Any]
    stage3: tuple[dict[str, Any], ...]
    series: tuple[dict[str, Any], ...]

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "field": self.field,
            "stage2": self.stage2,
            "stage3": list(self.stage3),
            "series": list(self.series),
        }


def run_doctor(
    *,
    database_url: str | None,
    config_file: str | Path | None = None,
    processed_root: str | Path | None = None,
    out_root: str | Path | None = None,
    instrument_profile: str | None = None,
    strict_optional: bool = False,
) -> DoctorResult:
    """Run a non-destructive installation/runtime preflight."""

    checks: list[OperationCheck] = []
    config_payload: dict[str, Any] = {}
    config_summary: dict[str, Any] = {}
    if config_file is None:
        checks.append(OperationCheck("config", "SKIP", "no config file supplied"))
    else:
        try:
            config_payload = read_config_file(config_file)
            config_summary = public_config_summary(config_payload)
        except Exception as exc:  # noqa: BLE001 - doctor reports configuration failures.
            checks.append(OperationCheck("config", "FAIL", f"{type(exc).__name__}: {exc}"))
        else:
            checks.append(OperationCheck("config", "OK", str(Path(config_file))))

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        OperationCheck(
            "python",
            "OK" if py_ok else "FAIL",
            platform.python_version(),
        )
    )
    checks.append(OperationCheck("grandgousier", "OK", f"version={__version__}"))

    missing_core = [name for name in CORE_IMPORTS if importlib.util.find_spec(name) is None]
    checks.append(
        OperationCheck(
            "core_dependencies",
            "OK" if not missing_core else "FAIL",
            "all imports available" if not missing_core else "missing=" + ",".join(missing_core),
        )
    )
    missing_optional = [name for name in OPTIONAL_IMPORTS if importlib.util.find_spec(name) is None]
    optional_status = "OK" if not missing_optional else ("FAIL" if strict_optional else "WARN")
    checks.append(
        OperationCheck(
            "optional_photometry_dependencies",
            optional_status,
            "all imports available" if not missing_optional else "missing=" + ",".join(missing_optional),
        )
    )

    try:
        validate_manifest()
        required_profiles = ["master.yaml", "robophot.yaml"]
        missing_profiles = [name for name in required_profiles if not instrument_profile_path(name).is_file()]
        if missing_profiles:
            raise FileNotFoundError("missing package resources: " + ",".join(missing_profiles))
    except Exception as exc:  # noqa: BLE001
        checks.append(OperationCheck("package_resources", "FAIL", f"{type(exc).__name__}: {exc}"))
    else:
        checks.append(OperationCheck("package_resources", "OK", "profiles and migration manifest available"))

    engine = None
    try:
        engine = make_engine(database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(OperationCheck("database", "OK", "connection and SELECT 1 succeeded"))
    except Exception as exc:  # noqa: BLE001
        checks.append(OperationCheck("database", "FAIL", f"{type(exc).__name__}: {exc}"))
    else:
        try:
            rows = migration_status(engine)
            pending = [row.migration_id for row in rows if row.state == "PENDING"]
            drift = [row.migration_id for row in rows if row.checksum_status == "DRIFT"]
            if pending or drift:
                details: list[str] = []
                if pending:
                    details.append("pending=" + ",".join(pending))
                if drift:
                    details.append("checksum_drift=" + ",".join(drift))
                checks.append(OperationCheck("schema_migrations", "FAIL", "; ".join(details)))
            else:
                checks.append(
                    OperationCheck(
                        "schema_migrations",
                        "OK",
                        f"total={len(rows)}, pending=0, checksum_drift=0",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(OperationCheck("schema_migrations", "FAIL", f"{type(exc).__name__}: {exc}"))
    finally:
        if engine is not None:
            engine.dispose()

    fa = field_archive_mapping(config_payload) if config_payload else {}
    resolved_processed_root = processed_root or fa.get("processed_root")
    resolved_out_root = out_root or fa.get("out_root")
    resolved_profile = str(instrument_profile or fa.get("instrument_profile") or "auto")

    if resolved_processed_root:
        root = Path(resolved_processed_root)
        checks.append(
            OperationCheck(
                "processed_root",
                "OK" if root.is_dir() else "FAIL",
                str(root) if root.is_dir() else f"directory not found: {root}",
            )
        )
    else:
        checks.append(OperationCheck("processed_root", "SKIP", "not configured"))

    profile_key = resolved_profile.strip().casefold()
    profile_names = {
        "master": ("master.yaml",),
        "robophot": ("robophot.yaml",),
        "auto": ("master.yaml", "robophot.yaml"),
    }.get(profile_key)
    if profile_names is None:
        checks.append(OperationCheck("instrument_profile", "FAIL", f"unsupported mode: {resolved_profile}"))
    else:
        missing = [name for name in profile_names if not instrument_profile_path(name).is_file()]
        checks.append(
            OperationCheck(
                "instrument_profile",
                "OK" if not missing else "FAIL",
                resolved_profile if not missing else "missing=" + ",".join(missing),
            )
        )

    if resolved_out_root:
        out = Path(resolved_out_root)
        try:
            out.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".gg_doctor_", dir=out, delete=True):
                pass
        except OSError as exc:
            checks.append(OperationCheck("out_root", "FAIL", f"not writable: {out}: {exc}"))
        else:
            checks.append(OperationCheck("out_root", "OK", str(out)))
    else:
        checks.append(OperationCheck("out_root", "SKIP", "not configured"))

    if any(row.status == "FAIL" for row in checks):
        status = "FAILED"
    elif any(row.status == "WARN" for row in checks):
        status = "READY_WITH_WARNINGS"
    else:
        status = "READY"
    return DoctorResult(status=status, checks=tuple(checks), config=config_summary)


def build_field_status(
    session: Session,
    *,
    field_id: int,
    catalog_name: str | None = None,
    required_stage2_recipe: str = STAGE2_RECIPE_STAGE3_SCALE_V1,
    min_points_per_object: int = 5,
    min_common_objects: int = 20,
) -> FieldStatusResult:
    """Build one compact read-only operational status for a field."""

    field = session.execute(
        text(
            """
            SELECT field_id, field_uid, field_name, primary_object_name,
                   ra_center_deg, dec_center_deg, radius_arcmin,
                   reference_catalog_name, status
            FROM fields
            WHERE field_id = :field_id
            """
        ),
        {"field_id": int(field_id)},
    ).mappings().first()
    if field is None:
        raise ValueError(f"No field found with field_id={field_id}")

    plan = build_interseries_plan(
        session,
        field_id=int(field_id),
        catalog_name=catalog_name,
        min_points_per_object=int(min_points_per_object),
        min_common_objects=int(min_common_objects),
        required_stage2_recipe=required_stage2_recipe,
    )
    series_rows = tuple(row.to_dict() for row in plan.series)
    status_counts = Counter(str(row.status) for row in plan.series)
    recipe_counts = Counter(
        str(row.recipe_compatibility)
        for row in plan.series
        if row.recipe_compatibility is not None
    )
    ready_by_scope: dict[tuple[str, int | None], set[int]] = defaultdict(set)
    for row in plan.series:
        if row.status == "READY" and row.filter_name:
            ready_by_scope[(str(row.filter_name), row.instrument_unit_id)].add(int(row.series_id))

    params: dict[str, Any] = {"field_id": int(field_id)}
    catalog_predicate = ""
    if catalog_name is not None:
        catalog_predicate = "AND g.catalog_name = :catalog_name"
        params["catalog_name"] = str(catalog_name)
    generation_rows = session.execute(
        text(
            f"""
            SELECT DISTINCT ON (COALESCE(g.filter_name, ''), COALESCE(g.instrument_unit_id, -1))
                   g.interseries_generation_id, g.filter_name, g.instrument_unit_id,
                   g.catalog_name, g.reference_series_id, g.reliability_status,
                   g.n_series, g.n_edges, g.graph_dof, g.reduced_chi2,
                   g.lifecycle_status, g.created_at
            FROM interseries_calibration_generations g
            WHERE g.field_id = :field_id
              {catalog_predicate}
            ORDER BY COALESCE(g.filter_name, ''), COALESCE(g.instrument_unit_id, -1),
                     g.created_at DESC, g.interseries_generation_id DESC
            """
        ),
        params,
    ).mappings().all()

    stage3_rows: list[dict[str, Any]] = []
    covered_scopes: set[tuple[str, int | None]] = set()
    stage3_problem = False
    for generation in generation_rows:
        generation_id = int(generation["interseries_generation_id"])
        verification = verify_interseries_generation(session, generation_id=generation_id)
        source_rows = load_interseries_generation_series(session, generation_id=generation_id)
        source_series = {int(row["series_id"]) for row in source_rows}
        filter_name = str(generation.get("filter_name") or "")
        instrument_unit_id = generation.get("instrument_unit_id")
        scope = (filter_name, int(instrument_unit_id) if instrument_unit_id is not None else None)
        expected_series = ready_by_scope.get(scope, set())
        scope_matches = bool(expected_series) and source_series == expected_series
        if filter_name:
            covered_scopes.add(scope)
        if verification.status != "OK" or not scope_matches:
            stage3_problem = True
        stage3_rows.append(
            {
                "filter_name": generation.get("filter_name"),
                "instrument_unit_id": generation.get("instrument_unit_id"),
                "interseries_generation_id": generation_id,
                "reference_series_id": int(generation["reference_series_id"]),
                "verification_status": verification.status,
                "reliability_status": generation.get("reliability_status"),
                "n_series": int(generation.get("n_series") or 0),
                "n_edges": int(generation.get("n_edges") or 0),
                "graph_dof": int(generation.get("graph_dof") or 0),
                "reduced_chi2": generation.get("reduced_chi2"),
                "source_scope_matches": scope_matches,
                "source_series_ids": sorted(source_series),
                "current_ready_series_ids": sorted(expected_series),
            }
        )

    required_scopes = {scope for scope, ids in ready_by_scope.items() if len(ids) >= 2}
    missing_stage3_scopes = sorted(
        required_scopes - covered_scopes,
        key=lambda item: (item[0], -1 if item[1] is None else item[1]),
    )
    blockers = status_counts.get("RECIPE_MISMATCH", 0) + status_counts.get("MISSING_PROVENANCE", 0)
    if blockers:
        overall = "BLOCKED_STAGE2_RECIPE"
    elif stage3_problem or missing_stage3_scopes:
        overall = "STAGE3_STALE_OR_MISSING"
    elif required_scopes:
        overall = "READY"
    else:
        overall = plan.status

    stage2 = {
        "required_recipe": required_stage2_recipe,
        "plan_status": plan.status,
        "n_series": plan.n_series,
        "n_ready_series": plan.n_ready_series,
        "n_eligible_pairs": plan.n_eligible_pairs,
        "largest_component_size": plan.largest_component_size,
        "recommended_reference_series_id": plan.recommended_reference_series_id,
        "status_counts": dict(sorted(status_counts.items())),
        "recipe_compatibility_counts": dict(sorted(recipe_counts.items())),
        "connected_components": plan.connected_components,
        "missing_stage3_scopes": [
            {"filter_name": scope[0], "instrument_unit_id": scope[1]}
            for scope in missing_stage3_scopes
        ],
    }
    return FieldStatusResult(
        status=overall,
        field=dict(field),
        stage2=stage2,
        stage3=tuple(stage3_rows),
        series=series_rows,
    )
