from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grandgousier.calibration.naming import compact_generated_calibration_name
from grandgousier.db.models import CalibrationProductAlias, CalibrationVersion


def get_source_calibration_version_id(cv: CalibrationVersion | None) -> int | None:
    """Return the explicit source calibration id, falling back to legacy parameters."""

    if cv is None:
        return None
    direct = getattr(cv, "source_calibration_version_id", None)
    if direct is not None:
        try:
            return int(direct)
        except Exception:
            return None
    params = cv.parameters or {}
    if isinstance(params, dict):
        value = params.get("source_calibration_version_id")
        if value is not None:
            try:
                return int(value)
            except Exception:
                return None
    return None


def get_source_calibration_name(cv: CalibrationVersion | None) -> str | None:
    """Return the source calibration name from relationship or legacy parameters."""

    if cv is None:
        return None
    source = getattr(cv, "source_calibration", None)
    name = getattr(source, "name", None)
    if name:
        return str(name)
    params = cv.parameters or {}
    if isinstance(params, dict):
        name = params.get("source_calibration_name")
        if name:
            return str(name)
    return None


def set_source_calibration(cv: CalibrationVersion, source_cv: CalibrationVersion | None) -> None:
    """Persist source-calibration provenance in both the FK column and JSON parameters.

    The column is the authoritative relation for new databases. The JSON keys are
    kept for compatibility with older patch code and exported diagnostics.
    """

    if source_cv is None:
        cv.source_calibration_version_id = None
        return
    cv.source_calibration_version_id = int(source_cv.calibration_version_id)
    params: dict[str, Any] = dict(cv.parameters or {}) if isinstance(cv.parameters, dict) else {}
    params["source_calibration_version_id"] = int(source_cv.calibration_version_id)
    params["source_calibration_name"] = source_cv.name
    cv.parameters = params


def resolve_source_calibration(session: Session, cv: CalibrationVersion | None) -> CalibrationVersion | None:
    """Resolve the source CalibrationVersion for a derived calibration.

    New rows use calibration_versions.source_calibration_version_id. Legacy rows
    produced before MVP5.30.5 may only have parameters.source_calibration_name.
    """

    if cv is None:
        return None
    source_id = get_source_calibration_version_id(cv)
    if source_id is not None:
        source = session.get(CalibrationVersion, source_id)
        if source is not None:
            return source
    source_name = get_source_calibration_name(cv)
    if source_name and source_name != cv.name:
        return session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == source_name))
    return None


# ---------------------------------------------------------------------------
# MVP5.39.5 immutable calibration-generation compatibility layer.
#
# ``CalibrationVersion.name`` remains the current publication/head name for
# backward compatibility with the existing query surface. A superseded immutable
# generation is renamed once to a deterministic archival physical name; its
# primary key, measurements, model parameters and scientific fingerprints are
# never rewritten after that point. ``calibration_product_aliases`` records the
# same head explicitly so a later release can retire name-as-head semantics.


def calibration_generation_archive_name(logical_name: str, generation_no: int) -> str:
    suffix = f"_g{int(generation_no):06d}"
    return compact_generated_calibration_name(
        f"{logical_name}__generation{suffix}",
        semantic_suffix=suffix,
    )


def calibration_generation_pending_name(logical_name: str, generation_no: int) -> str:
    suffix = f"_pending_g{int(generation_no):06d}"
    return compact_generated_calibration_name(
        f"{logical_name}__pending_generation_g{int(generation_no):06d}",
        semantic_suffix=suffix,
    )


def _next_generation_no(session: Session, logical_name: str) -> int:
    maximum = session.scalar(
        select(func.max(CalibrationVersion.generation_no)).where(
            CalibrationVersion.logical_name == str(logical_name)
        )
    )
    return max(1, int(maximum or 0) + 1)


def prepare_calibration_version_for_write(
    session: Session,
    *,
    logical_name: str,
    model_name: str,
    instrument_unit_id: int | None,
    used_catalogs: dict[str, Any] | None,
    parameters: dict[str, Any] | None,
    notes: str | None,
    source_cv: CalibrationVersion | None = None,
    instrument_profile: dict[str, Any] | None = None,
    immutable_generation: bool = False,
    reuse_completed_immutable: bool = False,
) -> CalibrationVersion:
    """Return a safe target row for one calibration materialization.

    Legacy/library callers retain the old mutable-name behavior until the main
    pipeline opts into ``immutable_generation``. Once a head has been finalized,
    a write-capable call never returns that immutable row: it allocates a pending
    generation instead. A caller may explicitly reuse a completed immutable head
    read-only (used by forced-photometry idempotent reuse).
    """

    logical_name = str(logical_name)
    current_head = session.scalar(
        select(CalibrationVersion).where(CalibrationVersion.name == logical_name)
    )
    current_status = str(getattr(current_head, "lifecycle_status", "") or "") if current_head else ""

    if current_head is not None and current_status in IMMUTABLE_CALIBRATION_STATUSES:
        if reuse_completed_immutable:
            return current_head
        if not immutable_generation:
            raise ValueError(
                f"Refusing to rewrite immutable calibration product {logical_name!r}; "
                "run through immutable-generation mode or choose a new calibration name"
            )
        generation_no = _next_generation_no(session, logical_name)
        cv = CalibrationVersion(
            name=calibration_generation_pending_name(logical_name, generation_no),
            model_name=str(model_name),
            instrument_unit_id=instrument_unit_id,
            instrument_profile=dict(instrument_profile or {}),
            used_catalogs=dict(used_catalogs or {}),
            parameters=dict(parameters or {}),
            notes=notes,
            logical_name=logical_name,
            generation_no=generation_no,
            supersedes_calibration_version_id=int(current_head.calibration_version_id),
            lifecycle_status="MATERIALIZING",
        )
        if source_cv is not None:
            set_source_calibration(cv, source_cv)
        session.add(cv)
        session.flush()
        return cv

    if current_head is None:
        generation_no = _next_generation_no(session, logical_name) if immutable_generation else None
        # If archived/pending generations already exist but the publication name
        # is absent, do not collide with them: create a pending generation and let
        # finalization restore the stable logical head atomically.
        storage_name = (
            calibration_generation_pending_name(logical_name, generation_no or 1)
            if immutable_generation and int(generation_no or 1) > 1
            else logical_name
        )
        current_head = CalibrationVersion(
            name=storage_name,
            model_name=str(model_name),
            instrument_unit_id=instrument_unit_id,
            instrument_profile=dict(instrument_profile or {}),
            used_catalogs=dict(used_catalogs or {}),
            parameters=dict(parameters or {}),
            notes=notes,
        )
        session.add(current_head)

    # Existing non-immutable compatibility rows may be rewritten once and then
    # sealed by the main pipeline. This deliberately preserves the migration path
    # from LEGACY_MUTABLE / COMPATIBILITY_MUTABLE rows without duplicating their
    # entire measurement layer merely to establish generation 1.
    current_head.model_name = str(model_name)
    current_head.instrument_unit_id = instrument_unit_id
    current_head.instrument_profile = dict(instrument_profile or current_head.instrument_profile or {})
    current_head.used_catalogs = dict(used_catalogs or {})
    current_head.parameters = dict(parameters or {})
    current_head.notes = notes
    if source_cv is not None:
        set_source_calibration(current_head, source_cv)
    if immutable_generation:
        current_head.logical_name = logical_name
        if getattr(current_head, "generation_no", None) is None:
            current_head.generation_no = 1
        current_head.lifecycle_status = "MATERIALIZING"
    session.flush()
    return current_head


def _upsert_calibration_alias(
    session: Session,
    *,
    logical_name: str,
    head: CalibrationVersion,
    pipeline_run_id: int | None,
) -> CalibrationProductAlias:
    alias = session.scalar(
        select(CalibrationProductAlias).where(CalibrationProductAlias.logical_name == str(logical_name))
    )
    if alias is None:
        alias = CalibrationProductAlias(logical_name=str(logical_name))
        session.add(alias)
    alias.head_calibration_version_id = int(head.calibration_version_id)
    alias.head_generation_no = int(getattr(head, "generation_no", None) or 1)
    alias.updated_by_pipeline_run_id = int(pipeline_run_id) if pipeline_run_id is not None else None
    return alias


def finalize_calibration_generation(
    session: Session,
    *,
    cv: CalibrationVersion,
    logical_name: str,
    pipeline_run_id: int | None,
) -> CalibrationVersion:
    """Atomically publish a materialized generation as the logical head."""

    logical_name = str(logical_name)
    current_head = session.scalar(
        select(CalibrationVersion).where(CalibrationVersion.name == logical_name)
    )
    if current_head is not None and int(current_head.calibration_version_id) != int(cv.calibration_version_id):
        current_generation = int(getattr(current_head, "generation_no", None) or 1)
        archive_name = calibration_generation_archive_name(logical_name, current_generation)
        conflicting = session.scalar(
            select(CalibrationVersion.calibration_version_id).where(
                CalibrationVersion.name == archive_name,
                CalibrationVersion.calibration_version_id != int(current_head.calibration_version_id),
            )
        )
        if conflicting is not None:
            # This should only happen after manual DB edits. Preserve uniqueness
            # deterministically instead of mutating another immutable generation.
            archive_name = compact_generated_calibration_name(
                f"{archive_name}_id{int(current_head.calibration_version_id)}",
                semantic_suffix=f"_id{int(current_head.calibration_version_id)}",
            )
        current_head.name = archive_name
        if str(getattr(current_head, "lifecycle_status", "") or "") == "COMPLETE_IMMUTABLE":
            current_head.lifecycle_status = "SUPERSEDED_IMMUTABLE"
        session.flush()  # release the unique publication name before promoting cv
        cv.name = logical_name
        cv.supersedes_calibration_version_id = int(current_head.calibration_version_id)
    elif current_head is None and cv.name != logical_name:
        cv.name = logical_name

    cv.logical_name = logical_name
    if getattr(cv, "generation_no", None) is None:
        cv.generation_no = 1
    cv.lifecycle_status = "COMPLETE_IMMUTABLE"
    cv.finalized_at = datetime.now(timezone.utc)
    _upsert_calibration_alias(
        session,
        logical_name=logical_name,
        head=cv,
        pipeline_run_id=pipeline_run_id,
    )
    session.flush()
    return cv


def resolve_calibration_head(session: Session, logical_name: str) -> CalibrationVersion | None:
    """Resolve the explicit alias head, falling back to the compatibility name."""

    alias = session.scalar(
        select(CalibrationProductAlias).where(CalibrationProductAlias.logical_name == str(logical_name))
    )
    if alias is not None and alias.head_calibration_version_id is not None:
        cv = session.get(CalibrationVersion, int(alias.head_calibration_version_id))
        if cv is not None:
            return cv
    return session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == str(logical_name)))


# ---------------------------------------------------------------------------
# MVP5.39.2 calibration-product provenance foundation.
#
# The current measurement layers are still compatibility-mutable: existing
# low-level calibration functions may reuse a CalibrationVersion name and
# replace its Measurement rows.  These helpers therefore do not claim byte- or
# row-level content immutability.  They provide typed scope/provenance fields and
# deterministic logical fingerprints so the later immutable base+final cutover
# has a stable data contract to target.

IMMUTABLE_CALIBRATION_STATUSES = frozenset({"COMPLETE_IMMUTABLE", "SUPERSEDED_IMMUTABLE"})


def _fingerprintable(value: Any) -> Any:
    """Convert common pipeline values into a canonical JSON-compatible shape."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value == float("inf"):
            return "+Infinity"
        if value == float("-inf"):
            return "-Infinity"
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {
            str(key): _fingerprintable(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_fingerprintable(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True),
        )
    if isinstance(value, (list, tuple)):
        return [_fingerprintable(item) for item in value]
    return str(value)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _fingerprintable(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def calibration_product_fingerprints(
    cv: CalibrationVersion,
    *,
    frame_ids: Iterable[int] | None = None,
    source_provenance_fingerprint: str | None = None,
    pipeline_run_id: int | None = None,
) -> dict[str, str]:
    """Return deterministic logical fingerprints for a calibration product.

    ``definition`` identifies the calibration model, parameters and explicit
    spatial/series scope. ``input`` identifies the parent calibration plus the
    selected frame set. ``provenance`` additionally binds the definition/input
    pair to the producing pipeline run.  These are logical/provenance hashes,
    not hashes of all materialized measurement bytes.
    """

    normalized_frame_ids = None
    if frame_ids is not None:
        normalized_frame_ids = sorted({int(frame_id) for frame_id in frame_ids})

    definition_payload = {
        "model_name": cv.model_name,
        "instrument_unit_id": cv.instrument_unit_id,
        "field_id": getattr(cv, "field_id", None),
        "series_id": getattr(cv, "series_id", None),
        "product_kind": getattr(cv, "product_kind", None),
        # product_role is publication metadata (INTERMEDIATE/SCIENCE) and may
        # change without changing the materialized scientific content.
        "materialization_kind": getattr(cv, "materialization_kind", None),
        "used_catalogs": cv.used_catalogs or {},
        "parameters": cv.parameters or {},
    }
    definition_fingerprint = _sha256_json(definition_payload)

    input_payload = {
        "source_calibration_version_id": get_source_calibration_version_id(cv),
        "source_provenance_fingerprint_sha256": source_provenance_fingerprint,
        "frame_ids": normalized_frame_ids,
    }
    input_fingerprint = _sha256_json(input_payload)

    provenance_payload = {
        "definition_fingerprint_sha256": definition_fingerprint,
        "input_fingerprint_sha256": input_fingerprint,
        "pipeline_run_id": pipeline_run_id,
    }
    return {
        "definition_fingerprint_sha256": definition_fingerprint,
        "input_fingerprint_sha256": input_fingerprint,
        "provenance_fingerprint_sha256": _sha256_json(provenance_payload),
    }


def stamp_calibration_product(
    session: Session,
    *,
    calibration_name: str,
    pipeline_run_id: int | None,
    field_id: int | None,
    series_id: int | None,
    frame_ids: Iterable[int] | None,
    product_kind: str | None = None,
    product_role: str | None = None,
    materialization_kind: str | None = None,
    lifecycle_status: str = "COMPATIBILITY_MUTABLE",
    calibration_version_id: int | None = None,
    finalize_immutable_generation: bool = False,
) -> CalibrationVersion | None:
    """Attach typed scope and logical provenance to one materialized calibration.

    The function deliberately refuses to rewrite a future immutable product.
    During the compatibility phase, re-running the legacy materializer may
    refresh the same CalibrationVersion row; this is represented explicitly by
    ``COMPATIBILITY_MUTABLE`` rather than falsely labelling it immutable.
    """

    cv = (
        session.get(CalibrationVersion, int(calibration_version_id))
        if calibration_version_id is not None
        else session.scalar(select(CalibrationVersion).where(CalibrationVersion.name == calibration_name))
    )
    if cv is None:
        return None

    current_status = str(getattr(cv, "lifecycle_status", "") or "")
    if current_status in IMMUTABLE_CALIBRATION_STATUSES:
        # Publication role is intentionally mutable: an ensemble generation may
        # become the SCIENCE head only after later candidate/color decisions.
        # All content-defining fields must remain identical.
        incompatible = []
        for label, requested, actual in (
            # pipeline_run_id identifies the producing run and intentionally
            # remains unchanged when a later run reuses this immutable product.
            ("field_id", field_id, getattr(cv, "field_id", None)),
            ("series_id", series_id, getattr(cv, "series_id", None)),
            ("product_kind", product_kind, getattr(cv, "product_kind", None)),
            ("materialization_kind", materialization_kind, getattr(cv, "materialization_kind", None)),
        ):
            if requested is not None and actual is not None and str(requested) != str(actual):
                incompatible.append(label)
        if incompatible:
            raise ValueError(
                f"Refusing to mutate immutable calibration product {calibration_name!r}; "
                f"content/provenance fields differ: {', '.join(incompatible)}"
            )
        if product_role is not None:
            cv.product_role = str(product_role)
        session.flush()
        return cv

    cv.pipeline_run_id = int(pipeline_run_id) if pipeline_run_id is not None else None
    cv.field_id = int(field_id) if field_id is not None else None
    cv.series_id = int(series_id) if series_id is not None else None
    if product_kind is not None:
        cv.product_kind = str(product_kind)
    if product_role is not None:
        cv.product_role = str(product_role)
    if materialization_kind is not None:
        cv.materialization_kind = str(materialization_kind)
    cv.lifecycle_status = str(lifecycle_status)
    cv.materialized_at = datetime.now(timezone.utc)

    source_fp = None
    source_id = get_source_calibration_version_id(cv)
    if source_id is not None:
        source_cv = session.get(CalibrationVersion, int(source_id))
        if source_cv is not None:
            source_fp = getattr(source_cv, "provenance_fingerprint_sha256", None)

    fingerprints = calibration_product_fingerprints(
        cv,
        frame_ids=frame_ids,
        source_provenance_fingerprint=source_fp,
        pipeline_run_id=pipeline_run_id,
    )
    cv.definition_fingerprint_sha256 = fingerprints["definition_fingerprint_sha256"]
    cv.input_fingerprint_sha256 = fingerprints["input_fingerprint_sha256"]
    cv.provenance_fingerprint_sha256 = fingerprints["provenance_fingerprint_sha256"]

    # Compact calibration terms are written by low-level materializers before
    # the runner stamps field/run scope. Keep their scope synchronized without
    # making the normalized writer depend on pipeline internals.
    from grandgousier.calibration.normalized_terms import synchronize_calibration_iteration_scope

    synchronize_calibration_iteration_scope(
        session,
        calibration_version_id=int(cv.calibration_version_id),
        pipeline_run_id=pipeline_run_id,
        field_id=field_id,
        series_id=series_id,
    )
    if finalize_immutable_generation:
        finalize_calibration_generation(
            session,
            cv=cv,
            logical_name=calibration_name,
            pipeline_run_id=pipeline_run_id,
        )
    session.flush()
    return cv
