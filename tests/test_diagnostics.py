from __future__ import annotations

import json
from pathlib import Path

from grandgousier import cli
from grandgousier.orchestration.field_archive import FieldArchiveRunResult, Stage3Policy
from grandgousier.services.diagnostics import (
    RUN_BUNDLE_FORMAT,
    diagnose_run_bundle,
    recovery_advice,
)
from grandgousier.services.field_archive_job import run_field_archive_job
from grandgousier.services.readiness import ReadinessCheck, RuntimeReadinessResult
from grandgousier.services.runtime import FieldArchiveJobConfig


def _config(tmp_path: Path) -> FieldArchiveJobConfig:
    processed = tmp_path / "processed"
    processed.mkdir(exist_ok=True)
    return FieldArchiveJobConfig(
        database_url="postgresql+psycopg://user:secret@db/grandgousier",
        processed_root=str(processed),
        field_id=33,
        catalog_name="shared_catalog",
        stage3_policies=(Stage3Policy("V", 15, 88),),
        out_root=str(tmp_path / "reports"),
        execute=False,
    )


def test_field_archive_job_writes_secret_safe_run_bundle(monkeypatch, tmp_path: Path):
    config = _config(tmp_path)
    ready = RuntimeReadinessResult(
        status="READY",
        checks=(ReadinessCheck("database", "OK", "test"),),
    )
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.check_runtime_readiness",
        lambda **kwargs: ready,
    )
    workflow = FieldArchiveRunResult(
        field_id=33,
        processed_root=config.processed_root,
        catalog_name=config.catalog_name,
        execute=False,
        resume=True,
        stage2_recipe="stage3_scale_v1",
        policies=list(config.stage3_policies),
        control_object_ids=[],
        status="PLAN",
    )
    workflow.outputs = {"final_report_json": "final_report.json", "report_html": "report.html"}
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.run_field_archive_workflow",
        lambda **kwargs: workflow,
    )

    result = run_field_archive_job(config, job_id="bundle-plan", acquire_lock=False)
    bundle = Path(result.artifacts["job_dir"])
    manifest_path = bundle / "run_manifest.json"
    diagnosis_path = bundle / "diagnosis.json"
    summary_path = bundle / "job_summary.json"

    assert manifest_path.is_file()
    assert diagnosis_path.is_file()
    assert summary_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert manifest["format"] == RUN_BUNDLE_FORMAT
    assert manifest["grandgousier_version"]
    assert manifest["mode"] == "plan"
    assert manifest["field_id"] == 33
    assert "secret" not in manifest_path.read_text(encoding="utf-8")
    assert diagnosis["advice"]["code"] == "PLAN_READY_FOR_REVIEW"
    assert summary["artifacts"]["run_manifest_json"] == str(manifest_path)
    assert summary["artifacts"]["diagnosis_json"] == str(diagnosis_path)

    inspected = diagnose_run_bundle(bundle)
    assert inspected.complete is True
    assert inspected.bundle_format == RUN_BUNDLE_FORMAT
    assert inspected.advice.code == "PLAN_READY_FOR_REVIEW"


def test_failed_readiness_gets_specific_recovery(monkeypatch, tmp_path: Path):
    config = _config(tmp_path)
    failed = RuntimeReadinessResult(
        status="FAILED",
        checks=(
            ReadinessCheck("database", "OK", "connected"),
            ReadinessCheck("schema_migrations", "FAIL", "pending=mvp_future"),
        ),
    )
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.check_runtime_readiness",
        lambda **kwargs: failed,
    )
    result = run_field_archive_job(config, job_id="bundle-fail", acquire_lock=False)
    diagnosis = json.loads(Path(result.artifacts["diagnosis_json"]).read_text(encoding="utf-8"))
    advice = diagnosis["advice"]
    assert result.status == "FAILED_PRECONDITION"
    assert advice["code"] == "RUNTIME_READINESS_FAILED"
    assert any("schema-status" in action for action in advice["actions"])


def test_recovery_advice_recognizes_scientific_preconditions():
    recipe = recovery_advice(
        {
            "status": "FAILED_PRECONDITION",
            "error_type": "FieldArchiveWorkflowError",
            "error_message": "series 12 RECIPE_MISMATCH",
            "workflow": {"errors": []},
        }
    )
    assert recipe.code == "STAGE2_PROVENANCE_CONFLICT"
    assert any("--resume" in action for action in recipe.actions)

    graph = recovery_advice(
        {
            "status": "FAILED_PRECONDITION",
            "error_type": "FieldArchiveWorkflowError",
            "error_message": "DISCONNECTED_AFTER_REFERENCE_QC",
            "workflow": {"errors": []},
        }
    )
    assert graph.code == "STAGE3_GRAPH_DISCONNECTED"


def test_diagnose_run_bundle_supports_legacy_jobs(tmp_path: Path):
    bundle = tmp_path / "legacy"
    bundle.mkdir()
    (bundle / "job_config.json").write_text("{}", encoding="utf-8")
    (bundle / "job_events.jsonl").write_text("", encoding="utf-8")
    (bundle / "job_summary.json").write_text(
        json.dumps({"job_id": "old", "status": "FAILED", "error_message": "boom"}),
        encoding="utf-8",
    )
    result = diagnose_run_bundle(bundle)
    assert result.bundle_format == "legacy_pre_0.5.59"
    assert result.complete is False
    assert "run_manifest.json" in result.artifacts_missing
    assert result.advice.code == "UNEXPECTED_PROCESSING_FAILURE"


def test_diagnose_run_cli(monkeypatch, tmp_path: Path, capsys):
    bundle = tmp_path / "job"
    bundle.mkdir()
    (bundle / "job_config.json").write_text("{}", encoding="utf-8")
    (bundle / "job_events.jsonl").write_text("", encoding="utf-8")
    (bundle / "job_summary.json").write_text(
        json.dumps({"job_id": "x", "status": "PLANNED"}),
        encoding="utf-8",
    )
    code = cli.main(["diagnose-run", str(bundle)])
    assert code == 0
    output = capsys.readouterr().out
    assert "PLAN_READY_FOR_REVIEW" in output
    assert "Recommended actions:" in output
