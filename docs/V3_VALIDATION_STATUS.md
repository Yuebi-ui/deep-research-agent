# V3 面试版验证状态

**检查日期**：2026-10-14  
**来源**：用户上传的原版 `deep-research-agent.zip` 与 V3 `deep-research-agent-parallel-only-v3.zip`。  
**验证环境**：Python 3.13，未安装 `langgraph`、`langchain_core`、`redis`、`ruff`；项目文档目标为 Python 3.12+。  
**范围**：离线代码级/节点逻辑/历史基线检查；**不构成真实服务端到端通过声明**。

## 本次复核已执行

| 检查 | 本次结果 | 命令 |
|---|---|---|
| V2 离线逻辑与节点测试 | 13/13 通过 | `python scripts/test_quality_gate_v2_offline.py` |
| V3 并行专用路由测试 | 7/7 通过 | `python scripts/test_parallel_only_v3_offline.py` |
| Python 语法编译检查 | 通过 | `python -m compileall -q deep_research backend scripts tests` |
| 仓库路径/文档契约 | 通过 | `python scripts/check_repo_hygiene.py` |
| 已提交离线确定性结果校验 | 通过（5 个文件） | `python benchmarks/publish_offline.py --verify` |
| 原版 Benchmark/Results 哈希一致性 | 通过 | `python scripts/verify_original_baseline.py` |
| 历史 Benchmark unittest | 54 项运行、53 通过、1 失败 | `python -m unittest discover -s benchmarks/tests -v` |

**那 1 项历史失败**：`test_published_aggregates_match_root_readme` 在 `benchmarks/verify_readme_results.py` 比较 `results/live/v1/manifest.json` 的 `root_readme_sha256` 与 README 时断言失败。**在未修改的原版包中独立复核，同样失败。** 这并非 V3 新 Agent 流程的测试失败，也不能隐瞒为全部通过。原版 `benchmarks/` 与 `results/` 已完整保留。


## 依赖齐全的环境中建议执行

```bash
python -m pip install -e ".[dev]"
python scripts/verify_original_baseline.py
python scripts/test_quality_gate_v2_offline.py
python scripts/test_parallel_only_v3_offline.py
python -m compileall -q deep_research backend scripts tests
ruff check deep_research backend tests scripts/check_repo_hygiene.py
APP_ENV=test pytest -q -m "not live"
```


`python -m unittest discover -s benchmarks/tests -v` 的历史哈希失败是已知问题，不应通过篡改结果或跳过该测试伪装全绿。CI 当前可能因该项失败而失败，合并主分支前应单独确定历史文档校验的修复策略并保留原始证据。
