from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    ObservationRecord,
    ProductRef,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_execution import (
    Stage1ExecutionItemResult,
    execute_stage1_stream,
)
from grandgousier.archive.stage1_planner import (
    DEFAULT_STAGE1_BATCH_SIZE,
    MAX_STAGE1_BATCH_SIZE,
    iter_stage1_plan_batches,
    normalize_stage1_batch_size,
)


class _Store:
    public_descriptor = {"store_id": "stream-test", "backend": "filesystem"}

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef("stream-test", "filesystem", f"{observation.native_observation_id}.fits")


class _SinglePassProvider:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def search(self, request):
        del request
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("streaming execution must not rewind the source")
        yield from self.records


def _records(count: int):
    for index in range(count):
        native = f"native-{index:06d}"
        ref = AssetRef("stream", "processed", "filesystem", f"frame-{index}.fits")
        yield ObservationRecord(
            source_id="stream",
            instrument="MASTER",
            frame_type="science",
            input_state="processed",
            identity_status="READY",
            native_observation_id=native,
            source_observation_uid=make_source_observation_uid("stream", native),
            identity_scheme="stream_identity_v1",
            p4_native_identity=native,
            observation_uid=f"observation-{index}",
            required_asset_roles=("processed",),
            assets=(ref,),
        )


def _source() -> ArchiveSourceDescriptor:
    return ArchiveSourceDescriptor(
        source_id="stream",
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="synthetic",
    )


def test_plan_batches_are_bounded_and_single_pass() -> None:
    provider = _SinglePassProvider(_records(10_000))
    batches = iter_stage1_plan_batches(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=provider,
        product_store=_Store(),
        batch_size=64,
    )

    maximum = 0
    count = 0
    for batch in batches:
        maximum = max(maximum, len(batch.items))
        count += len(batch.items)

    assert maximum <= 64
    assert count == 10_000
    assert provider.calls == 1


def test_stream_execution_starts_before_source_is_exhausted_and_writes_bounded_report(
    monkeypatch, tmp_path: Path
) -> None:
    total = 10_000
    consumed = 0
    execution_started_at = None

    def records():
        nonlocal consumed
        for record in _records(total):
            consumed += 1
            yield record

    provider = _SinglePassProvider(records())

    def fake_execute(item, **kwargs):
        del kwargs
        nonlocal execution_started_at
        if execution_started_at is None:
            execution_started_at = consumed
        return Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=item.observation.native_observation_id,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status="SKIPPED_EXISTING",
            product=item.product,
        )

    monkeypatch.setattr("grandgousier.archive.stage1_execution._execute_item", fake_execute)
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=provider,
        asset_provider=object(),
        product_store=_Store(),
        batch_size=64,
        plan_json_path=tmp_path / "plan.json",
        report_json_path=tmp_path / "report.json",
    )

    assert execution_started_at == 64
    assert execution_started_at < total
    assert summary.planned_items == total
    assert summary.counts["SKIPPED_EXISTING"] == total
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["summary"]["items"] == total
    assert len(report["items"]) == total


def test_stream_execution_at_max_batch_preserves_order_status_counts_and_csv_schema(
    monkeypatch, tmp_path: Path
) -> None:
    from grandgousier.archive import stage1_planner

    original_iterator = stage1_planner.iter_stage1_plan_batches
    batch_lengths: list[int] = []

    def observe_batches(**kwargs):
        for batch in original_iterator(**kwargs):
            batch_lengths.append(len(batch.items))
            yield batch

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", observe_batches)
    monkeypatch.setattr(
        "grandgousier.archive.stage1_execution._execute_item",
        lambda item, **kwargs: Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=item.observation.native_observation_id,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status="SKIPPED_EXISTING",
            product=item.product,
        ),
    )

    total = MAX_STAGE1_BATCH_SIZE + 1
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(_records(total)),
        asset_provider=object(),
        product_store=_Store(),
        batch_size=MAX_STAGE1_BATCH_SIZE,
        plan_json_path=tmp_path / "max-plan.json",
        report_json_path=tmp_path / "max-report.json",
    )

    expected_ids = [f"native-{index:06d}" for index in range(total)]
    plan = json.loads((tmp_path / "max-plan.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "max-report.json").read_text(encoding="utf-8"))
    with (tmp_path / "max-plan.csv").open(encoding="utf-8", newline="") as handle:
        plan_rows = list(csv.DictReader(handle))
    with (tmp_path / "max-report.csv").open(encoding="utf-8", newline="") as handle:
        report_rows = list(csv.DictReader(handle))

    assert batch_lengths == [MAX_STAGE1_BATCH_SIZE, 1]
    assert summary.status == summary.plan_status == "READY"
    assert summary.discovered_records == summary.planned_items == total
    assert summary.counts == {"SKIPPED_EXISTING": total}
    assert [item["observation"]["native_observation_id"] for item in plan["items"]] == expected_ids
    assert [item["native_observation_id"] for item in report["items"]] == expected_ids
    assert plan["summary"] == {
        "discovered_records": total,
        "science_items": total,
        "ready_items": total,
        "blocked_items": 0,
    }
    assert report["summary"]["items"] == total
    assert report["summary"]["skipped_existing"] == total
    assert [row["native_observation_id"] for row in plan_rows] == expected_ids
    assert [row["native_observation_id"] for row in report_rows] == expected_ids
    assert list(plan_rows[0]) == [
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
    assert list(report_rows[0]) == [
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


def test_stream_execution_normalizes_default_before_iterating(monkeypatch, tmp_path: Path) -> None:
    from grandgousier.archive import stage1_planner

    original_iterator = stage1_planner.iter_stage1_plan_batches
    received_batch_sizes: list[int] = []

    def observe_batches(**kwargs):
        received_batch_sizes.append(kwargs["batch_size"])
        yield from original_iterator(**kwargs)

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", observe_batches)
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(iter(())),
        asset_provider=object(),
        product_store=_Store(),
        plan_json_path=tmp_path / "default-plan.json",
        report_json_path=tmp_path / "default-report.json",
    )

    assert received_batch_sizes == [DEFAULT_STAGE1_BATCH_SIZE]
    assert summary.plan_status == "EMPTY"


def test_stream_empty_and_partial_batches(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "grandgousier.archive.stage1_execution._execute_item",
        lambda item, **kwargs: Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=item.observation.native_observation_id,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status="SKIPPED_EXISTING",
            product=item.product,
        ),
    )
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(_records(3)),
        asset_provider=object(),
        product_store=_Store(),
        batch_size=2,
        plan_json_path=tmp_path / "plan.json",
        report_json_path=tmp_path / "report.json",
    )
    assert summary.planned_items == 3
    assert summary.status == "READY"

    empty = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(iter(())),
        asset_provider=object(),
        product_store=_Store(),
        batch_size=2,
        plan_json_path=tmp_path / "empty-plan.json",
        report_json_path=tmp_path / "empty-report.json",
    )
    assert empty.plan_status == "EMPTY"
    assert empty.status == "READY"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_STAGE1_BATCH_SIZE),
        (1, 1),
        (MAX_STAGE1_BATCH_SIZE, MAX_STAGE1_BATCH_SIZE),
        (str(MAX_STAGE1_BATCH_SIZE), MAX_STAGE1_BATCH_SIZE),
    ],
)
def test_batch_size_normalization_accepts_default_and_inclusive_bounds(value, expected) -> None:
    assert normalize_stage1_batch_size(value) == expected


@pytest.mark.parametrize("value", [0, -1, MAX_STAGE1_BATCH_SIZE + 1])
def test_batch_size_normalization_rejects_out_of_range_values(value) -> None:
    with pytest.raises(ValueError, match="1 through 512"):
        normalize_stage1_batch_size(value)


def test_stream_execution_rejects_oversized_batch_before_search_or_staging(
    tmp_path: Path,
) -> None:
    provider = _SinglePassProvider(_records(1))
    plan_path = tmp_path / "nested" / "plan.json"
    report_path = tmp_path / "nested" / "report.json"

    with pytest.raises(ValueError, match="1 through 512"):
        execute_stage1_stream(
            request=ArchiveRequest(source_id="stream"),
            source=_source(),
            metadata_provider=provider,
            asset_provider=object(),
            product_store=_Store(),
            batch_size=MAX_STAGE1_BATCH_SIZE + 1,
            plan_json_path=plan_path,
            report_json_path=report_path,
        )

    assert provider.calls == 0
    assert not plan_path.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_batch_order_is_deterministic() -> None:
    first = [
        [item.observation.native_observation_id for item in batch.items]
        for batch in iter_stage1_plan_batches(
            request=ArchiveRequest(source_id="stream"),
            source=_source(),
            metadata_provider=_SinglePassProvider(_records(7)),
            product_store=_Store(),
            batch_size=3,
        )
    ]
    second = [
        [item.observation.native_observation_id for item in batch.items]
        for batch in iter_stage1_plan_batches(
            request=ArchiveRequest(source_id="stream"),
            source=_source(),
            metadata_provider=_SinglePassProvider(_records(7)),
            product_store=_Store(),
            batch_size=3,
        )
    ]
    assert first == second


def test_stream_failure_isolated_to_one_item(monkeypatch, tmp_path: Path) -> None:
    processed: list[str] = []

    def fake_execute(item, **kwargs):
        del kwargs
        native = str(item.observation.native_observation_id)
        processed.append(native)
        status = "FAILED" if native == "native-000003" else "SKIPPED_EXISTING"
        return Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=item.observation.native_observation_id,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status=status,
            product=item.product,
            error="synthetic failure" if status == "FAILED" else None,
        )

    monkeypatch.setattr("grandgousier.archive.stage1_execution._execute_item", fake_execute)
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(_records(8)),
        asset_provider=object(),
        product_store=_Store(),
        batch_size=3,
        plan_json_path=tmp_path / "plan.json",
        report_json_path=tmp_path / "report.json",
    )

    assert len(processed) == 8
    assert summary.failed_items == 1
    assert summary.counts["SKIPPED_EXISTING"] == 7


def test_opt_in_stops_on_blocked_batch_without_executing_later_items(
    monkeypatch, tmp_path: Path
) -> None:
    def records():
        for index, record in enumerate(_records(3)):
            if index == 1:
                record = replace(
                    record, required_asset_roles=("processed", "missing"),
                )
            yield record

    calls = []
    monkeypatch.setattr(
        "grandgousier.archive.stage1_execution._execute_item",
        lambda item, **kwargs: calls.append(item) or None,
    )
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="stream"),
        source=_source(),
        metadata_provider=_SinglePassProvider(records()),
        asset_provider=object(),
        product_store=_Store(),
        batch_size=3,
        plan_json_path=tmp_path / "plan.json",
        report_json_path=tmp_path / "report.json",
        stop_on_first_failure=True,
    )
    assert not calls
    assert summary.status == summary.plan_status == "BLOCKED"
    assert json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))["summary"] == {
        "discovered_records": 3,
        "science_items": 3,
        "ready_items": 2,
        "blocked_items": 1,
        "attempted_items": 0,
        "reported_items": 1,
        "stopped_early": True,
    }
