# 数据集说明

此目录只保留小型任务 / 故障计划及引用规则数据。大规模虚构检索语料不随 Git 仓库分发；运行时可确定性重建。

```bash
python benchmarks/fixtures/build_public_datasets.py
```

- `research_tasks.v1.jsonl`：60 条待运行的研究 Prompt（不是已生成报告）。
- `fault_scenarios.v1.jsonl`：40 条故障测试计划（不是已成功恢复的观测）。
- `citation_contract.v1.jsonl`：32 个静态引用约束测试案例。
- * **`memory_corpus.v1.jsonl` / `memory_queries.v1.jsonl`**：原有 360 条合成片段与 120 个检索问题保留为可复现的离线回归测试集；另基于人工标注的真实研究证据集，在真实 Chroma + Embedding 环境中完成 Dense / Hybrid Retrieval 对比及消融评测。公开的 Chroma Recall@5 与 nDCG@10 汇总记录于 `results/live/v1/`；完整标注方法、原始观测和配置指纹已不在本压缩包中。这份压缩包只保留根据根 README 转录的历史真实汇总；原始真实数据与日志已缺失，无法直接从现存数据核验。60 条公开 Prompt 也不能直接当作历史 30 题的运行样本。


生成器无外部 API 调用；结果可按 `results/offline/v1/manifest.json` 中的哈希验证。
