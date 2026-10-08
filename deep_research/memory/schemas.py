"""结构化记忆的 Schema 定义。Entity / Claim / Evidence / Contradiction 四层。"""

from __future__ import annotations
import time
from typing import Literal
from pydantic import BaseModel, Field


class Entity(BaseModel):
    """研究过程中发现的关键实体（技术、人物、组织、概念）。"""
    id: str = ""
    name: str
    type: Literal["technology", "person", "organization", "concept", "dataset", "metric"]
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    first_seen: float = Field(default_factory=time.time)
    last_updated: float = Field(default_factory=time.time)
    importance: float = Field(default=5.0, ge=0.0, le=10.0, description="重要性评分 0-10")


class MemoryClaim(BaseModel):
    """从研究中提取的断言（区别于 verification 模块的 ClaimVerdict）。"""
    id: str = ""
    text: str
    entities: list[str] = Field(default_factory=list, description="关联的 Entity ID 列表")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_url: str | None = None
    source_title: str | None = None
    report_id: str = ""  # 来源报告；兼容历史记录（缺省为空）
    verification_status: Literal["unverified", "supported", "disputed", "refuted"] = "unverified"
    first_seen: float = Field(default_factory=time.time)
    last_seen: float = Field(default_factory=time.time)
    # None means unknown (legacy records must not get invented historical dates).
    valid_from: str | None = None
    valid_to: str | None = None
    source_published_at: str | None = None
    observed_at: float | None = None
    supersedes_claim_id: str | None = None


class Evidence(BaseModel):
    """支持或反对某条 Claim 的证据。"""
    id: str = ""
    claim_id: str = ""
    report_id: str = ""  # 来源报告（不可作为独立验证结论）
    type: Literal["experiment", "benchmark", "paper", "case_study", "blog", "official_doc"]
    description: str = ""
    url: str | None = None
    strength: float = Field(default=0.5, ge=0.0, le=1.0, description="证据力度 0-1")
    timestamp: float = Field(default_factory=time.time)


class Contradiction(BaseModel):
    """两条 Claim 之间的冲突。"""
    id: str = ""
    claim_a_id: str = ""
    claim_b_id: str = ""
    report_id: str = ""
    description: str = ""
    resolution: str | None = None
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    # "possible_change" is a candidate across reports, not a proven conflict.
    relation_type: Literal["explicit_conflict", "possible_change"] = "explicit_conflict"
    review_status: Literal["unverified", "confirmed", "dismissed"] = "unverified"
    # Explicit dates only; never inferred from observed_at or a document timestamp.
    interval_relation: Literal["unknown", "disjoint", "overlap", "invalid"] = "unknown"
    claim_a_report_id: str = ""
    claim_b_report_id: str = ""
    timestamp: float = Field(default_factory=time.time)
