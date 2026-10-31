from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from grandgousier.db.models import Base, LegacyBase
from grandgousier.db.schema_contract import (
    CURRENT_SCHEMA_TABLES,
    FORBIDDEN_LEGACY_TABLES,
    forbidden_tables,
    missing_tables,
    _assert_m4_schema_rows,
    assert_schema_contract,
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
    assert _fk_ondelete("interseries_calibration_series", "science_calibration_version_id") == {"RESTRICT", "NO ACTION"}
    assert any(fk.ondelete == "RESTRICT" and len(fk.constraint.elements) == 1
               for fk in Base.metadata.tables["interseries_calibration_series"].c.science_calibration_version_id.foreign_keys)
    assert _fk_ondelete("event_frame_coverage", "calibration_version_id") == {"RESTRICT"}
    assert _fk_ondelete("event_frame_coverage", "measurement_id") == {"RESTRICT"}


def test_approved_nullable_identity_uniques_are_nulls_not_distinct_only() -> None:
    approved = {
        "instrument_units": (
            "uq_instrument_units_identity",
            ("instrument", "site", "tube", "camera", "filter_name", "observing_mode"),
        ),
        "object_qc": (
            "uq_object_qc_identity",
            (
                "object_id",
                "catalog_name",
                "instrument_unit_id",
                "calibration_version_id",
                "algorithm",
            ),
        ),
        "event_frame_coverage": (
            "uq_event_frame_coverage",
            ("event_id", "frame_id", "object_id", "calibration_version_id"),
        ),
    }
    excluded = {
        ("calibration_versions", "uq_calibration_versions_logical_generation"): (
            "logical_name",
            "generation_no",
        ),
        ("artifacts", "uq_artifacts_run_uri"): ("pipeline_run_id", "uri"),
        ("artifacts", "uq_artifacts_batch_attempt_uri"): ("batch_job_attempt_id", "uri"),
    }
    constraints_with_nnd: set[tuple[str, str | None]] = set()

    for table_name, (constraint_name, columns) in approved.items():
        table = Base.metadata.tables[table_name]
        constraint = next(
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name == constraint_name
        )
        assert tuple(constraint.columns.keys()) == columns
        assert constraint.dialect_options["postgresql"].get("nulls_not_distinct") is True
        constraints_with_nnd.add((table_name, constraint_name))

        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UNIQUE NULLS NOT DISTINCT" in ddl

    for (table_name, constraint_name), columns in excluded.items():
        table = Base.metadata.tables[table_name]
        constraint = next(
            constraint
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name == constraint_name
        )
        assert tuple(constraint.columns.keys()) == columns
        assert constraint.dialect_options["postgresql"].get("nulls_not_distinct") is None
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "UNIQUE NULLS NOT DISTINCT" not in ddl

    actual_nnd = {
        (table.name, constraint.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.dialect_options["postgresql"].get("nulls_not_distinct") is True
    }
    assert actual_nnd == constraints_with_nnd


def test_m4_composite_field_scope_and_exact_delete_actions_compile_for_postgresql() -> None:
    expected_uniques = {
        "field_series": ("uq_field_series_series_field", ("series_id", "field_id")),
        "interseries_calibration_generations": (
            "uq_interseries_generations_id_field", ("interseries_generation_id", "field_id")
        ),
    }
    for table_name, (name, columns) in expected_uniques.items():
        constraint = next(c for c in Base.metadata.tables[table_name].constraints if c.name == name)
        assert isinstance(constraint, UniqueConstraint)
        assert tuple(constraint.columns.keys()) == columns

    expected_fks = {
        "fk_interseries_generations_reference_series_field": (
            "interseries_calibration_generations", ("reference_series_id", "field_id"),
            ("field_series.series_id", "field_series.field_id"), "RESTRICT",
        ),
        "fk_interseries_series_generation_field": (
            "interseries_calibration_series", ("interseries_generation_id", "field_id"),
            ("interseries_calibration_generations.interseries_generation_id",
             "interseries_calibration_generations.field_id"), "CASCADE",
        ),
        "fk_interseries_series_series_field": (
            "interseries_calibration_series", ("series_id", "field_id"),
            ("field_series.series_id", "field_series.field_id"), "SET NULL (series_id)",
        ),
        "fk_interseries_edges_left_series_restrict": (
            "interseries_calibration_edges", ("left_series_id",),
            ("field_series.series_id",), "RESTRICT",
        ),
        "fk_interseries_edges_right_series_restrict": (
            "interseries_calibration_edges", ("right_series_id",),
            ("field_series.series_id",), "RESTRICT",
        ),
        "fk_interseries_edges_left_generation_member": (
            "interseries_calibration_edges", ("interseries_generation_id", "left_series_id"),
            ("interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"), "NO ACTION",
        ),
        "fk_interseries_edges_right_generation_member": (
            "interseries_calibration_edges", ("interseries_generation_id", "right_series_id"),
            ("interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"), "NO ACTION",
        ),
    }
    for name, (table_name, columns, targets, ondelete) in expected_fks.items():
        table = Base.metadata.tables[table_name]
        constraint = next(c for c in table.constraints if c.name == name)
        assert isinstance(constraint, ForeignKeyConstraint)
        assert tuple(constraint.columns.keys()) == columns
        assert tuple(str(element.column) for element in constraint.elements) == targets
        assert constraint.ondelete == ondelete
        if name in {
            "fk_interseries_edges_left_series_restrict",
            "fk_interseries_edges_right_series_restrict",
        }:
            assert constraint.deferrable is False
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert f"ON DELETE {ondelete}" in ddl
        if name.endswith("_generation_member"):
            assert constraint.onupdate == "NO ACTION"
            assert constraint.deferrable is True
            assert constraint.initially == "DEFERRED"
            assert "ON UPDATE NO ACTION DEFERRABLE INITIALLY DEFERRED" in ddl

    member = Base.metadata.tables["interseries_calibration_series"]
    assert member.c.field_id.nullable is False
    assert member.c.series_id.nullable is True
    assert _fk_ondelete("interseries_calibration_generations", "reference_series_id") == {"RESTRICT", "NO ACTION"}
    assert _fk_ondelete("interseries_calibration_series", "series_id") == {"SET NULL (series_id)", "NO ACTION"}

    edges = Base.metadata.tables["interseries_calibration_edges"]
    assert edges.c.left_series_id.nullable is False
    assert edges.c.right_series_id.nullable is False
    for side in ("left", "right"):
        assert {
            fk.constraint.name: fk.ondelete
            for fk in edges.c[f"{side}_series_id"].foreign_keys
        } == {
            f"fk_interseries_edges_{side}_series_restrict": "RESTRICT",
            f"fk_interseries_edges_{side}_generation_member": "NO ACTION",
        }
    assert _fk_ondelete("interseries_calibration_edges", "interseries_generation_id") == {
        "CASCADE", "NO ACTION",
    }
    edge_fks = [
        constraint for constraint in edges.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(edge_fks) == 5
    parent_unique = next(
        c for c in member.constraints
        if c.name == "uq_interseries_calibration_series_generation_series"
    )
    assert tuple(parent_unique.columns.keys()) == ("interseries_generation_id", "series_id")
    assert parent_unique.deferrable is not True


def test_reference_membership_and_science_cv_scope_are_enforced_in_fresh_metadata() -> None:
    generation = Base.metadata.tables["interseries_calibration_generations"]
    member = Base.metadata.tables["interseries_calibration_series"]
    cv = Base.metadata.tables["calibration_versions"]
    unique_keys = {c.name: tuple(c.columns.keys()) for c in cv.constraints
                   if isinstance(c, UniqueConstraint)}
    assert unique_keys["uq_calibration_versions_id_field"] == ("calibration_version_id", "field_id")
    assert unique_keys["uq_calibration_versions_id_field_series"] == (
        "calibration_version_id", "field_id", "series_id")
    for name in ("uq_calibration_versions_id_field", "uq_calibration_versions_id_field_series"):
        constraint = next(c for c in cv.constraints if c.name == name)
        assert constraint.deferrable is not True
    expected = {
        "fk_interseries_generations_reference_member": (
            generation, ("interseries_generation_id", "reference_series_id"),
            ("interseries_calibration_series.interseries_generation_id",
             "interseries_calibration_series.series_id"), True),
        "fk_interseries_series_science_cv_field": (
            member, ("science_calibration_version_id", "field_id"),
            ("calibration_versions.calibration_version_id", "calibration_versions.field_id"), False),
        "fk_interseries_series_science_cv_field_series": (
            member, ("science_calibration_version_id", "field_id", "series_id"),
            ("calibration_versions.calibration_version_id", "calibration_versions.field_id",
             "calibration_versions.series_id"), False),
    }
    for name, (table, columns, targets, deferred) in expected.items():
        fk = next(c for c in table.constraints if c.name == name)
        assert isinstance(fk, ForeignKeyConstraint)
        assert tuple(fk.columns.keys()) == columns
        assert tuple(str(e.column) for e in fk.elements) == targets
        assert fk.ondelete == fk.onupdate == "NO ACTION"
        assert fk.deferrable is deferred
        assert fk.initially == ("DEFERRED" if deferred else None)
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert f"CONSTRAINT {name} FOREIGN KEY" in ddl
    assert member.c.series_id.nullable is True  # MATCH SIMPLE skips the triple, not the pair.
    assert member.c.field_id.nullable is False
    # No lifecycle/product-role freeze: publication can supersede an immutable CV.
    assert not any(isinstance(c, CheckConstraint) and
                   ("product_role" in str(c.sqltext) or "lifecycle_status" in str(c.sqltext))
                   for c in cv.constraints)


def _m4_runtime_rows():
    nullability = {
        "calibration_versions": {
            "calibration_version_id": True, "field_id": False, "series_id": False,
        },
        "field_series": {"series_id": True, "field_id": True},
        "interseries_calibration_generations": {
            "interseries_generation_id": True, "field_id": True, "reference_series_id": True,
        },
        "interseries_calibration_series": {
            "interseries_generation_id": True, "field_id": True, "series_id": False,
            "science_calibration_version_id": True,
        },
        "interseries_calibration_edges": {
            "interseries_generation_id": True, "left_series_id": True, "right_series_id": True,
        },
    }
    columns = [
        {"table_name": table, "column_name": column, "not_null": not_null, "is_bigint": True}
        for table, entries in nullability.items() for column, not_null in entries.items()
    ]
    uniques = [
        ("calibration_versions", "uq_calibration_versions_id_field",
         ["calibration_version_id", "field_id"]),
        ("calibration_versions", "uq_calibration_versions_id_field_series",
         ["calibration_version_id", "field_id", "series_id"]),
        ("field_series", "uq_field_series_series_field", ["series_id", "field_id"]),
        ("interseries_calibration_generations", "uq_interseries_generations_id_field",
         ["interseries_generation_id", "field_id"]),
        ("interseries_calibration_series", "uq_interseries_calibration_series_generation_series",
         ["interseries_generation_id", "series_id"]),
        ("interseries_calibration_edges", "uq_interseries_calibration_edges_generation_pair",
         ["interseries_generation_id", "left_series_id", "right_series_id"]),
    ]
    constraints = [
        {"child_table": table, "child_schema": "public", "name": name, "kind": "u", "child_columns": cols,
          "validated": True, "deferrable": False, "initially_deferred": False,
          "index_valid": True, "index_ready": True, "index_immediate": True,
          "server_version_num": 150000, "enforced": None,
          "is_local": True, "inherited_count": 0}
        for table, name, cols in uniques
    ]
    constraints.append({
        "child_table": "field_series", "child_schema": "public", "name": "field_series_pkey",
        "kind": "p", "child_columns": ["series_id"], "validated": True,
          "deferrable": False, "is_local": True, "inherited_count": 0,
    })
    constraints.append({
        "child_table": "interseries_calibration_edges", "child_schema": "public",
        "name": "ck_interseries_calibration_edges_series_order", "kind": "c",
        "validated": True, "check_expression": "(left_series_id < right_series_id)",
    })
    fks = [
        ("interseries_calibration_generations", "fk_interseries_generations_reference_member",
         "interseries_calibration_series", ["interseries_generation_id", "reference_series_id"],
         ["interseries_generation_id", "series_id"], "a", []),
        ("interseries_calibration_generations", "fk_interseries_generations_reference_series_field",
         "field_series", ["reference_series_id", "field_id"], ["series_id", "field_id"], "r", []),
        ("interseries_calibration_series", "fk_interseries_series_generation_field",
         "interseries_calibration_generations", ["interseries_generation_id", "field_id"],
         ["interseries_generation_id", "field_id"], "c", []),
        ("interseries_calibration_series", "fk_interseries_series_series_field", "field_series",
           ["series_id", "field_id"], ["series_id", "field_id"], "n", ["series_id"]),
        ("interseries_calibration_series", "interseries_calibration_series_science_calibration_version_id_fkey",
         "calibration_versions", ["science_calibration_version_id"],
         ["calibration_version_id"], "r", []),
        ("interseries_calibration_series", "fk_interseries_series_science_cv_field",
         "calibration_versions", ["science_calibration_version_id", "field_id"],
         ["calibration_version_id", "field_id"], "a", []),
        ("interseries_calibration_series", "fk_interseries_series_science_cv_field_series",
         "calibration_versions", ["science_calibration_version_id", "field_id", "series_id"],
         ["calibration_version_id", "field_id", "series_id"], "a", []),
        ("interseries_calibration_edges", "interseries_calibration_edges_interseries_generation_id_fkey",
         "interseries_calibration_generations", ["interseries_generation_id"],
         ["interseries_generation_id"], "c", []),
        ("interseries_calibration_edges", "fk_interseries_edges_left_series_restrict",
         "field_series", ["left_series_id"], ["series_id"], "r", []),
        ("interseries_calibration_edges", "fk_interseries_edges_right_series_restrict",
          "field_series", ["right_series_id"], ["series_id"], "r", []),
        ("interseries_calibration_edges", "fk_interseries_edges_left_generation_member",
          "interseries_calibration_series", ["interseries_generation_id", "left_series_id"],
          ["interseries_generation_id", "series_id"], "a", []),
        ("interseries_calibration_edges", "fk_interseries_edges_right_generation_member",
          "interseries_calibration_series", ["interseries_generation_id", "right_series_id"],
          ["interseries_generation_id", "series_id"], "a", []),
    ]
    constraints.extend(
        {"child_table": child, "child_schema": "public", "name": name, "kind": "f",
         "parent_table": parent, "parent_schema": "public",
         "child_columns": cols, "parent_columns": target, "delete_action": action,
         "delete_columns": delete_cols, "update_action": "a", "match_type": "s",
           "validated": True, "deferrable": name.endswith(("_generation_member", "_reference_member")),
           "initially_deferred": name.endswith(("_generation_member", "_reference_member")),
          "server_version_num": 150000, "enforced": None,
          "is_local": True, "inherited_count": 0}
        for child, name, parent, cols, target, action, delete_cols in fks
    )
    return columns, constraints


@pytest.mark.parametrize("damage", ["missing", "nullable", "non_bigint"])
def test_runtime_m4_contract_requires_nonnull_bigint_science_cv(damage: str) -> None:
    columns, constraints = _m4_runtime_rows()
    _assert_m4_schema_rows(columns, constraints)
    column = next(row for row in columns if row["table_name"] == "interseries_calibration_series"
                  and row["column_name"] == "science_calibration_version_id")
    if damage == "missing":
        columns.remove(column)
    elif damage == "nullable":
        column["not_null"] = False
    else:
        column["is_bigint"] = False
    with pytest.raises(RuntimeError, match="M4 schema column"):
        _assert_m4_schema_rows(columns, constraints)


def test_m4_runtime_contract_refuses_partial_fk_delete_and_nullability() -> None:
    columns, constraints = _m4_runtime_rows()
    _assert_m4_schema_rows(columns, constraints)

    def fk_named(rows, name):
        return next(row for row in rows if row["name"] == name)

    def column_named(rows, key):
        return next(
            row for row in rows
            if (row["table_name"], row["column_name"]) == key
        )

    changes = (
        lambda cols, fks: fk_named(fks, "fk_interseries_series_series_field").update(
            delete_columns=[]
        ),
        lambda cols, fks: fk_named(fks, "fk_interseries_series_series_field").update(
            delete_columns=["series_id", "field_id"]
        ),
        lambda cols, fks: fk_named(fks, "fk_interseries_edges_left_series_restrict").update(
            delete_action="c"
        ),
        lambda cols, fks: fk_named(fks, "fk_interseries_edges_right_series_restrict").update(
            child_columns=["right_series_id", "left_series_id"]
        ),
        lambda cols, fks: fk_named(fks, "fk_interseries_edges_left_series_restrict").update(
            parent_schema="other"
        ),
        lambda cols, fks: column_named(
            cols, ("interseries_calibration_edges", "left_series_id")
        ).update(
            is_bigint=False
        ),
        lambda cols, fks: fks.append({
            **fk_named(fks, "fk_interseries_edges_left_series_restrict"),
            "name": "competing_left_fk",
        }),
        lambda cols, fks: fk_named(
            fks, "interseries_calibration_edges_interseries_generation_id_fkey"
        ).update(delete_action="r"),
        lambda cols, fks: fk_named(fks, "fk_interseries_edges_left_generation_member").update(
            initially_deferred=False
        ),
        lambda cols, fks: fk_named(fks, "fk_interseries_edges_right_generation_member").update(
            parent_columns=["series_id", "interseries_generation_id"]
        ),
    )
    for change in changes:
        changed_columns = [dict(row) for row in columns]
        changed_constraints = [dict(row) for row in constraints]
        change(changed_columns, changed_constraints)
        with pytest.raises(RuntimeError, match="M4"):
            _assert_m4_schema_rows(changed_columns, changed_constraints)


def test_runtime_m4_contract_requires_exact_five_edge_fks_and_endpoint_restrict() -> None:
    columns, constraints = _m4_runtime_rows()
    _assert_m4_schema_rows(columns, constraints)

    no_right_fk = [
        row for row in constraints
        if row.get("name") != "fk_interseries_edges_right_series_restrict"
    ]
    with pytest.raises(RuntimeError, match="M4"):
        _assert_m4_schema_rows(columns, no_right_fk)
    no_composite = [
        row for row in constraints
        if row.get("name") != "fk_interseries_edges_left_generation_member"
    ]
    with pytest.raises(RuntimeError, match="M4"):
        _assert_m4_schema_rows(columns, no_composite)
    extra = [*constraints, {
        **next(row for row in constraints if row.get("name") == "fk_interseries_edges_left_generation_member"),
        "name": "unapproved_extra_edge_fk",
    }]
    with pytest.raises(RuntimeError, match="M4"):
        _assert_m4_schema_rows(columns, extra)


def test_runtime_m4_contract_rejects_missing_or_changed_a2_b_fk_scopes() -> None:
    columns, constraints = _m4_runtime_rows()
    _assert_m4_schema_rows(columns, constraints)
    for name in ("fk_interseries_generations_reference_member",
                 "fk_interseries_series_science_cv_field",
                 "fk_interseries_series_science_cv_field_series"):
        without = [row for row in constraints if row["name"] != name]
        with pytest.raises(RuntimeError, match="M4"):
            _assert_m4_schema_rows(columns, without)
    for name, field, value in (
        ("fk_interseries_generations_reference_member", "initially_deferred", False),
        ("fk_interseries_generations_reference_member", "delete_action", "c"),
        ("fk_interseries_series_science_cv_field", "deferrable", True),
        ("fk_interseries_series_science_cv_field_series", "parent_columns",
         ["calibration_version_id", "series_id", "field_id"]),
        ("fk_interseries_series_science_cv_field_series", "match_type", "f"),
    ):
        changed = [dict(row) for row in constraints]
        next(row for row in changed if row["name"] == name)[field] = value
        with pytest.raises(RuntimeError, match="M4"):
            _assert_m4_schema_rows(columns, changed)
    for name in ("uq_calibration_versions_id_field", "uq_calibration_versions_id_field_series"):
        changed = [dict(row) for row in constraints]
        next(row for row in changed if row["name"] == name)["index_immediate"] = False
        with pytest.raises(RuntimeError, match="M4"):
            _assert_m4_schema_rows(columns, changed)


@pytest.mark.parametrize("version", [150000, 170000, 180000])
def test_runtime_m4_contract_checks_parent_index_and_pg18_enforcement(version) -> None:
    columns, constraints = _m4_runtime_rows()
    for row in constraints:
        if row["kind"] in {"f", "u"}:
            row["server_version_num"] = version
            row["enforced"] = True if version >= 180000 else None
    _assert_m4_schema_rows(columns, constraints)

    parent = next(row for row in constraints if row.get("name") ==
                  "uq_interseries_calibration_series_generation_series")
    for field in ("index_valid", "index_ready", "index_immediate", "deferrable"):
        changed = [dict(row) for row in constraints]
        next(row for row in changed if row.get("name") == parent["name"])[field] = (
            True if field == "deferrable" else False
        )
        with pytest.raises(RuntimeError, match="M4"):
            _assert_m4_schema_rows(columns, changed)
    if version >= 180000:
        for target in ("fk_interseries_edges_left_generation_member",
                       "fk_interseries_edges_right_generation_member",
                       "fk_interseries_generations_reference_member",
                       "fk_interseries_series_science_cv_field",
                       "fk_interseries_series_science_cv_field_series"):
            for bad in (False, None):
                changed = [dict(row) for row in constraints]
                next(row for row in changed if row.get("name") == target)["enforced"] = bad
                with pytest.raises(RuntimeError, match="M4"):
                    _assert_m4_schema_rows(columns, changed)


def test_complete_schema_runs_m4_catalog_check_but_missing_tables_do_not(monkeypatch) -> None:
    from grandgousier.db import schema_contract as contract

    columns, constraints = _m4_runtime_rows()
    calls: list[str] = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            calls.append("close")

        def execute(self, _statement):
            calls.append("catalog")
            data = columns if calls.count("catalog") == 1 else constraints
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: data))

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=FakeConnection)
    monkeypatch.setattr(contract, "inspect", lambda _engine: SimpleNamespace(
        get_table_names=lambda: list(CURRENT_SCHEMA_TABLES)
    ))
    assert_schema_contract(engine)
    assert calls == ["catalog", "catalog", "close"]

    calls.clear()
    monkeypatch.setattr(contract, "inspect", lambda _engine: SimpleNamespace(
        get_table_names=lambda: ["fields"]
    ))
    with pytest.raises(RuntimeError, match="missing tables"):
        assert_schema_contract(engine)
    assert calls == []
