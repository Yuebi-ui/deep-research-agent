"""Authenticate callers and derive a server-trusted owner for task and Memory access.

Standalone mode has one local owner. Multi-user mode is enabled only by setting
DR_API_USERS_JSON to {"account-id": "long-random-bearer-token", ...}. Never
trust a user_id supplied by the client or proxy headers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

from fastapi import Header, HTTPException
from deep_research.memory.tenant import LOCAL_USER, validate_user_id


def api_users() -> dict[str, str]:
    raw = os.getenv("DR_API_USERS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("DR_API_USERS_JSON must contain a JSON object") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("DR_API_USERS_JSON must map account IDs to tokens")
    tokens: set[str] = set()
    for account, secret in parsed.items():
        validate_user_id(account)
        if not isinstance(secret, str) or len(secret) < 32 or secret in tokens:
            raise RuntimeError("each account must have a distinct random token of at least 32 characters")
        tokens.add(secret)
    return parsed


def validate_auth_config() -> None:
    api_users()


async def authenticated_user(authorization: str | None = Header(default=None)) -> str:
    users = api_users()
    if not users:
        # Standalone mode is deliberately single-user; never allow a header to
        # select an arbitrary owner in this mode.
        return LOCAL_USER
    scheme, sep, supplied = (authorization or "").partition(" ")
    if not sep or scheme.lower() != "bearer" or not supplied:
        raise HTTPException(status_code=401, detail="Bearer authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    supplied_hash = hashlib.sha256(supplied.encode()).digest()
    matched = None
    for account, secret in users.items():
        expected = hashlib.sha256(secret.encode()).digest()
        if hmac.compare_digest(supplied_hash, expected):
            matched = account
    if matched is None:
        raise HTTPException(status_code=401, detail="Invalid access token",
                            headers={"WWW-Authenticate": "Bearer"})
    return matched
