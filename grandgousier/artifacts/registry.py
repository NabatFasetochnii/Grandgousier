from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.db.models import Artifact, ArtifactPublication
from grandgousier.logging_utils import safe_filename


class ArtifactRegistryError(RuntimeError):
    pass


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def capture_tree_state(root: str | Path | None) -> dict[str, tuple[int, int]]:
    """Capture cheap file signatures before a run starts.

    The snapshot is used only to distinguish files produced/refreshed by this run
    from stale compatibility-publication files left by older runs.
    """

    if root is None:
        return {}
    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return {}
    state: dict[str, tuple[int, int]] = {}
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        state[path.relative_to(root_path).as_posix()] = (int(stat.st_size), int(stat.st_mtime_ns))
    return state


def run_artifact_directory(
    publication_root: str | Path,
    *,
    run_uid: str,
    pipeline_run_id: int | None,
    artifact_root: str | Path | None = None,
) -> Path:
    publication_root = Path(publication_root)
    base = Path(artifact_root) if artifact_root is not None else publication_root.parent / "runs"
    safe_uid = safe_filename(run_uid)
    leaf = f"{int(pipeline_run_id)}_{safe_uid}" if pipeline_run_id is not None else safe_uid
    return base / leaf


def infer_artifact_format(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or None


def classify_export_artifact(relative_path: str | Path) -> tuple[str, str]:
    rel = Path(relative_path).as_posix().lower()
    name = Path(rel).name
    if "target_light_curve" in name:
        return "TARGET_LIGHT_CURVE", "SCIENCE"
    if rel.startswith("candidates/") or "candidate" in name:
        return "CANDIDATE_PRODUCT", "SCIENCE_DIAGNOSTIC"
    if rel.startswith("random_error_model/") or "random_error" in name or "residual_noise_floor" in name:
        return "RANDOM_ERROR_DIAGNOSTIC", "SCIENCE_DIAGNOSTIC"
    if rel.startswith("aperture_correction/") or "aperture_correction" in name:
        return "APERTURE_CORRECTION_DIAGNOSTIC", "CALIBRATION_DIAGNOSTIC"
    if rel.startswith("color_calibration/") or "color_calibration" in name or "color_residual" in name:
        return "COLOR_CALIBRATION_DIAGNOSTIC", "CALIBRATION_DIAGNOSTIC"
    if rel.startswith("reference_photometry/"):
        return "REFERENCE_PHOTOMETRY_CACHE", "INPUT_CACHE"
    if "field_qc" in name:
        return "FIELD_QC", "SCIENCE_DIAGNOSTIC"
    if "light_curve_summary" in name:
        return "LIGHT_CURVE_SUMMARY", "SCIENCE"
    return "PIPELINE_EXPORT", "DIAGNOSTIC"


def _artifact_uid(
    *,
    pipeline_run_id: int | None,
    batch_job_attempt_id: int | None,
    uri: str,
    sha256: str,
) -> str:
    payload = (
        f"{pipeline_run_id if pipeline_run_id is not None else '-'}\0"
        f"{batch_job_attempt_id if batch_job_attempt_id is not None else '-'}\0{uri}\0{sha256}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def register_file_artifact(
    session: Session,
    *,
    path: str | Path,
    pipeline_run_id: int | None,
    artifact_type: str,
    artifact_role: str,
    pipeline_run_step_id: int | None = None,
    batch_run_id: int | None = None,
    batch_job_attempt_id: int | None = None,
    calibration_version_id: int | None = None,
    series_id: int | None = None,
    schema_version: str | None = None,
    publication_uri: str | Path | None = None,
    relative_path: str | Path | None = None,
    lifecycle_status: str = "ACTIVE_IMMUTABLE",
    is_immutable: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> Artifact:
    """Register one existing file and optionally advance a publication alias.

    Artifact content for a given ``(pipeline_run_id, uri)`` is immutable. An
    idempotent second registration with the same checksum is accepted; different
    content at the same run URI is rejected instead of silently rewriting history.
    """

    file_path = Path(path)
    if not file_path.is_file():
        raise ArtifactRegistryError(f"Artifact file does not exist: {file_path}")
    stat = file_path.stat()
    digest = file_sha256(file_path)
    uri = str(file_path)
    publication = str(publication_uri) if publication_uri is not None else None
    rel = Path(relative_path).as_posix() if relative_path is not None else None

    query = select(Artifact).where(Artifact.uri == uri)
    if batch_job_attempt_id is not None:
        query = query.where(Artifact.batch_job_attempt_id == int(batch_job_attempt_id))
    elif pipeline_run_id is None:
        query = query.where(Artifact.pipeline_run_id.is_(None), Artifact.batch_job_attempt_id.is_(None))
    else:
        query = query.where(Artifact.pipeline_run_id == int(pipeline_run_id))
    existing = session.scalar(query)
    if existing is not None:
        if str(existing.sha256 or "") != digest or int(existing.size_bytes or -1) != int(stat.st_size):
            raise ArtifactRegistryError(
                "Refusing to mutate immutable artifact content for "
                f"pipeline_run_id={pipeline_run_id}, uri={uri}"
            )
        artifact = existing
    else:
        artifact = Artifact(
            artifact_uid=_artifact_uid(
                pipeline_run_id=pipeline_run_id,
                batch_job_attempt_id=batch_job_attempt_id,
                uri=uri,
                sha256=digest,
            ),
            pipeline_run_id=int(pipeline_run_id) if pipeline_run_id is not None else None,
            pipeline_run_step_id=(int(pipeline_run_step_id) if pipeline_run_step_id is not None else None),
            batch_run_id=int(batch_run_id) if batch_run_id is not None else None,
            batch_job_attempt_id=(int(batch_job_attempt_id) if batch_job_attempt_id is not None else None),
            calibration_version_id=(int(calibration_version_id) if calibration_version_id is not None else None),
            series_id=int(series_id) if series_id is not None else None,
            artifact_type=str(artifact_type),
            artifact_role=str(artifact_role),
            artifact_format=infer_artifact_format(file_path),
            schema_version=str(schema_version) if schema_version is not None else None,
            storage_kind="FILE",
            uri=uri,
            publication_uri=publication,
            relative_path=rel,
            size_bytes=int(stat.st_size),
            sha256=digest,
            lifecycle_status=str(lifecycle_status),
            is_immutable=bool(is_immutable),
            metadata_json=dict(metadata or {}),
        )
        session.add(artifact)
        session.flush()

    if publication:
        head = session.scalar(
            select(ArtifactPublication).where(ArtifactPublication.publication_uri == publication)
        )
        if head is None:
            head = ArtifactPublication(
                publication_uri=publication,
                artifact_id=int(artifact.artifact_id),
                updated_by_pipeline_run_id=(int(pipeline_run_id) if pipeline_run_id is not None else None),
                metadata_json={"artifact_type": str(artifact_type)},
            )
            session.add(head)
        else:
            head.artifact_id = int(artifact.artifact_id)
            head.updated_by_pipeline_run_id = int(pipeline_run_id) if pipeline_run_id is not None else None
            head.metadata_json = {"artifact_type": str(artifact_type)}
    return artifact


def snapshot_file_artifact(
    session: Session,
    *,
    source_path: str | Path,
    run_dir: str | Path,
    relative_path: str | Path,
    pipeline_run_id: int | None,
    artifact_type: str,
    artifact_role: str,
    pipeline_run_step_id: int | None = None,
    batch_run_id: int | None = None,
    batch_job_attempt_id: int | None = None,
    calibration_version_id: int | None = None,
    series_id: int | None = None,
    schema_version: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Artifact:
    source = Path(source_path)
    rel = Path(relative_path)
    destination = Path(run_dir) / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return register_file_artifact(
        session,
        path=destination,
        pipeline_run_id=pipeline_run_id,
        pipeline_run_step_id=pipeline_run_step_id,
        batch_run_id=batch_run_id,
        batch_job_attempt_id=batch_job_attempt_id,
        calibration_version_id=calibration_version_id,
        series_id=series_id,
        artifact_type=artifact_type,
        artifact_role=artifact_role,
        schema_version=schema_version,
        publication_uri=source,
        relative_path=rel,
        metadata=metadata,
    )


def snapshot_changed_export_tree(
    session: Session,
    *,
    publication_root: str | Path,
    run_dir: str | Path,
    baseline_state: Mapping[str, tuple[int, int]] | None,
    pipeline_run_id: int | None,
    series_id: int | None = None,
    calibration_version_id: int | None = None,
    calibration_version_ids_by_type: Mapping[str, int | None] | None = None,
) -> list[Artifact]:
    root = Path(publication_root)
    if not root.exists() or not root.is_dir():
        return []
    baseline = dict(baseline_state or {})
    artifacts: list[Artifact] = []
    for source in sorted(path for path in root.rglob("*") if path.is_file()):
        rel = source.relative_to(root)
        stat = source.stat()
        signature = (int(stat.st_size), int(stat.st_mtime_ns))
        if baseline.get(rel.as_posix()) == signature:
            continue
        artifact_type, artifact_role = classify_export_artifact(rel)
        type_calibration_version_id = calibration_version_id
        if calibration_version_ids_by_type is not None and artifact_type in calibration_version_ids_by_type:
            type_calibration_version_id = calibration_version_ids_by_type.get(artifact_type)
        artifacts.append(
            snapshot_file_artifact(
                session,
                source_path=source,
                run_dir=run_dir,
                relative_path=rel,
                pipeline_run_id=pipeline_run_id,
                calibration_version_id=type_calibration_version_id,
                series_id=series_id,
                artifact_type=artifact_type,
                artifact_role=artifact_role,
                metadata={"publication_relative_path": rel.as_posix()},
            )
        )
    return artifacts


_LOG_FILE_TYPES = {
    "log_file": ("PIPELINE_TEXT_LOG", "PROVENANCE"),
    "jsonl_log_file": ("PIPELINE_JSONL_LOG", "PROVENANCE"),
    "error_log_file": ("PIPELINE_ERROR_LOG", "PROVENANCE"),
    "command_json_file": ("PIPELINE_COMMAND", "PROVENANCE"),
    "environment_json_file": ("PIPELINE_ENVIRONMENT", "PROVENANCE"),
}


def register_run_log_artifacts(
    session: Session,
    *,
    log_files: Mapping[str, str],
    pipeline_run_id: int | None,
    series_id: int | None = None,
) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for key, (artifact_type, role) in _LOG_FILE_TYPES.items():
        value = log_files.get(key)
        if not value or not Path(value).is_file():
            continue
        artifacts.append(
            register_file_artifact(
                session,
                path=value,
                pipeline_run_id=pipeline_run_id,
                series_id=series_id,
                artifact_type=artifact_type,
                artifact_role=role,
                relative_path=Path(value).name,
                metadata={"log_key": key},
            )
        )
    return artifacts


def verify_registered_artifact(artifact: Artifact) -> tuple[bool, str]:
    path = Path(str(artifact.uri))
    if not path.is_file():
        return False, "MISSING"
    stat = path.stat()
    if artifact.size_bytes is not None and int(stat.st_size) != int(artifact.size_bytes):
        return False, "SIZE_MISMATCH"
    if artifact.sha256 and file_sha256(path) != str(artifact.sha256):
        return False, "CHECKSUM_MISMATCH"
    return True, "OK"
