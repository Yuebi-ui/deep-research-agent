"""测试成本追踪 —— CostTrackerCallback 的 token 统计与费用计算。"""

import tempfile
from uuid import uuid4

from langchain_core.outputs import LLMResult

from deep_research.callbacks.cost_tracker import CostTrackerCallback


def make_mock_result(model_name, input_tokens, output_tokens):
    """构造 LangChain LLMResult 用于测试回调。"""
    return LLMResult(
        llm_output={
            "model_name": model_name,
            "token_usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
        },
        generations=[],
    )


def test_cost_tracker_deepseek_v4_pro():
    """deepseek-v4-pro: input 2元/百万, output 8元/百万。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cb = CostTrackerCallback(log_dir=tmpdir)
        mock = make_mock_result("deepseek-v4-pro", 100_000, 50_000)
        cb.on_llm_end(mock, run_id=uuid4())

        # (100000*2 + 50000*8) / 1_000_000 = 0.6
        assert cb.total_cost == 0.6
        assert cb.total_tokens == 150_000


def test_cost_tracker_unknown_model():
    """未知模型应不崩溃，费用为 0。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cb = CostTrackerCallback(log_dir=tmpdir)
        mock = make_mock_result("unknown-model", 10_000, 5_000)
        cb.on_llm_end(mock, run_id=uuid4())

        assert cb.total_cost == 0.0
        assert cb.total_tokens == 15_000


def test_cost_tracker_accumulates():
    """多次调用应累加 token 和费用。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cb = CostTrackerCallback(log_dir=tmpdir)
        cb.on_llm_end(make_mock_result("deepseek-v4-pro", 50_000, 25_000), run_id=uuid4())
        cb.on_llm_end(make_mock_result("deepseek-v4-pro", 50_000, 25_000), run_id=uuid4())

        assert cb.total_cost == 0.6
        assert cb.total_tokens == 150_000


def test_cost_summary_format():
    """summary() 应包含关键字段。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        cb = CostTrackerCallback(log_dir=tmpdir)
        cb.on_llm_end(make_mock_result("deepseek-v4-pro", 100_000, 50_000), run_id=uuid4())
        summary = cb.summary()
        assert "成本汇总" in summary
        assert "deepseek-v4-pro" in summary
        assert "¥0.6000" in summary
