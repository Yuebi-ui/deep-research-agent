"""成本追踪回调：记录 Token 消耗和成本估算。

基于 LangChain BaseCallbackHandler，Hook LLM 调用结束事件提取 usage 信息。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from langchain_core.callbacks.base import BaseCallbackHandler
from uuid import UUID

from langchain_core.outputs import LLMResult

import contextvars

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

_current_callback: contextvars.ContextVar = contextvars.ContextVar("cost_callback", default=None)


def set_cost_callback(cb: CostTrackerCallback | None) -> None:
    _current_callback.set(cb)


def get_cost_callback() -> CostTrackerCallback | None:
    return _current_callback.get(None)

# Qwen3 模型单价（DashScope，每百万 token 价格：RMB）
# 来源：https://help.aliyun.com/zh/model-studio/getting-started/models
_MODEL_PRICE_PER_M_TOKEN = {
    # 以下均在阿里百炼有 100 万 token 免费额度
    "deepseek-r1": {"input": 4.0, "output": 16.0},
    "deepseek-v4-pro": {"input": 2.0, "output": 8.0},
    "deepseek-v4-flash": {"input": 0.5, "output": 2.0},
    # 旧 Qwen 模型（保留备用）
    "qwen3-8b": {"input": 0.5, "output": 2.0},
    "qwen3-235b-a22b": {"input": 4.0, "output": 16.0},
    "qwen3-32b": {"input": 3.5, "output": 7.0},
}

_DEFAULT_PRICE = {"input": 0.0, "output": 0.0}


class CostTrackerCallback(BaseCallbackHandler):
    """追踪每次 LLM 调用的 Token 消耗和估算费用。"""

    def __init__(self, log_dir: str | None = None, thread_id: str = "") -> None:
        super().__init__()
        self._records: list[dict] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost = 0.0
        self._per_model: dict[str, dict] = {}
        self._thread_id = thread_id
        log_dir = log_dir or get_engine_settings().resolved_data_dir
        self._log_path = Path(log_dir) / "cost_log.jsonl"
        os.makedirs(log_dir, exist_ok=True)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None, **kwargs) -> None:
        """LLM 调用结束后提取 usage 并计算成本。"""
        model_name = self._extract_model(response)
        input_tokens, output_tokens = self._extract_tokens(response)

        if input_tokens == 0 and output_tokens == 0:
            return

        price = _MODEL_PRICE_PER_M_TOKEN.get(model_name, _DEFAULT_PRICE)
        cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000

        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        self._total_cost += cost

        if model_name not in self._per_model:
            self._per_model[model_name] = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        self._per_model[model_name]["input_tokens"] += input_tokens
        self._per_model[model_name]["output_tokens"] += output_tokens
        self._per_model[model_name]["cost"] += cost

        record = {
            "thread_id": self._thread_id,
            "timestamp": time.time(),
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_rmb": round(cost, 6),
        }
        self._records.append(record)
        self._write_log(record)

    def _extract_model(self, response: LLMResult) -> str:
        """从 LLMResult 中提取模型名。"""
        if response.llm_output and response.llm_output.get("model_name"):
            return response.llm_output["model_name"]
        for gen_list in response.generations:
            for gen in gen_list:
                resp = gen.generation_info or {}
                if resp.get("model_name"):
                    return resp["model_name"]
        return "unknown"

    def _extract_tokens(self, response: LLMResult) -> tuple[int, int]:
        """从 LLMResult 中提取 input/output token 数。"""
        if response.llm_output and "token_usage" in response.llm_output:
            usage = response.llm_output["token_usage"]
            return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        for gen_list in response.generations:
            for gen in gen_list:
                info = gen.generation_info or {}
                if "usage_metadata" in info:
                    um = info["usage_metadata"]
                    return um.get("input_tokens", 0), um.get("output_tokens", 0)
        return 0, 0

    def _write_log(self, record: dict) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def summary(self) -> str:
        """生成成本汇总报告。"""
        lines = [
            "\n" + "=" * 50,
            "成本汇总 (Cost Summary)",
            "=" * 50,
            f"总输入 Token:  {self._total_input_tokens:>10,}",
            f"总输出 Token:  {self._total_output_tokens:>10,}",
            f"总 Token:      {self._total_input_tokens + self._total_output_tokens:>10,}",
            f"总费用 (RMB):  ¥{self._total_cost:>10.4f}",
            "-" * 50,
        ]
        for model, stats in self._per_model.items():
            lines.append(
                f"  {model}: in={stats['input_tokens']:,} out={stats['output_tokens']:,} cost=¥{stats['cost']:.4f}"
            )
        lines.append("=" * 50)
        summary = "\n".join(lines)
        logger.info(summary)
        return summary

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def total_tokens(self) -> int:
        return self._total_input_tokens + self._total_output_tokens
