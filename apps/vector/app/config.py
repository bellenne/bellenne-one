from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_url: str
    app_timezone: str
    wb_base_url: str
    wb_http_timeout_seconds: float
    wb_request_interval_seconds: float
    wb_info_request_interval_seconds: float
    session_https_only: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        database_url = os.getenv(
            "DATABASE_URL",
            f"sqlite:///{(data_dir / 'ads_statistics.db').as_posix()}",
        )
        return cls(
            data_dir=data_dir,
            database_url=database_url,
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            wb_base_url=os.getenv(
                "WB_BASE_URL",
                "https://advert-api.wildberries.ru",
            ).rstrip("/"),
            wb_http_timeout_seconds=float(
                os.getenv("WB_HTTP_TIMEOUT_SECONDS", "30")
            ),
            wb_request_interval_seconds=float(
                os.getenv("WB_REQUEST_INTERVAL_SECONDS", "20.2")
            ),
            wb_info_request_interval_seconds=float(
                os.getenv("WB_INFO_REQUEST_INTERVAL_SECONDS", "0.22")
            ),
            session_https_only=(
                os.getenv("SESSION_HTTPS_ONLY", "false").casefold()
                in {"1", "true", "yes", "on"}
            ),
        )
