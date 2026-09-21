from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.calibration.ensemble import (
    EnsembleCalibrationResult,
    _load_compact_normalized_aperture_zp_source_measurements,
)


class _NoBaseQuerySession:
    def __init__(self):
        self.execute_called = False

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, _statement):
        # legacy Measurement rows for the normalized APERTURE_ZP generation
        return 0

    def execute(self, _statement):
        self.execute_called = True
        raise AssertionError("shared APERTURE_ZP seed path must not issue a BASE query")


def test_ensemble_reuses_aperture_zp_and_analysis_seeds_without_base_query():
    session = _NoBaseQuerySession()
    source_cv = SimpleNamespace(
        calibration_version_id=33,
        product_kind="APERTURE_ZP",
        materialization_kind="NORMALIZED_TERMS",
    )
    result = EnsembleCalibrationResult()
    aperture_zp_rows = (
        {
            "object_id": 7,
            "frame_id": 101,
            "mag_inst": 15.2,
            "mag_cal": 16.3,
            "catalog_name": "gaia",
        },
        {
            "object_id": 8,
            "frame_id": 101,
            "mag_inst": 15.4,
            "mag_cal": 16.5,
            "catalog_name": "gaia",
        },
    )
    analysis_rows = (
        {
            "measurement_id": 1001,
            "object_id": 7,
            "frame_id": 101,
            "x": 10.0,
            "y": 20.0,
            "flux": 1000.0,
            "mag_inst": 15.1,
            "mag_err": 0.03,
            "mag_err_random": 0.02,
            "mag_err_systematic": None,
            "aperture_radius_pix": 3.0,
            "catalog_name": "gaia",
        },
    )

    rows = _load_compact_normalized_aperture_zp_source_measurements(
        session,
        source_cv=source_cv,
        catalog_name="gaia",
        instrument_unit_id=None,
        frame_ids=[101],
        prefer_mag_cal=True,
        result=result,
        prepare_analysis_snapshot_seed=True,
        preloaded_aperture_zp_rows=aperture_zp_rows,
        preloaded_analysis_snapshot_base_rows=analysis_rows,
    )

    assert session.execute_called is False
    assert [row.value for row in rows] == pytest.approx([16.3, 16.5])
    assert result.downstream_seed_reused is True
    assert result.downstream_seed_rows == 2
    assert result.timings_sec["compact_base_query"] == 0.0
    assert result.analysis_snapshot_seed_prepared is True
    assert result.analysis_snapshot_seed_rows == 1
    assert tuple(result._analysis_snapshot_base_rows) == analysis_rows


def test_runner_routes_aperture_zp_shared_seed_to_all_ensemble_calls():
    runner_path = Path(__file__).resolve().parents[1] / "grandgousier" / "pipeline" / "runner.py"
    tree = ast.parse(runner_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_field_pipeline"
    )

    aperture_zp_calls = []
    ensemble_calls = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
        if node.func.id == "aperture_zp_calibrate":
            aperture_zp_calls.append(keywords)
        elif node.func.id == "ensemble_calibrate_measurements":
            ensemble_calls.append(keywords)

    assert aperture_zp_calls
    assert any("prepare_downstream_seed" in kws for kws in aperture_zp_calls)
    assert any("prepare_analysis_snapshot_seed" in kws for kws in aperture_zp_calls)
    assert ensemble_calls
    assert all("_preloaded_aperture_zp_rows" in kws for kws in ensemble_calls)
    assert all("_preloaded_analysis_snapshot_base_rows" in kws for kws in ensemble_calls)
