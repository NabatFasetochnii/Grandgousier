from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from astropy.io import fits
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grandgousier import __version__
from grandgousier.db.models import (
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    FrameLocation,
    Measurement,
    Object,
    Observation,
    PipelineRun,
    PipelineRunStep,
    SciencePhotometry,
)

SCHEMA = "grandgousier.legacy-stage1-cleanup.v1"
CLASSIFICATIONS = {"LEGACY_RECLAIMABLE", "BLOCKED", "ALREADY_MISSING", "UNLINKED", "UNKNOWN"}
BLOCK_REASONS = {
    "OUTSIDE_LEGACY_ROOT", "PATH_IS_SYMLINK", "PATH_IS_REPARSE_POINT", "PRODUCT_NOT_REGULAR",
    "IDENTITY_MISMATCH", "FITS_HEADER_MISMATCH", "PIPELINE_ACTIVE", "PIPELINE_AMBIGUOUS",
    "SCIENCE_STATE_INCOMPLETE", "PROCESSING_NOT_DONE", "HASH_FAILURE",
    "DUPLICATE_PRODUCT_MAPPING", "UNKNOWN",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(part, path)


def _safe_path(path: Path, root: Path) -> tuple[bool, str | None]:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False, "OUTSIDE_LEGACY_ROOT"
    current = root
    try:
        for part in path.absolute().relative_to(root.absolute()).parts:
            current /= part
            if current.exists():
                mode = current.lstat().st_mode
                attrs = getattr(current.stat(), "st_file_attributes", 0)
                if stat.S_ISLNK(mode):
                    return False, "PATH_IS_SYMLINK"
                if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    return False, "PATH_IS_REPARSE_POINT"
    except OSError:
        return False, "PRODUCT_NOT_REGULAR"
    try:
        if path.resolve().relative_to(root.resolve()) is None:
            return False, "OUTSIDE_LEGACY_ROOT"
    except ValueError:
        return False, "OUTSIDE_LEGACY_ROOT"
    return True, None


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _pipeline_status(session: Session, series_id: int) -> dict[str, Any]:
    runs = session.scalars(
        select(PipelineRun).where(PipelineRun.series_id == series_id).order_by(PipelineRun.started_at, PipelineRun.pipeline_run_id)
    ).all()
    running_runs = [r.pipeline_run_id for r in runs if str(r.status).upper() == "RUNNING"]
    latest = runs[-1] if runs else None
    steps = session.scalars(
        select(PipelineRunStep).where(PipelineRunStep.pipeline_run_id == latest.pipeline_run_id)
    ).all() if latest else []
    running_steps = [s.pipeline_run_step_id for s in steps if str(s.status).upper() == "RUNNING"]
    return {
        "latest_status": str(latest.status).upper() if latest else "MISSING",
        "latest_run_id": latest.pipeline_run_id if latest else None,
        "running_runs": running_runs,
        "running_steps": running_steps,
        "durable": bool(latest and str(latest.status).upper() == "DONE" and not running_runs and not running_steps
                        and all(str(s.status).upper() == "DONE" for s in steps)),
    }


def _series_durability(session: Session, series_id: int, frame_ids: list[int]) -> dict[str, Any]:
    pipeline = _pipeline_status(session, series_id)
    done = session.scalar(select(func.count()).select_from(Frame).where(Frame.frame_id.in_(frame_ids), Frame.processing_status == "PHOTOMETRY_DONE")) or 0
    science = session.scalar(
        select(func.count(func.distinct(Measurement.frame_id)))
        .join(SciencePhotometry, SciencePhotometry.base_measurement_id == Measurement.measurement_id)
        .where(Measurement.frame_id.in_(frame_ids))
    ) or 0
    return {
        "series_frame_count": len(frame_ids),
        "photometry_done_count": int(done),
        "science_coverage_count": int(science),
        "pipeline": pipeline,
        "durable": bool(done == len(frame_ids) and science == len(frame_ids) and pipeline["durable"]),
    }


def _read_header(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        header = fits.getheader(path, 0, memmap=False)
        return {str(k): header[k] for k in header.keys() if k}, None
    except Exception:
        return None, "FITS_HEADER_MISMATCH"


def _base_row(frame: Frame, observation: Observation | None, series: FieldSeries, location: FrameLocation | None, path: Path) -> dict[str, Any]:
    components = (observation.identity_components if observation else {}) or {}
    return {
        "frame_id": frame.frame_id,
        "observation_id": frame.observation_id,
        "physical_identity": observation.observation_uid if observation else None,
        "uniqnum": components.get("UNIQNUM") or (observation.native_identity if observation else None),
        "field_id": series.field_id,
        "series_id": series.series_id,
        "filter": series.filter_name or frame.filter_name,
        "product_path": str(path),
        "size": 0,
        "sha256": None,
        "GGSTAGE": None,
        "GGALG": None,
        "GGVER": None,
        "processing_status": str(frame.processing_status),
        "pipeline_durable_status": None,
        "science_coverage_status": None,
        "classification": "UNKNOWN",
        "block_reason": "UNKNOWN",
        "location_id": location.frame_location_id if location else None,
    }


def build_plan(session: Session, root: str | Path, *, field_id: int | None = None, object_name: str = "GPX_TF17_2") -> dict[str, Any]:
    root = Path(root).absolute()
    rows = session.execute(
        select(Frame, Observation, FieldSeries, FrameLocation)
        .join(FieldSeriesFrame, FieldSeriesFrame.frame_id == Frame.frame_id)
        .join(FieldSeries, FieldSeries.series_id == FieldSeriesFrame.series_id)
        .outerjoin(Observation, Observation.observation_id == Frame.observation_id)
        .outerjoin(FrameLocation, (FrameLocation.frame_id == Frame.frame_id) & FrameLocation.is_primary.is_(True))
        .where(FieldSeries.field_id == field_id if field_id is not None else True)
        .order_by(FieldSeries.series_id, FieldSeriesFrame.order_index, Frame.frame_id)
    ).all()
    by_path: dict[str, list[dict[str, Any]]] = {}
    series_frames: dict[int, list[int]] = {}
    series_meta: dict[int, FieldSeries] = {}
    for frame, observation, series, location in rows:
        path = Path(str(location.locator)) if location else Path("")
        row = _base_row(frame, observation, series, location, path)
        key = str(path.absolute()).casefold()
        by_path.setdefault(key, []).append(row)
        series_frames.setdefault(series.series_id, []).append(frame.frame_id)
        series_meta[series.series_id] = series
    durability = {sid: _series_durability(session, sid, ids) for sid, ids in series_frames.items()}
    candidates: list[dict[str, Any]] = []
    for items in by_path.values():
        duplicate = len(items) != 1
        for row in items:
            row["pipeline_durable_status"] = durability[row["series_id"]]
            path = Path(row["product_path"])
            if duplicate:
                row.update(classification="BLOCKED", block_reason="DUPLICATE_PRODUCT_MAPPING")
            elif not row["product_path"]:
                row.update(classification="BLOCKED", block_reason="OUTSIDE_LEGACY_ROOT")
            else:
                safe, reason = _safe_path(path, root)
                if not safe:
                    row.update(classification="BLOCKED", block_reason=reason)
                elif not path.exists():
                    row.update(classification="ALREADY_MISSING", block_reason=None)
                elif not path.is_file():
                    row.update(classification="BLOCKED", block_reason="PRODUCT_NOT_REGULAR")
                else:
                    row["size"] = path.stat().st_size
                    header, header_error = _read_header(path)
                    if header_error:
                        row.update(classification="BLOCKED", block_reason=header_error)
                    else:
                        row["GGSTAGE"] = str(header.get("GGSTAGE", ""))
                        row["GGALG"] = str(header.get("GGALG", ""))
                        row["GGVER"] = str(header.get("GGVER", ""))
                        expected = str(row["uniqnum"] or "")
                        if header.get("GGSTAGE") != "1B" or header.get("GGALG") != "MASTER_DARK_FLAT_V1":
                            row.update(classification="BLOCKED", block_reason="FITS_HEADER_MISMATCH")
                        elif str(header.get("UNIQNUM", "")) != expected:
                            row.update(classification="BLOCKED", block_reason="IDENTITY_MISMATCH")
                        elif object_name and (str(frame.object_name or "") != object_name):
                            row.update(classification="BLOCKED", block_reason="IDENTITY_MISMATCH")
                        elif str(row["processing_status"]).split(".")[-1] != "PHOTOMETRY_DONE":
                            row.update(classification="BLOCKED", block_reason="PROCESSING_NOT_DONE")
                        elif not row["pipeline_durable_status"]["durable"]:
                            row.update(classification="BLOCKED", block_reason="SCIENCE_STATE_INCOMPLETE")
                        else:
                            try:
                                row["sha256"] = _sha256(path)
                                row.update(classification="LEGACY_RECLAIMABLE", block_reason=None)
                            except OSError:
                                row.update(classification="BLOCKED", block_reason="HASH_FAILURE")
    db_paths = {str(Path(p).absolute()).casefold() for p in by_path}
    for path in sorted(root.glob("**/*_processed.fits")):
        if str(path.absolute()).casefold() not in db_paths:
            candidates.append({"product_path": str(path), "size": path.stat().st_size, "sha256": _sha256(path), "classification": "UNLINKED", "block_reason": None})
    for items in by_path.values():
        candidates.extend(items)
    candidates.sort(key=lambda row: str(row["product_path"]).casefold())
    for ordinal, row in enumerate(candidates, 1):
        row["ordinal"] = ordinal
    summary = {
        "db_linked_products": len(rows),
        "existing_products": sum(1 for r in candidates if r["classification"] not in {"ALREADY_MISSING", "UNLINKED"} and r.get("sha256")),
        "legacy_reclaimable": sum(1 for r in candidates if r["classification"] == "LEGACY_RECLAIMABLE"),
        "blocked": sum(1 for r in candidates if r["classification"] == "BLOCKED"),
        "already_missing": sum(1 for r in candidates if r["classification"] == "ALREADY_MISSING"),
        "unlinked": sum(1 for r in candidates if r["classification"] == "UNLINKED"),
        "reclaimable_bytes": sum(int(r.get("size") or 0) for r in candidates if r["classification"] == "LEGACY_RECLAIMABLE"),
        "blocked_bytes": sum(int(r.get("size") or 0) for r in candidates if r["classification"] == "BLOCKED"),
        "series_count": len(series_frames),
        "field_count": len({s.field_id for s in series_meta.values()}),
        "filters": sorted({str(s.filter_name) for s in series_meta.values()}),
    }
    return {"schema": SCHEMA, "generated_at_utc": _now(), "legacy_root": str(root), "field_id": field_id, "object_name": object_name, "summary": summary, "series_durability": durability, "pre_delete_db_snapshot": _snapshot(session), "pre_delete_filesystem_snapshot": _filesystem_snapshot(root), "pre_delete_downstream_smoke": _downstream_smoke(session), "candidates": candidates}


def _snapshot(session: Session) -> dict[str, int]:
    tables = {"frames": Frame, "observations": Observation, "measurements": Measurement, "science_photometry": SciencePhotometry, "calibration_state": PipelineRun}
    return {name: int(session.scalar(select(func.count()).select_from(model)) or 0) for name, model in tables.items()}


def _filesystem_snapshot(root: Path) -> dict[str, int]:
    files = [path for path in root.glob("**/*_processed.fits") if path.is_file()]
    return {"free_bytes": shutil.disk_usage(root).free, "fits_count": len(files), "fits_bytes": sum(path.stat().st_size for path in files)}


def _downstream_smoke(session: Session) -> dict[str, Any]:
    science = int(session.scalar(select(func.count()).select_from(SciencePhotometry)) or 0)
    objects = int(session.scalar(select(func.count()).select_from(Object)) or 0)
    sample = session.scalar(select(SciencePhotometry.base_measurement_id).limit(1))
    return {"science_photometry_rows": science, "object_rows": objects, "science_sample_read": sample is not None}


def write_plan(root: Path, plan: dict[str, Any], *, database_identity: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    contract = {"schema": SCHEMA, "legacy_root": plan["legacy_root"], "database": database_identity, "generated_at_utc": _now(), "git_head": _git_head(), "version": __version__, "allowlist": "DB-linked LEGACY_RECLAIMABLE only", "never_delete": ["raw", "dark", "flat", "processing headers", "sidecars", "current-format products", "unlinked files"]}
    plan["contract"] = contract
    _atomic_json(root / "cleanup_plan.json", plan)
    _atomic_json(root / "cleanup_contract.json", contract)
    fields = sorted({k for row in plan["candidates"] for k in row})
    with (root / "cleanup_plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(plan["candidates"])
    with (root / "checksums.txt").open("w", encoding="utf-8") as handle:
        for name in ("cleanup_plan.json", "cleanup_contract.json"):
            handle.write(f"{_file_sha256(root / name)}  {name}\n")
    (root / "README.txt").write_text("Frozen legacy Stage-1 cleanup evidence. Only DB-linked LEGACY_RECLAIMABLE rows may be deleted.\n", encoding="utf-8")


def execute_plan(session: Session, manifest_root: Path, *, database_identity: str) -> dict[str, Any]:
    plan_path = manifest_root / "cleanup_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_hash = _file_sha256(plan_path)
    contract_hash = _file_sha256(manifest_root / "cleanup_contract.json")
    rows = [r for r in plan["candidates"] if r["classification"] == "LEGACY_RECLAIMABLE"]
    if plan["summary"]["blocked"] != 0:
        raise RuntimeError("unexpected BLOCKED rows prevent first legacy cleanup execution")
    root = Path(plan["legacy_root"])
    db_before = _snapshot(session)
    if db_before != plan.get("pre_delete_db_snapshot"):
        raise RuntimeError("database snapshot changed after plan construction")
    intent = {"schema": SCHEMA, "plan_sha256": plan_hash, "contract_sha256": contract_hash, "git_head": _git_head(), "version": __version__, "started_at_utc": _now(), "database": database_identity, "intended_deletions": len(rows), "intended_bytes": sum(int(r["size"]) for r in rows), "db_snapshot_before": db_before, "filesystem_snapshot_before": _filesystem_snapshot(root), "downstream_smoke_before": _downstream_smoke(session)}
    _atomic_json(manifest_root / "cleanup_intent.json", intent)
    progress_path = manifest_root / "cleanup_progress.jsonl"
    deleted = 0
    deleted_bytes = 0
    with progress_path.open("a", encoding="utf-8") as log:
        for row in rows:
            path = Path(row["product_path"])
            safe, reason = _safe_path(path, root)
            if not safe or not path.is_file() or path.is_symlink() or path.stat().st_size != int(row["size"]) or _sha256(path).casefold() != str(row["sha256"]).casefold():
                raise RuntimeError(f"pre-delete gate failed ordinal={row['ordinal']} path={path} reason={reason or 'CHANGED'}")
            header, error = _read_header(path)
            if error or header.get("GGSTAGE") != "1B" or header.get("GGALG") != "MASTER_DARK_FLAT_V1" or str(header.get("UNIQNUM", "")) != str(row["uniqnum"]):
                raise RuntimeError(f"pre-delete FITS identity gate failed ordinal={row['ordinal']} path={path}")
            durability = _series_durability(session, int(row["series_id"]), [int(row["frame_id"])])
            if not durability["pipeline"]["durable"]:
                raise RuntimeError(f"pre-delete pipeline gate failed ordinal={row['ordinal']} series={row['series_id']}")
            path.unlink()
            if path.exists():
                raise RuntimeError(f"unlink verification failed ordinal={row['ordinal']} path={path}")
            deleted += 1
            deleted_bytes += int(row["size"])
            log.write(json.dumps({"ordinal": row["ordinal"], "product_path": str(path), "sha256": row["sha256"], "bytes": row["size"], "status": "DELETED", "timestamp": _now()}) + "\n")
            log.flush()
            os.fsync(log.fileno())
    final = {"schema": SCHEMA, "plan_sha256": plan_hash, "started_at_utc": intent["started_at_utc"], "ended_at_utc": _now(), "planned_files": len(rows), "deleted_files": deleted, "failed_files": [], "planned_bytes": intent["intended_bytes"], "deleted_bytes": deleted_bytes, "database": database_identity, "source_commit": _git_head(), "db_snapshot_before": db_before, "db_snapshot_after": _snapshot(session), "filesystem_snapshot_before": intent["filesystem_snapshot_before"], "filesystem_snapshot_after": _filesystem_snapshot(root), "downstream_smoke_after": _downstream_smoke(session)}
    _atomic_json(manifest_root / "cleanup_final.json", final)
    return final
