from __future__ import annotations

import hashlib
import secrets


TOKEN_PREFIX = "bn_nest_"


def generate_api_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_parts(token: str) -> tuple[str, str]:
    return token[:12], token[-4:]


def masked_token(prefix: str, last_four: str) -> str:
    return f"{prefix}••••••••••••{last_four}"
