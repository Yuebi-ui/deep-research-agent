"""Phase 4B：critical path 归因的单元测试。

核心纪律：并行批次的贡献是**簇 makespan**，不是 summed 之和；
未归因的空隙必须显式保留，不得摊派给任何 role。
"""

from __future__ import annotations

from deep_research.profiling.critical_path import (
    attribute_blocking,
    chain_gaps,
    cluster_binders,
    cluster_wall,
    overlap_clusters,
    top_level_segments,
)


def _iv(role, start, end):
    return {"role": role, "start": start, "end": end}


def test_overlap_clusters_groups_transitive_overlaps():
    intervals = [_iv("a", 0, 5), _iv("b", 3, 8), _iv("c", 7, 9), _iv("d", 20, 21)]
    clusters = overlap_clusters(intervals)
    assert len(clusters) == 2
    assert [len(c) for c in clusters] == [3, 1]


def test_overlap_clusters_empty_and_touching():
    assert overlap_clusters([]) == []
    # 首尾相接（end == start）不算重叠
    assert len(overlap_clusters([_iv("a", 0, 1), _iv("b", 1, 2)])) == 2


def test_cluster_wall_and_binders():
    cluster = [_iv("a", 0, 3), _iv("b", 1, 10), _iv("c", 2, 4)]
    assert cluster_wall(cluster) == 10
    assert [iv["role"] for iv in cluster_binders(cluster)] == ["b"]


def test_parallel_batch_counts_makespan_not_summed():
    """10 个并行 judge：summed 21s，但 blocking 只有批次的 6s。"""
    judges = [_iv("evaluator", 100 + i * 0.1, 100 + i * 0.1 + (6 - i * 0.5)) for i in range(10)]
    result = attribute_blocking(judges)
    assert result["calls"]["evaluator"] == 10
    assert result["summed_s"]["evaluator"] == 37.5  # 累计 6+5.5+…+1.5
    assert result["summed_s"]["evaluator"] > 6 * result["blocking_s"]["evaluator"]
    assert result["blocking_s"]["evaluator"] == 6.0  # 关键路径只算批次 makespan
    assert result["coverage_s"] == 6.0


def test_serial_calls_each_contribute_blocking():
    calls = [_iv("writer", 0, 90), _iv("evaluator", 91, 107)]
    result = attribute_blocking(calls)
    assert result["blocking_s"] == {"evaluator": 16.0, "writer": 90.0}
    assert result["coverage_s"] == 106.0
    assert result["gap_s"] == 1.0  # 90→91 的空隙必须显式保留


def test_simultaneous_finish_splits_cluster_wall():
    calls = [_iv("a", 0, 5), _iv("b", 0, 5)]
    result = attribute_blocking(calls)
    assert result["blocking_s"] == {"a": 2.5, "b": 2.5}


def test_share_decomposition_is_additive():
    """份额归因必须可加：Σ blocking == coverage（被覆盖的时长）。"""
    calls = [_iv("a", 0, 10), _iv("b", 5, 15), _iv("c", 12, 20), _iv("d", 40, 44)]
    result = attribute_blocking(calls)
    assert round(sum(result["blocking_s"].values()), 3) == result["coverage_s"]
    # 簇1 = a/b/c 传递重叠（0→20），簇2 = d（40→44）
    assert result["coverage_s"] == 20 + 4


def test_late_starting_binder_does_not_absorb_whole_cluster():
    """迟到的收尾者不该把整簇的 wall 记在自己头上（旧的 binder 口径会高估）。"""
    calls = [_iv("long_worker", 0, 30), _iv("late_finisher", 25, 32)]
    result = attribute_blocking(calls)
    assert result["binder_s"]["late_finisher"] == 32  # 它是最后收尾者（诊断口径）
    # 份额口径：0–25 归 long_worker，25–30 两人平分，30–32 归 late_finisher
    assert result["blocking_s"]["long_worker"] == 27.5
    assert result["blocking_s"]["late_finisher"] == 4.5


def test_top_level_segments_orders_and_envelopes():
    rows = [
        {"node": "write_draft_report", "started_at": 10, "finished_at": 20},
        {"node": "write_draft_report", "started_at": 30, "finished_at": 40},
        {"node": "claim_verification", "started_at": 50, "finished_at": 60},
        {"node": "ignored_node", "started_at": 0, "finished_at": 1},
    ]
    segs = top_level_segments(rows, ["write_draft_report", "claim_verification"])
    assert [s["node"] for s in segs] == ["write_draft_report", "claim_verification"]
    assert segs[0]["start"] == 10 and segs[0]["end"] == 40
    assert segs[0]["calls"] == 2


def test_chain_gaps_reports_only_positive_gaps():
    segs = [
        {"node": "a", "start": 0, "end": 10},
        {"node": "b", "start": 12.5, "end": 20},
        {"node": "c", "start": 19, "end": 25},  # 重叠（异常但不应产生负 gap）
    ]
    gaps = chain_gaps(segs)
    assert gaps == [{"after": "a", "before": "b", "gap_s": 2.5}]
