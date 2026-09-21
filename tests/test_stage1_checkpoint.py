from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from grandgousier import __version__
from grandgousier.cli import build_parser
from grandgousier.archive.stage1_checkpoint import (
    CheckpointMismatchError,
    CheckpointStateError,
    Stage1Checkpoint,
    execution_fingerprint,
)
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    ObservationRecord,
    ProductRef,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_execution import (
    STAGE1_PROVENANCE_SCHEMA,
    Stage1ExecutionItemResult,
    _checkpoint_product_is_durable,
    execute_stage1_stream,
)


class _Store:
    public_descriptor = {"store_id": "checkpoint-test", "backend": "filesystem"}

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef("checkpoint-test", "filesystem", f"{observation.native_observation_id}.fits")


class _Provider:
    def __init__(self, records):
        self.records = records

    def search(self, request):
        del request
        yield from self.records


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


def test_stage1_execute_resume_modes_are_mutually_exclusive() -> None:
    parser = build_parser()
    resumed = parser.parse_args(["stage1-execute", "--source", "master-smoke", "--resume"])
    restarted = parser.parse_args(["stage1-execute", "--source", "master-smoke", "--restart-run"])
    assert resumed.resume is True
    assert resumed.restart_run is False
    assert restarted.resume is False
    assert restarted.restart_run is True
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["stage1-execute", "--source", "master-smoke", "--resume", "--restart-run"]
        )


def test_synthetic_10000_resume_reexecutes_only_unconfirmed_items(monkeypatch, tmp_path: Path) -> None:
    total = 10_000
    batch_size = 64
    checkpoint_path = tmp_path / "run.sqlite3"
    run_fingerprint = _fingerprint(tmp_path, batch_size=batch_size)
    execution_calls: list[str] = []
    interrupt_after = 4_000

    def fake_execute(item, **kwargs):
        del kwargs
        native = str(item.observation.native_observation_id)
        execution_calls.append(native)
        if len(execution_calls) == interrupt_after:
            raise KeyboardInterrupt
        product_path = tmp_path / "products" / "synthetic.fits"
        product_path.parent.mkdir(parents=True, exist_ok=True)
        product_path.write_bytes(b"synthetic-product")
        checksum = __import__("hashlib").sha256(product_path.read_bytes()).hexdigest()
        provenance_path = product_path.with_name(product_path.name + ".provenance.json")
        provenance_path.write_text(
            json.dumps(
                {
                    "schema": STAGE1_PROVENANCE_SCHEMA,
                    "fingerprint": "synthetic-fingerprint",
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
            asset_provider=object(),
            product_store=_Store(),
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
        asset_provider=object(),
        product_store=_Store(),
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
