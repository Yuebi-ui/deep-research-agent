#!/usr/bin/env bash
# 启动 Worker（**唯一正式的 LangGraph 执行者**）。
#
#     bash scripts/autodl/start_worker.sh
#
# Worker 会：
#   * 拒绝 memory checkpointer（跨进程恢复的前提）
#   * 校验 DB schema 版本
#   * 消费 Redis job 队列
#   * 周期扫描孤儿任务

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env
require_venv

PY="$(python_bin)"
export APP_ENV CONFIG_PATH CHECKPOINTER_BACKEND

section "启动 Worker"
info "APP_ENV=${APP_ENV}  CHECKPOINTER_BACKEND=${CHECKPOINTER_BACKEND}"

if [ "${CHECKPOINTER_BACKEND}" = "memory" ]; then
  die "CHECKPOINTER_BACKEND=memory 不被 Worker 接受。
跨进程恢复是 Server V1 的核心能力，用 memory 会让它失效。
请改为 redis（或 sqlite）。"
fi

start_daemon "worker" "${LOG_DIR}/worker.log" "backend.worker" \
  "$PY" -m backend.worker

# Worker 启动是"连上 Redis 并开始消费"，没有 HTTP 端点，
# 因此这里用日志里的一行作为就绪信号。
waited=0
while [ "$waited" -lt 40 ]; do
  if grep -q "worker 就绪" "${LOG_DIR}/worker.log" 2>/dev/null; then
    ok "Worker 就绪（${waited}s）"
    exit 0
  fi
  if ! is_running worker; then
    err "Worker 已退出，日志尾部："
    tail -n 20 "${LOG_DIR}/worker.log" 2>/dev/null | sed 's/^/    /'
    exit 1
  fi
  sleep 2; waited=$((waited + 2)); printf '.'
done
printf '\n'
warn "未在 40s 内看到就绪日志，请查看 ${LOG_DIR}/worker.log"
