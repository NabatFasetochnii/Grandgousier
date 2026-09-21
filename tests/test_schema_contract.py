from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from grandgousier.db.models import Base, LegacyBase
from grandgousier.db.schema_contract import (
    CURRENT_SCHEMA_TABLES,
    FORBIDDEN_LEGACY_TABLES,
    forbidden_tables,
    missing_tables,
)


def _constraint_names(table_name: str, constraint_type: type) -> set[str | None]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


def _fk_ondelete(table_name: str, column_name: str) -> set[str | None]:
    return {
        fk.ondelete
        for fk in Base.metadata.tables[table_name].c[column_name].foreign_keys
    }


def test_metadata_covers_complete_clean_schema_contract() -> None:
    assert set(Base.metadata.tables) == set(CURRENT_SCHEMA_TABLES)
    assert len(Base.metadata.tables) == 46
    assert not (set(Base.metadata.tables) & set(FORBIDDEN_LEGACY_TABLES))
    assert set(FORBIDDEN_LEGACY_TABLES).issubset(LegacyBase.metadata.tables)





def test_phase_d_reference_photometry_is_band_normalized() -> None:
    assert "reference_photometry" not in Base.metadata.tables
    assert "reference_photometry" in LegacyBase.metadata.tables

    bands = Base.metadata.tables["photometric_bands"]
    sources = Base.metadata.tables["reference_photometry_sources"]
    points = Base.metadata.tables["reference_photometry_points"]

    assert {"refcat_id", "canonical_name", "provider_band_name", "photometric_system", "metadata_json"}.issubset(bands.c.keys())
    assert "uq_photometric_bands_catalog_name" in _constraint_names("photometric_bands", UniqueConstraint)
    assert _fk_ondelete("photometric_bands", "refcat_id") == {"CASCADE"}

    assert {
        "refcat_id",
        "catalog_source_id",
        "ref_uid",
        "identity_catalog_name",
        "identity_source_id",
        "identity_catalog_source_id",
        "distance_to_identity_arcsec",
        "ra_deg",
        "dec_deg",
        "epoch",
        "colors",
        "quality_flags",
        "raw_json",
    }.issubset(sources.c.keys())
    assert not any(name.startswith("mag_") for name in sources.c.keys())
    assert "uq_reference_photometry_sources_catalog_source" in _constraint_names(
        "reference_photometry_sources", UniqueConstraint
    )
    assert _fk_ondelete("reference_photometry_sources", "refcat_id") == {"CASCADE"}
    assert _fk_ondelete("reference_photometry_sources", "identity_catalog_source_id") == {"RESTRICT"}

    assert {
        "refphot_source_id",
        "band_id",
        "magnitude",
        "magnitude_error",
        "quality_status",
        "quality_flags",
        "provider_payload",
    }.issubset(points.c.keys())
    assert "uq_reference_photometry_points_source_band" in _constraint_names(
        "reference_photometry_points", UniqueConstraint
    )
    assert _fk_ondelete("reference_photometry_points", "refphot_source_id") == {"CASCADE"}
    assert _fk_ondelete("reference_photometry_points", "band_id") == {"RESTRICT"}

def test_phase_c_identity_and_location_schema_is_normalized() -> None:
    frames = Base.metadata.tables["frames"]
    locations = Base.metadata.tables["frame_locations"]
    objects = Base.metadata.tables["objects"]
    links = Base.metadata.tables["object_catalog_links"]

    assert {"raw_path", "file_size_bytes", "file_mtime_utc"}.isdisjoint(frames.c.keys())
    assert {"locator", "locator_uid", "location_kind", "is_primary", "file_size_bytes", "file_mtime_utc", "content_sha256"}.issubset(locations.c.keys())
    assert _fk_ondelete("frame_locations", "frame_id") == {"CASCADE"}
    primary_index = next(
        index for index in locations.indexes if index.name == "uq_frame_locations_one_primary"
    )
    assert primary_index.unique is True
    assert primary_index.dialect_options["postgresql"].get("where") is not None
    assert primary_index.dialect_options["sqlite"].get("where") is not None

    assert {"identity_scheme", "reference_epoch"}.issubset(objects.c.keys())
    assert {"gaia_dr3_id", "external_ids"}.isdisjoint(objects.c.keys())
    assert {"object_id", "catalog_source_id", "association_method", "separation_arcsec", "comparison_epoch", "evidence"}.issubset(links.c.keys())
    assert _fk_ondelete("object_catalog_links", "catalog_source_id") == {"RESTRICT"}


def test_detection_storage_is_run_normalized_and_high_cardinality_rows_are_narrow() -> None:
    runs = Base.metadata.tables["detection_runs"]
    run_frames = Base.metadata.tables["detection_run_frames"]
    detections = Base.metadata.tables["source_detections"]
    matches = Base.metadata.tables["detection_catalog_matches"]

    assert {"pipeline_run_id", "run_kind", "catalog_name", "algorithm", "algorithm_version", "parameters", "fingerprint"}.issubset(runs.c.keys())
    assert {"detection_run_id", "frame_id", "status", "diagnostics"}.issubset(run_frames.c.keys())
    assert _fk_ondelete("detection_runs", "pipeline_run_id") == {"RESTRICT"}
    run_checks = _constraint_names("detection_runs", CheckConstraint)
    assert {"ck_detection_runs_kind", "ck_detection_runs_catalog_scope"}.issubset(run_checks)
    assert _fk_ondelete("detection_run_frames", "detection_run_id") == {"CASCADE"}
    assert _fk_ondelete("detection_run_frames", "frame_id") == {"CASCADE"}

    assert "detection_run_frame_id" in detections.c
    assert "frame_id" not in detections.c
    assert "algorithm" not in detections.c
    assert "parameters" not in detections.c
    assert _fk_ondelete("source_detections", "detection_run_frame_id") == {"CASCADE"}

    assert "detection_run_frame_id" in matches.c
    for removed in ("frame_id", "catalog_name", "algorithm", "details"):
        assert removed not in matches.c
    for preserved_snapshot in ("detection_mag_inst", "catalog_mag", "zero_point"):
        assert preserved_snapshot in matches.c
    match_constraints = _constraint_names("detection_catalog_matches", UniqueConstraint)
    assert "uq_detection_catalog_matches_run_frame_detection" in match_constraints
    assert "uq_detection_catalog_matches_run_frame_catalog_source" in match_constraints

def test_missing_and_forbidden_tables_are_reported_separately() -> None:
    existing = set(CURRENT_SCHEMA_TABLES) - {"pipeline_runs", "event_registry"}
    existing.update({"unrelated_application_table", "science_measurements"})
    assert missing_tables(existing) == ["event_registry", "pipeline_runs"]
    assert forbidden_tables(existing) == ["science_measurements"]


def test_field_series_frames_support_alternative_groupings() -> None:
    names = _constraint_names("field_series_frames", UniqueConstraint)
    assert names == {"uq_field_series_frames_series_frame"}
    indexes = {index.name: tuple(column.name for column in index.columns) for index in Base.metadata.tables["field_series_frames"].indexes}
    assert indexes["ix_field_series_frames_frame"] == ("frame_id",)


def test_measurements_are_nonnullable_base_storage_with_restrict_lineage() -> None:
    table = Base.metadata.tables["measurements"]
    assert table.c.calibration_version_id.nullable is False
    assert _fk_ondelete("measurements", "calibration_version_id") == {"RESTRICT"}


def test_artifact_registry_is_part_of_orm_contract() -> None:
    assert "artifacts" in Base.metadata.tables
    assert "artifact_publications" in Base.metadata.tables
    artifact_targets = {
        element.target_fullname
        for constraint in Base.metadata.tables["artifacts"].constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert "pipeline_runs.pipeline_run_id" in artifact_targets
    assert "batch_runs.batch_run_id" in artifact_targets
    assert "batch_job_attempts.batch_job_attempt_id" in artifact_targets
    assert "calibration_versions.calibration_version_id" in artifact_targets
    assert "field_series.series_id" in artifact_targets
    assert _fk_ondelete("artifacts", "calibration_version_id") == {"RESTRICT"}


def test_artifact_publication_is_unique_mutable_head() -> None:
    table = Base.metadata.tables["artifact_publications"]
    assert any(
        constraint.name is None
        and tuple(constraint.columns.keys()) == ("publication_uri",)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def test_batch_orchestration_is_part_of_orm_contract() -> None:
    expected = {"batch_runs", "batch_inventory_items", "batch_jobs", "batch_job_attempts"}
    assert expected.issubset(Base.metadata.tables)


def test_normalized_calibration_terms_do_not_duplicate_calibration_identity() -> None:
    frame = Base.metadata.tables["calibration_frame_terms"]
    obj = Base.metadata.tables["calibration_iteration_objects"]
    assert "calibration_version_id" not in frame.c
    assert "calibration_version_id" not in obj.c

    frame_targets = {
        element.target_fullname
        for constraint in frame.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    object_targets = {
        element.target_fullname
        for constraint in obj.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert frame_targets == {
        "calibration_iterations.calibration_iteration_id",
        "frames.frame_id",
    }
    assert object_targets == {
        "calibration_iterations.calibration_iteration_id",
        "objects.object_id",
    }


def test_normalized_calibration_terms_have_idempotency_constraints() -> None:
    assert "uq_calibration_iterations_version_kind_no" in _constraint_names("calibration_iterations", UniqueConstraint)
    assert "uq_calibration_frame_terms_iteration_frame_kind" in _constraint_names("calibration_frame_terms", UniqueConstraint)
    assert "uq_calibration_iteration_objects_iteration_object" in _constraint_names("calibration_iteration_objects", UniqueConstraint)


def test_durable_science_photometry_is_narrow_and_geometry_free() -> None:
    table = Base.metadata.tables["science_photometry"]
    assert tuple(column.name for column in table.columns) == (
        "calibration_version_id",
        "base_measurement_id",
        "flux",
        "flux_err",
        "mag_inst",
        "mag_cal",
        "mag_err",
        "mag_err_random",
        "mag_err_systematic",
        "local_zp_correction",
    )
    assert tuple(column.name for column in table.primary_key.columns) == (
        "calibration_version_id",
        "base_measurement_id",
    )
    assert not table.indexes
    assert _fk_ondelete("science_photometry", "calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("science_photometry", "base_measurement_id") == {"RESTRICT"}


def test_key_scientific_lineage_fks_do_not_set_null() -> None:
    assert _fk_ondelete("calibration_versions", "source_calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("calibration_versions", "supersedes_calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("calibration_product_aliases", "head_calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("calibration_iterations", "calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("calibration_iterations", "source_calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("interseries_calibration_series", "science_calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("event_frame_coverage", "calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("event_frame_coverage", "measurement_id") == {"RESTRICT"}
