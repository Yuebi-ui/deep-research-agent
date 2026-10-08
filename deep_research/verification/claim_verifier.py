"""逐条核查 Claim 的事实依据。并行搜索 + 并行 LLM 判断。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from deep_research.llm import claim_verify_thinking, get_chat_model_for_task
from deep_research.tools.tool import tavily_search
from deep_research.verification.schemas import ClaimVerdict
from deep_research.utils import parse_json_response
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

CLAIM_VERIFY_PROMPT = """你是一名严格的事实核查员。请判断以下断言是否能从搜索结果中找到可靠依据。

断言：{claim_text}

搜索结果：
{search_results}

判断标准：
- SUPPORTED：搜索结果中有明确证据，能直接佐证该断言的核心事实
- PARTIAL：搜索结果部分相关，能找到间接证据或部分讨论，但不足以完全确认
- UNSUPPORTED：搜索结果与断言明显矛盾，或搜索到了相关信息但无法找到任何支撑
- UNVERIFIABLE：仅当断言涉及未公开的内部数据、尚未发生的事件、或无法通过公开信息判断的个人主观感受时使用

关键原则：
- 如果搜索结果中找不到相关证据，且该断言理应可被公开信息验证，应判为 UNSUPPORTED 而非 UNVERIFIABLE
- 不要因为 PARTIAL 也能接受就放宽标准——只有真正存在支撑证据时才判 PARTIAL
- UNVERIFIABLE 是狭窄的例外类别，不应作为默认选项

返回 JSON：
```json
{{"verdict": "SUPPORTED|PARTIAL|UNSUPPORTED|UNVERIFIABLE", "evidence": "关键证据摘要（1-2句话）", "confidence": 0.0-1.0}}
```
"""


class ClaimVerifier:
    """逐条核查 Claim，返回核查结果。搜索和判断均并行执行。"""

    MAX_CLAIMS = 10
    SEARCH_RESULTS_PER_CLAIM = 2  # 每条 Claim 搜索 2 条结果

    def __init__(self) -> None:
        # E1a：仅此调用点受 DR_CLAIM_VERIFY_THINKING 控制（默认 on）
        self._model = get_chat_model_for_task("verifying", thinking=claim_verify_thinking())

    async def verify(self, claims: list) -> list[ClaimVerdict]:
        """并行核查所有 Claim，兼容字符串和字典格式。"""
        # 标准化
        texts: list[str] = []
        for claim in claims[: self.MAX_CLAIMS]:
            if isinstance(claim, str):
                texts.append(claim)
            elif isinstance(claim, dict):
                texts.append(claim.get("text") or claim.get("claim") or str(claim))
            else:
                texts.append(str(claim))

        if not texts:
            return []

        # 第一阶段：并行搜索所有 Claim
        logger.info("Verifying %d claims in parallel (search phase)", len(texts))
        search_results = await asyncio.gather(
            *[self._search_one(t) for t in texts],
            return_exceptions=True,
        )

        # 第二阶段：并行 LLM 判断
        logger.info("Verification: judging phase for %d claims", len(texts))
        verdicts = await asyncio.gather(
            *[self._judge_one(text, sr) for text, sr in zip(texts, search_results)],
            return_exceptions=True,
        )

        # 收集结果
        results: list[ClaimVerdict] = []
        for text, verdict in zip(texts, verdicts):
            if isinstance(verdict, Exception):
                logger.warning("Verification failed for claim: %s", verdict)
                results.append(ClaimVerdict(claim_text=text, verdict="UNVERIFIABLE"))
            else:
                results.append(verdict)

        supported = sum(1 for v in results if v.verdict == "SUPPORTED")
        partial = sum(1 for v in results if v.verdict == "PARTIAL")
        total = len(results)
        verified_rate = (supported + partial) / total * 100 if total > 0 else 0
        logger.info(
            "Verification: %d supported + %d partial = %.0f%% verified (%d total)",
            supported, partial, verified_rate, total,
        )
        return results

    async def _search_one(self, claim_text: str) -> str:
        """搜索单条 Claim，返回搜索结果文本。

        P0 根因修复（Phase 2）：tavily_search 是同步调用链（搜索 + 网页总结
        里还有本地 LLM 推理）。若直接在本协程里执行，会**阻塞事件循环**，
        把 heartbeat 饿死超过 claim TTL（实测 3/3 次运行在 verification
        洪峰后丢失 claim）。放入线程执行，循环保持可调度。
        """
        if not claim_text.strip():
            return ""
        search_query = claim_text[:120].strip().strip('"').strip("'")
        for word in ["此外", "同时", "值得注意", "综上"]:
            search_query = search_query.replace(word, "")
        try:
            result = await asyncio.to_thread(
                tavily_search, search_query, max_results=self.SEARCH_RESULTS_PER_CLAIM
            )
            return str(result)[:3000]
        except Exception as e:
            logger.warning("Search failed for claim verification: %s", e)
            return f"SEARCH_ERROR: {e}"

    async def _judge_one(self, claim_text: str, search_results: str) -> ClaimVerdict:
        """LLM 判断单条 Claim（异步调用，不阻塞事件循环）。"""
        if not claim_text.strip():
            return ClaimVerdict(claim_text=claim_text, verdict="UNVERIFIABLE")
        if not search_results or search_results.startswith("SEARCH_ERROR"):
            return ClaimVerdict(claim_text=claim_text, verdict="UNVERIFIABLE",
                              evidence=search_results if search_results else "无搜索结果")

        prompt = CLAIM_VERIFY_PROMPT.format(
            claim_text=claim_text,
            search_results=search_results,
        )
        response = await self._model.ainvoke([HumanMessage(content=prompt)])

        try:
            data = parse_json_response(response.content)
            return ClaimVerdict(
                claim_text=claim_text,
                verdict=data.get("verdict", "UNVERIFIABLE"),
                evidence=data.get("evidence", ""),
                confidence=float(data.get("confidence", 0.5)),
            )
        except Exception as e:
            logger.warning("Failed to parse verification result: %s", e)
            return ClaimVerdict(claim_text=claim_text, verdict="UNVERIFIABLE")
