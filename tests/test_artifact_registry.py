from __future__ import annotations

from pathlib import Path

from grandgousier.artifacts.registry import (
    capture_tree_state,
    classify_export_artifact,
    file_sha256,
    run_artifact_directory,
)


def test_file_sha256_is_content_based(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('{"x": 1}', encoding="utf-8")
    digest1 = file_sha256(path)
    path.write_text('{"x": 2}', encoding="utf-8")
    digest2 = file_sha256(path)
    assert len(digest1) == 64
    assert digest1 != digest2


def test_capture_tree_state_detects_changed_publication_file(tmp_path: Path) -> None:
    root = tmp_path / "exports" / "series"
    root.mkdir(parents=True)
    path = root / "report.json"
    path.write_text("old", encoding="utf-8")
    before = capture_tree_state(root)
    path.write_text("new content", encoding="utf-8")
    after = capture_tree_state(root)
    assert before["report.json"] != after["report.json"]


def test_run_artifact_directory_is_sibling_runs_tree_and_windows_safe(tmp_path: Path) -> None:
    publication_root = tmp_path / "exports" / "science_prefix"
    run_dir = run_artifact_directory(
        publication_root,
        run_uid="field:science_prefix:20260812T220000Z",
        pipeline_run_id=42,
    )
    assert run_dir.parent == tmp_path / "exports" / "runs"
    assert run_dir.name.startswith("42_field_science_prefix_")
    assert ":" not in run_dir.name


def test_export_artifact_classification_covers_science_and_diagnostics() -> None:
    assert classify_export_artifact("target_light_curve.json") == ("TARGET_LIGHT_CURVE", "SCIENCE")
    assert classify_export_artifact("candidates/object_1_light_curve.csv") == (
        "CANDIDATE_PRODUCT",
        "SCIENCE_DIAGNOSTIC",
    )
    assert classify_export_artifact("aperture_correction/aperture_correction_frames.csv") == (
        "APERTURE_CORRECTION_DIAGNOSTIC",
        "CALIBRATION_DIAGNOSTIC",
    )
