#***********************************************
#      Filename: research_agent.py
#   Description:  研究智能体
#***********************************************

"""Research Agent核心实现
该文件实现了一个Research Agent，它可以执行迭代式网络搜索和综合分析，以回答复杂的研究问题。
"""


from typing_extensions import Literal
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, filter_messages

from deep_research.llm import get_chat_model, with_role_metadata
from deep_research.states import ResearcherState, ResearcherOutputState
from deep_research.utils import get_today_str
from deep_research.tools import _tavily_search_tool, _think_tool
from deep_research.prompts import RESEARCH_AGENT_PROMPT, COMPRESS_RESEARCH_SYSTEM_PROMPT, COMPRESS_RESEARCH_HUMAN_PROMPT
from deep_research import logging as dr_logging
from deep_research.memory.stage_retrieval import stage_enabled, retrieve_stage_context
from deep_research.memory.episodes import extract_research_trace

logger = dr_logging.get_logger(__name__)


# ===== CONFIGURATION =====

MAX_RESEARCHER_TOOL_ITERATIONS = 4  # 每个 Research Agent 最多搜索+反思 4 轮

# 初始化tools
tools = [_tavily_search_tool, _think_tool]
tools_by_name = {tool.name: tool for tool in tools}

# 初始化模型
model = get_chat_model("researcher_main")
# bind_tools 会丢失构造期 metadata —— 补挂 role 归因（仅观测，不改行为）
model_with_tools = with_role_metadata(model.bind_tools(tools), "researcher_main")
compress_model = get_chat_model("researcher_compressor")


# ===== AGENT NODES =====

def llm_call(state: ResearcherState):
    """根据当前状态决策下一步的动作"""

    msg_count = len(state.get("researcher_messages", []))
    logger.debug("llm_call invoked with %d messages", msg_count)

    # Stage 5: fetch relevant historical hints once per delegated topic,
    # and optionally once again after substantial search progress. A sync
    # LangGraph node runs in its executor; no blocking work is added to asyncio.
    hint = state.get("researcher_memory_hint", "") or ""
    recalled_round = state.get("researcher_memory_round", 0)
    turn = state.get("tool_call_iterations", 0)
    query = str(state.get("research_topic", "") or "")[:400]
    should_refresh = (recalled_round == 0) or (turn >= 2 and recalled_round == 1)
    if stage_enabled() and should_refresh:
        if turn >= 2:
            # New queries are only a retrieval key, never trusted instructions.
            recent = []
            for message in list(state.get("researcher_messages", []))[-8:]:
                for call in getattr(message, "tool_calls", None) or []:
                    if call.get("name") == "tavily_search" and isinstance(call.get("args"), dict):
                        text = call["args"].get("query", "")
                        if isinstance(text, str):
                            recent.append(text[:140])
            if recent:
                query = (query[:240] + " " + recent[-1])[:400]
        try:
            hint = retrieve_stage_context(query, "researcher")
        except Exception as exc:  # defensive: never fail the research graph
            logger.warning("Researcher stage memory unavailable: %s", exc)
            hint = ""
        recalled_round = 2 if turn >= 2 else 1

    # Memory is a separate, bounded historical reference, not a user message.
    messages = [SystemMessage(content=RESEARCH_AGENT_PROMPT)]
    if stage_enabled() and hint:
        messages.append(HumanMessage(content=hint))
    response = model_with_tools.invoke(messages + state["researcher_messages"])

    logger.info(
        "llm_call produced response tool_calls=%s num_tool_calls=%d",
        bool(response.tool_calls),
        len(response.tool_calls or []),
    )
    return {
        "researcher_messages": [response],
        "researcher_memory_hint": hint,
        "researcher_memory_round": recalled_round,
    }

def tool_node(state: ResearcherState):
    """根据前一次大模型结果执行所有工具调用"""

    tool_calls = state["researcher_messages"][-1].tool_calls
    logger.info("tool_node executing %d tool calls", len(tool_calls or []))

    # 获取已搜索过的 URL 集合，用于跨轮去重
    seen_urls_set = set(state.get("seen_urls", []))
    new_urls: list[str] = []

    # 调用工具
    observations = []
    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        logger.info("Invoking tool %s with args=%s", tool_name, tool_call["args"])
        try:
            tool = tools_by_name[tool_name]
            result = tool.invoke(tool_call["args"])
        except Exception as exc:
            # A failed search is observable and recoverable; do not elevate it
            # to a permanent lesson, or leak credentials/exception details.
            logger.warning("Research tool %s failed (%s)", tool_name, type(exc).__name__)
            result = f"[SEARCH_ERROR] Tool {tool_name} unavailable; choose a different query or source."
        observations.append(result)

    # 从搜索结果中提取 URL 用于跨轮去重
    for obs in observations:
        if isinstance(obs, str) and "URL:" in obs:
            for line in obs.split("\n"):
                if line.strip().startswith("URL:"):
                    url = line.strip()[4:].strip()
                    if url and url not in seen_urls_set:
                        new_urls.append(url)

    if new_urls:
        logger.info("tool_node found %d new URLs, total seen=%d", len(new_urls), len(seen_urls_set) + len(new_urls))

    # 获取工具输出
    tool_outputs = [
        ToolMessage(
            content=observation,
            name=tool_call["name"],
            tool_call_id=tool_call["id"]
        ) for observation, tool_call in zip(observations, tool_calls)
    ]

    return {
        "researcher_messages": tool_outputs,
        "tool_call_iterations": state.get("tool_call_iterations", 0) + 1,
        "seen_urls": new_urls,
    }

def compress_research(state: ResearcherState) -> dict:
    """把研究发现压缩为高价值摘要，只保留有用信息."""

    # 组装prompt
    system_message = COMPRESS_RESEARCH_SYSTEM_PROMPT.format(date=get_today_str())
    messages = [SystemMessage(content=system_message)] + state.get("researcher_messages", []) +\
            [HumanMessage(content=COMPRESS_RESEARCH_HUMAN_PROMPT.format(research_topic=state.get("research_topic", "")))]
    logger.info("compress_research invoked with %d messages", len(messages))

    # 调用summary模型
    response = compress_model.invoke(messages)

    # 从messages和tools抽取raw notes
    raw_notes = [
        str(m.content) for m in filter_messages(
            state["researcher_messages"],
            include_types=["tool", "ai"]
        )
    ]

    logger.debug("compress_research produced raw_notes_count=%d", len(raw_notes))
    compressed = str(response.content)
    return {
        "compressed_research": compressed,
        "raw_notes": ["\n".join(raw_notes)],
        "episode_trace": extract_research_trace(state, compressed),
    }

# ===== ROUTING LOGIC =====

def should_continue(state: ResearcherState) -> Literal["tool_node", "compress_research"]:
    """Determine whether to continue research or provide final answer."""
    messages = state["researcher_messages"]
    last_message = messages[-1]
    iterations = state.get("tool_call_iterations", 0)

    # 超过迭代上限，强制压缩结束
    if iterations >= MAX_RESEARCHER_TOOL_ITERATIONS:
        logger.info("should_continue: hit max iterations (%d), forcing compress", MAX_RESEARCHER_TOOL_ITERATIONS)
        return "compress_research"

    decision = "tool_node" if last_message.tool_calls else "compress_research"
    logger.info("should_continue decision=%s (has_tool_calls=%s, iter=%d)", decision, bool(last_message.tool_calls), iterations)
    return decision


# ===== GRAPH CONSTRUCTION =====

# Build the agent
agent_builder = StateGraph(ResearcherState, output_schema=ResearcherOutputState)

# Add nodes to the graph
agent_builder.add_node("llm_call", llm_call)
agent_builder.add_node("tool_node", tool_node)
agent_builder.add_node("compress_research", compress_research)

# Add edges to connect nodes
agent_builder.add_edge(START, "llm_call")
agent_builder.add_conditional_edges(
    "llm_call",
    should_continue,
    {
        "tool_node": "tool_node", # Continue research loop
        "compress_research": "compress_research", # 返回 final answer
    },
)
agent_builder.add_edge("tool_node", "llm_call") # 继续搜索获得更多结果
agent_builder.add_edge("compress_research", END)

# Compile the agent
researcher_agent = agent_builder.compile()

if __name__ == "__main__":
    print(researcher_agent.get_graph().draw_ascii())
