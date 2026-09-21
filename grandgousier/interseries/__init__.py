"""Stage 3: field-level inter-series photometric calibration."""

from .planning import (
    InterseriesPairCandidate,
    InterseriesPlan,
    InterseriesSeriesCandidate,
    build_interseries_plan,
    interseries_pair_columns,
    interseries_series_columns,
)

__all__ = [
    "InterseriesPairCandidate",
    "InterseriesPlan",
    "InterseriesSeriesCandidate",
    "build_interseries_plan",
    "interseries_pair_columns",
    "interseries_series_columns",
]

from .persistence import (
    RELIABILITY_REDUNDANT,
    RELIABILITY_SPARSE,
    persist_interseries_calibration,
    verify_interseries_generation,
)

__all__ += [
    "RELIABILITY_REDUNDANT",
    "RELIABILITY_SPARSE",
    "persist_interseries_calibration",
    "verify_interseries_generation",
]
