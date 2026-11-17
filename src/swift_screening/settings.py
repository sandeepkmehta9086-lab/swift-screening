from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SWIFT_SCREENING_", env_file=".env", extra="ignore"
    )

    config: str = "config/tenants.example.yaml"
    database_url: str = "sqlite:///./data/screening.db"
    list_store: str = "./data/lists"
    log_json: bool = True
