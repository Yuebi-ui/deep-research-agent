# 🔴 合成参考结果（不是 Agent 实测）

> 🔴 **本页所有 E2E、质量、成本和故障数据均由固定种子生成，用于演示结果结构和数值关系；不能用于声称项目已达到这些指标。**

本示例与仓库中真实执行的 [离线词法检索压力测试](../../offline/v1/README.md) 完全分开。

## 🔴 研究任务消融（60 个任务 / 组，四组共 240 行合成任务记录）

| 组别 | 成功任务 | 完成任务 | P50 / P95 (s) | Search/task | Token/task | 假设成本 (元/task) | 不支持 Claim | 来源支持引用 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| memory_off | 46/60 (76.7%) | 53/60 | 223.0 / 260.9 | 12.2 | 56,005 | 2.35 | 16.6% | 82.0% |
| report_section_memory | 49/60 (81.7%) | 55/60 | 218.1 / 261.1 | 11.3 | 53,763 | 2.28 | 13.9% | 85.3% |
| stage_recall | 51/60 (85.0%) | 56/60 | 215.0 / 258.2 | 9.8 | 51,820 | 2.20 | 12.2% | 88.7% |
| stage_plus_episodic | 54/60 (90.0%) | 57/60 | 212.1 / 255.4 | 8.8 | 50,306 | 2.14 | 8.4% | 92.8% |

## 🔴 假设故障注入（不代表实际运行）

- 模拟场景：40 / 40；自动恢复：39 / 40（97.5%）。
- 恢复耗时：P50 26.08s / P95 36.65s（只统计已恢复场景）。
- 模拟记录中的任务丢失数：0；重复持久化写入数：0。
- 模拟的未恢复场景保留在 Dead Letter，需要人工处理；它不计入成功率或恢复耗时。

## 另一个数据来源：真实执行过的离线词法检索测试

> **下面这张表是代码真实计算得到的离线 fixture 分数，不是上述 Agent 合成示例、不是 Chroma Embedding 实测、也不是线上效果。**

| 离线检索方案 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| bm25_local | 51.7% | 100.0% | 100.0% | 0.692 | 0.769 |
| char_tfidf_local | 71.7% | 94.2% | 98.3% | 0.825 | 0.869 |
| rrf_bm25_char | 62.5% | 86.7% | 86.7% | 0.746 | 0.777 |
| rrf_bm25_char_entity | 52.5% | 80.0% | 80.0% | 0.662 | 0.699 |

公开 fixture：360 条文档、120 个标注问题；结果存在融合排序退化，应保留而不美化。详见 `results/offline/v1/`。

## 🔴 口径与限制

- `reviewed_success` 是模拟的审阅结论；HTTP completed 不直接等同于质量合格。
- Unsupported Claim 为假设审阅的“不支持 Claim 数 / 审阅 Claim 总数”；引用指标也是假设逐引用核查结果，不是静态编号检查。
- 失败且没有报告的任务，不伪造 Claim/Citation 审阅数；汇总仅对有可审阅报告的任务计算比例。
- Token 为模拟用量；成本按固定**虚构单价**（见 `manifest.synthetic.json`）计算，绝非实际账单。
- 故障场景的 `recovered=false` 无恢复耗时，这是有意义的缺失值，而不是遗漏数据。
- 四组消融仅使用仓库里真实存在的 feature flag 组合，未将 Claim Verification/Outbox 冒充可切换实验。
- 这些样例不能用于简历成果数字、真实项目效果宣称或与论文榜单比较。

## 复算

```bash
python benchmarks/fixtures/build_full_reference_results.py --verify
python -m unittest discover -s benchmarks/tests
python benchmarks/publish_offline.py --verify
```

🔴 逐任务和逐故障观测原始记录不放入公开仓库；本地需要时可用生成器重建：

```bash
python benchmarks/fixtures/build_full_reference_results.py --raw-output artifacts/benchmarks/reference_v1_raw
```
