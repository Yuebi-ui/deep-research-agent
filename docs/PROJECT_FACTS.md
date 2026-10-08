# Project Facts

Evidence-backed facts about this project, suitable for résumé bullets, demo
scripts, and interview answers. Every item here is traceable to code, to the
offline test suite, or to the recorded E2E run
([docs/E2E_EVIDENCE.md](E2E_EVIDENCE.md)).

If a claim is not on this page, do not make it.

---

## Hardware / Serving

- Served a 30B-class MoE model (Qwen3-30B-A3B) on a **single 24 GB RTX 4090 D**.
- Used **GPTQ INT4** quantization to fit the weight footprint on one GPU.
- Weights execute through the **GPTQ-Marlin kernel** (selected automatically by
  vLLM at load time).
- Measured weight load: 15.7 GiB, 25.69 s. Cold start to serving: ~1 min 51 s.
- KV cache after load: 3.57 GiB → 38,992 tokens → 4.76x max concurrency at 8192
  context.

## Serving Stack

- **vLLM 0.19.1**, exposed as an **independent OpenAI-compatible HTTP service**
  on `127.0.0.1:8001`.
- The application **never loads model weights in-process** — agent code holds
  only an HTTP client.
- Torch 2.10.0+cu128, CUDA 12.8, driver 570.124.04.
- Tool calling verified end to end with the **hermes** parser (`finish_reason:
  tool_calls`, arguments parsed correctly).

## Hybrid Routing

- **Hybrid local/cloud routing** driven entirely by config: role → backend +
  handle. Switching a role between local and cloud requires no agent code change.
- Local: `researcher_main`, `researcher_compressor`, `researcher_summarizer`,
  `context_pruner` → local Qwen.
- Cloud: `supervisor`, `writer`, `draft` → `deepseek-v4-pro`; `evaluator`,
  `red_team` → `deepseek-v4-flash` (DashScope, OpenAI-compatible).
- Embedding: DashScope `text-embedding-v4` (1024 dims).
- **Thinking policy (accepted state, 2026-10-06)**: extractor `off`, judge `off`
  (E1a KEEP), supervisor `on` (E1b REJECT), red_team `on`, draft `off`
  (E9 KEEP, `DR_DRAFT_THINKING=on` restores), final writer `off`
  (E8 KEEP, `DR_WRITER_THINKING=on` restores). Switches are
  per-call-site, never per role/provider; each call's
  thinking/reasoning tokens are recorded in `llm_calls.jsonl`, and config
  fingerprints capture the switch state.
- **Single embedding space (Phase 3C P0):** every memory collection stores its
  embedding identity as collection metadata (`dr_embedding_*` markers:
  provider/model/dimension/schema_version). Writes and queries both go through
  the one `deep_research.memory.embeddings.EmbeddingClient`; Chroma's default
  EF is never used. Legacy collections without markers (or with mismatched
  markers) are rejected at open time — migrate explicitly with
  `scripts/migrate_memory_schema.py` (re-embeds stored documents in place,
  preserving ids/documents/metadatas).
- Full-cloud rollback is a four-line config edit.

## Application

- **LangGraph** multi-agent orchestration: Supervisor, Research, Draft,
  Evaluator, Red Team; rework loop with stall detection.
- **FastAPI** API layer with SSE progress streaming and an observability surface
  (trace, cost, stats, alerts).
- **Redis-backed async worker** that is the sole production graph executor; the
  API never builds the graph.
- **Persistence:** Redis checkpointer + SQLite task store + ChromaDB vector
  memory + structured memory (Entities/Claims/Evidence/Contradictions) + JSONL
  traces.
- **HITL review/resume** via LangGraph `interrupt()`, with review decisions
  persisted in SQLite so an API restart cannot strand a task.
- **Parallel fact verification** producing a quantifiable hallucination rate.
- **Tavily** search behind a provider registry.

## Reliability

- Claim/lease-based job execution over a Redis Stream consumer group.
- Heartbeat renewal, orphan reconciliation, bounded retry, graceful shutdown
  that deliberately withholds claim release to prevent duplicate node execution.
- **Orphan recovery was exercised for real**, not just unit-tested: during the
  recorded E2E the worker lost claim ownership and the task was reclaimed and
  completed on attempt 2.

## Testing

- **554 tests, 0 failures**（Redis 运行时可全量执行；Redis 缺席时 95 个依赖
  用例自动 skip；live-API 用例需显式 opt-in。测试网络与真实数据库均有守卫兜底）。
- Deterministic substitutes: FakeChatModel, FakeSearchProvider, fake_embedding.
- A socket-level network guard blocks any non-loopback connection during tests,
  so an accidental paid-API call fails loudly instead of costing money.
- Zero external API dependency: the suite runs with no keys, no GPU, and no
  running Redis.
- Verified zero local-model calls during offline regression (vLLM access-log
  line count unchanged across the run).

## Verified E2E

- One real hybrid task completed end to end:
  `Local Qwen + DashScope Pro/Flash + Tavily + Worker + LangGraph → final report`.
- Result: **completed**, 12,735-character Markdown report with 6 citations.
- Local model was genuinely invoked (30 successful HTTP 200 inference requests
  recorded by vLLM, on top of 14 rejected requests).
- Cloud models were genuinely invoked (`deepseek-v4-pro`, `deepseek-v4-flash`).
- Four real Tavily searches were made.

---

## Claims We Must NOT Make

These are unsupported by evidence. Do not put them on a résumé, in a demo, or in
an interview answer.

| Claim | Why not |
|---|---|
| "Production-ready" | One E2E run; known unresolved heartbeat issue; 3 documented limitations |
| "High availability" | Single worker, single GPU, single Redis; no failover testing |
| "Optimal routing" | Role mapping is a first-version, non-benchmarked split |
| "Improved quality by X%" | No baseline comparison was ever run |
| "Reduced cost by X%" | No cost measurement or comparison was captured |
| "Reduced latency by X%" | No latency measurement or comparison was captured |
| "Stable under high concurrency" | Only one task was ever run at a time; KV headroom is 4.76x |
| "All local model requests succeeded" | 14 of 44 requests were rejected with HTTP 400 |
| "8192 context is sufficient" | It demonstrably is not for webpage summarization |
| "Redis heartbeat issue root cause identified" | It is explicitly unresolved |
| "GPTQ INT4 has no quality loss" | Never measured against full precision |
| "Benchmarked performance" | No benchmark was run |
| "Multi-user / multi-tenant" | No such isolation exists |
| "Security-hardened" | Only secret-hygiene checks were done |

### Safe phrasings

- "Built and validated a hybrid local/cloud multi-agent research system,
  running a 30B-class MoE model quantized to INT4 on a single 24 GB GPU."
- "Ran a real end-to-end hybrid task: local Qwen for research-path roles, cloud
  DeepSeek for planning/writing, Tavily for search."
- "457 offline tests with deterministic fakes and a network guard that blocks
  paid API calls during CI."
- "Documented three real limitations found in E2E, including an unresolved
  worker heartbeat timeout."

---

## Numbers Cheat Sheet

| Metric | Value | Source |
|---|---|---|
| Offline tests | 442 passed / 0 failed | `pytest -q -m "not live"` |
| GPU | RTX 4090 D, 24 GB class | `nvidia-smi` |
| Model weights on GPU | 15.7 GiB | vLLM log |
| KV cache | 3.57 GiB / 38,992 tokens | vLLM log |
| Cold start | ~1 min 51 s | vLLM log |
| E2E duration | ~7 min 30 s | task lifecycle |
| E2E report | 12,735 chars, 6 citations | task record |
| Local requests during E2E | 44 total (30×200, 14×400) | vLLM access log |
| Tavily searches during E2E | 4 | worker log |
| External API calls during offline regression | 0 | network guard + logs |
| Local model calls during offline regression | 0 | vLLM log line count |
