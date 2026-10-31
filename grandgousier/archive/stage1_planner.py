from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from grandgousier.archive.stage1_contract import (
    STAGE1_PLAN_SCHEMA,
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetProvider,
    CalibrationGroup,
    CalibrationPlan,
    CalibrationPlanItem,
    MetadataProvider,
    ObservationRecord,
    ProductStore,
)

CALIBRATION_ROLES = ("bias", "dark", "flat", "processing_header")
DEFAULT_STAGE1_BATCH_SIZE = 128
MAX_STAGE1_BATCH_SIZE = 512


@dataclass(frozen=True, slots=True)
class Stage1PlanBatch:
    items: tuple[CalibrationPlanItem, ...]
    discovered_records: int


@dataclass(frozen=True, slots=True)
class Stage1PlanStreamSummary:
    discovered_records: int
    science_items: int
    ready_items: int
    blocked_items: int

    @property
    def status(self) -> str:
        if self.science_items == 0:
            return "EMPTY"
        return "BLOCKED" if self.blocked_items else "READY"


def normalize_stage1_batch_size(value: int | str | None) -> int:
    try:
        batch_size = DEFAULT_STAGE1_BATCH_SIZE if value is None else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"stage1_batch_size must be an integer from 1 through {MAX_STAGE1_BATCH_SIZE}"
        ) from exc
    if not 1 <= batch_size <= MAX_STAGE1_BATCH_SIZE:
        raise ValueError(
            f"stage1_batch_size must be from 1 through {MAX_STAGE1_BATCH_SIZE} inclusive"
        )
    return batch_size


def build_stage1_plan(
    *,
    request: ArchiveRequest,
    source: ArchiveSourceDescriptor,
    metadata_provider: MetadataProvider,
    asset_provider: AssetProvider,
    product_store: ProductStore,
) -> CalibrationPlan:
    """Build a metadata-only Stage-1 plan without materializing source bytes."""
    if request.source_id != source.source_id:
        raise ValueError(
            f"ArchiveRequest source_id {request.source_id!r} does not match "
            f"source descriptor {source.source_id!r}"
        )
    del asset_provider
    if request.native_observation_ids and source.adapter != "master-manifest":
        raise ValueError("exact native observation selection requires master-manifest")
    records = list(metadata_provider.search(request))
    items: list[CalibrationPlanItem] = []
    for record in records:
        if record.source_id != source.source_id:
            raise ValueError(
                f"MetadataProvider returned source_id {record.source_id!r} for "
                f"source {source.source_id!r}"
            )
        if record.frame_type != "science":
            continue
        items.append(_plan_science_record(record, product_store=product_store))
    return CalibrationPlan.now(
        source=source,
        request=request,
        product_store=product_store.public_descriptor,
        items=items,
        discovered_records=len(records),
    )


def iter_stage1_plan_batches(
    *,
    request: ArchiveRequest,
    source: ArchiveSourceDescriptor,
    metadata_provider: MetadataProvider,
    product_store: ProductStore,
    batch_size: int | str | None = None,
) -> Iterator[Stage1PlanBatch]:
    """Yield deterministic, bounded metadata-only plan batches."""
    if request.source_id != source.source_id:
        raise ValueError(
            f"ArchiveRequest source_id {request.source_id!r} does not match "
            f"source descriptor {source.source_id!r}"
        )
    size = normalize_stage1_batch_size(batch_size)
    if request.native_observation_ids and source.adapter != "master-manifest":
        raise ValueError("exact native observation selection requires master-manifest")
    batch: list[CalibrationPlanItem] = []
    discovered = 0
    for record in metadata_provider.search(request):
        discovered += 1
        if record.source_id != source.source_id:
            raise ValueError(
                f"MetadataProvider returned source_id {record.source_id!r} for "
                f"source {source.source_id!r}"
            )
        if record.frame_type == "science":
            batch.append(_plan_science_record(record, product_store=product_store))
        if discovered % size == 0:
            yield Stage1PlanBatch(items=tuple(batch), discovered_records=discovered)
            batch.clear()
    if discovered % size:
        yield Stage1PlanBatch(items=tuple(batch), discovered_records=discovered)


def _plan_science_record(
    record: ObservationRecord,
    *,
    product_store: ProductStore,
) -> CalibrationPlanItem:
    issues: list[str] = []
    preflight_blockers = record.metadata.get("stage1_blockers", ())
    if isinstance(preflight_blockers, (list, tuple)):
        for blocker in preflight_blockers:
            if isinstance(blocker, Mapping):
                code = str(blocker.get("code") or "SOURCE_PREFLIGHT")
                message = str(blocker.get("message") or code)
                issues.append(f"Source preflight blocker [{code}]: {message}")
            elif blocker not in (None, ""):
                issues.append(f"Source preflight blocker: {blocker}")
    if record.identity_status != "READY":
        issues.append(
            "Stable source identity is unavailable; path/raw-id fallback is forbidden"
            + (f": {record.identity_detail}" if record.identity_detail else "")
        )

    available_roles = {asset.role for asset in record.assets}
    missing_roles = tuple(role for role in record.required_asset_roles if role not in available_roles)
    if missing_roles:
        issues.append("Missing required asset refs: " + ", ".join(missing_roles))

    if record.input_state == "processed":
        action = "IMPORT_PROCESSED"
        calibration = CalibrationGroup(
            mode="SOURCE_PROCESSED",
            assets=(),
            criteria={},
            explanation=(
                "Source declares an already processed science FITS; no calibration "
                "selection is performed in Stage-1 planning."
            ),
        )
    else:
        action = "REDUCE_RAW"
        calibration_assets = record.assets_for_roles(CALIBRATION_ROLES)
        calibration = CalibrationGroup(
            mode="DIRECT_REFERENCES" if calibration_assets else "UNSPECIFIED",
            assets=calibration_assets,
            criteria={
                "instrument": record.instrument,
                "camera": record.camera,
                "filter": record.filter_name,
                "exptime": record.exptime,
                "binning": [record.binning_x, record.binning_y],
            },
            explanation=(
                "Calibration assets are authoritative source references supplied by the adapter; "
                "the foundation planner does not replace them by filename/date heuristics."
                if calibration_assets
                else "No calibration references were supplied by the source adapter."
            ),
        )

    product = None
    if record.identity_status == "READY":
        product = product_store.plan_product(record)

    return CalibrationPlanItem(
        observation=record,
        action=action,
        status="READY" if not issues else "BLOCKED",
        product=product,
        calibration=calibration,
        missing_asset_roles=missing_roles,
        issues=tuple(issues),
    )


def write_stage1_plan_json(plan: CalibrationPlan, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan.to_json(indent=2) + "\n", encoding="utf-8")
    return target


def write_stage1_plan_csv(plan: CalibrationPlan, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = _stage1_plan_csv_columns()
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in plan.items:
            writer.writerow(_stage1_plan_csv_row(item))
    return target


def write_stage1_plan_batches(
    batches: Iterator[Stage1PlanBatch],
    *,
    source: ArchiveSourceDescriptor,
    request: ArchiveRequest,
    product_store: Mapping[str, Any],
    json_path: str | Path,
    csv_path: str | Path,
    on_item: Callable[[CalibrationPlanItem], None] | None = None,
) -> tuple[Stage1PlanStreamSummary, Path, Path]:
    """Write a complete plan from bounded batches without retaining plan items."""
    json_target = Path(json_path)
    csv_target = Path(csv_path)
    json_destination = json_target.resolve(strict=False)
    csv_destination = csv_target.resolve(strict=False)
    if _stage1_output_paths_alias(json_destination, csv_destination):
        raise ValueError(
            "stage1-plan JSON and CSV output paths must be different; "
            f"both paths refer to the same file: {json_target}"
        )

    json_destination.parent.mkdir(parents=True, exist_ok=True)
    csv_destination.parent.mkdir(parents=True, exist_ok=True)

    discovered_records = 0
    science_items = 0
    ready_items = 0
    blocked_items = 0
    csv_columns = _stage1_plan_csv_columns()
    json_temp: Path | None = None
    csv_temp: Path | None = None
    json_handle: TextIO | None = None
    csv_handle: TextIO | None = None
    try:
        json_temp, json_handle = _open_stage1_output(json_destination)
        csv_temp, csv_handle = _open_stage1_output(csv_destination)
        csv_writer = csv.DictWriter(csv_handle, fieldnames=csv_columns)
        csv_writer.writeheader()

        generated_at_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        metadata_prefix = json.dumps(
            {
                "schema": STAGE1_PLAN_SCHEMA,
                "generated_at_utc": generated_at_utc,
                "source": source.to_dict(),
                "request": request.to_dict(),
                "product_store": dict(product_store),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        json_handle.write(metadata_prefix[:-2])
        json_handle.write(',\n  "items": [')

        for batch in batches:
            if len(batch.items) > MAX_STAGE1_BATCH_SIZE:
                raise ValueError(
                    "Stage1PlanBatch contains "
                    f"{len(batch.items)} items; maximum is {MAX_STAGE1_BATCH_SIZE}"
                )
            discovered_records = batch.discovered_records
            for item in batch.items:
                science_items += 1
                if item.status == "READY":
                    ready_items += 1
                else:
                    blocked_items += 1

                json_handle.write(",\n" if science_items > 1 else "\n")
                serialized_item = json.dumps(
                    item.to_dict(), ensure_ascii=False, indent=2, sort_keys=False
                )
                json_handle.write("    " + serialized_item.replace("\n", "\n    "))
                csv_writer.writerow(_stage1_plan_csv_row(item))
                if on_item is not None:
                    on_item(item)

        summary = Stage1PlanStreamSummary(
            discovered_records=discovered_records,
            science_items=science_items,
            ready_items=ready_items,
            blocked_items=blocked_items,
        )
        if science_items:
            json_handle.write("\n  ]")
        else:
            json_handle.write("]")
        json_handle.write(',\n  "status": ')
        json.dump(summary.status, json_handle, ensure_ascii=False)
        json_handle.write(',\n  "summary": ')
        summary_json = json.dumps(
            {
                "discovered_records": summary.discovered_records,
                "science_items": summary.science_items,
                "ready_items": summary.ready_items,
                "blocked_items": summary.blocked_items,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        json_handle.write("  " + summary_json.replace("\n", "\n  "))
        json_handle.write("\n}\n")

        json_handle.close()
        json_handle = None
        csv_handle.close()
        csv_handle = None

        os.replace(json_temp, json_destination)
        json_temp = None
        os.replace(csv_temp, csv_destination)
        csv_temp = None

        return summary, json_target, csv_target
    except BaseException as failure:
        for handle in (json_handle, csv_handle):
            if handle is not None and not handle.closed:
                try:
                    handle.close()
                except OSError as cleanup_error:
                    failure.add_note(f"could not close Stage-1 plan staging file: {cleanup_error}")
        for temp_path in (json_temp, csv_temp):
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    failure.add_note(f"could not remove Stage-1 plan staging file: {cleanup_error}")
        raise


def _stage1_output_paths_alias(first: Path, second: Path) -> bool:
    try:
        if os.path.samefile(first, second):
            return True
    except OSError:
        pass
    first_identity = os.path.normcase(os.path.realpath(os.path.abspath(first)))
    second_identity = os.path.normcase(os.path.realpath(os.path.abspath(second)))
    return first_identity == second_identity


def _open_stage1_output(target: Path) -> tuple[Path, TextIO]:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(name)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
    except BaseException as failure:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            failure.add_note(f"could not remove Stage-1 plan staging file: {cleanup_error}")
        raise
    return temp_path, handle


def _stage1_plan_csv_columns() -> list[str]:
    return [
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
    ]


def _stage1_plan_csv_row(item: CalibrationPlanItem) -> dict[str, Any]:
    obs = item.observation
    return {
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


__all__ = [
    "DEFAULT_STAGE1_BATCH_SIZE",
    "MAX_STAGE1_BATCH_SIZE",
    "Stage1PlanBatch",
    "Stage1PlanStreamSummary",
    "build_stage1_plan",
    "iter_stage1_plan_batches",
    "normalize_stage1_batch_size",
    "write_stage1_plan_batches",
    "write_stage1_plan_csv",
    "write_stage1_plan_json",
]
