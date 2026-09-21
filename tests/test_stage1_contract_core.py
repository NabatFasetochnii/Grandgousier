from __future__ import annotations

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
from grandgousier.archive.stage1_planner import (
    build_stage1_plan,
    write_stage1_plan_csv,
    write_stage1_plan_json,
)


class _ProductStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def public_descriptor(self):
        return {"store_id": "test", "backend": "filesystem"}

    def plan_product(self, observation: ObservationRecord) -> ProductRef:
        return ProductRef("test", "filesystem", f"{observation.source_observation_uid}.fits")

    def resolve(self, ref: ProductRef) -> Path:
        return self.root / ref.locator


def _source(source_id: str = "fixture") -> ArchiveSourceDescriptor:
    return ArchiveSourceDescriptor(
        source_id=source_id,
        adapter="fake",
        instrument_profile="MASTER",
        metadata_backend="fake",
        asset_backend="fake",
    )


def _ready_processed_record(source_id: str = "fixture") -> ObservationRecord:
    native = "203_17870"
    return ObservationRecord(
        source_id=source_id,
        instrument="MASTER",
        frame_type="science",
        input_state="processed",
        identity_status="READY",
        native_observation_id=native,
        source_observation_uid=make_source_observation_uid(source_id, native),
        identity_scheme="master_uniqnum_v1",
        p4_native_identity=native,
        observation_uid="obs_test",
        required_asset_roles=("processed",),
        assets=(AssetRef(source_id, "processed", "filesystem", "night/frame.fits"),),
    )


def test_source_observation_uid_is_path_independent_and_source_namespaced() -> None:
    left = make_source_observation_uid("master-prod", "203_17870")
    right = make_source_observation_uid("master-prod", "203_17870")
    other = make_source_observation_uid("master-copy", "203_17870")
    assert left == right
    assert left.startswith("srcobs_")
    assert left != other


def test_archive_request_validates_spatial_and_limit_contract() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        ArchiveRequest(source_id="x", ra_deg=1.0)
    with pytest.raises(ValueError, match="radius_deg requires"):
        ArchiveRequest(source_id="x", radius_deg=1.0)
    with pytest.raises(ValueError, match="positive"):
        ArchiveRequest(source_id="x", limit=0)


def test_ready_observation_requires_matching_source_identity() -> None:
    with pytest.raises(ValueError, match="source_observation_uid"):
        ObservationRecord(
            source_id="fixture",
            instrument="MASTER",
            frame_type="science",
            input_state="processed",
            identity_status="READY",
            native_observation_id="203_17870",
            source_observation_uid="srcobs_wrong",
            identity_scheme="master_uniqnum_v1",
            p4_native_identity="203_17870",
            observation_uid="obs_test",
        )


def test_plan_never_materializes_assets(tmp_path: Path) -> None:
    record = _ready_processed_record()

    class Metadata:
        def search(self, request):
            return [record]

    class CountingAssets:
        def __init__(self):
            self.calls = 0

        def materialize(self, ref, destination=None):
            self.calls += 1
            raise AssertionError("plan-only must not materialize")

    assets = CountingAssets()
    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="fixture"),
        source=_source(),
        metadata_provider=Metadata(),
        asset_provider=assets,
        product_store=_ProductStore(tmp_path),
    )
    assert plan.status == "READY"
    assert assets.calls == 0
    assert plan.items[0].action == "IMPORT_PROCESSED"


def test_missing_identity_is_blocked_without_product(tmp_path: Path) -> None:
    record = ObservationRecord(
        source_id="fixture",
        instrument="MASTER",
        frame_type="science",
        input_state="raw",
        identity_status="MISSING",
        identity_detail="missing UNIQNUM",
        required_asset_roles=("science_raw",),
        assets=(AssetRef("fixture", "science_raw", "fake", "raw.fit"),),
    )

    class Metadata:
        def search(self, request):
            return [record]

    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="fixture"),
        source=_source(),
        metadata_provider=Metadata(),
        asset_provider=object(),
        product_store=_ProductStore(tmp_path),
    )
    assert plan.status == "BLOCKED"
    assert plan.items[0].product is None
    assert "path/raw-id fallback is forbidden" in plan.items[0].issues[0]


def test_missing_required_asset_role_blocks_plan(tmp_path: Path) -> None:
    record = _ready_processed_record()
    record = ObservationRecord(
        **{
            **record.to_dict(),
            "required_asset_roles": ("processed", "processing_header"),
            "assets": record.assets,
            "metadata": {},
        }
    )

    class Metadata:
        def search(self, request):
            return [record]

    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="fixture"),
        source=_source(),
        metadata_provider=Metadata(),
        asset_provider=object(),
        product_store=_ProductStore(tmp_path),
    )
    assert plan.status == "BLOCKED"
    assert plan.items[0].missing_asset_roles == ("processing_header",)


def test_plan_json_and_csv_serialization(tmp_path: Path) -> None:
    record = _ready_processed_record()

    class Metadata:
        def search(self, request):
            return [record]

    plan = build_stage1_plan(
        request=ArchiveRequest(source_id="fixture"),
        source=_source(),
        metadata_provider=Metadata(),
        asset_provider=object(),
        product_store=_ProductStore(tmp_path),
    )
    json_path = write_stage1_plan_json(plan, tmp_path / "plan.json")
    csv_path = write_stage1_plan_csv(plan, tmp_path / "plan.csv")
    assert '"schema": "grandgousier.stage1.plan.v1"' in json_path.read_text(encoding="utf-8")
    assert "source_observation_uid" in csv_path.read_text(encoding="utf-8")


def test_planner_rejects_request_source_mismatch(tmp_path: Path) -> None:
    record = _ready_processed_record()

    class Metadata:
        def search(self, request):
            return [record]

    with pytest.raises(ValueError, match="does not match"):
        build_stage1_plan(
            request=ArchiveRequest(source_id="other-source"),
            source=_source(),
            metadata_provider=Metadata(),
            asset_provider=object(),
            product_store=_ProductStore(tmp_path),
        )


def test_planner_rejects_provider_record_from_other_source(tmp_path: Path) -> None:
    record = _ready_processed_record("other-source")

    class Metadata:
        def search(self, request):
            return [record]

    with pytest.raises(ValueError, match="returned source_id"):
        build_stage1_plan(
            request=ArchiveRequest(source_id="fixture"),
            source=_source(),
            metadata_provider=Metadata(),
            asset_provider=object(),
            product_store=_ProductStore(tmp_path),
        )
