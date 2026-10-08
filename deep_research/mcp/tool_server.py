"""MCP 工具服务封装。

将现有 Agent 工具以 MCP 协议标准暴露，支持通过 langchain-mcp-adapters 动态加载。
包括 stdio transport 模式的 MCP Server 入口和 Client 集成辅助函数。
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from deep_research.tools.tool import tavily_search, think_tool, refine_draft_report
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

mcp = FastMCP("deep-research-tools")


@mcp.tool()
def mcp_tavily_search(query: str, max_results: int = 3, topic: str = "general") -> str:
    """在 Web 上搜索信息，返回去重并摘要后的搜索结果。

    Args:
        query: 搜索关键词
        max_results: 返回的最大结果数，默认 3
        topic: 搜索主题 (general/news/finance)，默认 general
    """
    return tavily_search(query, max_results=max_results, topic=topic)


@mcp.tool()
def mcp_think_tool(reflection: str) -> str:
    """用于研究过程中的策略反思。在每次搜索后使用此工具分析结果并规划下一步。

    Args:
        reflection: 关于研究进展、发现、差距和后续步骤的详细反思
    """
    return think_tool(reflection)


@mcp.tool()
def mcp_refine_draft(refinement_instruction: str, draft_report: str) -> str:
    """根据新的研究发现或用户反馈完善报告草稿。

    Args:
        refinement_instruction: 修改说明或新发现
        draft_report: 当前报告草稿
    """
    return refine_draft_report(
        research_brief=refinement_instruction,
        findings=refinement_instruction,
        draft_report=draft_report,
    )


def start_mcp_server():
    """启动 MCP Server（stdio transport），供外部 Client 连接。"""
    logger.info("Starting MCP server (stdio transport)")
    mcp.run(transport="stdio")


def get_mcp_tools() -> list[dict[str, Any]]:
    """返回 MCP 工具的元数据列表，供 Agent 注册使用。"""
    return [
        {
            "name": "tavily_search",
            "description": "Web 搜索引擎，获取网页信息并进行智能摘要",
            "parameters": {
                "query": "搜索关键词",
                "max_results": "返回结果数量 (默认 3)",
                "topic": "搜索主题: general/news/finance",
            },
        },
        {
            "name": "think_tool",
            "description": "策略反思工具，分析研究进展与差距",
            "parameters": {"reflection": "反思内容"},
        },
        {
            "name": "refine_draft_report",
            "description": "精炼报告草稿",
            "parameters": {"refinement_instruction": "修改说明", "draft_report": "当前草稿"},
        },
    ]


if __name__ == "__main__":
    start_mcp_server()
