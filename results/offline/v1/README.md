# 本地检索 / 引用规则：已执行的离线汇总


| 方法 | Recall@1 | Recall@3 | Recall@5 | MRR@10 | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| BM25 | 51.7% | 100.0% | 100.0% | 0.692 | 0.769 |
| Character TF-IDF | 71.7% | 94.2% | 98.3% | 0.825 | 0.869 |
| BM25 + Character RRF | 62.5% | 86.7% | 86.7% | 0.746 | 0.777 |
| RRF + Entity | 52.5% | 80.0% | 80.0% | 0.663 | 0.699 |

对四组方案使用同一组 **120** 个标注查询和 **360** 条虚构章节记录。融合通道增加后，本 fixture 中部分排序变差；不能只挑最高的一行声称混合检索有效。

同一快照下，引用规则检查的 **32/32** 个案例与预期行为一致，但其中有 **4 个**内容不被来源支持的案例仍通过静态规则。因此该检查不提供事实正确率或真实 Grounded Citation Rate。

`fault_plan_status.json` 只记录**本离线 fixture 的计划未执行**，并不是根 README 中历史 40 次真实故障实验的状态；历史汇总独立见 [`results/live/v1/fault_summary.json`](../../live/v1/fault_summary.json)。

只公开 `retrieval_summary.json`、`ablation_summary.csv`、`citation_contract.json`、`fault_plan_status.json`、`manifest.json`。详细的 `retrieval_per_query.jsonl` 不上传；需要时在本地生成：

```bash
python benchmarks/publish_offline.py --verify
python benchmarks/run_offline.py --output artifacts/benchmarks/offline_v1
```

生成代码：[`benchmarks/fixtures/build_public_datasets.py`](../../../benchmarks/fixtures/build_public_datasets.py)。公开 manifest 中保留原 fixture/源码的指纹，可用上面的命令重新计算核对。
