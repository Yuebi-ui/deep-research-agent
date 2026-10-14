# 原版测试与实验结果冻结说明

本次面试展示版只对**文档、校验工具和历史归档**作整理，不修改 V3 研究图、Backend、活动测试或原有的 Benchmark/Results 数据。

## 原版与 V3 差异

| 内容 | 原版 | V3 活动版本 | 处理 |
|---|---:|---:|---|
| `benchmarks/` | 33 个文件 | 33 个文件，逐文件 SHA-256 相同 | 原样保留 |
| `results/` | 27 个文件 | 27 个文件，逐文件 SHA-256 相同 | 原样保留 |
| `tests/` | 57 个文件 | 60 个文件 | 原版单独归档，V3 活动测试不覆盖 |

V3 对原版 5 个测试做了并行路径适配：

- `tests/conftest.py`
- `tests/test_phase5a_research_seed.py`
- `tests/test_phase5b_fencing.py`
- `tests/test_phase5b_runner_lineage.py`
- `tests/test_runtime_ownership.py`

V3 新增 `test_quality_gate_v1.py`、`test_quality_gate_v2.py` 和 `test_quality_gate_v2_api.py`（都在 `tests/` 下）。原版 57 个文件完整保存在 [`historical/original-tests.zip`](../historical/original-tests.zip)。

## 如何验证没有无意修改原版结果

```bash
python scripts/verify_original_baseline.py
```

校验器检查原版完整测试归档、活动项目中的 Benchmark/Results 全部文件，发现缺失、内容变化或额外文件时会报错退出。参考清单：[`historical/original_sha256_manifest.json`](../historical/original_sha256_manifest.json)。

**注意**：原版 README 与 `results/live/v1/manifest.json` 记录的 `root_readme_sha256` 在源包里已经不一致；即使不修改任何历史测试，原版的 `benchmarks/verify_readme_results.py` 仍会在该断言失败。这是独立存在的旧版结果元数据问题。现阶段保留原始证据，**没有修改 manifest 或 Benchmark 测试来制造通过**。

历史报告指标归历史版本所有，不能据此声称 V3 已经验证速度、成功率或报告质量有所提升。
