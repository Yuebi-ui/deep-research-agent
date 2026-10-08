"""Baseline 观测体系离线测试（Phase 1）。

覆盖任务包要求的 7 项：

1. run_id propagation
2. LLM success metric
3. LLM failure metric
4. context_overflow classification
5. context utilization calculation
6. artifact aggregation
7. instrumentation failure 不破坏业务路径

外加：search metric、node metric 记录、render_baseline_md。

全部离线：不访问真实 LLM/Search（conftest 的 socket 守卫兜底），
不产生费用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from deep_research.callbacks import baseline_metrics as bm
from deep_research.callbacks import baseline_report as br
from deep_research.callbacks.baseline_metrics import (
    BaselineMetricsCollector,
    RunContext,
    classify_llm_error,
    safe_run_id,
)
from deep_research.testing.fake_llm import MODE_ERROR, FakeChatModel, make_fake_chat_model

_VLLM_OVERFLOW_MESSAGE = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
    "8192 tokens. However, you requested 0 output tokens and your prompt contains at "
    "least 8193 input tokens, for a total of at least 8193 tokens. Please reduce the "
    "length of the input prompt or the number of requested output tokens. "
    "(parameter=input_tokens, value=8193)\", 'code': 400}}"
)


def _make_collector(tmp_path: Path, run_id: str = "run-test-1", task_id: str = "task-1",
                    attempt: int = 1) -> BaselineMetricsCollector:
    return BaselineMetricsCollector(
        RunContext(run_id=run_id, task_id=task_id, attempt=attempt, worker_id="w1"),
        raw_root=tmp_path,
    )


def _read_rows(tmp_path: Path, run_id: str, filename: str) -> list[dict]:
    path = tmp_path / safe_run_id(run_id) / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ===== 1. run_id propagation =====


async def test_run_id_propagates_into_llm_rows(tmp_path):
    collector = _make_collector(tmp_path, run_id="run-abc", task_id="task-xyz", attempt=2)
    model = make_fake_chat_model("writer")

    await model.ainvoke(
        [HumanMessage(content="hi")],
        config={
            "callbacks": [collector],
            "metadata": {
                "langgraph_node": "write_research_brief",
                "dr_role": "writer",
                "dr_backend": "openai",
                "dr_handle": "deepseek-v4-pro",
            },
        },
    )

    rows = _read_rows(tmp_path, "run-abc", "llm_calls.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-abc"
    assert row["task_id"] == "task-xyz"
    assert row["attempt"] == 2
    assert row["worker_id"] == "w1"


# ===== 2. LLM success metric =====


async def test_llm_success_metric_tokens_latency_attribution(tmp_path):
    collector = _make_collector(tmp_path)
    model = make_fake_chat_model("writer")

    await model.ainvoke(
        [HumanMessage(content="hi")],
        config={
            "callbacks": [collector],
            "metadata": {
                "langgraph_node": "supervisor",
                "dr_role": "writer",
                "dr_backend": "openai",
                "dr_handle": "deepseek-v4-pro",
            },
        },
    )

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["success"] is True
    assert row["input_tokens"] == 100  # FakeChatModel 固定 usage
    assert row["output_tokens"] == 50
    assert row["token_source"] == "provider_usage"
    assert row["latency_ms"] >= 0
    assert row["node"] == "supervisor"
    assert row["role"] == "writer"
    assert row["provider"] == "openai"
    assert row["model"] == "deepseek-v4-pro"
    assert row["error_type"] is None


# ===== 3. LLM failure metric =====


async def test_llm_failure_metric(tmp_path):
    collector = _make_collector(tmp_path)
    model = FakeChatModel(role="writer", mode=MODE_ERROR)

    with pytest.raises(Exception):
        await model.ainvoke(
            [HumanMessage(content="hi")],
            config={"callbacks": [collector], "metadata": {"langgraph_node": "llm_call"}},
        )

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["success"] is False
    assert row["error_type"] == "other"
    assert "fake provider failure" in (row["error_message"] or "")
    assert row["input_tokens"] is None
    assert row["node"] == "llm_call"


# ===== 4. context_overflow classification =====


def test_classify_context_overflow_parses_limit_and_bound():
    class _Err(Exception):
        pass

    info = classify_llm_error(_Err(_VLLM_OVERFLOW_MESSAGE))
    assert info["error_type"] == "context_overflow"
    assert info["context_limit"] == 8192
    assert info["context_limit_source"] == "error_message"
    assert info["input_tokens_lower_bound"] == 8193


def test_classify_timeout_and_rate_limit():
    class _Err(Exception):
        pass

    assert classify_llm_error(_Err("Request timed out after 30s"))["error_type"] == "timeout"
    assert classify_llm_error(_Err("Error 429: rate limit exceeded"))["error_type"] == "rate_limit"


async def test_context_overflow_row_structure(tmp_path):
    collector = _make_collector(tmp_path)

    def _raise(_messages):
        raise ValueError(_VLLM_OVERFLOW_MESSAGE)

    model = FakeChatModel(role="researcher_summarizer", responder=_raise)

    with pytest.raises(Exception):
        await model.ainvoke(
            [HumanMessage(content="long")],
            config={
                "callbacks": [collector],
                "metadata": {
                    "langgraph_node": "tool_node",
                    "dr_role": "researcher_summarizer",
                    "dr_backend": "openai_local",
                    "dr_handle": "qwen3-30b-a3b-local",
                },
            },
        )

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["success"] is False
    assert row["error_type"] == "context_overflow"
    assert row["context_limit"] == 8192
    assert row["input_tokens_lower_bound"] == 8193
    assert row["overflow_tokens"] == 1  # max(8193-8192, 0)
    assert row["input_tokens"] is None  # 不伪造精确值
    assert row["context_utilization"] is None


# ===== 5. context utilization calculation =====


async def test_context_utilization_uses_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(bm.LOCAL_LIMIT_ENV, "8192")
    collector = _make_collector(tmp_path)
    model = make_fake_chat_model("researcher_summarizer")

    await model.ainvoke(
        [HumanMessage(content="hi")],
        config={
            "callbacks": [collector],
            "metadata": {
                "langgraph_node": "tool_node",
                "dr_role": "researcher_summarizer",
                "dr_backend": "openai_local",
                "dr_handle": "qwen3-30b-a3b-local",
            },
        },
    )

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["context_limit"] == 8192
    assert row["context_limit_source"] == "env_override"
    assert row["context_utilization"] == round(100 / 8192, 4)  # fake usage input=100


async def test_cloud_calls_have_no_utilization(tmp_path):
    collector = _make_collector(tmp_path)
    model = make_fake_chat_model("writer")

    await model.ainvoke(
        [HumanMessage(content="hi")],
        config={
            "callbacks": [collector],
            "metadata": {"dr_backend": "openai", "dr_handle": "deepseek-v4-pro"},
        },
    )

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["context_limit"] is None
    assert row["context_utilization"] is None


# ===== 6. artifact aggregation =====


def _synthetic_rows() -> dict[str, list[dict]]:
    def llm(provider, model, ok=True, err=None, itok=100, otok=50, node="supervisor", rid="run-1"):
        return {
            "run_id": rid, "task_id": "t1", "attempt": 1, "provider": provider,
            "model": model, "node": node, "role": model if provider == "openai" else "researcher_main",
            "success": ok, "error_type": err, "input_tokens": itok if ok else None,
            "output_tokens": otok if ok else None, "token_source": "provider_usage" if ok else None,
            "latency_ms": 100, "context_limit": 8192 if provider == "openai_local" else None,
            "context_limit_source": "env_override" if provider == "openai_local" else None,
            "context_utilization": 0.5 if provider == "openai_local" else None,
            "overflow_tokens": 1 if err == "context_overflow" else None,
            "input_tokens_lower_bound": 8193 if err == "context_overflow" else None,
            "error_message": "boom" if err else None,
        }

    return {
        "llm_calls": [
            llm("openai", "deepseek-v4-pro", itok=1_000_000, otok=500_000),
            llm("openai_local", "qwen3-30b-a3b-local", node="tool_node"),
            llm("openai_local", "qwen3-30b-a3b-local", ok=False, err="context_overflow", node="tool_node"),
        ],
        "node_metrics": [
            {"run_id": "run-1", "node": "supervisor", "latency_ms": 2000, "success": True,
             "started_at": 1.0, "finished_at": 3.0},
            {"run_id": "run-1", "node": "tool_node", "latency_ms": None, "success": False,
             "started_at": 3.0, "finished_at": None},
        ],
        "search_metrics": [
            {"run_id": "run-1", "node": "tool_node", "provider": "TavilyProvider",
             "query": "q1", "success": True, "result_count": 3, "latency_ms": 800},
            {"run_id": "run-1", "node": "tool_node", "provider": "TavilyProvider",
             "query": "q2", "success": True, "result_count": 3, "latency_ms": 700},
        ],
        "reliability_events": [
            {"run_id": "run-1", "event": "claim_acquired", "attempt": 1, "details": {}},
            {"run_id": "run-1", "event": "claim_renewal_failed", "attempt": 1, "details": {}},
            {"run_id": "run-1", "event": "recovery_requeued", "attempt": 1, "details": {}},
        ],
    }


def test_aggregate_totals_and_rates():
    task = {
        "thread_id": "t1", "status": "completed", "attempt": 3,
        "created_at": "2026-10-05T00:00:00", "updated_at": "2026-10-05T00:07:30",
        "error": "",
    }
    run = br.aggregate(_synthetic_rows(), run_id="run-1", task=task)

    assert run["llm"]["calls"] == 3
    assert run["llm"]["success"] == 2
    assert run["llm"]["failed"] == 1
    assert run["local"]["calls"] == 2
    assert run["local"]["context_overflow"] == 1
    assert run["cloud"]["calls"] == 1
    assert run["cloud"]["input_tokens"] == 1_000_000
    # deepseek-v4-pro: input ¥2/M, output ¥8/M
    assert run["cloud"]["estimated_cost_rmb"] == pytest.approx(2.0 + 4.0)
    assert run["context"]["overflow_count"] == 1
    assert run["context"]["overflow_by_node"] == {"tool_node": 1}
    assert run["context"]["overflow_rate_of_local_calls"] == 0.5
    assert run["search"]["calls"] == 2
    assert run["search"]["results"] == 6
    assert run["reliability"]["claim_losses"] == 1
    assert run["reliability"]["recoveries"] == 1
    assert run["reliability"]["attempts"] == 3
    assert run["timeline"]["total_latency_ms"] == 450_000

    node_names = [n["node"] for n in run["nodes"]]
    assert "supervisor" in node_names and "tool_node" in node_names
    md = br.render_baseline_md(run)
    assert "# V1 Baseline" in md
    assert "Observed Bottlenecks" in md
    assert "Data Quality / Limitations" in md


def test_load_rows_missing_files_are_empty(tmp_path):
    rows = br.load_rows(tmp_path / "nope")
    assert set(rows) == {
        "llm_calls", "node_metrics", "search_metrics", "budget_events", "reliability_events",
    }
    assert all(v == [] for v in rows.values())


# ===== 7. instrumentation failure 不破坏业务路径 =====


async def test_collector_write_failure_does_not_raise(tmp_path):
    # 用文件充当目录：mkdir/open 必然失败
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x")
    collector = BaselineMetricsCollector(
        RunContext(run_id="run-bad", task_id="t1"), raw_root=bogus
    )
    model = make_fake_chat_model("writer")

    # LLM 调用必须照常成功
    result = await model.ainvoke(
        [HumanMessage(content="hi")],
        config={"callbacks": [collector], "metadata": {"langgraph_node": "n"}},
    )
    assert isinstance(result, AIMessage)

    # 其它记录接口也不得抛出
    collector.record_search(
        provider="X", query="q", topic=None, max_results=3,
        started_at=0.0, finished_at=1.0, success=True, result_count=1,
    )
    collector.record_reliability("claim_acquired")
    collector.record_node_metric("node", "rid", 0.0, 1.0, True)
    bm.append_reliability_event("run-bad", "t1", "claim_not_acquired", raw_root=bogus)


async def test_graph_run_with_collector_matches_without(tmp_path):
    """同一离线图，加不加 collector，最终行为必须一致。"""
    from langgraph.checkpoint.memory import InMemorySaver

    from deep_research.agent_builder import _create_builder

    async def run(with_collector: bool) -> str:
        graph = _create_builder(with_hitl=False).compile(checkpointer=InMemorySaver())
        cfg: dict = {"configurable": {"thread_id": f"t-{with_collector}"}, "recursion_limit": 60}
        if with_collector:
            cfg["callbacks"] = [_make_collector(tmp_path)]
            cfg["metadata"] = {"dr_run_id": "run-test-1"}
        final = await graph.ainvoke(
            {"messages": [HumanMessage(content="Compare LangGraph and CrewAI")]}, config=cfg
        )
        return final.get("final_report", "")

    without = await run(False)
    with_c = await run(True)
    assert without and with_c
    assert without == with_c


# ===== E1a/E4 instrument（Phase 3B）=====


def test_extract_usage_reasoning_tokens_from_message_metadata():
    from langchain_core.messages import AIMessage as _AI
    from langchain_core.outputs import ChatGeneration, LLMResult

    msg = _AI(content="answer", usage_metadata={
        "input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
        "output_token_details": {"reasoning": 40},
    })
    result = LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output={})
    model, i, o, src, reasoning = bm._extract_usage(result)
    assert (i, o, reasoning) == (100, 60, 40)


def test_extract_usage_reasoning_tokens_from_llm_output():
    from langchain_core.outputs import LLMResult

    result = LLMResult(generations=[], llm_output={
        "model_name": "deepseek-v4-flash",
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 20,
                        "completion_tokens_details": {"reasoning_tokens": 7}},
    })
    model, i, o, src, reasoning = bm._extract_usage(result)
    assert (model, i, o, reasoning) == ("deepseek-v4-flash", 10, 20, 7)


async def test_llm_row_records_reasoning_and_thinking(tmp_path):
    from uuid import uuid4

    from langchain_core.messages import AIMessage as _AI
    from langchain_core.outputs import ChatGeneration, LLMResult

    collector = _make_collector(tmp_path)
    rid = uuid4()
    collector.on_chat_model_start(
        None, [[]], run_id=rid,
        metadata={"langgraph_node": "claim_verification", "dr_role": "evaluator",
                  "dr_backend": "openai", "dr_thinking": "off"},
    )
    msg = _AI(content="x", usage_metadata={
        "input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
        "output_token_details": {"reasoning": 40},
    })
    collector.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=rid)

    row = _read_rows(tmp_path, "run-test-1", "llm_calls.jsonl")[0]
    assert row["thinking"] == "off"
    assert row["reasoning_tokens"] == 40
    assert row["content_tokens"] == 20


def test_search_metric_hash_fields_normalized(tmp_path):
    from deep_research.callbacks.baseline_metrics import set_baseline_collector
    from deep_research.tools import search_factory, tool as dr_tools
    from deep_research.tools.tool import tavily_search

    search_factory.clear_cache()
    dr_tools.reset_search_runtime()
    collector = _make_collector(tmp_path)
    set_baseline_collector(collector)
    try:
        tavily_search("LangGraph   Use Cases", max_results=2)
        tavily_search("langgraph use cases", max_results=2)  # 空白/大小写归一后同 hash
    finally:
        set_baseline_collector(None)
        search_factory.clear_cache()
        dr_tools.reset_search_runtime()

    rows = _read_rows(tmp_path, "run-test-1", "search_metrics.jsonl")
    assert len(rows) == 2
    assert rows[0]["query_hash"] == rows[1]["query_hash"], "规范化 query hash 必须一致"
    assert rows[0]["url_keys"] and rows[0]["content_sha1s"]
    assert len(rows[0]["url_keys"]) == rows[0]["result_count"]


# ===== node metric（runner 侧记账逻辑）=====


def test_node_metric_pairing_and_incomplete(tmp_path):
    from backend.runtime.runner import TaskRunner

    collector = _make_collector(tmp_path)
    runner = TaskRunner(
        redis=None, worker_id="w1", settings=None, repository=None, checkpointer=None
    )
    runner._collector = collector

    runner._note_node_start("supervisor", "lg-1")
    runner._note_node_end("supervisor", "lg-1")
    runner._note_node_start("human_review", "lg-2")  # 未收尾（HITL 挂起）
    runner._close_open_nodes(success=None, error_type="suspended")

    rows = _read_rows(tmp_path, "run-test-1", "node_metrics.jsonl")
    by_node = {r["node"]: r for r in rows}
    assert by_node["supervisor"]["success"] is True
    assert by_node["supervisor"]["latency_ms"] is not None
    assert by_node["human_review"]["success"] is None
    assert by_node["human_review"]["finished_at"] is None
    assert by_node["human_review"]["error_type"] == "suspended"

    # 关闭后不得残留（避免跨 run 串数据）
    assert runner._open_nodes == {}


def test_build_openai_kwargs_stream_usage_flag(monkeypatch):
    """DR_STREAM_USAGE 开关：默认关闭；打开时请求附带 usage（观测用）。"""
    from deep_research.llm import _build_openai_kwargs

    api_cfg = {"base_url": "http://127.0.0.1:8001/v1", "api_key": "EMPTY"}

    monkeypatch.delenv("DR_STREAM_USAGE", raising=False)
    assert "stream_usage" not in _build_openai_kwargs("qwen3-30b-a3b-local", api_cfg, None, None)

    monkeypatch.setenv("DR_STREAM_USAGE", "1")
    assert _build_openai_kwargs("qwen3-30b-a3b-local", api_cfg, None, None)["stream_usage"] is True


# ===== search metric（假 provider，离线）=====


def test_search_metric_recorded_with_fake_provider(tmp_path, monkeypatch):
    from deep_research.tools import search_factory, tool as dr_tools
    from deep_research.tools.tool import tavily_search

    search_factory.clear_cache()
    dr_tools.reset_search_runtime()

    collector = _make_collector(tmp_path)
    bm.set_baseline_collector(collector)
    try:
        out = tavily_search("fake query", max_results=3)
        assert "SOURCE" in out
    finally:
        bm.set_baseline_collector(None)
        search_factory.clear_cache()
        dr_tools.reset_search_runtime()

    rows = _read_rows(tmp_path, "run-test-1", "search_metrics.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] is True
    assert row["result_count"] >= 1
    assert row["provider"]  # 类名（FakeSearchProvider）
    assert row["node"]  # 无 LLM/tool 上下文时为 "unknown"


def test_safe_run_id_rejects_path_traversal():
    assert safe_run_id("abc-123_x.y") == "abc-123_x.y"
    assert ".." not in safe_run_id("../../etc/passwd")
    assert "/" not in safe_run_id("../../etc/passwd")
    assert safe_run_id("../evil") != "../evil"
