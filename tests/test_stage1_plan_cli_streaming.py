from __future__ import annotations

import csv
import errno
import gc
import json
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from grandgousier import cli, config
from grandgousier.archive import stage1_adapters, stage1_checkpoint, stage1_execution, stage1_planner
from grandgousier.archive.stage1_contract import (
    STAGE1_PLAN_SCHEMA,
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    CalibrationPlan,
    ObservationRecord,
    ProductRef,
    make_source_observation_uid,
)
from grandgousier.exit_codes import OK, PRECONDITION, USAGE


class _Store:
    public_descriptor: ClassVar[dict[str, str]] = {
        "store_id": "stream-test",
        "backend": "filesystem",
    }

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef(
            "stream-test", "filesystem", f"{observation.native_observation_id}.fits"
        )


class _Assets:
    def __init__(self) -> None:
        self.calls = 0

    def materialize(self, ref, destination=None):
        del ref, destination
        self.calls += 1
        raise AssertionError("stage1-plan must not materialize assets")


class _OnePassProvider:
    def __init__(self, records) -> None:
        self.records = records
        self.calls = 0
        self.request = None

    def search(self, request):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("stage1-plan must not rewind metadata")
        self.request = request
        yield from self.records


def _source(*, adapter: str = "synthetic") -> ArchiveSourceDescriptor:
    return ArchiveSourceDescriptor(
        source_id="stream",
        adapter=adapter,
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="synthetic",
    )


def _record(
    index: int,
    *,
    frame_type: str = "science",
    blocked: bool = False,
) -> ObservationRecord:
    native = f"native-{index:06d}"
    processed = AssetRef("stream", "processed", "filesystem", f"frame-{index}.fits")
    return ObservationRecord(
        source_id="stream",
        instrument="MASTER",
        frame_type=frame_type,
        input_state="processed",
        identity_status="READY",
        native_observation_id=native,
        source_observation_uid=make_source_observation_uid("stream", native),
        identity_scheme="stream_identity_v1",
        p4_native_identity=native,
        observation_uid=f"observation-{index}",
        filter_name="V",
        observed_at=f"2026-09-30T00:{index % 60:02d}:00Z",
        required_asset_roles=("processed", "processing_header") if blocked else ("processed",),
        assets=(processed,),
        metadata={"raw_id": str(index)},
    )


def _record_without_native_id(*, raw_id: str = "legacy-raw-375506") -> ObservationRecord:
    processed = AssetRef("stream", "processed", "filesystem", f"{raw_id}.fits")
    return ObservationRecord(
        source_id="stream",
        instrument="MASTER",
        frame_type="science",
        input_state="processed",
        identity_status="MISSING",
        identity_detail="missing UNIQNUM",
        required_asset_roles=("processed",),
        assets=(processed,),
        metadata={"raw_id": raw_id},
    )


def _cli_args(
    tmp_path: Path,
    *,
    output_format: str = "table",
    native_ids: tuple[str, ...] = (),
):
    return SimpleNamespace(
        config_file="synthetic-config",
        source="stream",
        stage1_filters=[],
        datetime_start=None,
        datetime_end=None,
        stage1_object_name=None,
        ra=None,
        dec=None,
        radius_deg=None,
        limit=None,
        native_observation_id=list(native_ids),
        out=str(tmp_path / "plan.json"),
        csv_out=None,
        format=output_format,
    )


_UNSET = object()


def _patch_cli_runtime(
    monkeypatch,
    provider,
    *,
    adapter: str = "synthetic",
    batch_size: int | None = 64,
    source_batch_size=_UNSET,
    top_level_batch_size=_UNSET,
):
    assets = _Assets()
    store = _Store()
    if source_batch_size is _UNSET:
        source_batch_size = batch_size
    if top_level_batch_size is _UNSET:
        top_level_batch_size = batch_size
    source_config = {}
    if source_batch_size is not None:
        source_config["stage1_batch_size"] = source_batch_size
    payload = {"archives": {"stream": source_config}}
    if top_level_batch_size is not None:
        payload["stage1_batch_size"] = top_level_batch_size
    runtime = SimpleNamespace(
        descriptor=_source(adapter=adapter),
        metadata_provider=provider,
        asset_provider=assets,
    )
    monkeypatch.setattr(config, "read_config_file", lambda path: payload)
    monkeypatch.setattr(
        stage1_adapters,
        "build_archive_source_runtime",
        lambda value, source_id: runtime,
    )
    monkeypatch.setattr(stage1_adapters, "build_product_store", lambda value: store)
    return assets, store


def _records(count: int):
    for index in range(count):
        yield _record(index)


@pytest.mark.parametrize(
    ("source_size", "top_level_size", "expected"),
    [(23, 7, 23), (None, 19, 19), (None, None, 128)],
)
def test_stage1_plan_cli_uses_source_then_top_level_then_default_batch_size(
    monkeypatch,
    tmp_path: Path,
    capsys,
    source_size: int | None,
    top_level_size: int | None,
    expected: int,
) -> None:
    provider = _OnePassProvider(_records(2))
    _patch_cli_runtime(
        monkeypatch,
        provider,
        batch_size=None,
        source_batch_size=source_size,
        top_level_batch_size=top_level_size,
    )
    original_iterator = stage1_planner.iter_stage1_plan_batches
    observed_sizes: list[int] = []

    def observe_batches(**kwargs):
        observed_sizes.append(kwargs["batch_size"])
        yield from original_iterator(**kwargs)

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", observe_batches)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == OK
    assert "status=READY" in capsys.readouterr().out
    assert observed_sizes == [expected]
    assert provider.calls == 1


def test_stage1_plan_cli_rejects_over_cap_config_with_source_guidance(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(_records(1))
    _patch_cli_runtime(
        monkeypatch,
        provider,
        batch_size=None,
        source_batch_size=513,
        top_level_batch_size=32,
    )

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == USAGE
    output = capsys.readouterr()
    assert "1 through 512" in output.err
    assert "archives.stream.stage1_batch_size" in output.err
    assert "top-level stage1_batch_size" in output.err
    assert provider.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_stage1_plan_cli_at_max_batch_preserves_output_order_status_counts_and_csv_schema(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(_records(514))
    _patch_cli_runtime(
        monkeypatch,
        provider,
        batch_size=None,
        source_batch_size=stage1_planner.MAX_STAGE1_BATCH_SIZE,
        top_level_batch_size=1,
    )
    original_iterator = stage1_planner.iter_stage1_plan_batches
    batch_sizes: list[int] = []

    def observe_batches(**kwargs):
        for batch in original_iterator(**kwargs):
            batch_sizes.append(len(batch.items))
            yield batch

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", observe_batches)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == OK
    output = capsys.readouterr().out
    assert "status=READY, source=stream, discovered=514, science=514, ready=514, blocked=0" in output
    assert batch_sizes == [512, 2]

    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    with (tmp_path / "plan.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames
    expected_ids = [f"native-{index:06d}" for index in range(514)]
    assert plan["status"] == "READY"
    assert plan["summary"] == {
        "discovered_records": 514,
        "science_items": 514,
        "ready_items": 514,
        "blocked_items": 0,
    }
    assert [item["observation"]["native_observation_id"] for item in plan["items"]] == expected_ids
    assert [row["native_observation_id"] for row in rows] == expected_ids
    assert columns == [
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


@pytest.mark.parametrize(
    ("cli_size", "source_size", "top_level_size", "expected"),
    [
        (7, 5, 3, 7),
        (None, 5, 3, 5),
        (None, None, 3, 3),
        (None, None, None, 128),
    ],
)
def test_stage1_execute_cli_preserves_batch_size_precedence_and_fingerprint(
    monkeypatch,
    tmp_path: Path,
    capsys,
    cli_size: int | None,
    source_size: int | None,
    top_level_size: int | None,
    expected: int,
) -> None:
    provider = _OnePassProvider(iter(()))
    _patch_cli_runtime(
        monkeypatch,
        provider,
        batch_size=None,
        source_batch_size=source_size,
        top_level_batch_size=top_level_size,
    )
    fingerprint_arguments: list[dict] = []
    execution_arguments: list[dict] = []
    checkpoint_closed: list[bool] = []

    def fake_fingerprint(**kwargs):
        fingerprint_arguments.append(kwargs)
        return "synthetic-run-fingerprint"

    class _Checkpoint:
        @classmethod
        def open(cls, *args, **kwargs):
            del cls, args, kwargs
            return _Checkpoint()

        def close(self):
            checkpoint_closed.append(True)

    def fake_execute(**kwargs):
        execution_arguments.append(kwargs)
        return SimpleNamespace(
            status="READY",
            to_dict=lambda: {
                "summary": {
                    "items": 0,
                    "successful_items": 0,
                    "blocked_items": 0,
                    "failed_items": 0,
                    "reduced": 0,
                    "imported": 0,
                    "skipped_existing": 0,
                    "adopted_existing": 0,
                }
            },
        )

    monkeypatch.setattr(stage1_checkpoint, "Stage1Checkpoint", _Checkpoint)
    monkeypatch.setattr(stage1_checkpoint, "execution_fingerprint", fake_fingerprint)
    monkeypatch.setattr(stage1_execution, "execute_stage1_stream", fake_execute)

    command = ["stage1-execute", "--config", "synthetic-config", "--source", "stream"]
    if cli_size is not None:
        command.extend(("--batch-size", str(cli_size)))
    args = cli.build_parser().parse_args(command)

    assert cli.cmd_stage1_execute(args) == OK
    assert f"batch_size={expected}" in capsys.readouterr().out
    assert fingerprint_arguments[0]["batch_size"] == expected
    assert execution_arguments[0]["batch_size"] == expected
    assert execution_arguments[0]["run_fingerprint"] == "synthetic-run-fingerprint"
    assert checkpoint_closed == [True]
    assert provider.calls == 0


def test_stage1_execute_cli_rejects_oversized_override_before_checkpoint_or_execution(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(iter(()))
    _patch_cli_runtime(
        monkeypatch,
        provider,
        batch_size=None,
        source_batch_size=5,
        top_level_batch_size=3,
    )

    class _UnexpectedCheckpoint:
        @classmethod
        def open(cls, *args, **kwargs):
            del cls, args, kwargs
            raise AssertionError("invalid batch size must be rejected before checkpoint access")

    def unexpected_execute(**kwargs):
        del kwargs
        raise AssertionError("invalid batch size must be rejected before stream execution")

    monkeypatch.setattr(stage1_checkpoint, "Stage1Checkpoint", _UnexpectedCheckpoint)
    monkeypatch.setattr(stage1_execution, "execute_stage1_stream", unexpected_execute)
    args = cli.build_parser().parse_args(
        [
            "stage1-execute",
            "--config",
            "synthetic-config",
            "--source",
            "stream",
            "--batch-size",
            "513",
            "--plan-out",
            str(tmp_path / "plan.json"),
            "--report-out",
            str(tmp_path / "report.json"),
        ]
    )

    assert cli.cmd_stage1_execute(args) == USAGE
    assert "1 through 512" in capsys.readouterr().err
    assert provider.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_stage1_batch_size_help_describes_limits_and_config_precedence(capsys) -> None:
    parser = cli.build_parser()
    for command, expected_phrases in (
        ("stage1-plan", ("archives.<source>", "top level", "1..512", "default is 128")),
        (
            "stage1-execute",
            ("1..512", "overrides", "archives.<source>.stage1_batch_size", "default 128"),
        ),
    ):
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args([command, "--help"])
        assert exit_info.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        for phrase in expected_phrases:
            assert phrase in help_text


def test_stage1_plan_cli_streams_10000_one_pass_records_with_bounded_live_items(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(_records(10_000))
    assets, _store = _patch_cli_runtime(monkeypatch, provider, batch_size=64)
    batch_sizes: list[int] = []
    live_items = 0
    peak_live_items = 0
    original_iterator = stage1_planner.iter_stage1_plan_batches
    original_plan_science_record = stage1_planner._plan_science_record

    class _TrackedItem:
        __slots__ = ("__weakref__", "inner")

        def __init__(self, inner) -> None:
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def to_dict(self):
            return self.inner.to_dict()

    def release_item() -> None:
        nonlocal live_items
        live_items -= 1

    def track_item(record, *, product_store):
        nonlocal live_items, peak_live_items
        tracked = _TrackedItem(original_plan_science_record(record, product_store=product_store))
        live_items += 1
        peak_live_items = max(peak_live_items, live_items)
        weakref.finalize(tracked, release_item)
        return tracked

    def observe_batches(**kwargs):
        for batch in original_iterator(**kwargs):
            batch_sizes.append(len(batch.items))
            yield batch

    def forbidden_full_plan(*args, **kwargs):
        del args, kwargs
        raise AssertionError("stage1-plan CLI must not build or serialize a full plan")

    def forbidden_full_output_spool(*args, **kwargs):
        del args, kwargs
        raise AssertionError("stage1-plan must not spool complete outputs")

    monkeypatch.setattr(stage1_planner, "iter_stage1_plan_batches", observe_batches)
    monkeypatch.setattr(stage1_planner, "_plan_science_record", track_item)
    monkeypatch.setattr(stage1_planner, "build_stage1_plan", forbidden_full_plan)
    monkeypatch.setattr(stage1_planner.tempfile, "TemporaryFile", forbidden_full_output_spool)
    monkeypatch.setattr(CalibrationPlan, "to_dict", forbidden_full_plan)
    monkeypatch.setattr(CalibrationPlan, "to_json", forbidden_full_plan)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == OK
    output = capsys.readouterr().out
    assert "status=READY, source=stream, discovered=10000, science=10000, ready=10000, blocked=0" in output
    assert provider.calls == 1
    assert assets.calls == 0
    assert max(batch_sizes) <= 64
    assert peak_live_items <= 128
    gc.collect()
    assert live_items == 0

    with (tmp_path / "plan.json").open("r", encoding="utf-8") as handle:
        plan_json = json.load(handle)
    assert plan_json["schema"] == STAGE1_PLAN_SCHEMA
    assert plan_json["summary"] == {
        "discovered_records": 10_000,
        "science_items": 10_000,
        "ready_items": 10_000,
        "blocked_items": 0,
    }
    assert len(plan_json["items"]) == 10_000
    assert plan_json["items"][0]["observation"]["native_observation_id"] == "native-000000"
    assert plan_json["items"][-1]["observation"]["native_observation_id"] == "native-009999"
    assert {path.name for path in tmp_path.iterdir()} == {"plan.json", "plan.csv"}


@pytest.mark.parametrize("case", ["READY", "BLOCKED", "EMPTY"])
def test_streaming_plan_json_and_csv_match_full_plan_contract(case: str, tmp_path: Path) -> None:
    if case == "READY":
        records = (_record(0), _record(1))
    elif case == "BLOCKED":
        records = (_record(0), _record(1, blocked=True))
    else:
        records = (_record(0, frame_type="dark"), _record(1, frame_type="bias"))

    request = ArchiveRequest(source_id="stream")
    source = _source()
    store = _Store()
    json_output = tmp_path / f"{case.lower()}.json"
    csv_output = tmp_path / f"{case.lower()}.csv"
    json_output.write_text("stale JSON", encoding="utf-8")
    csv_output.write_text("stale CSV", encoding="utf-8")
    expected = stage1_planner.build_stage1_plan(
        request=request,
        source=source,
        metadata_provider=_OnePassProvider(records),
        asset_provider=_Assets(),
        product_store=store,
    )
    summary, json_path, csv_path = stage1_planner.write_stage1_plan_batches(
        stage1_planner.iter_stage1_plan_batches(
            request=request,
            source=source,
            metadata_provider=_OnePassProvider(records),
            product_store=store,
            batch_size=3,
        ),
        source=source,
        request=request,
        product_store=store.public_descriptor,
        json_path=json_output,
        csv_path=csv_output,
    )

    with json_path.open("r", encoding="utf-8") as handle:
        actual = json.load(handle)
    expected_dict = expected.to_dict()
    actual_generated_at = actual.pop("generated_at_utc")
    expected_dict.pop("generated_at_utc")
    assert actual_generated_at.endswith("Z")
    assert actual == expected_dict
    assert summary.status == expected.status == case
    assert summary.discovered_records == len(records)
    assert summary.science_items == len(expected.items)
    assert summary.ready_items == expected.ready_items
    assert summary.blocked_items == expected.blocked_items

    reference_csv = stage1_planner.write_stage1_plan_csv(
        expected, tmp_path / f"{case.lower()}-reference.csv"
    )
    assert csv_path.read_bytes() == reference_csv.read_bytes()


def test_non_science_partial_tail_updates_stream_summary(tmp_path: Path) -> None:
    records = (_record(0), _record(1, frame_type="dark"), _record(2, frame_type="bias"))
    request = ArchiveRequest(source_id="stream")
    source = _source()
    store = _Store()
    batches = list(
        stage1_planner.iter_stage1_plan_batches(
            request=request,
            source=source,
            metadata_provider=_OnePassProvider(records),
            product_store=store,
            batch_size=2,
        )
    )
    assert batches[-1].items == ()
    assert batches[-1].discovered_records == 3

    summary, json_path, _csv_path = stage1_planner.write_stage1_plan_batches(
        stage1_planner.iter_stage1_plan_batches(
            request=request,
            source=source,
            metadata_provider=_OnePassProvider(records),
            product_store=store,
            batch_size=2,
        ),
        source=source,
        request=request,
        product_store=store.public_descriptor,
        json_path=tmp_path / "mixed-tail.json",
        csv_path=tmp_path / "mixed-tail.csv",
    )
    assert summary.discovered_records == 3
    assert summary.science_items == 1
    with json_path.open("r", encoding="utf-8") as handle:
        assert json.load(handle)["summary"] == {
            "discovered_records": 3,
            "science_items": 1,
            "ready_items": 1,
            "blocked_items": 0,
        }


def test_streaming_writer_rejects_manual_oversized_batch_before_publication(
    tmp_path: Path,
) -> None:
    source = _source()
    request = ArchiveRequest(source_id="stream")
    item = stage1_planner._plan_science_record(_record(0), product_store=_Store())
    json_path = tmp_path / "existing-plan.json"
    csv_path = tmp_path / "existing-plan.csv"
    json_path.write_text("previous JSON", encoding="utf-8")
    csv_path.write_text("previous CSV", encoding="utf-8")
    emitted = []
    oversized = stage1_planner.Stage1PlanBatch(
        items=(item,) * (stage1_planner.MAX_STAGE1_BATCH_SIZE + 1),
        discovered_records=stage1_planner.MAX_STAGE1_BATCH_SIZE + 1,
    )

    with pytest.raises(ValueError, match="maximum is 512"):
        stage1_planner.write_stage1_plan_batches(
            iter((oversized,)),
            source=source,
            request=request,
            product_store=_Store.public_descriptor,
            json_path=json_path,
            csv_path=csv_path,
            on_item=emitted.append,
        )

    assert emitted == []
    assert json_path.read_text(encoding="utf-8") == "previous JSON"
    assert csv_path.read_text(encoding="utf-8") == "previous CSV"
    assert {path.name for path in tmp_path.iterdir()} == {
        "existing-plan.json",
        "existing-plan.csv",
    }


def test_provider_failure_does_not_publish_partial_plan_or_leave_staging_files(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class _FailingProvider:
        calls = 0

        def search(self, request):
            del request
            self.calls += 1
            yield _record(0)
            yield _record(1)
            raise RuntimeError("synthetic metadata provider failure")

    provider = _FailingProvider()
    _patch_cli_runtime(monkeypatch, provider)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == USAGE
    captured = capsys.readouterr()
    assert "synthetic metadata provider failure" in captured.err
    assert provider.calls == 1
    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "plan.csv").exists()
    assert list(tmp_path.iterdir()) == []


def test_alias_json_and_csv_output_paths_fail_before_provider_or_filesystem_writes(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(_records(1))
    _patch_cli_runtime(monkeypatch, provider)
    args = _cli_args(tmp_path)
    args.csv_out = str(tmp_path / "." / "plan.json")

    assert cli.cmd_stage1_plan(args) == USAGE
    captured = capsys.readouterr()
    assert "JSON and CSV output paths must be different" in captured.err
    assert provider.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_streaming_writer_follows_output_symlink_and_detects_resolved_alias(
    tmp_path: Path,
) -> None:
    link = tmp_path / "requested-plan.json"
    referent = tmp_path / "plan-target.json"
    csv_path = tmp_path / "plan.csv"
    referent.write_text("previous output", encoding="utf-8")
    try:
        link.symlink_to(referent.name)
    except NotImplementedError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    except OSError as exc:
        unsupported_errnos = {
            errno.EACCES,
            errno.EPERM,
            errno.ENOSYS,
            errno.ENOTSUP,
            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        }
        if exc.errno in unsupported_errnos or getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        raise

    request = ArchiveRequest(source_id="stream")
    source = _source()
    store = _Store()
    provider = _OnePassProvider(_records(1))

    with pytest.raises(ValueError, match="output paths must be different"):
        stage1_planner.write_stage1_plan_batches(
            stage1_planner.iter_stage1_plan_batches(
                request=request,
                source=source,
                metadata_provider=provider,
                product_store=store,
                batch_size=1,
            ),
            source=source,
            request=request,
            product_store=store.public_descriptor,
            json_path=link,
            csv_path=referent,
        )
    assert provider.calls == 0
    assert referent.read_text(encoding="utf-8") == "previous output"
    assert link.is_symlink()

    summary, returned_json, returned_csv = stage1_planner.write_stage1_plan_batches(
        stage1_planner.iter_stage1_plan_batches(
            request=request,
            source=source,
            metadata_provider=provider,
            product_store=store,
            batch_size=1,
        ),
        source=source,
        request=request,
        product_store=store.public_descriptor,
        json_path=link,
        csv_path=csv_path,
    )

    assert summary.status == "READY"
    assert returned_json == link
    assert returned_csv == csv_path
    assert link.is_symlink()
    assert json.loads(referent.read_text(encoding="utf-8"))["schema"] == STAGE1_PLAN_SCHEMA
    assert json.loads(link.read_text(encoding="utf-8"))["summary"]["science_items"] == 1
    assert csv_path.is_file()
    assert {path.name for path in tmp_path.iterdir()} == {
        "requested-plan.json",
        "plan-target.json",
        "plan.csv",
    }


def test_cli_preserves_exact_native_selection_and_blocked_table_diagnostics(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    requested = ("native-000002", "native-000001", "native-000003")
    selected = {record.native_observation_id: record for record in (
        _record(1), _record(2, blocked=True), _record(3, blocked=True)
    )}

    class _ExactSelectionProvider(_OnePassProvider):
        def search(self, request):
            assert request.native_observation_ids == requested
            yield from super().search(request)

    provider = _ExactSelectionProvider(selected[native] for native in requested)
    assets, _store = _patch_cli_runtime(
        monkeypatch, provider, adapter="master-manifest", batch_size=2
    )

    assert cli.cmd_stage1_plan(
        _cli_args(tmp_path, native_ids=requested)
    ) == PRECONDITION
    captured = capsys.readouterr()
    assert (
        "status=BLOCKED, source=stream, discovered=3, science=3, ready=1, blocked=2"
        in captured.out
    )
    diagnostics = [line for line in captured.err.splitlines() if line.startswith("  BLOCKED")]
    assert diagnostics == [
        "  BLOCKED native-000002: Missing required asset refs: processing_header",
        "  BLOCKED native-000003: Missing required asset refs: processing_header",
    ]
    assert assets.calls == 0

    with (tmp_path / "plan.json").open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert [item["observation"]["native_observation_id"] for item in payload["items"]] == list(
        requested
    )
    with (tmp_path / "plan.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["native_observation_id"] for row in rows] == list(requested)


def test_table_blocked_diagnostic_falls_back_to_metadata_raw_id(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    provider = _OnePassProvider(iter((_record_without_native_id(),)))
    assets, _store = _patch_cli_runtime(monkeypatch, provider)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path)) == PRECONDITION
    captured = capsys.readouterr()
    diagnostics = [line for line in captured.err.splitlines() if line.startswith("  BLOCKED")]
    assert diagnostics == [
        (
            "  BLOCKED legacy-raw-375506: Stable source identity is unavailable; "
            "path/raw-id fallback is forbidden: missing UNIQNUM"
        )
    ]
    assert "  BLOCKED ?:" not in captured.err
    assert assets.calls == 0


def test_json_mode_does_not_emit_blocked_table_diagnostics(monkeypatch, tmp_path: Path, capsys) -> None:
    provider = _OnePassProvider(iter((_record_without_native_id(),)))
    assets, _store = _patch_cli_runtime(monkeypatch, provider)

    assert cli.cmd_stage1_plan(_cli_args(tmp_path, output_format="json")) == PRECONDITION
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == "BLOCKED"
    assert assets.calls == 0


def test_json_console_output_reads_completed_file_in_bounded_chunks(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    output_path = tmp_path / "plan.json"
    provider = _OnePassProvider(_records(300))
    assets, _store = _patch_cli_runtime(monkeypatch, provider, batch_size=32)
    original_open = Path.open
    read_sizes: list[int] = []

    def forbidden_full_plan(*args, **kwargs):
        del args, kwargs
        raise AssertionError("JSON console output must not build a full plan JSON string")

    monkeypatch.setattr(stage1_planner, "build_stage1_plan", forbidden_full_plan)
    monkeypatch.setattr(CalibrationPlan, "to_dict", forbidden_full_plan)
    monkeypatch.setattr(CalibrationPlan, "to_json", forbidden_full_plan)

    class _TrackingReader:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self.handle.__exit__(exc_type, exc, traceback)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def tracked_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if Path(path) == output_path and mode == "r":
            return _TrackingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)
    assert cli.cmd_stage1_plan(_cli_args(tmp_path, output_format="json")) == OK
    console = capsys.readouterr().out
    assert assets.calls == 0
    assert output_path.stat().st_size > 64 * 1024
    assert read_sizes
    assert set(read_sizes) == {64 * 1024}

    monkeypatch.setattr(Path, "open", original_open)
    with output_path.open("r", encoding="utf-8") as handle:
        file_content = handle.read()
    assert console == file_content
    assert json.loads(console)["summary"]["science_items"] == 300
