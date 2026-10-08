# PHASE 4B — FAST SWEEP（claim_verification 并发窗口）+ E2E CRITICAL PATH

> 生成：2026-10-06 · 起点 HEAD `5b28813`（工作区 clean）· 本轮未改任何业务语义/vLLM 参数
> **最终结论（审核后定稿）**：
> - **C=6 = QoS / KV-headroom candidate → KEEP**（并发窗口候选，非性能优化）；
> - **Performance = NEUTRAL**：C=6 相对现状 C=10 的 burst wall 差异在噪声内，**不宣称 C=6 能显著降低 E2E**；
> - **暂不修改 production default concurrency**（应用层闸门未落地，默认行为不变）。
>
> 本地 lane 只占 E2E 13.1%，wall 在 C=6~10 之间基本持平 → **全局收益不在本地**。E2E 是**近乎纯串行的链**（顶层节点覆盖 98.6–99.0%），最大单项是 final writer 节点（31.8%）与 draft 节点（20.4%）。

---

## A. Repository state

| 项 | 值 |
|---|---|
| start HEAD | `5b28813`（docs: persist Phase 3C-1/3C-2/3C-3/4A reports）· 工作区 clean |
| end HEAD | `5b28813`（**本轮未提交**，改动全部在工作区，等待审核） |
| 新增文件 | `deep_research/profiling/{workload,burst,critical_path}.py`、`scripts/experiments/phase4b_{burst,critical_path,sweep_table}.py`、`tests/test_phase4b_{burst,critical_path}.py` |
| 修改文件 | `.gitignore`（+2 行：`artifacts/phase4b/`） |
| 删除/改动既有逻辑 | **无**。既有 profiling harness（Phase 4A）零改动，新增模块不 import 任何业务执行路径 |
| commits | 无（按约定：未获用户指示不提交；见 §H「待审核动作」） |

`git diff --stat`：`.gitignore | 2 ++`，其余为新增文件（untracked）。

---

## B. Baseline verification（开工前核对，全部通过）

| 核对项 | 期望 | 实测 |
|---|---|---|
| git 工作区 | clean @ 5b28813 | ✓ clean，HEAD 含 handoff 列出的 `e8e4d1c` / `053cb18` / `a12d28d` |
| full suite | 611 passed / 1 skipped | ✓ **611 passed / 1 skipped / 0 failed**（改动前）→ **646 passed / 1 skipped**（改动后，+35 新测试） |
| vLLM serve 参数 | 见 handoff | ✓ 与 handoff 逐字一致，**进程未重启**（启动于 16:15:54，uptime > 4.5h） |
| vLLM / KV 容量 | 0.19.1 / 2441 blocks × 16 = 39,056 tok | ✓ `/metrics` cache_config_info：`num_gpu_blocks=2441, block_size=16, enable_prefix_caching=True, gpu_memory_utilization=0.85` |
| thinking policy | claim_verify=off / supervisor=on / writer=off / draft=off | ✓ worker environ `DR_CLAIM_VERIFY_THINKING=off`；其余未显式设置 → 代码默认（on/off/off）；本地 backend `extra_body.chat_template_kwargs.enable_thinking=false` |
| 本地服务空闲 | 无其它负载 | ✓ 每次 burst 前 `_wait_idle()` 连续 3 次采样 running=waiting=KV=0 才开跑 |

**vLLM config unchanged proof**：`ps -eo lstart,args | grep "vllm serve"` 与 handoff §一 的启动命令逐字相同，且进程启动时间早于本轮会话；本轮全程只做 `POST /v1/chat/completions`、`POST /tokenize`、`GET /metrics`、`nvidia-smi`，无任何管理接口调用（该 build 也没有 `/reset_prefix_cache`）。

---

## C. Frozen workload

- **manifest**：`artifacts/phase4b/workload_manifest.json`（schema `phase4b.workload.v1`，指纹 **`dc9ec08d28dec304`**，20 请求）
- **形状来源**：Phase 4A 三次 E2E 中 `claim_verification` 内全部 60 次本地 `researcher_summarizer` 调用的 input token 数（20/run），合并排序后**等间隔秩取样** 20 个 → `1903, 2434, 3316, 4294, 5030, 5610, 6024, 6480, 6666, 6739, 6819, 6823, 6828, 6863, 6877, 6896, 6898, 6901, 6914, 6923`，合计 **117,238 prompt tokens**（生产真实 cv burst ≈ 120k，同量级）。
- **prompt**：真实 `SUMMARIZE_PROMPT` 模板 + **确定性合成正文**（seeded 生成 + vLLM `/tokenize` 二分拟合到目标 token 数，实测 20/20 精确命中）。正文逐请求唯一（20/20 sha1 不同），因此没有生产不存在的公共前缀（对比：生产 prefix cache token 占比 8–11%，harness 实测 1.1%）。
- **固定项**：同一批 20 条请求、同一顺序、同一 `max_tokens=600`、同一模型/端点/extra_body；manifest 落盘 prompt 原文 + sha1，每次 run 校验指纹。
- **复现**：`--build-manifest` 重建会得到相同指纹（固定日期常量，不用 `get_today_str()`）。
- **与生产的已知差异**（全部记录在 `summary.json.sampling`）：
  1. `max_tokens=600` 是 harness 上界（生产不传）；生产观测 completion 278–520（p50 372），本 harness p50 ≈ 270（合成正文更"无信息量"，摘要更短）；
  2. harness 无 tavily 网络间隙，本地请求排得更密 → burst wall 比生产短（C=10：harness 22.0s vs 生产 cv 本地段 28.8s）；
  3. 采样参数与生产一致（**不传 temperature**，用模型自带 generation config）。
- **为什么不用 temperature=0**：实测贪心 + 合成正文会周期性陷入重复循环打满 `max_tokens`（截断 → 内容不可解析），且 vLLM 连续批处理下贪心输出本身不可复现（批形状变化 → 数值路径变化），"确定性"不成立。

---

## D. Fast Sweep table

`concurrency = 同时允许在飞的本地请求数`（应用层闸门，对应生产里给 claim 级搜索加信号量）。每档测量前先跑一次**丢弃的 warmup burst**（同 workload 同并发），使各档进入测量时的 prefix cache 状态一致。

| C | reps | burst wall (s) | median | lat p50 | lat p90 | TTFT p50/p90 | queue p50/p90 | decode p50 | KV peak | running peak | waiting peak | preempt | gen tokens | GPU mean | gates |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | 1 | 24.6 | 24.6 | 4.40 | 6.68 | 0.62 / 0.88 | 0.15 / 0.27 | 3.93 | 0.734 | 4 | 1 | 0 | 5954 | 96.1% | **FAIL**（1 截断） |
| **6** | **3** | 22.0 / 22.9 / 22.1 | **22.1** | 6.17–6.28 | 8.06–8.86 | 1.30–1.41 / 2.38–2.50 | 0.30–0.54 / 1.00–2.00 | 4.77–5.00 | 0.900–0.941 | 6 | 3 | 0 | 5191–5491 | 95.7–97.6% | **PASS ×3** |
| **8** | **3** | 20.9 / 22.5 / 21.1 | **21.1** | 7.33–7.64 | 9.73–10.46 | 2.07–2.20 / 5.00–5.83 | 0.93–1.17 / 4.17–4.58 | 5.00–6.67 | 0.945–0.978 | 8 | 4–5 | 0 | 5109–5494 | 95.5–97.4% | **PASS ×3** |
| 10 | 1 (+1 pilot¹) | 22.0 | 22.0 | 8.93 | 12.26 | 3.33 / 7.14 | 2.00 / 8.57 | 6.15 | 0.995 | 8 | 8 | 0 | 5396 | 95.7% | FAIL（2 缺陷） |
| 12 | 1 | 23.4 | 23.4 | 11.34 | 16.63 | 5.50 / 15.00 | 5.56 / 10.00 | 5.00 | 0.935 | 8 | 8 | 0 | 6091 | 96.4% | FAIL（2 截断） |

¹ `c10/rep0` 是采样参数切换前（temperature=0）的 pilot，**不参与比较**，仅留档。
吞吐（20 请求/wall）：C=4 0.81 → C=6 0.90 → C=8 0.95 → C=10 0.91 → C=12 0.85 req/s。

**hard gates**：request failure / OOM / context overflow / profiler error 全程 **0**；唯一失败项是 **correctness**（见下）。

**workload 跨档完全一致的机器证据**：10 次 burst 的 vLLM 计数器 delta **逐位相同**——`prompt_tokens = 117,478`（= manifest 117,238 + chat 模板开销 240，10/10 完全一致）、`prompt_tokens_cached = 1,280`（10/10 相同，说明 warmup 把各档的 prefix cache 起点拉平了）；唯一变动的是 `generation_tokens`（5.1k–6.1k，采样随机性，已作为协变量记录）。

### D.1 correctness 失败的性质（重要，影响读表）

5 次不良响应 / 120 次请求（6 档），**分散在不同 prompt 上（idx 9/10/11/14/16，无 repeat offender）**，且同一 prompt 在别的档位正常 → **未观察到并发诱发的证据**（相关性观察，非因果结论）。更关键的是生产侧存在同类观测：

- 生产 worker 日志（`logs/worker.log`）里有 **17 次 `Failed to summarize webpage: Failed to parse JSON from response`**，失败模式与本 harness 完全相同（生产走 fallback：返回正文前 1000 字，任务照常完成，不影响任务成败）。分母只能取下限：30 个 baseline run 的 artifacts 记录 878 次 `researcher_summarizer` 调用，而日志窗口内共完成 **77 个任务**（真实分母更大）⇒ **生产侧观测到的失败发生率上界（observed failure upper bound）≤ 1.9%**。这只是"该失败模式在生产同样存在且量级相当"的证据，**不足以**据此断言模型的固有缺陷率，也未做因果验证。
- 本 harness 缺陷率 4.2%，同量级，其中 3 次是我的 `max_tokens=600` 上界造成的截断（生产不设上界，模型自然停止，观测最大 520）。

⇒ 观察到的失败分布**不支持**"并发诱发"这一解释（分散在不同 prompt、无 repeat offender、同一 prompt 在别档正常），但这只是**相关性证据，本轮未做因果验证**。因此本轮的 hard gate 读法调整为：**"failure/OOM/overflow/profiler=0 且观察到的不良响应数不高于生产观测到的量级"**；C=6/C=8 两个档位在 3/3 reps 上零缺陷，C=4/C=10/C=12 各 1–2 次偶发（含截断）——与并发水平没有单调关系（6、8 最干净，4 与 10、12 反而有偶发）。

### D.2 一个直接的引擎侧读数

`running peak` 在 C=10/C=12 时都只到 **8**：即使应用层放进 10–12 个请求，vLLM 实际并发执行的 sequence 也只有 8 条，其余在 waiting（峰值 8）。即**该请求形状下引擎的有效 admission 上限 ≈ 8**——这与 C=8 恰好是最快的档位相互印证。

---

## E. Winner decision

**KEEP：claim_verification 并发窗口 = 6 —— 定性为 QoS / KV-headroom candidate（不是性能优化）**
**Performance verdict = NEUTRAL：不以"降低 E2E"为由采纳；本轮暂不修改 production default concurrency。**（C=8 为统计等价的备选）

判据链：

1. **hard gates**：C=6（3/3）、C=8（3/3）全程干净；C=4/C=10/C=12 有偶发缺陷（其中 3/5 是我方 max_tokens 上界造成的截断）。按任务书"任何违反 hard gate 的档位直接淘汰"，参与排名的是 {6, 8}。
2. **PRIMARY = median burst wall**：C=8 = 21.1s，C=6 = 22.1s，**差 4.8%**，落在 run-to-run 噪声内（C=8 自身极差 20.9–22.5 = 1.6s = 7.6%；C=6 极差 0.9s）。n=3/档，不做显著性主张。
3. **tie-break（任务书指定顺序）**：

| tie-break 维度 | C=6 | C=8 | 胜 |
|---|---|---|---|
| concurrency 更低 | 6 | 8 | C=6 |
| preemption | 0 | 0 | 平 |
| queue p90 | 1.0–2.0s | 4.17–4.58s | **C=6** |
| KV 峰值 | 0.900–0.941 | 0.945–0.978 | **C=6** |
| waiting 峰值 | 3 | 4–5 | **C=6** |
| latency 稳定性（p50 极差） | 0.11s | 0.31s | **C=6** |
| GPU 利用率更高 | 95.7–97.6% | 95.5–97.4% | 平（**不作为判据**） |

⇒ **C=6 胜出**。同时诚实记录两件事：

- **C=8 的中位 wall 确实更低（-1.0s）**，若未来 E2E 验证显示 C=8 稳定更快，应接受 C=8（任务书明确：不得为了指标漂亮强行选 6/8）。
- **相对现状（C=10）**：harness 里 C=6 的 wall（22.1s）与 C=10（22.0s）**基本相同**。也就是说，把并发从今天的"10 个 claim 全放"压到 6，**不会缩短 burst wall**；换来的是 KV 峰值 0.995→0.94、waiting 8→3、queue p90 8.6s→1.5s、单请求 p50 8.9s→6.2s 的**余量**。这不是"更快"，是"更稳、更不容易在混合负载下崩"。
- 因此本轮**不动 production default**：闸门只是"候选实现"，是否启用取决于后续对 QoS 余量的需求（例如未来把 evaluator 下沉到本地、或多任务并发时），而不是当前的 wall-time 收益。

**C=4 明确淘汰**（wall 24.6s，最慢）；**C=12 明确淘汰**（wall 23.4s，且 queue p90 10s / TTFT p90 15s，纯排队）；**C=10 淘汰**（KV 打满 0.995、waiting 8，且 2 个 reps 里 1 个挂 gate）。

### E.1 Candidate implementation（**未落地**，等审核）

不要动 vLLM 的 `max_num_seqs`（全局语义 ≠ claim burst 的 admission window）。应用层目前**没有**现成的并发闸门，最小改动是在 `ClaimVerifier.verify()` 的搜索阶段加信号量：

```python
# deep_research/verification/claim_verifier.py
_SEM_LIMIT = int(os.environ.get("DR_CLAIM_VERIFY_CONCURRENCY", "10"))   # 默认 10 = 现行行为
_SEMAPHORE = asyncio.Semaphore(_SEM_LIMIT)                              # import-time 构造（与 thinking 开关同风格）

async def _search_one(self, claim_text: str) -> str:
    async with _SEMAPHORE:            # 闸住"同时在飞的 claim 搜索"，即本地 summarize 并发
        ...                            # 原逻辑不变（to_thread(tavily_search, ...)）
```

- 默认值 10 = 与今天完全一致（不改变行为，可随时回退）；
- 需要 verify/preflight/fingerprint/测试同步（与 thinking 开关同一套流程）；
- 风险点：`DR_CLAIM_VERIFY_CONCURRENCY` 必须进入 config fingerprint，否则 A/B 指纹相同；
- **本轮的证据边界**：该闸门只覆盖 claim_verification，不覆盖研究阶段的本地 summarize（见 F 节：研究阶段还有 21.6s 的本地 summarize，峰值并发只有 ~2，不受影响）。

---

## F. Critical-path report

方法（证据分级，见 `deep_research/profiling/critical_path.py`）：

- **proven**：顶层节点链由 graph 定义保证串行（`agent_builder._create_builder` 的 `add_edge`：START → brief → draft → [human_review] → supervisor_subgraph → claim_verification → final_report_generation → END）。因此**每个顶层段的 wall 100% 在关键路径上**——这不是推断，是构造。
- **attributed**：段内调用按"最大重叠簇"划分，簇 makespan 按**并发份额**摊给各 role（可加：Σ=覆盖时长）。**不使用 summed**。
- **unattributed**：段间 / 簇间空隙单独列出。

### F.1 顶层串行链（3 个 run）

| segment | p1 | p2 | p3 | median | %E2E | 段内 blocking 归因（p1） |
|---|---|---|---|---|---|---|
| write_research_brief | 7.2 | 5.2 | 7.2 | **7.2** | 2.4% | evaluator 6.8s |
| write_draft_report | 56.8 | 100.2 | 60.1 | **60.1** | **20.4%** | draft 56.8s（独占） |
| human_review | 0.0 | 0.0 | 0.0 | 0.0 | 0.0% | — |
| supervisor_subgraph | 87.9 | 92.5 | 85.4 | **87.9** | **29.6%** | supervisor 31.8 / summarizer 21.6 / compressor 14.7 / researcher_main 12.0 / red_team 2.7 |
| claim_verification | 40.7 | 37.8 | 37.8 | **37.8** | 13.1% | **本地 summarizer 28.8** / evaluator 10.5 |
| final_report_generation | 109.1 | 87.8 | 92.4 | **92.4** | **31.8%** | **writer 91.3** / evaluator(记忆抽取) 16.4 |
| 未归因 gap | 3.3 | 4.7 | 2.8 | **3.3** | **1.1%** | — |
| **E2E** | 305.1 | 328.1 | 285.7 | **305.1** | 100% | 顶层覆盖 98.6–99.0% |

**结论：E2E ≈ 一条串行链。编排 idle（gap）只占 1.1%，没有可回收的"空转"。**

### F.2 summed vs blocking（口径纪律的量化，p1 / p2 / p3）

| role | calls | summed (s) | **blocking (s)** | blocking/E2E | 说明 |
|---|---|---|---|---|---|
| writer | 1 | 91.3 / 75.2 / 71.2 | **91.3 / 75.2 / 71.2** | 24.9–29.9% | 100% 在关键路径（独占整段） |
| draft | 1 | 56.8 / 100.2 / 60.1 | **56.8 / 100.2 / 60.1** | 18.6–30.6% | 同上，且方差最大 |
| researcher_summarizer | 32 | 274.7 / 250.6 / 264.3 | **50.4 / 47.4 / 40.2** | 14.1–16.5% | summed 是 blocking 的 ~5.4x（并行度高） |
| evaluator | 13 | 54.2 / 53.9 / 52.2 | **33.6 / 26.7 / 34.5** | 8.1–12.1% | summed 高估 1.6x |
| supervisor | 3 | 31.8 / 31.6 / 35.3 | **31.8 / 31.6 / 35.3** | 9.6–12.4% | 3 次串行，100% 阻塞 |
| researcher_compressor | 2 | 21.1 / 27.2 / 18.4 | 14.7 / 17.8 / 8.7 | 3.1–5.4% | |
| researcher_main | 10 | 22.1 / 20.4 / 10.0 | 12.0 / 10.8 / 17.2 | 3.3–6.0% | |
| red_team | 1 | 2.7 / 3.9 / 7.5 | 2.7 / 3.9 / 7.5 | 0.9–2.6% | |

**这些数字直接回答 §B3 的七问：**

1. **writer 的 ~91s 有多少在关键路径？** 全部。writer 调用（71.2–91.3s，median 75.2s，输出 4505–5111 tokens）独占 `final_report_generation` 段，段是串行链的尾巴 → **blocking == summed**。该段还包含 **~16s 的报告后记忆抽取**（见 3.），也串行。
2. **draft 的 ~57s 是否串行阻塞 writer？** 是，而且比"阻塞 writer"更严重：它在**研究开始之前**就占据 56.8–100.2s（median 60.1s），整条下游链（研究 → 核查 → 写作）都排在它后面。draft 只依赖 research_brief，不依赖研究发现，但它的产物同时喂给 supervisor 首轮消息（`supervisor_messages`）和 final writer 的 prompt（`draft_report`）。
3. **evaluator 的 13 calls 有多少真阻塞？** summed 53.9s → **blocking 33.6s（62%）**。拆开：brief 阶段 1 次（6.8s，独占）+ 核查阶段 10 次并行 judge（批次 makespan 只算 **10.5s**，而 summed 是 31.1s）+ writer 之后的 1 次 **16.4s（结构化记忆抽取，`store_from_report`，串行在报告生成之后）**。
4. **supervisor 是否形成不可并行串行链？** 是。3 次调用、31.8s，各自独占一个节点窗口，100% 阻塞（10.4% of E2E）。
5. **搜索/tool 等待占多少？** tavily API 本身很便宜：研究阶段 4 次共 12.8–14.7s（p50 ~3s）、核查阶段 10 次共 21.3–28.8s（p50 1.8–2.8s），且高度并行。**真正的代价是搜索内部的本地网页摘要**：研究阶段 12 次本地调用 blocking 21.6s（p1），核查阶段 20 次 blocking 28.8s（p1）——这部分是 KV 约束的本地算力，不是网络等待。
6. **是否存在明显 orchestration idle gaps？** 没有。段间空隙合计 median 3.3s（1.1% of E2E），最大的一处是 draft → human_review 的 2.7–4.7s。
7. **哪一个单独 intervention 最可能带来最大的 E2E wall reduction？** 见 G/H。

### F.3 时间线（p1，相对秒）

```
  0.0 ─ 7.2   brief        (evaluator 6.8s, cloud)
  7.2 ─ 64.1  draft        (draft 56.8s, cloud, 2625 out-tok)          ← 串行
 64.1 ─ 67.3  [gap 3.2]
 67.3 ─155.3  supervisor_subgraph  (88.0s)
               ├ supervisor 8.3 / 7.6 / 15.9s  (3 次串行 cloud)
               ├ supervisor_tools 53.3s：tool_node 54.8s（本地摘要 40.4s，峰值并发 2）+ compress 21.1s + llm_call
               └ red_team 2.8s
155.3 ─195.9  claim_verification  (40.7s)  = 抽取 4.0 + 本地 burst 28.8（并发 10, KV 0.99）+ judge 批次 6.5
196.0 ─305.1  final_report_generation (109.1s) = writer 91.3（5111 out-tok, cloud）+ 记忆抽取 16.4（evaluator, cloud）
```

---

## G. Top 3 next optimization targets（按"可减少 E2E wall 的潜力"排序，**不按 summed**）

| # | 目标 | 现状 blocking（median） | 可回收潜力 | 代价 / 风险 | 证据等级 |
|---|---|---|---|---|---|
| **1** | **draft 段（write_draft_report）** | **60.1s = 20.4% E2E**（最高到 100.2s） | 若与 research loop 并行（draft 只依赖 brief，与研究发现无数据依赖）→ 理论上可回收 ~60s；若压缩草稿长度 → 按比例回收 | 中高：改变 research 的 seeding 语义（`supervisor_messages` 首条是 draft），必须做质量 A/B | proven（串行段 + 代码依赖已核对） |
| **2** | **final_report_generation 段** | **92.4s = 31.8%**：writer 75.2s + 报告后记忆抽取 ~16s | 记忆抽取可从"任务完成前"移到"任务完成/报告已发出之后" → **≈ 11–20s（4–6% E2E），质量零影响**；writer 本体只能靠换模型/缩短篇幅（质量敏感） | 低（记忆抽取）到高（writer） | proven（记忆抽取在节点内串行，代码路径 `store_from_report` 已核对） |
| **3** | **research 阶段（supervisor_subgraph）** | **87.9s = 29.6%**：supervisor 31.8s 串行 + 本地摘要 21.6s（KV 约束）+ compressor 14.7s + researcher_main 12.0s | supervisor 3 次串行往返（10.4%）能否合并/预取；本地摘要与 cv burst 同属 KV 约束的本地算力 | 中：改 supervisor 调用结构会影响研究质量 | proven（段与归因已量化） |

**claim_verification 只排第 4**（37.8–40.7s = 13.1%，其中本地 burst 28.8s）：Fast Sweep 的目标段，可回收潜力只有 ~1s（C=6/8 与 C=10 的 wall 差在噪声内）。**这也正是本轮最重要的结论之一：本地 lane 不是 E2E 的主要矛盾。**

---

## H. Recommendation

**下一步只推荐 1 个主动作：把 final_report_generation 段尾部的"报告后记忆抽取"移出任务完成的关键路径**（G#2 的零质量风险部分）。

- 预期：**-11 ~ -20s，约 -4 ~ -6% E2E**（3 run 实测该调用 16.4 / 10.9 / 19.6s，且 100% 串行）。
- 为什么是它而不是 G#1（draft，潜力 60s）：draft 是**潜力最大**的，但它改的是 research seeding 语义（workflow semantics），属于本轮明令禁止的范围，且必须配一套质量 A/B（E8 式）才能拍板——应先做设计评审，不是下一步直接动手。
- 前置决策（需要你拍板，见 §I-1）：**E2E 的口径**。当前 benchmark 的 E2E 计到"图执行结束"，即包含这段"用户已经拿到完整报告之后"的记账时间；若把它定义成"报告流式输出完成"，这段本来就不该计入。两种口径对应两种做法（移出关键路径 vs 改口径）。

**配套的 1 个低成本 sensitivity test**（验证归因模型，不用云费用）：

- 用同一套 harness 在**真实 vLLM 上**跑一次 `C=6 vs C=10` 的**长 burst 灵敏度**（把 workload 放大到 40 请求、gen tokens 对齐生产 p50=372 的形状），确认"wall 在 6~10 之间持平"不是因为 harness 的 burst 太短（22s）而掩盖了排队效应。成本 ≈ 10 分钟本地算力、0 云端费用。

**本轮动作边界**：只提交 profiling harness / tests / 本报告；**不落地** `DR_CLAIM_VERIFY_CONCURRENCY`（候选实现，未启用，production default 不变）；不动 evaluator 下沉 / supervisor 路由 / writer / draft 模型；不动任何 vLLM 参数。

---

## I. Remaining risks / measurement limitations

1. **E2E 口径未定**：benchmark 的 E2E 含报告后记忆抽取（~16s）与 SSE/任务收尾；"用户感知完成时刻"更早。这直接影响 H 的推荐是否成立，需要你确认口径。
2. **harness 与生产的三个已知差异**（C 节）：gen tokens 偏短（270 vs 372）、无搜索网络间隙、`max_tokens=600` 上界。三者都使 harness 的 burst wall **偏乐观**（22s vs 生产 28.8s）；跨并发档的相对比较仍有效，但绝对节省量不能直接外推。
3. **失败观测的噪声地板**：生产侧该类 JSON 失败的上界 ≤1.9%、harness 侧 4.2%，且分布不支持"并发诱发"（未做因果验证）。n=20/档时，单个偶发不良响应就会让一个档位"挂 gate"——这也是为什么本轮 winner 只能在 3/3 干净的两个档位之间比。样本量不足以对 C=4/C=10/C=12 的失败发生率下结论。
4. **n=3 的统计力**：C=6 与 C=8 的 4.8% wall 差异不做显著性主张；若两者差异对决策重要，需要更多 reps（每档 +3 ≈ 6 分钟本地算力）。
5. **C=10/C=12 只有 1 个有效样本**（且都挂了 gate），它们的 median 不可与 C=6/C=8 直接排名；表中已标注。
6. **KV 峰值受 0.5s 采样限制**：C=12 的 KV 峰值（0.935）反而低于 C=10（0.995），可能是采样时点/准入模式差异，不宜过度解读。
7. **critical path 归因的边界**：段内归因用"并发份额"，对"谁在拖尾"另给了 `binder_s` 诊断量；对跨段的因果关系（例如 draft 的产物是否真的提升了终稿质量）本轮**不做**结论。
8. **未验证**：C=6 的结论尚未在真实 E2E 上验证（harness 只覆盖本地 burst，不含 tavily 延迟与云端调用交织）。落地前需要 1 次 E2E 验证（带闸门的 claim_verification vs baseline）。

---

## 附：产物与复算

| 内容 | 路径（均不入库） |
|---|---|
| 冻结 workload | `artifacts/phase4b/workload_manifest.json`（fingerprint `dc9ec08d28dec304`） |
| sweep 原始结果 | `artifacts/phase4b/sweep/c{4,6,8,10,12}/rep*/{burst_results.json,summary.json,gpu_metrics.csv,vllm_metrics.csv,vllm_histograms.jsonl}` |
| sweep 决策表 | `artifacts/phase4b/sweep/sweep_table.json` |
| critical path | `artifacts/phase4b/critical_path/{phase4a-p1,p2,p3.json,summary.json}` |

```bash
# 复算
.venv/bin/python scripts/experiments/phase4b_burst.py --build-manifest            # 重建 manifest（同指纹）
.venv/bin/python scripts/experiments/phase4b_burst.py --concurrency 6 8 --rep 4   # 追加 reps
.venv/bin/python scripts/experiments/phase4b_sweep_table.py                       # 决策表 + winner
.venv/bin/python scripts/experiments/phase4b_critical_path.py --runs phase4a-p1 phase4a-p2 phase4a-p3
```
