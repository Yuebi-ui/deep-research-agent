"""跨进程 checkpoint 恢复（执行包 §29 Scenario 1）。

以 **subprocess** 启动两个独立进程，验证 worker #1 退出后 worker #2 能从
同一 thread_id 恢复 HITL 中断并跑完。

刻意用 subprocess 而不是 multiprocessing：Windows 上只有 spawn，
LangGraph / checkpointer 对象通常不可 pickle。

后端用 **sqlite**（临时文件）。`memory` 无法跨进程，用它做本验证没有意义，
脚本会直接拒绝；`redis` 是 Phase F 的目标后端，但当前环境 Redis 不可达
（见 docs/worker-spike.md）。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SPIKE = REPO_ROOT / "scripts" / "worker_spike.py"


def _run_stage(stage: str, thread_id: str, *, backend: str, ckpt_path: pathlib.Path, action: str = "approve"):
    env = dict(
        os.environ,
        APP_ENV="test",
        CHECKPOINTER_BACKEND=backend,
        # 子进程无法继承 monkeypatch，路径必须通过环境变量下发，
        # 否则子进程会写到真实的 data/checkpoints.db
        CHECKPOINTER_SQLITE_PATH=str(ckpt_path),
        PYTHONIOENCODING="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(SPIKE), stage,
         "--thread-id", thread_id, "--action", action],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=180,
    )

    payload: dict = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("stage") == stage:
            payload.update(data)

    return proc.returncode, payload, proc


@pytest.fixture
def spike_env(tmp_path):
    """checkpointer 文件路径（经环境变量传给子进程）。

    注意不能只 monkeypatch 父进程的 resolve_sqlite_path——spike 是以
    subprocess 运行的，补丁不会跨进程生效。
    """
    return tmp_path / "checkpoints.db"


def test_worker_spike_stage1_pauses_at_human_review(spike_env):
    """worker #1：启动任务，停在 human_review，进程退出。"""
    thread_id = f"spike-{uuid.uuid4().hex[:8]}"
    rc, payload, proc = _run_stage("stage1", thread_id, backend="sqlite", ckpt_path=spike_env)

    assert rc == 0, f"stage1 失败:\n{proc.stdout}\n{proc.stderr[-800:]}"
    assert payload.get("next") == ["human_review"]
    assert payload.get("draft_len", 0) > 0


def test_worker_spike_cross_process_recovery(spike_env):
    """**核心场景**：worker #1 退出 → worker #2 恢复 → 完成。

    成功条件（执行包 §29）：必须是真正不同进程，而不是同一 Python 对象里
    调用两次函数。
    """
    thread_id = f"spike-{uuid.uuid4().hex[:8]}"

    rc1, out1, proc1 = _run_stage("stage1", thread_id, backend="sqlite", ckpt_path=spike_env)
    assert rc1 == 0, f"stage1 失败:\n{proc1.stdout}\n{proc1.stderr[-800:]}"

    rc2, out2, proc2 = _run_stage("stage2", thread_id, backend="sqlite", ckpt_path=spike_env)
    assert rc2 == 0, f"stage2 失败:\n{proc2.stdout}\n{proc2.stderr[-800:]}"

    # 进程隔离：必须是两个不同的 PID
    assert out1.get("pid") and out2.get("pid")
    assert out1["pid"] != out2["pid"], "两个 stage 跑在同一进程里，隔离不成立"

    # 恢复证据
    assert out2.get("resumed_from") == ["human_review"], "未读到上一进程留下的中断状态"
    assert out2.get("next") == [], "resume 后未结束"
    assert out2.get("has_report") is True, "未产出最终报告"


def test_spike_refuses_memory_backend(spike_env):
    """memory 后端无法跨进程——脚本必须拒绝，而不是给出假阳性。"""
    thread_id = f"spike-{uuid.uuid4().hex[:8]}"
    rc, _payload, proc = _run_stage("stage1", thread_id, backend="memory", ckpt_path=spike_env)

    assert rc == 2, "memory 后端应被拒绝"
    assert "跨进程" in (proc.stdout or ""), f"应给出明确原因，实际输出:\n{proc.stdout}"


def test_checkpoint_file_is_actually_written(spike_env):
    """确认状态确实落到了 sqlite 文件，而不是留在内存里。"""
    thread_id = f"spike-{uuid.uuid4().hex[:8]}"
    _run_stage("stage1", thread_id, backend="sqlite", ckpt_path=spike_env)

    assert spike_env.exists(), "checkpointer 文件未生成"
    assert spike_env.stat().st_size > 0, "checkpointer 文件为空"


# ===== Redis 后端（Phase F 的目标后端）=====


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis.from_url("redis://localhost:6379", socket_connect_timeout=1.5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis 不可达（需要 redis-stack，含 RediSearch）")
def test_worker_spike_cross_process_recovery_redis(spike_env):
    """§0.5.2 要求的核心验证：**基于 Redis checkpointer** 的跨进程恢复。

    Redis 是 Phase F 与未来 Phase G 的目标后端。sqlite 通过只能证明机制
    成立，不能替代本测试——目标后端必须自己跑通。
    """
    thread_id = f"spike-{uuid.uuid4().hex[:8]}"

    rc1, out1, proc1 = _run_stage("stage1", thread_id, backend="redis", ckpt_path=spike_env)
    assert rc1 == 0, f"redis stage1 失败:\n{proc1.stdout}\n{proc1.stderr[-1200:]}"
    assert out1.get("backend") == "redis", "未真正使用 redis 后端"
    assert out1.get("next") == ["human_review"]

    rc2, out2, proc2 = _run_stage("stage2", thread_id, backend="redis", ckpt_path=spike_env)
    assert rc2 == 0, f"redis stage2 失败:\n{proc2.stdout}\n{proc2.stderr[-1200:]}"

    assert out1["pid"] != out2["pid"], "两个 stage 跑在同一进程里，隔离不成立"
    assert out2.get("resumed_from") == ["human_review"], "未从 Redis 读到中断状态"
    assert out2.get("next") == []
    assert out2.get("has_report") is True
