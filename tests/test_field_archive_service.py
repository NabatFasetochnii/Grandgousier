from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.orchestration.field_archive import FieldArchiveRunResult, Stage3Policy
from grandgousier.services.field_archive_job import run_field_archive_job
from grandgousier.services.readiness import ReadinessCheck, RuntimeReadinessResult
from grandgousier.services.runtime import FieldArchiveJobConfig


def test_job_config_source_precedence_and_policy_parsing(tmp_path: Path):
    cfg = tmp_path / "job.yaml"
    cfg.write_text(
        """
processed_root: from_file
field_id: 33
catalog_name: file_catalog
execute: true
resume: false
stage3_policies:
  - filter: V
    min_common: 15
    reference_series_id: 76
out_root: reports/from_file
""".strip(),
        encoding="utf-8",
    )
    env = {
        "GRANDGOUSIER_DATABASE_URL": "postgresql+psycopg://env/db",
        "GRANDGOUSIER_FIELD_ARCHIVE_PROCESSED_ROOT": "from_env",
        "GRANDGOUSIER_FIELD_ARCHIVE_CATALOG_NAME": "env_catalog",
        "GRANDGOUSIER_FIELD_ARCHIVE_STAGE3_POLICIES": "R:20:77",
    }
    config = FieldArchiveJobConfig.from_sources(
        config_file=cfg,
        environ=env,
        overrides={"processed_root": "from_cli", "execute": False},
    )
    assert config.processed_root == "from_cli"
    assert config.catalog_name == "file_catalog"
    assert config.execute is False
    assert config.resume is False
    assert config.database_url == "postgresql+psycopg://env/db"
    assert config.stage3_policies == (Stage3Policy("V", 15, 76),)


def test_job_config_public_dict_redacts_database_url():
    config = FieldArchiveJobConfig(
        database_url="postgresql+psycopg://user:secret@db/grandgousier",
        processed_root="processed",
        field_id=33,
        catalog_name="catalog",
        stage3_policies=(Stage3Policy("V", 15, 76),),
    )
    payload = config.public_dict()
    assert payload["database_url"] == "<configured>"
    assert "secret" not in json.dumps(payload)
    assert len(config.fingerprint()) == 64


def test_run_field_archive_job_wraps_existing_m8_and_writes_artifacts(monkeypatch, tmp_path: Path):
    config = FieldArchiveJobConfig(
        database_url="postgresql+psycopg://db/test",
        processed_root=str(tmp_path / "processed"),
        field_id=33,
        catalog_name="shared_catalog",
        stage3_policies=(Stage3Policy("V", 15, 76),),
        out_root=str(tmp_path / "reports"),
        execute=False,
    )
    (tmp_path / "processed").mkdir()

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
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return workflow

    monkeypatch.setattr("grandgousier.services.field_archive_job.run_field_archive_workflow", fake_run)
    result = run_field_archive_job(config, job_id="job-test", acquire_lock=False)

    assert result.status == "PLANNED"
    assert result.workflow_result is workflow
    assert calls and calls[0]["policies"] == config.stage3_policies
    job_dir = Path(result.artifacts["job_dir"])
    assert (job_dir / "job_config.json").is_file()
    assert (job_dir / "job_summary.json").is_file()
    config_text = (job_dir / "job_config.json").read_text(encoding="utf-8")
    assert "postgresql+psycopg://db/test" not in config_text
    events = [json.loads(line) for line in (job_dir / "job_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in events] == [
        "job_started",
        "readiness_started",
        "readiness_completed",
        "workflow_started",
        "workflow_completed",
        "job_completed",
    ]


def test_run_field_archive_job_stops_on_failed_readiness(monkeypatch, tmp_path: Path):
    config = FieldArchiveJobConfig(
        processed_root=str(tmp_path / "processed"),
        field_id=33,
        catalog_name="shared_catalog",
        stage3_policies=(Stage3Policy("R", 20, 77),),
        out_root=str(tmp_path / "reports"),
    )
    failed = RuntimeReadinessResult(
        status="FAILED",
        checks=(ReadinessCheck("schema_migrations", "FAIL", "pending=x"),),
    )
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.check_runtime_readiness",
        lambda **kwargs: failed,
    )
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.run_field_archive_workflow",
        lambda **kwargs: pytest.fail("M8 must not run after failed readiness"),
    )

    result = run_field_archive_job(config, job_id="job-fail", acquire_lock=False)
    assert result.status == "FAILED_PRECONDITION"
    assert result.error_type == "RuntimeReadinessError"
    assert Path(result.artifacts["failure_json"]).is_file()


def test_run_field_archive_job_retains_exception_failure(monkeypatch, tmp_path: Path):
    config = FieldArchiveJobConfig(
        processed_root=str(tmp_path / "processed"),
        field_id=33,
        catalog_name="shared_catalog",
        stage3_policies=(Stage3Policy("V", 15, 76),),
        out_root=str(tmp_path / "reports"),
    )
    monkeypatch.setattr(
        "grandgousier.services.field_archive_job.run_field_archive_workflow",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = run_field_archive_job(config, job_id="job-exception", run_preflight=False, acquire_lock=False)
    assert result.status == "FAILED"
    assert result.error_type == "RuntimeError"
    failure = json.loads(Path(result.artifacts["failure_json"]).read_text(encoding="utf-8"))
    assert failure["error_message"] == "boom"
    assert "traceback" in failure


def test_cli_process_field_archive_forwards_to_service(monkeypatch, tmp_path: Path, capsys):
    pytest.importorskip("astropy")
    from grandgousier import cli

    fake_workflow = SimpleNamespace(
        field_id=33,
        status="PLAN",
        execute=False,
        batches=[],
        stage2_runs=[],
        stage3=[],
        control_objects=[],
        outputs={"final_report_json": "final.json", "report_html": "report.html"},
        warnings=[],
        errors=[],
    )
    fake_job = SimpleNamespace(
        job_id="cli-job",
        status="PLANNED",
        config_fingerprint="a" * 64,
        readiness=None,
        workflow_result=fake_workflow,
        error_message=None,
        error_type=None,
        artifacts={"job_summary_json": "job.json", "job_events_jsonl": "events.jsonl"},
        to_dict=lambda: {"status": "PLANNED"},
    )
    seen = {}

    def fake_service(config, **kwargs):
        seen["config"] = config
        return fake_job

    monkeypatch.setattr("grandgousier.services.field_archive_job.run_field_archive_job", fake_service)
    code = cli.main([
        "process-field-archive",
        "--processed-root", str(tmp_path),
        "--field-id", "33",
        "--catalog-name", "shared_catalog",
        "--stage3-policy", "V:15:76",
    ])
    assert code == 0
    assert seen["config"].field_id == 33
    assert seen["config"].stage3_policies[0].reference_series_id == 76
    assert "Field-archive job" in capsys.readouterr().out
