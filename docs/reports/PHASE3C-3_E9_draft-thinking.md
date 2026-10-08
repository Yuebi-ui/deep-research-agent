# PHASE 3C-3 — E8 LANDING + E9 DRAFT THINKING A/B REPORT

> 生成：2026-10-06 · 起点 `8c69bcd` → 终点 `2ba326a`
> 结论：E8 落地 **完成**（`5ea0b2d`）；E9 **KEEP**（默认翻转在 Phase 4A STEP 1 完成，`053cb18`）

---

## 1. Executive verdict

| 项 | 结论 |
|---|---|
| E8 landed? | **YES**（`5ea0b2d`，writer thinking 默认 OFF，override 保留） |
| E9 verdict | **KEEP**（研究行为等价 + 质量无实质下降 + draft 延迟收益可重复） |

## 2. E9 调用路径审计（§9）

| 问题 | 答案 |
|---|---|
| write_draft_report | `draft_agent.py:62`（agent_builder 直接导入使用） |
| draft model | **模块级 import-time** `draft_model = get_chat_model("draft")`（draft_agent.py:32） |
| 是否经 get_chat_model_auto | **否**——role "draft" 直达；auto 路由只存在于 `write_research_brief`（本 query → evaluator/flash，**另一个 logical role**） |
| 其它复用 | 全仓库无（role "draft" 仅此一个构造点） |

→ 开关绑定 logical role=draft 的唯一调用点；brief 路由与 `refine_draft_report` 两臂恒定。

## 3. 代码/config 变更（E9 switch）

- `llm.py`：`draft_thinking()`（env `DR_DRAFT_THINKING`，E9 未决期默认 on）；
- `draft_agent.py:32`：`draft_model = get_chat_model("draft", thinking=draft_thinking())`；
- fingerprint/run_baseline：`draft` 进入开关清单与 `--expect-thinking`；
- 冻结 revision `0966d5e`；实验后工具修复 `2ba326a`（打印 None 崩溃）。

## 4. Proof：只有 draft thinking 变

- capture 测试：off → `extra_body.enable_thinking=false`；import-time 接线 spy+reload 验证；
- **不泄漏**：writer（显式 on 时仍 on）、auto 路由、refine_draft_report、supervisor、claim-verify（测试断言）；
- 运行时：6/6 run preflight 读 worker env（A=draft on / B=off）；llm_calls draft 行 thinking A="on"/B="off"、B 组 reasoning=None(0)。

## 5. A/B raw table（experiment_id=draft-thinking-e9，交错 A1→B1→A2→B2→A3→B3）

| run | variant | E2E (s) | draft 节点 (s) | draft rea/out | draft 字/节/引 | sup/iter/tool/srch | uurl/evid/claims | final 引/覆盖 | cost |
|---|---|---|---|---|---|---|---|---|---|
| e9-a1 | on | 312.3 | 95.4 | 1066/4596 | 6957/9/26 | 3/10/8/14 | 30/10/10 | 30/10-10 | 0.1664 |
| e9-b1 | off | 317.5 | 89.0 | 0/4434 | 9839/7/11 | 3/10/8/14 | 27/10/10 | 39/9-9 | 0.1796 |
| e9-a2 | on | 295.0 | 77.1 | 641/3820 | 6230/8/7 | 3/10/8/14 | 29/10/10 | 43/8-9 | 0.1551 |
| e9-b2 | off | 272.2 | 68.0 | 0/3040 | 6285/6/6 | 3/10/8/14 | 24/10/10 | 32/9-9 | 0.1418 |
| e9-a3 | on | 288.6 | 90.8 | 88/4491 | 9595/8/26 | 3/**8/6/13** | 25/10/10 | 46/6-6 | 0.1660 |
| e9-b3 | off | 278.5 | 60.5 | 0/2894 | 6102/7/38 | 3/10/8/14 | 30/10/10 | 50/8-8 | 0.1459 |

中位数：**E2E 295.0→278.5（-5.6%，2/3 对）**；**draft 节点 90.8→68.0（-25.1%，3/3 对）**；draft output -32.3%、reasoning→0；总 reasoning -21.7%；cost -12.1%。

## 6. Research depth 硬护栏（本实验最重要的部分）

supervisor rounds 3/3；researcher calls 10/10；iterations 10/10（唯一 8 在 **ON 侧 a3**）；tool_node 8/8（唯一 6 在 ON 侧）；search 14/14（唯一 13 在 ON 侧）；evidence 10/10 全 run；claims 10/10。
**→ 无深度收缩；OFF 侧全部 ≥ ON 侧**（§11 担心的"靠少研究换延迟"未发生）。

## 7. 质量

- coverage：A=100%/89%/100%，B=100%/100%/100%；unsupported rate median 持平（0.1）；报告结构完整。
- 独立 judge（red_team）：**OFF/OFF/ON**（2/3 偏 OFF；reasons 涉及内容差异，E2E 层仍有上游混淆）。
- Draft Micro（固定 brief，3+3）：latency ON 78.1s vs OFF 68.9s（-11.8%，分布重叠）；**topic 重叠全向低**（within-ON 0.021 / within-OFF 0.103 / cross 0.067）→ draft 大纲的 run 间随机性远大于 thinking 效应。

## 8. 链路归因（§21）

变化**第一次且唯一一次**出现在 **draft 步**；research 链完全等量；最终报告差异在噪声内——理想 KEEP 形态。

## 9. Git commits

```
2ba326a fix(benchmark): E9 analysis summary printer handles None medians
0966d5e feat(benchmark): E9 research-depth and draft micro A/B tooling   ← E9 冻结 revision
59ea51b feat(draft): explicit draft thinking policy (E9 switch)
5ea0b2d perf(writer): adopt thinking-off as accepted default             ← E8 landing
```
diff `8c69bcd..HEAD`：15 files, +959 / -35。

## 10. Invalid/aborted runs

**无**（6/6 一次通过）。

## 11. 产物位置

- runs：`artifacts/baseline/e9-{a1,b1,a2,b2,a3,b3}/`
- 分析：`artifacts/experiments/phase3/e9_analysis.json`、`e9_quality.json`、`e9_judge_e2e.json`、`e9_micro/`（不入库）
- 复算：`scripts/experiments/e9_analysis.py` / `e9_draft_micro_ab.py` / `e8_paired_judge.py`

## 12. Remaining risks / 下一阶段建议

1. E2E 样本小、噪声 ±20s；draft 步收益（-25.1%，3/3）比 E2E（-5.6%）可信度高一个量级。
2. draft 章节数 ON 侧略高（E2E 3/3、micro 28 vs 23）——唯一反向线索，但 topic 重叠证明随机性主导。
3. 语言漂移、单一 benchmark case。
4. 建议：Phase 4 先做 E9 落地；hybrid routing 已有 role-level profile 可作对照起点（当时观察到本地 summarizer 累计 282s、云端 evaluator 13 次/run）。
