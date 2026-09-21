from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from grandgousier import __version__

RUN_BUNDLE_FORMAT = "grandgousier_run_bundle_v1"
DIAGNOSIS_FORMAT = "grandgousier_run_diagnosis_v1"


@dataclass(frozen=True, slots=True)
class RecoveryAdvice:
    code: str
    severity: str
    summary: str
    actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True, slots=True)
class RunBundleDiagnosis:
    bundle_dir: str
    bundle_format: str
    complete: bool
    job_id: str | None
    job_status: str
    advice: RecoveryAdvice
    artifacts_found: tuple[str, ...]
    artifacts_missing: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DIAGNOSIS_FORMAT,
            "bundle_dir": self.bundle_dir,
            "bundle_format": self.bundle_format,
            "complete": self.complete,
            "job_id": self.job_id,
            "job_status": self.job_status,
            "advice": self.advice.to_dict(),
            "artifacts_found": list(self.artifacts_found),
            "artifacts_missing": list(self.artifacts_missing),
        }


def build_run_manifest(
    *,
    algorithm: str,
    job_id: str,
    config_fingerprint: str,
    started_at: str,
    public_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build secret-safe runtime provenance for a field-archive run bundle."""

    return {
        "format": RUN_BUNDLE_FORMAT,
        "algorithm": str(algorithm),
        "grandgousier_version": __version__,
        "job_id": str(job_id),
        "started_at": str(started_at),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_fingerprint": str(config_fingerprint),
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_release": platform.release(),
            "platform_machine": platform.machine(),
        },
        "mode": "execute" if bool(public_config.get("execute")) else "plan",
        "field_id": public_config.get("field_id"),
        "catalog_name": public_config.get("catalog_name"),
        "instrument_profile": public_config.get("instrument_profile"),
        "stage2_recipe": public_config.get("stage2_recipe"),
        "config": dict(public_config),
        "bundle_files": {
            "manifest": "run_manifest.json",
            "resolved_config": "job_config.json",
            "events": "job_events.jsonl",
            "summary": "job_summary.json",
            "diagnosis": "diagnosis.json",
            "failure": "failure.json",
        },
    }


def _readiness_failure_advice(payload: Mapping[str, Any]) -> RecoveryAdvice:
    readiness = payload.get("readiness") or {}
    failed = [
        row for row in readiness.get("checks", [])
        if isinstance(row, Mapping) and str(row.get("status")) == "FAIL"
    ]
    names = [str(row.get("name") or "unknown") for row in failed]
    details = [f"{row.get('name')}: {row.get('detail')}" for row in failed]
    actions: list[str] = []
    if "database" in names:
        actions.append("Check GRANDGOUSIER_DATABASE_URL, PostgreSQL reachability and credentials; then rerun doctor.")
    if "schema_migrations" in names:
        actions.append("Run `grandgousier schema-status`; review pending/checksum drift before any migrate-db action.")
    if "processed_root" in names:
        actions.append("Verify the configured processed_root exists and points to the intended calibrated FITS tree.")
    if "out_root" in names:
        actions.append("Verify out_root exists or can be created and is writable by the current user.")
    if "instrument_profile" in names:
        actions.append("Verify instrument_profile and packaged MASTER/RoboPhot resources with `grandgousier doctor`.")
    if not actions:
        actions.append("Run `grandgousier doctor --config <config>` and resolve every FAIL check before retrying.")
    return RecoveryAdvice(
        code="RUNTIME_READINESS_FAILED",
        severity="ERROR",
        summary=("Runtime preflight failed: " + "; ".join(details)) if details else "Runtime preflight failed.",
        actions=tuple(actions),
    )


def recovery_advice(payload: Mapping[str, Any]) -> RecoveryAdvice:
    """Translate one job summary into stable user-facing recovery guidance."""

    status = str(payload.get("status") or "UNKNOWN")
    error_type = str(payload.get("error_type") or "")
    error_message = str(payload.get("error_message") or "")
    workflow = payload.get("workflow") or {}
    workflow_errors = [str(value) for value in workflow.get("errors", [])] if isinstance(workflow, Mapping) else []
    workflow_warnings = [str(value) for value in workflow.get("warnings", [])] if isinstance(workflow, Mapping) else []
    combined = " | ".join([error_type, error_message, *workflow_errors, *workflow_warnings]).upper()

    if status == "READY":
        return RecoveryAdvice("RUN_READY", "INFO", "Run completed successfully; no recovery action is required.")
    if status == "READY_WITH_WARNINGS":
        return RecoveryAdvice(
            "RUN_READY_WITH_WARNINGS",
            "WARN",
            "Run completed, but warnings were recorded.",
            ("Review workflow warnings and report.html before using the products as final outputs.",),
        )
    if status == "PLANNED":
        return RecoveryAdvice(
            "PLAN_READY_FOR_REVIEW",
            "INFO",
            "Plan-only run completed successfully.",
            ("Review final_report.json/report.html; execute only after the planned batches and Stage-3 scope are acceptable.",),
        )
    if status == "INTERRUPTED" or error_type == "KeyboardInterrupt":
        return RecoveryAdvice(
            "JOB_INTERRUPTED",
            "WARN",
            "The job was interrupted before completion.",
            ("Inspect job_events.jsonl to identify the last completed stage, then rerun with --resume.",),
        )
    if error_type == "RuntimeReadinessError":
        return _readiness_failure_advice(payload)
    if error_type == "FieldArchiveJobLocked":
        return RecoveryAdvice(
            "FIELD_ARCHIVE_JOB_LOCKED",
            "WARN",
            "Another field-archive job holds the advisory lock for this scope.",
            ("Do not bypass the lock; wait for the active job to finish, inspect its bundle, then retry.",),
        )
    if "RECIPE_MISMATCH" in combined or "MISSING_PROVENANCE" in combined:
        return RecoveryAdvice(
            "STAGE2_PROVENANCE_CONFLICT",
            "ERROR",
            "Stage-2 provenance is not compatible with the required Stage-3 recipe.",
            (
                "Run `grandgousier status --field-id <id> --verbose` and identify the affected series.",
                "Do not let --resume rebuild it implicitly; rerun the affected series deliberately with an explicit series-id and the required recipe.",
            ),
        )
    if "NO_DETECTION_SUPPORT" in combined:
        return RecoveryAdvice(
            "STAGE2_DETECTION_SUPPORT_MISSING",
            "ERROR",
            "Stage-3 reference eligibility is blocked because required detection support is absent.",
            (
                "Keep the NO_DETECTION_SUPPORT scientific veto enabled.",
                "Rebuild only the affected Stage-2 series with the production recipe/detection branch, then rerun status.",
            ),
        )
    if "FAILED_PRECONDITION_CATALOG" in combined or "CATALOG_IDENTITY" in combined:
        return RecoveryAdvice(
            "CATALOG_PRECONDITION_FAILED",
            "ERROR",
            "Catalog identity/readiness precondition failed.",
            ("Run the field catalog-identity audit and repair the catalog/series identity conflict before retrying processing.",),
        )
    if "DISCONNECTED_AFTER_REFERENCE_QC" in combined:
        return RecoveryAdvice(
            "STAGE3_GRAPH_DISCONNECTED",
            "ERROR",
            "Stage-3 graph became disconnected after reference-quality filtering.",
            (
                "Inspect the inter-series plan/analysis and reference rejection diagnostics.",
                "Do not weaken scientific QC automatically; change thresholds only after reviewing the affected graph edges and references.",
            ),
        )
    if status == "FAILED_PRECONDITION":
        return RecoveryAdvice(
            "FIELD_ARCHIVE_PRECONDITION_FAILED",
            "ERROR",
            error_message or "A field-archive precondition failed.",
            ("Run doctor and status, then inspect failure.json and final_report.json before retrying.",),
        )
    if status == "FAILED":
        return RecoveryAdvice(
            "UNEXPECTED_PROCESSING_FAILURE",
            "ERROR",
            error_message or "The job failed unexpectedly.",
            (
                "Inspect failure.json for the retained traceback and job_events.jsonl for the last completed stage.",
                "Preserve the run bundle when reporting the failure; do not rerun destructive cleanup commands as a first response.",
            ),
        )
    return RecoveryAdvice(
        "UNKNOWN_JOB_STATE",
        "WARN",
        f"Unrecognized job status: {status}",
        ("Inspect job_summary.json and job_events.jsonl manually.",),
    )


def diagnosis_payload(job_summary: Mapping[str, Any]) -> dict[str, Any]:
    advice = recovery_advice(job_summary)
    return {
        "format": DIAGNOSIS_FORMAT,
        "job_id": job_summary.get("job_id"),
        "job_status": job_summary.get("status"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "advice": advice.to_dict(),
    }


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def diagnose_run_bundle(path: str | Path) -> RunBundleDiagnosis:
    """Inspect a current or legacy field-archive job bundle without touching the database."""

    source = Path(path)
    if source.is_dir():
        bundle_dir = source
        summary_path = bundle_dir / "job_summary.json"
    elif source.is_file() and source.name == "job_summary.json":
        bundle_dir = source.parent
        summary_path = source
    else:
        raise ValueError("diagnose-run expects a job directory or job_summary.json")
    if not summary_path.is_file():
        raise ValueError(f"Run bundle is missing job_summary.json: {bundle_dir}")

    summary = _load_json(summary_path)
    required = ("job_config.json", "job_events.jsonl", "job_summary.json")
    modern = ("run_manifest.json", "diagnosis.json")
    found = tuple(name for name in (*required, *modern, "failure.json") if (bundle_dir / name).is_file())
    missing_required = tuple(name for name in required if not (bundle_dir / name).is_file())
    missing_modern = tuple(name for name in modern if not (bundle_dir / name).is_file())
    manifest_path = bundle_dir / "run_manifest.json"
    bundle_format = "legacy_pre_0.5.59"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path)
        bundle_format = str(manifest.get("format") or "unknown")

    return RunBundleDiagnosis(
        bundle_dir=str(bundle_dir),
        bundle_format=bundle_format,
        complete=not missing_required and not missing_modern,
        job_id=(str(summary.get("job_id")) if summary.get("job_id") is not None else None),
        job_status=str(summary.get("status") or "UNKNOWN"),
        advice=recovery_advice(summary),
        artifacts_found=found,
        artifacts_missing=tuple([*missing_required, *missing_modern]),
    )
