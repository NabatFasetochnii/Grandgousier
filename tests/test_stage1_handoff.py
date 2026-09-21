from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from grandgousier.archive.stage1_execution import STAGE1_EXECUTION_SCHEMA, STAGE1_PROVENANCE_SCHEMA
from grandgousier.archive.stage1_handoff import (
    handoff_stage1_execution,
    load_validated_stage1_products,
)
from grandgousier.cli import build_parser
from grandgousier.db.models import (
    Base,
    Field,
    FieldSeries,
    FieldSeriesFrame,
    Frame,
    FrameStatus,
    FrameType,
    Observation,
)
from grandgousier.identity.observation import make_observation_uid


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(_type, _compiler, **_kw):
    return "INTEGER"


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_execution_report(tmp_path: Path, *, native: str = "204_15814") -> tuple[Path, Path]:
    product = tmp_path / "product.fits"
    product.write_bytes(b"stage1-product")
    checksum = _sha(product)
    obs_uid = make_observation_uid("master_uniqnum_v1", native)
    provenance = product.with_name(product.name + ".provenance.json")
    fingerprint = "fingerprint-test"
    provenance.write_text(
        json.dumps(
            {
                "schema": STAGE1_PROVENANCE_SCHEMA,
                "fingerprint": fingerprint,
                "observation": {
                    "native_observation_id": native,
                    "source_observation_uid": "srcobs-test",
                    "observation_uid": obs_uid,
                },
                "product": {
                    "store_id": "stage1-test",
                    "backend": "filesystem",
                    "locator": "MASTER/2018/09/07/product.fits",
                    "checksum_sha256": checksum,
                },
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "execution.json"
    report.write_text(
        json.dumps(
            {
                "schema": STAGE1_EXECUTION_SCHEMA,
                "status": "READY",
                "source_id": "master-smoke",
                "items": [
                    {
                        "native_observation_id": native,
                        "source_observation_uid": "srcobs-test",
                        "observation_uid": obs_uid,
                        "action": "REDUCE_RAW",
                        "status": "SKIPPED_EXISTING",
                        "product": {
                            "store_id": "stage1-test",
                            "backend": "filesystem",
                            "locator": "MASTER/2018/09/07/product.fits",
                        },
                        "product_path": str(product),
                        "product_checksum_sha256": checksum,
                        "provenance_path": str(provenance),
                        "fingerprint": fingerprint,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return report, product


def _observation(session: Session, native: str) -> Observation:
    scheme = "master_uniqnum_v1"
    row = Observation(
        observation_uid=make_observation_uid(scheme, native),
        instrument="MASTER",
        identity_scheme=scheme,
        native_identity=native,
        camera_identity="204",
        observed_at=datetime(2018, 9, 7, 17, 4, 22, tzinfo=timezone.utc),
        identity_components={"UNIQNUM": native},
    )
    session.add(row)
    session.flush()
    return row


def _frame(session: Session, observation: Observation, *, uid: str = "existing-frame") -> Frame:
    frame = Frame(
        frame_uid=uid,
        observation_id=observation.observation_id,
        raw_path=f"C:/historical/{uid}.fits",
        instrument="MASTER",
        camera="204",
        filter_name="V",
        image_type=FrameType.SCIENCE,
        date_obs=datetime(2018, 9, 7, 17, 4, 22, tzinfo=timezone.utc),
        exptime=50.0,
        processing_status=FrameStatus.HEADER_READ,
        quality_flags={},
        header_json={"UNIQNUM": observation.native_identity},
    )
    session.add(frame)
    session.flush()
    return frame


def _assign(
    session: Session,
    frame: Frame,
    *,
    field: Field | None = None,
    series_uid: str = "handoff-series",
) -> FieldSeries:
    if field is None:
        field = Field(
            field_uid="handoff-field",
            field_name="handoff",
            ra_center_deg=327.5,
            dec_center_deg=72.1,
            radius_arcmin=30.0,
            metadata_json={},
        )
        session.add(field)
        session.flush()
    series = FieldSeries(
        field_id=field.field_id,
        series_uid=series_uid,
        filter_name="V",
        n_frames=1,
        grouping_method="test",
        metadata_json={},
    )
    session.add(series)
    session.flush()
    session.add(FieldSeriesFrame(series_id=series.series_id, frame_id=frame.frame_id, order_index=1))
    session.flush()
    return series


def _install_fake_auto_process(monkeypatch, product: Path, *, native: str, frame_uid: str, process_call):
    obs_uid = make_observation_uid("master_uniqnum_v1", native)
    detected = SimpleNamespace(
        path=str(product),
        instrument="MASTER",
        frame_uid=frame_uid,
        observation_uid=obs_uid,
        identity_scheme="master_uniqnum_v1",
        native_identity=native,
        observation_identity_status="READY",
        observation_identity_detail=None,
    )

    def scan_archive_paths(paths, **_kwargs):
        assert [Path(value) for value in paths] == [product]
        return [detected], [], []

    fake = types.ModuleType("grandgousier.archive.auto_process")
    fake.scan_archive_paths = scan_archive_paths
    fake.process_archive_path = process_call
    monkeypatch.setitem(sys.modules, "grandgousier.archive.auto_process", fake)


def test_execution_report_validation_rejects_changed_product(tmp_path: Path) -> None:
    report, product = _write_execution_report(tmp_path)
    product.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_validated_stage1_products(report)


def test_already_represented_handoff_does_not_invoke_stage2_or_insert_frame(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_15814"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    existing = _frame(db_session, obs)
    series = _assign(db_session, existing)
    before_frames = db_session.scalar(select(func.count(Frame.frame_id)))
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("Stage 2 must not run for an already represented physical observation")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-product-frame-uid",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "READY"
    assert result.selected_items == 0
    assert result.retained_items == 1
    assert result.stage2_result is None
    assert result.items[0].p4_action == "ALREADY_REPRESENTED"
    assert result.items[0].existing_series_ids == (series.series_id,)
    assert called["process"] == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == before_frames



def test_historical_multiple_representatives_are_safe_handoff_noop(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_DOUBLE_HANDOFF"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    first = _frame(db_session, obs, uid="historical-a")
    second = _frame(db_session, obs, uid="historical-b")
    first_series = _assign(db_session, first, series_uid="handoff-series-a")
    field = db_session.get(Field, first_series.field_id)
    assert field is not None
    second_series = _assign(db_session, second, field=field, series_uid="handoff-series-b")
    before_frames = db_session.scalar(select(func.count(Frame.frame_id)))
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("historical multiple representatives must remain a handoff no-op")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-product-frame-uid",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "READY"
    assert result.selected_items == 0
    assert result.retained_items == 1
    assert result.blocked_items == 0
    assert result.stage2_result is None
    assert result.items[0].status == "ALREADY_REPRESENTED"
    assert result.items[0].p4_action == "EXISTING_MULTIPLE_REPRESENTATIVES"
    assert result.items[0].selected_frame_id is None
    assert result.items[0].existing_series_ids == tuple(sorted((first_series.series_id, second_series.series_id)))
    assert called["process"] == 0
    assert db_session.scalar(select(func.count(Frame.frame_id))) == before_frames

def test_new_observation_invokes_process_archive_path_with_exact_product_only(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_NEW"
    report, product = _write_execution_report(tmp_path, native=native)
    captured = {}

    class FakeProcessResult:
        def to_dict(self):
            return {
                "status": "DONE",
                "discovered_files": 1,
                "indexed_frame_ids": [999],
                "created_series_ids": [],
                "processed_series_ids": [],
            }

    def fake_process(_session, **kwargs):
        captured.update(kwargs)
        return FakeProcessResult()

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-observation-product",
        process_call=fake_process,
    )
    result = handoff_stage1_execution(
        db_session,
        execution_report_path=report,
        execute=True,
        catalog_name="existing-catalog",
        instrument_profile="MASTER",
    )

    assert result.status == "READY"
    assert result.selected_items == 1
    assert result.retained_items == 0
    assert result.stage2_result["status"] == "DONE"
    assert captured["exact_paths"] == [product]
    assert captured["recursive"] is False
    assert captured["catalog_name"] == "existing-catalog"
    assert captured["execute"] is True


def test_ambiguous_new_product_blocks_before_stage2(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_AMBIG"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    _frame(db_session, obs, uid="old-unassigned")
    called = {"process": 0}

    def forbidden_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("blocked handoff must not invoke Stage 2")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-unassigned",
        process_call=forbidden_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "BLOCKED"
    assert result.blocked_items == 1
    assert result.items[0].p4_action == "AMBIGUOUS_MULTIPLE_PRODUCTS"
    assert result.stage2_result is None
    assert called["process"] == 0


def test_stage1_handoff_cli_contract() -> None:
    args = build_parser().parse_args(
        [
            "stage1-handoff",
            "--config",
            "grandgousier.yaml",
            "--execution-report",
            "execution.json",
            "--catalog-name",
            "catalog",
        ]
    )
    assert args.command == "stage1-handoff"
    assert args.config_file == "grandgousier.yaml"
    assert args.execution_report == "execution.json"
    assert args.execute is False
    assert args.catalog_name == "catalog"
    assert args.min_frames_per_series == 10


def test_execution_report_validation_rejects_product_locator_mismatch(tmp_path: Path) -> None:
    report, _product = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0]["product"]["locator"] = "different/product.fits"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="product locator mismatch"):
        load_validated_stage1_products(report)


def test_nested_stage2_exception_is_returned_as_failed_report(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_FAIL"
    report, product = _write_execution_report(tmp_path, native=native)

    def failing_process(*_args, **_kwargs):
        raise RuntimeError("synthetic nested failure")

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-observation-product",
        process_call=failing_process,
    )
    result = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)

    assert result.status == "FAILED"
    assert result.selected_items == 1
    assert result.stage2_result is None
    assert result.errors == ("Stage-2 handoff failed: RuntimeError: synthetic nested failure",)
