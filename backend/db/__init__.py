"""持久化层。

边界：

    backend.domain     纯业务规则（无基础设施依赖）
    backend.db         模型 / engine / session / repository 实现
    backend.services   把域规则与持久化组合（业务服务）

当前唯一受支持的数据库后端是 **SQLite**。
MySQL 已移除，原因见 ENGINEERING_OPTIMIZATION_PLAN_V3.md 与执行包 §0.5.4：
它既未在 requirements 中声明、也未安装，实际不可运行，且与 SQLite 的
`verification` 语义已经分叉。
"""

from backend.db.engine import (
    get_engine,
    get_session,
    reset_engine,
    session_scope,
)
from backend.db.models import Base, ResearchTask
from backend.db.repository import (
    SqlAlchemyTaskRepository,
    TaskRepository,
    get_repository,
    reset_repository,
)

__all__ = [
    "Base",
    "ResearchTask",
    "SqlAlchemyTaskRepository",
    "TaskRepository",
    "get_engine",
    "get_repository",
    "get_session",
    "reset_engine",
    "reset_repository",
    "session_scope",
]
