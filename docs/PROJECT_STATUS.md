# 项目状态与验证范围

本文区分**代码已经实现的能力**、**已有验证记录**和**仍需验证的运行环境**。项目的功能与测试情况应以实际代码和测试结果为准，不将离线测试等同于生产环境验证。

## 核心实现

| 模块 | 实现状态 | 说明 |
|---|---|---|
| FastAPI 命令、查询与 SSE 接口 | 已实现 | API 不直接执行研究图 |
| Redis 任务队列、Worker 接管与恢复 | 已实现 | 需要真实 Redis 验证运行路径 |
| LangGraph 研究、HITL、核查与 Writer | 已实现 | 图拓扑受功能开关影响 |
| 报告与章节级记忆 | 已实现 | 支持确定性切分与重建检查 |
| 结构化 Claim / Evidence | 已实现 | 包含来源追踪与结构化抽取 |
| 混合检索 | 已实现 | 语义、关键词和实体信号融合 |
| Supervisor / Researcher 阶段检索 | 已实现 | 受预算控制，作为非可信历史参考 |
| Episodic Memory | 已实现 | 保存实际观察到的研究轨迹 |
| Temporal Claim 审核账本 | 基础能力已实现 | 权威时序关系依赖明确审核 |
| 持久化 Outbox 与记忆整理 | 基础能力已实现 | 支持租约、重试与死信处理 |
| 多租户记忆隔离 | 尚未实现 | 共享部署前需要补齐 |
| 高级知识图谱 / A-MEM | 尚未实现 | 见 Roadmap |

## 验证情况

**离线测试：** 仓库提供四个独立的 Memory smoke suite，用于验证确定性的存储、检索、时间审核和异常恢复逻辑：

- `tests/offline_phase123_smoke.py`
- `tests/offline_memory3_smoke.py`
- `tests/offline_memory56_smoke.py`
- `tests/offline_memory7_temporal_smoke.py`

这些测试使用模拟存储和/或本地 SQLite，不能替代真实 Chroma、Redis 与外部 LLM 的端到端验证。

**完整测试：** `tests/` 中的 pytest 用例由 CI 配置为 Fake / Offline 模式运行。完整执行需要安装项目依赖。

**历史 E2E：** [E2E_EVIDENCE.md](E2E_EVIDENCE.md) 保存了较早的 Hybrid Runtime 实验结果；这些结果产生于当前 Memory 3.0 功能加入之前，不代表最新版本已经重新通过完整 E2E。

## 后续工程验证

- 为用户与租户增加记忆访问边界和清理策略；
- 进行独立 Memory Worker 的负载与崩溃恢复测试；
- 检查时序审核流程在实际研究数据中的准确性；
- 重新评估当前记忆路径的成本、延迟与报告质量；
- 真实运行 API、Redis、Chroma、LangGraph 和外部 Provider 的集成链路。

本仓库不包含前端实现，通过 REST / SSE 提供外部客户端接口。
