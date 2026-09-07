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
    database_url: str
    module_prefix: str
    docs_enabled: bool

    @classmethod
    def from_env(cls) -> "AppSettings":
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        return cls(
            data_dir=data_dir,
            database_url=os.getenv(
                "DATABASE_URL",
                f"sqlite:///{(data_dir / 'bellennenest.db').as_posix()}",
            ),
            module_prefix=os.getenv("MODULE_PREFIX", "").rstrip("/"),
            docs_enabled=env_flag("NEST_API_DOCS_ENABLED"),
        )
