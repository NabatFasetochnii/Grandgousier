from __future__ import annotations

import ast
from pathlib import Path

from grandgousier.calibration.spatial_ensemble import SPATIAL_ENSEMBLE_ALGORITHM

ROOT = Path(__file__).parents[1]


def _function(path: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_spatial_ensemble_base_is_published_before_spatial_read():
    runner = _function(ROOT / "grandgousier" / "pipeline" / "runner.py", "run_field_pipeline")
    helper = next(
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_ensemble_with_optional_spatial"
    )

    stamp_calls = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_stamp_product"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "base_name"
    ]
    spatial_calls = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_spatial_ensemble_correction"
    ]

    assert len(stamp_calls) == 1
    assert len(spatial_calls) == 1
    stamp = stamp_calls[0]
    spatial = spatial_calls[0]
    assert stamp.lineno < spatial.lineno

    keywords = {kw.arg: kw.value for kw in stamp.keywords if kw.arg}
    assert isinstance(keywords["materialization_kind"], ast.Constant)
    assert keywords["materialization_kind"].value == "NORMALIZED_TERMS"
    cv = keywords["calibration_version_id"]
    assert isinstance(cv, ast.Attribute)
    assert isinstance(cv.value, ast.Name) and cv.value.id == "base_result"
    assert cv.attr == "target_calibration_version_id"


def test_spatial_ensemble_retry_uses_new_product_generation_suffix():
    runner_text = (ROOT / "grandgousier" / "pipeline" / "runner.py").read_text(encoding="utf-8")
    assert "_ensemble_{config.mag_key.lower()}_spatial_v3" in runner_text
    assert SPATIAL_ENSEMBLE_ALGORITHM == "robust_zero_mean_spatial_residual_v3"


def test_spatial_ensemble_pipeline_passes_immutable_generation_flag():
    runner = _function(ROOT / "grandgousier" / "pipeline" / "runner.py", "run_field_pipeline")
    helper = next(
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_ensemble_with_optional_spatial"
    )
    spatial_call = next(
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_spatial_ensemble_correction"
    )
    keywords = {kw.arg: kw.value for kw in spatial_call.keywords if kw.arg}
    immutable = keywords["immutable_generation"]
    assert isinstance(immutable, ast.Call)
    assert isinstance(immutable.func, ast.Name)
    assert immutable.func.id == "bool"


def test_spatial_target_uses_shared_immutable_generation_writer():
    helper = _function(
        ROOT / "grandgousier" / "calibration" / "spatial_ensemble.py",
        "_get_or_create_target_calibration",
    )
    calls = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "prepare_calibration_version_for_write"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: kw.value for kw in calls[0].keywords if kw.arg}
    assert isinstance(keywords["logical_name"], ast.Name)
    assert keywords["logical_name"].id == "name"
    assert isinstance(keywords["immutable_generation"], ast.Constant)
    assert keywords["immutable_generation"].value is True


def test_spatial_immutable_retry_reuses_unfinished_logical_row_with_new_source_generation():
    from grandgousier.calibration.spatial_ensemble import _get_or_create_target_calibration
    from grandgousier.db.models import CalibrationVersion

    source = CalibrationVersion(
        calibration_version_id=202,
        name="ordinary_ensemble_new_generation",
        model_name="ensemble_differential_v1",
        used_catalogs={},
        parameters={},
    )
    unfinished = CalibrationVersion(
        calibration_version_id=303,
        name="spatial_logical",
        logical_name="spatial_logical",
        generation_no=1,
        lifecycle_status="MATERIALIZING",
        model_name="robust_zero_mean_spatial_residual_v3",
        used_catalogs={},
        parameters={
            "source_calibration_name": "ordinary_ensemble_old_generation",
            "source_calibration_version_id": 201,
            "algorithm": "robust_zero_mean_spatial_residual_v3",
        },
    )

    class Session:
        def __init__(self):
            self.flush_count = 0

        def scalar(self, _statement):
            return unfinished

        def add(self, _value):
            raise AssertionError("unfinished generation should be reused, not duplicated")

        def flush(self):
            self.flush_count += 1

    session = Session()
    result = _get_or_create_target_calibration(
        session,  # type: ignore[arg-type]
        name="spatial_logical",
        source_cv=source,
        instrument_unit_id=7,
        parameters={"algorithm": "robust_zero_mean_spatial_residual_v3", "max_order": 2},
        dry_run=False,
        immutable_generation=True,
    )

    assert result is unfinished
    assert unfinished.source_calibration_version_id == 202
    assert unfinished.parameters["source_calibration_version_id"] == 202
    assert unfinished.parameters["source_calibration_name"] == source.name
    assert unfinished.parameters["max_order"] == 2
    assert unfinished.lifecycle_status == "MATERIALIZING"
    assert session.flush_count == 1


def test_spatial_immutable_rerun_allocates_new_generation_after_completed_head():
    from grandgousier.calibration.spatial_ensemble import _get_or_create_target_calibration
    from grandgousier.db.models import CalibrationVersion

    source = CalibrationVersion(
        calibration_version_id=402,
        name="ordinary_ensemble_generation_2",
        model_name="ensemble_differential_v1",
        used_catalogs={},
        parameters={},
    )
    completed = CalibrationVersion(
        calibration_version_id=501,
        name="spatial_logical",
        logical_name="spatial_logical",
        generation_no=1,
        lifecycle_status="COMPLETE_IMMUTABLE",
        model_name="robust_zero_mean_spatial_residual_v3",
        used_catalogs={},
        parameters={"source_calibration_version_id": 401},
    )

    class Session:
        def __init__(self):
            self.scalars = [completed, 1]
            self.added = []
            self.flush_count = 0

        def scalar(self, _statement):
            return self.scalars.pop(0)

        def add(self, value):
            self.added.append(value)

        def flush(self):
            self.flush_count += 1

    session = Session()
    result = _get_or_create_target_calibration(
        session,  # type: ignore[arg-type]
        name="spatial_logical",
        source_cv=source,
        instrument_unit_id=7,
        parameters={"algorithm": "robust_zero_mean_spatial_residual_v3", "max_order": 2},
        dry_run=False,
        immutable_generation=True,
    )

    assert result is not completed
    assert result.logical_name == "spatial_logical"
    assert result.generation_no == 2
    assert result.lifecycle_status == "MATERIALIZING"
    assert result.supersedes_calibration_version_id == 501
    assert result.source_calibration_version_id == 402
    assert session.added == [result]
