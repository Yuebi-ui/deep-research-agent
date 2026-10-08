"""Baseline 聚合与报告渲染。

输入：采集器写出的 JSONL 原始指标（``<data_dir>/baseline_metrics/<run_id>/``）
      + 任务快照 + 环境快照。
输出：``run.json`` 结构性结果 + ``BASELINE.md`` 人读报告。

本模块只做纯聚合，不做任何网络 / LLM / 业务调用；所有「拿不到」的数据
在输出中显式标记 unavailable / null，不伪造。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deep_research.callbacks.cost_tracker import _MODEL_PRICE_PER_M_TOKEN

_METRIC_FILES = {
    "llm_calls": "llm_calls.jsonl",
    "node_metrics": "node_metrics.jsonl",
    "search_metrics": "search_metrics.jsonl",
    "budget_events": "budget_events.jsonl",
    "reliability_events": "reliability_events.jsonl",
}

_TERMINAL = {"completed", "failed", "cancelled", "deleted"}


def load_rows(raw_dir: Path | str) -> dict[str, list[dict]]:
    """读取原始 JSONL。缺失文件按空列表处理。"""
    root = Path(raw_dir)
    rows: dict[str, list[dict]] = {}
    for kind, filename in _METRIC_FILES.items():
        entries: list[dict] = []
        path = root / filename
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        rows[kind] = entries
    return rows


def aggregate(
    rows: dict[str, list[dict]],
    *,
    run_id: str,
    task: dict[str, Any],
    environment: dict[str, Any] | None = None,
    case: dict[str, Any] | None = None,
    generated_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把原始行聚合为 run.json 结构。全部字段来自真实行；取不到则 null。"""
    llm_rows = rows.get("llm_calls", [])
    node_rows = rows.get("node_metrics", [])
    search_rows = rows.get("search_metrics", [])
    budget_rows = rows.get("budget_events", [])
    rel_rows = rows.get("reliability_events", [])

    # ---- LLM ----
    success_rows = [r for r in llm_rows if r.get("success")]
    failed_rows = [r for r in llm_rows if not r.get("success")]

    def _sum_tokens(items: list[dict], key: str) -> int | None:
        values = [r.get(key) for r in items if isinstance(r.get(key), (int, float))]
        return int(sum(values)) if values else None

    def _provider_group(provider: str) -> list[dict]:
        return [r for r in llm_rows if r.get("provider") == provider]

    local_rows = _provider_group("openai_local")
    cloud_rows = _provider_group("openai")
    unknown_provider_rows = [
        r for r in llm_rows if r.get("provider") not in ("openai_local", "openai")
    ]

    overflow_rows = [r for r in llm_rows if r.get("error_type") == "context_overflow"]

    def _group_count(items: list[dict], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in items:
            k = str(r.get(key) or "unknown")
            out[k] = out.get(k, 0) + 1
        return out

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    cloud_cost = _estimate_cloud_cost(cloud_rows)
    local_cost = _estimate_cloud_cost(local_rows)  # 本地无价格 → 应为 None

    utilizations = [
        r["context_utilization"]
        for r in llm_rows
        if isinstance(r.get("context_utilization"), (int, float))
    ]
    overflow_token_values = [
        r["overflow_tokens"]
        for r in overflow_rows
        if isinstance(r.get("overflow_tokens"), (int, float))
    ]
    local_limits = {
        r.get("context_limit") for r in local_rows if r.get("context_limit")
    }

    # ---- nodes ----
    node_stats: dict[str, dict[str, Any]] = {}
    for r in node_rows:
        node = str(r.get("node") or "unknown")
        st = node_stats.setdefault(
            node, {"node": node, "executions": 0, "completed": 0, "unfinished": 0,
                   "failed": 0, "total_latency_ms": 0, "latencies_ms": []}
        )
        st["executions"] += 1
        if r.get("finished_at") is None:
            st["unfinished"] += 1
        else:
            st["completed"] += 1
            latency = r.get("latency_ms")
            if isinstance(latency, (int, float)):
                st["total_latency_ms"] += latency
                st["latencies_ms"].append(latency)
        if r.get("success") is False:
            st["failed"] += 1

    node_list = []
    for st in node_stats.values():
        node_list.append({
            "node": st["node"],
            "executions": st["executions"],
            "completed": st["completed"],
            "unfinished": st["unfinished"],
            "failed": st["failed"],
            "total_latency_ms": st["total_latency_ms"] or None,
            "avg_latency_ms": _avg(st["latencies_ms"]),
            "max_latency_ms": max(st["latencies_ms"]) if st["latencies_ms"] else None,
        })
    node_list.sort(key=lambda x: -(x["total_latency_ms"] or 0))

    # ---- search ----
    search_ok = [r for r in search_rows if r.get("success")]
    search_failed = [r for r in search_rows if not r.get("success")]

    # ---- reliability ----
    events = [str(r.get("event")) for r in rel_rows]
    claim_losses = events.count("claim_renewal_failed")
    claim_acquired = events.count("claim_acquired")
    recoveries = events.count("recovery_requeued") + events.count("job_reclaimed_stale")

    attempts = task.get("attempt")
    total_latency_ms = _task_latency_ms(task)

    run_json: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task.get("thread_id"),
        "variant": (case or {}).get("variant", "v1_baseline"),
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": environment or {},
        "case": case or {},
        "status": task.get("status"),
        "attempts": attempts,
        "timeline": {
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "total_latency_ms": total_latency_ms,
            "task_error": task.get("error") or None,
        },
        "llm": {
            "calls": len(llm_rows),
            "success": len(success_rows),
            "failed": len(failed_rows),
            "input_tokens": _sum_tokens(success_rows, "input_tokens"),
            "output_tokens": _sum_tokens(success_rows, "output_tokens"),
            "reasoning_tokens": _sum_tokens(success_rows, "reasoning_tokens"),
            "content_tokens": _sum_tokens(success_rows, "content_tokens"),
            "by_role_reasoning": _sum_by_key(success_rows, "role", "reasoning_tokens"),
            "thinking_flags": _group_count(llm_rows, "thinking"),
            "success_without_usage": sum(
                1 for r in success_rows if not r.get("token_source")
            ),
            "by_role": _group_count(llm_rows, "role"),
            "by_model": _group_count(llm_rows, "model"),
            "by_node": _group_count(llm_rows, "node"),
            "avg_latency_ms": _avg(
                [r["latency_ms"] for r in llm_rows if isinstance(r.get("latency_ms"), (int, float))]
            ),
        },
        "local": {
            "calls": len(local_rows),
            "success": len([r for r in local_rows if r.get("success")]),
            "failed": len([r for r in local_rows if not r.get("success")]),
            "input_tokens": _sum_tokens(local_rows, "input_tokens"),
            "output_tokens": _sum_tokens(local_rows, "output_tokens"),
            "context_overflow": len([r for r in local_rows if r.get("error_type") == "context_overflow"]),
            "context_limits_seen": sorted(v for v in local_limits if v),
        },
        "cloud": {
            "calls": len(cloud_rows),
            "success": len([r for r in cloud_rows if r.get("success")]),
            "failed": len([r for r in cloud_rows if not r.get("success")]),
            "input_tokens": _sum_tokens(cloud_rows, "input_tokens"),
            "output_tokens": _sum_tokens(cloud_rows, "output_tokens"),
            "estimated_cost_rmb": cloud_cost,
            "cost_note": "按 cost_tracker 价格表估算（RMB），非账单精确值"
            if cloud_cost is not None else "unavailable: 价格表缺少对应 model",
        },
        "other_providers": {
            "calls": len(unknown_provider_rows),
            "by_provider": _group_count(unknown_provider_rows, "provider"),
        },
        "context": {
            "overflow_count": len(overflow_rows),
            "overflow_rate_of_all_calls": round(len(overflow_rows) / len(llm_rows), 4) if llm_rows else None,
            "overflow_rate_of_local_calls": round(len(overflow_rows) / len(local_rows), 4) if local_rows else None,
            "overflow_by_node": _group_count(overflow_rows, "node"),
            "overflow_by_role": _group_count(overflow_rows, "role"),
            "overflow_by_model": _group_count(overflow_rows, "model"),
            "overflow_tokens": {
                "count": len(overflow_token_values),
                "min": min(overflow_token_values) if overflow_token_values else None,
                "max": max(overflow_token_values) if overflow_token_values else None,
                "avg": _avg([float(v) for v in overflow_token_values]),
                "note": "来自 provider 错误消息中的 input token 下限（at least N），非精确值"
                if overflow_token_values else None,
            },
            "utilization": {
                "samples": len(utilizations),
                "avg": _avg([float(v) for v in utilizations]),
                "max": max(utilizations) if utilizations else None,
                "note": "input_tokens / context_limit，仅当两者都已知时计算",
            },
            "local_cost_rmb": local_cost,
        },
        "context_budget": _aggregate_budget(budget_rows),
        "search": {
            "calls": len(search_rows),
            "success": len(search_ok),
            "failed": len(search_failed),
            "queries": len({r.get("query") for r in search_rows if r.get("query")}),
            "results": sum(
                r.get("result_count") or 0 for r in search_ok if isinstance(r.get("result_count"), int)
            ) if any(isinstance(r.get("result_count"), int) for r in search_ok) else None,
            "avg_latency_ms": _avg(
                [r["latency_ms"] for r in search_rows if isinstance(r.get("latency_ms"), (int, float))]
            ),
            "by_tool_node": _group_count(search_rows, "node"),
        },
        "reliability": {
            "attempts": attempts,
            "claim_acquired": claim_acquired,
            "claim_losses": claim_losses,
            "recoveries": recoveries,
            # P0 ownership fencing：中止与放弃的显式计数（与 recovery 可区分）
            "execution_aborts": events.count("execution_aborted"),
            "stale_execution_abandoned": events.count("stale_execution_abandoned"),
            "events": _reliability_event_summary(rel_rows),
            "final_status": task.get("status"),
        },
        "nodes": node_list,
        "data_quality": _data_quality(llm_rows, overflow_rows, rel_rows, run_id),
    }

    if generated_extra:
        run_json.update(generated_extra)
    return run_json


def _count_by(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


def _sum_by_key(rows: list[dict], group_key: str, value_key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        value = r.get(value_key)
        if isinstance(value, (int, float)) and value:
            k = str(r.get(group_key) or "unknown")
            out[k] = out.get(k, 0) + int(value)
    return out


def _aggregate_budget(budget_rows: list[dict]) -> dict[str, Any]:
    """P1 context budget 汇总。无事件时如实标注 unavailable。"""

    def _sum(key: str) -> int | None:
        values = [r.get(key) for r in budget_rows if isinstance(r.get(key), (int, float))]
        return int(sum(values)) if values else None

    applied = [r for r in budget_rows if r.get("applied")]
    trims = [r for r in applied if str(r.get("reason", "")).startswith("trimmed")]
    limits = sorted({r.get("context_limit") for r in budget_rows if isinstance(r.get("context_limit"), int)})

    return {
        "events": len(budget_rows),
        "applied": len(applied),
        "trims": len(trims),
        # 单位说明：content tokens = 原始内容 token；prompt tokens = 含模板/标记的
        # 最终 prompt token。两者不可直接相减（"fits" 事件的 prompt > content）。
        "original_content_tokens_total": _sum("original_tokens"),
        "final_prompt_tokens_total": _sum("final_tokens"),
        "trimmed_tokens_total": _sum("trimmed_tokens"),
        "by_reason": _count_by(budget_rows, "reason"),
        "by_counter": _count_by(budget_rows, "counter"),
        "limits_seen": limits,
        "unavailable": None if budget_rows else "no budget events captured",
    }


def _estimate_cloud_cost(rows: list[dict]) -> float | None:
    """按已知价格表估算费用（RMB）。价格未知的 model 返回 None。"""
    if not rows:
        return None
    total = 0.0
    priced_any = False
    for r in rows:
        model = str(r.get("model") or "")
        price = _MODEL_PRICE_PER_M_TOKEN.get(model)
        if not price:
            continue
        input_tokens = r.get("input_tokens") or 0
        output_tokens = r.get("output_tokens") or 0
        total += (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
        priced_any = True
    return round(total, 6) if priced_any else None


def _task_latency_ms(task: dict[str, Any]) -> int | None:
    created, updated = task.get("created_at"), task.get("updated_at")
    if not created or not updated:
        return None
    try:
        c = datetime.fromisoformat(str(created))
        u = datetime.fromisoformat(str(updated))
        return int((u - c).total_seconds() * 1000)
    except ValueError:
        return None


def _reliability_event_summary(rows: list[dict]) -> list[dict[str, Any]]:
    summary = []
    for r in rows:
        summary.append({
            "event": r.get("event"),
            "attempt": r.get("attempt"),
            "timestamp": r.get("timestamp"),
            "details": r.get("details"),
        })
    return summary


def _data_quality(
    llm_rows: list[dict],
    overflow_rows: list[dict],
    rel_rows: list[dict],
    run_id: str,
) -> dict[str, Any]:
    notes: list[str] = []
    unavailable: list[str] = []

    success_rows = [r for r in llm_rows if r.get("success")]
    no_usage = [r for r in success_rows if not r.get("token_source")]
    if no_usage:
        notes.append(
            f"{len(no_usage)} 次成功调用未拿到 provider usage（token 记为 null）"
        )

    local_rows = [r for r in llm_rows if r.get("provider") == "openai_local"]
    unknown_limit = [r for r in local_rows if not r.get("context_limit")]
    if unknown_limit:
        notes.append(
            f"{len(unknown_limit)} 次本地调用的 context_limit 未知（未能探测 / 未出现在错误消息中）"
        )

    cloud_rows = [r for r in llm_rows if r.get("provider") == "openai"]
    if cloud_rows and all(not r.get("context_limit") for r in cloud_rows):
        unavailable.append("cloud context_limit（DashScope 不在响应中返回）")
        notes.append("云端调用的 context_utilization 全部为 null —— 无可靠 context limit 来源")

    if not overflow_rows:
        notes.append("未观察到 context overflow（可能是真实为 0，也可能是未触发该路径）")

    if not any(r.get("event") == "claim_renewal_failed" for r in rel_rows):
        notes.append("claim_losses=0 表示未观测到心跳续约失败事件")

    mismatched = [r for r in llm_rows if r.get("run_id") != run_id]
    if mismatched:
        notes.append(f"{len(mismatched)} 行 LLM 指标的 run_id 与本 run 不一致")

    return {
        "unavailable": unavailable,
        "notes": notes,
        "precision": {
            "input_output_tokens_success_calls": "精确（provider usage）"
            if success_rows and not no_usage else "部分/不可用（见 notes）",
            "overflow_tokens": "下限值（错误消息 at least N）" if overflow_rows else "n/a",
            "estimated_cloud_cost": "估算（价格表），非账单",
            "node_latency": "来自 worker 侧 graph 事件时间戳，含少量事件发布开销",
        },
    }


# ===== BASELINE.md 渲染 =====


def render_baseline_md(run: dict[str, Any]) -> str:
    env = run.get("environment") or {}
    llm = run.get("llm") or {}
    local = run.get("local") or {}
    cloud = run.get("cloud") or {}
    ctx = run.get("context") or {}
    search = run.get("search") or {}
    rel = run.get("reliability") or {}
    budget = run.get("context_budget") or {}
    nodes = run.get("nodes") or []
    dq = run.get("data_quality") or {}
    case = run.get("case") or {}
    timeline = run.get("timeline") or {}

    def _fmt(value: Any, suffix: str = "") -> str:
        return "unavailable" if value is None else f"{value}{suffix}"

    lines = [
        "# V1 Baseline",
        "",
        f"run_id: `{run.get('run_id')}`  |  task_id: `{run.get('task_id')}`  |  variant: `{run.get('variant')}`",
        "",
        "## Environment",
        "",
        f"- GPU: {env.get('gpu', 'unavailable')}",
        f"- Local model: {env.get('local_model', 'unavailable')}",
        f"- Serving: {env.get('serving', 'unavailable')}",
        f"- Context limit (local): {_fmt(env.get('context_limit_local'))}",
        f"- Routing: {env.get('routing', 'unavailable')}",
        f"- APP_ENV: {env.get('app_env', 'unavailable')}, STAGE: {env.get('stage', 'unavailable')}",
        f"- Versions: {env.get('versions', 'unavailable')}",
        "",
        "## Case",
        "",
        f"- query: {case.get('query', 'unavailable')}",
        f"- review action: {case.get('review_action', 'unavailable')}",
        "",
        "## Result",
        "",
        f"- Final status: **{run.get('status')}**",
        f"- Attempts: {_fmt(run.get('attempts'))}",
        f"- Total latency: {_fmt(timeline.get('total_latency_ms'), ' ms')}"
        + (f" ({timeline.get('total_latency_ms', 0) / 1000:.1f} s)" if timeline.get("total_latency_ms") else ""),
        "",
        "### LLM",
        "",
        "| | calls | success | failed | input tokens | output tokens |",
        "|---|---|---|---|---|---|",
        f"| Total | {llm.get('calls')} | {llm.get('success')} | {llm.get('failed')} | {_fmt(llm.get('input_tokens'))} | {_fmt(llm.get('output_tokens'))} |",
        f"| Local | {local.get('calls')} | {local.get('success')} | {local.get('failed')} | {_fmt(local.get('input_tokens'))} | {_fmt(local.get('output_tokens'))} |",
        f"| Cloud | {cloud.get('calls')} | {cloud.get('success')} | {cloud.get('failed')} | {_fmt(cloud.get('input_tokens'))} | {_fmt(cloud.get('output_tokens'))} |",
        "",
        f"- by role: {llm.get('by_role')}",
        f"- by model: {llm.get('by_model')}",
        f"- reasoning tokens（若 provider 上报）: {_fmt(llm.get('reasoning_tokens'))} "
        f"（content tokens: {_fmt(llm.get('content_tokens'))}）",
        f"- thinking flags: {llm.get('thinking_flags')} | reasoning by role: {llm.get('by_role_reasoning')}",
        "",
        "### Context",
        "",
        f"- Overflow count: {ctx.get('overflow_count')}",
        f"- Overflow rate (of all LLM calls): {_fmt(ctx.get('overflow_rate_of_all_calls'))}",
        f"- Overflow rate (of local calls): {_fmt(ctx.get('overflow_rate_of_local_calls'))}",
        f"- Affected nodes: {ctx.get('overflow_by_node')}",
        f"- Affected roles: {ctx.get('overflow_by_role')}",
        f"- Overflow tokens: {ctx.get('overflow_tokens')}",
        f"- Utilization: {ctx.get('utilization')}",
        "",
        "### Context Budget (P1)",
        "",
        f"- budget events: {budget.get('events')} | applied: {budget.get('applied')} | "
        f"trims: {budget.get('trims')}"
        + (f" | unavailable: {budget.get('unavailable')}" if budget.get("unavailable") else ""),
        f"- tokens: original content={_fmt(budget.get('original_content_tokens_total'))} | "
        f"final prompt={_fmt(budget.get('final_prompt_tokens_total'))} | "
        f"trimmed={_fmt(budget.get('trimmed_tokens_total'))}",
        f"- by reason: {budget.get('by_reason')}",
        f"- limits seen: {budget.get('limits_seen')} | counter: {budget.get('by_counter')}",
        "",
        "### Cost",
        "",
        f"- Local tokens: in={_fmt(local.get('input_tokens'))} out={_fmt(local.get('output_tokens'))}"
        " （本地推理，无按 token 计费）",
        f"- Cloud estimated cost: {_fmt(cloud.get('estimated_cost_rmb'), ' RMB')} — {cloud.get('cost_note')}",
        "",
        "### Search",
        "",
        f"- calls: {search.get('calls')}  | success: {search.get('success')} | failed: {search.get('failed')}",
        f"- queries: {_fmt(search.get('queries'))} | results: {_fmt(search.get('results'))}",
        f"- avg latency: {_fmt(search.get('avg_latency_ms'), ' ms')}",
        "",
        "### Reliability",
        "",
        f"- claim acquired: {_fmt(rel.get('claim_acquired'))}",
        f"- claim losses: {_fmt(rel.get('claim_losses'))}",
        f"- execution aborts after ownership loss: {_fmt(rel.get('execution_aborts'))}",
        f"- stale executions abandoned (no ack): {_fmt(rel.get('stale_execution_abandoned'))}",
        f"- recoveries: {_fmt(rel.get('recoveries'))}",
        "",
        "## Node Latency (top)",
        "",
        "| node | executions | completed | failed | total ms | avg ms | max ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in nodes[:15]:
        lines.append(
            f"| {n['node']} | {n['executions']} | {n['completed']} | {n['failed']} | "
            f"{_fmt(n['total_latency_ms'])} | {_fmt(n['avg_latency_ms'])} | {_fmt(n['max_latency_ms'])} |"
        )

    lines += [
        "",
        "## Observed Bottlenecks",
        "",
        "> 本节只记录 measurement 观察到的事实，不在此实施解决方案。",
        "",
        *_observed_bottlenecks(run),
        "",
        "## Data Quality / Limitations",
        "",
        f"- unavailable: {dq.get('unavailable') or '（无）'}",
    ]
    for note in dq.get("notes") or []:
        lines.append(f"- {note}")
    lines.append(f"- precision: {dq.get('precision')}")
    lines.append("")
    return "\n".join(lines)


def _observed_bottlenecks(run: dict[str, Any]) -> list[str]:
    """从数据中派生「观察到的」事实列表（描述性，无建议）。"""
    out: list[str] = []
    ctx = run.get("context") or {}
    nodes = run.get("nodes") or []
    rel = run.get("reliability") or {}

    if ctx.get("overflow_count"):
        by_node = ctx.get("overflow_by_node") or {}
        top = ", ".join(f"{k}={v}" for k, v in sorted(by_node.items(), key=lambda x: -x[1]))
        out.append(f"1. Context overflow 共 {ctx['overflow_count']} 次，集中在: {top}")

    total = sum(n.get("total_latency_ms") or 0 for n in nodes)
    if total > 0:
        top = nodes[0]
        share = (top.get("total_latency_ms") or 0) / total * 100
        out.append(
            f"2. 节点耗时占比最高: {top['node']}（{top.get('total_latency_ms')} ms, "
            f"占已记录节点总耗时的 {share:.1f}%）"
        )

    if rel.get("claim_losses"):
        out.append(f"3. 发生 execution claim loss {rel['claim_losses']} 次（详见 reliability_events.jsonl）")
    if rel.get("recoveries"):
        out.append(f"4. 发生 recovery/重新入队 {rel['recoveries']} 次")
    if not out:
        out.append("（本次 run 未观察到显著 bottleneck 事实）")
    return out


__all__ = ["aggregate", "load_rows", "render_baseline_md"]
