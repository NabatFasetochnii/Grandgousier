from __future__ import annotations


def test_legacy_science_storage_commands_are_not_public_07_cli() -> None:
    import argparse
    import pytest

    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    retired = {
        "materialize-science-calibration",
        "verify-science-materialization",
        "backfill-science-photometry-cache",
        "backfill-science-flag-overrides",
        "verify-science-flag-overrides",
        "retire-science-measurements",
    }
    assert retired.isdisjoint(subparsers.choices)


def test_clean_pipeline_cli_has_no_full_derived_materialization_switches() -> None:
    import argparse
    import pytest

    pytest.importorskip("astropy")
    from grandgousier.cli import build_parser

    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    for command in ("run-field-pipeline", "process-archive-path"):
        option_strings = {
            option
            for action in subparsers.choices[command]._actions
            for option in action.option_strings
        }
        assert "--materialize-intermediate-calibrations" not in option_strings
        assert "--diagnose-science-materialization" not in option_strings
