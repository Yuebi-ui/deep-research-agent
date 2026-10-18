#***********************************************
#      Filename: supervisor_agent.py
#   Description: 监督智能体
#***********************************************

"""用于协调多个Sub-Research-Agent的监督。该模块实现了一种监督者模式，其中：
1. Supervisor Agent协调研究活动并分配任务
2. 多个Sub-Research-Agent独立地处理特定的子主题
3. 结果汇总并压缩，用于最终报告
Supervisor Agent采用并行执行方式来提高效率，同时为每个研究主题保持独立的上下文窗口。
"""


import asyncio
import json
from typing_extensions import Literal
from langchain_core.messages import (
    HumanMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
    filter_messages
)
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command

from deep_research.llm import get_chat_model, supervisor_thinking, with_role_metadata
from deep_research.prompts import MULTI_STEP_DENOISE_PROMPT
from deep_research.research_seed import quarantine_update
from deep_research.evidence import attribute_sources
from deep_research.agents.research_agent import researcher_agent
from deep_research.states import (
    SupervisorState,
    ConductResearch,
    ResearchComplete,
)
from deep_research.utils import get_today_str
from deep_research.tools import _think_tool
from deep_research import logging as dr_logging
from deep_research.memory.stage_retrieval import async_stage_context, stage_enabled

logger = dr_logging.get_logger(__name__)

# 确保 sniffio 能检测到 asyncio 上下文 (Windows 兼容)
# 该导入有意放在 logger 初始化之后——它带副作用（设置 contextvar），
# 且需要包在 try/except 中，因此不走文件顶部的常规导入区。
from sniffio._impl import current_async_library_cvar  # noqa: E402
try:
    current_async_library_cvar.set("asyncio")
except Exception:
    pass


def get_notes_from_tool_calls(messages: list[BaseMessage]) -> list[str]:
    """从Supervisor agent消息历史记录中的 ToolMessage 对象提取Research Notes。
    当Supervisor通过 ConductResearch tools调用将研究任务委托给子代理时，
    每个Sub-Agent 都会返回其压缩的研究结果（以 ToolMessage 内容形式）。
    此函数提取所有此类 ToolMessage 内容，以得到合并后的最终的研究笔记。

    Args：
        messages：主管对话历史记录中的消息列表

    Return：
        从ToolMessage对象中提取的Research Notes字符串列表
    """
    return [tool_msg.content for tool_msg in filter_messages(messages, include_types="tool")]



# ===== CONFIGURATION =====

supervisor_tools = [ConductResearch, ResearchComplete, _think_tool]
# E1b：supervisor 的 thinking 策略（环境变量 DR_SUPERVISOR_THINKING，默认 on）
supervisor_model = get_chat_model("supervisor", thinking=supervisor_thinking())
# bind_tools 会丢失构造期 metadata —— 补挂 role 归因（仅观测，不改行为）
supervisor_model_with_tools = with_role_metadata(
    supervisor_model.bind_tools(supervisor_tools), "supervisor"
)


# System constants (最大迭代次数/最大并行Sub-Agents)
max_researcher_iterations = 3  # Calls to think_tool + ConductResearch
max_concurrent_researchers = 2  # 最大并行子agent数


# ===== SUPERVISOR NODES =====

async def supervisor(state: SupervisorState) -> Command[Literal["supervisor_tools"]]:
    """分析研究简报和当前进展
    功能：
        - 需要研究哪些主题
        - 是否开展并行研究
        - 研究何时完成

    Args：
        state：当前supervisor状态，包含messages和progress

    Returns：
        用于跳转到 supervisor_tools 节点并更新状态的命令
    """
    supervisor_messages = state.get("supervisor_messages", [])
    iteration = state.get("research_iterations", 0)
    logger.info("[SUPERVISOR] supervisor invoked (iteration=%d, messages=%d)", iteration, len(supervisor_messages))

    # 组装系统提示词
    system_message = MULTI_STEP_DENOISE_PROMPT.format(
        date=get_today_str(),
        max_concurrent_research_units=max_concurrent_researchers,
        max_researcher_iterations=max_researcher_iterations
    )
    messages = [SystemMessage(content=system_message)] + supervisor_messages
    if state.get("quality_gate_version") != 3:
        raise ValueError("only V3 Supervisor state is supported")
    checklist = state.get("required_aspects") or []
    items = [{"aspect_id": x.get("aspect_id"), "title": x.get("title")}
             for x in checklist if isinstance(x, dict)]
    messages.insert(1, SystemMessage(content=(
        "V3 必查维度（不可由搜索文本更改）：" + json.dumps(items, ensure_ascii=False)
        + "。ConductResearch 必须使用精确 aspect_id；无法关联时留空。"
        + "只收集和压缩证据，不要改写 Draft 或调用额外 Red Team。"
    )))
    # V3 semantic/repair feedback is handled by the parent graph, not this child.
    hint = state.get("supervisor_memory_hint", "") or ""
    if stage_enabled() and iteration == 0:
        query = str(state.get("research_brief", "") or "")[:400]
        hint = await async_stage_context(query, "supervisor")
    if stage_enabled() and hint:
        messages.insert(1, HumanMessage(content=hint))

    # 决策调用哪一个工具
    response = await supervisor_model_with_tools.ainvoke(messages)
    logger.info(
        "supervisor model produced tool_calls=%s num_tool_calls=%d",
        bool(response.tool_calls),
        len(response.tool_calls or []),
    )

    # 跳转到supervisor_tools
    return Command(
        goto="supervisor_tools",
        update={
            "supervisor_messages": [response],
            "research_iterations": iteration + 1,
            "supervisor_memory_hint": hint
        }
    )


async def supervisor_tools(state: SupervisorState) -> Command[Literal["supervisor", "__end__"]]:
    """
    执行Supervisor决策——继续下一轮研究或者是结束流程。

    功能：
        - 执行 think_tool 调用以进行思考
        - 顺序执行针对不同主题的research agent
        - 汇总研究结果
        - 确定研究何时完成

    参数：
        state：包含supervisor messages和迭代次数

    返回值：
        继续下一轮supervisor/结束流程
    """
    supervisor_messages = state.get("supervisor_messages", [])
    research_iterations = state.get("research_iterations", 0)
    most_recent_message = supervisor_messages[-1]

    # 检查是否达到了最大迭代次数或者supervisor是否输出工具调用
    exceeded_iterations = research_iterations >= max_researcher_iterations
    no_tool_calls = not most_recent_message.tool_calls
    research_complete = any(
        tool_call["name"] == "ResearchComplete"
        for tool_call in most_recent_message.tool_calls
    )

    if exceeded_iterations or no_tool_calls or research_complete:
        notes = get_notes_from_tool_calls(supervisor_messages)
        return Command(goto=END, update=quarantine_update(state, notes))

    else:
        # 初始化变量
        tool_messages = []
        all_raw_notes = []
        updates = {}
        next_step = "supervisor"

        # 执行所有的工具调用
        try:
            think_tool_calls = [
                tool_call for tool_call in most_recent_message.tool_calls
                if tool_call["name"] == "think_tool"
            ]

            conduct_research_calls = [
                tool_call for tool_call in most_recent_message.tool_calls
                if tool_call["name"] == "ConductResearch"
            ]

            # Draft refinement and adversarial audit belong to the parent V3 graph.
            logger.info(
                "[SUPERVISOR] supervisor_tools executing think=%d conduct=%d",
                len(think_tool_calls),
                len(conduct_research_calls),
            )

            # 调用 think 工具（在调用其他工具之前，必须拿到反思结果）
            for tool_call in think_tool_calls:
                observation = _think_tool.invoke(tool_call["args"])
                tool_messages.append(
                    ToolMessage(
                        content=observation,
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"]
                    )
                )

            # 调用 ConductResearch工具 (并行执行)
            if conduct_research_calls:
                # 并行启动多个 research agents
                coros = [
                    researcher_agent.ainvoke({
                        "researcher_messages": [
                            HumanMessage(content=tool_call["args"]["research_topic"])
                        ],
                        "research_topic": tool_call["args"]["research_topic"],
                        "quality_gate_version": 3,
                    })
                    for tool_call in conduct_research_calls
                ]

                # 等待所有research agents 返回研究结果
                tool_results = await asyncio.gather(*coros)

                provenance: list[dict] = []
                for result, tool_call in zip(tool_results, conduct_research_calls):
                    sources = result.get("observed_sources") if isinstance(result, dict) else None
                    if isinstance(sources, list):
                        provenance.extend(attribute_sources(
                            sources,
                            aspect_id=str(tool_call.get("args", {}).get("aspect_id") or ""),
                            valid_aspects=state.get("required_aspects") or [],
                            generation=state.get("research_generation", 0),
                        ))
                if provenance:
                    updates["research_evidence"] = provenance

                # 将研究结果格式化为工具消息
                research_tool_messages = [
                    ToolMessage(
                        content=result.get("compressed_research", "Error synthesizing research report"),
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"]
                    ) for result, tool_call in zip(tool_results, conduct_research_calls)
                ]

                tool_messages.extend(research_tool_messages)
                traces = []
                for result, tool_call in zip(tool_results, conduct_research_calls):
                    trace = result.get("episode_trace") if isinstance(result, dict) else None
                    if isinstance(trace, dict):
                        # Track accepted lineage only at post-completion time.
                        traces.append({
                            **trace,
                            "topic": str(tool_call["args"]["research_topic"])[:400],
                            "generation": state.get("research_generation", 0),
                        })
                if traces:
                    updates["research_trace"] = traces

                # 聚合所有的raw notes
                all_raw_notes = [
                    "\n".join(result.get("raw_notes", []))
                    for result in tool_results
                ]

            # 更新本次迭代状态信息
            updates["supervisor_messages"] = tool_messages
            updates["raw_notes"] = all_raw_notes

            return Command(goto=next_step, update=updates)

        except Exception as exc:
            logger.warning("Supervisor tool execution failed; finishing with available notes: %s", exc)
            return Command(
                goto=END,
                update={
                    **quarantine_update(state, get_notes_from_tool_calls(supervisor_messages)),
                    # 同上：不回写 research_brief（恒等透传 + 并发写冲突）
                }
            )



# ===== GRAPH CONSTRUCTION =====

supervisor_builder = StateGraph(SupervisorState)
supervisor_builder.add_node("supervisor", supervisor)
supervisor_builder.add_node("supervisor_tools", supervisor_tools)
supervisor_builder.add_edge(START, "supervisor")

supervisor_agent = supervisor_builder.compile()


if __name__ == "__main__":
    print(supervisor_agent.get_graph().draw_ascii())
