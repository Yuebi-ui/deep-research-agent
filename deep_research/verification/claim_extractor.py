"""从报告草稿中提取关键事实 Claim。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from deep_research.llm import claim_verify_thinking, get_chat_model_for_task
from deep_research.utils import parse_json_response
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

CLAIM_EXTRACT_PROMPT = """你是一名事实核查员。从以下报告草稿中提取所有**可验证的关键事实断言**。

规则：
1. 只提取包含具体数据、结论、因果关系的陈述（如"FAISS 存储成本降低 68%"、"MemGPT 发表于 2023 年"）
2. 忽略主观评价和概括性陈述（如"值得深入研究"、"总体而言"）
3. 每条 Claim 标注其在报告中的引用标记（如 [1], [3]），如果没有明确出处则填 "none"
4. 最多提取 10 条最重要的 Claim

返回 JSON 格式：
```json
[{{"text": "断言内容", "source_tag": "[1] 或 none"}}]
```

报告草稿：
{draft_report}
"""


class ClaimExtractor:
    """从报告草稿中提取关键事实断言。"""

    def __init__(self) -> None:
        # E1a：仅此调用点受 DR_CLAIM_VERIFY_THINKING 控制（默认 on）
        self._model = get_chat_model_for_task("extracting", thinking=claim_verify_thinking())

    def extract(self, draft_report: str) -> list[dict]:
        """提取 Claim 列表。

        Returns:
            [{"text": "...", "source_tag": "..."}, ...]
        """
        if not draft_report or len(draft_report) < 50:
            return []

        prompt = CLAIM_EXTRACT_PROMPT.format(draft_report=draft_report[:8000])
        response = self._model.invoke([HumanMessage(content=prompt)])
        try:
            claims = parse_json_response(response.content)
            # 标准化：统一转成 list[dict] 格式
            normalized = []
            for c in claims:
                if isinstance(c, str):
                    normalized.append({"text": c, "source_tag": "none"})
                elif isinstance(c, dict):
                    normalized.append(c)
            logger.info("Extracted %d claims from draft report", len(normalized))
            return normalized
        except Exception as e:
            logger.warning("Failed to parse claims: %s", e)
            return []
