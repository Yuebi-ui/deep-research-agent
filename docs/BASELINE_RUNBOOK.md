# Baseline Runbook（Phase 1：测量，不优化）

本文档说明如何重复运行一次 **V1 Baseline**：固定 case、唯一 run_id、
机器可读 artifacts。适用于 V2 工程化改造的 Phase 1 / 后续任何对比测量。

> 原则：**Measure first. Optimize later.**
> Baseline 运行期间不得修改 prompt / 路由 / 上下文策略 / heartbeat 等业务行为；
> 已知问题（context overflow、claim loss）本身就是被测量的对象。

---

## 1. 前置条件（Prerequisites）

必须全部满足，runner **不会**替你启动这些服务：

| 依赖 | 检查方式 | 启动命令 |
|---|---|---|
| GPU（RTX 4090D） | `nvidia-smi -L` | — |
| 本地模型服务 vLLM :8001 | `curl 127.0.0.1:8001/v1/models` | `bash scripts/model-service/start_vllm.sh` |
| Redis :6379 | `redis-cli ping` | `bash scripts/autodl/start_redis.sh` |
| 数据库已初始化 | `data/tasks.db` 存在 | `bash scripts/autodl/init_db.sh` |
| `config.yml`（hybrid，含真实 Key） | 文件存在 | `cp config.hybrid.example.yml config.yml` 并填 Key |
| `.env.server`（APP_ENV / CHECKPOINTER_BACKEND 等） | 文件存在 | 见 `config.server.example.yml` 说明 |

启动 API 与 Worker（**唯一正式的 graph 执行者**）：

```bash
bash scripts/autodl/start_all.sh          # Redis → API → Worker
# 或分别：start_api.sh / start_worker.sh
```

就绪确认：

```bash
bash scripts/autodl/status.sh
curl -s http://127.0.0.1:8000/api/health
```

---

## 2. 运行 Baseline

```bash
.venv/bin/python scripts/run_baseline.py
```

行为：

1. 生成 `run_id`（形如 `v1baseline-20261005T231500-a1b2`），随 `POST /api/research/start`
   传给 API，由 API 写入 Redis（`dr:runid:{task_id}`）；
2. 自动处理 HITL：状态到 `waiting_review` 时提交 `approve`；
3. 轮询到终态（`completed` / `failed` / `cancelled` / `deleted`）；
4. 从 `data/baseline_metrics/<run_id>/` 收集 worker 侧原始指标；
5. 聚合输出到 `artifacts/baseline/<run_id>/`。

常用参数：

```bash
--run-id v1baseline-manual-01     # 自定义 run_id（便于对比）
--query "..."                     # 覆盖 case query（默认复用历史 E2E query）
--timeout 2700                    # 最长等待秒数
--force                           # artifacts 目录已存在时覆盖

# Phase 3C P1：experiment identity（必填语义，默认 adhoc/v1_baseline/baseline）
--experiment-id writer-thinking-001 --variant thinking-on --experiment-kind experiment

# Phase 3C P1：preflight 预期（与预期不符 → FAIL FAST，拒绝启动任务）
--expect-local-model qwen3-30b-a3b-local     # 默认取 config roles.researcher_main.handle
--expect-context-limit 8192                  # 默认 DR_BASELINE_LOCAL_CONTEXT_LIMIT 或 8192
--expect-claim-verify-thinking off           # 默认 off（E1a KEEP）
--expect-supervisor-thinking on              # 默认 on（E1b REJECT 后保持）
--expect-embedding-provider dashscope --expect-embedding-model text-embedding-v4 \
--expect-embedding-dimension 1024 --expect-embedding-schema-version 1
--allow-stale-services                       # 豁免「服务早于最新源码」检查（默认拒绝）
```

退出码：`0` = 任务 completed 且 run VALID；`2` = 任务非 completed；
`3` = 启动/轮询失败；`4` = **preflight 未通过**（未启动任务）；
`5` = run 期间代码/配置变化（`INVALID_*`，不得进入统计）。

### 2.1 Run fingerprint / preflight（Phase 3C P1）

每次 run 在启动任务**之前**：

1. 记录 experiment identity（`experiment_id` / `variant` / `kind`）与
   code revision（git commit + dirty + 源码树确定性指纹）、config fingerprint
   （角色路由 / thinking / context budget / embedding identity / feature flags，
   经 scrub，绝不含密钥）；
2. 执行 self-check：API / Redis / vLLM / 本地模型 / max context /
   worker thinking 环境（**读 worker 进程的实际环境**，不是 runner 的）/
   embedding identity / persisted memory schema / worker 存活 /
   服务新鲜度（进程启动必须晚于最新源码 mtime）。
   任何不一致 → 打印逐项明细、写
   `artifacts/benchmark_preflight/<run_id>.json`、**exit 4，不启动任务**。

run 结束后重取 revision + config fingerprint：任一变化 → run.json 的
`integrity.validity` = `INVALID_CODE_CHANGED` / `INVALID_CONFIG`（或二者组合），
该 run 不得进入统计。

---

## 3. 产物（Artifacts）

```text
artifacts/baseline/<run_id>/
  run.json                 # 聚合结果（机器可读；含 experiment / integrity）
  llm_calls.jsonl          # 每次 LLM 调用一行（成功与失败都记录）
  node_metrics.jsonl       # graph node 耗时
  search_metrics.jsonl     # 每次 search 调用一行
  reliability_events.jsonl # claim / recovery / run 生命周期事件
  BASELINE.md              # 人读报告

artifacts/benchmark_preflight/<run_id>.json   # self-check 明细（无论通过与否）
```

`run.json` 中与可复现性相关的键（Phase 3C P1）：

- `experiment`：`{experiment_id, variant, kind}`；
- `integrity`：`validity`（`VALID` / `INVALID_CODE_CHANGED` / `INVALID_CONFIG`）、
  `code_revision_start` / `code_revision_end`（commit、dirty、source_fingerprint）、
  `config_fingerprint_start` / `config_fingerprint_end` 与 `config_snapshot`
  （已 scrub，可直接审计）。

原始指标（worker 进程写入，run 完成后保留）：

```text
data/baseline_metrics/<run_id>/
```

---

## 4. 观测口径与已知限制

- **P0 ownership fencing（Phase 2）**：心跳判定失去 claim 所有权后，执行会在
  下一个事件边界中止（事件流显式 `aclose()` 停止后台图任务），且 stale owner
  不写终态、不 ACK/不释放；恢复走 XAUTOCLAIM / orphan reconciler，
  经 `None` 输入从 checkpoint 续跑（不重投 query）。相关事件：
  `claim_renewal_failed` → `execution_aborted` → `stale_execution_abandoned`。
- **Track A/B 开关（Phase 3）**：`DR_CLAIM_VERIFY_THINKING` 控制 claim
  extractor/judge 的云端 thinking（E1a KEEP 后**默认 off**，显式 `on` 恢复旧行为）；
  `DR_SUPERVISOR_THINKING` 控制 supervisor（E1b REJECT，保持 on）；
  `DR_WRITER_THINKING` 控制 **final writer**（E8 KEEP 后**默认 off**，显式 `on`
  恢复旧行为——作用域仅 `agent_builder.writer_model`，
  `refine_draft_report` 与 draft 的 writer-role 路由不受影响）；
  `DR_DRAFT_THINKING` 控制 **draft 报告生成**（E9 KEEP 后**默认 off**，显式 `on`
  恢复——作用域仅 `draft_agent.draft_model`，research_brief 的 auto 路由不受影响）。
  三者都只作用于具体调用点，不按 role/provider 全局关闭；每次云端调用的
  thinking/reasoning tokens 都记录在 `llm_calls.jsonl`。
  **accepted thinking policy（2026-10-06，E9 KEEP 落地后）**：extractor off /
  judge off / supervisor on / red_team on / **draft off** / final writer off——
  runtime、preflight、config fingerprint、测试与文档必须对该状态保持一致。
- **P1 context budget（Phase 2）**：`researcher_summarizer` 的 prompt 在发送前
  强制满足 `prompt + reserve_output + safety_margin <= context_limit`；
  limit 解析优先级：`cognition.<backend>.models.<handle>.context_window`
  → `DR_BASELINE_LOCAL_CONTEXT_LIMIT`（仅本地）→ vLLM `/v1/models` 探测；
  云端 role 未配置 limit 时不裁剪。tokenizer 优先加载本地模型目录的
  `tokenizer.json`（可用 `DR_TOKENIZER_PATH` 指定），不可用时用保守启发式并记录。
  决策明细写入 `budget_events.jsonl`。
- **`DR_STREAM_USAGE=1`（观测开关，默认关闭）**：worker 用 `astream_events`
  驱动图时，LangChain 会把子模型调用统一转成 streaming；对自定义 base_url，
  langchain-openai 默认不请求 usage，导致 token 不可得。打开该开关后请求会附带
  `stream_options={"include_usage": true}` —— **只追加 usage 上报，不改变生成内容 /
  采样参数**。两个端点（vLLM / DashScope）均已验证兼容。Baseline 环境在
  `.env.server` 中开启。
- **token 数**：来自 provider 返回的 usage（精确）。失败调用拿不到 usage，
  `input_tokens` 记 null；context overflow 的调用会附带
  `input_tokens_lower_bound`（错误消息中的 "at least N"，是下限不是精确值）。
- **context limit（本地）**：优先 `DR_BASELINE_LOCAL_CONTEXT_LIMIT` 环境变量覆盖；
  否则探测 vLLM `/v1/models` 的 `max_model_len`；两者都拿不到则 null。
- **context limit（云端）**：DashScope 不在响应中返回，恒为 null，
  `context_utilization` 相应为 null（不伪造）。
- **context_utilization** = `input_tokens / context_limit`，仅当两者都已知时计算。
- **estimated_cost**：按 `deep_research/callbacks/cost_tracker.py` 的价格表估算（RMB），
  非账单精确值；价格表缺失的 model 不参与估算。
- **claim loss**：记录心跳续约失败事件（`claim_renewal_failed`）。注意当前实现下
  该事件**不会停止执行**（`OwnershipLost` 未接线，属已知缺陷，Phase 1 只测量不修复）。
- **recovery**：`recovery_requeued`（孤儿回收重新入队）+ `job_reclaimed_stale`
  （XAUTOCLAIM 接管）两类事件之和。
- 指标写入是**旁路**行为：任何写入失败都不会影响任务执行；失败只体现在 artifacts
  缺失或 data_quality 说明中。

## 5. 离线测试

```bash
.venv/bin/python -m pytest tests/test_baseline_metrics.py -q -m "not live"
.venv/bin/python -m pytest -q -m "not live"     # 全量回归
```

测试不访问真实 LLM / Search，不产生费用（conftest 的 socket 守卫兜底）。
