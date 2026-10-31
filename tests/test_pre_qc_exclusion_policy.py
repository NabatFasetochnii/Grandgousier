from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("astropy")

from grandgousier.pipeline import runner
from grandgousier.quality import frame_pre_qc


class _NoSessionOperations:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"unexpected session operation: {name}")


class _CleanNoWriteSession:
    def __init__(self, *, new=(), dirty=(), deleted=()):
        self.new = list(new)
        self.dirty = list(dirty)
        self.deleted = list(deleted)
        self.operations: list[str] = []
        self.dml_calls = 0
        self.no_autoflush_enters = 0
        self.no_autoflush_exits = 0
        self.no_autoflush_active = False

    @property
    def no_autoflush(self):
        return self._no_autoflush()

    @contextmanager
    def _no_autoflush(self):
        self.no_autoflush_enters += 1
        self.no_autoflush_active = True
        try:
            yield
        finally:
            self.no_autoflush_active = False
            self.no_autoflush_exits += 1

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement).lstrip().upper()
        self.operations.append("execute")
        if sql.startswith(("INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE")):
            self.dml_calls += 1
            raise AssertionError("target dry-run attempted DML")
        raise AssertionError("unexpected SQL operation in isolated runner test")

    def commit(self):
        self.operations.append("commit")
        raise AssertionError("target dry-run attempted commit")

    def flush(self, *_args, **_kwargs):
        self.operations.append("flush")
        raise AssertionError("target dry-run attempted flush")

    def rollback(self):
        self.operations.append("rollback")
        raise AssertionError("target dry-run attempted rollback")


def _config(
    *,
    frame_ids: list[int] | None = None,
    series_id: int | None = None,
    dry_run: bool = True,
    record_run: bool = False,
    run_pre_photometry_qc: bool = True,
    exclude_bad_pre_qc_frames: bool = True,
) -> runner.FieldPipelineConfig:
    return runner.FieldPipelineConfig(
        catalog_name="policy-test-catalog",
        calibration_prefix="pre-qc-policy-test",
        photometry_estimator="circular",
        series_id=series_id,
        frame_ids=frame_ids,
        run_pre_photometry_qc=run_pre_photometry_qc,
        exclude_bad_pre_qc_frames=exclude_bad_pre_qc_frames,
        skip_wcs=True,
        skip_catalog_frame_match=True,
        skip_detection=True,
        skip_detection_catalog_match=True,
        skip_forced_photometry=True,
        skip_aperture_correction=True,
        skip_aperture_zp=True,
        skip_ensemble=True,
        skip_object_qc=True,
        skip_aperture_stability_qc=True,
        skip_field_report=True,
        export_target_light_curve=False,
        export_candidate_light_curves=False,
        export_candidate_plots=False,
        dry_run=dry_run,
        record_run=record_run,
        continue_on_error=False,
        log_dir=None,
        no_file_log=True,
    )


def _qc_result(selected_ids: list[int], bad_ids: list[int]):
    return SimpleNamespace(
        selected_frame_ids=list(selected_ids),
        bad_frame_ids=list(bad_ids),
        detection_seeds={},
        summary=lambda: {"selected_frames": len(selected_ids), "bad_frames": len(bad_ids)},
    )


def _assert_no_session_writes(session: _CleanNoWriteSession) -> None:
    assert session.operations == []
    assert session.dml_calls == 0


def test_unscoped_ordinary_target_refuses_before_any_session_operation():
    session = _NoSessionOperations()
    config = _config(frame_ids=None, series_id=None, dry_run=False)
    config.instrument_unit_id = 17
    config.field_id = 12

    with pytest.raises(runner.FramePreQcExclusionPreconditionError) as error:
        runner.run_field_pipeline(session, config)

    assert error.value.status == "FAILED_PRECONDITION"
    assert session.calls == []


def test_target_dry_run_with_recording_refuses_before_session_access():
    session = _NoSessionOperations()
    config = _config(frame_ids=[2], dry_run=True, record_run=True)

    with pytest.raises(runner.FramePreQcExclusionPreconditionError) as error:
        runner.run_field_pipeline(session, config)

    assert error.value.status == "FAILED_PRECONDITION"
    assert session.calls == []


def test_target_dry_run_uses_resolved_ids_and_filters_bad_ids_in_memory(monkeypatch):
    session = _CleanNoWriteSession()
    qc_calls = []
    downstream_scopes = []

    def fake_pre_qc(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        qc_calls.append(kwargs["frame_ids"])
        return _qc_result([2, 5, 9], [5])

    def reject_persisted_flag_query(*_args, **_kwargs):
        raise AssertionError("dry-run exclusion must not query persisted QC flags")

    def fake_detection(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        downstream_scopes.append(kwargs["frame_ids"])
        return {"selected_frames": len(kwargs["frame_ids"]), "checked_frames": len(kwargs["frame_ids"])}

    monkeypatch.setattr(runner, "run_frame_pre_qc", fake_pre_qc)
    monkeypatch.setattr(runner, "filter_bad_pre_qc_frame_ids", reject_persisted_flag_query)
    monkeypatch.setattr(runner, "detect_sources_in_frames", fake_detection)

    config = _config(frame_ids=[2, 5, 9])
    config.skip_detection = False
    result = runner.run_field_pipeline(session, config)

    assert qc_calls == [[2, 5, 9]]
    assert downstream_scopes == [[2, 9]]
    assert result.status == "DONE"
    assert session.no_autoflush_enters == 1
    assert session.no_autoflush_exits == 1
    _assert_no_session_writes(session)


def test_unscoped_target_dry_run_passes_none_to_pre_qc_and_survivors_downstream(
    monkeypatch,
):
    session = _CleanNoWriteSession()
    qc_scopes = []
    downstream_scopes = []

    def fake_pre_qc(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        qc_scopes.append(kwargs["frame_ids"])
        return _qc_result([2, 5, 9], [5])

    def fake_detection(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        downstream_scopes.append(kwargs["frame_ids"])
        return {
            "selected_frames": len(kwargs["frame_ids"]),
            "checked_frames": len(kwargs["frame_ids"]),
        }

    monkeypatch.setattr(runner, "run_frame_pre_qc", fake_pre_qc)
    monkeypatch.setattr(runner, "detect_sources_in_frames", fake_detection)

    config = _config(frame_ids=None)
    config.skip_detection = False
    result = runner.run_field_pipeline(session, config)

    assert qc_scopes == [None]
    assert downstream_scopes == [[2, 9]]
    assert result.status == "DONE"
    assert session.no_autoflush_enters == 1
    assert session.no_autoflush_exits == 1
    _assert_no_session_writes(session)


def test_zero_eligible_target_dry_run_raises_precondition_without_downstream_or_writes(
    monkeypatch,
):
    session = _CleanNoWriteSession()
    qc_calls = []
    downstream_calls = []

    def fake_pre_qc(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        qc_calls.append(kwargs["frame_ids"])
        return _qc_result([], [])

    monkeypatch.setattr(runner, "run_frame_pre_qc", fake_pre_qc)
    monkeypatch.setattr(
        runner,
        "detect_sources_in_frames",
        lambda *_args, **_kwargs: downstream_calls.append(True),
    )

    config = _config(frame_ids=[7, 11])
    config.skip_detection = False
    config.continue_on_error = True

    with pytest.raises(runner.FramePreQcExclusionPreconditionError) as error:
        runner.run_field_pipeline(session, config)

    assert error.value.status == "FAILED_PRECONDITION"
    assert qc_calls == [[7, 11]]
    assert downstream_calls == []
    assert session.no_autoflush_enters == 1
    assert session.no_autoflush_exits == 1
    _assert_no_session_writes(session)


def test_all_bad_eligible_target_dry_run_stops_without_downstream_or_writes(
    monkeypatch,
):
    session = _CleanNoWriteSession()
    qc_scopes = []
    downstream_calls = []

    def fake_pre_qc(actual_session, **kwargs):
        assert actual_session is session
        assert session.no_autoflush_active
        qc_scopes.append(kwargs["frame_ids"])
        return _qc_result([7, 11], [7, 11])

    monkeypatch.setattr(runner, "run_frame_pre_qc", fake_pre_qc)
    monkeypatch.setattr(
        runner,
        "detect_sources_in_frames",
        lambda *_args, **_kwargs: downstream_calls.append("detection"),
    )
    monkeypatch.setattr(
        runner,
        "run_forced_aperture_photometry",
        lambda *_args, **_kwargs: downstream_calls.append("forced_photometry"),
    )
    monkeypatch.setattr(
        runner,
        "match_detections_to_catalog",
        lambda *_args, **_kwargs: downstream_calls.append(
            "detection_catalog_match"
        ),
    )

    config = _config(frame_ids=[7, 11])
    config.skip_detection = False
    config.skip_forced_photometry = False
    config.skip_detection_catalog_match = False
    result = runner.run_field_pipeline(session, config)

    assert qc_scopes == [[7, 11]]
    assert result.status == runner.NO_USABLE_FRAMES_STATUS
    assert result.status == "DONE_NO_SCIENCE_FRAMES"
    assert downstream_calls == []
    assert session.no_autoflush_enters == 1
    assert session.no_autoflush_exits == 1
    _assert_no_session_writes(session)


@pytest.mark.parametrize("pending_attribute", ["new", "dirty", "deleted"])
def test_target_dry_run_refuses_pending_orm_state_without_mutating_it(pending_attribute):
    pending = object()
    session = _CleanNoWriteSession(**{pending_attribute: [pending]})
    before = {
        name: list(getattr(session, name)) for name in ("new", "dirty", "deleted")
    }
    config = _config(frame_ids=[3])

    with pytest.raises(runner.FramePreQcExclusionPreconditionError) as error:
        runner.run_field_pipeline(session, config)

    assert error.value.status == "FAILED_PRECONDITION"
    assert {name: list(getattr(session, name)) for name in before} == before
    assert session.no_autoflush_enters == 0
    _assert_no_session_writes(session)


def test_truly_unscoped_pre_qc_without_exclusion_passes_none(monkeypatch):
    session = _NoSessionOperations()
    observed_scopes = []

    def fake_pre_qc(actual_session, **kwargs):
        assert actual_session is session
        observed_scopes.append(kwargs["frame_ids"])
        return SimpleNamespace(summary=lambda: {"selected_frames": 0}, detection_seeds={})

    monkeypatch.setattr(runner, "run_frame_pre_qc", fake_pre_qc)
    config = _config(
        frame_ids=None,
        dry_run=False,
        run_pre_photometry_qc=True,
        exclude_bad_pre_qc_frames=False,
    )

    runner.run_field_pipeline(session, config)

    assert observed_scopes == [None]
    assert session.calls == []


def test_pre_qc_result_exposes_exact_selector_ids_only_as_transient_data(monkeypatch):
    selected_frames = [
        SimpleNamespace(frame_id=2, raw_path="never-opened-2.fits"),
        SimpleNamespace(frame_id=9, raw_path="never-opened-9.fits"),
    ]

    class FakeSession:
        def __init__(self):
            self.statements = []
            self.commit_calls = 0

        def scalars(self, statement):
            self.statements.append(statement)
            return SimpleNamespace(all=lambda: selected_frames)

        def commit(self):
            self.commit_calls += 1
            raise AssertionError("dry-run pre-QC must not commit")

    class MissingFilePath:
        def __init__(self, _path):
            pass

        def exists(self):
            return False

    monkeypatch.setattr(frame_pre_qc, "Path", MissingFilePath)
    session = FakeSession()
    result = frame_pre_qc.run_frame_pre_qc(
        session,
        frame_ids=[2, 9],
        dry_run=True,
    )

    assert result.selected_frame_ids == [2, 9]
    assert "selected_frame_ids" not in result.summary()
    assert len(session.statements) == 1
    assert session.commit_calls == 0
