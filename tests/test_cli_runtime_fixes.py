from __future__ import annotations

from types import SimpleNamespace

import pytest

from grandgousier import cli


class _EmptyRows:
    def all(self):
        return []


class _SessionContext:
    def __init__(self, session=None):
        self.session = session or self

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return _EmptyRows()


@pytest.mark.parametrize(
    "command",
    ("show-frames", "frame-summary", "frame-qc-summary"),
)
def test_frame_diagnostic_cli_handlers_accept_parser_namespace_and_empty_rows(
    monkeypatch, command
):
    parser = cli.build_parser()
    args = parser.parse_args([command, "--format", "json"])
    assert not hasattr(args, "calibration_name")
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _url: lambda: _SessionContext(),
    )

    assert args.func(args) == 0


def test_series_finalization_cli_calls_only_the_flush_helper(monkeypatch, capsys):
    from grandgousier.archive import fields_series
    from grandgousier.pipeline import runner

    parser = cli.build_parser()
    args = parser.parse_args(["finalize-field-series", "--series-id", "42"])
    observed = []

    def fake_finalize(session, series_id):
        observed.append((session, series_id))
        return {
            "series_id": series_id,
            "status": "READY",
            "finalized": True,
            "eligible": True,
        }

    monkeypatch.setattr(fields_series, "finalize_open_series", fake_finalize)
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _url: lambda: _SessionContext(session=object()),
    )
    monkeypatch.setattr(
        runner,
        "run_field_pipeline",
        lambda *_args, **_kwargs: pytest.fail("flush CLI must not invoke Stage 2"),
    )

    assert args.func(args) == 0
    assert observed and observed[0][1] == 42
    assert '"status": "READY"' in capsys.readouterr().out


def test_streaming_tail_flags_default_to_legacy_finalization_and_can_be_disabled():
    parser = cli.build_parser()

    assert parser.parse_args(["build-field-series"]).finalize_tail is True
    assert parser.parse_args(
        ["build-field-series", "--no-finalize-tail"]
    ).finalize_tail is False
    assert parser.parse_args(
        ["process-archive-path", "--path", "synthetic"]
    ).finalize_tail is True
    assert parser.parse_args(
        ["process-archive-path", "--path", "synthetic", "--no-finalize-tail"]
    ).finalize_tail is False


def test_build_field_series_cli_forwards_disabled_tail_finalization(monkeypatch):
    from grandgousier.archive import fields_series

    parser = cli.build_parser()
    args = parser.parse_args(
        ["build-field-series", "--no-finalize-tail", "--format", "json"]
    )
    captured = {}
    result = SimpleNamespace(
        summary=dict,
        observation_selection_rows=[],
        rows=[],
    )

    def fake_build(session, **kwargs):
        captured.update(kwargs)
        assert session is not None
        return result

    monkeypatch.setattr(fields_series, "build_field_series", fake_build)
    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _url: lambda: _SessionContext(session=object()),
    )

    assert cli.cmd_build_field_series(args) == 0
    assert captured["finalize_tail"] is False


def test_interseries_cli_recipe_contract_and_analyze_forwarding(monkeypatch, tmp_path):
    parser = cli.build_parser()
    base_persist_args = ["persist-interseries-calibration", "--field-id", "33"]
    with pytest.raises(SystemExit):
        parser.parse_args(base_persist_args)

    persist_args = parser.parse_args(
        [*base_persist_args, "--required-stage2-recipe", "stage3_scale_v1"]
    )
    assert persist_args.required_stage2_recipe == "stage3_scale_v1"

    analyze_args = parser.parse_args(
        ["analyze-interseries-calibration", "--field-id", "33", "--format", "json"]
    )
    assert analyze_args.required_stage2_recipe is None

    analyze_args = parser.parse_args(
        [
            "analyze-interseries-calibration",
            "--field-id",
            "33",
            "--required-stage2-recipe",
            "stage3_scale_v1",
            "--science-calibration-version-id",
            "101",
            "--science-calibration-version-id",
            "102",
            "--format",
            "json",
        ]
    )
    captured = {}
    analysis_result = SimpleNamespace(to_dict=lambda: {"status": "READY_M1_M3"})

    def fake_analyze(session, **kwargs):
        captured.update(kwargs)
        return analysis_result

    def fake_export(analysis, out_root):
        assert analysis is analysis_result
        assert out_root == analyze_args.out_root
        return {"summary": tmp_path / "summary.json"}

    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _url: lambda: _SessionContext(),
    )
    monkeypatch.setattr(
        "grandgousier.interseries.analysis.analyze_interseries_calibration",
        fake_analyze,
    )
    monkeypatch.setattr(
        "grandgousier.interseries.analysis.export_interseries_analysis",
        fake_export,
    )

    assert cli.cmd_analyze_interseries_calibration(analyze_args) == 0
    assert captured["required_stage2_recipe"] == "stage3_scale_v1"
    assert captured["science_calibration_version_ids"] == [101, 102]


def test_plan_interseries_cli_forwards_explicit_science_heads(monkeypatch, capsys):
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "plan-interseries-calibration",
            "--field-id",
            "33",
            "--science-calibration-version-id",
            "101",
            "--science-calibration-version-id",
            "102",
            "--format",
            "json",
        ]
    )
    captured = {}
    plan = SimpleNamespace(to_dict=lambda: {"status": "NEED_MORE_READY_SERIES"})

    def fake_plan(session, **kwargs):
        captured.update(kwargs)
        return plan

    monkeypatch.setattr(
        cli,
        "make_session_factory",
        lambda _url: lambda: _SessionContext(),
    )
    monkeypatch.setattr(
        "grandgousier.interseries.planning.build_interseries_plan",
        fake_plan,
    )

    assert cli.cmd_plan_interseries_calibration(args) == 0
    assert captured["science_calibration_version_ids"] == [101, 102]
    assert '"status": "NEED_MORE_READY_SERIES"' in capsys.readouterr().out


def test_persist_interseries_cli_forwards_required_recipe(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "persist-interseries-calibration",
            "--field-id",
            "33",
            "--required-stage2-recipe",
            "stage3_scale_v1",
            "--science-calibration-version-id",
            "101",
            "--science-calibration-version-id",
            "102",
            "--format",
            "json",
        ]
    )
    captured = {}
    persisted = SimpleNamespace(to_dict=lambda: {"generation_id": 1})

    def fake_persist(session, **kwargs):
        captured.update(kwargs)
        return persisted

    class SessionFactory:
        @staticmethod
        def begin():
            return _SessionContext(session=object())

    monkeypatch.setattr(cli, "make_session_factory", lambda _url: SessionFactory)
    monkeypatch.setattr(
        "grandgousier.interseries.persistence.persist_interseries_calibration",
        fake_persist,
    )

    assert cli.cmd_persist_interseries_calibration(args) == 0
    assert captured["required_stage2_recipe"] == "stage3_scale_v1"
    assert captured["science_calibration_version_ids"] == [101, 102]
