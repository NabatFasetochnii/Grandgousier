from dataclasses import dataclass

import pytest
import yaml

from grandgousier.calibration.photometric_policy import (
    identity_scoped_reference_catalog_name,
    resolve_color_calibration_policy,
)


@dataclass
class FakeProfile:
    raw_config: dict
    filter_aliases: dict[str, str]

    def normalize_filter(self, value):
        raw = str(value).strip()
        return self.filter_aliases.get(raw, self.filter_aliases.get(raw.upper(), raw))


def _profile(path: str) -> FakeProfile:
    data = yaml.safe_load(open(path, encoding="utf-8"))
    return FakeProfile(raw_config=data, filter_aliases={str(k): str(v) for k, v in data.get("filter_aliases", {}).items()})


def test_robophot_i_profile_resolves_ps1_i_r_minus_i_policy():
    policy = resolve_color_calibration_policy(_profile("configs/robophot.yaml"), "i", enabled_override=True)

    assert policy.enabled is True
    assert policy.reference_catalog_name == "panstarrs_dr2"
    assert policy.photometric_system == "ps1_ab"
    assert policy.reference_band == "i"
    assert policy.color_index == "r-i"
    assert policy.require_bands == ("r", "i")
    assert policy.max_measurement_mag_err == pytest.approx(0.08)
    assert policy.max_reference_mag_err == pytest.approx(0.05)
    assert policy.min_calibrator_objects == 20
    assert policy.min_color_span == pytest.approx(0.3)
    assert policy.max_transform_residual_mad == pytest.approx(0.10)
    assert policy.max_pivot_color_fraction == pytest.approx(0.25)
    assert policy.max_shape_metric_delta == pytest.approx(1e-10)
    assert policy.min_color_term_significance == pytest.approx(2.0)
    assert policy.min_color_model_bic_improvement == pytest.approx(6.0)
    assert policy.min_color_model_mad_improvement == pytest.approx(0.0)


def test_profile_keeps_unsupported_filter_disabled_without_inventing_transform():
    policy = resolve_color_calibration_policy(_profile("configs/robophot.yaml"), "B")

    assert policy.enabled is False
    assert policy.skip_reason == "disabled"
    assert policy.reference_band is None
    assert policy.color_index is None


def test_explicit_color_policy_overrides_profile_values():
    policy = resolve_color_calibration_policy(
        _profile("configs/robophot.yaml"),
        "i",
        enabled_override=True,
        reference_catalog_name="custom_ref",
        reference_band="z",
        color_index="i-z",
    )

    assert policy.enabled is True
    assert policy.reference_catalog_name == "custom_ref"
    assert policy.reference_band == "z"
    assert policy.color_index == "i-z"


def test_reference_catalog_namespace_is_stable_bounded_and_identity_scoped():
    first = identity_scoped_reference_catalog_name(
        "panstarrs_dr2",
        "auto_catalog_robophot_20260402_BK_Lyn_i",
    )
    repeated = identity_scoped_reference_catalog_name(
        "panstarrs_dr2",
        "auto_catalog_robophot_20260402_BK_Lyn_i",
    )
    other = identity_scoped_reference_catalog_name(
        "panstarrs_dr2",
        "auto_catalog_robophot_20260403_BK_Lyn_i",
    )

    assert first == repeated
    assert first != other
    assert first.startswith("panstarrs_dr2__")
    assert len(first) <= 64
