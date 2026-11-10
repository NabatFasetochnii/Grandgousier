from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STAGE2_PROFILES = ("bulk", "diagnostic")
DEFAULT_STAGE2_PROFILE = "bulk"


def normalize_stage2_profile(profile: str | None) -> str:
    value = str(profile or DEFAULT_STAGE2_PROFILE).strip().lower()
    if value not in STAGE2_PROFILES:
        raise ValueError(
            f"stage2_profile must be one of: {', '.join(STAGE2_PROFILES)}"
        )
    return value


def resolve_stage2_profile_settings(
    profile: str | None,
    *,
    requested: Mapping[str, Any],
) -> dict[str, Any]:
    """Return auditable effective Stage-2 optional-work settings.

    These switches are restricted to diagnostic work and output serialization;
    no scientific selection, measurement-error, identity, or calibration setting
    is changed here. Candidate-triage input remains required when candidate
    exclusion is requested, including in the bulk profile.
    """

    selected = normalize_stage2_profile(profile)
    diagnostic = selected == "diagnostic"
    candidate_exclusion = bool(requested.get("exclude_candidates_from_ensemble"))
    enable_photometric_frame_qc = bool(requested.get("enable_photometric_frame_qc"))
    candidate_exports = bool(requested.get("export_candidate_light_curves", True))
    make_summary = bool(requested.get("make_light_curve_summary", True))
    field_report_requested = bool(requested.get("field_report_enabled", True))
    random_error_validation = bool(requested.get("random_error_validation", True))
    diagnostic_output_requested = bool(
        requested.get("random_error_diagnostic_exports_requested", True)
    )
    summary_output_requested = bool(
        requested.get("light_curve_summary_json_requested", True)
    )

    return {
        "profile": selected,
        "psf_field_diagnostics": (
            bool(requested.get("enable_psf_field_diagnostics")) if diagnostic else False
        ),
        "aperture_stability_qc": (
            bool(requested.get("enable_aperture_stability_qc")) if diagnostic else False
        ),
        "candidate_light_curve_exports": candidate_exports if diagnostic else False,
        "candidate_plot_exports": (
            bool(requested.get("export_candidate_plots", True))
            if diagnostic and candidate_exports
            else False
        ),
        "forced_photometry_performance_diagnostics": (
            bool(requested.get("forced_photometry_diagnostics")) if diagnostic else False
        ),
        "calibration_performance_diagnostics": (
            bool(requested.get("calibration_performance_diagnostics")) if diagnostic else False
        ),
        "field_qc_detail_reports": diagnostic and field_report_requested,
        "field_qc_candidate_input": candidate_exclusion,
        "candidate_axis_correlation_diagnostics": diagnostic and enable_photometric_frame_qc,
        "candidate_membership_history_export": diagnostic and candidate_exports,
        "light_curve_summary_row_materialization": diagnostic and make_summary,
        "light_curve_summary_compact_counts": not diagnostic and make_summary,
        "light_curve_summary_json": (
            diagnostic and make_summary and summary_output_requested
        ),
        # Bulk retains physical validation and residual-floor work but skips
        # legacy-error comparison and pairwise-correlation diagnostics.
        "random_error_validation": random_error_validation,
        "random_error_legacy_comparison": random_error_validation and diagnostic,
        "random_error_pairwise_residual_correlations": (
            random_error_validation and diagnostic
        ),
        "random_error_residual_floor_diagnostics": random_error_validation,
        "random_error_diagnostic_exports": (
            diagnostic and random_error_validation and diagnostic_output_requested
        ),
        "random_error_validation_and_legacy_comparison": (
            random_error_validation and diagnostic
        ),
    }
