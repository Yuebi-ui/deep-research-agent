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
from deep_research.prompts import CRITICAL_ADDRESS_PROMPT, MULTI_STEP_DENOISE_PROMPT
from deep_research.research_seed import quarantine_update
from deep_research.agents.research_agent import researcher_agent
from deep_research.agents.red_team_agent import red_team_node
from deep_research.agents.evaluator_agent import evaluate_draft_quality
from deep_research.states import (
    SupervisorState,
    ConductResearch,
    ResearchComplete,
    QualityMetric
)
from deep_research.utils import get_today_str
from deep_research.tools import _think_tool, _refine_draft_report_tool
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

supervisor_tools = [ConductResearch, ResearchComplete, _think_tool, _refine_draft_report_tool]
# E1b：supervisor 的 thinking 策略（环境变量 DR_SUPERVISOR_THINKING，默认 on）
supervisor_model = get_chat_model("supervisor", thinking=supervisor_thinking())
# bind_tools 会丢失构造期 metadata —— 补挂 role 归因（仅观测，不改行为）
supervisor_model_with_tools = with_role_metadata(
    supervisor_model.bind_tools(supervisor_tools), "supervisor"
)


# System constants (最大迭代次数/最大并行Sub-Agents)
max_researcher_iterations = 3  # Calls to think_tool + ConductResearch + refine_draft_report
max_concurrent_researchers = 2  # 最大并行子agent数
min_need_repair_score = 6.0    # 评估低于这个分数，就要出发agent修复提醒


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
    # Use live historical clues when planning, then checkpoint the bounded hint
    # for later rounds. Requery only when quality feedback changes the problem.
    hint = state.get("supervisor_memory_hint", "") or ""
    should_refresh = iteration == 0 or bool(state.get("needs_quality_repair"))
    if stage_enabled() and should_refresh:
        query = str(state.get("research_brief", "") or "")[:400]
        if state.get("needs_quality_repair") and state.get("quality_history"):
            last = state["quality_history"][-1]
            feedback = last.get("feedback", "") if isinstance(last, dict) else getattr(last, "feedback", "")
            query = (query[:230] + " " + str(feedback)[:150])[:400]
        hint = await async_stage_context(query, "supervisor")
    if stage_enabled() and hint:
        messages.insert(1, HumanMessage(content=hint))

    # 动态上下文注入：检查并注入任何未处理的对抗性反馈，实现自我纠正机制。
    critiques = state.get("active_critiques", [])
    unaddressed = [c for c in critiques if not c.addressed]
    if unaddressed:
        critique_text = "\n".join([f"- {c.author} says: {c.concern}" for c in unaddressed])
        intervention = SystemMessage(content=CRITICAL_ADDRESS_PROMPT.format(critique_text=critique_text))
        messages.append(intervention)

    # 如果上一次迭代中质量得分较低，则会发出提醒
    if state.get("needs_quality_repair"):
        messages.append(SystemMessage(content="上一稿报告质量较低（得分低于7/10），请继续完善。"))

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
            "needs_quality_repair": False, # 在向supervisor发出提醒后，重置修复标志
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

    # 质量分数停滞检测：连续 2 次打分无提升 → 提前退出
    quality_history = state.get("quality_history", [])
    score_stagnated = False
    if len(quality_history) >= 2:
        recent = [q.get("score", 0) for q in quality_history[-2:]]
        if len(recent) == 2 and recent[0] >= recent[1]:
            score_stagnated = True
            logger.info("Quality scores stagnated (%s → %s), triggering early exit", recent[0], recent[1])

    # 如果超过则退出
    if exceeded_iterations or no_tool_calls or research_complete or score_stagnated:
        # 如果满足退出条件，我们会准备最终的、经过整理的notes。
        final_notes = get_notes_from_tool_calls(state.get("supervisor_messages", []))
        logger.info("[REPORT] The research is complete, writing the final report.")

        # 强制执行红队审查后再退出
        # 设置 final_exit 标志，red_team 节点看到后会跳转到 __end__
        #
        # Phase 5B：结果落点由 lineage 决定 —— 串行写 notes（历史行为）；
        # 投机 lineage 写 speculative_research（HITL ACCEPT 前**不进入下游**）。
        # 注意：**不要**回写 research_brief。它是恒等透传（值没变），
        # 但在 Phase 5A 投机模式里本子图与 draft 分支同 superstep，
        # 两边同时写 LastValue 通道会触发 InvalidUpdateError。
        return Command(
                goto="red_team",
                update={
                    **quarantine_update(state, final_notes),
                    "final_exit": True,
        })

    else:
        # 初始化变量
        tool_messages = []
        all_raw_notes = []
        draft_report = state.get("draft_report", "")
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

            refine_report_calls = [
                tool_call for tool_call in most_recent_message.tool_calls
                if tool_call["name"] == "refine_draft_report"
            ]

            logger.info(
                "[SUPERVISOR] supervisor_tools executing think=%d conduct=%d refine=%d",
                len(think_tool_calls),
                len(conduct_research_calls),
                len(refine_report_calls),
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
                        "research_topic": tool_call["args"]["research_topic"]
                    })
                    for tool_call in conduct_research_calls
                ]

                # 等待所有research agents 返回研究结果
                tool_results = await asyncio.gather(*coros)

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

            # 开始调用大模型结合已有信息修正调研报告
            for tool_call in refine_report_calls:
                if not state.get("draft_report", ""):
                    # Phase 5A：投机研究模式下 draft 与 research 并行，首轮 superstep
                    # 里 draft_report 尚不存在。必须回一条 ToolMessage（否则下一次
                    # 模型调用会因为 tool_call 没有对应响应而报错），并跳过 refine。
                    # 串行模式下 draft 必然存在 —— 此分支只在投机模式生效。
                    logger.info("[SUPERVISOR] refine_draft_report 跳过：draft 尚不可用")
                    tool_messages.append(ToolMessage(
                        content="No draft report is available yet. Continue researching; "
                                "the draft will be refined later.",
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"],
                    ))
                    continue

                findings = "\n".join(get_notes_from_tool_calls(state.get("supervisor_messages", [])))

                # 同步模型调用 → 线程（云端调用可达数十秒，不能阻塞事件循环，
                # 否则 heartbeat 会被饿死超过 claim TTL —— P0 根因修复）
                new_draft = await asyncio.to_thread(_refine_draft_report_tool.invoke, {
                    "research_brief": state.get("research_brief", ""),
                    "findings": findings,
                    "draft_report": state.get("draft_report", "")
                })

                # 执行Critical Step：Self-Evolution的评估
                eval_result = await asyncio.to_thread(
                    evaluate_draft_quality,
                    research_brief=state.get("research_brief", ""),
                    draft_report=new_draft,
                )
                logger.info(
                    "[EVALUATOR] comprehensive score=%f, accuracy score=%f, coherence score=%f",
                    eval_result.comprehensiveness_score,
                    eval_result.accuracy_score,
                    eval_result.coherence_score
                )
                logger.info(f"[EVALUATOR] scoing reason: {eval_result.reason}")

                if eval_result.missing_aspects:
                    logger.info("[EVALUATOR] missing aspects: %s", eval_result.missing_aspects)
                if eval_result.need_more_research:
                    logger.info("[EVALUATOR] recommending additional research")

                # 评估报告质量得分：(综合得分+准确率得分+一致性得分) / 3
                avg_score = (eval_result.comprehensiveness_score + eval_result.accuracy_score + eval_result.coherence_score) / 3

                # 构建质量评分消息
                quality_msg = f"Draft Updated.\nQuality Score: {avg_score:.1f}/10.\nJudge Feedback: {eval_result.reason}"
                if eval_result.missing_aspects:
                    quality_msg += f"\nMissing Aspects: {', '.join(eval_result.missing_aspects)}"

                # 把质量得分追加到tool message, 供Supervisor Agent参考
                tool_messages.append(ToolMessage(
                    content=quality_msg,
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"]
                ))

                draft_report = new_draft
                updates["draft_report"] = draft_report

                # 记录报告质量评分的记录，如果分数低于 min_need_repair_score，把repaire标志位置位true
                updates["quality_history"] = [QualityMetric(
                    score=avg_score,
                    feedback=eval_result.reason,
                    iteration=state.get("research_iterations", 0))
                ]

                if avg_score < min_need_repair_score or eval_result.need_more_research:
                    updates["needs_quality_repair"] = True

                # Reflection 闭环：如果有缺失方面，返回 supervisor 触发补充研究
                if eval_result.need_more_research and eval_result.missing_aspects:
                    missing = "\n".join(f"- {aspect}" for aspect in eval_result.missing_aspects)
                    hint_message = SystemMessage(content=(
                        f"评估发现以下方面需要补充研究：\n{missing}\n"
                        f"请为每个缺失方面分配 ConductResearch 任务进行补充调研。"
                    ))
                    tool_messages.append(hint_message)
                    next_step = "supervisor"  # 回到 supervisor 分配新研究任务
                else:
                    # 跳转到self-correction节点 (Red Team)
                    next_step = "red_team"

            # 更新本次迭代状态信息
            updates["supervisor_messages"] = tool_messages
            updates["raw_notes"] = all_raw_notes

            return Command(goto=next_step, update=updates)

        except Exception as exc:
            logger.warning("Supervisor tool execution failed; finishing with available notes: %s", exc)
            return Command(
                goto=END,
                update={
                    # Phase 5B：同 exit 路径 —— 结果落点按 lineage 选择
                    # （串行 → notes；投机 → speculative_research 隔离）。
                    **quarantine_update(state, get_notes_from_tool_calls(supervisor_messages)),
                    # 同上：不回写 research_brief（恒等透传 + 并发写冲突）
                }
            )



# ===== GRAPH CONSTRUCTION =====

supervisor_builder = StateGraph(SupervisorState)
supervisor_builder.add_node("supervisor", supervisor)
supervisor_builder.add_node("supervisor_tools", supervisor_tools)
supervisor_builder.add_node("red_team", red_team_node)

supervisor_builder.add_edge(START, "supervisor")
supervisor_builder.add_edge("supervisor", "supervisor_tools")

# 注意：此处**不能**再加 add_edge("red_team", "supervisor")。
#
# red_team_node 的每一条返回路径都显式返回 Command(goto=...)，其中
# final_exit 模式下返回的是 Command(goto=END)。而 LangGraph 中静态出边
# 会**覆盖**节点返回的 Command(goto=...)：一旦存在 red_team -> supervisor
# 这条静态边，red_team 的 Command(goto=END) 就会被忽略，子图永远无法
# 通过红队路径终止，只能撞到 recursion_limit 才停下。
#
# 该静态边相对于 red_team 的显式路由是完全冗余的（所有路径都已指定目标），
# 移除它即可恢复 red_team docstring 中声明的语义：
# "如果是 final_exit 模式，审查后直接跳转到 END"。

supervisor_agent = supervisor_builder.compile()


if __name__ == "__main__":
    print(supervisor_agent.get_graph().draw_ascii())
