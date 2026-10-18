"""Selective adversarial review for Quality Gate V3 only."""

from langchain_core.messages import HumanMessage
from deep_research.llm import get_chat_model

red_team_model = get_chat_model("red_team")

async def review_research_bundle_v3(
    research_brief: str, draft_report: str, research_notes: str,
    *, checklist: str, evidence_manifest: str,
) -> dict:
    """V3 Red Team: structured counterexamples, no repeated quality scoring."""
    from deep_research.prompts.research_quality import RESEARCH_RED_TEAM_V3_PROMPT
    from deep_research.utils import parse_json_response

    prompt = RESEARCH_RED_TEAM_V3_PROMPT.format(
        research_brief=research_brief, draft_report=draft_report,
        research_notes=research_notes, checklist=checklist,
        evidence_manifest=evidence_manifest,
    )
    response = await red_team_model.ainvoke([HumanMessage(content=prompt)])
    result = parse_json_response(str(response.content or ""))
    if not isinstance(result, dict) or type(result.get("passed")) is not bool:
        raise ValueError("V3 Red Team returned invalid passed status")
    issues = result.get("issues")
    if not isinstance(issues, list) or len(issues) > 3:
        raise ValueError("V3 Red Team returned invalid issues")
    from deep_research.states.eval_result import QualityIssue
    clean = [QualityIssue(**issue).model_dump() for issue in issues]
    if any(x["kind"] not in ("counterevidence", "logic_risk") for x in clean):
        raise ValueError("Red Team must report counterevidence/logic_risk only")
    if result["passed"] and clean or not result["passed"] and not clean:
        raise ValueError("Red Team passed flag contradicts issues")
    return {"passed": result["passed"], "feedback": str(result.get("feedback") or "")[:2400],
            "issues": clean}
