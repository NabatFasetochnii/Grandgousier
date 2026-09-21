from __future__ import annotations

import csv
import html
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import text

from grandgousier import __version__
from grandgousier.pipeline.recipe_contracts import (
    MISSING_PROVENANCE,
    RECIPE_MISMATCH,
    STAGE2_RECIPE_STAGE3_SCALE_V1,
    SUPPORTED_STAGE2_RECIPES,
    stage2_recipe_process_kwargs,
)


FIELD_ARCHIVE_ALGORITHM = "field_archive_m8_v1"
STAGE2_MIN_FRAMES_PER_SERIES = 10


@dataclass(frozen=True)
class Stage3Policy:
    filter_name: str
    min_common_objects: int
    reference_series_id: int | None = None
    instrument_unit_id: int | None = None
    min_points_per_object: int = 5
    clip_sigma: float = 4.0
    max_clip_iterations: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArchiveBatch:
    path: str
    relative_path: str
    observation_date: str | None
    n_files: int
    object_names: tuple[str, ...]
    filters: tuple[str, ...]
    action: str
    db_status: str
    indexed_files: int
    series_ids: tuple[int, ...]
    resume_series_ids: tuple[int, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["object_names"] = list(self.object_names)
        row["filters"] = list(self.filters)
        row["series_ids"] = list(self.series_ids)
        row["resume_series_ids"] = list(self.resume_series_ids)
        return row


@dataclass
class FieldArchiveRunResult:
    field_id: int
    processed_root: str
    catalog_name: str
    execute: bool
    resume: bool
    stage2_recipe: str
    policies: list[Stage3Policy]
    control_object_ids: list[int]
    algorithm: str = FIELD_ARCHIVE_ALGORITHM
    version: str = __version__
    status: str = "PLANNED"
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    finished_at: str | None = None
    batches: list[dict[str, Any]] = field(default_factory=list)
    stage2_runs: list[dict[str, Any]] = field(default_factory=list)
    science_verifications: list[dict[str, Any]] = field(default_factory=list)
    stage2_recipe_readiness: list[dict[str, Any]] = field(default_factory=list)
    catalog_identity: dict[str, Any] | None = None
    stage3: list[dict[str, Any]] = field(default_factory=list)
    control_objects: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "version": self.version,
            "status": self.status,
            "field_id": self.field_id,
            "processed_root": self.processed_root,
            "catalog_name": self.catalog_name,
            "execute": self.execute,
            "resume": self.resume,
            "stage2_recipe": self.stage2_recipe,
            "policies": [row.to_dict() for row in self.policies],
            "control_object_ids": self.control_object_ids,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "batches": self.batches,
            "stage2_runs": self.stage2_runs,
            "science_verifications": self.science_verifications,
            "stage2_recipe_readiness": self.stage2_recipe_readiness,
            "catalog_identity": self.catalog_identity,
            "stage3": self.stage3,
            "control_objects": self.control_objects,
            "warnings": self.warnings,
            "errors": self.errors,
            "outputs": self.outputs,
        }



def _stage2_series_ids_for_batch(batch: ArchiveBatch) -> list[int] | None:
    """Return explicit existing series to rerun for a resumable incomplete batch.

    Ordinary archive-path execution intentionally does not rerun already represented
    observations.  A field-archive resume therefore has to opt in explicitly when
    incompleteness is caused by an existing series without a READY SCIENCE product.
    Incompleteness caused by missing/unrepresented frames keeps ordinary discovery
    semantics and returns ``None``.
    """

    if batch.action != "RESUME_INCOMPLETE" or not batch.resume_series_ids:
        return None
    return sorted({int(v) for v in batch.resume_series_ids})


def parse_stage3_policy(value: str) -> Stage3Policy:
    """Parse FILTER:MIN_COMMON[:REFERENCE_SERIES_ID[:INSTRUMENT_UNIT_ID]]."""

    parts = [part.strip() for part in str(value).split(":")]
    if len(parts) < 2 or len(parts) > 4 or not parts[0]:
        raise ValueError(
            "Stage-3 policy must be FILTER:MIN_COMMON[:REFERENCE_SERIES_ID[:INSTRUMENT_UNIT_ID]]"
        )
    try:
        min_common = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Invalid min-common value in Stage-3 policy: {value!r}") from exc
    if min_common < 1:
        raise ValueError("Stage-3 min-common-objects must be >= 1")

    def optional_int(index: int) -> int | None:
        if len(parts) <= index or parts[index] == "":
            return None
        try:
            parsed = int(parts[index])
        except ValueError as exc:
            raise ValueError(f"Invalid integer in Stage-3 policy: {value!r}") from exc
        if parsed < 1:
            raise ValueError(f"Stage-3 policy ids must be positive: {value!r}")
        return parsed

    return Stage3Policy(
        filter_name=parts[0],
        min_common_objects=min_common,
        reference_series_id=optional_int(2),
        instrument_unit_id=optional_int(3),
    )



def _emit_workflow_event(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event: str,
    **payload: Any,
) -> None:
    if callback is not None:
        callback(str(event), dict(payload))


def run_field_archive_workflow(
    *,
    database_url: str | None,
    processed_root: str | Path,
    field_id: int,
    catalog_name: str,
    policies: Sequence[Stage3Policy],
    control_object_ids: Iterable[int] = (),
    execute: bool = False,
    resume: bool = True,
    instrument_profile: str = "MASTER",
    stage2_recipe: str = STAGE2_RECIPE_STAGE3_SCALE_V1,
    exclude_dates: Iterable[str] = (),
    include_dates: Iterable[str] = (),
    out_root: str | Path = "reports/field_archive",
    plot_max_points: int = 200,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> FieldArchiveRunResult:
    if not policies:
        raise ValueError("At least one explicit Stage-3 policy is required")
    _validate_policy_inputs(policies)
    if stage2_recipe not in SUPPORTED_STAGE2_RECIPES:
        raise ValueError(
            f"Unsupported Stage-2 recipe {stage2_recipe!r}; supported: {', '.join(SUPPORTED_STAGE2_RECIPES)}"
        )

    root = Path(processed_root)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Processed root does not exist or is not a directory: {root}")
    out = Path(out_root)
    out.mkdir(parents=True, exist_ok=True)

    result = FieldArchiveRunResult(
        field_id=int(field_id),
        processed_root=str(root),
        catalog_name=str(catalog_name),
        execute=bool(execute),
        resume=bool(resume),
        stage2_recipe=stage2_recipe,
        policies=list(policies),
        control_object_ids=sorted({int(v) for v in control_object_ids}),
    )
    from grandgousier.archive.auto_process import (
        DEFAULT_EXTENSIONS,
        process_archive_path,
        scan_archive_path,
    )
    from grandgousier.db.session import make_session_factory
    from grandgousier.interseries.analysis import (
        analyze_interseries_calibration,
        export_interseries_analysis,
    )
    from grandgousier.interseries.catalog_identity import audit_field_catalog_identity
    from grandgousier.interseries.persistence import (
        load_interseries_generation_edges,
        load_interseries_generation_series,
        persist_interseries_calibration,
        verify_interseries_generation,
    )
    from grandgousier.interseries.planning import build_interseries_plan
    from grandgousier.lightcurves.global_object import export_global_object_light_curve
    from grandgousier.photometry.science_materialization import science_materialization_parity_report

    Session = make_session_factory(database_url)

    try:
        detected, _, scan_warnings = scan_archive_path(
            root,
            instrument_profile=instrument_profile,
            recursive=True,
            extensions=DEFAULT_EXTENSIONS,
        )
        result.warnings.extend(scan_warnings)
        _emit_workflow_event(
            event_callback,
            "scan_completed",
            stage="scan",
            status="OK" if detected else "EMPTY",
            detected_files=len(detected),
            warning_count=len(scan_warnings),
        )
        if not detected:
            return _finish(result, out, status="FAILED_NO_FITS", error="No readable FITS files found")

        raw_batches = _group_detected_by_parent(root, detected)
        include_set = {_normalize_date_token(v) for v in include_dates if str(v).strip()}
        exclude_set = {_normalize_date_token(v) for v in exclude_dates if str(v).strip()}

        with Session() as session:
            current_plan = build_interseries_plan(
                session,
                field_id=int(field_id),
                catalog_name=str(catalog_name),
                min_points_per_object=min(policy.min_points_per_object for policy in policies),
                min_common_objects=min(policy.min_common_objects for policy in policies),
                required_stage2_recipe=stage2_recipe,
            )
            result.stage2_recipe_readiness = _recipe_readiness_rows(current_plan.series)
            ready_by_series = {row.series_id: row for row in current_plan.series}

            batches: list[ArchiveBatch] = []
            for item in raw_batches:
                obs_date = item["observation_date"]
                if item.get("date_ambiguous"):
                    batches.append(
                        ArchiveBatch(
                            path=item["path"],
                            relative_path=item["relative_path"],
                            observation_date=None,
                            n_files=len(item["paths"]),
                            object_names=tuple(item["object_names"]),
                            filters=tuple(item["filters"]),
                            action="STOP_CONFLICT",
                            db_status="AMBIGUOUS_BATCH_DATE",
                            indexed_files=0,
                            series_ids=(),
                            reason=(
                                "leaf batch contains multiple observing dates: "
                                + ",".join(item.get("observation_dates") or [])
                            ),
                        )
                    )
                    continue
                if include_set and obs_date not in include_set:
                    batches.append(_excluded_batch(item, "NOT_IN_INCLUDE_DATES"))
                    continue
                if obs_date in exclude_set:
                    batches.append(_excluded_batch(item, "EXCLUDED_DATE"))
                    continue
                state = _inspect_batch_db_state(
                    session,
                    paths=item["paths"],
                    field_id=int(field_id),
                    ready_by_series=ready_by_series,
                )
                max_group_files = int(item.get("max_series_like_group_files") or 0)
                action = _batch_action(
                    state["status"],
                    resume=resume,
                    max_series_like_group_files=max_group_files,
                )
                if action == "SKIP_TOO_SMALL":
                    state = dict(state)
                    state["reason"] = (
                        "NO_SERIES_GROUP_MEETS_MIN_FRAMES_PER_SERIES:"
                        f"min={STAGE2_MIN_FRAMES_PER_SERIES},max={max_group_files}"
                    )
                batches.append(
                    ArchiveBatch(
                        path=item["path"],
                        relative_path=item["relative_path"],
                        observation_date=obs_date,
                        n_files=len(item["paths"]),
                        object_names=tuple(item["object_names"]),
                        filters=tuple(item["filters"]),
                        action=action,
                        db_status=state["status"],
                        indexed_files=state["indexed_files"],
                        series_ids=tuple(state["series_ids"]),
                        resume_series_ids=tuple(state.get("resume_series_ids") or ()),
                        reason=state.get("reason"),
                    )
                )
            result.batches = [row.to_dict() for row in batches]

        action_counts: dict[str, int] = defaultdict(int)
        for row in batches:
            action_counts[row.action] += 1
        _emit_workflow_event(
            event_callback,
            "batch_plan_completed",
            stage="plan",
            status="OK",
            batch_count=len(batches),
            action_counts=dict(action_counts),
        )

        conflicts = [row for row in batches if row.action == "STOP_CONFLICT"]
        if conflicts:
            details = "; ".join(f"{row.relative_path}: {row.reason or row.db_status}" for row in conflicts)
            return _finish(result, out, status="FAILED_BATCH_CONFLICT", error=details)

        if not execute:
            return _finish(result, out, status="PLAN")

        processed_series_ids: set[int] = set()
        for batch in batches:
            if batch.action in {"SKIP_READY", "SKIP_EXCLUDED", "SKIP_TOO_SMALL"}:
                continue
            if batch.action not in {"RUN_NEW", "RESUME_INCOMPLETE"}:
                return _finish(
                    result,
                    out,
                    status="FAILED_BATCH_STATE",
                    error=f"Unsupported batch action {batch.action} for {batch.relative_path}",
                )
            batch_token = _safe_token(batch.observation_date or batch.relative_path)
            batch_out = out / "stage2" / batch_token
            manifest = batch_out / "manifest.json"
            _emit_workflow_event(
                event_callback,
                "stage2_batch_started",
                stage="stage2",
                status="RUNNING",
                batch_date=batch.observation_date,
                batch_relative_path=batch.relative_path,
                action=batch.action,
                n_files=batch.n_files,
            )
            with Session() as session:
                archive_result = process_archive_path(
                    session,
                    root=batch.path,
                    catalog_name=str(catalog_name),
                    execute=True,
                    instrument_profile=instrument_profile,
                    recursive=False,
                    series_ids=_stage2_series_ids_for_batch(batch),
                    min_frames_per_series=STAGE2_MIN_FRAMES_PER_SERIES,
                    max_field_separation_arcmin=30.0,
                    max_series_gap_minutes=60.0,
                    calibration_prefix_template=(
                        "grandgousier_m8_{instrument_lower}_{date_yyyymmdd}_{field_name}_"
                        "{tube}_{camera}_{filter_name}_s{series_id}_v1"
                    ),
                    out_root=batch_out / "exports",
                    manifest_out=manifest,
                    **_stage2_recipe_kwargs(stage2_recipe),
                )
            run_row = archive_result.to_dict()
            run_row["batch_relative_path"] = batch.relative_path
            run_row["batch_date"] = batch.observation_date
            result.stage2_runs.append(run_row)
            processed_series_ids.update(int(v) for v in archive_result.processed_series_ids)
            _emit_workflow_event(
                event_callback,
                "stage2_batch_completed",
                stage="stage2",
                status=archive_result.status,
                batch_date=batch.observation_date,
                batch_relative_path=batch.relative_path,
                processed_series_ids=[int(v) for v in archive_result.processed_series_ids],
                failed_files=int(archive_result.failed_files),
            )
            if archive_result.status not in {"DONE", "DONE_WITH_WARNINGS"}:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2",
                    error=f"Stage 2 failed for {batch.relative_path}: {archive_result.status}",
                )
            if int(archive_result.failed_files) > 0:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2_FILES",
                    error=f"Stage 2 reported {archive_result.failed_files} failed FITS files for {batch.relative_path}",
                )
            failed_pipelines = [
                row for row in archive_result.pipeline_results
                if str(row.get("status") or "").upper() not in {"DONE", "DONE_WITH_WARNINGS"}
            ]
            if failed_pipelines:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2_PIPELINE",
                    error=f"Stage 2 pipeline failure in {batch.relative_path}",
                )

        # Re-plan after every requested Stage-2 batch.  This selects the same newest
        # immutable SCIENCE generations that M0/M1 use downstream.
        with Session() as session:
            all_plan = build_interseries_plan(
                session,
                field_id=int(field_id),
                catalog_name=str(catalog_name),
                min_points_per_object=min(policy.min_points_per_object for policy in policies),
                min_common_objects=min(policy.min_common_objects for policy in policies),
                required_stage2_recipe=stage2_recipe,
            )
            result.stage2_recipe_readiness = _recipe_readiness_rows(all_plan.series)
            candidate_by_series = {row.series_id: row for row in all_plan.series}
            verify_ids = sorted(processed_series_ids)
            for series_id in verify_ids:
                candidate = candidate_by_series.get(series_id)
                if candidate is None or candidate.calibration_version_id is None:
                    return _finish(
                        result,
                        out,
                        status="FAILED_SCIENCE_SELECTION",
                        error=f"No selected SCIENCE generation after Stage 2 for series_id={series_id}",
                    )
                report = science_materialization_parity_report(
                    session,
                    calibration_version_id=int(candidate.calibration_version_id),
                    tolerance=1.0e-10,
                )
                row = dict(report)
                row["series_id"] = series_id
                result.science_verifications.append(row)
                if report.get("status") != "OK":
                    return _finish(
                        result,
                        out,
                        status="FAILED_SCIENCE_VERIFICATION",
                        error=(
                            f"SCIENCE materialization verification failed for series_id={series_id}, "
                            f"calibration_version_id={candidate.calibration_version_id}"
                        ),
                    )

            audit = audit_field_catalog_identity(session, field_id=int(field_id))
            result.catalog_identity = audit.to_dict()
            if audit.status != "READY_SHARED_IDENTITY":
                return _finish(
                    result,
                    out,
                    status="FAILED_CATALOG_IDENTITY",
                    error=f"Catalog identity audit failed: {audit.status}",
                )

        generation_ids: list[int] = []
        for policy in policies:
            _emit_workflow_event(
                event_callback,
                "stage3_scope_started",
                stage="stage3",
                status="RUNNING",
                filter_name=policy.filter_name,
                min_common_objects=policy.min_common_objects,
                reference_series_id=policy.reference_series_id,
            )
            with Session() as session:
                resolved_policy = _resolve_policy_scope(session, int(field_id), policy)
                plan = build_interseries_plan(
                    session,
                    field_id=int(field_id),
                    instrument_unit_id=resolved_policy.instrument_unit_id,
                    filter_name=resolved_policy.filter_name,
                    catalog_name=str(catalog_name),
                    min_points_per_object=resolved_policy.min_points_per_object,
                    min_common_objects=resolved_policy.min_common_objects,
                    required_stage2_recipe=stage2_recipe,
                )
                reference_series_id = (
                    resolved_policy.reference_series_id
                    if resolved_policy.reference_series_id is not None
                    else plan.recommended_reference_series_id
                )
                stage3_row: dict[str, Any] = {
                    "policy": resolved_policy.to_dict(),
                    "plan": plan.to_dict(),
                    "reference_series_id": reference_series_id,
                }
                result.stage3.append(stage3_row)
                if plan.status != "READY_FOR_GRAPH_VALIDATION":
                    return _finish(
                        result,
                        out,
                        status="FAILED_M0",
                        error=(
                            f"M0 gate failed for filter={resolved_policy.filter_name}: {plan.status}. "
                            "Thresholds are never relaxed automatically."
                        ),
                    )
                if reference_series_id is None:
                    return _finish(
                        result,
                        out,
                        status="FAILED_M0_REFERENCE",
                        error=f"No reference series for filter={resolved_policy.filter_name}",
                    )
                analysis = analyze_interseries_calibration(
                    session,
                    field_id=int(field_id),
                    instrument_unit_id=resolved_policy.instrument_unit_id,
                    filter_name=resolved_policy.filter_name,
                    catalog_name=str(catalog_name),
                    reference_series_id=int(reference_series_id),
                    min_points_per_object=resolved_policy.min_points_per_object,
                    min_common_objects=resolved_policy.min_common_objects,
                    clip_sigma=resolved_policy.clip_sigma,
                    max_clip_iterations=resolved_policy.max_clip_iterations,
                    required_stage2_recipe=stage2_recipe,
                )
                diag_dir = out / "stage3" / _safe_token(resolved_policy.filter_name)
                paths = export_interseries_analysis(analysis, diag_dir)
                stage3_row["analysis"] = analysis.to_dict()
                stage3_row["analysis_outputs"] = {key: str(value) for key, value in paths.items()}
                graph = analysis.graph_solution
                if analysis.status != "READY_M1_M3" or graph is None or graph.status != "OK":
                    return _finish(
                        result,
                        out,
                        status="FAILED_M1_M3",
                        error=(
                            f"M1-M3 gate failed for filter={resolved_policy.filter_name}: "
                            f"analysis={analysis.status}, graph={graph.status if graph else None}. "
                            "Thresholds are never relaxed automatically."
                        ),
                    )

            # Persistence is deliberately a separate transaction after the read-only
            # M1-M3 gate has passed.
            with Session.begin() as session:
                persisted = persist_interseries_calibration(
                    session,
                    field_id=int(field_id),
                    instrument_unit_id=resolved_policy.instrument_unit_id,
                    filter_name=resolved_policy.filter_name,
                    catalog_name=str(catalog_name),
                    reference_series_id=int(reference_series_id),
                    min_points_per_object=resolved_policy.min_points_per_object,
                    min_common_objects=resolved_policy.min_common_objects,
                    clip_sigma=resolved_policy.clip_sigma,
                    max_clip_iterations=resolved_policy.max_clip_iterations,
                    required_stage2_recipe=stage2_recipe,
                )
                stage3_row["generation"] = persisted.to_dict()
                generation_id = int(persisted.interseries_generation_id)

            with Session() as session:
                verification = verify_interseries_generation(session, generation_id=generation_id)
                stage3_row["verification"] = verification.to_dict()
                stage3_row["series"] = load_interseries_generation_series(
                    session, generation_id=generation_id
                )
                stage3_row["edges"] = load_interseries_generation_edges(
                    session, generation_id=generation_id
                )
                if verification.status != "OK":
                    return _finish(
                        result,
                        out,
                        status="FAILED_M4_VERIFICATION",
                        error=f"M4 verification failed for generation_id={generation_id}",
                    )
            generation_ids.append(generation_id)
            _emit_workflow_event(
                event_callback,
                "stage3_scope_completed",
                stage="stage3",
                status="OK",
                filter_name=resolved_policy.filter_name,
                generation_id=generation_id,
                verification_status=verification.status,
            )

        for object_id in result.control_object_ids:
            object_root = out / "m5" / f"object_{object_id}"
            _emit_workflow_event(
                event_callback,
                "m5_control_started",
                stage="m5",
                status="RUNNING",
                object_id=int(object_id),
                generation_ids=list(generation_ids),
            )
            with Session() as session:
                control = export_global_object_light_curve(
                    session,
                    object_id=int(object_id),
                    field_id=int(field_id),
                    generation_ids=generation_ids,
                    out_root=object_root,
                    include_rejected_debug=True,
                    plots=True,
                    plot_reduction="auto",
                    plot_max_points=max(1, int(plot_max_points)),
                    plot_time_axis="auto",
                )
            control_row = control.to_dict()
            result.control_objects.append(control_row)
            _emit_workflow_event(
                event_callback,
                "m5_control_completed",
                stage="m5",
                status=control.status,
                object_id=int(object_id),
                source_rows=int(control.rows_source_exact_m4),
                accepted_rows=int(control.accepted_rows),
                rejected_rows=int(control.rejected_rows),
            )
            if control.accepted_rows <= 0:
                return _finish(
                    result,
                    out,
                    status="FAILED_M5_CONTROL",
                    error=f"M5 control object_id={object_id} has no accepted points",
                )

        return _finish(result, out, status="READY")
    except Exception as exc:  # keep a machine-readable partial report for production diagnosis
        result.errors.append(f"{type(exc).__name__}: {exc}")
        _finish(result, out, status="FAILED_EXCEPTION")
        raise



def _validate_policy_inputs(policies: Sequence[Stage3Policy]) -> None:
    """Reject ambiguous M8 v1 policy sets before any archive or DB work."""

    seen_filters: dict[str, Stage3Policy] = {}
    for policy in policies:
        key = str(policy.filter_name).strip().casefold()
        if not key:
            raise ValueError("Stage-3 policy filter must not be empty")
        if key in seen_filters:
            raise ValueError(
                f"Duplicate Stage-3 policy for filter={policy.filter_name!r}; "
                "M8 v1 requires exactly one generation policy per filter"
            )
        seen_filters[key] = policy


def _stage2_recipe_kwargs(recipe: str) -> dict[str, Any]:
    # Backwards-compatible private wrapper used by existing orchestration tests.
    return stage2_recipe_process_kwargs(recipe)


def _series_like_group_key(row: Any) -> tuple[str, ...]:
    """Approximate the Stage-2 series grouping without running the DB pipeline.

    The archive scanner exposes these identity fields for supported instrument
    profiles.  ``getattr`` keeps the planner conservative for lightweight test
    rows or future scanners: missing dimensions collapse groups together, which
    can cause an unnecessary RUN_NEW but never a false SKIP_TOO_SMALL.
    """

    return tuple(
        str(getattr(row, name, None) or "").strip().casefold()
        for name in (
            "profile_name",
            "instrument",
            "site",
            "tube",
            "camera",
            "filter_name",
            "object_name",
        )
    )


def _group_detected_by_parent(root: Path, detected: Sequence[Any]) -> list[dict[str, Any]]:
    grouped: dict[Path, list[Any]] = defaultdict(list)
    for row in detected:
        grouped[Path(row.path).parent].append(row)
    result: list[dict[str, Any]] = []
    for parent in sorted(grouped, key=lambda p: str(p).casefold()):
        rows = grouped[parent]
        dates = sorted({_date_from_value(row.date_obs) for row in rows if _date_from_value(row.date_obs)})
        observation_date = dates[0] if len(dates) == 1 else None
        relative = _relative_or_self(parent, root)
        series_like_counts: dict[tuple[str, ...], int] = defaultdict(int)
        for row in rows:
            series_like_counts[_series_like_group_key(row)] += 1
        result.append(
            {
                "path": str(parent),
                "relative_path": relative,
                "observation_date": observation_date,
                "observation_dates": dates,
                "date_ambiguous": len(dates) > 1,
                "paths": sorted({str(Path(row.path)) for row in rows}),
                "object_names": sorted({str(row.object_name) for row in rows if row.object_name}),
                "filters": sorted({str(row.filter_name) for row in rows if row.filter_name}),
                "max_series_like_group_files": max(series_like_counts.values(), default=0),
            }
        )
    return result


def _inspect_batch_db_state(
    session: Any,
    *,
    paths: Sequence[str],
    field_id: int,
    ready_by_series: dict[int, Any],
) -> dict[str, Any]:
    if not paths:
        return {"status": "EMPTY", "indexed_files": 0, "series_ids": [], "resume_series_ids": [], "reason": "NO_PATHS"}
    rows = session.execute(
        text(
            """
            SELECT fl.locator AS raw_path, fr.frame_id, fr.observation_id, fsf.series_id, fs.field_id
            FROM frames fr
            JOIN frame_locations fl ON fl.frame_id = fr.frame_id AND fl.is_primary
            LEFT JOIN field_series_frames fsf ON fsf.frame_id = fr.frame_id
            LEFT JOIN field_series fs ON fs.series_id = fsf.series_id
            WHERE fl.locator = ANY(:paths)
            """
        ),
        {"paths": list(paths)},
    ).mappings().all()
    indexed_paths = {str(row["raw_path"]) for row in rows}
    rows_by_path: dict[str, list[Any]] = defaultdict(list)
    conflicting_fields: set[int] = set()
    observation_ids: set[int] = set()
    for row in rows:
        raw_path = str(row["raw_path"])
        rows_by_path[raw_path].append(row)
        observation_id = row.get("observation_id")
        if observation_id is not None:
            observation_ids.add(int(observation_id))
        series_id = row.get("series_id")
        row_field_id = row.get("field_id")
        if (
            series_id is not None
            and row_field_id is not None
            and int(row_field_id) != int(field_id)
        ):
            conflicting_fields.add(int(row_field_id))
    if conflicting_fields:
        return {
            "status": "CONFLICT",
            "indexed_files": len(indexed_paths),
            "series_ids": [],
            "reason": f"frames already belong to other field_id values: {sorted(conflicting_fields)}",
        }
    if not indexed_paths:
        return {"status": "NEW", "indexed_files": 0, "series_ids": [], "resume_series_ids": [], "reason": None}

    # P4 physical-observation identity allows more than one Frame product to map to
    # the same observation. M8 readiness must therefore ask whether the physical
    # Observation is represented by a science-ready series in this field, rather
    # than requiring every Frame row for every raw path to be assigned directly.
    representatives_by_observation: dict[int, set[int]] = defaultdict(set)
    if observation_ids:
        representative_rows = session.execute(
            text(
                """
                SELECT fr.observation_id, fsf.series_id, fs.field_id
                FROM frames fr
                JOIN field_series_frames fsf ON fsf.frame_id = fr.frame_id
                JOIN field_series fs ON fs.series_id = fsf.series_id
                WHERE fr.observation_id = ANY(:observation_ids)
                """
            ),
            {"observation_ids": sorted(observation_ids)},
        ).mappings().all()
        for row in representative_rows:
            if row.get("observation_id") is None or row.get("series_id") is None:
                continue
            if row.get("field_id") is None or int(row["field_id"]) != int(field_id):
                continue
            representatives_by_observation[int(row["observation_id"])].add(
                int(row["series_id"])
            )

    def _series_rank(series_id: int) -> tuple[int, int, int]:
        candidate = ready_by_series.get(series_id)
        is_ready = int(candidate is not None and candidate.status == "READY")
        n_science_frames = int(getattr(candidate, "n_science_frames", 0) or 0)
        return is_ready, n_science_frames, int(series_id)

    series_counts: dict[int, int] = defaultdict(int)
    unresolved_observations: set[int] = set()
    ambiguous_paths: list[str] = []
    legacy_unassigned_frames = 0
    counted_observations: set[int] = set()

    for raw_path, path_rows in rows_by_path.items():
        path_observation_ids = {
            int(row["observation_id"])
            for row in path_rows
            if row.get("observation_id") is not None
        }
        if len(path_observation_ids) > 1:
            ambiguous_paths.append(raw_path)
            continue

        if path_observation_ids:
            observation_id = next(iter(path_observation_ids))
            if observation_id in counted_observations:
                continue
            counted_observations.add(observation_id)
            representative_series_ids = representatives_by_observation.get(
                observation_id, set()
            )
            if not representative_series_ids:
                unresolved_observations.add(observation_id)
                continue
            selected_series_id = max(representative_series_ids, key=_series_rank)
            series_counts[int(selected_series_id)] += 1
            continue

        # Legacy rows without physical Observation identity retain the pre-P4
        # frame-level semantics. An unassigned legacy Frame is still incomplete.
        for row in path_rows:
            series_id = row.get("series_id")
            row_field_id = row.get("field_id")
            if series_id is None:
                legacy_unassigned_frames += 1
                continue
            if row_field_id is not None and int(row_field_id) == int(field_id):
                series_counts[int(series_id)] += 1

    if ambiguous_paths:
        return {
            "status": "CONFLICT",
            "indexed_files": len(indexed_paths),
            "series_ids": sorted(series_counts),
            "reason": (
                "raw paths map to multiple physical observation identities: "
                f"count={len(ambiguous_paths)}"
            ),
        }
    if (
        len(indexed_paths) < len(paths)
        or unresolved_observations
        or legacy_unassigned_frames
    ):
        return {
            "status": "INCOMPLETE",
            "indexed_files": len(indexed_paths),
            "series_ids": sorted(series_counts),
            "reason": (
                f"partial indexing/observation representation: "
                f"indexed={len(indexed_paths)}/{len(paths)}, "
                f"unrepresented_observations={len(unresolved_observations)}, "
                f"legacy_unassigned_frames={legacy_unassigned_frames}"
            ),
        }
    if not series_counts:
        return {
            "status": "INCOMPLETE",
            "indexed_files": len(indexed_paths),
            "series_ids": [],
            "reason": "indexed observations are not represented by field series",
        }
    incomplete_series_ids: list[int] = []
    incomplete_reasons: list[str] = []
    for series_id, batch_frame_count in series_counts.items():
        candidate = ready_by_series.get(series_id)
        if candidate is None:
            incomplete_series_ids.append(int(series_id))
            incomplete_reasons.append(
                f"series_id={series_id} has no READY SCIENCE generation"
            )
            continue
        if candidate.status in {RECIPE_MISMATCH, MISSING_PROVENANCE}:
            reasons = list(getattr(candidate, "recipe_mismatch_reasons", ()) or ())
            return {
                "status": candidate.status,
                "indexed_files": len(indexed_paths),
                "series_ids": sorted(series_counts),
                "resume_series_ids": [],
                "reason": (
                    f"series_id={series_id} {candidate.status}: "
                    + ("; ".join(reasons) if reasons else "Stage-2 provenance is not compatible")
                ),
            }
        if candidate.status != "READY":
            incomplete_series_ids.append(int(series_id))
            incomplete_reasons.append(
                f"series_id={series_id} has no READY SCIENCE generation"
            )
            continue
        if int(candidate.n_science_frames) < int(batch_frame_count):
            incomplete_series_ids.append(int(series_id))
            incomplete_reasons.append(
                f"series_id={series_id} SCIENCE frame coverage "
                f"{candidate.n_science_frames} < batch frames {batch_frame_count}"
            )

    if incomplete_series_ids:
        return {
            "status": "INCOMPLETE",
            "indexed_files": len(indexed_paths),
            "series_ids": sorted(series_counts),
            "resume_series_ids": sorted(set(incomplete_series_ids)),
            "reason": "; ".join(incomplete_reasons),
        }

    return {
        "status": "READY",
        "indexed_files": len(indexed_paths),
        "series_ids": sorted(series_counts),
        "resume_series_ids": [],
        "reason": None,
    }


def _batch_action(
    status: str,
    *,
    resume: bool,
    max_series_like_group_files: int | None = None,
) -> str:
    if (
        status in {"NEW", "INCOMPLETE"}
        and max_series_like_group_files is not None
        and int(max_series_like_group_files) < STAGE2_MIN_FRAMES_PER_SERIES
    ):
        return "SKIP_TOO_SMALL"
    if status == "READY":
        return "SKIP_READY" if resume else "STOP_CONFLICT"
    if status == "NEW":
        return "RUN_NEW"
    if status == "INCOMPLETE":
        return "RESUME_INCOMPLETE" if resume else "STOP_CONFLICT"
    if status in {"CONFLICT", RECIPE_MISMATCH, MISSING_PROVENANCE}:
        return "STOP_CONFLICT"
    return "STOP_CONFLICT"


def _excluded_batch(item: dict[str, Any], reason: str) -> ArchiveBatch:
    return ArchiveBatch(
        path=item["path"],
        relative_path=item["relative_path"],
        observation_date=item["observation_date"],
        n_files=len(item["paths"]),
        object_names=tuple(item["object_names"]),
        filters=tuple(item["filters"]),
        action="SKIP_EXCLUDED",
        db_status="EXCLUDED",
        indexed_files=0,
        series_ids=(),
        resume_series_ids=(),
        reason=reason,
    )


def _resolve_policy_scope(session: Any, field_id: int, policy: Stage3Policy) -> Stage3Policy:
    instrument_unit_id = policy.instrument_unit_id
    if policy.reference_series_id is not None:
        row = session.execute(
            text(
                """
                SELECT series_id, field_id, instrument_unit_id, filter_name
                FROM field_series
                WHERE series_id = :series_id
                """
            ),
            {"series_id": int(policy.reference_series_id)},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Reference series_id={policy.reference_series_id} does not exist")
        if int(row["field_id"]) != int(field_id):
            raise ValueError(
                f"Reference series_id={policy.reference_series_id} belongs to field_id={row['field_id']}, not {field_id}"
            )
        if str(row.get("filter_name") or "").casefold() != policy.filter_name.casefold():
            raise ValueError(
                f"Reference series_id={policy.reference_series_id} filter={row.get('filter_name')!r} "
                f"does not match policy filter={policy.filter_name!r}"
            )
        ref_unit = int(row["instrument_unit_id"]) if row.get("instrument_unit_id") is not None else None
        if instrument_unit_id is not None and ref_unit != instrument_unit_id:
            raise ValueError(
                f"Reference series_id={policy.reference_series_id} instrument_unit_id={ref_unit} "
                f"does not match policy instrument_unit_id={instrument_unit_id}"
            )
        instrument_unit_id = ref_unit
    if instrument_unit_id is None:
        rows = session.execute(
            text(
                """
                SELECT DISTINCT instrument_unit_id
                FROM field_series
                WHERE field_id = :field_id
                  AND lower(COALESCE(filter_name, '')) = lower(:filter_name)
                  AND instrument_unit_id IS NOT NULL
                ORDER BY instrument_unit_id
                """
            ),
            {"field_id": int(field_id), "filter_name": policy.filter_name},
        ).scalars().all()
        units = [int(v) for v in rows]
        if len(units) != 1:
            raise ValueError(
                f"Policy filter={policy.filter_name!r} is ambiguous across instrument units {units}; "
                "provide reference series or explicit instrument unit in the policy"
            )
        instrument_unit_id = units[0]
    return Stage3Policy(
        filter_name=policy.filter_name,
        min_common_objects=policy.min_common_objects,
        reference_series_id=policy.reference_series_id,
        instrument_unit_id=instrument_unit_id,
        min_points_per_object=policy.min_points_per_object,
        clip_sigma=policy.clip_sigma,
        max_clip_iterations=policy.max_clip_iterations,
    )



def _recipe_readiness_rows(series: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in series:
        rows.append(
            {
                "series_id": candidate.series_id,
                "filter_name": candidate.filter_name,
                "stage3_status": candidate.status,
                "science_calibration_id": candidate.calibration_version_id,
                "science_calibration_name": getattr(candidate, "calibration_name", None),
                "pipeline_run_id": getattr(candidate, "pipeline_run_id", None),
                "pipeline_status": getattr(candidate, "pipeline_status", None),
                "recipe_compatibility": getattr(candidate, "recipe_compatibility", None),
                "mismatch_reasons": list(
                    getattr(candidate, "recipe_mismatch_reasons", ()) or ()
                ),
                "compatibility_notes": list(
                    getattr(candidate, "recipe_compatibility_notes", ()) or ()
                ),
                "provenance_source": getattr(candidate, "provenance_source", None),
            }
        )
    return rows

def _finish(
    result: FieldArchiveRunResult,
    out: Path,
    *,
    status: str,
    error: str | None = None,
) -> FieldArchiveRunResult:
    result.status = status
    result.finished_at = datetime.now().astimezone().isoformat()
    if error:
        result.errors.append(error)
    _write_reports(result, out)
    return result


def _write_reports(result: FieldArchiveRunResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "final_report.json"
    batches_path = out / "batches.csv"
    series_path = out / "series.csv"
    edges_path = out / "interseries_edges.csv"
    offsets_path = out / "interseries_offsets.csv"
    controls_path = out / "control_objects.csv"
    frame_qc_path = out / "frame_qc.csv"
    readiness_path = out / "stage2_recipe_readiness.csv"
    html_path = out / "report.html"

    result.outputs.update(
        {
            "final_report_json": str(json_path),
            "batches_csv": str(batches_path),
            "series_csv": str(series_path),
            "interseries_edges_csv": str(edges_path),
            "interseries_offsets_csv": str(offsets_path),
            "control_objects_csv": str(controls_path),
            "frame_qc_csv": str(frame_qc_path),
            "stage2_recipe_readiness_csv": str(readiness_path),
            "report_html": str(html_path),
        }
    )
    json_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    _write_csv(batches_path, result.batches)

    series_rows: list[dict[str, Any]] = []
    edge_rows: list[dict[str, Any]] = []
    offset_rows: list[dict[str, Any]] = []
    for scope in result.stage3:
        policy = scope.get("policy") or {}
        generation = scope.get("generation") or {}
        generation_id = generation.get("interseries_generation_id")
        analysis = scope.get("analysis") or {}
        for row in scope.get("series") or []:
            series_rows.append({"filter_name": policy.get("filter_name"), "generation_id": generation_id, **row})
        for row in scope.get("edges") or []:
            edge_rows.append({"filter_name": policy.get("filter_name"), "generation_id": generation_id, **row})
        graph = analysis.get("graph_solution") or {}
        for row in graph.get("offsets") or []:
            offset_rows.append({"filter_name": policy.get("filter_name"), "generation_id": generation_id, **row})
    _write_csv(series_path, series_rows)
    _write_csv(edges_path, edge_rows)
    _write_csv(offsets_path, offset_rows)

    control_rows: list[dict[str, Any]] = []
    for row in result.control_objects:
        control_rows.append(
            {
                "object_id": row.get("object_id"),
                "status": row.get("status"),
                "source_rows": row.get("rows_source_exact_m4"),
                "accepted_rows": row.get("accepted_rows"),
                "rejected_rows": row.get("rejected_rows"),
                "generation_ids": ",".join(str(v) for v in row.get("generation_ids") or []),
                "rejection_reasons": json.dumps(row.get("rejection_reasons") or {}, ensure_ascii=False),
                "csv_path": row.get("csv_path"),
                "summary_path": row.get("summary_path"),
            }
        )
    _write_csv(controls_path, control_rows)

    frame_qc_rows: list[dict[str, Any]] = []
    for run in result.stage2_runs:
        batch_date = run.get("batch_date")
        batch_path = run.get("batch_relative_path")
        for pipe in run.get("pipeline_results") or []:
            quality = pipe.get("quality_policy") or {}
            frame_qc_rows.append({
                "batch_date": batch_date,
                "batch_path": batch_path,
                "series_id": pipe.get("series_id"),
                "filter_name": pipe.get("filter_name"),
                "pipeline_status": pipe.get("status"),
                "science_status": pipe.get("science_status"),
                "total_points": pipe.get("total_points"),
                "accepted_points": pipe.get("accepted_points"),
                "good_frames": quality.get("photometric_frame_qc_good_frames"),
                "suspect_frames": quality.get("photometric_frame_qc_suspect_frames"),
                "bad_frames": quality.get("photometric_frame_qc_bad_frames"),
                "frames_with_spatial_systematic": quality.get("photometric_frame_qc_frames_with_spatial_systematic"),
                "median_residual_robust_sigma_mag": quality.get("photometric_frame_qc_median_residual_robust_sigma_mag"),
                "median_sigma_frame_systematic_mag": quality.get("photometric_frame_qc_median_sigma_frame_systematic_mag"),
                "max_sigma_frame_systematic_mag": quality.get("photometric_frame_qc_max_sigma_frame_systematic_mag"),
                "error_policy": quality.get("photometric_frame_qc_error_policy"),
                "science_calibration": pipe.get("science_calibration"),
            })
    _write_csv(frame_qc_path, frame_qc_rows)
    _write_csv(readiness_path, result.stage2_recipe_readiness)
    html_path.write_text(_render_html_report(result), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in columns})


def _render_html_report(result: FieldArchiveRunResult) -> str:
    stage3_rows = []
    for item in result.stage3:
        policy = item.get("policy") or {}
        analysis = item.get("analysis") or {}
        graph = analysis.get("graph_solution") or {}
        generation = item.get("generation") or {}
        stage3_rows.append(
            {
                "filter": policy.get("filter_name"),
                "min_common": policy.get("min_common_objects"),
                "reference": item.get("reference_series_id"),
                "series": len(analysis.get("selected_series_ids") or []),
                "edges": sum(1 for row in analysis.get("pairwise_offsets") or [] if row.get("status") == "OK"),
                "dof": graph.get("dof"),
                "reduced_chi2": graph.get("reduced_chi2"),
                "max_std_residual": graph.get("max_abs_standardized_residual"),
                "generation_id": generation.get("interseries_generation_id"),
                "verification": (item.get("verification") or {}).get("status"),
            }
        )
    batch_rows = [
        {
            "date": row.get("observation_date"),
            "path": row.get("relative_path"),
            "files": row.get("n_files"),
            "db_status": row.get("db_status"),
            "action": row.get("action"),
            "series": ",".join(str(v) for v in row.get("series_ids") or []),
            "reason": row.get("reason"),
        }
        for row in result.batches
    ]
    control_rows = [
        {
            "object": row.get("object_id"),
            "status": row.get("status"),
            "source": row.get("rows_source_exact_m4"),
            "accepted": row.get("accepted_rows"),
            "rejected": row.get("rejected_rows"),
            "reasons": json.dumps(row.get("rejection_reasons") or {}, ensure_ascii=False),
        }
        for row in result.control_objects
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Grandgousier field archive report</title>
<style>
body{{font-family:Arial,sans-serif;margin:2rem;line-height:1.35;color:#222}}table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}}th,td{{border:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}}th{{background:#f3f3f3}}code{{background:#f5f5f5;padding:.1rem .25rem}}.ok{{font-weight:bold}}.err{{color:#900}}
</style></head><body>
<h1>Grandgousier field archive report</h1>
<p><b>Status:</b> {html.escape(result.status)} &nbsp; <b>Version:</b> {html.escape(result.version)} &nbsp; <b>Algorithm:</b> <code>{html.escape(result.algorithm)}</code></p>
<p><b>Field:</b> {result.field_id} &nbsp; <b>Catalog:</b> <code>{html.escape(result.catalog_name)}</code><br><b>Processed root:</b> <code>{html.escape(result.processed_root)}</code></p>
<h2>Archive batches</h2>{_html_table(batch_rows)}
<h2>Stage-2 recipe readiness</h2>{_html_table(result.stage2_recipe_readiness)}
<h2>Stage 3</h2>{_html_table(stage3_rows)}
<h2>M5 controls</h2>{_html_table(control_rows)}
<h2>Warnings</h2>{_html_list(result.warnings)}
<h2>Errors</h2>{_html_list(result.errors, css_class="err")}
<p>Machine-readable provenance: <code>final_report.json</code>.</p>
</body></html>"""


def _html_table(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "<p>None.</p>"
    columns = list(rows[0])
    head = "".join(f"<th>{html.escape(str(key))}</th>" for key in columns)
    body = []
    for row in rows:
        body.append(
            "<tr>" + "".join(f"<td>{html.escape(str(row.get(key, '') if row.get(key) is not None else ''))}</td>" for key in columns) + "</tr>"
        )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _html_list(values: Sequence[str], *, css_class: str = "") -> str:
    if not values:
        return "<p>None.</p>"
    cls = f' class="{css_class}"' if css_class else ""
    return "<ul>" + "".join(f"<li{cls}>{html.escape(str(v))}</li>" for v in values) + "</ul>"


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _date_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text_value = str(value).strip()
    if len(text_value) >= 10 and text_value[4:5] == "-" and text_value[7:8] == "-":
        return text_value[:10]
    return None


def _normalize_date_token(value: Any) -> str:
    token = str(value).strip().replace("/", "-").replace("\\", "-")
    try:
        return date.fromisoformat(token).isoformat()
    except ValueError as exc:
        raise ValueError(f"Expected date YYYY-MM-DD, got {value!r}") from exc


def _relative_or_self(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
        return str(rel) if str(rel) else "."
    except ValueError:
        return str(path)


def _safe_token(value: str) -> str:
    raw = str(value).strip()
    token = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw)
    return token.strip("_") or "batch"
