from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grandgousier.db.models import (
    BatchInventoryItem,
    BatchJob,
    BatchJobAttempt,
    BatchRun,
)

COMPLETED_STATUSES = frozenset({"DONE", "DONE_WITH_WARNINGS"})


@dataclass(frozen=True, slots=True)
class BatchSyncResult:
    batch_run_id: int
    batch_uid: str
    plan_revision: int
    plan_changed: bool
    inventory_count: int
    queue_count: int
    imported_legacy_attempts: int
    status: str


@dataclass(frozen=True, slots=True)
class BatchAttemptStart:
    batch_job_attempt_id: int
    batch_job_id: int
    attempt_no: int


def _canonical_windowsish_path(value: str) -> str:
    return str(value).strip().replace("/", "\\").rstrip("\\").casefold()


def make_batch_uid(batch_name: str, root_path: str) -> str:
    payload = f"{batch_name.strip().casefold()}\0{_canonical_windowsish_path(root_path)}"
    return "batch_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _job_from_row(batch_run_id: int, plan_revision: int, queue_index: int, row: dict[str, str]) -> BatchJob:
    return BatchJob(
        batch_run_id=batch_run_id,
        job_id=str(row.get("JobId") or ""),
        object_name=row.get("ObjectName") or None,
        observation_name=row.get("ObservationName") or None,
        path=str(row.get("Path") or ""),
        relative_path=row.get("RelativePath") or None,
        fits_count=_int(row.get("FitsCount")),
        queue_index=queue_index,
        first_seen_revision=plan_revision,
        last_seen_revision=plan_revision,
        active_in_plan=True,
    )


def _inventory_from_row(batch_run_id: int, plan_revision: int, row: dict[str, str]) -> BatchInventoryItem:
    return BatchInventoryItem(
        batch_run_id=batch_run_id,
        plan_revision=plan_revision,
        job_id=str(row.get("JobId") or ""),
        object_name=row.get("ObjectName") or None,
        observation_name=row.get("ObservationName") or None,
        directory_name=row.get("DirectoryName") or None,
        path=str(row.get("Path") or ""),
        relative_path=row.get("RelativePath") or None,
        fits_count=_int(row.get("FitsCount")),
        classification=row.get("Classification") or None,
        exclude_reason=row.get("ExcludeReason") or None,
        selected=_truthy(row.get("Selected")),
    )


def _copy_attempt_to_job(job: BatchJob, attempt: BatchJobAttempt) -> None:
    job.current_attempt_no = int(attempt.attempt_no)
    job.current_status = str(attempt.status)
    job.current_health = str(attempt.health)
    job.current_science_status = attempt.science_status
    job.latest_pipeline_run_id = attempt.pipeline_run_id
    job.exit_code = attempt.exit_code
    job.catalog_name = attempt.catalog_name
    job.catalog_sources = int(attempt.catalog_sources or 0)
    job.pipeline_count = int(attempt.pipeline_count or 0)
    job.total_points = int(attempt.total_points or 0)
    job.accepted_points = int(attempt.accepted_points or 0)
    job.elapsed_sec = float(attempt.elapsed_sec or 0.0)
    job.manifest_path = attempt.manifest_path
    job.console_log_path = attempt.console_log_path
    job.message = attempt.message
    job.last_started_at = attempt.started_at
    job.last_finished_at = attempt.finished_at


def _recompute_batch_status(session: Session, batch: BatchRun) -> str:
    jobs = session.scalars(
        select(BatchJob).where(
            BatchJob.batch_run_id == batch.batch_run_id,
            BatchJob.active_in_plan.is_(True),
        )
    ).all()
    if not jobs:
        status = "PLANNED"
    elif any(job.current_status == "RUNNING" for job in jobs):
        status = "RUNNING"
    elif all(job.current_status in COMPLETED_STATUSES for job in jobs):
        status = "DONE"
    elif any(job.current_attempt_no > 0 for job in jobs):
        status = "PAUSED"
    else:
        status = "PLANNED"
    batch.status = status
    batch.last_activity_at = datetime.now(timezone.utc)
    if status == "DONE":
        batch.finished_at = datetime.now(timezone.utc)
    elif status in {"PLANNED", "RUNNING", "PAUSED"}:
        batch.finished_at = None
    return status


def _interrupt_stale_running_attempts(session: Session, batch_run_id: int) -> int:
    now = datetime.now(timezone.utc)
    attempts = session.scalars(
        select(BatchJobAttempt)
        .join(BatchJob, BatchJob.batch_job_id == BatchJobAttempt.batch_job_id)
        .where(
            BatchJob.batch_run_id == batch_run_id,
            BatchJobAttempt.status == "RUNNING",
            BatchJobAttempt.finished_at.is_(None),
        )
    ).all()
    for attempt in attempts:
        attempt.status = "INTERRUPTED"
        attempt.health = "FAILED_INTERRUPTED"
        attempt.finished_at = now
        if not attempt.message:
            attempt.message = "Previous batch controller exited before finishing this attempt"
        job = session.get(BatchJob, attempt.batch_job_id)
        if job is not None:
            _copy_attempt_to_job(job, attempt)
    return len(attempts)


def _import_legacy_state(
    session: Session,
    *,
    batch: BatchRun,
    state_csv: Path,
) -> int:
    if not state_csv.is_file():
        return 0
    existing_count = session.scalar(
        select(func.count(BatchJobAttempt.batch_job_attempt_id))
        .join(BatchJob, BatchJob.batch_job_id == BatchJobAttempt.batch_job_id)
        .where(BatchJob.batch_run_id == batch.batch_run_id)
    ) or 0
    if int(existing_count) > 0:
        return 0

    jobs = {
        str(job.job_id): job
        for job in session.scalars(
            select(BatchJob).where(BatchJob.batch_run_id == batch.batch_run_id)
        ).all()
    }
    imported = 0
    used_attempts: dict[int, set[int]] = {}
    for row in _read_csv(state_csv):
        job = jobs.get(str(row.get("JobId") or ""))
        if job is None:
            continue
        preferred_no = max(1, _int(row.get("Attempt"), 1))
        used = used_attempts.setdefault(job.batch_job_id, set())
        attempt_no = preferred_no
        while attempt_no in used:
            attempt_no += 1
        used.add(attempt_no)
        finished_at = _parse_datetime(row.get("Timestamp")) or datetime.now(timezone.utc)
        elapsed_sec = _float(row.get("ElapsedSec"))
        started_at = finished_at - timedelta(seconds=max(0.0, elapsed_sec))
        attempt = BatchJobAttempt(
            batch_job_id=job.batch_job_id,
            attempt_no=attempt_no,
            attempt_kind="LEGACY_IMPORT",
            pipeline_run_id=_int(row.get("PipelineRunId"), 0) or None,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=_int(row.get("ExitCode"), 0) if row.get("ExitCode") not in (None, "") else None,
            status=str(row.get("Status") or "UNKNOWN"),
            health=str(row.get("Health") or "UNKNOWN"),
            science_status=row.get("ScienceStatus") or None,
            catalog_name=row.get("CatalogName") or None,
            catalog_sources=_int(row.get("CatalogSources")),
            pipeline_count=_int(row.get("PipelineCount")),
            total_points=_int(row.get("TotalPoints")),
            accepted_points=_int(row.get("AcceptedPoints")),
            elapsed_sec=elapsed_sec,
            manifest_path=row.get("ManifestPath") or None,
            console_log_path=row.get("ConsoleLogPath") or None,
            message=row.get("Message") or None,
            metadata_json={"source": "legacy_state_csv"},
        )
        session.add(attempt)
        session.flush()
        imported += 1
        if attempt_no >= job.current_attempt_no:
            _copy_attempt_to_job(job, attempt)
    return imported


def sync_batch_plan(
    session: Session,
    *,
    batch_name: str,
    root_path: str,
    inventory_csv: str | Path,
    queue_csv: str | Path,
    script_name: str | None = None,
    script_version: str | None = None,
    config: dict[str, Any] | None = None,
    legacy_state_csv: str | Path | None = None,
) -> BatchSyncResult:
    inventory_path = Path(inventory_csv)
    queue_path = Path(queue_csv)
    inventory_rows = _read_csv(inventory_path)
    queue_rows = _read_csv(queue_path)
    inventory_digest = file_sha256(inventory_path)
    queue_digest = file_sha256(queue_path)
    batch_uid = make_batch_uid(batch_name, root_path)

    batch = session.scalar(select(BatchRun).where(BatchRun.batch_uid == batch_uid))
    now = datetime.now(timezone.utc)
    if batch is None:
        batch = BatchRun(
            batch_uid=batch_uid,
            batch_name=batch_name,
            root_path=root_path,
            script_name=script_name,
            script_version=script_version,
            plan_revision=1,
            inventory_sha256=inventory_digest,
            queue_sha256=queue_digest,
            config_json=dict(config or {}),
            status="PLANNED",
            planned_at=now,
            last_activity_at=now,
        )
        session.add(batch)
        session.flush()
        plan_changed = True
    else:
        plan_changed = (
            batch.inventory_sha256 != inventory_digest or batch.queue_sha256 != queue_digest
        )
        if plan_changed:
            batch.plan_revision = int(batch.plan_revision or 0) + 1
            batch.planned_at = now
        batch.batch_name = batch_name
        batch.root_path = root_path
        batch.script_name = script_name
        batch.script_version = script_version
        batch.inventory_sha256 = inventory_digest
        batch.queue_sha256 = queue_digest
        batch.config_json = dict(config or {})
        batch.last_activity_at = now

    revision = int(batch.plan_revision)
    snapshot_exists = session.scalar(
        select(func.count(BatchInventoryItem.batch_inventory_item_id)).where(
            BatchInventoryItem.batch_run_id == batch.batch_run_id,
            BatchInventoryItem.plan_revision == revision,
        )
    ) or 0
    if int(snapshot_exists) == 0:
        session.add_all(
            [_inventory_from_row(batch.batch_run_id, revision, row) for row in inventory_rows]
        )

    existing_jobs = {
        str(job.job_id): job
        for job in session.scalars(
            select(BatchJob).where(BatchJob.batch_run_id == batch.batch_run_id)
        ).all()
    }
    for job in existing_jobs.values():
        job.active_in_plan = False

    for queue_index, row in enumerate(queue_rows, start=1):
        job_id = str(row.get("JobId") or "")
        if not job_id:
            continue
        job = existing_jobs.get(job_id)
        if job is None:
            job = _job_from_row(batch.batch_run_id, revision, queue_index, row)
            session.add(job)
            session.flush()
            existing_jobs[job_id] = job
        else:
            job.object_name = row.get("ObjectName") or None
            job.observation_name = row.get("ObservationName") or None
            job.path = str(row.get("Path") or "")
            job.relative_path = row.get("RelativePath") or None
            job.fits_count = _int(row.get("FitsCount"))
            job.queue_index = queue_index
            job.last_seen_revision = revision
            job.active_in_plan = True

    _interrupt_stale_running_attempts(session, batch.batch_run_id)
    imported = 0
    if legacy_state_csv:
        imported = _import_legacy_state(
            session,
            batch=batch,
            state_csv=Path(legacy_state_csv),
        )
    status = _recompute_batch_status(session, batch)
    session.flush()
    return BatchSyncResult(
        batch_run_id=int(batch.batch_run_id),
        batch_uid=str(batch.batch_uid),
        plan_revision=revision,
        plan_changed=bool(plan_changed),
        inventory_count=len(inventory_rows),
        queue_count=len(queue_rows),
        imported_legacy_attempts=imported,
        status=status,
    )


def start_batch_attempt(
    session: Session,
    *,
    batch_run_id: int,
    job_id: str,
    manifest_path: str | None = None,
    console_log_path: str | None = None,
    attempt_kind: str = "EXECUTION",
    metadata: dict[str, Any] | None = None,
) -> BatchAttemptStart:
    batch = session.get(BatchRun, batch_run_id)
    if batch is None:
        raise ValueError(f"Unknown batch_run_id={batch_run_id}")
    job = session.scalar(
        select(BatchJob)
        .where(BatchJob.batch_run_id == batch_run_id, BatchJob.job_id == job_id)
        .with_for_update()
    )
    if job is None:
        raise ValueError(f"Unknown batch job {job_id!r} in batch_run_id={batch_run_id}")
    if not job.active_in_plan:
        raise ValueError(f"Batch job {job_id!r} is not active in the current plan")

    now = datetime.now(timezone.utc)
    stale = session.scalars(
        select(BatchJobAttempt).where(
            BatchJobAttempt.batch_job_id == job.batch_job_id,
            BatchJobAttempt.status == "RUNNING",
            BatchJobAttempt.finished_at.is_(None),
        )
    ).all()
    for prior in stale:
        prior.status = "INTERRUPTED"
        prior.health = "FAILED_INTERRUPTED"
        prior.finished_at = now
        prior.message = prior.message or "Superseded by a new batch attempt"

    attempt_no = int(job.current_attempt_no or 0) + 1
    attempt = BatchJobAttempt(
        batch_job_id=job.batch_job_id,
        attempt_no=attempt_no,
        attempt_kind=attempt_kind,
        started_at=now,
        status="RUNNING",
        health="RUNNING",
        manifest_path=manifest_path,
        console_log_path=console_log_path,
        metadata_json=dict(metadata or {}),
    )
    session.add(attempt)
    session.flush()

    job.current_attempt_no = attempt_no
    job.current_status = "RUNNING"
    job.current_health = "RUNNING"
    job.last_started_at = now
    job.last_finished_at = None
    job.manifest_path = manifest_path
    job.console_log_path = console_log_path
    batch.status = "RUNNING"
    batch.started_at = batch.started_at or now
    batch.finished_at = None
    batch.last_activity_at = now
    session.flush()
    return BatchAttemptStart(
        batch_job_attempt_id=int(attempt.batch_job_attempt_id),
        batch_job_id=int(job.batch_job_id),
        attempt_no=attempt_no,
    )


def finish_batch_attempt(
    session: Session,
    *,
    batch_job_attempt_id: int,
    exit_code: int | None,
    status: str,
    health: str,
    science_status: str | None = None,
    catalog_name: str | None = None,
    catalog_sources: int = 0,
    pipeline_count: int = 0,
    total_points: int = 0,
    accepted_points: int = 0,
    elapsed_sec: float = 0.0,
    manifest_path: str | None = None,
    console_log_path: str | None = None,
    message: str | None = None,
    pipeline_run_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = session.get(BatchJobAttempt, batch_job_attempt_id)
    if attempt is None:
        raise ValueError(f"Unknown batch_job_attempt_id={batch_job_attempt_id}")
    job = session.get(BatchJob, attempt.batch_job_id)
    if job is None:
        raise ValueError(f"Missing batch job for attempt {batch_job_attempt_id}")
    batch = session.get(BatchRun, job.batch_run_id)
    if batch is None:
        raise ValueError(f"Missing batch run for attempt {batch_job_attempt_id}")

    now = datetime.now(timezone.utc)
    attempt.finished_at = now
    attempt.exit_code = exit_code
    attempt.status = status
    attempt.health = health
    attempt.science_status = science_status or None
    attempt.catalog_name = catalog_name or None
    attempt.catalog_sources = int(catalog_sources or 0)
    attempt.pipeline_count = int(pipeline_count or 0)
    attempt.total_points = int(total_points or 0)
    attempt.accepted_points = int(accepted_points or 0)
    attempt.elapsed_sec = float(elapsed_sec or 0.0)
    if manifest_path is not None:
        attempt.manifest_path = manifest_path
    if console_log_path is not None:
        attempt.console_log_path = console_log_path
    attempt.message = message or None
    attempt.pipeline_run_id = pipeline_run_id
    attempt.metadata_json = dict(metadata or {})
    _copy_attempt_to_job(job, attempt)
    batch.last_activity_at = now
    batch_status = _recompute_batch_status(session, batch)
    session.flush()
    return {
        "batch_run_id": int(batch.batch_run_id),
        "batch_job_id": int(job.batch_job_id),
        "batch_job_attempt_id": int(attempt.batch_job_attempt_id),
        "attempt_no": int(attempt.attempt_no),
        "job_id": str(job.job_id),
        "status": str(attempt.status),
        "health": str(attempt.health),
        "batch_status": batch_status,
    }



def snapshot_batch_attempt_artifacts(
    session: Session,
    *,
    batch_job_attempt_id: int,
) -> list[int]:
    """Snapshot mutable batch-controller files into an immutable per-attempt tree."""

    from grandgousier.artifacts.registry import snapshot_file_artifact

    attempt = session.get(BatchJobAttempt, batch_job_attempt_id)
    if attempt is None:
        raise ValueError(f"Unknown batch_job_attempt_id={batch_job_attempt_id}")
    job = session.get(BatchJob, attempt.batch_job_id)
    if job is None:
        raise ValueError(f"Missing batch job for attempt {batch_job_attempt_id}")

    candidates: list[tuple[str | None, str, str]] = [
        (attempt.manifest_path, "manifest", "BATCH_PROCESS_MANIFEST"),
        (attempt.console_log_path, "console", "BATCH_CONSOLE_LOG"),
    ]
    existing_sources = [(Path(path), rel, kind) for path, rel, kind in candidates if path and Path(path).is_file()]
    if not existing_sources:
        return []

    first = existing_sources[0][0]
    if first.parent.name.casefold() in {"manifests", "console_logs"}:
        batch_dir = first.parent.parent
    else:
        batch_dir = first.parent
    run_dir = batch_dir / "artifacts" / f"attempt_{int(attempt.batch_job_attempt_id):08d}"

    artifact_ids: list[int] = []
    for source, rel_group, artifact_type in existing_sources:
        artifact = snapshot_file_artifact(
            session,
            source_path=source,
            run_dir=run_dir,
            relative_path=Path(rel_group) / source.name,
            pipeline_run_id=attempt.pipeline_run_id,
            batch_run_id=job.batch_run_id,
            batch_job_attempt_id=attempt.batch_job_attempt_id,
            artifact_type=artifact_type,
            artifact_role="PROVENANCE",
            metadata={
                "batch_job_id": int(job.batch_job_id),
                "job_id": str(job.job_id),
                "attempt_no": int(attempt.attempt_no),
                "attempt_kind": str(attempt.attempt_kind),
            },
        )
        artifact_ids.append(int(artifact.artifact_id))
    return artifact_ids

def _state_row(job: BatchJob, attempt: BatchJobAttempt) -> dict[str, Any]:
    return {
        "Timestamp": (attempt.finished_at or attempt.started_at).isoformat() if (attempt.finished_at or attempt.started_at) else "",
        "Attempt": int(attempt.attempt_no),
        "JobId": job.job_id,
        "ObjectName": job.object_name or "",
        "ObservationName": job.observation_name or "",
        "Path": job.path,
        "RelativePath": job.relative_path or "",
        "FitsCount": int(job.fits_count or 0),
        "ExitCode": "" if attempt.exit_code is None else int(attempt.exit_code),
        "Status": attempt.status,
        "Health": attempt.health,
        "ScienceStatus": attempt.science_status or "",
        "CatalogName": attempt.catalog_name or "",
        "CatalogSources": int(attempt.catalog_sources or 0),
        "PipelineCount": int(attempt.pipeline_count or 0),
        "TotalPoints": int(attempt.total_points or 0),
        "AcceptedPoints": int(attempt.accepted_points or 0),
        "ElapsedSec": float(attempt.elapsed_sec or 0.0),
        "ManifestPath": attempt.manifest_path or "",
        "ConsoleLogPath": attempt.console_log_path or "",
        "Message": attempt.message or "",
        "PipelineRunId": "" if attempt.pipeline_run_id is None else int(attempt.pipeline_run_id),
        "AttemptKind": attempt.attempt_kind,
        "BatchRunId": int(job.batch_run_id),
    }


def _summary_row(job: BatchJob) -> dict[str, Any]:
    return {
        "Timestamp": job.last_finished_at.isoformat() if job.last_finished_at else "",
        "Attempt": int(job.current_attempt_no or 0),
        "JobId": job.job_id,
        "ObjectName": job.object_name or "",
        "ObservationName": job.observation_name or "",
        "Path": job.path,
        "RelativePath": job.relative_path or "",
        "FitsCount": int(job.fits_count or 0),
        "ExitCode": "" if job.exit_code is None else int(job.exit_code),
        "Status": job.current_status,
        "Health": job.current_health,
        "ScienceStatus": job.current_science_status or "",
        "CatalogName": job.catalog_name or "",
        "CatalogSources": int(job.catalog_sources or 0),
        "PipelineCount": int(job.pipeline_count or 0),
        "TotalPoints": int(job.total_points or 0),
        "AcceptedPoints": int(job.accepted_points or 0),
        "ElapsedSec": float(job.elapsed_sec or 0.0),
        "ManifestPath": job.manifest_path or "",
        "ConsoleLogPath": job.console_log_path or "",
        "Message": job.message or "",
        "PipelineRunId": "" if job.latest_pipeline_run_id is None else int(job.latest_pipeline_run_id),
        "BatchRunId": int(job.batch_run_id),
    }


STATE_FIELDS = [
    "Timestamp", "Attempt", "JobId", "ObjectName", "ObservationName", "Path", "RelativePath",
    "FitsCount", "ExitCode", "Status", "Health", "ScienceStatus", "CatalogName", "CatalogSources",
    "PipelineCount", "TotalPoints", "AcceptedPoints", "ElapsedSec", "ManifestPath", "ConsoleLogPath",
    "Message", "PipelineRunId", "AttemptKind", "BatchRunId",
]
SUMMARY_FIELDS = [field for field in STATE_FIELDS if field != "AttemptKind"]


def _write_csv(path: str | Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_batch_csvs(
    session: Session,
    *,
    batch_run_id: int,
    state_csv: str | Path,
    summary_csv: str | Path,
) -> dict[str, Any]:
    batch = session.get(BatchRun, batch_run_id)
    if batch is None:
        raise ValueError(f"Unknown batch_run_id={batch_run_id}")
    jobs = session.scalars(
        select(BatchJob)
        .where(BatchJob.batch_run_id == batch_run_id, BatchJob.active_in_plan.is_(True))
        .order_by(BatchJob.queue_index, BatchJob.batch_job_id)
    ).all()
    job_by_id = {job.batch_job_id: job for job in jobs}
    attempts = session.scalars(
        select(BatchJobAttempt)
        .where(BatchJobAttempt.batch_job_id.in_(list(job_by_id) or [-1]))
        .order_by(BatchJobAttempt.batch_job_attempt_id)
    ).all()
    state_rows = [_state_row(job_by_id[a.batch_job_id], a) for a in attempts if a.batch_job_id in job_by_id]
    summary_rows = [_summary_row(job) for job in jobs]
    _write_csv(state_csv, state_rows, STATE_FIELDS)
    _write_csv(summary_csv, summary_rows, SUMMARY_FIELDS)
    counts = summarize_batch_jobs(jobs)
    return {
        "batch_run_id": int(batch_run_id),
        "status": batch.status,
        "state_rows": len(state_rows),
        "summary_rows": len(summary_rows),
        **counts,
    }


def summarize_batch_jobs(jobs: Iterable[BatchJob]) -> dict[str, int]:
    rows = list(jobs)
    return {
        "completed": sum(1 for job in rows if job.current_status in COMPLETED_STATUSES),
        "healthy": sum(1 for job in rows if job.current_health == "OK"),
        "warnings": sum(1 for job in rows if str(job.current_health).startswith("WARNING")),
        "failed": sum(1 for job in rows if str(job.current_health).startswith("FAILED")),
        "running": sum(1 for job in rows if job.current_status == "RUNNING"),
        "pending": sum(1 for job in rows if job.current_status == "PENDING"),
    }


def batch_run_rows(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    batches = session.scalars(select(BatchRun).order_by(BatchRun.batch_run_id.desc()).limit(limit)).all()
    rows: list[dict[str, Any]] = []
    for batch in batches:
        jobs = session.scalars(
            select(BatchJob).where(BatchJob.batch_run_id == batch.batch_run_id, BatchJob.active_in_plan.is_(True))
        ).all()
        rows.append(
            {
                "batch_run_id": int(batch.batch_run_id),
                "batch_uid": batch.batch_uid,
                "batch_name": batch.batch_name,
                "root_path": batch.root_path,
                "plan_revision": int(batch.plan_revision),
                "status": batch.status,
                "jobs": len(jobs),
                **summarize_batch_jobs(jobs),
                "last_activity_at": batch.last_activity_at,
            }
        )
    return rows


def batch_job_rows(session: Session, *, batch_run_id: int) -> list[dict[str, Any]]:
    jobs = session.scalars(
        select(BatchJob)
        .where(BatchJob.batch_run_id == batch_run_id, BatchJob.active_in_plan.is_(True))
        .order_by(BatchJob.queue_index, BatchJob.batch_job_id)
    ).all()
    return [
        {
            "batch_job_id": int(job.batch_job_id),
            "job_id": job.job_id,
            "object_name": job.object_name,
            "relative_path": job.relative_path,
            "fits_count": int(job.fits_count or 0),
            "attempt": int(job.current_attempt_no or 0),
            "status": job.current_status,
            "health": job.current_health,
            "science_status": job.current_science_status,
            "catalog_name": job.catalog_name,
            "total_points": int(job.total_points or 0),
            "accepted_points": int(job.accepted_points or 0),
            "pipeline_run_id": job.latest_pipeline_run_id,
            "elapsed_sec": float(job.elapsed_sec or 0.0),
        }
        for job in jobs
    ]
