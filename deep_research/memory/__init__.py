"""Lazy memory exports.

Keep pure Memory 3.0 utilities (chunking, ranking, dry-run tooling) importable
without initializing Chroma/LLM clients or changing existing public imports.
"""

from importlib import import_module

_EXPORTS = {
    "VectorMemoryStore": "deep_research.memory.vector_store",
    "MemoryManager": "deep_research.memory.manager",
    "Entity": "deep_research.memory.schemas",
    "MemoryClaim": "deep_research.memory.schemas",
    "Evidence": "deep_research.memory.schemas",
    "Contradiction": "deep_research.memory.schemas",
    "StructuredMemoryStore": "deep_research.memory.structured_store",
    "get_memory_manager": "deep_research.memory.runtime",
    "get_episode_store": "deep_research.memory.runtime",
    "reset_memory_runtime": "deep_research.memory.runtime",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(_EXPORTS[name]), name)
