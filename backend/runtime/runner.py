"""单个 Job 的执行。

见 docs/phase-g-runtime-design.md §17.2。

这是 Phase G 的核心：**唯一正式 LangGraph 执行者**。

决策由**状态驱动**而非消息驱动（设计文档 §8.2）：

```text
无 checkpoint                       → 首次执行
checkpoint 且 next == (human_review,) → 有审查决定则 resume，否则等待
checkpoint 且已结束                  → 不应发生（终态任务不该被 claim）
```

取消是**协作式**的：在事件边界检查，**不抢占**已发出的 LLM / Search 调用。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend.domain.task import TaskStatus, coerce_status
from backend.runtime import events as ev
from backend.runtime import heartbeat as hb_mod
from backend.runtime.events import EventType
from backend.runtime.post_completion import PostCompletionTasks
from backend.runtime.redis import resolve_run_id
from deep_research import logging as dr_logging
from deep_research.callbacks.baseline_metrics import (
    BaselineMetricsCollector,
    RunContext,
    append_reliability_event,
    set_baseline_collector,
)

logger = dr_logging.get_logger(__name__)

# 与 sse_adapter 中的节点标签保持一致——UI 展示用的中文名
_NODE_LABELS = {
    "write_research_brief": "研究简报生成",
    "write_draft_report": "报告草稿生成",
    "human_review": "人工审查",
    "supervisor": "Supervisor 决策",
    "supervisor_tools": "工具执行",
    "final_report_generation": "最终报告生成",
    "llm_call": "Research Agent 思考",
    "tool_node": "工具调用",
    "compress_research": "研究结果压缩",
    "red_team": "Red Team 对抗审查",
    "claim_verification": "事实核查",
    "regenerate_research": "研究再生成",
    "accept_research": "研究采纳",
}

# LangGraph 事件 → canonical 事件类型
_TOOL_EVENT_MAP = {
    "on_tool_start": EventType.TOOL_STARTED,
    "on_tool_end": EventType.TOOL_COMPLETED,
}


class TaskCancelled(Exception):
    """任务在安全边界被取消。"""


class OwnershipLost(Exception):
    """心跳发现 claim 已被他人接管 —— 必须立即停止，否则双跑。"""


@dataclass
class RunOutcome:
    """一次执行的结果，供 worker 决定 ack / retry。"""

    status: str
    detail: str = ""
    retryable: bool = False


def build_input(query: str) -> dict[str, Any]:
    return {"messages": [HumanMessage(content=query)]}


def build_resume_command(action: str, feedback: str = "") -> Command:
    if action == "revise":
        return Command(resume={"action": "revise", "feedback": feedback})
    return Command(resume={"action": "approve"})


class TaskRunner:
    """执行一个被 claim 的任务。"""

    def __init__(
        self,
        *,
        redis: Any,
        worker_id: str,
        settings: Any,
        repository: Any,
        checkpointer: Any,
        post_completion: PostCompletionTasks | None = None,
    ) -> None:
        self._redis = redis
        self._worker_id = worker_id
        self._settings = settings
        self._repo = repository
        self._checkpointer = checkpointer
        # Phase 4C-lite：post-completion 后台任务的生命周期管理器。
        # None = 无生命周期管理器（测试/嵌入式调用）→ 记忆落库**内联**执行，
        # 保持与改动前一致的语义（绝不 fire-and-forget）。
        self._post_completion = post_completion
        # baseline 观测（旁路，绝不参与业务判定）
        self._collector: BaselineMetricsCollector | None = None
        self._open_nodes: dict[str, tuple[str, float]] = {}
        # Phase 2 / P0：本 attempt 的 claim 心跳（ownership fencing）
        self._hb: hb_mod.Heartbeat | None = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def run(self, thread_id: str) -> RunOutcome:
        task = self._repo.get(thread_id)
        if task is None:
            return RunOutcome(TaskStatus.PENDING.value, "任务不存在", retryable=False)

        current = coerce_status(task.status)
        if current in (
            TaskStatus.COMPLETED, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.DELETED,
        ):
            logger.info("任务 %s 已是终态 %s，跳过", thread_id, current.value)
            return RunOutcome(current.value, "已是终态", retryable=False)

        # 取消请求 —— 在任何执行之前先看一眼
        if task.cancel_requested:
            await self._finish_cancelled(thread_id)
            return RunOutcome(TaskStatus.CANCELLED.value, "执行前已请求取消")

        task.claimed_by = self._worker_id
        task.claimed_at = datetime.now()
        task.heartbeat_at = datetime.now()
        task.attempt = (task.attempt or 0) + 1
        self._repo.save(task)

        await ev.publish(
            self._redis, thread_id, EventType.TASK_CLAIMED,
            {"attempt": task.attempt}, worker_id=self._worker_id,
            maxlen=self._settings.event_retention_maxlen,
        )

        await self._start_metrics(thread_id, task)

        hb = hb_mod.Heartbeat(
            self._redis, thread_id, self._worker_id,
            interval_ms=self._settings.heartbeat_interval_ms,
            ttl_ms=self._settings.claim_ttl_ms,
            on_lost=self._on_claim_lost,
        )
        self._hb = hb
        await hb.start()
        outcome: RunOutcome | None = None
        try:
            outcome = await self._execute(thread_id, task)
            return outcome
        finally:
            await hb.stop()
            self._record_run_finished(outcome)

    @property
    def ownership_lost(self) -> bool:
        """本 attempt 是否已确认/推定失去 claim 所有权（供 worker 决定是否 ack）。"""
        try:
            return bool(self._hb is not None and self._hb.lost)
        except Exception:  # noqa: BLE001
            return False

    def _ownership_lost_now(self) -> bool:
        return self.ownership_lost

    # ------------------------------------------------------------------
    # Baseline 观测（旁路，全部异常隔离）
    # ------------------------------------------------------------------

    async def _start_metrics(self, thread_id: str, task: Any) -> None:
        """创建本 attempt 的 collector 并接入 contextvar。失败仅降级观测。"""
        try:
            run_id = await resolve_run_id(self._redis, thread_id)
            collector = BaselineMetricsCollector(RunContext(
                run_id=run_id,
                task_id=thread_id,
                attempt=int(task.attempt or 0),
                worker_id=self._worker_id,
            ))
            self._collector = collector
            set_baseline_collector(collector)
            collector.record_reliability("claim_acquired", attempt=task.attempt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("baseline collector 初始化失败（观测降级）: %s", exc)

    async def _on_claim_lost(self) -> None:
        """心跳判定失去所有权时的旁路记录（P0 后执行将随即中止）。"""
        try:
            if self._collector is not None:
                reason = self._hb.loss_reason if self._hb is not None else None
                self._collector.record_reliability(
                    "claim_renewal_failed",
                    reason=reason,
                    note="P0: 执行将在下一个安全边界中止（不再继续推进）",
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("claim lost 记录失败（已忽略）: %s", exc)

    def _record_execution_aborted(self, *, phase: str) -> None:
        """失去所有权后的结构化事件（ownership_lost 归属与恢复可区分）。"""
        try:
            if self._collector is not None:
                reason = self._hb.loss_reason if self._hb is not None else None
                self._collector.record_reliability(
                    "execution_aborted", phase=phase, reason=reason,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("execution_aborted 记录失败（已忽略）: %s", exc)

    def _abandon_if_ownership_lost(self, phase: str) -> RunOutcome | None:
        """终态写入前的护栏：确认失去所有权时放弃 settle/finalize。

        返回 RunOutcome 表示必须放弃（调用方直接 return）；None 表示可继续。
        「宁可中断，不可双跑」：settle 写下的终态是全局事实，stale owner
        没有资格写它。
        """
        if not self._ownership_lost_now():
            return None
        logger.warning("任务进入终态写入前发现已失去 claim 所有权（phase=%s），放弃写入", phase)
        self._record_execution_aborted(phase=phase)
        return RunOutcome(
            TaskStatus.RUNNING.value, "claim 已失去，放弃终态写入（由 recovery 接管）", retryable=True
        )

    def _record_run_finished(self, outcome: RunOutcome | None) -> None:
        try:
            if self._collector is not None:
                self._collector.record_reliability(
                    "run_finished",
                    status=outcome.status if outcome else "unknown",
                    retryable=outcome.retryable if outcome else None,
                    detail=(outcome.detail or "")[:300] if outcome else None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("run_finished 记录失败（已忽略）: %s", exc)
        finally:
            set_baseline_collector(None)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def _lineage_is_speculative(self, config: dict[str, Any]) -> bool | None:
        """checkpoint 里的 lineage 标记（Phase 5B）。

        返回值语义：

        - `None` —— **未绑定或不存在**：新任务（无 checkpoint），或崩溃发生在
          write_research_brief 提交之前（标记尚未写入）。两种情况下 lineage 都
          还没决定 → 拓扑按 env 开关（`DR_SEED_LITE_SPECULATIVE`），即将重跑的
          brief 节点会读**同一个 env** 写下一致的标记（自洽）。
        - `True/False` —— 标记已提交（brief 总是原子写 0 或 1）：resume **必须**
          用与该 lineage 一致的拓扑，开关中途翻转（回滚）不得改变在途任务语义。

        为什么必须读 checkpoint 而不是只看 env：开关决定"新 lineage 怎么起步"，
        已存在的 lineage 必须保持自己的语义 —— 否则开关翻转时 resume 会用错拓扑
        （例如投机 lineage 在串行拓扑下 approve 会跳过 HITL 直接写 notes == 语义
        破坏；串行 lineage 在投机拓扑下会等一个永远不会到来的 join == 卡死）。
        """
        tup = await self._checkpointer.aget_tuple(config)
        if tup is None:
            return None
        values = (tup.checkpoint or {}).get("channel_values") or {}
        if "research_generation" not in values:
            return None
        try:
            return int(values.get("research_generation") or 0) >= 1
        except (TypeError, ValueError):
            # 标记值无法解析 = 状态被污染；显式失败（不静默降级到 env）
            raise RuntimeError(
                f"checkpoint lineage marker 非法: research_generation={values.get('research_generation')!r}"
            )

    async def _execute(self, thread_id: str, task: Any) -> RunOutcome:
        from deep_research.agent_builder import _create_builder

        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}
        # Phase 5B：resume 的拓扑由 checkpoint 的 lineage 决定；新任务沿用 env 开关。
        speculative = await self._lineage_is_speculative(config)
        graph = _create_builder(with_hitl=True, speculative=speculative).compile(
            checkpointer=self._checkpointer
        )
        if self._collector is not None:
            # baseline 观测接线：callback 只观察，不改变 graph 行为
            config["callbacks"] = [self._collector]
            config["metadata"] = {
                "dr_run_id": self._collector.context.run_id,
                "dr_task_id": thread_id,
                "dr_attempt": self._collector.context.attempt,
            }

        snapshot = await graph.aget_state(config)
        next_nodes = tuple(snapshot.next or ())
        # A crash / SQLite outage may happen AFTER graph finished but BEFORE
        # task completion + memory outbox commits. Replay settlement directly
        # from the checkpoint instead of restarting all paid research nodes.
        if not next_nodes and (snapshot.values or {}).get("final_report"):
            logger.info("Restoring finished graph settlement from checkpoint: %s", thread_id)
            return await self._settle(thread_id, graph, config)
        resuming = next_nodes == ("human_review",)

        if resuming:
            review = self._repo.latest_review(thread_id)
            if review is None:
                # 到达审查点但用户还没决定 —— 正常等待，不是错误
                return await self._finish_waiting_review(thread_id, snapshot)
            user_input = build_resume_command(review.action, review.feedback)
            self._repo.mark_review_consumed(review.id, self._worker_id)
            await ev.publish(
                self._redis, thread_id, EventType.TASK_RESUMED,
                {"action": review.action}, worker_id=self._worker_id,
                maxlen=self._settings.event_retention_maxlen,
            )
        elif next_nodes:
            # P0 恢复路径：上次执行中断于图中途（claim 丢失 / worker 崩溃），
            # checkpoint 中仍有 pending 节点 —— 以 **None** 输入从 checkpoint 续跑。
            #
            # 实测语义（Phase 2 探针）：None 会让 LangGraph 继续 pending 节点
            # （含 supervisor_subgraph 的内部 checkpoint）；而重新投喂 query
            # 会让整图从 START 重跑 —— 对 resume-after-review 场景表现为
            # 「再次回到 human_review」，把已完成的工作全部丢弃。
            user_input = None
            fresh = self._repo.get(thread_id)
            if fresh is None:
                return RunOutcome(TaskStatus.PENDING.value, "任务已不存在")
            if fresh.cancel_requested:
                await self._finish_cancelled(thread_id)
                return RunOutcome(TaskStatus.CANCELLED.value, "续跑前已请求取消")
            if coerce_status(fresh.status) is not TaskStatus.RUNNING:
                self._transition(fresh, TaskStatus.RUNNING)
            await ev.publish(
                self._redis, thread_id, EventType.TASK_RESUMED,
                {"action": "continue_checkpoint"}, worker_id=self._worker_id,
                maxlen=self._settings.event_retention_maxlen,
            )
        else:
            user_input = build_input(task.query)
            await ev.publish(
                self._redis, thread_id, EventType.TASK_STARTED,
                {}, worker_id=self._worker_id,
                maxlen=self._settings.event_retention_maxlen,
            )

            # ⚠️ 必须重新读一次再写。
            #
            # `task` 是 run() 开头取的快照，中间隔着一个 await（上面的事件发布），
            # 而 repository.save() 走的是 session.merge —— 它会把这个快照里的
            # **全部列**写回去。若 API 在这段窗口内处理了取消请求
            # （PENDING -> CANCELLED 且 cancel_requested=True），用陈旧对象保存
            # 就会把那次写入整个覆盖掉，**取消请求被静默丢弃**。
            #
            # 这里读与写之间没有 await，窗口收窄到可以忽略；同时顺带
            # 在启动前多设了一个取消检查点。
            fresh = self._repo.get(thread_id)
            if fresh is None:
                return RunOutcome(TaskStatus.PENDING.value, "任务已不存在")

            if fresh.cancel_requested:
                await self._finish_cancelled(thread_id)
                return RunOutcome(TaskStatus.CANCELLED.value, "启动前已请求取消")

            self._transition(fresh, TaskStatus.RUNNING)

        try:
            await self._stream(graph, user_input, config, thread_id)
        except TaskCancelled:
            # 取消与所有权丢失可能同时发生 —— 先看所有权
            abandoned = self._abandon_if_ownership_lost("cancel_handling")
            if abandoned is not None:
                self._close_open_nodes(success=False, error_type="ownership_lost")
                return abandoned
            self._close_open_nodes(success=False, error_type="aborted")
            await self._finish_cancelled(thread_id)
            return RunOutcome(TaskStatus.CANCELLED.value, "执行中被取消")
        except OwnershipLost:
            # 已失去所有权 —— 绝不能继续，也绝不能写终态
            self._close_open_nodes(success=False, error_type="ownership_lost")
            self._record_execution_aborted(phase="streaming")
            return RunOutcome(TaskStatus.RUNNING.value, "claim 被接管，已停止", retryable=True)
        except Exception as exc:
            abandoned = self._abandon_if_ownership_lost("error_handling")
            if abandoned is not None:
                self._close_open_nodes(success=False, error_type="ownership_lost")
                return abandoned
            self._close_open_nodes(success=False, error_type="aborted")
            return await self._finish_failed(thread_id, exc)

        # 正常结束但仍有未收尾的 node：典型是 HITL interrupt（human_review
        # 挂起等待人工输入）—— 记为 suspended，而不是失败
        self._close_open_nodes(success=None, error_type="suspended")

        return await self._settle(thread_id, graph, config)

    async def _stream(self, graph: Any, user_input: Any, config: dict, thread_id: str) -> None:
        """驱动图，翻译事件，并在边界检查取消与所有权。

        P0 起：每个事件边界同时检查 claim ownership —— 失去所有权时立即
        抛 :class:`OwnershipLost`，并在 ``finally`` 里**显式关闭事件流**。

        为什么必须显式 ``aclose()``：Phase 2 探针实测（真实 langgraph 图）表明，
        仅从消费循环 raise/break 不会停止 graph —— LangGraph 内部任务会继续在
        后台推进节点、继续产生 LLM/搜索副作用；``aclose()`` 把 GeneratorExit
        送入生成器后，图任务才被取消（实测节点迭代计数立即冻结）。
        """
        maxlen = self._settings.event_retention_maxlen
        agen = graph.astream_events(user_input, config=config, version="v2")

        try:
            async for event in agen:
                # ownership 检查放在最前：它是内存 Event，零 I/O、零成本
                if self._ownership_lost_now():
                    raise OwnershipLost()

                # 事件边界检查取消（协作式取消的落点）
                if await self._is_cancel_requested(thread_id):
                    raise TaskCancelled()

                kind = event.get("event", "")
                name = event.get("name", "")
                lg_run_id = str(event.get("run_id") or "")

                if kind == "on_chain_start" and name in _NODE_LABELS:
                    self._note_node_start(name, lg_run_id)
                    await ev.publish(
                        self._redis, thread_id, EventType.GRAPH_NODE_STARTED,
                        {"node": name, "label": _NODE_LABELS[name]},
                        worker_id=self._worker_id, maxlen=maxlen,
                    )
                elif kind == "on_chain_end" and name in _NODE_LABELS:
                    self._note_node_end(name, lg_run_id)
                    await ev.publish(
                        self._redis, thread_id, EventType.GRAPH_NODE_COMPLETED,
                        {"node": name, "label": _NODE_LABELS[name]},
                        worker_id=self._worker_id, maxlen=maxlen,
                    )
                elif kind in _TOOL_EVENT_MAP:
                    await ev.publish(
                        self._redis, thread_id, _TOOL_EVENT_MAP[kind],
                        {"tool": name}, worker_id=self._worker_id, maxlen=maxlen,
                    )
        finally:
            await self._close_graph_stream(agen)

    async def _close_graph_stream(self, agen: Any) -> None:
        """显式关闭 astream_events 生成器（停止后台图任务）。"""
        try:
            await agen.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug("关闭 graph 事件流异常（已忽略）: %s", exc)

    # ---- node 计时（baseline 观测；全部异常隔离）----

    def _note_node_start(self, node: str, lg_run_id: str) -> None:
        try:
            if lg_run_id and self._collector is not None:
                self._open_nodes[lg_run_id] = (node, time.time())
        except Exception as exc:  # noqa: BLE001
            logger.debug("node start 记录失败（已忽略）: %s", exc)

    def _note_node_end(self, node: str, lg_run_id: str) -> None:
        try:
            info = self._open_nodes.pop(lg_run_id, None)
            if info is not None and self._collector is not None:
                self._collector.record_node_metric(
                    node, lg_run_id, info[1], time.time(), success=True
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("node end 记录失败（已忽略）: %s", exc)

    def _close_open_nodes(self, *, success: bool | None, error_type: str) -> None:
        """把没等到 end 事件的 node 按事实记录（不补假结束时间）。

        * ``suspended`` —— 图正常停在 interrupt（HITL 等待人工输入）
        * ``aborted``   —— 被异常/取消中断
        """
        try:
            if self._collector is None:
                return
            for lg_run_id, (node, started_at) in list(self._open_nodes.items()):
                self._collector.record_node_metric(
                    node, lg_run_id, started_at, None,
                    success=success, error_type=error_type,
                )
            self._open_nodes.clear()
        except Exception as exc:  # noqa: BLE001
            logger.debug("未完成 node 记录失败（已忽略）: %s", exc)

    async def _settle(self, thread_id: str, graph: Any, config: dict) -> RunOutcome:
        """图跑完后判定终局。"""
        abandoned = self._abandon_if_ownership_lost("pre_settle")
        if abandoned is not None:
            return abandoned

        snapshot = await graph.aget_state(config)
        values = snapshot.values or {}
        next_nodes = tuple(snapshot.next or ())

        if next_nodes == ("human_review",):
            return await self._finish_waiting_review(thread_id, snapshot)

        final_report = values.get("final_report")
        if final_report:
            from deep_research.agent_builder import extract_user_query

            return await self._finish_completed(
                thread_id, final_report, values.get("verification_report"),
                user_query=extract_user_query(values.get("messages") or []),
                research_trace=values.get("research_trace") or [],
                research_generation=values.get("research_generation", 0),
            )

        return await self._finish_failed(
            thread_id, RuntimeError("工作流结束但未产出最终报告")
        )

    # ------------------------------------------------------------------
    # 终局落库（顺序：先 DB 后事件 —— 设计文档 §11）
    # ------------------------------------------------------------------

    async def _finish_completed(
        self, thread_id: str, report: str, verification: Any, *, user_query: str = "",
        research_trace: list[dict] | None = None, research_generation: int = 0,
    ) -> RunOutcome:
        abandoned = self._abandon_if_ownership_lost("pre_finalize_completed")
        if abandoned is not None:
            return abandoned

        task = self._repo.get(thread_id)
        if task is None:
            raise RuntimeError("cannot mark missing task completed")
        task.status = TaskStatus.COMPLETED.value
        task.stage = "final_report_generation"
        task.final_report = report
        task.verification = verification
        task.updated_at = datetime.now()

        from backend.runtime.memory_outbox import (
            enabled as durable_memory_enabled, MemoryOutboxEnqueueError,
        )
        durable_memory = durable_memory_enabled()
        if durable_memory:
            save_atomic = getattr(self._repo, "save_completed_with_outbox", None)
            if save_atomic is None:
                # Never publish completion if the job cannot be persisted.
                raise MemoryOutboxEnqueueError("repository lacks atomic completion/outbox commit")
            try:
                save_atomic(
                    task, user_query=user_query, research_trace=research_trace,
                    research_generation=research_generation,
                )
            except Exception as exc:
                # Task completion was rolled back in the SAME transaction.
                # The caller must retain the Redis job for later reclaim.
                raise MemoryOutboxEnqueueError("atomic completion/outbox commit failed") from exc
        else:
            self._repo.save(task)

        await ev.publish(
            self._redis, thread_id, EventType.TASK_COMPLETED,
            {"has_report": True}, worker_id=self._worker_id,
            maxlen=self._settings.event_retention_maxlen,
        )

        # Completion ordering is intentional. In durable mode the outbox row was
        # committed atomically with the report before this event. Only rollback
        # mode schedules the legacy best-effort enrichment below.
        if not durable_memory:
            # Rollback mode: preserve legacy best-effort scheduling semantics.
            await self._run_post_completion(
                thread_id, user_query, report,
                research_trace=research_trace, research_generation=research_generation,
            )
        # Durable mode: independently polled by a memory worker. Neither the
        # LLM extraction nor Chroma writes can delay task.completed.
        return RunOutcome(TaskStatus.COMPLETED.value)

    # ------------------------------------------------------------------
    # Legacy post-completion fallback (durable outbox is the default)
    # ------------------------------------------------------------------

    async def _run_post_completion(
        self, thread_id: str, user_query: str, report: str, *,
        research_trace: list[dict] | None = None, research_generation: int = 0,
    ) -> None:
        """把结构化记忆落库放到 user-visible completion 之后。

        - 有生命周期管理器（worker）→ 后台任务，worker 在消费下一个 job 前
          `wait_idle()`、退出时 `drain()`；
        - 没有（测试/嵌入式）→ 内联执行，保持旧语义（绝不 fire-and-forget）。
        """
        if not report:
            return
        if self._post_completion is None:
            await self._enrich_memory(
                thread_id, user_query, report,
                research_trace=research_trace, research_generation=research_generation,
            )
            return
        self._post_completion.schedule(
            lambda: self._enrich_memory(
                thread_id, user_query, report,
                research_trace=research_trace, research_generation=research_generation,
            ),
            name=f"memory-enrichment:{thread_id}",
        )

    async def _enrich_memory(
        self, thread_id: str, user_query: str, report: str, *,
        research_trace: list[dict] | None = None, research_generation: int = 0,
    ) -> dict[str, Any]:
        """Legacy fallback: enrich report + episodic memory without changing task truth.

        This method is only used when the durable outbox is disabled. It never
        raises into the task state machine; failures are observability signals.
        """
        from deep_research.agent_builder import store_report_memory

        started = time.monotonic()
        doc_id: Any = None
        error: str | None = None
        try:
            doc_id = await asyncio.to_thread(store_report_memory, user_query, report)
        except Exception as exc:  # noqa: BLE001  # 记忆是派生数据，失败不得冒泡
            error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("记忆落库失败（已降级，不影响任务状态）: %s", exc)
        episode_count = 0
        if research_trace:
            # This is intentionally AFTER task.completed and outside the critical
            # path, sharing the lifecycle/error isolation of report enrichment.
            try:
                from deep_research.memory.stage_retrieval import episodic_enabled, get_episode_store
                from deep_research.memory.episodes import build_completed_episodes
                if episodic_enabled():
                    episodes = build_completed_episodes(thread_id, research_trace, research_generation)
                    if episodes:
                        episode_count = await asyncio.to_thread(get_episode_store().upsert, episodes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("研究经验落库失败（不影响任务状态）: %s", exc)
                error = f"{error}; episodic={type(exc).__name__}" if error else f"episodic={type(exc).__name__}"
        elapsed = time.monotonic() - started
        await self._record_post_completion(
            thread_id, outcome="failed" if error else "ok", elapsed_s=elapsed,
            detail=error or f"doc_id={doc_id}",
        )
        return {"doc_id": doc_id, "error": error, "episodes": episode_count, "elapsed_s": round(elapsed, 3)}

    async def _record_post_completion(
        self, thread_id: str, *, outcome: str, elapsed_s: float, detail: str = ""
    ) -> None:
        """把「fully settled」的耗时写进 reliability 事件（旁路，失败不影响任务）。"""
        try:
            run_id = await resolve_run_id(self._redis, thread_id)
            append_reliability_event(
                run_id, thread_id, "post_completion_enrichment",
                worker_id=self._worker_id, outcome=outcome,
                elapsed_ms=int(elapsed_s * 1000), detail=detail or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("post-completion 事件记录失败（已忽略）: %s", exc)

    async def _finish_waiting_review(self, thread_id: str, snapshot: Any) -> RunOutcome:
        abandoned = self._abandon_if_ownership_lost("pre_finalize_waiting_review")
        if abandoned is not None:
            return abandoned

        values = snapshot.values or {}
        draft = values.get("draft_report", "") or ""

        task = self._repo.get(thread_id)
        if task is not None:
            task.status = TaskStatus.WAITING_REVIEW.value
            task.stage = "human_review"
            task.draft_report = draft
            task.updated_at = datetime.now()
            self._repo.save(task)

        await ev.publish(
            self._redis, thread_id, EventType.TASK_WAITING_REVIEW,
            {"draft_preview": draft[:2000]}, worker_id=self._worker_id,
            maxlen=self._settings.event_retention_maxlen,
        )
        return RunOutcome(TaskStatus.WAITING_REVIEW.value)

    async def _finish_failed(self, thread_id: str, exc: Exception) -> RunOutcome:
        from backend.runtime.retry import classify

        abandoned = self._abandon_if_ownership_lost("pre_finalize_failed")
        if abandoned is not None:
            return abandoned

        decision = classify(exc)
        task = self._repo.get(thread_id)

        if decision.retryable and task is not None and (task.attempt or 0) < self._settings.worker_max_attempts:
            # 保持非终态，交由 claim 过期 + 重试策略处理（设计文档 §13）
            logger.warning(
                "任务 %s 第 %d 次执行失败（可重试）: %s", thread_id, task.attempt, exc
            )
            return RunOutcome(TaskStatus.RUNNING.value, str(exc), retryable=True)

        if task is not None:
            task.status = TaskStatus.FAILED.value
            task.error = f"{decision.code}: {exc}"
            task.updated_at = datetime.now()
            self._repo.save(task)

        await ev.publish(
            self._redis, thread_id, EventType.TASK_FAILED,
            {"code": decision.code, "message": str(exc)[:500]},
            worker_id=self._worker_id, maxlen=self._settings.event_retention_maxlen,
        )
        return RunOutcome(TaskStatus.FAILED.value, str(exc))

    async def _finish_cancelled(self, thread_id: str) -> None:
        task = self._repo.get(thread_id)
        if task is not None:
            task.status = TaskStatus.CANCELLED.value
            task.updated_at = datetime.now()
            self._repo.save(task)

        await ev.publish(
            self._redis, thread_id, EventType.TASK_CANCELLED,
            {}, worker_id=self._worker_id,
            maxlen=self._settings.event_retention_maxlen,
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _transition(self, task: Any, target: TaskStatus) -> None:
        from backend.domain.task import ensure_transition

        current = coerce_status(task.status)
        ensure_transition(current, target)
        task.status = target.value
        task.updated_at = datetime.now()
        self._repo.save(task)

    async def _is_cancel_requested(self, thread_id: str) -> bool:
        """每个事件边界都会调用，因此直接读 DB 而不是缓存。

        缓存会让取消延迟生效，而取消的时效性正是这一阶段的验收项。
        """
        task = self._repo.get(thread_id)
        return bool(task.cancel_requested) if task is not None else False


__all__ = [
    "OwnershipLost",
    "RunOutcome",
    "TaskCancelled",
    "TaskRunner",
    "build_input",
    "build_resume_command",
]
