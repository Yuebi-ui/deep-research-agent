"""Build the LangGraph research workflow and its task-facing nodes.

The graph owns research planning, HITL, verification and final report generation.
Long-term memory is accessed through ``deep_research.memory.runtime`` so the
memory subsystem does not depend back on this graph builder.
"""

import asyncio
import os
import sqlite3
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from deep_research.utils import get_today_str
from deep_research.states import AgentState, AgentInputState
from deep_research.prompts import FINAL_REPORT_PROMPT, RESEARCH_BRIEF_PROMPT
from deep_research.research_seed import (
    ResearchFenceError,
    active_generation,
    build_research_seed,
    fenced_accept_update,
    render_seed_message,
    seed_lite_speculative_enabled,
    seed_metrics,
    speculative_lineage,
)
from deep_research.agents import supervisor_agent
from deep_research.agents.draft_agent import write_draft_report
from deep_research.memory.runtime import get_memory_manager
from deep_research.writer_context import build_final_report_prompt, unique_notes
from deep_research.writer_validation import validate_report_citations
from deep_research.llm import get_chat_model, get_chat_model_auto, writer_thinking
from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)

# ===== Config =====

# The final-writer reasoning policy is process-scoped and configurable through
# DR_WRITER_THINKING. The model is constructed at import time, so changing the
# setting requires a process restart. The current default follows
# ``writer_thinking()`` (off unless explicitly enabled).
writer_model = get_chat_model("writer", thinking=writer_thinking())


def _get_memory_manager():
    """Compatibility wrapper around the memory composition root.

    Older integrations imported this private helper from ``agent_builder``. New
    code should depend on :func:`deep_research.memory.runtime.get_memory_manager`
    directly so the memory layer never needs to import the graph builder.
    """
    return get_memory_manager()


def extract_user_query(messages: Any) -> str:
    """从 state.messages 提取用户 query。

    与 `final_report_generation` 历史上的取法逐字一致（取第一条，content 非
    字符串时退化为 str(...)），供 **post-completion 记忆落库**复用。
    """
    if not messages:
        return ""
    first = messages[0]
    content = getattr(first, "content", None)
    return content if isinstance(content, str) else str(first)


def store_report_memory(user_query: str, report: str) -> Any:
    """把完成报告写入派生记忆。

    默认 durable 模式由 ``MemoryOutboxProcessor`` 在任务完成之后调用；关闭
    ``DR_MEMORY_OUTBOX_ENABLED`` 时，runner 才会走旧的 best-effort fallback。
    无论哪条路径，最终报告本身都是任务事实源，记忆只属于派生数据。
    """
    if not report:
        return None
    return _get_memory_manager().store_from_report(user_query, report)


# ===== RESEARCH BRIEF (with Memory Injection) =====

async def write_research_brief(state: AgentState) -> Command[str]:
    """生成研究简报，同时注入历史记忆作为上下文参考。"""

    messages = state.get("messages", [])
    # 提取用户最新 query 用于记忆检索和智能路由
    user_query = messages[-1].content if messages else ""

    # 检索历史记忆（同步 chroma + embedding 调用 → 放线程，避免阻塞事件循环）
    memory_context = ""
    try:
        mgr = _get_memory_manager()
        memory_context = await asyncio.to_thread(mgr.retrieve_context, user_query)
        if memory_context:
            logger.info("Injected memory context into research_brief")
    except Exception as e:
        logger.warning("Memory retrieval failed: %s", e)

    # 智能路由选择模型
    draft_model = get_chat_model_auto("draft", query_text=user_query)

    prompt = RESEARCH_BRIEF_PROMPT.format(
        messages=messages,
        date=get_today_str()
    )

    # 注入记忆上下文
    if memory_context:
        prompt = memory_context + "\n\n" + prompt

    response = await draft_model.ainvoke([HumanMessage(content=prompt)])
    research_brief = response.content

    # Phase 5A/5B（DR_SEED_LITE_SPECULATIVE，**默认 on**，显式 off 回滚）：
    # 投机模式下**在这个节点内**产出 research_seed 并写入 supervisor_messages，
    # 然后返回普通 update（不用 Command(goto)）—— 这样静态 fan-out 边生效，
    # draft 与 research 成为它的**直接后继**，落在同一个 superstep
    # （唯一能真正并行的位置）。
    #
    # 踩过的坑：把 seed 放到独立节点（brief→seed→research）会让 research 落到下一个
    # superstep；superstep 是屏障 → research 仍要等 draft 跑完（实测 overlap = 0.0s）。
    #
    # Phase 5B：此处是**唯一**读取 env 开关的地方（决定新 lineage 是否投机起步）；
    # `research_generation=1` 是 lineage 标记，之后所有节点只看 checkpoint 里的事实。
    if seed_lite_speculative_enabled():
        seed = build_research_seed(research_brief)
        metrics = seed_metrics(seed)
        logger.info(
            "research_seed built: fingerprint=%s chars=%d (phase5b speculative lineage start, generation=1)",
            metrics["seed_fingerprint"], metrics["seed_message_chars"],
        )
        return {
            "research_brief": research_brief,
            "supervisor_messages": [render_seed_message(seed), research_brief],
            "research_generation": 1,
        }

    # Phase 5B：串行 lineage 也要**显式**写下 research_generation=0 ——
    # 标记必须与 brief 的决策原子提交，否则“崩溃发生在 brief 提交之前”的
    # checkpoint 会处于未绑定状态（见 runner._lineage_is_speculative：
    # 未绑定 → 按 env；绑定后 → 永久保持自己的语义）。
    return Command(
        goto="write_draft_report",
        update={"research_brief": research_brief, "research_generation": 0}
    )


# ===== HUMAN REVIEW (HITL) =====

async def human_review(state: AgentState) -> Command[str]:
    """HITL 节点：展示报告草稿，等待人工审查。

    用户可以通过 resume 传入 {'action': 'approve'} 或 {'action': 'revise', 'feedback': '...'}

    Phase 5B 语义（按 **lineage** 而非 env 开关判定）：

    - 串行 lineage（research_generation 缺省 0）——历史行为逐字不变：
      approve → supervisor_subgraph 开始研究；revise → 改写草稿回到本节点。
    - 投机 lineage（research_generation >= 1）——研究已在并行分支跑完（隔离在
      `speculative_research` 中）；approve 经 **fence 校验**后把当前 generation
      的结果并入 `notes` 再进 claim_verification（stale 结果不一致会显式失败）；
      revise → 改写草稿 → regenerate_research（推进 generation、用新种子重跑研究）
      → 回到本节点再次审查。旧 generation 的投机结果被下一代覆盖，永不进入下游。
    """
    draft = state.get("draft_report", "")
    research_brief = state.get("research_brief", "")
    speculative = speculative_lineage(state)

    # 触发中断，等待人工输入
    review = interrupt({
        "message": "报告草稿已生成，请审查",
        "draft_report_preview": draft[:1000] + ("..." if len(draft) > 1000 else ""),
        "research_brief": research_brief[:500],
        "options": ["approve", "revise"],
        # 观测字段（不参与决策）：审查对象属于哪一代研究
        "research_generation": active_generation(state),
    })

    action = review if isinstance(review, str) else review.get("action", "approve")

    if action == "revise":
        feedback = review.get("feedback", "") if isinstance(review, dict) else ""
        revision_prompt = f"请根据以下反馈修改报告草稿：\n{feedback}\n\n当前草稿：\n{draft}"
        response = await writer_model.ainvoke([HumanMessage(content=revision_prompt)])
        if speculative:
            # REJECT/REGENERATE：旧 generation 全部作废，研究必须重跑。
            # 草稿改写走既有产品语义（writer 模型），随后 regenerate_research
            # 推进 generation、重建确定性 seed 并重跑研究，再回到本节点审查。
            logger.info(
                "HITL: revised → invalidating research generation %d, regenerating",
                active_generation(state),
            )
            return Command(
                goto="regenerate_research",
                update={"draft_report": response.content},
            )
        logger.info("HITL: report revised based on user feedback")
        return Command(
            goto="human_review",
            update={"draft_report": response.content}
        )
    else:
        if speculative:
            # ACCEPT：fence 校验（payload.generation == active_generation）通过后，
            # 当前 generation 的研究结果成为**唯一**进入下游的研究。
            updates = fenced_accept_update(state)
            logger.info(
                "HITL: accepted research generation %d (%d notes admitted downstream)",
                active_generation(state), len(updates["notes"]),
            )
            return Command(goto="claim_verification", update=updates)

        # 串行模式：批准后**才开始**研究（research 排在 HITL 之后）。
        logger.info("HITL: user approved draft, proceeding to %s", "supervisor_subgraph")
        return Command(goto="supervisor_subgraph")


# ===== REGENERATE RESEARCH (Phase 5B, speculative only) =====

async def regenerate_research(state: AgentState, config: RunnableConfig) -> dict:
    """REJECT/REGENERATE 回路：推进 generation，用干净输入重跑研究（隔离输出）。

    在节点内 inline 调用 supervisor 子图（它没有 checkpointer）：

    - 输入是**显式构造的干净 state**：新一代研究不继承上一代任何研究上下文
      （messages / iterations / quality_history / critiques 全部归零）；
    - 输出只回写 `research_generation` / `speculative_research`（以及研究途中
      refine_draft_report 改写过的草稿，与串行语义对齐）；子图内部状态**不会**
      写回父图；
    - config 只继承观测侧（callbacks/metadata/tags），剥离 configurable ——
      子图不得触碰父图的 thread/checkpoint 配置；
    - 节点整体重放安全：seed 是 brief 的确定性投影，崩溃后重跑同一节点不会
      产生业务污染（只浪费一次研究成本）。

    spike 实测（scripts/experiments/phase5b_topology_spike.py）：节点内
    inline ainvoke 每次调用状态干净、内层节点事件出现在外层事件流（UI/观测保留）。
    """
    gen = active_generation(state) + 1
    brief = state.get("research_brief", "") or ""
    draft = state.get("draft_report", "") or ""
    seed = build_research_seed(brief)
    metrics = seed_metrics(seed)
    child_input = {
        "supervisor_messages": [render_seed_message(seed), brief],
        "research_brief": brief,
        "research_iterations": 0,
        "needs_quality_repair": False,
        "final_exit": False,
        "notes": [],
        "raw_notes": [],
        "quality_history": [],
        "active_critiques": [],
        "research_trace": [],
        "supervisor_memory_hint": "",
        # 修订后的草稿：与串行模式一致，研究过程中的 refine_draft_report
        # 可以读取/改写它（实测触发率极低，但保持该路径可达）。
        "draft_report": draft,
        "research_generation": gen,
    }
    child_config: dict[str, Any] = {k: v for k, v in (config or {}).items() if k != "configurable"}
    child_config.setdefault("recursion_limit", 50)

    started = time.monotonic()
    result = await supervisor_agent.ainvoke(child_input, config=child_config)
    elapsed = time.monotonic() - started

    payload = result.get("speculative_research")
    if not payload or payload.get("generation") != gen:
        # 子图在投机 lineage 必须写出带 generation 的 payload；缺失 = wiring 被破坏。
        raise ResearchFenceError(
            f"regenerate_research: supervisor 未产出 generation {gen} 的 payload "
            f"(got {payload!r})"
        )

    updates: dict[str, Any] = {"research_generation": gen, "speculative_research": payload}
    if result.get("research_trace"):
        updates["research_trace"] = result["research_trace"]
    refined = result.get("draft_report") or ""
    if refined and refined != draft:
        # 研究途中 refine 改写过草稿：与串行语义一致，回写供后续审查/终稿使用。
        updates["draft_report"] = refined
        logger.info("regenerate_research: draft refined during research (gen %d)", gen)

    logger.info(
        "research regenerated: generation %d invalidated → generation %d produced "
        "(wall=%.1fs, notes=%d, seed=%s)",
        gen - 1, gen, elapsed, len(payload.get("notes") or []), metrics["seed_fingerprint"],
    )
    return updates


# ===== ACCEPT RESEARCH (Phase 5B, speculative without HITL) =====

async def accept_research(state: AgentState) -> dict:
    """无 HITL 构建下的 auto-accept：fence 校验后把投机结果并入 `notes`。

    生产路径始终带 HITL（runner `with_hitl=True`）；本节点只服务离线/测试
    构建（with_hitl=False + 投机）。语义与 human_review 的 ACCEPT 分支一致：
    只有 generation 匹配的结果可以进入下游，否则显式失败。
    """
    updates = fenced_accept_update(state)
    logger.info(
        "auto-accept: research generation %d admitted downstream (%d notes)",
        active_generation(state), len(updates["notes"]),
    )
    return updates


# ===== CLAIM VERIFICATION =====

async def claim_verification(state: AgentState) -> dict:
    """报告草稿的事实核查：提取关键 Claim → 搜索验证 → 生成核查报告。

    完全容错：任何步骤失败都优雅跳过，不影响主流程。
    """
    try:
        from deep_research.verification.claim_extractor import ClaimExtractor
        from deep_research.verification.claim_verifier import ClaimVerifier
        from deep_research.verification.schemas import VerificationReport

        draft = state.get("draft_report", "")
        if not draft or len(draft) < 100:
            logger.info("Skipping claim verification: draft too short")
            return {"verification_report": None, "claim_verification_warning": ""}

        extractor = ClaimExtractor()
        verifier = ClaimVerifier()

        # 同步 LLM 调用 → 线程（不阻塞事件循环，保护 heartbeat）
        claims = await asyncio.to_thread(extractor.extract, draft)
        if not claims:
            logger.info("No claims extracted from draft")
            return {"verification_report": None, "claim_verification_warning": ""}

        verdicts = await verifier.verify(claims)
        report = VerificationReport.from_verdicts(verdicts)
        logger.info(
            "Verification complete: %d/%d supported, hallucination_rate=%.1f%%",
            report.supported, report.total_claims, report.hallucination_rate * 100,
        )

        warning = ""
        if report.unsupported > 0:
            unsupported_texts = [v.claim_text[:100] for v in verdicts if v.verdict == "UNSUPPORTED"]
            warning = f"\n[事实核查警告] 以下 {report.unsupported} 条断言缺乏证据支持，请在最终报告中标注或移除：\n" + \
                      "\n".join(f"- {t}" for t in unsupported_texts)

        return {
            "verification_report": report.model_dump() if report else None,
            "claim_verification_warning": warning,
        }
    except Exception as e:
        logger.warning("Claim verification failed (skipping): %s", e)
        return {"verification_report": None, "claim_verification_warning": ""}


# ===== FINAL REPORT GENERATION =====

async def final_report_generation(state: AgentState):
    """最终报告的生成: 用户query，研究简报，findings, 报告初稿 => 报告"""

    notes = state.get("notes", [])
    report_context, build_stats = build_final_report_prompt(
        research_brief=state.get("research_brief", "") or "",
        draft_report=state.get("draft_report", "") or "",
        notes=notes,
        date=get_today_str(),
        verification_warning=state.get("claim_verification_warning", "") or "",
    )
    logger.info("Writer context build: %s", build_stats)
    final_report_prompt = report_context

    # 流式生成：逐 token 输出，前端可以实时渲染
    report_chunks: list[str] = []
    async for chunk in writer_model.astream([HumanMessage(content=final_report_prompt)]):
        if chunk.content:
            report_chunks.append(chunk.content)
    report_content = "".join(report_chunks)

    citation_validation = validate_report_citations(
        report_content,
        "\n\n".join([
            state.get("research_brief", "") or "",
            state.get("draft_report", "") or "",
            *unique_notes(notes),
        ]),
    )
    if not citation_validation["ok"]:
        logger.warning("Final writer citation check: %s", citation_validation["issues"])

    # Phase 4C-lite：结构化记忆落库**不在这里做**。
    #
    # 它原先内联在此处（asyncio.to_thread + try/except），使 user-visible
    # completion 多等一次 LLM 抽取 + embedding + Chroma upsert（实测 11–20s）。
    # 现在由 runner 在任务置 completed / 发布 task.completed 之后调用
    # `store_report_memory`，并纳入 post-completion 后台任务生命周期。
    return {
        "final_report": report_content,
        "writer_validation": citation_validation,
        "messages": ["最终的报告: " + report_content],
    }


# ===== BUILD GRAPH =====

async def build_agent_async(with_hitl: bool = True, checkpoint_db: str | None = None) -> Any:
    """异步构建 Deep Research Agent（支持 astream_events）。

    流式场景使用 InMemorySaver（已验证兼容 astream_events），
    持久化由 build_agent() 同步版的 SqliteSaver 负责。

    Args:
        with_hitl: 是否启用 HITL 中断审查
        checkpoint_db: 忽略（保留参数兼容性），流式场景统一用 InMemorySaver

    Returns:
        编译后的 LangGraph agent（带 InMemorySaver）
    """
    builder = _create_builder(with_hitl)
    checkpointer = InMemorySaver()
    logger.info("Agent built with InMemorySaver for streaming")
    return builder.compile(checkpointer=checkpointer)


def build_agent(with_hitl: bool = True, checkpoint_db: str | None = None) -> Any:
    """同步构建 Agent（用于 get_graph 或非流式 ainvoke）。

    流式场景请使用 build_agent_async。
    """
    builder = _create_builder(with_hitl)

    checkpointer = None
    if checkpoint_db:
        os.makedirs(os.path.dirname(checkpoint_db) or ".", exist_ok=True)
        conn = sqlite3.connect(checkpoint_db, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        logger.info("Agent built with SqliteSaver at %s", checkpoint_db)
    else:
        logger.info("Agent built without persistent checkpointer")

    return builder.compile(checkpointer=checkpointer)


def _create_builder(with_hitl: bool = True, speculative: bool | None = None) -> StateGraph:
    """创建 StateGraph 并添加节点和边（公共逻辑）。

    `speculative`：
      - `None`（默认）= 按 env 开关 `DR_SEED_LITE_SPECULATIVE` 决定（新任务；
        开关**默认 on** —— 投机拓扑即生产默认路径，显式 `=off` 一键回滚）；
      - 显式 True/False = 强制拓扑 —— runner 在 resume 时依据 checkpoint 里的
        lineage 标记（research_generation）传入，保证**在途任务即使在开关翻转后
        重启也按其自身 lineage 的拓扑恢复**（回滚不需要 state migration）。

    串行拓扑（历史行为，开关显式 off 的新任务）:
      START → write_research_brief → write_draft_report → [human_review] → supervisor_subgraph
          → claim_verification → final_report_generation → END

    投机拓扑（Phase 5A/5B）:
      START → write_research_brief → ┬ write_draft_report ────┐
                                     └ supervisor_subgraph ───┴→ [human_review] → 后续同左
      带 HITL 时 REJECT 回路：
        human_review --revise(Command)--> regenerate_research → human_review
      research_seed 在 write_research_brief 内部写出；两条分支是它的直接后继，
      因此**同一 superstep 真正并发**；HITL 位于 AND-join 之后（spike 实测：
      LangGraph 的 superstep 是屏障，HITL 不可能早于 research 分支结束）。
    """
    from deep_research.research_seed import seed_lite_speculative_enabled

    if speculative is None:
        speculative = seed_lite_speculative_enabled()
    builder = StateGraph(AgentState, input_schema=AgentInputState)

    builder.add_node("write_research_brief", write_research_brief)
    builder.add_node("write_draft_report", write_draft_report)
    builder.add_node("supervisor_subgraph", supervisor_agent)
    builder.add_node("claim_verification", claim_verification)
    builder.add_node("final_report_generation", final_report_generation)

    builder.add_edge(START, "write_research_brief")

    if speculative:
        # seed 在 write_research_brief 内部产出（见 draft_agent），因此这里是**纯 fan-out**：
        # 两条分支都是 write_research_brief 的直接后继 → 落在**同一个 superstep** →
        # 真正并发。任何额外的中转节点都会把 research 推到下一个 superstep（屏障），
        # 从而重新串行化（实测踩过：overlap = 0.0）。
        builder.add_edge("write_research_brief", "write_draft_report")
        builder.add_edge("write_research_brief", "supervisor_subgraph")
        # ⚠️ join 必须用**列表形式**：两次单独的 add_edge(a, c) / add_edge(b, c) 是
        # "每条入边各触发一次"（OR/多次执行），不是等待全部完成。实测（spike）：
        # 写成两条边时 human_review 会在 draft 完成后立刻触发、与 research 并行，
        # 且 resume 时会把仍在 pending 的 supervisor_subgraph 再跑一遍（重复副作用）。
        if with_hitl:
            builder.add_node("human_review", human_review)
            builder.add_node("regenerate_research", regenerate_research)
            builder.add_edge(["write_draft_report", "supervisor_subgraph"], "human_review")
            # REJECT 回路：join 入边 + 独立回边指向同一目标 —— spike 实测
            # （phase5b_topology_spike.py）：第一轮 join 仍只触发一次；之后
            # 每一轮 regenerate 完成恰好再触发一次审查；分支不会被重复执行。
            builder.add_edge("regenerate_research", "human_review")
            # ⚠️ human_review **不得有静态出边**：LangGraph 中静态边会覆盖
            # Command(goto)（tests/test_graph_wiring.py 锁定的语义）。approve /
            # revise 的去向全部由节点自己返回的 Command 决定。
        else:
            # 无 HITL 的构建（离线/测试）：没有 reject 语义，研究在 join 后
            # 自动 accept（fence 校验后并入 notes），与带 HITL 的 ACCEPT 同语义。
            builder.add_node("accept_research", accept_research)
            builder.add_edge(["write_draft_report", "supervisor_subgraph"], "accept_research")
            builder.add_edge("accept_research", "claim_verification")
        builder.add_edge("claim_verification", "final_report_generation")
        builder.add_edge("final_report_generation", END)
        return builder

    if with_hitl:
        builder.add_node("human_review", human_review)
        builder.add_edge("write_research_brief", "write_draft_report")
        builder.add_edge("write_draft_report", "human_review")
    else:
        builder.add_edge("write_research_brief", "write_draft_report")
        builder.add_edge("write_draft_report", "supervisor_subgraph")

    # supervisor 之后走 claim_verification → final_report
    builder.add_edge("supervisor_subgraph", "claim_verification")
    builder.add_edge("claim_verification", "final_report_generation")
    builder.add_edge("final_report_generation", END)
    return builder


# ===== MODULE-LEVEL INSTANCES =====

# 流式兼容的模块级 agent（InMemorySaver，支持 astream_events）
_streaming_agent = _create_builder(with_hitl=True).compile(checkpointer=InMemorySaver())
agent = _streaming_agent

# 流式场景使用 agent_async（同一套 InMemorySaver 逻辑）
# 用法: agent = await build_agent_async(with_hitl=True)

# 兼容旧引用
deep_researcher_builder = _create_builder(with_hitl=True)
