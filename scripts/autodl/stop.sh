#!/usr/bin/env bash
# 停止 API 与 Worker；--all 会同时停止 Redis。
#
#     bash scripts/autodl/stop.sh
#     bash scripts/autodl/stop.sh --all

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

STOP_REDIS=0
[ "${1:-}" = "--all" ] && STOP_REDIS=1

section "停止服务"
stop_daemon "api" 10
stop_daemon "worker" 30

if [ "$STOP_REDIS" -eq 1 ]; then
  stop_daemon "redis" 10
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "dr-redis-stack"; then
    info "停止 Redis 容器 dr-redis-stack…"
    docker stop dr-redis-stack >/dev/null && ok "容器已停止"
  fi
fi

section "完成"
info "Redis 数据保留在 data/redis/，任务数据保留在 config.yml 指定的 SQLite 路径"
