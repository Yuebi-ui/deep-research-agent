"""FastAPI application entrypoint.

Development command::

    uvicorn backend.main:app --reload --port 8000
"""

from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from backend.core.api_errors import register_exception_handlers
from backend.core.errors import InfrastructureError
from backend.core.middleware import (
    rate_limit_middleware,
    request_id_middleware,
)
from backend.core.settings import get_settings
from backend.routes.research import router as research_router
from backend.routes.observability import router as obs_router
from backend.schemas.responses import HealthResponse, SSEEventEnvelope
from deep_research import __version__
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# ---- Rate Limiter ----
# store 由模块持有以便测试重置（见 backend/core/middleware.py 的说明）
_rate_limit_store: dict[str, list[float]] = defaultdict(list)


async def _rate_limit_middleware(request: Request, call_next):
    """滑动窗口限流（实现见 backend/core/middleware.py）。"""
    settings = get_settings()
    return await rate_limit_middleware(
        request, call_next,
        store=_rate_limit_store,
        window=settings.rate_limit_window,
        limit=settings.rate_limit_max,
    )


# 注：Phase G 之后 API **不再预热 LLM 与 Agent 图**。
#
# 图由 Worker 拥有（docs/phase-g-runtime-design.md §16）：
# API 只做命令、查询与事件投影，既不构建 LangGraph，也不持有 checkpointer。
# 原先的 _warmup_optional() 已移除。


async def _init_critical(settings) -> None:
    """关键基础设施初始化。

    失败语义按环境分级（V3 §3.2）：

    * ``production`` —— 抛 :class:`InfrastructureError`，由 lifespan 向上传播，
      阻止应用启动（fail fast）。绝不静默降级到 InMemorySaver。
    * 其他环境 —— 记录 warning 后继续启动。
    """
    from backend.db.engine import get_engine
    from backend.db.schema import assert_schema_up_to_date

    # 数据库 schema 必须已迁移到位。未迁移时任何写入都会触发
    # `IntegrityError: NOT NULL constraint failed: tasks.verification`，
    # 因此这里在启动阶段就 fail fast，并给出可直接照做的修复命令。
    #
    # 该检查在任何环境都执行（不经 except 降级）——未迁移时应用根本无法
    # 创建任务，继续启动只会把问题推迟到第一次写入时才暴露。
    # 测试环境跳过：测试使用自建 schema，不依赖真实库的迁移状态。
    if not settings.is_test:
        assert_schema_up_to_date(get_engine())

    # Redis 是入队与事件通道，属于本进程的关键依赖。
    # 探活失败时 production fail fast；其他环境记 warning 即可——
    # 入队失败时 API 会明确返回 503，不会静默丢任务。
    try:
        from backend.runtime import redis as rt_redis

        if not await rt_redis.ping():
            raise InfrastructureError("Redis 不可达")
    except InfrastructureError:
        if settings.is_production:
            raise
        logger.warning("Redis 不可达：任务无法入队，API 会返回 503", exc_info=True)
    except Exception as exc:
        if settings.is_production:
            raise InfrastructureError(f"Redis 探活失败: {exc}") from exc
        logger.warning("Redis 探活失败：%s", exc)

    # 注：API **不再初始化 checkpointer，也不再清理残留任务**。
    #
    # Phase G 之前，InMemorySaver 下重启会让 running/pending 任务不可恢复，
    # 所以 API 启动时把它们统一标记为失败。现在 checkpointer 是持久化的，
    # 那些任务**可以被 worker 从 checkpoint 恢复** —— 再标记为失败反而是错的。
    #
    # 恢复职责归 worker：未 ack 的 job 由 reclaim_stale 接管，claim 过期后
    # 由新 worker 从 checkpoint 继续（见 docs/phase-g-runtime-design.md §4）。


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化基础设施，关闭时释放资源。"""
    settings = get_settings()

    # 数据目录是关键前置条件：失败直接抛出（任何环境）
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # 关键基础设施**必须先于**预热：预热会预编译 agent 图，而图编译依赖
    # 已初始化的 checkpointer。顺序反了会导致预热拿到
    # CheckpointerError（被 warning 吞掉，但会打印无意义的 traceback，
    # 且预热实际上没生效）。
    try:
        await _init_critical(settings)
    except InfrastructureError:
        raise  # production fail-fast，不得吞掉
    except Exception as exc:
        if settings.is_production:
            raise InfrastructureError("关键基础设施初始化失败") from exc
        logger.warning("关键基础设施初始化失败，开发环境下继续启动: %s", exc, exc_info=True)

    yield


app = FastAPI(
    title="Agentic Deep Research Platform",
    description="基于 LangGraph 的多智能体深度研究系统 API",
    version=__version__,
    lifespan=lifespan,
)

app.middleware("http")(request_id_middleware)
app.middleware("http")(_rate_limit_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(research_router)
app.include_router(obs_router)


register_exception_handlers(app)


@app.get("/api/test-sse")
async def test_sse():
    """极简 SSE 测试——排除 FastAPI StreamingResponse 问题。"""
    import asyncio

    async def gen():
        for i in range(5):
            frame = SSEEventEnvelope(event="ping", data={"i": i})
            yield f"data: {frame.model_dump_json()}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # 增强健康检查：报告关键组件状态
    import os
    status = {"status": "ok", "components": {}}
    data_dir = get_settings().data_dir
    persist_dir = str(data_dir / "chroma")

    # Redis：runtime 的关键依赖（入队 + 事件通道）
    try:
        from backend.runtime import redis as rt_redis

        status["components"]["redis"] = "ok" if await rt_redis.ping() else "unreachable"
    except Exception as e:
        status["components"]["redis"] = f"error: {e}"

    # ChromaDB check
    try:
        if os.path.exists(persist_dir):
            status["components"]["chromadb"] = f"ok (persist_dir: {persist_dir})"
        else:
            status["components"]["chromadb"] = "not_initialized"
    except Exception as e:
        status["components"]["chromadb"] = f"error: {e}"

    # Memory embedding schema check（Phase 3C P0）：
    # 旧 embedding space 的 collection 在查询时会直接失败——必须在这里显式暴露，
    # 不能等到 worker 运行期才暴露（benchmark preflight 会读这一项）。
    try:
        if not os.path.exists(persist_dir):
            status["components"]["memory_schema"] = "not_initialized"
        else:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            from deep_research.memory.embeddings import EmbeddingClient
            from deep_research.memory.migration import MANAGED_COLLECTIONS
            from deep_research.memory.schema_guard import inspect_collection

            identity = EmbeddingClient().identity
            client = chromadb.PersistentClient(
                path=persist_dir, settings=ChromaSettings(anonymized_telemetry=False)
            )
            reports = [inspect_collection(client, name, identity) for name, _ in MANAGED_COLLECTIONS]
            bad = [r for r in reports if not r["compatible"]]
            if bad:
                detail = ", ".join(f"{r['collection']}={r['status']}" for r in bad)
                status["components"]["memory_schema"] = f"incompatible: {detail}"
            else:
                status["components"]["memory_schema"] = (
                    f"ok ({len(reports)}/{len(reports)} compatible, {identity.describe()})"
                )
    except Exception as e:
        status["components"]["memory_schema"] = f"error: {e}"

    # SQLite check
    try:
        db_path = str(data_dir / "tasks.db")
        status["components"]["sqlite"] = "ok" if os.path.exists(db_path) else "not_initialized"
    except Exception as e:
        status["components"]["sqlite"] = f"error: {e}"

    return status
