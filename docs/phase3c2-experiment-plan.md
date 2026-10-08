# Phase 3C-2 实验设计（E8 / E9）——设计稿，未执行

> 状态：**DRAFT，未运行任何实验**。Phase 3C-1（memory correctness + benchmark
> hardening）通过后由人工审核本设计，再决定是否执行。
>
> 硬约束（沿用 Project 约定）：GPU 模式 / vLLM / hybrid local+cloud / 本地
> max context = 8192（不得靠放大窗口隐藏问题）。全部实验经
> `scripts/run_baseline.py`（含 preflight + run fingerprint + freeze 检测）执行，
> run 必须 `VALID` 才能进入统计。

---

## 0. 两个实验共同的前置改造（代码，属于 3C-2 执行）


| # | 改造 | 位置 | 说明 |
|---|---|---|---|
| 1 | `writer_thinking()` 开关（env `DR_WRITER_THINKING`，默认 on） | `deep_research/llm.py`（沿用 E1a/E1b 模式） | `agent_builder.py:33` 的 `writer_model = get_chat_model("writer")` 是**模块导入期**构造，需在此处接入 `thinking=` |
| 2 | `draft_thinking()` 开关（env `DR_DRAFT_THINKING`，默认 on） | `deep_research/llm.py` | `agent_builder.py:66` `get_chat_model_auto("draft", query_text=...)` —— draft 可能被路由到 **writer(deepseek-v4-pro) 或 evaluator(deepseek-v4-flash)** 两个 handle，开关必须作用于解析后的调用点 |
| 3 | preflight thinking 期望泛化 | `deep_research/benchmark/preflight.py` + `scripts/run_baseline.py` | 现在是 `--expect-claim-verify-thinking/--expect-supervisor-thinking` 两个固定参数；改为可重复的 `--expect-thinking KEY=on\|off`，`THINKING_ENV_KEYS` 增加 `writer`/`draft`（config fingerprint 自动跟随） |
| 4 | 质量指标采集脚本 | `scripts/experiments/`（复用 `e1a_micro_quality.py` 的风格） | 从 `tasks.db.final_report` / `draft_report` 计算：报告长度、引用数、唯一来源数、section 数、verified-claims 被表达数；受控微实验（同一 draft/findings 分别喂 thinking on/off）评分 |

**注意（E8 的隐性影响面）**：`writer_model` 同时服务 `final_report_generation`
（astream）与 `human_review` 的 revise 路径——writer thinking off 不只影响最终
报告生成。Benchmark 固定走 `approve`，但结论里必须声明该开关的完整影响面。

---

## 1. E8 — Final Writer Thinking OFF

**假设**：`final_report_generation`（Phase 3A profiling 的最大 critical-path
瓶颈）中的云端 thinking 是纯延迟成本，关闭后报告质量不降。

**变体（两个独立 run 组）**：

| 组 | `DR_WRITER_THINKING` | 其它 |
|---|---|---|
| A（对照） | on（现状） | supervisor on、red_team on、draft **on**、extractor off、judge off、其余全部相同 |
| B（实验） | off | 同 A |

**执行参数**：

```bash
# A
.venv/bin/python scripts/run_baseline.py --run-id e8-a-N \
  --experiment-id writer-thinking-001 --variant thinking-on  --experiment-kind experiment \
  --expect-thinking writer=on
# B
.venv/bin/python scripts/run_baseline.py --run-id e8-b-N \
  --experiment-id writer-thinking-001 --variant thinking-off --experiment-kind experiment \
  --expect-thinking writer=off
```

**样本量建议**：每组 ≥ 3 个 VALID run（沿用 E1a/E1b 的节奏；两组合计 ≥ 6 次）。

**测量（全部来自 artifacts，不新增隐式埋点）**：

* 延迟：`node_metrics.final_report_generation`；`llm_calls` 中 writer 行 duration
* reasoning tokens：`llm.by_role_reasoning.writer`（E1a 后 writer 是 reasoning
  大户；smoke run 中 writer=1576，占全部 reasoning 的 ~44%）
* output tokens：`llm.by_role.writer` 对应行
* 报告质量：长度 / 引用 `[n]` 数 / 唯一 URL 数 / section 数 / verified claims
  被表达比例（`verification` JSON 与 final_report 交叉）
* 事实一致性：`verification_report`（supported/partial/unsupported、
  hallucination_rate）；E8 不能只看速度——**若 unsupported 显著上升或引用/证据
  覆盖下降 → REJECT**

**预注册判定规则（先写后跑，防事后解释）**：

1. 任一质量指标出现实质退化（引用来向的 claim 覆盖下降、unsupported 中位数
   上升 ≥ 2、报告长度下降 > 20%）→ **REJECT**，无论快多少；
2. 质量不降且 `final_report_generation` 中位延迟下降 ≥ 15% → **KEEP**；
3. 介于两者之间（质量持平但延迟下降 < 15%）→ 记录，暂不 KEEP（低收益高风险）。

---

## 2. E9 — Draft Thinking OFF（仅在 E8 完成后）

**假设**：draft 阶段（`write_draft_report`，Phase 3A ~92.7s）的 thinking 关闭
不损害下游 research 的输入质量（draft 会作为 supervisor 后续研究的起点）。

**变体**：

| 组 | `DR_DRAFT_THINKING` | 其它 |
|---|---|---|
| A（对照） | on | 与 E8 之后的 accepted 状态完全一致 |
| B（实验） | off | 同 A |

**重点观察**（与 E8 不同——draft 是**上游**，风险是级联的）：

* `write_draft_report` 延迟与 reasoning tokens；
* 下游行为变化：supervisor research steps 数、搜索 fan-out（`search_metrics`）、
  不变量（Phase 3B 的 10 steps / 14 searches 参考值）；
* 最终报告质量同上（长度/引用/唯一来源/section/verified claims）；
* `research_brief` 内容质量的受控微对照（同一 query 下 draft on/off 的
  brief 结构完整性）。

**判定规则**：与 E8 相同的「质量优先」原则；额外一票否决项——若 research
depth 或 search fan-out 低于历史不变量（10/14）→ **REJECT**（E1b 的教训）。

---

## 3. 与 Phase 3C-1 基建的配合

- 每组 run 的 `run.json.experiment` 携带 `experiment_id/variant/kind`，
  `integrity.validity` 必须为 `VALID`；
- preflight 的 `thinking_policy` 检查（读 worker 进程环境）保证 A/B 两组的
  worker 真的是不同配置——E1a 期间「忘记重启服务」的事故由基建兜底；
- 任何 run 期间改代码/改配置 → `INVALID_*`，不得进入统计。
