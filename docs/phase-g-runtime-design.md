# Phase G 运行时设计（Design Gate 交付物）

> **历史设计说明：** 本文记录早期 Phase G 的运行时设计，部分前端路径来自当时的仓库结构。当前仓库为 API-first，现行拓扑以 `architecture/README.md`、`docker-compose.yml` 和 `docs/PROJECT_STATUS.md` 为准。


> 对应 `PHASE_G_WORKER_RUNTIME_EXECUTION_PLAN.md` §5 / §35。
>
> **本文档是实施前的硬闸门。** §36 的实施（G1–G9）必须在本文档逐项通过
> §35 检查清单后开始。
>
> 基线：`engineering-pre-worker-v1`（328 passed，ruff PASS，外部 API 0）。

---

## 0. 审计结果（§5 要求的代码事实）

### 0.1 API 进程当前承担的职责（全部要移走）

```text
backend/routes/research.py        8 处任务状态写入
    :153 :178  set_task_stage(human_review)     ← SSE handler 内
    :159 :218  set_task_stage(supervisor_subgraph)
    :163       set_task_stage(write_research_brief)
    :172       mark_task_completed
    :182       mark_task_failed
    :234       mark_task_deleted（这是 API 该做的，保留）

backend/routes/research.py:137    构建 agent 并驱动 LangGraph（核心耦合）
backend/services/agent_service.py:271  get_report 经 graph 读 checkpoint
backend/main.py:93-95             lifespan 预热时预编译 agent 图
```

**结论：不只是「SSE 不再驱动图」，而是「API 不再拥有执行状态」。**
§35.3 的清单已覆盖。

### 0.2 前端事件契约（6 个，不可破坏）

实测 `frontend/app/page.tsx` 的 switch：

```text
node_start
node_complete
tool_call
human_review_required
complete
error
```

另有 `report_chunk` —— **前端有消费分支，后端从未发出**，是死分支。
Phase G 不实现它（不扩大范围），仅在投影层保留透传位置。

### 0.3 前端调用顺序（决定了兼容性天然成立）

```js
// 创建
const { thread_id } = await api.startResearch(q);   // POST，先返回
connectSSE(api.getStreamUrl(thread_id), tid);       // 再连事件

// 审查
await fetch(api.getResumeUrl(tid), { action });     // POST，先返回
connectSSE(api.getStreamUrl(tid), tid);             // 再连事件
```

**前端本来就是「先发命令、再订阅事件」。** 因此把 `POST` 改成入队、
`/stream` 改成纯投影之后，**前端零改动**。§18 的兼容目标天然成立。

### 0.4 现存不一致（设计必须处理）

```text
1. config.yml  memory.checkpoint.backend = sqlite
                redis.enabled = true
   → 两者语义不一致；已决策改为 redis（见 §11）

2. config.yml  redis.url = redis://localhost:6379
                redis.db  = 0
   → URL 未体现 db；设计统一由 url + db 组合（见 §12）

3. docker-compose.yml  redis 服务为 redis:7-alpine（纯 Redis）
   → 必须改 redis/redis-stack-server（§23）

4. _store_review 用 setex(..., 600) 或进程内 dict —— 都是短命的
   → Phase G 改为 DB 持久化（见 §6）
```

### 0.5 已确认的三项决策（由项目所有者下达）

```text
1. Review decision 存储：独立 task_reviews 表
2. Graceful shutdown：停止 heartbeat，等 claim 自然过期（不主动 release）
3. checkpointer 配置：改为 redis，sqlite 保留为文档化的回退
```

---

## 1. 职责边界（锁定）

```text
SQLite / SQLAlchemy   Task 业务事实 + 最终结果（写入顺序上优先）
Redis Checkpointer    LangGraph 执行状态
Redis Queue           哪个任务需要执行（transport）
Redis Streams         执行过程中发生什么（projection）
Worker                唯一正式 LangGraph 执行者
FastAPI               命令 + 查询 + 事件投影
SSE                   只观察
```

---

## 2. Atomic Claim 算法

### 2.1 键与值

```text
key    dr:claim:task:{thread_id}
value  worker_id = {hostname}:{pid}:{uuid4 前 8 位}
       不用纯 PID：容器/PID 复用会撞
```

### 2.2 获取（原子）

```text
SET dr:claim:task:{tid} {worker_id} NX PX {claim_ttl_ms}
→ OK      表示获取成功
→ nil     表示已被占用
```

**禁止** `GET → if empty → SET` 这类非原子序列（§6 明令）。

### 2.3 续约（仅 owner）

```lua
-- KEYS[1] = claim key, ARGV[1] = worker_id, ARGV[2] = ttl_ms
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
```

Lua 保证「比较 + 续期」原子。非 owner 续约返回 0。

### 2.4 释放（仅 owner）

```lua
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
```

### 2.5 TaskStatus 与 Claim 分离（§6 强制）

```text
TaskStatus  = 业务状态（用户可见，粗粒度，8 个值）
Claim       = 运行时互斥（Redis，带 TTL，用户不可见）
```

**不得**把 `status == RUNNING` 当作 claim。两者的生命周期不同：
一个任务可以在没有 claim 的情况下处于 RUNNING（worker 崩溃后、
claim 过期前的那段时间）。

### 2.6 DB 侧的运行时元数据

claim 本身是易失的（Redis + TTL），但以下需要落库以便观测与重试：

```text
attempt          Mapped[int]        默认 0
claimed_by       Mapped[str | None] 最后一次成功 claim 的 worker_id
claimed_at       Mapped[datetime | None]
heartbeat_at     Mapped[datetime | None]
```

DB 字段是**观测与重试依据**，不承担互斥职责。

---

## 3. Claim TTL / Heartbeat

所有时间参数走 Settings，不散落 magic number（§7）。

```text
                   生产默认    测试值
heartbeat_interval   10 s       1 s
claim_ttl            30 s       3 s      （= 3 × interval，满足 §7 约束）
```

心跳任务与 job 执行并行运行；心跳失败（Redis 不可达）→ 记录并
**停止续约**，让 claim 自然过期，而不是尝试重连后继续持有。

---

## 4. Stale Recovery

claim 过期后，另一个 worker 抢到同一任务。此时**必须**先检查：

```text
1. 任务是否终态（COMPLETED / FAILED / CANCELLED / DELETED）
       → 是：跳过，直接释放 claim（§24 test_terminal_task_is_not_reclaimed）

2. cancel_requested 是否为真
       → 是：转入 CANCELLED，不执行

3. checkpoint 是否存在
       → 存在：resume（绝不从头重跑）
       → 不存在：按首次执行启动
```

**禁止看到 RUNNING 就从头重跑**（§7 明令）。判断依据是 checkpoint
是否存在，不是任务状态。

---

## 5. Cancellation

### 5.1 持久化

```text
cancel_requested      Mapped[bool]         默认 false
cancel_requested_at   Mapped[datetime | None]
```

必须是 DB 字段——§8 明令「不能只存在 Python global 或纯 ephemeral
Redis flag」。API 重启后取消请求必须仍然有效。

### 5.2 API

```text
POST /api/research/{thread_id}/cancel

允许取消的状态：PENDING / RUNNING / WAITING_REVIEW
终态（COMPLETED / FAILED / CANCELLED / DELETED）→ 409
```

分两种情况：

```text
任务未在执行（PENDING / WAITING_REVIEW）
    → 直接置 CANCELLED（无需 worker 介入）

任务在执行中（RUNNING）
    → 置 cancel_requested = true
    → 任务状态保持 RUNNING（worker 在下一个边界处理）
    → 若 claim 已过期无人在跑，可直接置 CANCELLING 并最终 CANCELLED
```

### 5.3 检查点（协作式）

worker 在以下边界检查 `cancel_requested`：

```text
[必做] graph 执行开始前
[必做] astream_events 每个事件之间
[必做] 持久化提交前
[尽力] 工具调用前后（工具内部不可中断）
[不做] LLM 调用中途
```

**明确声明（§8 要求）**：已经发出的 LLM / Search 请求**无法安全抢占**。
取消语义是「不再开始新的步骤」，不是「立即中止当前步骤」。当前步骤
返回后才生效。这一点必须在 API 文档与测试中显式表达，不得含糊。

状态迁移：

```text
RUNNING → CANCELLING → CANCELLED
```

worker 检测到 cancel → 置 CANCELLING → 释放 claim → 置 CANCELLED。
（两步分开是为了让「已请求但未完成」可观测。）

---

## 6. Review Decision 持久化与消费语义（§35.1）

### 6.1 存储（已决策：独立表）

```text
task_reviews
    id             Mapped[str]        uuid4().hex
    thread_id      Mapped[str]        外键语义（不建约束，与现有风格一致）
    action         Mapped[str]        approve | revise | reject
    feedback       Mapped[str]        默认 ""
    created_at     Mapped[datetime]
    consumed_at    Mapped[datetime | None]
    consumed_by    Mapped[str | None] worker_id
```

迁移 `0002_*`，**绝不修改已应用到真实库的 `0001_baseline_tasks.py`**（§31）。

### 6.2 消费语义（关键）

**「已消费」不是恢复的依据——checkpoint 才是。**

理由：若以 `consumed_at` 作为恢复依据，会遇到这个卡死窗口：

```text
worker 标记 consumed_at → resume 之前崩溃
→ decision 视为已消费，不再重放
→ 但 graph 从未收到 resume
→ 任务永卡 WAITING_REVIEW
```

因此本设计采用：

```text
恢复判断依据 = LangGraph checkpoint 中 human_review 的 interrupt 是否仍 pending

  仍 pending  → review 尚未真正生效 → 可以安全重放
  已越过      → review 已生效        → 不重放
```

**该判据是可实现的，且已在 Phase F 验证过**：

```python
snap = await graph.aget_state(config)
snap.next == ("human_review",)     # interrupt 仍 pending
snap.next == ()                    # 已越过，图已结束或推进
```

Phase F 的跨进程 spike 正是靠这个判断确认「stage2 读到了 stage1 留下的
中断状态」（`resumed_from == ['human_review']`）。

`consumed_at` / `consumed_by` **仅用于审计与观测**，不参与恢复决策。

### 6.3 幂等保证

```text
同一 review 被重放多次
→ checkpoint 显示 interrupt 仍 pending 时才重放
→ 一旦图越过 human_review，后续重放会因 interrupt 不存在而无害返回
```

### 6.4 Duplicate resume 防护

```text
API 层   重复 review → 409（Phase D 已实现，保留）
Queue 层 重复 job    → 可能发生，但 claim 保证只有一个 worker 执行
Claim 层 唯一安全边界（§9 明示：queue dedupe 不是最终安全边界）
```

---

## 7. HITL 边界的 Claim 释放（§35.2）

已决策：

```text
worker 到达 WAITING_REVIEW → 该 job 结束 → **立即释放 claim**
等待人工审核期间 **不持有** claim
approve 后由 POST /resume 入队新 job → 重新 atomic claim
```

理由（§35.2 明令）：若跨等待持有 claim，TTL 会在人工审核期间过期，
另一个 worker 就会接管并 **resume 一个用户尚未批准的任务**。

因此：**任务状态是 WAITING_REVIEW ⇒ 必然没有 worker 在跑它。**
这是一个可断言的强不变量，测试应覆盖。

---

## 8. Queue Transport

### 8.1 选型

**Redis Stream + consumer group**，而非 LIST。

理由：Stream 原生提供 at-least-once 语义、`XACK` 确认、`XPENDING` /
`XAUTOCLAIM` 未确认消息恢复——正好覆盖 §9 要求的 ack / recovery，
无需手写。LIST 需要自己实现 processing list 与超时回收。

```text
stream   dr:jobs
group    dr:workers
```

**不引入 Celery / RQ**（§3、§9 明令）。

### 8.2 Job 模型

统一为单一 operation（§9 允许）：

```json
{
  "job_id": "uuid",
  "thread_id": "abc123",
  "operation": "run",
  "attempt": 0,
  "created_at": "2026-09-30T..."
}
```

`run` 的含义由 **worker 根据 DB 状态 + checkpoint 决定**：

```text
PENDING                              → 首次执行
WAITING_REVIEW 且 interrupt pending  → 应用 review 并 resume
RUNNING（claim 过期后接管）           → 从 checkpoint resume
终态                                 → 跳过
```

**为什么统一优于拆分 start/resume**：状态驱动天然幂等，重复投递
（double click / HTTP retry / Redis redelivery）不会产生不同的路径分支，
减少了「消息类型与实际状态不一致」这类 bug。

### 8.3 Dedupe

```text
dr:jobdedupe:{thread_id}   SET NX PX {dedupe_ttl}（默认 5 s）
```

**仅用于抑制瞬时重复投递，不是安全边界。** §9 明示：最终安全边界是
Atomic Claim。

### 8.4 投递失败

`enqueue` 时 Redis 不可达 → API 返回 503，**任务保持 PENDING**。
用户可重试。不静默丢任务，也不把任务标记为 FAILED。

---

## 9. Event Plane

### 9.1 拓扑

```text
dr:events:{thread_id}    每任务一条 Stream
dr:evseq:{thread_id}     INCR 计数器
```

**为什么按任务分片**：replay 只需从某个 stream id 继续读；保留策略可按
任务设置 MAXLEN；不同任务之间不会互相驱逐。

### 9.2 Schema

```json
{
  "event_id": "1712345678901-0",
  "task_id": "abc123",
  "sequence": 42,
  "type": "graph.node.started",
  "timestamp": "2026-09-30T02:00:00.000000+00:00",
  "worker_id": "host:1234:a1b2c3d4",
  "data": {}
}
```

`sequence` 由 `INCR dr:evseq:{thread_id}` 原子分配，任务内单调递增。
Stream ID 作为 transport cursor，`sequence` 作为业务序号。二者职责不同：
前者由 Redis 生成、可用于 `Last-Event-ID`；后者稳定、可用于跨 transport
的去重与排序断言。

### 9.3 Canonical 事件词表

```text
task.queued            task.claimed
task.started           task.resumed
task.waiting_review    task.cancel_requested
task.cancelled         task.completed
task.failed
graph.node.started     graph.node.completed
agent.started          agent.completed
tool.started           tool.completed
```

### 9.4 与前端契约的映射（关键）

**投影层负责翻译**，浏览器继续收到它已知的 6 个名字：

| Canonical（Redis） | → 浏览器（SSE） |
|---|---|
| `graph.node.started` | `node_start` |
| `graph.node.completed` | `node_complete` |
| `tool.started` / `tool.completed` | `tool_call` |
| `task.waiting_review` | `human_review_required` |
| `task.completed` | `complete` |
| `task.failed` | `error` |
| 其余（`task.queued` / `task.claimed` / `agent.*` 等） | 不发给浏览器，仅供可观测性消费 |

**这个映射表是实现兼容性的核心**——前端零改动（见 §0.3）。

### 9.5 保留

```text
XADD ... MAXLEN ~ {event_retention}（默认 1000，配置化）
```

慢客户端不会拖慢 worker：worker 只做 XADD 后继续，从不等待消费者。

### 9.6 禁止写入事件的内容

```text
API key / secret / authorization header
完整环境变量
不必要的完整 prompt、完整网页正文
```

---

## 10. SSE 投影

### 10.1 端点

```text
保留 GET /api/research/{thread_id}/stream   ← 前端已依赖此路径
语义变为：只读 Redis Stream + DB，不构建 graph
```

§14 允许 `GET /events` 或兼容旧路径。**保留旧路径**是前端零改动的前提。

### 10.2 行为

```text
1. 校验任务存在（DB）
2. 若任务已终态 → 直接发终态事件并结束（不订阅）
3. 读取 Last-Event-ID（或 query 参数 cursor）
4. XREAD 从该 cursor 起回放
5. 回放完成后转为阻塞读取，持续推送
6. 客户端断开 → 仅结束本次投影，**不影响 worker**
```

**SSE handler 禁止构建 graph、禁止写任务状态**——本轮核心验收项。

### 10.3 Replay 语义

```text
收到 event 42
→ 断开
→ 期间产生 43..51
→ 以 Last-Event-ID=42 重连
→ 回放 43..51
→ 转实时
```

### 10.4 兜底

若任务已终态但 Stream 因 MAXLEN 已驱逐早期事件，投影层**以 DB 为准**
补发终态事件（DB 是业务事实源）。避免「连上来什么都没有」。

---

## 11. Source of Truth 与写入顺序（§20）

```text
Task DB          = 业务事实源（唯一）
Redis Checkpoint = 图执行状态
Redis Queue      = transport
Redis Events     = projection（可丢失，可由 DB 补偿）
```

**写入顺序**：

```text
completed:
  1. persist final_report + status=COMPLETED + 提交事务
  2. emit task.completed
```

**绝不先发事件再写 DB。** SQLite 与 Redis 之间没有分布式事务，
**不假装有**。补偿策略：

```text
事件丢失  → DB 状态正确；客户端重连时由投影层按 DB 补发终态
事件重复  → sequence 单调，消费者可去重
DB 写失败 → 不得发完成事件；任务保持原状态，走重试策略
```

---

## 12. Redis 命名空间

```text
dr:claim:task:{thread_id}
dr:jobs
dr:jobdedupe:{thread_id}
dr:events:{thread_id}
dr:evseq:{thread_id}
```

连接串由 `redis.url` + `redis.db` 组合（修正 §0.4 的第 2 条：
URL 当前未体现 db）。checkpointer 的 key 由 langgraph-checkpoint-redis
自行管理，前缀与上述区分。

---

## 13. Redis 故障语义（§35.4）

| 故障点 | 行为 | 任务状态 |
|---|---|---|
| enqueue 时不可达 | API 返回 503，不投递 | 保持 PENDING（用户可重试） |
| dequeue 时不可达 | worker 退避重试；Stream 条目仍在 | 不变 |
| **执行中 checkpoint 写失败** | 图运行抛错；**worker 不标 FAILED** | 保持 RUNNING，claim 过期后由新 worker 从**最后一个成功 checkpoint** 恢复 |
| event emit 失败 | 记录并重试；DB 已是事实源 | 不变（投影层兜底） |
| heartbeat 时不可达 | 停止续约 | 不变（claim 自然过期） |

**硬性禁止**：

```text
Redis / checkpoint 失败后继续执行并标记 COMPLETED
静默降级到 InMemorySaver（延续 Phase F 已建立的原则）
```

**恢复后如何判断**：

```text
checkpoint 存在且可加载 → resume
checkpoint 存在但损坏   → 计一次 attempt，走重试
checkpoint 不存在       → 属首次执行；若任务已是 RUNNING 则属异常，
                          计一次 attempt
attempt >= max_attempts → FAILED，持久化 error_code
```

---

## 14. Graceful Shutdown

已决策：**停止 heartbeat，等 claim 自然过期，不主动 release。**

```text
SIGTERM / SIGINT
  → 置 shutdown 标志
  → 停止消费新 job（XREAD 循环退出）
  → 当前 job 跑到下一个安全边界（节点边界）后停止
  → 持久化已产生的结果
  → 停止 heartbeat 任务
  → **不释放 claim**（让它自然过期）
  → 关闭 Redis / DB 连接
  → 退出
```

**为什么不等价于主动 release**：图可能正跑在某个节点中途，checkpoint 只到
上一个节点边界。主动 release 会让另一个 worker 立刻接管并**重复执行当前
节点**——对 LLM 调用意味着重复计费。让 claim 自然过期可保证 TTL 窗口内
绝无第二执行者。

代价：恢复延迟一个 TTL（生产默认 30 s）。这是用可接受的延迟换取
§2「duplicate enqueue != duplicate execution」不变量的确定性。

---

## 15. Retry Policy

### 15.1 分类（§12 要求区分）

```text
可重试
    Redis 瞬时不可达 / 连接重置
    provider 瞬时错误：LLM 超时、限流、5xx
    DB 锁竞争

不可重试
    领域错误：非法状态迁移、任务不存在
    invalid state：review 状态不符
    用户取消
    checkpoint 永久损坏 / 反序列化失败
    配置错误
```

### 15.2 参数

```text
                   生产默认    测试值
max_attempts          3          2
backoff               指数        固定 1 ms
```

每次 retry 递增 `attempt` 并落库；`attempt >= max_attempts` → FAILED
并持久化 `error_code`。

**不要所有异常统一 retry**（§12 明令）。

---

## 16. API 侧 Graph 构建移除（§35.3）

| 位置 | 处理 |
|---|---|
| `backend/main.py:93-95` lifespan 预热 | **删除**。图归 worker；API 不再预热 |
| `backend/routes/research.py:137` SSE 驱动 | **删除**，改为投影 |
| `backend/services/agent_service.py:271` `get_report` | 改为**直接读 DB**（§20） |
| `backend/routes/research.py:112/151/176` `aget_state` | **删除**，靠 DB 状态判断 |

最终 API 不 import `_create_builder`，也不持有 checkpointer。

`agent_service` 的模型相关函数（`build_input` / `build_resume_command` /
`extract_final_state`）**移交给 worker 侧模块**。

---

## 17. Worker Runtime

### 17.1 入口

```text
python -m backend.worker
```

### 17.2 生命周期

```text
start
 → 连接 Redis，校验 checkpointer 可用（失败则退出，不降级）
 → 校验 DB schema 版本（复用 backend/db/schema.py）
 → 以 dr:workers 组成员身份消费 dr:jobs
 → 对每个 job：
      解析 job → atomic claim
      抢不到 → XACK 并跳过（他人负责）
      claim 成功 → 记录 claimed_by / claimed_at / heartbeat 启动
      读 DB 任务
      终态 → 释放 claim，XACK
      cancel_requested → 转 CANCELLED，释放 claim，XACK
      WAITING_REVIEW 且有未生效 review → 应用 review，Command(resume=...)
      其余 → 从 checkpoint resume 或首次执行
      执行中：每个事件 → 分配 sequence → XADD 事件 → 检查 cancel
      结束：
         成功 → persist final_report + COMPLETED，再 emit
         失败 → 按 §15 判断 retry / FAILED
      WAITING_REVIEW（HITL 中断）→ persist draft + WAITING_REVIEW，再 emit
         → **释放 claim**
      释放 claim / XACK
 → shutdown 信号 → §14 流程
```

### 17.3 worker_id

```text
{hostname}:{pid}:{uuid4().hex[:8]}
```

不能只用 PID（§10 明令）——容器与 PID 复用会撞。

---

## 18. 强制不变量（§2）如何被满足

| 不变量 | 由什么保证 |
|---|---|
| Browser disconnect != task cancellation | SSE 投影层不写状态、不释放 claim |
| SSE disconnect != worker stop | worker 不感知 SSE 存在 |
| FastAPI restart != task stop | worker 是独立进程 |
| Worker crash != permanent task loss | claim TTL 过期 + checkpoint resume |
| duplicate enqueue != duplicate execution | Atomic Claim（不是 queue dedupe） |
| duplicate resume != double graph resume | claim + checkpoint 的 interrupt pending 判定 |
| cancel request != unsafe process kill | 协作式边界检查，不做强制中止 |

---

## 19. 测试设计要点

```text
并发测试必须真实制造竞争（§24）
  用多线程/多进程同时打 SET NX，不能串行模拟后声称证明原子性

时间参数用测试值（§30）
  heartbeat 1s / TTL 3s / backoff 1ms，不真实等待几十秒

跨进程场景沿用 Phase F 的 subprocess 手法
  已验证可行（scripts/worker_spike.py）

真实 data/tasks.db 保护
  conftest 的会话级守卫继续生效；worker 测试必须注入隔离 DB
```

---

## 20. §35 Design Gate 检查清单结果

```text
[x] Atomic claim algorithm              §2
[x] claim TTL / heartbeat               §3
[x] stale recovery                      §4
[x] cancellation semantics              §5
[x] queue transport                     §8
[x] event schema                        §9.2
[x] event retention                     §9.5
[x] SSE replay                          §10.3
[x] review/resume path                  §6 / §7
[x] DB source-of-truth                  §11
[x] Redis failure semantics             §13
[x] graceful shutdown                   §14
[x] retry policy                        §15

[x] 35.1 Review decision persistence    §6
[x] 35.2 HITL boundary claim release    §7
[x] 35.3 API-side graph removal         §16
[x] 35.4 Redis mid-execution failure    §13
```

**无重大未决项。可以进入 §36 的实施（G1–G9）。**

---

## 21. 实施顺序与影响面

```text
G1 Runtime primitives   claim / worker identity / heartbeat
G2 Queue                enqueue / consume / dedupe
G3 Worker               start / resume / failure persistence
G4 Event Stream         schema / publisher / retention
G5 API command path     create / review / cancel → enqueue
G6 SSE projection       replay / reconnect / disconnect independence
G7 Recovery             stale claim / worker crash / API restart
G8 Failure injection
G9 Server V1 readiness
```

**迁移 0002 的内容**（G1 之前或同期）：

```text
tasks 表新增：attempt / claimed_by / claimed_at / heartbeat_at
              cancel_requested / cancel_requested_at
新建表：task_reviews
```

不得修改 `0001_baseline_tasks.py`。

**Docker/Compose**：`redis:7-alpine` → `redis/redis-stack-server`，
并新增 `worker` 服务。
