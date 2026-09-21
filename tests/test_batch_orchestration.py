from __future__ import annotations

from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from grandgousier.batch.state import make_batch_uid
from grandgousier.db.models import Base


def test_batch_uid_is_stable_across_windows_path_case_and_separator() -> None:
    left = make_batch_uid("tess_all_observations", r"C:\Users\Nikita\Desktop\tess")
    right = make_batch_uid("TESS_ALL_OBSERVATIONS", "c:/users/nikita/desktop/tess/")
    assert left == right
    assert left.startswith("batch_")
    assert len(left) == 38


def test_batch_schema_has_append_only_attempt_identity_and_pipeline_link() -> None:
    attempt_table = Base.metadata.tables["batch_job_attempts"]
    unique_names = {
        constraint.name
        for constraint in attempt_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_batch_job_attempts_job_attempt" in unique_names
    targets = {
        element.target_fullname
        for constraint in attempt_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert "batch_jobs.batch_job_id" in targets
    assert "pipeline_runs.pipeline_run_id" in targets


def test_batch_job_current_state_is_scoped_to_stable_batch_run() -> None:
    job_table = Base.metadata.tables["batch_jobs"]
    unique_names = {
        constraint.name
        for constraint in job_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_batch_jobs_run_job" in unique_names
    assert "active_in_plan" in job_table.c
    assert "current_status" in job_table.c
    assert "current_attempt_no" in job_table.c
    assert "latest_pipeline_run_id" in job_table.c


def test_v15_batch_script_uses_database_state_and_v14_is_wrapper() -> None:
    root = Path(__file__).resolve().parents[1]
    v15 = (root / "process_all_tess_observations_v1_5.ps1").read_text(encoding="utf-8-sig")
    v14 = (root / "process_all_tess_observations_v1_4.ps1").read_text(encoding="utf-8-sig")
    assert '"batch-sync-plan"' in v15
    assert '"batch-start-attempt"' in v15
    assert '"batch-finish-attempt"' in v15
    assert '"batch-export-state"' in v15
    assert "Authoritative state: PostgreSQL" in v15
    assert "process_all_tess_observations_v1_5.ps1" in v14
    assert "@PSBoundParameters" in v14
    # Keep the error text out of an expandable string entirely. This avoids
    # PowerShell's `$name:` scoped/drive-variable parser ambiguity.
    assert '$nativeExitCode:' not in v15
    assert '${nativeExitCode}: $text' not in v15
    assert 'exit code {0}: {1}' in v15
    assert '-f $nativeExitCode, $text' in v15
    # Optional manifest fields must not be emitted as empty native-process
    # arguments. Windows PowerShell 5.1 rejects a string[] parameter that
    # contains an empty string before Invoke-GrandgousierJsonCommand runs.
    assert '"--science-status", [string]$manifestSummary.ScienceStatus,' not in v15
    assert '"--catalog-name", [string]$manifestSummary.CatalogName,' not in v15
    assert '        "--message", [string]$manifestSummary.Message\n    )' not in v15
    assert 'IsNullOrWhiteSpace([string]$manifestSummary.ScienceStatus)' in v15
    assert 'IsNullOrWhiteSpace([string]$manifestSummary.CatalogName)' in v15
    assert 'IsNullOrWhiteSpace([string]$manifestSummary.Message)' in v15
    assert 'IsNullOrWhiteSpace([string]$recoveredSummary.ScienceStatus)' in v15
    assert 'IsNullOrWhiteSpace([string]$recoveredSummary.CatalogName)' in v15
    # A new EXECUTION must not read a stale successful manifest if Python exits
    # before writing the new one. Recovery is handled earlier in the script.
    assert 'Remove-Item -LiteralPath $manifestPath -Force' in v15
