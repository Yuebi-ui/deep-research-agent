"""API 响应模型。

执行计划 §7.3：主要 API **不得**返回裸 ``dict`` / ``Any`` / 形状不定的 JSON。
本模块为每个端点提供明确 schema，OpenAPI 才能生成可用的 TS 类型。

字段集合与既有 SSE/客户端契约保持一致（`external UI client`），
因此引入类型化不会造成响应变化。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ===== 统一错误契约（§7.4）=====

class ErrorDetail(BaseModel):
    """错误主体。"""

    code: str = Field(..., description="稳定的机器可读错误码")
    message: str = Field(..., description="人类可读的说明")
    request_id: str | None = Field(None, description="用于关联日志的请求 ID")
    details: dict[str, Any] | None = Field(None, description="补充上下文（可选）")


class ErrorResponse(BaseModel):
    """所有错误响应的统一结构。

    **不要把 Python traceback 返回给前端**（§7.4）。
    """

    error: ErrorDetail


# ===== 健康检查 =====

class HealthResponse(BaseModel):
    status: str = "ok"
    components: dict[str, str] = Field(default_factory=dict)


# ===== 任务 =====

class TaskCreatedResponse(BaseModel):
    """POST /api/research/start 的响应。"""

    thread_id: str
    status: str
    created_at: str


class TaskSummaryResponse(BaseModel):
    """任务状态快照。

    字段与改造前的 `agent_service.get_task_status()` 输出一致，
    另加 Phase G 引入的运行时元数据（attempt / cancel_requested）。
    """

    thread_id: str
    status: str
    stage: str = ""
    query: str = ""
    draft_report: str = ""
    final_report: str = ""
    verification: dict[str, Any] | None = None
    error: str = ""

    # Phase G runtime metadata（只读观测字段）
    attempt: int = 0
    cancel_requested: bool = False


class TaskListItem(BaseModel):
    """历史任务列表项。"""

    thread_id: str
    query: str
    status: str
    created_at: str
    updated_at: str


class ReviewResponse(BaseModel):
    ok: bool = True
    thread_id: str


class CancelResponse(BaseModel):
    ok: bool = True
    thread_id: str
    status: str


class DeleteResponse(BaseModel):
    deleted: bool = True


class ReportResponse(BaseModel):
    thread_id: str
    query: str = ""
    final_report: str = Field("", description="最终报告正文")
    verification: dict[str, Any] | None = None
    draft_report: str = ""
    generated_at: str | None = None


# ===== 事件（SSE）=====

#: SSE 事件的稳定 wire 名（前端 switch 消费的就是这些）
SSEEventName = Literal[
    "node_start",
    "node_complete",
    "tool_call",
    "report_chunk",
    "human_review_required",
    "complete",
    "error",
]


class SSEEventEnvelope(BaseModel):
    """SSE 事件的稳定 envelope（§8）。

    `data` 内是事件载荷；其余字段是**envelope 元数据**，供客户端做
    去重、排序与关联，前端可以忽略它们。

    同时保留历史的 `event` 字段名 —— 前端已依赖它，改名会破坏契约。
    """

    event: str = Field(..., description="wire 事件名（前端契约）")
    data: dict[str, Any] = Field(default_factory=dict)

    event_id: str | None = Field(None, description="Redis Stream ID，用于 Last-Event-ID 重连")
    task_id: str | None = None
    sequence: int | None = Field(None, description="任务内单调递增序号")
    timestamp: str | None = None
    schema_version: str = Field("1", description="envelope 结构版本")


# ===== Observability =====

class TraceNode(BaseModel):
    """执行轨迹树的一个节点。"""

    id: str
    name: str
    label: str = ""
    status: str = ""
    duration_ms: float | None = None
    children: list[TraceNode] = Field(default_factory=list)


class CostSummary(BaseModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_rmb: float = 0.0


class CostByModel(BaseModel):
    model_config = {"extra": "allow"}

    input_tokens: int = 0
    output_tokens: int = 0
    cost_rmb: float = 0.0
    calls: int = 0


class CostStatsResponse(BaseModel):
    total_cost_rmb: float = 0.0
    total_tokens: int = 0
    by_model: dict[str, CostByModel] = Field(default_factory=dict)
    count: int = 0


class AlertsResponse(BaseModel):
    """告警概览。

    阈值可配置、维度可扩展，因此 `alerts` 内部保留开放结构
    —— 属 §7.3 允许的「字段本质上就是开放 JSON metadata」。
    """

    alerts_count: int = 0
    thresholds: dict[str, float] = Field(default_factory=dict)
    alerts: list[dict[str, Any]] = Field(default_factory=list)


class TraceGraphResponse(BaseModel):
    """执行轨迹图（nodes / edges）。

    图结构本身是开放的（节点类型会随 Agent 演进增加），因此节点与边
    声明为 ``extra="allow"``：已知字段有类型，未知字段原样透传，
    不会因为加了 response_model 而把新字段悄悄过滤掉。
    """

    model_config = {"extra": "allow"}

    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class CostDetailResponse(BaseModel):
    """单任务成本明细。

    字段与前端 `CostDashboard.tsx` 消费的保持一致。
    `model_stats` 以模型名为键、`records` 为原始记录列表，二者都是
    开放结构。
    """

    model_config = {"extra": "allow"}

    thread_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_rmb: float = 0.0
    model_stats: dict[str, CostByModel] = Field(default_factory=dict)
    records: list[dict[str, Any]] = Field(default_factory=list)


class StatsResponse(BaseModel):
    """全局统计摘要。"""

    model_config = {"extra": "allow"}

    total_tasks: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_rmb: float = 0.0
    total_llm_calls: int = 0
    total_trace_events: int = 0
    quality_stats: dict[str, Any] = Field(default_factory=dict)


TraceNode.model_rebuild()
