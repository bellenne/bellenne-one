from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    module_prefix: str
    encryption_key: str
    http_timeout: float = 30
    retry_attempts: int = 3
    worker_idle: float = 1
    lease_seconds: int = 300

    def __post_init__(self):
        if self.http_timeout <= 0 or not 1 <= self.retry_attempts <= 10 or self.worker_idle <= 0 or self.lease_seconds <= self.http_timeout + 60:
            raise ValueError("Invalid Parity runtime settings: positive timeout/idle, 1..10 attempts, lease > timeout + 60 seconds required")

    @classmethod
    def from_env(cls):
        directory = Path(os.getenv("PARITY_DATA_DIR", "/data"))
        return cls(
            directory, os.getenv("DATABASE_URL", f"sqlite:///{directory / 'parity.db'}"),
            os.getenv("MODULE_PREFIX", "/parity").rstrip("/"),
            os.getenv("PARITY_ENCRYPTION_KEY", ""),
            float(os.getenv("PARITY_HTTP_TIMEOUT_SECONDS", "30")),
            int(os.getenv("PARITY_RETRY_ATTEMPTS", "3")),
            float(os.getenv("PARITY_WORKER_IDLE_SECONDS", "1")),
            int(os.getenv("PARITY_JOB_LEASE_SECONDS", "300")),
        )
