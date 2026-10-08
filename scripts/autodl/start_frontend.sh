#!/usr/bin/env bash
# 构建并启动前端。
#
#     bash scripts/autodl/start_frontend.sh
#
# 说明：AutoDL 实例通常不对外开放 3000 端口，你多半用不到 Web UI。
# 这个脚本存在的意义是**验证前端能构建通过**（部署完整性的证据）。
# 如果只是想验收 Runtime，用 accept_fake_runtime.sh 即可，不需要前端。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env

[ -f "${PROJECT_ROOT}/frontend/package.json" ] || die "当前仓库快照不包含 frontend；请挂载/检出独立 UI 客户端后再使用此脚本"
cd "${PROJECT_ROOT}/frontend"

section "1. 构建前端"
info "NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE:-http://127.0.0.1:8000/api}"
export NEXT_PUBLIC_API_BASE="${NEXT_PUBLIC_API_BASE:-http://127.0.0.1:8000/api}"

if npm run build 2>&1 | tail -n 8 | sed 's/^/    /'; then
  ok "前端构建通过"
else
  die "前端构建失败 —— 查看上面的输出"
fi

section "2. 启动前端"
if port_in_use "${FRONTEND_PORT}"; then
  warn "端口 ${FRONTEND_PORT} 已占用"
  exit 1
fi

cd "${PROJECT_ROOT}"
start_daemon "frontend" "${LOG_DIR}/frontend.log" "next" \
  npm --prefix "${PROJECT_ROOT}/frontend" run start -- --port "${FRONTEND_PORT}"

wait_for_http "http://127.0.0.1:${FRONTEND_PORT}" "前端" 60 \
  || warn "前端未就绪，但它不影响 Runtime 验收"
