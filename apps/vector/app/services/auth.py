from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

from starlette.responses import Response

SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
USERNAME_PATTERN = re.compile(r"[\w.@+-]{3,64}", re.UNICODE)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class AuthManager:
    cookie_name = "wb_ads_session"

    def __init__(
        self,
        data_dir: Path,
        *,
        secure_cookie: bool = False,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secure_cookie = secure_cookie
        self._secret = self._load_or_create_secret()

    def _load_or_create_secret(self) -> bytes:
        path = self.data_dir / ".session_key"
        if path.exists():
            return _decode(path.read_text(encoding="ascii").strip())

        secret = secrets.token_bytes(32)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(_encode(secret), encoding="ascii")
        os.replace(temporary, path)
        return secret

    @staticmethod
    def normalize_username(username: str) -> str:
        return unicodedata.normalize("NFKC", username).strip().casefold()

    @classmethod
    def username_is_valid(cls, username: str) -> bool:
        return bool(USERNAME_PATTERN.fullmatch(cls.normalize_username(username)))

    @staticmethod
    def password_is_valid(password: str) -> bool:
        return 8 <= len(password) <= 128

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=64,
        )
        return (
            f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
            f"{_encode(salt)}${_encode(digest)}"
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, n, r, p, salt, expected = encoded.split("$", 5)
            if algorithm != "scrypt":
                return False
            parameters = (int(n), int(r), int(p))
            if parameters != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
                return False
            actual = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_decode(salt),
                n=parameters[0],
                r=parameters[1],
                p=parameters[2],
                maxmem=SCRYPT_MAXMEM,
                dklen=64,
            )
            return hmac.compare_digest(actual, _decode(expected))
        except (TypeError, ValueError):
            return False

    def issue_session(self, user_id: int) -> str:
        payload = f"{user_id}:{int(time.time())}".encode("ascii")
        signature = hmac.new(
            self._secret,
            payload,
            hashlib.sha256,
        ).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def verify_session(self, token: str | None) -> int | None:
        if not token:
            return None
        try:
            payload_encoded, signature_encoded = token.split(".", 1)
            payload = _decode(payload_encoded)
            signature = _decode(signature_encoded)
            expected = hmac.new(
                self._secret,
                payload,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            raw_user_id, raw_issued_at = payload.decode("ascii").split(":", 1)
            user_id = int(raw_user_id)
            issued_at = int(raw_issued_at)
            age = int(time.time()) - issued_at
            if user_id <= 0 or age < -60 or age > SESSION_MAX_AGE_SECONDS:
                return None
            return user_id
        except (UnicodeDecodeError, ValueError):
            return None

    def set_session_cookie(self, response: Response, user_id: int) -> None:
        response.set_cookie(
            key=self.cookie_name,
            value=self.issue_session(user_id),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
            path="/",
        )
