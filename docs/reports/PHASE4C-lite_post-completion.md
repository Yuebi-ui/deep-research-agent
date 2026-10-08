# PHASE 4C-lite — Post-report Enrichment 关键路径移除

> 生成：2026-10-06 · 起点 `c8215b5`（Phase 4B 已提交）· 3 次真实 E2E 验证（`phase4c-lite-{1,2,3}`，均 VALID）
> 结论：结构化记忆落库（Phase 4B 实测 11–20s）已从 **user-visible completion** 移出，进入受生命周期管理的后台任务；
> 真实产品行为变化（任务更早 completed + 报告已落库 + 事件已发布），**不是计时口径的修改**。

---

## 1. Before / After 依赖路径

**Before（`c8215b5`）**——落库在节点内，用户完成时刻被它推后：

```
final_report_generation:
    writer.astream(...)                      71.2–91.3s   → report
    await asyncio.to_thread(store_from_report)  10.9–19.6s  ← 阻塞节点返回
    return {"final_report": ...}
runner._settle → _finish_completed:
    task.status=completed; task.updated_at=now(); save     ← user-visible completion
    publish task.completed
worker: release claim → ACK → 消费下一个 job
```

**After（本轮）**——落库在完成之后，且受生命周期管理：

```
final_report_generation:
    writer.astream(...)                      → report
    return {"final_report": ...}                       ← 节点不再做落库
runner._settle → _finish_completed:
    task.status=completed; task.updated_at=now(); save     ← user-visible completion
    publish task.completed
    ├─ 有 tracker：PostCompletionTasks.schedule(...)（**空 context**，不继承 collector）
    └─ 无 tracker：内联 await（兼容路径，绝不 fire-and-forget）
worker: release claim → ACK
    └─ 消费下一个 job 之前：await tracker.wait_idle()        ← 本地 Chroma 访问保持串行
       后台任务：to_thread(store_report_memory)  ~15–16s
                 → reliability 事件 post_completion_enrichment{outcome, elapsed_ms}
worker 退出：await tracker.drain(timeout=60s)               ← 有界优雅收尾
```

代码落点：`deep_research/agent_builder.py`（节点去掉落库 + 新增 `store_report_memory` / `extract_user_query`）、
`backend/runtime/post_completion.py`（新，生命周期）、`backend/runtime/runner.py`（`_finish_completed` 之后调度）、
`backend/worker.py`（持有 tracker、`wait_idle`、`drain`）。

---

## 2. user-visible completion wall / fully-settled wall

三次 E2E（同一 query、同配置、benchmark 口径 `task.created_at → task.updated_at`）：

| run | E2E（user-visible） | final 节点 | writer 调用 | **节点 − writer**（= 被移出部分） | 落库（完成之后） | **fully settled** |
|---|---|---|---|---|---|---|
| phase4a-p1（before） | 307.8s | 109.1s | 91.3s | **17.8s** | 节点内 | 307.8s |
| phase4a-p2（before） | 328.2s | 87.8s | 75.2s | **12.6s** | 节点内 | 328.2s |
| phase4a-p3（before） | 285.8s | 92.4s | 71.2s | **21.2s** | 节点内 | 285.8s |
| **phase4c-lite-1** | **255.3s** | 58.9s | 58.9s | **0.0s** | 16.04s | 271.4s |
| **phase4c-lite-2** | **286.3s** | 71.6s | 71.6s | **0.0s** | 15.01s | 301.3s |
| **phase4c-lite-3** | **277.9s** | 66.6s | 66.6s | **0.0s** | 16.43s | 294.4s |

**怎么读这张表（口径纪律）**：

- **结构性、确定性的事实**：`final_report_generation` 现在**恰好等于 writer 调用**（before 是 writer + 10.9–19.6s 额外）；
  这 10.9–19.6s 从 E2E 里消失，且**同一时间出现在了完成之后**（fully settled 仍包含它）。
- **E2E 中位数 307.8 → 277.9s（−29.9s）**：其中只有 ~15–17s 可归因于本次改动，其余是云端波动
  （writer 在 after 组恰好更快：中位 75.2s → 66.6s；本次改动没有触碰 writer）。
  **不宣称 −30s。** 可归因的量 = 被移出的落库时长本身。
- **总工作量不变**：fully settled wall ≈ 改动前的 E2E 量级（271.4/301.3/294.4 vs 307.8/328.2/285.8，差异同样是云端波动）。

---

## 3. 正确性证据

| 项 | 证据 |
|---|---|
| run 有效性 | 3/3 `VALID`（preflight 全绿、fingerprint 前后一致、service freshness 通过） |
| 任务正确性 | 3/3 `completed`，attempts=2（HITL 正常），`context overflow=0`、`claim_losses=0`、`LLM failed=0` |
| 报告完整性 | 终稿 8152 / 9617 / 6888 字符，均以「参考文献」章节正常收尾，无截断；writer 输入 prompt 未改动 |
| 持久化正确 | 落库按原路径执行：`Added memory: <doc_id>` + `Structured extraction: {entities, claims}`；事件含 `doc_id`。改动前 baseline 的 payload 量级一致 |
| **落库恰好一次** | 三次 run 前后计数：semantic **33 → 36**（+3）、entities **445 → 487**（+42 = 15+12+15）、claims **330 → 360**（+30 = 10+10+10）——与三条 `Structured extraction` 日志逐项对上，无重复写、无丢失 |
| 顺序不变量 | 单测锁定：**先** 落库报告/置 completed/发布事件，**后** 落库记忆（`test_completion_precedes_enrichment_and_is_not_blocked`） |
| 观测不污染 | 后台任务在**空 context** 中运行，不继承 run 的 collector → run artifacts 可复现（单测锁定） |
| 既有测试 | full suite `657 passed / 1 skipped / 0 failed`；新增 11 个针对性测试 |

**实测日志（run-1，节选）**——顺序即证据：

```
21:37:20 任务 416c4896d2de 执行结束: status=completed        ← user-visible completion
21:37:20 等待 1 个 post-completion 任务结束后再消费下一个 job
21:37:36 Structured extraction: {'entities': 15, 'claims': 10, 'contradictions': 0}
21:37:36 post-completion 任务 memory-enrichment:416c4896d2de 结束: outcome=ok elapsed=16.04s
```

（对比改动前同一日志形态：`Structured extraction` **先于** `执行结束: status=completed`。）

---

## 4. 失败 / 恢复语义

| 场景 | 行为 | 证据 |
|---|---|---|
| 落库失败（网络/API/解析） | 只降级记忆：warning + reliability 事件 `outcome=failed`，**不影响任务状态**；内联路径同样隔离 | 单测 `test_enrichment_failure_does_not_affect_completion` |
| **worker 崩溃（落库中途）** | **记忆丢失、不重试**。报告与任务状态已在此之前 durable → 用户可见结果不受影响 | 语义变化，见 §5 |
| 优雅退出 SIGTERM | 等待在飞落库（上限 60s）后退出；实测 16.43s 落库完整跑完后进程才退出 | run-3 实测：21:50:58 SIGTERM → 21:51:14 enrichment ok → 21:51:15 worker 退出 |
| 退出超时 | `drain()` 记录被放弃的任务名与数量，显式放弃（best-effort 派生数据） | 单测 `test_drain_times_out_and_reports_abandoned` |
| 失去 claim 所有权 | `_finish_completed` 在写终态前就放弃 → **不写 completed、也不落库**（由新 owner 负责） | 单测 `test_ownership_lost_skips_completion_and_enrichment` |
| 取消 / HITL / resume | 路径未改动；取消走 `_finish_cancelled`（无落库），HITL 走 `_finish_waiting_review`（无落库） | 既有测试全绿 |
| 下一个 job 的串行性 | worker 在消费下一个 job 前 `wait_idle()` → 本地 Chroma 访问保持单并发，不引入新的并发写 | `backend/worker.py::serve` |

---

## 5. 审计结论（实施前要求的 8 个问题）

1. **调用点**：`agent_builder.final_report_generation` 节点尾部 → `MemoryManager.store_from_report`（`asyncio.to_thread`）。
2. **写入什么 durable state**：ChromaDB（`data/chroma`）——① `research_memory`：报告前 2000 字 + metadata（query/timestamp/length）；
   ② 结构化记忆：entities/claims/contradictions（E3 批量 upsert）。两次实测 +1 doc、+12~15 entities、+10 claims。
3. **后续 workflow 是否依赖**：任务自身不依赖；**下一个任务**的 `write_research_brief` 会 `retrieve_context(user_query)` 读取
   （`agent_builder.py:66`）——这是唯一影响面。
4. **fencing / heartbeat / shutdown 覆盖**：原先在节点内、受 claim 心跳与 ownership 中止保护；现在在 finalize **之后**
   （claim 已释放）。因为任务已终态，**不存在双跑风险**；生命周期由新的 `PostCompletionTasks` 承担。
5. **崩溃是否允许丢失**：允许（best-effort 派生数据）。**语义变化**：改动前节点未返回 ⇒ 不会被 checkpoint 标记完成 ⇒
   恢复时重跑该节点（至少一次）；改动后崩溃即丢失（不重试）。缓解：报告已落库、记忆可由报告重建；日志显式记录 outcome。
6. **exactly-once / at-least-once**：改动前 ≈ at-least-once（重跑会再写一次，靠 >0.85 相似度去重）；
   **改动后 = at-most-once**。这是本轮唯一的可靠性语义变化，已在此显式记录。
7. **用户紧接着开新任务是否要求立即可见**：改动后存在 ≤ 落库时长（15–16s）的可见性窗口。**当前单 worker 部署下影响为零**——
   worker 在消费下一个 job 前 `wait_idle()`，下一个任务开始检索时落库已完成；只有多 worker 并发才可能出现窗口。
8. **"completed" 的产品语义**：`task.status=completed` + `task.completed` SSE 事件 + `task.final_report` 已落库（前端据此展示终稿）。
   这三件事现在都不再等待记忆落库。

---

## 6. 本轮不做什么（边界）

未改：writer/report 生成、draft、research、claim_verification、thinking 策略、模型路由、vLLM 参数、HITL 语义、记忆内容与 schema。
未建设：新的 durable job/outbox 基础设施（审计结论是 best-effort 派生数据 → 走路径 A；§5-6 的语义变化已记录在案）。

## 7. Remaining risks

1. **崩溃丢失窗口**：落库期间 worker 崩溃 ⇒ 该任务的记忆永久缺失（可从报告重建，但不会自动重建）。
   若未来要求"记忆必达"，需要走 durable outbox（本轮明确不做）。
2. **可见性窗口（多 worker 时）**：当前单 worker 为 0；扩到多 worker 时，刚完成任务的记忆可能对新任务不可见 ≤16s。
3. **样本量**：n=3/臂，E2E 波动 ~15%；可归因部分是结构性的（节点差），不依赖 n。
4. **harness 之外的路径未覆盖**：`revise` 分支的 E2E 未跑（HITL revise 不触发落库路径，风险低）；
   跨进程 crash（kill -9）未实测，按语义推断为"丢失"。
5. **reliability 事件时机**：`post_completion_enrichment` 写在 run artifacts 快照**之后**（它在快照后才发生），
   因此只能在 `data/baseline_metrics/<run_id>/` 读到——这是预期行为，已在报告说明。
