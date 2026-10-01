from __future__ import annotations

import hashlib
import secrets

TOKEN_BYTES = 32


def generate_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
