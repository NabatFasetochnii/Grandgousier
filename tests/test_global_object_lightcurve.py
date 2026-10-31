from __future__ import annotations

import ctypes
import errno
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.lightcurves import global_object as m5


class _MappingsResult:
    def __init__(self, rows):
        self.rows = list(rows)
    def mappings(self):
        return self
    def all(self):
        return list(self.rows)
    def first(self):
        return self.rows[0] if self.rows else None
    def __iter__(self):
        return iter(self.rows)


def _minimal_m5_export_row() -> dict:
    return {
        "filter_name": "R",
        "is_rejected": False,
        "series_id": 77,
        "mjd": 60000.0,
        "date_obs": None,
        "mag_global": 12.3,
        "mag_err_random": 0.02,
        "mag_err_series_zp": 0.002,
        "interseries_generation_id": 1,
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
    }


def _configure_minimal_m5_export(monkeypatch, rows_factory):
    generation = m5.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_r",
        field_id=33,
        instrument_unit_id=8,
        filter_name="R",
        catalog_name="shared",
        reference_series_id=77,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((77, 967),),
    )
    coverage = [m5.SeriesCoverage(77, "R", 967, 967, 1, "READY_EXACT_M4_SOURCE")]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "R", "series_id": 77, "n_points": 1, "mjd_min": 60000.0, "mjd_max": 60000.0}
        ],
    )
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: rows_factory())
    monkeypatch.setattr(m5, "_to_m5_row", lambda raw, **kwargs: dict(raw))

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 42}])
            raise AssertionError(str(statement))

    return FakeSession()


def _forbid_m5_rmtree(monkeypatch):
    calls = []

    def reject_deletion(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("opt-in M5 export must not call rmtree")

    monkeypatch.setattr(m5.shutil, "rmtree", reject_deletion)
    return calls


def _fake_budget_m5_session(monkeypatch, rows_factory, *, counts=None):
    """Only synthetic SQL results: no engine, connection, config or real path reads."""
    session = _configure_minimal_m5_export(monkeypatch, rows_factory)
    counts = counts or {}
    queried = []

    def execute(statement, params):
        sql = " ".join(str(statement).split())
        queried.append(sql)
        if "SELECT COUNT(*)::bigint AS n" in sql:
            if "FROM fields fld" in sql:
                key = "field"
            elif "FROM field_series fs JOIN calibration_versions cv" in sql:
                key = "aliases"
            elif "FROM field_series fs WHERE" in sql:
                key = "series"
            elif "FROM interseries_calibration_generations g" in sql:
                key = "generations"
            elif "FROM calibration_versions cv" in sql:
                key = "cv"
            elif "FROM science_photometry s JOIN measurements bm" in sql:
                key = "foreign_base" if "<> :base" in sql else "object_points"
            elif "FROM science_photometry s WHERE" in sql:
                key = "science"
            elif "FROM measurements bm LEFT JOIN science_photometry s" in sql:
                key = "missing_science"
            elif "FROM measurements bm WHERE" in sql:
                key = "base"
            elif "FROM calibration_iterations it" in sql:
                key = "iteration"
            elif "FROM calibration_frame_terms ft" in sql:
                key = "frame_terms"
            elif "FROM calibration_iteration_objects ot" in sql:
                key = "object_terms"
            elif "FROM frames f JOIN field_series_frames fsf" in sql:
                key = "frames"
            elif "FROM object_qc qc" in sql:
                key = "qc"
            else:
                raise AssertionError(sql)
            return _MappingsResult([{
                "n": counts.get(key, 0 if key in {"foreign_base", "missing_science", "qc"} else 1),
                "largest": counts.get("largest", 0),
                "payload_bytes": counts.get("payload_bytes", 0),
            }])
        if sql.startswith("SELECT field_id, field_name FROM fields"):
            return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
        if sql.startswith("SELECT object_id, object_uid"):
            return _MappingsResult([{"object_id": 42, "object_uid": "safe"}])
        if sql.startswith("SELECT calibration_version_id, source_calibration_version_id"):
            cv = int(params["cv"])
            return _MappingsResult([{
                "calibration_version_id": cv,
                "source_calibration_version_id": 500 if cv == 967 else None,
                "product_kind": "ENSEMBLE" if cv == 967 else "BASE_PHOTOMETRY",
                "product_role": "SCIENCE" if cv == 967 else "BASE",
            }])
        if sql.startswith("SELECT calibration_iteration_id, iteration_kind"):
            return _MappingsResult([{"calibration_iteration_id": 111, "iteration_kind": "ENSEMBLE"}])
        raise AssertionError(sql)

    session.execute = execute
    session.get = lambda model, cv: SimpleNamespace(
        calibration_version_id=cv,
        source_calibration_version_id=500 if cv == 967 else None,
        product_kind="ENSEMBLE" if cv == 967 else "BASE_PHOTOMETRY",
        name=f"cv_{cv}",
    )
    session.scalar = lambda statement: SimpleNamespace(calibration_iteration_id=111)
    return session, queried


def _budget_m5_export(session, root: Path, **options):
    return m5.export_global_object_light_curve(
        session, object_id=42, field_id=33, out_root=root,
        plots=False, preserve_staging_on_failure=True,
        enforce_export_budget=True, **options,
    )


def _explicit_m4_fixture():
    generations = [
        m5.M5Generation(
            interseries_generation_id=gen_id, generation_uid=f"m4_{gen_id}", field_id=33,
            instrument_unit_id=8, filter_name=filter_name, catalog_name="shared",
            reference_series_id=start, reliability_status="REDUNDANT_GRAPH_OK", graph_dof=1,
            series_sources=tuple((series_id, 900 + series_id)
                                 for series_id in range(start, start + 9)),
        )
        for gen_id, start, filter_name in ((3, 1, "R"), (4, 10, "V"))
    ]
    heads = []
    for series_id in range(1, 19):
        for cv_id in ((900 + series_id, 1200 + series_id)
                      if series_id <= 14 else (900 + series_id,)):
            heads.append({
                "series_id": series_id, "series_field_id": 33,
                "series_instrument_unit_id": 8,
                "filter_name": "R" if series_id <= 9 else "V",
                "calibration_version_id": cv_id, "calibration_field_id": 33,
                "calibration_series_id": series_id, "calibration_product_role": "SCIENCE",
                "calibration_instrument_unit_id": 8, "catalog_name": "shared",
                "alias_logical_name": f"science_{cv_id}", "has_science_alias_head": True,
            })
    return generations, heads


class _ExplicitM4Session:
    """Fake only: exercise the real opt-in budget and planner explicit-ID reader."""

    def __init__(self, heads, *, aliases=None, cv_changes=None, point_count=1):
        self.heads = heads
        self.cv_changes = cv_changes or {}
        self.point_count = point_count
        self.aliases = aliases or {
            series_id: (2 if series_id <= 14 else 1) for series_id in range(1, 19)
        }
        self.queries = []

    def execute(self, statement, params):
        sql = " ".join(str(statement).split())
        self.queries.append(sql)
        if sql.startswith("SELECT field_id, field_name FROM fields"):
            return _MappingsResult([{"field_id": 33, "field_name": "synthetic"}])
        if sql.startswith("SELECT object_id, object_uid"):
            return _MappingsResult([{"object_id": 42, "object_uid": "synthetic"}])
        if "SELECT COUNT(*)::bigint AS n" in sql:
            if "FROM fields fld" in sql:
                n = 1
            elif ("FROM field_series fs WHERE" in sql
                  or "FROM interseries_calibration_generations g" in sql):
                n = 18
            elif "FROM field_series fs JOIN calibration_versions cv" in sql:
                n = 32
            else:
                raise AssertionError(sql)
            return _MappingsResult([{"n": n, "largest": 0, "payload_bytes": 0}])
        if "GROUP BY fs.series_id ORDER BY fs.series_id" in sql:
            return _MappingsResult([
                {"series_id": sid, "n_alias": n, "n_distinct_cv": n, "n_invalid": 0,
                 "largest": 0, "payload_bytes": 4}
                for sid, n in sorted(self.aliases.items())
            ])
        if sql.startswith("SELECT calibration_version_id, field_id, series_id, instrument_unit_id"):
            return _MappingsResult([
                dict({"calibration_version_id": cv_id, "field_id": 33,
                      "series_id": cv_id - 900, "instrument_unit_id": 8,
                      "product_role": "SCIENCE", "lifecycle_status": "COMPLETE_IMMUTABLE"},
                     **self.cv_changes.get(cv_id, {}))
                for cv_id in params["ids"]
            ])
        if "FROM field_series fs LEFT JOIN LATERAL" in sql:
            return _MappingsResult(self.heads)
        if "SELECT s.calibration_version_id, COUNT(*)::bigint AS n_points" in sql:
            return _MappingsResult([
                {"calibration_version_id": cv_id, "n_points": self.point_count}
                for cv_id in params["calibration_ids"]
            ])
        raise AssertionError(sql)


def test_m5_explicit_m4_18_series_32_distinct_heads_and_default_ambiguity() -> None:
    generations, heads = _explicit_m4_fixture()
    session = _ExplicitM4Session(heads)
    with pytest.raises(ValueError, match="multiple current SCIENCE alias heads"):
        m5.inspect_m5_series_coverage(
            session, field_id=33, object_id=42, generations=generations,
        )
    budget = m5._M5ExportBudget()
    multiplicity = m5._m5_budget_early_scope(
        session, field_id=33, generation_ids=[3, 4], budget=budget,
        use_explicit_m4_source_heads=True,
    )
    assert len(multiplicity) == 18
    assert sum(multiplicity.values()) == 32
    assert budget.payload_bytes == 18 * 4  # Alias decoded bytes counted once.
    ids = m5._m5_explicit_m4_source_ids(session, field_id=33, generations=generations)
    assert len(ids) == len(set(ids)) == 18
    coverage = m5.inspect_m5_series_coverage(
        session, field_id=33, object_id=42, generations=generations,
        science_calibration_version_ids=ids,
    )
    assert len(coverage) == 18
    assert all(row.status == "READY_EXACT_M4_SOURCE" for row in coverage)
    assert any("FROM field_series fs LEFT JOIN LATERAL" in sql for sql in session.queries)


def test_m5_explicit_m4_18_series_32_heads_api_fake_export(monkeypatch, tmp_path: Path) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    generations, heads = _explicit_m4_fixture()
    session = _ExplicitM4Session(heads)
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: generations)
    # Only the SCIENCE reconstruction/row iterator is stubbed here. The alias
    # budget, actual explicit-ID planner reader, and M5 coverage run on fake SQL.
    def bounded_science(_session, *, object_id, generations, budget):
        assert object_id == 42 and len(generations) == 2
        budget.object_rows = 18

    monkeypatch.setattr(m5, "_m5_budget_preflight", bounded_science)
    monkeypatch.setattr(m5, "_load_plot_source_stats", lambda *args, **kwargs: [
        {"filter_name": "R", "series_id": 1, "n_points": 9, "mjd_min": 1.0, "mjd_max": 2.0},
        {"filter_name": "V", "series_id": 10, "n_points": 9, "mjd_min": 1.0, "mjd_max": 2.0},
    ])
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: (
        dict(_minimal_m5_export_row(), series_id=sid,
             filter_name="R" if sid <= 9 else "V") for sid in range(1, 19)
    ))
    monkeypatch.setattr(m5, "_to_m5_row", lambda raw, **kwargs: dict(raw))
    result = _budget_m5_export(
        session, tmp_path / "selected", generation_ids=[3, 4],
        use_explicit_m4_source_heads=True,
    )
    assert result.rows_source_exact_m4 == result.accepted_rows == result.rows_written == 18
    assert result.rejected_rows == 0
    assert result.selection_provenance["field_science_alias_rows"] == 32
    assert result.selection_provenance["field_series_count"] == 18
    assert result.selection_provenance["csv_records"] == 18
    assert len(result.selection_provenance["selected_sources"]) == 18
    assert calls == []


@pytest.mark.parametrize("aliases", [
    {**{sid: 2 for sid in range(1, 19)}, 1: 3},
    {sid: 2 for sid in range(1, 20)},
])
def test_m5_explicit_m4_alias_caps_refuse_before_reader_or_staging(aliases) -> None:
    _generations, heads = _explicit_m4_fixture()
    session = _ExplicitM4Session(heads, aliases=aliases)
    with pytest.raises(ValueError, match="M5 budget"):
        m5._m5_budget_early_scope(
            session, field_id=33, generation_ids=[3, 4], budget=m5._M5ExportBudget(),
            use_explicit_m4_source_heads=True,
        )
    assert not any("FROM field_series fs LEFT JOIN LATERAL" in sql for sql in session.queries)


@pytest.mark.parametrize("size", ["item", "cumulative"])
def test_m5_explicit_m4_alias_decoded_byte_ceiling_refuses_before_reader(size) -> None:
    _generations, heads = _explicit_m4_fixture()
    session = _ExplicitM4Session(heads)
    original_execute = session.execute

    def oversize(statement, params):
        result = original_execute(statement, params)
        if "GROUP BY fs.series_id ORDER BY fs.series_id" in str(statement):
            rows = result.all()
            rows[0]["largest" if size == "item" else "payload_bytes"] = (
                m5._M5_BUDGET_ITEM_BYTES + 1 if size == "item"
                else m5._M5_BUDGET_JSON_BYTES + 1
            )
            return _MappingsResult(rows)
        return result

    session.execute = oversize
    with pytest.raises(ValueError, match="decoded payload"):
        m5._m5_budget_early_scope(
            session, field_id=33, generation_ids=[3, 4], budget=m5._M5ExportBudget(),
            use_explicit_m4_source_heads=True,
        )
    assert not any("FROM field_series fs LEFT JOIN LATERAL" in sql for sql in session.queries)


@pytest.mark.parametrize("change", [
    {"field_id": 44}, {"series_id": 2}, {"instrument_unit_id": 9},
    {"product_role": "BASE"}, {"lifecycle_status": "LEGACY_MUTABLE"},
])
def test_m5_explicit_m4_bad_cv_refuses_before_alias_reader(change) -> None:
    generations, heads = _explicit_m4_fixture()
    session = _ExplicitM4Session(heads, cv_changes={901: change})
    with pytest.raises(ValueError, match="source CV is invalid"):
        m5._m5_explicit_m4_source_ids(session, field_id=33, generations=generations)
    assert not any("FROM field_series fs LEFT JOIN LATERAL" in sql for sql in session.queries)


@pytest.mark.parametrize("problem", ["missing_head", "wrong_filter", "wrong_catalog"])
def test_m5_explicit_m4_missing_or_wrong_scope_head_refuses(problem) -> None:
    generations, heads = _explicit_m4_fixture()
    if problem == "missing_head":
        heads = [row for row in heads if row["calibration_version_id"] != 901]
    else:
        heads[0]["filter_name" if problem == "wrong_filter" else "catalog_name"] = "OTHER"
    session = _ExplicitM4Session(heads)
    ids = m5._m5_explicit_m4_source_ids(session, field_id=33, generations=generations)
    with pytest.raises(ValueError, match="current SCIENCE alias head|outside generation scope"):
        m5.inspect_m5_series_coverage(
            session, field_id=33, object_id=42, generations=generations,
            science_calibration_version_ids=ids,
        )


def test_m5_explicit_m4_duplicate_source_refuses() -> None:
    generations, heads = _explicit_m4_fixture()
    generations[0] = m5.M5Generation(
        **{**generations[0].__dict__, "series_sources": generations[0].series_sources + ((1, 901),)}
    )
    with pytest.raises(ValueError, match="repeats a series"):
        m5._m5_explicit_m4_source_ids(
            _ExplicitM4Session(heads), field_id=33, generations=generations,
        )


@pytest.mark.parametrize("points", [0, 1])
def test_m5_explicit_m4_unselected_series_refuses_even_without_object_points(points) -> None:
    generations, heads = _explicit_m4_fixture()
    generations[1] = m5.M5Generation(
        **{**generations[1].__dict__, "series_sources": generations[1].series_sources[:-1]}
    )
    session = _ExplicitM4Session(heads, point_count=points)
    ids = m5._m5_explicit_m4_source_ids(session, field_id=33, generations=generations)
    with pytest.raises(ValueError, match="do not cover every field series"):
        m5.inspect_m5_series_coverage(
            session, field_id=33, object_id=42, generations=generations,
            science_calibration_version_ids=ids,
        )
    assert not any("FROM science_photometry s" in sql for sql in session.queries)


@pytest.mark.parametrize("override", [
    {"enforce_export_budget": False}, {"preserve_staging_on_failure": False},
    {"plots": True}, {"include_rejected_debug": True}, {"allow_partial": True},
    {"generation_ids": None}, {"generation_ids": []}, {"generation_ids": [3, 3]},
])
def test_m5_explicit_m4_bad_options_refuse_before_db_and_fs(tmp_path: Path, override) -> None:
    class NoQuery:
        def execute(self, *_args, **_kwargs):
            pytest.fail("invalid opt-in must not query")

    options = {
        "object_id": 42, "field_id": 33,
        "out_root": tmp_path / "missing" / "object42",
        "enforce_export_budget": True, "preserve_staging_on_failure": True,
        "plots": False, "include_rejected_debug": False, "allow_partial": False,
        "generation_ids": [3, 4], "use_explicit_m4_source_heads": True,
    }
    options.update(override)
    with pytest.raises(ValueError):
        m5.export_global_object_light_curve(NoQuery(), **options)
    assert not (tmp_path / "missing").exists()


def test_m5_budget_requires_preserve_before_any_db_or_fs(monkeypatch, tmp_path: Path) -> None:
    _forbid_m5_rmtree(monkeypatch)

    class RefuseSession:
        def execute(self, *_args, **_kwargs):
            pytest.fail("budget must refuse before DB access")

    with pytest.raises(ValueError, match="preserve_staging_on_failure"):
        m5.export_global_object_light_curve(
            RefuseSession(), object_id=42, field_id=33,
            out_root=tmp_path / "missing_parent" / "object42",
            enforce_export_budget=True, plots=False,
        )
    assert not (tmp_path / "missing_parent").exists()


def test_m5_budget_limits_generation_iterable_before_db(monkeypatch, tmp_path: Path) -> None:
    _forbid_m5_rmtree(monkeypatch)

    class RefuseSession:
        def execute(self, *_args, **_kwargs):
            pytest.fail("overlong selection must refuse before DB access")

    with pytest.raises(ValueError, match="generation selection exceeds ceiling"):
        m5.export_global_object_light_curve(
            RefuseSession(), object_id=42, field_id=33, out_root=tmp_path / "object42",
            plots=False, preserve_staging_on_failure=True, enforce_export_budget=True,
            generation_ids=iter(range(10**9)),
        )
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


@pytest.mark.parametrize("option", ["plots", "include_rejected_debug"])
def test_m5_budget_refuses_unbounded_side_outputs_before_db(
    monkeypatch, tmp_path: Path, option: str,
) -> None:
    _forbid_m5_rmtree(monkeypatch)

    class RefuseSession:
        def execute(self, *_args, **_kwargs):
            pytest.fail("side-output refusal must precede DB access")

    with pytest.raises(ValueError, match="plots=False and include_rejected_debug=False"):
        m5.export_global_object_light_curve(
            RefuseSession(), object_id=42, field_id=33, out_root=tmp_path / "object42",
            plots=option == "plots", include_rejected_debug=option == "include_rejected_debug",
            preserve_staging_on_failure=True, enforce_export_budget=True,
        )
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


@pytest.mark.parametrize("scope", ["series", "aliases", "generations"])
def test_m5_budget_bounds_field_wide_reads_before_selection(
    monkeypatch, tmp_path: Path, scope: str,
) -> None:
    _forbid_m5_rmtree(monkeypatch)
    session, queried = _fake_budget_m5_session(
        monkeypatch, lambda: iter([_minimal_m5_export_row()]), counts={scope: 19},
    )
    with pytest.raises(ValueError, match="M5 budget"):
        _budget_m5_export(session, tmp_path / "object42")
    assert queried
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


@pytest.mark.parametrize("broken", ["missing", "cycle", "iteration"])
def test_m5_budget_refuses_broken_reconstruction_metadata_before_staging(
    monkeypatch, tmp_path: Path, broken: str,
) -> None:
    _forbid_m5_rmtree(monkeypatch)
    session, _ = _fake_budget_m5_session(
        monkeypatch, lambda: iter([_minimal_m5_export_row()]),
    )
    execute = session.execute

    def broken_execute(statement, params):
        sql = " ".join(str(statement).split())
        if sql.startswith("SELECT calibration_version_id, source_calibration_version_id"):
            if broken == "missing" and params["cv"] == 500:
                return _MappingsResult([])
            if broken == "cycle" and params["cv"] == 500:
                return _MappingsResult([{
                    "calibration_version_id": 500,
                    "source_calibration_version_id": 967,
                    "product_kind": "ENSEMBLE",
                    "product_role": "INTERMEDIATE",
                }])
        if broken == "iteration" and sql.startswith("SELECT calibration_iteration_id, iteration_kind"):
            return _MappingsResult([])
        return execute(statement, params)

    session.execute = broken_execute
    root = tmp_path / "object42"
    with pytest.raises(ValueError, match="M5 budget"):
        _budget_m5_export(session, root)
    assert not root.exists()
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


@pytest.mark.parametrize("name,ceiling", [
    ("science", 600001), ("object_points", 3001), ("base", 1001),
    ("frame_terms", 30001), ("object_terms", 100001),
    ("largest", 65537), ("payload_bytes", 67108865),
])
def test_m5_budget_refuses_excess_before_staging(
    monkeypatch, tmp_path: Path, name: str, ceiling: int,
) -> None:
    _forbid_m5_rmtree(monkeypatch)
    session, queried = _fake_budget_m5_session(
        monkeypatch, lambda: iter([_minimal_m5_export_row()]), counts={name: ceiling},
    )
    root = tmp_path / "object42"
    with pytest.raises(ValueError, match="M5 budget"):
        _budget_m5_export(session, root)
    assert queried
    assert not root.exists()
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


def test_m5_budget_total_object_points_refuses_before_staging(
    monkeypatch, tmp_path: Path,
) -> None:
    rmtree_calls = _forbid_m5_rmtree(monkeypatch)

    def no_rows():
        pytest.fail("object-point budget must refuse before row iteration")

    session, queried = _fake_budget_m5_session(monkeypatch, no_rows)
    original_preflight = m5._m5_budget_preflight

    def preflight_with_previous_cv(session, *, object_id, generations, budget):
        budget.object_rows = 3000  # Previous CVs were individually <=3000.
        return original_preflight(
            session, object_id=object_id, generations=generations, budget=budget,
        )

    monkeypatch.setattr(m5, "_m5_budget_preflight", preflight_with_previous_cv)
    original_mkdir = Path.mkdir

    def refuse_parent_creation(path, *args, **kwargs):
        if path == tmp_path:
            pytest.fail("budget refusal must precede parent creation")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_parent_creation)
    monkeypatch.setattr(
        m5.tempfile, "mkdtemp",
        lambda *args, **kwargs: pytest.fail("budget refusal must precede staging"),
    )
    root = tmp_path / "object42"
    with pytest.raises(ValueError, match="total object source points exceed ceiling"):
        _budget_m5_export(session, root)

    assert any("FROM science_photometry s JOIN measurements bm" in sql for sql in queried)
    assert not any("FROM calibration_frame_terms ft" in sql for sql in queried)
    assert not root.exists()
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []
    assert rmtree_calls == []


@pytest.mark.parametrize("mode", ["symlink", "reparse"])
def test_m5_budget_refuses_unsafe_parent_without_db(
    monkeypatch, tmp_path: Path, mode: str,
) -> None:
    _forbid_m5_rmtree(monkeypatch)
    original_lstat = Path.lstat

    def unsafe_lstat(path, *args, **kwargs):
        if path == tmp_path:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if mode == "symlink" else stat.S_IFDIR,
                st_file_attributes=0x400 if mode == "reparse" else 0,
            )
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", unsafe_lstat)

    class RefuseSession:
        def execute(self, *_args, **_kwargs):
            pytest.fail("unsafe parent must refuse before DB access")

    with pytest.raises(ValueError, match="non-reparse"):
        _budget_m5_export(RefuseSession(), tmp_path / "object42")


@pytest.mark.parametrize("existing", ["bundle", "legacy"])
def test_m5_budget_refuses_existing_destination_or_legacy_before_db(
    monkeypatch, tmp_path: Path, existing: str,
) -> None:
    _forbid_m5_rmtree(monkeypatch)
    root = tmp_path / "object42"
    if existing == "bundle":
        root.mkdir()
        marker = root / "keep.bin"
    else:
        marker = tmp_path / "object42_rejections.csv"
    marker.write_bytes(b"unchanged")

    class RefuseSession:
        def execute(self, *_args, **_kwargs):
            pytest.fail("destination refusal must precede DB access")

    with pytest.raises(FileExistsError):
        _budget_m5_export(RefuseSession(), root)
    assert marker.read_bytes() == b"unchanged"
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []


def test_m5_budget_csv_and_summary_match_unbounded_small_fixture(
    monkeypatch, tmp_path: Path,
) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    rows = lambda: iter([_minimal_m5_export_row()])
    budget_session, queried = _fake_budget_m5_session(monkeypatch, rows)
    budget_root = tmp_path / "bounded"
    bounded = _budget_m5_export(budget_session, budget_root)
    assert any("octet_length((ft.details)::text)" in sql for sql in queried)
    assert any("octet_length((bm.flags)::text)" in sql for sql in queried)
    assert any("octet_length((f.quality_flags)::text)" in sql for sql in queried)
    assert any("octet_length((qc.flags)::text)" in sql for sql in queried)
    assert any("octet_length((ot.details)::text)" in sql for sql in queried)
    assert all("pg_column_size" not in sql for sql in queried)
    # Fresh independent fake for the unchanged no-budget route, also preserving
    # staging to prevent any rmtree call in this focused test.
    default_session = _configure_minimal_m5_export(monkeypatch, rows)
    default_root = tmp_path / "unbounded"
    ordinary = m5.export_global_object_light_curve(
        default_session, object_id=42, field_id=33, out_root=default_root,
        plots=False, preserve_staging_on_failure=True,
    )
    assert Path(bounded.csv_path).read_bytes() == Path(ordinary.csv_path).read_bytes()
    left = json.loads(Path(bounded.summary_path).read_text(encoding="utf-8"))
    right = json.loads(Path(ordinary.summary_path).read_text(encoding="utf-8"))
    for key in ("csv_path", "summary_path"):
        left.pop(key)
        right.pop(key)
    assert left == right
    assert calls == []


def _fake_small_explicit_m4_export(monkeypatch, rows_factory):
    """One selected M4 source and one current SCIENCE head, with no real DB."""
    real_coverage = m5.inspect_m5_series_coverage
    session, queried = _fake_budget_m5_session(monkeypatch, rows_factory)
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", real_coverage)
    original_execute = session.execute

    def execute(statement, params):
        sql = " ".join(str(statement).split())
        if "GROUP BY fs.series_id ORDER BY fs.series_id" in sql:
            return _MappingsResult([{
                "series_id": 77, "n_alias": 1, "n_distinct_cv": 1, "n_invalid": 0,
                "largest": 0, "payload_bytes": 4,
            }])
        if sql.startswith("SELECT calibration_version_id, field_id, series_id, instrument_unit_id"):
            assert params["ids"] == [967]
            return _MappingsResult([{
                "calibration_version_id": 967, "field_id": 33, "series_id": 77,
                "instrument_unit_id": 8, "product_role": "SCIENCE",
                "lifecycle_status": "COMPLETE_IMMUTABLE",
            }])
        if "FROM field_series fs LEFT JOIN LATERAL" in sql:
            return _MappingsResult([{
                "series_id": 77, "series_field_id": 33, "series_instrument_unit_id": 8,
                "filter_name": "R", "calibration_version_id": 967,
                "calibration_field_id": 33, "calibration_series_id": 77,
                "calibration_product_role": "SCIENCE", "calibration_instrument_unit_id": 8,
                "catalog_name": "shared", "alias_logical_name": "science_967",
                "has_science_alias_head": True,
            }])
        if "SELECT s.calibration_version_id, COUNT(*)::bigint AS n_points" in sql:
            return _MappingsResult([{"calibration_version_id": 967, "n_points": 1}])
        return original_execute(statement, params)

    session.execute = execute
    return session, queried


def test_m5_explicit_m4_summary_provenance_preserves_default_numeric_csv(
    monkeypatch, tmp_path: Path,
) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    rows = lambda: iter([_minimal_m5_export_row()])
    explicit_session, _queried = _fake_small_explicit_m4_export(monkeypatch, rows)
    selected = _budget_m5_export(
        explicit_session, tmp_path / "selected", generation_ids=[1],
        use_explicit_m4_source_heads=True,
    )
    ordinary_session, _ = _fake_budget_m5_session(monkeypatch, rows)
    ordinary = m5.export_global_object_light_curve(
        ordinary_session, object_id=42, field_id=33, out_root=tmp_path / "ordinary",
        generation_ids=[1], plots=False, preserve_staging_on_failure=True,
        enforce_export_budget=True,
    )
    assert Path(selected.csv_path).read_bytes() == Path(ordinary.csv_path).read_bytes()
    with (Path(selected.summary_path).open(encoding="utf-8") as summary_file,
          Path(ordinary.summary_path).open(encoding="utf-8") as ordinary_file):
        summary = json.load(summary_file)
        default_summary = json.load(ordinary_file)
    provenance = summary.pop("selection_provenance")
    assert provenance["mode"] == "EXPLICIT_M4_SOURCE_CURRENT_ALIAS_HEADS"
    assert provenance["requested_generation_ids"] == [1]
    assert provenance["selected_sources"] == [
        {"generation_id": 1, "series_id": 77, "science_calibration_version_id": 967,
         "current_alias_head_match": True, "field_science_alias_rows": 1}
    ]
    assert provenance["csv_records"] == selected.rows_written == 1
    assert "selection_provenance" not in default_summary
    for item in (summary, default_summary):
        item.pop("csv_path")
        item.pop("summary_path")
    assert summary == default_summary
    assert selected.accepted_rows == ordinary.accepted_rows == 1
    assert calls == []


@pytest.mark.parametrize("failure", ["short_iterator", "rename_collision"])
def test_m5_explicit_m4_failure_preserves_stage_without_recursive_delete(
    monkeypatch, tmp_path: Path, failure: str,
) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    rows = lambda: iter([] if failure == "short_iterator" else [_minimal_m5_export_row()])
    session, _ = _fake_small_explicit_m4_export(monkeypatch, rows)
    root = tmp_path / "selected"
    if failure == "rename_collision":
        publish = m5._rename_directory_noreplace

        def collide(source, destination):
            root.mkdir()
            (root / "keep.bin").write_bytes(b"existing destination")
            publish(source, destination)

        monkeypatch.setattr(m5, "_rename_directory_noreplace", collide)
        expected = FileExistsError
    else:
        expected = ValueError
    with pytest.raises(expected):
        _budget_m5_export(
            session, root, generation_ids=[1], use_explicit_m4_source_heads=True,
        )
    if failure == "rename_collision":
        assert (root / "keep.bin").read_bytes() == b"existing destination"
    else:
        assert not root.exists()
    stages = list(tmp_path.glob(".selected.m5-staging-*"))
    assert len(stages) == 1
    assert (stages[0] / "selected.csv").exists()
    assert calls == []


@pytest.mark.parametrize("limit", ["csv", "summary"])
def test_m5_budget_byte_ceiling_preserves_partial_stage(
    monkeypatch, tmp_path: Path, limit: str,
) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    session, _ = _fake_budget_m5_session(
        monkeypatch, lambda: iter([_minimal_m5_export_row()]),
    )
    if limit == "csv":
        monkeypatch.setattr(m5, "_M5_BUDGET_CSV_BYTES", len(m5._m5_budget_csv_bytes(None)) + 1)
    else:
        monkeypatch.setattr(m5, "_M5_BUDGET_SUMMARY_BYTES", 10)
    root = tmp_path / "object42"
    with pytest.raises(ValueError, match="byte ceiling"):
        _budget_m5_export(session, root)
    assert not root.exists()
    stages = list(tmp_path.glob(".object42.m5-staging-*"))
    assert len(stages) == 1
    assert (stages[0] / "object42.csv").is_file()
    assert not (stages[0] / "object42_summary.json").exists()
    if limit == "csv":
        assert (stages[0] / "object42.csv").read_bytes() == m5._m5_budget_csv_bytes(None)
    assert calls == []


def test_m5_budget_race_collision_preserves_destination_and_stage(
    monkeypatch, tmp_path: Path,
) -> None:
    calls = _forbid_m5_rmtree(monkeypatch)
    session, _ = _fake_budget_m5_session(
        monkeypatch, lambda: iter([_minimal_m5_export_row()]),
    )
    root = tmp_path / "object42"
    publish = m5._rename_directory_noreplace

    def race(source, destination):
        root.mkdir()
        (root / "keep.bin").write_bytes(b"racing writer")
        publish(source, destination)

    monkeypatch.setattr(m5, "_rename_directory_noreplace", race)
    with pytest.raises(FileExistsError):
        _budget_m5_export(session, root)
    assert (root / "keep.bin").read_bytes() == b"racing writer"
    stages = list(tmp_path.glob(".object42.m5-staging-*"))
    assert len(stages) == 1
    assert (stages[0] / "object42.csv").is_file()
    assert (stages[0] / "object42_summary.json").is_file()
    assert calls == []


def test_m5_columns_keep_random_and_correlated_zp_separate() -> None:
    columns = m5.global_light_curve_columns()
    for name in (
        "mag_science",
        "mag_global",
        "mag_err_random",
        "mag_err_science_systematic",
        "mag_err_series_zp",
        "mag_err_effective",
        "series_zp_correlated",
        "science_calibration_version_id",
        "interseries_generation_id",
        "interseries_reliability",
        "m5_link_status",
    ):
        assert name in columns


def test_to_m5_row_applies_additive_offset_and_keeps_error_semantics() -> None:
    raw = {
        "science_measurement_id": 10,
        "measurement_id": 10,
        "object_id": 5,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.345,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.003,
        "interseries_offset_err": 0.004,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 2,
        "interseries_generation_uid": "m4_x",
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "interseries_graph_dof": 0,
        "interseries_reference_series_id": 76,
    }
    row = m5._to_m5_row(raw, reject_flags=set(), field_id=33)
    assert row["mag_science"] == pytest.approx(12.345)
    assert row["mag_global"] == pytest.approx(12.348)
    assert row["mag_err_random"] == pytest.approx(0.02)
    assert row["mag_err_series_zp"] == pytest.approx(0.004)
    assert row["mag_err_effective"] == pytest.approx((0.02**2 + 0.004**2) ** 0.5)
    assert row["series_zp_correlated"] is True
    assert row["m5_link_status"] == "M4_EXACT_SOURCE_MATCH"
    assert row["is_rejected"] is False


def test_auto_generation_selection_refuses_ambiguous_same_scope() -> None:
    rows = []
    for gid, uid, cv in ((1, "m4_a", 960), (3, "m4_b", 1001)):
        rows.append({
            "interseries_generation_id": gid,
            "generation_uid": uid,
            "field_id": 33,
            "instrument_unit_id": 7,
            "filter_name": "V",
            "catalog_name": "shared",
            "reference_series_id": 76,
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "graph_dof": 1,
            "series_id": 76,
            "science_calibration_version_id": cv,
        })

    class FakeSession:
        def execute(self, statement, params):
            return _MappingsResult(rows)

    with pytest.raises(ValueError, match="Ambiguous M4 generations"):
        m5.resolve_m5_generations(FakeSession(), field_id=33)


def test_explicit_generation_selection_allows_distinct_filter_scopes() -> None:
    rows = [
        {
            "interseries_generation_id": 1,
            "generation_uid": "m4_r",
            "field_id": 33,
            "instrument_unit_id": 8,
            "filter_name": "R",
            "catalog_name": "shared",
            "reference_series_id": 77,
            "reliability_status": "REDUNDANT_GRAPH_OK",
            "graph_dof": 1,
            "series_id": 77,
            "science_calibration_version_id": 967,
        },
        {
            "interseries_generation_id": 2,
            "generation_uid": "m4_v",
            "field_id": 33,
            "instrument_unit_id": 7,
            "filter_name": "V",
            "catalog_name": "shared",
            "reference_series_id": 76,
            "reliability_status": "SPARSE_GRAPH_NO_CLOSURE",
            "graph_dof": 0,
            "series_id": 76,
            "science_calibration_version_id": 960,
        },
    ]

    class FakeSession:
        def execute(self, statement, params):
            assert params["generation_ids"] == [1, 2]
            return _MappingsResult(rows)

    resolved = m5.resolve_m5_generations(FakeSession(), field_id=33, generation_ids=[2, 1])
    assert [row.interseries_generation_id for row in resolved] == [1, 2]


def test_m5_generation_selection_refuses_orphan_before_output() -> None:
    rows = [
        {"interseries_generation_id": 9, "generation_uid": "m4_9", "field_id": 33,
         "instrument_unit_id": 7, "filter_name": "V", "catalog_name": "shared",
         "reference_series_id": 76, "n_series": 2, "reliability_status": "REDUNDANT_GRAPH_OK",
         "graph_dof": 0, "membership_field_id": 33, "series_id": 76,
         "science_calibration_version_id": 960},
        {"interseries_generation_id": 9, "generation_uid": "m4_9", "field_id": 33,
         "instrument_unit_id": 7, "filter_name": "V", "catalog_name": "shared",
         "reference_series_id": 76, "n_series": 2, "reliability_status": "REDUNDANT_GRAPH_OK",
         "graph_dof": 0, "membership_field_id": 33, "series_id": None,
         "science_calibration_version_id": 961},
    ]

    class FakeSession:
        def execute(self, _statement, _params):
            return _MappingsResult(rows)

    with pytest.raises(ValueError, match="orphaned or incomplete membership"):
        m5.resolve_m5_generations(FakeSession(), field_id=33, generation_ids=[9])


def test_m5_exact_reader_rechecks_orphan_before_loading_science() -> None:
    generation = m5.M5Generation(
        interseries_generation_id=9, generation_uid="m4_9", field_id=33,
        instrument_unit_id=7, filter_name="V", catalog_name="shared",
        reference_series_id=76, reliability_status="REDUNDANT_GRAPH_OK", graph_dof=0,
        series_sources=((76, 960),),
    )

    class FakeSession:
        def execute(self, _statement, _params):
            return _MappingsResult([{
                "interseries_generation_id": 9, "series_id": None,
                "science_calibration_version_id": 961, "membership_field_id": 33,
                "generation_field_id": 33,
            }])

    with pytest.raises(RuntimeError, match="orphaned"):
        list(m5._iter_exact_m4_rows_from_science(
            FakeSession(), object_id=1, field_id=33, generations=[generation],
        ))


def test_plot_plan_switches_to_binning_without_limiting_main_csv() -> None:
    stats = [
        {"filter_name": "V", "series_id": 76, "n_points": 7000, "mjd_min": 60000.0, "mjd_max": 60001.0},
        {"filter_name": "V", "series_id": 78, "n_points": 3000, "mjd_min": 60002.0, "mjd_max": 60003.0},
        {"filter_name": "R", "series_id": 77, "n_points": 100, "mjd_min": 60000.0, "mjd_max": 60001.0},
    ]
    plan = m5._build_plot_plan(stats, reduction="auto", max_points=5000, bin_minutes=None)
    assert plan["V"]["mode"] == "bin"
    assert plan["R"]["mode"] == "raw"
    allocations = m5._allocate_plot_points(stats[:2], max_points=5000)
    assert sum(allocations.values()) == 5000
    assert allocations[76] > allocations[78]


def test_streaming_bin_accumulator_keeps_mean_envelope_and_random_sem() -> None:
    plan = {
        "mode": "bin",
        "series": {76: {"mjd_min": 60000.0, "bin_width_days": 1.0}},
    }
    bins = {}
    base = {
        "filter_name": "V",
        "series_id": 76,
        "interseries_generation_id": 2,
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "mag_err_series_zp": 0.004,
    }
    for mjd, mag in ((60000.1, 12.0), (60000.2, 12.2), (60000.3, 11.8)):
        row = dict(base, mjd=mjd, mag_global=mag, mag_err_random=0.03)
        m5._accumulate_plot_bin(bins, row, plan)
    assert len(bins) == 1
    reduced = next(iter(bins.values())).finalize()
    assert reduced["n_points"] == 3
    assert reduced["mag_global"] == pytest.approx(12.0)
    assert reduced["mag_min"] == pytest.approx(11.8)
    assert reduced["mag_max"] == pytest.approx(12.2)
    assert reduced["mag_err_series_zp"] == pytest.approx(0.004)
    assert reduced["mag_err_random"] > 0


def test_m5_series_coverage_detects_stale_and_unlinked_current_science(monkeypatch) -> None:
    current = [
        {"series_id": 76, "filter_name": "V", "calibration_version_id": 960},
        {"series_id": 78, "filter_name": "V", "calibration_version_id": 999},
        {"series_id": 79, "filter_name": "V", "calibration_version_id": 981},
    ]
    monkeypatch.setattr(m5, "_load_series_candidates", lambda *args, **kwargs: current)

    generation = m5.M5Generation(
        interseries_generation_id=2,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="SPARSE_GRAPH_NO_CLOSURE",
        graph_dof=0,
        series_sources=((76, 960), (78, 991)),
    )

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM science_photometry_cache" in sql:
                return _MappingsResult([])
            if "AS n_points" in sql:
                return _MappingsResult([
                    {"calibration_version_id": 960, "n_points": 10},
                    {"calibration_version_id": 999, "n_points": 12},
                    {"calibration_version_id": 981, "n_points": 8},
                ])
            if "FROM science_measurements" in sql:
                return _MappingsResult([
                    {"calibration_version_id": 960, "n": 10},
                    {"calibration_version_id": 999, "n": 12},
                    {"calibration_version_id": 981, "n": 8},
                ])
            return _MappingsResult([])

    coverage = m5.inspect_m5_series_coverage(
        FakeSession(), field_id=33, object_id=42, generations=[generation]
    )
    statuses = {row.series_id: row.status for row in coverage}
    assert statuses[76] == "READY_EXACT_M4_SOURCE"
    assert statuses[78] == "M4_SOURCE_NOT_CURRENT_SCIENCE"
    assert statuses[79] == "NO_COMPATIBLE_INTERSERIES_GENERATION"


def test_global_plot_export_uses_existing_matplotlib_infrastructure(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = [
        {
            "mjd": 60000.0,
            "mag_global": 12.0,
            "mag_err_random": 0.02,
            "mag_err_series_zp": 0.003,
            "series_id": 76,
            "mag_min": 11.98,
            "mag_max": 12.02,
        },
        {
            "mjd": 60000.1,
            "mag_global": 12.1,
            "mag_err_random": 0.02,
            "mag_err_series_zp": 0.003,
            "series_id": 76,
            "mag_min": 12.05,
            "mag_max": 12.14,
        },
    ]
    out = tmp_path / "global.png"
    result = export_global_lightcurve_plot(rows, out, reduction_mode="bin")
    assert out.exists()
    assert out.stat().st_size > 0
    assert result.n_points == 2
    assert result.inverted_y is True


def test_m5_cli_contract() -> None:
    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    args = build_parser().parse_args([
        "export-global-object-light-curve",
        "--object-id", "42",
        "--field-id", "33",
        "--generation-id", "1",
        "--generation-id", "2",
        "--out-root", "reports/object42",
        "--plot-max-points", "3000",
        "--plot-reduction", "auto",
        "--plot-time-axis", "compressed",
        "--include-rejected-debug",
    ])
    assert args.object_id == 42
    assert args.field_id == 33
    assert args.generation_ids == [1, 2]
    assert args.plot_max_points == 3000
    assert args.plot_reduction == "auto"
    assert args.plot_time_axis == "compressed"
    assert args.include_rejected_debug is True


def test_plot_budget_is_strict_even_for_series_endpoints() -> None:
    stats = [
        {"filter_name": "V", "series_id": 76, "n_points": 25, "mjd_min": 60000.0, "mjd_max": 60000.2},
        {"filter_name": "V", "series_id": 78, "n_points": 29, "mjd_min": 60003.0, "mjd_max": 60003.3},
        {"filter_name": "V", "series_id": 79, "n_points": 10, "mjd_min": 60003.5, "mjd_max": 60003.6},
    ]
    plan = m5._build_plot_plan(stats, reduction="bin", max_points=20, bin_minutes=None)
    assert sum(int(row["max_bins"]) for row in plan["V"]["series"].values()) == 20

    bins = {}
    for stat in stats:
        n = int(stat["n_points"])
        start = float(stat["mjd_min"])
        end = float(stat["mjd_max"])
        for i in range(n):
            frac = i / (n - 1) if n > 1 else 0.0
            row = {
                "filter_name": "V",
                "series_id": int(stat["series_id"]),
                "interseries_generation_id": 2,
                "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
                "mag_err_series_zp": 0.003,
                "mag_err_random": 0.02,
                "mjd": start + frac * (end - start),
                "mag_global": 12.0 + 0.01 * frac,
            }
            m5._accumulate_plot_bin(bins, row, plan["V"])
    assert len(bins) <= 20
    per_series = {}
    for _filter, series_id, _bin_index in bins:
        per_series[series_id] = per_series.get(series_id, 0) + 1
    for series_id, series_plan in plan["V"]["series"].items():
        assert per_series.get(series_id, 0) <= int(series_plan["max_bins"])


def test_plot_budget_refuses_to_hide_entire_series() -> None:
    stats = [
        {"filter_name": "V", "series_id": 1, "n_points": 10, "mjd_min": 1.0, "mjd_max": 1.1},
        {"filter_name": "V", "series_id": 2, "n_points": 10, "mjd_min": 2.0, "mjd_max": 2.1},
        {"filter_name": "V", "series_id": 3, "n_points": 10, "mjd_min": 3.0, "mjd_max": 3.1},
    ]
    with pytest.raises(ValueError, match="smaller than the number of non-empty series"):
        m5._build_plot_plan(stats, reduction="bin", max_points=2, bin_minutes=None)


def test_compressed_time_axis_preserves_intraseries_time_and_shortens_large_gaps() -> None:
    from grandgousier.visualization.lightcurve_plot import _build_global_time_axis

    rows = [
        {"series_id": 76, "mjd": 60000.0, "mag_global": 12.0},
        {"series_id": 76, "mjd": 60000.1, "mag_global": 12.1},
        {"series_id": 78, "mjd": 60003.0, "mag_global": 12.0},
        {"series_id": 78, "mjd": 60003.2, "mag_global": 12.1},
        {"series_id": 79, "mjd": 60003.5, "mag_global": 12.0},
        {"series_id": 79, "mjd": 60003.6, "mag_global": 12.1},
    ]
    axis = _build_global_time_axis(rows, requested_mode="auto")
    assert axis["mode"] == "compressed"
    assert len(axis["break_positions"]) == 2
    xs = list(axis["x_values"])
    assert xs[1] - xs[0] == pytest.approx(0.1)
    assert xs[3] - xs[2] == pytest.approx(0.2)
    assert xs[-1] - xs[0] < 1.0
    assert len(axis["tick_positions"]) == 3


def test_global_plot_reports_compressed_time_axis(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = [
        {"mjd": 60000.0, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": 76},
        {"mjd": 60000.1, "mag_global": 12.1, "mag_err_random": 0.02, "series_id": 76},
        {"mjd": 60003.0, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": 78},
        {"mjd": 60003.1, "mag_global": 12.1, "mag_err_random": 0.02, "series_id": 78},
    ]
    out = tmp_path / "compressed.png"
    result = export_global_lightcurve_plot(rows, out, time_axis_mode="auto")
    assert out.exists()
    assert result.time_axis_mode == "compressed"
    assert result.n_time_breaks == 1
    assert result.x_key == "mjd_compressed"


def test_global_plot_switches_to_archive_overview_for_many_series(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    from grandgousier.visualization.lightcurve_plot import export_global_lightcurve_plot

    rows = []
    for series_id in range(1, 14):
        base = 60000.0 + series_id
        rows.extend([
            {"mjd": base, "mag_global": 12.0, "mag_err_random": 0.02, "series_id": series_id},
            {"mjd": base + 0.05, "mag_global": 12.01, "mag_err_random": 0.02, "series_id": series_id},
        ])
    out = tmp_path / "overview.png"
    result = export_global_lightcurve_plot(rows, out, time_axis_mode="auto")
    assert out.exists()
    assert result.plot_style == "archive_overview"
    assert result.time_axis_mode == "compressed"
    assert result.n_time_breaks >= 1

def test_export_m5_writes_exact_csv_and_summary_without_downsampling(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_r",
        field_id=33,
        instrument_unit_id=8,
        filter_name="R",
        catalog_name="shared",
        reference_series_id=77,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((77, 967),),
    )
    coverage = [
        m5.SeriesCoverage(77, "R", 967, 967, 2, "READY_EXACT_M4_SOURCE")
    ]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "R", "series_id": 77, "n_points": 2, "mjd_min": 60000.0, "mjd_max": 60000.1}
        ],
    )

    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 42,
        "frame_id": 100,
        "series_id": 77,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_r",
        "calibration_version_id": 967,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 8,
        "instrument": "MASTER",
        "site": "X",
        "tube": "WEST",
        "camera": "203",
        "filter_name": "R",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.3,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.004,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 1,
        "interseries_generation_uid": "m4_r",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 1,
        "interseries_reference_series_id": 77,
    }
    rejected = dict(base)
    rejected.update(
        science_measurement_id=2,
        measurement_id=2,
        frame_id=101,
        mjd=60000.1,
        measurement_flags={"catalog_name": "shared", "source_id": "42", "SATURATED_OR_NEAR_SATURATED": True},
    )
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([base, rejected]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 42}])
            raise AssertionError(str(statement))

    def write_plot(rows, path, **kwargs):
        Path(path).write_bytes(b"plot bytes")
        return SimpleNamespace(
            n_points=len(rows),
            time_axis_mode="continuous",
            n_time_breaks=0,
            plot_style="series_detailed",
        )

    published_directories = []
    real_publish = m5._rename_directory_noreplace

    def record_publish(source, destination):
        published_directories.append((Path(source), Path(destination)))
        assert Path(source).parent == Path(destination).parent
        assert Path(source).is_dir()
        assert not Path(destination).exists()
        real_publish(source, destination)

    monkeypatch.setattr(m5, "export_global_lightcurve_plot", write_plot)
    monkeypatch.setattr(m5, "_rename_directory_noreplace", record_publish)
    monkeypatch.setattr(m5.os, "link", lambda *args, **kwargs: pytest.fail("file links are not publication"))
    root = tmp_path / "object42"
    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=42,
        field_id=33,
        out_root=root,
        include_rejected=False,
        plots=True,
    )
    assert result.status == m5.M5_STATUS_READY
    assert result.rows_source_exact_m4 == 2
    assert result.accepted_rows == 1
    assert result.rejected_rows == 1
    assert result.rows_written == 1
    assert result.plot_policy["main_csv_downsampled"] is False
    assert result.csv_path == str(root / "object42.csv")
    assert result.summary_path == str(root / "object42_summary.json")
    assert result.plot_data_paths == {"R": str(root / "object42_R_plot.csv")}
    assert result.plot_paths == {"R": str(root / "object42_R.png")}
    assert len(published_directories) == 1
    stage_path, published_path = published_directories[0]
    assert stage_path.parent == tmp_path
    assert published_path == root
    assert not stage_path.exists()
    csv_lines = Path(result.csv_path).read_text(encoding="utf-8").splitlines()
    assert len(csv_lines) == 2
    assert Path(result.plot_data_paths["R"]).exists()
    assert Path(result.plot_paths["R"]).read_bytes() == b"plot bytes"
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["csv_path"] == str(root / "object42.csv")
    assert summary["summary_path"] == str(root / "object42_summary.json")
    assert summary["plot_data_paths"] == {"R": str(root / "object42_R_plot.csv")}
    assert summary["plot_paths"] == {"R": str(root / "object42_R.png")}
    assert all(
        Path(path).parent == root
        for path in [
            result.csv_path,
            result.summary_path,
            *result.plot_data_paths.values(),
            *result.plot_paths.values(),
        ]
    )
    assert ".m5-staging-" not in json.dumps(summary)
    assert not Path(f"{root}.csv").exists()
    assert "REDUNDANT_GRAPH_OK" in json.dumps(summary)
    assert "correlated" in json.dumps(summary)


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_m5_existing_bundle_refuses_before_row_iteration(
    monkeypatch, tmp_path: Path, populated: bool
) -> None:
    iterator_started = False

    def rows():
        nonlocal iterator_started
        iterator_started = True
        yield _minimal_m5_export_row()

    session = _configure_minimal_m5_export(monkeypatch, rows)
    root = tmp_path / "object42"
    root.mkdir()
    existing_path = root / "keep.bin"
    original_bytes = b"pre-existing bundle bytes\x00\xff"
    if populated:
        existing_path.write_bytes(original_bytes)

    with pytest.raises(FileExistsError, match="M5 output bundle already exists"):
        m5.export_global_object_light_curve(
            session,
            object_id=42,
            field_id=33,
            out_root=root,
            include_rejected_debug=True,
            plots=True,
        )

    assert root.is_dir()
    if populated:
        assert existing_path.read_bytes() == original_bytes
    else:
        assert list(root.iterdir()) == []
    assert iterator_started is False
    assert list(tmp_path.glob(f".{root.name}.m5-staging-*")) == []


@pytest.mark.parametrize("member_suffix", [
    ".csv",
    "_summary.json",
    "_rejections.csv",
    "_R_plot.csv",
    "_R.png",
])
def test_m5_legacy_flat_output_refuses_without_modification(
    monkeypatch, tmp_path: Path, member_suffix: str
) -> None:
    iterator_started = False

    def rows():
        nonlocal iterator_started
        iterator_started = True
        yield _minimal_m5_export_row()

    session = _configure_minimal_m5_export(monkeypatch, rows)
    root = tmp_path / "object42"
    legacy_path = Path(f"{root}{member_suffix}")
    original_bytes = b"legacy flat bytes\x00\xff"
    legacy_path.write_bytes(original_bytes)

    with pytest.raises(FileExistsError, match="M5 legacy flat output already exists"):
        m5.export_global_object_light_curve(
            session,
            object_id=42,
            field_id=33,
            out_root=root,
            include_rejected_debug=True,
            plots=True,
        )

    assert legacy_path.read_bytes() == original_bytes
    assert iterator_started is False
    assert not root.exists()
    assert list(tmp_path.glob(f".{root.name}.m5-staging-*")) == []


@pytest.mark.parametrize("failure_stage", ["iterator", "plot", "write"])
def test_m5_export_failure_leaves_no_final_files_or_staging(
    monkeypatch, tmp_path: Path, failure_stage: str
) -> None:
    def failing_rows():
        yield _minimal_m5_export_row()
        if failure_stage == "iterator":
            raise RuntimeError("injected iterator failure")

    session = _configure_minimal_m5_export(monkeypatch, failing_rows)
    root = tmp_path / "object42"
    if failure_stage == "plot":
        def failing_plot(rows, path, **kwargs):
            Path(path).write_bytes(b"partial plot")
            raise RuntimeError("injected plot failure")

        monkeypatch.setattr(m5, "export_global_lightcurve_plot", failing_plot)
    elif failure_stage == "write":
        def failing_plot_data(rows, path):
            Path(path).write_bytes(b"partial plot CSV")
            raise RuntimeError("injected write failure")

        monkeypatch.setattr(m5, "_write_plot_data", failing_plot_data)

    with pytest.raises(RuntimeError, match=f"injected {failure_stage} failure"):
        m5.export_global_object_light_curve(
            session,
            object_id=42,
            field_id=33,
            out_root=root,
            include_rejected_debug=True,
            plots=failure_stage != "iterator",
        )

    assert not root.exists()
    expected_legacy_paths = [
        Path(f"{root}.csv"),
        Path(f"{root}_summary.json"),
        Path(f"{root}_rejections.csv"),
        Path(f"{root}_R_plot.csv"),
        Path(f"{root}_R.png"),
    ]
    assert all(not path.exists() for path in expected_legacy_paths)
    assert list(tmp_path.glob(f".{root.name}.m5-staging-*")) == []


def test_m5_stale_staging_sibling_is_never_reused_or_removed(monkeypatch, tmp_path: Path) -> None:
    session = _configure_minimal_m5_export(
        monkeypatch, lambda: iter([_minimal_m5_export_row()])
    )
    root = tmp_path / "object42"
    stale_stage = tmp_path / ".object42.m5-staging-stale"
    stale_stage.mkdir()
    marker = stale_stage / "keep.bin"
    marker.write_bytes(b"stale run")

    result = m5.export_global_object_light_curve(
        session,
        object_id=42,
        field_id=33,
        out_root=root,
        plots=False,
    )

    assert Path(result.csv_path).parent == root
    assert marker.read_bytes() == b"stale run"
    assert stale_stage.is_dir()
    assert list(tmp_path.glob(f".{root.name}.m5-staging-*")) == [stale_stage]


def test_m5_preserve_staging_opt_in_publishes_without_rmtree(monkeypatch, tmp_path: Path) -> None:
    session = _configure_minimal_m5_export(
        monkeypatch, lambda: iter([_minimal_m5_export_row()])
    )
    rmtree_calls = _forbid_m5_rmtree(monkeypatch)
    root = tmp_path / "object42"

    result = m5.export_global_object_light_curve(
        session, object_id=42, field_id=33, out_root=root,
        plots=False, preserve_staging_on_failure=True,
    )

    assert result.status == m5.M5_STATUS_READY
    assert result.rows_written == result.accepted_rows == 1
    assert result.csv_path == str(root / "object42.csv")
    assert result.summary_path == str(root / "object42_summary.json")
    csv_lines = (root / "object42.csv").read_text(encoding="utf-8").splitlines()
    assert len(csv_lines) == 2
    assert "R" in csv_lines[1]
    summary = json.loads((root / "object42_summary.json").read_text(encoding="utf-8"))
    assert summary["csv_path"] == result.csv_path
    assert summary["summary_path"] == result.summary_path
    assert summary["status"] == m5.M5_STATUS_READY
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []
    assert rmtree_calls == []


@pytest.mark.parametrize("failure_stage", ["iterator", "write"])
def test_m5_preserve_staging_opt_in_retains_partial_stage_on_failure(
    monkeypatch, tmp_path: Path, failure_stage: str
) -> None:
    def rows():
        yield _minimal_m5_export_row()
        if failure_stage == "iterator":
            raise RuntimeError("injected iterator failure")

    session = _configure_minimal_m5_export(monkeypatch, rows)
    if failure_stage == "write":
        real_write = m5.csv.DictWriter.writerow

        def write_then_fail(writer, row):
            result = real_write(writer, row)
            if row.get("filter_name") == "R":
                raise RuntimeError("injected write failure")
            return result

        monkeypatch.setattr(m5.csv.DictWriter, "writerow", write_then_fail)
    rmtree_calls = _forbid_m5_rmtree(monkeypatch)
    root = tmp_path / "object42"

    with pytest.raises(RuntimeError, match=f"injected {failure_stage} failure"):
        m5.export_global_object_light_curve(
            session, object_id=42, field_id=33, out_root=root,
            plots=False, preserve_staging_on_failure=True,
        )

    assert not root.exists()
    stages = list(tmp_path.glob(".object42.m5-staging-*"))
    assert len(stages) == 1
    assert stages[0].is_dir()
    assert len((stages[0] / "object42.csv").read_text(encoding="utf-8").splitlines()) == 2
    assert not (stages[0] / "object42_summary.json").exists()
    assert rmtree_calls == []


def test_m5_preserve_staging_opt_in_existing_root_is_untouched(
    monkeypatch, tmp_path: Path
) -> None:
    session = _configure_minimal_m5_export(
        monkeypatch, lambda: iter([_minimal_m5_export_row()])
    )
    rmtree_calls = _forbid_m5_rmtree(monkeypatch)
    root = tmp_path / "object42"
    root.mkdir()
    marker = root / "keep.bin"
    marker.write_bytes(b"existing bundle")

    with pytest.raises(FileExistsError, match="M5 output bundle already exists"):
        m5.export_global_object_light_curve(
            session, object_id=42, field_id=33, out_root=root,
            plots=False, preserve_staging_on_failure=True,
        )

    assert marker.read_bytes() == b"existing bundle"
    assert list(root.iterdir()) == [marker]
    assert list(tmp_path.glob(".object42.m5-staging-*")) == []
    assert rmtree_calls == []


def test_m5_preserve_staging_opt_in_race_keeps_destination_and_stage(
    monkeypatch, tmp_path: Path
) -> None:
    session = _configure_minimal_m5_export(
        monkeypatch, lambda: iter([_minimal_m5_export_row()])
    )
    rmtree_calls = _forbid_m5_rmtree(monkeypatch)
    root = tmp_path / "object42"
    original_publish = m5._rename_directory_noreplace
    staged = []

    def collide_at_publication(source, destination):
        assert Path(destination) == root
        staged.append(Path(source))
        root.mkdir()
        (root / "keep.bin").write_bytes(b"concurrent destination")
        original_publish(source, destination)

    monkeypatch.setattr(m5, "_rename_directory_noreplace", collide_at_publication)

    with pytest.raises(FileExistsError):
        m5.export_global_object_light_curve(
            session, object_id=42, field_id=33, out_root=root,
            plots=False, preserve_staging_on_failure=True,
        )

    assert (root / "keep.bin").read_bytes() == b"concurrent destination"
    assert list(root.iterdir()) == [root / "keep.bin"]
    assert len(staged) == 1
    assert staged[0].is_dir()
    assert len((staged[0] / "object42.csv").read_text(encoding="utf-8").splitlines()) == 2
    assert (staged[0] / "object42_summary.json").is_file()
    assert list(tmp_path.glob(".object42.m5-staging-*")) == staged
    assert rmtree_calls == []


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="native no-replace directory rename is unsupported on this platform",
)
def test_native_no_replace_rename_preserves_existing_bundle(
    tmp_path: Path, populated: bool
) -> None:
    source = tmp_path / "stage"
    destination = tmp_path / "bundle"
    source.mkdir()
    destination.mkdir()
    if populated:
        (destination / "keep.bin").write_bytes(b"destination bytes")

    with pytest.raises(FileExistsError) as error:
        m5._rename_directory_noreplace(source, destination)

    assert error.value.errno == errno.EEXIST
    assert source.is_dir()
    assert destination.is_dir()
    if populated:
        assert (destination / "keep.bin").read_bytes() == b"destination bytes"
    else:
        assert list(destination.iterdir()) == []


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="native no-replace directory rename is unsupported on this platform",
)
def test_m5_concurrent_bundle_collision_preserves_destination_and_cleans_own_stage(
    monkeypatch, tmp_path: Path, populated: bool
) -> None:
    session = _configure_minimal_m5_export(
        monkeypatch, lambda: iter([_minimal_m5_export_row()])
    )
    root = tmp_path / "object42"
    original_publish = m5._rename_directory_noreplace

    def race_with_another_publisher(source, destination):
        Path(destination).mkdir()
        if populated:
            (Path(destination) / "keep.bin").write_bytes(b"concurrent destination")
        original_publish(source, destination)

    monkeypatch.setattr(m5, "_rename_directory_noreplace", race_with_another_publisher)

    with pytest.raises(FileExistsError):
        m5.export_global_object_light_curve(
            session,
            object_id=42,
            field_id=33,
            out_root=root,
            plots=False,
        )

    assert root.is_dir()
    if populated:
        assert (root / "keep.bin").read_bytes() == b"concurrent destination"
    else:
        assert list(root.iterdir()) == []
    assert list(tmp_path.glob(f".{root.name}.m5-staging-*")) == []


@pytest.mark.parametrize(
    ("platform", "symbol", "expected_argtypes", "expected_flags"),
    [
        ("linux", "renameat2", [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint], 1),
        ("darwin", "renamex_np", [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint], 4),
    ],
)
def test_m5_native_libc_wrapper_uses_expected_no_replace_abi(
    monkeypatch,
    tmp_path: Path,
    platform: str,
    symbol: str,
    expected_argtypes: list,
    expected_flags: int,
) -> None:
    calls = []

    class FakeFunction:
        def __init__(self):
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            calls.append(args)
            return 0

    native_function = FakeFunction()
    libc = SimpleNamespace(**{symbol: native_function})
    cdll_calls = []
    monkeypatch.setattr(
        m5.ctypes,
        "CDLL",
        lambda library, use_errno: cdll_calls.append((library, use_errno)) or libc,
    )
    monkeypatch.setattr(m5.sys, "platform", platform)
    source = tmp_path / "stage"
    destination = tmp_path / "bundle"
    source.mkdir()

    m5._rename_directory_noreplace(source, destination)

    assert cdll_calls == [(None, True)]
    assert native_function.argtypes == expected_argtypes
    assert native_function.restype is ctypes.c_int
    assert len(calls) == 1
    if platform == "linux":
        assert calls[0] == (-100, str(source).encode(), -100, str(destination).encode(), expected_flags)
    else:
        assert calls[0] == (str(source).encode(), str(destination).encode(), expected_flags)


@pytest.mark.parametrize("platform,symbol", [("linux", "renameat2"), ("darwin", "renamex_np")])
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_m5_native_libc_collision_preserves_existing_bundle(
    monkeypatch,
    tmp_path: Path,
    platform: str,
    symbol: str,
    populated: bool,
) -> None:
    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, *args):
            ctypes.set_errno(errno.EEXIST)
            return -1

    libc = SimpleNamespace(**{symbol: FakeFunction()})
    monkeypatch.setattr(m5.ctypes, "CDLL", lambda *args, **kwargs: libc)
    monkeypatch.setattr(m5.sys, "platform", platform)
    monkeypatch.setattr(m5.os, "rename", lambda *args, **kwargs: pytest.fail("must not fall back"))
    source = tmp_path / "stage"
    destination = tmp_path / "bundle"
    source.mkdir()
    destination.mkdir()
    if populated:
        (destination / "keep.bin").write_bytes(b"existing")

    with pytest.raises(FileExistsError) as error:
        m5._rename_directory_noreplace(source, destination)

    assert error.value.errno == errno.EEXIST
    assert source.is_dir()
    assert destination.is_dir()
    if populated:
        assert (destination / "keep.bin").read_bytes() == b"existing"
    else:
        assert list(destination.iterdir()) == []


@pytest.mark.parametrize("platform,symbol", [("linux", "renameat2"), ("darwin", "renamex_np")])
def test_m5_native_libc_wrapper_missing_symbol_fails_closed(
    monkeypatch, tmp_path: Path, platform: str, symbol: str
) -> None:
    monkeypatch.setattr(m5.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(m5.sys, "platform", platform)
    monkeypatch.setattr(m5.os, "rename", lambda *args, **kwargs: pytest.fail("must not fall back"))

    with pytest.raises(NotImplementedError, match=symbol):
        m5._rename_directory_noreplace(tmp_path / "stage", tmp_path / "bundle")


def test_m5_native_libc_unsupported_filesystem_error_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, *args):
            ctypes.set_errno(errno.EOPNOTSUPP)
            return -1

    monkeypatch.setattr(
        m5.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(renameat2=FakeFunction()),
    )
    monkeypatch.setattr(m5.sys, "platform", "linux")
    monkeypatch.setattr(m5.os, "rename", lambda *args, **kwargs: pytest.fail("must not fall back"))

    with pytest.raises(OSError) as error:
        m5._rename_directory_noreplace(tmp_path / "stage", tmp_path / "bundle")

    assert error.value.errno == errno.EOPNOTSUPP


def test_m5_native_libc_unexpected_return_without_errno_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, *args):
            return 7

    monkeypatch.setattr(
        m5.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(renameat2=FakeFunction()),
    )
    monkeypatch.setattr(m5.sys, "platform", "linux")

    with pytest.raises(OSError, match="returned 7 without setting errno"):
        m5._rename_directory_noreplace(tmp_path / "stage", tmp_path / "bundle")


def test_m5_no_replace_rename_unsupported_platform_fails_closed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m5.sys, "platform", "freebsd")
    monkeypatch.setattr(m5.os, "rename", lambda *args, **kwargs: pytest.fail("must not fall back"))

    with pytest.raises(NotImplementedError, match="freebsd"):
        m5._rename_directory_noreplace(tmp_path / "stage", tmp_path / "bundle")



def test_m5_saturation_policy_rejects_epoch_flag_not_object_diagnostic() -> None:
    reject_flags = m5._m5_reject_flags(
        require_detection_support=True,
        extra_reject_flags=None,
    )
    assert "SATURATED_LIKE" not in reject_flags
    assert "SATURATED_OR_NEAR_SATURATED" in reject_flags

    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 29786,
        "frame_id": 100,
        "series_id": 82,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 998,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 58372.7,
        "jd": 2458373.2,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "KAO",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 10.0,
        "mag_cal": 10.48,
        "mag_err": 0.01,
        "mag_err_random": 0.01,
        "mag_err_systematic": 0.0,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "29786"},
        "frame_quality_flags": {},
        "object_qc_flags": {"SATURATED_LIKE": True},
        "interseries_offset_mag": -0.0034,
        "interseries_offset_err": 0.0004,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 6,
        "interseries_generation_uid": "m4_v_scale",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 2,
        "interseries_reference_series_id": 76,
    }
    accepted = m5._to_m5_row(base, reject_flags=reject_flags, field_id=33)
    assert accepted["is_rejected"] is False
    assert "SATURATED_LIKE" in accepted["quality_flags"]

    saturated_epoch = dict(base)
    saturated_epoch["science_measurement_id"] = 2
    saturated_epoch["measurement_id"] = 2
    saturated_epoch["frame_id"] = 101
    saturated_epoch["measurement_flags"] = {
        "catalog_name": "shared",
        "source_id": "29786",
        "SATURATED_OR_NEAR_SATURATED": True,
    }
    rejected = m5._to_m5_row(saturated_epoch, reject_flags=reject_flags, field_id=33)
    assert rejected["is_rejected"] is True
    assert rejected["reject_reasons"] == "SATURATED_OR_NEAR_SATURATED"


def test_m5_explicit_extra_flag_can_restore_saturated_like_veto() -> None:
    reject_flags = m5._m5_reject_flags(
        require_detection_support=True,
        extra_reject_flags="SATURATED_LIKE",
    )
    assert "SATURATED_LIKE" in reject_flags
    assert "SATURATED_OR_NEAR_SATURATED" in reject_flags

def test_export_m5_all_rejected_reports_reasons_and_debug(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=2,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="SPARSE_GRAPH_NO_CLOSURE",
        graph_dof=0,
        series_sources=((76, 960),),
    )
    coverage = [m5.SeriesCoverage(76, "V", 960, 960, 2, "READY_EXACT_M4_SOURCE")]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "V", "series_id": 76, "n_points": 2, "mjd_min": 60000.0, "mjd_max": 60000.1}
        ],
    )
    base = {
        "science_measurement_id": 1,
        "measurement_id": 1,
        "object_id": 29700,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 7.5,
        "mag_cal": 7.7,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {
            "HIGH_RMS": True,
            "SATURATED_LIKE": True,
            "NO_DETECTION_SUPPORT": True,
        },
        "interseries_offset_mag": 0.004,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 2,
        "interseries_generation_uid": "m4_v",
        "interseries_reliability": "SPARSE_GRAPH_NO_CLOSURE",
        "interseries_graph_dof": 0,
        "interseries_reference_series_id": 76,
    }
    second = dict(base, science_measurement_id=2, measurement_id=2, frame_id=101, mjd=60000.1)
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([base, second]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 29700}])
            raise AssertionError(str(statement))

    root = tmp_path / "object29700"
    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=29700,
        field_id=33,
        out_root=root,
        include_rejected_debug=True,
        plots=True,
    )
    assert result.status == m5.M5_STATUS_NO_ACCEPTED
    assert result.rows_source_exact_m4 == 2
    assert result.rows_written == 0
    assert result.accepted_rows == 0
    assert result.rejected_rows == 2
    assert result.rejection_reasons == {
        "HIGH_RMS": 2,
        "NO_DETECTION_SUPPORT": 2,
    }
    assert "SATURATED_LIKE" not in result.rejection_reasons_by_series["76"]
    assert result.plot_paths == {}
    debug_path = Path(result.rejection_debug_path)
    assert debug_path.parent == root
    assert debug_path.exists()
    lines = debug_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "SATURATED_LIKE" in lines[1]  # preserved as diagnostic evidence in quality_flags
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["csv_path"] == str(root / "object29700.csv")
    assert summary["summary_path"] == str(root / "object29700_summary.json")
    assert summary["rejection_debug_path"] == result.rejection_debug_path
    assert ".m5-staging-" not in json.dumps(summary)
    assert "NO_ACCEPTED_POINTS_AFTER_QC" in json.dumps(summary)
    assert "SATURATED_LIKE" not in result.rejection_reasons


def test_m5_rejection_debug_columns_include_qc_evidence() -> None:
    columns = m5.rejection_debug_columns()
    assert "quality_flags" in columns
    assert "reject_reasons" in columns
    assert "science_calibration_version_id" in columns
    assert "interseries_generation_id" in columns


def test_export_m5_marks_durable_science_read_source(monkeypatch, tmp_path: Path) -> None:
    generation = m5.M5Generation(
        interseries_generation_id=1,
        generation_uid="m4_v",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=76,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=1,
        series_sources=((76, 960),),
    )
    coverage = [m5.SeriesCoverage(76, "V", 960, 960, 1, "READY_EXACT_M4_SOURCE")]
    monkeypatch.setattr(m5, "resolve_m5_generations", lambda *args, **kwargs: [generation])
    monkeypatch.setattr(m5, "inspect_m5_series_coverage", lambda *args, **kwargs: coverage)
    monkeypatch.setattr(
        m5,
        "_load_plot_source_stats",
        lambda *args, **kwargs: [
            {"filter_name": "V", "series_id": 76, "n_points": 1, "mjd_min": 60000.0, "mjd_max": 60000.0}
        ],
    )
    raw = {
        "science_measurement_id": 123,
        "measurement_id": 123,
        "object_id": 42,
        "frame_id": 100,
        "series_id": 76,
        "field_name": "GPX_TF17_2",
        "calibration_name": "science_v",
        "calibration_version_id": 960,
        "model_name": "ensemble",
        "date_obs": None,
        "mjd": 60000.0,
        "jd": 2460000.5,
        "instrument_unit_id": 7,
        "instrument": "MASTER",
        "site": "X",
        "tube": "EAST",
        "camera": "204",
        "filter_name": "V",
        "x": 10.0,
        "y": 20.0,
        "flux": 1000.0,
        "flux_err": 10.0,
        "mag_inst": 12.0,
        "mag_cal": 12.3,
        "mag_err": 0.02,
        "mag_err_random": 0.02,
        "mag_err_systematic": 0.01,
        "aperture_radius_pix": 4.0,
        "local_background": 3.0,
        "local_zp_correction": 0.0,
        "centroid_dx_pix": 0.0,
        "centroid_dy_pix": 0.0,
        "measurement_flags": {"catalog_name": "shared", "source_id": "42"},
        "frame_quality_flags": {},
        "object_qc_flags": {},
        "interseries_offset_mag": 0.0,
        "interseries_offset_err": 0.002,
        "interseries_offset_status": "M4_EXACT_SOURCE_MATCH",
        "interseries_algorithm": "interseries_graph_median_v1",
        "interseries_generation_id": 1,
        "interseries_generation_uid": "m4_v",
        "interseries_reliability": "REDUNDANT_GRAPH_OK",
        "interseries_graph_dof": 1,
        "interseries_reference_series_id": 76,
    }
    monkeypatch.setattr(m5, "_iter_exact_m4_rows", lambda *args, **kwargs: iter([raw]))

    class FakeSession:
        def __init__(self):
            self.calls = 0
        def execute(self, statement, params):
            self.calls += 1
            if self.calls == 1:
                return _MappingsResult([{"field_id": 33, "field_name": "GPX_TF17_2"}])
            if self.calls == 2:
                return _MappingsResult([{"object_id": 42}])
            raise AssertionError(str(statement))

    result = m5.export_global_object_light_curve(
        FakeSession(),
        object_id=42,
        field_id=33,
        out_root=tmp_path / "cache_object42",
        plots=False,
    )
    assert result.status == m5.M5_STATUS_READY
    assert result.science_read_source == "SCIENCE_PHOTOMETRY"
    assert result.accepted_rows == 1


def test_m5_science_iterator_rebuilds_exact_generation_row(monkeypatch) -> None:
    from types import SimpleNamespace

    generation = m5.M5Generation(
        interseries_generation_id=20,
        generation_uid="m4_v20",
        field_id=33,
        instrument_unit_id=7,
        filter_name="V",
        catalog_name="shared",
        reference_series_id=88,
        reliability_status="REDUNDANT_GRAPH_OK",
        graph_dof=27,
        series_sources=((92, 1107),),
    )
    measurement = SimpleNamespace(
        measurement_id=501,
        base_measurement_id=501,
        object_id=29786,
        frame_id=7134,
        x=10.0,
        y=20.0,
        flux=1000.0,
        flux_err=10.0,
        mag_inst=12.0,
        mag_cal=12.34,
        mag_err=0.02,
        mag_err_random=0.02,
        mag_err_systematic=0.01,
        aperture_radius_pix=4.0,
        local_background=3.0,
        local_zp_correction=0.2,
        centroid_dx_pix=0.01,
        centroid_dy_pix=-0.01,
        flags={"catalog_name": "shared", "source_id": "42", "saturated_or_near_saturated": True},
    )
    monkeypatch.setattr(
        m5,
        "load_calibration_photometry_view",
        lambda *args, **kwargs: SimpleNamespace(
            source="SCIENCE_PHOTOMETRY",
            rows=[measurement],
        ),
    )

    class FakeSession:
        def execute(self, statement, params):
            sql = str(statement)
            if "FROM interseries_calibration_series ics" in sql:
                return _MappingsResult([{
                    "interseries_generation_id": 20,
                    "series_id": 92,
                    "science_calibration_version_id": 1107,
                    "offset_mag": 0.003,
                    "offset_err_mag": 0.004,
                    "analysis_algorithm": "interseries_graph_v1",
                }])
            if "FROM frames f" in sql:
                return _MappingsResult([{
                    "frame_id": 7134,
                    "date_obs": None,
                    "mjd": 60000.0,
                    "jd": 2460000.5,
                    "instrument_unit_id": 7,
                    "instrument": "MASTER",
                    "site": "X",
                    "tube": "EAST",
                    "camera": "204",
                    "filter_name": "V",
                    "quality_flags": {},
                    "series_id": 92,
                    "field_id": 33,
                    "field_name": "GPX_TF17_2",
                }])
            if "FROM object_qc" in sql:
                return _MappingsResult([{
                    "object_id": 29786,
                    "calibration_version_id": 1107,
                    "catalog_name": "shared",
                    "instrument_unit_id": 7,
                    "flags": {"HIGH_RMS": False},
                }])
            if "FROM calibration_versions" in sql:
                return _MappingsResult([{
                    "calibration_version_id": 1107,
                    "name": "science_v",
                    "model_name": "ensemble",
                }])
            raise AssertionError(sql)

    rows = list(m5._iter_exact_m4_rows_from_science(
        FakeSession(), object_id=29786, field_id=33, generations=[generation]
    ))
    assert len(rows) == 1
    row = rows[0]
    assert row["science_measurement_id"] == 501
    assert row["measurement_id"] == 501
    assert row["calibration_version_id"] == 1107
    assert row["interseries_generation_id"] == 20
    assert row["interseries_offset_mag"] == pytest.approx(0.003)
    assert row["interseries_offset_err"] == pytest.approx(0.004)
    assert row["interseries_algorithm"] == "interseries_graph_v1"
    assert row["measurement_flags"]["source_id"] == "42"
