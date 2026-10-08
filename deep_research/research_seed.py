"""Phase 5A：research_seed —— 供给 research 分支的**确定性**研究种子。

为什么是"投影"而不是"再生成"（证据见 Phase 5A 报告的依赖矩阵）：

- 真实 graph 里 **researcher 只看 `research_topic`**（supervisor 派发的主题字符串），
  draft 从不进入子研究者的上下文；
- draft 唯一影响 research 的路径是 supervisor 首轮上下文里的那一条
  「Here is the draft report: <全文>」消息；
- 而 draft 本身只由 research_brief + 模型先验生成（它没有任何检索证据——
  生成时研究还没开始），brief 已经完整包含 objective / 维度 / 范围 / 来源指引；
- 因此 research 真正需要的、brief 里没有的信息 **= 空集**（本轮用反事实实验复核）。

所以最小 seed = brief 的确定性投影 + 一行显式说明（"本轮没有草稿"），
**不新增任何 LLM 调用**，不改写用户/简报的原始意图。

    seed = {
      "seed_version": "seed-lite-v1",
      "objective": <research_brief 原文>,
      "draft_available": False,
    }
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

SEED_VERSION = "seed-lite-v1"

#: 投机研究开关：**默认 on**（Phase 5B 定版：2026-10-07 人工审核后翻默认）。
#: 语义：新 lineage 默认走投机拓扑（Research 在 HITL Accept 前并行执行，
#: 只有被 ACCEPT 的当前 generation 进入下游）。显式 `=off` 一键回滚为
#: Phase 5A 之前的串行语义（在途 lineage 不受影响，按 checkpoint 标记恢复）。
SEED_LITE_ENV = "DR_SEED_LITE_SPECULATIVE"
SEED_LITE_DEFAULT = "on"


def seed_lite_speculative_enabled() -> bool:
    """投机研究（seed-lite）模式是否开启（默认 on；显式 off 可回滚）。

    读取时机：每次构图（`_create_builder`）与每个新 lineage 的起点
    （`write_research_brief`）—— 与 thinking 开关不同，本开关不参与
    import-time 模型构造，因此**不需要重启即可在同一进程内切换**；
    但 benchmark 的指纹/preflight 仍按 env 记录，A/B 两组分别重启 worker 更干净。

    注意：本开关只决定**新** lineage 如何起步；已存在的 lineage 由 checkpoint
    里的 `research_generation` 标记决定语义（见 runner._lineage_is_speculative）。
    """
    return (
        os.environ.get(SEED_LITE_ENV, SEED_LITE_DEFAULT).strip().lower()
        in ("1", "true", "yes", "on")
    )

#: supervisor 首轮看到的种子消息（与旧行为 "Here is the draft report: ..." 对位）
SEED_MESSAGE_TEMPLATE = (
    "Research seed (no draft report is available yet — plan the research from this alone):\n"
    "{objective}"
)


def build_research_seed(research_brief: str) -> dict[str, Any]:
    """把 research_brief 确定性投影成 research_seed（纯函数，无副作用）。"""
    objective = (research_brief or "").strip()
    return {
        "seed_version": SEED_VERSION,
        "objective": objective,
        "draft_available": False,
    }


def render_seed_message(seed: dict[str, Any]) -> str:
    """渲染成 supervisor 首轮消息文本。"""
    return SEED_MESSAGE_TEMPLATE.format(objective=seed.get("objective", ""))


def seed_fingerprint(seed: dict[str, Any]) -> str:
    """种子指纹（实验产物溯源用；同 brief 必得同指纹）。"""
    payload = json.dumps(
        {"v": seed.get("seed_version"), "o": seed.get("objective", "")},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def seed_metrics(seed: dict[str, Any], *, draft_chars: int | None = None) -> dict[str, Any]:
    """尺寸对比（字符数；token 数由调用方用真实 tokenizer 补）。"""
    obj_chars = len(seed.get("objective", ""))
    out = {
        "seed_version": seed.get("seed_version"),
        "seed_objective_chars": obj_chars,
        "seed_message_chars": len(render_seed_message(seed)),
        "seed_fingerprint": seed_fingerprint(seed),
    }
    if draft_chars:
        out["draft_chars"] = draft_chars
        out["size_ratio_draft_over_seed"] = round(draft_chars / max(1, out["seed_message_chars"]), 2)
    return out


# =====================================================================
# Phase 5B：research generation / stale-result fencing
# =====================================================================
#
# 产品语义（已审核）：Research 允许在 HITL Accept **之前** speculative 执行，
# 但 speculative 执行 ≠ 允许进入下游。只有「当前 generation 且已 HITL ACCEPT」
# 的结果可以进入 claim_verification / writer / 持久化。
#
# 实现基于**状态事实**而不是 env 开关：
#   - `research_generation >= 1` ⇔ 这是一条**投机 lineage**（由 write_research_brief
#     在投机模式起点写入 = 1；每次 reject/regenerate 由 regenerate 节点 +1）。
#   - 串行 lineage 从不写该字段（缺省 0）。
#
# 为什么 lineage 语义不读 env：开关只决定「**新** lineage 怎么起步」；已存在的
# lineage 在 restart/resume 时保持自己的语义 —— 这样开关中途翻转（回滚）不会
# 让在途任务跑错拓扑（runner 依据 checkpoint 里的标记选择 resume 拓扑）。
#
# 为什么结果必须放独立字段：`notes` 是 `operator.add` reducer，`[]` 清不掉、
# 回显会翻倍（tests/test_phase5a_langgraph_semantics.py 已锁定）—— 因此
# speculative 结果写入 `speculative_research`，只在 ACCEPT 时由 fence 校验后
# 并入 `notes`；REJECT 后旧 payload 自然被新一代覆盖（stale 永不合并）。


class ResearchFenceError(RuntimeError):
    """ACCEPT 时投机结果与 active generation 不一致。

    correctness 优先于恢复：此类不一致意味着状态被污染（ownership 失控、
    重放异常、人为注入），**必须显式失败**，不得静默用空/旧结果继续下游。
    被 retry.classify 归为领域错误（绝不重试）。
    """


def speculative_lineage(state: Mapping[str, Any] | None) -> bool:
    """当前 checkpoint 是否属于投机（seed-lite）lineage。"""
    if not state:
        return False
    raw = state.get("research_generation", 0)
    try:
        return int(raw or 0) >= 1
    except (TypeError, ValueError):
        return False


def active_generation(state: Mapping[str, Any] | None) -> int:
    try:
        return int((state or {}).get("research_generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_speculative_payload(
    *, generation: int, research_brief: str, notes: list[str]
) -> dict[str, Any]:
    """构造隔离的投机研究结果（带 generation 标签，可被 fence 校验）。"""
    return {
        "generation": int(generation),
        "seed_fingerprint": seed_fingerprint(build_research_seed(research_brief)),
        "notes": list(notes),
    }


def quarantine_update(state: Mapping[str, Any] | None, final_notes: list[str]) -> dict[str, Any]:
    """研究分支收尾时的落点选择（supervisor 子图两条退出路径共用）。

    - 串行 lineage：写入 `notes`（历史行为，逐字不变）；
    - 投机 lineage：写入 `speculative_research`（隔离；ACCEPT 前不进入下游）。
    """
    if speculative_lineage(state):
        return {
            "speculative_research": build_speculative_payload(
                generation=active_generation(state),
                research_brief=(state or {}).get("research_brief", "") or "",
                notes=final_notes,
            )
        }
    return {"notes": final_notes}


def fenced_accept_update(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """HITL ACCEPT（或 auto-accept）时把当前 generation 的研究结果并入下游。

    仅当 `payload.generation == active_generation` 时合并；否则抛
    :class:`ResearchFenceError`。晚到的 stale 结果（未通过校验）永远不会
    进入 `notes` / claim_verification / writer。
    """
    active = active_generation(state)
    payload = (state or {}).get("speculative_research") or {}
    got = payload.get("generation")
    if active < 1 or got != active:
        raise ResearchFenceError(
            "stale speculative research rejected: "
            f"active_generation={active} payload_generation={got} "
            f"payload_notes={len(payload.get('notes') or [])}"
        )
    return {"notes": list(payload.get("notes") or [])}
