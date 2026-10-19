"""Encrypting the credentials clinics give us for their own systems (WATI token, PMS key).

The key is derived from DJANGO_SECRET_KEY, so a database copy alone doesn't reveal them. Changing the secret
key makes stored credentials unreadable: they then read as empty and the clinic re-enters them.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"clinic-integrations:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def hint(value: str) -> str:
    """'••••1a2b' — enough to recognise a credential without showing it."""
    return f"••••{value[-4:]}" if len(value) > 4 else ("••••" if value else "")
