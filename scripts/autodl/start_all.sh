#!/usr/bin/env bash
# 按正确顺序启动 API 与 Research Worker。
#
#     bash scripts/autodl/start_all.sh
#
# 顺序：Redis → 检查 DB → API → Worker

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

section "0. 前置检查"
[ -x "${VENV_DIR}/bin/python" ] || die "未安装依赖，请先运行：bash scripts/autodl/setup.sh"
[ -f "${PROJECT_ROOT}/config.yml" ] || die "缺少 config.yml，请先运行：bash scripts/autodl/setup.sh"
[ -f "${PROJECT_ROOT}/data/tasks.db" ] || die "数据库未初始化，请先运行：bash scripts/autodl/init_db.sh"
ok "前置文件齐全"

section "1. Redis"
if ! bash "${PROJECT_ROOT}/scripts/autodl/start_redis.sh"; then
  die "Redis 未就绪 —— 它是硬依赖，无法继续"
fi

section "2. API"
bash "${PROJECT_ROOT}/scripts/autodl/start_api.sh" || die "API 启动失败"

section "3. Worker"
bash "${PROJECT_ROOT}/scripts/autodl/start_worker.sh" || die "Worker 启动失败"

section "完成"
bash "${PROJECT_ROOT}/scripts/autodl/status.sh"
