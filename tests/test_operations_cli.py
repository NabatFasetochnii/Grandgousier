from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from grandgousier import cli


def test_config_option_is_accepted_before_or_after_bootstrap_subcommand():
    parser = cli.build_parser()
    before = parser.parse_args(["--config", "a.yaml", "doctor"])
    after = parser.parse_args(["doctor", "--config", "a.yaml"])
    assert before.config_file == "a.yaml"
    assert after.config_file == "a.yaml"


def test_config_example_writes_packaged_template(tmp_path: Path):
    out = tmp_path / "grandgousier.yaml"
    code = cli.main(["config-example", "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "field_archive:" in text
    assert "stage2_recipe: stage3_scale_v1" in text
    assert "archives:" in text
    assert "products:" in text



def test_stage1_plan_parser_accepts_source_query_and_outputs():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "stage1-plan",
            "--config",
            "grandgousier.yaml",
            "--source",
            "robophot-local",
            "--filter",
            "i",
            "--ra",
            "327.6",
            "--dec",
            "72.1",
            "--radius-deg",
            "0.8",
            "--out",
            "reports/stage1.json",
        ]
    )
    assert args.config_file == "grandgousier.yaml"
    assert args.source == "robophot-local"
    assert args.stage1_filters == ["i"]
    assert args.radius_deg == 0.8
    assert args.out == "reports/stage1.json"

def test_doctor_cli_formats_result_and_returns_success(monkeypatch, capsys):
    result = SimpleNamespace(
        status="READY_WITH_WARNINGS",
        ready=True,
        checks=(SimpleNamespace(to_dict=lambda: {"name": "optional", "status": "WARN", "detail": "x"}),),
        to_dict=lambda: {"status": "READY_WITH_WARNINGS", "ready": True},
    )
    monkeypatch.setattr("grandgousier.services.operations.run_doctor", lambda **kwargs: result)
    code = cli.main(["doctor", "--format", "json"])
    assert code == 0
    assert "READY_WITH_WARNINGS" in capsys.readouterr().out


def test_invalid_application_config_fails_before_command(tmp_path: Path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("- not-a-mapping\n", encoding="utf-8")
    code = cli.main(["--config", str(path), "config-example", "--out", str(tmp_path / "x.yaml")])
    assert code == 2
    assert "config root must be a mapping" in capsys.readouterr().err


def test_acceptance_check_cli_ready(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace
    from grandgousier import cli

    checks = (SimpleNamespace(name="stage2_readiness", status="OK", detail="match", to_dict=lambda: {"name": "stage2_readiness", "status": "OK", "detail": "match"}),)
    result = SimpleNamespace(
        status="READY",
        ready=True,
        dataset="gpx_tf17_2",
        checks=checks,
        to_dict=lambda: {"status": "READY", "ready": True, "dataset": "gpx_tf17_2", "checks": []},
    )
    import grandgousier.services.acceptance as acceptance
    monkeypatch.setattr(acceptance, "run_acceptance", lambda **kwargs: result)
    report = tmp_path / "acceptance.json"
    code = cli.main(["acceptance-check", "--report-out", str(report)])
    assert code == 0
    assert "Grandgousier acceptance: READY" in capsys.readouterr().out
    assert report.is_file()


def test_acceptance_check_cli_failed_returns_processing(monkeypatch, capsys):
    from types import SimpleNamespace
    from grandgousier import cli

    checks = (SimpleNamespace(name="stage3_V_unit7", status="FAIL", detail="drift", to_dict=lambda: {"name": "stage3_V_unit7", "status": "FAIL", "detail": "drift"}),)
    result = SimpleNamespace(
        status="FAILED",
        ready=False,
        dataset="gpx_tf17_2",
        checks=checks,
        to_dict=lambda: {"status": "FAILED", "ready": False, "dataset": "gpx_tf17_2", "checks": []},
    )
    import grandgousier.services.acceptance as acceptance
    monkeypatch.setattr(acceptance, "run_acceptance", lambda **kwargs: result)
    code = cli.main(["acceptance-check"])
    assert code == 6
    assert "Grandgousier acceptance: FAILED" in capsys.readouterr().out
