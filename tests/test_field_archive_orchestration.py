from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier.orchestration.field_archive import (
    ArchiveBatch,
    FieldArchiveRunResult,
    Stage3Policy,
    _batch_action,
    _emit_workflow_event,
    _group_detected_by_parent,
    _inspect_batch_db_state,
    _normalize_date_token,
    _stage2_recipe_kwargs,
    _stage2_series_ids_for_batch,
    _write_reports,
    parse_stage3_policy,
)



def test_workflow_event_callback_is_optional_and_forwards_payload():
    seen = []
    _emit_workflow_event(None, "ignored", stage="x")
    _emit_workflow_event(lambda event, payload: seen.append((event, payload)), "batch", stage="stage2", n=3)
    assert seen == [("batch", {"stage": "stage2", "n": 3})]

def test_parse_stage3_policy_keeps_explicit_scientific_thresholds():
    policy = parse_stage3_policy("V:15:76")
    assert policy == Stage3Policy(
        filter_name="V",
        min_common_objects=15,
        reference_series_id=76,
    )

    policy = parse_stage3_policy("R:20:77:8")
    assert policy.min_common_objects == 20
    assert policy.reference_series_id == 77
    assert policy.instrument_unit_id == 8


@pytest.mark.parametrize("value", ["", "V", "V:0", "V:x", "V:15:0", "V:15:76:8:extra"])
def test_parse_stage3_policy_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_stage3_policy(value)


def test_stage2_scale_recipe_preserves_validated_semantics():
    recipe = _stage2_recipe_kwargs("stage3_scale_v1")
    assert recipe["photometry_estimator"] == "adaptive_ellipse"
    assert recipe["aperture_correction_plane_policy"] == "evidence"
    assert recipe["exclude_bad_pre_qc_frames"] is True
    assert recipe["enable_detection_branch"] is True
    assert recipe["enable_psf_field_diagnostics"] is True
    assert recipe["ensemble_min_ref_stars_per_frame"] == 20
    assert recipe["exclude_candidates_from_ensemble"] is True
    assert recipe["candidate_ensemble_until_converged"] is True
    assert recipe["candidate_ensemble_convergence_max_iterations"] == 10
    assert recipe["candidate_ensemble_growth_policy"] == "initial_only"
    assert recipe["enable_photometric_frame_qc"] is True
    assert recipe["enable_color_calibration"] is True
    assert recipe["target_max_sep_arcsec"] == -1.0
    assert recipe["export_target_light_curve"] is False
    assert recipe["enable_spatial_ensemble"] is False


def test_batch_db_state_uses_ready_observation_representatives_over_historical_frames():
    class FakeMappedResult:
        def __init__(self, rows):
            self._rows = rows
        def mappings(self):
            return self
        def all(self):
            return list(self._rows)

    class FakeSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "WHERE fl.locator = ANY(:paths)" in sql:
                return FakeMappedResult([
                    {"raw_path": "p1", "frame_id": 1, "observation_id": 101, "series_id": 74, "field_id": 33},
                    {"raw_path": "p1", "frame_id": 2, "observation_id": 101, "series_id": None, "field_id": None},
                    {"raw_path": "p2", "frame_id": 3, "observation_id": 102, "series_id": 92, "field_id": 33},
                    {"raw_path": "p3", "frame_id": 4, "observation_id": 103, "series_id": 75, "field_id": 33},
                    {"raw_path": "p4", "frame_id": 5, "observation_id": 104, "series_id": 93, "field_id": 33},
                ])
            if "WHERE fr.observation_id = ANY(:observation_ids)" in sql:
                return FakeMappedResult([
                    {"observation_id": 101, "series_id": 74, "field_id": 33},
                    {"observation_id": 101, "series_id": 76, "field_id": 33},
                    {"observation_id": 102, "series_id": 92, "field_id": 33},
                    {"observation_id": 103, "series_id": 75, "field_id": 33},
                    {"observation_id": 103, "series_id": 77, "field_id": 33},
                    {"observation_id": 104, "series_id": 93, "field_id": 33},
                ])
            raise AssertionError(sql)

    ready = {
        74: SimpleNamespace(status="NO_SCIENCE_ROWS", n_science_frames=0),
        75: SimpleNamespace(status="NO_SCIENCE_ROWS", n_science_frames=0),
        76: SimpleNamespace(status="READY", n_science_frames=25),
        77: SimpleNamespace(status="READY", n_science_frames=25),
        92: SimpleNamespace(status="READY", n_science_frames=111),
        93: SimpleNamespace(status="READY", n_science_frames=111),
    }

    state = _inspect_batch_db_state(
        FakeSession(),
        paths=["p1", "p2", "p3", "p4"],
        field_id=33,
        ready_by_series=ready,
    )

    assert state == {
        "status": "READY",
        "indexed_files": 4,
        "series_ids": [76, 77, 92, 93],
        "resume_series_ids": [],
        "reason": None,
    }



def test_batch_db_state_marks_only_failed_existing_series_for_resume():
    class FakeMappedResult:
        def __init__(self, rows):
            self._rows = rows
        def mappings(self):
            return self
        def all(self):
            return list(self._rows)

    class FakeSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "WHERE fl.locator = ANY(:paths)" in sql:
                return FakeMappedResult([
                    {"raw_path": "p1", "frame_id": 1, "observation_id": 101, "series_id": 5, "field_id": 33},
                    {"raw_path": "p2", "frame_id": 2, "observation_id": 102, "series_id": 6, "field_id": 33},
                ])
            if "WHERE fr.observation_id = ANY(:observation_ids)" in sql:
                return FakeMappedResult([
                    {"observation_id": 101, "series_id": 5, "field_id": 33},
                    {"observation_id": 102, "series_id": 6, "field_id": 33},
                ])
            raise AssertionError(sql)

    ready = {
        5: SimpleNamespace(status="FAILED", n_science_frames=0),
        6: SimpleNamespace(status="READY", n_science_frames=1),
    }
    state = _inspect_batch_db_state(
        FakeSession(),
        paths=["p1", "p2"],
        field_id=33,
        ready_by_series=ready,
    )

    assert state["status"] == "INCOMPLETE"
    assert state["series_ids"] == [5, 6]
    assert state["resume_series_ids"] == [5]
    assert "series_id=5" in state["reason"]

    batch = ArchiveBatch(
        path="batch",
        relative_path="2018/09/10",
        observation_date="2018-09-10",
        n_files=78,
        object_names=("GPX_TF17_2",),
        filters=("R", "V"),
        action="RESUME_INCOMPLETE",
        db_status="INCOMPLETE",
        indexed_files=78,
        series_ids=(5, 6),
        resume_series_ids=(5,),
    )
    assert _stage2_series_ids_for_batch(batch) == [5]


def test_resume_incomplete_without_existing_failed_series_keeps_discovery_mode():
    batch = ArchiveBatch(
        path="batch",
        relative_path="2018/09/10",
        observation_date="2018-09-10",
        n_files=78,
        object_names=("GPX_TF17_2",),
        filters=("R", "V"),
        action="RESUME_INCOMPLETE",
        db_status="INCOMPLETE",
        indexed_files=70,
        series_ids=(5, 6),
        resume_series_ids=(),
    )
    assert _stage2_series_ids_for_batch(batch) is None

def test_batch_db_state_keeps_truly_unrepresented_observation_incomplete():
    class FakeMappedResult:
        def __init__(self, rows):
            self._rows = rows
        def mappings(self):
            return self
        def all(self):
            return list(self._rows)

    class FakeSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "WHERE fl.locator = ANY(:paths)" in sql:
                return FakeMappedResult([
                    {"raw_path": "p1", "frame_id": 1, "observation_id": 101, "series_id": None, "field_id": None},
                ])
            if "WHERE fr.observation_id = ANY(:observation_ids)" in sql:
                return FakeMappedResult([])
            raise AssertionError(sql)

    state = _inspect_batch_db_state(
        FakeSession(),
        paths=["p1"],
        field_id=33,
        ready_by_series={},
    )

    assert state["status"] == "INCOMPLETE"
    assert "unrepresented_observations=1" in state["reason"]


def test_batch_db_state_preserves_legacy_null_observation_assignment_guard():
    class FakeMappedResult:
        def __init__(self, rows):
            self._rows = rows
        def mappings(self):
            return self
        def all(self):
            return list(self._rows)

    class FakeSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "WHERE fl.locator = ANY(:paths)" in sql:
                return FakeMappedResult([
                    {"raw_path": "legacy", "frame_id": 1, "observation_id": None, "series_id": None, "field_id": None},
                ])
            raise AssertionError(sql)

    state = _inspect_batch_db_state(
        FakeSession(),
        paths=["legacy"],
        field_id=33,
        ready_by_series={},
    )

    assert state["status"] == "INCOMPLETE"
    assert "legacy_unassigned_frames=1" in state["reason"]


def test_resume_batch_actions_do_not_rerun_ready_data():
    assert _batch_action("READY", resume=True) == "SKIP_READY"
    assert _batch_action("NEW", resume=True) == "RUN_NEW"
    assert _batch_action("INCOMPLETE", resume=True) == "RESUME_INCOMPLETE"
    assert _batch_action("READY", resume=False) == "STOP_CONFLICT"
    assert _batch_action("CONFLICT", resume=True) == "STOP_CONFLICT"
    assert _batch_action("RECIPE_MISMATCH", resume=True) == "STOP_CONFLICT"
    assert _batch_action("MISSING_PROVENANCE", resume=True) == "STOP_CONFLICT"


def test_too_small_new_or_incomplete_batches_are_skipped_before_stage2():
    assert _batch_action("NEW", resume=True, max_series_like_group_files=9) == "SKIP_TOO_SMALL"
    assert _batch_action("INCOMPLETE", resume=True, max_series_like_group_files=3) == "SKIP_TOO_SMALL"
    assert _batch_action("NEW", resume=True, max_series_like_group_files=10) == "RUN_NEW"
    assert _batch_action("READY", resume=True, max_series_like_group_files=1) == "SKIP_READY"


def test_group_detected_by_parent_creates_independent_leaf_batches(tmp_path: Path):
    a = tmp_path / "2018" / "09" / "16"
    b = tmp_path / "2018" / "09" / "23"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    rows = [
        SimpleNamespace(path=str(a / "1.fits"), date_obs=datetime(2018, 9, 16, 18, tzinfo=timezone.utc), object_name="GPX", filter_name="V"),
        SimpleNamespace(path=str(a / "2.fits"), date_obs=datetime(2018, 9, 16, 18, tzinfo=timezone.utc), object_name="GPX", filter_name="R"),
        SimpleNamespace(path=str(b / "3.fits"), date_obs=datetime(2018, 9, 23, 18, tzinfo=timezone.utc), object_name="GPX", filter_name="V"),
    ]
    batches = _group_detected_by_parent(tmp_path, rows)
    assert [row["observation_date"] for row in batches] == ["2018-09-16", "2018-09-23"]
    assert batches[0]["relative_path"] == str(Path("2018") / "09" / "16")
    assert batches[0]["filters"] == ["R", "V"]
    assert len(batches[0]["paths"]) == 2
    assert batches[0]["max_series_like_group_files"] == 1


def test_date_normalization_accepts_windows_or_posix_tokens():
    assert _normalize_date_token("2018-09-07") == "2018-09-07"
    assert _normalize_date_token("2018/09/07") == "2018-09-07"
    assert _normalize_date_token(r"2018\09\07") == "2018-09-07"


def test_consolidated_report_writes_machine_and_human_products(tmp_path: Path):
    result = FieldArchiveRunResult(
        field_id=33,
        processed_root=r"C:\master\gpx\smoke\processed",
        catalog_name="shared_catalog",
        execute=True,
        resume=True,
        stage2_recipe="stage3_scale_v1",
        policies=[Stage3Policy("V", 15, 76)],
        control_object_ids=[30112],
        status="READY",
    )
    result.batches = [
        ArchiveBatch(
            path="batch",
            relative_path="2018/09/16",
            observation_date="2018-09-16",
            n_files=544,
            object_names=("GPX",),
            filters=("R", "V"),
            action="RUN_NEW",
            db_status="NEW",
            indexed_files=0,
            series_ids=(),
        ).to_dict()
    ]
    result.stage2_runs = [{
        "batch_date": "2018-09-16",
        "batch_relative_path": "2018/09/16",
        "pipeline_results": [{
            "series_id": 88,
            "filter_name": "V",
            "status": "DONE",
            "science_status": "OK",
            "total_points": 100,
            "accepted_points": 90,
            "science_calibration": "science_v",
            "quality_policy": {
                "photometric_frame_qc_good_frames": 250,
                "photometric_frame_qc_suspect_frames": 18,
                "photometric_frame_qc_bad_frames": 4,
                "photometric_frame_qc_frames_with_spatial_systematic": 20,
            },
        }],
    }]
    result.control_objects = [{
        "object_id": 30112,
        "status": "READY",
        "rows_source_exact_m4": 1888,
        "accepted_rows": 1860,
        "rejected_rows": 28,
        "generation_ids": [15, 16],
        "rejection_reasons": {"NO_VALID_MAG": 28},
        "csv_path": "object.csv",
        "summary_path": "object_summary.json",
    }]

    _write_reports(result, tmp_path)
    for name in (
        "final_report.json",
        "batches.csv",
        "series.csv",
        "interseries_edges.csv",
        "interseries_offsets.csv",
        "control_objects.csv",
        "frame_qc.csv",
        "report.html",
    ):
        assert (tmp_path / name).exists()
    assert "Grandgousier field archive report" in (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "good_frames" in (tmp_path / "frame_qc.csv").read_text(encoding="utf-8-sig")


def test_execute_orchestration_runs_new_batch_then_stage3_and_m5(monkeypatch, tmp_path: Path):
    import sys
    import types

    root = tmp_path / "processed"
    batch = root / "2018" / "09" / "16"
    batch.mkdir(parents=True)

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    detected = [
        *[
            SimpleNamespace(
                path=str(batch / f"v_{i:02d}.fits"),
                date_obs=datetime(2018, 9, 16, 18, tzinfo=timezone.utc),
                object_name="GPX",
                filter_name="V",
            )
            for i in range(10)
        ],
        *[
            SimpleNamespace(
                path=str(batch / f"r_{i:02d}.fits"),
                date_obs=datetime(2018, 9, 16, 18, tzinfo=timezone.utc),
                object_name="GPX",
                filter_name="R",
            )
            for i in range(10)
        ],
    ]

    class FakeArchiveResult:
        status = "DONE_WITH_WARNINGS"
        failed_files = 0
        processed_series_ids = [88, 89]
        pipeline_results = [
            {"series_id": 88, "filter_name": "V", "status": "DONE_WITH_WARNINGS", "science_status": "OK", "quality_policy": {}},
            {"series_id": 89, "filter_name": "R", "status": "DONE", "science_status": "OK", "quality_policy": {}},
        ]
        warnings = ["target provenance disabled"]

        def to_dict(self):
            return {
                "status": self.status,
                "failed_files": 0,
                "processed_series_ids": self.processed_series_ids,
                "pipeline_results": self.pipeline_results,
                "warnings": self.warnings,
            }

    process_calls = []

    def fake_process_archive_path(session, **kwargs):
        process_calls.append(kwargs)
        return FakeArchiveResult()

    module(
        "grandgousier.archive.auto_process",
        DEFAULT_EXTENSIONS=(".fits",),
        process_archive_path=fake_process_archive_path,
        scan_archive_path=lambda *a, **k: (detected, [], []),
    )

    class FakeMappedResult:
        def __init__(self, rows):
            self.rows = rows
        def mappings(self):
            return self
        def all(self):
            return list(self.rows)
        def first(self):
            return self.rows[0] if self.rows else None
        def scalars(self):
            return self

    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, statement, params=None):
            sql = str(statement)
            params = params or {}
            if "FROM frames fr" in sql:
                return FakeMappedResult([])
            if "FROM field_series" in sql and "WHERE series_id" in sql:
                sid = int(params["series_id"])
                return FakeMappedResult([{
                    "series_id": sid,
                    "field_id": 33,
                    "instrument_unit_id": 7 if sid == 76 else 8,
                    "filter_name": "V" if sid == 76 else "R",
                }])
            raise AssertionError(sql)

    class FakeSessionFactory:
        def __call__(self): return FakeSession()
        def begin(self): return FakeSession()

    module("grandgousier.db.session", make_session_factory=lambda url: FakeSessionFactory())

    class Candidate:
        def __init__(self, sid, filt, cv):
            self.series_id = sid
            self.filter_name = filt
            self.instrument_unit_id = 7 if filt == "V" else 8
            self.calibration_version_id = cv
            self.status = "READY"
            self.n_science_frames = 272

    class Plan:
        status = "READY_FOR_GRAPH_VALIDATION"
        recommended_reference_series_id = 76
        def __init__(self):
            self.series = [Candidate(88, "V", 1100), Candidate(89, "R", 1101)]
        def to_dict(self):
            return {"status": self.status, "series": [vars(v) for v in self.series]}

    module("grandgousier.interseries.planning", build_interseries_plan=lambda *a, **k: Plan())

    class Audit:
        status = "READY_SHARED_IDENTITY"
        def to_dict(self): return {"status": self.status}
    module("grandgousier.interseries.catalog_identity", audit_field_catalog_identity=lambda *a, **k: Audit())

    class Graph:
        status = "OK"
        n_series = 8
        n_edges = 28
        dof = 21
        reduced_chi2 = 1.1
        max_abs_standardized_residual = 1.8

    class Analysis:
        status = "READY_M1_M3"
        graph_solution = Graph()
        selected_series_ids = tuple(range(1, 9))
        pairwise_offsets = ()
        def to_dict(self):
            return {
                "status": self.status,
                "selected_series_ids": list(self.selected_series_ids),
                "pairwise_offsets": [],
                "graph_solution": {
                    "status": "OK", "n_series": 8, "n_edges": 28, "dof": 21,
                    "reduced_chi2": 1.1, "max_abs_standardized_residual": 1.8,
                    "offsets": [],
                },
            }

    def fake_export_analysis(analysis, out_root):
        Path(out_root).mkdir(parents=True, exist_ok=True)
        p = Path(out_root) / "summary.json"
        p.write_text("{}", encoding="utf-8")
        return {"summary": p}

    module(
        "grandgousier.interseries.analysis",
        analyze_interseries_calibration=lambda *a, **k: Analysis(),
        export_interseries_analysis=fake_export_analysis,
    )

    generation_counter = iter([15, 16])
    class Persisted:
        def __init__(self, gid):
            self.interseries_generation_id = gid
        def to_dict(self):
            return {"interseries_generation_id": self.interseries_generation_id, "created": True}
    class Verification:
        status = "OK"
        def to_dict(self): return {"status": "OK"}

    module(
        "grandgousier.interseries.persistence",
        persist_interseries_calibration=lambda *a, **k: Persisted(next(generation_counter)),
        verify_interseries_generation=lambda *a, **k: Verification(),
        load_interseries_generation_series=lambda *a, **k: [],
        load_interseries_generation_edges=lambda *a, **k: [],
    )
    module(
        "grandgousier.photometry.science_materialization",
        science_materialization_parity_report=lambda *a, **k: {"status": "OK", "calibration_version_id": k["calibration_version_id"]},
    )

    m5_export_calls = []

    class Control:
        object_id = 30112
        status = "READY"
        rows_source_exact_m4 = 1888
        accepted_rows = 1860
        rejected_rows = 28

        def __init__(self, bundle_root):
            self.bundle_root = Path(bundle_root)

        def to_dict(self):
            return {
                "object_id": self.object_id, "status": self.status,
                "rows_source_exact_m4": self.rows_source_exact_m4,
                "accepted_rows": self.accepted_rows, "rejected_rows": self.rejected_rows,
                "generation_ids": [15, 16], "rejection_reasons": {"NO_VALID_MAG": 28},
                "csv_path": str(self.bundle_root / f"{self.bundle_root.name}.csv"),
                "summary_path": str(self.bundle_root / f"{self.bundle_root.name}_summary.json"),
            }

    def fake_export_m5(_session, **kwargs):
        m5_export_calls.append(kwargs)
        return Control(kwargs["out_root"])

    module(
        "grandgousier.lightcurves.global_object",
        export_global_object_light_curve=fake_export_m5,
    )

    from grandgousier.orchestration.field_archive import run_field_archive_workflow
    result = run_field_archive_workflow(
        database_url=None,
        processed_root=root,
        field_id=33,
        catalog_name="shared",
        policies=[parse_stage3_policy("V:15:76"), parse_stage3_policy("R:20:77")],
        control_object_ids=[30112],
        execute=True,
        exclude_dates=[],
        out_root=tmp_path / "report",
    )
    assert result.status == "READY"
    assert len(process_calls) == 1
    assert result.stage2_runs[0]["pipeline_results"][0]["status"] == "DONE_WITH_WARNINGS"
    assert process_calls[0]["photometry_estimator"] == "adaptive_ellipse"
    assert process_calls[0]["enable_spatial_ensemble"] is False
    assert [row["generation"]["interseries_generation_id"] for row in result.stage3] == [15, 16]
    assert result.control_objects[0]["accepted_rows"] == 1860
    expected_bundle = tmp_path / "report" / "m5" / "object_30112"
    assert m5_export_calls[0]["out_root"] == expected_bundle
    assert result.control_objects[0]["csv_path"] == str(expected_bundle / "object_30112.csv")
    assert result.control_objects[0]["summary_path"] == str(
        expected_bundle / "object_30112_summary.json"
    )
    assert (tmp_path / "report" / "final_report.json").exists()


def test_duplicate_filter_policies_are_rejected(tmp_path: Path):
    from grandgousier.orchestration.field_archive import run_field_archive_workflow

    root = tmp_path / "processed"
    root.mkdir()
    with pytest.raises(ValueError, match="Duplicate Stage-3 policy"):
        run_field_archive_workflow(
            database_url=None,
            processed_root=root,
            field_id=33,
            catalog_name="shared",
            policies=[parse_stage3_policy("V:15:76"), parse_stage3_policy("v:20:82")],
        )


def test_group_detected_marks_multidate_leaf_ambiguous(tmp_path: Path):
    from grandgousier.orchestration.field_archive import _group_detected_by_parent

    root = tmp_path / "processed"
    leaf = root / "mixed"
    leaf.mkdir(parents=True)
    rows = [
        SimpleNamespace(path=str(leaf / "a.fits"), date_obs=datetime(2018, 9, 16, 23, tzinfo=timezone.utc), object_name="GPX", filter_name="V"),
        SimpleNamespace(path=str(leaf / "b.fits"), date_obs=datetime(2018, 9, 17, 0, tzinfo=timezone.utc), object_name="GPX", filter_name="R"),
    ]
    grouped = _group_detected_by_parent(root, rows)
    assert len(grouped) == 1
    assert grouped[0]["observation_date"] is None
    assert grouped[0]["date_ambiguous"] is True
    assert grouped[0]["observation_dates"] == ["2018-09-16", "2018-09-17"]


def test_real_plan_shape_marks_6_and_3_frame_leaves_too_small(tmp_path: Path):
    root = tmp_path / "processed"
    d1 = root / "2018" / "09" / "08"
    d2 = root / "2018" / "10" / "02"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    rows = [
        *[
            SimpleNamespace(
                path=str(d1 / f"v_{i}.fits"),
                date_obs=datetime(2018, 9, 8, 18, tzinfo=timezone.utc),
                object_name="GPX_TF17_2",
                filter_name="V",
                profile_name="MASTER",
                instrument="MASTER",
                site="KAO",
                tube="EAST",
                camera="204",
            )
            for i in range(3)
        ],
        *[
            SimpleNamespace(
                path=str(d1 / f"r_{i}.fits"),
                date_obs=datetime(2018, 9, 8, 18, tzinfo=timezone.utc),
                object_name="GPX_TF17_2",
                filter_name="R",
                profile_name="MASTER",
                instrument="MASTER",
                site="KAO",
                tube="WEST",
                camera="203",
            )
            for i in range(3)
        ],
        *[
            SimpleNamespace(
                path=str(d2 / f"r_{i}.fits"),
                date_obs=datetime(2018, 10, 2, 18, tzinfo=timezone.utc),
                object_name="GPX_TF17_2",
                filter_name="R",
                profile_name="MASTER",
                instrument="MASTER",
                site="KAO",
                tube="WEST",
                camera="203",
            )
            for i in range(3)
        ],
    ]
    batches = _group_detected_by_parent(root, rows)
    assert [(row["observation_date"], row["max_series_like_group_files"]) for row in batches] == [
        ("2018-09-08", 3),
        ("2018-10-02", 3),
    ]
    assert all(
        _batch_action("NEW", resume=True, max_series_like_group_files=row["max_series_like_group_files"])
        == "SKIP_TOO_SMALL"
        for row in batches
    )


def test_batch_db_state_blocks_recipe_mismatch_without_resume_rerun():
    class FakeMappedResult:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return self
        def all(self): return list(self._rows)
    class FakeSession:
        def execute(self, statement, params=None):
            sql = str(statement)
            if "WHERE fl.locator = ANY(:paths)" in sql:
                return FakeMappedResult([
                    {"raw_path": "p1", "frame_id": 1, "observation_id": 101, "series_id": 92, "field_id": 33},
                ])
            if "WHERE fr.observation_id = ANY(:observation_ids)" in sql:
                return FakeMappedResult([
                    {"observation_id": 101, "series_id": 92, "field_id": 33},
                ])
            raise AssertionError(sql)

    ready = {
        92: SimpleNamespace(
            status="RECIPE_MISMATCH",
            n_science_frames=111,
            recipe_mismatch_reasons=("photometry_estimator:expected=adaptive_ellipse,actual=circular",),
        )
    }
    state = _inspect_batch_db_state(
        FakeSession(), paths=["p1"], field_id=33, ready_by_series=ready,
    )
    assert state["status"] == "RECIPE_MISMATCH"
    assert "photometry_estimator" in state["reason"]
    assert _batch_action(state["status"], resume=True) == "STOP_CONFLICT"
