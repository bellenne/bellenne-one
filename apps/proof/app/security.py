from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def secret_parts(value: str) -> tuple[str, str]:
    return value[:12], value[-4:]


def masked_secret(prefix: str, last_four: str) -> str:
    return f"{prefix}••••••••{last_four}"


def credential_cipher(secret: str, explicit_key: str | None = None) -> Fernet:
    if explicit_key:
        try:
            return Fernet(explicit_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("CREDENTIALS_ENCRYPTION_KEY must be a valid Fernet key") from exc
    derived = hashlib.sha256(f"bellenneproof:{secret}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(cipher: Fernet, values: dict[str, Any]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return cipher.encrypt(payload).decode("ascii")


def decrypt_secret(cipher: Fernet, encrypted: str | None) -> dict[str, Any]:
    if not encrypted:
        return {}
    try:
        payload = cipher.decrypt(encrypted.encode("ascii"))
        result = json.loads(payload.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return {}


def constant_time_matches(raw: str, digest: str) -> bool:
    return bool(raw and digest and hmac.compare_digest(token_digest(raw), digest))
