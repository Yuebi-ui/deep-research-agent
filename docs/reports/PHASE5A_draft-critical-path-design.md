# PHASE 5A — Draft Critical-Path Removal：设计（**未实施**）

> 生成：2026-10-06 · 依据：真实 graph/code + Phase 4B 三次 E2E 的实测数据 · **本轮不改任何 draft/research 语义**
> 目标：判断 `write_draft_report`（Phase 4B 实测 median **60.1s = 20.4% E2E**，最高 100.2s）能否与 research 主链并行，
> 并给出可执行的质量 A/B 设计。

---

## 0. 现状一句话

`write_draft_report` 是一个**只依赖 research_brief、却排在整个研究之前**的 ~60s 串行云端生成（2500–3000 output tokens），
它的产物同时承担 **① HITL 人工审查对象 ② supervisor 研究种子 ③ final writer 的输入** 三个角色——这是它无法简单挪走的根本原因。

---

## 1. write_draft_report 向哪些后续节点提供哪些字段？

实现：`deep_research/agents/draft_agent.py:65-89`，返回三个字段：

| 字段 | 内容 | 消费者 |
|---|---|---|
| `research_brief` | 原样透传 | supervisor（多处）、final writer prompt |
| `draft_report` | 草稿全文（p1 实测 2625 output tokens） | ① `human_review`；② `supervisor_tools` 的 refine 路径；③ `final_report_generation` |
| `supervisor_messages` | **`["Here is the draft report: " + draft_report, research_brief]`**（2 条纯字符串） | `supervisor` 节点的首轮上下文 |

具体消费点：

1. **HITL（人工审查）** —— `agent_builder.py:123-150`：`interrupt()` 的 payload 带 `draft_report_preview`（前 1000 字）；
   runner 在 `_finish_waiting_review` 把 `task.draft_report` 落库供前端审查。**用户审查的是这份草稿**；
   `action == "revise"` 时用 writer 模型改写草稿并 `Command(goto="human_review")` 回到审查点。
2. **supervisor 首轮上下文** —— `agents/supervisor.py:101-111`：
   `messages = [SystemMessage(MULTI_STEP_DENOISE_PROMPT)] + supervisor_messages`，即草稿全文进入规划上下文。
   实测：系统提示词 1129 tokens，而 p1 第一次 supervisor 调用 input = **4460 tokens** → 草稿（≈2600 tokens）是种子上下文的主体。
3. **研究中的 refine（条件路径）** —— `supervisor.py:275-320`：supervisor 可调用 `refine_draft_report` 工具
   （`supervisor_tools = [ConductResearch, ResearchComplete, _think_tool, _refine_draft_report_tool]`），
   以 `state["draft_report"]` + findings 产出 `new_draft`，紧接着 `evaluate_draft_quality` 打分，写回 `draft_report`。
   **实测：Phase 4A 三次 run 中该工具 0 次触发**（三次 run 唯一 writer-role 调用都在 `final_report_generation`）。
4. **final writer** —— `agent_builder.py:216`：`FINAL_REPORT_PROMPT.format(..., draft_report=state["draft_report"])`。

---

## 2. research seeding 对 draft 的具体依赖

- **无条件依赖（当前实现）**：`supervisor_messages[0]` 是草稿全文；supervisor 的 system prompt 里**没有** brief，
  brief 是靠 `supervisor_messages[1]` 传进去的（`supervisor.py:101`）。所以种子 = 草稿 + brief 两条消息。
- **条件依赖（运行时）**：`refine_draft_report` 需要 `state["draft_report"]` 存在（3/3 run 未触发，但是工具列表成员）。
- **不依赖 draft 的部分**：research brief、tools、red team、claim verification 全链路都不读 draft。

---

## 3. 哪些是真正的语义依赖，哪些只是当前 graph ordering？

| 类别 | 依赖 | 判定 |
|---|---|---|
| **语义（产品可见）** | **S1**：HITL 审查发生在**研究之前**，审查对象是草稿；`revise` 的反馈会改写草稿，再由草稿驱动后续研究 → "用户反馈影响研究"是一条真实产品保证 | 真语义 |
| **语义（数据流）** | **S2**：final writer 的 prompt 需要 `draft_report`（join 依赖，但位置在最后，不影响并行） | 真语义（易满足） |
| **排序/便利** | **O1**：草稿全文进 supervisor 首轮上下文 | 只是 seeding 便利：brief 已在 `supervisor_messages[1]`，去掉草稿不会丢输入；对研究质量的影响是**实证问题**，须由 A/B 回答 |
| **排序/便利** | **O2**：`refine_draft_report` 需要 state 里有 draft | 可加空值保护；且实测 0/3 触发 |

**结论：唯一真正的阻塞是 S1（HITL 相对 research 的位置）。** 若产品接受"审查点后移 / 审查不再改写研究"，
draft 与 research 之间**不存在数据依赖**，可以并行。

---

## 4. 是否可以让 draft 与 research/supervisor 主链并行？

技术上可以（两者都只依赖 brief，draft 用 writer/draft 模型、research 用 supervisor + 本地 researcher，资源不冲突：
draft 是云端调用，不占本地 GPU）。**但在 LangGraph 里有两个必须先验证的实现约束**：

- **I1（interrupt 语义）**：`human_review` 用 `interrupt()` 挂起**整张图**。因此"用户慢慢审查草稿的同时 research 继续跑"
  在单次 graph run 内**做不到**——除非把 `human_review` 与 research 放进同一个 superstep（fan-out）并验证
  LangGraph 对同 superstep 兄弟节点的 interrupt 行为（这是**必须先做 spike 验证**的点，不能假设）。
- **I2（revise 的一致性）**：若 research 已并行跑完，用户的 `revise` 便无法再塑造研究。
  两种可接受的产品语义需要二选一：
  - **P1（推荐先评）**：审查点后移到 research 之后（审查"草稿 + 研究发现"）；`revise` 只作用于 final writer。
  - **P2**：保留"研究前审查"，则不并行 → 无收益。

---

## 5. 没有 early draft 时，research 需要的最小替代 seed

`supervisor_messages = [research_brief]`（去掉草稿那一条），其余不动：

- supervisor 的 system prompt（1129 tokens）已包含完整研究流程指令与工具约束；
- brief 本来就作为第二条消息传入，信息不丢失；
- `refine_draft_report` 路径加"draft 未就绪则跳过"的保护（实测 0/3 触发，影响面小）；
- 可选（若 A/B 显示质量下降）：在 treatment 里加一条**极廉价的 outline 调用**（几百 token）替代 2600-token 草稿作为种子。

---

## 6. 理论最大 critical-path saving

并行后关键路径从 `brief → draft → research → cv → writer` 变为 `brief → max(draft, research) → cv → writer`，
可回收 = **min(draft_wall, research_wall)**（Phase 4B 实测）：

| run | draft | research | min | 占该 run E2E |
|---|---|---|---|---|
| phase4a-p1 | 56.8s | 87.9s | **56.8s** | 18.6% |
| phase4a-p2 | 100.2s | 92.5s | **92.5s** | 28.2% |
| phase4a-p3 | 60.1s | 85.4s | **60.1s** | 21.0% |
| median | 60.1s | 87.9s | **≈60s** | **≈20%** |

**上界 ≈ 60s（20% E2E）**，且这是**目前所有候选里最大的一块**（对比 Phase 4C-lite 的 11–20s）。
另有非量化收益：真实用户的 HITL 思考时间目前 100% 在关键路径上（benchmark 里 auto-approve 只有 2.7–4.7s，
但真实用户可能是分钟级）——P1 方案会把这段也移出关键路径。

**成本/风险**：两条云端调用并发 → 同一 provider 的并发配额与限流；research 失去草稿种子后**可能变长或变差**（须 A/B 量化）。

---

## 7. 质量 A/B 设计

**Arm A（Control）**：现有串行流程，一字不改。

**Arm B（Treatment）**：`write_research_brief → {write_draft_report ∥ research_entry}`，
`research_entry` 的 supervisor 以 `supervisor_messages=[research_brief]` 起步；两分支 join 后再进 cv → writer
（join 点按 §4 的 P1 语义放在 `human_review` 之前或之后，由 spike 结果决定）。

**控制变量（两臂必须完全一致）**：query、模型与 handle、thinking 策略、context budget、
claim_verification、tools、max_researcher_iterations=3、评测集、并发（不要与其他实验混跑）。

**样本量**：每臂 **n ≥ 3**（Phase 4B 显示 E2E 的 run-to-run 波动 15%、draft 本身 56.8–100.2s → n=3 是**最低**要求；
若 min(draft,research) 的差值 < 20s，需要 n≥5 才能分辨）。

**必须同时记录的指标**（复用现有工具，不新造轮子）：

| 用途 | 现成工具 |
|---|---|
| E2E / 分段时间线 / 串行覆盖 | `scripts/run_baseline.py` + `scripts/experiments/phase4b_critical_path.py` |
| research 深度/广度 | `scripts/experiments/e9_analysis.py`（`depth`: supervisor_rounds、supervisor_tools_rounds、researcher_calls/iterations、tool_node_calls、search_calls、unique_urls、evidence_items） |
| 核查与报告质量 | `deep_research/benchmark/quality.py`（verification 分布、supported-claim coverage、citation markers） |
| 独立评委 | `scripts/experiments/e8_paired_judge.py`（red_team 作独立 judge，E8 已验证的成对比较） |
| 质量成对比较 | `scripts/experiments/e8_quality.py` 的 `paired_compare` |

---

## 8. 必须保持的质量 gates

**硬门槛（任一违反 → 该 run 作废，不进统计）**：
1. 正确性：context overflow = 0、claim loss = 0、LLM failed = 0、任务 status=completed；
2. HITL：`approve` 与 `revise` 两条路径都正确（revise 后必须回到审查点、草稿确实被改写）；
3. resume/retry：中断后续跑不重复执行已完成节点；
4. 观测完整性：fingerprint/preflight 通过、run validity=VALID、memory schema compatible。

**非劣性门槛（treatment 不得显著差于 control，逐项对比 n≥3 的中位数）**：
5. claim verification 分布（SUPPORTED+PARTIAL 比例、UNSUPPORTED 数）；
6. 终稿的 supported-claim coverage（`claim_coverage`）；
7. 引用质量：draft 与终稿的 citation markers 数量；
8. research breadth/depth：search_calls、unique_urls、evidence_items、researcher_iterations（**这是本实验最可能退化的项**——
   草稿种子没了，supervisor 的规划可能改变）；
9. 终稿质量：独立 judge 的成对评分（prefer/neutral/worse）+ 报告长度与结构合理性。

**判定**：E2E 显著下降（>噪声带）且 5–9 全部非劣 → KEEP；若 8/9 明显退化 → 需要 §5 的 outline 补偿后再评；
若 E2E 收益落进噪声带 → 不值得改语义。

---

## 9. 本轮不做什么（明确边界）

- **不修改** `draft_agent.py` / `supervisor.py` / `agent_builder.py` 的 draft/research 语义、不改 graph 边；
- **不实现**并行分支，**不先做** interrupt 语义 spike 之外的任何代码改动；
- 不调 thinking 策略、不改模型路由、不动 claim_verification。

**下一步（等审核）**：先做 §4-I1 的 LangGraph interrupt spike（只读实验：验证同 superstep 兄弟节点 + interrupt 的行为），
再决定 P1/P2 语义，然后按 §7 跑 A/B。
