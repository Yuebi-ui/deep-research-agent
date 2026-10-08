"""离线 Fake LLM。

设计要点（均已对照 `.venv` 中安装的 langchain-core 1.4.0 源码验证）：

* 必须继承 :class:`BaseChatModel` —— 图在 **import 期** 就会调用
  ``.bind_tools()``（``agents/supervisor.py``、``agents/research_agent.py``），
  而 ``BaseChatModel.bind_tools`` 的默认实现直接 ``raise NotImplementedError``。
  因此本类必须覆写它。
* 必须实现 ``_generate``（基类默认实现返回 ``None``）。实现后
  ``ainvoke`` 会自动可用（基类 ``_agenerate`` 委托给 ``_generate``）。
* 必须实现 ``_stream``（基类默认 ``raise NotImplementedError``）供 ``astream`` 使用。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

# 固定 usage，便于 cost tracker 与断言使用
_FAKE_USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

# 运行模式
MODE_SUCCESS = "success"
MODE_TIMEOUT = "timeout"
MODE_ERROR = "error"
MODE_RATE_LIMIT = "rate_limit"


class FakeLLMError(RuntimeError):
    """Fake provider 主动抛出的错误基类。"""


class FakeLLMTimeoutError(FakeLLMError):
    """模拟超时。"""


class FakeLLMRateLimitError(FakeLLMError):
    """模拟限流。"""


FakeResponder = Callable[[list[BaseMessage]], AIMessage]


def _is_followup(messages: list[BaseMessage]) -> bool:
    """判断是否已经执行过工具——用于模拟「拿到资料后收尾」。

    注意**不能只看** ``messages[-1]``：supervisor 节点在组装 prompt 时会于
    消息末尾追加 ``SystemMessage``（对抗性反馈 / 质量修复提醒），此时最后一条
    并非 ``ToolMessage``。改为「历史中已出现 ToolMessage」这一更宽松且稳定的
    判据，Fake 才能在有限步内收敛，而不是无限派发研究。
    """
    return any(isinstance(m, ToolMessage) for m in messages)


def _default_responder(role: str, messages: list[BaseMessage]) -> AIMessage:
    """按 role 返回确定性的 AIMessage，使整张图能在离线状态下跑通。"""
    followup = _is_followup(messages)

    if role == "supervisor":
        if followup:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "ResearchComplete", "args": {}, "id": "call_complete"}
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ConductResearch",
                    "args": {"research_topic": "fake research topic"},
                    "id": "call_research",
                }
            ],
        )

    if role == "researcher_main":
        if followup:
            # 无 tool_calls → 图转入 compress_research
            return AIMessage(content="Fake research findings for offline testing.")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "tavily_search",
                    "args": {"query": "fake query"},
                    "id": "call_search",
                }
            ],
        )

    if role in ("researcher_compressor", "researcher_summarizer"):
        return AIMessage(
            content=json.dumps(
                {"summary": "Fake compressed summary.", "key_excerpts": "Fake excerpt."}
            )
        )

    if role == "red_team":
        # 简短/含 PASS 的输出会让 red_team 节点提前退出
        return AIMessage(content="PASS")

    if role == "evaluator":
        return AIMessage(
            content=json.dumps(
                {
                    "comprehensiveness_score": 7,
                    "accuracy_score": 7,
                    "coherence_score": 7,
                    "reason": "Fake evaluation for offline testing.",
                    "missing_aspects": [],
                    "need_more_research": False,
                    # MemoryManager 的抽取字段，与 evaluator 共用同一 role
                    "entities": [],
                    "claims": [],
                    "contradictions": [],
                }
            )
        )

    if role in ("writer", "draft"):
        return AIMessage(content="# Fake Report\n\nGenerated offline without external APIs.")

    return AIMessage(content=f"Fake response for role '{role}'.")


# 测试可通过 set_responder 覆盖特定 role 的行为
_RESPONDER_OVERRIDES: dict[str, FakeResponder] = {}


def set_responder(role: str, responder: FakeResponder | None) -> None:
    """为指定 role 注册/清除自定义应答函数。"""
    if responder is None:
        _RESPONDER_OVERRIDES.pop(role, None)
    else:
        _RESPONDER_OVERRIDES[role] = responder


def reset_responders() -> None:
    """清除所有自定义应答函数，供测试隔离使用。"""
    _RESPONDER_OVERRIDES.clear()


class FakeChatModel(BaseChatModel):
    """与 ChatOpenAI 接口兼容的离线替身。"""

    role: str = "fake"
    mode: str = MODE_SUCCESS
    delay_seconds: float = 0.0
    bound_tool_names: list[str] = Field(default_factory=list)
    # 用 Any 声明，避免 pydantic 尝试校验/序列化可调用对象
    responder: Any = Field(default=None, exclude=True)
    calls: list[Any] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self) -> str:
        return f"fake-chat-model:{self.role}"

    # ===== 必须覆写：基类默认 raise NotImplementedError =====
    def bind_tools(self, tools: Any, *, tool_choice: str | None = None, **kwargs: Any):
        """记录工具名并返回自身副本。

        真实的 ``ChatOpenAI.bind_tools`` 会返回一个绑定工具的可运行对象；
        离线场景不需要真正绑定，只要不抛异常且保持可调用即可。
        """
        names: list[str] = []
        for t in tools or []:
            name = getattr(t, "name", None) or getattr(t, "__name__", None)
            if name:
                names.append(str(name))
        return self.model_copy(update={"bound_tool_names": names})

    # ===== 生成 =====
    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        if self.mode == MODE_TIMEOUT:
            raise FakeLLMTimeoutError("fake timeout")
        if self.mode == MODE_RATE_LIMIT:
            raise FakeLLMRateLimitError("fake rate limit")
        if self.mode == MODE_ERROR:
            raise FakeLLMError("fake provider failure")

        responder = self.responder or _RESPONDER_OVERRIDES.get(self.role)
        if responder is not None:
            msg = responder(messages)
        else:
            msg = _default_responder(self.role, messages)

        msg.usage_metadata = dict(_FAKE_USAGE)  # type: ignore[assignment]
        return msg

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if self.delay_seconds:
            import time

            time.sleep(self.delay_seconds)
        message = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        ).generations[0].message

        chunks = [
            {
                "name": tc.get("name"),
                "args": json.dumps(tc.get("args") or {}),
                "id": tc.get("id"),
                "index": i,
                "type": "tool_call_chunk",
            }
            for i, tc in enumerate(getattr(message, "tool_calls", None) or [])
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content,
                tool_call_chunks=chunks,  # type: ignore[arg-type]
                usage_metadata=dict(_FAKE_USAGE),  # type: ignore[arg-type]
            )
        )


def make_fake_chat_model(role: str, **kwargs: Any) -> FakeChatModel:
    """构造一个绑定到指定 role 的 Fake LLM。"""
    return FakeChatModel(role=role, **kwargs)


__all__ = [
    "MODE_ERROR",
    "MODE_RATE_LIMIT",
    "MODE_SUCCESS",
    "MODE_TIMEOUT",
    "FakeChatModel",
    "FakeLLMError",
    "FakeLLMRateLimitError",
    "FakeLLMTimeoutError",
    "make_fake_chat_model",
    "reset_responders",
    "set_responder",
]
