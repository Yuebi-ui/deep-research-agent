"""研究任务的状态机。

纯业务规则，不依赖数据库 / FastAPI。

设计要点：

* ``DELETED`` 是必需的。当前删除任务实现为软删除——``status="deleted"``，
  且列表查询用 ``WHERE status != 'deleted'`` 过滤。若状态机不包含它，
  删除操作会被判定为非法迁移。
* 迁移表刻意保持宽松：现状是"任意状态都可以被删除"，收紧会改变既有行为。
  等 Worker / Queue 落地、引入 queued / retrying 等状态后，再考虑收敛。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """任务状态。取值同时作为持久化字符串与 API 响应字段。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    DELETED = "deleted"


# 合法状态迁移。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.DELETED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_REVIEW,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
            TaskStatus.DELETED,
        }
    ),
    TaskStatus.WAITING_REVIEW: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
            TaskStatus.DELETED,
        }
    ),
    TaskStatus.CANCELLING: frozenset(
        {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.DELETED,
        }
    ),
    TaskStatus.COMPLETED: frozenset({TaskStatus.DELETED}),
    TaskStatus.CANCELLED: frozenset({TaskStatus.DELETED}),
    TaskStatus.FAILED: frozenset({TaskStatus.DELETED}),
    # 软删除是终态：没有恢复入口。
    # 若将来需要"取消删除"，在此加入 TaskStatus.PENDING。
    TaskStatus.DELETED: frozenset(),
}

# 终态：不会再产生业务进展
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class InvalidTaskTransition(ValueError):
    """非法的任务状态迁移。"""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"非法的任务状态迁移: {current.value} -> {target.value}")


class UnknownTaskStatus(ValueError):
    """数据库中出现了状态机不认识的状态值。"""


class InvalidReviewState(ValueError):
    """在不允许审查的状态下提交了 HITL 审查决定。

    只有 ``waiting_review`` 可以接受审查决定。若不加这条前置校验，
    ``pending`` / ``running`` 都会被状态机的 ``-> RUNNING`` 迁移"顺带放行"，
    从而接受一次语义上无效的审查（实测行为）。
    """

    def __init__(self, current: TaskStatus) -> None:
        self.current = current
        super().__init__(
            f"任务当前状态为 {current.value}，只有 "
            f"{TaskStatus.WAITING_REVIEW.value} 状态可以提交审查决定"
        )


def coerce_status(value: str | TaskStatus) -> TaskStatus:
    """把持久化字符串转成 :class:`TaskStatus`。

    无法识别时抛 :class:`UnknownTaskStatus`——宁可显式失败，
    也不要把未知状态当成某个默认值静默处理。
    """
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(value)
    except ValueError as exc:
        raise UnknownTaskStatus(f"未知的任务状态: {value!r}") from exc


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """判断迁移是否合法（不抛异常）。"""
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    """校验迁移合法性，非法时抛 :class:`InvalidTaskTransition`。

    相同状态之间的"迁移"视为合法（幂等写入），因为它不改变业务语义，
    而重复写入在重试、重复 resume 等场景下是正常现象。
    """
    if current == target:
        return
    if not can_transition(current, target):
        raise InvalidTaskTransition(current, target)


# ===== 由 stage 推导状态 =====
#
# 收敛自 backend/services/agent_service.py 中原本散落的 status_map。
# 未列出的 stage 一律视为"执行中"。

STAGE_TO_STATUS: dict[str, TaskStatus] = {
    "human_review": TaskStatus.WAITING_REVIEW,
    "final_report_generation": TaskStatus.COMPLETED,
}

DEFAULT_STAGE_STATUS: TaskStatus = TaskStatus.RUNNING


def status_for_stage(stage: str) -> TaskStatus:
    """根据当前执行阶段推导任务状态。"""
    return STAGE_TO_STATUS.get(stage, DEFAULT_STAGE_STATUS)
