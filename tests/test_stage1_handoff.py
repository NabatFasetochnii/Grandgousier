from __future__ import annotations

import hashlib
import json
import sys
import types
from dataclasses import replace
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
    Stage1HandoffItem,
    _persist_selected_evidence,
    handoff_stage1_execution,
    load_validated_stage1_products,
)
from grandgousier.archive.stage1_reclaim import _validate_product
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
    product = tmp_path / "srcobs-test_processed.fits"
    product.write_bytes(b"stage1-product")
    checksum = _sha(product)
    obs_uid = make_observation_uid("master_uniqnum_v1", native)
    provenance = product.with_name(product.name + ".provenance.json")
    fingerprint = "a" * 64
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
                    "locator": f"MASTER/2018/09/07/{product.name}",
                    "checksum_sha256": checksum,
                    "size_bytes": product.stat().st_size,
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
                            "locator": f"MASTER/2018/09/07/{product.name}",
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


@pytest.mark.parametrize("value", [None, "invalid", "b" * 64])
def test_fingerprint_missing_invalid_or_mismatched_blocks_before_mutation(
    db_session: Session, tmp_path: Path, value
) -> None:
    report, product = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0]["fingerprint"] = value
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0
    assert product.is_file()


@pytest.mark.parametrize("field", ["native_observation_id", "source_observation_uid", "observation_uid"])
def test_incomplete_observation_evidence_blocks_before_mutation(
    db_session: Session, tmp_path: Path, field: str
) -> None:
    report, _ = _write_execution_report(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["items"][0][field] = None
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert db_session.scalar(select(func.count(Frame.frame_id))) == 0


def test_conflicting_existing_fingerprint_blocks_before_stage2(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    native = "204_CONFLICT"
    report, product = _write_execution_report(tmp_path, native=native)
    obs = _observation(db_session, native)
    frame = _frame(db_session, obs, uid="new-product-frame-uid")
    frame.raw_path = str(product)
    frame.header_json = {"stage1_fingerprint": "b" * 64}
    db_session.commit()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("conflict must be found before Stage 2")

    _install_fake_auto_process(monkeypatch, product, native=native,
                               frame_uid="new-product-frame-uid", process_call=forbidden)
    with pytest.raises(ValueError, match="fingerprint conflict"):
        handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    db_session.refresh(frame)
    assert frame.header_json["stage1_fingerprint"] == "b" * 64


def test_missing_second_frame_cannot_partially_attach_evidence(
    db_session: Session, tmp_path: Path
) -> None:
    report, product = _write_execution_report(tmp_path)
    valid = load_validated_stage1_products(report)[0]
    obs = _observation(db_session, valid.native_observation_id)
    frame = _frame(db_session, obs)
    frame.raw_path = str(product)
    db_session.commit()
    first = Stage1HandoffItem(valid, "SELECTED", "SELECTED_FOR_STAGE2", frame.frame_id, (), "")
    second = Stage1HandoffItem(
        replace(valid, product_path=tmp_path / "not-indexed.fits"),
        "SELECTED", "SELECTED_FOR_STAGE2", None, (), "",
    )
    with pytest.raises(ValueError, match="no unique exact Frame location"):
        _persist_selected_evidence(db_session, [first, second], [frame.frame_id])
    db_session.rollback()
    db_session.refresh(frame)
    assert "stage1_fingerprint" not in frame.header_json
    assert next(loc for loc in frame.locations if loc.locator == str(product)).content_sha256 is None


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

@pytest.mark.parametrize("threshold", [None, 20])
def test_new_observation_invokes_process_archive_path_with_exact_product_only(
    db_session: Session, tmp_path: Path, monkeypatch, threshold
) -> None:
    native = "204_NEW"
    report, product = _write_execution_report(tmp_path, native=native)
    captured = {}
    unrelated = _frame(db_session, _observation(db_session, "204_UNRELATED"), uid="unrelated")
    field = db_session.get(Field, _assign(db_session, unrelated).field_id)
    db_session.commit()

    class FakeProcessResult:
        def __init__(self, frame_id):
            self.frame_id = frame_id

        def to_dict(self):
            return {
                "status": "DONE",
                "execute": True,
                "discovered_files": 1,
                "detected_files": 1,
                "failed_files": 0,
                "warnings": [],
                "indexed_frame_ids": [self.frame_id],
                "index_results": [{"discovered": 1, "failed": 0}],
                "observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": [
                    {"action": "SELECTED", "selected_frame_id": self.frame_id},
                ]}],
                "created_series_ids": [series_id],
                "processed_series_ids": [series_id],
                "pipeline_results": [{"series_id": series_id, "status": "DONE", "science_status": "OK"}],
            }

    series_id = None

    def fake_process(_session, **kwargs):
        nonlocal series_id
        captured.update(kwargs)
        obs = _observation(_session, native)
        frame = _frame(_session, obs, uid="new-observation-product")
        frame.raw_path = str(product)
        _session.flush()
        series_id = _assign(_session, frame, field=field, series_uid="selected-series").series_id
        return FakeProcessResult(frame.frame_id)

    _install_fake_auto_process(
        monkeypatch,
        product,
        native=native,
        frame_uid="new-observation-product",
        process_call=fake_process,
    )
    kwargs = {} if threshold is None else {"ensemble_min_ref_stars_per_frame": threshold}
    result = handoff_stage1_execution(
        db_session, execution_report_path=report, execute=True,
        catalog_name="existing-catalog", instrument_profile="MASTER", **kwargs,
    )

    assert result.status == "READY"
    assert result.selected_items == 1
    assert result.retained_items == 0
    assert result.stage2_result["status"] == "DONE"
    assert result.stage2_result["processed_series_ids"] == [series_id]
    assert series_id != 1
    assert captured["exact_paths"] == [product]
    assert captured["recursive"] is False
    assert captured["catalog_name"] == "existing-catalog"
    assert captured["execute"] is True
    assert captured["ensemble_min_ref_stars_per_frame"] == (30 if threshold is None else 20)
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "new-observation-product"))
    assert frame.header_json["stage1_fingerprint"] == "a" * 64
    location = next(loc for loc in frame.locations if loc.locator == str(product))
    assert location.content_sha256 == _sha(product)
    reclaim = _validate_product(frame, location, tmp_path)
    assert reclaim["fingerprint_status"] == "VALID"
    assert reclaim["reason_code"] is None

    # A replay of the same physical observation does not attach evidence to
    # another Frame and cannot alter the already committed fingerprint.
    second = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert second.status == "READY"
    assert second.retained_items == 1
    assert frame.header_json["stage1_fingerprint"] == "a" * 64


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
    assert args.ensemble_min_ref_stars_per_frame == 30
    explicit = build_parser().parse_args([
        "stage1-handoff", "--execution-report", "execution.json",
        "--ensemble-min-ref-stars-per-frame", "20",
    ])
    assert explicit.ensemble_min_ref_stars_per_frame == 20


def test_stage1_handoff_cli_forwards_explicit_threshold_without_processing(monkeypatch) -> None:
    from unittest.mock import MagicMock

    import grandgousier.archive.stage1_handoff as handoff_module
    import grandgousier.cli as cli_module

    captured = {}
    session_factory = MagicMock()
    monkeypatch.setattr(cli_module, "make_session_factory", lambda _url: session_factory)

    def fake_handoff(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="READY", to_json=lambda **_kwargs: '{"status":"READY"}',
                               to_dict=lambda: {"status": "READY"},
                               selected_items=0, retained_items=0, blocked_items=0,
                               stage2_result=None)

    monkeypatch.setattr(handoff_module, "handoff_stage1_execution", fake_handoff)
    monkeypatch.setattr(handoff_module, "write_stage1_handoff_json", lambda _report, path: path)
    args = build_parser().parse_args([
        "stage1-handoff", "--execution-report", "execution.json",
        "--ensemble-min-ref-stars-per-frame", "20", "--format", "json",
    ])
    assert args.func(args) == 0
    assert captured["ensemble_min_ref_stars_per_frame"] == 20
    assert captured["execute"] is False


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


@pytest.mark.parametrize(
    "change",
    [
        {"status": "DONE_WITH_WARNINGS", "warnings": ["science incomplete"]},
        {"status": "FAILED"},
        {"pipeline_results": [], "processed_series_ids": []},
        {"pipeline_results": [{"series_id": 42, "status": "DONE_NO_SCIENCE_FRAMES", "science_status": "BAD_FRAME_QC"}]},
        {"pipeline_results": [{"series_id": 42, "status": "DONE", "science_status": "WARNING_NO_MEASUREMENTS"}]},
        {"index_results": [{"discovered": 1, "failed": 1}]},
        {"indexed_frame_ids": []},
        {"observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": []}]},
        {"created_series_ids": [], "processed_series_ids": [], "pipeline_results": []},
        {"pipeline_results": [{"series_id": 43, "status": "DONE", "science_status": "OK"}]},
        {"_remove_membership": True},
        {"_add_unselected_member": True},
    ],
)
def test_incomplete_nested_stage2_never_attaches_evidence(
    db_session: Session, tmp_path: Path, monkeypatch, change
) -> None:
    native = "204_PARTIAL"
    report, product = _write_execution_report(tmp_path, native=native)

    def fake_process(session, **_kwargs):
        obs = _observation(session, native)
        frame = _frame(session, obs, uid="partial-frame")
        frame.raw_path = str(product)
        series = _assign(session, frame)
        payload = {
            "execute": True, "status": "DONE", "warnings": [],
            "discovered_files": 1, "detected_files": 1, "failed_files": 0,
            "index_results": [{"discovered": 1, "failed": 0}],
            "indexed_frame_ids": [frame.frame_id],
            "observation_selection_results": [{"mode": "EXECUTE_PERSISTED_FRAMES", "rows": [
                {"action": "SELECTED", "selected_frame_id": frame.frame_id},
            ]}],
            "created_series_ids": [series.series_id], "processed_series_ids": [series.series_id],
            "pipeline_results": [{"series_id": series.series_id, "status": "DONE", "science_status": "OK"}],
        }
        if change.get("_remove_membership"):
            membership = session.scalar(select(FieldSeriesFrame).where(
                FieldSeriesFrame.series_id == series.series_id,
                FieldSeriesFrame.frame_id == frame.frame_id,
            ))
            session.delete(membership)
            session.flush()
        if change.get("_add_unselected_member"):
            extra = _frame(session, _observation(session, "204_EXTRA"), uid="extra-frame")
            session.add(FieldSeriesFrame(series_id=series.series_id, frame_id=extra.frame_id,
                                         order_index=2))
            session.flush()
        payload.update({key: ([{**row, "series_id": series.series_id} for row in value]
                             if key == "pipeline_results" and value and value[0]["series_id"] == 42
                             else value) for key, value in change.items() if not key.startswith("_")})
        return SimpleNamespace(to_dict=lambda: payload)

    _install_fake_auto_process(monkeypatch, product, native=native,
                               frame_uid="partial-frame", process_call=fake_process)
    outcome = handoff_stage1_execution(db_session, execution_report_path=report, execute=True)
    assert outcome.status == "FAILED"
    assert outcome.stage2_result is None
    frame = db_session.scalar(select(Frame).where(Frame.frame_uid == "partial-frame"))
    # Nested work may already have committed independently; the handoff never stamps it.
    if frame is not None:
        assert "stage1_fingerprint" not in (frame.header_json or {})
        assert next(loc for loc in frame.locations if loc.locator == str(product)).content_sha256 is None
