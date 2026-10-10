"""Validated information-retrieval and runtime metrics, no external packages."""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def _validate_ids(relevant: Sequence[str], retrieved: Sequence[str]) -> None:
    if not relevant or len(set(relevant)) != len(relevant):
        raise ValueError('relevant must contain distinct nonempty document IDs')
    if any(not isinstance(x, str) or not x for x in relevant):
        raise ValueError('relevant IDs must be nonempty strings')
    if len(set(retrieved)) != len(retrieved):
        raise ValueError('retrieved IDs must be unique')


def retrieval_metrics(relevant: Sequence[str], retrieved: Sequence[str]) -> dict[str, float]:
    """Binary relevance; Recall@k uses number of gold documents as denominator."""
    _validate_ids(relevant, retrieved)
    gold = set(relevant)
    measures = {}
    for k in (1, 3, 5, 10):
        measures[f'recall_at_{k}'] = len(gold.intersection(retrieved[:k])) / len(gold)
    reciprocal_ranks = [1.0 / (i + 1) for i, doc_id in enumerate(retrieved[:10]) if doc_id in gold]
    measures['reciprocal_rank_at_10'] = max(reciprocal_ranks, default=0.0)
    dcg = sum(1.0 / math.log2(i + 2) for i, doc_id in enumerate(retrieved[:10]) if doc_id in gold)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), 10)))
    measures['ndcg_at_10'] = dcg / idcg
    return measures


def mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        raise ValueError('cannot evaluate an empty result set')
    names = set(rows[0])
    if any(set(row) != names for row in rows):
        raise ValueError('every query must have the same measured fields')
    return {k: round(statistics.fmean(row[k] for row in rows), 6) for k in sorted(names)}


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation on rank (n-1)*p. Explicit to prevent P95 ambiguity."""
    if not values:
        return None
    if not 0 <= p <= 1 or any(not math.isfinite(float(v)) for v in values):
        raise ValueError('percentile arguments out of range')
    numbers = sorted(float(x) for x in values)
    point = (len(numbers) - 1) * p
    lo = math.floor(point)
    hi = math.ceil(point)
    return numbers[lo] + (numbers[hi] - numbers[lo]) * (point - lo)
