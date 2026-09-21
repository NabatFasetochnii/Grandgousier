from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

# Canonical fresh-baseline schema for Grandgousier 0.7 clean-schema epoch.  Compatibility-only
# 0.6 relations are deliberately excluded: a fresh database must never recreate
# the retired wide SCIENCE layer or legacy Stage-3 offsets.
CURRENT_SCHEMA_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "instrument_units",
        "observations",
        "frames",
        "frame_locations",
        "processing_events",
        "catalog_sources",
        "reference_photometry_catalogs",
        "photometric_bands",
        "reference_photometry_sources",
        "reference_photometry_points",
        "frame_catalog_links",
        "detection_runs",
        "detection_run_frames",
        "source_detections",
        "detection_catalog_matches",
        "frame_qc",
        "objects",
        "object_catalog_links",
        "object_qc",
        "measurements",
        "science_photometry",
        "calibration_versions",
        "calibration_product_aliases",
        "calibration_iterations",
        "calibration_frame_terms",
        "calibration_iteration_objects",
        "interseries_calibration_generations",
        "interseries_calibration_series",
        "interseries_calibration_edges",
        "fields",
        "field_series",
        "field_series_frames",
        "pipeline_runs",
        "pipeline_run_steps",
        "pipeline_log_events",
        "artifacts",
        "artifact_publications",
        "batch_runs",
        "batch_inventory_items",
        "batch_jobs",
        "batch_job_attempts",
        "event_registry",
        "event_object_links",
        "event_frame_coverage",
        "event_detection_links",
    }
)

# Their presence identifies a pre-clean-schema database.  0.7 is intentionally
# fresh-baseline-only: neither init-db nor migrate-db may mutate such a database
# into the clean layout in place.
FORBIDDEN_LEGACY_TABLES: frozenset[str] = frozenset(
    {
        "science_measurements",
        "science_photometry_cache",
        "science_flag_override_sets",
        "science_flag_overrides",
        "interseries_calibration_offsets",
        "reference_photometry",
    }
)


def missing_tables(existing_tables: Iterable[str]) -> list[str]:
    """Return canonical Grandgousier tables missing from an inspected schema."""

    return sorted(CURRENT_SCHEMA_TABLES.difference(set(existing_tables)))


def forbidden_tables(existing_tables: Iterable[str]) -> list[str]:
    """Return 0.6 compatibility relations forbidden by the clean 0.7 contract."""

    return sorted(FORBIDDEN_LEGACY_TABLES.intersection(set(existing_tables)))


def assert_clean_schema_epoch(existing_tables: Iterable[str]) -> None:
    """Refuse in-place use of a pre-0.7 compatibility schema."""

    forbidden = forbidden_tables(existing_tables)
    if forbidden:
        raise RuntimeError(
            "Grandgousier 0.7 clean schema is fresh-baseline-only; refusing an "
            "in-place operation on a database containing legacy tables: "
            + ", ".join(forbidden)
        )


def assert_schema_contract(engine: Engine) -> None:
    """Raise unless the database satisfies the clean 0.7 table contract."""

    existing = inspect(engine).get_table_names()
    assert_clean_schema_epoch(existing)
    missing = missing_tables(existing)
    if missing:
        raise RuntimeError(
            "Grandgousier database schema is incomplete; missing tables: "
            + ", ".join(missing)
        )
