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


def test_process_archive_cli_forwards_spatial_psf_options():
    function = _function(ROOT / "grandgousier" / "cli.py", "cmd_process_archive_path")
    keywords = _call_keywords(function, "process_archive_path")
    expected = {
        "photometry_estimator",
        "psf_profile",
        "psf_moffat_beta",
        "psf_fit_radius_fwhm",
        "psf_min_fit_radius_pix",
        "psf_max_fit_radius_pix",
        "psf_centroid_search_radius_pix",
        "psf_centroid_grid_steps",
        "psf_residual_clip_sigma",
        "psf_residual_clip_iterations",
        "psf_fallback_to_adaptive",
    }
    for name in expected:
        value = keywords[name]
        assert isinstance(value, ast.Attribute)
        assert isinstance(value.value, ast.Name)
        assert value.value.id == "args"
        assert value.attr == name


def test_process_archive_function_exposes_spatial_psf_options():
    function = _function(ROOT / "grandgousier" / "archive" / "auto_process.py", "process_archive_path")
    names = {arg.arg for arg in function.args.kwonlyargs}
    assert {
        "photometry_estimator",
        "psf_profile",
        "psf_moffat_beta",
        "psf_fit_radius_fwhm",
        "psf_min_fit_radius_pix",
        "psf_max_fit_radius_pix",
        "psf_centroid_search_radius_pix",
        "psf_centroid_grid_steps",
        "psf_residual_clip_sigma",
        "psf_residual_clip_iterations",
        "psf_fallback_to_adaptive",
    } <= names


def test_pipeline_forwards_spatial_psf_options_to_base_photometry():
    function = _function(ROOT / "grandgousier" / "pipeline" / "runner.py", "run_field_pipeline")
    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_forced_aperture_photometry"
    ]
    assert calls
    keywords = {kw.arg: kw.value for kw in calls[0].keywords if kw.arg}
    for name in (
        "photometry_estimator",
        "psf_profile",
        "psf_moffat_beta",
        "psf_fit_radius_fwhm",
        "psf_centroid_search_radius_pix",
        "psf_fallback_to_adaptive",
    ):
        assert name in keywords
