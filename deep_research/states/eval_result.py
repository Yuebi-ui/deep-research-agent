#***********************************************
#      Filename: eval_result.py
#   Description: 报告评估结果结构化输出
#***********************************************

from pydantic import BaseModel, Field


class EvaluationResult(BaseModel):

    # 综合性得分：0-10分衡量是否覆盖了简报的所有方面
    comprehensiveness_score: float = Field(description="0-10 score on coverage")

    # 准确性得分：衡量报告是否有事实依据
    accuracy_score: float = Field(description="0-10 score on factual grounding")

    # 一致性得分：衡量报告是否有逻辑缺陷，以及报告是否流畅，可读性良好
    coherence_score: float = Field(description="0-10 score on flow")

    # 打分原因，用于改善报告质量
    reason: str = Field(description="Feedback for the researcher")

    # 结构化缺口列表 — 供 Supervisor 触发补充研究
    missing_aspects: list[str] = Field(
        default_factory=list,
        description="Specific topics or aspects missing from the draft (e.g., cost analysis, latest cases)",
    )

    # 是否需要补充研究
    need_more_research: bool = Field(
        default=False,
        description="True if any missing_aspects require additional web research",
    )
