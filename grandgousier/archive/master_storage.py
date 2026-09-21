from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.archive.master_reduction import (
    MasterReductionTask,
    delete_master_raw_after_valid_output,
    inspect_existing_master_output,
)
from grandgousier.db.models import (
    Frame,
    FrameLocation,
    FrameStatus,
    Measurement,
    SciencePhotometry,
)


PRUNE_REPORT_COLUMNS = [
    "raw_id",
    "scope",
    "path",
    "status",
    "bytes_freed",
    "frame_id",
    "reason",
]


@dataclass(frozen=True)
class MasterPruneResult:
    raw_id: int
    scope: str
    path: Path
    status: str
    bytes_freed: int = 0
    frame_id: int | None = None
    reason: str | None = None

    def as_row(self) -> dict[str, object]:
        return {
            "raw_id": self.raw_id,
            "scope": self.scope,
            "path": str(self.path),
            "status": self.status,
            "bytes_freed": self.bytes_freed,
            "frame_id": self.frame_id,
            "reason": self.reason,
        }


def plan_raw_prune(tasks: Sequence[MasterReductionTask]) -> list[MasterPruneResult]:
    results: list[MasterPruneResult] = []
    for task in tasks:
        path = task.raw_local_path
        if not path.exists():
            results.append(MasterPruneResult(task.raw_id, "raw", path, "ALREADY_MISSING"))
            continue
        try:
            # This validates Stage-1B provenance before any destructive action.
            # The helper itself only deletes after validation, so for plan-only we
            # cannot call it.  Instead mark the candidate; execute repeats validation.
            size = int(path.stat().st_size)
        except OSError as exc:
            results.append(MasterPruneResult(task.raw_id, "raw", path, "FAILED", reason=str(exc)))
            continue
        inspected = inspect_existing_master_output(task)
        if inspected.status == "FAILED":
            results.append(
                MasterPruneResult(
                    task.raw_id,
                    "raw",
                    path,
                    "BLOCKED",
                    reason=inspected.error,
                )
            )
            continue
        results.append(MasterPruneResult(task.raw_id, "raw", path, "ELIGIBLE", bytes_freed=size))
    return results


def execute_raw_prune(tasks: Sequence[MasterReductionTask]) -> list[MasterPruneResult]:
    results: list[MasterPruneResult] = []
    for task in tasks:
        path = task.raw_local_path
        if not path.exists():
            results.append(MasterPruneResult(task.raw_id, "raw", path, "ALREADY_MISSING"))
            continue
        try:
            deleted, size = delete_master_raw_after_valid_output(task)
            results.append(
                MasterPruneResult(
                    task.raw_id,
                    "raw",
                    path,
                    "DELETED" if deleted else "ALREADY_MISSING",
                    bytes_freed=size,
                )
            )
        except Exception as exc:
            results.append(
                MasterPruneResult(
                    task.raw_id,
                    "raw",
                    path,
                    "BLOCKED",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def _norm_local_path(value: str | Path) -> str:
    # DB paths are written by the same host.  Normalize slash/case without
    # resolving, because the file may already have been deleted on a prior run.
    return str(value).replace("/", "\\").rstrip("\\").casefold()


def stage2_ready_frame_map(session: Session, tasks: Sequence[MasterReductionTask]) -> dict[str, tuple[int, bool, str]]:
    """Return processed-path -> (frame_id, has science rows, processing status)."""
    if not tasks:
        return {}
    stmt = (
        select(Frame.frame_id, FrameLocation.locator.label("raw_path"), Frame.processing_status)
        .join(FrameLocation, (FrameLocation.frame_id == Frame.frame_id) & FrameLocation.is_primary)
        .where(Frame.instrument == "MASTER")
    )
    rows = session.execute(stmt).all()
    frame_by_path = {
        _norm_local_path(raw_path): (int(frame_id), str(getattr(status, "value", status)))
        for frame_id, raw_path, status in rows
        if raw_path
    }
    frame_ids = [item[0] for item in frame_by_path.values()]
    science_ids: set[int] = set()
    if frame_ids:
        science_ids = {
            int(value)
            for value in session.execute(
                select(Measurement.frame_id)
                .join(
                    SciencePhotometry,
                    SciencePhotometry.base_measurement_id == Measurement.measurement_id,
                )
                .where(Measurement.frame_id.in_(frame_ids))
                .distinct()
            ).scalars()
        }
    result: dict[str, tuple[int, bool, str]] = {}
    for path, (frame_id, status) in frame_by_path.items():
        result[path] = (frame_id, frame_id in science_ids, status)
    return result


def plan_processed_prune(
    tasks: Sequence[MasterReductionTask],
    *,
    session: Session,
) -> list[MasterPruneResult]:
    readiness = stage2_ready_frame_map(session, tasks)
    results: list[MasterPruneResult] = []
    for task in tasks:
        path = task.output_path
        if not path.exists():
            results.append(MasterPruneResult(task.raw_id, "processed", path, "ALREADY_MISSING"))
            continue
        info = readiness.get(_norm_local_path(path))
        if info is None:
            results.append(
                MasterPruneResult(task.raw_id, "processed", path, "BLOCKED", reason="processed FITS is not indexed in frames")
            )
            continue
        frame_id, has_science, status = info
        if status != FrameStatus.PHOTOMETRY_DONE.value:
            results.append(
                MasterPruneResult(
                    task.raw_id,
                    "processed",
                    path,
                    "BLOCKED",
                    frame_id=frame_id,
                    reason=f"frames.processing_status={status!r}, expected PHOTOMETRY_DONE",
                )
            )
            continue
        if not has_science:
            results.append(
                MasterPruneResult(
                    task.raw_id,
                    "processed",
                    path,
                    "BLOCKED",
                    frame_id=frame_id,
                    reason="no durable science_photometry rows for frame",
                )
            )
            continue
        results.append(
            MasterPruneResult(
                task.raw_id,
                "processed",
                path,
                "ELIGIBLE",
                bytes_freed=int(path.stat().st_size),
                frame_id=frame_id,
            )
        )
    return results


def execute_processed_prune(
    tasks: Sequence[MasterReductionTask],
    *,
    session: Session,
) -> list[MasterPruneResult]:
    planned = plan_processed_prune(tasks, session=session)
    results: list[MasterPruneResult] = []
    for item in planned:
        if item.status != "ELIGIBLE":
            results.append(item)
            continue
        try:
            size = int(item.path.stat().st_size)
            item.path.unlink()
            results.append(
                MasterPruneResult(
                    item.raw_id,
                    item.scope,
                    item.path,
                    "DELETED",
                    bytes_freed=size,
                    frame_id=item.frame_id,
                )
            )
        except OSError as exc:
            results.append(
                MasterPruneResult(
                    item.raw_id,
                    item.scope,
                    item.path,
                    "FAILED",
                    frame_id=item.frame_id,
                    reason=str(exc),
                )
            )
    return results


def write_master_prune_report(results: Iterable[MasterPruneResult], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRUNE_REPORT_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(result.as_row())
    return out
