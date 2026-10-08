"""Baseline 观测：结构化 metric 采集与聚合（Phase 2 基线阶段）。

设计约束（见任务包 Phase 1）：

* **旁路观测**——指标写入失败绝不影响 Deep Research 任务本身；
  每个公开方法自带异常隔离，出错只记 warning。
* **不调用额外 LLM / Search**，不改变 graph 行为、retry、timeout、routing。
* **不伪造数据**——拿不到的字段写 null，并标明来源；不猜精确值。

采集点：

```text
LLM 调用     LangChain callback（on_chat_model_start / on_llm_end / on_llm_error）
Node 耗时    TaskRunner._stream 中既有的 _NODE_LABELS 事件检测点
Search       deep_research.tools.tool.tavily_search_multiple（唯一搜索入口）
Reliability  claim / heartbeat / reconciler 的旁路 hook
```

原始数据落盘目录：``<data_dir>/baseline_metrics/<run_id>/``
聚合产物目录：``<project_root>/artifacts/baseline/<run_id>/``
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler

from deep_research import logging as dr_logging
from deep_research.model_capabilities import (
    local_backend_info as _local_backend_info,
    probe_local_context_limit,
)
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

RAW_METRICS_DIRNAME = "baseline_metrics"
ARTIFACT_ROOT_DIRNAME = "artifacts"

# 环境变量：本地模型 context limit 覆盖值（观测用，不改任何业务行为）。
# 正常情况下由 vLLM /v1/models 探测得到，或从 overflow 错误消息解析。
LOCAL_LIMIT_ENV = "DR_BASELINE_LOCAL_CONTEXT_LIMIT"

_ROW_MAXLEN = 500  # error_message / query 等字段截断长度


# ===== 错误分类 =====

_RE_CONTEXT_LIMIT = re.compile(r"maximum context length is (\d+) tokens", re.IGNORECASE)
_RE_INPUT_AT_LEAST = re.compile(
    r"(?:prompt contains at least|contains at least|at least) (\d+) input tokens",
    re.IGNORECASE,
)
_RE_INPUT_VALUE_PARAM = re.compile(r"value=(\d+)\)?\s*$")


def classify_llm_error(exc: BaseException) -> dict[str, Any]:
    """把 LLM 调用异常映射为结构化字段。

    返回 ``error_type`` 及（当可解析时）context limit / input token 下限。
    """
    message = f"{type(exc).__name__}: {exc}"
    lower = message.lower()

    info: dict[str, Any] = {
        "error_type": "other",
        "error_message": message[:_ROW_MAXLEN],
        "context_limit": None,
        "context_limit_source": None,
        "input_tokens_lower_bound": None,
    }

    if "maximum context length" in lower:
        info["error_type"] = "context_overflow"
        m = _RE_CONTEXT_LIMIT.search(message)
        if m:
            info["context_limit"] = int(m.group(1))
            info["context_limit_source"] = "error_message"
        m2 = _RE_INPUT_AT_LEAST.search(message)
        if m2:
            info["input_tokens_lower_bound"] = int(m2.group(1))
        else:
            # vLLM 新格式：... (parameter=input_tokens, value=8193)
            m3 = _RE_INPUT_VALUE_PARAM.search(message)
            if m3 and "input_tokens" in message:
                info["input_tokens_lower_bound"] = int(m3.group(1))
        return info

    if "timeout" in lower or "timed out" in lower:
        info["error_type"] = "timeout"
    elif "rate limit" in lower or "ratelimit" in lower or "429" in message:
        info["error_type"] = "rate_limit"
    elif "connection" in lower or "connect" in lower and "refused" in lower:
        info["error_type"] = "connection"
    elif "401" in message or "403" in message or "unauthorized" in lower or "api key" in lower:
        info["error_type"] = "auth"
    elif "500" in message or "502" in message or "503" in message or "server error" in lower:
        info["error_type"] = "server_error"

    return info


# ===== Context limit 解析 =====
#
# 探测实现集中在 deep_research.model_capabilities（业务侧 context budget 共用），
# 本模块只保留 Phase 1 的公开名与调用方式。


# ===== 采集上下文 =====

_current_collector: contextvars.ContextVar["BaselineMetricsCollector | None"] = (
    contextvars.ContextVar("baseline_collector", default=None)
)


def set_baseline_collector(collector: "BaselineMetricsCollector | None") -> None:
    _current_collector.set(collector)


def get_baseline_collector() -> "BaselineMetricsCollector | None":
    return _current_collector.get(None)


@dataclass
class RunContext:
    """一次 task attempt 的观测上下文。"""

    run_id: str
    task_id: str
    attempt: int = 0
    variant: str = "v1_baseline"
    worker_id: str = ""

    def to_fields(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "variant": self.variant,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
        }


def default_raw_root() -> Path:
    return get_engine_settings().resolved_data_dir / RAW_METRICS_DIRNAME


_RE_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def safe_run_id(run_id: str) -> str:
    """run_id 会作为目录名使用 —— 拒绝路径逃逸字符（防御 API 侧输入）。"""
    value = str(run_id or "")
    if _RE_SAFE_RUN_ID.fullmatch(value) and value not in (".", ".."):
        return value
    return "invalid-" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def raw_dir_for(run_id: str) -> Path:
    return default_raw_root() / safe_run_id(run_id)


def append_reliability_event(
    run_id: str,
    task_id: str,
    event: str,
    *,
    attempt: int = 0,
    worker_id: str = "",
    raw_root: Path | str | None = None,
    **details: Any,
) -> None:
    """在 collector 生命周期之外追加一条 reliability 事件（旁路）。

    供 worker / reconciler 记录 claim 抢不到、recovery 重新入队等事件。
    任何失败只记 debug 日志。
    """
    try:
        root = Path(raw_root) if raw_root is not None else default_raw_root()
        directory = root / safe_run_id(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        row = {
            "run_id": run_id,
            "task_id": task_id,
            "variant": "v1_baseline",
            "attempt": attempt,
            "worker_id": worker_id,
            "event": event,
            "timestamp": time.time(),
            "details": details,
        }
        with open(directory / _METRIC_FILES["reliability_events"], "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("reliability event 追加失败（已忽略）: %s", exc)


# ===== Collector =====

_METRIC_FILES = {
    "llm_calls": "llm_calls.jsonl",
    "node_metrics": "node_metrics.jsonl",
    "search_metrics": "search_metrics.jsonl",
    "budget_events": "budget_events.jsonl",
    "reliability_events": "reliability_events.jsonl",
}


class BaselineMetricsCollector(BaseCallbackHandler):
    """采集单个 run 的 LLM / node / search / reliability 指标。

    作为 LangChain callback 挂在 graph 调用 config 上；其它采集点
    （search / node / reliability）通过 :func:`get_baseline_collector` 取到实例。
    """

    def __init__(self, context: RunContext, raw_root: Path | str | None = None) -> None:
        super().__init__()
        self.context = context
        self._root = Path(raw_root) if raw_root is not None else default_raw_root()
        self._dir = self._root / safe_run_id(context.run_id)
        self._lock = threading.Lock()
        self._starts: dict[str, dict[str, Any]] = {}
        self._last_node: str = "unknown"
        self._closed = False

        self._local_limit: int | None = None
        self._local_limit_resolved = False

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("baseline metrics 目录不可写（观测降级）: %s", exc)

    # ------------------------------------------------------------------
    # 写入基础设施（异常隔离：绝不向外抛）
    # ------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._dir

    def _append(self, kind: str, row: dict[str, Any]) -> None:
        try:
            payload = {**self.context.to_fields(), **row}
            line = json.dumps(payload, ensure_ascii=False, default=str)
            with self._lock:
                with open(self._dir / _METRIC_FILES[kind], "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 —— 观测失败绝不影响业务
            logger.debug("baseline metric 写入失败（已忽略）: %s", exc)

    def write_meta(self, data: dict[str, Any]) -> None:
        """写 run 级元数据（不覆盖，追加为单文件 JSON）。"""
        try:
            with self._lock:
                (self._dir / "meta.json").write_text(
                    json.dumps({**self.context.to_fields(), **data}, ensure_ascii=False, default=str, indent=2),
                    encoding="utf-8",
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("baseline meta 写入失败（已忽略）: %s", exc)

    # ------------------------------------------------------------------
    # LLM callbacks
    # ------------------------------------------------------------------

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: Any,
        *,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            if run_id is None:
                return
            meta = dict(metadata or {})
            invocation = kwargs.get("invocation_params") or {}
            node = meta.get("langgraph_node") or "unknown"
            self.note_current_node(node)
            self._starts[str(run_id)] = {
                "started_at": time.time(),
                "parent_call_id": str(parent_run_id) if parent_run_id else None,
                "node": node,
                "checkpoint_ns": str(meta.get("langgraph_checkpoint_ns") or "")[:300] or None,
                "role": meta.get("dr_role"),
                "provider": meta.get("dr_backend") or _infer_provider(meta),
                "model": meta.get("dr_handle") or invocation.get("model") or invocation.get("model_name"),
                "thinking": meta.get("dr_thinking"),
                "message_count": len(messages[0]) if messages else None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_chat_model_start 采集失败（已忽略）: %s", exc)

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """记录工具运行的所在 node，供其内部 search 等副作用归因。"""
        try:
            node = (metadata or {}).get("langgraph_node")
            self.note_current_node(str(node)) if node else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_tool_start 采集失败（已忽略）: %s", exc)

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            start = self._starts.pop(str(run_id), {}) if run_id is not None else {}
            started_at = start.get("started_at", time.time())
            finished_at = time.time()

            model_name, input_tokens, output_tokens, token_source, reasoning_tokens = _extract_usage(response)
            model = start.get("model") or model_name
            limit, limit_source = self._context_limit_for(start.get("provider"), start.get("model"))

            utilization = None
            if input_tokens is not None and limit:
                utilization = round(input_tokens / limit, 4)

            content_tokens = None
            if output_tokens is not None and reasoning_tokens is not None:
                content_tokens = max(output_tokens - reasoning_tokens, 0)

            self._append("llm_calls", {
                "call_id": str(run_id) if run_id else None,
                "parent_call_id": start.get("parent_call_id"),
                "node": start.get("node", "unknown"),
                "checkpoint_ns": start.get("checkpoint_ns"),
                "role": start.get("role"),
                "provider": start.get("provider"),
                "model": model,
                "thinking": start.get("thinking"),
                "started_at": started_at,
                "finished_at": finished_at,
                "latency_ms": int((finished_at - started_at) * 1000),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "content_tokens": content_tokens,
                "token_source": token_source,
                "context_limit": limit,
                "context_limit_source": limit_source,
                "context_utilization": utilization,
                "success": True,
                "error_type": None,
                "error_message": None,
                "overflow_tokens": None,
                "input_tokens_lower_bound": None,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_llm_end 采集失败（已忽略）: %s", exc)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            start = self._starts.pop(str(run_id), {}) if run_id is not None else {}
            started_at = start.get("started_at", time.time())
            finished_at = time.time()

            info = classify_llm_error(error)
            limit = info.get("context_limit")
            limit_source = info.get("context_limit_source")
            if limit is None:
                limit, limit_source = self._context_limit_for(start.get("provider"), start.get("model"))

            lower_bound = info.get("input_tokens_lower_bound")
            overflow_tokens = None
            if lower_bound is not None and limit:
                overflow_tokens = max(lower_bound - limit, 0)

            self._append("llm_calls", {
                "call_id": str(run_id) if run_id else None,
                "parent_call_id": start.get("parent_call_id"),
                "node": start.get("node", "unknown"),
                "checkpoint_ns": start.get("checkpoint_ns"),
                "role": start.get("role"),
                "provider": start.get("provider"),
                "model": start.get("model"),
                "thinking": start.get("thinking"),
                "started_at": started_at,
                "finished_at": finished_at,
                "latency_ms": int((finished_at - started_at) * 1000),
                "input_tokens": None,          # 失败调用拿不到 provider usage
                "output_tokens": None,
                "reasoning_tokens": None,
                "content_tokens": None,
                "token_source": None,
                "context_limit": limit,
                "context_limit_source": limit_source,
                "context_utilization": None,   # 无精确 input_tokens 时不计算
                "success": False,
                "error_type": info["error_type"],
                "error_message": info["error_message"],
                "overflow_tokens": overflow_tokens,
                "input_tokens_lower_bound": lower_bound,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("on_llm_error 采集失败（已忽略）: %s", exc)

    # ------------------------------------------------------------------
    # Node / Search / Reliability
    # ------------------------------------------------------------------

    def record_node_metric(
        self,
        node: str,
        langgraph_run_id: str,
        started_at: float,
        finished_at: float | None,
        success: bool | None,
        error_type: str | None = None,
    ) -> None:
        """记录一个 graph node 的执行（完成时一行）。

        ``finished_at=None`` 表示该 node 未正常收尾（任务中途失败/取消）——
        照实记录，不补一个假结束时间。
        """
        try:
            latency = int((finished_at - started_at) * 1000) if finished_at else None
            self._append("node_metrics", {
                "node": node,
                "langgraph_run_id": langgraph_run_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "latency_ms": latency,
                "success": success,
                "error_type": error_type,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("node metric 采集失败（已忽略）: %s", exc)

    def record_search(
        self,
        *,
        provider: str,
        query: str,
        topic: str | None,
        max_results: int | None,
        started_at: float,
        finished_at: float,
        success: bool,
        result_count: int | None = None,
        error_message: str | None = None,
        node: str | None = None,
        query_hash: str | None = None,
        url_keys: list[str] | None = None,
        content_sha1s: list[str] | None = None,
    ) -> None:
        try:
            self._append("search_metrics", {
                "node": node or self.current_node(),
                "provider": provider,
                "query": (query or "")[:_ROW_MAXLEN],
                "query_hash": query_hash,
                "topic": topic,
                "max_results": max_results,
                "started_at": started_at,
                "finished_at": finished_at,
                "latency_ms": int((finished_at - started_at) * 1000),
                "success": success,
                "result_count": result_count,
                "url_keys": url_keys,
                "content_sha1s": content_sha1s,
                "error_message": error_message,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("search metric 采集失败（已忽略）: %s", exc)

    def record_budget(self, **fields: Any) -> None:
        """记录一次 context budget 决策（P1）。

        字段由调用方给出（original/final/trimmed tokens、limit、reserve、
        safety、truncation reason 等），本方法只负责旁路落盘。
        """
        try:
            row = {k: v for k, v in fields.items()}
            row.setdefault("node", self.current_node())
            self._append("budget_events", row)
        except Exception as exc:  # noqa: BLE001
            logger.debug("budget event 采集失败（已忽略）: %s", exc)

    def record_reliability(self, event: str, **details: Any) -> None:
        try:
            self._append("reliability_events", {
                "event": event,
                "timestamp": time.time(),
                "details": details,
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("reliability event 采集失败（已忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def current_node(self) -> str:
        """最近一次 LLM / tool 运行所属 node（search 等非 callback 场景的近似归因）。

        取不到时返回 "unknown" —— 不猜。
        """
        return self._last_node or "unknown"

    def note_current_node(self, node: str) -> None:
        """由 on_chat_model_start / on_tool_start 维护的「最近节点」。"""
        try:
            if node and node != "unknown":
                self._last_node = node
        except Exception:
            pass

    def _context_limit_for(self, provider: str | None, model: str | None) -> tuple[int | None, str | None]:
        """返回 (context_limit, source)。未知则 (None, None)。"""
        if provider != "openai_local":
            return None, None

        env_value = os.environ.get(LOCAL_LIMIT_ENV)
        if env_value:
            try:
                return int(env_value), "env_override"
            except ValueError:
                pass

        if not self._local_limit_resolved:
            base_url, api_key = _local_backend_info()
            self._local_limit = probe_local_context_limit(base_url, api_key)
            self._local_limit_resolved = True

        if self._local_limit:
            return self._local_limit, "vllm_probe"
        return None, None


def _infer_provider(metadata: dict[str, Any]) -> str | None:
    """无 dr_backend 标记时，按 ls_provider 兜底（主要服务离线测试）。"""
    ls_provider = str(metadata.get("ls_provider") or "")
    if ls_provider == "fakechatmodel":
        return "fake"
    if ls_provider:
        return ls_provider
    return None


def _extract_reasoning_tokens(details: Any) -> int | None:
    """从 usage 明细中提取 reasoning token 数（provider 提供时）。

    DashScope/OpenAI 兼容层的位置：``completion_tokens_details.reasoning_tokens``
    或 ``usage_metadata.output_token_details.reasoning``。
    """
    if not isinstance(details, dict):
        return None
    for key in ("reasoning", "reasoning_tokens"):
        value = details.get(key)
        if isinstance(value, int):
            return value
    return None


def _extract_usage(
    response: Any,
) -> tuple[str | None, int | None, int | None, str | None, int | None]:
    """从 LLMResult 提取 (model_name, input_tokens, output_tokens, token_source, reasoning_tokens)。"""
    model_name: str | None = None
    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        model_name = llm_output.get("model_name")
        usage = llm_output.get("token_usage")
        if isinstance(usage, dict) and ("prompt_tokens" in usage or "completion_tokens" in usage):
            reasoning = _extract_reasoning_tokens(usage.get("completion_tokens_details"))
            return (
                model_name,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                "provider_usage",
                reasoning,
            )

    generations = getattr(response, "generations", None) or []
    for gen_list in generations:
        for gen in gen_list:
            info = getattr(gen, "generation_info", None) or {}
            if model_name is None and isinstance(info, dict):
                model_name = info.get("model_name") or info.get("model")
            usage = info.get("usage_metadata") if isinstance(info, dict) else None
            if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
                return (
                    model_name,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    "provider_usage",
                    _extract_reasoning_tokens(usage.get("output_token_details")),
                )
            message = getattr(gen, "message", None)
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
                response_meta = getattr(message, "response_metadata", None) or {}
                if model_name is None and isinstance(response_meta, dict):
                    model_name = response_meta.get("model_name") or response_meta.get("model")
                return (
                    model_name,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    "provider_usage",
                    _extract_reasoning_tokens(usage.get("output_token_details")),
                )
    return model_name, None, None, None, None


__all__ = [
    "BaselineMetricsCollector",
    "LOCAL_LIMIT_ENV",
    "RunContext",
    "append_reliability_event",
    "classify_llm_error",
    "default_raw_root",
    "get_baseline_collector",
    "probe_local_context_limit",
    "raw_dir_for",
    "safe_run_id",
    "set_baseline_collector",
]
