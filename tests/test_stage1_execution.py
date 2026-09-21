from __future__ import annotations

import json
from pathlib import Path

from grandgousier.archive.master_reduction import MasterReductionResult
from grandgousier.archive.stage1_contract import (
    ArchiveRequest,
    ArchiveSourceDescriptor,
    AssetRef,
    CalibrationGroup,
    CalibrationPlan,
    CalibrationPlanItem,
    LocalAsset,
    ObservationRecord,
    ProductRef,
    make_source_observation_uid,
)
from grandgousier.archive.stage1_execution import (
    STAGE1_PROVENANCE_SCHEMA,
    execute_stage1_plan,
)
from grandgousier.cli import build_parser


class FakeAssets:
    def __init__(self, mapping: dict[str, Path]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def materialize(self, ref: AssetRef, destination=None) -> LocalAsset:
        del destination
        self.calls.append(ref.ref_uid)
        return LocalAsset(ref=ref, path=self.mapping[ref.ref_uid])


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store_id = "test-store"

    @property
    def public_descriptor(self):
        return {"store_id": self.store_id, "backend": "filesystem"}

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef(self.store_id, "filesystem", f"{observation.source_observation_uid}_processed.fits")

    def resolve(self, ref: ProductRef) -> Path:
        assert ref.store_id == self.store_id
        return self.root / ref.locator


def _ready_observation(*, action: str, assets: tuple[AssetRef, ...], instrument: str = "MASTER") -> ObservationRecord:
    native = "203_17870"
    return ObservationRecord(
        source_id="source",
        instrument=instrument,
        frame_type="science",
        input_state="processed" if action == "IMPORT_PROCESSED" else "raw",
        identity_status="READY",
        native_observation_id=native,
        source_observation_uid=make_source_observation_uid("source", native),
        identity_scheme="test_identity_v1",
        p4_native_identity=native,
        observation_uid="obs_test",
        observed_at="2018-09-07T17:04:22+00:00",
        required_asset_roles=tuple(ref.role for ref in assets),
        assets=assets,
        metadata={"raw_id": "375506", "processed_frame_id": "131841"},
    )


def _plan_for(obs: ObservationRecord, store: FakeStore, *, action: str, status: str = "READY") -> CalibrationPlan:
    item = CalibrationPlanItem(
        observation=obs,
        action=action,
        status=status,
        product=store.plan_product(obs) if status == "READY" else None,
        calibration=CalibrationGroup(mode="TEST", assets=()),
        issues=() if status == "READY" else ("blocked for test",),
    )
    return CalibrationPlan.now(
        source=ArchiveSourceDescriptor(
            source_id="source",
            adapter="test",
            instrument_profile=obs.instrument,
            metadata_backend="test",
            asset_backend="test",
        ),
        request=ArchiveRequest(source_id="source"),
        product_store=store.public_descriptor,
        items=[item],
        discovered_records=1,
    )


def test_processed_import_writes_product_provenance_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.fits"
    source.write_bytes(b"fits-like-source-v1")
    ref = AssetRef("source", "processed", "filesystem", "source.fits")
    store = FakeStore(tmp_path / "products")
    obs = _ready_observation(action="IMPORT_PROCESSED", assets=(ref,))
    plan = _plan_for(obs, store, action="IMPORT_PROCESSED")
    assets = FakeAssets({ref.ref_uid: source})

    first = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    assert first.status == "READY"
    assert first.results[0].status == "IMPORTED"
    output = Path(first.results[0].product_path)
    provenance = Path(first.results[0].provenance_path)
    assert output.read_bytes() == source.read_bytes()
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    assert payload["schema"] == STAGE1_PROVENANCE_SCHEMA
    assert payload["fingerprint"] == first.results[0].fingerprint
    assert payload["observation"]["source_observation_uid"] == obs.source_observation_uid
    assert payload["product"]["checksum_sha256"] == first.results[0].product_checksum_sha256

    second = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    assert second.status == "READY"
    assert second.results[0].status == "SKIPPED_EXISTING"
    assert second.results[0].fingerprint == first.results[0].fingerprint


def test_changed_processed_input_refuses_stale_existing_product(tmp_path: Path) -> None:
    source = tmp_path / "source.fits"
    source.write_bytes(b"version-one")
    ref = AssetRef("source", "processed", "filesystem", "source.fits")
    store = FakeStore(tmp_path / "products")
    obs = _ready_observation(action="IMPORT_PROCESSED", assets=(ref,))
    plan = _plan_for(obs, store, action="IMPORT_PROCESSED")
    assets = FakeAssets({ref.ref_uid: source})

    first = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    output = Path(first.results[0].product_path)
    assert output.read_bytes() == b"version-one"

    source.write_bytes(b"version-two")
    second = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    assert second.status == "FAILED"
    assert second.results[0].status == "FAILED"
    assert "fingerprint" in str(second.results[0].error)
    assert output.read_bytes() == b"version-one"

    third = execute_stage1_plan(plan, asset_provider=assets, product_store=store, overwrite=True)
    assert third.status == "READY"
    assert third.results[0].status == "IMPORTED"
    assert output.read_bytes() == b"version-two"


def test_blocked_plan_item_never_materializes_assets(tmp_path: Path) -> None:
    ref = AssetRef("source", "processed", "filesystem", "source.fits")
    store = FakeStore(tmp_path / "products")
    obs = _ready_observation(action="IMPORT_PROCESSED", assets=(ref,))
    plan = _plan_for(obs, store, action="IMPORT_PROCESSED", status="BLOCKED")
    assets = FakeAssets({})

    report = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    assert report.status == "BLOCKED"
    assert report.results[0].status == "BLOCKED"
    assert assets.calls == []



def test_blocked_plan_prevents_partial_execution(tmp_path: Path) -> None:
    first_source = tmp_path / "first.fits"
    first_source.write_bytes(b"first")
    first_ref = AssetRef("source", "processed", "filesystem", "first.fits")
    second_ref = AssetRef("source", "processed", "filesystem", "second.fits")
    store = FakeStore(tmp_path / "products")
    ready_obs = _ready_observation(action="IMPORT_PROCESSED", assets=(first_ref,))
    blocked_obs = ObservationRecord(
        source_id="source",
        instrument="MASTER",
        frame_type="science",
        input_state="processed",
        identity_status="MISSING",
        identity_detail="missing identity",
        required_asset_roles=("processed",),
        assets=(second_ref,),
    )
    plan = CalibrationPlan.now(
        source=ArchiveSourceDescriptor(
            source_id="source", adapter="test", instrument_profile="MASTER", metadata_backend="test", asset_backend="test"
        ),
        request=ArchiveRequest(source_id="source"),
        product_store=store.public_descriptor,
        items=[
            CalibrationPlanItem(
                observation=ready_obs, action="IMPORT_PROCESSED", status="READY",
                product=store.plan_product(ready_obs), calibration=CalibrationGroup(mode="TEST", assets=()),
            ),
            CalibrationPlanItem(
                observation=blocked_obs, action="IMPORT_PROCESSED", status="BLOCKED",
                product=None, calibration=CalibrationGroup(mode="TEST", assets=()), issues=("missing identity",),
            ),
        ],
        discovered_records=2,
    )
    assets = FakeAssets({first_ref.ref_uid: first_source})
    report = execute_stage1_plan(plan, asset_provider=assets, product_store=store)
    assert report.status == "BLOCKED"
    assert assets.calls == []
    assert not store.root.exists()
    assert [item.status for item in report.results] == ["BLOCKED", "BLOCKED"]


def test_execution_rejects_product_store_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.fits"
    source.write_bytes(b"input")
    ref = AssetRef("source", "processed", "filesystem", "source.fits")
    planned_store = FakeStore(tmp_path / "planned")
    actual_store = FakeStore(tmp_path / "actual")
    actual_store.store_id = "different-store"
    obs = _ready_observation(action="IMPORT_PROCESSED", assets=(ref,))
    plan = _plan_for(obs, planned_store, action="IMPORT_PROCESSED")
    assets = FakeAssets({ref.ref_uid: source})
    import pytest
    with pytest.raises(ValueError, match="product_store store_id"):
        execute_stage1_plan(plan, asset_provider=assets, product_store=actual_store)
    assert assets.calls == []

def test_master_execute_reuses_existing_reducer_contract(tmp_path: Path, monkeypatch) -> None:
    refs = tuple(
        AssetRef("source", role, "test", f"{role}.fits")
        for role in ("science_raw", "processing_header", "dark", "flat")
    )
    paths: dict[str, Path] = {}
    for ref in refs:
        path = tmp_path / ref.locator
        path.write_bytes((ref.role + "-bytes").encode("ascii"))
        paths[ref.ref_uid] = path

    store = FakeStore(tmp_path / "products")
    obs = _ready_observation(action="REDUCE_RAW", assets=refs)
    plan = _plan_for(obs, store, action="REDUCE_RAW")
    assets = FakeAssets(paths)
    captured = {}

    def fake_reduce(task, *, overwrite=False, max_dark_temperature_delta_c=5.0):
        captured["task"] = task
        captured["overwrite"] = overwrite
        captured["max_dark_temperature_delta_c"] = max_dark_temperature_delta_c
        task.output_path.parent.mkdir(parents=True, exist_ok=True)
        task.output_path.write_bytes(b"reduced-product")
        return MasterReductionResult(task=task, status="REDUCED")

    monkeypatch.setattr("grandgousier.archive.stage1_execution.reduce_master_task", fake_reduce)
    report = execute_stage1_plan(plan, asset_provider=assets, product_store=store)

    assert report.status == "READY"
    assert report.results[0].status == "REDUCED"
    task = captured["task"]
    assert task.raw_remote_path == "science_raw.fits"
    assert task.header_remote_path == "processing_header.fits"
    assert task.dark_remote_path == "dark.fits"
    assert task.flat_remote_path == "flat.fits"
    assert task.raw_local_path == paths[refs[0].ref_uid]
    assert task.output_path == Path(report.results[0].product_path)
    sidecar = json.loads(Path(report.results[0].provenance_path).read_text(encoding="utf-8"))
    assert sidecar["recipe"]["name"] == "MASTER_DARK_FLAT_V1"
    assert {row["ref"]["role"] for row in sidecar["inputs"]} == {
        "science_raw",
        "processing_header",
        "dark",
        "flat",
    }



def test_master_existing_without_canonical_sidecar_requires_rebuild(tmp_path: Path, monkeypatch) -> None:
    refs = tuple(
        AssetRef("source", role, "test", f"{role}.fits")
        for role in ("science_raw", "processing_header", "dark", "flat")
    )
    paths: dict[str, Path] = {}
    for ref in refs:
        path = tmp_path / ref.locator
        path.write_bytes((ref.role + "-bytes").encode("ascii"))
        paths[ref.ref_uid] = path
    store = FakeStore(tmp_path / "products")
    obs = _ready_observation(action="REDUCE_RAW", assets=refs)
    plan = _plan_for(obs, store, action="REDUCE_RAW")
    output = store.resolve(plan.items[0].product)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"legacy-master-output")
    called = {"reduce": 0}

    def should_not_reduce(*args, **kwargs):
        called["reduce"] += 1
        raise AssertionError("reducer must not run without explicit overwrite")

    monkeypatch.setattr("grandgousier.archive.stage1_execution.reduce_master_task", should_not_reduce)
    report = execute_stage1_plan(plan, asset_provider=FakeAssets(paths), product_store=store)
    assert report.status == "FAILED"
    assert report.results[0].status == "FAILED"
    assert "cannot prove the exact input bytes" in str(report.results[0].error)
    assert called["reduce"] == 0
    assert output.read_bytes() == b"legacy-master-output"

def test_stage1_execute_cli_contract() -> None:
    args = build_parser().parse_args(
        [
            "stage1-execute",
            "--config",
            "grandgousier.yaml",
            "--source",
            "master-smoke",
            "--limit",
            "5",
            "--plan-out",
            "plan.json",
            "--report-out",
            "execution.json",
        ]
    )
    assert args.command == "stage1-execute"
    assert args.config_file == "grandgousier.yaml"
    assert args.source == "master-smoke"
    assert args.limit == 5
    assert args.overwrite is False
    assert args.max_dark_temperature_delta_c == 5.0
