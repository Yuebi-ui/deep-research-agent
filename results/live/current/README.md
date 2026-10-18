# 当前架构实测实验：README 指标对齐汇总（原始记录未保留）

本目录的数字按项目根目录 [`README.md`](../../../README.md) “实测结果与消融分析”整理。**项目负责人确认该节为当前架构实测指标；但压缩包不包含对应的原始运行日志、报告审阅记录、Chroma 逐题检索结果和统计重采样样本。**这里可以核对“抄录与 README 一致”，不能证明重新执行了实验，也不能从现存文件独立复算当年的真实性能。

## Agent 消融：README 报告的 30 道研究任务、四组配置

| Variant | Completion | Reviewed Success | vs baseline | Reviewed Success 95% CI |
|---|---:|---:|---:|---|
| `memory_off` | 93.3% | 77.8% | — | — |
| `report_section_memory` | 94.4% | 81.1% | +3.3 pp | [-1.7, +8.3] pp |
| `stage_recall` | 95.0% | 83.3% | +5.6 pp | [-0.6, +11.7] pp |
| `stage_plus_episodic` | 96.1% | 85.6% | +7.8 pp | [+1.7, +14.4] pp |

下表是 README 提供的基线与最终配置对照；中间两组的其他指标未在 README 中给出，因此 JSON 中是 `null`。

| Metric | memory_off | stage_plus_episodic |
|---|---:|---:|
| Mean Aspect Coverage | 78.4% | 86.5% |
| Source Adequacy Pass Rate | 85.1% | 91.9% |
| Unsupported Claim Rate | 12.9% | 7.9% |
| Supported Citation Rate | 87.1% | 93.7% |
| Tool Success Rate | 98.5% | 98.6% |
| Search Calls / Task | 12.2 | 10.4 |
| Total Tokens / Task | 56,000 | 49,200 |
| Latency P50 / P95 | 223 / 268s | 219 / 273s |
| Context Overflow Count | 0 | 0 |

`e2e_summary.json` 保存以上汇总。30 道题是 README 的研究任务数，**不能凭百分比倒推每组整数成功次数**：原始运行分母和重复试验数没有保留，`n_runs`、`completed_tasks`、审阅分母等为 `null`。本目录不发布无法追溯的估算成本、单价或逐任务审阅伪数据。

## 真实 Chroma 检索汇总（120 道标注问题）

| Metric | Dense-only | Hybrid | README 报告变化 |
|---|---:|---:|---:|
| Recall@5 | 84.2% | 88.3% | +4.1 pp |
| nDCG@10 | 0.803 | 0.831 | +0.028 |

Chroma Recall@5 的增量为 +4.1 pp，检索实验与公开离线词法检索 fixture 分别统计。

## 真实故障注入汇总

根 README 报告 **40 次故障实验、38 次恢复、恢复率 95.0%**，见 `fault_summary.json`。各故障类型的执行数量、恢复用时 P50/P95、丢任务数、重复写入数、是否人工处理均未提供，因此一律标记为 `null`。`coverage_by_type` 只是仓库内 40 条公开测试场景的计划分布，**不是原始执行分布**。`results/offline/current/fault_plan_status.json` 为单独的离线计划状态，不代表这一组已报告的故障实验。

## 置信区间与缺失数据

Reviewed Task Success 的配对提升为 +7.8 pp，95% CI 为 [+1.7, +14.4] pp。

`domain_breakdown.csv` 仅保留表头，状态写于 `domain_breakdown.status.json`。公开的 60 题合成分域数据不能拿来充当当前架构实测 30 题实验的分域数据。

## 数据分区与检查

- [`results/examples/reference/`](../../examples/reference/README.md) 是**固定种子生成的合成示例**，与本目录真实指标的来源不同。
- [`results/offline/current/`](../../offline/current/README.md) 是**可复算的虚构离线检索**，与 Chroma / E2E 真实汇总不同。
- `manifest.json` 记录现存证据边界。`README_REPORTED_REAL_RUNTIME` 表示“由根 README 记录的当前架构实测指标”，**不是原始日志已校验**。

从项目根目录运行：

```bash
python benchmarks/verify_readme_results.py
python benchmarks/fixtures/build_full_reference_results.py --verify
python benchmarks/publish_offline.py --verify
python -m unittest discover -s benchmarks/tests -v
```

第一条命令只检查 README 指标与本目录汇总文件一致，后三条分别检查合成参考、离线 fixture 与回归测试；**没有一条能替代丢失的真实实验日志**。
