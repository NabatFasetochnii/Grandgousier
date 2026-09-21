"""Grandgousier stable Python API.

Only symbols exported here are covered by the public API compatibility contract.
Internal modules remain implementation details even when they are importable.
"""

from grandgousier.api.interseries import (
    GraphEdgeDiagnostic,
    GraphSeriesOffset,
    GraphSolution,
    INTERSERIES_SIGN_CONVENTION,
    InterseriesObjectState,
    PairCalibrator,
    PairwiseOffset,
    compute_interseries_pair_offset,
    solve_interseries_offsets,
)
from grandgousier.api.photometry import (
    ApertureErrorBudget,
    CircularApertureMeasurement,
    LEGACY_APERTURE_ERROR_MODEL,
    MEDIAN_BACKGROUND_VARIANCE_FACTOR,
    PHYSICAL_APERTURE_ERROR_MODEL,
    compute_aperture_error_budget,
    measure_circular_aperture,
)

__all__ = [
    "ApertureErrorBudget",
    "CircularApertureMeasurement",
    "GraphEdgeDiagnostic",
    "GraphSeriesOffset",
    "GraphSolution",
    "INTERSERIES_SIGN_CONVENTION",
    "InterseriesObjectState",
    "LEGACY_APERTURE_ERROR_MODEL",
    "MEDIAN_BACKGROUND_VARIANCE_FACTOR",
    "PHYSICAL_APERTURE_ERROR_MODEL",
    "PairCalibrator",
    "PairwiseOffset",
    "compute_aperture_error_budget",
    "compute_interseries_pair_offset",
    "measure_circular_aperture",
    "solve_interseries_offsets",
]
