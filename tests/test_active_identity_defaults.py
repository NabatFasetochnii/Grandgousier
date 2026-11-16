from __future__ import annotations

import inspect

import pytest

from grandgousier.calibration.color_calibration import (
    apply_color_calibration,
    compute_color_calibration_diagnostics,
)
from grandgousier.catalogs import reference_photometry


def test_color_calibration_callable_identity_defaults_are_gaia_dr3() -> None:
    assert (
        inspect.signature(compute_color_calibration_diagnostics)
        .parameters["identity_catalog_name"]
        .default
        == "gaia_dr3"
    )
    assert (
        inspect.signature(apply_color_calibration)
        .parameters["identity_catalog_name"]
        .default
        == "gaia_dr3"
    )


def test_reference_photometry_diagnostic_fallback_identity_is_gaia_dr3(monkeypatch) -> None:
    observed: dict[str, str] = {}

    def no_identity_sources(_session, *, field_id, identity_catalog_name):
        observed["identity_catalog_name"] = identity_catalog_name
        return []

    monkeypatch.setattr(
        reference_photometry,
        "_field_identity_source_ids",
        no_identity_sources,
    )

    result = reference_photometry.diagnose_reference_photometry(object())

    assert observed["identity_catalog_name"] == "gaia_dr3"
    assert result.identity_catalog_name == "gaia_dr3"


def test_color_calibration_cli_identity_defaults_are_gaia_dr3() -> None:
    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    diagnose = parser.parse_args(["diagnose-reference-photometry"])
    compute = parser.parse_args(
        [
            "compute-color-calibration",
            "--calibration-name",
            "source",
            "--reference-catalog-name",
            "reference",
            "--reference-band",
            "i",
            "--color-index",
            "r-i",
        ]
    )
    apply = parser.parse_args(
        [
            "apply-color-calibration",
            "--source-calibration-name",
            "source",
            "--target-calibration-name",
            "target",
            "--reference-catalog-name",
            "reference",
            "--reference-band",
            "i",
            "--color-index",
            "r-i",
        ]
    )

    assert diagnose.identity_catalog_name == "gaia_dr3"
    assert compute.identity_catalog_name == "gaia_dr3"
    assert apply.identity_catalog_name == "gaia_dr3"
