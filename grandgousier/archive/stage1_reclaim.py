from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.archive.stage1_execution import STAGE1_PROVENANCE_SCHEMA
from grandgousier.db.models import (
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    FrameLocation,
    Measurement,
    PipelineRun,
    PipelineRunStep,
    SciencePhotometry,
)

SCHEMA = "grandgousier.stage1.reclaim.v2"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(part, path)


def _is_reparse_or_symlink(path: Path) -> bool:
    if os.path.islink(path):
        return True
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISLNK(mode):
        return True
    file_attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _unsafe_path_component(path: Path, root: Path) -> str | None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return "OUTSIDE_MANAGED_ROOT"
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() and _is_reparse_or_symlink(current):
            return "PATH_IS_SYMLINK"
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class ReclaimResult:
    series_id: int
    status: str
    reason_codes: tuple[str, ...]
    products: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "series_id": self.series_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "products": list(self.products),
            "summary": self.summary,
            "operation_id": self.operation_id,
        }


def _manifest_path(manifest_root: Path, series_id: int) -> Path:
    return manifest_root / f"series-{series_id}.json"


def _lifecycle_status(path: Path, series_id: int) -> str | None:
    payload = _read_json(path)
    if payload is None or payload.get("series_id") != series_id:
        return None
    status = str(payload.get("status") or "")
    return status if status in {"RECLAIMED", "INTENT", "FAILED", "PLAN"} else None


def _locations(session: Session, series_id: int) -> dict[int, tuple[Frame, FrameLocation]]:
    rows = session.execute(
        select(Frame, FrameLocation)
        .join(FieldSeriesFrame, FieldSeriesFrame.frame_id == Frame.frame_id)
        .join(FrameLocation, FrameLocation.frame_id == Frame.frame_id)
        .where(FieldSeriesFrame.series_id == series_id)
        .order_by(FieldSeriesFrame.order_index, FrameLocation.is_primary.desc())
    ).all()
    result: dict[int, tuple[Frame, FrameLocation]] = {}
    for frame, location in rows:
        if frame.frame_id not in result and (location.is_primary or str(location.locator).endswith("_processed.fits")):
            result[frame.frame_id] = (frame, location)
    return result


def _pipeline_state(session: Session, series_id: int) -> tuple[str, tuple[str, ...], int | None]:
    run = session.scalar(
        select(PipelineRun)
        .where(PipelineRun.series_id == series_id)
        .order_by(PipelineRun.started_at.desc(), PipelineRun.pipeline_run_id.desc())
        .limit(1)
    )
    if run is None:
        return "NOT_DONE", ("PIPELINE_NOT_DONE",), None
    status = str(run.status).upper()
    if status == "RUNNING":
        return "ACTIVE", ("ACTIVE_PIPELINE_RUN",), run.pipeline_run_id
    if status != "DONE":
        return "FAILED", ("PIPELINE_NOT_DONE",), run.pipeline_run_id
    steps = session.scalars(
        select(PipelineRunStep).where(PipelineRunStep.pipeline_run_id == run.pipeline_run_id)
    ).all()
    step_reasons: list[str] = []
    for step in steps:
        step_status = str(step.status).upper()
        if step_status == "RUNNING":
            step_reasons.append("ACTIVE_PIPELINE_STEP")
        elif step_status == "FAILED":
            step_reasons.append("FAILED_PIPELINE_STEP")
    return ("DONE" if not step_reasons else "INVALID", tuple(dict.fromkeys(step_reasons)), run.pipeline_run_id)


def _validate_product(
    frame: Frame,
    location: FrameLocation | None,
    root: Path,
    *,
    lifecycle: str | None = None,
) -> dict[str, Any]:
    path = Path(str(location.locator)) if location is not None else None
    row: dict[str, Any] = {
        "frame_id": frame.frame_id,
        "observation_id": frame.observation_id,
        "source_observation_uid": None,
        "product_locator": str(path) if path is not None else None,
        "product_exists": bool(path and path.exists()),
        "product_bytes": 0,
        "sidecar_locator": str(path.with_name(path.name + ".provenance.json")) if path else None,
        "sidecar_status": "MISSING",
        "fingerprint_status": "MISSING",
        "identity_status": "UNKNOWN",
        "path_safety_status": "UNKNOWN",
        "checksum_status": "UNKNOWN",
        "lifecycle_status": lifecycle or "NONE",
        "classification": "BLOCKED",
        "reason_code": "PRODUCT_LOCATION_INCOMPLETE" if path is None else None,
    }
    if path is None:
        return row
    if _is_reparse_or_symlink(path):
        row.update(path_safety_status="SYMLINK_OR_REPARSE", reason_code="PATH_IS_SYMLINK")
        return row
    unsafe_component = _unsafe_path_component(path, root)
    if unsafe_component == "OUTSIDE_MANAGED_ROOT":
        row.update(path_safety_status="OUTSIDE_ROOT", reason_code=unsafe_component)
        return row
    if unsafe_component:
        row.update(path_safety_status="SYMLINK_OR_REPARSE", reason_code=unsafe_component)
        return row
    if not _inside(path, root):
        row.update(path_safety_status="OUTSIDE_ROOT", reason_code="OUTSIDE_MANAGED_ROOT")
        return row
    row["path_safety_status"] = "SAFE"
    if not path.exists():
        if lifecycle in {"RECLAIMED", "INTENT"}:
            row.update(classification="ALREADY_RECLAIMED", reason_code=None)
        else:
            row["reason_code"] = "UNKNOWN_MISSING_PRODUCT"
        return row
    if not path.is_file():
        row["reason_code"] = "PRODUCT_NOT_REGULAR_FILE"
        return row
    row["product_bytes"] = path.stat().st_size
    row["product_checksum"] = _sha256(path).casefold()
    sidecar_path = path.with_name(path.name + ".provenance.json")
    sidecar = _read_json(sidecar_path)
    if sidecar is None:
        row["reason_code"] = "SIDECAR_PARSE_ERROR" if sidecar_path.exists() else "SIDECAR_MISSING"
        return row
    if sidecar.get("schema") != STAGE1_PROVENANCE_SCHEMA:
        row.update(sidecar_status="INVALID", reason_code="SIDECAR_SCHEMA_INVALID")
        return row
    product = sidecar.get("product")
    observation = sidecar.get("observation")
    if not isinstance(product, Mapping) or not isinstance(observation, Mapping):
        row.update(sidecar_status="INVALID", reason_code="SIDECAR_SCHEMA_INVALID")
        return row
    row["sidecar_status"] = "VALID"
    row["source_observation_uid"] = observation.get("source_observation_uid")
    expected_checksum = str(location.content_sha256 or "").casefold()
    sidecar_checksum = str(product.get("checksum_sha256") or "").casefold()
    actual_checksum = _sha256(path).casefold()
    if not _SHA256.fullmatch(sidecar_checksum) or actual_checksum != sidecar_checksum:
        row.update(checksum_status="INVALID", reason_code="PRODUCT_CHECKSUM_MISMATCH")
        return row
    if expected_checksum and actual_checksum != expected_checksum:
        row.update(checksum_status="DB_MISMATCH", reason_code="PRODUCT_CHECKSUM_MISMATCH")
        return row
    row["checksum_status"] = "VALID"
    row["checksum_sha256"] = actual_checksum
    if product.get("size_bytes") is not None and int(product["size_bytes"]) != path.stat().st_size:
        row["reason_code"] = "PRODUCT_SIZE_MISMATCH"
        return row
    if str(product.get("locator") or "") and Path(str(product["locator"])).name != path.name:
        row.update(identity_status="INVALID", reason_code="IDENTITY_MISMATCH")
        return row
    obs = frame.observation
    if obs is None or observation.get("observation_uid") != obs.observation_uid:
        row.update(identity_status="INVALID", reason_code="IDENTITY_MISMATCH")
        return row
    if observation.get("native_observation_id") != obs.native_identity:
        row.update(identity_status="INVALID", reason_code="IDENTITY_MISMATCH")
        return row
    expected_source_uid = str(
        (frame.header_json or {}).get("source_observation_uid")
        or (frame.quality_flags or {}).get("source_observation_uid")
        or ""
    )
    if expected_source_uid and str(observation.get("source_observation_uid") or "") != expected_source_uid:
        row.update(identity_status="INVALID", reason_code="IDENTITY_MISMATCH")
        return row
    filename_uid = path.name.removesuffix("_processed.fits")
    if filename_uid != str(observation.get("source_observation_uid") or ""):
        row.update(identity_status="INVALID", reason_code="IDENTITY_MISMATCH")
        return row
    row["identity_status"] = "VALID"
    fingerprint = str(sidecar.get("fingerprint") or "")
    if not _SHA256.fullmatch(fingerprint):
        row.update(fingerprint_status="INVALID", reason_code="FINGERPRINT_MISMATCH")
        return row
    expected_fingerprint = str(
        (frame.header_json or {}).get("stage1_fingerprint")
        or (frame.quality_flags or {}).get("stage1_fingerprint")
        or ""
    )
    if not expected_fingerprint:
        row.update(fingerprint_status="UNVERIFIABLE", reason_code="FINGERPRINT_UNVERIFIABLE")
    elif expected_fingerprint != fingerprint:
        row.update(fingerprint_status="INVALID", reason_code="FINGERPRINT_MISMATCH")
    else:
        row["fingerprint_status"] = "VALID"
    return row


def plan_reclaim(
    session: Session,
    *,
    series_id: int,
    processed_root: str | Path,
    manifest_root: str | Path,
) -> ReclaimResult:
    root = Path(processed_root).resolve()
    manifest = _manifest_path(Path(manifest_root), series_id)
    series = session.get(FieldSeries, series_id)
    if series is None:
        return ReclaimResult(series_id, "BLOCKED", ("SERIES_NOT_FOUND",), (), {}, None)
    memberships = session.scalars(
        select(FieldSeriesFrame).where(FieldSeriesFrame.series_id == series_id).order_by(FieldSeriesFrame.order_index)
    ).all()
    frame_ids = [int(item.frame_id) for item in memberships]
    durable_ids = set(session.scalars(
        select(Measurement.frame_id)
        .join(SciencePhotometry, SciencePhotometry.base_measurement_id == Measurement.measurement_id)
        .where(Measurement.frame_id.in_(frame_ids))
        .distinct()
    ).all()) if frame_ids else set()
    pipeline_status, pipeline_reasons, pipeline_run_id = _pipeline_state(session, series_id)
    locations = _locations(session, series_id)
    rows: list[dict[str, Any]] = []
    lifecycle = _lifecycle_status(manifest, series_id) or _lifecycle_status(
        manifest.with_name(manifest.name + ".intent"), series_id
    )
    for frame_id in frame_ids:
        frame = locations.get(frame_id, (session.get(Frame, frame_id), None))[0]
        location = locations.get(frame_id, (None, None))[1]
        row = _validate_product(frame, location, root, lifecycle=lifecycle)
        row["series_id"] = series_id
        row["pipeline_status"] = pipeline_status
        row["pipeline_run_id"] = pipeline_run_id
        row["durable_science"] = frame_id in durable_ids
        if not row["durable_science"] and row["reason_code"] is None:
            row["reason_code"] = "SCIENCE_COVERAGE_INCOMPLETE"
        if pipeline_reasons and row["reason_code"] is None:
            row["reason_code"] = pipeline_reasons[0]
        if row["reason_code"] is None and row["classification"] == "BLOCKED":
            row["classification"] = "RECLAIMABLE"
        rows.append(row)
    reason_codes = list(pipeline_reasons)
    reason_codes.extend(str(row["reason_code"]) for row in rows if row["reason_code"])
    reason_codes = list(dict.fromkeys(reason_codes))
    reclaimable = [row for row in rows if row["classification"] == "RECLAIMABLE"]
    already = [row for row in rows if row["classification"] == "ALREADY_RECLAIMED"]
    summary = {
        "total_series_frames": len(frame_ids),
        "expected_frames": len(frame_ids),
        "durable_frames": len(durable_ids),
        "missing_durable_frames": sorted(set(frame_ids) - durable_ids),
        "reclaimable_frames": len(reclaimable),
        "already_reclaimed_frames": len(already),
        "blocked_frames": len(rows) - len(reclaimable) - len(already),
        "existing_product_bytes": sum(int(row["product_bytes"]) for row in rows),
        "reclaimable_bytes": sum(int(row["product_bytes"]) for row in reclaimable),
        "retained_sidecar_bytes": sum(
            Path(str(row["sidecar_locator"])).stat().st_size
            for row in rows
            if row["sidecar_status"] == "VALID" and Path(str(row["sidecar_locator"])).is_file()
        ),
    }
    status = "RECLAIMABLE" if rows and not summary["blocked_frames"] else "BLOCKED"
    if summary["already_reclaimed_frames"] == len(rows) and rows:
        status = "ALREADY_RECLAIMED"
    return ReclaimResult(series_id, status, tuple(reason_codes), tuple(rows), summary, None)


def reclaim_series(
    session: Session,
    *,
    series_id: int,
    processed_root: str | Path,
    manifest_root: str | Path,
    execute: bool = False,
    report_path: str | Path | None = None,
) -> ReclaimResult:
    result = plan_reclaim(
        session,
        series_id=series_id,
        processed_root=processed_root,
        manifest_root=manifest_root,
    )
    if report_path is not None:
        _atomic_json(Path(report_path), {**result.to_dict(), "phase": "PLAN"})
    if not execute or result.status not in {"RECLAIMABLE"}:
        return result
    operation_id = str(uuid.uuid4())
    manifest = _manifest_path(Path(manifest_root), series_id)
    intent = {
        **result.to_dict(),
        "operation_id": operation_id,
        "phase": "INTENT",
        "status": "INTENT",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(manifest.with_name(manifest.name + ".intent"), intent)
    deleted: list[dict[str, Any]] = []
    try:
        for item in result.products:
            path = Path(str(item["product_locator"]))
            root = Path(processed_root).resolve()
            if _unsafe_path_component(path, root) or not _inside(path, root) or not path.is_file():
                raise OSError(f"pre-delete safety check failed: {path}")
            if _sha256(path).casefold() != str(item.get("checksum_sha256") or "").casefold():
                raise OSError(f"pre-delete checksum check failed: {path}")
            path.unlink()
            deleted.append({**item, "deleted": True})
    except OSError:
        return ReclaimResult(series_id, "FAILED", ("DELETE_FAILED",), tuple(deleted), result.summary, operation_id)
    final = ReclaimResult(series_id, "RECLAIMED", (), tuple(deleted), result.summary, operation_id)
    _atomic_json(
        manifest,
        {**final.to_dict(), "phase": "FINAL", "status": "RECLAIMED", "created_at": datetime.now(timezone.utc).isoformat()},
    )
    manifest.with_name(manifest.name + ".intent").unlink(missing_ok=True)
    return final


__all__ = ["ReclaimResult", "plan_reclaim", "reclaim_series"]
