"""Task 状态机的全量迁移测试。

覆盖矩阵中**每一个** (current, target) 组合——合法组合必须通过，
非法组合必须抛 :class:`InvalidTaskTransition`。这样迁移表被修改时，
测试会立刻反映出来。
"""

import itertools

import pytest

from backend.domain.task import (
    ALLOWED_TRANSITIONS,
    DEFAULT_STAGE_STATUS,
    STAGE_TO_STATUS,
    TERMINAL_STATUSES,
    InvalidTaskTransition,
    TaskStatus,
    UnknownTaskStatus,
    can_transition,
    coerce_status,
    ensure_transition,
    status_for_stage,
)

ALL_STATUSES = list(TaskStatus)
ALL_PAIRS = list(itertools.product(ALL_STATUSES, ALL_STATUSES))

LEGAL_PAIRS = [
    (current, target)
    for current in ALL_STATUSES
    for target in ALLOWED_TRANSITIONS[current]
]
ILLEGAL_PAIRS = [
    (current, target)
    for current, target in ALL_PAIRS
    if target not in ALLOWED_TRANSITIONS[current] and current != target
]


# ===== 枚举本身 =====


def test_status_values_match_persisted_strings():
    """取值即持久化字符串，改动会破坏既有数据。"""
    assert [s.value for s in TaskStatus] == [
        "pending",
        "running",
        "waiting_review",
        "completed",
        "failed",
        "cancelling",
        "cancelled",
        "deleted",
    ]


def test_deleted_status_exists():
    """回归测试：删除是软删除（status='deleted'），状态机必须认识它。

    漏掉它会导致 DELETE /api/research/{id} 被状态机拒绝。
    """
    assert hasattr(TaskStatus, "DELETED")
    assert TaskStatus.DELETED.value == "deleted"


def test_every_status_has_a_transition_entry():
    for status in ALL_STATUSES:
        assert status in ALLOWED_TRANSITIONS


def test_only_deleted_is_unreachable_terminal():
    assert ALLOWED_TRANSITIONS[TaskStatus.DELETED] == frozenset()


def test_terminal_statuses_only_allow_deletion():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset({TaskStatus.DELETED})


# ===== 全量迁移矩阵 =====


@pytest.mark.parametrize(("current", "target"), LEGAL_PAIRS)
def test_legal_transition_passes(current, target):
    ensure_transition(current, target)
    assert can_transition(current, target) is True


@pytest.mark.parametrize(("current", "target"), ILLEGAL_PAIRS)
def test_illegal_transition_raises(current, target):
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(current, target)
    assert can_transition(current, target) is False


def test_exhaustive_matrix_is_covered():
    """确保上面的参数化确实覆盖了全矩阵，没有遗漏组合。"""
    assert len(LEGAL_PAIRS) + len(ILLEGAL_PAIRS) + len(ALL_STATUSES) == len(ALL_PAIRS)


def test_invalid_transition_carries_context():
    with pytest.raises(InvalidTaskTransition) as exc_info:
        ensure_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)

    assert exc_info.value.current is TaskStatus.COMPLETED
    assert exc_info.value.target is TaskStatus.RUNNING
    assert "completed" in str(exc_info.value)
    assert "running" in str(exc_info.value)


# ===== 幂等 =====


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_same_status_is_idempotent(status):
    """重复写入同一状态必须成功——重试 / 重复 resume 是正常现象。"""
    ensure_transition(status, status)


# ===== 具体语义断言 =====


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_cannot_restart(terminal):
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(terminal, TaskStatus.RUNNING)


def test_cannot_complete_from_waiting_review_directly():
    """waiting_review 必须先回到 running，不能直接 completed。"""
    with pytest.raises(InvalidTaskTransition):
        ensure_transition(TaskStatus.WAITING_REVIEW, TaskStatus.COMPLETED)


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_deletion_allowed_from_every_status(status):
    """与既有实现一致：任何状态都可以被软删除。"""
    ensure_transition(status, TaskStatus.DELETED)


# ===== coerce_status =====


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_coerce_roundtrip(status):
    assert coerce_status(status.value) is status
    assert coerce_status(status) is status


@pytest.mark.parametrize("bad", ["unknown", "", "PENDING", "running ", "deleted_at"])
def test_coerce_rejects_unknown(bad):
    with pytest.raises(UnknownTaskStatus):
        coerce_status(bad)


# ===== stage → status 推导 =====


def test_stage_to_status_mapping():
    assert status_for_stage("human_review") is TaskStatus.WAITING_REVIEW
    assert status_for_stage("final_report_generation") is TaskStatus.COMPLETED
    assert status_for_stage("supervisor_subgraph") is DEFAULT_STAGE_STATUS
    assert status_for_stage("write_research_brief") is DEFAULT_STAGE_STATUS
    assert status_for_stage("") is DEFAULT_STAGE_STATUS


def test_default_stage_status_is_running():
    assert DEFAULT_STAGE_STATUS is TaskStatus.RUNNING


def test_mapped_stages_are_reachable_from_running():
    """所有由 stage 推导出的状态，必须能从 RUNNING 合法到达。"""
    for stage, status in STAGE_TO_STATUS.items():
        assert can_transition(TaskStatus.RUNNING, status), f"stage={stage}"


# ===== 真实业务路径 =====


def test_real_workflow_transitions_are_all_legal():
    """把代码里实际发生的迁移序列跑一遍，确保没有被状态机误伤。

    对应一次完整的 HITL 流程：
    创建 → 运行 → 等待审查 → 审查通过 → 运行 → 完成。
    """
    sequence = [
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_REVIEW,
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
    ]
    for current, target in zip(sequence, sequence[1:]):
        ensure_transition(current, target)


def test_real_failure_paths_are_legal():
    """进程重启清理（running/pending → failed）与异常终止路径。"""
    for current in (TaskStatus.RUNNING, TaskStatus.PENDING):
        ensure_transition(current, TaskStatus.FAILED)
    ensure_transition(TaskStatus.WAITING_REVIEW, TaskStatus.FAILED)
