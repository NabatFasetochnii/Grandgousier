from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from grandgousier import __version__
from grandgousier.archive.master_reduction import MasterReductionResult
from grandgousier.archive.stage1_adapters import FilesystemProductStore
from grandgousier.archive.stage1_checkpoint import (
    CheckpointMismatchError,
    CheckpointStateError,
    Stage1Checkpoint,
    execution_fingerprint,
    item_fingerprint,
)
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    LocalAsset,
    ObservationRecord,
    ProductRef,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_execution import (
    STAGE1_PROVENANCE_SCHEMA,
    Stage1ExecutionItemResult,
    _checkpoint_product_is_durable,
    execute_stage1_plan,
    execute_stage1_stream,
)
from grandgousier.archive.stage1_planner import build_stage1_plan
from grandgousier.cli import build_parser


class _Store:
    public_descriptor = {"store_id": "checkpoint-test", "backend": "filesystem"}

    def __init__(self, root: Path | None = None) -> None:
        self.root = root

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef("checkpoint-test", "filesystem", f"{observation.native_observation_id}.fits")

    def resolve(self, product: ProductRef) -> Path:
        if self.root is None:
            raise ValueError("A product root is required to resolve checkpoint test products")
        return self.root / product.locator


class _Provider:
    def __init__(self, records):
        self.records = records

    def search(self, request):
        del request
        yield from self.records


class _AssetProvider:
    def __init__(self, mapping: dict[str, Path], *, default_path: Path | None = None) -> None:
        self.mapping = mapping
        self.default_path = default_path
        self.calls: list[str] = []

    def materialize(self, ref: AssetRef, destination=None) -> LocalAsset:
        del destination
        self.calls.append(ref.ref_uid)
        path = self.mapping.get(ref.ref_uid, self.default_path)
        if path is None:
            raise KeyError(ref.ref_uid)
        return LocalAsset(ref=ref, path=path)


def _source() -> ArchiveSourceDescriptor:
    return ArchiveSourceDescriptor(
        source_id="checkpoint",
        adapter="synthetic",
        instrument_profile="MASTER",
        metadata_backend="synthetic",
        asset_backend="synthetic",
    )


def _records(count: int):
    for index in range(count):
        native = f"native-{index:05d}"
        yield ObservationRecord(
            source_id="checkpoint",
            instrument="MASTER",
            frame_type="science",
            input_state="processed",
            identity_status="READY",
            native_observation_id=native,
            source_observation_uid=make_source_observation_uid("checkpoint", native),
            identity_scheme="checkpoint_identity_v1",
            p4_native_identity=native,
            observation_uid=f"obs-{index}",
            required_asset_roles=("processed",),
            assets=(AssetRef("checkpoint", "processed", "synthetic", f"{native}.fits"),),
        )


def _fingerprint(tmp_path: Path, *, batch_size: int = 64) -> str:
    return execution_fingerprint(
        source=_source().to_dict(),
        request=ArchiveRequest(source_id="checkpoint").to_dict(),
        product_store=_Store().public_descriptor,
        batch_size=batch_size,
        overwrite=False,
        max_dark_temperature_delta_c=5.0,
        software_version=__version__,
    )


def _raw_record() -> ObservationRecord:
    native = "native-raw"
    refs = tuple(
        AssetRef("checkpoint", role, "synthetic", f"{native}-{role}.fits")
        for role in ("science_raw", "processing_header", "dark", "flat")
    )
    return ObservationRecord(
        source_id="checkpoint",
        instrument="MASTER",
        frame_type="science",
        input_state="raw",
        identity_status="READY",
        native_observation_id=native,
        source_observation_uid=make_source_observation_uid("checkpoint", native),
        identity_scheme="checkpoint_identity_v1",
        p4_native_identity=native,
        observation_uid="obs-raw",
        required_asset_roles=tuple(ref.role for ref in refs),
        assets=refs,
        metadata={"raw_id": "42"},
    )


def _seed_completed_checkpoint(
    *,
    tmp_path: Path,
    record: ObservationRecord,
    store: _Store,
    assets: _AssetProvider,
) -> tuple[Path, str, str, Stage1ExecutionItemResult]:
    run_fingerprint = _fingerprint(tmp_path)
    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="checkpoint"),
        source=_source(),
        metadata_provider=_Provider((record,)),
        asset_provider=assets,
        product_store=store,
    )
    executed = execute_stage1_plan(
        plan,
        asset_provider=assets,
        product_store=store,
    )
    assert executed.status == "READY"
    result = executed.results[0]
    item_key = item_fingerprint(
        run_fingerprint=run_fingerprint,
        item=plan.items[0].to_dict(),
    )
    checkpoint_path = tmp_path / "run.sqlite3"
    with Stage1Checkpoint.open(
        checkpoint_path,
        source_id="checkpoint",
        config_fingerprint=run_fingerprint,
        resume=False,
        restart=False,
    ) as checkpoint:
        checkpoint.mark_completed(item_key=item_key, result=result.to_dict())
        checkpoint.set_terminal_status("INTERRUPTED")
    return checkpoint_path, run_fingerprint, item_key, result


def _resume_stream(
    *,
    tmp_path: Path,
    checkpoint_path: Path,
    run_fingerprint: str,
    record: ObservationRecord,
    assets: _AssetProvider,
    store: _Store,
    prefix: str,
) -> dict:
    with Stage1Checkpoint.open(
        checkpoint_path,
        source_id="checkpoint",
        config_fingerprint=run_fingerprint,
        resume=True,
        restart=False,
    ) as checkpoint:
        summary = execute_stage1_stream(
            request=ArchiveRequest(source_id="checkpoint"),
            source=_source(),
            metadata_provider=_Provider((record,)),
            asset_provider=assets,
            product_store=store,
            batch_size=64,
            plan_json_path=tmp_path / f"{prefix}-plan.json",
            report_json_path=tmp_path / f"{prefix}-report.json",
            checkpoint=checkpoint,
            run_fingerprint=run_fingerprint,
        )
    assert summary.status in {"READY", "FAILED"}
    return json.loads((tmp_path / f"{prefix}-report.json").read_text(encoding="utf-8"))


def test_checkpoint_requires_explicit_resume_and_rejects_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    with pytest.raises(CheckpointStateError, match="missing checkpoint"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint="a",
            resume=True,
            restart=False,
        )
    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint="a",
        resume=False,
        restart=False,
    ) as checkpoint:
        assert checkpoint.state().status == "RUNNING"

    with pytest.raises(CheckpointStateError, match="use --resume"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint="a",
            resume=False,
            restart=False,
        )
    with pytest.raises(CheckpointMismatchError, match="fingerprint"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint="b",
            resume=True,
            restart=False,
        )


def test_checkpoint_fingerprint_keeps_batch_size_and_mismatch_never_restarts(
    tmp_path: Path,
) -> None:
    old_batch_fingerprint = _fingerprint(tmp_path, batch_size=513)
    capped_batch_fingerprint = _fingerprint(tmp_path, batch_size=512)
    assert old_batch_fingerprint != capped_batch_fingerprint

    path = tmp_path / "batch-size-run.sqlite3"
    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint=old_batch_fingerprint,
        resume=False,
        restart=False,
    ) as checkpoint:
        original_run_id = checkpoint.state().run_id
        checkpoint.mark_completed(item_key="completed-item", result={"status": "IMPORTED"})
        checkpoint.set_terminal_status("INTERRUPTED")

    with pytest.raises(CheckpointMismatchError, match="fingerprint"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint=capped_batch_fingerprint,
            resume=True,
            restart=False,
        )

    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint=old_batch_fingerprint,
        resume=True,
        restart=False,
    ) as checkpoint:
        state = checkpoint.state()
        assert state.run_id == original_run_id
        assert state.completed == 1
        assert checkpoint.get_completed("completed-item") == {"status": "IMPORTED"}

    assert list(tmp_path.glob("batch-size-run.sqlite3.*.previous")) == []


def test_checkpoint_partial_file_recovery_is_checkpoint_specific(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint="a",
        resume=False,
        restart=False,
    ) as checkpoint:
        checkpoint.set_terminal_status("INTERRUPTED")
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(path.read_bytes())

    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint="a",
        resume=True,
        restart=False,
    ):
        pass
    assert not partial.exists()


def test_checkpoint_rejects_unrelated_sqlite_and_restart_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.close()
    with pytest.raises(CheckpointStateError, match="not a Grandgousier checkpoint"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint="a",
            resume=True,
            restart=False,
        )

    path.unlink()
    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint="a",
        resume=False,
        restart=False,
    ) as checkpoint:
        first_run_id = checkpoint.state().run_id
        checkpoint.mark_completed(item_key="item", result={"status": "IMPORTED"})
        checkpoint.mark_completed(item_key="item", result={"status": "IMPORTED"})
        assert checkpoint.state().completed == 1
        checkpoint.set_terminal_status("COMPLETE")
    with pytest.raises(CheckpointStateError, match="use --restart-run"):
        Stage1Checkpoint.open(
            path,
            source_id="checkpoint",
            config_fingerprint="a",
            resume=True,
            restart=False,
        )
    with Stage1Checkpoint.open(
        path,
        source_id="checkpoint",
        config_fingerprint="a",
        resume=False,
        restart=True,
    ) as checkpoint:
        assert checkpoint.state().run_id != first_run_id
        assert checkpoint.state().completed == 0
    assert list(tmp_path.glob("run.sqlite3.*.previous"))


def test_checkpoint_durable_validation_rejects_missing_or_corrupt_outputs(tmp_path: Path) -> None:
    product = tmp_path / "product.fits"
    provenance = tmp_path / "product.provenance.json"
    product.write_bytes(b"product")
    checksum = __import__("hashlib").sha256(product.read_bytes()).hexdigest()
    provenance.write_text(
        json.dumps(
            {
                "schema": STAGE1_PROVENANCE_SCHEMA,
                "fingerprint": "fp",
                "product": {"checksum_sha256": checksum},
            }
        ),
        encoding="utf-8",
    )
    result = {
        "status": "IMPORTED",
        "product_path": str(product),
        "provenance_path": str(provenance),
        "product_checksum_sha256": checksum,
        "fingerprint": "fp",
    }
    assert _checkpoint_product_is_durable(result)
    product.write_bytes(b"corrupt")
    assert not _checkpoint_product_is_durable(result)
    product.unlink()
    assert not _checkpoint_product_is_durable(result)


@pytest.mark.parametrize("locator", ["../outside.fits", "/outside.fits"])
def test_filesystem_product_store_rejects_non_relative_locators(
    locator: str, tmp_path: Path
) -> None:
    store = FilesystemProductStore(root=tmp_path / "products", store_id="checkpoint-test")
    ref = ProductRef("checkpoint-test", "filesystem", locator)

    with pytest.raises(ValueError, match="safe relative path"):
        store.resolve(ref)


def test_filesystem_product_store_resolves_normal_relative_locator(tmp_path: Path) -> None:
    root = tmp_path / "products"
    store = FilesystemProductStore(root=root, store_id="checkpoint-test")
    ref = ProductRef(
        "checkpoint-test",
        "filesystem",
        "source/MASTER/2026/09/30/observation_processed.fits",
    )

    expected = (
        root
        / "source"
        / "MASTER"
        / "2026"
        / "09"
        / "30"
        / "observation_processed.fits"
    )
    assert store.resolve(ref) == expected


def test_filesystem_product_store_rejects_absolute_drive_locator(
    tmp_path: Path,
) -> None:
    store = FilesystemProductStore(root=tmp_path / "products", store_id="checkpoint-test")
    ref = ProductRef("checkpoint-test", "filesystem", str(tmp_path / "outside.fits"))

    with pytest.raises(ValueError, match="safe relative path"):
        store.resolve(ref)


def test_filesystem_product_store_rejects_resolved_path_escape(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "products"
    root.mkdir()
    outside = tmp_path / "outside" / "product.fits"
    store = FilesystemProductStore(root=root, store_id="checkpoint-test")
    ref = ProductRef("checkpoint-test", "filesystem", "linked/product.fits")
    product_path = root / "linked" / "product.fits"
    original_resolve = Path.resolve

    def resolve_with_symlink_escape(path: Path, *, strict: bool = False) -> Path:
        if path == product_path:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_symlink_escape)

    with pytest.raises(ValueError, match="resolves outside"):
        store.resolve(ref)


def test_filesystem_product_store_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "products"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    store = FilesystemProductStore(root=root, store_id="checkpoint-test")
    ref = ProductRef("checkpoint-test", "filesystem", "linked/product.fits")

    with pytest.raises(ValueError, match="resolves outside"):
        store.resolve(ref)


@pytest.mark.parametrize("action", ["IMPORT_PROCESSED", "REDUCE_RAW"])
def test_checkpoint_fast_reuse_requires_same_paths_and_current_input_bytes(
    action: str, monkeypatch, tmp_path: Path
) -> None:
    from grandgousier.archive import stage1_execution

    if action == "REDUCE_RAW":
        record = _raw_record()

        def fake_reduce(task, **kwargs):
            del kwargs
            task.output_path.parent.mkdir(parents=True, exist_ok=True)
            task.output_path.write_bytes(b"synthetic-reduced-product")
            return MasterReductionResult(task=task, status="REDUCED")

        monkeypatch.setattr(stage1_execution, "reduce_master_task", fake_reduce)
    else:
        record = next(_records(1))

    input_paths: dict[str, Path] = {}
    for ref in record.assets:
        path = tmp_path / f"{ref.role}.fits"
        path.write_bytes(f"input bytes for {ref.role}".encode())
        input_paths[ref.ref_uid] = path
    assets = _AssetProvider(input_paths)
    store = _Store(tmp_path / "products")
    checkpoint_path, run_fingerprint, _, seeded_result = _seed_completed_checkpoint(
        tmp_path=tmp_path,
        record=record,
        store=store,
        assets=assets,
    )
    assets.calls.clear()

    def unexpected_execute(*args, **kwargs):
        del args, kwargs
        raise AssertionError("unchanged durable checkpoint result should use the fast path")

    monkeypatch.setattr(stage1_execution, "_execute_item", unexpected_execute)
    report = _resume_stream(
        tmp_path=tmp_path,
        checkpoint_path=checkpoint_path,
        run_fingerprint=run_fingerprint,
        record=record,
        assets=assets,
        store=store,
        prefix=f"reuse-{action.lower()}",
    )

    assert report["status"] == "READY"
    assert report["items"][0]["status"] == seeded_result.status
    assert assets.calls == [ref.ref_uid for ref in record.assets]


def test_changed_input_bytes_fall_through_without_overwriting_checkpoint_product(
    monkeypatch, tmp_path: Path
) -> None:
    from grandgousier.archive import stage1_execution

    record = next(_records(1))
    ref = record.assets[0]
    input_path = tmp_path / "processed-input.fits"
    input_path.write_bytes(b"processed-v1")
    assets = _AssetProvider({ref.ref_uid: input_path})
    store = _Store(tmp_path / "products")
    checkpoint_path, run_fingerprint, seeded_item_key, seeded_result = _seed_completed_checkpoint(
        tmp_path=tmp_path,
        record=record,
        store=store,
        assets=assets,
    )
    old_product = Path(str(seeded_result.product_path))
    old_bytes = old_product.read_bytes()
    input_path.write_bytes(b"processed-v2")
    assets.calls.clear()
    current_plan = build_stage1_plan(
        request=ArchiveRequest(source_id="checkpoint"),
        source=_source(),
        metadata_provider=_Provider((record,)),
        asset_provider=assets,
        product_store=store,
    )
    assert item_fingerprint(
        run_fingerprint=run_fingerprint,
        item=current_plan.items[0].to_dict(),
    ) == seeded_item_key

    original_execute = stage1_execution._execute_item
    observed: dict[str, object] = {}

    def observe_fallback(item, **kwargs):
        observed["overwrite"] = kwargs["overwrite"]
        observed["context"] = kwargs["context"]
        return original_execute(item, **kwargs)

    monkeypatch.setattr(stage1_execution, "_execute_item", observe_fallback)
    report = _resume_stream(
        tmp_path=tmp_path,
        checkpoint_path=checkpoint_path,
        run_fingerprint=run_fingerprint,
        record=record,
        assets=assets,
        store=store,
        prefix="changed-input",
    )

    assert report["status"] == "FAILED"
    assert report["items"][0]["status"] == "FAILED"
    assert observed["overwrite"] is False
    assert observed["context"] is not None
    assert assets.calls == [ref.ref_uid]
    assert old_product.read_bytes() == old_bytes


def test_checkpoint_prevalidation_materialization_failure_is_reported_once(
    tmp_path: Path,
) -> None:
    records = tuple(_records(2))
    first_ref = records[0].assets[0]
    input_path = tmp_path / "processed-input.fits"
    input_path.write_bytes(b"processed-source")
    seed_assets = _AssetProvider({first_ref.ref_uid: input_path})
    store = _Store(tmp_path / "products")
    checkpoint_path, run_fingerprint, _, seeded_result = _seed_completed_checkpoint(
        tmp_path=tmp_path,
        record=records[0],
        store=store,
        assets=seed_assets,
    )
    existing_product = Path(str(seeded_result.product_path))
    existing_sidecar = Path(str(seeded_result.provenance_path))
    original_product_bytes = existing_product.read_bytes()
    original_sidecar_bytes = existing_sidecar.read_bytes()

    class _FailingAssetProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def materialize(self, ref: AssetRef, destination=None) -> LocalAsset:
            del destination
            self.calls.append(ref.ref_uid)
            raise RuntimeError("synthetic materialization failure")

    failing_assets = _FailingAssetProvider()
    with Stage1Checkpoint.open(
        checkpoint_path,
        source_id="checkpoint",
        config_fingerprint=run_fingerprint,
        resume=True,
        restart=False,
    ) as checkpoint:
        summary = execute_stage1_stream(
            request=ArchiveRequest(source_id="checkpoint"),
            source=_source(),
            metadata_provider=_Provider(records),
            asset_provider=failing_assets,
            product_store=store,
            batch_size=64,
            plan_json_path=tmp_path / "materialization-failure-plan.json",
            report_json_path=tmp_path / "materialization-failure-report.json",
            checkpoint=checkpoint,
            run_fingerprint=run_fingerprint,
            stop_on_first_failure=True,
        )
        state = checkpoint.state()

    report = json.loads(
        (tmp_path / "materialization-failure-report.json").read_text(encoding="utf-8")
    )
    second_product = store.resolve(store.plan_product(records[1]))
    assert failing_assets.calls == [first_ref.ref_uid]
    assert summary.status == "FAILED"
    assert summary.stopped_early is True
    assert summary.attempted_items == 1
    assert report["summary"]["reported_items"] == 1
    assert report["items"][0]["status"] == "FAILED"
    assert report["items"][0]["error"] == "RuntimeError: synthetic materialization failure"
    assert Path(report["items"][0]["product_path"]) == existing_product
    assert Path(report["items"][0]["provenance_path"]) == existing_sidecar
    assert (state.status, state.attempted, state.completed, state.failed) == (
        "FAILED",
        1,
        1,
        1,
    )
    assert existing_product.read_bytes() == original_product_bytes
    assert existing_sidecar.read_bytes() == original_sidecar_bytes
    assert not second_product.exists()


def test_changed_product_store_root_does_not_reuse_old_root_checkpoint_result(
    tmp_path: Path,
) -> None:
    record = next(_records(1))
    ref = record.assets[0]
    input_path = tmp_path / "processed-input.fits"
    input_path.write_bytes(b"processed-source")
    assets = _AssetProvider({ref.ref_uid: input_path})
    original_store = _Store(tmp_path / "original-products")
    checkpoint_path, run_fingerprint, seeded_item_key, seeded_result = _seed_completed_checkpoint(
        tmp_path=tmp_path,
        record=record,
        store=original_store,
        assets=assets,
    )
    old_product = Path(str(seeded_result.product_path))
    old_bytes = old_product.read_bytes()
    assets.calls.clear()
    current_store = _Store(tmp_path / "different-products")
    current_plan = build_stage1_plan(
        request=ArchiveRequest(source_id="checkpoint"),
        source=_source(),
        metadata_provider=_Provider((record,)),
        asset_provider=assets,
        product_store=current_store,
    )
    assert item_fingerprint(
        run_fingerprint=run_fingerprint,
        item=current_plan.items[0].to_dict(),
    ) == seeded_item_key

    report = _resume_stream(
        tmp_path=tmp_path,
        checkpoint_path=checkpoint_path,
        run_fingerprint=run_fingerprint,
        record=record,
        assets=assets,
        store=current_store,
        prefix="changed-root",
    )

    current_product = current_store.resolve(current_store.plan_product(record))
    assert report["status"] == "READY"
    assert report["items"][0]["status"] == "IMPORTED"
    assert Path(report["items"][0]["product_path"]) == current_product
    assert current_product.read_bytes() == input_path.read_bytes()
    assert old_product.read_bytes() == old_bytes
    assert assets.calls == [ref.ref_uid]


@pytest.mark.parametrize("checksum_state", ["missing", "malformed"])
def test_checkpoint_input_checksum_must_be_present_and_well_formed(
    checksum_state: str, monkeypatch, tmp_path: Path
) -> None:
    from grandgousier.archive import stage1_execution

    record = next(_records(1))
    ref = record.assets[0]
    input_path = tmp_path / "processed-input.fits"
    input_path.write_bytes(b"processed-source")
    assets = _AssetProvider({ref.ref_uid: input_path})
    store = _Store(tmp_path / "products")
    checkpoint_path, run_fingerprint, _, seeded_result = _seed_completed_checkpoint(
        tmp_path=tmp_path,
        record=record,
        store=store,
        assets=assets,
    )
    sidecar_path = Path(str(seeded_result.provenance_path))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if checksum_state == "missing":
        sidecar["inputs"][0].pop("checksum_sha256")
    else:
        sidecar["inputs"][0]["checksum_sha256"] = "not-a-sha256"
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    assets.calls.clear()

    original_execute = stage1_execution._execute_item
    fallthrough_calls: list[bool] = []

    def observe_fallback(item, **kwargs):
        fallthrough_calls.append(bool(kwargs["overwrite"]))
        return original_execute(item, **kwargs)

    monkeypatch.setattr(stage1_execution, "_execute_item", observe_fallback)
    report = _resume_stream(
        tmp_path=tmp_path,
        checkpoint_path=checkpoint_path,
        run_fingerprint=run_fingerprint,
        record=record,
        assets=assets,
        store=store,
        prefix=f"bad-input-checksum-{checksum_state}",
    )

    assert fallthrough_calls == [False]
    assert report["status"] == "READY"
    assert report["items"][0]["status"] == "SKIPPED_EXISTING"
    assert report["items"][0]["status"] != seeded_result.status
    assert assets.calls == [ref.ref_uid]


def test_stage1_execute_resume_modes_are_mutually_exclusive() -> None:
    parser = build_parser()
    resumed = parser.parse_args(["stage1-execute", "--source", "master-smoke", "--resume"])
    restarted = parser.parse_args(["stage1-execute", "--source", "master-smoke", "--restart-run"])
    assert resumed.resume is True
    assert resumed.restart_run is False
    assert restarted.resume is False
    assert restarted.restart_run is True
    assert resumed.stop_on_first_failure is False
    assert parser.parse_args(
        ["stage1-execute", "--source", "master-smoke", "--stop-on-first-failure"]
    ).stop_on_first_failure is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["stage1-execute", "--source", "master-smoke", "--resume", "--restart-run"]
        )


@pytest.mark.parametrize("batch_size, planned", [(1, 2), (3, 3)])
def test_stop_on_first_failure_persists_partial_report_and_resumes(
    monkeypatch, tmp_path: Path, batch_size: int, planned: int
) -> None:
    checkpoint_path = tmp_path / "run.sqlite3"
    fingerprint = _fingerprint(tmp_path, batch_size=batch_size)
    input_path = tmp_path / "processed-input.fits"
    input_path.write_bytes(b"synthetic-processed-input")
    assets = _AssetProvider({}, default_path=input_path)
    store = _Store(tmp_path)
    calls: list[str] = []
    fail_middle = True

    def fake_execute(item, **kwargs):
        native = str(item.observation.native_observation_id)
        calls.append(native)
        if fail_middle and native == "native-00001":
            return Stage1ExecutionItemResult(
                source_observation_uid=item.observation.source_observation_uid,
                native_observation_id=native,
                observation_uid=item.observation.observation_uid,
                action=item.action,
                status="FAILED",
                product=item.product,
                error="synthetic failure",
            )
        materialized = kwargs["context"].materialize(item.observation.asset("processed"))
        product = kwargs["product_store"].resolve(item.product)
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes(native.encode())
        checksum = hashlib.sha256(product.read_bytes()).hexdigest()
        provenance = product.with_name(product.name + ".provenance.json")
        provenance.write_text(json.dumps({
            "schema": STAGE1_PROVENANCE_SCHEMA,
            "fingerprint": "synthetic-fingerprint",
            "inputs": [materialized.to_dict()],
            "product": {"checksum_sha256": checksum},
        }), encoding="utf-8")
        return Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=native,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status="IMPORTED",
            product=item.product,
            product_path=str(product),
            product_checksum_sha256=checksum,
            provenance_path=str(provenance),
            fingerprint="synthetic-fingerprint",
        )

    monkeypatch.setattr("grandgousier.archive.stage1_execution._execute_item", fake_execute)

    def run(checkpoint, *, prefix):
        return execute_stage1_stream(
            request=ArchiveRequest(source_id="checkpoint"),
            source=_source(),
            metadata_provider=_Provider(_records(3)),
            asset_provider=assets,
            product_store=store,
            batch_size=batch_size,
            plan_json_path=tmp_path / f"{prefix}-plan.json",
            report_json_path=tmp_path / f"{prefix}-report.json",
            checkpoint=checkpoint,
            run_fingerprint=fingerprint,
            stop_on_first_failure=True,
        )

    with Stage1Checkpoint.open(checkpoint_path, source_id="checkpoint",
                               config_fingerprint=fingerprint, resume=False, restart=False) as checkpoint:
        first = run(checkpoint, prefix="first")
        state = checkpoint.state()
        assert (state.status, state.attempted, state.completed, state.failed) == (
            "FAILED", 2, 1, 1,
        )
    assert calls == ["native-00000", "native-00001"]
    assert not (tmp_path / "native-00002.fits").exists()
    assert first.status == first.plan_status == "FAILED"
    assert first.to_dict()["summary"]["attempted_items"] == 2
    report = json.loads((tmp_path / "first-report.json").read_text(encoding="utf-8"))
    plan = json.loads((tmp_path / "first-plan.json").read_text(encoding="utf-8"))
    assert (report["status"], report["plan_status"], plan["status"]) == (
        "FAILED", "FAILED", "FAILED",
    )
    assert (plan["summary"]["science_items"], plan["summary"]["attempted_items"]) == (
        planned, 2,
    )
    assert (report["summary"]["items"], report["summary"]["reported_items"]) == (
        planned, 2,
    )
    assert [row["status"] for row in report["items"]] == ["IMPORTED", "FAILED"]
    assert len((tmp_path / "first-report.csv").read_text(encoding="utf-8").splitlines()) == 3
    assert not (tmp_path / "first-report.json.part").exists()

    fail_middle = False
    with Stage1Checkpoint.open(checkpoint_path, source_id="checkpoint",
                               config_fingerprint=fingerprint, resume=True, restart=False) as checkpoint:
        second = run(checkpoint, prefix="second")
        state = checkpoint.state()
        assert (state.status, state.attempted, state.completed, state.failed) == (
            "COMPLETE", 4, 3, 1,
        )
    assert calls == ["native-00000", "native-00001", "native-00001", "native-00002"]
    assert second.status == "READY"
    assert second.to_dict()["summary"]["attempted_items"] == 2
    assert [row["status"] for row in json.loads(
        (tmp_path / "second-report.json").read_text(encoding="utf-8")
    )["items"]] == ["IMPORTED"] * 3


def test_synthetic_10000_resume_reexecutes_only_unconfirmed_items(monkeypatch, tmp_path: Path) -> None:
    total = 10_000
    batch_size = 64
    checkpoint_path = tmp_path / "run.sqlite3"
    run_fingerprint = _fingerprint(tmp_path, batch_size=batch_size)
    input_path = tmp_path / "synthetic-input.fits"
    input_path.write_bytes(b"synthetic-input")
    assets = _AssetProvider({}, default_path=input_path)
    store = _Store(tmp_path / "products")
    execution_calls: list[str] = []
    interrupt_after = 4_000

    def fake_execute(item, **kwargs):
        native = str(item.observation.native_observation_id)
        execution_calls.append(native)
        if len(execution_calls) == interrupt_after:
            raise KeyboardInterrupt
        materialized = kwargs["context"].materialize(item.observation.asset("processed"))
        product_path = kwargs["product_store"].resolve(item.product)
        product_path.parent.mkdir(parents=True, exist_ok=True)
        product_path.write_bytes(b"synthetic-product")
        checksum = hashlib.sha256(product_path.read_bytes()).hexdigest()
        provenance_path = product_path.with_name(product_path.name + ".provenance.json")
        provenance_path.write_text(
            json.dumps(
                {
                    "schema": STAGE1_PROVENANCE_SCHEMA,
                    "fingerprint": "synthetic-fingerprint",
                    "inputs": [materialized.to_dict()],
                    "product": {"checksum_sha256": checksum},
                }
            ),
            encoding="utf-8",
        )
        return Stage1ExecutionItemResult(
            source_observation_uid=item.observation.source_observation_uid,
            native_observation_id=item.observation.native_observation_id,
            observation_uid=item.observation.observation_uid,
            action=item.action,
            status="IMPORTED",
            product=item.product,
            product_path=str(product_path),
            product_checksum_sha256=checksum,
            provenance_path=str(provenance_path),
            fingerprint="synthetic-fingerprint",
        )

    monkeypatch.setattr("grandgousier.archive.stage1_execution._execute_item", fake_execute)
    first_checkpoint = Stage1Checkpoint.open(
        checkpoint_path,
        source_id="checkpoint",
        config_fingerprint=run_fingerprint,
        resume=False,
        restart=False,
    )
    # Keep the stress test practical on Windows; production defaults to FULL.
    first_checkpoint.connection.executescript(
        "PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF"
    )
    with pytest.raises(KeyboardInterrupt):
        execute_stage1_stream(
            request=ArchiveRequest(source_id="checkpoint"),
            source=_source(),
            metadata_provider=_Provider(_records(total)),
            asset_provider=assets,
            product_store=store,
            batch_size=batch_size,
            plan_json_path=tmp_path / "first-plan.json",
            report_json_path=tmp_path / "first-report.json",
            checkpoint=first_checkpoint,
            run_fingerprint=run_fingerprint,
        )
    first_checkpoint.close()

    second_checkpoint = Stage1Checkpoint.open(
        checkpoint_path,
        source_id="checkpoint",
        config_fingerprint=run_fingerprint,
        resume=True,
        restart=False,
    )
    second_checkpoint.connection.executescript(
        "PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF"
    )
    summary = execute_stage1_stream(
        request=ArchiveRequest(source_id="checkpoint"),
        source=_source(),
        metadata_provider=_Provider(_records(total)),
        asset_provider=assets,
        product_store=store,
        batch_size=batch_size,
        plan_json_path=tmp_path / "final-plan.json",
        report_json_path=tmp_path / "final-report.json",
        checkpoint=second_checkpoint,
        run_fingerprint=run_fingerprint,
    )
    state = second_checkpoint.state()
    second_checkpoint.close()

    assert summary.status == "READY"
    assert summary.counts["IMPORTED"] == total
    assert state.status == "COMPLETE"
    assert state.completed == total
    assert len(execution_calls) == total + 1
    report = json.loads((tmp_path / "final-report.json").read_text(encoding="utf-8"))
    assert report["summary"]["items"] == total
    assert len(report["items"]) == total
