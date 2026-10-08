"""Bounded lexical candidates and deterministic, embedding-distance-free rank fusion.

Vector and keyword results are represented by existing Chroma document records.
No external reranker, new embedding model, or additional network call is needed.
"""

from __future__ import annotations

import re
from datetime import date
from collections import defaultdict


def _grams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.casefold())
    return {compact[i:i + 2] for i in range(len(compact) - 1)} or ({compact} if compact else set())


def lexical_score(query: str, text: str) -> float:
    a, b = _grams(query), _grams(text[:4000])
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def search_terms(query: str, max_terms: int = 6) -> list[str]:
    """Generate Chinese-friendly substring probes, without an external tokenizer."""
    tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z][\w.-]+|\d{4}(?:[-/]\d{1,2})?", query)
    ranked: list[str] = []
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if 2 <= len(token) <= 6:
                ranked.append(token)
            if len(token) > 2:
                ranked.extend(token[i:i + 3] for i in range(len(token) - 2))
            if len(token) == 2:
                ranked.append(token)
        elif len(token) > 1:
            ranked.append(token)
    return list(dict.fromkeys(ranked))[:max_terms]


def fuse_records(
    query: str, channels: list[list[dict]], *, limit: int,
    per_report: int = 2,
) -> list[dict]:
    """Reciprocal-rank fusion + lexical evidence, stable and bounded by report."""
    if limit <= 0:
        return []
    scores: dict[str, float] = defaultdict(float)
    entries: dict[str, dict] = {}
    for weight, channel in zip((1.0, 1.15, 1.0, 0.8), channels):
        for rank, item in enumerate(channel):
            rid = str(item.get("id", ""))
            if not rid:
                continue
            entries[rid] = item
            scores[rid] += weight / (25 + rank)
    for rid, item in entries.items():
        scores[rid] += 0.02 * lexical_score(query, item.get("content", ""))
    output: list[dict] = []
    per_parent: dict[str, int] = defaultdict(int)
    for rid in sorted(entries, key=lambda key: (-scores[key], key)):
        item = entries[rid]
        parent = item.get("metadata", {}).get("report_id") or rid
        if per_parent[parent] >= per_report:
            continue
        per_parent[parent] += 1
        output.append(item)
        if len(output) >= limit:
            break
    return output


def numeric_signature(text: str) -> tuple[str, tuple[str, ...]]:
    """Strong candidate signal only: exact same wording after replacing numbers."""
    normalized = re.sub(r"\s+", "", text.casefold())
    numbers = tuple(re.findall(r"\d+(?:\.\d+)?", normalized))
    skeleton = re.sub(r"\d+(?:\.\d+)?", "#", normalized)
    return skeleton, numbers


def possible_temporal_change(old: str, new: str) -> bool:
    """Potential change, NOT proof of contradiction/supersession."""
    old_skeleton, old_numbers = numeric_signature(old)
    new_skeleton, new_numbers = numeric_signature(new)
    return (
        old != new and len(old_skeleton) >= 10
        and old_skeleton == new_skeleton
        and bool(old_numbers) and bool(new_numbers)
        and old_numbers != new_numbers
    )


def _valid_date(value):
    # fromisoformat accepts some compact forms (YYYYMMDD). Explicit report
    # timestamps must use the unambiguous ISO extended representation.
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def valid_interval(claim) -> bool:
    """Explicit dates only; a missing boundary stays unknown."""
    start, end = _valid_date(claim.valid_from), _valid_date(claim.valid_to)
    return not (start and end and start > end)


def interval_relation(first, second) -> str:
    """Never confuse missing validity dates with known overlapping intervals."""
    if not valid_interval(first) or not valid_interval(second):
        return "invalid"
    a0, a1 = _valid_date(first.valid_from), _valid_date(first.valid_to)
    b0, b1 = _valid_date(second.valid_from), _valid_date(second.valid_to)
    if (a1 and b0 and a1 < b0) or (b1 and a0 and b1 < a0):
        return "disjoint"
    if a0 and a1 and b0 and b1:
        return "overlap"
    return "unknown"
