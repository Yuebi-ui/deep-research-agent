# 评测结果说明（简版）

- [`live/v1/`](live/v1/README.md)：根 README 报告的历史真实 E2E、Chroma、质量审阅、Token 与故障恢复**汇总值**；原始运行日志随历史配套资料缺失，因此不能从当前归档独立重算。
- [`offline/v1/`](offline/v1/README.md)：仓库代码可复算的虚构语料本地排序与静态引用测试，**不是** Chroma 实测。
- [`examples/reference_v1/`](examples/reference_v1/README.md)：固定种子生成的合成示例，不能对外称为真实 Agent 结果。

```bash
python benchmarks/verify_readme_results.py
python benchmarks/fixtures/build_full_reference_results.py --verify
python benchmarks/publish_offline.py --verify
python -m unittest discover -s benchmarks/tests -v
```

注意：验证 README 一致性与验证真实历史实验的真实性是不同任务。详见 [`results/README.md`](README.md)。
