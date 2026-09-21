import pytest

pytest.importorskip("astropy")

import numpy as np

from grandgousier.cli import build_parser
from grandgousier.photometry.forced_aperture import measure_one_source


def test_measure_one_source_collects_phase_timings_when_requested():
    data = np.full((40, 40), 100.0, dtype=float)
    data[20, 20] = 1000.0
    timings: dict[str, float] = {}

    measurement = measure_one_source(
        data,
        x=20.0,
        y=20.0,
        aperture_radius_pix=3.0,
        annulus_inner_pix=8.0,
        annulus_outer_pix=12.0,
        exptime=10.0,
        zero_point=0.0,
        saturation_level_adu=None,
        gain_e_per_adu=1.5,
        read_noise_e=5.0,
        performance_timings=timings,
    )

    assert measurement.flux is not None
    assert set(timings) == {
        "source_geometry",
        "source_background_flux",
        "source_error_model",
        "source_magnitude_finalize",
    }
    assert all(value >= 0.0 for value in timings.values())


def test_forced_photometry_diagnostic_cli_switches_parse():
    parser = build_parser()

    direct = parser.parse_args(["run-forced-photometry", "--diagnose-performance"])
    assert direct.diagnose_performance is True

    pipeline = parser.parse_args(
        [
            "run-field-pipeline",
            "--catalog-name",
            "catalog",
            "--calibration-prefix",
            "test",
            "--diagnose-forced-photometry",
        ]
    )
    assert pipeline.diagnose_forced_photometry is True

    archive = parser.parse_args(
        ["process-archive-path", "--path", ".", "--diagnose-forced-photometry"]
    )
    assert archive.diagnose_forced_photometry is True
