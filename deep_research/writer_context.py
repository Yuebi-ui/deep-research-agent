"""Final Writer 上下文组装：无损去重、证据来源保留、可选 token budget。

不进行 LLM 总结，不把相似但可能冲突的研究结论合并成一个事实。
上下文窗口未知时保留原有报告 Prompt 语义；已知窗口时利用现有
context_budget.fit_prompt_for_role 共同管理草稿与研究记录的预算。
"""

from __future__ import annotations

import re
import os
from typing import Any

from deep_research.prompts.final_report import FINAL_REPORT_PROMPT


def unique_notes(notes: list[Any] | None) -> list[str]:
    """仅折叠空白相同的重复笔记；不同版本的数值、链接不可去重。"""
    kept: list[str] = []
    seen: set[str] = set()
    for note in notes or []:
        text = note if isinstance(note, str) else str(note)
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        kept.append(text.strip())
    return kept


def build_final_report_prompt(
    *, research_brief: str, draft_report: str, notes: list[Any] | None,
    date: str, verification_warning: str = "",
) -> tuple[str, dict]:
    """返回最终 Writer Prompt 与可观测决策；不做隐式 URL 伪造。

    云端上下文未知时沿用历史模板；本地已知上下文限制时，将 Draft
    与 Notes 放入同一可裁剪块，确保两者都参与总 token 上限计算。
    """
    unique = unique_notes(notes)
    findings = "\n\n".join(unique)
    original = FINAL_REPORT_PROMPT.format(
        research_brief=research_brief, findings=findings,
        draft_report=draft_report, date=date,
    ) + verification_warning
    stats: dict = {"notes_total": len(notes or []), "notes_kept": len(unique), "budget": "unknown"}

    # 兼容回滚开关：强制关闭时保留旧 Writer 上下文语义（仍去除精确重复）。
    if os.getenv("DR_WRITER_CONTEXT_BUDGET", "on").strip().lower() in {"off", "false", "0"}:
        stats["budget"] = "disabled"
        return original, stats

    # 延迟导入，不改变远端 writer 角色模型路由和 import 阶段成本。
    try:
        from deep_research.context_budget import fit_prompt_for_role

        blocks = [f"<报告草稿>\n{draft_report}\n</报告草稿>"] if draft_report else []
        blocks.extend(
            f"<研究笔记 index=\"{idx}\">\n{note}\n</研究笔记>"
            for idx, note in enumerate(unique, 1)
        )
        combined = "\n\n".join(blocks)

        def build(content: str) -> str:
            return FINAL_REPORT_PROMPT.format(
                research_brief=research_brief, findings=content,
                draft_report="（已合并在上方调查结果中；按 <报告草稿> 标记识别）",
                date=date,
            ) + verification_warning

        fitted, decision = fit_prompt_for_role(
            role="writer", build_prompt=build,
            content=combined, query=research_brief,
        )
        stats["budget"] = decision.to_fields()
        if decision.reason == "limit_unknown":
            return original, stats
        return fitted, stats
    except Exception as exc:
        # budget 配置/探测出错不能阻断研究报告。
        stats["budget"] = {"reason": "error", "detail": type(exc).__name__}
        return original, stats
