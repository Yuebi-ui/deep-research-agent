#!/usr/bin/env bash
# 安装服务端依赖。
#
#     bash scripts/autodl/setup.sh
#
# 做的事：
#   1. 创建 .env.server（若不存在，从 .env.example 复制）
#   2. 创建 config.yml（若不存在，从服务器模板复制）
#   3. 建 Python 虚拟环境并安装运行/开发依赖
#
# 不启动任何服务，也不会覆盖已有配置。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

section "1. 准备配置文件"

if [ -f "${ENV_FILE}" ]; then
  ok ".env.server 已存在，保持不变"
else
  cp "${PROJECT_ROOT}/.env.example" "${ENV_FILE}"
  # .env.example 面向离线开发（sqlite checkpoint）；服务器 Worker 使用 Redis
  # checkpoint 才能保持与 config.server.example.yml 的跨进程恢复配置一致。
  sed -i 's/^CHECKPOINTER_BACKEND=.*/CHECKPOINTER_BACKEND=redis/' "${ENV_FILE}"
  ok "已创建 .env.server（Fake Provider + Redis checkpoint，来自 .env.example）"
fi

if [ -f "${PROJECT_ROOT}/config.yml" ]; then
  ok "config.yml 已存在，保持不变"
else
  cp "${PROJECT_ROOT}/config.server.example.yml" "${PROJECT_ROOT}/config.yml"
  ok "已创建 config.yml（来自服务器模板，不含任何密钥）"
fi

load_env

section "2. Python 虚拟环境"
if [ -x "${VENV_DIR}/bin/python" ]; then
  ok "虚拟环境已存在：${VENV_DIR}"
else
  info "创建虚拟环境…"
  python3 -m venv "$VENV_DIR" || die "创建虚拟环境失败（Debian/Ubuntu 可能需要 apt install python3-venv）"
  ok "已创建 ${VENV_DIR}"
fi

PY="$(python_bin)"
info "使用解释器：$PY"
info "版本：$("$PY" -c 'import sys; print(sys.version.split()[0])')"

section "3. 安装 Python 依赖"
info "这一步可能需要几分钟（会安装 LangGraph、ChromaDB 等）"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r "${PROJECT_ROOT}/requirements.txt" -q || die "Python 运行依赖安装失败"
"$PY" -m pip install -r "${PROJECT_ROOT}/requirements-dev.txt" -q || die "开发依赖安装失败"
ok "Python 依赖安装完成"

section "完成"
cat <<'EOF2'
下一步：

  1. 启动 Redis Stack      bash scripts/autodl/start_redis.sh
  2. 初始化数据库          bash scripts/autodl/init_db.sh
  3. 启动 API + Worker     bash scripts/autodl/start_all.sh
  4. 运行 Fake 验收        bash scripts/autodl/accept_fake_runtime.sh
EOF2
