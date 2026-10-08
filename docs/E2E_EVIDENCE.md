# Hybrid E2E Evidence

**Status: PASS**

This document records a real, single-shot Hybrid E2E run of the platform:
Local Qwen (vLLM) + Cloud DeepSeek (DashScope) + Tavily Search, executed through
the production API → Redis → Worker → LangGraph path.

All figures below are taken from existing logs, the task record, and the running
services. No benchmark was run, and no API calls were made to produce this
document.

**No API keys or credentials appear in this file.**

---

## 1. Environment

| Item | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 4090 D |
| Driver | 570.124.04 |
| CUDA | 12.8 |
| Total VRAM | 24564 MiB |
| vLLM | 0.19.1 |
| Torch | 2.10.0+cu128 |
| Python | 3.12.3 |
| Local model | Qwen/Qwen3-30B-A3B-GPTQ-Int4 |
| Local model path | `/root/autodl-tmp/models/Qwen3-30B-A3B-GPTQ-Int4` |
| Served model | `qwen3-30b-a3b-local` |
| Local endpoint | `http://127.0.0.1:8001/v1` |
| `max_model_len` | 8192 |
| `gpu_memory_utilization` | 0.85 |
| Tool call parser | hermes (`--enable-auto-tool-choice --tool-call-parser hermes`) |

---

## 2. Local Model Validation

```
GPU_DETECTED      = YES
VLLM_STARTED      = YES
V1_MODELS         = PASS
LOCAL_INFERENCE   = PASS
LOCAL_MODEL_READY = YES
```

Startup facts from the vLLM server log:

| Item | Value |
|---|---|
| Resolved architecture | `Qwen3MoeForCausalLM` |
| Quantization | GPTQ → GPTQ-Marlin kernel |
| Model loading memory | 15.7 GiB |
| Weight load time | 25.69 s |
| Attention backend | FLASH_ATTN (FlashAttention v2) |
| Available KV cache | 3.57 GiB |
| KV cache size | 38,992 tokens |
| Max concurrency @ 8192 ctx | 4.76x |
| Engine init (profile + KV + warmup) | 53.95 s |
| Cold start to serving | ~1 min 51 s |

Behavioral checks:

| Check | Result |
|---|---|
| Minimal inference probe | PASS |
| Tool calling (`tool_calls` returned, arguments parsed) | PASS |
| Hermes parser | PASS |
| `chat_template_kwargs.enable_thinking=false` | PASS |

Notes:

* The models on both sides are **thinking models**. Without
  `enable_thinking=false` the response begins with `<think>` and reasoning tokens
  are consumed first. This flag is a per-request parameter and cannot be disabled
  globally at server start; the application passes it via `extra_body`.
* `gpu_memory_utilization=0.85` is nominal. Measured steady-state usage was
  22,183 MiB (~90% of total) because CUDA graph memory is not counted against
  that budget in vLLM 0.19. This is why KV cache headroom is modest.

---

## 3. Hybrid Architecture

Model routing for this run (unchanged since configuration):

### Cloud (DashScope, OpenAI-compatible)

| Role | Model |
|---|---|
| `supervisor` | `deepseek-v4-pro` |
| `writer` | `deepseek-v4-pro` |
| `draft` | existing auto-routing behavior (`get_chat_model_auto()`) |
| `evaluator` | `deepseek-v4-flash` |
| `red_team` | `deepseek-v4-flash` |

### Local (vLLM)

| Role | Model |
|---|---|
| `researcher_main` | `qwen3-30b-a3b-local` |
| `researcher_compressor` | `qwen3-30b-a3b-local` |
| `researcher_summarizer` | `qwen3-30b-a3b-local` |
| `context_pruner` | `qwen3-30b-a3b-local` |

### Other backends

| Concern | Backend |
|---|---|
| Embedding | DashScope `text-embedding-v4` (1024 dims) |
| Search | Tavily |
| Checkpointer | Redis |
| Task store | SQLite |

---

## 4. Real E2E Task

| Item | Value |
|---|---|
| task id | `874da134c127` |
| query | What is LangGraph and what are its main use cases? |
| final status | `completed` |
| attempt | 2 |
| duration | approximately 7 minutes 30 seconds (02:44:38 → 02:52:08) |

Lifecycle observed in logs:

- API accepted the task (`POST /api/research/start` → 200)
- Worker consumed the task from the Redis Stream job queue
- LangGraph executed
- HITL `waiting_review` occurred at stage `human_review` (02:46:09)
- Human approval issued via `POST /api/research/{id}/resume` (`action=approve`)
- Execution resumed and continued
- Task completed
- Final Markdown report generated

---

## 5. Local Model Evidence

The worker's provider-selection log shows the local backend was chosen for the
local roles:

```
backend 'openai_local' for role 'researcher_main'       with handle 'qwen3-30b-a3b-local'
backend 'openai_local' for role 'researcher_compressor' with handle 'qwen3-30b-a3b-local'
backend 'openai_local' for role 'researcher_summarizer' with handle 'qwen3-30b-a3b-local'
```

The vLLM access log recorded the following during the E2E window:

| Result | Count |
|---|---|
| `POST /v1/chat/completions` → HTTP 200 | 30 |
| `POST /v1/chat/completions` → HTTP 400 | 14 |
| **Total local requests** | **44** |

**These are 44 total requests, not 44 successful calls.** The 14 non-200
responses were rejected because the prompt exceeded the 8192-token context limit
(see §11). The 30 HTTP 200 responses are genuine successful local inference
calls. The vLLM instance serves exactly one model, `qwen3-30b-a3b-local`.

---

## 6. Cloud Model Evidence

The worker's provider-selection log shows the cloud backend was chosen:

```
backend 'openai' for role 'supervisor' with handle 'deepseek-v4-pro'
backend 'openai' for role 'writer'     with handle 'deepseek-v4-pro'
backend 'openai' for role 'draft'      with handle 'deepseek-v4-pro'
backend 'openai' for role 'evaluator'  with handle 'deepseek-v4-flash'
backend 'openai' for role 'red_team'   with handle 'deepseek-v4-flash'
```

Actual model handles observed: **`deepseek-v4-pro`** and **`deepseek-v4-flash`**.

Only the handles are recorded. No token counts, cost figures, or latency numbers
are asserted here, because no reliable per-role aggregation was captured for this
run.

---

## 7. Search Evidence

Four real Tavily search calls occurred during the E2E:

```
02:46:50  tavily_search  {'query': 'LangGraph community ecosystem and competitive landscape as of 2026'}
02:46:50  tavily_search  {'query': 'LangGraph latest version, release notes, and updates as of October 2026, ...'}
02:47:02  tavily_search  {'query': 'LangGraph v0.1 release notes, LangGraph Cloud beta features, pricing, ...'}
02:47:04  tavily_search  {'query': 'Elastic Security case study using LangGraph'}
```

Two additional `tavily_search` lines with `{'query': 'fake query'}` exist in the
worker log but belong to an earlier Fake-provider phase and are **not** counted.

---

## 8. Final Report

| Item | Value |
|---|---|
| generated | YES |
| length | 12,735 characters |
| format | Markdown |
| citations | 6 URL references |

The report is a structured Markdown document covering introduction, core
concepts, architecture, use cases, ecosystem/version landscape, competitor
comparison, risks, and conclusion, with a numbered reference list.

Summary (not reproduced in full): the report argues that traditional Chain
abstractions are DAGs and cannot cleanly express cycles, conditional jumps, and
cross-step persistent state, and that LangGraph addresses this with
node/edge abstractions plus built-in state management, persistent checkpoints,
and human-in-the-loop support.

---

## 9. Offline Regression

Run after the E2E, with `APP_ENV=test`:

```
passed:              442
failed:              0
skipped:             0
external API calls:  0
localhost:8001 calls: 0
```

| Fake component | Result |
|---|---|
| FakeChatModel | PASS |
| FakeSearchProvider | PASS |
| fake_embedding | PASS |

Verification method for the zero-call claims: the external-network guard
(`tests/conftest.py`) raised no `RuntimeError`, and the vLLM access log line
count was identical before and after the test run (1574 → 1574), proving the
suite never contacted the local model service. The vLLM service was left running
throughout.

---

## 10. Recovery Evidence

This section records a real recovery event. It is reported as observed, and the
root cause is explicitly **not** claimed.

**Observed:**

- A Redis heartbeat renewal timeout occurred during the E2E:
  `心跳续约异常，停止续约: Timeout connecting to server`
- Claim ownership was lost:
  `claim 续约失败，已失去 874da134c127 的所有权 —— 停止执行以免双跑`
- The worker stopped the in-flight execution for that task to avoid double-running
- The orphan recovery path subsequently reclaimed the task
- The task was re-queued as **attempt 2**
- The task completed successfully on attempt 2

**Unknown:**

- The exact root cause of the heartbeat timeout.

The timeout occurred immediately after the claim-verification phase, which runs
10 claims in parallel alongside a burst of local inference. A plausible
hypothesis is event-loop starvation delaying the heartbeat thread, but **this is
unverified** and must not be stated as fact.

**Consequence to be aware of:** the task effectively executed twice, so cloud
token consumption for this E2E was higher than a single clean run would imply.

---

## 11. Known Limitations

These are real, observed issues. None of them was fixed during this phase.

### Local context limit

14 local requests returned HTTP 400 because the prompt exceeded the 8192-token
maximum context length:

```
This model's maximum context length is 8192 tokens. However, you requested 0
output tokens and your prompt contains at least 8193 input tokens ...
```

They were observed primarily during webpage summarization
(`deep_research.tools.tool: Failed to summarize webpage`). The graph caught the
errors and continued, so the run completed — but the source summarization
evidence was lost, and claim verification in that run reported only 3 supported
+ 3 partial out of 10 claims (60% verified, hallucination_rate 40%).

**Research quality for this run is therefore not fully reliable.** The 8192
context window is not sufficient for the webpage-summarization step on
content-rich sources.

### Worker heartbeat / claim ownership

A Redis heartbeat renewal timeout occurred under real E2E load. Task ownership
was lost, the worker stopped execution, and orphan recovery resumed the task as
attempt 2. Recovery worked as designed, but the root cause remains unresolved.

### Chroma default embedding download

A Chroma code path downloaded `all-MiniLM-L6-v2` (~83 MB) into the system cache
at `/root/.cache/chroma` at 02:51, despite the application configuring an
external embedding model (`text-embedding-v4`) and passing embeddings
explicitly. This indicates an embedding configuration/path inconsistency.

Two secondary concerns: the download is unexpected network activity, and it
landed on the system disk rather than the data disk.

**Not fixed in this phase.**

---

## 12. Final Verdict

```
LOCAL_MODEL_READY    = YES
HYBRID_E2E           = PASS
OFFLINE_REGRESSION   = PASS
```

`HYBRID_E2E = PASS` means the complete hybrid execution path successfully
produced a final report: API → Redis → Worker → LangGraph → Local Qwen →
DashScope → Tavily → Final Report.

It does **not** mean that all individual model calls succeeded, nor that
production reliability or performance has been established.

---

## Evidence Sources

| Claim | Source |
|---|---|
| GPU / driver / CUDA | `nvidia-smi` |
| vLLM startup facts | `/root/autodl-tmp/logs/vllm/serve.log` |
| Role → backend/model selection | `logs/worker.log` |
| Local request counts | vLLM access log |
| Search calls | `logs/worker.log` |
| Task lifecycle, status, attempt | `GET /api/research/874da134c127/status`, `logs/worker.log` |
| Final report length | `GET /api/research/874da134c127/report` |
| Offline regression | `pytest -q -m "not live"` with `APP_ENV=test` |
