#!/usr/bin/env bash
# 环境检查：只读，不安装、不修改任何内容。
#
#     bash scripts/autodl/check_env.sh

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

FAIL=0
note_fail() { FAIL=$((FAIL + 1)); }

section "1. 基本信息"
info "主机名    : $(hostname 2>/dev/null || echo 未知)"
info "当前用户  : $(whoami 2>/dev/null || echo 未知)"
info "项目根目录: ${PROJECT_ROOT}"
info "系统      : $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME}" || uname -s)"
info "内核      : $(uname -r 2>/dev/null || echo 未知)"
info "当前时间  : $(date '+%Y-%m-%d %H:%M:%S %z' 2>/dev/null)"

section "2. Python"
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo '?')"
  info "python3    : $(command -v python3)"
  info "版本       : ${PY_VER}"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    ok "Python 版本满足要求（>= 3.12）"
  else
    err "Python 版本过低（需要 >= 3.12），当前 ${PY_VER}"
    note_fail
  fi
else
  err "未找到 python3"
  note_fail
fi

if python3 -c 'import venv' 2>/dev/null; then
  ok "venv 模块可用"
else
  warn "python3-venv 可能未安装（Debian/Ubuntu 上是 python3-venv 包）"
fi

section "3. Redis（硬依赖）"
REDIS_READY=0
if command -v redis-cli >/dev/null 2>&1; then
  if redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
    ok "redis-cli 可用，且 127.0.0.1:6379 有响应（PONG）"
    REDIS_READY=1
  else
    warn "redis-cli 存在，但 127.0.0.1:6379 无响应（服务可能尚未启动）"
  fi
else
  warn "未找到 redis-cli"
fi

if [ "$REDIS_READY" -eq 1 ]; then
  if redis-cli -h 127.0.0.1 -p 6379 MODULE LIST 2>/dev/null | grep -qiE "search|json"; then
    ok "检测到 Redis Stack 模块（RediSearch / RedisJSON）"
  else
    err "该 Redis 缺少 RediSearch/RedisJSON 模块（不是 Redis Stack）"
    note_fail
  fi
else
  info "Redis 尚未就绪 —— 可稍后用 scripts/autodl/start_redis.sh 启动"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "Docker 可用（可用于运行 Redis Stack 或完整 Compose）"
else
  info "Docker 不可用 —— AutoDL 脚本仍可按直接进程模式运行"
fi

section "4. GPU（Fake Runtime 不需要）"
if command -v nvidia-smi >/dev/null 2>&1; then
  info "nvidia-smi 存在"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/    /' || true
else
  info "无 nvidia-smi —— Fake Provider 模式不受影响"
fi

section "5. 磁盘与端口"
DISK_AVAIL="$(df -h "$PROJECT_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')"
info "项目所在分区可用空间: ${DISK_AVAIL:-未知}"
for p in "${API_PORT:-8000}" 6379; do
  if port_in_use "$p"; then
    warn "端口 ${p} 已被占用"
  else
    ok "端口 ${p} 空闲"
  fi
done

section "6. 项目文件完整性"
for f in requirements.txt alembic.ini pytest.ini config.server.example.yml .env.example \
         backend/main.py backend/worker.py migrations/env.py; do
  if [ -e "${PROJECT_ROOT}/${f}" ]; then
    ok "${f}"
  else
    err "缺少 ${f}"
    note_fail
  fi
done

section "检查结果"
if [ "$FAIL" -eq 0 ]; then
  ok "全部必需项通过"
  echo
  echo "下一步：bash scripts/autodl/setup.sh"
  exit 0
fi

err "有 ${FAIL} 项必需检查未通过，请先解决再继续"
echo
 echo "（Redis 尚未启动本身不算失败 —— 它由 start_redis.sh 负责）"
exit 1
