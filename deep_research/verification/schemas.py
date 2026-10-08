"""Claim 核查的数据结构定义。"""

from typing import Literal
from pydantic import BaseModel, Field


class ClaimVerdict(BaseModel):
    """单条 Claim 的核查判决。"""
    claim_text: str = Field(description="被核查的原始断言文本")
    verdict: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNVERIFIABLE"] = Field(
        description="核查结论：SUPPORTED=有明确证据支持, PARTIAL=部分支持, UNSUPPORTED=无证据支持, UNVERIFIABLE=无法核查"
    )
    evidence: str = Field(default="", description="找到的证据摘要")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="核查置信度 0-1")


class VerificationReport(BaseModel):
    """完整的核查报告。"""
    total_claims: int
    supported: int
    partial: int
    unsupported: int
    unverifiable: int = 0
    hallucination_rate: float = Field(default=0.0, description="幻觉率 = (unsupported + unverifiable) / total")
    verified_rate: float = Field(default=0.0, description="验证通过率 = (supported + partial) / total")
    details: list[ClaimVerdict] = Field(default_factory=list)

    @classmethod
    def from_verdicts(cls, verdicts: list[ClaimVerdict]) -> "VerificationReport":
        supported = sum(1 for v in verdicts if v.verdict == "SUPPORTED")
        partial = sum(1 for v in verdicts if v.verdict == "PARTIAL")
        unsupported = sum(1 for v in verdicts if v.verdict == "UNSUPPORTED")
        unverifiable = sum(1 for v in verdicts if v.verdict == "UNVERIFIABLE")
        total = len(verdicts)
        return cls(
            total_claims=total,
            supported=supported,
            partial=partial,
            unsupported=unsupported,
            unverifiable=unverifiable,
            hallucination_rate=(unsupported + unverifiable) / total if total > 0 else 0.0,
            verified_rate=(supported + partial) / total if total > 0 else 0.0,
            details=verdicts,
        )
