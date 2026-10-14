# Deep Research Agent V3 — 面试讲解提纲

## 30 秒项目介绍（建议按实际测试情况陈述）

> 这是一个基于 LangGraph、FastAPI、Redis 和 SQLite 的可恢复多 Agent 深度研究后端。我在原有 Worker、Checkpoint、Memory 机制上，重构了研究执行流程：Draft 与 Supervisor/Researcher 并行；汇合后由独立 Evaluator 和 Red Team 检查研究证据，必要时有限轮次地定向补研；人工审核后再生成最终报告。新版本已有离线路由和节点测试，但真实 LangGraph 与 Worker 的端到端集成验证仍需完成。历史 Benchmark 是原版实验，不直接代表 V3 的改进效果。

## 面试中容易被追问的设计选择

| 问题 | 代码依据与回答重点 |
|---|---|
| 为什么让 Draft 与 Research 并行？ | `deep_research/agent_builder.py` 的并行主图让两分支在 Research Brief 后并发，减少 Draft 等待 Research 的串行阻塞；**不应在没有 V3 计时实验时宣称加速百分比**。 |
| Evaluator 在评估谁？ | `evaluate_research` 使用 Brief、Draft 和 generation 校验后的候选 Findings，评估**研究准备度与证据缺口**，不是最终报告评分器。 |
| Red Team 与 Evaluator 区别？ | Evaluator 偏覆盖度和证据充分性；Red Team 偏寻找矛盾、遗漏与反例，两者的意见供质量决策使用。 |
| 如何避免无休止补研？ | `deep_research/quality_gate.py` 检查是否有具体研究主题、预算和新证据；循环计数在 Checkpoint 状态里，不足或异常时交 HITL。 |
| 为什么 HITL 放在质量审查后？ | 用户能同时看 Draft、候选研究成果、质量意见，再决定批准、修改 Draft 或定向补研。 |
| 研究结果如何隔离？ | `research_generation`、seed fingerprint 和候选 `speculative_research` 防止旧分支结果混入；正式 `notes` 在审核通过后才接纳。 |
| 如何恢复任务？ | Worker 独占 LangGraph 执行，通过任务持久化和 Checkpoint 管理恢复；**真实重启恢复链路尚未在本次环境验证**。 |
| Claim Verification 核查什么？ | 当前验证发生在 Final Writer 前，主要针对 Draft 断言；不可说 Final Report 已经过独立事实级校验。 |
| Memory 3.0 的位置？ | Report/Section、Structured、Episodic、Temporal 和 Outbox 主要维护长期上下文和可追溯材料，不应将历史记忆直接当作本轮已验证来源。 |
| 为什么保留历史实验？ | `benchmarks/`、`results/` 的所有文件和原版逐字节一致。它们证明历史实验记录存在，不是 V3 自动补研改造后的性能评测。 |

## 演示建议（先做可重复的离线演示）

```bash
python scripts/test_quality_gate_v2_offline.py
python scripts/test_parallel_only_v3_offline.py
python scripts/check_repo_hygiene.py
```

想演示真实研究任务，先在隔离环境安装依赖、启动 Redis/Worker 并完成 [集成验收清单](V3_VALIDATION_STATUS.md)。不要在面试现场第一次验证尚未跑过的真实任务恢复链路。

## 诚实陈述验证范围

已验证：V2/V3 离线 20 项、历史文件哈希、代码语法和仓库契约。  
尚未验证：完整 LangGraph/Worker/Checkpoint E2E 与 V3 性能提升。  
独立历史问题：原版 `results/live/v1/manifest.json` 的 README 摘要不匹配，原版也存在；无需改写实验成绩。
