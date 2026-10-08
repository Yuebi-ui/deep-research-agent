# PHASE 5B — SEED-LITE SPECULATIVE RESEARCH PRODUCTIONIZATION

> 生成：2026-10-07 · 起点 `b7095af`（Phase 5A KEEP CANDIDATE）
> 产品语义（已审核）：**保留 HITL；Research 允许在 HITL Accept 前 speculative 执行；
> 只有被 ACCEPT 的当前 generation 的结果可以进入 downstream。**
> **最终决定（2026-10-07 人工审核）：`DR_SEED_LITE_SPECULATIVE` 默认翻 ON ——
> 投机拓扑即生产默认路径；显式 `=off` 一键回滚。**
> 决策背景（如实记录）：扩大配对验收因外部搜索配额（Tavily 套餐上限）耗尽未跑完
> （详见 §M–R）；决策人审核了现有证据（5A 16-run A/B + 5B 真实 E2E 冒烟 + 722 测试）
> 后批准定版。剩余 25-run 验收作为**上线后复核**待配额恢复补跑，不构成回滚 gate。

---

## A. Repository state / commits

| 项 | 值 |
|---|---|
| start HEAD | `b7095af`（Phase 5A，工作区 clean；full suite 685 passed / 1 skipped） |
| commit 1 | `88582ae` feat(research): productionize —— reject/regenerate 回路 + generation fencing |
| commit 2 | `816b142` test(research): Phase 5B 回归矩阵（+36 测试，full suite 721 passed / 0 failed） |
| commit 3 | `c66970b` feat(benchmark): Phase 5B A/B 驱动 + run_baseline 多步 review 序列 |
| commit 4 | `2fbe1ea` fix(research): lineage 标记原子提交（发现并修复真实 bug，含回归测试） |
| commit 5 | `7f61fd0` + `f64f363` benchmark 工具（analyzer --prefix / 配对汇总脚本） |
| commit 6 | （定版）feat(research): 投机拓扑翻为生产默认（本文档同批提交） |
| end HEAD | 见 git log（report commit 为 5B 最后一个提交） |

已知提交核对：`c8215b5`（4B）、`22d77bb`（4C-lite）、`b7095af`（5A）全部存在且为 HEAD 祖先。

## B. Repository audit（与 handoff 无冲突）

- HEAD = `b7095af`，`git status` clean；三个已知 commit 全部在且顺序正确。
- `DR_SEED_LITE_SPECULATIVE` 默认 **off**（**审计时状态**：`research_seed.py`
  缺省 "off"，与 handoff 一致；定版翻转见 §K/§T）。
- full suite 基线：**685 passed / 1 skipped / 0 failed**（实测，58s）。
- 服务运行面：vLLM(8001) / Redis(6379, db0) / API(8000) / worker 均在跑；
  worker 需持久化 checkpointer（redis），与 runner 的 resume 语义一致。
- 生产 tasks.db 中 3 个历史 waiting_review 任务（"Cancel me" 等，均为**串行
  lineage**）：worker 重启时 reconcicler 会重新入队，runner 走 resume 分支时
  因无新 review 决策而直接回到 waiting_review —— 无执行、无害（实测确认后不影响 benchmark）。

## C. Graph before / after（行为已锁定；注意 LangGraph 的 `get_graph()` 对
Command 路由不完整，拓扑以实测行为 + 测试为准）

**串行（flag off，历史行为，逐字不变）**

```text
START → write_research_brief → write_draft_report → [human_review] → supervisor_subgraph
      → claim_verification → final_report_generation → END
  human_review: approve --Command--> supervisor_subgraph ; revise --Command--> human_review
```

**投机（flag on，Phase 5B）**

```text
START → write_research_brief ─┬→ write_draft_report ─────────────┐
                              └→ supervisor_subgraph(gen N) ──────┴─(AND-join)→ human_review
                                                                                 │
        human_review --approve(Command, fenced)--> claim_verification → final_report_generation → END
        human_review --revise(Command)--> regenerate_research(gen N+1) → human_review（回环，再次审查）
```

**投机无 HITL（离线/测试构建）**：join → `accept_research`（fenced auto-accept）→
claim_verification → …

关键约束（全部实测，见 `scripts/experiments/phase5b_topology_spike.py`）：

1. seed 必须在 `write_research_brief` 节点内产出（任何中转节点都会把 research 推
   到下一个 superstep —— superstep 是屏障，overlap 归零，5A 已实测）；
2. join 必须 `add_edge([a, b], c)`；**join 入边 + 独立回边指向同一目标**在
   langgraph 1.2.12 上按轮次各触发一次（第一轮 join 触发 1 次；此后每次
   regenerate 完成再触发 1 次）—— 回环不需要新的审查节点名，`next` 恒为
   `("human_review",)`，runner 的 resume 判定零改动；
3. `human_review` **不得有静态出边**（静态边覆盖 `Command(goto)`，见
   tests/test_graph_wiring.py）：5A 版本在投机构建里恰好带着
   `human_review → claim_verification` 静态边，意味着当时的 revise 分支
   实际会被覆盖、直接跳进 downstream —— 本阶段已移除，approve/revise 全部由
   Command 路由（这是 5B 修的 5A 潜在缺陷之一）。

## D. HITL 语义（明确定义）

**动作名称映射**：产品 API/前端只有 `approve` / `revise` 两个动作；mission §5 的
"REJECT/REGENERATE" 在本产品中即 **revise**（统一语义：不做 revision 意图分类，
一律作废当前 generation 并重跑研究）。

| 事件 | 语义 |
|---|---|
| Research 在 Accept 前执行 | **允许**（speculative execution）；结果写入 `speculative_research`，与 `notes` 隔离 |
| HITL ACCEPT（approve） | fence 校验（`payload.generation == active_generation`）通过后，当前 generation 的研究结果**恰好一次**并入 `notes` → claim_verification → writer；不重跑研究 |
| HITL REJECT/REGENERATE（revise） | 当前 generation 全部作废：改写草稿（既有产品语义）→ 重建确定性 seed → `research_generation += 1` → 重跑研究 → **再次**进入 HITL；旧 payload 被新一代覆盖，永不进入下游 |
| 审查时机 | 审查发生在 speculative research **之后**（superstep 屏障的硬约束，5A 已与产品确认为接受的语义变化） |
| 下游消费 | claim_verification / final writer / 记忆落库只看到**被 ACCEPT 的当前 generation** 的 notes（`final_report_generation` 原样读 `notes`，代码未改） |

## E. Research generation / fencing 设计

- **generation identity**：`research_generation: int`（LastValue 通道，AgentState 与
  SupervisorState 同名）。`>=1` ⇔ 投机 lineage；串行 lineage 从不写（缺省 0）。
  初值在 `write_research_brief`（投机起点）写 1；`regenerate_research` 每次 +1。
- **结果绑定**：`speculative_research = {generation, seed_fingerprint, notes}`
  （内容由 `quarantine_update` 构造；不含研究正文以外的敏感字段）。
- **唯一消费点**：`fenced_accept_update`（human_review approve / accept_research）：
  `payload.generation != active_generation` → `ResearchFenceError`（**显式失败**，
  retry 分类为不可重试领域错误 `RESEARCH_FENCE_ERROR`）—— 绝不用空/旧结果静默继续。
- **为什么不用 env 判 lineage**：开关只决定**新** lineage 的起步；在途 lineage 的
  语义由 checkpoint 事实决定。runner 在 resume 前直接读 checkpoint 的
  `channel_values.research_generation` 选择拓扑 —— 开关中途翻转（回滚）不会让
  在途任务跑错语义（见 §K）。
- **为什么独立字段**：`notes` 是 `operator.add` reducer —— `[]` 不能清空、回显
  自身 state 会翻倍（5A 测试锁定）。隔离字段是唯一不依赖 reducer 清除语义的方案。
- **stale 覆盖的最终防线在读侧**：即使一个旧 generation 的写晚到并覆盖了隔离字段
  （`update_state` 注入实测），ACCEPT 时的 generation 校验也会拒绝它 ——
  fencing correctness 不依赖"旧执行被取消/不会写"。

## F. Reject / Regenerate 状态机（V1，保守）

```text
speculative lineage, generation N
  ├─ ACCEPT  → fence(N) → notes += payload_N.notes → claim_verification → writer → END
  └─ REVISE  → 改写草稿(D2, writer 模型) → regenerate_research:
                 gen = N+1
                 seed S(N+1) = 确定性投影(research_brief)   ← 零 LLM 调用
                 子图以**干净输入**重跑（messages/iterations/quality/critiques 全清零）
                   └→ 产出 payload(N+1)（隔离）→ 回到 HITL（审查 D2 + R(N+1)）
```

- **不做** revision 意图分类（不区分 writing/scope change），统一"重跑研究"——
  宁可 rare reject 路径多付一次研究成本，不引入新的 correctness 风险。
- **草稿改写与串行一致**（writer 模型 + 反馈）；**新一代 seed 仍是 brief 的确定性
  投影**（不注入修订稿）——与串行语义对齐：串行模式下研究种子同样来自原始草稿，
  修订稿只通过 writer/refine 路径影响最终产出。
- 研究途中 `refine_draft_report`（低触发率路径）在 gen≥2 保持可达（draft 存在），
  若触发则改写后的草稿回写父图，审查对象 = 最终被使用的对象。
- 崩溃重放安全：regen 节点整体重放（seed 确定性、输出隔离、只在节点完成时提交），
  generation 号从 checkpoint 重算，不产生双增。

## G. 外部副作用审计（投机分支）

| 副作用 | 位置 | 分类 | 说明 |
|---|---|---|---|
| 云端 LLM 调用（supervisor/researcher/red_team/evaluator/refine） | 投机研究分支 | **B. 可重复** | 只产生 token 计费；输出全部进 state，被 fencing 覆盖/拒绝；无外部写 |
| 本地 LLM 调用（vLLM researcher） | 同上 | **B. 可重复** | 同上 |
| 搜索工具调用（tavily 等） | researcher 子图 | **A. 只读** | 读外部网页；无外部写；provider/client 只有**进程内 client 缓存**（无结果落盘） |
| Checkpoint 写入（Redis） | 各 superstep | **D. generation-fenced** | 隔离字段 + ACCEPT 校验；旧 generation 写入不能成为下游输入 |
| 记忆落库（Chroma） | `post_completion`（runner） | **E. 受既有约束保护** | **不在**投机分支内：只在任务 completed 后由 runner 触发（Phase 4C-lite），且只消费最终报告 |
| 报告前记忆检索（embedding 云调用） | `write_research_brief` | **A. 只读** | 两臂同样发生（不是投机特有） |
| 任务/审查 DB 行 | API/runner | **B. 可重复** | 审查行只在用户动作时创建；runner 落库只在终局 |
| 遥测（baseline_metrics / Redis 事件 / 日志） | 观测 | **B. 可重复、append-only** | 只增不改；分析口径按 run_id/attempt 区分，不构成业务状态 |
| 磁盘 artifacts（artifacts/、data/baseline_metrics/） | 观测 | **B. 可重复** | 同上 |

**结论：投机分支不存在"不可 rollback、不可 fence、且会改变业务语义"的副作用。**
Reject 的唯一损失 = 被作废 generation 的 LLM/搜索**成本**（见 §R），无状态污染。

## H. Checkpoint / resume 行为（实测）

- 投机 lineage 每轮等待时 `snapshot.next == ("human_review",)`（spike + 图级测试
  逐轮断言）—— runner 既有的 `next == ("human_review",)` resume 判定**零改动**兼容。
- regen 中途死亡：checkpoint 停在 `next == ("regenerate_research",)`（草稿改写已
  提交、generation 未推进、旧 payload 原样）；新进程以 None 输入续跑，**只重放
  regen 节点**（gen1 分支不重放）→ 收敛到第二次 HITL（图级 + runner 级 + 真实
  Redis/SQLite 跨 checkpointer 实例实测）。
- gen1 研究中途死亡：整个失败的 superstep **不提交任何兄弟写入**（实测：与
  interrupt 不同 —— interrupt 会提交已完成兄弟）→ 续跑时 draft/research 各重放
  一次（浪费一次 draft 调用，无下游重复）。
- 跨进程恢复依赖持久化 checkpointer（生产 = Redis），与既有语义一致；resume 前
  runner 读 `channel_values.research_generation` 决定拓扑（跨实例实测）。
- 优雅关停（SIGTERM）：worker 等**当前 attempt 跑完**再退出（既有语义，未改动；
  HITL 等待点正常释放 claim）。若进程被强杀（或任务运行中被取消）：在途 attempt
  无终态/无部分提交，claim 过期后由 recovery 以 None 输入续跑 pending 节点 ——
  与 #9 同一恢复路径（asyncio 取消模拟实测：重跑收敛、无污染）。

## I. Failure matrix（§8 的 11 行 × 证据）

| # | 场景 | 结论 | 证据 |
|---|---|---|---|
| 1 | normal ACCEPT | ✅ 研究结果恰好一次并入 notes；下游各节点恰好一次 | 图级 test_accept_*、runner test_flag_on_*、真实 E2E 冒烟（node counts / checkpoint 逐字比对） |
| 2 | REJECT → 新 generation → ACCEPT | ✅ 只有 gen2 进入下游；分支不重跑 | test_reject_regenerates_*、runner test_reject_then_accept、真实 E2E 冒烟（regenerate=1, draft=1, notes==payload2） |
| 3 | REJECT → REJECT → ACCEPT | ✅ generation 3；regen×2；陈旧两代均被拒 | test_reject_reject_accept |
| 4 | draft 完成 / research 仍在跑 | ⚠️ 该中间态在 runtime 不可观察（superstep 原子提交）；等价失败语义 = 整 superstep 重放（#9） | test_restart_mid_initial_research |
| 5 | research 完成 / draft 仍在跑 | 同上（原子性） | 同上 |
| 6 | 两分支完成 / join 前 | 同一 superstep 边界内，不可外部观察；等价于 #7 | 同上 |
| 7 | join 完成 / HITL 待审 | ✅ `next==("human_review",)`、notes 为空（隔离证明） | 全部等待点断言 + runner test |
| 8 | HITL 待审期间进程重启 | ✅ 新 runner/新 graph 实例 resume approve 正确 | runner tests（含新 checkpointer 实例） |
| 9 | 投机研究期间进程重启 | ✅ gen1：整 superstep 重放一次；regen：只重放 regen | test_restart_mid_*、test_process_restart_mid_regenerate |
| 10 | 投机研究期间优雅关停 | ✅ SIGTERM：attempt 跑完后退出（无打断，既有语义）；运行中被取消/强杀：无终态/无部分提交，重跑收敛 | test_cancel_mid_speculative_run_then_resume |
| 11 | worker 失败后 retry/resume | ✅ 不重复已完成分支；fence 错误不重试（显式失败） | 同 8/9 + retry 分类单测 |

## J. Late stale result 证据（hard gate）

两类顺序都用 `aupdate_state(as_node="regenerate_research")` 在真实图上注入（= 陈旧
执行者晚到写），全部实测通过：

1. **R1 在 R2 完成后到达**（覆盖当前隔离字段）：ACCEPT 抛 `ResearchFenceError`；
   claim_verification / final_report_generation **零执行**；notes 保持为空、无
   final_report —— 陈旧结果无法覆盖、无法并入、无法触发下游
   （`test_late_stale_after_r2_is_fenced_out`）。
2. **R1 在 R2 运行前/期间到达**（将被 R2 完成写覆盖）：最终 ACCEPT 只认 R2 的
   payload，注入的 `STALE-R1` 内容不出现在任何位置（`test_stale_write_before_r2_is_superseded`）。
3. reducer 无重复：`notes` 与 payload **逐字相等**（非包含），且完成后重放
   （再次投喂）为 no-op（`test_replay_after_completion_is_noop`）。

真实 runtime 对照：reject 冒烟（q2）最终 checkpoint `notes == payload(gen2).notes`
（3/3 条逐字相等）。

## K. Feature flag / rollback 证据

- flag 不变（`DR_SEED_LITE_SPECULATIVE`），**不引入新 framework**；
  **定版默认值 = on**（`research_seed.SEED_LITE_DEFAULT`，2026-10-07 人工审核后翻转），
  显式 `=off` 一键回滚。
- **ON（默认）**：投机拓扑（上述全部证据）。
- **OFF（回滚）**：新任务 = 串行拓扑与语义逐字不变（runner
  test_flag_off_new_task_is_serial 断言无 generation 标记、草稿照常写
  supervisor_messages、approve 后研究才跑；test_default_topology_unchanged）。
- 定版翻转同步更新：conftest 把测试环境钉为 off（测试确定性，两条路径各有显式
  覆盖）；两个 A/B driver 的 control 臂改为**显式** off（默认翻转前依赖
  unset=off 会得到两臂同语义的假结果）；benchmark 指纹白名单加入本开关
  （`FEATURE_FLAG_KEYS`，值 None = 未设置 → 解析为默认 on）。
- **回滚语义（明确记录）**：开关只决定**新** lineage 的起步。在途 lineage 由
  checkpoint 标记决定 resume 拓扑 —— 中途翻转（双向）都实测正确：
  - 投机 lineage + env off 恢复 → 仍按投机拓扑（join → HITL），不会跳过审查/空 notes；
  - 串行 lineage + env on 恢复 → 仍是串行语义，不会被升级。
  - **无需 state migration、无需危险操作**。
- **标记的原子性（回归修复）**：`write_research_brief` 在**两种**模式下都原子提交
  标记（投机=1 / 串行=0）。标记**缺失** = 未绑定（崩溃发生在 brief 提交之前）→
  拓扑按 env（即将重跑的 brief 读同一 env，写下一致的标记）。曾实测的坏组合：
  runner 见"有 checkpoint 无标记"建串行拓扑、brief 却按 env 起投机 lineage →
  研究分支完全跳过、直接停在审查点（approve 被 fence 拒绝 → 必然失败）——
  已用确定性回归测试锁定（test_crash_before_brief_commit_resumes_consistently）。
- **已知边界（记录，不处理）**：**5B 之前**产生的在途 checkpoint 没有标记且
  brief 已提交（env ON 下会按投机拓扑恢复 → 与串行 lineage 的状态不匹配 →
  响亮失败，不会静默污染）。仅存在于 5A 实验期的 benchmark 历史任务；生产开关
  从未打开。建议：翻转开关前确认无此类遗留任务（或先 cancel）。

## L. 测试结果

| 层 | 结果 |
|---|---|
| 新增 targeted（5B） | 36 个：cycle 语义 2、fencing 单测 19、真实图 reject/stale/重放 8、runner/lineage/重启/取消 7 |
| 相关集成（5A/离线图/wiring/runtime） | 全部通过（43 + 16 + …） |
| **full suite** | **721 passed / 1 skipped / 0 failed**（65s；基线 685+36） |
| ruff | 变更文件全部通过 |

## M–R. 扩大配对质量 / 性能 / 成本 —— **未完成（外部资源阻断，如实记录）**

**设计**（预注册）：8 查询（q1–q4 与 5A 相同 + z1–z4 中文，取自
tests/eval_dataset.json）× 2 轮 × 2 臂 = 16 pairs；控制变量与 5A 相同；
盲评 = red_team 独立评委、顺序随机化；口径与工具复用 5A（analyzer/judge/汇总脚本）。

**执行实况**：

- r1 跑到一半时 **Tavily 套餐搜索配额耗尽**（首个错误 02:29:38，
  `This request exceeds your plan's set usage limit`），此后所有 run 的
  research/claim-verification 搜索全部失败。
- 逐 run 核对搜索健康度后的结论：
  - **干净（14/14 搜索成功）**：ctl r1 × {q1,q2,q3,q4,z1,z2,z3}（7 run，全部 VALID）；
  - **污染（0/12 搜索成功，不可用于任何推断）**：ctl z4-r1、**treatment r1 全部 8 个**、
    r2 已跑的 2 个 —— treatment 臂**零干净 run**。
- 污染数据中 treatment 的"异常变快"（research 塌缩、overlap 退化到 ~19s）是
  **搜索失败伪影**，与实现无关：配额耗尽前的真实 E2E 冒烟（下）与 5A 的证据结构完全一致。
- 已停止实验并保留现场：driver 已停、服务回安全态；重跑清单与入口见 §R 附。

**因此本报告不给 M/N/O/P/Q/R 的配对结论**。5B 定版所依据的证据基础为：

| 证据 | 结果 |
|---|---|
| 5A 配对 A/B（16 run，4 EN 查询 × 2 轮） | 配对 Δ 8/8 为负、中位 −60s；盲评 overall 4:4；无 correctness 退化（5A 报告 §J/§K） |
| 5B 真实 E2E 冒烟（本日 01:34–01:40，配额耗尽前） | accept 路径 146.3s VALID；reject→accept 路径 230.0s VALID；checkpoint 逐字验证 `notes == payload(gen2).notes`、`regenerate_research` 恰 1 次、draft 分支未重跑、overflow/claim loss = 0 |
| 5B 回归矩阵 | full suite 722 passed / 0 failed（无卡模式下复核 616 passed + 108 env-skip + 0 failed）；含 reject/回环/stale 注入/重启/取消/开关双向翻转 |

**§R 附：剩余验收的复跑清单（配额恢复后）**：需重跑 25 个 run —— trt r1 ×8、
ctl z4-r1、r2 ×16（干净 7 个 ctl r1 保留）。入口：
`bash scripts/experiments/phase5b_ab_driver.sh --arm both --round r1` 与 `--round r2`
（脚本已更新为 control 显式 off）。配额预算：每 E2E ≈ 14–16 次搜索 +
claim-verification 若干 → 25 run 约需 ~400–500 次搜索额度。
Reject 路径的真实浪费已有单点实测：regen wall = 71.5s、notes=3（worker 日志
telemetry 行，见 §R 的 smoke 证据）。

## S. Known limitations

1. **扩大配对验收未完成**（Tavily 配额耗尽，§M–R）：5B 代码本身没有干净的大样本
   A/B 数据；质量/性能结论目前外推自 5A + 冒烟 + 测试。定版为**人工审核后的
   产品决策**（已记录），剩余 25-run 验收列为上线后复核。
2. **质量子信号持续观察**（5A 遗留）：structure（ctl 4:1）、citations（ctl 4:2）
   仍未在扩大样本中复核；上线后应结合真实流量/后续 A/B 继续盯。
3. HITL 审查点在 research 之后（superstep 屏障硬约束；产品已接受）。
4. REJECT 路径成本 = 被作废 generation 的一次研究（单点实测 regen wall ≈71.5s /
   notes=3；见 §M–R 附）；reject 为 rare path。
5. 中途失败语义差异：**异常**不提交同 superstep 已完成兄弟（重放一次 draft）；
   **interrupt** 提交（5A 已锁定）。两者都不产生下游重复。
6. UNSUPPORTED 率绝对值高（5A ≈38–39%）：本阶段未动 evaluator；建议后续单独审计
   metric 定义/分母/校准（非 5B blocker）。
7. run_baseline 的 review 序列的 feedback 是固定占位文本（机制验证，非质量验证）。
8. 真实用户 HITL 行为数据仍缺失（5A 38/38 皆 benchmark auto-approve）；accept rate
   不可外推。telemetry 增量 = 结构化日志（HITL 决策/代数/作废代数/regen wall/
   notes 计数），未建新系统，未记录正文。
9. 5B 之前的在途 checkpoint 无 generation 标记（见 §K 边界）。
10. **搜索配额是外部硬依赖**：每个 E2E run 消耗 14–16 次 Tavily 搜索；dev key
    的套餐上限会在一次完整 A/B 中途被打爆（本轮实测）——benchmark 前先核配额。

## T. 最终决定：**DEFAULT ON（已定版，2026-10-07）**

- **决定**：`DR_SEED_LITE_SPECULATIVE` 默认翻 ON —— 投机拓扑成为生产默认路径
  （新任务即"Research 在 HITL Accept 前并行执行，只有被 ACCEPT 的当前 generation
  进入下游"）。显式 `=off` 一键回滚（无需 state migration；在途 lineage 按
  checkpoint 标记保持自身语义）。
- **决策人**：项目负责人（人工审核）；本报告作者未自行翻默认（mission §17/§18
  的约束已遵守到审核点）。
- **决策时已知悉的偏离**：mission §18 的决策规则要求"paired quality shows no
  meaningful regression"等扩大验收通过才建议 ON；本次因外部搜索配额阻断，
  **扩大配对验收未完成**，决策人基于以下证据批准：5A 完整 16-run A/B（机制与
  质量）+ 5B 真实 E2E 冒烟（accept/reject 两条路径端到端验证）+ 722 测试回归矩阵。
- **上线后待办（非回滚 gate）**：
  1. 配额恢复后补跑 25-run 配对验收（§M–R 附复跑清单），复核 5A 的两个质量子信号
     （structure/citations）与性能机制；
  2. 观察真实 HITL 决策分布（reject/regenerate 为罕见路径，期望浪费 = 一次研究）；
  3. UNSUPPORTED metric 单独审计（§14，非 5B 遗留 blocker）。
- **回滚预案**：`DR_SEED_LITE_SPECULATIVE=off` + 重启 worker/API（新任务回串行；
  在途任务不受影响）。
