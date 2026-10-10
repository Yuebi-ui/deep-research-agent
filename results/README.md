# Result provenance / 结果来源

本项目同时保留三种**不能混为一谈**的数据来源：

| 路径 | 来源 | 可验证范围 |
|---|---|---|
| [`live/v1/`](live/v1/README.md) | 项目负责人确认的历史真实指标；来自根 README | 可验证汇总文件与 README 一致；压缩包内无历史运行和审阅原始日志，不能独立重跑 |
| [`offline/v1/`](offline/v1/README.md) | 公开虚构语料上的确定性本地离线检索与引用规则测试 | 可用 `publish_offline.py --verify` 独立复算；不是 Chroma / Agent E2E |
| [`examples/reference_v1/`](examples/reference_v1/README.md) | 固定种子公式生成的合成案例 | 可重新生成，**不能证明真实 Agent 指标** |
| [`examples/synthetic_task_runs.example.jsonl`](examples/synthetic_task_runs.example.jsonl) | 6 题 × 2 组的最小合成测试输入 | 测试聚合器 / 异常边界，非真实评测 |

`live/v1/e2e_summary.json`、`fault_summary.json`、`retrieval_summary.json`、`paired_comparisons.json` 已按照根 `README.md` 的公开数字整理，并在 `live/v1/manifest.json` 中说明来源。现存 ZIP **没有**历史 30 题运行表、逐报告审阅、逐题 Chroma 相关性标注、故障事件明细或 bootstrap 抽样数据。缺失分母和细分指标均为 `null`，不能从旧合成示例伪造。

`offline/v1/fault_plan_status.json` 记录公开离线故障计划 **尚未在离线 fixture 中执行**，并不否定根 README 所述的历史独立 40 次故障实验。两者不是一份实验。

## 校验命令

```bash
# 仅核对根 README 与 live/v1 的公开汇总数字是否一致，不验证丢失的原始实验
python benchmarks/verify_readme_results.py

# 参考样例可由固定生成器原样重算（与 live 数据独立）
python benchmarks/fixtures/build_full_reference_results.py --verify

# 公开离线词法检索可复算（与历史 Chroma 数据独立）
python benchmarks/publish_offline.py --verify

python -m unittest discover -s benchmarks/tests -v
```

如有机会恢复历史日志，推荐用 `benchmarks/evaluate_runs.py`、`evaluate_faults.py` 和 `analyze_trials.py` 再次汇总，并将可追溯的运行配置、审阅规则、每项有效样本数和置信区间推导补入 manifest。不要将合成案例当成丢失的历史观测。
