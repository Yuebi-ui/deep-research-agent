"""Phase 4B：E2E critical path 归因（纯函数）。

口径纪律（与任务书 §B2 一致）：

- **summed latency ≠ critical-path contribution**。10 个并行 judge 的 summed
  可能是 54s，但它们重叠在同一个 ~6.5s 批次里 —— 该批次的贡献就是 6.5s；
- 只能对**有证据**的关系下结论：
  * 顶层节点链由 graph 结构保证串行（`agent_builder._create_builder`）→ 每个顶层
    节点 wall 100% 在关键路径上；
  * 节点内部的并行批次用"最大重叠簇"划分，簇的 makespan 才是该簇的阻塞时间，
    归因给"最后完成的调用"所属 role（binder）；
  * 无法归因的部分显式记为 gap / unattributed，不硬凑。

方法：把区间按重叠关系做**最大簇划分**（union-find 传递闭包），每个簇的
makespan = 该批次的阻塞时长；binder = 结束时间落在簇尾容差内的调用。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

DEFAULT_BINDER_TOL = 0.05  # 秒：与簇尾相差 50ms 内视为"同时收尾"


def overlap_clusters(intervals: Sequence[dict], *, key_start: str = "start", key_end: str = "end") -> list[list[dict]]:
    """把区间按重叠关系聚成**最大簇**（传递重叠：A∩B、B∩C ⇒ A,B,C 同簇）。"""
    items = [iv for iv in intervals if isinstance(iv.get(key_start), (int, float))
             and isinstance(iv.get(key_end), (int, float))]
    if not items:
        return []
    items = sorted(items, key=lambda iv: (iv[key_start], iv[key_end]))
    clusters: list[list[dict]] = [[items[0]]]
    cluster_end = items[0][key_end]
    for iv in items[1:]:
        if iv[key_start] < cluster_end:  # 与当前簇有重叠
            clusters[-1].append(iv)
            cluster_end = max(cluster_end, iv[key_end])
        else:
            clusters.append([iv])
            cluster_end = iv[key_end]
    return clusters


def cluster_wall(cluster: Sequence[dict], *, key_start: str = "start", key_end: str = "end") -> float:
    return max(iv[key_end] for iv in cluster) - min(iv[key_start] for iv in cluster)


def cluster_binders(cluster: Sequence[dict], *, tol: float = DEFAULT_BINDER_TOL,
                    key_end: str = "end") -> list[dict]:
    """簇内最后收尾的调用（临界调用）；tol 容忍同批次的收尾抖动。"""
    latest = max(iv[key_end] for iv in cluster)
    return [iv for iv in cluster if latest - iv[key_end] <= tol]


def share_decomposition(cluster: Sequence[dict], *, role_key: str = "role") -> dict[str, float]:
    """把簇的 makespan 按"并发份额"拆给各 role（可加：Σ = 簇 wall）。

    在每个基本时间片（由所有 start/end 切分）内，把片长平均分给当时并发的调用。
    这样：
    - 独占的调用拿到 100% 片长；
    - 10 个并行 judge 合起来只算一个批次的 wall，摊到每个 judge 是 1/10；
    - **不会**把整个簇的 wall 都记到最后收尾的那个调用头上（它可能起得晚）。
    """
    events = sorted({iv["start"] for iv in cluster} | {iv["end"] for iv in cluster})
    credit: dict[str, float] = {}
    for lo, hi in zip(events, events[1:]):
        if hi <= lo:
            continue
        active = [iv for iv in cluster if iv["start"] <= lo and iv["end"] >= hi]
        if not active:
            continue
        share = (hi - lo) / len(active)
        for iv in active:
            role = iv.get(role_key) or "unknown"
            credit[role] = credit.get(role, 0.0) + share
    return credit


def attribute_blocking(intervals: Sequence[dict], *, role_key: str = "role",
                       tol: float = DEFAULT_BINDER_TOL) -> dict[str, Any]:
    """把一串调用区间归因到"关键路径阻塞时间"，返回按 role 的汇总。

    - `blocking_s[role]`：该 role 的**并发份额时间**（Σ 各簇内份额）。
      可加性：Σ_role blocking_s = coverage_s = 被调用覆盖的时长；
    - `summed_s[role]`：该 role 的累计耗时（仅说明并行工作量，**不是**关键路径）；
    - `binder_s[role]`：该 role 作为"最后收尾者"的簇 wall 之和（诊断用，
      与 blocking 一起看可以判断是谁在拖尾）；
    - `coverage_s`：所有簇 makespan 之和；`span_s`：首尾跨度；`gap_s`：簇间空隙。
    """
    clusters = overlap_clusters(intervals)
    summed: dict[str, float] = {}
    calls: dict[str, int] = {}
    for iv in intervals:
        role = iv.get(role_key) or "unknown"
        summed[role] = summed.get(role, 0.0) + (iv["end"] - iv["start"])
        calls[role] = calls.get(role, 0) + 1

    share_s: dict[str, float] = {}
    binder_s: dict[str, float] = {}
    per_cluster: list[dict] = []
    for cluster in clusters:
        wall = cluster_wall(cluster)
        binders = cluster_binders(cluster, tol=tol)
        for role, value in share_decomposition(cluster, role_key=role_key).items():
            share_s[role] = share_s.get(role, 0.0) + value
        for iv in binders:
            role = iv.get(role_key) or "unknown"
            binder_s[role] = binder_s.get(role, 0.0) + wall / len(binders)
        per_cluster.append({
            "start": min(iv["start"] for iv in cluster),
            "end": max(iv["end"] for iv in cluster),
            "wall_s": round(wall, 3),
            "calls": len(cluster),
            "binder_roles": sorted({(iv.get(role_key) or "unknown") for iv in binders}),
            "roles": sorted({(iv.get(role_key) or "unknown") for iv in cluster}),
        })

    coverage = sum(c["wall_s"] for c in per_cluster)
    span = 0.0
    if clusters:
        span = max(iv["end"] for c in clusters for iv in c) - min(iv["start"] for c in clusters for iv in c)
    return {
        "blocking_s": {k: round(v, 3) for k, v in sorted(share_s.items(), key=lambda kv: -kv[1])},
        "binder_s": {k: round(v, 3) for k, v in sorted(binder_s.items(), key=lambda kv: -kv[1])},
        "summed_s": {k: round(v, 3) for k, v in sorted(summed.items(), key=lambda kv: -kv[1])},
        "calls": calls,
        "coverage_s": round(coverage, 3),
        "span_s": round(span, 3),
        "gap_s": round(span - coverage, 3),
        "clusters": per_cluster,
    }


def top_level_segments(node_rows: Iterable[dict], top_level: Sequence[str]) -> list[dict]:
    """按拓扑顺序把顶层节点压成串行段（同名节点多次出现时取首尾包络）。"""
    rows = [r for r in node_rows if r.get("node") in set(top_level)
            and isinstance(r.get("started_at"), (int, float))
            and isinstance(r.get("finished_at"), (int, float))]
    segments: list[dict] = []
    for node in top_level:
        spans = [(float(r["started_at"]), float(r["finished_at"])) for r in rows if r["node"] == node]
        if not spans:
            continue
        segments.append({
            "node": node,
            "start": min(s for s, _ in spans),
            "end": max(e for _, e in spans),
            "calls": len(spans),
        })
    segments.sort(key=lambda s: s["start"])
    return segments


def chain_gaps(segments: Sequence[dict]) -> list[dict]:
    """相邻串行段之间的空隙（编排 idle / 未归因时间）。"""
    gaps: list[dict] = []
    for prev, nxt in zip(segments, segments[1:]):
        gap = nxt["start"] - prev["end"]
        if gap > 0:
            gaps.append({"after": prev["node"], "before": nxt["node"], "gap_s": round(gap, 3)})
    return gaps
