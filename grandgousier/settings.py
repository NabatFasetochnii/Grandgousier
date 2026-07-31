from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://grandgousier:glodga@localhost:5432/grandgousier"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="GRANDGOUSIER_",
        env_file=".env",
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()
