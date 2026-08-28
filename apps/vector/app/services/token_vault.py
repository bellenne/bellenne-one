from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class TokenVaultError(RuntimeError):
    pass


class TokenVault:
    def __init__(self, data_dir: Path) -> None:
        self._key_path = data_dir / ".token_key"
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()

        key = Fernet.generate_key()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(self._key_path, flags, 0o600)
        try:
            os.write(descriptor, key)
        finally:
            os.close(descriptor)
        return key

    def encrypt(self, token: str) -> str:
        clean_token = token.strip()
        if not clean_token:
            raise TokenVaultError("API-ключ не может быть пустым")
        return self._fernet.encrypt(clean_token.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_token: str | None) -> str:
        if not encrypted_token:
            raise TokenVaultError("API-ключ ещё не сохранён")
        try:
            return self._fernet.decrypt(
                encrypted_token.encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise TokenVaultError(
                "Не удалось расшифровать API-ключ. "
                "Проверьте, что Docker volume с файлом ключа не был заменён."
            ) from exc

    @staticmethod
    def hint(token: str) -> str:
        clean_token = token.strip()
        if len(clean_token) <= 10:
            return "••••••"
        return f"{clean_token[:4]}••••{clean_token[-4:]}"

