from __future__ import annotations

from copy import deepcopy

from grandgousier.astrometry.gaia_wcs_com import (
    GAIA_WCS_COM_ALGORITHM,
    GAIA_WCS_COM_POLICY,
)
from grandgousier.cli import build_parser
from grandgousier.orchestration.field_archive import _stage2_recipe_kwargs
from grandgousier.pipeline.recipe_contracts import (
    MISSING_PROVENANCE,
    READY_STAGE3_RECIPE,
    RECIPE_MISMATCH,
    assess_stage2_recipe_json,
)


def _recipe_json() -> dict:
    recipe = _stage2_recipe_kwargs("stage3_scale_v1")
    recipe.pop("enable_detection_branch")
    recipe.pop("enable_color_calibration")
    recipe.pop("target_max_sep_arcsec")
    recipe.pop("export_target_light_curve")
    recipe["color_calibration_requested"] = True
    recipe["recipe_version"] = "field_pipeline_v10"
    recipe["quality_policy"] = {
        "detection_branch_enabled": True,
        "astrometric_centering": {
            **GAIA_WCS_COM_POLICY,
            "fwhm_header_unit": "arcsec",
            "neighbor_completeness_proven": False,
        },
    }
    recipe["astrometric_centering_algorithm"] = GAIA_WCS_COM_ALGORITHM
    return recipe


def test_canonical_stage3_recipe_is_ready():
    result = assess_stage2_recipe_json(_recipe_json())
    assert result.status == READY_STAGE3_RECIPE
    assert result.mismatch_reasons == ()


def test_stage2_recipe_kwargs_include_approved_centering_and_qc_defaults():
    kwargs = _stage2_recipe_kwargs("stage3_scale_v1")
    expected = {
        "astrometric_centering_algorithm": GAIA_WCS_COM_ALGORITHM,
        "local_com_max_shift_pix": 2.0,
        "frame_shift_min_matches": 20,
        "frame_shift_max_robust_scatter_pix": 0.5,
        "frame_shift_max_absolute_pix": 2.0,
        "crowded_arcsec": GAIA_WCS_COM_POLICY["crowded_arcsec"],
        "blended_arcsec": GAIA_WCS_COM_POLICY["blended_arcsec"],
        "dynamic_separation": GAIA_WCS_COM_POLICY["dynamic_separation"],
        "dynamic_seeing_factor": GAIA_WCS_COM_POLICY["dynamic_seeing_factor"],
        "dynamic_aperture_factor": GAIA_WCS_COM_POLICY["dynamic_aperture_factor"],
        "dynamic_crowding_factor": GAIA_WCS_COM_POLICY["dynamic_crowding_factor"],
        "fwhm_header_unit": GAIA_WCS_COM_POLICY["fwhm_header_unit"],
    }
    assert {key: kwargs[key] for key in expected} == expected


def test_centering_policy_cli_options_are_available_with_canonical_defaults():
    parser = build_parser()
    commands = (
        ("run-field-pipeline", ["--catalog-name", "gaia_dr3", "--calibration-prefix", "test"]),
        ("run-archive-pipeline", ["--catalog-name", "gaia_dr3"]),
        ("process-archive-path", ["--path", "."]),
    )
    options = [
        "--local-com-max-shift-pix",
        "1.25",
        "--frame-shift-min-matches",
        "24",
        "--frame-shift-max-robust-scatter-pix",
        "0.3",
        "--frame-shift-max-absolute-pix",
        "1.5",
        "--crowded-arcsec",
        "12",
        "--blended-arcsec",
        "6",
        "--disable-dynamic-separation",
        "--dynamic-seeing-factor",
        "1.7",
        "--dynamic-aperture-factor",
        "0.8",
        "--dynamic-crowding-factor",
        "1.3",
        "--fwhm-header-unit",
        "pix",
    ]
    for command, base_args in commands:
        defaults = parser.parse_args([command, *base_args])
        assert defaults.local_com_max_shift_pix == 2.0
        assert defaults.frame_shift_min_matches == 20
        assert defaults.frame_shift_max_robust_scatter_pix == 0.5
        assert defaults.frame_shift_max_absolute_pix == 2.0
        assert defaults.crowded_arcsec == GAIA_WCS_COM_POLICY["crowded_arcsec"]
        assert defaults.blended_arcsec == GAIA_WCS_COM_POLICY["blended_arcsec"]
        assert defaults.dynamic_separation is True

        configured = parser.parse_args([command, *base_args, *options])
        assert configured.local_com_max_shift_pix == 1.25
        assert configured.frame_shift_min_matches == 24
        assert configured.frame_shift_max_robust_scatter_pix == 0.3
        assert configured.frame_shift_max_absolute_pix == 1.5
        assert configured.crowded_arcsec == 12.0
        assert configured.blended_arcsec == 6.0
        assert configured.dynamic_separation is False
        assert configured.dynamic_seeing_factor == 1.7
        assert configured.dynamic_aperture_factor == 0.8
        assert configured.dynamic_crowding_factor == 1.3
        assert configured.fwhm_header_unit == "pix"


def test_generic_done_with_circular_photometry_and_detection_off_is_mismatch():
    recipe = _recipe_json()
    recipe["photometry_estimator"] = "circular"
    recipe["quality_policy"]["detection_branch_enabled"] = False
    result = assess_stage2_recipe_json(recipe)
    assert result.status == RECIPE_MISMATCH
    assert any("photometry_estimator" in reason for reason in result.mismatch_reasons)
    assert any("detection_branch_enabled" in reason for reason in result.mismatch_reasons)


def test_previous_canonical_min_ref_30_is_compatible_but_reported():
    recipe = _recipe_json()
    recipe["ensemble_min_ref_stars_per_frame"] = 30
    result = assess_stage2_recipe_json(recipe)
    assert result.status == READY_STAGE3_RECIPE
    assert any("canonical=20,compatible_previous=30" in note for note in result.compatibility_notes)


def test_previous_recipe_version_is_not_equivalent_to_gaia_wcs_com_v1():
    recipe = _recipe_json()
    recipe["recipe_version"] = "field_pipeline_v9"
    result = assess_stage2_recipe_json(recipe)
    assert result.status == RECIPE_MISMATCH
    assert any("recipe_version:expected=field_pipeline_v10" in reason for reason in result.mismatch_reasons)


def test_centering_policy_change_is_recipe_mismatch():
    recipe = _recipe_json()
    recipe["quality_policy"]["astrometric_centering"]["frame_shift_min_matches"] = 19
    result = assess_stage2_recipe_json(recipe)
    assert result.status == RECIPE_MISMATCH
    assert any("frame_shift_min_matches" in reason for reason in result.mismatch_reasons)


def test_missing_legacy_provenance_is_not_guessed_from_partial_recipe():
    recipe = _recipe_json()
    recipe.pop("photometry_estimator")
    recipe.pop("enable_photometric_frame_qc")
    result = assess_stage2_recipe_json(recipe)
    assert result.status == MISSING_PROVENANCE
    assert "missing:photometry_estimator" in result.mismatch_reasons
    assert "missing:enable_photometric_frame_qc" in result.mismatch_reasons


def test_color_activation_outcome_is_not_part_of_recipe_compatibility():
    recipe = _recipe_json()
    first = assess_stage2_recipe_json(recipe)
    second_recipe = deepcopy(recipe)
    second_recipe["color_calibration_activated"] = False
    second_recipe["color_transform_quality_gate_passed"] = False
    second = assess_stage2_recipe_json(second_recipe)
    assert first.status == READY_STAGE3_RECIPE
    assert second.status == READY_STAGE3_RECIPE
    assert second.mismatch_reasons == ()


def test_unvalidated_ensemble_reference_threshold_is_mismatch():
    recipe = _recipe_json()
    recipe["ensemble_min_ref_stars_per_frame"] = 25
    result = assess_stage2_recipe_json(recipe)
    assert result.status == RECIPE_MISMATCH
    assert any("expected_one_of=20|30" in reason for reason in result.mismatch_reasons)
