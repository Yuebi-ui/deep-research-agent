# Worker Compatibility Spike 结果

> 对应 `CLAUDE_CODE_NEXT_PHASE_EXECUTION_PLAN.md` §26–36。
>
> **结论：Redis 后端 GO（Phase F 的目标后端已实测通过）；SQLite 后端 GO。**
> 见「结论」一节。

---

## 1. Environment

```text
OS                  Windows 11 (10.0.26200), AMD64
Shell               Git Bash
Python              3.14.4 (MSC v.1944 64 bit)
virtualenv          项目根 .venv
APP_ENV             test
ALLOW_LIVE_EXTERNAL_APIS  false
```

版本：

```text
langgraph                   1.2.1
langchain                   1.3.1
langchain-core              1.4.0
langgraph-checkpoint        4.1.1
langgraph-checkpoint-sqlite 3.1.0
langgraph-checkpoint-redis  0.4.1
redis (redis-py)            7.4.0
aiosqlite                   0.22.1
sqlalchemy                  2.0.49
```

---

## 2. Checkpointer 实现

按执行包 §0.5.2 引入工厂 `deep_research/checkpoint.py`：

| 后端 | 实现 | 用途 | 跨进程 |
|---|---|---|---|
| `memory` | `InMemorySaver` | 仅无需持久恢复的测试 | ❌ |
| `sqlite` | `AsyncSqliteSaver` | 单机开发 / 持久化测试 | ✅（单机文件） |
| `redis` | `AsyncRedisSaver` | **Phase F 目标 / Phase G 部署** | ✅ |

解析优先级：

```text
1. CHECKPOINTER_BACKEND 环境变量
2. APP_ENV=test 时固定 memory（避免测试写进真实 data/checkpoints.db）
3. config.yml 的 memory.checkpoint.backend（当前为 sqlite）
4. 默认 memory
```

**已移除原有的「Redis 连不上就静默降级 InMemorySaver」逻辑**——静默降级会让跨进程恢复的验证变成假阳性。

`AsyncRedisSaver` 必须先 `await saver.asetup()`；`AsyncSqliteSaver` 接收
`aiosqlite` 连接；二者的 `from_conn_string()` 都是 `@asynccontextmanager`，
因此工厂直接使用构造函数并自行管理资源释放。

---

## 3. Test Topology

```text
scripts/worker_spike.py run
        │
        ├─ subprocess: worker_spike.py stage1 --thread-id <T>
        │      独立进程 #1（PID 25736）
        │      建图 + astream_events + 跑到 human_review interrupt
        │      进程退出
        │
        └─ subprocess: worker_spike.py stage2 --thread-id <T> --action approve
               独立进程 #2（PID 44552）
               重新建图（进程内无任何残留状态）
               从同一 thread_id 读 checkpoint → resume → 跑到 END
```

两个 stage 由**外层驱动以 `subprocess` 启动**，因此进程内内存状态必然丢失——
stage2 若成功，只可能是从持久化 checkpointer 恢复的。

---

## 4. Scenario Results

### Scenario 1 —— HITL 跨进程恢复 ✅ PASS（sqlite）

```text
Stage 1（PID 25736）
  next = ['human_review']      ← 停在中断点
  draft_len = 55               ← 草稿已持久化
  nodes_seen = 4
  进程退出

Stage 2（PID 44552，**不同进程**）
  resumed_from = ['human_review']   ← 读到了上一进程留下的中断状态
  next = []                         ← 跑到 END
  has_report = True                 ← 产出最终报告
```

**这是 §29 要求的核心场景，在 sqlite 后端上成立。**

### Scenario 1（Redis，Phase F 目标后端）✅ PASS

§0.5.2 明确要求本场景**必须基于 Redis checkpointer 实测**，不接受用
sqlite 或 memory 替代。补跑结果：

```text
Stage 1（PID 1048）
  next = ['human_review']      ← 停在中断点
  draft_len = 55

Stage 2（PID 49876，**不同进程**）
  resumed_from = ['human_review']
  next = []
  has_report = True
```

Redis 侧确认数据确实落在服务端（而非进程内）：

```text
keys: checkpoint_write:spike-3bb44aa8:supervisor_subgraph:...
      （dbsize 501，含 checkpoint: / checkpoint_write: / checkpoint_latest: 三类）
```

连续 3 次独立运行全部 PASS，PID 每次不同：

```text
第 1 次  stage1 pid=50088  stage2 pid=8628
第 2 次  stage1 pid=27296  stage2 pid=51480
第 3 次  stage1 pid=48972  stage2 pid=39372
```

环境：容器 `redis/redis-stack-server:latest`，`redis://localhost:6379`。

> 注意用的是 **redis-stack**（含 RediSearch / RedisJSON）而非纯 `redis` 镜像。
> `langgraph-checkpoint-redis` 依赖这两者建立索引，换成纯 Redis 镜像会失败。

### Scenario 5 —— `astream_events` + checkpointer ✅ PASS（sqlite）

`astream_events(version="v2")` 与 `AsyncSqliteSaver` 组合运行正常，**未出现
`NotImplementedError`**。

> 历史背景：`PLAN.md` 记录过该项目早期用**同步** `SqliteSaver` 时
> `astream_events` 抛 `NotImplementedError`，因此退回 `InMemorySaver`。
> 本次验证表明**异步** saver 无此问题。

### Scenario 2 —— SSE disconnect

**未执行。** 当前架构下 SSE 连接即执行生命周期（`GET /stream` 直接驱动
LangGraph），断开行为无法在 spike 层面与本场景解耦验证。这正是 Phase G
要解决的问题；按 §30，此处记为**已知待解决**，不伪造成功。

### Scenario 3 —— API restart

**未执行**，原因同 Scenario 2：当前执行绑定在 API 进程内。

### Scenario 4 —— Duplicate resume

**部分覆盖。** Phase D 已在 API 层验证重复 review 返回 409
（`INVALID_REVIEW_STATE`）。但**跨进程的原子 claim 尚未实现**——
两个 worker 同时读到同一 thread_id 时，checkpointer 层不做互斥。
按 §32，这属于 Phase G 需引入的机制。

---

## 5. Stack Traces

本次运行的场景全部通过，无失败栈。

（若出现失败，应在此粘贴完整 traceback。当前为空。）

---

## 6. Known Limitations

1. **Redis 后端未验证**（见「结论」）。目标后端与已验证后端不一致，
   这是本文件不能给出完整 GO 的根本原因。
2. **Scenario 2 / 3 未执行**——当前 SSE 与执行生命周期耦合，无法在
   spike 层面隔离验证。属 Phase G 范畴。
3. **无原子 claim**：两个 worker 可同时从同一 checkpoint 恢复。
4. **无 heartbeat**：worker 崩溃后无人接管。
5. **无 cancellation**：`POST /api/research/{id}/cancel` 不存在（Phase D 已用
   测试显式记录为 404）。
6. **本机为 Windows**：`multiprocessing` 只有 `spawn`。本 spike 用
   `subprocess` 规避了 pickling 问题，但 Phase G 实现真实 worker 时
   仍需注意此处。

---

## 7. Required Phase G Changes

```text
1.  Redis Queue：任务投递（本阶段未实现，也未提前实现）
2.  Redis Streams：事件流 + Last-Event-ID 回放
3.  原子 claim：UPDATE ... WHERE status='queued' 语义，避免双执行
4.  heartbeat_at + stale 检测 + recovery policy
5.  协作式 cancellation（节点/工具/LLM 调用边界检查）
6.  SSE 只做事件投影，不再驱动执行
7.  worker 进程的生命周期管理（本 spike 用 subprocess 临时占位）
```

---

## 8. 结论

### SQLite 后端

```text
GO —— 就以下能力而言：
  [x] checkpoint 能跨 worker 生命周期读取
  [x] HITL interrupt state 可恢复
  [x] resume 可从新 worker 继续
  [x] astream_events 与 checkpointer 可共存
  [x] Fake graph 能完整收敛（Phase D.5）
  [x] persistence boundary 可支持 worker
```

### Redis 后端（Phase F 的目标后端）

```text
GO —— 就上述同一组能力而言，已在 Redis 上实测通过
```

环境：

```text
容器          redis (redis/redis-stack-server:latest)
地址          redis://localhost:6379
config.yml    stages.prod.redis.enabled: true  ← 本次为跑通验证而启用
```

复现命令：

```bash
CHECKPOINTER_BACKEND=redis python scripts/worker_spike.py run
```

对应自动化测试（Redis 不可达时自动 skip）：

```bash
pytest tests/test_worker_spike.py -q
```

---

## 9. 对 Phase G 的影响

Phase F 的目的已达成：**「独立 worker 进程 + Redis checkpointer + HITL
interrupt/resume + astream_events」这条组合被证明可行**，因此 Phase G
的正式解耦有了技术基础，不再是未知数。

但本阶段**未**（按 §37 也不应）实现：

```text
Redis Queue / Redis Streams / heartbeat / atomic claim / cancellation
```

Scenario 2（SSE disconnect）与 Scenario 3（API restart）仍未执行——
当前 SSE 连接即执行生命周期，这两个场景只有在 Phase G 把 SSE 降级为
纯事件投影之后才有意义。

进入 Phase G 前需要人工确认的事项：

```text
1. config.yml 中 redis.enabled 已由本次验证改为 true；
   若生产环境不需要 Redis，应显式改回并在 CHECKPOINTER_BACKEND 上做对应配置。
2. 部署环境必须使用 redis-stack（含 RediSearch / RedisJSON），
   纯 redis 镜像不满足 langgraph-checkpoint-redis 的依赖。
3. Phase G 的原子 claim 仍缺失——两个 worker 可同时从同一 checkpoint 恢复，
   本阶段未引入互斥机制。
```
