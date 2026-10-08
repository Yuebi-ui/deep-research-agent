# PHASE 4A — GPU/vLLM BASELINE PROFILING REPORT

> 生成：2026-10-06 · 起点 `2ba326a` · E9 landing `053cb18` · **3 次 E2E 的冻结 revision `e8e4d1c`** · 测量脚本补充 `a12d28d`
> 结论：本地阶段 = **KV 约束的 100% 饱和 burst**；全局 = **云端串行链 + 本地 25% 占空比**；未改任何参数

---

## 1. Executive summary

- E9 已落地（draft thinking 默认 OFF），accepted policy 与 runtime/preflight/fingerprint/tests/docs 一致。
- **GPU 忙碌是二元的**：本地推理活跃期 99.8–100%，其余空闲；全 run 中 >90% 占 77/77/64 采样、**10–90% 占 0**、<10% 占其余。GPU>50% 时长 65–77s（≈22–25% wall）。
- **全部 queue/KV 压力 100% 集中在 `claim_verification` burst**：KV 峰值 0.986–0.996、waiting 峰值 4–5、preemption 每 run 1–2 次、该节点并行重叠 5.54–6.21x、峰值并发稳定 10。
- **prefill 不是瓶颈、decode 主导**：prefill p50 0.51s（6.8k prompt）、decode p50 5.0s；空闲单请求 1.5–1.7s 且与 prompt 大小几乎无关。
- 全局瓶颈在**云端调用与编排**（约 75% wall GPU 无活）。

## 2. 环境与 vLLM 配置（未修改）

| 项 | 值 |
|---|---|
| GPU / driver | RTX 4090D 24,564 MiB / 580.105.08 |
| vLLM | 0.19.1（V1 engine），Qwen3-30B-A3B-GPTQ-Int4（gptq_marlin，fp16，FLASH_ATTN v2） |
| 启动命令 | `vllm serve … --max-model-len 8192 --gpu-memory-utilization 0.85 --enable-auto-tool-choice --tool-call-parser hermes` |
| max_num_seqs | **256**（0.19.1 对 <70GiB GPU 的 OPENAI_API_SERVER 默认） |
| max_num_batched_tokens | **2048**（同上；运行观测一致：iteration 均值 30.6 tok/step） |
| prefix caching / chunked prefill | **True / True**（V1 默认） |
| KV cache | 2441 blocks × 16 = **39,056 tokens**（/metrics 实测） |

## 3. Metrics 可用性

可用：running/waiting、kv_cache_usage_perc、prompt/generation tokens、preemptions、prefix cache hits/queries + cached/recomputed tokens、**TTFT / queue / prefill / decode / e2e / per-output-token 直方图**、iteration_tokens_total。
NOT EXPOSED：swapped 请求、per-request 级 queue/TTFT。

## 4. Profiler（只读）

`deep_research/profiling/{scrape,gpu,sampler,analysis}.py` + `scripts/experiments/phase4a_analysis.py`：1s 采样（GPU via nvidia-smi、vLLM via /metrics），逐行 flush、故障隔离；分析给出并发时间线、node 窗口、**overlap factor**、峰值归因、计数器 delta。3 run 实测 321/341/301 行、**0 错误**。测试 15 用例；full suite **611 passed / 0 failed**。

## 5. 三次 E2E 原始数据

| run | E2E | validity | local calls | local in-tok | GPU>50% | KV 峰值 | waiting 峰值 | preemptions |
|---|---|---|---|---|---|---|---|---|
| phase4a-p1 | 307.8s | VALID | 44 | 226,247 | 77s | 0.996 | 5 | 1 |
| phase4a-p2 | 328.2s | VALID | 44 | 219,503 | 77s | 0.986 | 4 | 2 |
| phase4a-p3 | 285.8s | VALID | 39 | 204,038 | 65s | 0.991 | 5 | 1 |

Correctness gates 3/3 全过（overflow=0、claim loss=0、stale=0、LLM failed=0、HITL 正常、memory schema ok、VALID）。

## 6. Node wall profile（p1，窗口和≈E2E）

| node | wall | 本地调用 | summed | overlap | 峰值并发 |
|---|---|---|---|---|---|
| final_report_generation | **109.1s** | 0（cloud） | — | — | — |
| write_draft_report | 56.8s | 0（cloud） | — | — | — |
| tool_node | 54.8s | 15 | 44.7s | ~0.8 | 2 |
| supervisor_tools | 53.3s | 24 | 83.6s | 1.57 | 2 |
| **claim_verification** | **40.7s** | **20** | **234.4s** | **5.76** | **10** |
| supervisor | 31.8s | 0（cloud） | — | — | — |
| llm_call | 22.4s | 11 | 34.1s | 1.53 | 2 |
| compress_research | 21.1s | 3 | 29.7s | 1.41 | 2 |

Local roles：summarizer 32 calls/183.6k in/274.7s summed；researcher_main 10/31.4k/22.1s；compressor 2/11.3k/21.1s。
**口径纪律**：全局 summed local 318s vs E2E wall 305s（1.04x）——**不得**表述为"summarizer 让 E2E 慢了 275s"；其关键路径贡献以 burst wall（cv 40.7s）计。

## 7. Claim verification 逐秒（p1）

T+0–6 claim 抽取（cloud，GPU 空）→ T+8 burst 开始（GPU 100%、running 5、waiting 4、KV 0.87、并发 9）→ T+12–28 峰值（并发 10、waiting 2–5、KV 0.84–0.94）→ T+32 收尾（KV 0.51）→ T+36–40 judges（cloud，GPU 空）。
**判定**：burst 内是 **KV 约束驱动的排队区**——并发确实到 10，但 10×~6k 超配 39k KV ≈2x → queue/preempt。

## 8. Queue / KV / prefill-decode 数据（3 run）

- queue time p50 0.24–0.27s / **p90 4.8–8.2s**；TTFT p50 0.64–0.73s / p90 5.9–7.6s；waiting>0 仅 21–25s/run 且 **100% 在 cv**。
- KV>0.9 仅 9–14s/run（2.6–4.7%），**100% 在 cv**；preemptions 1–2。
- **prefill p50 0.51s / p90 0.77s；decode p50 5.0s / p90 9.2s** → decode 主导，推翻 prefill-heavy 假设。
- prefix cache 命中率 1.25–1.44%、cached token 占比 8.2–10.9% → 收益有限（公共前缀相对 5–7k 内容很小）。

## 9. 单请求 micro 基线（§19，idle、合成 prompt、~300 output tok）

1512 tok → 1.44–1.54s；5012 → 1.56–1.66s；6812 → 1.54–1.68s。
**与 prompt 大小几乎无关**（prefill 便宜）；burst 下同形状约慢 ~4x（e2e mean 7.2s，queue p90 ~8s）。

## 10. Token 方差（§26）

本地 input tokens 226k/220k/204k（~10% 波动），E2E 305/328/286s（~15%）。**p1↔p2 证明 token 不是主因**（p2 本地 token 更少但 E2E 更长）——差异更多来自云端调用时长；n=3 不做更强归因。

## 11. Bottleneck classification（§36）

| 分类 | 判定 | 关键证据 |
|---|---|---|
| A. GPU compute saturated | ✔（burst 内） | 活跃期 99.8–100%；忙碌分布二元（10–90% = 0 采样） |
| B. vLLM queue/scheduler limited | ✔（burst 内，由 KV 引发） | waiting 峰值 5、queue p90 4.8–8.2s，全部在 cv |
| C. KV-cache constrained | ✔（burst 内主约束） | KV 峰值 0.99、>0.9 时段 100% 在 cv、preemptions 1–2、10×6k 超配 2x |
| D. prefill-heavy | ✘ 被否定 | prefill p50 0.51s vs decode p50 5.0s |
| E. concurrency underutilizes GPU | ✔（全局） | 75% wall GPU 空闲；瓶颈在云端串行链 |

## 12. Phase 4B 设计（仅设计，未执行）

范围 **4 / 6 / 8 / 10 / 12**（数据依据：峰值 10、KV 有效并发 ~6）。方法：固定 workload burst harness（micro 扩展到受控并发），每档 ×3，复用 Phase 4A profiler。主指标：burst wall（固定工作量）；次指标：p50/p90、TTFT、queue、吞吐、GPU、KV 峰值、preemption；硬门槛：输出正确性、overflow/OOM/失败=0。**不以 GPU 利用率更高为目标。**

## 13. Cloud/local 角色画像（observation only）

p1 云端 summed：writer 91.3s/1 call（off）、draft 56.8s（off）、**evaluator 54.2s/13 calls（rea 710）**、supervisor 31.8s/3（rea 716）、red_team 2.7s。
候选观察（**不实施**）：evaluator 为未来 cloud→local 第一顺位（高频、结构化、思考需求低），但注意其 10 次调用与本地 burst 同窗、下沉会争抢 KV。supervisor 明确排除（E1b 质量退化）。red_team 按 §30 仅记录。

## 14. Git commits

```
a12d28d feat(profiling): single-request local micro baseline harness (Phase 4A §19)
e8e4d1c feat(profiling): add read-only vLLM and GPU baseline telemetry   ← 3 次 E2E 冻结 revision
053cb18 perf(draft): adopt thinking-off as accepted default              ← E9 landing
```

## 15. 产物与复算

- runs：`artifacts/baseline/phase4a-p{1,2,3}/`；采样与 summary：`artifacts/phase4a/phase4a-p{1,2,3}/{gpu_metrics.csv,vllm_metrics.csv,vllm_histograms.jsonl,timeline.csv,summary.json}`（不入库）。
- 复算：`python -m deep_research.profiling.sampler --out-dir …`（run 期间）+ `scripts/experiments/phase4a_analysis.py --run-id … --profile-dir …`；micro：`scripts/experiments/phase4a_local_micro.py`。

## 16. Remaining risks

1. n=3 且 token 波动 ~10%，方差归因只有方向性。2. GPU 忙碌口径受 1s 采样粒度影响。3. 直方图分位为桶插值近似。4. `iteration_tokens_total` 单位是 tok/step（勿当秒）。5. micro 合成内容与真实网页分布不同（形状一致）。6. 模型目录无 tokenizer.json → 生产 context budget 走 chars/2 启发式（如实记录，未改）。
