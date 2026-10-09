"""Context Budget（Phase 2 / P1）：请求发出前的确定性 token 预算。

目标（不是「捕获 400 后重试」）：

```text
prompt_tokens + reserved_output_tokens + safety_margin <= model_context_limit
```

设计要点：

* **limit 集中解析**（config → 环境覆盖 → vLLM 探测，见 model_capabilities），
  业务代码不硬编码 8192；拿不到 limit（如云端）时**不裁剪**（不误伤）。
* **tokenizer 尽量与真实模型一致**：优先加载本地模型目录的 tokenizer.json
  （tokenizers 库，无 transformers 依赖）；不可用时退化为保守启发式并**记录**。
* **content-aware trimming**：长网页按段落切块，按与 query/claim 的词面相关性
  （字符 bigram 重叠，确定性、无额外 LLM）贪心保留，**保持原始顺序**重排，
  仅在丢弃处插入显式裁剪标记 —— 不做字符串尾部粗暴截断。
* 决策全量记录（供给 baseline_metrics 的 budget_events.jsonl）。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from deep_research import logging as dr_logging
from deep_research.model_capabilities import (
    local_backend_info,
    probe_local_model,
    resolve_tokenizer_path,
)
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

# 默认预算参数（可被 stage 级 config: context_budget.{reserve_output_tokens,safety_margin_tokens} 覆盖）
DEFAULT_RESERVE_OUTPUT_TOKENS = 1024
DEFAULT_SAFETY_MARGIN_TOKENS = 256

# 本地 limit 的环境覆盖（Phase 1 观测沿用同名变量；优先级高于探测）
LOCAL_LIMIT_ENV = "DR_BASELINE_LOCAL_CONTEXT_LIMIT"

# 单块过大时的二次切分窗口（字符）—— 仅用于极端单段网页
_MAX_CHUNK_CHARS = 4000

_TRIM_MARKER = "\n\n[... 内容过长，已按时序保留 {kept}/{total} 段高相关片段 ...]\n\n"


# ===== token 计数 =====


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class _HeuristicCounter:
    """无 tokenizer 时的保守近似：约 2 字符 = 1 token。

    误差策略（保守方向）：中文 ≈ 1 token/字、英文 ≈ 1 token/4 字符，
    取 1/2 是折中；对英文是**高估**（更保守、可能多裁），对高密度中文是低估，
    因此默认 safety margin（256 tokens）用于吸收剩余误差，并记录
    ``counter="heuristic_chars_div_2"`` 以便审计。
    """

    name = "heuristic_chars_div_2"

    def count(self, text: str) -> int:
        return math.ceil(len(text) / 2)


class _TokenizerCounter:
    def __init__(self, path: str) -> None:
        from tokenizers import Tokenizer  # 延迟导入：缺失时自动走启发式

        self._tokenizer = Tokenizer.from_file(path)
        self.name = f"tokenizer:{Path(path).parent.name}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)


_counter_cache: dict[str, TokenCounter] = {}


def get_token_counter(model_root: str | None = None) -> TokenCounter:
    """优先真实 tokenizer，不可用时保守启发式。"""
    path = resolve_tokenizer_path(model_root)
    if path:
        cached = _counter_cache.get(path)
        if cached is not None:
            return cached
        try:
            counter = _TokenizerCounter(path)
            _counter_cache[path] = counter
            return counter
        except Exception as exc:  # noqa: BLE001  # 计数失败不影响总结本身
            logger.warning("tokenizer 加载失败，退化为启发式计数: %s", exc)
    return _HeuristicCounter()


def reset_counter_cache() -> None:
    _counter_cache.clear()


# ===== limit 解析 =====


@dataclass(frozen=True)
class BudgetLimits:
    context_limit: int
    limit_source: str  # config / env_override / vllm_probe
    reserved_output: int
    safety_margin: int
    counter_name: str


def _role_target(role: str, stage: str | None = None) -> tuple[str | None, str | None, dict]:
    """role → (backend, handle, stage_cfg)。失败返回 (None, None, {})。"""
    try:
        from deep_research.utils import load_config

        cfg = load_config(stage_name=stage or get_engine_settings().stage)
        role_cfg = (cfg.get("roles") or {}).get(role) or {}
        return role_cfg.get("backend"), role_cfg.get("handle"), cfg
    except Exception as exc:  # noqa: BLE001
        logger.debug("role 配置解析失败: %s", exc)
        return None, None, {}


def _budget_params(stage_cfg: dict) -> tuple[int, int]:
    block = (stage_cfg.get("context_budget") or {}) if isinstance(stage_cfg, dict) else {}
    reserve = block.get("reserve_output_tokens")
    safety = block.get("safety_margin_tokens")
    return (
        int(reserve) if isinstance(reserve, int) and reserve >= 0 else DEFAULT_RESERVE_OUTPUT_TOKENS,
        int(safety) if isinstance(safety, int) and safety >= 0 else DEFAULT_SAFETY_MARGIN_TOKENS,
    )


def resolve_budget_limits(role: str, *, stage: str | None = None) -> BudgetLimits | None:
    """解析某 role 的 context budget。

    limit 来源优先级：

    ```text
    1. config.yml: cognition.<backend>.models.<handle>.context_window（集中配置）
    2. 环境变量 DR_BASELINE_LOCAL_CONTEXT_LIMIT（部署覆盖，仅本地 backend）
    3. vLLM /v1/models 探测（仅本地 backend）
    未知（如云端 DashScope）→ None：**不裁剪**，不按本地 8192 误伤云端
    """
    backend, handle, cfg = _role_target(role, stage)
    if not backend or not handle:
        return None

    reserve, safety = _budget_params(cfg)
    cognition = (cfg.get("cognition") or {}) if isinstance(cfg, dict) else {}
    backend_cfg = cognition.get(backend) or {}
    model_cfg = ((backend_cfg.get("models") or {}).get(handle)) or {}

    limit: int | None = None
    source = ""
    configured = model_cfg.get("context_window") or model_cfg.get("max_model_len")
    if isinstance(configured, int) and configured > 0:
        limit, source = configured, "config"

    if limit is None and backend == "openai_local":
        env_value = os.environ.get(LOCAL_LIMIT_ENV)
        if env_value:
            try:
                limit, source = int(env_value), "env_override"
            except ValueError:
                pass

    if limit is None and backend == "openai_local":
        base_url, api_key = local_backend_info()
        capability = probe_local_model(base_url, api_key)
        if capability and capability.max_model_len:
            limit, source = capability.max_model_len, "vllm_probe"

    if limit is None:
        return None

    counter = get_token_counter(_local_model_root_if_any(backend))
    return BudgetLimits(
        context_limit=limit,
        limit_source=source,
        reserved_output=reserve,
        safety_margin=safety,
        counter_name=counter.name,
    )


def _local_model_root_if_any(backend: str) -> str | None:
    if backend != "openai_local":
        return None
    base_url, api_key = local_backend_info()
    capability = probe_local_model(base_url, api_key)
    return capability.model_root if capability else None


# ===== 切块与相关性 =====


def split_chunks(content: str) -> list[str]:
    """按空行分段；超长段落再按窗口切分（保持顺序，确定性）。"""
    blocks = [b for b in re.split(r"\n\s*\n", content or "") if b.strip()]
    chunks: list[str] = []
    for block in blocks:
        if len(block) <= _MAX_CHUNK_CHARS:
            chunks.append(block)
            continue
        for i in range(0, len(block), _MAX_CHUNK_CHARS):
            chunks.append(block[i : i + _MAX_CHUNK_CHARS])
    return chunks


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _hard_truncate(text: str, max_tokens: int, counter: TokenCounter) -> str:
    """按 token 预算二分截断（字符级），保证 count(prefix) <= max_tokens。"""
    if max_tokens <= 0:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def relevance_score(chunk: str, query: str) -> float:
    """query 与 chunk 的字符 bigram 重叠（确定性词面相关度）。"""
    if not query:
        return 0.0
    cb = _bigrams(chunk)
    qb = _bigrams(query)
    if not cb or not qb:
        return 0.0
    overlap = len(cb & qb)
    return overlap / (1.0 + math.log(1.0 + len(cb)))


# ===== 预算裁剪 =====


@dataclass
class BudgetDecision:
    applied: bool
    reason: str
    context_limit: int | None = None
    limit_source: str | None = None
    reserved_output: int | None = None
    safety_margin: int | None = None
    template_overhead_tokens: int | None = None
    original_tokens: int | None = None
    final_tokens: int | None = None
    trimmed_tokens: int | None = None
    counter: str | None = None
    budget_tokens: int | None = None
    chunks_total: int | None = None
    chunks_kept: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        out = {
            "applied": self.applied,
            "reason": self.reason,
            "context_limit": self.context_limit,
            "limit_source": self.limit_source,
            "reserved_output": self.reserved_output,
            "safety_margin": self.safety_margin,
            "template_overhead_tokens": self.template_overhead_tokens,
            "original_tokens": self.original_tokens,
            "final_tokens": self.final_tokens,
            "trimmed_tokens": self.trimmed_tokens,
            "counter": self.counter,
            "budget_tokens": self.budget_tokens,
            "chunks_total": self.chunks_total,
            "chunks_kept": self.chunks_kept,
        }
        out.update(self.extra)
        return out


def fit_prompt(
    *,
    build_prompt: Callable[[str], str],
    content: str,
    query: str,
    limits: BudgetLimits,
    counter: TokenCounter,
) -> tuple[str, BudgetDecision]:
    """把 content 裁剪到预算内并组装 prompt。

    预算等式：``prompt <= limit - reserved_output - safety_margin``
    （prompt 用同一 counter 计数，包含模板开销）。

    返回 (prompt, decision)。**本函数不抛异常**——计数/切分失败时原样返回。
    """
    try:
        return _fit_prompt_inner(
            build_prompt=build_prompt, content=content or "", query=query or "",
            limits=limits, counter=counter,
        )
    except Exception as exc:  # noqa: BLE001  # 预算失败不得阻断总结
        logger.warning("context budget 计算失败，跳过裁剪: %s", exc)
        return build_prompt(content or ""), BudgetDecision(applied=False, reason="budget_error")


def _fit_prompt_inner(
    *,
    build_prompt: Callable[[str], str],
    content: str,
    query: str,
    limits: BudgetLimits,
    counter: TokenCounter,
) -> tuple[str, BudgetDecision]:
    template_overhead = counter.count(build_prompt(""))
    hard_ceiling = limits.context_limit - limits.reserved_output - limits.safety_margin
    budget = hard_ceiling - template_overhead

    base = BudgetDecision(
        applied=False,
        reason="fits",
        context_limit=limits.context_limit,
        limit_source=limits.limit_source,
        reserved_output=limits.reserved_output,
        safety_margin=limits.safety_margin,
        template_overhead_tokens=template_overhead,
        counter=counter.name,
        budget_tokens=max(budget, 0),
    )

    if budget <= 0:
        # 预算被 reserve/safety 吃光 —— 只能放弃内容（安全优先）
        prompt = build_prompt("")
        base.applied = True
        base.reason = "budget_exhausted"
        base.original_tokens = counter.count(content)
        base.final_tokens = counter.count(prompt)
        base.trimmed_tokens = base.original_tokens
        base.chunks_total = len(split_chunks(content))
        base.chunks_kept = 0
        return prompt, base

    original_tokens = counter.count(content)
    base.original_tokens = original_tokens
    if original_tokens <= budget:
        prompt = build_prompt(content)
        base.final_tokens = counter.count(prompt)
        base.trimmed_tokens = 0
        base.chunks_total = base.chunks_kept = len(split_chunks(content))
        return prompt, base

    # ---- 需要裁剪：相关度贪心 + 时序重排 ----
    chunks = split_chunks(content)
    if not chunks:
        prompt = build_prompt("")
        base.applied = True
        base.reason = "trimmed_empty"
        base.final_tokens = counter.count(prompt)
        base.trimmed_tokens = original_tokens
        base.chunks_total = base.chunks_kept = 0
        return prompt, base

    token_counts = [counter.count(c) for c in chunks]
    scores = [relevance_score(c, query) for c in chunks]
    order = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))  # 稳定：同分取更早

    marker_reserve = counter.count(_TRIM_MARKER.format(kept=0, total=len(chunks)))
    kept: list[int] = []
    used = marker_reserve
    for idx in order:
        if used + token_counts[idx] <= budget:
            kept.append(idx)
            used += token_counts[idx]
    kept_set = set(kept)
    kept_ordered = [i for i in range(len(chunks)) if i in kept_set]

    # 极端情况：所有块都装不下（例如无分段的长文，单块 > 整个预算）。
    # 退化为「保留最相关块 + token 二分硬截断」，避免整页被丢弃。
    hard_truncated = False
    if not kept_ordered and budget > marker_reserve and chunks:
        best = order[0]
        room = budget - marker_reserve
        truncated = _hard_truncate(chunks[best], room, counter)
        if truncated.strip():
            chunks = list(chunks)
            chunks[best] = truncated
            token_counts[best] = counter.count(truncated)
            kept_ordered = [best]
            hard_truncated = True

    def assemble(keep: list[int]) -> str:
        body = "\n\n".join(chunks[i] for i in keep)
        marker = _TRIM_MARKER.format(kept=len(keep), total=len(chunks)) if len(keep) < len(chunks) else ""
        return marker + body

    # 组装后复核（组装标记/换行的实际计数与预留可能略有出入）——必要时从
    # **相关度最低**的已保留块开始逐个丢弃，直到进入硬上限。
    assembled = assemble(kept_ordered)
    while counter.count(build_prompt(assembled)) > hard_ceiling and kept_ordered:
        # kept_ordered is in *source* order, not score order.  Dropping its
        # final member could discard the highest-ranked passage. Preserve
        # reading order for presentation, but evict by relevance.
        lowest = min(kept_ordered, key=lambda i: (scores[i], -i))
        kept_ordered.remove(lowest)
        assembled = assemble(kept_ordered)

    prompt = build_prompt(assembled)
    final_tokens = counter.count(prompt)
    base.applied = True
    base.reason = "trimmed_hard_chunk" if hard_truncated else "trimmed"
    base.chunks_total = len(chunks)
    base.chunks_kept = len(kept_ordered)
    base.final_tokens = final_tokens
    base.trimmed_tokens = max(original_tokens - (final_tokens - template_overhead), 0)
    if final_tokens > hard_ceiling:
        # 极端情况（例如单块超过整个预算且组装后仍超）—— 记录事实
        base.reason = "trimmed_over_budget"
    return prompt, base


# ===== 业务接线 =====


def fit_prompt_for_role(
    *,
    role: str,
    build_prompt: Callable[[str], str],
    content: str,
    query: str = "",
    stage: str | None = None,
) -> tuple[str, BudgetDecision]:
    """按 role 解析预算并裁剪；拿不到 limit（云端/未知）时不裁剪。"""
    limits = resolve_budget_limits(role, stage=stage)
    if limits is None:
        return build_prompt(content or ""), BudgetDecision(applied=False, reason="limit_unknown")

    counter = get_token_counter(_local_model_root_if_any(_role_backend(role, stage)))
    return fit_prompt(
        build_prompt=build_prompt, content=content, query=query,
        limits=limits, counter=counter,
    )


def _role_backend(role: str, stage: str | None) -> str | None:
    backend, _handle, _cfg = _role_target(role, stage)
    return backend


__all__ = [
    "BudgetDecision",
    "BudgetLimits",
    "DEFAULT_RESERVE_OUTPUT_TOKENS",
    "DEFAULT_SAFETY_MARGIN_TOKENS",
    "LOCAL_LIMIT_ENV",
    "TokenCounter",
    "fit_prompt",
    "fit_prompt_for_role",
    "get_token_counter",
    "relevance_score",
    "reset_counter_cache",
    "resolve_budget_limits",
    "split_chunks",
]
