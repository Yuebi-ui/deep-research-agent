"""流式输出回调：实时展示多 Agent 协作进度。

基于 LangGraph astream_events() API，监听节点切换、工具调用、质量评分等事件。
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

_NODE_LABELS = {
    "write_research_brief": "研究简报生成",
    "write_draft_report": "报告草稿生成",
    "human_review": "人工审查",
    "supervisor": "Supervisor 决策",
    "supervisor_tools": "工具执行",
    "final_report_generation": "最终报告生成",
    "llm_call": "Research Agent 思考",
    "tool_node": "工具调用",
    "compress_research": "研究结果压缩",
    "red_team": "Red Team 对抗审查",
}


async def stream_agent(full_agent, user_input: dict, thread_config: dict):
    """流式运行 Agent 并实时打印进度。

    用法：
        async for event in stream_agent(full_agent, input_data, thread_config):
            print(event)  # 每个事件是一个进度描述
    """
    config = {
        **thread_config,
        "recursion_limit": 50,
        "configurable": {**thread_config.get("configurable", {})},
    }

    node_step = 0
    console.print(Panel("[bold cyan]Deep Research Agent 启动[/bold cyan]", style="cyan"))

    try:
        async for event in full_agent.astream_events(user_input, config=config, version="v2"):
            kind = event.get("event", "")
            name = event.get("name", "")

            if kind == "on_chain_start" and name in _NODE_LABELS:
                node_step += 1
                style = _style_for_node(name)
                console.print(Text(f"  [{node_step}] {_NODE_LABELS.get(name, name)} ...", style=style))

            elif kind == "on_tool_start":
                tool_name = name
                tool_input = event.get("data", {}).get("input", "")
                if tool_name == "tavily_search" and isinstance(tool_input, dict):
                    query = tool_input.get("query", str(tool_input))
                    console.print(Text(f"     搜索: {query}", style="dim blue"))
                elif tool_name == "think_tool" and isinstance(tool_input, dict):
                    ref = tool_input.get("reflection", str(tool_input))[:120]
                    console.print(Text(f"     反思: {ref}...", style="dim yellow"))
                elif tool_name == "ConductResearch" and isinstance(tool_input, dict):
                    topic = tool_input.get("research_topic", str(tool_input))[:150]
                    console.print(Text(f"     启动 Research Agent: {topic}", style="dim green"))
                elif tool_name == "refine_draft_report":
                    console.print(Text("     修正报告草稿...", style="dim magenta"))
                elif tool_name == "ResearchComplete":
                    console.print(Text("     研究完成", style="bold green"))

            elif kind == "on_custom_event":
                custom_name = name
                data = event.get("data", {})
                if "quality_score" in custom_name or "quality_score" in str(data):
                    console.print(Text(f"     报告质量评分: {data}", style="bold yellow"))

            yield event

    except Exception as e:
        console.print(Panel(f"[bold red]Error: {e}[/bold red]", style="red"))
        raise

    console.print(Panel("[bold green]Deep Research 完成[/bold green]", style="green"))


def _style_for_node(name: str) -> str:
    if "supervisor" in name:
        return "bold cyan"
    if "research" in name or "llm_call" in name or "tool" in name:
        return "dim green"
    if "red_team" in name:
        return "bold red"
    if "final" in name or "draft" in name or "brief" in name:
        return "bold yellow"
    return "white"
