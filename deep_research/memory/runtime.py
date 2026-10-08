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
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from deep_research.settings import get_engine_settings

if TYPE_CHECKING:
    from deep_research.memory.episodes import EpisodeMemoryStore
    from deep_research.memory.manager import MemoryManager

_manager: Any = None
_manager_lock = threading.Lock()


def get_memory_manager() -> "MemoryManager":
    """Return the shared process-local :class:`MemoryManager` instance."""
    global _manager
    if _manager is None:
        # Heavy import stays behind first use so offline utilities can import the
        # memory package without installing/initializing the full LLM stack.
        from deep_research.memory.manager import MemoryManager

        # Supervisor/Researcher lookups may race on first use. Constructing one
        # shared client avoids duplicate Chroma/embedding initialization.
        with _manager_lock:
            if _manager is None:
                _manager = MemoryManager(
                    persist_dir=str(get_engine_settings().resolved_data_dir / "chroma")
                )
    return _manager


@lru_cache(maxsize=1)
def get_episode_store() -> "EpisodeMemoryStore":
    """Return the process-local episodic-memory store for the configured data dir."""
    from deep_research.memory.episodes import EpisodeMemoryStore

    path = get_engine_settings().resolved_data_dir / "research_episodes.sqlite3"
    return EpisodeMemoryStore(path)


def reset_memory_runtime() -> None:
    """Reset process-local clients without deleting persisted data."""
    global _manager
    with _manager_lock:
        _manager = None
    get_episode_store.cache_clear()
