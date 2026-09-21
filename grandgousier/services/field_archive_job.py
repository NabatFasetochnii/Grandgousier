from __future__ import annotations

import json
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import nullcontext

from grandgousier.orchestration.field_archive import FieldArchiveRunResult, run_field_archive_workflow
from grandgousier.services.events import JobEventWriter
from grandgousier.services.diagnostics import build_run_manifest, diagnosis_payload
from grandgousier.services.locks import FieldArchiveJobLocked, field_archive_advisory_lock
from grandgousier.services.readiness import RuntimeReadinessResult, check_runtime_readiness
from grandgousier.services.runtime import FieldArchiveJobConfig


SERVICE_ALGORITHM = "field_archive_job_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class FieldArchiveJobResult:
    job_id: str
    config_fingerprint: str
    status: str
    started_at: str
    finished_at: str | None = None
    readiness: RuntimeReadinessResult | None = None
    workflow_result: FieldArchiveRunResult | None = None
    error_type: str | None = None
    error_message: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": SERVICE_ALGORITHM,
            "job_id": self.job_id,
            "config_fingerprint": self.config_fingerprint,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "readiness": self.readiness.to_dict() if self.readiness else None,
            "workflow": self.workflow_result.to_dict() if self.workflow_result else None,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "artifacts": dict(self.artifacts),
        }


def _map_workflow_status(workflow: FieldArchiveRunResult) -> str:
    status = workflow.status
    if status == "READY":
        return "READY_WITH_WARNINGS" if workflow.warnings else "READY"
    if status in {"PLAN", "PLANNED"}:
        return "PLANNED"
    if status.startswith("FAILED_"):
        return "FAILED_PRECONDITION"
    return "FAILED"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _finalize_bundle(result: "FieldArchiveJobResult", *, summary_path: Path, diagnosis_path: Path) -> None:
    result.artifacts["diagnosis_json"] = str(diagnosis_path)
    _write_json(diagnosis_path, diagnosis_payload(result.to_dict()))
    _write_json(summary_path, result.to_dict())


def run_field_archive_job(
    config: FieldArchiveJobConfig,
    *,
    job_id: str | None = None,
    run_preflight: bool = True,
    acquire_lock: bool = True,
) -> FieldArchiveJobResult:
    """Run one server-facing field-archive job through the existing M8 workflow."""

    resolved_job_id = str(job_id or uuid.uuid4().hex)
    job_dir = Path(config.out_root) / "_jobs" / resolved_job_id
    events_path = job_dir / "job_events.jsonl"
    summary_path = job_dir / "job_summary.json"
    failure_path = job_dir / "failure.json"
    config_path = job_dir / "job_config.json"
    manifest_path = job_dir / "run_manifest.json"
    diagnosis_path = job_dir / "diagnosis.json"
    result = FieldArchiveJobResult(
        job_id=resolved_job_id,
        config_fingerprint=config.fingerprint(),
        status="RUNNING",
        started_at=_now(),
        artifacts={
            "job_dir": str(job_dir),
            "job_events_jsonl": str(events_path),
            "job_summary_json": str(summary_path),
            "job_config_json": str(config_path),
            "run_manifest_json": str(manifest_path),
            "diagnosis_json": str(diagnosis_path),
        },
    )
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.status = "FAILED_PRECONDITION"
        result.error_type = type(exc).__name__
        result.error_message = f"Cannot create job artifact directory {job_dir}: {exc}"
        result.finished_at = _now()
        result.artifacts = {}
        return result

    writer = JobEventWriter(events_path, job_id=resolved_job_id)
    _write_json(
        manifest_path,
        build_run_manifest(
            algorithm=SERVICE_ALGORITHM,
            job_id=resolved_job_id,
            config_fingerprint=result.config_fingerprint,
            started_at=result.started_at,
            public_config=config.public_dict(),
        ),
    )
    _write_json(config_path, {"config_fingerprint": result.config_fingerprint, "config": config.public_dict()})
    writer.emit("job_started", stage="job", status="RUNNING", config_fingerprint=result.config_fingerprint)

    try:
        if run_preflight:
            writer.emit("readiness_started", stage="readiness", status="RUNNING")
            readiness = check_runtime_readiness(
                database_url=config.database_url,
                processed_root=config.processed_root,
                out_root=config.out_root,
                instrument_profile=config.instrument_profile,
            )
            result.readiness = readiness
            writer.emit(
                "readiness_completed",
                stage="readiness",
                status=readiness.status,
                checks=[check.to_dict() for check in readiness.checks],
            )
            if not readiness.ready:
                result.status = "FAILED_PRECONDITION"
                result.error_type = "RuntimeReadinessError"
                result.error_message = "Runtime readiness checks failed"
                result.finished_at = _now()
                _write_json(
                    failure_path,
                    {
                        "job_id": result.job_id,
                        "status": result.status,
                        "error_type": result.error_type,
                        "error_message": result.error_message,
                        "readiness": readiness.to_dict(),
                    },
                )
                result.artifacts["failure_json"] = str(failure_path)
                writer.emit("job_failed", stage="job", status=result.status, error_type=result.error_type)
                _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
                return result

        lock_context = field_archive_advisory_lock(config) if acquire_lock else nullcontext(None)
        with lock_context as lock_key:
            if lock_key is not None:
                writer.emit("lock_acquired", stage="concurrency", status="OK", lock_key=lock_key)
            writer.emit("workflow_started", stage="m8", status="RUNNING")

            def emit_m8_event(event: str, payload: dict[str, Any]) -> None:
                details = dict(payload)
                stage = str(details.pop("stage", "m8"))
                status = details.pop("status", None)
                writer.emit(event, stage=stage, status=status, **details)

            workflow = run_field_archive_workflow(
                **config.workflow_kwargs(),
                event_callback=emit_m8_event,
            )
            result.workflow_result = workflow
            if lock_key is not None:
                writer.emit("lock_releasing", stage="concurrency", status="OK", lock_key=lock_key)
        result.status = _map_workflow_status(workflow)
        result.finished_at = _now()
        writer.emit(
            "workflow_completed",
            stage="m8",
            status=workflow.status,
            mapped_job_status=result.status,
            stage2_runs=len(workflow.stage2_runs),
            stage3_scopes=len(workflow.stage3),
            controls=len(workflow.control_objects),
            outputs=workflow.outputs,
        )
        for key in ("final_report_json", "report_html"):
            if workflow.outputs.get(key):
                result.artifacts[f"workflow_{key}"] = str(workflow.outputs[key])
        if result.status not in {"READY", "READY_WITH_WARNINGS", "PLANNED"}:
            result.error_type = "FieldArchiveWorkflowError"
            result.error_message = "; ".join(workflow.errors) or f"M8 status={workflow.status}"
            _write_json(
                failure_path,
                {
                    "job_id": result.job_id,
                    "status": result.status,
                    "error_type": result.error_type,
                    "error_message": result.error_message,
                    "workflow_status": workflow.status,
                    "workflow_errors": workflow.errors,
                    "workflow_warnings": workflow.warnings,
                },
            )
            result.artifacts["failure_json"] = str(failure_path)
            writer.emit("job_failed", stage="job", status=result.status, error_type=result.error_type)
        else:
            writer.emit("job_completed", stage="job", status=result.status, warning_count=len(workflow.warnings))
        _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
        return result
    except KeyboardInterrupt:
        result.status = "INTERRUPTED"
        result.error_type = "KeyboardInterrupt"
        result.error_message = "Field-archive job interrupted"
        result.finished_at = _now()
        _write_json(
            failure_path,
            {
                "job_id": result.job_id,
                "status": result.status,
                "error_type": result.error_type,
                "error_message": result.error_message,
            },
        )
        result.artifacts["failure_json"] = str(failure_path)
        writer.emit("job_interrupted", stage="job", status=result.status)
        _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
        return result
    except FieldArchiveJobLocked as exc:
        result.status = "FAILED_PRECONDITION"
        result.error_type = type(exc).__name__
        result.error_message = str(exc)
        result.finished_at = _now()
        _write_json(
            failure_path,
            {
                "job_id": result.job_id,
                "status": result.status,
                "error_type": result.error_type,
                "error_message": result.error_message,
            },
        )
        result.artifacts["failure_json"] = str(failure_path)
        writer.emit("job_blocked", stage="concurrency", status=result.status, error_type=result.error_type)
        _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
        return result
    except ValueError as exc:
        result.status = "FAILED_PRECONDITION"
        result.error_type = type(exc).__name__
        result.error_message = str(exc)
        result.finished_at = _now()
        _write_json(
            failure_path,
            {
                "job_id": result.job_id,
                "status": result.status,
                "error_type": result.error_type,
                "error_message": result.error_message,
            },
        )
        result.artifacts["failure_json"] = str(failure_path)
        writer.emit("job_failed", stage="job", status=result.status, error_type=result.error_type)
        _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
        return result
    except Exception as exc:  # noqa: BLE001 - service boundary must retain a failure artifact.
        result.status = "FAILED"
        result.error_type = type(exc).__name__
        result.error_message = str(exc)
        result.finished_at = _now()
        _write_json(
            failure_path,
            {
                "job_id": result.job_id,
                "status": result.status,
                "error_type": result.error_type,
                "error_message": result.error_message,
                "traceback": traceback.format_exc(),
            },
        )
        result.artifacts["failure_json"] = str(failure_path)
        writer.emit("job_failed", stage="job", status=result.status, error_type=result.error_type)
        _finalize_bundle(result, summary_path=summary_path, diagnosis_path=diagnosis_path)
        return result
