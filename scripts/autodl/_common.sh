#!/usr/bin/env bash
# 部署脚本共用函数。由其他脚本 source，不单独执行。
#
# 设计前提：AutoDL 普通实例**不一定支持 nested Docker**，
# 因此全部脚本都按"直接跑进程"设计，不依赖 Docker。

set -euo pipefail

# ===== 路径 =====
# PROJECT_ROOT 由调用方脚本设置，或从本文件位置推断：
#   <project>/scripts/autodl/_common.sh  →  <project>
if [ -z "${PROJECT_ROOT:-}" ]; then
  _THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PROJECT_ROOT="$(cd "${_THIS_DIR}/../.." && pwd)"
fi
export PROJECT_ROOT

RUN_DIR="${PROJECT_ROOT}/.run"
LOG_DIR="${PROJECT_ROOT}/logs"
VENV_DIR="${PROJECT_ROOT}/.venv"
ENV_FILE="${PROJECT_ROOT}/.env.server"

mkdir -p "$RUN_DIR" "$LOG_DIR"

# ===== 颜色 =====
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_OFF=""
fi

ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()  { printf '%s✗%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
info() { printf '%s·%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
die()  { err "$*"; exit 1; }

section() {
  printf '\n%s==== %s ====%s\n' "$C_BLUE" "$*" "$C_OFF"
}

# ===== 环境文件 =====
load_env() {
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
  else
    warn "未找到 ${ENV_FILE}（可先 cp .env.example .env.server）"
  fi

  # 默认值（可被 .env.server 覆盖）
  export APP_ENV="${APP_ENV:-test}"
  export ALLOW_LIVE_EXTERNAL_APIS="${ALLOW_LIVE_EXTERNAL_APIS:-false}"
  export CONFIG_PATH="${CONFIG_PATH:-config.yml}"
  export CHECKPOINTER_BACKEND="${CHECKPOINTER_BACKEND:-redis}"
  export API_PORT="${API_PORT:-8000}"

  # 相对路径按项目根解析（应用侧也是这么做的）
  case "$CONFIG_PATH" in
    /*) : ;;
    *) export CONFIG_PATH="${PROJECT_ROOT}/${CONFIG_PATH}" ;;
  esac
}

# ===== Python =====
python_bin() {
  if [ -x "${VENV_DIR}/bin/python" ]; then
    echo "${VENV_DIR}/bin/python"
  else
    echo "python3"
  fi
}

require_venv() {
  [ -x "${VENV_DIR}/bin/python" ] || die "虚拟环境不存在：${VENV_DIR}
请先运行：bash scripts/autodl/setup.sh"
}

# ===== 进程管理 =====
# 每个服务一个 pidfile，放在 .run/ 下（已 gitignore）
pid_file()  { echo "${RUN_DIR}/$1.pid"; }
sock_file() { echo "${RUN_DIR}/$1.sock"; }

# pidfile 格式（两行）：
#     <pid>
#     <marker>      ← 进程命令行里必然出现的一段，用于防止 PID 复用误判
#
# 只检查 kill -0 是不够的：机器重启后 PID 会被重新分配，残留 pidfile 里
# 的 PID 可能指向一个**完全无关**的进程。那样会导致
#   * is_running 误判"已在运行" → 服务根本不会被启动
#   * stop_daemon 会去杀那个无关进程
# 因此额外校验 /proc/<pid>/cmdline 是否含 marker。

read_pidfile() {
  # 输出 "<pid> <marker>"；文件不存在或格式异常时输出空
  local name="$1" pf
  pf="$(pid_file "$name")"
  [ -f "$pf" ] || return 1
  local pid marker
  pid="$(sed -n '1p' "$pf" 2>/dev/null | tr -d '[:space:]')"
  marker="$(sed -n '2p' "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  printf '%s %s
' "$pid" "$marker"
}

_process_matches() {
  # _process_matches <pid> <marker>
  local pid="$1" marker="$2"
  [ -n "$marker" ] || return 0          # 没记 marker 就退回旧行为
  [ -r "/proc/$pid/cmdline" ] || return 1
  grep -qa -- "$marker" "/proc/$pid/cmdline" 2>/dev/null
}

is_running() {
  local name="$1" line pid marker
  line="$(read_pidfile "$name")" || return 1
  pid="${line%% *}"
  marker="${line#* }"
  [ "$marker" = "$pid" ] && marker=""    # 只有一行（旧格式）

  kill -0 "$pid" 2>/dev/null || return 1
  _process_matches "$pid" "$marker"
}

start_daemon() {
  # start_daemon <name> <logfile> <marker> <命令...>
  #   marker 是命令行的特征串（如 backend.worker），用于防 PID 复用误判
  local name="$1" logfile="$2" marker="$3"; shift 3
  if is_running "$name"; then
    warn "$name 已在运行（pid $(read_pidfile "$name" | cut -d' ' -f1)），跳过"
    return 0
  fi

  nohup "$@" >> "$logfile" 2>&1 &
  local pid=$!
  printf '%s
%s
' "$pid" "$marker" > "$(pid_file "$name")"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    ok "$name 已启动（pid ${pid}，日志 ${logfile}）"
  else
    err "$name 启动后立即退出，请查看 ${logfile}"
    tail -n 20 "$logfile" 2>/dev/null || true
    return 1
  fi
}

stop_daemon() {
  # stop_daemon <name> [grace_seconds]
  local name="$1" grace="${2:-10}" pf pid
  pf="$(pid_file "$name")"

  if [ ! -f "$pf" ]; then
    info "$name 未在运行（无 pidfile）"
    return 0
  fi

  local line
  line="$(read_pidfile "$name")" || { info "$name 未在运行"; rm -f "$pf"; return 0; }
  pid="${line%% *}"
  local marker="${line#* }"
  [ "$marker" = "$pid" ] && marker=""

  if ! kill -0 "$pid" 2>/dev/null || ! _process_matches "$pid" "$marker"; then
    info "$name 未在运行（pidfile 已失效）"
    rm -f "$pf"
    return 0
  fi

  info "停止 $name（pid ${pid}）…"
  # 先 SIGTERM，让 Worker 有机会走优雅退出（跑到安全边界、停止心跳）
  kill -TERM "$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$grace" ]; do
    sleep 1; waited=$((waited + 1))
  done

  if kill -0 "$pid" 2>/dev/null; then
    warn "$name 未在 ${grace}s 内退出，发送 SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
  fi

  rm -f "$pf"
  ok "$name 已停止"
}

# ===== HTTP 探活 =====
http_ok() {
  # http_ok <url> [超时秒]
  local url="$1" timeout="${2:-3}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time "$timeout" "$url" >/dev/null 2>&1
  else
    python3 - "$url" "$timeout" <<'PY' 2>/dev/null
import sys, urllib.request
url, timeout = sys.argv[1], float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        sys.exit(0 if 200 <= r.status < 400 else 1)
except Exception:
    sys.exit(1)
PY
  fi
}

wait_for_http() {
  # wait_for_http <url> <描述> [最大等待秒]
  local url="$1" label="$2" max="${3:-60}" waited=0
  while [ "$waited" -lt "$max" ]; do
    if http_ok "$url"; then
      ok "$label 就绪（${waited}s）"
      return 0
    fi
    sleep 2; waited=$((waited + 2))
    printf '.'
  done
  printf '\n'
  err "$label 在 ${max}s 内未就绪"
  return 1
}

port_in_use() {
  # port_in_use <port>
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${port} "
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ":${port} "
  else
    python3 - "$port" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
    sys.exit(1)   # 能 bind → 未被占用
except OSError:
    sys.exit(0)   # 占用中
finally:
    s.close()
PY
  fi
}

export -f ok warn err info die section 2>/dev/null || true
