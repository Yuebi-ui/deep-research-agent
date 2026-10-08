#!/usr/bin/env bash
# 安装依赖 —— 服务器上的**第二步**。
#
#     bash scripts/autodl/setup.sh
#
# 做的事：
#   1. 创建 .env.server（若不存在，从模板复制）
#   2. 创建 config.yml（若不存在，从服务器模板复制）
#   3. 建 Python 虚拟环境并安装依赖
#   4. 安装前端依赖并生成 API 类型
#
# 不做的事：不启动任何服务、不改动任何已存在的配置文件。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

section "1. 准备配置文件"

if [ -f "${ENV_FILE}" ]; then
  ok ".env.server 已存在，保持不变"
else
  cp "${PROJECT_ROOT}/.env.server.example" "${ENV_FILE}"
  ok "已创建 .env.server（来自模板）"
fi

if [ -f "${PROJECT_ROOT}/config.yml" ]; then
  ok "config.yml 已存在，保持不变"
else
  cp "${PROJECT_ROOT}/config.server.example.yml" "${PROJECT_ROOT}/config.yml"
  ok "已创建 config.yml（来自服务器模板，**不含任何密钥**）"
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
info "这一步可能几分钟（会装 langgraph / chromadb 等）"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r "${PROJECT_ROOT}/requirements.txt" -q || die "Python 依赖安装失败"
ok "运行时依赖已安装"

"$PY" -m pip install -r "${PROJECT_ROOT}/requirements-dev.txt" -q || die "开发依赖安装失败"
ok "开发依赖（pytest / ruff）已安装"

section "4. 安装前端依赖（可选）"
# 前端对 Fake Runtime 验收**不是必需的**（AutoDL 一般不开放 3000 端口，
# 用不到 Web UI）。因此这一步失败只警告、不中断部署。
FRONTEND_OK=0
if [ ! -f "${PROJECT_ROOT}/frontend/package.json" ]; then
  warn "当前仓库快照不包含 frontend —— 跳过前端依赖"
elif ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  warn "未安装 node/npm —— 跳过前端"
  warn "  这不影响 Server Runtime 验收（API / Worker / SSE 都不依赖前端）"
else
  cd "${PROJECT_ROOT}/frontend"
  if [ -d node_modules ]; then
    ok "node_modules 已存在，跳过 npm ci"
    FRONTEND_OK=1
  elif npm ci --no-audit --no-fund 2>&1 | tail -n 3; then
    ok "前端依赖已安装"
    FRONTEND_OK=1
  else
    warn "npm ci 失败 —— 继续部署（前端可选）"
  fi

  if [ "$FRONTEND_OK" -eq 1 ]; then
    section "5. 生成 API 类型（验证前后端契约一致）"
    if npm run api:types >/dev/null 2>&1; then
      ok "已从 openapi.json 生成 TS 类型"
    else
      warn "npm run api:types 失败（不阻塞部署，但前端类型可能过期）"
    fi
  fi
fi

cd "${PROJECT_ROOT}"

section "完成"
cat <<'EOF'
下一步：

  1. 启动 Redis Stack      bash scripts/autodl/start_redis.sh
  2. 初始化数据库           bash scripts/autodl/init_db.sh
  3. 启动全部服务           bash scripts/autodl/start_all.sh
  4. 运行 Fake 验收         bash scripts/autodl/accept_fake_runtime.sh
EOF
