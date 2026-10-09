# Server V1 Readiness

> 对应 `PHASE_G_WORKER_RUNTIME_EXECUTION_PLAN.md` §38。
>
> **前提**：Phase G Acceptance Gate 全绿（见《工程化改造进度交接_第四轮.md》）。

---

## 1. Architecture

```text
Browser
  │ REST（命令 / 查询）
  ▼
FastAPI (backend)                        ← 不构建 LangGraph，不持有 checkpointer
  ├──────────────► Task DB (SQLite)      ← 业务事实源
  │
  └──────────────► Redis
                     ├── dr:jobs           job transport
                     └── dr:events:{tid}   事件流
                              ▲
                              │
Worker (python -m backend.worker)        ← 唯一正式 LangGraph 执行者
  ├──────────────► Task DB
  ├──────────────► Redis Checkpointer    LangGraph 执行状态
  └──────────────► Redis dr:events       XADD（不等待消费者）

Browser ◄── SSE ◄── FastAPI ◄── Redis dr:events（纯投影）
```

职责边界（**不可混淆**）：

```text
SQLite / SQLAlchemy   Task 业务事实与最终结果
Redis Queue           哪个任务需要执行（transport）
Redis Checkpointer    LangGraph 执行到哪里
Redis Streams         执行过程中发生什么（projection）
Worker                执行
FastAPI               命令 + 查询 + 事件投影
SSE                   只观察
```

---

## 2. Required Processes

| 进程 | 命令 | 数量 |
|---|---|---|
| API | `uvicorn backend.main:app --host 0.0.0.0 --port 8000` | ≥1 |
| **Worker** | `python -m backend.worker` | ≥1（可水平扩展） |
| Redis Stack | `redis/redis-stack-server:latest` | 1 |

**Worker 可以独立于 API 扩缩容**，这是 Phase G 的核心收益。

---

## 3. Ports

```text
8000    API
6379    Redis Stack
```

---

## 4. Environment Variables

### API 与 Worker 共用

```text
APP_ENV                    development | test | production
STAGE                      取 config.yml 中的 stage（默认 prod）
CHECKPOINTER_BACKEND       redis（部署必须）| sqlite | memory（仅测试）
CONFIG_PATH                config.yml 路径（相对路径按项目根解析）
DR_DATA_DIR                数据目录
DEEP_RESEARCH_LOG_DIR      日志目录
ALLOW_LIVE_EXTERNAL_APIS   false 时强制 Fake provider
```

### Worker 专属（调优）

```text
HEARTBEAT_INTERVAL_SECONDS   默认 10
CLAIM_TTL_SECONDS            默认 30（约束：>= 3 × heartbeat）
WORKER_MAX_ATTEMPTS          默认 3
WORKER_BACKOFF_BASE_SECONDS  默认 1
EVENT_RETENTION_MAXLEN       默认 1000（每任务事件保留条数）
JOB_DEDUPE_TTL_SECONDS       默认 5
```

---

## 5. Persistent Directories

```text
./data/                 SQLite 任务库（+ 可选的 chroma / checkpoints.db）
./data/tasks.db         业务事实源 —— **必须备份**
Redis volume            队列、事件、checkpoint
```

**备份优先级**：`data/tasks.db` > Redis volume。
Redis 丢失可由 checkpoint 重建执行状态，但队列中的待执行 job 会丢；
`tasks.db` 丢失则是业务数据丢失。

---

## 6. Redis Stack Requirement（硬性）

```text
必须  redis/redis-stack-server（含 RediSearch / RedisJSON）
禁止  纯 redis 镜像
```

`langgraph-checkpoint-redis` 依赖 RediSearch 与 RedisJSON 建立索引，
纯 Redis 会在 checkpointer 初始化时直接失败。

Phase F 的 spike 与 docker-compose 均已按此配置。

---

## 7. DB Migration Command

```bash
alembic upgrade head
```

**必须在启动 API 与 Worker 之前执行。**

未迁移时：
* API 启动即 fail fast（`backend/db/schema.py` 的启动校验），并给出该命令
* Worker 启动同样校验，不通过则退出

**绝不**以「删除数据库重建」作为升级手段。

---

## 8. Startup Order

```text
1. Redis Stack
      ↓ 等待 healthy（redis-cli ping）
2. alembic upgrade head
      ↓
3. Worker（可先于 API 启动 —— 它会等待 job）
      ↓
4. API
```

Worker 先启动是安全的：队列为空时它只是阻塞等待。

---

## 9. Shutdown Order

```text
1. API          （无状态，随时可停）
2. Worker       ← 发 SIGTERM，等待其优雅退出
3. Redis Stack
```

### Worker 的优雅退出语义（重要）

```text
SIGTERM → 停止消费新 job
        → 当前 job 跑到下一个安全边界
        → 持久化已产生的结果
        → 停止 heartbeat
        → **不释放 claim**（等它自然过期）
        → 退出
```

**不释放 claim 是刻意的**：图可能正跑在节点中途，checkpoint 只到上一个
节点边界。主动释放会让接管者立即重复执行当前节点——对 LLM 调用意味着
重复计费。让 claim 自然过期可保证 TTL 窗口（默认 30s）内绝无第二执行者。

代价：恢复延迟一个 TTL。

---

## 10. Health Checks

```text
GET /api/health
```

返回各组件的实际状态：

```json
{
  "status": "ok",
  "components": {
    "redis": "ok",
    "chromadb": "ok (...)",
    "sqlite": "ok"
  }
}
```

语义：

```text
liveness   = 进程存活
readiness  = DB schema 已迁移 + Redis 可达
```

生产环境下 Redis 不可达会在启动时 fail fast（`InfrastructureError`），
而不是带着坏掉的入队能力启动。

---

## 11. Backup Requirements

| 对象 | 频率 | 说明 |
|---|---|---|
| `data/tasks.db` | 每日 + 变更前 | 业务事实源 |
| Redis volume | 可选 | checkpoint 与队列，可由 DB 状态重建大部分 |

**不要**把 `.env`、`config.yml`（含密钥）纳入镜像或提交到仓库。

---

## 12. Expected Resource Usage

CPU/内存都属轻量级（Fake provider 或云端 API 场景下）：

```text
API       ~200–400 MB
Worker    ~300–600 MB（LangGraph + ChromaDB 常驻）
Redis     取决于事件保留量，通常 < 200 MB
```

无 GPU 需求——GPU 只属于独立的 Model Service（见下）。

---

## 13. Known Limitations

```text
1. 内存 checkpointer 不支持跨进程恢复 —— 部署必须用 redis 或 sqlite。
2. 取消是协作式的：已经发出的 LLM / Search 调用无法安全抢占，
   当前步骤返回后才生效。
3. 孤儿任务：若任务处于非终态但队列中已无对应 job（极端情况），
   当前没有主动清扫机制。
4. 事件保留默认 1000 条/任务，超长任务的历史事件会被驱逐。
5. 尚未接入完整的 Prometheus / OTel 观测栈（只有结构化上下文）。
6. Worker 无内置调度器（cron 类需求），只有队列驱动。
```

---

## 14. AutoDL Adaptation

见 `docs/deployment-autodl-notes.md`。要点：

```text
本 Phase **不**部署 AutoDL、不下载模型、不安装 CUDA/vLLM。
拓扑保持不变，只是把 Model Provider 的 base_url 指向本机推理服务。
```

---

## 15. Model Service Interface

**硬性边界**（架构上必须保持）：

```text
Worker
  │  OpenAI-compatible HTTP
  ▼
Model Service（独立进程 / 独立容器）
  │
  ▼
GPU
```

**禁止**：

```text
API 或 Worker 直接 torch.load() 模型
把模型权重打进业务镜像
让 Agent 代码根据"是不是 GPU 机器"写条件分支
```

Agent 只依赖 Model Provider 接口；本机推理与云端 API 通过**同一个**
`base_url` + `api_key` + `model` 配置切换：

```dotenv
# 开发：云端
LLM_BASE_URL=https://provider.example/v1
LLM_MODEL=some-cloud-model

# 24GB 机器：本机推理服务
LLM_BASE_URL=http://model:8000/v1
LLM_MODEL=local-model
```

这样 8GB 开发机与 24GB 部署机之间**只需要改配置**。
