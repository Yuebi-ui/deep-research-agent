"""Research-episode memory: observed tool traces, not self-certified skills.

A small, optional SQLite index stores bounded *operational* traces after a task
has successfully completed. It never stores retrieved page bodies, model reasoning,
or inferred user preferences. No LLM call or new infrastructure is needed.

Important: a completed subtask is NOT proof that its queries were effective.
All memories are presented as untrusted historical hints, never instructions.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from deep_research.memory.retrieval import lexical_score

_MAX_EPISODES_PER_TASK = 24
_MAX_QUERY_ITEMS = 12
_MAX_DOMAINS = 15


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    # Best-effort removal of common credentials in tool queries. Not a substitute
    # for tenant-scoped retention policies or an upstream PII/sensitive-data filter.
    cleaned = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+",
                     r"\1=[REDACTED]", cleaned)
    cleaned = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._-]{12,}", "Bearer [REDACTED]", cleaned)
    cleaned = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED_KEY]", cleaned)
    return cleaned[:limit]


def _unique_text(values: object, limit: int, length: int) -> list[str]:
    if not isinstance(values, (tuple, list)):
        return []
    found: list[str] = []
    for value in values[:limit * 3]:
        item = _clean_text(value, length)
        if item and item not in found:
            found.append(item)
        if len(found) >= limit:
            break
    return found


def _domains(urls: object) -> list[str]:
    """URLs are never kept, only bounded public hostname clues."""
    output = []
    for url in urls[:40] if isinstance(urls, (tuple, list)) else []:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            # Exclude local addresses and URL user-info; do not trust arbitrary schemes.
            if parsed.scheme not in ("http", "https") or not host or len(host) > 160:
                continue
            if host == "localhost" or host.endswith((".local", ".internal")) or "." not in host:
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                pass
        except ValueError:
            continue
        if host not in output:
            output.append(host)
        if len(output) >= _MAX_DOMAINS:
            break
    return output


def extract_research_trace(state: dict, compressed: str) -> dict:
    """Reconstruct only OBSERVABLE research activity from existing graph messages."""
    queries: list[str] = []
    search_calls = 0
    tool_errors = 0
    for message in list(state.get("researcher_messages") or [])[-80:]:
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict) or call.get("name") != "tavily_search":
                continue
            search_calls += 1
            args = call.get("args") or {}
            query = _clean_text(args.get("query", "") if isinstance(args, dict) else "", 240)
            if query and query not in queries and len(queries) < _MAX_QUERY_ITEMS:
                queries.append(query)
        if getattr(message, "name", None) == "tavily_search" and str(getattr(message, "content", "")).startswith(
            "[SEARCH_ERROR]"
        ):
            tool_errors += 1
    return {
        "topic": _clean_text(state.get("research_topic"), 400),
        "queries": queries,
        "domains": _domains(state.get("seen_urls", [])),
        "search_calls": min(100, search_calls),
        "tool_errors": min(100, tool_errors),
        # Only means a compressed response was emitted, not that it was accurate.
        "findings_emitted": bool(isinstance(compressed, str) and compressed.strip()),
    }


@dataclass(frozen=True)
class ResearchEpisode:
    episode_id: str
    task_id: str
    generation: int
    topic: str
    queries: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    search_calls: int = 0
    tool_errors: int = 0
    findings_emitted: bool = False
    completed_at: float = field(default_factory=time.time)


def build_completed_episodes(task_id: str, traces: object, generation: int) -> list[ResearchEpisode]:
    """Only persist the accepted lineage, never rejected speculative research."""
    task_id = _clean_text(task_id, 160)
    if not task_id or not isinstance(traces, list):
        return []
    episodes: list[ResearchEpisode] = []
    for ordinal, item in enumerate(traces[:_MAX_EPISODES_PER_TASK * 4]):
        if not isinstance(item, dict) or item.get("generation") != generation:
            continue
        topic = _clean_text(item.get("topic"), 400)
        if not topic:
            continue
        key = f"{task_id}\x00{generation}\x00{ordinal}\x00{topic}"
        episodes.append(ResearchEpisode(
            episode_id="episode-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:28],
            task_id=task_id, generation=generation, topic=topic,
            queries=_unique_text(item.get("queries"), _MAX_QUERY_ITEMS, 240),
            domains=_unique_text(item.get("domains"), _MAX_DOMAINS, 160),
            search_calls=max(0, min(100, int(item.get("search_calls") or 0))),
            tool_errors=max(0, min(100, int(item.get("tool_errors") or 0))),
            findings_emitted=bool(item.get("findings_emitted", False)),
        ))
        if len(episodes) >= _MAX_EPISODES_PER_TASK:
            break
    return episodes


class EpisodeMemoryStore:
    """Disk-backed, idempotent, bounded lexical episode lookup (SQLite WAL)."""

    def __init__(self, database: str | Path):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS research_episodes (
                episode_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                topic TEXT NOT NULL,
                queries TEXT NOT NULL,
                domains TEXT NOT NULL,
                search_calls INTEGER NOT NULL,
                tool_errors INTEGER NOT NULL,
                findings_emitted INTEGER NOT NULL,
                completed_at REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_time ON research_episodes(completed_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_task ON research_episodes(task_id)")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            with conn:
                yield conn
        finally:
            conn.close()

    def upsert(self, episodes: list[ResearchEpisode]) -> int:
        if not episodes:
            return 0
        with self._connection() as conn:
            conn.executemany("""INSERT INTO research_episodes
                (episode_id, task_id, generation, topic, queries, domains,
                 search_calls, tool_errors, findings_emitted, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    queries=excluded.queries, domains=excluded.domains,
                    search_calls=excluded.search_calls, tool_errors=excluded.tool_errors,
                    findings_emitted=excluded.findings_emitted
            """, [(
                x.episode_id, x.task_id, x.generation, x.topic,
                json.dumps(x.queries, ensure_ascii=False),
                json.dumps(x.domains, ensure_ascii=False),
                x.search_calls, x.tool_errors, int(x.findings_emitted), x.completed_at,
            ) for x in episodes])
        return len(episodes)

    def count(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("SELECT count(*) FROM research_episodes").fetchone()[0])

    def delete_task(self, task_id: str) -> int:
        """Explicit task-level forgetting hook; does not delete other memory stores."""
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM research_episodes WHERE task_id=?", (task_id,))
            return cur.rowcount

    def search(self, query: str, top_k: int = 3, *, min_score: float = 0.18) -> list[ResearchEpisode]:
        if not isinstance(query, str) or not query.strip() or top_k <= 0:
            return []
        with self._connection() as conn:
            rows = conn.execute("""SELECT episode_id, task_id, generation, topic, queries,
                       domains, search_calls, tool_errors, findings_emitted, completed_at
                       FROM research_episodes ORDER BY completed_at DESC LIMIT 400""").fetchall()
        results: list[tuple[float, ResearchEpisode]] = []
        for r in rows:
            try:
                queries = json.loads(r[4]); domains = json.loads(r[5])
                if not isinstance(queries, list) or not isinstance(domains, list):
                    continue
                episode = ResearchEpisode(
                    episode_id=r[0], task_id=r[1], generation=r[2], topic=r[3],
                    queries=_unique_text(queries, _MAX_QUERY_ITEMS, 240),
                    domains=_unique_text(domains, _MAX_DOMAINS, 160),
                    search_calls=r[6], tool_errors=r[7],
                    findings_emitted=bool(r[8]), completed_at=r[9],
                )
            except (TypeError, ValueError, IndexError):
                continue
            similarity = max(lexical_score(query, episode.topic),
                             0.75 * max((lexical_score(query, q) for q in episode.queries), default=0.0))
            if similarity >= min_score:
                results.append((similarity, episode))
        # Relevance first; no claim of verified quality or 1st-party authority.
        results.sort(key=lambda item: (-item[0], -item[1].completed_at, item[1].episode_id))
        return [episode for _, episode in results[:min(top_k, 8)]]
