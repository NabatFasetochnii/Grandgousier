from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy.exc import SQLAlchemyError

from grandgousier.archive import auto_process
from grandgousier.calibration.photometric_policy import ColorCalibrationPolicy
from grandgousier.catalogs import reference_photometry
from grandgousier.pipeline import runner


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _ImporterSession:
    def __init__(self, *, fail_on_execute: bool = False):
        self.fail_on_execute = fail_on_execute
        self.commit_count = 0
        self.rollback_count = 0
        self.executed = []

    def execute(self, statement, *_args, **_kwargs):
        self.executed.append(statement)
        if self.fail_on_execute:
            raise SQLAlchemyError("synthetic reference write failure")
        if getattr(statement, "is_select", False):
            return _Rows([("ref-1", 101)])
        return _Rows()

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class _TransactionalSession:
    def __init__(self):
        self.state = {}
        self.committed_state = {}
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, *_args, **_kwargs):
        return _Rows()

    def commit(self):
        self.commit_count += 1
        self.committed_state = deepcopy(self.state)

    def rollback(self):
        self.rollback_count += 1
        self.state = deepcopy(self.committed_state)


def _patch_importer(monkeypatch):
    monkeypatch.setattr(
        reference_photometry,
        "_read_reference_photometry_table",
        lambda *_args, **_kwargs: pd.DataFrame(
            [{"source": "ref-1", "ra": 10.0, "dec": 20.0, "mag_g": 15.0}]
        ),
    )
    monkeypatch.setattr(
        reference_photometry, "_upsert_reference_catalog", lambda *_args, **_kwargs: 7
    )
    monkeypatch.setattr(
        reference_photometry,
        "_ensure_photometric_bands",
        lambda *_args, **_kwargs: {"g": 3},
    )


def _import(session, *, commit=None):
    kwargs = {} if commit is None else {"commit": commit}
    return reference_photometry.import_reference_photometry(
        session,
        path="reference.csv",
        catalog_name="test_reference",
        source_id_col="source",
        mag_cols={"g": "mag_g"},
        **kwargs,
    )


def test_import_reference_photometry_commits_by_default(monkeypatch):
    _patch_importer(monkeypatch)
    session = _ImporterSession()

    result = _import(session)

    assert result.valid_rows == 1
    assert result.inserted_or_updated == 1
    assert session.commit_count == 1
    assert session.rollback_count == 0


def test_import_reference_photometry_commit_false_leaves_transaction_open(monkeypatch):
    _patch_importer(monkeypatch)
    session = _ImporterSession()

    result = _import(session, commit=False)

    assert result.inserted_or_updated == 1
    assert len(session.executed) == 4
    assert session.commit_count == 0
    assert session.rollback_count == 0


def test_import_sql_error_with_commit_false_defers_rollback(monkeypatch):
    _patch_importer(monkeypatch)
    session = _ImporterSession(fail_on_execute=True)

    with pytest.raises(RuntimeError, match="Reference photometry DB import failed"):
        _import(session, commit=False)

    assert session.rollback_count == 0
    assert session.commit_count == 0


def _policy() -> ColorCalibrationPolicy:
    return ColorCalibrationPolicy(
        enabled=True,
        reference_catalog_name="panstarrs_dr2",
        reference_band="g",
        color_index="g-r",
        require_bands=("g", "r"),
        min_stars_per_frame=1,
    )


def _configure_auto_reference(monkeypatch, tmp_path, *, importer, coverage_check):
    monkeypatch.setattr(
        auto_process,
        "load_persisted_identity_sources",
        lambda *_args, **_kwargs: [
            {"source_id": "42", "ra_deg": 10.0, "dec_deg": 20.0}
        ],
    )
    monkeypatch.setattr(
        auto_process, "_reference_band_counts_for_series", coverage_check
    )
    def fake_provider(_session, **kwargs):
        csv_path = Path(kwargs["out_path"])
        csv_path.write_text(
            "catalog_source_id,selected_identity_source_id\nps1-42,42\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            raw_rows=1,
            written_rows=1,
            path=str(csv_path),
            filter_summary={
                "selected_reference_diagnostics": {
                    "logical_query_groups": 1,
                    "provider_requests": 1,
                    "provider_response_bytes_received": 42,
                    "provider_rows_received": 1,
                    "provider_rows_retained": 1,
                    "provider_exact_duplicates_collapsed": 0,
                    "provider_conflicting_source_ids": 0,
                    "provider_conflicting_rows": 0,
                    "matched_rows": 1,
                    "ambiguous_rows": 0,
                    "ambiguous_identities": 0,
                    "unmatched_rows": 0,
                    "unmatched_identities": 0,
                }
            },
        )

    monkeypatch.setattr(auto_process, "create_reference_photometry_csv", fake_provider)
    monkeypatch.setattr(auto_process, "import_reference_photometry", importer)


def _ensure_reference(session, tmp_path):
    return auto_process._ensure_reference_photometry_for_series(
        session,
        series={"series_id": 5, "field_id": 8},
        policy=_policy(),
        identity_catalog_name="gaia_dr3",
        selected_identity_source_ids={"42"},
        out_dir=tmp_path,
        allow_remote_fetch=True,
    )


def test_reference_import_failure_rolls_back_only_reference_writes(
    monkeypatch, tmp_path
):
    session = _TransactionalSession()
    session.state["gaia_promotion"] = "committed"
    session.commit()  # The preceding Gaia promotion step is already durable.

    def fail_import(_session, **kwargs):
        assert kwargs["commit"] is False
        session.state["reference_rows"] = "uncommitted"
        raise RuntimeError("synthetic import failure")

    _configure_auto_reference(
        monkeypatch,
        tmp_path,
        importer=fail_import,
        coverage_check=lambda *_args, **_kwargs: {},
    )

    result = _ensure_reference(session, tmp_path)

    assert result["status"] == "REFERENCE_ACQUISITION_FAILED"
    assert session.rollback_count == 1
    assert session.commit_count == 1
    assert session.state == {"gaia_promotion": "committed"}
    assert session.committed_state == {"gaia_promotion": "committed"}


def test_post_import_coverage_error_rolls_back_reference_writes(
    monkeypatch, tmp_path
):
    session = _TransactionalSession()
    session.state["gaia_promotion"] = "committed"
    session.commit()
    coverage_calls = 0

    def coverage_check(*_args, **_kwargs):
        nonlocal coverage_calls
        coverage_calls += 1
        if coverage_calls == 2:
            raise RuntimeError("synthetic post-import coverage failure")
        return {}

    def import_reference(_session, **kwargs):
        assert kwargs["commit"] is False
        session.state["reference_rows"] = "uncommitted"
        return SimpleNamespace(
            inserted_or_updated=1, matched_to_gaia=1, unmatched_to_gaia=0
        )

    _configure_auto_reference(
        monkeypatch,
        tmp_path,
        importer=import_reference,
        coverage_check=coverage_check,
    )

    result = _ensure_reference(session, tmp_path)

    assert result["status"] == "REFERENCE_POST_IMPORT_CHECK_FAILED"
    assert session.rollback_count == 1
    assert session.commit_count == 1
    assert session.state == {"gaia_promotion": "committed"}


def test_insufficient_valid_coverage_is_nonfatal_and_commits_with_reference_step(
    monkeypatch, tmp_path
):
    session = _TransactionalSession()
    session.state["gaia_promotion"] = "committed"
    session.commit()

    def import_reference(_session, **kwargs):
        assert kwargs["commit"] is False
        session.state["reference_rows"] = "valid but insufficient"
        return SimpleNamespace(
            inserted_or_updated=1, matched_to_gaia=1, unmatched_to_gaia=0
        )

    _configure_auto_reference(
        monkeypatch,
        tmp_path,
        importer=import_reference,
        coverage_check=lambda *_args, **_kwargs: {},
    )

    result = _ensure_reference(session, tmp_path)

    assert result["status"] == "INSUFFICIENT_REFERENCE_COVERAGE"
    assert result["ready"] is False
    assert session.rollback_count == 0
    assert session.commit_count == 1
    assert session.committed_state == {"gaia_promotion": "committed"}

    assert (
        runner._completed_step_status("ensure_selected_reference_photometry", result)
        == "DONE"
    )
    runner._finish_pipeline_step(session, 99, "DONE", result, None)

    assert session.commit_count == 2
    assert session.committed_state == {
        "gaia_promotion": "committed",
        "reference_rows": "valid but insufficient",
    }
