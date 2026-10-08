#!/usr/bin/env bash
# Model Service（本地 vLLM）脚本共用函数。由其他脚本 source，不单独执行。
#
# 目录约定（AutoDL 数据盘，本机挂载点为 /root/autodl-tmp）：
#   /root/autodl-tmp/
#   ├── models/Qwen3-30B-A3B-GPTQ-Int4     模型权重（~18 GB）
#   ├── huggingface/                       HF cache（HF_HOME，避免占系统盘）
#   ├── vllm-env/                          独立 vLLM 虚拟环境（与 app .venv 隔离）
#   └── logs/vllm/                         vLLM 日志与 pid
#
# 所有路径与参数都可通过环境变量覆盖（见下方默认值）。

set -euo pipefail

MODEL_SERVICE_ROOT="${MODEL_SERVICE_ROOT:-/root/autodl-tmp}"
VLLM_VENV="${VLLM_VENV:-${MODEL_SERVICE_ROOT}/vllm-env}"
VLLM_MODEL_PATH="${VLLM_MODEL_PATH:-${MODEL_SERVICE_ROOT}/models/Qwen3-30B-A3B-GPTQ-Int4}"
VLLM_LOG_DIR="${VLLM_LOG_DIR:-${MODEL_SERVICE_ROOT}/logs/vllm}"

VLLM_HOST="${VLLM_HOST:-127.0.0.1}"
VLLM_PORT="${VLLM_PORT:-8001}"
# 必须与 config.hybrid.example.yml 中 roles 的 handle 一致
VLLM_SERVED_NAME="${VLLM_SERVED_NAME:-qwen3-30b-a3b-local}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.85}"
# 启动等待上限（秒）：首次加载 GPTQ 模型 + 编译 kernel，4090D 上数分钟属正常
VLLM_STARTUP_TIMEOUT="${VLLM_STARTUP_TIMEOUT:-600}"

VLLM_LOG_FILE="${VLLM_LOG_DIR}/serve.log"
VLLM_PID_FILE="${VLLM_LOG_DIR}/vllm.pid"

mkdir -p "$VLLM_LOG_DIR"

# ===== 颜色与输出 =====
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

# ===== 进程管理 =====
# pidfile 两行格式：<pid> / <marker>。marker 用于防止 PID 复用误判，
# 与 scripts/autodl/_common.sh 的做法一致。

read_pidfile() {
  [ -f "$VLLM_PID_FILE" ] || return 1
  local pid marker
  pid="$(sed -n '1p' "$VLLM_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
  marker="$(sed -n '2p' "$VLLM_PID_FILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  printf '%s %s\n' "$pid" "$marker"
}

is_running() {
  local line pid marker
  line="$(read_pidfile)" || return 1
  pid="${line%% *}"; marker="${line#* }"
  [ "$marker" = "$pid" ] && marker=""
  kill -0 "$pid" 2>/dev/null || return 1
  [ -z "$marker" ] || grep -qa -- "$marker" "/proc/$pid/cmdline" 2>/dev/null || return 1
  return 0
}

running_pid() {
  local line; line="$(read_pidfile)" || return 1
  echo "${line%% *}"
}

# ===== HTTP 探活 =====
http_ok() {
  local url="$1" timeout="${2:-3}"
  curl -fsS --max-time "$timeout" "$url" >/dev/null 2>&1
}

wait_for_http() {
  # wait_for_http <url> <描述> [最大等待秒]
  local url="$1" label="$2" max="${3:-120}" waited=0
  while [ "$waited" -lt "$max" ]; do
    if http_ok "$url"; then
      ok "$label 就绪（${waited}s）"
      return 0
    fi
    # 进程提前退出就没必要继续等
    if ! is_running; then
      err "$label 进程已退出（见 ${VLLM_LOG_FILE}）"
      return 1
    fi
    sleep 3; waited=$((waited + 3))
    printf '.'
  done
  printf '\n'
  err "$label 在 ${max}s 内未就绪"
  return 1
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${port} "
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ":${port} "
  else
    return 1
  fi
}
