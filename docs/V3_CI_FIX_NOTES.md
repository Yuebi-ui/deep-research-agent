# V3 CI 修复说明（2026-10-14）

这份记录对应 GitHub Actions 日志中 `19 failed, 657 passed, 107 skipped, 1 deselected` 的排查。**此记录不是完整 pytest 复测通过的证明**：当前修复环境无法从 PyPI 获取 LangGraph/LangChain 依赖，因此无法直接执行那套完整集成测试。

## 修复分组

- **API 终态取消**：`backend/routes/research.py` 的 `ApiError` 调用误插入第二个错误码，导致 `TypeError`。保留标准 `CANCEL_NOT_ALLOWED` 409 返回。
- **API 输入验证**：`backend/core/api_errors.py` 把 Pydantic 422 中 `ctx.error` 的 `ValueError` 转为文字，再返回 JSON，避免 `Object of type ValueError is not JSON serializable`。
- **Benchmark 指纹敏感信息**：`deep_research/benchmark/fingerprint.py` 把已有 `<REDACTED>` 等不同大小写形式规范为 `<redacted>`，不修改实验记录。
- **CI 测试隔离**：`.github/workflows/ci.yml` 不再对整个 pytest 会话强制设置 `LLM_PROVIDER=fake`、`SEARCH_PROVIDER=fake` 和 `CHECKPOINTER_BACKEND=sqlite`。`APP_ENV=test` 与 `ALLOW_LIVE_EXTERNAL_APIS=false` 仍保证默认走 Fake，不请求付费外部 API；持久化用例自行指定后端。
- **缺失运行配置**：CI 从**不含密钥**的 `config.hybrid.example.yml` 复制 `config.yml`（已被 `.gitignore` 忽略），让 context budget 与指纹测试有完整角色配置；项目压缩包不附带本地密钥配置。
- **Fake LLM 分工**：在 `deep_research/llm.py` 中仅让 Fake 模式下的 `draft` 智能路由模型按任务身份应答，使 Evaluator 测试的替身只用于真正的 Evaluator 阶段。生产环境仍按原策略选择小/大模型。
- **V3 图回归测试**：两个旧图测试的输入增加 `research_generation=1`，与 V3 强制并行的状态语义一致；离线无 HITL 路径的预期节点补上 `accept_research`。**没有移除 generation 校验。**

## 已知历史 Benchmark README 哈希问题

`results/live/v1/manifest.json` 保存的是历史版根 README 的 SHA-256。V3 改写了根 README 来介绍并行架构，因此 `benchmarks/tests/test_readme_results.py::ReadmeResultTests::test_published_aggregates_match_root_readme` 无法核对旧值。修复包**未修改** `benchmarks/`、`results/` 或历史校验断言。

CI 现在严格运行其余 53 项 Benchmark 协议测试，并将唯一的历史 README 哈希绑定校验单独执行、明确发出警告，避免把已知文档哈希差异混淆成新架构功能错误。**这不代表旧哈希检查通过**。离线结果发布校验 `python benchmarks/publish_offline.py --verify` 独立保持必过。

## 当前环境的实际验证

- `python -m compileall -q deep_research backend scripts tests`：通过。
- `python scripts/check_repo_hygiene.py`：通过。
- `python scripts/test_quality_gate_v2_offline.py`：13/13 通过。
- `python scripts/test_parallel_only_v3_offline.py`：7/7 通过。
- 20 条独立模块级诊断（验证错误处理、脱敏、环境隔离、预算来源、Fake 路由和 generation fence）：通过。
- `pytest -q benchmarks/tests --deselect=benchmarks/tests/test_readme_results.py::ReadmeResultTests::test_published_aggregates_match_root_readme`：53 通过、1 显式排除。
- `python benchmarks/publish_offline.py --verify`：5 个离线结果文件校验通过。
- **尚未验证**：CI 完整 `pytest -q -m "not live"`、完整 LangGraph 中断/恢复、Ruff（当前环境缺少相应依赖且无法下载安装）。

## GitHub 上的重新验证顺序

不要直接推送覆盖 `main`，先提交开发分支并等 GitHub Actions 报告结果：

```bash
python -m pip install -e '.[dev]'
cp config.hybrid.example.yml config.yml
ruff check deep_research backend tests scripts/check_repo_hygiene.py
python -m compileall -q deep_research backend scripts tests
pytest -q -m 'not live'
pytest -q benchmarks/tests --deselect=benchmarks/tests/test_readme_results.py::ReadmeResultTests::test_published_aggregates_match_root_readme
python benchmarks/publish_offline.py --verify
```

如仍有失败，应以 GitHub 新日志为准继续定位，不应把本文件中离线通过的结论扩大成完整端到端通过。
