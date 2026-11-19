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
from grandgousier.observing_day import (
    DEFAULT_OBSERVING_DAY_POLICY,
    ObservingDayPolicy,
    operational_date_iso,
    resolve_site_timezone,
)
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
    operational_date: str | None = None
    paths: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    relative_parents: tuple[str, ...] = ()
    site_timezones: dict[str, str] = field(default_factory=dict)
    pipeline_run_ids: tuple[int, ...] = ()
    series_run_ids: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["object_names"] = list(self.object_names)
        row["filters"] = list(self.filters)
        row["series_ids"] = list(self.series_ids)
        row["resume_series_ids"] = list(self.resume_series_ids)
        row["paths"] = list(self.paths)
        row["parents"] = list(self.parents)
        row["relative_parents"] = list(self.relative_parents)
        row["site_timezones"] = dict(self.site_timezones)
        row["pipeline_run_ids"] = list(self.pipeline_run_ids)
        row["series_run_ids"] = [dict(value) for value in self.series_run_ids]
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
    observing_day_policy: dict[str, Any] = field(default_factory=dict)
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
            "observing_day_policy": self.observing_day_policy,
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


def _series_run_rows(
    series_ids: Iterable[int],
    ready_by_series: dict[int, Any],
) -> tuple[tuple[int, ...], tuple[dict[str, Any], ...]]:
    rows: list[dict[str, Any]] = []
    for series_id in sorted({int(value) for value in series_ids}):
        candidate = ready_by_series.get(series_id)
        run_id = getattr(candidate, "pipeline_run_id", None) if candidate is not None else None
        rows.append(
            {
                "series_id": series_id,
                "pipeline_run_id": int(run_id) if run_id is not None else None,
            }
        )
    run_ids = tuple(
        sorted(
            {
                int(row["pipeline_run_id"])
                for row in rows
                if row["pipeline_run_id"] is not None
            }
        )
    )
    return run_ids, tuple(rows)


def _series_ids_by_archive_path(
    session: Any,
    *,
    paths: Sequence[str],
    field_id: int,
) -> dict[str, set[int]]:
    if not paths:
        return {}
    rows = session.execute(
        text(
            """
            SELECT fl.locator AS raw_path, fsf.series_id
            FROM frame_locations fl
            JOIN field_series_frames fsf ON fsf.frame_id = fl.frame_id
            JOIN field_series fs ON fs.series_id = fsf.series_id
            WHERE fl.is_primary
              AND fl.locator = ANY(:paths)
              AND fs.field_id = :field_id
            ORDER BY fl.locator, fsf.series_id
            """
        ),
        {"paths": list(paths), "field_id": int(field_id)},
    ).mappings().all()
    by_path: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        by_path[str(row["raw_path"])].add(int(row["series_id"]))
    return by_path


def _attach_stage2_mapping(
    batches: Sequence[ArchiveBatch],
    batch_reports: Sequence[dict[str, Any]],
    *,
    series_ids_by_path: dict[str, set[int]],
    pipeline_results: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    reports_by_scope = {
        (
            str(report.get("operational_date")),
            tuple(str(path) for path in report.get("paths") or ()),
        ): report
        for report in batch_reports
    }
    run_id_by_series: dict[int, int | None] = {}
    for row in pipeline_results:
        series_id = row.get("series_id")
        if series_id is None:
            continue
        run_id = row.get("pipeline_run_id")
        run_id_by_series[int(series_id)] = int(run_id) if run_id is not None else None

    date_mappings: list[dict[str, Any]] = []
    for batch in batches:
        report = reports_by_scope[(str(batch.operational_date), tuple(batch.paths))]
        series_ids = set(int(value) for value in batch.series_ids)
        for path in batch.paths:
            series_ids.update(series_ids_by_path.get(str(path), set()))
        ordered_series_ids = sorted(series_ids)
        prior_run_ids = {
            int(row["series_id"]): row.get("pipeline_run_id")
            for row in report.get("series_run_ids") or []
        }
        series_runs = [
            {
                "series_id": series_id,
                "pipeline_run_id": (
                    run_id_by_series[series_id]
                    if series_id in run_id_by_series
                    else prior_run_ids.get(series_id)
                ),
            }
            for series_id in ordered_series_ids
        ]
        pipeline_run_ids = sorted(
            {
                int(row["pipeline_run_id"])
                for row in series_runs
                if row["pipeline_run_id"] is not None
            }
        )
        report["series_ids"] = ordered_series_ids
        report["series_run_ids"] = series_runs
        report["pipeline_run_ids"] = pipeline_run_ids
        date_mappings.append(
            {
                "operational_date": batch.operational_date,
                "relative_path": batch.relative_path,
                "paths": list(batch.paths),
                "parents": list(batch.parents),
                "series_ids": ordered_series_ids,
                "series_run_ids": series_runs,
                "pipeline_run_ids": pipeline_run_ids,
            }
        )
    return date_mappings


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
    observing_day_policy: ObservingDayPolicy | None = None,
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
    day_policy = observing_day_policy or DEFAULT_OBSERVING_DAY_POLICY
    if not isinstance(day_policy, ObservingDayPolicy):
        day_policy = ObservingDayPolicy.from_mapping(day_policy)

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
        observing_day_policy=day_policy.to_dict(),
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

        raw_batches = _group_detected_by_parent(root, detected, policy=day_policy)
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
                obs_date = item["operational_date"]
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
                batch_pipeline_run_ids, batch_series_run_ids = _series_run_rows(
                    state["series_ids"], ready_by_series
                )
                batches.append(
                    ArchiveBatch(
                        path=item["path"],
                        relative_path=item["relative_path"],
                        observation_date=obs_date,
                        operational_date=obs_date,
                        n_files=len(item["paths"]),
                        object_names=tuple(item["object_names"]),
                        filters=tuple(item["filters"]),
                        action=action,
                        db_status=state["status"],
                        indexed_files=state["indexed_files"],
                        series_ids=tuple(state["series_ids"]),
                        resume_series_ids=tuple(state.get("resume_series_ids") or ()),
                        reason=state.get("reason"),
                        paths=tuple(item["paths"]),
                        parents=tuple(item["parents"]),
                        relative_parents=tuple(item["relative_parents"]),
                        site_timezones=dict(item["site_timezones"]),
                        pipeline_run_ids=batch_pipeline_run_ids,
                        series_run_ids=batch_series_run_ids,
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
        stage2_batches = [
            batch for batch in batches
            if batch.action in {"RUN_NEW", "RESUME_INCOMPLETE"}
        ]
        unsupported_batches = [
            batch for batch in batches
            if batch.action not in {
                "SKIP_READY", "SKIP_EXCLUDED", "SKIP_TOO_SMALL",
                "RUN_NEW", "RESUME_INCOMPLETE",
            }
        ]
        if unsupported_batches:
            details = "; ".join(
                f"{batch.relative_path}: {batch.action}"
                for batch in unsupported_batches
            )
            return _finish(
                result,
                out,
                status="FAILED_BATCH_STATE",
                error=f"Unsupported field-archive batch action: {details}",
            )
        if stage2_batches:
            stage2_paths = tuple(
                sorted({path for batch in stage2_batches for path in batch.paths})
            )
            resume_series_ids = sorted(
                {
                    int(series_id)
                    for batch in stage2_batches
                    if batch.action == "RESUME_INCOMPLETE"
                    for series_id in batch.resume_series_ids
                }
            )
            operational_dates = sorted(
                {str(batch.operational_date) for batch in stage2_batches}
            )
            relative_paths = sorted({batch.relative_path for batch in stage2_batches})
            batch_out = out / "stage2" / f"field_{int(field_id)}"
            manifest = batch_out / "manifest.json"
            _emit_workflow_event(
                event_callback,
                "stage2_batch_started",
                stage="stage2",
                status="RUNNING",
                consolidated=True,
                field_id=int(field_id),
                batch_date=operational_dates[0] if len(operational_dates) == 1 else None,
                batch_operational_dates=operational_dates,
                batch_relative_paths=relative_paths,
                n_files=len(stage2_paths),
                batch_paths=list(stage2_paths),
            )
            with Session() as session:
                archive_result = process_archive_path(
                    session,
                    root=str(root),
                    catalog_name=None if str(catalog_name) == "gaia_dr3" else str(catalog_name),
                    execute=True,
                    instrument_profile=instrument_profile,
                    recursive=False,
                    exact_paths=stage2_paths,
                    series_ids=None,
                    resume_series_ids=resume_series_ids,
                    required_field_id=int(field_id),
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
            run_row["consolidated_field_major"] = True
            run_row["required_field_id"] = int(field_id)
            run_row["batch_relative_paths"] = relative_paths
            run_row["batch_date"] = operational_dates[0] if len(operational_dates) == 1 else None
            run_row["batch_operational_dates"] = operational_dates
            run_row["batch_paths"] = list(stage2_paths)
            run_row["batch_parents"] = sorted(
                {parent for batch in stage2_batches for parent in batch.parents},
                key=str.casefold,
            )
            run_row["observing_day_policy"] = day_policy.to_dict()
            pipeline_results = list(getattr(archive_result, "pipeline_results", []) or [])
            with Session() as session:
                series_ids_by_path = _series_ids_by_archive_path(
                    session,
                    paths=stage2_paths,
                    field_id=int(field_id),
                )
            batch_series_run_map = _attach_stage2_mapping(
                stage2_batches,
                result.batches,
                series_ids_by_path=series_ids_by_path,
                pipeline_results=pipeline_results,
            )
            run_row["batch_series_run_map"] = batch_series_run_map
            result.stage2_runs.append(run_row)
            processed_ids = [
                int(value)
                for value in (getattr(archive_result, "processed_series_ids", []) or [])
            ]
            processed_series_ids.update(processed_ids)
            _emit_workflow_event(
                event_callback,
                "stage2_batch_completed",
                stage="stage2",
                status=archive_result.status,
                consolidated=True,
                field_id=int(field_id),
                batch_operational_dates=operational_dates,
                processed_series_ids=processed_ids,
                failed_files=int(getattr(archive_result, "failed_files", 0)),
                batch_paths=list(stage2_paths),
                batch_series_run_map=batch_series_run_map,
            )
            if archive_result.status not in {"DONE", "DONE_WITH_WARNINGS"}:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2",
                    error=f"Consolidated Stage 2 failed: {archive_result.status}",
                )
            if int(getattr(archive_result, "failed_files", 0)) > 0:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2_FILES",
                    error=(
                        "Consolidated Stage 2 reported "
                        f"{getattr(archive_result, 'failed_files', 0)} failed FITS files"
                    ),
                )
            failed_pipelines = [
                row for row in pipeline_results
                if str(row.get("status") or "").upper() not in {"DONE", "DONE_WITH_WARNINGS"}
            ]
            if failed_pipelines:
                return _finish(
                    result,
                    out,
                    status="FAILED_STAGE2_PIPELINE",
                    error="Pipeline failure in consolidated Stage 2",
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


def _group_detected_by_parent(
    root: Path,
    detected: Sequence[Any],
    *,
    policy: ObservingDayPolicy = DEFAULT_OBSERVING_DAY_POLICY,
) -> list[dict[str, Any]]:
    """Group exact FITS paths by site-local operational date across parent directories."""

    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in detected:
        path = str(row.path)
        site = getattr(row, "site", None)
        date_obs = getattr(row, "date_obs", None)
        site_label = str(site).strip() if site is not None and str(site).strip() else "<missing>"
        try:
            if date_obs is None:
                raise ValueError("DATE-OBS is missing")
            operation_date = operational_date_iso(date_obs, site, policy)
        except ValueError as exc:
            raise ValueError(
                f"Cannot assign an operational date for site {site_label!r}, "
                f"FITS path {path}, DATE-OBS={date_obs!r}: {exc}"
            ) from exc
        if operation_date is None:
            raise ValueError(
                f"Cannot assign an operational date for site {site_label!r}, "
                f"FITS path {path}, DATE-OBS={date_obs!r}: DATE-OBS is missing"
            )
        grouped[operation_date].append(row)

    result: list[dict[str, Any]] = []
    for observation_date in sorted(grouped):
        rows = grouped[observation_date]
        paths = sorted({str(row.path) for row in rows})
        parents = sorted({str(Path(path).parent) for path in paths}, key=str.casefold)
        relative_parents = sorted(
            {_relative_or_self(Path(parent), root) for parent in parents},
            key=str.casefold,
        )
        site_timezones: dict[str, str] = {}
        for row in rows:
            site = getattr(row, "site", None)
            site_label = str(site).strip() if site is not None and str(site).strip() else "<missing>"
            site_timezones[site_label] = resolve_site_timezone(site, policy)

        series_like_counts: dict[tuple[str, ...], int] = defaultdict(int)
        for row in rows:
            series_like_counts[_series_like_group_key(row)] += 1
        result.append(
            {
                # The date is the batch identity; exact_paths carry the only input scope.
                "path": str(root),
                "relative_path": observation_date,
                "observation_date": observation_date,
                "operational_date": observation_date,
                "observation_dates": [observation_date],
                "date_ambiguous": False,
                "paths": paths,
                "parents": parents,
                "relative_parents": relative_parents,
                "site_timezones": dict(sorted(site_timezones.items(), key=lambda pair: pair[0].casefold())),
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
        operational_date=item["operational_date"],
        n_files=len(item["paths"]),
        object_names=tuple(item["object_names"]),
        filters=tuple(item["filters"]),
        action="SKIP_EXCLUDED",
        db_status="EXCLUDED",
        indexed_files=0,
        series_ids=(),
        resume_series_ids=(),
        reason=reason,
        paths=tuple(item["paths"]),
        parents=tuple(item["parents"]),
        relative_parents=tuple(item["relative_parents"]),
        site_timezones=dict(item["site_timezones"]),
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
        dates_by_series: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for date_mapping in run.get("batch_series_run_map") or []:
            for series_id in date_mapping.get("series_ids") or []:
                dates_by_series[int(series_id)].append(date_mapping)
        for pipe in run.get("pipeline_results") or []:
            quality = pipe.get("quality_policy") or {}
            series_id = pipe.get("series_id")
            mappings = dates_by_series.get(int(series_id), []) if series_id is not None else []
            if not mappings:
                mappings = [{
                    "operational_date": run.get("batch_date"),
                    "relative_path": None,
                }]
            for date_mapping in mappings:
                frame_qc_rows.append({
                    "batch_date": date_mapping.get("operational_date"),
                    "batch_path": date_mapping.get("relative_path"),
                    "series_id": series_id,
                    "filter_name": pipe.get("filter_name"),
                    "pipeline_run_id": pipe.get("pipeline_run_id"),
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
            "parents": json.dumps(row.get("parents") or [], ensure_ascii=False),
            "paths": json.dumps(row.get("paths") or [], ensure_ascii=False),
            "series": ",".join(str(v) for v in row.get("series_ids") or []),
            "pipeline_runs": ",".join(str(v) for v in row.get("pipeline_run_ids") or []),
            "series_run_ids": json.dumps(row.get("series_run_ids") or [], ensure_ascii=False),
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
