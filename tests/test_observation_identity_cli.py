from __future__ import annotations

import pytest

pytest.importorskip("astropy")

from grandgousier.cli import build_parser


def test_cli_observation_backfill_is_plan_only_by_default() -> None:
    args = build_parser().parse_args(["backfill-observation-identity", "--instrument", "MASTER"])
    assert args.instrument == ["MASTER"]
    assert args.execute is False
