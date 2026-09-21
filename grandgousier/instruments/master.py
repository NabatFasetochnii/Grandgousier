from __future__ import annotations

from pathlib import Path

from grandgousier.instruments.base import InstrumentProfile
from grandgousier.resources import instrument_profile_path


DEFAULT_MASTER_PROFILE = instrument_profile_path("master.yaml")


def load_master_profile(config_path: str | Path) -> InstrumentProfile:
    return InstrumentProfile.from_yaml(config_path)
