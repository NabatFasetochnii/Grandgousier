from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from grandgousier import cli, logging_config
from grandgousier.logging_config import _PackageDiagnosticSanitizingFilter


def test_config_option_is_accepted_before_or_after_bootstrap_subcommand():
    parser = cli.build_parser()
    before = parser.parse_args(["--config", "a.yaml", "doctor"])
    after = parser.parse_args(["doctor", "--config", "a.yaml"])
    assert before.config_file == "a.yaml"
    assert after.config_file == "a.yaml"


def test_config_example_writes_packaged_template(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
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
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
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


def test_invalid_application_config_fails_before_command(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
    path = tmp_path / "bad.yaml"
    path.write_text("- not-a-mapping\n", encoding="utf-8")
    code = cli.main(["--config", str(path), "config-example", "--out", str(tmp_path / "x.yaml")])
    assert code == 2
    assert "config root must be a mapping" in capsys.readouterr().err


def test_main_sanitizes_parser_and_configuration_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
    parser_secret = "SYNTHETIC_CLI_PARSER_SECRET"
    with pytest.raises(SystemExit) as parser_exit:
        cli.main([f"api_key={parser_secret}"])
    assert parser_exit.value.code == 2
    parser_output = capsys.readouterr()
    assert parser_secret not in parser_output.err
    assert "api_key=***" in parser_output.err
    assert parser_output.out == ""

    config_secret = "SYNTHETIC_CLI_CONFIG_SECRET"

    def fail_config_load(*, config_file=None):
        del config_file
        raise ValueError(f"configuration rejected password={config_secret}")

    monkeypatch.setattr(cli, "get_settings", fail_config_load)
    assert cli.main(["config-example"]) == 2
    config_output = capsys.readouterr()
    assert config_secret not in config_output.err
    assert "configuration rejected password=***" in config_output.err
    assert config_output.out == ""


def test_main_sanitizes_command_system_exit_and_preserves_stdout(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
    command_secret = "SYNTHETIC_CLI_COMMAND_SECRET"

    def fail_command(_args):
        print("table output preserved")
        print(f"command failed token={command_secret}", file=sys.stderr)
        raise SystemExit(f"command failed token={command_secret}")

    monkeypatch.setattr(cli, "cmd_config_example", fail_command)
    with pytest.raises(SystemExit) as command_exit:
        cli.main(["config-example"])

    assert str(command_exit.value) == "command failed token=***"
    output = capsys.readouterr()
    assert output.out == "table output preserved\n"
    assert command_secret not in output.err
    assert "command failed token=***" in output.err


def test_main_sanitizes_unhandled_command_exception_and_returns_failure(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
    url_password = "SYNTHETIC_UNHANDLED_URL_PASSWORD"
    cause_token = "SYNTHETIC_UNHANDLED_CAUSE_TOKEN"
    note_token = "SYNTHETIC_UNHANDLED_NOTE_TOKEN"
    custom_str_token = "SYNTHETIC_UNHANDLED_STR_TOKEN"
    database_url = (
        "postgresql+psycopg://safe_user:"
        f"{url_password}@db.example.test/safe_db"
    )

    class SyntheticCommandError(RuntimeError):
        def __str__(self):
            return f"command failed credential={custom_str_token}"

    def raise_command_error(_args):
        print("ordinary command output")
        cause = ValueError(f"connection failed for {database_url}; token={cause_token}")
        cause.add_note(f"cause note api_key={note_token}")
        error = SyntheticCommandError()
        error.add_note(f"outer note token={note_token}")
        raise error from cause

    monkeypatch.setattr(cli, "cmd_config_example", raise_command_error)
    assert cli.main(["config-example"]) == 1
    output = capsys.readouterr()
    assert output.out == "ordinary command output\n"
    assert url_password not in output.err
    assert cause_token not in output.err
    assert note_token not in output.err
    assert custom_str_token not in output.err
    assert (
        "postgresql+psycopg://safe_user:***@db.example.test/safe_db"
        in output.err
    )
    assert "token=***" in output.err
    assert "api_key=***" in output.err
    assert "credential=***" in output.err
    assert "The above exception was the direct cause" in output.err
    assert "cause note api_key=***" in output.err
    assert "outer note token=***" in output.err


def test_cli_configured_python_logging_redacts_package_messages(
    monkeypatch, capsys
):
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    log_secret = "SYNTHETIC_CLI_LOG_SECRET"

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="WARNING"),
    )

    def log_from_command(_args):
        logging.getLogger("grandgousier.synthetic.cli").warning(
            "diagnostic API-Key=%s", log_secret
        )
        return 0

    monkeypatch.setattr(cli, "cmd_config_example", log_from_command)
    try:
        assert cli.main(["config-example"]) == 0
        assert handler in root_logger.handlers
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)

    output = capsys.readouterr()
    assert log_secret not in output.err
    assert "WARNING grandgousier.synthetic.cli: diagnostic API-Key=***" in output.err
    assert output.out == ""


def test_package_logger_filter_fails_closed_for_malformed_messages(capsys):
    logger = logging.getLogger("grandgousier.synthetic.malformed_filter")
    previous_level = logger.level
    previous_propagate = logger.propagate

    class CapturingStreamHandler(logging.StreamHandler):
        def emit(self, record):
            self.record = record
            super().emit(record)

    log_secret = "SYNTHETIC_MALFORMED_LOG_REPR_TOKEN"

    class MalformedMessage:
        def __str__(self):
            raise ValueError("synthetic malformed message")

        def __repr__(self):
            return f"<MalformedMessage {log_secret}>"

    handler = CapturingStreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_PackageDiagnosticSanitizingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        logger.warning("malformed diagnostic %s", MalformedMessage())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    output = capsys.readouterr()
    assert log_secret not in output.err
    assert "[package log message unavailable]" in output.err
    assert log_secret not in repr((handler.record.msg, handler.record.args))
    assert handler.record.msg == "[package log message unavailable]"
    assert handler.record.args == ()


def test_package_logger_filter_fails_closed_when_exception_formatting_fails(
    monkeypatch, capsys
):
    logger = logging.getLogger("grandgousier.synthetic.malformed_exception")
    previous_level = logger.level
    previous_propagate = logger.propagate

    class CapturingStreamHandler(logging.StreamHandler):
        def emit(self, record):
            self.record = record
            super().emit(record)

    log_secret = "SYNTHETIC_MALFORMED_EXCEPTION_TOKEN"

    def fail_exception_formatting(*_args):
        raise ValueError(f"traceback formatting failed {log_secret}")

    monkeypatch.setattr(
        logging_config.traceback, "format_exception", fail_exception_formatting
    )
    handler = CapturingStreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_PackageDiagnosticSanitizingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        try:
            raise RuntimeError(f"synthetic exception {log_secret}")
        except RuntimeError:
            logger.exception("synthetic operation failed")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    output = capsys.readouterr()
    assert log_secret not in output.err
    assert "[package exception details unavailable]" in output.err
    assert handler.record.exc_text == "[package exception details unavailable]"
    assert log_secret not in repr(
        (handler.record.msg, handler.record.args, handler.record.exc_text)
    )


def test_rms_mag_summary_keeps_conditional_stdout_and_stderr_routing(
    monkeypatch, capsys
):
    from grandgousier.quality import lightcurve_quality

    class SessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )
    monkeypatch.setattr(
        cli, "make_session_factory", lambda _url: lambda: SessionContext()
    )
    monkeypatch.setattr(lightcurve_quality, "load_object_scatter_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(lightcurve_quality, "build_rms_mag_summary", lambda *_a, **_k: [])

    assert cli.main(
        ["rms-mag-summary", "--calibration-name", "synthetic", "--format", "csv"]
    ) == 0
    csv_output = capsys.readouterr()
    assert csv_output.out.startswith("mag_bin_left,mag_bin_right,mag_bin_center,")
    assert csv_output.err == "objects_used=0\n"

    assert cli.main(
        ["rms-mag-summary", "--calibration-name", "synthetic", "--format", "table"]
    ) == 0
    table_output = capsys.readouterr()
    assert table_output.out == "No rows.\nobjects_used=0\n"
    assert table_output.err == ""


def test_acceptance_check_cli_ready(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace
    from grandgousier import cli

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )

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

    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda **_kwargs: SimpleNamespace(log_level="INFO"),
    )

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
