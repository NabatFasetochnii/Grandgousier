from __future__ import annotations

from copy import deepcopy

from grandgousier.pipeline.recipe_contracts import (
    MISSING_PROVENANCE,
    READY_STAGE3_RECIPE,
    RECIPE_MISMATCH,
    assess_stage2_recipe_json,
)
from grandgousier.orchestration.field_archive import _stage2_recipe_kwargs


def _recipe_json() -> dict:
    recipe = _stage2_recipe_kwargs("stage3_scale_v1")
    recipe.pop("enable_detection_branch")
    recipe.pop("enable_color_calibration")
    recipe.pop("target_max_sep_arcsec")
    recipe.pop("export_target_light_curve")
    recipe["color_calibration_requested"] = True
    recipe["recipe_version"] = "field_pipeline_v9"
    recipe["quality_policy"] = {"detection_branch_enabled": True}
    return recipe


def test_canonical_stage3_recipe_is_ready():
    result = assess_stage2_recipe_json(_recipe_json())
    assert result.status == READY_STAGE3_RECIPE
    assert result.mismatch_reasons == ()


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


def test_legacy_recipe_version_with_canonical_min_ref_20_is_compatible():
    recipe = _recipe_json()
    recipe["recipe_version"] = "field_pipeline_v8"
    result = assess_stage2_recipe_json(recipe)
    assert result.status == READY_STAGE3_RECIPE
    assert any("field_pipeline_v8" in note for note in result.compatibility_notes)


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
