# Benchmark 与 Ablation

这里保留评测运行器、质量指标、故障观测聚合器和消融配置。**大规模展开的 fixture、逐题结果和 trace 只在本地生成，默认写入被 Git 忽略的目录**。

## 数据输入与公开范围

| 数据 | 规模 | GitHub 状态 |
|---|---:|---|
| Research task prompts | 60 | 保留任务定义 |
| Memory 检索 corpus / queries | 360 / 120 | 不提交展开文件，运行时由 `fixtures/build_public_datasets.py` 生成虚构 fixture |
| Citation contract cases | 32 | 保留规则测试输入；已实际执行离线验证 |
| Fault plan | 40 | 保留计划定义 |
| 逐题检索、任务与故障明细 | 按需生成 | 隐藏在 `artifacts/`，不公开日志 |

数据生成器：[`fixtures/build_public_datasets.py`](fixtures/build_public_datasets.py)，引用规则生成器：[`fixtures/build_citation_cases.py`](fixtures/build_citation_cases.py)。所有检索语料均为虚构实体和 `.example` URL，不应当作真实外部事实。

## 本地离线复算（不调用 LLM）

```bash
python benchmarks/fixtures/build_public_datasets.py
python benchmarks/publish_offline.py --verify
python benchmarks/run_ablation.py --mode offline
python benchmarks/evaluate_citations.py
python benchmarks/evaluate_faults.py
python -m unittest discover -s benchmarks/tests -v
```

本地四组检索方案：`bm25_local`、`char_tfidf_local`、`rrf_bm25_char` 和 `rrf_bm25_char_entity`。融合调用了项目已有 `deep_research.memory.retrieval.fuse_records()`；

结果见 [`results/offline/v1/`](../results/offline/v1/README.md)。每题明细由 `run_offline.py` 写入 `artifacts/benchmarks/`，不会上传 GitHub。

##  Agent E2E 与故障恢复

Preset：[`configs/runtime_ablation.v1.json`](configs/runtime_ablation.v1.json)。实际运行时须保持任务集、模型、Prompt、搜索工具与参数一致，在不同 preset 之间重启 Worker。

```bash
# 需要真实服务和明确授权的外部 Provider；可能产生费用
python benchmarks/run_live.py --variant memory_off --provider-kind live --limit 1 --confirm-live
python benchmarks/run_live.py --variant stage_plus_episodic --provider-kind live --limit 1 \
    --output artifacts/benchmarks/candidate.jsonl --confirm-live

python benchmarks/merge_runs.py --inputs \
    artifacts/benchmarks/live_task_runs.jsonl \
    artifacts/benchmarks/candidate.jsonl \
    --output artifacts/benchmarks/combined.jsonl
python benchmarks/evaluate_runs.py --input artifacts/benchmarks/combined.jsonl \
    --baseline memory_off --candidate stage_plus_episodic
```

`run_live.py` 仅记录它能观察到的事实。Token、搜索调用与费用可以按 task/run ID 合并项目的 `baseline_metrics`：

```bash
python benchmarks/import_runtime_metrics.py \
    --tasks artifacts/benchmarks/live_task_runs.jsonl \
    --artifact-dir artifacts/baseline
```



故障观察用 `evaluate_faults.py --observations artifacts/benchmarks/observed_faults.jsonl` 聚合；没有观测记录时，输出 `null` 而非假定成功。

## 汇总报告

[`results/live/v1/`](../results/live/v1/README.md) 提供与根 README 一致的**历史真实汇总值**，但原始 E2E / Chroma / 审阅 / 故障日志未保留；无法核实的费用、分域统计和原始分母保持 `null`。

```bash
python benchmarks/verify_readme_results.py
# 以下生成器仅验证另一目录中的合成数据，并非验证历史实测
python benchmarks/fixtures/build_full_reference_results.py --verify
# 若需要本地逐条合成参考数据，在被忽略的 artifacts/ 下重建：
python benchmarks/fixtures/build_full_reference_results.py --raw-output artifacts/benchmarks/reference_raw
```

当前可运行的评测入口见本目录脚本，结果状态见 [结果说明](../results/README.md)。
