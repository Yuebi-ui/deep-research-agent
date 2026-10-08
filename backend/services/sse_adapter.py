"""将 LangGraph astream_events 转换为 SSE 格式，同时记录成本与执行轨迹。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

from deep_research.callbacks.tracer import AgentTrace
from deep_research.callbacks.cost_tracker import CostTrackerCallback, set_cost_callback

from backend.core.settings import get_settings

_NODE_LABELS = {
    "write_research_brief": "研究简报生成",
    "write_draft_report": "报告草稿生成",
    "human_review": "人工审查",
    "supervisor": "Supervisor 决策",
    "supervisor_tools": "工具执行",
    "final_report_generation": "最终报告生成",
    "llm_call": "Research Agent 思考",
    "tool_node": "工具调用",
    "compress_research": "研究结果压缩",
    "red_team": "Red Team 对抗审查",
    "claim_verification": "事实核查",
}

def _data_dir() -> str:
    """数据目录。延迟解析，避免 import 期固化路径。"""
    return str(get_settings().data_dir)


def _cost_log_path() -> Path:
    return Path(_data_dir()) / "cost_log.jsonl"

# Qwen3 模型单价（每百万 token，RMB）
_MODEL_PRICE: dict[str, dict[str, float]] = {
    "qwen3-235b-a22b": {"input": 4.0, "output": 16.0},
    "qwen3-32b": {"input": 3.5, "output": 7.0},
}
_DEFAULT_PRICE = {"input": 0.0, "output": 0.0}

# 模块级共享 AgentTrace
_agent_trace: AgentTrace | None = None


def _get_agent_trace() -> AgentTrace:
    global _agent_trace
    if _agent_trace is None:
        _agent_trace = AgentTrace(log_dir=_data_dir())
    return _agent_trace


def _record_cost(thread_id: str, model_name: str, input_tokens: int, output_tokens: int):
    """直接将 token 消耗写入 cost_log.jsonl。"""
    price = _MODEL_PRICE.get(model_name, _DEFAULT_PRICE)
    cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
    record = {
        "thread_id": thread_id,
        "timestamp": time.time(),
        "model": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_rmb": round(cost, 6),
    }
    os.makedirs(_data_dir(), exist_ok=True)
    with open(_cost_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _extract_tokens_from_event(data: dict) -> tuple[str, int, int]:
    """从 on_chat_model_end 事件中提取 model + token 用量。"""
    output = data.get("output", {})
    # LangChain ChatResult 格式
    if hasattr(output, "response_metadata"):
        meta = output.response_metadata
        model = meta.get("model_name", "")
        usage = meta.get("token_usage", {})
        return model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if hasattr(output, "llm_output"):
        llm_out = output.llm_output or {}
        model = llm_out.get("model_name", "")
        usage = llm_out.get("token_usage", {})
        return model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    # generations 格式
    if hasattr(output, "generations"):
        for gen_list in output.generations:
            for gen in gen_list:
                info = gen.generation_info or {}
                if "usage_metadata" in info:
                    um = info["usage_metadata"]
                    return info.get("model_name", ""), um.get("input_tokens", 0), um.get("output_tokens", 0)
    return "", 0, 0


def _make_event(event_type: str, data: dict) -> str:
    payload = json.dumps({"event": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


async def sse_event_stream(
    agent: Any,
    user_input: dict,
    thread_config: dict,
    thread_id: str = "",
) -> AsyncIterator[str]:
    """将 LangGraph agent 的 astream_events 转换为 SSE 事件流。

    同时自动记录 LLM Token 消耗和执行轨迹，所有记录绑定 thread_id。
    """
    agent_trace = _get_agent_trace()
    cost_tracker = CostTrackerCallback(log_dir=_data_dir(), thread_id=thread_id)
    set_cost_callback(cost_tracker)

    config = {
        **thread_config,
        "recursion_limit": 50,
        "callbacks": [cost_tracker],
        "configurable": {
            **thread_config.get("configurable", {}),
        },
    }

    node_step = 0
    start_time = time.time()
    yield _make_event("start", {"timestamp": start_time})

    try:
        async for event in agent.astream_events(user_input, config=config, version="v2"):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            # --- 节点进度 ---
            if kind == "on_chain_start" and name in _NODE_LABELS:
                node_step += 1
                agent_trace.record(name, "decision", {"step": node_step, "label": _NODE_LABELS.get(name, name)}, thread_id=thread_id)
                yield _make_event("node_start", {
                    "node": name,
                    "label": _NODE_LABELS.get(name, name),
                    "step": node_step,
                })

            # --- 工具调用 ---
            elif kind == "on_tool_start":
                tool_summary = _summarize_tool_input(name, data.get("input", ""))
                agent_trace.record_tool_call(
                    tool_summary.get("type", name),
                    name,
                    str(data.get("input", ""))[:200],
                    thread_id=thread_id,
                )
                yield _make_event("tool_call", {
                    "tool": name,
                    "input": tool_summary,
                })

            # --- 节点完成 ---
            elif kind == "on_chain_end" and name in _NODE_LABELS:
                yield _make_event("node_complete", {
                    "node": name,
                    "label": _NODE_LABELS.get(name, name),
                })

            # --- 质量评分 ---
            elif kind == "on_custom_event":
                custom_data = event.get("data", {})
                if "quality_score" in name or "quality_score" in str(custom_data):
                    yield _make_event("quality_score", {"score": custom_data})

    except Exception as e:
        set_cost_callback(None)
        yield _make_event("error", {"message": str(e), "stage": "streaming"})
        return  # 异常后不再发送 complete

    set_cost_callback(None)
    agent_trace.save()
    # 不再在此处发送 complete —— 由上层 research.py 根据最终 state 决定发送 complete / human_review_required / error
    return


def _summarize_tool_input(tool_name: str, tool_input: Any) -> dict:
    if tool_name == "tavily_search" and isinstance(tool_input, dict):
        return {"type": "search", "query": tool_input.get("query", str(tool_input))}
    elif tool_name == "think_tool" and isinstance(tool_input, dict):
        return {"type": "think", "reflection": tool_input.get("reflection", str(tool_input))[:200]}
    elif tool_name == "ConductResearch" and isinstance(tool_input, dict):
        return {"type": "research", "topic": tool_input.get("research_topic", str(tool_input))[:200]}
    elif tool_name == "refine_draft_report":
        return {"type": "refine"}
    elif tool_name == "ResearchComplete":
        return {"type": "complete"}
    else:
        return {"type": tool_name, "input": str(tool_input)[:200]}
