"""基础设施层异常。

与 `backend/domain/` 下的业务异常区分：这里只表示**运行时基础设施**问题
（数据库、checkpointer、数据目录），通常发生在启动阶段。
"""

from __future__ import annotations


class InfrastructureError(RuntimeError):
    """关键基础设施不可用。

    在 ``APP_ENV=production`` 下，启动阶段抛出此异常会直接阻止应用启动
    （fail fast），而不是静默降级到不可靠的运行时语义
    ——参见 ENGINEERING_OPTIMIZATION_PLAN_V3.md §3.2。
    """


class UnsupportedDatabaseBackend(InfrastructureError):
    """配置要求了当前不支持的数据库后端。

    目前唯一受支持并被 CI 验证的后端是 SQLite。历史上存在一个
    `MySQLBackend`，但它既未在 requirements 中声明、也未安装，
    且与 SQLite 的 `verification` 语义已经分叉，因此已移除。

    刻意**不做 silent fallback**：配置写了 mysql 却静默连上 SQLite，
    会让"数据到底写到哪里"变得不可知——这比启动失败危险得多。
    """
