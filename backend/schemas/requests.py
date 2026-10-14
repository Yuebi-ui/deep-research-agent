"""API 请求模型。"""

from pydantic import BaseModel, Field, model_validator


class ResearchRequest(BaseModel):
    """提交研究任务的请求。"""
    query: str = Field(..., min_length=1, max_length=5000, description="研究主题/问题")
    # 观测用 run 标识（可选）。不传时行为与改造前完全一致 —— worker 侧
    # 会回退为 auto-<thread_id>。仅用于 baseline 指标归因，不参与业务逻辑。
    run_id: str | None = Field(
        None,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]{1,64}$",
        description="baseline 观测 run 标识（可选）",
    )


class ReviewAction(BaseModel):
    """Human Review 审查决定。"""
    action: str = Field(
        ..., pattern="^(approve|revise|revise_draft|more_research)$",
        description="审查动作；V2 支持结构调整 revise_draft 与定向补研 more_research",
    )
    feedback: str = Field(default="", description="修改草稿或定向补研的具体要求；V2 专用动作限 500 字")

    @model_validator(mode="after")
    def validate_research_request(self):
        if self.action in ("more_research", "revise_draft") and not (0 < len(self.feedback.strip()) <= 500):
            raise ValueError("V2 more_research/revise_draft require 1-500 characters of feedback")
        return self
