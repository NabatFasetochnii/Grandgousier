from __future__ import annotations

import csv
from pathlib import Path
from collections.abc import Iterator
from dataclasses import dataclass

from grandgousier.archive.stage1_contract import (
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


@dataclass(frozen=True, slots=True)
class Stage1PlanBatch:
    items: tuple[CalibrationPlanItem, ...]
    discovered_records: int


def normalize_stage1_batch_size(value: int | str | None) -> int:
    batch_size = DEFAULT_STAGE1_BATCH_SIZE if value is None else int(value)
    if batch_size <= 0:
        raise ValueError("stage1_batch_size must be a positive integer")
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
    if discovered % size and batch:
        yield Stage1PlanBatch(items=tuple(batch), discovered_records=discovered)


def _plan_science_record(
    record: ObservationRecord,
    *,
    product_store: ProductStore,
) -> CalibrationPlanItem:
    issues: list[str] = []
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
    columns = [
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
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for item in plan.items:
            obs = item.observation
            writer.writerow(
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
    return target


__all__ = [
    "DEFAULT_STAGE1_BATCH_SIZE",
    "Stage1PlanBatch",
    "build_stage1_plan",
    "iter_stage1_plan_batches",
    "normalize_stage1_batch_size",
    "write_stage1_plan_csv",
    "write_stage1_plan_json",
]
