#!/usr/bin/env bash
# 停止本地 vLLM Model Service。
#
#     bash scripts/model-service/stop_vllm.sh
#
# 先 SIGTERM 优雅退出（vLLM 需要几秒释放 GPU 显存），超时后 SIGKILL。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/model-service/_common.sh
. ./_common.sh

STOP_GRACE="${STOP_GRACE:-60}"

if ! is_running; then
  if [ -f "$VLLM_PID_FILE" ]; then
    warn "vLLM 未在运行（pidfile 已失效），清理"
    rm -f "$VLLM_PID_FILE"
  else
    info "vLLM 未在运行"
  fi
  exit 0
fi

pid="$(running_pid)"
info "停止 vLLM（pid ${pid}）…"
kill -TERM "$pid" 2>/dev/null || true

waited=0
while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$STOP_GRACE" ]; do
  sleep 1; waited=$((waited + 1))
done

if kill -0 "$pid" 2>/dev/null; then
  warn "未在 ${STOP_GRACE}s 内退出，发送 SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
fi

rm -f "$VLLM_PID_FILE"
ok "vLLM 已停止"
