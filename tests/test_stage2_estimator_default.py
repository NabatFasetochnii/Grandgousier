"""DB-free contract for new Stage-2 estimator selection (not photometric acceptance)."""

import ast
import inspect
from pathlib import Path

import pytest

from grandgousier.archive.auto_process import process_archive_path
from grandgousier.cli import build_parser
from grandgousier.photometry.forced_aperture import run_forced_aperture_photometry
from grandgousier.pipeline.recipe_contracts import assess_stage2_recipe_json
from grandgousier.pipeline.runner import FieldPipelineConfig


@pytest.mark.parametrize("command", ["run-field-pipeline", "process-archive-path", "run-forced-photometry"])
def test_cli_default_and_explicit_circular(command):
    parser = build_parser()
    # Parsers do not validate required arguments until parse_args; inspect the
    # registered action rather than constructing a DB-backed command invocation.
    subparsers = next(action for action in parser._actions if action.dest == "command")
    actions = subparsers.choices[command]._actions
    estimator = next(action for action in actions if action.dest == "photometry_estimator")
    assert estimator.default == "adaptive_ellipse"
    assert "circular" in estimator.choices
    assert estimator.type is None


def test_python_entrypoint_defaults_and_explicit_circular():
    config = FieldPipelineConfig(catalog_name="test", calibration_prefix="test")
    assert config.photometry_estimator == "adaptive_ellipse"
    assert FieldPipelineConfig(
        catalog_name="test", calibration_prefix="test", photometry_estimator="circular"
    ).photometry_estimator == "circular"
    for function in (process_archive_path, run_forced_aperture_photometry):
        parameter = inspect.signature(function).parameters["photometry_estimator"]
        assert parameter.default == "adaptive_ellipse"
        assert parameter.kind == inspect.Parameter.KEYWORD_ONLY


def test_handoff_inherits_archive_default_without_overriding_it():
    source = Path(__file__).parents[1] / "grandgousier/archive/stage1_handoff.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    handoff = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                   and node.name == "handoff_stage1_execution")
    calls = [node for node in ast.walk(handoff) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "process_archive_path"]
    assert len(calls) == 1
    assert "photometry_estimator" not in {kw.arg for kw in calls[0].keywords}
    assert inspect.signature(process_archive_path).parameters["photometry_estimator"].default == "adaptive_ellipse"


def test_generic_adaptive_default_does_not_make_stage3_recipe_ready():
    recipe = {"photometry_estimator": FieldPipelineConfig(
        catalog_name="test", calibration_prefix="test"
    ).photometry_estimator}
    result = assess_stage2_recipe_json(recipe)
    assert result.status != "READY_STAGE3_RECIPE"
    assert result.mismatch_reasons


def test_pipeline_recipe_lineage_captures_effective_estimator():
    from dataclasses import asdict

    default = FieldPipelineConfig(catalog_name="test", calibration_prefix="test")
    circular = FieldPipelineConfig(
        catalog_name="test", calibration_prefix="test", photometry_estimator="circular"
    )
    assert asdict(default)["photometry_estimator"] == "adaptive_ellipse"
    assert asdict(circular)["photometry_estimator"] == "circular"
    source = Path(__file__).parents[1] / "grandgousier/pipeline/runner.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    runner = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "run_field_pipeline")
    assert any(isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "recipe" for target in node.targets
    ) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_to_jsonable"
        and isinstance(node.value.args[0], ast.Call)
        and isinstance(node.value.args[0].func, ast.Name)
        and node.value.args[0].func.id == "asdict" for node in ast.walk(runner))
