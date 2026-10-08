# PHASE 3C-2 — E8 FINAL WRITER THINKING A/B REPORT

> 生成：2026-10-06 · 起点 `1d19285` → 终点 `13fa13b`（+ 实验后工具修复 `8c69bcd`）
> 结论：**KEEP**（E8 默认落地在 Phase 3C-3 STEP 1 完成，commit `5ea0b2d`）

---

## 1. Executive verdict

**KEEP** —— Final Writer thinking 默认 OFF（已落地）。
判据：E2E 3/3 对一致下降（median -17.4%）；writer 单调用 3/3 对下降（-33.9%）；reasoning -30%；成本 -17.4%；受控 micro 下质量无差异（6 对 judge：5 tie + 1 弱 ON；claim 覆盖两臂相同）。

## 2. 调用路径审计（决定接线方式的关键事实）

`writer` role 有 **3 个消费者**：
1. `agent_builder.py:33`（final_report_generation + HITL revise）← **E8 唯一目标**；
2. `tools/tool.py:31` → `refine_draft_report`（**research 阶段内被 supervisor 调用**）；
3. `get_chat_model_auto("draft")` 长 query 路由到 writer role（write_research_brief）。

→ 开关必须在**调用点**接线（module 级 import-time 构造），role 层接线会污染 research 路径（违反单变量）。

## 3. 代码/config 变更

- `llm.py`：`writer_thinking()`（env `DR_WRITER_THINKING`，默认 on→后续翻转 off）；`thinking=False → extra_body.enable_thinking=false`；`thinking=True` 不注入任何字段（A 组与历史基线请求体逐字节一致）。
- `agent_builder.py:40`：`writer_model = get_chat_model("writer", thinking=writer_thinking())`。
- `fingerprint.py`：`writer` 进入 `THINKING_ENV_KEYS/DEFAULTS`；`run_baseline.py`：`--expect-thinking KEY=on|off`（可重复）+ 旧 flag 保留别名。
- 冻结期间发现并修复 1 个基建 bug（`59af651`）：runner 的 config fingerprint 误用被剥离的 observation → 永远回退 runner_env → A/B 指纹相同。按规则**中止 A1 并全部重跑**。

## 4. Proof：只有 writer thinking 变

- 接线：spy+reload 证明 import-time 构造带开关；`=off` 时 `enable_thinking=false` 真实进入 `init_chat_model` kwargs。
- 隔离：`refine_draft_report` 与 auto 路由的 writer 构造**均无 thinking 参数**（测试断言）。
- 运行时：6/6 run preflight 读 worker 进程环境（A=writer on / B=off）；llm_calls writer 行 thinking 字段 A="on"/B="off"；B 组 reasoning=None(0)。
- 指纹：A/B `config_fingerprint` 不同（597bf700… vs 4a271ecf…），差异仅在 `thinking.writer`。

## 5. A/B raw table

| run | variant | E2E (s) | final_report_gen (s) | writer lat (s) | writer in/out/rea | total rea | cost RMB |
|---|---|---|---|---|---|---|---|
| e8-a1 | on | 340.0 | 133.3 | 98.3 | 8423/5111/910 | 3738 | 0.1791 |
| e8-b1 | off | 327.9 | 102.7 | 90.2 | 9319/5474/0 | 2393 | 0.1766 |
| e8-a2 | on | 377.8 | 115.2 | 126.9 | 10701/6795/1946 | 5823 | 0.1951 |
| e8-b2 | off | 311.3 | 86.2 | 72.6 | 8004/4613/0 | 2653 | 0.1612 |
| e8-a3 | on | 380.9 | 152.3 | 112.1 | 10362/7023/1431 | 4338 | 0.2016 |
| e8-b3 | off | 311.9 | 92.8 | 74.1 | 9843/3595/0 | 3434 | 0.1550 |

中位数：E2E 377.8→311.9（**-17.4%**）；writer 112.1→74.1（**-33.9%**）；reasoning 4234→2965（-30%）；cost 0.195→0.161（-17.4%）。correctness gates 6/6 全过、VALID 6/6。

## 6. Quality（客观 + judge + micro）

- E2E：coverage 中位 7/7 vs 7/7；unsupported rate 持平；6/6 报告结构完整；重复率 ≤0.025。
- **引用量的上游归因**：draft 引用→final 引用 Pearson **r=0.778**；"citation -41.8%" 是小样本伪影（B1=71 全场最多）。
- 语言不稳定：同一状态两次调用可输出不同语言（a3 生产=英文、6 次 replay=中文）——writer 层固有方差。
- 独立 judge（red_team/flash，位置随机化）：E2E OFF/ON/ON（但理由均为上游内容差异）；**micro（同输入）tie×5 + 弱 ON×1**。
- Writer Micro A/B（2 个固定输入状态 × on/off 各 3 次）：state1 两臂覆盖 7/7（medC=1.0）、latency ON 93.5s vs OFF 83.4s（-10.8%）；state2 8/8、-19.6%；无 claim 丢失。

## 7. 判定推理

- REJECT 条件无一成立；KEEP 条件全部满足（质量无实质下降 + 可重复收益）。
- 未判 INCONCLUSIVE：micro 受控实验消解了上游方差混淆。

## 8. Git commits

```
13fa13b feat(benchmark): E8 quality/performance comparison tooling
9f2f505 test(e8): validate writer-thinking wiring at request and import level
fbfed55 feat(llm): add explicit final-writer thinking policy (E8 switch)
59af651 fix(benchmark): build the config fingerprint from the probe, not its stripped report   ← 冻结期修复，导致 A/B 全部重跑
8c69bcd feat(benchmark): E8 micro/judge harness fixes + experiment ledger entry（实验后）
```

## 9. Invalid/aborted runs

`e8-a1-abort`（任务 `52b3467c789a`，fingerprint bug 暴露后主动中止，停在 waiting_review，无 artifacts，不参与统计）。

## 10. 产物位置

- runs：`artifacts/baseline/e8-{a1,b1,a2,b2,a3,b3}/`
- 分析：`artifacts/experiments/phase3/e8_quality.json`、`e8_micro/`、`e8_micro_a3/`、`e8_judge_{e2e,micro,micro_a3}.json`（不入库）
- 复算：`scripts/experiments/e8_quality.py` / `e8_writer_micro_ab.py` / `e8_paired_judge.py`

## 11. Remaining risks / E9 建议

- 样本量小（3/臂）、组内方差大；E2E judge 2/3 偏向 ON 属上游混淆（micro 与之冲突）。
- writer 输出语言漂移影响跨 run 长度/引用比较。
- E9 建议：draft 在上游、风险级联，必须把 research depth / search fan-out 作为一票否决项；micro 只作辅助。
