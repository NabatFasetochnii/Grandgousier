import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]

EXPECTED = {
    "enable_spatial_ensemble",
    "spatial_ensemble_max_order",
    "spatial_ensemble_min_ref_stars_per_frame",
    "spatial_ensemble_clip_sigma",
    "spatial_ensemble_n_iterations",
    "spatial_ensemble_min_bic_improvement",
    "spatial_ensemble_min_spatial_peak_to_peak",
    "spatial_ensemble_max_residual_mad",
    "spatial_ensemble_max_residual_mad_degradation",
    "spatial_ensemble_max_abs_spatial_deviation",
    "spatial_ensemble_min_good_frame_fraction",
}


def _function(path: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _call_keywords(function: ast.FunctionDef, func_name: str) -> dict[str, ast.AST]:
    call = next(
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name
    )
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def test_process_archive_cli_forwards_spatial_ensemble_options():
    function = _function(ROOT / "grandgousier" / "cli.py", "cmd_process_archive_path")
    keywords = _call_keywords(function, "process_archive_path")
    assert EXPECTED <= set(keywords)


def test_process_archive_function_exposes_spatial_ensemble_options():
    function = _function(ROOT / "grandgousier" / "archive" / "auto_process.py", "process_archive_path")
    names = {arg.arg for arg in function.args.kwonlyargs}
    assert EXPECTED <= names


def test_auto_process_forwards_spatial_ensemble_options_to_pipeline_config():
    function = _function(ROOT / "grandgousier" / "archive" / "auto_process.py", "process_archive_path")
    keywords = _call_keywords(function, "FieldPipelineConfig")
    assert EXPECTED <= set(keywords)


def test_runner_calls_spatial_ensemble_correction():
    function = _function(ROOT / "grandgousier" / "pipeline" / "runner.py", "run_field_pipeline")
    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_spatial_ensemble_correction"
    ]
    assert calls
    keywords = {kw.arg for kw in calls[0].keywords if kw.arg}
    expected_runtime = {
        "max_order",
        "min_ref_stars_per_frame",
        "clip_sigma",
        "n_iterations",
        "min_bic_improvement",
        "min_spatial_peak_to_peak",
        "max_residual_mad",
        "max_residual_mad_degradation",
        "max_abs_spatial_deviation",
        "min_good_frame_fraction",
    }
    assert expected_runtime <= keywords
