"""业务域模型。

与 `backend/services/` 的区别：这里是**纯业务规则**，不依赖数据库、
FastAPI 或任何基础设施。`services/` 负责把域规则与持久化组合起来。
"""

from backend.domain.task import (
    ALLOWED_TRANSITIONS,
    STAGE_TO_STATUS,
    InvalidTaskTransition,
    TaskStatus,
    can_transition,
    coerce_status,
    ensure_transition,
    status_for_stage,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "STAGE_TO_STATUS",
    "InvalidTaskTransition",
    "TaskStatus",
    "can_transition",
    "coerce_status",
    "ensure_transition",
    "status_for_stage",
]
