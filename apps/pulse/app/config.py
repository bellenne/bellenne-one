from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "BellennePulse"
    app_host: str = "0.0.0.0"
    app_port: int = 17863
    app_secret_key: str = Field(default="development-only-change-me")
    credentials_encryption_key: str | None = None
    database_url: str = "sqlite:///./data/bellennepulse.db"
    timezone: str = "Europe/Moscow"
    secure_cookies: bool = False
    scheduler_enabled: bool = True
    demo_mode: bool = True
    seed_demo_data: bool = True
    request_timeout_seconds: float = 45.0
    source_xlsx_path: str | None = None
    log_level: str = "INFO"

    @property
    def data_dir(self) -> Path:
        if self.database_url.startswith("sqlite:///./"):
            return Path(self.database_url.removeprefix("sqlite:///./")).parent
        return Path("data")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
