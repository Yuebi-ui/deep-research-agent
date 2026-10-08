# PHASE 5A — SEED-LITE SPECULATIVE RESEARCH（Runtime Spike + Small A/B）

> 生成：2026-10-06 · 起点 `22d77bb`（Phase 4C-lite 已提交）· 本轮**未 productionize**：投机模式由
> `DR_SEED_LITE_SPECULATIVE` 开关控制，**默认 off**（关闭时生产 wiring/语义逐字不变）
> 结论：**KEEP CANDIDATE**（见 §N）——性能配对 8/8 更快（中位 −60s），质量盲评 4:4 打平；
> 但 **HITL 位置被 superstep 屏障强制后移**（审查发生在 research 之后），这是产品语义变更，必须审核。

---

## A. Repository state

| 项 | 值 |
|---|---|
| start HEAD | `22d77bb`（Phase 4C-lite）· 工作区 clean |
| 本轮新增 | `deep_research/research_seed.py`、`scripts/experiments/phase5a_{langgraph_spike,analyze,ab_driver}.*`、`tests/test_phase5a_{langgraph_semantics,research_seed}.py` |
| 本轮修改 | `agent_builder.py`（开关 + seed 节点 + 投机 wiring + HITL 批准路由）、`agents/draft_agent.py`（投机模式下不写 supervisor_messages）、`agents/supervisor.py`（draft 缺失时 refine 保护）、`scripts/experiments/e8_paired_judge.py`（变体标签通用化，E8 行为不变） |
| 未改动 | vLLM 参数、模型/路由、thinking 策略、claim 并发、Phase 4C-lite 行为、生产 graph 默认路径 |
| 测试 | full suite **682 passed / 1 skipped / 0 failed**（改动后）；新增 26 个 Phase 5A 测试 |

---

## B. draft → research 依赖矩阵（问题 1）

**先看事实（不是 token 数）**：`researcher_agent` 的输入只有 supervisor 派发的
`research_topic` 字符串（`supervisor.py:243-249`：`{"researcher_messages":[HumanMessage(research_topic)]}`）——
**draft 从不进入子研究者上下文**。draft 影响 research 的唯一路径是 supervisor 首轮消息里的
那一条「Here is the draft report: <全文>」。

| information item | 当前 source | draft 之前可得？ | research 需要？ | 只 writer/HITL 需要？ | seed 来源 |
|---|---|---|---|---|---|
| research objective / 维度 / 范围 | `research_brief`（LLM 生成，888–1097 字符 JSON） | ✅ | ✅ | — | **research_brief 原文（确定性投影）** |
| 来源指引（官方文档/GitHub/权威来源） | `research_brief` | ✅ | ✅ | — | research_brief |
| draft 的 18 个标题层级 / 叙述结构 | `draft_report` | ❌ | ✘（researcher 看不到；supervisor 只当作参考） | ✅（writer prompt） | 不需要——supervisor 自行规划 |
| draft 中的"事实" | `draft_report` | ❌ | ✘ **无检索证据**：draft 在 research 之前生成，只有 brief + 模型先验 | ✅ 作为终稿底稿 | 不需要 |
| HITL 审查对象 | `draft_report` | ❌ | ✘ | ✅ | 不变（draft 仍照常生成） |
| refine 工具输入（研究途中） | `draft_report` | ❌ | ⚠️ 条件依赖，**实测 3/3 run 未触发** | — | 空值保护（已实现） |
| 研究主题本身 | supervisor 生成 | — | ✅ | — | supervisor 照常生成 |

**核心问题的回答**：*"Research 真正需要、但 Brief/现有 structured state 中没有的信息是什么？"*
→ **在本轮证据下 = 空集**。依据三条：

1. **可达性**：researcher 只吃 `research_topic`；draft 不进其上下文（代码事实）。
2. **时效性**：draft 生成时研究尚未开始，它**不可能包含任何检索证据**，只是"brief 的模型先验复述 + 报告结构"；
   而 brief 已经把 objective/维度/范围/来源指引写全（实测 brief 文本见 §0 引文）。
3. **反例位置**：真实 graph 里唯一依赖 draft 的 research 侧路径（`refine_draft_report`）在 3/3 run 中未触发；
   `red_team` 更有"无 draft 则退回 research_brief"的现成 fallback（`red_team_agent.py:38-40`）。

⚠️ **不做的过度声明**：词汇重叠分析（topic vs brief vs draft）**不足以判定**——draft 比 brief 长 7–20×，
词汇覆盖率天然更高（实测 draft 独有术语 94–99%）。本轮结论以**代码可达性 + 时序证据 + A/B 质量对比**为准。

---

## C. research_seed schema + provenance（问题 2）

```python
seed = {
  "seed_version": "seed-lite-v1",
  "objective":     <research_brief 原文，逐字不改写>,
  "draft_available": False,
}
# supervisor 首轮消息：
# "Research seed (no draft report is available yet — plan the research from this alone):\n{objective}"
```

- **构造来源**：`build_research_seed(research_brief)` —— **确定性纯函数，零 LLM 调用**；
- **可 fingerprint**：`seed_fingerprint()` = sha256(version+objective)[:16]（实测 `2809ab536bb0cc4a`）；
- **不新增字段**：brief 已是完整的 research question，没有"为了填 schema"的字段（遵守 §2 要求）；
- **不改用户意图**：objective 逐字透传（测试锁定）。

---

## D. seed vs draft 尺寸（问题 2 的量化）

| 项 | 实测 |
|---|---|
| seed message | **873 字符**（q1；含一行框架说明） |
| draft（同一 run） | 7442 – 17980 字符（Phase 4A/4C 实测区间） |
| 比值 | **8.5× – 20.6×**（draft / seed） |
| 被移除且 research 不需要的信息 | draft 的 18 个标题结构、2600–5000 tokens 的叙述正文、以及其中的模型先验"事实"（无引用） |

---

## E. LangGraph 并发 / interrupt spike（问题 3）

**方法**：`scripts/experiments/phase5a_langgraph_spike.py` —— 真实 langgraph **1.2.12** + 真实 checkpointer
后端（AsyncRedisSaver），最小拓扑 + 可观测假节点；结论再用 `tests/test_phase5a_langgraph_semantics.py`
（InMemorySaver，5 个回归测试）钉住。

| # | 问题 | 实测结论 |
|---|---|---|
| 1 | 两分支是否真并发 | ✅ 是。同 superstep 内并发执行，总时长 ≈ max(分支)，重叠 = 0.50s/0.50s |
| 2 | 早早就绪的节点能否提前 HITL | ❌ **不能**。节点必须等所在 superstep 全部结束才进入下一 superstep（实测 HITL 在 +2.01s 才触发，而 draft 分支 +0.5s 就绪） |
| 3 | 分支 interrupt 时兄弟节点如何 | ❌ **不被取消**：兄弟节点跑完并落 checkpoint（实测 elapsed=0.51s vs 兄弟 0.5s） |
| 4 | resume 是否重复执行已完成分支 | ✅ 不重复：只有被中断的节点重跑（`node_runs={hitl:1}`，research/draft 均为 0 次重跑） |
| 5 | **join 语义** | ⚠️ **重大发现**：两次独立的 `add_edge(a,c)`/`add_edge(b,c)` **不是 join**，而是"每条入边触发一次"（实测 sink 执行 2 次）；必须用 `add_edge([a,b], c)` 才是 AND-join（实测 1 次） |
| 6 | 进程重启后 resume | ✅ 可行：换新 checkpointer 实例后 resume 正常（C1） |
| 7 | reducer 语义 | ⚠️ `operator.add` 型字段**无法用 `[]` 清空**，回显自身 state 会**翻倍**（对 reject 语义有直接影响，见 §H） |

### E.1 这个 join 语义在真实图上咬过人（smoke 实测）

第一次投机 wiring 我写成两条边（`add_edge("write_draft_report","human_review")` +
`add_edge("supervisor_subgraph","human_review")`），实测后果：

```
phase1: brief → {seed, draft} → __interrupt__ ‖ supervisor_subgraph   ← HITL 与 research 并行触发
        （human_review 在 draft 完成时就跑了，根本没等 research）
phase2: supervisor_subgraph（**又跑了一遍**） → human_review → ... → __interrupt__  ← 重复副作用 + 二次 HITL
```

修成 `add_edge(["write_draft_report","supervisor_subgraph"], "human_review")` 后：

```
phase1: brief → {seed, draft} → supervisor_subgraph → __interrupt__      ← 真 join
phase2: human_review → claim_verification → final_report_generation      ← 研究不重跑
```

**教训（已写成回归测试）**：并行分支的 join 必须用列表形式；"节点会被执行几次"是 LangGraph
最容易被文档误导的地方，必须以 spike 为准。

---

## F. checkpoint / resume / replay 结论（问题 3 的 4–9）

| 问题 | 结论 |
|---|---|
| worker ownership/heartbeat | 未改动；投机模式下 research 分支仍在同一 worker/claim 内，resume 语义不变 |
| duplicate side effects | **修 join 后为 0**：`supervisor_subgraph` 在两个 phase 合计只执行 1 次（真实图实测 + 测试锁定）。修复前会重跑（已在 §E.1 记录） |
| checkpoint state merge 安全性 | ✅ 两分支写入不同字段（draft_report / supervisor_messages），join 后都可见；**同一字段两分支同时写会被 reducer 合并**（add_messages 会拼成两条）→ 因此投机模式下 draft 分支必须让位（已实现） |
| 进程重启 resume | ✅（spike C1） |
| HITL 等待期间的 worker/task 生命周期 | **与串行模式完全一致**：任务停在 `human_review`，runner 返回 `waiting_review` 并释放 claim（既有语义）；本轮的 A/B 用 auto-approve，等待窗口 ~3–5s |
| cloud/local 调用是否因 interrupt/resume 重放 | ✅ 不重放（次数守恒，见 §J/§K 的调用计数） |

---

## G. HITL metadata 审计（问题 5）

只读 `data/tasks.db`：

| 指标 | 值 |
|---|---|
| task_reviews 总数 | **38** |
| accept | **38（100%）** |
| reject / regenerate | **0** |
| revision 分布 | 无数据（feedback 字段未记录 revise 次数） |
| tasks: completed / waiting_review / cancelled | 37 / 2 / 2 |

**结论：insufficient evidence**。这 38 条全部来自 benchmark/自动 approve 路径
（`run_baseline --review-action approve`），**不能代表真实用户的接受率**；2 个 `waiting_review`
是未被决策的真实等待任务。⇒ 投机研究的**期望浪费无法从现有数据估计**；本轮 A/B 用 auto-accept，
wasted speculative work = **0**（所有投机结果都被接受）。不为此新建 telemetry。

---

## H. Reject / Regenerate 语义（问题 4）

**运行时约束（来自 §E）**：`cancel if still running` **不可用**——interrupt 不会取消在飞兄弟节点，
投机 research 在 HITL 触发时**已经跑完并 checkpoint**。因此只剩"允许完成 + 丢弃/标记 stale"。

**丢弃机制（已验证的约束）**：`notes: Annotated[list[str], operator.add]` —— 返回 `[]` 不能清空
（测试 `test_additive_reducer_cannot_be_cleared_and_echo_doubles` 锁定）。
⇒ 生产实现必须让投机结果落在**独立字段**（如 `speculative_notes`），HITL 批准时再合并进 `notes`；
拒绝时**不合并**，并重新 seed（从修订后的草稿）重跑 research。

**本轮范围内**做的：机制验证（reducer 约束 + 分支结果可分离性）+ 设计；
**未做**：拒绝后的自动重跑（需要 graph 回路，属 productionize 范畴）。
A/B 使用 auto-accept，**未触发 reject 路径**——这是本轮结论的已知边界。

---

## I. A/B workload（问题 7）

4 个查询覆盖 broad / narrow / synthesis / scope-open 四类；**两臂同一批查询**，只切
`DR_SEED_LITE_SPECULATIVE`，其余（模型、thinking、context budget、search、evaluator、
claim verification 并发）全部一致。每臂 **2 轮 × 4 查询 = 8 run**（r1 筛选 + r2 配对确认）。

| id | 类型 | 查询 |
|---|---|---|
| q1 | broad | What is LangGraph and what are its main use cases? |
| q2 | narrow/factual | Which organization maintains the LangGraph library, and what is its current stable major version? |
| q3 | multi-source synthesis | Compare LangGraph, CrewAI, and AutoGen … key architectural differences and trade-offs? |
| q4 | scope-open（易 drift） | How should a small engineering team decide whether to adopt an agent framework like LangGraph …? |

**16/16 run 全部 VALID**（preflight 全绿、fingerprint 前后一致）。驱动脚本
`scripts/experiments/phase5a_ab_driver.sh`（含开关核验：读 worker 进程 environ）。

---

## J. Control vs Treatment 质量（问题 8）

**盲评（`e8_paired_judge`，red_team 独立评委，展示顺序随机化，8 对）**：

| 维度 | ctl 胜 | trt 胜 | tie |
|---|---|---|---|
| **overall** | **4** | **4** | 0 |
| coverage | 1 | **6** | 1 |
| unsupported | 2 | 1 | 5 |
| structure | 4 | 1 | 3 |
| citations | 4 | 2 | 2 |

（评委置信度中位 0.825）

**客观指标（n=8/臂）**：

| 指标 | ctl | trt | 判定 |
|---|---|---|---|
| claim 总数 | 10.0 | 10.0 | 相同 |
| UNSUPPORTED 占比 | **39%** | **38%** | 相同（r1 曾显示 trt 更差，r2 反转为 trt 更好 → **r1 的差异是噪声**） |
| supported-claim coverage | 100% | 100% | 相同（该指标饱和，无区分度） |
| researcher 调用 | 10.0 | 9.5 | 相同 |
| search 调用 | 14.0 | 13.9 | 相同 |
| 独立 URL 数 | 26.0 | 26.6 | 相同 |
| supervisor rounds | 3.0 | 3.0 | 相同 |
| 报告长度 / 引用数 | 混合 | 混合 | 无一致方向 |

**结论：无质量退化**。overall 4:4 打平；研究广度/深度完全一致；unsupported 率一致。
**需要盯的两个子信号**：structure（ctl 4:1）与 citations（ctl 4:2）名义上偏向 control ——
不显著（8 对、且与 coverage 反向），但**应作为 productionize 后的观察项**。

---

## K. Control vs Treatment 性能 / 成本（问题 9）

| query | round | ctl E2E | trt E2E | 配对差 | ctl draft | trt draft | trt research | **overlap（隐藏的 research wall）** |
|---|---|---|---|---|---|---|---|---|
| q1 | r1 | 293.0 | 255.3 | **−37.7** | 63.5 | 125.1 | 78.9 | 78.9 |
| q1 | r2 | 256.0 | 180.9 | **−75.1** | 64.1 | 54.7 | 74.8 | 54.7 |
| q2 | r1 | 243.2 | 176.5 | **−66.7** | 41.3 | 42.2 | 75.6 | 42.2 |
| q2 | r2 | 200.1 | 146.8 | **−53.3** | 29.7 | 27.6 | 66.2 | 27.6 |
| q3 | r1 | 398.4 | 260.3 | **−138.1** | 117.0 | 91.6 | 90.4 | 90.4 |
| q3 | r2 | 318.2 | 278.9 | **−39.3** | 102.5 | 111.1 | 80.8 | 80.8 |
| q4 | r1 | 418.6 | 265.0 | **−153.6** | 91.2 | 124.4 | 72.9 | 73.0 |
| q4 | r2 | 311.0 | 288.1 | **−22.9** | 66.5 | 121.1 | 88.2 | 88.2 |

- **配对差 8/8 全部为负（treatment 更快）**，中位 **−60.0s**；
- 未配对中位：ctl 302.0s → trt 257.8s（−15%，但被轮次漂移污染，不作为主口径）；
- **结构性机制**：treatment 每次 run 都有 `overlap > 0`，且 overlap ≈ **min(draft, research)**
  （27.6–90.4s）→ research **完全藏在 draft 后面**；control 全程 0.0。
- **成本**：云端调用数两臂完全相同（18/run）；4 查询合计估费 ctl 0.731 RMB → trt 0.590 RMB（**−19%**），
  因为 supervisor 输入 token −55%（94445 → 42689，种子比草稿短得多）。
- **wasted speculative work = 0**（8/8 全部 accept）；**restart/replay = 0**（分支节点执行次数均为 1）。
- fully-settled（含 Phase 4C-lite 的记忆落库）：trt 同样比 ctl 快 18–31s。

---

## L. 实际隐藏了多少 draft wall（问题 9 的核心）

**不是**"省掉整个 draft"，而是"**让 research 与 draft 重叠**"：

- control：`brief → draft → HITL → research → cv → writer`（draft 与 research 相加）
- treatment：`brief → max(draft, research) → HITL → cv → writer`（取两者较大者）

⇒ 理论可回收 = **min(draft, research)**；实测 overlap 恰等于该值（8/8），**中位 ≈76s**。
由于 draft（27.6–125.1s）与 research（66.2–90.4s）量级接近，**收益取决于哪一边更长**：
draft 更长时收益=research 全长（q1r1 78.9、q3r1 90.4、q4r1 73.0）；research 更长时收益=draft 全长（q2r2 27.6、q3r2 80.8）。
这也解释了配对差的大范围（−22.9 ~ −153.6s）。

---

## M. Risks / limitations

1. **质量子维度的弱信号**：structure / citations 名义偏向 control（4:1 / 4:2，8 对）——不显著但需在 productionize 后监控。
2. **HITL 位置变了**：审查发生在 research 之后（superstep 屏障的硬约束，见 §E）。若产品要求"先审草稿再研究"，本方案不适用。
3. **reject/revise 路径未实现**：A/B 全为 auto-accept；投机结果的丢弃机制已用测试锁定约束（add reducer 不能清空），但"重新 seed + 重跑 research"的回路仍是设计（§H）。
4. **样本量**：8 对（2 轮 × 4 查询），单轮 4 对会给出误导结论（r1 的 unsupported 差异在 r2 反转）——**这正是本轮引入 r2 的价值**。
5. **合成负载的边界**：查询都是英文技术类；中文/多语言、超长查询未覆盖。
6. **轮次漂移**：同一臂两轮之间 E2E 差 10–20%（云端波动），因此**只能看配对差与结构性 overlap**，不能看跨轮绝对中位。
7. **未做**：多语言/更广 workload、reject 真实路径、多 worker 并发下的行为。

---

## N. Decision: **KEEP CANDIDATE**

| # | 成功标准（§10） | 结果 |
|---|---|---|
| 1 | LangGraph/checkpoint/HITL 语义安全 | ✅ spike + 26 测试；join 用列表形式；resume 不重放 |
| 2 | research_seed 明显小于 draft | ✅ 873 字符 vs 7442–17980（8.5–20.6×） |
| 3 | Accept 路径不再等完整 draft | ✅ 8/8 run overlap>0，research 完全藏在 draft 后 |
| 4 | 无 duplicate/replayed side effects | ✅ 分支执行次数=1（16 run）；云端调用数相同 |
| 5 | 无明确质量退化 | ✅ 盲评 overall 4:4；depth 一致；unsupported 率 38% vs 39% |
| 6 | user-visible E2E 明显下降 | ✅ 配对 8/8 更快，中位 −60s |
| 7 | 无 correctness regression | ✅ 16/16 VALID，overflow/claim loss = 0 |
| 8 | 投机成本/浪费可接受 | ✅ 调用数相同、成本 −19%、浪费 0（全 accept） |
| 9 | Reject 有明确安全方案 | ✅ 语义与机制已验证；回路设计见 §H（未实现） |

**结论：KEEP CANDIDATE —— 可以进入 productionize 设计，但必须先审核本节的 quality caveat（§M-1/2/3）。**

---

## O. 最小 production implementation plan（若审核通过）

**已实现（本轮，flag 默认 off）**
- graph：`write_research_brief → {write_draft_report, supervisor_subgraph}`（同 superstep 真并行）
  → `add_edge([...], "human_review")` AND-join → `claim_verification` → `final_report_generation`
- `research_seed`：在 brief 节点内确定性产出（零 LLM 调用），写入 `supervisor_messages`
- `human_review`：投机模式下 approve → `claim_verification`（串行模式仍 → `supervisor_subgraph`）
- 并发写保护：draft 分支不再回写 `research_brief` / `supervisor_messages`（LastValue 冲突 + add_messages 合并）
- 开关：`DR_SEED_LITE_SPECULATIVE`（**默认 off = 逐字回到当前串行流程，一键回滚**）

**待实现（productionize 阶段）**
1. **state schema**：新增 `speculative_notes: Annotated[list[str], operator.add]` 与 `research_generation: int`；
   投机 research 写 `speculative_notes`；approve 时由 HITL 合并进 `notes`；reject 时**不合并**（旧结果自然被隔离）。
   （*为什么必须新字段*：`notes` 是 add reducer，返回 `[]` 无法清空 —— 测试锁定）
2. **reject/revise 回路**：`human_review` 在投机模式下的 revise → 改写草稿 → 重新 seed → 回到 `supervisor_subgraph`（`research_generation += 1`），
   由 generation 号做 stale fencing；下游只消费最新 generation 的结果。
3. **checkpoint/backward compatibility**：新字段为可缺省（`.get()` 兜底），在途 checkpoint 不受影响；
   开关关闭时字段不写入。
4. **ownership/retry**：research 分支在同一 claim 内执行，语义不变（失去所有权 → 整次 attempt 中止，既有 fencing 覆盖）；
   resume 已验证不重放已完成分支。
5. **测试**：现有 26 个 Phase 5A 测试 + 新增 reject 回路与 generation fencing 测试；
   E2E 验证计划 = 本报告的配对 A/B（每查询 n≥3），质量 gates 沿用 §J 的指标，并把 structure/citations 列为监控项。

---

## P.（若 REJECT）writer 优化建议

本轮不是 REJECT，故不适用。若后续质量信号反转并 REJECT，则按 Phase 4B 的排序直接切到
**final writer 关键路径**（92.4s / 31.8% E2E，其中 ~16s 已被 Phase 4C-lite 移出），
不再回到 GPU/vLLM tuning。

---

## 附：产物与复算

| 内容 | 路径（gitignored） |
|---|---|
| spike 结果 | `artifacts/phase5a/spike.json` |
| A/B 分析（两轮 16 run） | `artifacts/phase5a/ab_analysis_all.json` |
| 盲评 | `artifacts/phase5a/paired_judge{,_r2}.json` |
| 第一版（无 overlap，已废弃）留档 | `artifacts/phase5a/ab_v1_no_overlap.json` |

```bash
bash scripts/experiments/phase5a_ab_driver.sh --arm both [--round r2]
.venv/bin/python scripts/experiments/phase5a_analyze.py --suffixes "" r2
.venv/bin/python scripts/experiments/e8_paired_judge.py --pairs <ctl>:<trt> ...
.venv/bin/python scripts/experiments/phase5a_langgraph_spike.py
```
