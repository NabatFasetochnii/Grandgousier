import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.archive.stage1_execution import STAGE1_PROVENANCE_SCHEMA
from grandgousier.archive.stage1_reclaim import _inside, _pipeline_state, _validate_product
from grandgousier.cli import build_parser


def test_reclaim_cli_defaults_to_plan_mode():
    args = build_parser().parse_args(
        [
            "reclaim-stage1-products",
            "--series-id",
            "42",
            "--processed-root",
            "processed",
            "--manifest-root",
            "lifecycle",
        ]
    )

    assert args.series_id == 42
    assert args.execute is False
    assert args.processed_root == "processed"


def test_reclaim_root_guard_rejects_escape():
    root = Path("processed").resolve()
    assert _inside(root / "nested" / "product.fits", root)
    assert not _inside(root.parent / "product.fits", root)


def _fixture_product(tmp_path: Path, **overrides):
    content = b"reclaim-test-product"
    uid = "source-observation-1"
    product = tmp_path / f"{uid}_processed.fits"
    product.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    fingerprint = "f" * 64
    payload = {
        "schema": STAGE1_PROVENANCE_SCHEMA,
        "fingerprint": fingerprint,
        "observation": {
            "source_observation_uid": uid,
            "observation_uid": "observation-1",
            "native_observation_id": "native-1",
        },
        "product": {
            "locator": product.name,
            "checksum_sha256": checksum,
            "size_bytes": len(content),
        },
    }
    payload.update(overrides)
    sidecar = product.with_name(product.name + ".provenance.json")
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    frame = SimpleNamespace(
        frame_id=1,
        observation_id=1,
        observation=SimpleNamespace(observation_uid="observation-1", native_identity="native-1"),
        header_json={"stage1_fingerprint": fingerprint},
        quality_flags={},
    )
    location = SimpleNamespace(locator=str(product), content_sha256=checksum)
    return product, sidecar, frame, location


def test_valid_provenance_and_identity_pass(tmp_path: Path):
    product, sidecar, frame, location = _fixture_product(tmp_path)

    row = _validate_product(frame, location, tmp_path)

    assert row["product_exists"] is True
    assert row["sidecar_status"] == "VALID"
    assert row["identity_status"] == "VALID"
    assert row["fingerprint_status"] == "VALID"
    assert row["checksum_status"] == "VALID"
    assert row["reason_code"] is None
    assert sidecar.is_file() and product.is_file()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda payload: payload.update(schema="wrong"), "SIDECAR_SCHEMA_INVALID"),
        (lambda payload: payload.update(fingerprint="bad"), "FINGERPRINT_MISMATCH"),
        (lambda payload: payload["observation"].update(observation_uid="wrong"), "IDENTITY_MISMATCH"),
        (lambda payload: payload["product"].update(size_bytes=999), "PRODUCT_SIZE_MISMATCH"),
    ],
)
def test_invalid_provenance_is_blocked(tmp_path: Path, mutation, reason: str):
    product, sidecar, frame, location = _fixture_product(tmp_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    mutation(payload)
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    row = _validate_product(frame, location, tmp_path)

    assert row["classification"] == "BLOCKED"
    assert row["reason_code"] == reason
    assert product.is_file()


def test_missing_sidecar_is_blocked(tmp_path: Path):
    product, sidecar, frame, location = _fixture_product(tmp_path)
    sidecar.unlink()

    row = _validate_product(frame, location, tmp_path)

    assert row["reason_code"] == "SIDECAR_MISSING"
    assert product.is_file()


def test_missing_product_without_lifecycle_evidence_is_unknown(tmp_path: Path):
    product, _, frame, location = _fixture_product(tmp_path)
    product.unlink()

    row = _validate_product(frame, location, tmp_path, lifecycle=None)

    assert row["reason_code"] == "UNKNOWN_MISSING_PRODUCT"
    assert row["classification"] == "BLOCKED"


def test_missing_product_with_intent_is_already_reclaimed(tmp_path: Path):
    product, _, frame, location = _fixture_product(tmp_path)
    product.unlink()

    row = _validate_product(frame, location, tmp_path, lifecycle="INTENT")

    assert row["classification"] == "ALREADY_RECLAIMED"
    assert row["reason_code"] is None


def test_directory_product_is_blocked(tmp_path: Path):
    product, sidecar, frame, location = _fixture_product(tmp_path)
    product.unlink()
    sidecar.unlink()
    product.mkdir()

    row = _validate_product(frame, location, tmp_path)

    assert row["reason_code"] == "PRODUCT_NOT_REGULAR_FILE"


def test_product_symlink_is_blocked(tmp_path: Path):
    product, _, frame, location = _fixture_product(tmp_path)
    target = tmp_path / "target.fits"
    target.write_bytes(product.read_bytes())
    product.unlink()
    try:
        product.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    row = _validate_product(frame, location, tmp_path)

    assert row["reason_code"] == "PATH_IS_SYMLINK"


class _PipelineSession:
    def __init__(self, run, steps=()):
        self.run = run
        self.steps = list(steps)

    def scalar(self, _statement):
        return self.run

    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.steps)


@pytest.mark.parametrize(
    ("run_status", "step_status", "expected"),
    [
        ("RUNNING", "DONE", ("ACTIVE", ("ACTIVE_PIPELINE_RUN",))),
        ("DONE", "RUNNING", ("INVALID", ("ACTIVE_PIPELINE_STEP",))),
        ("DONE", "FAILED", ("INVALID", ("FAILED_PIPELINE_STEP",))),
        ("DONE", "DONE", ("DONE", ())),
    ],
)
def test_pipeline_state_guards(run_status: str, step_status: str, expected):
    run = SimpleNamespace(pipeline_run_id=7, status=run_status)
    step = SimpleNamespace(status=step_status)

    state, reasons, run_id = _pipeline_state(_PipelineSession(run, [step]), 42)

    assert (state, reasons) == expected
    assert run_id == 7


def test_pipeline_without_run_is_not_done():
    state, reasons, run_id = _pipeline_state(_PipelineSession(None), 42)

    assert (state, reasons, run_id) == ("NOT_DONE", ("PIPELINE_NOT_DONE",), None)
