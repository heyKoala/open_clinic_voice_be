from __future__ import annotations

from threading import local

_local = local()


def set_current_user(user) -> None:
    _local.user = user


def get_current_user():
    return getattr(_local, "user", None)
