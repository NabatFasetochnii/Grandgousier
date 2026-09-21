import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _function(path: Path, name: str) -> ast.FunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_process_archive_cli_forwards_adaptive_estimator_options():
    function = _function(ROOT / "grandgousier" / "cli.py", "cmd_process_archive_path")
    call = next(
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "process_archive_path"
    )
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    expected = {
        "photometry_estimator": "photometry_estimator",
        "adaptive_min_psf_snr": "adaptive_min_psf_snr",
        "adaptive_max_psf_sources_per_frame": "adaptive_max_psf_sources_per_frame",
        "adaptive_psf_cutout_radius_pix": "adaptive_psf_cutout_radius_pix",
        "adaptive_psf_min_isolation_pix": "adaptive_psf_min_isolation_pix",
        "adaptive_interpolation_neighbors": "adaptive_interpolation_neighbors",
        "adaptive_min_psf_references": "adaptive_min_psf_references",
        "adaptive_aperture_fwhm_factor": "adaptive_aperture_fwhm_factor",
        "adaptive_max_axis_ratio": "adaptive_max_axis_ratio",
        "adaptive_max_radius_scale": "adaptive_max_radius_scale",
        "adaptive_fallback_to_circular": "adaptive_fallback_to_circular",
    }
    for keyword, attribute in expected.items():
        value = keywords[keyword]
        assert isinstance(value, ast.Attribute)
        assert isinstance(value.value, ast.Name)
        assert value.value.id == "args"
        assert value.attr == attribute


def test_process_archive_function_exposes_adaptive_estimator_options():
    function = _function(
        ROOT / "grandgousier" / "archive" / "auto_process.py",
        "process_archive_path",
    )
    names = {arg.arg for arg in function.args.kwonlyargs}
    assert {
        "photometry_estimator",
        "adaptive_min_psf_snr",
        "adaptive_max_psf_sources_per_frame",
        "adaptive_psf_cutout_radius_pix",
        "adaptive_psf_min_isolation_pix",
        "adaptive_interpolation_neighbors",
        "adaptive_min_psf_references",
        "adaptive_aperture_fwhm_factor",
        "adaptive_max_axis_ratio",
        "adaptive_max_radius_scale",
        "adaptive_fallback_to_circular",
    } <= names


def test_process_archive_summary_exposes_effective_photometry_estimator():
    source = (ROOT / "grandgousier" / "archive" / "auto_process.py").read_text(encoding="utf-8")
    assert '"metric": "photometry_estimator"' in source
    assert '.get("photometry_estimator")' in source
