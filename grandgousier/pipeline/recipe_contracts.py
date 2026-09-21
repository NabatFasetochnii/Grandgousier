from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


STAGE2_RECIPE_STAGE3_SCALE_V1 = "stage3_scale_v1"
SUPPORTED_STAGE2_RECIPES = (STAGE2_RECIPE_STAGE3_SCALE_V1,)

READY_STAGE3_RECIPE = "READY_STAGE3_RECIPE"
RECIPE_MISMATCH = "RECIPE_MISMATCH"
MISSING_PROVENANCE = "MISSING_PROVENANCE"


_STAGE3_SCALE_V1_PROCESS_KWARGS: dict[str, Any] = {
    "photometry_estimator": "adaptive_ellipse",
    "aperture_correction_plane_policy": "evidence",
    "working_catalog_mag_limit": 19.0,
    "max_working_sources_per_frame": 2000,
    "catalog_edge_margin_pix": 20.0,
    "exclude_bad_pre_qc_frames": True,
    "enable_detection_branch": True,
    "enable_psf_field_diagnostics": True,
    "ensemble_min_ref_stars_per_frame": 20,
    "exclude_candidates_from_ensemble": True,
    "candidate_ensemble_until_converged": True,
    "candidate_ensemble_convergence_max_iterations": 10,
    "candidate_ensemble_growth_policy": "initial_only",
    "enable_photometric_frame_qc": True,
    "enable_color_calibration": True,
    "target_max_sep_arcsec": -1.0,
    "export_target_light_curve": False,
    "enable_spatial_ensemble": False,
}


@dataclass(frozen=True)
class Stage2RecipeCompatibility:
    required_recipe: str
    status: str
    mismatch_reasons: tuple[str, ...] = ()
    compatibility_notes: tuple[str, ...] = ()
    provenance_source: str = "pipeline_runs.recipe_json"

    @property
    def ready(self) -> bool:
        return self.status == READY_STAGE3_RECIPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_recipe": self.required_recipe,
            "status": self.status,
            "mismatch_reasons": list(self.mismatch_reasons),
            "compatibility_notes": list(self.compatibility_notes),
            "provenance_source": self.provenance_source,
        }


def stage2_recipe_process_kwargs(recipe: str) -> dict[str, Any]:
    if recipe != STAGE2_RECIPE_STAGE3_SCALE_V1:
        raise ValueError(f"Unsupported Stage-2 recipe: {recipe}")
    return dict(_STAGE3_SCALE_V1_PROCESS_KWARGS)


def assess_stage2_recipe_json(
    recipe_json: Mapping[str, Any] | None,
    *,
    required_recipe: str = STAGE2_RECIPE_STAGE3_SCALE_V1,
) -> Stage2RecipeCompatibility:
    """Classify persisted Stage-2 provenance for Stage-3 use.

    The contract deliberately compares only scientific settings that determine
    whether a published SCIENCE product is compatible with the validated
    Stage-3 scale workflow. It does not compare paths, run ids, output options,
    calibration names, frame lists, or quality-gate outcomes.

    ``stage3_scale_v1`` emits ``ensemble_min_ref_stars_per_frame=20``.
    The GPX_TF17_2 production acceptance includes a real 29-frame V series with
    22 usable references, so 20 is the validated archive-wide threshold. Runs
    already produced with the previous canonical value 30 remain compatible;
    compatibility therefore accepts exactly the validated values {20, 30} and
    reports 30 as a compatibility note rather than silently normalizing it.
    """

    if required_recipe != STAGE2_RECIPE_STAGE3_SCALE_V1:
        raise ValueError(f"Unsupported Stage-2 recipe: {required_recipe}")
    if not isinstance(recipe_json, Mapping) or not recipe_json:
        return Stage2RecipeCompatibility(
            required_recipe=required_recipe,
            status=MISSING_PROVENANCE,
            mismatch_reasons=("missing:pipeline_runs.recipe_json",),
        )

    quality_policy = recipe_json.get("quality_policy")
    if not isinstance(quality_policy, Mapping):
        quality_policy = {}

    required_values: tuple[tuple[str, Any, Any], ...] = (
        ("photometry_estimator", recipe_json.get("photometry_estimator"), "adaptive_ellipse"),
        (
            "aperture_correction_plane_policy",
            recipe_json.get("aperture_correction_plane_policy"),
            "evidence",
        ),
        ("working_catalog_mag_limit", recipe_json.get("working_catalog_mag_limit"), 19.0),
        (
            "max_working_sources_per_frame",
            recipe_json.get("max_working_sources_per_frame"),
            2000,
        ),
        ("catalog_edge_margin_pix", recipe_json.get("catalog_edge_margin_pix"), 20.0),
        (
            "exclude_bad_pre_qc_frames",
            recipe_json.get("exclude_bad_pre_qc_frames"),
            True,
        ),
        (
            "quality_policy.detection_branch_enabled",
            quality_policy.get("detection_branch_enabled"),
            True,
        ),
        (
            "enable_psf_field_diagnostics",
            recipe_json.get("enable_psf_field_diagnostics"),
            True,
        ),
        (
            "exclude_candidates_from_ensemble",
            recipe_json.get("exclude_candidates_from_ensemble"),
            True,
        ),
        (
            "candidate_ensemble_until_converged",
            recipe_json.get("candidate_ensemble_until_converged"),
            True,
        ),
        (
            "candidate_ensemble_convergence_max_iterations",
            recipe_json.get("candidate_ensemble_convergence_max_iterations"),
            10,
        ),
        (
            "candidate_ensemble_growth_policy",
            recipe_json.get("candidate_ensemble_growth_policy"),
            "initial_only",
        ),
        (
            "enable_photometric_frame_qc",
            recipe_json.get("enable_photometric_frame_qc"),
            True,
        ),
        (
            "color_calibration_requested",
            recipe_json.get("color_calibration_requested"),
            True,
        ),
        ("enable_spatial_ensemble", recipe_json.get("enable_spatial_ensemble"), False),
    )

    missing: list[str] = []
    mismatch: list[str] = []
    notes: list[str] = []
    for name, actual, expected in required_values:
        if actual is None:
            missing.append(f"missing:{name}")
            continue
        if not _semantic_equal(actual, expected):
            mismatch.append(f"{name}:expected={_fmt(expected)},actual={_fmt(actual)}")

    ensemble_value = recipe_json.get("ensemble_min_ref_stars_per_frame")
    if ensemble_value is None:
        missing.append("missing:ensemble_min_ref_stars_per_frame")
    else:
        try:
            ensemble_min = int(ensemble_value)
        except (TypeError, ValueError):
            mismatch.append(
                "ensemble_min_ref_stars_per_frame:expected_one_of=20|30,"
                f"actual={_fmt(ensemble_value)}"
            )
        else:
            if ensemble_min not in {20, 30}:
                mismatch.append(
                    "ensemble_min_ref_stars_per_frame:expected_one_of=20|30,"
                    f"actual={ensemble_min}"
                )
            elif ensemble_min != 20:
                notes.append(
                    "ensemble_min_ref_stars_per_frame:"
                    f"canonical=20,compatible_previous={ensemble_min}"
                )

    recipe_version = recipe_json.get("recipe_version")
    if recipe_version is not None and str(recipe_version) != "field_pipeline_v9":
        notes.append(f"recipe_version:current=field_pipeline_v9,actual={recipe_version}")

    if missing:
        return Stage2RecipeCompatibility(
            required_recipe=required_recipe,
            status=MISSING_PROVENANCE,
            mismatch_reasons=tuple(missing + mismatch),
            compatibility_notes=tuple(notes),
        )
    if mismatch:
        return Stage2RecipeCompatibility(
            required_recipe=required_recipe,
            status=RECIPE_MISMATCH,
            mismatch_reasons=tuple(mismatch),
            compatibility_notes=tuple(notes),
        )
    return Stage2RecipeCompatibility(
        required_recipe=required_recipe,
        status=READY_STAGE3_RECIPE,
        compatibility_notes=tuple(notes),
    )


def _semantic_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(actual) == expected and float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, float):
        try:
            return abs(float(actual) - expected) <= 1.0e-12
        except (TypeError, ValueError):
            return False
    return str(actual).strip().casefold() == str(expected).strip().casefold()


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
