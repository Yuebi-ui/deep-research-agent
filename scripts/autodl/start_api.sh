#!/usr/bin/env bash
# 启动 API 服务。
#
#     bash scripts/autodl/start_api.sh
#
# API 只做命令 / 查询 / 事件投影，**不构建 LangGraph、不持有 checkpointer**
# （执行归 Worker）。因此它启动快、内存占用小。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env
require_venv

PY="$(python_bin)"
export APP_ENV CONFIG_PATH CHECKPOINTER_BACKEND

if port_in_use "$API_PORT"; then
  warn "端口 ${API_PORT} 已被占用 —— 可能 API 已在运行"
  info "查看状态：bash scripts/autodl/status.sh"
  exit 1
fi

section "启动 API（端口 ${API_PORT}）"
info "APP_ENV=${APP_ENV}  CHECKPOINTER_BACKEND=${CHECKPOINTER_BACKEND}"
info "CONFIG_PATH=${CONFIG_PATH}"

start_daemon "api" "${LOG_DIR}/api.log" "uvicorn" \
  "$PY" -m uvicorn backend.main:app \
    --host 0.0.0.0 --port "$API_PORT" --workers 1

wait_for_http "http://127.0.0.1:${API_PORT}/api/health" "API" 60 \
  || die "API 未就绪，请查看 ${LOG_DIR}/api.log"

# 顺手把 health 打出来，便于确认各组件状态
if command -v curl >/dev/null 2>&1; then
  echo
  info "健康检查："
  curl -fsS "http://127.0.0.1:${API_PORT}/api/health" | sed 's/^/    /' || true
  echo
fi
