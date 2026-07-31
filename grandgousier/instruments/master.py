from __future__ import annotations

from pathlib import Path

from grandgousier.instruments.base import InstrumentProfile


DEFAULT_MASTER_PROFILE = Path(__file__).resolve().parents[2] / "configs" / "master.yaml"


def load_master_profile(config_path: str | Path) -> InstrumentProfile:
    return InstrumentProfile.from_yaml(config_path)
