from __future__ import annotations

import math

import pandas as pd
import pytest

from grandgousier.catalogs.reference_photometry import (
    _make_panstarrs_remote_filter_params,
    _standardize_panstarrs_reference_dataframe,
    _synthesize_tonry2012_johnson_bv,
)


def test_tonry2012_ps1_to_johnson_bv_values_and_errors():
    result = _synthesize_tonry2012_johnson_bv(
        mag_g=15.4,
        mag_r=15.0,
        err_g=0.01,
        err_r=0.02,
    )
    color = 0.4
    expected_b = 15.4 + 0.212 + 0.556 * color + 0.034 * color**2
    expected_v = 15.0 + 0.005 + 0.462 * color + 0.013 * color**2

    assert result["mag_b"] == pytest.approx(expected_b)
    assert result["mag_v"] == pytest.approx(expected_v)
    assert result["err_b"] is not None and result["err_b"] >= 0.032
    assert result["err_v"] is not None and result["err_v"] >= 0.012
    assert result["synthetic_bv_transform"] == "tonry2012_table6_stellar_vega"


def test_standardized_panstarrs_rows_include_synthetic_bv_columns():
    raw = pd.DataFrame(
        [
            {
                "objID": "123",
                "raMean": 10.0,
                "decMean": 20.0,
                "gMeanPSFMag": 15.4,
                "gMeanPSFMagErr": 0.01,
                "rMeanPSFMag": 15.0,
                "rMeanPSFMagErr": 0.02,
                "iMeanPSFMag": 14.8,
                "iMeanPSFMagErr": 0.02,
                "zMeanPSFMag": 14.7,
                "zMeanPSFMagErr": 0.03,
                "yMeanPSFMag": 14.6,
                "yMeanPSFMagErr": 0.03,
                "nDetections": 20,
            }
        ]
    )

    result = _standardize_panstarrs_reference_dataframe(raw)

    assert len(result) == 1
    assert math.isfinite(float(result.loc[0, "mag_b"]))
    assert math.isfinite(float(result.loc[0, "mag_v"]))
    assert result.loc[0, "synthetic_bv_transform"] == "tonry2012_table6_stellar_vega"


def test_remote_filters_expand_synthetic_bv_to_native_ps1_g_and_r():
    params = _make_panstarrs_remote_filter_params(
        require_bands=["g", "r", "b", "v"],
        max_mag={"v": 19.0},
        max_mag_err={"b": 0.05, "v": 0.04},
    )

    assert params["gMeanPSFMag.gt"] == "-900.0"
    assert params["rMeanPSFMag.gt"] == "-900.0"
    assert params["gMeanPSFMagErr.lte"] == "0.04"
    assert params["rMeanPSFMagErr.lte"] == "0.04"
    assert not any(key.startswith("bMean") or key.startswith("vMean") for key in params)
