"""Read-only, exact-path selective Stage-2 planning contract."""

from types import SimpleNamespace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from grandgousier.archive import auto_process


@pytest.fixture
def archive_plan(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE field_series (series_id integer PRIMARY KEY)",
            "CREATE TABLE observations (observation_id integer PRIMARY KEY, observation_uid text)",
            "CREATE TABLE frames (frame_id integer PRIMARY KEY, observation_id integer)",
            "CREATE TABLE frame_locations (frame_id integer, locator text, is_primary boolean)",
            "CREATE TABLE field_series_frames (series_id integer, frame_id integer)",
        ):
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO field_series VALUES (1), (2)"))
        for fid in range(1, 27):
            conn.execute(text("INSERT INTO observations VALUES (:id, :uid)"),
                         {"id": fid, "uid": f"master:{fid}"})
            conn.execute(text("INSERT INTO frames VALUES (:id, :id)"), {"id": fid})
            conn.execute(text("INSERT INTO frame_locations VALUES (:id, :path, 1)"),
                         {"id": fid, "path": str(Path(f"/fits/{fid}.fits"))})
            conn.execute(text("INSERT INTO field_series_frames VALUES (:sid, :id)"),
                         {"sid": 1 if fid <= 25 else 2, "id": fid})

    def scan_exact(paths, **_kwargs):
        return ([SimpleNamespace(path=str(p), observation_uid=f"master:{p.stem}")
                 for p in paths], [], [])

    monkeypatch.setattr(auto_process, "scan_archive_paths", scan_exact)
    monkeypatch.setattr(auto_process, "_science_identity_candidate", lambda item: item)
    monkeypatch.setattr(auto_process, "load_auto_profiles", lambda **_kw: {})
    monkeypatch.setattr(auto_process, "plan_file_science_selection", lambda *_a, **_kw:
                        SimpleNamespace(summary={"already_represented": 25}, rows=[]))
    monkeypatch.setattr(auto_process, "_write_manifest_if_requested", lambda *_a, **_kw: None)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _plan(session, ids, series_ids):
    before = session.execute(text("SELECT total_changes()")).scalar_one()
    result = auto_process.process_archive_path(
        session, root="/fits", exact_paths=[f"/fits/{fid}.fits" for fid in ids],
        catalog_name="test", series_ids=series_ids,
    )
    assert session.execute(text("SELECT total_changes()")).scalar_one() == before
    return result


def test_represented_25_frames_are_explicitly_planned(archive_plan):
    result = _plan(archive_plan, range(1, 26), [1])
    assert result.status == "PLAN"
    assert result.processed_series_ids == []
    assert result.created_series_ids == []
    selection = result.observation_selection_results[1]
    assert selection["selected_series_ids"] == [1]
    assert selection["rows"][0]["selected_frame_count"] == 25
    assert selection["rows"][0]["total_series_frame_count"] == 25
    assert selection["rows"][0]["matched_observation_identity_count"] == 25
    assert len(selection["rows"][0]["selected_observation_uids"]) == 25
    assert selection["rows"][0]["coverage"] == "COMPLETE"


@pytest.mark.parametrize("requested", [[999], [2], [1, 2]])
def test_missing_or_unrelated_series_fails_closed(archive_plan, requested):
    result = _plan(archive_plan, range(1, 26), requested)
    assert result.status == "FAILED_PRECONDITION_SERIES_SELECTION"
    assert result.observation_selection_results[1]["selected_series_ids"] == []


def test_subset_is_reported_as_partial_supported_execute_scope(archive_plan):
    result = _plan(archive_plan, range(1, 11), [1])
    assert result.status == "PLAN"
    row = result.observation_selection_results[1]["rows"][0]
    assert row["coverage"] == "SUBSET"
    assert row["selected_frame_ids"] == list(range(1, 11))
    assert row["missing_series_frame_ids"] == list(range(11, 26))
    assert row["matched_observation_identity_count"] == 10


def test_ordinary_plan_unchanged(archive_plan):
    result = _plan(archive_plan, range(1, 26), None)
    assert result.status == "PLAN"
    assert [entry["mode"] for entry in result.observation_selection_results] == ["PLAN_INPUT_FILES"]


def test_scanned_identity_mismatch_fails_closed(archive_plan, monkeypatch):
    monkeypatch.setattr(auto_process, "scan_archive_paths", lambda paths, **_kw: (
        [SimpleNamespace(path=str(paths[0]), observation_uid="wrong")], [], []
    ))
    result = _plan(archive_plan, [1], [1])
    assert result.status == "FAILED_PRECONDITION_SERIES_SELECTION"
    assert result.observation_selection_results[1]["rows"][0]["identity_mismatch_frame_ids"] == [1]
