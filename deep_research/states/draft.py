#***********************************************
#      Filename: state_draft.py
#   Description: 报告草稿的结构化字段定义
#***********************************************

"""用于draft State格式定义。
这定义了用于State对象和结构化模式，包括状态管理和输入输出格式。
"""

import operator
from typing_extensions import Optional, Annotated, Sequence

from langchain_core.messages import BaseMessage
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ===== STATE DEFINITIONS =====

class AgentInputState(MessagesState):
    """Agent的输入状态 - 仅包含来自用户输入的消息"""
    pass

class AgentState(MessagesState):
    """多Agent深度研究系统的主状态。
    扩展 MessagesState，添加用于研究协调的附加字段。
    注意：为了正确定义状态，某些字段在不同的状态类中重复出现。子图与主工作流之间的管理。
    """

    research_brief: Optional[str]                                           # 根据用户对话历史生成的研究简报
    supervisor_messages: Annotated[Sequence[BaseMessage], add_messages]     # 与Supervisor Agent交换的协调消息
    raw_notes: Annotated[list[str], operator.add] = []                      # 研究阶段收集的原始未处理研究笔记
    notes: Annotated[list[str], operator.add] = []                          # 已处理和结构化的笔记，可用于生成报告
    draft_report: str                                                       # 研究报告草稿
    final_report: str                                                       # 最终格式化的研究报告
    verification_report: Optional[dict] = None                              # Claim 级事实核查报告（VerificationReport.model_dump()）
    writer_validation: Optional[dict] = None                               # Writer 引用完整性检查（静态告警）
    claim_verification_warning: str = ""                                    # 核查警告文本，注入到 final_report 生成 Prompt
    required_aspects: list[dict] = []                                   # V3 checkpointed brief checklist
    checklist_fingerprint: str = ""                                      # V3 freezes checklist across generations
    checklist_status: str = ""                                           # proposed / needs_confirmation
    quality_risk_tags: list[str] = []                                     # deterministic risk triggers
    evidence_coverage: Optional[dict] = None                             # V3 full-ledger structural coverage
    # ---- Generation-fenced parallel evidence lineage (V3 only) ----
    research_generation: int = 0                                            # active 研究代数（>=1 表示投机 lineage）
    quality_gate_version: int = 3                                           # V3-only checkpoint lineage
    quality_review: Optional[dict] = None                                  # independent evaluator verdict, latest generation
    red_team_review: Optional[dict] = None                                 # independent red-team verdict, latest generation
    quality_repair_count: int = 0                                           # V3 automatic research rounds consumed
    quality_repair_limit: int = 2                                           # V3 checkpointed max automatic rounds
    manual_research_count: int = 0                                          # V3 explicit human research requests
    manual_research_limit: int = 2                                          # V3 checkpointed max human rounds
    quality_repair_history: list[dict] = []                                 # V3 bounded provenance (replacement, not add)
    quality_gate_decision: Optional[dict] = None                            # V3 latest deterministic routing decision
    repair_request: Optional[dict] = None                                   # V3 targeted tasks (never authoritative findings)
    speculative_research: Optional[dict] = None
    # Episode candidates, never authoritative facts. Recorded after completion.
    research_trace: Annotated[list[dict], operator.add] = []
    supervisor_memory_hint: str = ""
                             # 隔离的投机研究结果 {generation, seed_fingerprint, notes}；ACCEPT 前不进入下游


# ===== STRUCTURED OUTPUT SCHEMAS =====

class ResearchQuestion(BaseModel):
    """用于生成结构化研究简报的字段定义"""

    research_brief: str = Field(
        description="A research question that will be used to guide the research.",
    )

class DraftReport(BaseModel):
    """用于生成结构化草稿报告的字段定义"""

    draft_report: str = Field(
        description="A draft report that will be used to guide the research.",
    )
