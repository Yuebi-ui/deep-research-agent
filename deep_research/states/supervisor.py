#***********************************************
#      Filename: state_supervisor.py
#   Description: Supervisor智能体的结构化字段定义
#***********************************************

"""
多智能体Supervisor的State定义
本文件定义了多智能体Supervisor工作流程中使用的State对象和tools字段定义。
"""

import operator
from typing_extensions import Annotated, Optional, Sequence, TypedDict, List

from langchain_core.messages import BaseMessage
from langchain_core.tools import tool
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field



class SupervisorState(TypedDict):
    """
    多智能体Supervisor的State定义。
    负责协调Supervisor和Research Agents之间的工作，跟踪研究进展并汇总来自多个Sub-Agent的研究成果。
    """

    supervisor_messages: Annotated[Sequence[BaseMessage], add_messages] # Supervisor信息,用于协调和传递信息
    research_brief: str                                                 # 指导整体研究方向的详细研究简报
    notes: Annotated[list[str], operator.add] = []                      # 已处理和结构化的笔记，可用于生成最终报告
    research_iterations: int = 0                                        # 跟踪研究迭代次数的计数器
    raw_notes: Annotated[list[str], operator.add] = []
    draft_report: str
    # ---- Phase 5B：与父图同名的 generation/fencing 字段（子图只读 generation，
    #      投机 lineage 下把收尾结果写进 speculative_research 而非 notes）----
    research_generation: int = 0                                        # active 研究代数（从父图流入）
    quality_gate_version: int = 3                                       # V3-only research graph
    speculative_research: Optional[dict] = None
    required_aspects: list[dict] = []
    checklist_fingerprint: str = ""
    research_evidence: Annotated[list[dict], operator.add] = []  # V3 tool-observed ledger
    # Observed tool traces from child Researchers; additive across supervisor rounds.
    research_trace: Annotated[list[dict], operator.add] = []
    # Advisory stage hints are checkpointed, but never appended to chat history.
    supervisor_memory_hint: str = ""
                         # 隔离的投机结果（由 quarantine_update 写入）

@tool
class ConductResearch(BaseModel):
    """用于将研究任务委派给专业子Agent (specialized sub-agent) 的工具。"""
    research_topic: str = Field(
        description="研究主题。每次委派的任务应该为单一主题，并需详细描述（至少一个段落）。",
    )
    aspect_id: str = Field(
        default="",
        description="V3 研究清单中的 aspect_id（必须精确匹配，无法匹配则留空）。",
    )

@tool
class ResearchComplete(BaseModel):
    """用于指示研究过程已完成的工具。"""
    pass
