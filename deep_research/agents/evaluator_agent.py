#***********************************************
#      Filename: evaluator_agent.py
#   Description: 报告评估智能体
#***********************************************

from langchain_core.messages import HumanMessage

from deep_research.llm import get_chat_model
from deep_research.states import EvaluationResult
from deep_research.utils import parse_json_response


# 初始化Judge Model
judge_model = get_chat_model("evaluator")


async def evaluate_research_readiness(
    research_brief: str, draft_report: str, research_notes: str,
    *, checklist: str = "", evidence_manifest: str = "", version: int = 3,
) -> EvaluationResult:
    """Independent research *readiness* assessment over draft AND evidence.

    Called by the parent graph after the parallel join, not by refine_draft_report.
    ``ainvoke`` avoids blocking the worker heartbeat during model calls.
    """
    from deep_research.prompts.research_quality import RESEARCH_READINESS_V3_PROMPT
    if version != 3:
        raise ValueError("only V3 evaluator is available")
    template = RESEARCH_READINESS_V3_PROMPT
    prompt = template.format(
        research_brief=research_brief,
        draft_report=draft_report,
        research_notes=research_notes, checklist=checklist, evidence_manifest=evidence_manifest,
    )
    response = await judge_model.ainvoke([HumanMessage(content=prompt)])
    data = parse_json_response(response.content)
    if version == 3 and (not isinstance(data, dict) or "issues" not in data):
        raise ValueError("V3 evaluator must return structured issues")
    return EvaluationResult(**data)
