#!/usr/bin/env bash
# 停止服务。
#
#     bash scripts/autodl/stop.sh              # 停 API + Worker + 前端
#     bash scripts/autodl/stop.sh --all        # 连 Redis 一起停
#
# Worker 收到 SIGTERM 后会走**优雅退出**：跑到下一个安全边界、停止心跳，
# 并**刻意不释放 claim**（避免另一个 Worker 立刻接管并重复执行当前节点）。
# 因此停止 Worker 后，那个任务的 claim 会自然过期，这是预期行为。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

STOP_REDIS=0
[ "${1:-}" = "--all" ] && STOP_REDIS=1

section "停止服务"

# 顺序：先停 API（无状态），再停 Worker（等它优雅退出）
stop_daemon "api" 10
stop_daemon "frontend" 10
stop_daemon "worker" 30   # 给 Worker 更长的优雅退出时间

if [ "$STOP_REDIS" -eq 1 ]; then
  stop_daemon "redis" 10
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "dr-redis-stack"; then
    info "停止 Redis 容器 dr-redis-stack…"
    docker stop dr-redis-stack >/dev/null && ok "容器已停止"
  fi
fi

section "完成"
info "Redis 数据保留在 data/redis/，任务数据保留在 data/tasks.db"
