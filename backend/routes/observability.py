"""可观测性 API：Agent 执行轨迹、成本统计。"""

from __future__ import annotations

import json
import os
from fastapi import APIRouter

from backend.core.settings import get_settings
from backend.schemas.responses import (
    AlertsResponse,
    CostDetailResponse,
    StatsResponse,
    TraceGraphResponse,
)

router = APIRouter(prefix="/api/obs", tags=["observability"])

# ---- 告警阈值 ----
ALERT_THRESHOLDS = {
    "max_cost_per_task_rmb": 2.0,     # 单次任务超过 ¥2 告警
    "max_hallucination_rate": 0.3,    # 幻觉率超过 30% 告警
    "min_quality_score": 5.0,         # 质量均分低于 5 告警
}

def _data_dir() -> str:
    """数据目录。延迟解析，避免 import 期固化路径。"""
    return str(get_settings().data_dir)


def _trace_path() -> str:
    return os.path.join(_data_dir(), "agent_trace.jsonl")


def _cost_path() -> str:
    return os.path.join(_data_dir(), "cost_log.jsonl")


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


@router.get("/trace/{thread_id}", response_model=TraceGraphResponse)
async def get_trace(thread_id: str):
    """返回指定任务的 Agent 执行轨迹（节点树 + 工具调用）。"""
    events = _read_jsonl(_trace_path())
    # 按 thread_id 过滤
    filtered = [e for e in events if e.get("thread_id") == thread_id]
    return _build_trace_tree(filtered)


@router.get("/cost/{thread_id}", response_model=CostDetailResponse)
async def get_cost(thread_id: str):
    """返回指定任务的 Token 消耗和成本。"""
    records = _read_jsonl(_cost_path())
    # 按 thread_id 过滤
    filtered = [r for r in records if r.get("thread_id") == thread_id]
    total_input = sum(r.get("input_tokens", 0) for r in filtered)
    total_output = sum(r.get("output_tokens", 0) for r in filtered)
    total_cost = sum(r.get("cost_rmb", 0) for r in filtered)

    model_stats: dict[str, dict] = {}
    for r in filtered:
        model = r.get("model", "unknown")
        if model not in model_stats:
            model_stats[model] = {"input_tokens": 0, "output_tokens": 0, "cost_rmb": 0.0, "calls": 0}
        model_stats[model]["input_tokens"] += r.get("input_tokens", 0)
        model_stats[model]["output_tokens"] += r.get("output_tokens", 0)
        model_stats[model]["cost_rmb"] += r.get("cost_rmb", 0)
        model_stats[model]["calls"] += 1

    return {
        "thread_id": thread_id,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_rmb": round(total_cost, 4),
        "model_stats": model_stats,
        "records": filtered[-50:],  # 最近 50 条
    }


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    """返回全局统计摘要 + 幻觉率趋势。"""
    records = _read_jsonl(_cost_path())
    trace_events = _read_jsonl(_trace_path())

    total_input = sum(r.get("input_tokens", 0) for r in records)
    total_output = sum(r.get("output_tokens", 0) for r in records)
    total_cost = sum(r.get("cost_rmb", 0) for r in records)

    # 幻觉率趋势：从 trace 中的 quality_score 和 critique 事件汇总
    quality_events = [e for e in trace_events if e.get("event_type") == "quality_score"]
    hallucination_rates: list[dict] = []

    # 按 thread_id 聚合 quality scores
    by_thread: dict[str, list[dict]] = {}
    for e in quality_events:
        tid = e.get("thread_id", "")
        if tid:
            by_thread.setdefault(tid, []).append(e)

    for tid, events in by_thread.items():
        scores = [ev.get("data", {}).get("score", 0) for ev in events]
        avg_score = sum(scores) / len(scores) if scores else 0
        hallucination_rates.append({
            "thread_id": tid,
            "avg_quality_score": round(avg_score, 2),
            "iterations": len(scores),
            "score_trend": scores,
        })

    return {
        "total_tasks": len(by_thread),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cost_rmb": round(total_cost, 4),
        "total_llm_calls": len(records),
        "total_trace_events": len(trace_events),
        "quality_stats": {
            "threads_with_quality_scores": len(hallucination_rates),
            "average_quality_score": round(sum(h["avg_quality_score"] for h in hallucination_rates) / len(hallucination_rates), 2) if hallucination_rates else 0,
            "per_thread": hallucination_rates[-20:],  # 最近 20 个任务
        },
    }


@router.get("/alerts", response_model=AlertsResponse)
async def get_alerts():
    """返回当前质量告警。"""
    records = _read_jsonl(_cost_path())
    trace_events = _read_jsonl(_trace_path())

    alerts: list[dict] = []

    # 按 thread_id 聚合 cost
    cost_by_thread: dict[str, float] = {}
    for r in records:
        tid = r.get("thread_id", "")
        cost_by_thread[tid] = cost_by_thread.get(tid, 0) + r.get("cost_rmb", 0)

    for tid, cost in cost_by_thread.items():
        if cost > ALERT_THRESHOLDS["max_cost_per_task_rmb"]:
            alerts.append({
                "type": "high_cost",
                "thread_id": tid,
                "message": f"任务成本 ¥{cost:.2f} 超过阈值 ¥{ALERT_THRESHOLDS['max_cost_per_task_rmb']}",
                "value": round(cost, 4),
                "threshold": ALERT_THRESHOLDS["max_cost_per_task_rmb"],
            })

    # 质量告警
    quality_events = [e for e in trace_events if e.get("event_type") == "quality_score"]
    by_thread: dict[str, list[dict]] = {}
    for e in quality_events:
        tid = e.get("thread_id", "")
        if tid:
            by_thread.setdefault(tid, []).append(e)

    for tid, events in by_thread.items():
        scores = [ev.get("data", {}).get("score", 0) for ev in events]
        avg_score = sum(scores) / len(scores) if scores else 0
        if avg_score < ALERT_THRESHOLDS["min_quality_score"] and avg_score > 0:
            alerts.append({
                "type": "low_quality",
                "thread_id": tid,
                "message": f"质量均分 {avg_score:.1f} 低于阈值 {ALERT_THRESHOLDS['min_quality_score']}",
                "value": round(avg_score, 2),
                "threshold": ALERT_THRESHOLDS["min_quality_score"],
            })

    return {
        "alerts_count": len(alerts),
        "thresholds": ALERT_THRESHOLDS,
        "alerts": alerts[-20:],  # 最近 20 条告警
    }


def _build_trace_tree(events: list[dict]) -> dict:
    """将平铺的 trace 事件转为树形结构。"""
    nodes = []
    edges = []
    # 追踪每个 agent 最新的 decision 节点，用于 tool_call 连边
    current_node: dict[str, str] = {}
    counter = [0]

    def new_id() -> str:
        nid = f"n{counter[0]}"
        counter[0] += 1
        return nid

    for evt in events:
        agent = evt.get("agent", "unknown")
        event_type = evt.get("event_type", "")
        data = evt.get("data", {})

        if event_type == "decision":
            nid = new_id()
            current_node[agent] = nid
            nodes.append({
                "id": nid,
                "type": "agent",
                "label": agent,
                "detail": data.get("decision", ""),
            })
        elif event_type == "tool_call":
            tool_name = data.get("tool", "unknown")
            nid = new_id()
            parent_nid = current_node.get(agent)
            nodes.append({
                "id": nid,
                "type": "tool",
                "label": tool_name,
                "detail": data.get("input", "")[:100],
            })
            if parent_nid:
                edges.append({"source": parent_nid, "target": nid})
        elif event_type == "quality_score":
            nodes.append({
                "id": new_id(),
                "type": "quality",
                "label": f"Quality: {data.get('score', '?')}/10",
                "detail": data.get("feedback", ""),
            })

    return {"nodes": nodes, "edges": edges}
