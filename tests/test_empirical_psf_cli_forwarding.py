import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _function(path: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _call_keywords(function: ast.FunctionDef, func_name: str) -> dict[str, ast.AST]:
    call = next(
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name
    )
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


EXPECTED = {
    "photometry_estimator",
    "empirical_psf_grid_size",
    "empirical_psf_stamp_radius_pix",
    "empirical_psf_reference_radius_pix",
    "empirical_psf_references_per_node",
    "empirical_psf_min_references_per_node",
    "empirical_psf_fallback_to_adaptive",
}


def test_process_archive_cli_forwards_empirical_psf_options():
    function = _function(ROOT / "grandgousier" / "cli.py", "cmd_process_archive_path")
    keywords = _call_keywords(function, "process_archive_path")
    for name in EXPECTED:
        value = keywords[name]
        assert isinstance(value, ast.Attribute)
        assert isinstance(value.value, ast.Name)
        assert value.value.id == "args"
        assert value.attr == name


def test_process_archive_function_exposes_empirical_psf_options():
    function = _function(ROOT / "grandgousier" / "archive" / "auto_process.py", "process_archive_path")
    names = {arg.arg for arg in function.args.kwonlyargs}
    assert EXPECTED <= names


def test_pipeline_forwards_empirical_psf_options_to_base_photometry():
    function = _function(ROOT / "grandgousier" / "pipeline" / "runner.py", "run_field_pipeline")
    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_forced_aperture_photometry"
    ]
    assert calls
    keywords = {kw.arg: kw.value for kw in calls[0].keywords if kw.arg}
    for name in EXPECTED:
        assert name in keywords
