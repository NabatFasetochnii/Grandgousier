from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from grandgousier.archive.observation_selection import (
    ObservationScienceAction,
    ScienceFileIdentityCandidate,
    plan_file_science_selection,
)
from grandgousier.archive.stage1_execution import (
    STAGE1_EXECUTION_SCHEMA,
    STAGE1_PROVENANCE_SCHEMA,
)
from grandgousier.io.frame_locations import add_or_touch_frame_location, find_frame_by_locator
from grandgousier.db.models import FieldSeriesFrame, Frame, FrameLocation, make_frame_location_uid

STAGE1_HANDOFF_SCHEMA = "grandgousier.stage1.handoff.v1"
_SUCCESS_EXECUTION_STATUSES = {
    "REDUCED",
    "IMPORTED",
    "SKIPPED_EXISTING",
    "ADOPTED_EXISTING",
}
_HARD_BLOCK_ACTIONS = {
    ObservationScienceAction.AMBIGUOUS_MULTIPLE_PRODUCTS.value,
    ObservationScienceAction.MISSING_OBSERVATION_ID.value,
    ObservationScienceAction.IDENTITY_CONFLICT.value,
}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _read_mapping(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON root must be a mapping: {path}")
    return payload


def _common_parent(paths: Sequence[Path]) -> Path:
    if not paths:
        return Path(".")
    if len(paths) == 1:
        return paths[0].parent
    try:
        return Path(os.path.commonpath([str(path.parent) for path in paths]))
    except ValueError:
        return paths[0].parent


@dataclass(frozen=True, slots=True)
class ValidatedStage1Product:
    native_observation_id: str | None
    source_observation_uid: str | None
    observation_uid: str | None
    execution_status: str
    product_path: Path
    product_checksum_sha256: str
    provenance_path: Path
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "native_observation_id": self.native_observation_id,
            "source_observation_uid": self.source_observation_uid,
            "observation_uid": self.observation_uid,
            "execution_status": self.execution_status,
            "product_path": str(self.product_path),
            "product_checksum_sha256": self.product_checksum_sha256,
            "provenance_path": str(self.provenance_path),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class Stage1HandoffItem:
    product: ValidatedStage1Product
    p4_action: str
    status: str
    selected_frame_id: int | None
    existing_series_ids: tuple[int, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.product.to_dict(),
            "p4_action": self.p4_action,
            "status": self.status,
            "selected_frame_id": self.selected_frame_id,
            "existing_series_ids": list(self.existing_series_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Stage1HandoffReport:
    execution_report_path: str
    generated_at_utc: str
    execute: bool
    items: tuple[Stage1HandoffItem, ...]
    stage2_result: Mapping[str, Any] | None = None
    errors: tuple[str, ...] = ()
    schema: str = STAGE1_HANDOFF_SCHEMA

    @property
    def blocked_items(self) -> int:
        return sum(item.status == "BLOCKED" for item in self.items)

    @property
    def selected_items(self) -> int:
        return sum(item.status == "SELECTED_FOR_STAGE2" for item in self.items)

    @property
    def retained_items(self) -> int:
        return sum(item.status == "ALREADY_REPRESENTED" for item in self.items)

    @property
    def status(self) -> str:
        if self.errors:
            return "FAILED"
        if self.blocked_items:
            return "BLOCKED"
        if self.stage2_result is not None and str(self.stage2_result.get("status") or "").startswith("FAILED"):
            return "FAILED"
        return "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at_utc": self.generated_at_utc,
            "status": self.status,
            "execute": self.execute,
            "execution_report_path": self.execution_report_path,
            "summary": {
                "items": len(self.items),
                "selected_for_stage2": self.selected_items,
                "already_represented": self.retained_items,
                "blocked": self.blocked_items,
                "stage2_invoked": self.stage2_result is not None,
            },
            "items": [item.to_dict() for item in self.items],
            "stage2_result": dict(self.stage2_result) if self.stage2_result is not None else None,
            "errors": list(self.errors),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def load_validated_stage1_products(path: str | Path) -> tuple[ValidatedStage1Product, ...]:
    """Load and checksum-validate only products named by one Stage-1 execution report."""

    report_path = Path(path)
    payload = _read_mapping(report_path)
    if payload.get("schema") != STAGE1_EXECUTION_SCHEMA:
        raise ValueError(
            f"Expected {STAGE1_EXECUTION_SCHEMA!r}, got {payload.get('schema')!r}: {report_path}"
        )
    if payload.get("status") != "READY":
        raise ValueError("Stage-1 execution report must have status=READY before Stage-2 handoff")
    raw_results = payload.get("items")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("Stage-1 execution report contains no results")

    products: list[ValidatedStage1Product] = []
    seen_paths: set[str] = set()
    for index, row in enumerate(raw_results):
        if not isinstance(row, Mapping):
            raise ValueError(f"Stage-1 execution result #{index} is not a mapping")
        status = str(row.get("status") or "")
        if status not in _SUCCESS_EXECUTION_STATUSES:
            raise ValueError(
                f"Stage-1 execution result #{index} is not handoff-safe: status={status!r}"
            )
        product_text = str(row.get("product_path") or "").strip()
        checksum = str(row.get("product_checksum_sha256") or "").strip().casefold()
        provenance_text = str(row.get("provenance_path") or "").strip()
        if not product_text or not _SHA256.fullmatch(checksum) or not provenance_text:
            raise ValueError(
                f"Stage-1 execution result #{index} lacks product_path/checksum/provenance_path"
            )
        product_path = Path(product_text)
        provenance_path = Path(provenance_text)
        if provenance_path != product_path.with_name(product_path.name + ".provenance.json"):
            raise ValueError(f"Stage-1 provenance path does not match product: {product_path}")
        if not product_path.is_file():
            raise FileNotFoundError(f"Stage-1 product is missing: {product_path}")
        if not provenance_path.is_file():
            raise FileNotFoundError(f"Stage-1 provenance sidecar is missing: {provenance_path}")
        canonical_path = str(product_path.resolve()).casefold()
        if canonical_path in seen_paths:
            raise ValueError(f"Stage-1 execution report repeats product path: {product_path}")
        seen_paths.add(canonical_path)

        actual_checksum = _sha256_file(product_path).casefold()
        if actual_checksum != checksum:
            raise ValueError(
                f"Stage-1 product checksum mismatch: {product_path}; report={checksum}, actual={actual_checksum}"
            )
        provenance = _read_mapping(provenance_path)
        if provenance.get("schema") != STAGE1_PROVENANCE_SCHEMA:
            raise ValueError(f"Incompatible Stage-1 provenance schema: {provenance_path}")
        provenance_product = provenance.get("product")
        if not isinstance(provenance_product, Mapping):
            raise ValueError(f"Stage-1 provenance product section is missing: {provenance_path}")
        provenance_checksum = str(provenance_product.get("checksum_sha256") or "").strip().casefold()
        if provenance_checksum != checksum:
            raise ValueError(
                f"Stage-1 provenance/report checksum mismatch for product: {product_path}"
            )
        if provenance_product.get("size_bytes") != product_path.stat().st_size:
            raise ValueError(f"Stage-1 provenance product size mismatch: {product_path}")
        if not provenance_product.get("locator") or Path(str(provenance_product["locator"])).name != product_path.name:
            raise ValueError(f"Stage-1 provenance product locator mismatch: {product_path}")
        report_product = row.get("product")
        if not isinstance(report_product, Mapping):
            raise ValueError(f"Stage-1 execution product section is invalid: {product_path}")
        for key in ("store_id", "backend", "locator"):
            report_value = report_product.get(key)
            provenance_value = provenance_product.get(key)
            if not report_value or not provenance_value or str(report_value) != str(provenance_value):
                raise ValueError(
                    f"Stage-1 provenance/report product {key} mismatch for product: {product_path}"
                )
        report_fingerprint = str(row.get("fingerprint") or "").strip()
        provenance_fingerprint = str(provenance.get("fingerprint") or "").strip()
        if not _SHA256.fullmatch(report_fingerprint) or not _SHA256.fullmatch(provenance_fingerprint):
            raise ValueError(f"Stage-1 fingerprint is not SHA-256: {product_path}")
        if report_fingerprint != provenance_fingerprint:
            raise ValueError(f"Stage-1 provenance/report fingerprint mismatch: {product_path}")
        provenance_obs = provenance.get("observation")
        if not isinstance(provenance_obs, Mapping):
            raise ValueError(f"Stage-1 provenance observation section is missing: {provenance_path}")
        for key in ("source_observation_uid", "observation_uid", "native_observation_id"):
            report_value = row.get(key)
            provenance_value = provenance_obs.get(key)
            if not report_value or not provenance_value or str(report_value) != str(provenance_value):
                raise ValueError(
                    f"Stage-1 provenance/report {key} mismatch for product: {product_path}"
                )
        if product_path.name != f"{row['source_observation_uid']}_processed.fits":
            raise ValueError(f"Stage-1 product filename/source identity mismatch: {product_path}")

        products.append(
            ValidatedStage1Product(
                native_observation_id=(str(row.get("native_observation_id")) if row.get("native_observation_id") else None),
                source_observation_uid=(str(row.get("source_observation_uid")) if row.get("source_observation_uid") else None),
                observation_uid=(str(row.get("observation_uid")) if row.get("observation_uid") else None),
                execution_status=status,
                product_path=product_path,
                product_checksum_sha256=checksum,
                provenance_path=provenance_path,
                fingerprint=report_fingerprint,
            )
        )
    return tuple(products)


def _check_frame_evidence(frame: Frame, product: ValidatedStage1Product) -> None:
    observation = frame.observation
    if (observation is None or observation.observation_uid != product.observation_uid
            or observation.native_identity != product.native_observation_id):
        raise ValueError(f"Stage-1 product Frame observation mismatch: {product.product_path}")
    for metadata in (frame.header_json or {}, frame.quality_flags or {}):
        existing = metadata.get("stage1_fingerprint")
        if existing is not None and existing != product.fingerprint:
            raise ValueError(f"Stage-1 product Frame fingerprint conflict: {product.product_path}")
        source_uid = metadata.get("source_observation_uid")
        if source_uid is not None and source_uid != product.source_observation_uid:
            raise ValueError(f"Stage-1 product Frame source identity conflict: {product.product_path}")


def _persist_selected_evidence(
    session: Session, items: Sequence[Stage1HandoffItem], indexed_ids: Sequence[int],
) -> None:
    """Validate every selected exact locator before mutating any Frame metadata."""
    selected = [item for item in items if item.status == "SELECTED_FOR_STAGE2"]
    updates: list[tuple[Frame, ValidatedStage1Product]] = []
    used_ids: set[int] = set()
    with session.no_autoflush:
        for item in selected:
            product = item.product
            locator_uid = make_frame_location_uid(str(product.product_path))
            matches = session.scalars(
                select(Frame).join(FrameLocation, FrameLocation.frame_id == Frame.frame_id)
                .where(FrameLocation.locator_uid == locator_uid)
            ).all()
            if len(matches) != 1:
                raise ValueError(f"Stage-1 product has no unique exact Frame location: {product.product_path}")
            frame = matches[0]
            if int(frame.frame_id) not in indexed_ids or int(frame.frame_id) in used_ids:
                raise ValueError(f"Stage-1 product not uniquely indexed: {product.product_path}")
            if item.selected_frame_id is not None and frame.frame_id != item.selected_frame_id:
                raise ValueError(f"Stage-1 product selected Frame mismatch: {product.product_path}")
            _check_frame_evidence(frame, product)
            location = next(
                loc for loc in frame.locations if loc.locator_uid == locator_uid
            )
            if location.content_sha256 and location.content_sha256.casefold() != product.product_checksum_sha256:
                raise ValueError(f"Stage-1 product location checksum conflict: {product.product_path}")
            used_ids.add(int(frame.frame_id))
            updates.append((frame, product))
        if len(updates) != len(selected):
            raise ValueError("Stage-1 product mapping is incomplete")
        for frame, product in updates:
            add_or_touch_frame_location(
                frame, locator=str(product.product_path),
                content_sha256=product.product_checksum_sha256, make_primary=True,
            )
            frame.header_json = {**(frame.header_json or {}), "stage1_fingerprint": product.fingerprint,
                                 "source_observation_uid": product.source_observation_uid}
    session.commit()


def _validate_executed_stage2(
    session: Session, items: Sequence[Stage1HandoffItem], result: Mapping[str, Any],
) -> None:
    """Gate evidence on complete, selected, successfully processed science work.

    This checks result and series membership, not independent durable science state.
    Stage-2 commits independently; failure here cannot roll those commits back.
    """
    selected = [item for item in items if item.status == "SELECTED_FOR_STAGE2"]
    if result.get("execute") is not True or result.get("status") != "DONE" or result.get("warnings"):
        raise ValueError("Stage-2 execution is not a warning-free DONE")
    if result.get("failed_files") != 0 or result.get("discovered_files") != len(selected) or result.get("detected_files") != len(selected):
        raise ValueError("Stage-2 did not detect every selected product")
    indexes = result.get("index_results")
    if not isinstance(indexes, list) or not indexes or any(
        not isinstance(row, Mapping) or row.get("failed") != 0 for row in indexes
    ) or sum(row.get("discovered", -1) for row in indexes) != len(selected):
        raise ValueError("Stage-2 did not index every selected product")
    indexed = result.get("indexed_frame_ids")
    if not isinstance(indexed, list) or len(indexed) != len(selected) or len(set(indexed)) != len(selected):
        raise ValueError("Stage-2 indexed Frame coverage is incomplete")
    selections = result.get("observation_selection_results")
    if not isinstance(selections, list) or len(selections) != 1 or selections[0].get("mode") != "EXECUTE_PERSISTED_FRAMES":
        raise ValueError("Stage-2 persisted-frame selection is missing")
    rows = selections[0].get("rows")
    if not isinstance(rows, list):
        raise ValueError("Stage-2 persisted-frame selection rows are missing")
    selected_ids = [row.get("selected_frame_id") for row in rows if row.get("action") in {
        ObservationScienceAction.SELECTED.value,
        ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID.value,
    }]
    if len(selected_ids) != len(selected) or set(selected_ids) != set(indexed):
        raise ValueError("Stage-2 did not select every indexed Frame for science")
    created = result.get("created_series_ids")
    processed = result.get("processed_series_ids")
    pipelines = result.get("pipeline_results")
    if (not isinstance(created, list) or not created or len(set(created)) != len(created)
            or not isinstance(processed, list) or set(processed) != set(created)
            or len(processed) != len(created) or not isinstance(pipelines, list)
            or len(pipelines) != len(created) or {row.get("series_id") for row in pipelines} != set(created)
            or any(row.get("status") != "DONE" or row.get("science_status") != "OK" for row in pipelines)):
        raise ValueError("Stage-2 did not complete exactly the intended series with OK science")
    with session.no_autoflush:
        memberships = session.execute(
            select(FieldSeriesFrame.series_id, FieldSeriesFrame.frame_id).where(
                FieldSeriesFrame.series_id.in_(created)
            )
        ).all()
    if ({frame_id for _series_id, frame_id in memberships} != set(indexed)
            or {series_id for series_id, _frame_id in memberships} != set(created)):
        raise ValueError("Stage-2 completed series do not exactly cover selected Frames")


def _candidate_from_detected(item: Any) -> ScienceFileIdentityCandidate:
    return ScienceFileIdentityCandidate(
        path=item.path,
        instrument=item.instrument,
        frame_uid=item.frame_uid,
        observation_uid=item.observation_uid,
        identity_scheme=item.identity_scheme,
        native_identity=item.native_identity,
        file_size_bytes=getattr(item, "file_size_bytes", None),
        naxis1=getattr(item, "naxis1", None),
        naxis2=getattr(item, "naxis2", None),
        object_name=getattr(item, "object_name", None),
        identity_status=item.observation_identity_status,
        identity_detail=item.observation_identity_detail,
    )


def handoff_stage1_execution(
    session: Session,
    *,
    execution_report_path: str | Path,
    execute: bool = False,
    catalog_name: str | None = None,
    instrument_profile: str = "auto",
    master_profile: str | Path | None = None,
    robophot_profile: str | Path | None = None,
    min_frames_per_series: int = 10,
    ensemble_min_ref_stars_per_frame: int = 30,
    max_field_separation_arcmin: float = 30.0,
    max_series_gap_minutes: float = 60.0,
    out_root: str | Path = "exports",
    log_dir: str | Path = "logs",
    manifest_out: str | Path | None = None,
) -> Stage1HandoffReport:
    """Handoff exact products from one Stage-1 execution report to Stage 2.

    Preflight is performed before any new Frame is indexed. Products for physical
    observations already represented in field_series_frames are retained as audit
    context and are not inserted as alternate Frame representatives. Ambiguous or
    conflicting identities block the entire handoff before DB writes.
    """

    products = load_validated_stage1_products(execution_report_path)

    # Import lazily: this path requires Astropy/FITS, while report validation above
    # remains usable in minimal release-tool environments.
    from grandgousier.archive.auto_process import process_archive_path, scan_archive_paths

    paths = [item.product_path for item in products]
    detected, _groups, scan_warnings = scan_archive_paths(
        paths,
        instrument_profile=instrument_profile,
        master_profile=master_profile,
        robophot_profile=robophot_profile,
    )
    if scan_warnings or len(detected) != len(products):
        errors = tuple(scan_warnings) or (
            f"Stage-2 handoff scanned {len(detected)} of {len(products)} exact Stage-1 products",
        )
        return Stage1HandoffReport(
            execution_report_path=str(execution_report_path),
            generated_at_utc=_utc_now(),
            execute=bool(execute),
            items=(),
            errors=errors,
        )

    expected_by_path = {str(item.product_path.resolve()).casefold(): item for item in products}
    candidates: list[ScienceFileIdentityCandidate] = []
    identity_errors: list[str] = []
    for detected_item in detected:
        key = str(Path(detected_item.path).resolve()).casefold()
        expected = expected_by_path.get(key)
        if expected is None:
            identity_errors.append(f"unexpected scanned product path: {detected_item.path}")
            continue
        if expected.observation_uid and detected_item.observation_uid != expected.observation_uid:
            identity_errors.append(
                f"P4 observation_uid mismatch for {detected_item.path}: "
                f"report={expected.observation_uid}, FITS={detected_item.observation_uid}"
            )
        if expected.native_observation_id and detected_item.native_identity != expected.native_observation_id:
            identity_errors.append(
                f"native observation identity mismatch for {detected_item.path}: "
                f"report={expected.native_observation_id}, FITS={detected_item.native_identity}"
            )
        candidates.append(_candidate_from_detected(detected_item))

    if identity_errors:
        return Stage1HandoffReport(
            execution_report_path=str(execution_report_path),
            generated_at_utc=_utc_now(),
            execute=bool(execute),
            items=(),
            errors=tuple(identity_errors),
        )

    if len(candidates) != len(products) or len({str(Path(c.path).resolve()).casefold() for c in candidates}) != len(products):
        raise ValueError("Stage-2 scan did not return each exact Stage-1 product once")

    selection = plan_file_science_selection(session, candidates, allow_reassign_existing_frame=False)
    product_by_path = {str(item.product_path): item for item in products}
    items: list[Stage1HandoffItem] = []
    selected_paths: list[Path] = []
    for row in selection.rows:
        path = str(row.get("raw_path") or "")
        product = product_by_path.get(path)
        if product is None:
            # Path spelling can differ only by absolute normalization/case on Windows.
            resolved = str(Path(path).resolve()).casefold()
            product = expected_by_path.get(resolved)
        if product is None:
            raise ValueError(f"P4 handoff selection returned an unknown product path: {path}")
        action = str(row.get("action") or "")
        if action in {
            ObservationScienceAction.ALREADY_REPRESENTED.value,
            ObservationScienceAction.EXISTING_MULTIPLE_REPRESENTATIVES.value,
        }:
            # For the handoff boundary both states are a safe retain/no-op.
            # The general P4 selector keeps EXISTING_MULTIPLE_REPRESENTATIVES
            # as an explicit historical audit condition; handoff does not choose
            # or repair those representatives, it only refuses to add another.
            status = "ALREADY_REPRESENTED"
        elif action in {
            ObservationScienceAction.SELECTED.value,
            ObservationScienceAction.SELECTED_LEGACY_NO_OBSERVATION_ID.value,
        }:
            status = "SELECTED_FOR_STAGE2"
            selected_paths.append(product.product_path)
        elif action in _HARD_BLOCK_ACTIONS:
            status = "BLOCKED"
        else:
            status = "BLOCKED"
        items.append(
            Stage1HandoffItem(
                product=product,
                p4_action=action,
                status=status,
                selected_frame_id=(int(row["selected_frame_id"]) if row.get("selected_frame_id") is not None else None),
                existing_series_ids=tuple(int(value) for value in (row.get("existing_series_ids") or [])),
                reason=str(row.get("reason") or ""),
            )
        )

    if len(items) != len(products) or len({str(item.product.product_path.resolve()).casefold() for item in items}) != len(products):
        raise ValueError("P4 selection did not account for every Stage-1 product exactly once")

    if any(item.status == "BLOCKED" for item in items):
        return Stage1HandoffReport(
            execution_report_path=str(execution_report_path),
            generated_at_utc=_utc_now(),
            execute=bool(execute),
            items=tuple(items),
        )

    # Existing locators may refer to an already indexed selected product. Refuse
    # contradictory provenance before the nested Stage-2 workflow can commit work.
    for item in items:
        if item.status != "SELECTED_FOR_STAGE2":
            continue
        frame = find_frame_by_locator(session, str(item.product.product_path))
        if frame is not None:
            _check_frame_evidence(frame, item.product)
            location = next(
                loc for loc in frame.locations
                if loc.locator_uid == make_frame_location_uid(str(item.product.product_path))
            )
            if (location.content_sha256 and
                    location.content_sha256.casefold() != item.product.product_checksum_sha256):
                raise ValueError(f"Stage-1 product location checksum conflict: {item.product.product_path}")

    stage2_result: Mapping[str, Any] | None = None
    if execute and selected_paths:
        root = _common_parent(selected_paths)
        nested_manifest = manifest_out
        try:
            result = process_archive_path(
                session,
                root=root,
                exact_paths=selected_paths,
                catalog_name=catalog_name,
                execute=True,
                instrument_profile=instrument_profile,
                master_profile=master_profile,
                robophot_profile=robophot_profile,
                recursive=False,
                min_frames_per_series=min_frames_per_series,
                ensemble_min_ref_stars_per_frame=ensemble_min_ref_stars_per_frame,
                max_field_separation_arcmin=max_field_separation_arcmin,
                max_series_gap_minutes=max_series_gap_minutes,
                include_assigned=False,
                out_root=out_root,
                log_dir=log_dir,
                manifest_out=nested_manifest,
            )
        except Exception as exc:
            return Stage1HandoffReport(
                execution_report_path=str(execution_report_path),
                generated_at_utc=_utc_now(),
                execute=bool(execute),
                items=tuple(items),
                errors=(f"Stage-2 handoff failed: {type(exc).__name__}: {exc}",),
            )
        try:
            result_dict = result.to_dict()
            _validate_executed_stage2(session, items, result_dict)
            _persist_selected_evidence(session, items, result_dict.get("indexed_frame_ids", []))
        except Exception as exc:
            session.rollback()
            return Stage1HandoffReport(
                execution_report_path=str(execution_report_path), generated_at_utc=_utc_now(),
                execute=True, items=tuple(items),
                errors=(f"Stage-1 evidence handoff failed: {type(exc).__name__}: {exc}",),
            )
        stage2_result = result_dict

    return Stage1HandoffReport(
        execution_report_path=str(execution_report_path),
        generated_at_utc=_utc_now(),
        execute=bool(execute),
        items=tuple(items),
        stage2_result=stage2_result,
    )


def write_stage1_handoff_json(report: Stage1HandoffReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.to_json(indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "STAGE1_HANDOFF_SCHEMA",
    "Stage1HandoffItem",
    "Stage1HandoffReport",
    "ValidatedStage1Product",
    "handoff_stage1_execution",
    "load_validated_stage1_products",
    "write_stage1_handoff_json",
]
