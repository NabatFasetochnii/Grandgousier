from pathlib import Path


def test_fresh_pipeline_run_insert_initializes_required_json_payloads():
    source = Path("grandgousier/pipeline/runner.py").read_text(encoding="utf-8")

    assert "status, summary_json, started_at" in source
    assert "CAST(:summary_json AS JSONB)" in source
    assert '"summary_json": json.dumps({}, ensure_ascii=False)' in source
    assert "status, result_json, started_at" in source
    assert "CAST(:result_json AS JSONB)" in source
    assert '"result_json": json.dumps({}, ensure_ascii=False)' in source


def test_archive_series_failures_are_transaction_isolated():
    source = Path("grandgousier/archive/auto_process.py").read_text(encoding="utf-8")
    marker = "except Exception as exc:  # noqa: BLE001 - batch command reports per-series failures."
    block = source[source.index(marker): source.index(marker) + 500]

    assert "session.rollback()" in block
    assert block.index("session.rollback()") < block.index("failure_status =")
