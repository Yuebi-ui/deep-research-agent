"""Central memory read/write gates for runtime ablations.

Unlike the feature-specific flags, these gates cover legacy report memory,
stage retrieval, episodic writes and the durable-outbox/fallback paths. Default
on preserves existing deployments; explicit off is required for a true control.
"""
from __future__ import annotations

import os

_FALSE = {"0", "off", "false", "no"}


def _enabled(name: str) -> bool:
    return os.getenv(name, "on").strip().lower() not in _FALSE


def memory_read_enabled() -> bool:
    return _enabled("DR_MEMORY_READ_ENABLED")


def memory_write_enabled() -> bool:
    return _enabled("DR_MEMORY_WRITE_ENABLED")
