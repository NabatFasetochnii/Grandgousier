from __future__ import annotations

import re

from sqlalchemy import ForeignKeyConstraint

from grandgousier.calibration.provenance import (
    calibration_generation_archive_name,
    calibration_generation_pending_name,
    calibration_product_fingerprints,
    finalize_calibration_generation,
    prepare_calibration_version_for_write,
)
from grandgousier.db.models import Base, CalibrationProductAlias, CalibrationVersion


def test_calibration_version_has_typed_product_provenance_columns() -> None:
    table = Base.metadata.tables["calibration_versions"]
    expected = {
        "pipeline_run_id",
        "field_id",
        "series_id",
        "supersedes_calibration_version_id",
        "product_kind",
        "product_role",
        "materialization_kind",
        "lifecycle_status",
        "definition_fingerprint_sha256",
        "input_fingerprint_sha256",
        "provenance_fingerprint_sha256",
        "materialized_at",
    }
    assert expected.issubset(table.columns.keys())


def test_calibration_version_scope_and_supersedes_foreign_keys_are_in_orm_graph() -> None:
    table = Base.metadata.tables["calibration_versions"]
    targets = {
        element.target_fullname
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert "pipeline_runs.pipeline_run_id" in targets
    assert "fields.field_id" in targets
    assert "field_series.series_id" in targets
    assert "calibration_versions.calibration_version_id" in targets


def test_logical_product_fingerprints_are_stable_and_order_independent_for_frame_scope() -> None:
    cv = CalibrationVersion(
        calibration_version_id=42,
        name="test",
        model_name="ensemble_differential_v1",
        instrument_unit_id=7,
        source_calibration_version_id=41,
        field_id=2,
        series_id=3,
        product_kind="ENSEMBLE",
        product_role="INTERMEDIATE",
        materialization_kind="FULL_MEASUREMENTS",
        used_catalogs={"catalog_name": "gaia_dr3_master"},
        parameters={"clip_sigma": 3.0, "n_iterations": 2},
    )

    left = calibration_product_fingerprints(
        cv,
        frame_ids=[9, 3, 9, 5],
        source_provenance_fingerprint="a" * 64,
        pipeline_run_id=100,
    )
    right = calibration_product_fingerprints(
        cv,
        frame_ids=[5, 9, 3],
        source_provenance_fingerprint="a" * 64,
        pipeline_run_id=100,
    )

    assert left == right
    assert set(left) == {
        "definition_fingerprint_sha256",
        "input_fingerprint_sha256",
        "provenance_fingerprint_sha256",
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in left.values())


def test_definition_fingerprint_ignores_publication_role_changes() -> None:
    cv = CalibrationVersion(
        name="test",
        model_name="ensemble_differential_v1",
        product_kind="ENSEMBLE",
        product_role="INTERMEDIATE",
        materialization_kind="FULL_MEASUREMENTS",
        used_catalogs={},
        parameters={},
    )
    intermediate = calibration_product_fingerprints(cv, frame_ids=[1, 2], pipeline_run_id=1)
    cv.product_role = "SCIENCE"
    science = calibration_product_fingerprints(cv, frame_ids=[1, 2], pipeline_run_id=1)
    assert intermediate["definition_fingerprint_sha256"] == science["definition_fingerprint_sha256"]


class _FakeSession:
    def __init__(self, current: CalibrationVersion, source: CalibrationVersion | None = None) -> None:
        self.current = current
        self.source = source
        self.flush_count = 0

    def scalar(self, _statement):
        return self.current

    def get(self, _model, key):
        if self.source is not None and int(key) == int(self.source.calibration_version_id):
            return self.source
        return None

    def flush(self) -> None:
        self.flush_count += 1


def test_stamp_calibration_product_sets_scope_role_and_logical_fingerprints() -> None:
    from grandgousier.calibration.provenance import stamp_calibration_product

    source = CalibrationVersion(
        calibration_version_id=10,
        name="source",
        model_name="forced_aperture_physical_v1",
        product_kind="BASE_PHOTOMETRY",
        product_role="BASE",
        materialization_kind="FULL_MEASUREMENTS",
        provenance_fingerprint_sha256="b" * 64,
        used_catalogs={},
        parameters={},
    )
    current = CalibrationVersion(
        calibration_version_id=11,
        name="target",
        model_name="ensemble_differential_v1",
        source_calibration_version_id=10,
        used_catalogs={},
        parameters={"n_iterations": 2},
    )
    session = _FakeSession(current, source)

    stamped = stamp_calibration_product(
        session,  # type: ignore[arg-type]
        calibration_name="target",
        pipeline_run_id=99,
        field_id=4,
        series_id=5,
        frame_ids=[7, 8],
        product_kind="ENSEMBLE",
        product_role="SCIENCE",
        materialization_kind="FULL_MEASUREMENTS",
    )

    assert stamped is current
    assert current.pipeline_run_id == 99
    assert current.field_id == 4
    assert current.series_id == 5
    assert current.product_kind == "ENSEMBLE"
    assert current.product_role == "SCIENCE"
    assert current.lifecycle_status == "COMPATIBILITY_MUTABLE"
    assert current.materialized_at is not None
    assert re.fullmatch(r"[0-9a-f]{64}", current.definition_fingerprint_sha256 or "")
    assert re.fullmatch(r"[0-9a-f]{64}", current.input_fingerprint_sha256 or "")
    assert re.fullmatch(r"[0-9a-f]{64}", current.provenance_fingerprint_sha256 or "")
    assert session.flush_count == 1



def test_immutable_generation_schema_adds_logical_identity_and_alias_head() -> None:
    cv_table = Base.metadata.tables["calibration_versions"]
    assert {"logical_name", "generation_no", "finalized_at"}.issubset(cv_table.columns.keys())
    alias_table = Base.metadata.tables["calibration_product_aliases"]
    assert {
        "logical_name",
        "head_calibration_version_id",
        "head_generation_no",
        "updated_by_pipeline_run_id",
    }.issubset(alias_table.columns.keys())


def test_generation_physical_names_are_deterministic_and_bounded() -> None:
    logical = "x" * 128
    archive = calibration_generation_archive_name(logical, 12)
    pending = calibration_generation_pending_name(logical, 13)
    assert len(archive) <= 128
    assert len(pending) <= 128
    assert archive == calibration_generation_archive_name(logical, 12)
    assert pending == calibration_generation_pending_name(logical, 13)
    assert archive != pending


class _QueueSession:
    def __init__(self, scalars):
        self._scalars = list(scalars)
        self.added = []
        self.flush_count = 0

    def scalar(self, _statement):
        if not self._scalars:
            raise AssertionError("unexpected scalar() call")
        return self._scalars.pop(0)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        self.flush_count += 1

    def get(self, _model, _key):
        return None


def test_prepare_write_allocates_pending_generation_instead_of_rewriting_immutable_head() -> None:
    current = CalibrationVersion(
        calibration_version_id=100,
        name="logical",
        logical_name="logical",
        generation_no=1,
        lifecycle_status="COMPLETE_IMMUTABLE",
        model_name="ensemble_differential_v1",
        used_catalogs={},
        parameters={},
    )
    session = _QueueSession([current, 1])
    new = prepare_calibration_version_for_write(
        session,  # type: ignore[arg-type]
        logical_name="logical",
        model_name="ensemble_differential_v1",
        instrument_unit_id=7,
        used_catalogs={"catalog_name": "cat"},
        parameters={"n_iterations": 2},
        notes="test",
        immutable_generation=True,
    )
    assert new is not current
    assert new.name != "logical"
    assert new.logical_name == "logical"
    assert new.generation_no == 2
    assert new.supersedes_calibration_version_id == 100
    assert new.lifecycle_status == "MATERIALIZING"
    assert current.lifecycle_status == "COMPLETE_IMMUTABLE"
    assert session.added == [new]


def test_prepare_write_can_reuse_completed_immutable_head_read_only() -> None:
    current = CalibrationVersion(
        calibration_version_id=100,
        name="logical",
        logical_name="logical",
        generation_no=1,
        lifecycle_status="COMPLETE_IMMUTABLE",
        model_name="forced_aperture_physical_v1",
        used_catalogs={},
        parameters={},
    )
    session = _QueueSession([current])
    reused = prepare_calibration_version_for_write(
        session,  # type: ignore[arg-type]
        logical_name="logical",
        model_name="forced_aperture_physical_v1",
        instrument_unit_id=7,
        used_catalogs={},
        parameters={},
        notes=None,
        immutable_generation=True,
        reuse_completed_immutable=True,
    )
    assert reused is current
    assert session.added == []
    assert session.flush_count == 0


def test_finalize_generation_archives_old_head_and_updates_alias() -> None:
    old = CalibrationVersion(
        calibration_version_id=100,
        name="logical",
        logical_name="logical",
        generation_no=1,
        lifecycle_status="COMPLETE_IMMUTABLE",
        model_name="ensemble_differential_v1",
        used_catalogs={},
        parameters={},
    )
    new = CalibrationVersion(
        calibration_version_id=101,
        name="logical__pending_generation_g000002",
        logical_name="logical",
        generation_no=2,
        lifecycle_status="MATERIALIZING",
        model_name="ensemble_differential_v1",
        used_catalogs={},
        parameters={},
    )
    session = _QueueSession([old, None, None])
    finalized = finalize_calibration_generation(
        session,  # type: ignore[arg-type]
        cv=new,
        logical_name="logical",
        pipeline_run_id=55,
    )
    assert finalized is new
    assert old.name != "logical"
    assert old.lifecycle_status == "SUPERSEDED_IMMUTABLE"
    assert new.name == "logical"
    assert new.lifecycle_status == "COMPLETE_IMMUTABLE"
    assert new.supersedes_calibration_version_id == 100
    assert new.finalized_at is not None
    alias = next(value for value in session.added if isinstance(value, CalibrationProductAlias))
    assert alias.logical_name == "logical"
    assert alias.head_calibration_version_id == 101
    assert alias.head_generation_no == 2
    assert alias.updated_by_pipeline_run_id == 55
