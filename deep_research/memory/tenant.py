"""Trusted tenant identity and request-scoped isolation for every Memory access."""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

LOCAL_USER = "local"
_user: ContextVar[str | None] = ContextVar("memory_owner", default=None)
_VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")


def validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not _VALID.fullmatch(user_id):
        raise ValueError("invalid trusted user identifier")
    return user_id


def current_user_id() -> str:
    value = _user.get()
    if value is not None:
        return value
    # A missing context is an error in multi-user mode; never leak into a
    # shared or default memory. Standalone/offline execution uses local only.
    if os.getenv("DR_API_USERS_JSON", "").strip():
        raise RuntimeError("Memory tenant context required when multi-user mode is enabled")
    return LOCAL_USER


@contextmanager
def memory_user_scope(user_id: str) -> Iterator[None]:
    token = _user.set(validate_user_id(user_id))
    try:
        yield
    finally:
        _user.reset(token)


def tenant_memory_dir(root: str | Path, user_id: str | None = None) -> Path:
    owner = validate_user_id(user_id) if user_id is not None else current_user_id()
    # SHA-256 path derivation is deterministic and cannot traverse directories;
    # no raw account names are persisted in directory names.
    return Path(root) / "tenants" / hashlib.sha256(owner.encode("utf-8")).hexdigest()


def resolve_cli_account(account: str | None) -> str:
    """Explicit owner selection for trusted local maintenance commands.

    A multi-user maintenance script must never default to an arbitrary tenant
    or silently process all users' reports into one memory store.
    """
    from backend.core.auth import api_users

    allowed = api_users()
    if allowed:
        if account not in allowed:
            raise ValueError("multi-user maintenance requires --user with a configured account")
        return validate_user_id(account)
    if account not in (None, LOCAL_USER):
        raise ValueError("standalone mode accepts only --user local")
    return LOCAL_USER
