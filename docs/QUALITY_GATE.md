# Quality Gate：研究质量控制

本项目的主图采用 **并行 Draft 与 Researcher + 确定性证据预检 + 语义审核 + 按需对抗审查 + HITL**。质量门控属于一套规则模块，不额外调用用于路由决策的模型。

## 执行结构

```text
User Query → Research Brief + Aspect Checklist
      ├─ Draft ──────────────────────────┐
      └─ Supervisor → Researcher(s) ─────┘
                         │ AND-join
           Python Evidence Precheck ── (缺口且有预算) ──┐
                         │                             │
                     Evaluator                        │
                         │                             │
           Quality Gate Python Router                  │
                  ├─ 高风险 → Red Team → Router        │
                  ├─ 可执行补研 → Targeted Research ───┘
                  └─ HITL Review
                         ├─ revise_draft → 重新预检
                         ├─ more_research → 定向补研
                         └─ approve → Claim Verification → Final Writer
```

## 模块的唯一职责

| 模块 | 负责 | 不负责 |
|---|---|---|
| Research Brief | 形成研究维度清单与覆盖规则 | 宣称清单绝对完整 |
| Researcher | 保留搜索 Provider 实际返回的可定位来源、发布者、摘录和研究代际 | 自行认证事实真实性 |
| Python Evidence Precheck | 遍历**完整**结构化证据，核对维度、来源去重、日期、摘要指纹及最低独立域名数 | 语义真伪判定 |
| Evaluator | 研究论证深度、证据与结论的语义关系、清单外重要遗漏 | 重复统计来源和日期 |
| Red Team | 在高风险或重大逻辑疑点时检验反例、假设和推理漏洞 | 常规完整性复查 |
| Quality Gate Router | 综合证据结果、语义问题、预算、故障及无新增覆盖状态作出确定性路由 | 重新打分或再调用判断模型 |
| HITL | 确认补研、修改草稿或批准研究材料 | 由自动模型代替人工批准 |

`deep_research/evidence.py` 负责结构化证据、维度关联、来源去重与证据清单；`deep_research/quality_gate.py` 负责规则；`deep_research/agent_builder.py` 负责 LangGraph 节点和跳转。

### 证据处理及补研

- 必查维度按 `aspect_id` 追踪；同维度、同来源域名的改写片段或新 URL 不重复计算为独立覆盖。来源去重只代表**来源结构覆盖**，不是事实已经获得验证。
- `targeted_research` 记录新增有效维度来源覆盖数、原始新增记录数、补研次数及原因；补研不带来有效新覆盖时停止无意义的自动循环。
- 自动与人工补研分别受持久化预算约束；LLM 的分数不能覆盖硬规则，超预算时进入人工审核。
- `generation` / `seed_fingerprint` 防止补研前的过期 Finding 混入新任务。只有人工批准才合并隔离区的候选研究。

### 审核上下文及人工界面

`build_review_context()` 分层抽样笔记和来源，优先覆盖必查维度；`context_scope` 记录实际呈现给模型的材料与省略量。Python 预检始终读取全部来源，而不是只检查截断后的上下文。

`build_human_review_summary()` 提供证据缺口、语义问题、Red Team 触发/跳过原因、审核视野、补研历史和剩余预算。人工审核使用 `approve`、`revise_draft` 和 `more_research`，并由 Worker/Checkpoint 恢复执行。

## Quality Gate 评测

评测器：`benchmarks/quality_gate/evaluate.py`。按 `case_id` 对齐研究运行、可选对照运行和独立审阅标签。报告已有的缺口召回率、严重缺口漏检率、无效补研率、Red Team 调用率以及成本与耗时，同时支持下列专项指标：

| 指标 | 计算对象 |
|---|---|
| False Evidence Coverage Rate | 被规则接纳、却被独立审阅判定为证据不合格的维度 / 已接纳维度 |
| Repair Gap Closure Rate | 补研后被独立确认解决的缺口 / 实际进入补研的缺口 |
| Red Team Unique Finding Yield | Red Team 相对已有审核独立新增且经审阅有效的问题数 / Red Team 调用次数 |

新增金标准字段：`coverage_labels`、`repair_gap_outcomes`、`unique_valid_red_team_findings`；运行字段：`covered_aspects`、`repair_target_gaps`、`red_team_findings`。缺少独立标签时输出 `null`，不把缺失值算作通过或失败。

执行示例：

```bash
python -m benchmarks.quality_gate.evaluate \
  --gold benchmarks/quality_gate/fixtures/gold.synthetic.jsonl \
  --candidate benchmarks/quality_gate/fixtures/v3.synthetic.jsonl \
  --baseline benchmarks/quality_gate/fixtures/baseline.synthetic.jsonl \
  --out /tmp/quality_gate_eval.json
```

## 验证入口

```bash
python scripts/test_quality_gate_v3_offline.py -q
python scripts/test_quality_gate_v3_p1p2_offline.py -q
python scripts/test_parallel_only_v3_offline.py -q
python -m pytest -q benchmarks/tests
python scripts/check_repo_hygiene.py
```

完整 LangGraph 端到端、真实搜索来源及 Checkpoint 重启需在安装服务依赖的环境中运行。见 [测试策略](TESTING.md)。
