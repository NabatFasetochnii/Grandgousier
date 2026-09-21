from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from grandgousier.config import application_settings_mapping, read_config_file


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://grandgousier:glodga@localhost:5432/grandgousier"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="GRANDGOUSIER_",
        env_file=".env",
        extra="ignore",
    )


def get_settings(config_file: str | Path | None = None) -> Settings:
    """Resolve defaults/environment and optionally overlay a YAML/JSON config.

    Environment handling remains delegated to pydantic-settings.  A supplied
    application config then overrides only explicit application-level values;
    command-line arguments are applied later by the CLI.
    """

    settings = Settings()
    if config_file is None:
        return settings
    payload = read_config_file(config_file)
    overrides = application_settings_mapping(payload)
    return settings.model_copy(update=overrides) if overrides else settings
