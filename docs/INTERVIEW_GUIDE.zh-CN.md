# 面试讲解提纲（中文）

这份文件是项目讲解辅助，不是运行说明。建议把项目讲成两个核心问题：

1. **长任务怎么可靠执行？**
2. **历史研究怎么安全复用？**

## 30 秒版本

> 这是一个基于 LangGraph 的 Deep Research Agent。API 不直接跑 Agent，而是把任务
> 持久化后交给 Worker；Worker 用 Redis queue、claim、heartbeat 和 checkpoint 做
> 可恢复执行。研究完成后，最终报告先作为业务事实落库，再通过 durable outbox 异步
> 整理成章节记忆、Claim/Evidence、Episodic Memory 和 Temporal Claim。历史记忆只能
> 作为未核验线索参与下一次研究，不能直接替代当前证据。

## 5 分钟版本

### 1. 为什么把 API 和 Agent 执行拆开？

研究任务很长，HTTP 生命周期不可靠。API 只负责 command/query/SSE，Worker 才拥有
LangGraph。这样客户端断开或 API 重启不会等价于研究失败。

### 2. 怎么避免两个 Worker 重复执行？

Redis job 只是投递，不是唯一安全边界。真正的安全边界是带 TTL 的 task claim；Worker
持续 heartbeat，失去 ownership 后不能 finalize/ACK。未 ACK 的 job 可以被 reclaim，
checkpoint 用于恢复执行。

### 3. Memory 3.0 为什么不是简单向量库？

因为 Deep Research 的记忆有三种不同问题：

- **Semantic**：过去发现过什么事实和证据；
- **Episodic**：过去实际发起过哪些查询、哪些工具失败；
- **Temporal**：同一事实在不同时间版本发生了什么变化。

所以我没有把所有历史文本一次性塞给模型，而是分层存储、阶段化检索、硬预算注入。

### 4. 为什么 Temporal Claim 不自动覆盖旧事实？

两个数字不同可能是时间区间、口径或范围不同，不一定冲突。系统只生成 possible change，
真正的 confirmed change/conflict 需要明确有效期、来源和审核记录。

### 5. 为什么要 Outbox？

报告生成成功以后，如果 Memory 写 Chroma 时 Worker 崩了，不能让用户任务重新失败，也
不能悄悄丢掉记忆。任务 completed 和 outbox job 在同一 SQLite 事务提交；之后 Memory
Worker 至少一次处理。exactly-once 不现实，所以通过稳定 ID + 幂等重放解决。

## 容易被追问的问题

### Q: 为什么不用 Neo4j / Graphiti？

A: 当前最核心的是 provenance、freshness、recovery 和 retrieval，不是任意多跳图查询。
先把 Claim/Evidence/Temporal 的正确性做稳；只有当图查询带来可测量收益时再增加图数据库。

### Q: 为什么 Episodic Memory 不让模型总结“成功经验”？

A: 模型自评很容易把偶然成功固化成错误策略。现在只存 observable trace：query、domain、
tool error、是否产出 findings。未来如果做 procedural memory，会要求多次验证或人工确认。

### Q: 这个项目现在生产可用吗？

A: 我会明确回答“核心架构和关键路径已经实现，离线测试和历史 E2E 证据都有，但最新 Memory
3.0 版本还需要真实 Redis/Chroma/外部模型的重新 E2E，以及 tenant isolation 和
observability hardening”。这比把离线测试包装成生产验证更可信。

### Q: 你最满意的一处工程设计是什么？

可以讲 durable completion boundary：最终报告是 source of truth，Memory 是 derived data。
这让用户可见完成时间、故障恢复和数据一致性三者的边界非常清楚。

## 建议现场打开的文件

1. `architecture/README.md`
2. `architecture/MEMORY.md`
3. `backend/runtime/runner.py`
4. `backend/runtime/memory_outbox.py`
5. `deep_research/memory/manager.py`
6. `deep_research/memory/stage_retrieval.py`
7. `deep_research/memory/temporal.py`
