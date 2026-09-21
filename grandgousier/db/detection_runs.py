from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from grandgousier.db.models import DetectionRun, DetectionRunFrame

SOURCE_DETECTION_RUN = "SOURCE_DETECTION"
CATALOG_MATCH_RUN = "CATALOG_MATCH"

_VERSION_RE = re.compile(r"(?:^|_)v([0-9][A-Za-z0-9_.-]*)$")


def algorithm_version(algorithm: str) -> str | None:
    """Extract the conventional trailing ``_vN`` version without changing the tag."""

    match = _VERSION_RE.search(str(algorithm))
    return match.group(1) if match else None


def detection_run_fingerprint(
    *,
    run_kind: str,
    algorithm: str,
    parameters: dict[str, Any] | None,
    catalog_name: str | None = None,
) -> str:
    """Return a stable configuration fingerprint for a detection subsystem run.

    The fingerprint deliberately excludes frame scope and pipeline-run identity: it
    identifies the algorithm/configuration, not one execution instance.
    """

    payload = {
        "run_kind": str(run_kind),
        "catalog_name": str(catalog_name) if catalog_name is not None else None,
        "algorithm": str(algorithm),
        "algorithm_version": algorithm_version(str(algorithm)),
        "parameters": parameters or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_detection_run(
    session: Session,
    *,
    run_kind: str,
    algorithm: str,
    parameters: dict[str, Any] | None,
    pipeline_run_id: int | None = None,
    catalog_name: str | None = None,
) -> DetectionRun:
    """Create one run-level provenance row inside the caller's transaction."""

    run = DetectionRun(
        pipeline_run_id=(int(pipeline_run_id) if pipeline_run_id is not None else None),
        run_kind=str(run_kind),
        catalog_name=(str(catalog_name) if catalog_name is not None else None),
        algorithm=str(algorithm),
        algorithm_version=algorithm_version(str(algorithm)),
        parameters=dict(parameters or {}),
        fingerprint=detection_run_fingerprint(
            run_kind=run_kind,
            algorithm=algorithm,
            parameters=parameters,
            catalog_name=catalog_name,
        ),
    )
    session.add(run)
    session.flush()
    return run


def create_detection_run_frame(
    session: Session,
    *,
    detection_run_id: int,
    frame_id: int,
    status: str,
    diagnostics: dict[str, Any] | None = None,
) -> DetectionRunFrame:
    """Create one per-frame run record and return it with its generated PK."""

    row = DetectionRunFrame(
        detection_run_id=int(detection_run_id),
        frame_id=int(frame_id),
        status=str(status),
        diagnostics=dict(diagnostics or {}),
    )
    session.add(row)
    session.flush()
    return row
