#***********************************************
#      Filename: evaluator_agent.py
#   Description: 报告评估智能体
#***********************************************

from langchain_core.messages import HumanMessage

from deep_research.llm import get_chat_model
from deep_research.states import EvaluationResult
from deep_research.prompts import DRAFT_EVALUATOR_PROMPT
from deep_research.utils import parse_json_response


# 初始化Judge Model
judge_model = get_chat_model("evaluator")


def evaluate_draft_quality(research_brief: str, draft_report: str) -> EvaluationResult:
    """
    此函数实现了"self-evolution"评分机制。用另一个LLM作为评判者，以评估报告草稿相对于原始简报的质量。
    打分的分数和原因会返回给supervisor智能体。
    """

    # 组装prompt
    eval_prompt = DRAFT_EVALUATOR_PROMPT.format(
            research_brief = research_brief,
            draft_report = draft_report
    )

    # 直接调用模型，手动解析 JSON
    response = judge_model.invoke([HumanMessage(content=eval_prompt)])
    data = parse_json_response(response.content)
    return EvaluationResult(**data)
