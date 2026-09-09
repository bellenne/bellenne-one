from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AppSettings:
    data_dir: Path
    result_dir: Path
    database_url: str
    module_prefix: str
    credentials_secret: str
    credentials_key: str | None
    docs_enabled: bool
    notification_async_enabled: bool
    mattermost_http_timeout_seconds: float
    public_base_url: str

    @property
    def amocrm_redirect_uri(self) -> str:
        return f"{self.public_base_url}{self.module_prefix}/integrations/amocrm/oauth/callback"

    @property
    def amocrm_revoked_uri(self) -> str:
        return f"{self.public_base_url}{self.module_prefix}/integrations/amocrm/oauth/revoked"

    @classmethod
    def from_env(cls) -> "AppSettings":
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        return cls(
            data_dir=data_dir,
            result_dir=Path(os.getenv("PROOF_RESULT_DIR", str(data_dir / "results"))).resolve(),
            database_url=os.getenv(
                "DATABASE_URL",
                f"sqlite:///{(data_dir / 'bellenneproof.db').as_posix()}",
            ),
            module_prefix=os.getenv("MODULE_PREFIX", "").rstrip("/"),
            credentials_secret=os.getenv("APP_SECRET_KEY", "replace-this-secret-before-production"),
            credentials_key=os.getenv("CREDENTIALS_ENCRYPTION_KEY") or None,
            docs_enabled=env_flag("PROOF_API_DOCS_ENABLED"),
            notification_async_enabled=env_flag("PROOF_NOTIFICATION_ASYNC", True),
            mattermost_http_timeout_seconds=float(os.getenv("MATTERMOST_HTTP_TIMEOUT_SECONDS", "10")),
            public_base_url=os.getenv("PROOF_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        )
