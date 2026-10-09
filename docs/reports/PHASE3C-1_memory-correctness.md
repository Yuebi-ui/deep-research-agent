# PHASE 3C-1 REPORT — Memory Correctness Remediation + Benchmark Hardening

> 生成：2026-10-06 · 仓库 `/root/autodl-tmp/deep-research-server-v1`
> 起点 commit `eca2c77`（Phase 3B accepted）→ 终点 `1d19285`
> 结论：**PASS**（P0 修复 + P1 基建均落地；未执行任何性能实验）

---

## 1. Executive verdict

**PASS**

- P0：Structured Memory embedding-space correctness bug 已实证确认（持久化数据 384 vs 1024）、修复、迁移真实数据并用真实 DashScope 验证语义检索正确。
- P1：run fingerprint / config fingerprint / preflight fail-fast / experiment identity / freeze 检测全部落地。
- 1 次受控 E2E smoke：completed、overflow 0、claim loss 0、LLM failed 0、VALID。
- 8 条 STOP CONDITIONS 均未触发。

## 2. Repository audit（对照历史）

- 工作树 clean @ `eca2c77`；E3 batch upsert ✔、E1a KEEP ✔、E1b 已回滚 ✔。
- 9 次 Phase 3B E2E artifacts 全部干净（唯一异常是 Phase 1 基线本身）。
- 测试口径澄清：历史"508 passed"= 当时 Redis 未启动下的 **collected 总数**（413 passed / 95 skipped）。

## 3. Embedding root cause

| | provider/model | dim | 证据 |
|---|---|---|---|
| structured 写入（旧） | Chroma 默认 ONNX all-MiniLM-L6-v2 | **384** | `chroma.sqlite3: memory_entities/claims dimension=384` |
| structured 查询（旧） | DashScope text-embedding-v4 | **1024** | 复现 `InvalidArgumentError: expecting 384, got 1024` |
| research_memory | DashScope text-embedding-v4 | 1024 | 写/查一致（该路径未爆炸） |

- 写路径 `structured_store.upsert_*(documents=…)` 不传 embeddings → Chroma 默认 EF 隐式编码；查询路径显式 DashScope。两条路径各自演化、无 schema 校验。
- 真实数据影响：memory_entities 224 条 / memory_claims 170 条处于错误空间。
- structured 检索**全仓库无生产调用方**（latent），但 structured **写入**每次 E2E 都发生。
- 关键实测约束：**chromadb 1.5.9 中 `embedding_function=None` 不会禁用默认 EF**（仍静默 384 维）→ 防线必须是代码级强制。

## 4. Chosen unified architecture（模式 A + identity marker）

- 统一到 DashScope `text-embedding-v4`（1024 维，与当时的项目配置记录一致）；离线用确定性 fake（provider=fake，与 live 空间互不兼容）。
- `deep_research/memory/embeddings.py`：**唯一** embedding 入口 `EmbeddingClient` + 不可变 `EmbeddingIdentity`；实测 text-embedding-v4 单请求 ≤10 条 → client 内自动分批（`MAX_BATCH_SIZE=10`）。
- `deep_research/memory/schema_guard.py`：collection metadata 写 identity marker，打开时校验；legacy（无 marker）或 mismatch → **显式抛错**；`ManagedCollection` 是唯一写/查入口（永远显式传 embeddings）。
- 四个 structured collection 统一 cosine 空间；`manager` 两个 store 共用单一 `EmbeddingClient`；`/api/health` 暴露 `memory_schema` 状态。

## 5. Historical collection migration

- 策略 **显式重编码**（保留 ids/documents/metadatas，只重算向量）：`deep_research/memory/migration.py` + `scripts/migrate_memory_schema.py`（dry-run 默认、`--apply` 才执行、fake 模式拒绝 apply、执行前整目录备份、报告落 `artifacts/memory_migration/`）。
- 实际执行：先在副本验证 → 对 `data/chroma` live 迁移 5/5 collection（17/224/170/0/0 条全部保真）→ 备份二重（`data/chroma.backup-phase3c-pre` + 脚本备份）。
- 一次失败（DashScope 400：batch>10）当场从备份恢复、修复分批后重跑成功，无数据丢失。

## 6. Files changed（20 files，+3019 / -261）

新增：`memory/embeddings.py`、`memory/schema_guard.py`、`memory/migration.py`、`scripts/migrate_memory_schema.py`、`deep_research/benchmark/{__init__,fingerprint,preflight}.py`、4 个测试文件。
修改：`memory/{vector_store,structured_store,manager}.py`、`backend/main.py`（health）、`scripts/run_baseline.py`、`docs/BASELINE_RUNBOOK.md` 等项目记录、`.gitignore`。

## 7. Tests added（40）

- `test_memory_embedding_space.py`（11）：identity/维度/确定性语义检索/live DashScope（opt-in）/持久化重开/legacy 与 mismatch 拒绝/跨空间拒绝/E3 批量回归/client 分批保序。
- `test_memory_migration.py`（5）：plan/保真迁移/幂等/dry-run 零副作用/CLI 安全阀。
- `test_benchmark_fingerprint.py`（11）+ `test_benchmark_preflight.py`（13）：确定性、密钥不入 artifact、token 计数不误判、config/源码变更传播、freeze 判定、identity 落盘、preflight 各失败路径。

## 8. Test commands / results

```
.venv/bin/python -m pytest -q                       # 554 passed, 1 skipped, 0 failed（Redis 运行时）
.venv/bin/python -m pytest -q                       # 457 passed, 96 skipped, 0 failed（Redis 缺席口径）
DR_LIVE_EMBEDDING_TEST=1 ALLOW_LIVE_EXTERNAL_APIS=true \
  .venv/bin/python -m pytest tests/test_memory_embedding_space.py -m live -q   # 1 passed
```

## 9. Retrieval / persistence / mismatch 结果

- Live 语义检索：query `"How does worker claim heartbeat work?"`，固定 corpus D1(`Redis heartbeat lease ownership`)/D2/D3 → **top-1 = D1** ✔（修复前为 InvalidArgumentError）。
- 真实数据抽查：`search_claims("multi-agent orchestration with LangGraph")` 返回 3 条高相关 claim。
- 持久化重开：write → 新实例 → 检索正确 ✔。
- legacy/mismatch：`LegacyMemoryCollectionError` / `EmbeddingSpaceMismatchError`，错误信息指向迁移脚本；`inspect_collection` 报 compatible=False——**不存在静默兼容路径**。

## 10. Benchmark hardening

- `fingerprint.py`：code revision（git commit/dirty + 源码树路径相对 sha256）；config snapshot→sha256（role 路由/thinking/context budget/embedding identity/feature flags），`scrub_secrets` 保证无密钥且**不误伤** `max_tokens` 类计数键；`ExperimentIdentity`；`evaluate_run_validity`。
- `preflight.py`：10 项检查，失败 exit 4 且不启动任务；thinking 读 **worker 进程环境**（防"忘了重启"）；worker observation 只留 env 键名、报告整体 scrub；**service_freshness**（进程启动必须晚于最新源码 mtime）。
- `run_baseline.py`：`--experiment-id/--variant/--experiment-kind`、`--expect-thinking KEY=on|off`；run 后重取指纹 → `run.json.integrity.validity`；exit 0/2/3/4/5。
- new: feature flags 取 worker 进程环境（runner shell 未加载 `.env.server`）。

## 11. Smoke（phase3c-smoke-001）

completed / attempts=2（HITL 正常模式）/ 362.4s / LLM 63/63 成功 / local 44 调用 overflow 0 / cloud cost 0.19 RMB / claim_losses 0 / stale 0 / **VALID**。
memory 写入：entities 224→239、claims 170→180、research_memory 17→18（15 条 entity 经 10+5 分批走通 DashScope）。E2E 只验证了 `research_memory` 检索；structured 检索由 live 测试证明（不冒充）。

## 12. Git commits

```
1d19285 docs: Phase 3C-2 E8/E9 experiment design (draft, not executed)
a958c65 feat(benchmark): fingerprint feature flags from the worker process env
bd95675 test(benchmark): fingerprint determinism, secret safety and preflight gates
56f673a feat(benchmark): reproducible run fingerprints + fail-fast preflight
4156450 test(memory): embedding-space, persistence and migration regressions
0cbb999 feat(memory): explicit migration tooling for legacy embedding schema
36c7cb1 fix(memory): unify structured-memory embedding space
```

## 13. Remaining risks（延续）

1. structured 检索仍无生产调用方（修复由测试保证；接入消费方时需重新评审）。
2. 迁移是手动的（刻意）；未迁移环境会 fail-loud。
3. smoke 运行于 `bd95675`（其后 a958c65 增加 worker-env flags 采集，未再跑 E2E）。
4. `service_freshness` 基于 mtime，`git checkout` 可能误报；有 `--allow-stale-services` 豁免。
5. 既有 `test_runtime_sse.py` warning 未处理（非本轮引入）。

## 14. 产物位置

- A/B 无关；smoke artifacts：`artifacts/baseline/phase3c-smoke-001/`、preflight 报告 `artifacts/benchmark_preflight/`、迁移报告 `artifacts/memory_migration/`（均不入库）。
