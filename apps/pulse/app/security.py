from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_encoded, hash_encoded = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_encoded.encode())
        expected = base64.urlsafe_b64decode(hash_encoded.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def credential_cipher(secret: str, explicit_key: str | None = None) -> Fernet:
    if explicit_key:
        try:
            return Fernet(explicit_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("CREDENTIALS_ENCRYPTION_KEY must be a valid Fernet key") from exc
    derived = hashlib.sha256(f"bellennepulse:{secret}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_credentials(cipher: Fernet, values: dict[str, Any]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return cipher.encrypt(payload).decode("ascii")


def decrypt_credentials(cipher: Fernet, encrypted: str | None) -> dict[str, Any]:
    if not encrypted:
        return {}
    try:
        payload = cipher.decrypt(encrypted.encode("ascii"))
        result = json.loads(payload.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return {}


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(session_token: str | None, form_token: str | None) -> bool:
    return bool(session_token and form_token and hmac.compare_digest(session_token, form_token))

