from __future__ import annotations

import csv
import hashlib
import json
import os
import signal
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from grandgousier import __version__
from grandgousier.archive.manifest import extract_numeric_frame_id
from grandgousier.archive.disk_guard import (
    DEFAULT_STAGE1_DISK_RESERVE_BYTES,
    DEFAULT_UNKNOWN_PRODUCT_BYTES,
    DiskRequirement,
    STAGE1_METADATA_OVERHEAD_BYTES,
    check_disk_budget,
)
from grandgousier.archive.stage1_checkpoint import (
    Stage1Checkpoint,
    item_fingerprint,
)
from grandgousier.archive.stage1_cache import cleanup_atomic_output_parts
from grandgousier.archive.master_reduction import (
    REDUCTION_ALGORITHM,
    MasterReductionResult,
    MasterReductionTask,
    reduce_master_task,
)
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetProvider,
    AssetRef,
    CalibrationPlan,
    CalibrationPlanItem,
    LocalAsset,
    MetadataProvider,
    ProductRef,
    ProductStore,
)

STAGE1_EXECUTION_SCHEMA = "grandgousier.stage1.execution.v1"
STAGE1_PROVENANCE_SCHEMA = "grandgousier.stage1.product-provenance.v1"
SOURCE_PROCESSED_COPY_RECIPE = "SOURCE_PROCESSED_COPY_V1"
_SUCCESS_STATUSES = {"REDUCED", "IMPORTED", "SKIPPED_EXISTING", "ADOPTED_EXISTING"}


class Stage1StopRequested(RuntimeError):
    """Raised when a graceful process stop was requested at a safe boundary."""


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


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _provenance_path(product_path: Path) -> Path:
    return product_path.with_name(product_path.name + ".provenance.json")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    if temp.exists():
        temp.unlink()
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _checkpoint_product_provenance(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if str(result.get("status") or "") not in _SUCCESS_STATUSES:
        return None
    product_text = str(result.get("product_path") or "").strip()
    provenance_text = str(result.get("provenance_path") or "").strip()
    expected_checksum = str(result.get("product_checksum_sha256") or "").strip()
    fingerprint = str(result.get("fingerprint") or "").strip()
    if not product_text or not provenance_text or not expected_checksum or not fingerprint:
        return None
    product_path = Path(product_text)
    provenance_path = Path(provenance_text)
    if not product_path.is_file() or not provenance_path.is_file():
        return None
    try:
        actual_checksum = _sha256_file(product_path)
    except OSError:
        return None
    if actual_checksum.casefold() != expected_checksum.casefold():
        return None
    sidecar = _read_json(provenance_path)
    if sidecar is None or sidecar.get("schema") != STAGE1_PROVENANCE_SCHEMA:
        return None
    if str(sidecar.get("fingerprint") or "") != fingerprint:
        return None
    product = sidecar.get("product")
    if not isinstance(product, Mapping):
        return None
    if str(product.get("checksum_sha256") or "").casefold() != actual_checksum.casefold():
        return None
    return sidecar


def _checkpoint_product_is_durable(result: Mapping[str, Any]) -> bool:
    return _checkpoint_product_provenance(result) is not None


def _checkpoint_paths_match(
    result: Mapping[str, Any],
    *,
    item: CalibrationPlanItem,
    product_store: ProductStore,
) -> tuple[Path, Path] | None:
    if item.product is None:
        return None
    stored_product = str(result.get("product_path") or "").strip()
    stored_provenance = str(result.get("provenance_path") or "").strip()
    if not stored_product or not stored_provenance:
        return None
    try:
        current_product = Path(product_store.resolve(item.product))
        current_provenance = _provenance_path(current_product)
        paths_match = (
            Path(stored_product).resolve(strict=False)
            == current_product.resolve(strict=False)
            and Path(stored_provenance).resolve(strict=False)
            == current_provenance.resolve(strict=False)
        )
        return (current_product, current_provenance) if paths_match else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _checkpoint_input_refs(item: CalibrationPlanItem) -> tuple[AssetRef, ...] | None:
    if item.status != "READY" or item.product is None:
        return None
    if item.action == "IMPORT_PROCESSED":
        processed = item.observation.asset("processed")
        return (processed,) if processed is not None else None
    if item.action == "REDUCE_RAW" and item.observation.instrument == "MASTER":
        roles = ("science_raw", "processing_header", "dark", "flat")
        refs = tuple(item.observation.asset(role) for role in roles)
        if any(ref is None for ref in refs):
            return None
        return tuple(ref for ref in refs if ref is not None)
    return None


def _checkpoint_sidecar_inputs_match(
    sidecar: Mapping[str, Any],
    inputs: Sequence[MaterializedInput],
) -> bool:
    sidecar_inputs = sidecar.get("inputs")
    if not isinstance(sidecar_inputs, list) or len(sidecar_inputs) != len(inputs):
        return False
    ordered = sorted(inputs, key=lambda value: (value.ref.role, value.ref.ref_uid))
    for entry, materialized in zip(sidecar_inputs, ordered, strict=True):
        if not isinstance(entry, Mapping):
            return False
        ref = entry.get("ref")
        if not isinstance(ref, Mapping):
            return False
        if ref.get("role") != materialized.ref.role or ref.get("ref_uid") != materialized.ref.ref_uid:
            return False
        checksum = entry.get("checksum_sha256")
        if not _is_sha256_checksum(checksum):
            return False
        if str(checksum).casefold() != materialized.checksum_sha256.casefold():
            return False
        declared_checksum = materialized.ref.checksum_sha256
        if declared_checksum:
            sidecar_declared_checksum = ref.get("checksum_sha256")
            if not _is_sha256_checksum(sidecar_declared_checksum):
                return False
            if str(sidecar_declared_checksum).casefold() != declared_checksum.casefold():
                return False
        elif "checksum_sha256" in ref:
            return False
    return True


def _is_sha256_checksum(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


@dataclass(frozen=True, slots=True)
class MaterializedInput:
    ref: AssetRef
    path: Path
    checksum_sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "checksum_sha256": self.checksum_sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class Stage1ExecutionItemResult:
    source_observation_uid: str | None
    native_observation_id: str | None
    observation_uid: str | None
    action: str
    status: str
    product: ProductRef | None
    product_path: str | None = None
    product_checksum_sha256: str | None = None
    provenance_path: str | None = None
    fingerprint: str | None = None
    error: str | None = None

    @property
    def successful(self) -> bool:
        return self.status in _SUCCESS_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_observation_uid": self.source_observation_uid,
            "native_observation_id": self.native_observation_id,
            "observation_uid": self.observation_uid,
            "action": self.action,
            "status": self.status,
            "product": self.product.to_dict() if self.product else None,
            "product_path": self.product_path,
            "product_checksum_sha256": self.product_checksum_sha256,
            "provenance_path": self.provenance_path,
            "fingerprint": self.fingerprint,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class Stage1ExecutionReport:
    source_id: str
    generated_at_utc: str
    plan_schema: str
    plan_status: str
    results: tuple[Stage1ExecutionItemResult, ...]
    schema: str = STAGE1_EXECUTION_SCHEMA

    @property
    def failed_items(self) -> int:
        return sum(item.status == "FAILED" for item in self.results)

    @property
    def blocked_items(self) -> int:
        return sum(item.status == "BLOCKED" for item in self.results)

    @property
    def successful_items(self) -> int:
        return sum(item.successful for item in self.results)

    @property
    def status(self) -> str:
        if self.failed_items:
            return "FAILED"
        if self.blocked_items:
            return "BLOCKED"
        return "READY"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generated_at_utc": self.generated_at_utc,
            "status": self.status,
            "source_id": self.source_id,
            "plan_schema": self.plan_schema,
            "plan_status": self.plan_status,
            "summary": {
                "items": len(self.results),
                "successful_items": self.successful_items,
                "blocked_items": self.blocked_items,
                "failed_items": self.failed_items,
                "reduced": sum(item.status == "REDUCED" for item in self.results),
                "imported": sum(item.status == "IMPORTED" for item in self.results),
                "skipped_existing": sum(item.status == "SKIPPED_EXISTING" for item in self.results),
                "adopted_existing": sum(item.status == "ADOPTED_EXISTING" for item in self.results),
            },
            "items": [item.to_dict() for item in self.results],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True, slots=True)
class Stage1StreamingExecutionSummary:
    source_id: str
    generated_at_utc: str
    plan_status: str
    discovered_records: int
    planned_items: int
    counts: Mapping[str, int]
    attempted_items: int | None = None
    stopped_early: bool = False
    schema: str = STAGE1_EXECUTION_SCHEMA

    @property
    def failed_items(self) -> int:
        return int(self.counts.get("FAILED", 0))

    @property
    def blocked_items(self) -> int:
        return int(self.counts.get("BLOCKED", 0))

    @property
    def status(self) -> str:
        if self.failed_items:
            return "FAILED"
        if self.blocked_items:
            return "BLOCKED"
        if self.stopped_early:
            return "FAILED"
        return "READY"

    def to_dict(self) -> dict[str, Any]:
        summary = {
            "discovered_records": self.discovered_records,
            "items": self.planned_items,
            "successful_items": sum(
                value for key, value in self.counts.items() if key in _SUCCESS_STATUSES
            ),
            "blocked_items": self.blocked_items,
            "failed_items": self.failed_items,
            "reduced": int(self.counts.get("REDUCED", 0)),
            "imported": int(self.counts.get("IMPORTED", 0)),
            "skipped_existing": int(self.counts.get("SKIPPED_EXISTING", 0)),
            "adopted_existing": int(self.counts.get("ADOPTED_EXISTING", 0)),
        }
        if self.attempted_items is not None:
            summary["attempted_items"] = self.attempted_items
            summary["reported_items"] = sum(self.counts.values())
            summary["stopped_early"] = self.stopped_early
        return {
            "schema": self.schema,
            "generated_at_utc": self.generated_at_utc,
            "status": self.status,
            "source_id": self.source_id,
            "plan_schema": "grandgousier.stage1.plan.v1",
            "plan_status": self.plan_status,
            "summary": summary,
            "items": [],
        }


class _StreamingJsonArtifact:
    def __init__(self, path: str | Path, prefix: Mapping[str, Any]) -> None:
        self.target = Path(path)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.temp = self.target.with_name(self.target.name + ".part")
        if self.temp.exists():
            self.temp.unlink()
        self.handle = self.temp.open("w", encoding="utf-8")
        self.handle.write("{")
        self._first_field = True
        for key, value in prefix.items():
            self._field(key, value)
        self.handle.write(',"items":[')
        self._first_item = True

    def _field(self, key: str, value: Any) -> None:
        if not self._first_field:
            self.handle.write(",")
        json.dump(key, self.handle, ensure_ascii=False)
        self.handle.write(":")
        json.dump(value, self.handle, ensure_ascii=False)
        self._first_field = False

    def write_item(self, value: Mapping[str, Any]) -> None:
        if not self._first_item:
            self.handle.write(",")
        json.dump(value, self.handle, ensure_ascii=False)
        self._first_item = False

    def finalize(self, fields: Mapping[str, Any]) -> Path:
        self.handle.write("]")
        for key, value in fields.items():
            self._field(key, value)
        self.handle.write("}\n")
        self.handle.close()
        os.replace(self.temp, self.target)
        return self.target

    def abort(self) -> None:
        try:
            self.handle.close()
        finally:
            if self.temp.exists():
                self.temp.unlink()


class _StreamingCsvArtifact:
    def __init__(self, path: str | Path, columns: Sequence[str]) -> None:
        self.target = Path(path)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.temp = self.target.with_name(self.target.name + ".part")
        if self.temp.exists():
            self.temp.unlink()
        self.handle = self.temp.open("w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.handle, fieldnames=columns)
        self.writer.writeheader()

    def write(self, row: Mapping[str, Any]) -> None:
        self.writer.writerow(row)

    def finalize(self) -> Path:
        self.handle.close()
        os.replace(self.temp, self.target)
        return self.target

    def abort(self) -> None:
        try:
            self.handle.close()
        finally:
            if self.temp.exists():
                self.temp.unlink()


class _ExecutionContext:
    def __init__(self, asset_provider: AssetProvider) -> None:
        self.asset_provider = asset_provider
        self.materialized: dict[str, MaterializedInput] = {}

    def materialize(self, ref: AssetRef) -> MaterializedInput:
        cached = self.materialized.get(ref.ref_uid)
        if cached is not None:
            return cached
        local = self.asset_provider.materialize(ref)
        if not isinstance(local, LocalAsset):
            raise TypeError("AssetProvider.materialize() must return LocalAsset")
        if local.ref.ref_uid != ref.ref_uid:
            raise ValueError("AssetProvider returned a LocalAsset for a different AssetRef")
        path = Path(local.path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"Materialized asset is missing or empty: {path}")
        checksum = _sha256_file(path)
        if ref.checksum_sha256 and checksum.casefold() != ref.checksum_sha256.casefold():
            raise ValueError(
                f"Asset checksum mismatch for {ref.role}: expected={ref.checksum_sha256}, actual={checksum}"
            )
        item = MaterializedInput(
            ref=ref,
            path=path,
            checksum_sha256=checksum,
            size_bytes=int(path.stat().st_size),
        )
        self.materialized[ref.ref_uid] = item
        return item


def _input_fingerprint_payload(
    item: CalibrationPlanItem,
    inputs: Sequence[MaterializedInput],
    *,
    recipe: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    obs = item.observation
    ordered = sorted(inputs, key=lambda value: (value.ref.role, value.ref.ref_uid))
    return {
        "schema": STAGE1_PROVENANCE_SCHEMA,
        "source_id": obs.source_id,
        "native_observation_id": obs.native_observation_id,
        "source_observation_uid": obs.source_observation_uid,
        "observation_uid": obs.observation_uid,
        "identity_scheme": obs.identity_scheme,
        "action": item.action,
        "recipe": recipe,
        "parameters": dict(parameters),
        "inputs": [
            {
                "role": value.ref.role,
                "ref_uid": value.ref.ref_uid,
                "checksum_sha256": value.checksum_sha256,
            }
            for value in ordered
        ],
    }


def _build_provenance(
    item: CalibrationPlanItem,
    inputs: Sequence[MaterializedInput],
    *,
    recipe: str,
    parameters: Mapping[str, Any],
    fingerprint: str,
    product: ProductRef,
    product_path: Path,
    product_checksum_sha256: str,
    execution_status: str,
) -> dict[str, Any]:
    obs = item.observation
    return {
        "schema": STAGE1_PROVENANCE_SCHEMA,
        "generated_at_utc": _utc_now(),
        "grandgousier_version": __version__,
        "execution_status": execution_status,
        "fingerprint": fingerprint,
        "observation": {
            "source_id": obs.source_id,
            "native_observation_id": obs.native_observation_id,
            "source_observation_uid": obs.source_observation_uid,
            "identity_scheme": obs.identity_scheme,
            "p4_native_identity": obs.p4_native_identity,
            "observation_uid": obs.observation_uid,
            "instrument": obs.instrument,
            "camera": obs.camera,
            "filter_name": obs.filter_name,
            "observed_at": obs.observed_at,
        },
        "recipe": {"name": recipe, "parameters": dict(parameters)},
        "inputs": [value.to_dict() for value in sorted(inputs, key=lambda v: (v.ref.role, v.ref.ref_uid))],
        "product": {
            **product.to_dict(),
            "checksum_sha256": product_checksum_sha256,
            "size_bytes": int(product_path.stat().st_size),
        },
    }


def _validate_existing_sidecar(
    *,
    sidecar: Mapping[str, Any],
    expected_fingerprint: str,
    product_path: Path,
) -> tuple[bool, str | None, str | None]:
    if sidecar.get("schema") != STAGE1_PROVENANCE_SCHEMA:
        return False, None, "existing provenance schema is missing or incompatible"
    if str(sidecar.get("fingerprint") or "") != expected_fingerprint:
        return False, None, "existing provenance fingerprint does not match current inputs/recipe"
    product = sidecar.get("product")
    if not isinstance(product, Mapping):
        return False, None, "existing provenance product section is missing"
    recorded_checksum = str(product.get("checksum_sha256") or "").strip()
    if not recorded_checksum:
        return False, None, "existing provenance product checksum is missing"
    actual_checksum = _sha256_file(product_path)
    if actual_checksum.casefold() != recorded_checksum.casefold():
        return False, actual_checksum, "existing product checksum differs from provenance"
    return True, actual_checksum, None


def _master_task_from_item(
    item: CalibrationPlanItem,
    materialized: Mapping[str, MaterializedInput],
    output_path: Path,
) -> MasterReductionTask:
    obs = item.observation

    def one(role: str) -> MaterializedInput:
        value = materialized.get(role)
        if value is None:
            raise ValueError(f"MASTER execution requires asset role {role!r}")
        return value

    raw = one("science_raw")
    header = one("processing_header")
    dark = one("dark")
    flat = one("flat")
    raw_id = None
    raw_id_text = str(obs.metadata.get("raw_id") or "").strip()
    if raw_id_text:
        try:
            raw_id = int(raw_id_text)
        except ValueError:
            raw_id = None
    if raw_id is None:
        raw_id = extract_numeric_frame_id(raw.ref.locator)
    if raw_id is None:
        raise ValueError(f"Cannot determine MASTER raw_id from {raw.ref.locator!r}")
    return MasterReductionTask(
        processed_frame_id=str(obs.metadata.get("processed_frame_id") or "").strip() or None,
        raw_id=int(raw_id),
        raw_remote_path=raw.ref.locator,
        header_remote_path=header.ref.locator,
        dark_remote_path=dark.ref.locator,
        flat_remote_path=flat.ref.locator,
        raw_local_path=raw.path,
        header_local_path=header.path,
        dark_local_path=dark.path,
        flat_local_path=flat.path,
        output_path=output_path,
    )


def _check_product_write_budget(
    *,
    output_path: Path,
    provenance_path: Path,
    product_bytes: int,
    reserve_bytes: int,
) -> None:
    check_disk_budget(
        (
            DiskRequirement(
                target=output_path,
                required_bytes=max(0, int(product_bytes)),
                label="Stage-1 product",
            ),
            DiskRequirement(
                target=provenance_path,
                required_bytes=STAGE1_METADATA_OVERHEAD_BYTES,
                label="Stage-1 provenance",
            ),
        ),
        reserve_bytes=reserve_bytes,
    )


def _result_for_item(
    item: CalibrationPlanItem,
    *,
    status: str,
    product: ProductRef | None = None,
    product_path: Path | None = None,
    checksum: str | None = None,
    provenance_path: Path | None = None,
    fingerprint: str | None = None,
    error: str | None = None,
) -> Stage1ExecutionItemResult:
    obs = item.observation
    return Stage1ExecutionItemResult(
        source_observation_uid=obs.source_observation_uid,
        native_observation_id=obs.native_observation_id,
        observation_uid=obs.observation_uid,
        action=item.action,
        status=status,
        product=product,
        product_path=str(product_path) if product_path is not None else None,
        product_checksum_sha256=checksum,
        provenance_path=str(provenance_path) if provenance_path is not None else None,
        fingerprint=fingerprint,
        error=error,
    )


def _execute_item(
    item: CalibrationPlanItem,
    *,
    context: _ExecutionContext,
    product_store: ProductStore,
    overwrite: bool,
    max_dark_temperature_delta_c: float | None,
) -> Stage1ExecutionItemResult:
    if item.status != "READY" or item.product is None:
        return _result_for_item(
            item,
            status="BLOCKED",
            product=item.product,
            error="; ".join(item.issues) or "Stage-1 plan item is not READY",
        )

    product = item.product
    output_path = product_store.resolve(product)
    provenance_path = _provenance_path(output_path)

    if item.action == "IMPORT_PROCESSED":
        processed_ref = item.observation.asset("processed")
        if processed_ref is None:
            return _result_for_item(
                item,
                status="FAILED",
                product=product,
                product_path=output_path,
                error="IMPORT_PROCESSED requires a processed asset",
            )
        try:
            inputs = [context.materialize(processed_ref)]
            parameters: dict[str, Any] = {"copy_mode": "byte_preserving"}
            payload = _input_fingerprint_payload(
                item, inputs, recipe=SOURCE_PROCESSED_COPY_RECIPE, parameters=parameters
            )
            fingerprint = _fingerprint(payload)
            if output_path.is_file() and not overwrite:
                sidecar = _read_json(provenance_path)
                if sidecar is not None:
                    valid, checksum, reason = _validate_existing_sidecar(
                        sidecar=sidecar,
                        expected_fingerprint=fingerprint,
                        product_path=output_path,
                    )
                    if valid:
                        return _result_for_item(
                            item,
                            status="SKIPPED_EXISTING",
                            product=product,
                            product_path=output_path,
                            checksum=checksum,
                            provenance_path=provenance_path,
                            fingerprint=fingerprint,
                        )
                    return _result_for_item(
                        item,
                        status="FAILED",
                        product=product,
                        product_path=output_path,
                        checksum=checksum,
                        provenance_path=provenance_path,
                        fingerprint=fingerprint,
                        error=f"Existing product is not safely reusable: {reason}; use --overwrite after review",
                    )
                source_checksum = inputs[0].checksum_sha256
                output_checksum = _sha256_file(output_path)
                if source_checksum.casefold() == output_checksum.casefold():
                    provenance = _build_provenance(
                        item,
                        inputs,
                        recipe=SOURCE_PROCESSED_COPY_RECIPE,
                        parameters=parameters,
                        fingerprint=fingerprint,
                        product=product,
                        product_path=output_path,
                        product_checksum_sha256=output_checksum,
                        execution_status="ADOPTED_EXISTING",
                    )
                    _check_product_write_budget(
                        output_path=output_path,
                        provenance_path=provenance_path,
                        product_bytes=0,
                        reserve_bytes=int(
                            getattr(
                                product_store,
                                "disk_reserve_bytes",
                                DEFAULT_STAGE1_DISK_RESERVE_BYTES,
                            )
                        ),
                    )
                    _atomic_write_json(provenance_path, provenance)
                    return _result_for_item(
                        item,
                        status="ADOPTED_EXISTING",
                        product=product,
                        product_path=output_path,
                        checksum=output_checksum,
                        provenance_path=provenance_path,
                        fingerprint=fingerprint,
                    )
                return _result_for_item(
                    item,
                    status="FAILED",
                    product=product,
                    product_path=output_path,
                    checksum=output_checksum,
                    provenance_path=provenance_path,
                    fingerprint=fingerprint,
                    error="Existing product has no provenance sidecar and differs from source; use --overwrite after review",
                )

            _check_product_write_budget(
                output_path=output_path,
                provenance_path=provenance_path,
                product_bytes=(
                    0
                    if inputs[0].path.resolve(strict=False) == output_path.resolve(strict=False)
                    else inputs[0].size_bytes
                ),
                reserve_bytes=int(
                    getattr(product_store, "disk_reserve_bytes", DEFAULT_STAGE1_DISK_RESERVE_BYTES)
                ),
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp = output_path.with_name(output_path.name + ".part")
            if temp.exists():
                temp.unlink()
            if inputs[0].path.resolve(strict=False) == output_path.resolve(strict=False):
                output_checksum = inputs[0].checksum_sha256
            else:
                shutil.copy2(inputs[0].path, temp)
                os.replace(temp, output_path)
                output_checksum = _sha256_file(output_path)
                if output_checksum.casefold() != inputs[0].checksum_sha256.casefold():
                    raise ValueError("Imported processed FITS checksum changed during copy")
            provenance = _build_provenance(
                item,
                inputs,
                recipe=SOURCE_PROCESSED_COPY_RECIPE,
                parameters=parameters,
                fingerprint=fingerprint,
                product=product,
                product_path=output_path,
                product_checksum_sha256=output_checksum,
                execution_status="IMPORTED",
            )
            _atomic_write_json(provenance_path, provenance)
            return _result_for_item(
                item,
                status="IMPORTED",
                product=product,
                product_path=output_path,
                checksum=output_checksum,
                provenance_path=provenance_path,
                fingerprint=fingerprint,
            )
        except Exception as exc:
            return _result_for_item(
                item,
                status="FAILED",
                product=product,
                product_path=output_path,
                provenance_path=provenance_path,
                error=f"{type(exc).__name__}: {exc}",
            )

    if item.action != "REDUCE_RAW":
        return _result_for_item(
            item,
            status="FAILED",
            product=product,
            product_path=output_path,
            error=f"Unsupported Stage-1 action {item.action!r}",
        )
    if item.observation.instrument != "MASTER":
        return _result_for_item(
            item,
            status="FAILED",
            product=product,
            product_path=output_path,
            error=(
                f"raw Stage-1 execution currently supports MASTER only; instrument={item.observation.instrument!r}. "
                "Use processed import for other sources until an instrument-specific reducer is registered."
            ),
        )

    try:
        materialized_by_role: dict[str, MaterializedInput] = {}
        for role in ("science_raw", "processing_header", "dark", "flat"):
            ref = item.observation.asset(role)
            if ref is None:
                raise ValueError(f"MASTER execution requires asset role {role!r}")
            materialized_by_role[role] = context.materialize(ref)
        inputs = list(materialized_by_role.values())
        parameters = {"max_dark_temperature_delta_c": max_dark_temperature_delta_c}
        payload = _input_fingerprint_payload(
            item, inputs, recipe=REDUCTION_ALGORITHM, parameters=parameters
        )
        fingerprint = _fingerprint(payload)
        task = _master_task_from_item(item, materialized_by_role, output_path)

        if output_path.is_file() and not overwrite:
            sidecar = _read_json(provenance_path)
            if sidecar is not None:
                valid, checksum, reason = _validate_existing_sidecar(
                    sidecar=sidecar,
                    expected_fingerprint=fingerprint,
                    product_path=output_path,
                )
                if valid:
                    return _result_for_item(
                        item,
                        status="SKIPPED_EXISTING",
                        product=product,
                        product_path=output_path,
                        checksum=checksum,
                        provenance_path=provenance_path,
                        fingerprint=fingerprint,
                    )
                return _result_for_item(
                    item,
                    status="FAILED",
                    product=product,
                    product_path=output_path,
                    checksum=checksum,
                    provenance_path=provenance_path,
                    fingerprint=fingerprint,
                    error=f"Existing product is not safely reusable: {reason}; use --overwrite after review",
                )
            return _result_for_item(
                item,
                status="FAILED",
                product=product,
                product_path=output_path,
                provenance_path=provenance_path,
                fingerprint=fingerprint,
                error=(
                    "Existing MASTER product has no canonical checksum provenance sidecar. "
                    "Legacy FITS provenance cards identify source paths but cannot prove the exact input bytes; "
                    "use --overwrite after review to rebuild and stamp canonical provenance"
                ),
            )

        _check_product_write_budget(
            output_path=output_path,
            provenance_path=provenance_path,
            product_bytes=max(
                DEFAULT_UNKNOWN_PRODUCT_BYTES,
                max(value.size_bytes for value in inputs),
            ),
            reserve_bytes=int(
                getattr(product_store, "disk_reserve_bytes", DEFAULT_STAGE1_DISK_RESERVE_BYTES)
            ),
        )
        reduction: MasterReductionResult = reduce_master_task(
            task,
            overwrite=overwrite,
            max_dark_temperature_delta_c=max_dark_temperature_delta_c,
            disk_reserve_bytes=int(
                getattr(product_store, "disk_reserve_bytes", DEFAULT_STAGE1_DISK_RESERVE_BYTES)
            ),
        )
        if reduction.status == "FAILED":
            return _result_for_item(
                item,
                status="FAILED",
                product=product,
                product_path=output_path,
                provenance_path=provenance_path,
                fingerprint=fingerprint,
                error=reduction.error or "MASTER reduction failed",
            )
        checksum = _sha256_file(output_path)
        if reduction.status != "REDUCED":
            return _result_for_item(
                item,
                status="FAILED",
                product=product,
                product_path=output_path,
                provenance_path=provenance_path,
                fingerprint=fingerprint,
                error=(
                    f"MASTER reducer returned unexpected status {reduction.status!r} without a prevalidated "
                    "canonical provenance sidecar; rerun after review or use --overwrite"
                ),
            )
        execution_status = "REDUCED"
        provenance = _build_provenance(
            item,
            inputs,
            recipe=REDUCTION_ALGORITHM,
            parameters=parameters,
            fingerprint=fingerprint,
            product=product,
            product_path=output_path,
            product_checksum_sha256=checksum,
            execution_status=execution_status,
        )
        _atomic_write_json(provenance_path, provenance)
        return _result_for_item(
            item,
            status=execution_status,
            product=product,
            product_path=output_path,
            checksum=checksum,
            provenance_path=provenance_path,
            fingerprint=fingerprint,
        )
    except Exception as exc:
        return _result_for_item(
            item,
            status="FAILED",
            product=product,
            product_path=output_path,
            provenance_path=provenance_path,
            error=f"{type(exc).__name__}: {exc}",
        )


def execute_stage1_plan(
    plan: CalibrationPlan,
    *,
    asset_provider: AssetProvider,
    product_store: ProductStore,
    overwrite: bool = False,
    max_dark_temperature_delta_c: float | None = 5.0,
) -> Stage1ExecutionReport:
    """Materialize and execute a canonical plan without writing Grandgousier DB state."""
    plan_store = dict(plan.product_store)
    actual_store = dict(product_store.public_descriptor)
    for key in ("store_id", "backend"):
        if plan_store.get(key) != actual_store.get(key):
            raise ValueError(
                f"CalibrationPlan product_store {key}={plan_store.get(key)!r} does not match "
                f"execution ProductStore {actual_store.get(key)!r}"
            )

    context = _ExecutionContext(asset_provider)
    if plan.status == "BLOCKED":
        results = tuple(
            _result_for_item(
                item,
                status="BLOCKED",
                product=item.product,
                error=(
                    "; ".join(item.issues)
                    if item.status != "READY"
                    else "Plan contains blocked items; no partial Stage-1 execution was performed"
                ),
            )
            for item in plan.items
        )
    else:
        results = tuple(
            _execute_item(
                item,
                context=context,
                product_store=product_store,
                overwrite=bool(overwrite),
                max_dark_temperature_delta_c=max_dark_temperature_delta_c,
            )
            for item in plan.items
        )
    return Stage1ExecutionReport(
        source_id=plan.source.source_id,
        generated_at_utc=_utc_now(),
        plan_schema=plan.schema,
        plan_status=plan.status,
        results=results,
    )


def execute_stage1_stream(
    *,
    request: ArchiveRequest,
    source: ArchiveSourceDescriptor,
    metadata_provider: MetadataProvider,
    asset_provider: AssetProvider,
    product_store: ProductStore,
    batch_size: int | str | None = None,
    plan_json_path: str | Path,
    report_json_path: str | Path,
    plan_csv_path: str | Path | None = None,
    report_csv_path: str | Path | None = None,
    overwrite: bool = False,
    max_dark_temperature_delta_c: float | None = 5.0,
    checkpoint: Stage1Checkpoint | None = None,
    run_fingerprint: str | None = None,
    stop_on_first_failure: bool = False,
) -> Stage1StreamingExecutionSummary:
    """Stream Stage-1 planning, execution, and reports in bounded batches."""
    from grandgousier.archive.stage1_contract import STAGE1_PLAN_SCHEMA
    from grandgousier.archive.stage1_planner import (
        iter_stage1_plan_batches,
        normalize_stage1_batch_size,
    )

    batch_size = normalize_stage1_batch_size(batch_size)
    generated_at = _utc_now()
    plan_json = _StreamingJsonArtifact(
        plan_json_path,
        {
            "schema": STAGE1_PLAN_SCHEMA,
            "generated_at_utc": generated_at,
            "source": source.to_dict(),
            "request": request.to_dict(),
            "product_store": dict(product_store.public_descriptor),
        },
    )
    report_json = _StreamingJsonArtifact(
        report_json_path,
        {
            "schema": STAGE1_EXECUTION_SCHEMA,
            "generated_at_utc": generated_at,
            "source_id": source.source_id,
            "plan_schema": STAGE1_PLAN_SCHEMA,
        },
    )
    plan_csv = _StreamingCsvArtifact(
        plan_csv_path or Path(plan_json_path).with_suffix(".csv"),
        (
            "source_id",
            "native_observation_id",
            "source_observation_uid",
            "observation_uid",
            "identity_status",
            "instrument",
            "camera",
            "filter",
            "observed_at",
            "action",
            "status",
            "input_state",
            "product_locator",
            "asset_roles",
            "missing_asset_roles",
            "issues",
        ),
    )
    report_csv = _StreamingCsvArtifact(
        report_csv_path or Path(report_json_path).with_suffix(".csv"),
        (
            "native_observation_id",
            "source_observation_uid",
            "observation_uid",
            "action",
            "status",
            "product_locator",
            "product_path",
            "product_checksum_sha256",
            "provenance_path",
            "fingerprint",
            "error",
        ),
    )
    discovered_records = 0
    planned_items = 0
    blocked_plan_items = 0
    planned_blocked_items = 0
    attempted_items = 0
    stopped_early = False
    counts: dict[str, int] = {}
    checkpoint_discovered = 0
    stop_requested = threading.Event()
    previous_sigterm = None

    def request_stop(_signum, _frame) -> None:
        stop_requested.set()

    try:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_stop)
    except (OSError, ValueError):
        previous_sigterm = None

    def write_plan_item(item: CalibrationPlanItem) -> None:
        obs = item.observation
        plan_json.write_item(item.to_dict())
        plan_csv.write(
            {
                "source_id": obs.source_id,
                "native_observation_id": obs.native_observation_id,
                "source_observation_uid": obs.source_observation_uid,
                "observation_uid": obs.observation_uid,
                "identity_status": obs.identity_status,
                "instrument": obs.instrument,
                "camera": obs.camera,
                "filter": obs.filter_name,
                "observed_at": obs.observed_at,
                "action": item.action,
                "status": item.status,
                "input_state": obs.input_state,
                "product_locator": item.product.locator if item.product else "",
                "asset_roles": ",".join(asset.role for asset in obs.assets),
                "missing_asset_roles": ",".join(item.missing_asset_roles),
                "issues": " | ".join(item.issues),
            }
        )

    def write_result(result: Stage1ExecutionItemResult | Mapping[str, Any]) -> None:
        row = result.to_dict() if isinstance(result, Stage1ExecutionItemResult) else dict(result)
        report_json.write_item(row)
        report_csv.write(
            {
                "native_observation_id": row.get("native_observation_id"),
                "source_observation_uid": row.get("source_observation_uid"),
                "observation_uid": row.get("observation_uid"),
                "action": row.get("action"),
                "status": row.get("status"),
                "product_locator": (
                    (row.get("product") or {}).get("locator", "")
                    if isinstance(row.get("product"), Mapping)
                    else ""
                ),
                "product_path": row.get("product_path"),
                "product_checksum_sha256": row.get("product_checksum_sha256"),
                "provenance_path": row.get("provenance_path"),
                "fingerprint": row.get("fingerprint"),
                "error": row.get("error"),
            }
        )
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1

    try:
        product_root = getattr(product_store, "root", None)
        if product_root is not None:
            cache_policy = getattr(asset_provider, "cache_policy", None)
            part_grace = float(getattr(cache_policy, "cleanup_grace_seconds", 3600.0))
            cleanup_atomic_output_parts(product_root, grace_seconds=part_grace)
        maintain_cache = getattr(asset_provider, "maintain_cache", None)
        for plan_batch in iter_stage1_plan_batches(
            request=request,
            source=source,
            metadata_provider=metadata_provider,
            product_store=product_store,
            batch_size=batch_size,
        ):
            if stop_requested.is_set():
                raise Stage1StopRequested("Stage-1 stop requested before the next batch")
            if callable(maintain_cache):
                maintain_cache()
            discovered_records = plan_batch.discovered_records
            if checkpoint is not None:
                checkpoint.record_discovery(
                    discovered=discovered_records - checkpoint_discovered,
                    planned=len(plan_batch.items),
                )
                checkpoint_discovered = discovered_records
            if not plan_batch.items:
                continue
            planned_items += len(plan_batch.items)
            planned_blocked_items += sum(item.status != "READY" for item in plan_batch.items)
            for item in plan_batch.items:
                write_plan_item(item)
            context = None
            batch_blocked = any(item.status != "READY" for item in plan_batch.items)
            for item in plan_batch.items:
                if stop_requested.is_set():
                    raise Stage1StopRequested("Stage-1 stop requested at an item boundary")
                item_key = None
                checkpoint_paths = None
                checkpoint_materialization_error = None
                if checkpoint is not None and run_fingerprint is not None:
                    item_key = item_fingerprint(
                        run_fingerprint=run_fingerprint,
                        item=item.to_dict(),
                    )
                    stored = checkpoint.get_completed(item_key)
                    if stored is not None:
                        checkpoint_paths = _checkpoint_paths_match(
                            stored,
                            item=item,
                            product_store=product_store,
                        )
                    if stored is not None and checkpoint_paths is not None:
                        sidecar = _checkpoint_product_provenance(stored)
                        if sidecar is not None:
                            if context is None:
                                context = _ExecutionContext(asset_provider)
                            refs = _checkpoint_input_refs(item)
                            if refs is not None:
                                try:
                                    materialized_inputs = [context.materialize(ref) for ref in refs]
                                except Exception as exc:  # noqa: BLE001 - do not implicitly retry provider failures
                                    materialized_inputs = None
                                    checkpoint_materialization_error = exc
                                if (
                                    materialized_inputs is not None
                                    and _checkpoint_sidecar_inputs_match(sidecar, materialized_inputs)
                                ):
                                    write_result(stored)
                                    continue
                if checkpoint_materialization_error is not None:
                    if checkpoint is not None:
                        checkpoint.mark_attempted()
                    attempted_items += 1
                    result = _result_for_item(
                        item,
                        status="FAILED",
                        product=item.product,
                        product_path=checkpoint_paths[0] if checkpoint_paths is not None else None,
                        provenance_path=checkpoint_paths[1] if checkpoint_paths is not None else None,
                        error=(
                            f"{type(checkpoint_materialization_error).__name__}: "
                            f"{checkpoint_materialization_error}"
                        ),
                    )
                elif batch_blocked:
                    result = _result_for_item(
                        item,
                        status="BLOCKED",
                        product=item.product,
                        error=(
                            "; ".join(item.issues)
                            if item.status != "READY"
                            else "Batch contains blocked Stage-1 items; no partial batch execution was performed"
                        ),
                    )
                else:
                    if context is None:
                        context = _ExecutionContext(asset_provider)
                    if checkpoint is not None:
                        checkpoint.mark_attempted()
                    attempted_items += 1
                    result = _execute_item(
                        item,
                        context=context,
                        product_store=product_store,
                        overwrite=bool(overwrite),
                        max_dark_temperature_delta_c=max_dark_temperature_delta_c,
                    )
                if result.successful and item_key is not None:
                    result_payload = result.to_dict()
                    if _checkpoint_product_is_durable(result_payload):
                        checkpoint.mark_completed(item_key=item_key, result=result_payload)
                elif result.status == "FAILED" and checkpoint is not None:
                    checkpoint.mark_failed()
                if result.status == "BLOCKED":
                    blocked_plan_items += 1
                write_result(result)
                if stop_on_first_failure and result.status in {"FAILED", "BLOCKED"}:
                    stopped_early = True
                    break
            context = None
            if stopped_early:
                break

        plan_blocked_count = planned_blocked_items if stop_on_first_failure else blocked_plan_items
        plan_status = (
            "FAILED" if stopped_early and counts.get("FAILED", 0)
            else "EMPTY" if planned_items == 0
            else "BLOCKED" if plan_blocked_count else "READY"
        )
        if stopped_early and plan_status == "READY":
            plan_status = "BLOCKED"
        plan_status_value = plan_status
        plan_json.finalize(
            {
                "summary": {
                    "discovered_records": discovered_records,
                    "science_items": planned_items,
                    "ready_items": planned_items - plan_blocked_count,
                    "blocked_items": plan_blocked_count,
                    **(
                        {
                            "attempted_items": attempted_items,
                            "reported_items": sum(counts.values()),
                            "stopped_early": stopped_early,
                        }
                        if stop_on_first_failure else {}
                    ),
                },
                "status": plan_status_value,
            }
        )
        summary = Stage1StreamingExecutionSummary(
            source_id=source.source_id,
            generated_at_utc=generated_at,
            plan_status=plan_status_value,
            discovered_records=discovered_records,
            planned_items=planned_items,
            counts=dict(counts),
            attempted_items=attempted_items if stop_on_first_failure else None,
            stopped_early=stopped_early,
        )
        if checkpoint is not None:
            checkpoint.set_terminal_status("COMPLETE" if summary.status == "READY" else "FAILED")
        report_json.finalize(
            {
                "summary": summary.to_dict()["summary"],
                "status": summary.status,
                "plan_status": summary.plan_status,
            }
        )
        plan_csv.finalize()
        report_csv.finalize()
        return summary
    except (KeyboardInterrupt, Stage1StopRequested):
        if checkpoint is not None:
            checkpoint.set_terminal_status("INTERRUPTED")
        plan_json.abort()
        report_json.abort()
        plan_csv.abort()
        report_csv.abort()
        raise
    except Exception:
        if checkpoint is not None:
            checkpoint.set_terminal_status("FAILED")
        plan_json.abort()
        report_json.abort()
        plan_csv.abort()
        report_csv.abort()
        raise
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def write_stage1_execution_json(report: Stage1ExecutionReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.to_json(indent=2) + "\n", encoding="utf-8")
    return target


def write_stage1_execution_csv(report: Stage1ExecutionReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "native_observation_id",
        "source_observation_uid",
        "observation_uid",
        "action",
        "status",
        "product_locator",
        "product_path",
        "product_checksum_sha256",
        "provenance_path",
        "fingerprint",
        "error",
    ]
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in report.results:
            writer.writerow(
                {
                    "native_observation_id": item.native_observation_id,
                    "source_observation_uid": item.source_observation_uid,
                    "observation_uid": item.observation_uid,
                    "action": item.action,
                    "status": item.status,
                    "product_locator": item.product.locator if item.product else "",
                    "product_path": item.product_path,
                    "product_checksum_sha256": item.product_checksum_sha256,
                    "provenance_path": item.provenance_path,
                    "fingerprint": item.fingerprint,
                    "error": item.error,
                }
            )
    return target


__all__ = [
    "SOURCE_PROCESSED_COPY_RECIPE",
    "STAGE1_EXECUTION_SCHEMA",
    "STAGE1_PROVENANCE_SCHEMA",
    "Stage1ExecutionItemResult",
    "Stage1ExecutionReport",
    "Stage1StreamingExecutionSummary",
    "execute_stage1_plan",
    "execute_stage1_stream",
    "write_stage1_execution_csv",
    "write_stage1_execution_json",
]
