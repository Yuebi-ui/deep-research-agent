#***********************************************
#      Filename: draft_agent.py
#   Description: 根据用户需求生成报告草稿  
#***********************************************


"""该文件的作用是澄清用户问题和生成研究简要报
主要包含以下3个模块：
1. 用户问题澄清：确认是否有必要反问
2. 确定提纲：根据问题生成一个研究的简报
3. 报告草稿：生成一个初步的报告草稿
"""

from typing_extensions import Literal
from rich.markdown import Markdown
from rich.console import Console

from langchain_core.messages import HumanMessage, get_buffer_string
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command

from deep_research import logging as dr_logging
from deep_research.llm import draft_thinking, get_chat_model
from deep_research.prompts import RESEARCH_BRIEF_PROMPT, DRAFT_REPORT_PROMPT
from deep_research.research_seed import speculative_lineage
from deep_research.states import AgentState, AgentInputState
from deep_research.utils import get_today_str

logger = dr_logging.get_logger(__name__)


# 初始化模型（E9：thinking 策略由 DR_DRAFT_THINKING 控制，默认 on）。
# import-time 构造 → 切换开关必须重启 worker；preflight 会读 worker 进程的
# 实际环境校验。作用域仅此调用点（write_draft_report）；research_brief 的
# auto 路由与 refine_draft_report 不受影响（见 llm.draft_thinking 注释）。
draft_model = get_chat_model("draft", thinking=draft_thinking())


# ===== Langgraph的节点 =====

def write_research_brief(state: AgentState) -> Command[Literal["write_draft_report"]]:
    """根据用户的query生成一个研究提纲，内容包含需要调研哪些方面，注意事项等等"""

    logger.debug(
        "write research_brief invoked with %d messages", len(state.get("messages", []))
    )

    # 组装prompt
    prompt = RESEARCH_BRIEF_PROMPT.format(
        messages=get_buffer_string(state.get("messages", [])),
        date=get_today_str()
    )
    logger.debug("write_research_brief invoking model with prompt_length=%d", len(prompt))

    # 直接调用模型
    response = draft_model.invoke([HumanMessage(content=prompt)])
    research_brief = response.content
    logger.debug("write_research_brief produced research_brief length=%d", len(research_brief))

    # 更新Messages并回传给主agent
    return Command(
            goto="write_draft_report",
            update={"research_brief": research_brief}
        )

def write_draft_report(state: AgentState) -> Command[Literal["__end__"]]:
    """生成提纲生成一个研究报告的草稿"""

    logger.debug(
        "write_draft_report invoked with research_brief present=%s",
        bool(state.get("research_brief")),
    )

    # 组装prompt
    research_brief = state.get("research_brief", "")
    draft_report_prompt = DRAFT_REPORT_PROMPT.format(
        research_brief=research_brief,
        date=get_today_str()
    )

    # 直接调用模型
    response = draft_model.invoke([HumanMessage(content=draft_report_prompt)])
    draft_report = response.content
    logger.debug("write_draft_report produced draft_report length=%d", len(draft_report))

    # 不回写 research_brief：恒等透传没有必要，且在 Phase 5A 投机模式下与
    # supervisor_subgraph 同 superstep → 并发写 LastValue 通道会 InvalidUpdateError
    update = {
        "draft_report": draft_report,
    }
    # Phase 5A/5B：投机 lineage 下 research 分支与 draft 并行，supervisor 的首轮
    # 上下文由 write_research_brief 内产出的 research_seed 提供 —— 两个分支同时写
    # supervisor_messages 会被 add_messages 合并成"草稿+种子"两条，因此这里让位。
    # 判定依据是 **lineage 标记**（research_generation >= 1，由 brief 在投机起点写入），
    # 而不是 env 开关 —— 保证在途 lineage 在 restart/回滚后语义不变。
    # 串行 lineage（缺省 0）：行为与历史逐字一致。
    if not speculative_lineage(state):
        update["supervisor_messages"] = ["Here is the draft report: " + draft_report, research_brief]
    return update


if __name__ == "__main__":
    # 构建Graph
    deep_researcher_builder = StateGraph(AgentState, input_schema=AgentInputState)

    # 增加节点 
    deep_researcher_builder.add_node("write_research_brief", write_research_brief)
    deep_researcher_builder.add_node("write_draft_report", write_draft_report)

    # 增加边
    deep_researcher_builder.add_edge(START, "write_research_brief")
    deep_researcher_builder.add_edge("write_research_brief", "write_draft_report")
    deep_researcher_builder.add_edge("write_draft_report", END)

    # 编译graph
    draft_agent = deep_researcher_builder.compile()

    # 打印graph
    print(draft_agent.get_graph().draw_ascii())

    # 测试问题
    thread = {"recursion_limit": 50, "configurable": {"thread_id": "1"}}
    result = draft_agent.invoke({"messages": [HumanMessage(content="帮我写一个关于英伟达最新GPU的调研报告")]}, config=thread)

    # 输出
    console = Console()
    print("=====  Research Brief ====")
    console.print(Markdown(result["research_brief"]))
    print()

    print("=====  Draft Report ====")
    console.print(Markdown(result["draft_report"]))

