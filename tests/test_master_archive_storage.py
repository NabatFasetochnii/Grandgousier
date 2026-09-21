from __future__ import annotations

from pathlib import Path

import pytest

import grandgousier.archive.master_storage as storage
from grandgousier.archive.master_reduction import MasterReductionResult, MasterReductionTask


def _task(tmp_path: Path) -> MasterReductionTask:
    return MasterReductionTask(
        processed_frame_id="131841",
        raw_id=375506,
        raw_remote_path="/master/imdata/rawdata/2018/09/07/375506.fit.gz",
        header_remote_path="/master/imdata/procdata/2018/09/07/375506_header.fits",
        dark_remote_path="/master/imdata/rawdata/2018/09/07/375453.fit.gz",
        flat_remote_path="/master/imdata/rawdata/2018/08/31/373624.fit.gz",
        raw_local_path=tmp_path / "raw.fit.gz",
        header_local_path=tmp_path / "header.fits",
        dark_local_path=tmp_path / "dark.fit.gz",
        flat_local_path=tmp_path / "flat.fit.gz",
        output_path=tmp_path / "processed" / "375506_processed.fits",
    )


def test_plan_raw_prune_requires_valid_stage1b_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.raw_local_path.write_bytes(b"raw-data")
    task.output_path.parent.mkdir(parents=True)
    task.output_path.write_bytes(b"processed")

    monkeypatch.setattr(
        storage,
        "inspect_existing_master_output",
        lambda current: MasterReductionResult(task=current, status="SKIPPED_EXISTING"),
    )
    result = storage.plan_raw_prune([task])[0]
    assert result.status == "ELIGIBLE"
    assert result.bytes_freed == len(b"raw-data")

    monkeypatch.setattr(
        storage,
        "inspect_existing_master_output",
        lambda current: MasterReductionResult(task=current, status="FAILED", error="bad provenance"),
    )
    blocked = storage.plan_raw_prune([task])[0]
    assert blocked.status == "BLOCKED"
    assert blocked.reason == "bad provenance"


def test_execute_raw_prune_deletes_only_after_helper_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.raw_local_path.write_bytes(b"raw-data")

    def fake_delete(current):
        size = current.raw_local_path.stat().st_size
        current.raw_local_path.unlink()
        return True, size

    monkeypatch.setattr(storage, "delete_master_raw_after_valid_output", fake_delete)
    result = storage.execute_raw_prune([task])[0]
    assert result.status == "DELETED"
    assert result.bytes_freed == len(b"raw-data")
    assert not task.raw_local_path.exists()


def test_processed_prune_blocks_without_stage2_science_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True)
    task.output_path.write_bytes(b"processed")
    monkeypatch.setattr(
        storage,
        "stage2_ready_frame_map",
        lambda session, tasks: {
            storage._norm_local_path(task.output_path): (74, False, "PHOTOMETRY_DONE")
        },
    )
    result = storage.plan_processed_prune([task], session=object())[0]
    assert result.status == "BLOCKED"
    assert "durable science_photometry" in str(result.reason)


def test_processed_prune_allows_only_photometry_done_with_science_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    task.output_path.parent.mkdir(parents=True)
    task.output_path.write_bytes(b"processed")
    monkeypatch.setattr(
        storage,
        "stage2_ready_frame_map",
        lambda session, tasks: {
            storage._norm_local_path(task.output_path): (74, True, "PHOTOMETRY_DONE")
        },
    )
    result = storage.plan_processed_prune([task], session=object())[0]
    assert result.status == "ELIGIBLE"
    assert result.frame_id == 74
    assert result.bytes_freed == len(b"processed")
