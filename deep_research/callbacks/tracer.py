"""Agent 全链路执行轨迹记录。

统一记录 Supervisor 决策、Evaluator 评分、Red Team 批评、Memory 注入等事件。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)


class TraceEvent(BaseModel):
    """单条执行轨迹事件。"""
    thread_id: str = ""
    timestamp: float = Field(default_factory=time.time)
    agent: str = ""
    event_type: str = ""  # decision | tool_call | quality_score | memory_injection | critique
    data: dict = Field(default_factory=dict)


class AgentTrace:
    """收集并导出 Agent 执行轨迹。"""

    def __init__(self, log_dir: str | None = None) -> None:
        self._events: list[TraceEvent] = []
        self._stats: dict[str, dict] = {}  # per-agent stats
        log_dir = log_dir or get_engine_settings().resolved_data_dir
        self._log_path = Path(log_dir) / "agent_trace.jsonl"
        os.makedirs(log_dir, exist_ok=True)

    def record(self, agent: str, event_type: str, data: dict[str, Any] | None = None, thread_id: str = "") -> None:
        """记录一条事件。"""
        event = TraceEvent(thread_id=thread_id, agent=agent, event_type=event_type, data=data or {})
        self._events.append(event)

        # 累积统计
        if agent not in self._stats:
            self._stats[agent] = {}
        if event_type not in self._stats[agent]:
            self._stats[agent][event_type] = 0
        self._stats[agent][event_type] += 1

    def get_by_thread(self, thread_id: str) -> list[TraceEvent]:
        """按 thread_id 过滤事件。"""
        return [e for e in self._events if e.thread_id == thread_id]

    def record_decision(self, agent: str, decision: str, details: dict | None = None, thread_id: str = "") -> None:
        self.record(agent, "decision", {"decision": decision, **(details or {})}, thread_id=thread_id)

    def record_tool_call(self, agent: str, tool_name: str, tool_input: str = "", thread_id: str = "") -> None:
        self.record(agent, "tool_call", {"tool": tool_name, "input": tool_input[:200]}, thread_id=thread_id)

    def record_quality_score(self, score: float, feedback: str, iteration: int, thread_id: str = "") -> None:
        self.record("evaluator", "quality_score", {"score": score, "feedback": feedback, "iteration": iteration}, thread_id=thread_id)

    def record_critique(self, author: str, concern: str, thread_id: str = "") -> None:
        self.record(author, "critique", {"concern": concern[:300]}, thread_id=thread_id)

    def record_memory_injection(self, num_results: int, query: str, thread_id: str = "") -> None:
        self.record("memory_manager", "memory_injection", {"num_results": num_results, "query": query[:100]}, thread_id=thread_id)

    @property
    def quality_scores(self) -> list[dict]:
        """提取 Evaluator 评分变化曲线。"""
        return [
            {"iteration": e.data.get("iteration"), "score": e.data.get("score"), "feedback": e.data.get("feedback")}
            for e in self._events
            if e.event_type == "quality_score"
        ]

    def summary(self) -> str:
        """生成文本摘要。"""
        lines = [
            "\n" + "=" * 50,
            "Agent 执行轨迹 (Execution Trace)",
            "=" * 50,
            f"总事件数: {len(self._events)}",
        ]
        for agent, types in self._stats.items():
            lines.append(f"  {agent}: {dict(types)}")

        scores = self.quality_scores
        if scores:
            lines.append("-" * 50)
            lines.append("Evaluator 评分变化:")
            for s in scores:
                lines.append(f"  Iter {s['iteration']}: {s['score']}/10")

        return "\n".join(lines)

    def save(self) -> None:
        """持久化到 JSONL 文件。"""
        with open(self._log_path, "a", encoding="utf-8") as f:
            for e in self._events:
                f.write(e.model_dump_json() + "\n")
        logger.info("Agent trace saved to %s (%d events)", self._log_path, len(self._events))
        self._events.clear()
