from __future__ import annotations

from types import SimpleNamespace

from grandgousier.photometry.science_flags import (
    apply_top_level_flag_patch,
    exact_top_level_flag_patch,
)
from grandgousier.photometry.science_flag_overrides import (
    _intersect_common_overrides,
    _intersect_common_removed,
)


def test_exact_top_level_flag_patch_handles_overrides_and_removals() -> None:
    reconstructed = {
        "same": 1,
        "changed": 2,
        "removed": {"nested": True},
        "explicit_none": 5,
    }
    historical = {
        "same": 1,
        "changed": 3,
        "added": [1, 2],
        "explicit_none": None,
    }
    overrides, removed = exact_top_level_flag_patch(reconstructed, historical)
    assert overrides == {
        "changed": 3,
        "added": [1, 2],
        "explicit_none": None,
    }
    assert removed == ["removed"]
    assert apply_top_level_flag_patch(reconstructed, overrides, removed) == historical


def test_common_patch_intersection_factors_only_values_shared_by_every_row() -> None:
    common = None
    removed = None
    rows = [
        ({"a": 1, "b": 2}, ["x", "y"]),
        ({"a": 1, "b": 3, "c": 4}, ["x", "z"]),
        ({"a": 1}, ["x"]),
    ]
    for overrides, removed_keys in rows:
        common = _intersect_common_overrides(common, overrides)
        removed = _intersect_common_removed(removed, removed_keys)
    assert common == {"a": 1}
    assert removed == {"x"}


def test_exact_row_forces_calibration_common_patch_to_empty() -> None:
    common = _intersect_common_overrides(None, {"a": 1})
    removed = _intersect_common_removed(None, ["x"])
    common = _intersect_common_overrides(common, {})
    removed = _intersect_common_removed(removed, [])
    assert common == {}
    assert removed == set()

class _MappingsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _ApplySession:
    def __init__(self, manifest, residuals):
        self.manifest = manifest
        self.residuals = residuals
        self.calls = 0

    def execute(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return _MappingsResult([self.manifest] if self.manifest is not None else [])
        return _MappingsResult(self.residuals)


def test_apply_complete_science_flag_overrides_layers_common_then_row_residual() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        apply_complete_science_flag_overrides,
    )

    session = _ApplySession(
        {
            "common_override_flags": {"common": 2, "added": True},
            "common_removed_keys": ["remove_common"],
            "status": "COMPLETE_EXACT",
        },
        [
            {
                "base_measurement_id": 101,
                "override_flags": {"common": 3, "row": "x"},
                "removed_keys": ["remove_row"],
            }
        ],
    )
    states = {
        (1, 10): {
            "base_measurement_id": 101,
            "flags": {
                "common": 1,
                "keep": "yes",
                "remove_common": 1,
                "remove_row": 2,
            },
        },
        (2, 10): {
            "base_measurement_id": 102,
            "flags": {"common": 1, "remove_common": 1},
        },
    }
    out = apply_complete_science_flag_overrides(
        session, calibration_version_id=9, states=states
    )
    assert out[(1, 10)]["flags"] == {
        "common": 3,
        "keep": "yes",
        "added": True,
        "row": "x",
    }
    assert out[(2, 10)]["flags"] == {"common": 2, "added": True}


def test_apply_complete_science_flag_overrides_ignores_uncommitted_manifest() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        apply_complete_science_flag_overrides,
    )

    states = {(1, 10): {"base_measurement_id": 101, "flags": {"a": 1}}}
    session = _ApplySession(
        {
            "common_override_flags": {"a": 2},
            "common_removed_keys": [],
            "status": "BUILDING",
        },
        [],
    )
    out = apply_complete_science_flag_overrides(
        session, calibration_version_id=9, states=states
    )
    assert out is states
    assert out[(1, 10)]["flags"] == {"a": 1}


def test_backfill_dry_run_factors_common_patch_and_counts_only_residual_rows(monkeypatch) -> None:
    import grandgousier.calibration.reconstruction as reconstruction_module
    import grandgousier.photometry.science_flag_overrides as module

    cv = SimpleNamespace(
        calibration_version_id=9,
        name="science",
        product_role="SCIENCE",
        lifecycle_status="COMPLETE_IMMUTABLE",
    )

    class Session:
        def get(self, model, key):
            assert key == 9
            return cv

        def scalar(self, _stmt):
            return 2

    base_state = {field: 1.0 for field in reconstruction_module._RECONSTRUCTED_FIELDS}
    states = {
        (1, 10): {
            **base_state,
            "base_measurement_id": 101,
            "object_id": 1,
            "frame_id": 10,
            "flags": {"common": 0, "same": True},
        },
        (2, 10): {
            **base_state,
            "base_measurement_id": 102,
            "object_id": 2,
            "frame_id": 10,
            "flags": {"common": 0, "same": True},
        },
    }
    stored = []
    for object_id, base_id, row_value in ((1, 101, "a"), (2, 102, "b")):
        row = {
            **base_state,
            "base_measurement_id": base_id,
            "object_id": object_id,
            "frame_id": 10,
            "base_flags": {},
            "science_flags": {"common": 1, "same": True, "row": row_value},
        }
        stored.append(row)

    monkeypatch.setattr(module, "_frame_batches", lambda *_args, **_kwargs: [[10]])
    monkeypatch.setattr(
        module,
        "_stored_rows_for_frames",
        lambda *_args, **_kwargs: [dict(row) for row in stored],
    )
    monkeypatch.setattr(
        reconstruction_module,
        "reconstruct_calibration_measurement_states",
        lambda *_args, **_kwargs: SimpleNamespace(states={k: dict(v) for k, v in states.items()}),
    )

    result = module.backfill_science_flag_overrides(
        Session(), calibration_version_id=9, execute=False
    )
    assert result.status == "READY"
    assert result.source_rows == 2
    assert result.rows_requiring_patch == 2
    assert result.common_override_keys == 1
    assert result.common_removed_keys == 0
    assert result.row_override_rows == 2
    assert result.scalar_mismatches == 0
    assert result.identity_mismatches == 0
    assert result.patch_failures == 0

class _ScalarValues:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _SelectorSession:
    def __init__(self, values):
        self.values = list(values)
        self.sql = None
        self.params = None

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _ScalarValues(self.values)


def test_select_v1_science_calibration_ids_field_scope_requires_v1_rows() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        select_v1_science_calibration_ids,
    )

    session = _SelectorSession([766, 771, 960])
    result = select_v1_science_calibration_ids(
        session,
        field_id=33,
        include_superseded=True,
    )
    assert result == [766, 771, 960]
    assert "EXISTS" in session.sql
    assert "science_measurements" in session.sql
    assert session.params == {
        "field_id": 33,
        "statuses": ["COMPLETE_IMMUTABLE", "SUPERSEDED_IMMUTABLE"],
    }


def test_select_v1_science_calibration_ids_explicit_scope_preserves_requested_ids() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        select_v1_science_calibration_ids,
    )

    class NoExecuteSession:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("explicit selection must not query field scope")

    assert select_v1_science_calibration_ids(
        NoExecuteSession(), calibration_version_ids=[1094, 1094, 1101]
    ) == [1094, 1101]


def test_apply_complete_science_flag_overrides_accepts_retired_exact_manifest() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        apply_complete_science_flag_overrides,
    )

    states = {(1, 10): {"base_measurement_id": 101, "flags": {"a": 1}}}
    session = _ApplySession(
        {
            "common_override_flags": {"a": 2},
            "common_removed_keys": [],
            "status": "RETIRED_EXACT",
        },
        [],
    )
    out = apply_complete_science_flag_overrides(
        session, calibration_version_id=9, states=states
    )
    assert out[(1, 10)]["flags"] == {"a": 2}


def test_select_v1_science_calibration_ids_global_materialized_scope() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        select_v1_science_calibration_ids,
    )

    session = _SelectorSession([12, 34])
    result = select_v1_science_calibration_ids(
        session,
        all_materialized=True,
        include_superseded=True,
    )
    assert result == [12, 34]
    assert ":field_id" not in session.sql
    assert "EXISTS" in session.sql
    assert session.params == {
        "statuses": ["COMPLETE_IMMUTABLE", "SUPERSEDED_IMMUTABLE"],
    }


def test_select_v1_science_calibration_ids_rejects_ambiguous_global_scope() -> None:
    from grandgousier.photometry.science_flag_overrides import (
        select_v1_science_calibration_ids,
    )

    try:
        select_v1_science_calibration_ids(
            _SelectorSession([]), field_id=33, all_materialized=True
        )
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("ambiguous scope must fail closed")


def test_select_v1_science_calibration_ids_field_scope_uses_non_null_bind() -> None:
    from grandgousier.photometry.science_flag_overrides import select_v1_science_calibration_ids

    session = _SelectorSession([56])
    result = select_v1_science_calibration_ids(session, field_id=33)
    assert result == [56]
    assert "cv.field_id = :field_id" in session.sql
    assert "IS NULL" not in session.sql
    assert "EXISTS" in session.sql
    assert session.params == {
        "field_id": 33,
        "statuses": ["COMPLETE_IMMUTABLE"],
    }
