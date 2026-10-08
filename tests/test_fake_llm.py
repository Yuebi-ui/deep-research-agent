"""FakeChatModel 的接口契约与失败模式。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from deep_research.testing.fake_llm import (
    MODE_ERROR,
    MODE_RATE_LIMIT,
    MODE_TIMEOUT,
    FakeChatModel,
    FakeLLMError,
    FakeLLMRateLimitError,
    FakeLLMTimeoutError,
    make_fake_chat_model,
    reset_responders,
    set_responder,
)


@tool
def ConductResearch(research_topic: str) -> str:
    """A fake tool used to exercise bind_tools."""
    return "ok"


def test_fake_chat_model_is_a_base_chat_model():
    from langchain_core.language_models.chat_models import BaseChatModel

    assert isinstance(make_fake_chat_model("writer"), BaseChatModel)


def test_bind_tools_does_not_raise():
    """import 期就会调用 bind_tools，基类默认实现会抛 NotImplementedError。"""
    model = make_fake_chat_model("supervisor")
    bound = model.bind_tools([ConductResearch])

    assert isinstance(bound, FakeChatModel)
    assert bound.bound_tool_names == ["ConductResearch"]


def test_bind_tools_accepts_empty_tool_list():
    assert make_fake_chat_model("writer").bind_tools([]).bound_tool_names == []


def test_invoke_returns_ai_message_with_usage():
    msg = make_fake_chat_model("writer").invoke([HumanMessage("hi")])

    assert isinstance(msg, AIMessage)
    assert msg.content
    assert msg.usage_metadata["input_tokens"] == 100
    assert msg.usage_metadata["output_tokens"] == 50


def test_ainvoke_works():
    msg = asyncio.run(make_fake_chat_model("writer").ainvoke([HumanMessage("hi")]))
    assert isinstance(msg, AIMessage)
    assert msg.content


def test_astream_works():
    async def collect():
        return [
            chunk
            async for chunk in make_fake_chat_model("writer").astream(
                [HumanMessage("hi")]
            )
        ]

    chunks = asyncio.run(collect())
    assert chunks
    assert "".join(c.content for c in chunks)


def test_supervisor_emits_tool_calls_then_completes():
    """supervisor 先派发研究，拿到工具结果后收尾——图才能正常终止。"""
    model = make_fake_chat_model("supervisor")

    first = model.invoke([HumanMessage("研究 X")])
    assert [tc["name"] for tc in first.tool_calls] == ["ConductResearch"]

    followup = model.invoke(
        [HumanMessage("研究 X"), ToolMessage(content="result", tool_call_id="1")]
    )
    assert [tc["name"] for tc in followup.tool_calls] == ["ResearchComplete"]


def test_researcher_routes_to_compress_after_tool_result():
    model = make_fake_chat_model("researcher_main")

    first = model.invoke([HumanMessage("q")])
    assert [tc["name"] for tc in first.tool_calls] == ["tavily_search"]

    # 无 tool_calls → 图转入 compress_research 而非继续调用工具
    followup = model.invoke(
        [HumanMessage("q"), ToolMessage(content="r", tool_call_id="1")]
    )
    assert not followup.tool_calls


def test_evaluator_returns_parseable_json():
    msg = make_fake_chat_model("evaluator").invoke([HumanMessage("score this")])
    payload = json.loads(msg.content)

    for key in ("comprehensiveness_score", "accuracy_score", "coherence_score"):
        assert key in payload


def test_summarizer_returns_summary_shape():
    msg = make_fake_chat_model("researcher_summarizer").invoke([HumanMessage("sum")])
    payload = json.loads(msg.content)

    assert "summary" in payload
    assert "key_excerpts" in payload


def test_responder_override():
    set_responder("writer", lambda messages: AIMessage(content="custom"))
    try:
        assert make_fake_chat_model("writer").invoke([HumanMessage("x")]).content == "custom"
    finally:
        reset_responders()


# ===== 失败模式 =====


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (MODE_TIMEOUT, FakeLLMTimeoutError),
        (MODE_RATE_LIMIT, FakeLLMRateLimitError),
        (MODE_ERROR, FakeLLMError),
    ],
)
def test_failure_modes_raise(mode, expected):
    with pytest.raises(expected):
        make_fake_chat_model("writer", mode=mode).invoke([HumanMessage("hi")])


def test_failure_modes_raise_in_astream():
    async def collect():
        return [
            c
            async for c in make_fake_chat_model("writer", mode=MODE_TIMEOUT).astream(
                [HumanMessage("hi")]
            )
        ]

    with pytest.raises(FakeLLMTimeoutError):
        asyncio.run(collect())


def test_get_chat_model_for_task_and_auto_route_to_fake():
    """两个路由辅助函数都必须继承离线行为。"""
    from deep_research.llm import get_chat_model_auto, get_chat_model_for_task

    assert isinstance(get_chat_model_for_task("extracting"), FakeChatModel)
    assert isinstance(get_chat_model_auto("draft", "a" * 500), FakeChatModel)
