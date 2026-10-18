"""Process-local composition root for the memory subsystem.

The memory package is used by graph nodes, the durable outbox worker and stage-aware
retrieval. Keeping construction here prevents higher-level modules from importing
``agent_builder`` just to reach a singleton, which keeps dependency direction clear.

Imports are deliberately lazy: importing a lightweight retrieval helper or CLI must
not initialize Chroma/LangChain/model clients. Cross-process write coordination is
handled by the durable outbox lease/fencing layer, not by this module.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from deep_research.settings import get_engine_settings

if TYPE_CHECKING:
    from deep_research.memory.episodes import EpisodeMemoryStore
    from deep_research.memory.manager import MemoryManager

_manager_lock = threading.Lock()
_managers: dict[tuple[str, str], Any] = {}
_episodes: dict[tuple[str, str], Any] = {}


def get_memory_manager() -> "MemoryManager":
    """Return a cached manager for the trusted, task-bound user only."""
    from deep_research.memory.tenant import current_user_id, tenant_memory_dir
    owner = current_user_id()
    root = get_engine_settings().resolved_memory_data_dir.resolve()
    key = (str(root), owner)
    with _manager_lock:
        if key not in _managers:
            from deep_research.memory.manager import MemoryManager
            data_dir = tenant_memory_dir(root, owner)
            _managers[key] = MemoryManager(persist_dir=str(data_dir / "chroma"))
        return _managers[key]


def get_episode_store() -> "EpisodeMemoryStore":
    """Episode SQLite lives in the same per-owner directory as Chroma."""
    from deep_research.memory.tenant import current_user_id, tenant_memory_dir
    owner = current_user_id()
    root = get_engine_settings().resolved_memory_data_dir.resolve()
    key = (str(root), owner)
    with _manager_lock:
        if key not in _episodes:
            from deep_research.memory.episodes import EpisodeMemoryStore
            path = tenant_memory_dir(root, owner) / "research_episodes.sqlite3"
            _episodes[key] = EpisodeMemoryStore(path)
        return _episodes[key]

def reset_memory_runtime() -> None:
    """Clear process-local clients; persisted per-owner files remain untouched."""
    with _manager_lock:
        _managers.clear()
        _episodes.clear()
