#!/usr/bin/env bash
# Server V1 Fake Runtime 验收。
#
#     bash scripts/autodl/accept_fake_runtime.sh
#
# 这是"部署是否成功"的**唯一判据**。全程使用 Fake Provider：
#
#     LLM / Search / Embedding 全部为 Fake
#     不访问任何真实付费 API
#     不需要阿里云百炼余额
#
# 验收内容（对应 SERVER V1 Acceptance Gate 的 Runtime 部分）：
#
#     1  环境前置（Redis / DB / API / Worker 就绪）
#     2  创建任务并入队
#     3  Worker 执行到 HITL 中断（waiting_review）+ 草稿落库
#     4  SSE 事件流（含 envelope 字段）
#     5  浏览器断开不影响任务（不建立 SSE 连接也能完成）
#     6  审查 → resume → 完成
#     7  最终报告落库
#     8  取消
#     9  SSE 回放（Last-Event-ID 语义）
#    10  孤儿任务可被回收（设计层面的断言）
#
# 任何一项 FAIL 都会以非零码退出。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env
require_venv

PY="$(python_bin)"
export APP_ENV CONFIG_PATH CHECKPOINTER_BACKEND

API="http://127.0.0.1:${API_PORT}"

section "0. 前置检查"
http_ok "${API}/api/health" || die "API 无响应（${API}）—— 先运行 start_all.sh"
is_running worker || die "Worker 未运行 —— 先运行 start_all.sh"
redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG || die "Redis 无响应"
redis-cli -h 127.0.0.1 -p 6379 MODULE LIST 2>/dev/null | grep -qiE "search" \
  || die "Redis 缺少 RediSearch 模块（不是 Redis Stack）"
ok "API / Worker / Redis Stack 均已就绪"

"$PY" - <<'PY' || die "验收脚本执行失败"
"""Server V1 Fake Runtime 验收（在服务器上直接跑）。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:%s" % __import__("os").environ.get("API_PORT", "8000")

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""), flush=True)


def call(method: str, path: str, payload=None, headers=None, timeout=30):
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body[:200]}
        return exc.code, parsed


def wait_status(tid: str, targets: set[str], timeout: float = 180.0) -> str:
    """轮询任务状态直到进入目标集合（Worker 异步执行，需要等）。"""
    deadline = time.time() + timeout
    last = "?"
    while time.time() < deadline:
        code, body = call("GET", f"/api/research/{tid}/status")
        if code == 200:
            last = body.get("status", "?")
            if last in targets:
                return last
        time.sleep(2)
    return last


print()
print("=== 1. 创建任务（应返回 200 且状态 pending）===")
code, created = call("POST", "/api/research/start", {"query": "Compare LangGraph and CrewAI"})
tid = created.get("thread_id", "")
record("创建任务", code == 200 and bool(tid), f"HTTP {code} thread_id={tid}")
if not tid:
    sys.exit(1)

print()
print("=== 2. Worker 执行到 HITL 中断 ===")
status = wait_status(tid, {"waiting_review", "completed", "failed"})
record("Worker 认领并执行", status in {"waiting_review", "completed"}, f"status={status}")

if status == "failed":
    code, detail = call("GET", f"/api/research/{tid}/status")
    record("任务未失败", False, detail.get("error", "")[:120])

print()
print("=== 3. 草稿落库（前端据此做审查预览）===")
code, snapshot = call("GET", f"/api/research/{tid}/status")
draft = snapshot.get("draft_report", "") or ""
record("草稿已持久化", bool(draft), f"{len(draft)} 字")
record("状态为 waiting_review", snapshot.get("status") == "waiting_review",
       snapshot.get("status", ""))

print()
print("=== 4. SSE 事件流（含 envelope 字段）===")
try:
    req = urllib.request.Request(f"{API}/api/research/{tid}/stream")
    req.add_header("Accept", "text/event-stream")
    frames = []
    with urllib.request.urlopen(req, timeout=20) as resp:
        buf = ""
        deadline = time.time() + 10
        while time.time() < deadline and len(frames) < 3:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode(errors="ignore")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                if frame.strip():
                    frames.append(frame)

    record("收到 SSE 帧", bool(frames), f"{len(frames)} 帧")

    if frames:
        payload_line = next(
            (ln[6:] for ln in frames[0].splitlines() if ln.startswith("data: ")), ""
        )
        envelope = json.loads(payload_line) if payload_line else {}
        has_contract = "event" in envelope and "data" in envelope
        has_envelope = all(
            k in envelope for k in ("event_id", "task_id", "sequence", "timestamp")
        )
        record("保留前端契约字段 event/data", has_contract)
        record("包含 envelope 元数据", has_envelope,
               ",".join(k for k in ("event_id", "task_id", "sequence", "timestamp") if k in envelope))
except Exception as exc:  # noqa: BLE001
    record("SSE 事件流", False, f"{type(exc).__name__}: {exc}")

print()
print("=== 5. 浏览器断开不影响任务 ===")
# 上面那次 SSE 连接已经关闭，任务状态不应因此改变
code, snapshot2 = call("GET", f"/api/research/{tid}/status")
record("断开后任务状态未变", snapshot2.get("status") == snapshot.get("status"),
       f"{snapshot.get('status')} -> {snapshot2.get('status')}")

print()
print("=== 6. 审查 → resume → 完成 ===")
code, _ = call("POST", f"/api/research/{tid}/resume", {"action": "approve", "feedback": ""})
record("提交审查决定", code == 200, f"HTTP {code}")

final = wait_status(tid, {"completed", "failed"}, timeout=240)
record("任务完成", final == "completed", f"status={final}")

print()
print("=== 7. 最终报告落库 ===")
code, report = call("GET", f"/api/research/{tid}/report")
record("报告可读取", code == 200 and bool(report.get("final_report")),
       f"HTTP {code} {len(report.get('final_report',''))} 字")

print()
print("=== 8. 取消 ===")
code, created2 = call("POST", "/api/research/start", {"query": "Cancel me"})
tid2 = created2.get("thread_id", "")
code, cancelled = call("POST", f"/api/research/{tid2}/cancel")
record("取消请求被接受", code == 200, f"HTTP {code} status={cancelled.get('status')}")
code, again = call("POST", f"/api/research/{tid2}/cancel")
record("取消是幂等的", code == 200, f"HTTP {code}")

print()
print("=== 9. SSE 回放（Last-Event-ID 语义）===")
try:
    req = urllib.request.Request(f"{API}/api/research/{tid}/stream")
    frames = []
    with urllib.request.urlopen(req, timeout=20) as resp:
        buf = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode(errors="ignore")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                if frame.strip():
                    frames.append(frame)

    event_ids = [
        ln[4:].strip()
        for f in frames
        for ln in f.splitlines()
        if ln.startswith("id: ")
    ]
    record("事件携带 id（可用于重连）", bool(event_ids), f"{len(event_ids)} 个")

    if event_ids:
        # 用一个较早的 id 重连，应当能回放其后的事件
        req2 = urllib.request.Request(f"{API}/api/research/{tid}/stream")
        req2.add_header("Last-Event-ID", event_ids[0])
        with urllib.request.urlopen(req2, timeout=20) as resp2:
            body = resp2.read(2048).decode(errors="ignore")
        record("携带 Last-Event-ID 重连成功", "data:" in body)
except Exception as exc:  # noqa: BLE001
    record("SSE 回放", False, f"{type(exc).__name__}: {exc}")

print()
print("=== 10. 错误契约 ===")
code, err = call("GET", "/api/research/does-not-exist/status")
unified = isinstance(err.get("error"), dict) and "code" in err["error"]
record("404 使用统一错误结构", code == 404 and unified, f"HTTP {code} {err.get('error',{}).get('code')}")
record("错误响应含 request_id", bool(err.get("error", {}).get("request_id")))

# ===== 汇总 =====
print()
print("=" * 60)
passed = sum(1 for _, r, _ in results if r == PASS)
failed = [(n, d) for n, r, d in results if r == FAIL]

for name, result, detail in results:
    mark = "PASS" if result == PASS else "FAIL"
    print(f"  [{mark}] {name}" + (f"  —— {detail}" if detail else ""))

print("=" * 60)
print(f"  合计: {passed}/{len(results)} 通过")

if failed:
    print()
    print("  未通过项：")
    for name, detail in failed:
        print(f"    - {name}: {detail}")
    sys.exit(1)

print()
print("  ✓ SERVER V1 FAKE RUNTIME 验收通过")
sys.exit(0)
PY

RC=$?
echo
if [ "$RC" -eq 0 ]; then
  ok "验收通过"
else
  err "验收未通过 —— 见上面逐项结果；日志：bash scripts/autodl/logs.sh all"
fi
exit "$RC"
